# API Route Conventions

This document describes the **current, real** API route patterns used in this repository (derived from `app/api/routes/projects.py` and `app/api/routes/chapters.py`, plus shared helpers they rely on).

---

## 1) Route handler structure

### 1.1 Typical handler signature (Request + typed deps + params + body)

Most JSON endpoints follow this pattern:

```py
@router.get("/projects")
def list_projects(request: Request, db: DbDep, user_id: UserIdDep) -> dict:
    request_id = request.state.request_id
    ...
    return ok_payload(request_id=request_id, data={...})
```

Common traits (seen across `projects.py` / `chapters.py`):

- `request: Request` is used for:
  - `request.state.request_id` (response envelope)
  - occasionally `request.url.path` / `request.method` (e.g. streaming endpoints)
- `db: DbDep` is the SQLAlchemy session dependency used for `get/execute/add/commit/refresh`.
- `user_id: UserIdDep` is injected user id for authorization.
- Path params are simple primitives (`project_id: str`, `chapter_id: str`, ...).
- Request bodies are Pydantic models:
  - imported schemas (e.g. `ProjectCreate`, `ChapterCreate`, `BulkCreateRequest`)
  - or local models inside the route module (e.g. `ChapterPostEditAdoption`, `ProjectMembershipCreate`)
- Query params use FastAPI `Query(...)` with validation constraints:
  - e.g. `cursor: int | None = Query(default=None, ge=0)`
  - e.g. `limit: int = Query(default=..., ge=1, le=...)`
- Header params use FastAPI `Header(...)` (notably with explicit `alias=`):
  - e.g. `x_llm_provider: str | None = Header(default=None, alias="X-LLM-Provider", max_length=64)`

### 1.2 Return types and response envelope

- Most JSON endpoints annotate `-> dict` and return `ok_payload(...)`.
- Some endpoints return a streaming response (no `-> dict`), e.g. SSE via `create_sse_response(...)`.

Success responses are wrapped with:

```py
from app.core.errors import ok_payload

return ok_payload(request_id=request_id, data=...)
```

Output objects are typically produced via Pydantic:

- `ProjectOut.model_validate(model).model_dump()`
- `ChapterOut.model_validate(model).model_dump()`
- `ChapterDetailOut.model_validate(model).model_dump()`

This keeps the external JSON stable and avoids leaking SQLAlchemy internals.

### 1.3 Db/session write patterns (commit/rollback)

- After creating/updating/deleting, handlers call `db.commit()`.
- When handling uniqueness/race conditions, handlers catch `IntegrityError`, then:
  - `db.rollback()`
  - raise an `AppError.conflict(...)` (see chapters bulk/create flows).

---

## 2) Error handling (AppError + HTTP status codes)

### 2.1 Use AppError and let the global exception handlers format the response

Route code typically raises `AppError` directly (or calls `require_*` helpers that raise it) and does not manually build `JSONResponse`.

`app/main.py` registers exception handlers that convert errors into the standard envelope:

- `AppError` → uses `exc.status_code` and `error_payload(...)`
- `RequestValidationError` → `400 VALIDATION_ERROR`
- `SQLAlchemyError` → `500 DB_ERROR`
- unhandled `Exception` → `500 INTERNAL_ERROR`

### 2.2 Status codes used by AppError helpers (core set)

`app/core/errors.py` defines the common constructors:

- **400**: `AppError.validation(...)` (`code="VALIDATION_ERROR"`)
  - used for parameter checks like invalid roles / invalid foreign ownership checks
- **401**: `AppError.unauthorized(...)` (`code="UNAUTHORIZED"`)
  - most commonly triggered by auth deps (see `UserIdDep` / `get_current_user_id`)
- **404**: `AppError.not_found(...)` (`code="NOT_FOUND"`)
  - used for missing resources and also “fail-closed” access checks
- **500**: either:
  - `AppError(..., status_code=500)` for explicit internal failures, **or**
  - uncaught exceptions handled by the global handlers (see above)

Additional codes observed in the routes:

- **403**: `AppError.forbidden(...)` (typically raised in `require_*` access helpers)
- **409**: `AppError.conflict(...)` (e.g. membership already exists, chapter number conflicts)

---

## 3) Router organization

### 3.1 One router per domain module

Each domain file defines a single router:

```py
router = APIRouter()
```

Examples:

- `app/api/routes/projects.py` → project + membership endpoints
- `app/api/routes/chapters.py` → chapter endpoints (both nested under projects and direct chapter-id routes)

### 3.2 Prefix and tag grouping

- All API routes are mounted under the `/api` prefix via `app/api/router.py`:
  - `api_router = APIRouter(prefix="/api")`
- Domain routers are included with tags for OpenAPI grouping:
  - `api_router.include_router(projects.router, tags=["projects"])`
  - `api_router.include_router(chapters.router, tags=["chapters"])`

Route modules typically **do not** set `prefix=` on `APIRouter()`; paths in decorators include the full path under `/api`, e.g.:

- `@router.get("/projects")`
- `@router.get("/projects/{project_id}/chapters")`
- `@router.get("/chapters/{chapter_id}")`

### 3.3 Path patterns (examples from real routes)

- Collection routes use plural nouns:
  - `GET /api/projects`
  - `POST /api/projects`
- Nested resources include the parent id:
  - `GET /api/projects/{project_id}/chapters`
  - `POST /api/projects/{project_id}/chapters/bulk_create`
- Direct resource routes use a top-level id path:
  - `GET /api/chapters/{chapter_id}`
  - `PUT /api/chapters/{chapter_id}`
  - `DELETE /api/chapters/{chapter_id}`

---

## 4) Common dependencies

### 4.1 DbDep (database session)

Defined in `app/api/deps.py`:

```py
DbDep = Annotated[Session, Depends(get_db)]
```

Handlers use it as `db: DbDep`.

### 4.2 UserIdDep (current user id)

Defined in `app/api/deps.py`:

```py
def get_current_user_id(request: Request) -> str:
    user_id = getattr(request.state, "user_id", None)
    if isinstance(user_id, str) and user_id:
        return user_id
    raise AppError.unauthorized()

UserIdDep = Annotated[str, Depends(get_current_user_id)]
```

Notes:

- The repository currently uses `get_current_user_id` (not `get_current_user`).
- `request.state.user_id` is populated by the auth session middleware in `app/main.py`.

### 4.3 Request (FastAPI/Starlette Request)

`request: Request` is commonly included to access:

- `request.state.request_id` (required for `ok_payload(...)`)
- request metadata (`request.url.path`, `request.method`) when needed (e.g. streaming).

### 4.4 Access control helpers (require_*)

Routes frequently call helpers from `app/api/deps.py` (e.g. `require_project_viewer`, `require_project_editor`, `require_chapter_editor`, `require_outline_viewer`) to enforce authorization and resource existence.

These helpers raise `AppError.not_found()` / `AppError.forbidden()` / `AppError.unauthorized()` and are intended to be propagated to the global exception handlers.

### 4.5 Project configuration access matrix

Project-scoped configuration follows the normal read/write split:

| Resource | Read | Write |
| --- | --- | --- |
| Prompt presets, builtin resource metadata, preset export | viewer | editor |
| Prompt Studio categories and preset detail | viewer | editor |
| Project LLM preset and task overrides | viewer | editor |
| Project default writing style | viewer | editor |
| Project bundle export | viewer | n/a |
| LLM model discovery | editor | n/a |

LLM model discovery deliberately remains editor-only because it consumes the
project/user credential configuration to contact an upstream provider.  It is
not an ordinary stored-resource read.

GET handlers must not initialize or upgrade stored configuration.  Builtin
prompt resources are initialized/reconciled only by the explicit editor-only
`POST /api/projects/{project_id}/prompt_presets/sync_builtin_defaults`
operation.  A missing project LLM preset is represented by a transient default
response and is persisted only by its PUT endpoint.

Generation never auto-creates or upgrades prompt presets.  A missing active
preset fails closed with a validation error until an editor runs the explicit
sync operation (or the project-creation initialization has provided it).
