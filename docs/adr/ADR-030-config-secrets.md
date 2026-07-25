# ADR-030 — Configuration & Secrets Platform

- **Status:** Accepted
- **Date:** 2026-07-25
- **Version:** v2.16.0
- **Supersedes / relates to:** ADR-028 (Capability Guard — shares `AuditEntry`),
  ADR-029 (MCP Gateway — consumes vault auth tokens)

## Context

Agents, workflows, MCP servers and plugins all need configuration values and
secrets (API keys, bearer tokens, DB passwords). Before ADR-030 these were
passed ad-hoc through constructor params, task metadata or environment lookups —
no central store, no scope isolation, no encryption at rest, no audit trail, and
no way to reload configuration without restarting the kernel.

We need a **centralized, scope-aware** store for both configuration and secrets
that is **optionally wireable** into the existing engines with **zero
regression** when unwired.

## Decision

Add a Configuration & Secrets Platform in three isolated, axis-clean modules
plus optional integration into the four consumer engines.

### Domain (`kernel/config_domain.py`)
Isolated ADR-local models (pattern: `security_domain.py` /
`observability_domain.py` / `marketplace_domain.py`) — stdlib `datetime` +
`pydantic` only:
- `ConfigScope` enum — `GLOBAL`, `AGENT`, `WORKFLOW`, `PLUGIN`, `MCP_SERVER`.
  A `(scope, scope_id)` pair namespaces every key.
- `ConfigEntry` — `key/value/scope/scope_id/version/updated_at/encrypted/deleted`.
- `SecretRef` — pointer to a secret (no ciphertext), safe to log/pass around.
- `SecretValue` — `ciphertext/nonce/tag/version/rotated_at` (AES-256-GCM shape).
- `ConfigChange` — audit-friendly mutation record.

### Events (`kernel/events.py`, namespaced `cfg.*`)
`ConfigChanged` (agg `scope:scope_id`), `SecretRotated` (agg secret_key),
`SecretAccessed` (agg secret_key), `ConfigReloaded` (agg `global`),
`ConfigAccessDenied` (agg principal).

### Engine (`kernel/config_vault.py`)
`ConfigVault` — async, fully injectable (`store`/`event_bus`/`event_store`/
`clock`/`cipher`/`sleep`). Config is stored plaintext; secrets are stored
encrypted and only decrypted via `resolve_secret` (which writes audit). `get`
never decrypts. Methods: `set` / `get` / `delete` (soft) / `list_keys` /
`set_secret` / `resolve_secret` / `rotate_secret` / `reload` / `get_audit_log`.
The cipher is an **injectable async callable** (`encrypt(str)->bytes` /
`decrypt(bytes)->str`); default is a base64 stub (`_Base64Cipher`) for
deterministic tests — production wires Fernet or AES-256-GCM.

### Store (`kernel/config_store.py`)
`ConfigStore` — SQLite (`config` / `secrets` / `config_audit`) + in-memory
fallback (`db_path=None`). Nullable `_conn` initialized before the conditional
(ADR-026 lesson). Repo-reload on `db_path`. Rotation **overwrites** (no history
table).

### Integration (all optional, `vault=None` default → zero regression)
- **`McpGateway(vault=)`** — `connect` resolves `mcp:{server_url}:auth_token`
  from the vault (scope=MCP_SERVER) when no explicit token is passed; tracks the
  auth source (`explicit`/`vault`/`none`) as audit context in metrics labels.
- **`AgentRuntime(vault=)`** — `get_config` / `resolve_secret` proxies
  (scope=AGENT); `execute` interpolates `${secrets.X}` / `${config.Y}` tokens in
  `task.parameters` and `task.metadata`.
- **`WorkflowEngine(vault=)`** — `execute_step` interpolates the same tokens in
  resolved step params (scope=WORKFLOW, scope_id=instance_id); `start`
  optionally seeds `workflow.defaults` as scoped config.
- **`PluginMarketplace(vault=)`** — `install` verifies `package.required_secrets`
  exist (scope=PLUGIN) before installing; fails with
  `PluginInstallFailed("missing_required_secrets")`, else emits
  `PluginInstalled(secrets_resolved=True)`.

## Consequences

### Positive
- One scope-aware store for all config + secrets; agent A never sees agent B.
- Encryption at rest via a pluggable cipher; secrets never returned by `get`.
- Full audit trail (`SecretAccessed` / `ConfigAccessDenied` + `AuditEntry`).
- Hot-reload of config into cache without a kernel restart.
- Every integration is opt-in; the 685 pre-existing tests pass unchanged.

### Honest limitations (deferred)
- **Encryption** is AES-256-GCM / Fernet via an **injectable cipher**, NOT an
  HSM/KMS integration. The default `_Base64Cipher` is a non-secure dev/test stub.
- **No runtime memory zeroing** — plaintext lives in RAM during `resolve_secret`.
- **No distributed consensus** — config is per-node (not etcd/Consul).
- **Secret rotation** is manual/triggered, not an automatic cron/poller; the
  store **overwrites** on rotation (no version history / previous ciphertext).
- **Access control** is scope-based, NOT full RBAC with roles.
- **Interpolation** `${secrets.X}` / `${config.Y}` is a simple regex, not a full
  templating engine (missing config → empty string; missing secret → KeyError).

## Metrics
- 3 new modules + 5 events + 4 optional integrations.
- 34 new tests (16 vault / 8 store / 10 integration) → **719 passed, 3 skipped**.
- Coverage: `config_domain.py` 93%, `config_store.py` 95%, `config_vault.py`
  96%; project total **92%**. `tach check` green.
