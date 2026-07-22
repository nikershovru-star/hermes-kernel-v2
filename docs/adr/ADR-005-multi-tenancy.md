# ADR-005 — Multi-tenancy (Auth, RBAC, Persistence)

- **Status:** accepted
- **Date:** 2026-07-22
- **Deciders:** Nikita (architect)
- **Depends on:** [ADR-001](ADR-001-kernel-architecture.md), [ADR-004](ADR-004-knowledge-pipeline.md) *(ADR-007 workspace isolation — planned)*

## Context

The kernel started single-tenant (ADR-001: "single-tenant start"). To serve
multiple users / teams on one deployment it needs three capabilities:

1. **Auth** — identify principals (users with credentials).
2. **RBAC** — gate operations by role/permission, not by hard-coded checks.
3. **Persistence** — survive restarts; entities live in a workspace-isolated
   store, not only in memory.

These are P5 (the final open phase). The constraint from earlier phases holds:
**no heavy new dependencies** — only stdlib (`hashlib`, `sqlite3`).

## Decision

Three cooperating layers, all in-memory-by-default with opt-in persistence:

### 1. Auth — `kernel/auth.py` (P5.1)
- `User(BaseEntity)`: `username`, `hashed_password` (never plaintext), `roles`.
- `AuthRegistry`: `register / authenticate / get_user / get_by_username /
  has_role / list`.
- **Passwords**: `hashlib.pbkdf2_hmac` (SHA-256, 16-byte random salt, 100k
  iterations), stored as `salt$hash`. Verification is `hmac.compare_digest`
  (constant-time) — no external `bcrypt`/`passlib`.
- No JWT / sessions yet — kept intentionally minimal; the registry is the
  source of truth for identity + role membership.

### 2. RBAC — `kernel/rbac.py` (P5.2)
- `Permission(resource, action)` and `Role(name, permissions)` — frozen,
  hashable value objects.
- `RBACRegistry(auth: AuthRegistry)`: `create_role`, `assign_role`,
  `check_permission`, `require_permission` (raises `PermissionError`),
  `list_roles`, `permissions_of`.
- **Guard, not wrapper.** RBAC does not wrap `WorkspaceRegistry` /
  `AgentRegistry` / `KnowledgeGraph`; callers call `require_permission` *before*
  invoking the operation. This keeps RBAC an orthogonal concern and avoids
  touching the 151 pre-existing registry tests.
- Roles resolve a user's permissions via the injected `AuthRegistry` (roles are
  stored on `User.roles`).

### 3. Persistence — `kernel/persistence.py` (P5.3)
- `PersistenceRegistry(db_path=":memory:")`: async CRUD
  (`save`/`get`/`list`/`list_all`/`delete`/`exists`/`count`/`mark`).
- **Universal store**: a single `entities(id, type, workspace_id, data_json)`
  table — every Pydantic entity serialises via `model_dump_json` and rehydrates
  by `type`. Avoids fragile per-field DDL. Plus a `markers` table for scanner
  de-duplication.
- **sqlite3 threading model**: all DB work runs on a **single-thread
  `ThreadPoolExecutor`** so one connection is reused safely and `:memory:`
  databases persist across calls. No `aiosqlite` dependency.
- **Workspace isolation**: every entity query filters `WHERE workspace_id = ?`.
- **Integration (composition, no registry rewrites)**:
  - `WorkspaceRegistry.save_to_db(persistence)` / `load_from_db(persistence)`
  - `KnowledgeGraph.persist_into(persistence, ws)` / `load_from_db(persistence, ws)`
    (embedding carried in `node.properties`, restored into `_embeddings`)
  - `FileScanner(persistence=...)` — `scan_once` skips paths already marked
    scanned (use a **file-backed** DB; `:memory:` markers are not shared
    across connections).

## Consequences

**Positive**
- Multi-tenancy is real: users authenticate, operations are gated by RBAC, and
  state survives restart in a workspace-isolated SQLite store.
- Zero new runtime dependencies (stdlib only) — CI stays light, axis clean.
- RBAC is additive: adding it touched no existing registry behaviour.

**Negative / trade-offs**
- RBAC enforcement is **call-site** (`require_permission`), not automatic at the
  registry boundary — a missed guard is a missed guard. A future stage could
  weave it into the kernel's operation dispatch.
- Persistence is a simple JSON-blob store: no migrations, no querying inside
  `data`, no indexing beyond `id`/`workspace_id`/`type`. Fine for the kernel's
  scale; a vector DB (ADR-009 `KnowledgeRetrievalService`) is separate.
- `:memory:` is single-process; multi-process deployments need a file/network
  DB (swap `db_path`).

## Related
- [ADR-001 — Kernel Architecture](ADR-001-kernel-architecture.md)
- [ADR-004 — Knowledge Pipeline](ADR-004-knowledge-pipeline.md)
- [Roadmap](../roadmap.md)
