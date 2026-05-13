from datetime import datetime
from decimal import Decimal
from typing import Dict, Any, List
from zoneinfo import ZoneInfo

async def calculate_monthly_revenue(
    property_id: str,
    tenant_id: str,
    month: int,
    year: int,
) -> Decimal:
    """
    Calculate revenue for a specific (year, month) for a single property.

    Month boundaries are computed in the *property's* local timezone and then
    converted to UTC for the comparison against `check_in_date` (stored as
    TIMESTAMP WITH TIME ZONE). This is what fixes the Sunset Properties
    "March doesn't match our records" report: reservation `res-tz-1` checks
    in at 2024-02-29 23:30 UTC which is 2024-03-01 00:30 in Europe/Paris,
    so it belongs to *March* in the property's local books.
    """
    from app.core.database_pool import DatabasePool
    from sqlalchemy import text

    db_pool = DatabasePool()
    await db_pool.initialize()

    if not db_pool.session_factory:
        raise RuntimeError("Database pool not available for monthly revenue calculation")

    async with db_pool.get_session() as session:
        tz_row = await session.execute(
            text(
                """
                SELECT timezone
                FROM properties
                WHERE id = :property_id AND tenant_id = :tenant_id
                """
            ),
            {"property_id": property_id, "tenant_id": tenant_id},
        )
        tz_record = tz_row.fetchone()
        if not tz_record:
            return Decimal("0.00")

        property_tz = ZoneInfo(tz_record.timezone or "UTC")

        local_start = datetime(year, month, 1, tzinfo=property_tz)
        if month == 12:
            local_end = datetime(year + 1, 1, 1, tzinfo=property_tz)
        else:
            local_end = datetime(year, month + 1, 1, tzinfo=property_tz)

        result = await session.execute(
            text(
                """
                SELECT COALESCE(SUM(total_amount), 0) AS total
                FROM reservations
                WHERE property_id = :property_id
                  AND tenant_id  = :tenant_id
                  AND check_in_date >= :start_ts
                  AND check_in_date <  :end_ts
                """
            ),
            {
                "property_id": property_id,
                "tenant_id": tenant_id,
                "start_ts": local_start,
                "end_ts": local_end,
            },
        )
        row = result.fetchone()
        return Decimal(str(row.total)) if row and row.total is not None else Decimal("0.00")

async def calculate_total_revenue(property_id: str, tenant_id: str) -> Dict[str, Any]:
    """
    Aggregates revenue from database.
    """
    try:
        # Import database pool
        from app.core.database_pool import DatabasePool
        
        # Initialize pool if needed
        db_pool = DatabasePool()
        await db_pool.initialize()
        
        if db_pool.session_factory:
            async with db_pool.get_session() as session:
                # Use SQLAlchemy text for raw SQL
                from sqlalchemy import text
                
                query = text("""
                    SELECT 
                        property_id,
                        SUM(total_amount) as total_revenue,
                        COUNT(*) as reservation_count
                    FROM reservations 
                    WHERE property_id = :property_id AND tenant_id = :tenant_id
                    GROUP BY property_id
                """)
                
                result = await session.execute(query, {
                    "property_id": property_id, 
                    "tenant_id": tenant_id
                })
                row = result.fetchone()
                
                if row:
                    total_revenue = Decimal(str(row.total_revenue))
                    return {
                        "property_id": property_id,
                        "tenant_id": tenant_id,
                        "total": str(total_revenue),
                        "currency": "USD", 
                        "count": row.reservation_count
                    }
                else:
                    # No reservations found for this property
                    return {
                        "property_id": property_id,
                        "tenant_id": tenant_id,
                        "total": "0.00",
                        "currency": "USD",
                        "count": 0
                    }
        else:
            raise Exception("Database pool not available")
            
    except Exception as e:
        print(f"Database error for {property_id} (tenant: {tenant_id}): {e}")
        
        # Create property-specific mock data for testing when DB is unavailable
        # This ensures each property shows different figures
        mock_data = {
            'prop-001': {'total': '1000.00', 'count': 3},
            'prop-002': {'total': '4975.50', 'count': 4}, 
            'prop-003': {'total': '6100.50', 'count': 2},
            'prop-004': {'total': '1776.50', 'count': 4},
            'prop-005': {'total': '3256.00', 'count': 3}
        }
        
        mock_property_data = mock_data.get(property_id, {'total': '0.00', 'count': 0})
        
        return {
            "property_id": property_id,
            "tenant_id": tenant_id, 
            "total": mock_property_data['total'],
            "currency": "USD",
            "count": mock_property_data['count']
        }
