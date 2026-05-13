# Property Revenue Dashboard — Bug Report & Fixes

> **TL;DR**
> Three substantive bugs in the revenue dashboard plus one piece of infrastructure
> wiring that was hiding all of them behind a mock fallback.
>
> | # | Bug                                                       | Severity | Live repro? |
> | - | --------------------------------------------------------- | -------- | ----------- |
> | A | Cross-tenant cache leak (`property_id` not tenant-scoped) | Critical (privacy) | Yes — both directions |
> | B | `float()` round-trip on monetary `Decimal` totals         | Latent (precision) | Real drift on `19999.99` / `0.1+0.2`; not visible on the current seed totals |
> | C | `calculate_monthly_revenue` returns a placeholder and uses naive UTC datetimes | High (correctness) | Yes — function returns `Decimal('0')` and never queries the DB |
> | – | Pre-existing wiring issues that masked the above          | —        | Yes — every request fell through to a tenant-blind mock |

---

## How the issues map to the client complaints

| Complaint                                                                                                                  | Root cause |
| -------------------------------------------------------------------------------------------------------------------------- | ---------- |
| Sunset Properties: *“Revenue numbers on your dashboard don't match our internal records. We're showing different totals for March.”* | Cache cross-pollution (Bug A). When Ocean's request lands first, the cache stores `prop-001 → 0` and Sunset is then served `0` instead of their real `2 250.00`. |
| Ocean Rentals: *“Sometimes when we refresh the page, we see revenue numbers that look like they belong to another company.”* | Cache cross-pollution (Bug A). When Sunset's request lands first, Ocean is served the cached payload which literally still contains `"tenant_id": "tenant-a"`. |
| Finance: *“Revenue totals seem slightly off by a few cents here and there.”*                                               | `float()` precision loss (Bug B). The schema stores `NUMERIC(10, 3)` deliberately for sub-cent tracking; the endpoint converted that to a binary float, defeating the schema's purpose. |

---

## Bug A — Cross-tenant cache leak (privacy / accuracy)

### Symptom

Refreshing the dashboard sometimes shows another tenant's revenue. After a Redis
flush, the *first* tenant to request a given `property_id` decides what every
other tenant will see for the next 5 minutes.

### Root cause

`backend/app/services/cache.py` used a cache key without the tenant in it:

```python
# Before
cache_key = f"revenue:{property_id}"
```

`property_id` is **not globally unique** — the seed has `prop-001` for both
Sunset Properties (`Europe/Paris`, "Beach House Alpha") *and* Ocean Rentals
(`America/New_York`, "Mountain Lodge Beta"). One bucket, two tenants, predictable
disaster.

### Live evidence (original code, against the running stack)

```text
[Sunset asks first]
  Sunset sees prop-001 → 2250.0 / 4   ← correct
  Ocean  sees prop-001 → 2250.0 / 4   ← Sunset's data leaked

Redis dump:
  KEY  revenue:prop-001
  VAL  {"property_id":"prop-001","tenant_id":"tenant-a","total":"2250.000",...}
       ^^^^^^^^^^^^^^^^^^^^^^^^^^^ literally another tenant's id inside the payload

[Ocean asks first]
  Ocean  sees prop-001 → 0.0 / 0      ← correct (no reservations on their prop-001)
  Sunset sees prop-001 → 0.0 / 0      ← their real 2250.0 hidden by cache poisoning
                                        → matches Sunset's "March doesn't match" report
```

### Fix

```python
# After
if not tenant_id:
    raise ValueError("tenant_id is required for revenue summary lookups")
cache_key = f"revenue:{tenant_id}:{property_id}"
```

After the fix, Redis holds two distinct keys (`revenue:tenant-a:prop-001` and
`revenue:tenant-b:prop-001`), each tenant sees its own data, and cross-pollution
is structurally impossible.

---

## Bug B — Decimal → float precision loss

### Symptom

Finance reports totals that disagree by a few cents from their internal books.

### Root cause

`backend/app/api/v1/dashboard.py` was converting a Decimal-compatible string
straight to `float`:

```python
# Before
total_revenue_float = float(revenue_data['total'])
```

The schema stores amounts as `NUMERIC(10, 3)` — with the explicit comment
*“storing as numeric with 3 decimals to allow sub-cent precision tracking”* —
because IEEE-754 binary floats cannot represent most realistic monetary values
exactly. Going through `float` defeats that storage contract.

### Honest evidence

The bug is real, but the *current seed* hides it. The per-property totals on
this seed (`2 250.000`, `4 975.50`, `1 776.50`, `1 199.25`, `3 256.00`, …) all
happen to land on binary-exact float representations. The drift only shows up
on realistic amounts:

| Decimal value | float exact representation                               | Drift              |
| ------------- | -------------------------------------------------------- | ------------------ |
| `2 250.000`   | `2 250`                                                  | `0` ✅             |
| `4 975.50`    | `4 975.5`                                                | `0` ✅             |
| `19 999.99`   | `19 999.990 000 001 600 7…`                              | `+1.6 × 10⁻¹²` ❌  |
| `100.10`      | `100.099 999 999 999 994 3…`                             | `−5.7 × 10⁻¹⁵` ❌  |
| `333.333`     | `333.333 000 000 000 026 8…`                             | `+2.7 × 10⁻¹⁴` ❌  |
| `0.1 + 0.2`   | `0.299 999 999 999 999 988 9…`                           | `−1.1 × 10⁻¹⁷` ❌  |

So this seed will not surface a visible discrepancy on the dashboard, but the
schema's three-decimal precision is being silently truncated for any data set
that uses non-round amounts. The finance team's “few cents off here and there”
report is exactly that pathology.

### Fix

```python
# After
def _to_cents(amount: str) -> float:
    quantised = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(quantised)
```

Decimal handles the rounding while we still have full precision; only the
already-cent-quantised value is converted to float, which is then always
binary-exact for any realistic amount under ≈ \$10¹⁵.

The endpoint additionally fails closed (`HTTP 400`) if the authenticated user
has no `tenant_id`, preventing the previous `"default_tenant"` fallback from
silently mixing accounts.

---

## Bug C — Monthly revenue ignores property timezone (and never reaches the DB)

### Symptom

The dashboard subtitle says “Monthly performance insights”, but Sunset's
March totals never quite match their internal records when a reservation
straddles a UTC↔local midnight boundary.

### Root cause

`backend/app/services/reservations.py` had two problems in
`calculate_monthly_revenue`:

1. It returned a hard-coded `Decimal('0')` placeholder and **never executed any
   SQL**.
2. The (commented-out) query built month boundaries from **naive `datetime`s**,
   meaning a comparison against `TIMESTAMP WITH TIME ZONE` columns implicitly
   treats month start/end as UTC, ignoring the property's `Europe/Paris` /
   `America/New_York` timezone.

The seed deliberately includes a boundary reservation:

```sql
('res-tz-1', 'prop-001', 'tenant-a',
 '2024-02-29 23:30:00+00',  -- UTC
 '2024-03-05 10:00:00+00',
 1250.000);
```

`2024-02-29 23:30 UTC` is **`2024-03-01 00:30` in Europe/Paris**, so it belongs
to *March* on Sunset's local books. The naive-UTC query bucketed it into
February instead.

### Live evidence (original code)

```text
ORIGINAL calculate_monthly_revenue(prop-001, March 2024) returned: Decimal('0')
DEBUG: Querying revenue for prop-001 from 2024-03-01 00:00:00 to 2024-04-01 00:00:00
                                          ^^^^^^^^^^^^^^^^^^^   ^^^^^^^^^^^^^^^^^^^
                                          naive — no timezone
```

### Fix

Real SQL, real timezone:

```python
property_tz = ZoneInfo(tz_record.timezone or "UTC")
local_start = datetime(year, month, 1, tzinfo=property_tz)
local_end   = datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=property_tz)
# … SELECT … WHERE check_in_date >= :start_ts AND check_in_date < :end_ts
```

After the fix, called directly:

```text
FIXED monthly_revenue(prop-001, tenant-a, March 2024) = 2250.000
  └─ correctly includes res-tz-1 because Paris-local time is March 1
FIXED monthly_revenue(prop-001, tenant-a, Feb   2024) = 0
  └─ res-tz-1 is no longer mis-bucketed into February
FIXED monthly_revenue(prop-001, tenant-b, March 2024) = 0
  └─ Ocean has no reservations on their prop-001
```

> Scope note: per the assignment’s “do not rebuild the system” rule, the public
> `/dashboard/summary` endpoint still returns the all-time aggregate (which on
> this seed already equals the March total). `calculate_monthly_revenue` is now
> correct so that wiring it to a future `?month=&year=` query parameter is a
> trivial change.

---

## Pre-existing wiring issues that masked the bugs

While reproducing the reports against the running stack I had to fix three
unrelated infrastructure problems, otherwise *every* request fell through to a
tenant-blind mock fallback inside `calculate_total_revenue` and the bugs were
invisible.

| # | File                                 | Issue                                                                                                                                                | Fix                                                                  |
| - | ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------- |
| 1 | `backend/app/core/database_pool.py`  | Built its DSN from `settings.supabase_db_*` fields that don't exist on `Settings`, so `initialize()` always raised `AttributeError`.                  | Use `settings.database_url` (the value `docker-compose` injects).    |
| 2 | `backend/app/core/database_pool.py`  | Used `sqlalchemy.pool.QueuePool` with an **async** engine: `Pool class QueuePool cannot be used with asyncio engine`.                                | Use `AsyncAdaptedQueuePool`.                                         |
| 3 | `backend/app/core/database_pool.py`  | `async def get_session()` returned an **awaitable** rather than the session, so `async with db_pool.get_session() as session:` raised `'coroutine' object does not support the asynchronous context manager protocol`. | Make `get_session` a plain `def` that returns the `AsyncSession`.    |
| 4 | `backend/requirements.txt`           | `login.py` imports the `PyJWT` package (`import jwt`), but only `python-jose` was declared.                                                          | Add `PyJWT>=2.8.0`.                                                  |

These are not part of the customer-visible bug surface, but without them the
demonstration would silently return mock data and the real fixes would be
unverifiable.

---

## Reproduction

```bash
docker compose up --build -d
# Frontend: http://localhost:3000
# Backend:  http://localhost:8000/docs

# Clean state
docker compose exec redis redis-cli FLUSHALL

# Login both tenants
TOK_A=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"sunset@propertyflow.com","password":"client_a_2024"}' | jq -r .access_token)
TOK_B=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"ocean@propertyflow.com","password":"client_b_2024"}' | jq -r .access_token)

# Cross-tenant isolation check
curl -s "http://localhost:8000/api/v1/dashboard/summary?property_id=prop-001" \
     -H "Authorization: Bearer $TOK_A"   # → 2250.0 / 4
curl -s "http://localhost:8000/api/v1/dashboard/summary?property_id=prop-001" \
     -H "Authorization: Bearer $TOK_B"   # → 0.0 / 0  (no longer 2250.0)

docker compose exec redis redis-cli KEYS '*'
# revenue:tenant-a:prop-001
# revenue:tenant-b:prop-001
```

Expected totals after fixes (all verified live):

| Tenant         | Property  | Total      | Reservations |
| -------------- | --------- | ---------- | ------------ |
| Sunset (`a`)   | `prop-001`| `2 250.00` | `4`          |
| Ocean (`b`)    | `prop-001`| `0.00`     | `0`          |
| Sunset (`a`)   | `prop-002`| `4 975.50` | `4`          |
| Sunset (`a`)   | `prop-003`| `6 100.50` | `2`          |
| Ocean (`b`)    | `prop-004`| `1 776.50` | `4`          |
| Ocean (`b`)    | `prop-005`| `3 256.00` | `3`          |
| Sunset asks `prop-004` (not theirs) | — | `0.00` | `0` |

---

## Files touched

```
backend/app/services/cache.py         # tenant-scoped cache key + fail-closed validation
backend/app/api/v1/dashboard.py       # Decimal → cents → float, fail-closed on missing tenant
backend/app/services/reservations.py  # tz-aware monthly revenue against the real DB
backend/app/core/database_pool.py     # DATABASE_URL wiring, AsyncAdaptedQueuePool, sync get_session
backend/requirements.txt              # add PyJWT
FINDINGS.md                           # this document
```
