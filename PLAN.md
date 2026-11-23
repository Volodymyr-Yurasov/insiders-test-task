# Inventory Management Engine API — Implementation Plan

Source: `Тестове_завдання_Inventory_Management_Engine_API.docx`. The bonus items (Idempotency-Key, sliding-window rate limit) are treated as core scope.

Stack: Python 3.11+, FastAPI, PostgreSQL 16, SQLAlchemy 2.0 async + asyncpg, Alembic, Redis, Pytest + pytest-asyncio + httpx, Docker Compose.

---

## 0. Design decisions (made up front; they affect several steps)

| # | Topic | Decision | Why |
|---|-------|----------|-----|
| D1 | Stock storage | No stock column anywhere. Stock = Σ qty where `to_warehouse = W` − Σ qty where `from_warehouse = W`, computed in SQL. | Hard rule from the spec. |
| D2 | Single vs batch movement | A single movement is a batch with one line. Both go through **one** service function. | One code path for locking, validation, cache invalidation, notifications, idempotency. |
| D3 | Concurrency control | In one transaction: `SELECT … FROM product WHERE id IN (…) AND NOT is_deleted ORDER BY id FOR UPDATE`, **then** compute stock, **then** insert. Isolation level stays **READ COMMITTED**. | Product rows are the only shared row we can lock (no stock row exists). `ORDER BY id` gives a fixed lock order, so batches can't deadlock. |
| D4 | Isolation trap | Do **not** use REPEATABLE READ. Under RR the snapshot is taken at the first statement, so the SUM after the lock wait would miss the movement the previous lock holder just committed, which lets stock go negative. Under READ COMMITTED each statement takes a new snapshot. | This is the main correctness trap in the task. |
| D5 | Duplicate SKU inside one batch | Reject with 422. | Otherwise `[A:5, A:5]` with stock 8 passes two separate checks and goes negative. Merging lines also works; rejecting is clearer. |
| D6 | Low-stock scope | Checked **per source warehouse**, for OUT and TRANSFER. Notify only when stock **crosses** the threshold (`before >= min > after`), not on every later write-off. | Prevents a flood of repeat emails. "Before" is already known from the locked check, so it costs no extra query. |
| D7 | Roles | `admin` can do everything a `storekeeper` can, plus its own actions. | Usual expectation. Keeps dependencies simple: `require_role("admin")`. |
| D8 | Soft delete / deactivation | Soft-deleting a product or deactivating a warehouse is allowed **only when its stock is 0** (409 otherwise). Deleted products and inactive warehouses reject new movements. | Answers "is a deleted product's stock still counted in valuation?" by never letting that state happen. |
| D9 | SKU | Trimmed and upper-cased. Globally unique, including soft-deleted rows. | Stops `abc-1` and `ABC-1` from both existing. A deleted SKU cannot silently come back as a new product. |
| D10 | Money / qty | `Numeric(12,2)` with `Decimal`, serialized as a string. `quantity` is an int with `CHECK > 0` and an upper bound (e.g. ≤ 1,000,000). Timestamps are `timestamptz`, set by the server. | No float rounding. The client cannot backdate movements or set the performer: `performed_by` comes from the JWT. |

---

## 0b. Project structure & conventions

Guide: [zhanymkanov/fastapi-best-practices](https://github.com/zhanymkanov/fastapi-best-practices). Organized by domain, not by file type.

```
src/
  main.py            create_app(): lifespan, exception handlers, routers, docs toggle
  config.py          global Config(BaseSettings): DATABASE_URL, REDIS_URL, ENVIRONMENT
  constants.py       Environment enum, global ErrorCode
  database.py        engine, session dependency, Base + naming convention
  redis.py           client + async dependency
  schemas.py         CustomModel (global Pydantic base)
  exceptions.py      DetailedHTTPException + NotFound/Conflict/PermissionDenied, one handler
  pagination.py      PageParams dependency + Page[T]
  health/            router.py
  auth/              router, schemas, models (AppUser), dependencies (current_user, require_role),
                     service, config (AuthConfig: JWT), constants (Role), exceptions, utils (jwt, hashing)
  warehouses/        router, schemas, models, dependencies (valid_warehouse_id), service, exceptions
  products/          router, schemas, models, dependencies (valid_product_id), service (aggregations), exceptions
  movements/         router, schemas, models (Operation, StockMovement), service (locking engine),
                     constants (MovementType), exceptions (InsufficientStock + shortages)
  reports/           router, schemas, service (valuation SQL), cache.py (generation counter)
  notifications/     service (Notifier protocol, LoggingNotifier), schemas
  idempotency/       dependencies, service, config
  rate_limit/        dependencies, service (Lua script), config
alembic/             migrations (named date_slug)
tests/               mirrors src/: auth/, products/, movements/, reports/, idempotency/, rate_limit/
```

| # | Convention | Applied as |
|---|-----------|------------|
| C1 | Cross-package imports name the module | `from src.products import service as products_service`. Never bare `from … import get_by_id`. |
| C2 | Routes and dependencies are `async def` and never block | A sync `def` dependency runs in the threadpool, so every dependency is `async def`. CPU-heavy argon2 hash/verify runs through `run_in_threadpool` so it doesn't stall the event loop. |
| C3 | Use Pydantic heavily | One `CustomModel` base: `from_attributes`, `populate_by_name`, Decimal → string in JSON. Every input is constrained with `Field` (`quantity: int = Field(gt=0, le=1_000_000)`, SKU pattern/max length, list length 1..100). `StrEnum` for `Role` and `MovementType`. Cross-field rules (from/to by movement type, from ≠ to, duplicate SKUs) live in `model_validator`s, so they return a 422 with details. |
| C4 | Settings split per module | Global `Config` plus `AuthConfig`, `RateLimitConfig`, `IdempotencyConfig`, `CacheConfig`, each with its own env prefix. Tests set env vars at the top of `conftest.py`, before importing `src`, and tweak values per test with `monkeypatch.setattr` on the settings object (e.g. rate limit = 3). |
| C5 | Validate through dependencies, chained | `valid_product_id` (exists, not deleted) → 404. `valid_warehouse_id` → `active_warehouse` → 404/409. Chained with `current_user` → `require_role`. `Annotated` aliases: `DbSession`, `CurrentUser`, `AdminUser`. FastAPI caches a dependency once per request, so chaining costs nothing. |
| C6 | Consistent REST paths | Path parameters are always named `{product_id}` and `{warehouse_id}`, so the validation dependencies can be reused. Routes declare `status_code`, `tags`, `summary`, and `responses` for 404/409/422/429 with their error models, so the OpenAPI docs are complete. |
| C7 | Exceptions per module | Module exceptions subclass the global `DetailedHTTPException` (status + code + detail). One handler renders `{"code", "detail", **extra}`; `InsufficientStock` adds `shortages`. |
| C8 | Docs only outside prod | `ENVIRONMENT` (local/staging/production): `openapi_url=None` in production. Compose defaults to `local` so reviewers can see `/docs`. |
| C9 | Database naming | Singular `lower_snake` table names: `app_user`, `warehouse`, `product`, `operation`, `stock_movement`. Not `user`, which is a reserved word in Postgres. `_at` suffix for timestamps. The guide's constraint names: `%(table)s_pkey`, `_key`, `_fkey`, `_check`, `%(column_0_label)s_idx`. |
| C10 | Migrations are static, reversible and readable | `file_template = %%(year)d-%%(month).2d-%%(day).2d_%%(slug)s`. The downgrade → upgrade round-trip is checked after every migration. |
| C11 | SQL first | Aggregations happen in SQL. The per-warehouse breakdown in `GET /products/{product_id}` uses `json_agg` in the same query. Routes return rows/dicts and let `response_model` validate once; building Pydantic objects in the route would validate twice. |
| C12 | Tests | Async client from day 0 (done). `dependency_overrides` instead of monkeypatching for the notifier and the clock. |

**Deliberate deviations from the guide:**
- `uv` dependency groups instead of `requirements/{base,dev,prod}.txt`: same split, plus a lockfile.
- Pydantic's ISO 8601 datetime output is kept, not the guide's `strftime("%z")` serializer, which drops microseconds and writes `+0000`. All datetimes are tz-aware UTC from the DB.
- `BackgroundTasks` for low-stock emails, although by the guide's rule ("if losing it would page someone, use a queue") a queue would be the production choice. The spec requires `BackgroundTasks`, and the email is log-only. The trade-off is documented in §4.

---

## 1. Data model

Table names are singular (C9). Each table is added by the migration of the step that first needs it.

**Timestamps:**
- Every mutable table gets `created_at` + `updated_at` from one `TimestampMixin` in `src/database.py`:
  - `timestamptz NOT NULL DEFAULT now()`
  - `updated_at` also has `onupdate=func.now()`, so every ORM and Core `UPDATE` bumps it
- **Exception:** the append-only ledger tables `operation` and `stock_movement` are never updated by design, so they get only `created_at`. An `updated_at` column there would suggest rows can change, which would undermine the audit trail (D1).

- `app_user`: id, username (unique, stored lower-cased), password_hash (argon2 via `pwdlib`; not passlib, which is unmaintained), role (VARCHAR + CHECK), is_active, timestamps.
- `refresh_token`: id, user_id → app_user, family_id (uuid), token_hash (sha256, unique), expires_at, revoked_at (nullable), timestamps. Only a hash of the token is stored, never the token itself.
- `warehouse`: id, name, location_code (unique), address, is_active, timestamps.
- `product`: id, sku (unique), name, base_price `CHECK >= 0`, min_stock_level `CHECK >= 0`, is_deleted, timestamps.
- `operation`: id, type, performed_by → app_user, idempotency_key (nullable), created_at. `UNIQUE (performed_by, idempotency_key)`. Groups the movements written by one request, and is the database-level backstop for idempotency (see step 8).
- `stock_movement`: id, operation_id, type enum (IN/OUT/TRANSFER), product_id, quantity, from_warehouse_id, to_warehouse_id, created_at, performed_by.
  - CHECKs:
    - IN → `from IS NULL AND to IS NOT NULL`
    - OUT → `from IS NOT NULL AND to IS NULL`
    - TRANSFER → `both NOT NULL AND from <> to`
  - Indexes: `(product_id, from_warehouse_id)`, `(product_id, to_warehouse_id)`, `(from_warehouse_id)`, `(to_warehouse_id)`, `(created_at)`.

The database enforces these invariants itself, so a buggy service still cannot write an invalid movement.

---

## 2. Steps

Each step ends with a **check** that must pass before the next step starts. Tests run against real Postgres and Redis from docker-compose (SQLite cannot test `FOR UPDATE`).

### Step 1 — Scaffolding & infrastructure ✅ done
- Built with an `app/{core,api/routers,models,...}` layout by file type. Step 1b replaces it with the domain layout from §0b.
- `docker-compose.yml`: `db` (postgres:16), `redis` (redis:7), `api` (runs `alembic upgrade head` then uvicorn). Healthchecks plus `depends_on: condition: service_healthy`.
- Settings via `pydantic-settings`: DB URL, Redis URL, JWT secret/TTL, rate limit (default 50/60s), cache TTL, idempotency TTL.
- `GET /health` pings DB and Redis.
- Test harness (`conftest.py`):
  - Separate test database, created and migrated once per session.
  - **TRUNCATE between tests. Do not wrap each test in a rolled-back outer transaction:** that pins every request to one connection and makes the race test pass for the wrong reason.
  - Flush the Redis test DB between tests.
  - `httpx.AsyncClient(transport=ASGITransport(app))`.
  - Pool size ≥ race-test concurrency.
  - Set pytest-asyncio loop scope to `session` for both the engine and the tests. A loop-scope mismatch causes the "attached to a different loop" error.
- **Check:** `docker compose up` gives a healthy `/health`; `pytest` runs (smoke test).

### Step 1b — Align the skeleton with §0b (refactor only, no new behavior)
- Rename `app/` → `src/` and `migrations/` → `alembic/`. Flatten `core/*` into `src/{config,database,redis}.py`, and move health into `src/health/router.py`.
- Add `src/schemas.py` (`CustomModel`), `src/constants.py` (`Environment`) and the `ENVIRONMENT`-based docs toggle.
  - `src/exceptions.py` + `ErrorCode` move to **Step 2**, and `src/pagination.py` to **Step 3**, where each is first used and tested.
- Switch to the guide's constraint-naming convention and the `date_slug` migration file template, and rename the baseline migration.
- `get_redis` becomes `async def`. It is currently sync, so it runs in the threadpool on every request.
- Config: a single `DATABASE_URL` (`PostgresDsn`) in place of the separate fields. `conftest.py` sets the test env (`…/inventory_test`, Redis DB 15) before importing `src`.
- Update `Dockerfile`, compose, `alembic.ini` and `pyproject.toml` for the new paths.
- **Check:** the existing smoke test is green, ruff and mypy are clean, the alembic downgrade/upgrade round-trip works, and the compose stack is healthy. Also, `/docs` returns 200 under `local` and 404 under `production` (one extra assertion in the smoke test file).

### Step 2 — Auth & roles
- Foundation first: `src/exceptions.py` (`DetailedHTTPException` + handler rendering `{code, detail, **extra}`) and `ErrorCode` in `src/constants.py` (C7).
- Auth uses **dependencies, not decorators**:
  - `parse_jwt_data → current_user → admin_user`, exposed as the `CurrentUser` / `AdminUser` `Annotated` aliases.
  - Admin-only route groups use router-level `dependencies=[Depends(admin_user)]`, so every route in the group is protected by default.
- `POST /auth/token` (OAuth2 password form) returns:
  - a short-lived access JWT (HS256, 15 min) with `sub=user_id`, `iat`, `exp`; no role claim, because the role is always read from the DB
  - an opaque refresh token (14 days)
- `current_user` decodes the token and **re-reads the user from the DB**, so deactivation or role changes take effect immediately.
- **Refresh tokens:**
  - `POST /auth/refresh {refresh_token}` → a new access + refresh pair. **Rotation:** the old refresh token is revoked atomically:
    `UPDATE … SET revoked_at = now() WHERE token_hash = :h AND revoked_at IS NULL AND expires_at > now() RETURNING …`
    Of two concurrent refreshes with the same token, exactly one wins.
  - **Reuse detection:** presenting an already-revoked token revokes its whole family → 401. A stolen token that was already used by the real client kills the attacker's chain too.
  - Refresh also checks `is_active`.
  - `POST /auth/logout {refresh_token}` revokes that token's family.
- **User management (admin):**
  - `POST /users` creates a user.
  - `GET /users/{user_id}` returns one user.
  - `PATCH /users/{user_id}` edits `username`, `role`, `password` or `is_active` (deactivate/reactivate).
    - Deactivating a user or changing their password revokes all their refresh tokens. Access tokens already die on the next request through the `is_active` check.
    - **Last-admin guard:** demoting or deactivating an admin locks all active admin rows (`… WHERE role='admin' AND is_active FOR UPDATE`) and rejects the change with 409 if no other active admin would remain. Two admins demoting each other concurrently cannot leave the system with zero admins.
    - A rename to a taken username → 409. This is an UPDATE, so it catches `IntegrityError`.
- The first admin is seeded from env on startup with `INSERT … ON CONFLICT (username) DO NOTHING`, which is safe when several workers start at once.
- Edge cases:
  - Expired, malformed, or wrong-signature token → 401.
  - Inactive or deleted user → 401.
  - Wrong role → 403.
  - Login: the same error for an unknown user and a wrong password (no user enumeration).
    - When the user is unknown, still verify against a dummy hash, so response timing doesn't reveal which usernames exist.
  - argon2 hash/verify is CPU-bound (~50 ms), so it runs through `run_in_threadpool` (C2).
- **Check (5 test functions):**
  - login
  - access matrix (parametrized)
  - refresh rotation/reuse/logout
  - user edit/deactivate incl. the concurrent last-admin race
  - seed idempotency

### Step 3 — Unified errors, pagination, warehouses & products CRUD
- **Unified error format:** FastAPI's own errors (422 validation, 404 unknown route, 405, other `HTTPException`s, unhandled 500s) are re-rendered as `{"code", "detail", ...}`. Validation adds an `errors` list.
- **Pagination foundation** (moved here from Step 5):
  - `PageParams` (`limit` 1..100, `offset` ≥ 0) and `Page[T]` `{items, total, limit, offset}`.
  - `paginate()` gets `total` from `COUNT(*) OVER()` in the same query.
  - First users: `GET /users` and `GET /warehouses`.
- Warehouses: POST/PATCH (admin); GET list/single (any authenticated user).
- Products: POST/PATCH/DELETE (soft) (admin); GET single (any). The SKU is immutable after creation.
- **D8 guards** (deactivate a warehouse / delete a product only at zero stock) need movements, so they land in **Step 4**. In Step 3, deactivation and soft-delete are unconditional.
- Edge cases:
  - Duplicate SKU or location_code on create: `INSERT … ON CONFLICT (sku) DO NOTHING RETURNING id`; no row returned → 409.
    - Not a pre-check SELECT: that races.
    - Not catching `IntegrityError`: a unique violation aborts the whole Postgres transaction.
    - `ON CONFLICT` is INSERT-only, so a PATCH that changes a unique field (e.g. `location_code`) still catches `IntegrityError` → 409.
  - PATCH of an unknown or deleted id → 404.
- **Check (3 test functions):**
  - error format
  - warehouse CRUD
  - product CRUD (incl. duplicate SKU → 409, also against a soft-deleted product)

  Plus one `GET /users` assertion in the existing admin test.

### Step 4 — Movement engine (core: 2.1 + 2.2)
- `POST /movements`: single line. `type` + product_id/sku + qty + from/to by type.
- `POST /movements/batch`: `type ∈ {OUT, TRANSFER}`, `from`, `to?`, `items: [{sku, quantity}]` (1..100 items).
- Service algorithm (one transaction):
  1. Validate the shape: from/to required by type, `from ≠ to`, no duplicate SKUs (D5), qty > 0.
  2. Lock warehouses involved `FOR SHARE` and check `is_active`. Lock order is always warehouses, then products.
  3. Lock products: `… WHERE sku IN (…) AND NOT is_deleted ORDER BY id FOR UPDATE` for OUT/TRANSFER, `FOR SHARE` for IN.
     - Why IN needs an explicit lock: an FK insert takes only `KEY SHARE`, which does not block the soft-delete UPDATE.
     - Missing SKUs → 404 listing **all** missing SKUs.
  4. For OUT/TRANSFER: **one** grouped query computes the source warehouse stock for all locked products.
  5. Collect **every** shortage, not just the first. If any → rollback → `409 {"detail": "insufficient_stock", "shortages": [{"sku", "requested", "available", "missing"}]}`.
  6. Insert the `operation` and the movements, commit.
  7. **After commit:** invalidate the cache (step 6) and schedule notifications (step 7).
- Add `SET LOCAL lock_timeout = '5s'`. On timeout → 503 with `Retry-After` instead of a hung request.
- **D8 guards (moved from Step 3):**
  - Product soft-delete and warehouse deactivation take the same row lock as movements (`FOR UPDATE` on the product / warehouse row), then check stock = 0 → otherwise 409.
  - This closes the race with a concurrent IN.
- `GET /movements`: history with filters (product, warehouse, type, date range) and pagination.
- `GET /products/{product_id}`: product plus per-warehouse stock breakdown, built with `json_agg` in one query (C11).
- **Checks (the 3 required tests):**
  1. **Race:** stock 5, `asyncio.gather` of 10 × OUT qty 1 → exactly 5×201 + 5×409, final stock **== 0** (exact equality, not just ≥ 0).
  2. **Batch atomicity:** stock A=10, B=2; batch OUT [A:5, B:3] → 409 whose `shortages` names B (requested 3, available 2). The movement count is unchanged and A is still 10.
  3. **Dynamic stock:** IN 10 → W1; TRANSFER 4 W1→W2; OUT 3 from W2 → W1=6, W2=1, total=7.

### Step 5 — SQL aggregations without N+1 (2.3)
- Reuses `src/pagination.py` from Step 3.
- `GET /products`: **one** query returning products with `total_stock`. `total` comes from `COUNT(*) OVER()` in the same query, so the count and the page come from the same snapshot; a separate count query could disagree with the page under concurrent writes.
  - An offset past the end returns zero rows, so `COUNT(*) OVER()` has nowhere to put the total. Only in that case, run a separate count (rare).
  - Total: `COALESCE(SUM(CASE WHEN type='IN' THEN qty WHEN type='OUT' THEN -qty ELSE 0 END), 0)`. TRANSFER nets to zero in the total.
  - Optional `warehouse_id`: `SUM(qty) FILTER (WHERE to=:w) - SUM(qty) FILTER (WHERE from=:w)`.
  - Filters: `search` (sku/name ILIKE), `min_price/max_price`, `min_stock/max_stock` (HAVING or an outer query), `below_min_stock=true`.
  - Sort: a whitelist (`stock|price|name|sku`, `asc|desc`). Always add `id` as a tie-breaker so pagination is stable. Pagination: `limit` (1..100) / `offset`, response `{items, total, limit, offset}`.
  - Edge cases: unknown sort field → 422 (never interpolated into SQL); products with no movements show stock 0 via LEFT JOIN + COALESCE; deleted products are excluded.
- `GET /reports/valuation` (admin): per warehouse `SUM(stock × base_price)` in one query, plus a grand total.
  - Built as warehouses LEFT JOIN movements `ON w.id IN (m.from, m.to)` JOIN products, with `SUM(CASE WHEN m.to = w.id THEN qty ELSE -qty END * p.base_price)`.
  - Warehouses with no stock show 0. Inactive warehouses are included and flagged (they are empty anyway by D8).
- **Check (1 test):** seed 3 products × 2 warehouses.
  - Assert sort-by-stock order, one filter, and page 2 contents.
  - Assert valuation numbers (exact `Decimal`).
  - **Assert statement count** via a SQLAlchemy `before_cursor_execute` listener: list = 1, valuation = 1. This is the N+1 guard.

### Step 6 — Redis cache for valuation (2.4)
- **Generation counter**, not a plain DELETE:
  - Read `valuation:gen`, look up `valuation:{gen}`. On a miss, compute and `SET valuation:{gen} … EX ttl`.
  - Invalidate with `INCR valuation:gen` **after** commit.
  - Why not DELETE: a slow reader could compute old data, then a movement commits and deletes the key, then the reader writes the stale value back, and it stays until the next movement. With generations, the stale value lands under a dead generation and is never read.
- Why after commit: if the INCR happened before commit, a reader could cache pre-commit data under the new generation.
- Invalidate on movements **and** on product price changes, product create/delete, and warehouse create. Price changes the valuation too.
- If Redis is down: reads fall back to the DB, and invalidation failures are logged. The TTL (e.g. 5 min) bounds staleness if INCR fails after commit.
- **Stampede guard:** on a miss, only one request recomputes.
  - It takes `SET valuation:{gen}:lock NX EX 5` and computes.
  - Other requests poll the value key briefly (e.g. up to ~1s in 50 ms steps), then compute from the DB themselves without caching. Correctness never depends on the lock.
- **Checks (2 tests):**
  1. GET valuation, then a movement, then GET valuation: the value changed. A second GET with no movement in between is served from Redis (DB statement count is 0).
  2. **Deterministic stale write-back race:** call the cache functions directly in the racy order:
     - reader reads gen N;
     - a movement commits and INCRs;
     - the reader stores its old value under N;
     - a new GET happens.
     The new GET must return the fresh value, never the stale one.

### Step 7 — Low-stock background notification (2.5)
- The service returns `[(product, warehouse, before, after, min_level)]` for crossings (D6). The router adds `BackgroundTasks.add_task(notifier.send_low_stock, payload)`.
- Pass **plain data**, not ORM objects or the session: the session is closed by the time the task runs.
- `Notifier` interface with a `LoggingNotifier` (logs "email sent to …"). Swapped out in tests with `app.dependency_overrides`.
- Scheduled only on the success path, so a rolled-back operation never notifies. Exceptions inside the task are caught and logged, so they don't crash the worker.
- **Check (1 test):** min=5, stock 6; OUT 2 → one notification; OUT 1 → no new notification (already below the threshold).

### Step 8 — Idempotency-Key (bonus 1)
- Applies to `POST /movements` and `/movements/batch`. Header optional, ≤ 255 chars, scoped per user: `idem:{user_id}:{key}`.
- Flow:
  1. `SET idem:… {"state":"processing","fp":sha256(method+path+body)} NX EX 30`.
  2. If NX fails, read the record:
     - `processing` → 409 "request in progress".
     - `done` with the same fingerprint → replay the stored status and body with header `Idempotent-Replayed: true`.
     - Different fingerprint → 422 "key reused with different payload".
  3. Execute. On 2xx → overwrite with `{"state":"done", status, body}` `EX 86400`. On any error → DELETE the key. No state changed, so a retry is safe and the result may legitimately differ.
- **Database guarantee:** the Postgres counterpart of `SET NX`.
  - The **first** statement of the movement transaction, before any product locks, is:
    `INSERT INTO operation (…) ON CONFLICT (performed_by, idempotency_key) DO NOTHING RETURNING id`.
  - Row returned → proceed.
  - No row → duplicate: SELECT the existing operation and replay its movements.
  - A concurrent duplicate blocks on the unique index until the first transaction ends:
    - First one committed → DO NOTHING → replay.
    - First one rolled back (e.g. not enough stock) → this request runs for real, which is correct because nothing was written.
  - This covers the crash window (DB committed, process died before Redis was updated), an expired `processing` marker, and Redis being down.
  - Redis `SET NX` stays as the fast path: it answers duplicates without a DB round-trip and returns an immediate 409 instead of a blocked request.
  - Inserting the operation before the product locks is deadlock-safe: a waiting duplicate holds no product locks.
- **Check (1 test):** the same key sent twice concurrently with `gather`, plus once after → exactly one operation in the DB and identical bodies (or 409 in-progress for the concurrent one). The same key with a different body → 422.

### Step 9 — Sliding-window rate limit (bonus 2)
- A Lua script runs atomically per user on `rl:{user_id}` (ZSET):
  1. `ZREMRANGEBYSCORE` older than now−60s.
  2. `ZCARD`.
  3. If < limit: `ZADD now member`, `PEXPIRE 60s`, allow. Otherwise deny, with retry-after = oldest + 60s − now.
- Use Redis `TIME` inside the script, so several API instances with clock skew agree. Make the member unique (`now:uuid`) so two requests in the same millisecond don't collapse into one.
- Applies to movement-creating endpoints only. A batch counts as 1 transaction (one DB transaction). Failed business attempts (409) still count: the limit protects the system, not only successful writes.
- Order: auth → idempotency replay (a replay does **not** consume quota) → rate limit → execute.
- If Redis is down: **fail open** with an error log. Availability of stock operations wins over the limiter.
- Returns 429 with a `Retry-After` header.
- **Check (1 test):** limit overridden to 3 in settings; 4th request → 429 with `Retry-After`. A different user is still allowed.

### Step 10 — Finish
- README: how to run, env vars, design decisions (copy section 0), example curl calls.
- Seed script (admin, storekeeper, two warehouses, a few products).
- Full `docker compose up` run + `pytest` green; ruff and mypy clean.

---

## 3. Test inventory (12 tests total)

Test files mirror `src/` packages (C12).

| # | Test | Step |
|---|------|------|
| 1 | Smoke: `/health` OK + docs toggle by environment | 1, 1b |
| 2 | Auth: login (no enumeration), access matrix (401/403/200, duplicate username 409), refresh rotation + reuse detection + logout, user edit/deactivate + concurrent last-admin guard, seed idempotency | 2 |
| 2b | Unified error format; warehouse CRUD; product CRUD (duplicate SKU 409, also vs. soft-deleted) | 3 |
| 3 | **Race: concurrent OUT never goes below zero** (required) | 4 |
| 4 | **Batch all-or-nothing + shortage details** (required) | 4 |
| 5 | **IN → TRANSFER → OUT stock correctness** (required) | 4 |
| 6 | Batch validation: duplicate SKU / from = to / inactive warehouse → 4xx, nothing written | 4 |
| 7 | Product list filter/sort/pagination + valuation numbers + statement count | 5 |
| 8 | Valuation cache hit + invalidation after movement | 6 |
| 12 | Cache stale write-back race (deterministic interleaving) | 6 |
| 9 | Low-stock notification fires once on crossing | 7 |
| 10 | Idempotency: replay, concurrent duplicate, payload mismatch | 8 |
| 11 | Rate limit: N+1 → 429, other user unaffected | 9 |

---

## 4. Known trade-offs (documented, not fixed)
- **Product-level `FOR UPDATE` is coarse:** OUTs of the same SKU from *different* warehouses serialize. The finer alternative is `pg_advisory_xact_lock(hash(warehouse_id, product_id))` in sorted order. Not chosen, because the spec explicitly points at `FOR UPDATE` and the throughput cost is negligible at this scale.
- **Stock is recalculated from the full history:** fine with the indexes above. At very large scale, the next step would be periodic snapshot rows plus a delta. That is out of scope and would bend the "no static field" rule.
- **Valuation uses the *current* base price**, not historical cost. This is what the spec asks for.
- **Low-stock emails run in `BackgroundTasks`:** they are lost if the worker dies after responding. The production answer is a transactional outbox row written in the movement transaction, plus a queue worker (Arq/Celery) with retries. The spec mandates `BackgroundTasks`, and the email is log-only.
