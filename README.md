# Property Revenue Dashboard — Debug Challenge Submission

Submission for the [`ASSIGNMENT.md`](./ASSIGNMENT.md) debugging exercise: three
customer-reported issues on a multi-tenant revenue dashboard, identified,
fixed, and verified live against the dockerised stack.

- 📺 **Video walkthrough:** [Submission walkthrough (videos)](https://github.com/erdo99/New_devs_App/releases/tag/v1.0-submission)
  - Part 1 — Bug A (cross-tenant cache leak): reproduction + fix
  - Part 2 — Bug B (Decimal/float precision) and Bug C (property timezone)
- 📝 **Detailed write-up:** [`FINDINGS.md`](./FINDINGS.md)

---

## TL;DR — what was broken

| # | Bug                                                                  | File                                  | Customer report it explains |
| - | -------------------------------------------------------------------- | ------------------------------------- | --------------------------- |
| A | Revenue cache key was not tenant-scoped (`revenue:{property_id}`)   | `backend/app/services/cache.py`       | Ocean: *"numbers belong to another company"* / Sunset: *"March doesn't match"* |
| B | `Decimal` → `float` round-trip on monetary totals                   | `backend/app/api/v1/dashboard.py`     | Finance: *"a few cents off here and there"* |
| C | `calculate_monthly_revenue` returned `Decimal('0')` and ignored property timezone | `backend/app/services/reservations.py` | Sunset's Paris-local March boundary report |

Plus four pre-existing infra issues that were silently routing **every**
request to a tenant-blind mock fallback and hiding all three bugs above
(`DatabasePool` wiring, async pool class, `get_session` protocol,
missing `PyJWT`). See [FINDINGS.md](./FINDINGS.md#pre-existing-wiring-issues-that-masked-the-bugs) for the full list.

## Live evidence

Bug A — same `property_id`, two tenants, original code:

```text
[Redis flushed, Sunset asks first]
  Sunset → 2250.0 / 4   ← correct
  Ocean  → 2250.0 / 4   ← Sunset's data, leaked

Cached payload:
  KEY  revenue:prop-001
  VAL  {"property_id":"prop-001","tenant_id":"tenant-a","total":"2250.000",...}
                                ^^^^^^^^^^^^^^^^^^^^^^^ another tenant's id, served to Ocean
```

Bug C — direct invocation of the original function:

```text
ORIGINAL calculate_monthly_revenue(prop-001, March 2024) → Decimal('0')
DEBUG: Querying revenue for prop-001 from 2024-03-01 00:00:00 to 2024-04-01 00:00:00
                                          ↑ naive datetime — Europe/Paris timezone ignored
```

After the fixes, both behaviours are gone:

```text
Sunset prop-001 → 2250.0 / 4   (their own data, includes the Paris-March boundary reservation)
Ocean  prop-001 → 0.0  / 0     (their own prop-001 has no reservations)
Redis  keys     → revenue:tenant-a:prop-001
                  revenue:tenant-b:prop-001    ← per-tenant isolation
```

## How to run / verify

```bash
docker compose up --build -d
# Frontend: http://localhost:3000
# Backend:  http://localhost:8000/docs
```

Login credentials (provided by the assignment):

| Tenant              | Email                       | Password         |
| ------------------- | --------------------------- | ---------------- |
| Sunset Properties   | `sunset@propertyflow.com`   | `client_a_2024`  |
| Ocean Rentals       | `ocean@propertyflow.com`    | `client_b_2024`  |

Quick smoke test (Bash):

```bash
docker compose exec redis redis-cli FLUSHALL

TOK_A=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"sunset@propertyflow.com","password":"client_a_2024"}' | jq -r .access_token)
TOK_B=$(curl -s -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"ocean@propertyflow.com","password":"client_b_2024"}' | jq -r .access_token)

curl -s "http://localhost:8000/api/v1/dashboard/summary?property_id=prop-001" \
     -H "Authorization: Bearer $TOK_A"   # → total_revenue 2250.0, count 4
curl -s "http://localhost:8000/api/v1/dashboard/summary?property_id=prop-001" \
     -H "Authorization: Bearer $TOK_B"   # → total_revenue 0.0,    count 0

docker compose exec redis redis-cli KEYS '*'
# revenue:tenant-a:prop-001
# revenue:tenant-b:prop-001
```

Expected per-tenant totals (every value verified against the running stack):

| Tenant         | Property   | Total      | Reservations |
| -------------- | ---------- | ---------- | ------------ |
| Sunset (`a`)   | `prop-001` | `2 250.00` | `4`          |
| Sunset (`a`)   | `prop-002` | `4 975.50` | `4`          |
| Sunset (`a`)   | `prop-003` | `6 100.50` | `2`          |
| Ocean (`b`)    | `prop-001` | `0.00`     | `0`          |
| Ocean (`b`)    | `prop-004` | `1 776.50` | `4`          |
| Ocean (`b`)    | `prop-005` | `3 256.00` | `3`          |
| Sunset asks Ocean's `prop-004` | — | `0.00` | `0` (tenant isolation) |

## Commit map

| Commit                                                                        | Scope                                                                     |
| ----------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| `fix(infra): wire DatabasePool to local Postgres and fix async protocol`     | `DatabasePool` DSN, `AsyncAdaptedQueuePool`, sync `get_session`, PyJWT, `.gitignore` |
| `fix(dashboard): isolate tenants, preserve decimal precision, respect property timezone` | Bugs A, B, C |
| `docs: add FINDINGS.md with bug analysis, fixes, and live reproduction`      | Long-form write-up                                                        |
| `docs: add README with submission overview`                                  | This file                                                                 |

## Files I touched

```
backend/app/services/cache.py         # tenant-scoped cache key + fail-closed validation
backend/app/api/v1/dashboard.py       # Decimal → cents → float, fail-closed on missing tenant
backend/app/services/reservations.py  # timezone-aware monthly revenue against real DB
backend/app/core/database_pool.py     # DATABASE_URL wiring, AsyncAdaptedQueuePool, sync get_session
backend/requirements.txt              # add PyJWT
.gitignore                            # __pycache__, editor artefacts
FINDINGS.md                           # full bug analysis with repro
README.md                             # this overview
```

## Notes for the reviewer

- I respected the “Do NOT rebuild the system” guidance: no schema changes, no
  refactors beyond what each bug required, frontend untouched, public API
  contract preserved.
- I am explicit in [FINDINGS.md](./FINDINGS.md#bug-b--decimal--float-precision-loss)
  about a caveat on Bug B: the precision pathology is real, but the current
  seed's specific totals are coincidentally binary-exact, so the visible impact
  on a demo is limited. The fix is still correct and defensive.
- All numbers in this README and in `FINDINGS.md` were verified against the
  running `docker compose` stack, not inferred from code.
