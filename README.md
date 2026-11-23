# Inventory Management Engine API

Async REST API for warehouse stock accounting: warehouses, products, and a movement ledger (IN / OUT / TRANSFER). Stock is never stored, only computed from the ledger in SQL.

**Stack:** Python 3.11+ · FastAPI · PostgreSQL 16 · SQLAlchemy 2 (async) + asyncpg · Alembic · Redis 7 · Pytest + pytest-asyncio + httpx · Docker Compose · uv.

---

## Quick start

```bash
cp .env.example .env              # dev defaults; set your own JWT_SECRET for anything real
docker compose up -d --build --wait
docker compose exec api python -m src.seed   # optional demo data (idempotent)
```

- API: <http://localhost:8000>
- Interactive docs: <http://localhost:8000/docs>. Docs are served only when `ENVIRONMENT` is `local` or `staging`; the default is `production`, which hides them.
- Logins: `admin / admin-dev-password` (from `FIRST_ADMIN_*`), and `storekeeper / storekeeper-pass` after seeding.

In `/docs`, click **Authorize**, enter the username and password, and you're done. It's a standard OAuth2 password flow.

```bash
TOKEN=$(curl -s -d "username=admin&password=admin-dev-password" localhost:8000/auth/token | jq -r .access_token)
curl -s -H "Authorization: Bearer $TOKEN" "localhost:8000/products?sort=stock&order=desc"
curl -s -H "Authorization: Bearer $TOKEN" localhost:8000/reports/valuation
```

## Running the tests

The tests run against the real Postgres and Redis from compose. They use a separate `<db>_test` database, which is created and migrated automatically, and Redis DB 15.

```bash
docker compose up -d db redis
uv sync
uv run pytest               # 49 tests, ~25 s
uv run ruff check . && uv run ruff format --check . && uv run mypy src tests
uv run alembic check        # models and migrations in sync
```

Isolation is `TRUNCATE` + `FLUSHDB` before each test. Tests are deliberately **not** wrapped in a rolled-back transaction: that would pin every request to one connection and make the concurrency tests pass for the wrong reason.

---

## How the spec is covered

| Spec | Where | Proven by |
|---|---|---|
| **1** Stock never stored; computed from movements in SQL | [src/movements/stock.py](src/movements/stock.py), ledger in [src/movements/models.py](src/movements/models.py) | every stock test |
| **2.1** Race conditions: concurrent write-offs never go below zero | [src/movements/service.py](src/movements/service.py) (`FOR UPDATE` + READ COMMITTED) | `tests/movements/test_engine.py::test_concurrent_out_never_oversells` |
| **2.2** Atomic batches, all-or-nothing, every shortage listed | same engine, `POST /movements/batch` | `test_batch_is_all_or_nothing` |
| **2.3** Product list with stock: filter / sort / paginate, one query | [src/products/service.py](src/products/service.py) `list_products` | `tests/products/test_list.py`, `tests/test_n_plus_one.py` |
| **2.3** Valuation per warehouse in one query | [src/reports/service.py](src/reports/service.py) | `tests/reports/test_valuation.py`, `tests/test_n_plus_one.py` |
| **2.4** Valuation cached in Redis, invalidated consistently | [src/reports/cache.py](src/reports/cache.py) | `tests/reports/test_cache.py` (incl. the stale write-back race and the stampede) |
| **2.5** Low-stock email via `BackgroundTasks`, response not blocked | [src/notifications/](src/notifications/), scheduled in [src/movements/router.py](src/movements/router.py) | `tests/movements/test_notifications.py` |
| **2.6** JWT Bearer; Storekeeper vs Admin | [src/auth/](src/auth/) | `tests/auth/` |
| **3** Required tests: race, batch rollback, IN → TRANSFER → OUT | | `tests/movements/test_engine.py` |
| **Bonus** Idempotency-Key, 24 h in Redis | [src/idempotency/](src/idempotency/) + DB constraint | `tests/idempotency/test_idempotency.py` |
| **Bonus** Sliding-window rate limit, 50 transactions/min/user | [src/rate_limit/service.py](src/rate_limit/service.py) | `tests/rate_limit/test_rate_limit.py` |

The concurrency-critical tests were also **mutation-checked**: the protection was removed on purpose, and the test had to fail.
- Without the product `FOR UPDATE` → the race test sees 10/10 write-offs succeed (stock −5).
- With `FOR UPDATE` kept but REPEATABLE READ → same oversell.
- Without the cache generation counter → the stale write-back is served.
- Without the admin-row lock → the system can lose its last admin.

---

## API overview

| Method | Path | Role | Notes |
|---|---|---|---|
| POST | `/auth/token` | — | OAuth2 password form → 15-min access JWT + 14-day refresh token |
| POST | `/auth/refresh`, `/auth/logout` | — | refresh-token rotation with reuse detection; logout revokes the chain |
| GET | `/auth/me` | any | |
| GET / POST / PATCH | `/users`, `/users/{id}` | admin | edit, deactivate; the last active admin is protected |
| GET / POST / PATCH | `/warehouses`, `/warehouses/{id}` | read: any, write: admin | deactivation only when empty |
| GET | `/warehouses/{id}/stock` | any | products currently in the warehouse |
| GET / POST / PATCH / DELETE | `/products`, `/products/{id}` | read: any, write: admin | list with computed stock; soft delete only at zero stock |
| GET | `/movements` | any | history; filters by product, warehouse, type, date range |
| POST | `/movements`, `/movements/batch` | any | `Idempotency-Key` supported; rate-limited |
| GET | `/reports/valuation` | admin | cached; `X-Cache: HIT / MISS / BYPASS` |
| GET | `/health` | — | DB and Redis probes, 503 if either is down |

**Errors** always look like `{"code": "...", "detail": "...", ...extra}`. For example, a 409 `insufficient_stock` carries `shortages: [{sku, requested, available, missing}]`, and a 422 `validation_error` carries `errors: [{loc, msg, type}]`. Submitted values are never echoed back in errors.

---

## Key design decisions

**Concurrency (2.1, 2.2).** One transaction per request, under **READ COMMITTED (set explicitly)**:
1. Lock the warehouses involved `FOR SHARE`.
2. Lock the products `FOR UPDATE`, in id order (`FOR SHARE` for IN).
3. Only then compute the source stock with one grouped `SUM … FILTER`. Each statement takes a fresh snapshot, so it sees whatever the previous lock holder committed.
4. Any shortage → raise, and nothing is written.

The lock order (warehouses → products, by id) rules out deadlocks. A lock wait beyond `LOCK_TIMEOUT_MS` returns `503 service_busy` with `Retry-After` instead of hanging. The database itself also enforces each movement's shape and the quantity range with CHECK constraints.

**Aggregations (2.3).**
- Product list: the ledger is pre-aggregated per product (`SUM(CASE …)` for the total, `SUM … FILTER` for one warehouse), then LEFT JOINed with `COALESCE`. The total row count comes from `COUNT(*) OVER()` in the same statement.
- Valuation: warehouses LEFT JOIN movements LEFT JOIN products with `SUM(CASE to → +qty, from → −qty) × base_price`. Empty warehouses show `0.00`.
- Execution plans were reviewed on 300,000 movements; numbers are in `tasks/todo.md`, Step 5.

**Cache (2.4).** Invalidation bumps a **generation counter** (`INCR`) after the commit, rather than deleting the key. A slow reader that computed pre-commit data then stores it under a dead generation instead of re-poisoning the cache.
- **What invalidates:** every write the report depends on (movements, product price/create/delete, warehouse create/update).
- **Stampede guard:** a `SET NX` lock, so only one request recomputes on a miss.
- **Redis down:** the report is served from the DB.

**Low stock (2.5).**
- **When:** per source warehouse, for OUT and TRANSFER, only on the *crossing* (`before ≥ min > after`), so it doesn't re-alert on every later write-off.
- **Consistent under concurrency:** the crossing is computed from the locked stock read, so of N concurrent write-offs exactly one alerts.
- **What the task gets:** plain data, never the ORM session.
- **Failures:** notifier errors are logged and never affect the 201 the client already received.

**Auth (2.6).**
- Access tokens are JWTs (HS256), checked through FastAPI dependencies (`CurrentUser`, `AdminUser`). The user is re-read from the DB on every request, so deactivation and role changes apply immediately.
- Refresh tokens are opaque strings stored only as a SHA-256 hash. Each refresh rotates the token atomically, and reusing an old one revokes the whole login chain.
- Login gives the same response for an unknown user, a wrong password and an inactive user, with a dummy hash check to equalize timing.
- argon2 hashing runs in a worker thread so it doesn't block the server.

**Idempotency (bonus).** Two layers:
1. **Redis (fast path):** `SET NX` marks a request in progress (409 for a concurrent duplicate). The result is replayed for 24 h with `Idempotent-Replayed: true`, and a key reused with a different request gets 422.
2. **Postgres (the guarantee):** the operation is the first write, claimed with `INSERT … ON CONFLICT (performed_by, idempotency_key) DO NOTHING`. Exactly-once therefore holds even if Redis is down or the process crashes between the commit and the Redis write.

**Rate limit (bonus).**
- **Algorithm:** a sliding-window log in a Redis ZSET, updated by one atomic Lua script that uses Redis `TIME` as the clock.
- **What counts:** one batch is one transaction. Idempotent replays cost no quota.
- **Redis down:** fails open, with an error log.

**Data rules.**
- Products are soft-deleted, and only at zero stock. Their SKU stays reserved, and SKUs can't be changed after creation.
- Warehouses can be deactivated only when empty.
- Money is `numeric(12,2)` and is returned as a string, e.g. `"12.50"`.
- Ledger rows are append-only, and their foreign keys are `RESTRICT`.

## Known trade-offs

- **Product-level `FOR UPDATE` is coarse:** write-offs of the same SKU from *different* warehouses wait for each other. A per-(warehouse, product) advisory lock would be finer. It wasn't chosen because the spec points to `FOR UPDATE` and the cost is negligible at this scale.
- **Stock is summed over the full history.** It's indexed and measured fast at 300k rows. At very large scale, periodic snapshots plus deltas would be the next step.
- **Valuation uses the current base price,** as the spec says, not historical cost.
- **`BackgroundTasks` is best-effort:** a task is lost if the worker dies after responding. In production, an outbox row plus a queue worker would make it durable.
- **Idempotency keys are reserved permanently, not for 24 h.** Redis replays a result for 24 h, but the Postgres unique constraint that guarantees exactly-once never expires. Reusing a key later with a *different* request returns `422 idempotency_key_reused`. Clients should use a fresh unique key (e.g. a UUID) per logical operation, which is the usual contract (Stripe works the same way). It's safer than letting an old key silently start a new write-off.
- **Strict refresh rotation:** a client that retries a refresh almost simultaneously (e.g. a double-click) trips reuse detection and must log in again. A short grace window is a possible extension.

## Configuration

All settings come from environment variables or `.env`; see [.env.example](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL`, `REDIS_URL` | localhost | connections; a plain `postgresql://` URL is switched to `asyncpg` automatically |
| `ENVIRONMENT` | `production` | `local` / `staging` show `/docs` |
| `JWT_SECRET` | — (required, ≥ 32 chars) | token signing |
| `FIRST_ADMIN_USERNAME` / `_PASSWORD` | unset | first admin, created on startup if missing (never overwritten) |
| `LOCK_TIMEOUT_MS` | 5000 | max wait for row locks → 503 |
| `VALUATION_CACHE_TTL_S` | 300 | cache safety-net TTL |
| `RATE_LIMIT_MAX_REQUESTS` / `RATE_LIMIT_WINDOW_S` | 50 / 60 | per-user movement limit |
| `LOW_STOCK_ALERT_EMAIL` | `inventory-alerts@example.com` | recipient written in the log |

## Project layout

Organized by domain, following [fastapi-best-practices](https://github.com/zhanymkanov/fastapi-best-practices). Each package under `src/` has its own `router` / `schemas` / `models` / `service` / `dependencies` / `exceptions`:

```
src/  auth  warehouses  products  movements  reports  notifications  idempotency  rate_limit  health
      main.py  config.py  database.py  redis.py  exceptions.py  pagination.py  models.py  seed.py
alembic/   migrations (date_slug names, sequential revision ids)
tests/     mirrors src/
tasks/     step-by-step plans and task lists; PLAN.md is the overall plan
```
