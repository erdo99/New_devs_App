import json
import redis.asyncio as redis
from typing import Dict, Any
import os

# Initialize Redis client (typically configured centrally).
redis_client = redis.Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/0"))

async def get_revenue_summary(property_id: str, tenant_id: str) -> Dict[str, Any]:
    """
    Fetches revenue summary, utilizing caching to improve performance.

    The cache key MUST include `tenant_id` because property identifiers are not
    globally unique across tenants (e.g. `prop-001` exists for both Sunset
    Properties and Ocean Rentals). Omitting the tenant from the key causes
    cross-tenant data leakage on cache hits.
    """
    if not tenant_id:
        raise ValueError("tenant_id is required for revenue summary lookups")

    cache_key = f"revenue:{tenant_id}:{property_id}"

    cached = await redis_client.get(cache_key)
    if cached:
        return json.loads(cached)

    from app.services.reservations import calculate_total_revenue

    result = await calculate_total_revenue(property_id, tenant_id)

    await redis_client.setex(cache_key, 300, json.dumps(result))

    return result
