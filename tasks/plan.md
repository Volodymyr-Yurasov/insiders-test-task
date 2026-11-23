# Implementation Plan: Step 1 — Scaffolding & Infrastructure

Parent plan: [../PLAN.md](../PLAN.md), Step 1. The later steps of PLAN.md will each get their own section in this file when they are planned.

## Overview
Set up a runnable, testable skeleton:
- FastAPI app with typed settings
- async SQLAlchemy engine and Redis client
- Alembic wired for async
- Docker Compose with Postgres 16 + Redis 7 + the API
- a pytest harness that later steps (including the concurrency test) can rely on

The deliverable is `GET /health`, which reports DB and Redis status, plus one green smoke test. No domain models yet: each model is added in the step that first needs it (vertical slicing).

## Environment findings
- Host: Windows 10, Python 3.13.1, `uv` 0.12.3 available.
- Docker Desktop is installed (WSL distro `docker-desktop`), but it is **stopped** and `docker` is **not on PATH** in this shell. → Open question 1.
- No local Postgres or Redis. Tests need the compose services running.

## Architecture Decisions
- **Dependency manager: `uv`** with `pyproject.toml` + `uv.lock`. `requires-python = ">=3.11"`; Docker image `python:3.13-slim` matches the local interpreter.
- **Layout:** `app/core` (config, db, redis), `app/api/routers`, `app/models`, `app/schemas`, `app/services`, `migrations/`, `tests/`.
- **App factory `create_app(settings)`:**
  - Builds the engine, session factory and Redis client and stores them on `app.state`.
  - Engine and client creation open no connections, so this is safe outside an event loop. The lifespan only disposes them.
  - Why: `httpx.ASGITransport` does not run the lifespan, so tests get the same wiring and can dispose it explicitly.
- **`DeclarativeBase` with a naming convention** (`ix_`, `uq_`, `ck_`, `fk_`, `pk_`). Alembic autogenerate then produces stable constraint names, and the CHECK constraints in later steps can be referenced by name.
- **Alembic async `env.py`** reads the URL from `Settings`, not from `alembic.ini`, so the app, tests and compose share one source of truth. Step 1 ships an empty baseline revision so that `upgrade head` is exercised from day one.
- **Health:**
  - Returns 200 `{"status":"ok","db":"ok","redis":"ok"}`, or 503 with the failing component marked `"error"`.
  - Each check is bounded by a short timeout (~2s), so a dead dependency doesn't hang the probe.
  - No auth.
- **Test harness:**
  - Runs against a separate database `<db>_test` (created if missing through the `postgres` maintenance DB, then `alembic upgrade head` once per session) and Redis DB index 15.
  - Cleanup between tests: `TRUNCATE <all metadata tables> RESTART IDENTITY CASCADE` (a no-op while there are no tables) plus `FLUSHDB`. **No per-test outer transaction**, for the race-test reason in PLAN.md.
  - pytest-asyncio `asyncio_mode=auto` with session loop scope for both fixtures and tests.
- **Compose:**
  - Healthchecks on `db` (`pg_isready`) and `redis` (`redis-cli ping`). `api` waits with `condition: service_healthy`, then runs `alembic upgrade head && uvicorn`.
  - Host ports come from `.env` (defaults 5432/6379/8000), in case they are already in use.
- **Tooling:** `ruff` (lint + format) and `mypy --strict` on `app/`.

## Dependency Graph
```
pyproject + settings
   ├── db engine/session ──┬── alembic env (async)
   ├── redis client        │
   └── create_app ── /health
                       │
        Dockerfile + compose (needs app + alembic)
                       │
        pytest harness (needs app factory + alembic + running compose services)
```

## Task List
See [todo.md](todo.md) for full acceptance criteria.

### Phase 1: Skeleton
- [x] Task 1: Project skeleton, settings, app factory, `/health`
- [x] Task 2: Alembic async setup + empty baseline revision

### Phase 2: Runtime & tests
- [x] Task 3: Dockerfile + docker-compose (db, redis, api)
- [x] Task 4: Pytest harness + health smoke test

### Checkpoint: Step 1 complete
- [x] `docker compose up -d --build` → all services healthy; `curl localhost:8000/health` → 200 ok
- [x] `uv run pytest` green; `uv run ruff check .` and `uv run mypy app` clean
- [x] Human review: approved 2026-11-23, with a follow-up to align with fastapi-best-practices → Step 1b

## Risks and Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| Docker not running / not on PATH | High: blocks Tasks 3–4 verification | Start Docker Desktop. Use the full path to `docker.exe` if PATH isn't refreshed. Tasks 1–2 don't need Docker except to verify against a DB. |
| pytest-asyncio loop-scope mismatch ("attached to a different loop") | Med | Configure the session loop scope in `pyproject.toml` from the start, and create the engine inside a session fixture. |
| asyncpg on Windows Proactor loop | Low | asyncpg supports Proactor; verified by the smoke test. |
| Host port 5432/6379 already in use | Low | Ports are configurable via `.env`. |
| Alembic autogenerate drift from unnamed constraints | Med (later steps) | Naming convention on `MetaData` from day one. |

## Open Questions
1. Can you start Docker Desktop and make sure `docker` works in a fresh terminal? If it isn't on PATH, I'll call it by its full path.
2. Are you OK with `uv` as the package manager? The alternative is plain `pip` + `requirements.txt`. `uv` is installed and gives a lockfile.

---

# Implementation Plan: Step 1b — Align the skeleton with fastapi-best-practices

Parent plan: [../PLAN.md](../PLAN.md) §0b (conventions C1–C12) and Step 1b.

## Overview
A refactor of the Step 1 skeleton into the domain-based `src/` layout, with no new endpoints.

The behavior changes are small:
- configuration via `DATABASE_URL` / `REDIS_URL` / `ENVIRONMENT`
- docs hidden outside `local`/`staging`
- the guide's constraint-naming and migration-filename conventions
- `get_redis` becomes async

The success bar is that everything that was green stays green.

## Architecture Decisions
- **Layout after 1b:**
  ```
  src/
    main.py config.py constants.py database.py redis.py schemas.py
    health/  __init__.py router.py schemas.py
  alembic/   env.py script.py.mako versions/2026-11-23_baseline.py
  tests/     conftest.py  health/__init__.py test_router.py
  ```
- **Scope trimmed vs PLAN.md (YAGNI):** each foundation module is added in the step that first uses and tests it.
  - `src/exceptions.py` (`DetailedHTTPException` + handler) and `ErrorCode` → **Step 2**, where the 401/403/409 paths are their first users.
  - `src/pagination.py` → **Step 5**.
- **Config (C4):**
  - Global `Config(BaseSettings)` in `src/config.py` with uppercase fields `DATABASE_URL: PostgresDsn`, `REDIS_URL: RedisDsn`, `ENVIRONMENT: Environment = PRODUCTION`, and a module-level `settings = Config()`.
  - The default is `PRODUCTION`, a safe default that hides docs. Compose and `.env.example` set `local`.
  - The asyncpg driver is enforced: a validator rewrites a `postgresql://` scheme to `postgresql+asyncpg://`, so a plain URL from compose or `.env` still works.
- **App wiring unchanged in spirit:** `create_app()` (no argument now) reads `src.config.settings` and builds the engine, session factory and Redis client on `app.state`, which works under `ASGITransport` without a lifespan.
- **Tests (C4, C12):**
  - `conftest.py` sets `DATABASE_URL` (…`/inventory_test`), `REDIS_URL` (…`/15`) and `ENVIRONMENT=local` in `os.environ` **before** any `src` import.
  - It derives them from the dev values (env or `.env`), so the test DB host and port follow the dev config.
  - The maintenance connection for `CREATE DATABASE` is the same URL with database `postgres`.
- **Docs toggle (C8):**
  - `SHOW_DOCS_ENVIRONMENTS = {LOCAL, STAGING}`. Otherwise `openapi_url=None`, which disables `/docs` and `/redoc` too.
  - Tested by setting `settings.ENVIRONMENT` with `monkeypatch` and building a fresh app.
- **Naming convention (C9):**
  - The guide's suffixes, with one tweak: `_N_` tokens, so multi-column constraints are named after **all** their columns.
  - E.g. `operation_performed_by_idempotency_key_key` instead of the misleading `operation_performed_by_key`.
  - `ix: %(table_name)s_%(column_0_N_name)s_idx`, `uq: %(table_name)s_%(column_0_N_name)s_key`, `ck: %(table_name)s_%(constraint_name)s_check`, `fk: %(table_name)s_%(column_0_name)s_fkey`, `pk: %(table_name)s_pkey`.
- **Migrations (C10):**
  - `file_template = %%(year)d-%%(month).2d-%%(day).2d_%%(slug)s`.
  - Revision **ids stay explicit and sequential** (`--rev-id 0002`, …) for a readable `down_revision` chain.
  - The baseline file is renamed but **keeps rev id `0001`**, so the existing dev and test DBs (both stamped `0001`) keep working without being wiped.

## Dependency Graph
```
T1 package move (src/, health pkg, async get_redis, CustomModel)
   └── T2 config + env-driven tests + docs toggle   (needs src/config.py location)
          └── T3 alembic/ rename + naming convention + file template  (env.py imports src.config)
                 └── Checkpoint (compose rebuild + full verification)
```
Strictly sequential: each task leaves the suite green.

## Task List
See [todo.md](todo.md) for details.
- [x] Task 5: Move to `src/` domain layout (no behavior change)
- [x] Task 6: Env-driven config (`DATABASE_URL`/`REDIS_URL`/`ENVIRONMENT`) + docs toggle
- [x] Task 7: `alembic/` directory, date_slug template, guide naming convention

### Checkpoint: Step 1b complete
- [x] `docker compose up -d --build --wait` healthy; `/health` → 200; `/docs` → 200 (local)
- [x] `uv run pytest` green (health + docs toggle); ruff + mypy (`src tests`) clean
- [x] `alembic downgrade base && alembic upgrade head` round-trip OK on dev DB
- [x] Human review: approved 2026-11-23

## Risks and Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| A top-level `alembic/` dir shadows the installed `alembic` package (`prepend_sys_path = .`) | Med | It has no `__init__.py`, so it's only a namespace-package candidate, and the installed regular package wins. Verified explicitly in T7 (`alembic upgrade` + `python -c "import alembic.context"`). |
| Stale `app` references left behind (Dockerfile CMD, mypy/isort config, env.py) | Med | `Grep` for `\bapp\.` / `"app"` after T1. Compose rebuild at the checkpoint. |
| Settings read at import time make test overrides order-sensitive | Med | Env set at the very top of `conftest.py`, before `src` imports. The docs-toggle test uses `monkeypatch` on the settings object, not env. |
| Renaming the baseline breaks DBs already stamped `0001` | Low | Rev id kept as `0001`; only the filename changes. |
| Test DB name derivation breaks if the dev URL has query params | Low | Use `sqlalchemy.make_url(...).set(database=...)`, not string slicing. |

---

# Implementation Plan: Step 2 — Auth, refresh tokens & user management

Parent plan: [../PLAN.md](../PLAN.md) Step 2, conventions C1–C12. Spec 2.6:
- JWT Bearer auth.
- `storekeeper`: view data, create movements.
- `admin`: create products/warehouses, see the financial report.

Scope was extended at review: refresh tokens, user editing/deactivation, and timestamps on every mutable table.

## Overview
New endpoints:

| Method | Path | Who | Purpose |
|---|---|---|---|
| POST | `/auth/token` | anyone | login (OAuth2 form) → access + refresh |
| POST | `/auth/refresh` | refresh-token holder | rotate → new access + refresh |
| POST | `/auth/logout` | refresh-token holder | revoke the refresh-token family |
| GET | `/auth/me` | any user | current user |
| POST | `/users` | admin | create user |
| GET | `/users/{user_id}` | admin | read user |
| PATCH | `/users/{user_id}` | admin | edit username/role/password, deactivate/reactivate |

Also: the shared error format, `TimestampMixin`, and seeding the first admin from env.

## Architecture Decisions
- **Libraries:**
  - `pyjwt`
  - `pwdlib[argon2]`
  - `python-multipart`: required by FastAPI for `OAuth2PasswordRequestForm`, currently missing
- **Auth through dependencies, not decorators:**
  - Chain: `parse_jwt_data → current_user → admin_user`, exposed as the `CurrentUser` / `AdminUser` `Annotated` aliases.
  - The `/users` router declares `dependencies=[Depends(admin_user)]`, so every route in it is admin-only by default.
  - `OAuth2PasswordBearer(auto_error=False)`, so a missing token goes through our 401 format.
- **Error foundation (C7):**
  - `src/exceptions.py`: `DetailedHTTPException` with `NotAuthenticated` (401 + `WWW-Authenticate: Bearer`), `PermissionDenied` (403), `NotFound` (404) and `Conflict` (409).
  - One handler renders `{"code", "detail", **extra}`. `ErrorCode` lives in `src/constants.py`; auth-specific codes in `src/auth/constants.py`.
- **Timestamps:**
  - `TimestampMixin` in `src/database.py`:
    - `created_at`, `updated_at`: `timestamptz NOT NULL server_default now()`
    - `updated_at` also has `onupdate=func.now()`
  - Used by every mutable table. The append-only ledger tables (`operation`, `stock_movement`, Step 4) get only `created_at`.
  - Raw `text()` UPDATEs would bypass `onupdate`, so the convention is: none on mutable tables.
- **Model registry:**
  - `src/models.py` imports every domain model module. `alembic/env.py` and `tests/conftest.py` import it.
  - Without this, autogenerate would see an empty metadata and propose dropping tables.
- **Tables:**
  - `app_user`: `id`, `username` (unique, lower-cased), `password_hash`, `role` (VARCHAR + CHECK via `Enum(native_enum=False, create_constraint=True)`), `is_active`, timestamps. Migration `0002`.
  - `refresh_token`: `id`, `user_id` FK (`ON DELETE CASCADE`), `family_id uuid`, `token_hash` (sha256 hex, unique), `expires_at`, `revoked_at NULL`, timestamps. Index on `(user_id)` and `(family_id)`. Migration `0003`.
- **Access token:**
  - Claims: `sub`, `iat`, `exp`; `JWT_ACCESS_EXP_MINUTES = 15`. Decoded with `algorithms=[JWT_ALG]` and `options={"require": ["exp","sub"]}`.
  - No role claim: role and `is_active` are re-read from the DB on every request.
- **Refresh token:**
  - Opaque: `secrets.token_urlsafe(32)`. Only `sha256(token)` is stored, so a DB leak doesn't leak usable tokens. It's high-entropy random, so no slow hash is needed.
  - `JWT_REFRESH_EXP_DAYS = 14`. Sent in the JSON body. An httpOnly cookie would be the choice for a browser SPA; out of scope.
  - **Rotation (race-safe):** one atomic statement, so of two concurrent refreshes exactly one wins:
    `UPDATE refresh_token SET revoked_at=now() WHERE token_hash=:h AND revoked_at IS NULL AND expires_at>now() RETURNING user_id, family_id`
    - Row returned → check that the user is active → issue a new pair in the **same family**.
  - **Reuse detection:** no row returned → look the hash up.
    - Exists but revoked → **revoke the whole family** (theft signal) → 401.
    - Expired or unknown → 401.
  - **Logout:** revoke the family of the presented token. Idempotent: always 204, so it reveals nothing.
  - Revoke **all** of a user's refresh tokens on deactivation and on password change.
- **Login hardening:**
  - Usernames are normalized (strip + lower) on create, login and rename.
  - An unknown user still runs a dummy-hash verify.
  - Unknown user, wrong password and inactive user all return the same `401 invalid_credentials`.
  - argon2 hash/verify run via `run_in_threadpool` (C2).
- **Create user:** `INSERT … ON CONFLICT (username) DO NOTHING RETURNING id`; no row → `409 username_taken`.
- **Edit user (`PATCH /users/{user_id}`):**
  - Body: `UserUpdate` with all fields optional: `username`, `role`, `password`, `is_active`. An empty body → 422.
  - A rename to a taken name → 409. This is an UPDATE, so it catches `IntegrityError` (a single statement, so the transaction abort doesn't matter).
  - **Last-admin guard:** if the change demotes or deactivates an active admin:
    1. `SELECT id FROM app_user WHERE role='admin' AND is_active ORDER BY id FOR UPDATE` (fixed lock order: no deadlock).
    2. Reject with `409 last_admin` if no other active admin would remain.
    - Two admins demoting each other concurrently → the second waits, re-checks and gets 409.
  - Self-edits are allowed (e.g. changing your own password), but the last-admin rule still applies to yourself.
  - `is_active=false` or a `password` change → revoke all of the user's refresh tokens in the same transaction.
  - No hard delete: users are referenced by movements (`performed_by`), so deactivation is the delete.
- **Config (C4), `AuthConfig`:**
  - `JWT_SECRET: SecretStr` required, ≥ 32 chars.
  - `JWT_ALG`, `JWT_ACCESS_EXP_MINUTES`, `JWT_REFRESH_EXP_DAYS`.
  - Optional `FIRST_ADMIN_USERNAME` / `FIRST_ADMIN_PASSWORD`.
  - Compose uses `${JWT_SECRET:?…}`; `conftest` sets a test secret.
- **Seed:**
  - Lifespan startup: `INSERT … ON CONFLICT DO NOTHING`. Create-only; never overwrites.
  - Tests call the function directly, because ASGITransport doesn't run the lifespan.
- **Still out of scope:**
  - `GET /users` list: needs pagination, which is a Step 5 foundation. Easy to add then.
  - Cleanup of expired refresh-token rows: harmless at this scale. A periodic `DELETE … WHERE expires_at < now() - interval '30 days'` would do.

## Dependency Graph
```
deps + exceptions/ErrorCode + TimestampMixin + src/models.py registry
   └── T8  app_user (0002) + hashing/JWT utils + POST /auth/token
          └── T9  dependencies chain + GET /auth/me + POST,GET /users + test fixtures
                 └── T10 refresh_token (0003) + login returns pair + /auth/refresh + /auth/logout
                        └── T11 PATCH /users/{user_id} (+ last-admin lock, refresh revocation)
                               └── T12 first-admin seed + compose/.env
```

## Task List
- [x] Task 8: Login slice: error foundation, `TimestampMixin`, `app_user` + migration, `POST /auth/token`
- [x] Task 9: Protected access: dependency chain, `GET /auth/me`, `POST /users`, `GET /users/{user_id}`, fixtures
- [x] Task 10: Refresh tokens: `refresh_token` + migration, rotation, reuse detection, logout

### Checkpoint A (after T10)
- [x] Suite green; ruff + mypy clean; alembic round-trip OK

- [x] Task 11: `PATCH /users/{user_id}`: edit, deactivate, last-admin guard, refresh revocation
- [x] Task 12: First-admin seed on startup + compose/.env config

### Checkpoint: Step 2 complete
- [x] `uv run pytest` green: 3 existing + 5 new test functions (login, access matrix, refresh, user update, seed)
- [x] ruff + mypy clean; alembic `base ↔ head` round-trip; `alembic check` reports no diff
- [x] Compose: log in as the seeded admin via curl → access works → refresh works → logout → the refresh token is rejected. `docker compose restart api` → healthy, still one admin.
- [x] Human review: approved 2026-11-23 (committed a69153b)

## Risks and Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| Models not imported → empty metadata → autogenerate drops tables / TRUNCATE skips them | High | `src/models.py` registry; `alembic check` at the checkpoint. |
| Form login fails without `python-multipart` | Med | Added as a dependency; the T8 test hits the form endpoint. |
| Concurrent refreshes with one token both succeed (double issuance) | High | A single conditional `UPDATE … RETURNING`. The T10 test fires two refreshes with `gather` → exactly one 200. |
| A legitimate client retrying a refresh (network blip) trips reuse detection and is logged out | Low | This is accepted, as in OAuth BCP strict rotation. A short reuse grace window is a documented possible extension. |
| Two admins demote each other → zero admins | High | `FOR UPDATE` on the active-admin rows in id order. The T11 test runs both PATCHes with `gather` → exactly one 200, one 409. |
| `updated_at` silently not bumped | Low | `onupdate` on the mixin; no raw-text UPDATEs. The T11 test asserts `updated_at` advanced. |
| Argon2 slows the suite | Low | Users are created through the service. Tokens for fixtures are minted directly, not via login. |
| Seeding overwrites a rotated admin password | Med | `ON CONFLICT DO NOTHING`; tested. |

---

# Implementation Plan: Step 3 — Unified errors, pagination, warehouses & products

Parent plan: [../PLAN.md](../PLAN.md) Step 3, conventions C1–C12, decisions D8–D10.

## Overview
Make every error response share one shape, add the pagination foundation, and build the catalog domains that movements will reference: warehouses and products.

New endpoints:

| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/users` | admin | paginated (closes the Step 2 gap) |
| POST | `/warehouses` | admin | `ON CONFLICT (location_code)` → 409 |
| GET | `/warehouses` | any user | paginated, optional `is_active` filter |
| GET | `/warehouses/{warehouse_id}` | any user | |
| PATCH | `/warehouses/{warehouse_id}` | admin | name, address, location_code, is_active |
| POST | `/products` | admin | `ON CONFLICT (sku)` → 409 |
| GET | `/products/{product_id}` | any user | 404 if soft-deleted; the stock breakdown is added in Step 4 |
| PATCH | `/products/{product_id}` | admin | name, base_price, min_stock_level; **SKU immutable** |
| DELETE | `/products/{product_id}` | admin | soft delete → 204; already deleted → 404 |

`GET /products` (list with stock, filters, sort) is **Step 5**, since it's the aggregation endpoint.

## Architecture Decisions
- **Unified error format (fixes the Step 2 gap)** in `src/exceptions.py`:
  - `RequestValidationError` → `422 {"code": "validation_error", "detail": "Request validation failed", "errors": [{"loc", "msg", "type"}]}`.
  - Starlette `HTTPException` (unknown route, 405, FastAPI internals) → `{"code": <by status: not_found | method_not_allowed | http_error>, "detail": exc.detail}`, keeping `exc.headers` (e.g. `Allow`).
  - Unhandled `Exception` → `500 {"code": "internal_error", "detail": "Internal server error"}`, logged with traceback. No internals leak to the client.
  - The existing Step 2 test that reads `detail[...].loc` switches to `errors`.
- **Pagination** (`src/pagination.py`):
  - `PageParams` is a dependency: `limit: Query(20, ge=1, le=100)`, `offset: Query(0, ge=0)`.
  - `Page[T]` is a generic response model.
  - `paginate(db, stmt, params)` adds `COUNT(*) OVER()` to the same SELECT, so the page and total come from one snapshot. Only when an offset past the end returns no rows does it run a separate `COUNT(*)`.
  - Ordering is the caller's job, always with a unique tie-breaker (`id`).
- **Auth wiring:**
  - Domain routers are `APIRouter(dependencies=[Depends(current_user)])`, so every route needs a logged-in user.
  - Write routes add `dependencies=[Depends(admin_user)]` on the decorator.
  - Admins therefore pass every check, and a storekeeper gets 403 on writes.
- **Warehouse** (`src/warehouses/`):
  - `warehouse`: `id`, `name` (1..255), `location_code` (unique; trim + upper-case; `^[A-Z0-9][A-Z0-9_-]{0,31}$`), `address` (1..500, required), `is_active`, timestamps. Migration `0004`.
  - `valid_warehouse_id` dependency → 404 `warehouse_not_found`.
  - Inactive warehouses stay readable; the list can filter by `is_active`.
- **Product** (`src/products/`):
  - `product`:
    - `id`, `sku` (unique **including soft-deleted** per D9; trim + upper-case; `^[A-Z0-9][A-Z0-9._-]{0,63}$`), `name`, `is_deleted`, timestamps
    - `base_price` `Numeric(12,2)` with `CHECK >= 0`
    - `min_stock_level` `Integer` with `CHECK >= 0`
    - Migration `0005`.
  - `base_price`:
    - Input: a `Decimal` with `ge=0`, `max_digits=12`, `decimal_places=2`. So `10.005` → 422, and floats are accepted without binary rounding.
    - Output: a JSON **string** (Pydantic v2's default for Decimal), as D10 requires.
  - `valid_product_id` (exists and not deleted) → 404 `product_not_found`. Deleted products look gone everywhere.
  - Duplicate SKU → `409 sku_taken`, "SKU already exists (possibly on a deleted product)", so the client understands why a "missing" SKU still conflicts.
  - **SKU is immutable.** The batch API identifies products by SKU, and the spec calls it the unique article number. Renaming is better modeled as a new product. This also leaves product PATCH with no unique field, so no `IntegrityError` path.
- **PATCH semantics:** same as users: all fields optional, omitted/null = unchanged, an empty body → 422. No nullable domain fields exist, so null never means "clear".
- **String hygiene:** `name`/`address` are trimmed per field. There's no global `str_strip_whitespace`, because that would silently strip leading/trailing spaces from passwords.
- **D8 guards deferred to Step 4.** Stock = 0 checks need the movement ledger. Until then, deactivate and soft-delete are unconditional; PLAN.md Step 4 carries the task.
- **Future hooks (not built now):** price changes and product/warehouse create/delete must invalidate the valuation cache (Step 6). Each service function gets a single `# Step 6: invalidate valuation cache` marker at its commit point.

## Dependency Graph
```
T13 unified error handlers ─────────────┐
T14 pagination + GET /users ─────────────┤ (both used by)
                                         ├── T15 warehouses (model 0004, CRUD, list)
                                         └── T16 products   (model 0005, CRUD, soft delete)
```
T15 and T16 are independent of each other; both are sequenced after T13/T14.

## Task List
- [x] Task 13: Unified error format for FastAPI/Starlette/unhandled errors
- [x] Task 14: Pagination foundation + `GET /users`

### Checkpoint A (after T14)
- [x] Suite green; ruff + mypy clean

- [x] Task 15: Warehouses: model + migration `0004` + create/list/get/patch
- [x] Task 16: Products: model + migration `0005` + create/get/patch/soft-delete

### Checkpoint: Step 3 complete
- [x] `uv run pytest` green (18 existing + 3 new test functions); ruff + mypy clean
- [x] Alembic `base ↔ head` round-trip; `alembic check` no diff
- [x] Compose: as the seeded admin, create a warehouse and a product via curl; a storekeeper can read them but gets 403 on writes
- [x] Commit (no self-credit); human review, then plan Step 4

## Risks and Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| The `Exception` handler makes tests raise instead of returning a 500 (`ASGITransport` re-raises by default) | Med | Don't test the 500 path through the client; unit-test the handler function, or skip it. The 422/404/405 paths are tested via HTTP. |
| `COUNT(*) OVER()` gives no total when the page is empty | Low | Fallback `COUNT(*)` only when no rows come back; covered by a "page past the end" assertion. |
| Decimal precision / float input surprises (`0.1 + 0.2`) | Med | `Numeric(12,2)` + a `decimal_places=2` validator; asserted as string output `"12.50"`. |
| Deleted SKU reuse confuses API users | Low | 409 message names the possibility explicitly; D9 documented in the README later. |
| Catalog writes forget cache invalidation in Step 6 | Med | Explicit marker comments at each commit point; Step 6 plan greps for them. |

---

# Implementation Plan: Step 4 — Movement engine

Parent plan: [../PLAN.md](../PLAN.md) Step 4. Spec 1 (no stored stock), 2.1 (race conditions) and 2.2 (atomic batches). Decisions D1–D5 and D8. **All three required tests live here.**

## Overview
Add the append-only movement ledger, the locking engine that writes IN/OUT/TRANSFER movements without ever overselling, all-or-nothing batches with detailed shortages, stock computed from history, the D8 "stock must be 0" guards, and movement history.

| Method | Path | Who | Notes |
|---|---|---|---|
| POST | `/movements` | any user | single line: `{type, sku, quantity, from_warehouse_id?, to_warehouse_id?}` |
| POST | `/movements/batch` | any user | `{type, from_warehouse_id?, to_warehouse_id?, items: [{sku, quantity}]}` (1..100) |
| GET | `/movements` | any user | history; filters `product_id`, `warehouse_id` (either side), `type`, `date_from`, `date_to`; paginated, newest first |
| GET | `/warehouses/{warehouse_id}/stock` | any user | products currently in the warehouse (qty > 0), paginated, one query |
| GET | `/products/{product_id}` | any user | **extended:** `total_stock` + `stock_by_warehouse` (nonzero only), one query |
| PATCH | `/warehouses/{id}` | admin | **guarded:** `is_active=false` → 409 `warehouse_not_empty` if it holds stock |
| DELETE | `/products/{id}` | admin | **guarded:** 409 `product_has_stock` if total stock ≠ 0 |

## Architecture Decisions
- **Ledger tables** (`src/movements/models.py`, migration `0006`). Append-only, so `created_at` only:
  - `operation`:
    - `id` (bigint identity), `type`, `performed_by` → `app_user`, `idempotency_key` (nullable, ≤ 255), `created_at`
    - `UNIQUE (performed_by, idempotency_key)`, which allows many NULLs. The column and constraint go in now so Step 8 needs no migration; until then the column is always NULL.
  - `stock_movement`:
    - `id` (bigint), `operation_id` → `operation`, `type`, `product_id` → `product`, `quantity`, `from_warehouse_id` / `to_warehouse_id` → `warehouse`, `performed_by` → `app_user`, `created_at`
    - FKs `ON DELETE RESTRICT`: ledger rows never disappear.
    - CHECK `quantity BETWEEN 1 AND 1000000`.
    - CHECK `movement_shape`: `(IN ∧ from IS NULL ∧ to NOT NULL) ∨ (OUT ∧ from NOT NULL ∧ to IS NULL) ∨ (TRANSFER ∧ both NOT NULL ∧ from <> to)`. The DB rejects invalid rows even if the service is buggy.
    - Indexes: `(product_id, from_warehouse_id)`, `(product_id, to_warehouse_id)`, `(from_warehouse_id)`, `(to_warehouse_id)`, `(operation_id)`, `(created_at)`.
  - `type`: VARCHAR + CHECK (`IN`/`OUT`/`TRANSFER`, as in the spec), like `role`.
- **Stock = SQL over the ledger** (`src/movements/stock.py`), never stored (D1):
  - Source warehouse stock for several products in one grouped query:
    `SUM(qty) FILTER (WHERE to=:w) − SUM(qty) FILTER (WHERE from=:w)`, wrapped in `COALESCE`, `WHERE product_id IN (…) AND :w IN (from, to)`.
  - Total product stock: `SUM(CASE type WHEN 'IN' THEN qty WHEN 'OUT' THEN −qty ELSE 0 END)`. TRANSFER nets to zero.
  - "Warehouse holds stock": `EXISTS` over the per-product net stock in that warehouse `HAVING net <> 0`.
  - Per-warehouse breakdown: a signed `UNION ALL` ledger (to: +q, from: −q) grouped by warehouse and joined with `warehouse`, aggregated with `json_agg`. Product + total + breakdown come from **one** statement (C11).
- **Engine: one function for single and batch** (D2):
  - `movements_service.execute(db, user, command)` with `command = {type, from, to, items}`. The single endpoint builds a one-item command.
  - **Batch allows IN too.** The spec requires OUT/TRANSFER batches; IN reuses the same engine, so there's no extra code path.
  - Inside one transaction, under **explicit READ COMMITTED** (D4), set on the engine with `isolation_level="READ COMMITTED"`:
    0. `SET LOCAL lock_timeout = '<LOCK_TIMEOUT_MS>ms'` (default 5000).
    1. **Warehouses:** `SELECT … WHERE id IN (from, to) ORDER BY id FOR SHARE`.
       - Missing → 404 `warehouse_not_found`. Inactive → 409 `warehouse_inactive`.
       - `FOR SHARE` lets movements run concurrently but blocks a concurrent deactivation (`FOR UPDATE`).
    2. **Products:** `SELECT id, sku FROM product WHERE sku IN (…) AND NOT is_deleted ORDER BY id`, with `FOR UPDATE` for OUT/TRANSFER and `FOR SHARE` for IN.
       - Missing or deleted SKUs → 404 `products_not_found` with `missing_skus: [...]` (**all** of them).
       - Why IN needs a lock at all: an FK insert only takes `KEY SHARE`, which doesn't block a soft-delete UPDATE.
    3. **OUT/TRANSFER:** one grouped stock query for the source warehouse. This is a new statement taken after the lock, so under READ COMMITTED it sees the previous lock holder's committed movements. Collect **every** shortage; if any →
       `409 {"code":"insufficient_stock", "detail":…, "shortages":[{"sku","requested","available","missing"}]}`.
       Nothing was written, and the session rolls back on close.
    4. Insert the `operation` (RETURNING id), then bulk-insert the movements; commit.
    5. Return `OperationRead {id, type, performed_by, created_at, movements: [...]}` → 201.
       `# Step 6` (cache invalidation) and `# Step 7` (low-stock crossings: the before-stock is already known from step 3) markers go here.
  - **Lock order** is always warehouses → products, each in id order.
    - The D8 guards take a single row each (warehouse `FOR UPDATE`, or product `FOR UPDATE`), then only read the ledger.
    - So no cycle is possible: deadlock-free by construction.
  - **Lock timeout / deadlock** (SQLSTATE `55P03` / `40P01`) → `503 {"code":"service_busy"}` + `Retry-After: 1`, instead of a hung or 500 request. `LOCK_TIMEOUT_MS` lives in `MovementsConfig`, so tests can shrink it.
- **Schemas** (`src/movements/schemas.py`):
  - The shape is validated by type: IN requires `to` and forbids `from`; OUT the reverse; TRANSFER needs both, and `from ≠ to`.
  - SKUs are normalized like products and **unique within a batch** (D5: duplicates → 422, so `[A:5, A:5]` can't pass two separate checks).
  - `quantity` is 1..1,000,000 and `items` is 1..100.
  - All of this returns 422 in the unified format, before any DB work.
- **D8 guards** (replacing the Step 3 markers):
  - `update_warehouse` with `is_active=false` → re-select the warehouse `FOR UPDATE` (`populate_existing`) → if it holds stock, 409 `warehouse_not_empty`.
  - `soft_delete_product` → re-select the product `FOR UPDATE` → if total stock ≠ 0, 409 `product_has_stock`.
  - The locks conflict with the movement engine's `FOR SHARE`/`FOR UPDATE`, so a concurrent IN can't slip in between the check and the flag flip.
- **Roles:** both storekeeper and admin can create movements and read history (spec 2.6).
- **Out of scope here:** cache invalidation (Step 6), notifications (Step 7), idempotency logic (Step 8), rate limiting (Step 9). Markers only.

## Dependency Graph
```
T17 ledger tables (0006) + stock queries + POST /movements (IN only path)
     + GET /products/{id} stock breakdown + schema validation
   └── T18 OUT/TRANSFER: product/warehouse locks, shortage check, batch endpoint,
           lock-timeout → 503                       ← the 3 required tests + timeout test
          └── T19 D8 guards (warehouse deactivate / product delete) + GET /movements
```

## Task List
- [x] Task 17: Ledger + stock queries + IN movements + product stock breakdown + input validation
- [x] Task 18: Locking engine for OUT/TRANSFER + batch endpoint + lock timeout → 503

### Checkpoint A (after T18): the core of the assignment
- [x] The 3 required tests are green and stable (5 consecutive full-suite runs)
- [x] **Mutation check:** with the product `FOR UPDATE` temporarily removed, the race test fails (oversell). Restored afterwards.
- [x] ruff + mypy clean; alembic round-trip + `alembic check`

- [x] Task 19: D8 guards + `GET /movements` history + `GET /warehouses/{id}/stock`
- [x] Task 20: Lazy cleanup of old refresh tokens on login (independent; added at review)

### Checkpoint: Step 4 complete
- [x] `uv run pytest` green (22 existing + 6 new test functions)
- [x] Compose:
  - IN → TRANSFER → OUT via curl with the stock breakdown correct
  - an oversell attempt → 409 with shortages
  - deactivating a non-empty warehouse → 409
- [x] Commit (no self-credit); human review, then plan Step 5

## Risks and Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| The race test passes for the wrong reason (requests serialized, not concurrent) | High | Pool size 20 ≥ 10 concurrent requests. No shared test transaction (Step 1 harness). The **mutation check** proves the lock is what prevents overselling. |
| REPEATABLE READ creeping in (driver/default change) → stale SUM after the lock wait | High | `isolation_level="READ COMMITTED"` set explicitly on the engine, with a comment pointing to D4. |
| Deadlocks between batches with overlapping SKUs | Med | A single locking statement per table with `ORDER BY id`; fixed warehouse → product order; the D8 guards lock one row. 40P01 maps to 503 as a last resort. |
| Duplicate SKUs in one batch bypass the shortage check | High | Rejected in the schema (422); a test in T17. |
| Unknown/deleted SKUs reported one at a time | Low | Compare requested vs locked SKU sets; list all missing; tested. |
| A long lock wait hangs requests | Med | `SET LOCAL lock_timeout`, plus a deterministic test (hold the row lock in another session → 503). |
| `json_agg` breakdown returns `null` for a product with no stock | Low | `COALESCE(json_agg(...), '[]')`; asserted. |
| Ledger grows large → slow SUMs | Low (scale) | Composite indexes; snapshot+delta noted as a trade-off in PLAN.md §4. |

---

# Implementation Plan: Step 5 — SQL aggregations without N+1

Parent plan: [../PLAN.md](../PLAN.md) Step 5. Spec 2.3: the product list with computed stock (filter, sort by stock and price, paginate) and the financial valuation per warehouse, each from **one efficient SQL query** (COALESCE, SUM(CASE…), FILTER).

## Overview
| Method | Path | Who | Notes |
|---|---|---|---|
| GET | `/products` | any user | products + `stock`; filters, sorting, pagination; **1 data statement** |
| GET | `/reports/valuation` | admin | per-warehouse value (stock × current base price) + grand total; **1 data statement** |

## Architecture Decisions
- **Product list query** (`products_service.list_products`):
  ```sql
  SELECT p.*, COALESCE(s.stock, 0) AS stock, COUNT(*) OVER () AS _total
  FROM product p
  LEFT JOIN (SELECT product_id, <stock aggregate> AS stock
             FROM stock_movement [WHERE :w IN (from, to)]
             GROUP BY product_id) s ON s.product_id = p.id
  WHERE NOT p.is_deleted AND <filters>
  ORDER BY <whitelisted sort> <dir>, p.id <dir>
  LIMIT :limit OFFSET :offset
  ```
  - **Pre-aggregate, then join:** the ledger is grouped once by `product_id`, then LEFT JOINed. That's cheaper than joining every movement to its product and grouping by all product columns. Products without movements get stock 0 through `COALESCE`.
  - Stock aggregate: without `warehouse_id` it's `SUM(CASE IN +q / OUT −q / TRANSFER 0)`, the total across warehouses. With `warehouse_id` it's `SUM(q) FILTER (to=:w) − SUM(q) FILTER (from=:w)`, the stock in that warehouse. Both reuse `movements/stock.py`.
  - Stock filters go in the outer `WHERE` on `COALESCE(s.stock, 0)`, so no `HAVING` is needed and zero-stock products filter correctly.
  - `total` comes from `COUNT(*) OVER()` in the same statement (Step 3 `paginate`).
- **Query parameters** are a Pydantic query model (`Annotated[ProductListQuery, Query()]`), validated before any SQL:
  - `search`: case-insensitive substring of `sku` or `name`, matched with `ILIKE` with `%`, `_` and `\` **escaped**, so a user's `%` or `_` matches literally instead of acting as a wildcard.
  - `min_price` / `max_price` (Decimal ≥ 0) and `min_stock` / `max_stock` (int). `min > max` → 422.
  - `below_min_stock: bool`: `stock < min_stock_level`, the same per-warehouse meaning when `warehouse_id` is given.
  - `warehouse_id`: unknown → 404 (one extra PK lookup, only when the parameter is used).
  - `sort ∈ {id, sku, name, price, stock}` (default `id`) and `order ∈ {asc, desc}`. Anything else → 422 from the `Literal` type, never interpolated. The `p.id` tie-breaker follows the same direction, so pages are stable.
- **Response:** `Page[ProductListItem]`, where `ProductListItem` = `ProductRead` + `stock: int`. The field is named `stock`, not `total_stock`, because with `warehouse_id` it's the warehouse's stock.
- **Valuation query** (`src/reports/`):
  ```sql
  SELECT w.id, w.name, w.location_code, w.is_active,
         COALESCE(SUM(CASE WHEN m.to_warehouse_id = w.id THEN m.quantity
                           ELSE -m.quantity END * p.base_price), 0)::numeric(20,2) AS total_value,
         COALESCE(SUM(CASE WHEN m.to_warehouse_id = w.id THEN m.quantity
                           ELSE -m.quantity END), 0) AS total_units
  FROM warehouse w
  LEFT JOIN stock_movement m ON w.id IN (m.from_warehouse_id, m.to_warehouse_id)
  LEFT JOIN product p ON p.id = m.product_id
  GROUP BY w.id ORDER BY w.id
  ```
  - Σ(movement qty × current price) = Σ(stock × price), because the price is fixed per product. So there's no intermediate per-product stock step, and it's still a single statement.
  - The product join is a **LEFT JOIN**. An inner join would silently drop warehouses with no movements, and those must show `0.00`.
  - `::numeric(20,2)`: empty warehouses render `"0.00"`, not `"0"`, and sums can't overflow `numeric(12,2)`.
  - Deleted products contribute 0, since D8 guarantees their stock is zero everywhere. Inactive warehouses are listed with `is_active: false` (also 0 by D8).
  - The `grand_total` is summed in Python from the same rows (Decimal), so there's no second query. `generated_at` is included, which Step 6 will need to show cache freshness.
  - `/reports` router: `dependencies=[Depends(admin_user)]`, so the whole report router is admin-only (spec 2.6).
  - I'll verify the plan with `EXPLAIN` during implementation. The expected shape is a nested loop over warehouses with a BitmapOr on the two warehouse indexes. If it degrades to a seq scan per warehouse, the fallback is to pre-aggregate the signed ledger per `(warehouse, product)` and join prices once.
- **N+1 guard (test infrastructure):** a `count_statements` fixture hooks SQLAlchemy's `before_cursor_execute` on the app engine.
  - The test asserts an **exact** count per request: 1 auth lookup + 1 data statement.
  - It also asserts that the count is **identical with 3 and with 12 products**. A per-row query would make it grow, which is the real N+1 proof.
- **Out of scope:** caching (Step 6). A marker goes where the valuation is computed.

## Dependency Graph
```
movements/stock.py aggregates (Step 4) ─┬── T21 GET /products (list + filters + sort)
pagination (Step 3) ────────────────────┤
                                        └── T22 GET /reports/valuation
T21 + T22 ── T23 statement-count fixture + N+1 test (both endpoints)
```

## Task List
- [x] Task 21: `GET /products`: list with stock, filters, sorting, pagination
- [x] Task 22: `GET /reports/valuation`: one-query valuation, admin-only
- [x] Task 23: N+1 guard: statement-count fixture + scale-invariance test

### Checkpoint: Step 5 complete
- [x] `uv run pytest` green (29 existing + 3 new test functions); ruff + mypy clean
- [x] `EXPLAIN` of both queries reviewed (index use, no per-row subplans); notes recorded in the task
- [x] Compose: `GET /products?sort=stock&order=desc` and `GET /reports/valuation` via curl match the ledger
- [x] Commit (no self-credit); human review, then plan Step 6

## Risks and Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| LIKE wildcards in `search` (e.g. `50%`) match too much | Low | Escape `\`, `%`, `_` and use `ILIKE … ESCAPE '\'`; tested with a literal `_`. |
| Sort by a stock value shared by many products → unstable pages | Med | Always add the `p.id` tie-breaker in the same direction; tested by paging through the sort. |
| Inner join drops empty warehouses from the valuation | Med | LEFT JOINs + COALESCE; an empty warehouse is asserted as `"0.00"`. |
| `0` vs `0.00` / numeric overflow in sums | Low | Cast to `numeric(20,2)`. |
| The statement counter sees unrelated statements (pool pre-ping, auth) | Med | Assert exact numbers measured once **and** invariance across data sizes; document what each counted statement is. |
| The `OR` join in the valuation scales poorly with many warehouses | Low | EXPLAIN check, with a documented pre-aggregation fallback. |

---

# Implementation Plan: Step 6 — Redis cache for the valuation report

Parent plan: [../PLAN.md](../PLAN.md) Step 6. Spec 2.4: the valuation result is cached in Redis and **consistently invalidated** when a new movement appears.

## Architecture Decisions
- **Generation counter, not DELETE** (`src/reports/cache.py`):
  - Read `valuation:gen` (missing = 0), then `GET valuation:v:{gen}`.
  - Hit → return it, with `X-Cache: HIT`.
  - Invalidate = `INCR valuation:gen` **after** the DB commit.
  - Why: a slow reader that computed pre-commit data stores it under the old generation, which nobody reads again. With DELETE, that stale value would be written back and served until the next movement.
  - Why after commit: an INCR before commit could let a reader cache pre-commit data under the new generation.
  - The generation is read **before** the DB query. If the data is newer than the generation, it's still correct to cache under it.
- **Stampede guard:** on a miss, `SET valuation:lock:{gen} NX EX 5`.
  - The winner computes, `SET valuation:v:{gen} … EX ttl`, and releases the lock.
  - Losers poll the value key (50 ms steps, ~1 s max), then compute uncached. Correctness never depends on the lock.
- **What invalidates:** every successful write to a table the report reads.
  - Movements: single and batch.
  - Products: create, patch (price), delete.
  - Warehouses: create, patch (name, code, `is_active` all appear in the report).
  - This is broader than "new movement" (the spec's minimum) because a price change or a warehouse rename would otherwise serve wrong data for the whole TTL.
- **Where:** a `ValuationCache` dependency used in the **routers**, called after the service returns. Errors raise before reaching it, so a rolled-back request never invalidates. Services stay Redis-free.
- **TTL:** `VALUATION_CACHE_TTL_S` (default 300) is a safety net, bounding staleness if an INCR is lost (Redis blip right after commit).
- **Redis unavailable:**
  - Reads fall back to computing from the DB (`X-Cache: BYPASS`).
  - Invalidation failures are logged. The request still succeeds, since the DB commit already happened.
- **Serialization:** `ValuationReport.model_dump_json()` / `model_validate_json()`; Decimals stay exact strings.

## Task List
- [x] Task 24: `reports/cache.py` (generation, stampede lock, fallback) + `X-Cache` header + `ValuationCache` dependency
- [x] Task 25: Invalidation wired into every write route
- [x] Task 26: Tests: hit/miss/invalidation/all write routes/Redis down; deterministic stale write-back race; stampede (1 compute for 10 concurrent cold requests)

### Checkpoint: Step 6 complete
- [x] Suite green; ruff + mypy clean; compose: MISS → HIT → movement → MISS with the new value
- [x] Commit (no self-credit)

## Risks and Mitigations
| Risk | Impact | Mitigation |
|------|--------|------------|
| A write route forgets to invalidate | High | A test bumps every write endpoint and asserts the generation increased. |
| Stale write-back race | High | Generation keys + a deterministic interleaving test. |
| Redis outage breaks the report or the writes | Med | Every Redis call is wrapped; BYPASS read path; invalidation is log-only on failure; tested with an unreachable Redis. |
| Thundering herd on a cold cache | Low | NX lock + bounded polling; tested (1 compute for 10 concurrent requests). |

---

# Implementation Plan: Step 7 — Low-stock background notifications

Parent plan: [../PLAN.md](../PLAN.md) Step 7. Spec 2.5: when a write-off takes a product's stock below `min_stock_level`, start a FastAPI `BackgroundTasks` job that "sends an email" (logging the send is enough). The HTTP response must not block on it.

## Architecture Decisions
- **When (D6):**
  - Per **source warehouse**, for OUT and TRANSFER: both deplete the source.
  - Only on the **crossing**: `before >= min_stock_level > after`, so later write-offs below the threshold don't re-alert.
  - `min_stock_level = 0` can never alert, since stock can't go below 0.
- **Data comes free from the engine:**
  - The engine already computes `before` (the locked stock read) for every item. `after = before - qty`.
  - `_lock_products` additionally selects `name` and `min_stock_level` in the same statement.
  - The crossings are computed under the lock, so they're consistent even under concurrency: of N concurrent write-offs, exactly the one that crosses alerts.
- **Shape:**
  - `movements_service.execute` returns `ExecutionResult(operation, low_stock: list[LowStockAlert])`.
  - `LowStockAlert` is **plain data** (product id/sku/name, warehouse id, before, after, min level, operation id). The DB session is closed by the time the task runs, so no ORM objects or sessions go into the task.
- **Notifier** (`src/notifications/`):
  - A `Notifier` protocol, with `LoggingNotifier.send_low_stock(alerts)` logging one "email sent to <LOW_STOCK_ALERT_EMAIL>" line per alert.
  - Provided through a dependency (`get_notifier`), so tests swap it with `app.dependency_overrides`.
  - `safe_send()` wraps it: any exception is logged and swallowed. A broken mail server must never surface as an error after the client already got its 201. Starlette would otherwise re-raise it in the server, and in tests through the transport.
- **Scheduling:**
  - Router: `background_tasks.add_task(safe_send, notifier, alerts)` only when `alerts` is non-empty, and only after a successful commit. A rolled-back request (409, 404…) never notifies.
  - Starlette sends the response first, then runs the task.
- **Config:** `LOW_STOCK_ALERT_EMAIL` (default `inventory-alerts@example.com`, a reserved example domain).

## Task List
- [x] Task 27: Engine computes low-stock crossings (`ExecutionResult`, `LowStockAlert`)
- [x] Task 28: Notifications package (protocol, logging notifier, `safe_send`, dependency) + scheduling in both movement routes
- [x] Task 29: Test `test_low_stock_notifications`:
  - crossing alerts once; no re-alert below the threshold
  - TRANSFER alerts per source warehouse
  - a batch with 2 crossings → 2 alerts
  - a failed request → none
  - a raising notifier → still 201
  - the logging notifier writes the "email sent" line

### Checkpoint: Step 7 complete
- [x] Suite green; ruff + mypy clean; compose: an OUT crossing the threshold logs "email sent" **after** uvicorn's `201` access line
- [x] Commit (no self-credit)

---

# Implementation Plan: Step 8 — Idempotency-Key (bonus 1)

Parent plan: [../PLAN.md](../PLAN.md) Step 8. Bonus spec: support an `Idempotency-Key` header, cache the result in Redis for 24 h, and make sure a repeated request never writes off twice.

## Architecture Decisions
- **Scope:** `POST /movements` and `POST /movements/batch`.
  - The header is optional: 1–255 visible ASCII characters (`[\x21-\x7E]`), else 422.
  - Keys are scoped **per user**, so the same key from two users never collides.
- **Fingerprint:** `sha256(path + canonical JSON of the *validated* body)`.
  - The same semantic request matches even if formatting differs (e.g. `sku: "p-1"` vs `"P-1"`).
  - Single vs batch never match each other.
- **Two layers:**
  1. **Redis (fast path, 24 h result cache)**, key `idem:{user_id}:{key}`:
     - `SET … {"state":"processing","fp","token"} NX EX 30`.
     - If the key is taken, look at the record:
       - `processing` + same fp → `409 idempotency_in_progress` + `Retry-After: 1`.
       - `done` + same fp → **replay** the stored status and body with `Idempotent-Replayed: true`.
       - Any other fp → `422 idempotency_key_reused`.
     - On success → `SET {"state":"done","fp","status","body"} EX 86400`.
     - On any error → release the key with a compare-and-delete on the token (Lua), so a retry runs for real. A failed request wrote nothing, so re-executing it is not a duplicate.
  2. **Postgres (the guarantee):** the engine's **first** write is
     `INSERT INTO operation (…, idempotency_key, request_fingerprint) … ON CONFLICT (performed_by, idempotency_key) DO NOTHING RETURNING id`.
     - The `operation` insert moves to the start of the transaction (before the locks; deadlock-safe, since a waiter holds no locks).
     - A concurrent duplicate blocks on the unique index. If the first commits → DO NOTHING → load the stored operation and **replay it from the DB** (fingerprint checked, mismatch → 422). If the first rolls back → this one proceeds.
     - This covers the crash window (DB committed, Redis not updated), an expired `processing` marker, and **Redis being down** entirely.
- **Migration `0008`:** `operation.request_fingerprint varchar(64)` (nullable).
- **Replays** never invalidate the cache or notify: nothing new was written. *(Step 9 rule: replays won't consume rate-limit quota.)*
- **Redis errors** in the idempotency layer → log and fall back to the DB layer only.
- **Config:** `IDEMPOTENCY_TTL_S = 86400`, `IDEMPOTENCY_PROCESSING_TTL_S = 30` (well above `LOCK_TIMEOUT_MS`).

## Task List
- [x] Task 30: Engine: operation-first `ON CONFLICT` insert, DB replay, migration `0008`
- [x] Task 31: `src/idempotency/` Redis layer + header dependency + wiring in `_record`
- [x] Task 32: Tests:
  - sequential replay
  - payload/endpoint mismatch → 422
  - per-user scope
  - a failed request releases the key
  - invalid header → 422
  - concurrent duplicates → exactly one operation
  - Redis down → DB backstop replays and rejects mismatches

### Checkpoint: Step 8 complete
- [x] Suite green; ruff + mypy clean; alembic check; compose replay via curl
- [x] Commit (no self-credit)

---

# Implementation Plan: Step 9 — Sliding-window rate limiting (bonus 2)

Parent plan: [../PLAN.md](../PLAN.md) Step 9. Bonus spec: at most 50 transactions per minute per employee, enforced through Redis with a sliding-window algorithm.

## Architecture Decisions
- **What counts:** every movement write attempt (`POST /movements`, `POST /movements/batch`).
  - A batch is **one** transaction (one DB transaction).
  - Failed business attempts (e.g. 409 insufficient stock) count too: the limit protects the system.
  - **Idempotent replays don't count** and are served even while the user is limited: they write nothing.
  - Reads are not limited.
- **Algorithm:** a sliding-window log, one ZSET per user (`rl:{user_id}`), in **one Lua script** so check-and-add is atomic (no two requests can both see 49):
  1. `now` = Redis `TIME` in ms. A single clock, so several API instances with skewed clocks agree.
  2. `ZREMRANGEBYSCORE key -inf now-window`.
  3. `ZCARD`.
  4. If below the limit: `ZADD now <uuid>` (a unique member, so two requests in the same millisecond don't merge) + `PEXPIRE window` → allowed, return the remaining count.
  5. Otherwise deny with `retry_after = oldest + window - now`.
- **Order in `_record`:** auth → idempotency `begin` (replays return here, quota untouched) → **rate limit** → engine. On a deny, the idempotency marker that was just claimed is released, so a later retry with the same key runs normally.
- **Responses:**
  - Allowed: `X-RateLimit-Limit` / `X-RateLimit-Remaining` headers.
  - Denied: `429 {"code": "rate_limited"}` + `Retry-After` (seconds, rounded up, ≥ 1).
- **Redis down → fail open,** with an error log. Availability of stock operations wins over the limiter.
- **Config:** `RATE_LIMIT_MAX_REQUESTS = 50` and `RATE_LIMIT_WINDOW_S = 60`. Tests patch the settings object.

## Task List
- [x] Task 33: `src/rate_limit/` (Lua sliding window, config, 429 exception) + wiring in `_record` + headers
- [x] Task 34: Tests:
  - limit reached → 429 + `Retry-After`; per user; reads unaffected; batch = 1; replay served while limited and not counted
  - concurrency: exactly *limit* of N parallel requests pass (atomicity)
  - the window slides (short window) and a 429 releases the idempotency key
  - Redis down → fail open

### Checkpoint: Step 9 complete
- [x] Suite green; ruff + mypy clean; compose: 51st request in a minute → 429
- [x] Commit (no self-credit)

---

# Implementation Plan: Step 10 — Finish

## Overview
Make the project easy to run, review and verify from a clean checkout.

## Decisions
- **README.md** is written for the reviewers of the test task:
  - quick start
  - configuration
  - API overview
  - **a map from each spec requirement to the code and the test that proves it**
  - key design decisions and known trade-offs
  - English, like the code.
- **Seed script** `python -m src.seed` (runs in the api container):
  - Idempotent: users/warehouses/products are created with `ON CONFLICT` semantics, and demo movements are only added when the ledger is empty.
  - Gives reviewers a storekeeper login and data to click through in `/docs`.
- **Fresh-checkout verification** in a **separate compose project** (`-p inventory-verify`, other host ports), so the developer's dev volume isn't wiped:
  - build → migrate → seed → smoke flow via curl
  - then `down -v` of that project only

## Task List
- [x] Task 35: `src/seed.py` (idempotent demo data)
- [x] Task 36: README.md (+ PLAN.md status)
- [x] Task 37: Fresh-project verification (separate compose project) + full suite + ruff/mypy/alembic check; commit
