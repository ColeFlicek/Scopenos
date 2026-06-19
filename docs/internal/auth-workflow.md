# Auth Workflow

Complete reference for everything authentication and authorization in Phronosis.
Covers the full path from key creation through enforcement at every layer.

---

## Database schema

```sql
-- One record per human or service account
CREATE TABLE users (
    id         TEXT PRIMARY KEY,   -- UUID
    email      TEXT UNIQUE NOT NULL,
    plan       TEXT NOT NULL DEFAULT 'free',  -- 'free' | 'paid'
    created_at TEXT NOT NULL
);

-- One or more keys per user. Raw key is never stored.
CREATE TABLE api_keys (
    id         TEXT PRIMARY KEY,   -- UUID
    user_id    TEXT NOT NULL REFERENCES users(id),
    key_hash   TEXT NOT NULL UNIQUE,  -- SHA-256 of raw key
    name       TEXT NOT NULL DEFAULT '',  -- human label ("dev laptop")
    created_at TEXT NOT NULL,
    last_used  TEXT
);

-- Which users can access which private projects, and at what level
CREATE TABLE project_access (
    user_id    TEXT NOT NULL REFERENCES users(id),
    project_id TEXT NOT NULL,
    role       TEXT NOT NULL,  -- 'owner' | 'viewer'
    PRIMARY KEY (user_id, project_id)
);

-- Projects in this table are readable by any authenticated user, never writable
CREATE TABLE demo_projects (
    project_id    TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    repo_url      TEXT NOT NULL,
    last_indexed  TEXT,
    auto_update   INTEGER NOT NULL DEFAULT 0,
    added_at      TEXT NOT NULL
);
```

---

## Key lifecycle

### Creating a user and issuing a key

```bash
# From the server, inside the venv
python scripts/create_user.py cole@example.com --name "dev laptop" --plan paid --project myapp --role owner
```

**What this does:**
1. Inserts a row in `users` (UUID id, email, plan, created_at)
2. Optionally inserts a row in `project_access` (user_id, project_id, role)
3. Generates a raw key via `secrets.token_urlsafe(32)` — 32 bytes = 43 characters of URL-safe base64
4. Computes `key_hash = SHA-256(raw_key)`
5. Inserts into `api_keys` (id, user_id, key_hash, name, created_at)
6. **Prints the raw key once and discards it** — not stored anywhere

The printed output instructs the user to add it to their Claude Code MCP config:
```json
"headers": {"X-API-Key": "<raw_key>"}
```

### Key format

Raw keys are `secrets.token_urlsafe(32)` output — URL-safe base64, 43 characters, no prefix. Example: `xK9mP2vQwRtYbNcDfGhJkLmNeOpQrSt`.

There is no `ph_` prefix in the current implementation.

### Key lookup (every authenticated request)

```python
# src/call_graph/storage.py — get_user_by_key()
key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
row = await db.execute(
    "SELECT u.id, u.email, u.plan, u.created_at
     FROM api_keys k JOIN users u ON u.id = k.user_id
     WHERE k.key_hash = ?",
    (key_hash,)
)
# On hit: updates api_keys.last_used, returns user dict
# On miss: returns None
```

No timing attack concern from the hash comparison because the WHERE clause does an exact indexed lookup — there is no constant-time comparison loop in Python.

---

## Two authentication paths

The server has two distinct callers: the MCP protocol (Claude Code) and HTTP (web UI + scripts). They use different auth mechanisms because MCP middleware doesn't fire on raw HTTP requests.

### Path A: MCP tools (Claude Code)

```
Claude Code ──X-API-Key header──► FastMCP
                                     │
                              AuthMiddleware.on_message()
                                     │ extracts X-API-Key
                                     │ calls get_user_by_key()
                                     │ sets _current_user ContextVar
                                     ▼
                              @mcp.tool() handler
                                     │
                              get_current_user()  ← reads ContextVar
                                     │
                              check_permission(user, project_id, "write", db)
                                     │ calls check_project_access()
                                     ▼
                              tool logic runs
```

**`AuthMiddleware`** fires on every MCP protocol message via `on_message`. It:
1. Extracts `X-API-Key` from the HTTP request headers using `get_http_request()` (suppresses exceptions if no HTTP context)
2. Calls `db.get_user_by_key(raw_key)` — returns user dict or None
3. Sets `_current_user` ContextVar to the user (or None if unauthenticated/invalid)
4. Calls `call_next()` to let the tool handler run
5. Resets the ContextVar after the handler returns

**`check_permission()`** is called inside write tools:
```python
await check_permission(get_current_user(), project_id, "write", svcs.db)
```
- If user is None → raises `HTTPException(401)`
- If user can't access project → raises `HTTPException(403)`
- Otherwise → returns None, tool proceeds

**Which MCP tools require auth:**
- `index_project` — write (indexes new project data)
- `index_changes` — write
- `reembed_project` — write
- `enrich_summaries` — write (triggers LLM spend)
- `log_decision` — write
- `create_contract`, `approve_contract` — write
- All read tools (`query_similar_functions`, `get_callers`, etc.) — **no auth required**

---

### Path B: HTTP endpoints (web UI + scripts)

MCP middleware does **not** fire for Starlette custom routes. Write endpoints call `_require_valid_key()` directly:

```
Web UI / curl ──X-API-Key header──► Starlette HTTP route
                                          │
                                   _require_valid_key(request, db)
                                          │ reads request.headers.get("X-API-Key")
                                          │ calls db.get_user_by_key()
                                          │ raises HTTPException(401) if missing/invalid
                                          ▼
                                   handler logic runs
```

```python
# src/server.py
async def _require_valid_key(request: Request, db: CallGraphDB) -> dict:
    key = request.headers.get("X-API-Key")
    if not key:
        raise HTTPException(status_code=401, detail="Authentication required — include X-API-Key header")
    user = await db.get_user_by_key(key)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return user
```

Note: `_require_valid_key` only validates that the key exists and belongs to a real user. It does **not** check project-level access (unlike `check_permission`). This is intentional — HTTP write endpoints operate at the admin level (creating contracts, triggering re-embeds) where project-scoped permission checks would require a project_id that isn't always available.

**HTTP endpoints protected by `_require_valid_key`:**
| Endpoint | Reason |
|---|---|
| `POST /api/index-bulk` | Arbitrary data injection |
| `POST /api/enrich-summaries/{id}` | Triggers LLM spend |
| `POST /api/reembed/{id}` | Triggers embedding run |
| `POST /api/contracts` | Creates contracts |
| `PUT /api/contracts/{id}` | Modifies contract examples |
| `POST /api/contracts/{id}/approve` | Activates enforcement |
| `POST /api/contracts/{id}/deactivate` | Disables enforcement |

**Intentionally unauthenticated HTTP endpoints:**
| Endpoint | Why |
|---|---|
| `POST /index` | Post-commit git hook — runs on developer machines with no user credentials |
| `POST /api/decisions` | Same — git hook logs decisions |
| All `GET /api/*` | Read-only, no secrets at risk |
| `GET /` (web UI) | Static page |
| `POST /api/signup` | Creates a new account — no key yet |

---

## Project-level access control

`check_project_access(user_id, project_id, operation)` is the single source of truth for what a user may do to a project:

```python
# src/call_graph/storage.py

async def check_project_access(self, user_id, project_id, operation) -> bool:
    # 1. Demo project check (overrides everything)
    is_demo = await db.execute("SELECT 1 FROM demo_projects WHERE project_id = ?", (project_id,))
    if is_demo:
        return operation == "read"   # read: always yes, write: always no

    # 2. Private project — check project_access table
    row = await db.execute(
        "SELECT role FROM project_access WHERE user_id = ? AND project_id = ?",
        (user_id, project_id)
    )
    if not row:
        return False          # no access row → denied

    role = row["role"]
    if role == "owner":
        return True           # owners can read and write
    if role == "viewer":
        return operation == "read"   # viewers can only read
    return False
```

**Access matrix:**

| User type | Demo project | Own project (owner) | Shared project (viewer) | No access |
|---|---|---|---|---|
| Read (get_callers, query_similar…) | ✅ | ✅ | ✅ | ❌ 401/403 |
| Write (index_project, log_decision…) | ❌ 403 | ✅ | ❌ 403 | ❌ 401/403 |

---

## Web UI auth

The web UI is a single-page app served from `GET /`. It has no server-side session. API key is stored client-side.

```javascript
// Loaded from localStorage on page init
let _apiKey = localStorage.getItem('phronosis_api_key') || '';

// All write requests use this
function writeHeaders(extra) {
    const h = {'Content-Type': 'application/json'};
    if (_apiKey) h['X-API-Key'] = _apiKey;
    return Object.assign(h, extra || {});
}

// Called before the first write if no key is stored
function promptApiKey(action) {
    const key = prompt('Enter your Phronosis API key to ' + action + ':\n(It will be saved in your browser for this session)');
    if (key) {
        _apiKey = key.trim();
        localStorage.setItem('phronosis_api_key', _apiKey);
    }
    return !!key;
}
```

**Web UI write flow:**
1. User clicks "generate examples" (contract creation)
2. JS checks `if (!_apiKey && !promptApiKey('create a contract')) return`
3. If no key stored: shows browser `prompt()`, user pastes key, key saved to localStorage
4. Fetch fires with `headers: writeHeaders()` → includes `X-API-Key`
5. Server calls `_require_valid_key(request, db)` → validates → proceeds

**Security note:** localStorage is visible to any JS on the same origin. Since the web UI is served from the Phronosis server itself (no external scripts), this is acceptable. Don't use this pattern if the web UI ever loads third-party scripts.

---

## Signup flow (POST /api/signup)

For Phase 17, `POST /api/signup` provides a self-service entry point:

```
User POSTs {email} to /api/signup
    │
    ▼
Server creates user (db.create_user)
    │
    ▼
Server creates API key (db.create_api_key)
    │
    ▼
Server calls email_sender(email, raw_key)
    │   Real sender: Resend API (when RESEND_API_KEY is set)
    │   Test sender: prints to stdout
    ▼
Returns {"status": "ok"} — key is NOT in the response body
```

The key is sent by email only. It is never returned in the HTTP response. `GET /api/me` lets an authenticated user view their account and projects.

---

## Authentication gaps to address before Phase 17

1. **No key revocation via web UI** — revocation requires direct DB access (`UPDATE api_keys SET revoked_at = NOW() ...`). The `revoked_at` column is not currently checked in `get_user_by_key`. It exists in the schema but the lookup query doesn't filter on it.

2. **No rate limiting on auth attempts** — a brute-force attack on valid key hashes is not feasible (SHA-256 preimage), but repeated failed requests aren't throttled.

3. **POST /index and POST /api/decisions are public** — documented and intentional, but must be network-restricted before public launch (see ops-runbook.md).

4. **Web UI API key prompt is abrupt** — first write attempt shows a raw `prompt()`. Phase 17 should replace this with a proper login modal.
