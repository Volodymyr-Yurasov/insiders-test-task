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
- [ ] Human review, then plan Step 3

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
