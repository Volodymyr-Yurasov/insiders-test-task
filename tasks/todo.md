# Todo — Step 1: Scaffolding & Infrastructure

## Task 1: Project skeleton, settings, app factory, `/health`

**Description:** Create the `uv` project, package layout, typed settings, async DB engine + session dependency, Redis client, `create_app()` factory, and a `/health` endpoint that pings both dependencies.

**Acceptance criteria:**
- [x] `create_app(settings)` stores the engine, session factory and Redis client on `app.state`. The lifespan disposes them on shutdown.
- [x] `GET /health` → 200 `{"status":"ok","db":"ok","redis":"ok"}` when both are reachable. → 503 with the failing part marked `"error"` otherwise, with each check bounded by a ~2s timeout.
- [x] `DeclarativeBase` with a constraint naming convention exists in `app/models/base.py`.

**Verification:**
- [x] `uv run ruff check .` and `uv run mypy app` clean.
- [x] Manual: `uv run uvicorn app.main:app` with nothing running → `/health` returns 503 within ~2s (proves the timeout path).

**Dependencies:** None

**Files likely touched:** `pyproject.toml`, `app/core/config.py`, `app/core/db.py`, `app/core/redis.py`, `app/main.py`, `app/api/routers/health.py`, `app/models/base.py`, `.env.example`

**Estimated scope:** Medium (the files are small; mostly boilerplate)

---

## Task 2: Alembic async setup + empty baseline revision

**Description:** Initialize Alembic with an async `env.py` that takes its URL from `Settings` and its `target_metadata` from `Base.metadata`. Add an empty baseline revision.

**Acceptance criteria:**
- [x] `alembic upgrade head` succeeds against an empty database and creates `alembic_version`.
- [x] The URL comes from `Settings` (env), not from a hard-coded value in `alembic.ini`.
- [x] `alembic downgrade base` succeeds.

**Verification:**
- [x] `uv run alembic upgrade head && uv run alembic downgrade base && uv run alembic upgrade head` (needs the DB from Task 3, or any reachable Postgres).

**Dependencies:** Task 1

**Files likely touched:** `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako`, `migrations/versions/0001_baseline.py`

**Estimated scope:** Small

---

## Task 3: Dockerfile + docker-compose (db, redis, api)

**Description:** Containerize the API and add Postgres 16 + Redis 7, with healthchecks and startup ordering. The API runs migrations before starting.

**Acceptance criteria:**
- [x] `docker compose up -d --build` brings `db`, `redis` and `api` to healthy/running. `api` starts only after `db` and `redis` are healthy.
- [x] The `api` container runs `alembic upgrade head` and then uvicorn on port 8000.
- [x] Host ports and credentials come from `.env`, with defaults in `.env.example`. `.dockerignore` excludes `.venv`, caches and tests.

**Verification:**
- [x] `docker compose ps` shows all services healthy.
- [x] `curl http://localhost:8000/health` → 200 ok.
- [x] `docker compose stop redis` → `/health` 503 with `"redis":"error"`; `docker compose start redis` → 200 again.

**Dependencies:** Tasks 1, 2

**Files likely touched:** `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example`

**Estimated scope:** Small

---

## Task 4: Pytest harness + health smoke test

**Description:** Add the shared test infrastructure that every later step depends on, and prove it with one smoke test.

**Acceptance criteria:**
- [x] A session fixture creates `<db>_test` if it is missing and runs `alembic upgrade head` on it once per session.
- [x] An autouse per-test cleanup fixture runs `TRUNCATE` on all metadata tables (`RESTART IDENTITY CASCADE`, skipped when there are none) and `FLUSHDB` on Redis DB 15. There is no wrapping transaction.
- [x] A `client` fixture yields an `httpx.AsyncClient(transport=ASGITransport(app))` for an app built with the test settings. The engine and Redis are disposed at teardown.
- [x] pytest-asyncio is configured with `asyncio_mode = "auto"` and session-scoped loop for fixtures and tests.
- [x] `tests/test_health.py`: `GET /health` → 200 with `db` and `redis` both `"ok"`.

**Verification:**
- [x] `uv run pytest -q` → 1 passed, no warnings about event loops.
- [x] Run twice in a row → still green (the create-DB step is idempotent).

**Dependencies:** Tasks 1, 2, 3 (needs the running db/redis)

**Files likely touched:** `tests/conftest.py`, `tests/test_health.py`, `pyproject.toml` (pytest config)

**Estimated scope:** Small

---

## Checkpoint: Step 1 complete
- [x] `docker compose up -d --build` healthy; `/health` → 200
- [x] `uv run pytest` green; ruff + mypy clean
- [x] Human review: approved, with a follow-up → Step 1b

---
---

# Todo — Step 1b: Align skeleton with fastapi-best-practices

## Task 5: Move to `src/` domain layout (no behavior change)

**Description:** Rename the package `app` → `src` and flatten `core/*` into top-level modules. Move health into its own domain package, add the global `CustomModel`, make `get_redis` async, and update every reference.

**Acceptance criteria:**
- [x] Layout:
  - `src/{main,config,database,redis,schemas}.py` and `src/health/{router,schemas}.py`
  - `app/` is gone, including the empty `api/`, `models/`, `schemas/` and `services/` packages
  - `Base` + naming convention live in `src/database.py`
- [x] `src/schemas.py::CustomModel` (`from_attributes`, `populate_by_name`) is the base of `HealthResponse`.
- [x] `get_redis` is `async def`. No sync `def` dependencies remain (C2).
- [x] The Dockerfile runs `uvicorn src.main:app`. Ruff `known-first-party = ["src"]`. Tests live in `tests/health/test_router.py`, mirroring `src/`.

**Verification:**
- [x] `uv run pytest -q` → 1 passed
- [x] `uv run ruff check . && uv run ruff format --check . && uv run mypy src tests` clean
- [x] `Grep` finds no remaining `app.` / `"app"` imports or paths outside `.venv`

**Dependencies:** None (Step 1 done)

**Files likely touched:** `src/**` (moved from `app/**`), `tests/conftest.py`, `tests/health/test_router.py`, `migrations/env.py` (import path), `Dockerfile`, `pyproject.toml`

**Estimated scope:** Medium (mostly mechanical moves)

---

## Task 6: Env-driven config + docs toggle

**Description:** Replace the separate Postgres fields with `DATABASE_URL`/`REDIS_URL` DSNs, add `ENVIRONMENT` with a production default, drive test config through env vars set before importing `src`, and hide the OpenAPI docs outside local/staging.

**Acceptance criteria:**
- [x] `src/config.py`: `Config` with `DATABASE_URL: PostgresDsn`, `REDIS_URL: RedisDsn`, `ENVIRONMENT: Environment = PRODUCTION`, a module-level `settings`.
  - A `postgresql://` URL is normalized to `postgresql+asyncpg://`.
  - `src/constants.py::Environment` is a `StrEnum` (`local`, `staging`, `production`).
- [x] `create_app()` sets `openapi_url=None` unless `ENVIRONMENT ∈ {local, staging}`.
- [x] `conftest.py` sets `DATABASE_URL`, `REDIS_URL` and `ENVIRONMENT` in `os.environ` before importing `src`. It derives the test DB name via `make_url(...).set(database=...)`.
- [x] Compose, `.env.example` and alembic `env.py` use `DATABASE_URL`/`REDIS_URL`/`ENVIRONMENT=local`.

**Verification:**
- [x] `uv run pytest -q` → 2 passed. New test: `/docs` + `/openapi.json` → 200 with `local`, 404 with `production` (fresh app built after `monkeypatch`).
- [x] ruff + mypy clean
- [x] `docker compose up -d --build --wait` → healthy; `curl :8000/docs` → 200

**Dependencies:** Task 5

**Files likely touched:** `src/config.py`, `src/constants.py`, `src/main.py`, `src/database.py`, `tests/conftest.py`, `tests/health/test_router.py`, `docker-compose.yml`, `.env.example`, `migrations/env.py`

**Estimated scope:** Medium

---

## Task 7: `alembic/` directory, date_slug template, guide naming convention

**Description:** Rename `migrations/` → `alembic/` and set the `date_slug` file template. Rename the baseline file while keeping rev id `0001`, and switch `Base.metadata` to the guide's constraint naming (with `_N_` tokens for multi-column constraints).

**Acceptance criteria:**
- [x] `alembic.ini`: `script_location = %(here)s/alembic` and `file_template = %%(year)d-%%(month).2d-%%(day).2d_%%(slug)s`. Ruff post-write hooks still configured.
- [x] `alembic/versions/2026-11-23_baseline.py` with `revision = "0001"`.
- [x] `NAMING_CONVENTION` = `_pkey` / `_key` / `_fkey` / `_check` / `_idx` as in tasks/plan.md.
- [x] The Dockerfile copies `alembic/`; the test harness points `script_location` at `alembic/`.

**Verification:**
- [x] `uv run python -c "import alembic.context, alembic.command"` works from the repo root (no shadowing).
- [x] `uv run alembic downgrade base && uv run alembic upgrade head && uv run alembic current` → `0001 (head)`
- [x] `uv run alembic revision --rev-id 9999 -m "probe" --autogenerate` (or a plain revision) produces a file named `YYYY-MM-DD_probe.py`; then delete it.
- [x] `uv run pytest -q` green; ruff + mypy clean

**Dependencies:** Task 6

**Files likely touched:** `alembic.ini`, `alembic/**` (moved from `migrations/**`), `src/database.py`, `tests/conftest.py`, `Dockerfile`, `.dockerignore`

**Estimated scope:** Small–Medium

---

## Checkpoint: Step 1b complete
- [x] `docker compose up -d --build --wait` healthy; `/health` → 200; `/docs` → 200 (local)
- [x] `uv run pytest` green; ruff + mypy (`src tests`) clean
- [x] Alembic round-trip OK on the dev DB
- [x] Human review: approved 2026-11-23

---
---

# Todo — Step 2: Auth, refresh tokens & user management

## Task 8: Login slice: error foundation, `TimestampMixin`, `app_user` + migration, `POST /auth/token`

**Description:**
- Add the shared exception handler, `TimestampMixin` and the model registry.
- Add the `AppUser` model with migration `0002`, plus hashing and access-JWT utils.
- Add the login endpoint.

**Acceptance criteria:**
- [x] `src/exceptions.py` + `ErrorCode` + a handler registered in `create_app`. Errors render `{"code","detail"}`; 401 carries `WWW-Authenticate: Bearer`.
- [x] `app_user`:
  - has `created_at` / `updated_at` from `TimestampMixin`
  - constraints named per convention: `app_user_pkey`, `app_user_username_key`, `app_user_role_check`
  - `src/models.py` is imported by `alembic/env.py` and `conftest`
- [x] `POST /auth/token` (form):
  - valid credentials → `{"access_token", "refresh_token": null, "token_type": "bearer"}`; refresh is filled in by T10
  - unknown user, wrong password or inactive user → identical `401 invalid_credentials`
  - dummy-hash verify on the unknown-user path; hashing runs via `run_in_threadpool`

**Verification:**
- [x] `tests/auth/test_router.py::test_login`: success → decodable token with `sub` = user id; wrong password and unknown user → identical status and body.
- [x] Alembic `downgrade base && upgrade head` OK; ruff + mypy clean; suite green.

**Dependencies:** Step 1b

**Files likely touched:** `pyproject.toml`, `src/{exceptions,constants,models,database,main}.py`, `src/auth/{config,constants,models,schemas,utils,service,router}.py`, `alembic/env.py`, `alembic/versions/2026-11-23_app_user.py`, `tests/conftest.py`, `tests/auth/test_router.py`

**Estimated scope:** Medium-Large (many small files; the smallest testable slice)

---

## Task 9: Protected access: dependency chain, `GET /auth/me`, `POST /users`, `GET /users/{user_id}`

**Description:** Turn a bearer token into an active DB user, enforce the admin role at router level, and expose the current user plus admin-only user create/read. Add the shared fixtures for later steps.

**Acceptance criteria:**
- [x] `parse_jwt_data → current_user → admin_user`, with aliases `CurrentUser`/`AdminUser`/`DbSession`. The `/users` router uses `dependencies=[Depends(admin_user)]`.
- [x] `POST /users`:
  - → 201 `UserRead` (never includes the hash)
  - duplicate username in any letter case → 409 `username_taken` via `ON CONFLICT`
  - bad input → 422
- [x] `GET /users/{user_id}` → 200, or 404 `user_not_found`.
- [x] Fixtures: `create_user(role=…, is_active=…)` and `auth_headers(user)`, which mints the token directly; `admin_headers` and `storekeeper_headers` build on them.

**Verification:**
- [x] `tests/auth/test_router.py::test_access_matrix` (parametrized):
  - no token / garbage / expired / wrong signature → 401
  - deactivated user → 401
  - storekeeper on `POST /users` → 403
  - storekeeper on `/auth/me` → 200
  - admin `POST /users` → 201, then the same name in upper case → 409
- [x] ruff + mypy clean; suite green.

**Dependencies:** Task 8

**Files likely touched:** `src/auth/{dependencies,router,service,schemas}.py`, `src/database.py`, `src/main.py`, `tests/conftest.py`, `tests/auth/test_router.py`

**Estimated scope:** Medium

---

## Task 10: Refresh tokens: rotation, reuse detection, logout

**Description:** Add the `refresh_token` table (migration `0003`), and have login return an access + refresh pair. Add `POST /auth/refresh` with atomic rotation and family-wide reuse detection, and `POST /auth/logout`.

**Acceptance criteria:**
- [x] Only `sha256(token)` is stored. `family_id` is shared along a rotation chain; `expires_at` = now + `JWT_REFRESH_EXP_DAYS`.
- [x] Refresh uses one conditional `UPDATE … RETURNING`:
  - success → a new pair in the same family
  - a revoked token presented again → the whole family is revoked → 401 `invalid_refresh_token`
  - expired, unknown or inactive user → 401
- [x] `POST /auth/logout` revokes the family and always returns 204.

**Verification:**
- [x] `tests/auth/test_router.py::test_refresh_rotation`:
  1. Login → refresh → the new access token works on `/auth/me`.
  2. Reuse the old refresh token → 401, **and** the newest one is now rejected too (family revoked).
  3. A fresh login → two concurrent refreshes with the same token via `asyncio.gather` → exactly one 200.
  4. Logout → refresh with that token → 401.
- [x] Alembic round-trip; ruff + mypy clean; suite green.

**Dependencies:** Task 9

**Files likely touched:** `src/auth/{models,service,router,schemas,utils,config}.py`, `alembic/versions/2026-11-23_refresh_token.py`, `tests/auth/test_router.py`

**Estimated scope:** Medium

---

## Checkpoint A (after Task 10)
- [x] Suite green; ruff + mypy clean; alembic round-trip OK

---

## Task 11: `PATCH /users/{user_id}`: edit, deactivate, last-admin guard

**Description:** An admin can change a user's `username`, `role`, `password` or `is_active`, guarded so the system can never lose its last active admin, even under concurrency. Deactivation and password changes revoke the user's refresh tokens.

**Acceptance criteria:**
- [x] `UserUpdate`: all fields optional; an empty body → 422. The username is normalized; a taken name → 409 `username_taken` (`IntegrityError` on the UPDATE).
- [x] Demoting or deactivating an active admin first locks the active-admin rows `FOR UPDATE` (ordered by id), then → 409 `last_admin` if none would remain.
- [x] `is_active=false` or a new password → all of the user's refresh tokens are revoked in the same transaction. `updated_at` advances.

**Verification:**
- [x] `tests/auth/test_router.py::test_update_user`:
  1. Rename and change role → 200, and `updated_at` > `created_at`.
  2. Deactivate a storekeeper → their access token 401 **and** their refresh token 401.
  3. The sole admin demotes themselves → 409 `last_admin`.
  4. Two admins demote each other concurrently via `gather` → exactly one 200, one 409, and one active admin remains.
- [x] ruff + mypy clean; suite green.

**Dependencies:** Task 10

**Files likely touched:** `src/auth/{router,service,schemas,constants}.py`, `tests/auth/test_router.py`

**Estimated scope:** Medium

---

## Task 12: First-admin seed on startup + compose/.env config

**Description:** Create the first admin from env at startup, idempotently and race-safely, and wire the auth settings through compose and `.env.example`.

**Acceptance criteria:**
- [x] The lifespan startup calls `auth_service.ensure_first_admin()`. It uses `INSERT … ON CONFLICT (username) DO NOTHING`, never updates, and is skipped with an info log when the vars are unset.
- [x] Compose passes `JWT_SECRET` (`${JWT_SECRET:?…}`) and the seed vars. `.env.example` documents them with dev-only values.

**Verification:**
- [x] `tests/auth/test_service.py::test_seed_first_admin_is_idempotent`: two calls, the second with a different password → one admin, and the original password still works.
- [x] Compose rebuild, then over curl: login → `/auth/me` 200 → refresh 200 → logout 204 → the same refresh 401. `docker compose restart api` → healthy, one admin.
- [x] `uv run alembic check` → no diff.

**Dependencies:** Task 11

**Files likely touched:** `src/auth/service.py`, `src/main.py`, `docker-compose.yml`, `.env.example`, `.env`, `tests/conftest.py`, `tests/auth/test_service.py`

**Estimated scope:** Small

---

## Checkpoint: Step 2 complete
- [x] `uv run pytest` green (3 existing + 5 new test functions); ruff + mypy clean
- [x] Alembic round-trip + `alembic check` no diff
- [x] Compose end-to-end: seeded admin login → me → refresh → logout; restart is idempotent
- [x] Human review: approved 2026-11-23 (committed a69153b)

---
---

# Todo — Step 3: Unified errors, pagination, warehouses & products

## Task 13: Unified error format for FastAPI/Starlette/unhandled errors

**Description:** Register handlers so that every error response has the shape `{"code", "detail", ...}`. Validation errors get an `errors` list. Unknown routes and 405s get codes, and unhandled exceptions become a logged, opaque 500.

**Acceptance criteria:**
- [x] 422 → `{"code": "validation_error", "detail": "Request validation failed", "errors": [{"loc", "msg", "type"}]}`.
- [x] Unknown route → `404 {"code": "not_found"}`. Wrong method → `405 {"code": "method_not_allowed"}` with the `Allow` header preserved.
- [x] Unhandled exception → `500 {"code": "internal_error"}` and a logged traceback.

**Verification:**
- [x] `tests/test_exceptions.py::test_error_format`: 404, 405 and 422 shapes over HTTP. The 500 handler is called directly.
- [x] The existing `test_admin_manages_users` is updated to read `errors`; suite green; ruff + mypy clean.

**Dependencies:** Step 2

**Files likely touched:** `src/exceptions.py`, `src/constants.py`, `tests/test_exceptions.py`, `tests/auth/test_router.py`

**Estimated scope:** Small

---

## Task 14: Pagination foundation + `GET /users`

**Description:** Add `PageParams`, `Page[T]` and `paginate()` (`COUNT(*) OVER()` with an empty-page fallback), and use them for the admin user list.

**Acceptance criteria:**
- [x] `GET /users?limit=&offset=` (admin) → `{items, total, limit, offset}`, ordered by `id`.
- [x] `limit` outside 1..100 or a negative `offset` → 422 in the unified format.
- [x] An offset past the end → `items: []` with the correct `total`.

**Verification:**
- [x] Assertions added to `test_admin_manages_users`: create 3 users → `limit=2` page 1 has 2 items with `total` = all users; an offset past the end → empty items, same total.
- [x] Suite green; ruff + mypy clean.

**Dependencies:** Task 13

**Files likely touched:** `src/pagination.py`, `src/auth/router.py`, `src/auth/service.py`, `tests/auth/test_router.py`

**Estimated scope:** Small

---

## Checkpoint A (after Task 14)
- [x] Suite green; ruff + mypy clean

---

## Task 15: Warehouses: model + migration `0004` + create/list/get/patch

**Description:** The warehouse domain package with CRUD minus delete. Any authenticated user can read; only admins can write.

**Acceptance criteria:**
- [x] `warehouse` table with timestamps; `location_code` unique (`warehouse_location_code_key`), trimmed and upper-cased.
- [x] `POST /warehouses` (admin) → 201. Duplicate code → `409 location_code_taken` via `ON CONFLICT`.
- [x] `GET /warehouses` (any user) is paginated with an optional `is_active` filter. `GET /warehouses/{warehouse_id}` → 200 / 404 `warehouse_not_found`.
- [x] `PATCH /warehouses/{warehouse_id}` (admin): name, address, location_code, is_active.
  - an empty body → 422
  - a duplicate code → 409, via `IntegrityError` on the UPDATE
  - an unknown id → 404

**Verification:**
- [x] `tests/warehouses/test_router.py::test_warehouse_crud`:
  - create (normalized code)
  - duplicate → 409
  - storekeeper POST/PATCH → 403; storekeeper list/get → 200
  - list filter `is_active=false` after deactivating one
  - PATCH rename + code clash → 409
  - unknown → 404
- [x] Migration round-trip; suite green; ruff + mypy clean.

**Dependencies:** Tasks 13, 14

**Files likely touched:** `src/warehouses/{__init__,models,schemas,service,dependencies,exceptions,constants,router}.py`, `src/models.py`, `src/main.py`, `alembic/versions/2026-11-23_warehouse.py`, `tests/warehouses/{__init__,test_router}.py`

**Estimated scope:** Medium

---

## Task 16: Products: model + migration `0005` + create/get/patch/soft-delete

**Description:** The product domain package with an immutable SKU, a Decimal price and soft delete. Deleted products are invisible (404) and their SKUs stay reserved.

**Acceptance criteria:**
- [x] `product` table:
  - `sku` unique (`product_sku_key`)
  - `base_price` `Numeric(12,2)` with `product_base_price_non_negative_check`
  - `min_stock_level` with `product_min_stock_level_non_negative_check`
  - timestamps
- [x] `POST /products` (admin) → 201 with `base_price` serialized as a string (`"12.50"`). A duplicate SKU, including one on a deleted product → `409 sku_taken`.
- [x] `GET /products/{product_id}` (any user), `PATCH` (admin: name / base_price / min_stock_level; `sku` is not accepted) and `DELETE` (admin, soft → 204). A deleted or unknown product → 404 `product_not_found` on all three.

**Verification:**
- [x] `tests/products/test_router.py::test_product_crud`:
  - create (SKU normalized, price string)
  - invalid prices (negative, 3 decimals) → 422
  - PATCH price
  - storekeeper write → 403, read → 200
  - DELETE → 204, then GET/PATCH/DELETE → 404
  - recreate the same SKU → 409
- [x] Migration round-trip + `alembic check`; suite green; ruff + mypy clean.

**Dependencies:** Tasks 13, 14 (independent of 15)

**Files likely touched:** `src/products/{__init__,models,schemas,service,dependencies,exceptions,constants,router}.py`, `src/models.py`, `src/main.py`, `alembic/versions/2026-11-23_product.py`, `tests/products/{__init__,test_router}.py`

**Estimated scope:** Medium

---

## Checkpoint: Step 3 complete
- [x] `uv run pytest` green; ruff + mypy clean; alembic round-trip + `alembic check`
- [x] Compose: the seeded admin creates a warehouse + product via curl; a storekeeper reads OK, writes 403
- [x] Commit (no self-credit); human review, then plan Step 4

---
---

# Todo — Step 4: Movement engine

## Task 17: Ledger + stock queries + IN movements + product stock breakdown + input validation

**Description:** Create the append-only `operation`/`stock_movement` tables with DB-enforced invariants, the SQL stock functions, and `POST /movements` working end-to-end for IN. Also: the full request validation for all movement types, the warehouse/product locking prelude (shared with OUT/TRANSFER), and the stock breakdown on `GET /products/{product_id}`.

**Acceptance criteria:**
- [x] Migration `0006`:
  - CHECKs: `stock_movement_movement_shape_check`, `stock_movement_quantity_range_check`
  - FKs `RESTRICT`
  - the planned indexes
  - `operation (performed_by, idempotency_key)` unique
  - engine `isolation_level="READ COMMITTED"` explicit
- [x] `POST /movements` (any user) with IN → 201 `OperationRead`.
  - unknown or deleted SKU → 404 `products_not_found` with `missing_skus`
  - unknown warehouse → 404
  - inactive warehouse → 409 `warehouse_inactive`
  - wrong shape (IN with `from`, OUT without `from`, TRANSFER `from == to`) → 422
  - no token → 401
- [x] `GET /products/{product_id}` returns `total_stock` and `stock_by_warehouse: [{warehouse_id, location_code, quantity}]` (nonzero only; `[]` when none) from one statement.

**Verification:**
- [x] `tests/movements/test_validation.py::test_movement_validation`:
  - each rejection above (also via `/movements/batch` with duplicate SKUs → 422) writes **zero** rows
  - a storekeeper IN → 201
  - the product breakdown shows that IN
- [x] Migration round-trip + `alembic check`; ruff + mypy clean; suite green.

**Dependencies:** Step 3

**Files likely touched:** `src/movements/{__init__,constants,config,exceptions,models,schemas,stock,service,router}.py`, `src/models.py`, `src/main.py`, `src/database.py`, `src/products/{router,schemas}.py`, `alembic/versions/2026-11-23_movement_ledger.py`, `tests/movements/{__init__,test_validation}.py`

**Estimated scope:** Large-ish (one new domain package; the slice is the smallest testable one)

---

## Task 18: Locking engine for OUT/TRANSFER + batch endpoint + lock timeout → 503

**Description:** Add the race-safe path:
- products locked `FOR UPDATE` in id order
- source stock computed after the lock (READ COMMITTED)
- all shortages collected, with all-or-nothing rollback

Also expose `POST /movements/batch`, and map lock timeouts and deadlocks to 503.

**Acceptance criteria:**
- [x] OUT/TRANSFER never take stock below zero under concurrency.
- [x] Insufficient stock → `409 insufficient_stock` listing **every** short SKU (`requested`, `available`, `missing`). Nothing is written.
- [x] `POST /movements/batch` (1..100 items, types IN/OUT/TRANSFER) uses the same engine. A lock wait longer than `LOCK_TIMEOUT_MS` → `503 service_busy` + `Retry-After`.

**Verification:**
- [x] `tests/movements/test_engine.py`:
  1. **`test_concurrent_out_never_oversells`** (required): stock 5, 10× OUT qty 1 via `asyncio.gather` → exactly 5×201 + 5×409, final stock **== 0**.
  2. **`test_batch_is_all_or_nothing`** (required): A=10, B=2, C=0; batch OUT [A:5, B:3, C:1] → 409 with shortages for B (3/2/1) and C (1/0/1). The movement count is unchanged and A is still 10.
  3. **`test_stock_after_in_transfer_out`** (required): IN 10 → W1; TRANSFER 4 W1→W2; OUT 3 from W2 → W1=6, W2=1, total=7 (via `GET /products/{id}`).
  4. `test_lock_timeout_returns_503`: another session holds the product `FOR UPDATE`; with `LOCK_TIMEOUT_MS` patched to 200, OUT → 503 with `Retry-After`.
- [x] Full suite 5× green; mutation check (remove `FOR UPDATE` → test 1 fails), then restore.
- [x] ruff + mypy clean.

**Dependencies:** Task 17

**Files likely touched:** `src/movements/{service,stock,router,schemas,exceptions}.py`, `src/exceptions.py` (503 class), `tests/movements/test_engine.py`

**Estimated scope:** Medium

---

## Checkpoint A (after Task 18)
- [x] The 3 required tests green and stable; mutation check proves the lock matters
- [x] ruff + mypy clean; alembic round-trip + `alembic check`

---

## Task 19: D8 guards + `GET /movements` history

**Description:**
- Block deactivating a warehouse that still holds stock, and soft-deleting a product with non-zero total stock. Both run under row locks that conflict with the engine.
- Expose movement history with filters.
- Expose what a warehouse currently holds.

**Acceptance criteria:**
- [x] `PATCH /warehouses/{id}` with `is_active=false` → 409 `warehouse_not_empty` while any product has nonzero stock there. Reactivating, and other edits, are unaffected.
- [x] `DELETE /products/{id}` → 409 `product_has_stock` while total stock ≠ 0. After the stock is moved out, both succeed.
- [x] `GET /movements` (any user):
  - paginated, ordered `created_at DESC, id DESC`
  - filters: `product_id`, `warehouse_id` (from or to), `type`, `date_from` / `date_to` (`date_to` is exclusive)
  - each row includes the SKU
- [x] `GET /warehouses/{warehouse_id}/stock` (any user):
  - paginated `{product_id, sku, name, quantity}` for products with quantity > 0
  - one grouped query over the ledger, ordered by SKU
  - unknown warehouse → 404

**Verification:**
- [x] `tests/movements/test_guards.py::test_stock_guards`:
  - deactivate a non-empty warehouse → 409, delete a product with stock → 409
  - OUT everything → both → 200 / 204
- [x] `test_stock_after_in_transfer_out` also asserts:
  - `GET /movements?product_id=` returns the 3 movements newest first, and `warehouse_id=W2` returns 2
  - `GET /warehouses/{W1}/stock` → that product with 6, and `W2` → 1
- [x] ruff + mypy clean; suite green.

**Dependencies:** Task 18

**Files likely touched:** `src/warehouses/service.py`, `src/warehouses/exceptions.py`, `src/products/service.py`, `src/products/exceptions.py`, `src/movements/{router,service,schemas}.py`, `tests/movements/{test_guards,test_engine}.py`

**Estimated scope:** Medium

---

---

## Task 20: Lazy cleanup of old refresh tokens

**Description:** Keep `refresh_token` from growing forever without adding a scheduler. Each login deletes a bounded batch of tokens that expired more than `REFRESH_TOKEN_RETENTION_DAYS` (default 7) ago.

**Acceptance criteria:**
- [x] Migration `0007`: index `refresh_token_expires_at_idx`.
- [x] On login, before the new token is issued:
  - `DELETE FROM refresh_token WHERE id IN (SELECT id … WHERE expires_at < now() - retention LIMIT 1000)`
  - global, not per user; bounded, so a backlog can never make one login slow
  - concurrent logins deleting the same rows are harmless
- [x] Tokens that are expired or revoked **within** the retention window are kept, so reuse of a recently rotated or expired token is still recognized as theft.

**Verification:**
- [x] `tests/auth/test_service.py::test_login_prunes_old_refresh_tokens`: insert one token expired 30 days ago and one expired 1 day ago; log in → the 30-day one is gone, the 1-day one and the new one remain.
- [x] Migration round-trip + `alembic check`; suite green.

**Dependencies:** none (independent of Tasks 17–19)

**Files likely touched:** `src/auth/{config,models,service}.py`, `alembic/versions/2026-11-23_refresh_token_expires_at_idx.py`, `tests/auth/test_service.py`

**Estimated scope:** Small

---

## Checkpoint: Step 4 complete
- [x] `uv run pytest` green; ruff + mypy clean; alembic round-trip + `alembic check`
- [x] Compose:
  - IN → TRANSFER → OUT via curl with the breakdown correct
  - oversell → 409 with shortages
  - deactivating a non-empty warehouse → 409
- [x] Commit (no self-credit); human review, then plan Step 5

---
---

# Todo — Step 5: SQL aggregations without N+1

## Task 21: `GET /products`: list with stock, filters, sorting, pagination

**Description:** List non-deleted products with their computed stock (total, or in one warehouse), filtered, sorted and paginated, from a single SQL statement.

**Acceptance criteria:**
- [x] One statement: the ledger pre-aggregated per product, LEFT JOINed, with `COALESCE(stock, 0)` and `COUNT(*) OVER()`. Deleted products are excluded.
- [x] Query model:
  - `search` (escaped ILIKE on SKU/name)
  - `min_price` / `max_price`, `min_stock` / `max_stock` (`min > max` → 422)
  - `below_min_stock`
  - `warehouse_id` (unknown → 404)
  - `sort` ∈ {id, sku, name, price, stock}, `order` ∈ {asc, desc}; anything else → 422
- [x] Ordering always ends with `p.id` in the same direction. The response is `Page[ProductListItem]` with `stock`.

**Verification:**
- [x] `tests/products/test_list.py::test_product_list` with 4 products × 2 warehouses:
  - sort by stock desc and by price asc
  - page 2 of `limit=2`
  - `min_stock` / `max_price` / `below_min_stock` filters
  - `warehouse_id` switches to per-warehouse stock
  - a search for a literal `_` doesn't act as a wildcard
  - deleted products are absent
  - bad `sort` / `min > max` → 422
- [x] ruff + mypy clean; suite green.

**Dependencies:** Step 4

**Files likely touched:** `src/products/{schemas,service,router}.py`, `tests/products/test_list.py`

**Estimated scope:** Medium

---

## Task 22: `GET /reports/valuation`: one-query valuation, admin-only

**Description:** A new `reports` package. Per-warehouse value = Σ(stock × current base price) and units, computed in one statement (LEFT JOINs + SUM(CASE…) + COALESCE), plus a grand total and `generated_at`.

**Acceptance criteria:**
- [x] Every warehouse is listed: `{warehouse_id, name, location_code, is_active, total_units, total_value}`. An empty one shows `"0.00"` / 0.
- [x] `grand_total` = Σ `total_value` (Decimal, `"X.XX"` string), plus `generated_at`. One data statement.
- [x] Admin only (router-level dependency): storekeeper → 403, anonymous → 401.

**Verification:**
- [x] `tests/reports/test_valuation.py::test_valuation`: exact Decimal values for 2 stocked warehouses, after a TRANSFER and an OUT; an empty warehouse is `0.00`; an inactive (empty) one is flagged; a price change is reflected in the next call; storekeeper → 403.
- [x] `EXPLAIN` reviewed; ruff + mypy clean; suite green.

**Dependencies:** Step 4 (independent of Task 21)

**Files likely touched:** `src/reports/{__init__,schemas,service,router}.py`, `src/main.py`, `tests/reports/{__init__,test_valuation}.py`

**Estimated scope:** Small–Medium

---

**EXPLAIN notes (300k movements, 2k products, 50 warehouses; test DB, seeded then wiped):**
- `GET /products` (total, sort by stock): parallel seq scan → partial/finalize HashAggregate per product → hash join product → WindowAgg (`COUNT(*) OVER`) → top-N sort. **67 ms.**
- `GET /products?warehouse_id=`: BitmapOr on `from_`/`to_warehouse_id` indexes → HashAggregate → hash join. **17 ms.**
- `GET /reports/valuation`: nested loop over 50 warehouses, each a BitmapOr index scan + hash join to product → GroupAggregate. **360–400 ms.**
  - The pre-aggregated alternative (integer sum per (warehouse, product), then × price) measured **390–440 ms**, with identical results (0 mismatches). Kept the original.
  - The cost is the 400k-row numeric aggregation itself, not the plan shape. Step 6's cache serves repeat calls.
## Task 23: N+1 guard: statement-count fixture + scale-invariance test

**Description:** Prove both aggregation endpoints run a constant number of SQL statements regardless of the data size.

**Acceptance criteria:**
- [x] A `count_statements` fixture (a `before_cursor_execute` listener on the app engine), usable as a context manager returning the count.
- [x] `GET /products` and `GET /reports/valuation` each execute exactly *auth lookup + 1* statement, and the count is identical with 3 and with 12 products.

**Verification:**
- [x] `tests/test_n_plus_one.py::test_aggregations_use_constant_queries` green.
- [x] Sanity check: temporarily computing stock per product in a Python loop makes the test fail; then reverted.

**Dependencies:** Tasks 21, 22

**Files likely touched:** `tests/conftest.py`, `tests/test_n_plus_one.py`

**Estimated scope:** Small

---

## Checkpoint: Step 5 complete
- [x] `uv run pytest` green; ruff + mypy clean
- [x] `EXPLAIN` of both queries reviewed and noted
- [x] Compose: product list sorted by stock + valuation via curl match the ledger
- [x] Commit (no self-credit); human review, then plan Step 6

---
---

# Todo — Step 6: Redis cache for the valuation report

## Task 24: Cache module + `X-Cache` + dependency
- [x] `get_or_compute(redis, compute)` → `(report, "HIT" | "MISS" | "BYPASS")`: generation keys, NX stampede lock with bounded polling, Redis errors → compute from DB
- [x] `invalidate(redis)` = `INCR` generation, errors logged and swallowed
- [x] `GET /reports/valuation` sets `X-Cache`; `ReportsConfig.VALUATION_CACHE_TTL_S` (300)
- **Files:** `src/reports/{cache,config,dependencies,router}.py`

## Task 25: Invalidate on every successful write
- [x] Routers call `await valuation_cache.invalidate()` after: `POST /movements`, `POST /movements/batch`, `POST/PATCH/DELETE /products`, `POST/PATCH /warehouses`
- **Files:** `src/{movements,products,warehouses}/router.py`

## Task 26: Tests
- [x] `tests/reports/test_cache.py`:
  - `test_valuation_cache`:
    - MISS → HIT (0 valuation statements on HIT)
    - a movement → MISS with the new value
    - every write route bumps the generation
    - unreachable Redis → 200 `BYPASS`
  - `test_stale_write_back_is_never_served`: deterministic interleaving via the cache functions
  - `test_cold_cache_stampede_computes_once`: 10 concurrent GETs → the valuation query runs once
- **Dependencies:** Tasks 24, 25

## Checkpoint: Step 6 complete
- [x] Suite green; ruff + mypy clean; compose MISS → HIT → movement → MISS
- [x] Commit (no self-credit)

---
---

# Todo — Step 7: Low-stock background notifications

## Task 27: Crossings computed by the engine
- [x] `_lock_products` returns sku → (id, name, min_stock_level). `execute` → `ExecutionResult(operation, low_stock)`. Alerts are only for OUT/TRANSFER where `before >= min > after`.
- **Files:** `src/movements/{service,schemas}.py`, `src/notifications/schemas.py`

## Task 28: Notifier + background scheduling
- [x] `Notifier` protocol, `LoggingNotifier`, `safe_send` (never raises), `get_notifier` dependency, `NotificationsConfig.LOW_STOCK_ALERT_EMAIL`
- [x] `POST /movements` and `/movements/batch` add the background task only when there are alerts
- **Files:** `src/notifications/{__init__,config,schemas,service,dependencies}.py`, `src/movements/router.py`

## Task 29: Tests
- [x] `tests/movements/test_notifications.py::test_low_stock_notifications` (crossing once, TRANSFER, batch ×2, failure → none, raising notifier → 201) + `test_logging_notifier_logs_email`

## Checkpoint: Step 7 complete
- [x] Suite green; ruff + mypy clean; compose log order: `201` access line, then "email sent"
- [x] Commit (no self-credit)

---
---

# Todo — Step 8: Idempotency-Key

## Task 30: DB layer (exactly-once guarantee)
- [x] Migration `0008`: `operation.request_fingerprint`
- [x] `Command` carries `idempotency_key` + `fingerprint`; the operation is inserted first with `ON CONFLICT DO NOTHING`. On conflict: load the operation + movements (with SKUs) → `ExecutionResult(replayed=True)`, or 422 on a fingerprint mismatch.
- **Files:** `src/movements/{models,service}.py`, `alembic/versions/…_operation_request_fingerprint.py`

## Task 31: Redis layer + wiring
- [x] `src/idempotency/{config,exceptions,service,dependencies}.py`: `begin` → Proceed(token) | Replay(status, body) | raise InProgress / KeyReused; `complete`; `release` (Lua compare-and-delete); Redis errors → DB layer only
- [x] `_record` integrates both layers; replays return the stored body with `Idempotent-Replayed: true` and skip side effects

## Task 32: Tests (`tests/idempotency/test_idempotency.py`)
- [x] `test_idempotent_replay` (replay, mismatch, per-user scope, failure releases the key, bad header)
- [x] `test_concurrent_duplicates_write_once`
- [x] `test_db_backstop_when_redis_is_down`

## Checkpoint: Step 8 complete
- [x] Suite green; ruff + mypy clean; alembic check; compose replay
- [x] Commit (no self-credit)
