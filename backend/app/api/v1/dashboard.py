from decimal import Decimal, ROUND_HALF_UP
from fastapi import APIRouter, Depends, HTTPException
from typing import Dict, Any
from app.services.cache import get_revenue_summary
from app.core.auth import authenticate_request as get_current_user

router = APIRouter()


def _to_cents(amount: str) -> float:
    """
    Convert a Decimal-compatible string to a cent-precise float.

    Doing the rounding in Decimal first (NUMERIC(10, 3) -> cents) eliminates
    the IEEE-754 drift that caused the "few cents off" reports from finance.
    Once a value is quantised to two decimal places, its float representation
    is exact for any realistic monetary amount, so the JSON contract (number)
    that the frontend depends on stays intact.
    """
    quantised = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(quantised)


@router.get("/dashboard/summary")
async def get_dashboard_summary(
    property_id: str,
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    tenant_id = getattr(current_user, "tenant_id", None)
    if not tenant_id:
        raise HTTPException(status_code=400, detail="Tenant context missing for current user")

    revenue_data = await get_revenue_summary(property_id, tenant_id)

    return {
        "property_id": revenue_data["property_id"],
        "total_revenue": _to_cents(revenue_data["total"]),
        "currency": revenue_data["currency"],
        "reservations_count": revenue_data["count"],
    }
