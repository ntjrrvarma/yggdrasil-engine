# Project Cosmos — Yggdrasil Neuron Engine Design (Approach A)

**Date:** 2026-11-07
**Project:** `yggdrasil-engine` (Core Memory Trunk of *Project Cosmos*)
**Status:** Draft for Review

---

## 1. Intent and the Why

**Project Cosmos** is a long-horizon personal knowledge architecture: a living tree-world of knowledge kept in grasp for decades, ready to be recalled or acted upon whenever needed.

Within **Project Cosmos**, **Yggdrasil** is the central World Tree trunk that connects every AI assistant and agent you use across devices (Gemini and ChatGPT on mobile, Claude Desktop and Antigravity on your laptop, plus your own ecosystem agents like *Renard*, *Vulpine/Lynn*, and *Nakama*). Each topic file and conversation entry stored inside Yggdrasil acts as a **Neuron**—capturing not just short summaries, but full topic conversations, architectural decisions, and evolving ideas in permanent, human-readable, version-controlled GitHub Markdown.

By upgrading `yggdrasil-engine` from a write-only prototype into a full Read/Write/Search Neuron Engine, any connected AI can:
1. **Commit** both distilled insights and full topic conversations to a shared GitHub repository.
2. **Recall & Pick Relevant Data** across past topic conversations on demand.
3. **Feed Phase 2 Digital Twins** (such as a public Interview Proxy on `rahulnr.com` or a commercial Project Delivery Agent) using strict scope filtering so restricted agents only see the memories they are permitted to read.

---

## 2. Goals, Non-Goals, and Anti-Goals

### Goals
1. **Stateless GitHub REST API Engine:** Replace the brittle `GitPython` `/tmp` clone-per-request in `main.py` with an async `httpx` client talking directly to GitHub's Contents API (`/repos/{owner}/{repo}/contents/{path}`), supporting atomic SHA-checked commits and optimistic concurrency retries.
2. **Whole-Conversation + Distilled Neuron Format:** Allow each memory commit to store both the high-signal distilled `summary` and an optional `conversation` transcript (rendered inside a clean collapsible `<details>` block) alongside machine-parsable metadata (`timestamp`, `source_agent`, `scope`, `realm`, `title`).
3. **Full Read, List, and Relevant-Slice Search API:**
   - `POST /memory/add` — Backward-compatible with existing `{tag, summary, source_agent}` payloads, while supporting optional `realm`, `scope`, `title`, and `conversation` fields.
   - `GET /memory/tags` — Lists all available topic neurons in the repository.
   - `GET /memory/read/{tag}` — Retrieves the full topic dossier (or only the entries matching a requested `scope` or `source_agent`).
   - `GET /memory/search` — Searches across topic neurons by keyword (`q`), `tag`, or `scope`, returning only the relevant matching entries rather than dumping entire files.
   - `GET /health` — Lightweight readiness probe for Render uptime checks.
4. **Scoped Access Control for Phase 2 Digital Twins:**
   - Master `YGGDRASIL_API_KEY` grants full read/write access across all scopes (`private`, `career-public`, `commercial`).
   - Optional `YGGDRASIL_PUBLIC_READ_KEY` grants read/search access **strictly filtered** to `scope="career-public"` entries so an Interview Proxy can never leak private or commercial notes.
5. **Dual AI Integration (OpenAPI + MCP):**
   - Rich FastAPI OpenAPI 3.1 schema descriptions so ChatGPT Actions and Gemini Extensions can auto-discover endpoints.
   - A lightweight `mcp_server.py` (Model Context Protocol over stdio) exposing `commit_to_memory`, `recall_memory`, `search_memories`, and `list_memory_tags` for Claude Desktop, Antigravity, and Cursor.

### Non-Goals (Current Phase)
- Provisioning an external PostgreSQL/vector database (keeping GitHub Markdown as the single source of truth ensures zero hosting cost and multi-decade durability).
- Building the frontend web UI for the Phase 2 Interview Proxy inside this repository.

### Anti-Goals
- **No full `git clone` per HTTP request:** Never clone or wipe `/tmp` on request paths.
- **No breaking changes to `POST /memory/add`:** Existing callers sending only `tag`, `summary`, and `source_agent` must continue to work seamlessly.
- **No proprietary binary storage:** Every file in the target GitHub repository must remain clean, human-readable Markdown even if `yggdrasil-engine` is turned off 20 years from now.

---

## 3. Constraints

- **Runtime:** Python 3.10+, FastAPI, Pydantic v2, `httpx`, `uvicorn`.
- **Hosting:** Render web service (ephemeral filesystem, zero persistent local disk assumed) and local Windows/Linux development.
- **Configuration (Environment Variables):**
  - `GITHUB_TOKEN` — GitHub Personal Access Token (fine-grained `Contents: Read & Write` on the memory repo).
  - `GITHUB_USER` — GitHub owner/username.
  - `MEMORY_REPO` — Target private GitHub repository name (e.g., `yggdrasil-memory`).
  - `YGGDRASIL_API_KEY` — Admin Bearer token for full read/write access.
  - `YGGDRASIL_PUBLIC_READ_KEY` *(optional)* — Read-only Bearer token restricted to `career-public` entries.

---

## 4. Approaches Considered

### Approach A (Selected): Stateless GitHub API Engine + Structured Neuron Dossiers + OpenAPI & MCP
- **Description:** Uses async `httpx` calls to the GitHub REST Contents API with a short in-memory cache (invalidated on write), formats each Markdown entry with invisible HTML metadata markers (`<!-- yggdrasil:entry ... -->`) for deterministic entry parsing and scope filtering, and exposes both REST/OpenAPI and an MCP server.
- **Trade-offs:** Subject to GitHub's 5,000 requests/hour authenticated rate limit (mitigated by in-memory ETag/TTL caching), in exchange for zero local disk state, fast cold starts on Render, and cross-platform reliability.

### Approach B: Persistent Local Git Checkout + Local SQLite FTS5 Index
- **Description:** Maintains a local git clone synced on a background loop and indexes Markdown files into local SQLite.
- **Why rejected:** Render's free/starter tier wipes ephemeral disk on sleep/redeploy, reintroducing cold-start clone latency and git lock conflicts on concurrent requests.

### Approach C: Minimal Patch on Current `GitPython` Clone-Per-Request
- **Description:** Keep `Repo.clone_from` in `main.py`, fix `/tmp` paths, and add read routes that clone and read files.
- **Why rejected:** 2–5 second latency on every read or write makes interactive recall and multi-tag search unusable.

---

## 5. Architecture & Detailed Design

### 5.1 Codebase Layout (Simplicity First)
```text
yggdrasil-engine/
├── main.py              # FastAPI app, auth dependencies, and REST/OpenAPI routes
├── github_storage.py    # Async GitHub Contents API client, neuron parser, & TTL cache
├── mcp_server.py        # Stdio MCP server wrapping Yggdrasil for Claude/Antigravity
├── requirements.txt     # fastapi, uvicorn, pydantic, httpx, pytest
└── tests/
    └── test_yggdrasil.py # Unit & integration tests for write, read, search, & scope auth
```

### 5.2 Neuron Markdown Format in GitHub
Each topic tag maps to `{tag}.md` (or `{realm}/{tag}.md` when `realm` is provided; slugs are sanitized to lowercase alphanumeric + hyphens to prevent path traversal).

Each entry appended to the file uses a clean, human-readable Markdown structure paired with an HTML comment header so the engine can parse individual entries, filter by `scope`, and search relevant slices without regex fragility:

```markdown
<!-- yggdrasil:entry {"id":"20261107T054800Z-a1b2","timestamp":"2026-11-07T05:48:00Z","source_agent":"Claude-Desktop","scope":"private","title":"Kubernetes Ingress Architecture"} -->
## [2026-11-07 05:48 UTC] Kubernetes Ingress Architecture
- **Source:** `Claude-Desktop` | **Scope:** `private`

### Distilled Summary
Decided to use Traefik over NGINX Ingress for automatic Let's Encrypt wildcard certificates and native CRD routing.

<details>
<summary>Full Topic Conversation</summary>

User: Should we use NGINX or Traefik for the k3s cluster?
Assistant: For k3s, Traefik ships out of the box and supports CRD-based middleware...
</details>

---
```
*(Note: Legacy entries already written by the old `main.py` without the HTML comment marker are gracefully parsed as `scope="private"` legacy entries so existing repo files never break.)*

### 5.3 Authentication & Scope Enforcement
- `verify_write_access`: Requires `Authorization: Bearer <YGGDRASIL_API_KEY>`. Used on `POST /memory/add`.
- `resolve_read_scope`: Accepts either:
  - `YGGDRASIL_API_KEY` → allowed scopes: `{"private", "career-public", "commercial"}`
  - `YGGDRASIL_PUBLIC_READ_KEY` (when configured) → allowed scopes: `{"career-public"}`
  - Otherwise raises `403 Forbidden`.

### 5.4 API Contract
1. **`GET /health`** (No auth required)
   - Returns `{"status": "ok", "engine": "yggdrasil", "project": "cosmos"}`.
2. **`POST /memory/add`** (Requires write auth)
   - Request body (`MemoryPayload`):
     - `tag: str` (required, e.g. `"kubernetes"`)
     - `summary: str` (required, distilled insight/decision)
     - `source_agent: str` (required, e.g. `"Gemini-Mobile"`, `"Claude-Desktop"`, `"Renard"`)
     - `title: Optional[str] = None`
     - `conversation: Optional[str] = None` (full conversation log or transcript snippet on this topic)
     - `scope: Literal["private", "career-public", "commercial"] = "private"`
     - `realm: Optional[str] = None` (optional folder namespace, e.g. `"architecture"`, `"projects"`, `"decisions"`)
   - Behavior: Fetches existing file content + `sha` from GitHub (if present), appends the formatted Neuron block, commits via `PUT /repos/{owner}/{repo}/contents/{path}` (with 1 automatic retry if a concurrent commit updated the `sha`), invalidates the cache for that file, and returns `{"status": "success", "path": ..., "entry_id": ..., "commit_sha": ...}`.
3. **`GET /memory/tags`** (Requires read auth)
   - Lists all `.md` neuron files in the repo, returning `[{"tag": ..., "realm": ..., "path": ..., "size": ...}]`.
4. **`GET /memory/read/{tag}`** (Requires read auth)
   - Query params: `realm: Optional[str] = None`, `scope: Optional[str] = None`, `include_conversation: bool = True`.
   - Parses the file's entries, filters out any entries outside the caller's allowed scopes (and optional `scope` query filter), and returns both the structured list of `entries` and the combined `markdown`.
5. **`GET /memory/search`** (Requires read auth)
   - Query params: `q: str` (required search query), `tag: Optional[str] = None`, `scope: Optional[str] = None`, `limit: int = 10`.
   - Scores entries across all (or specified) topic files by matching `q` terms against `title`, `tag`, `summary`, and `conversation`, returning the top matching entries sorted by relevance score and recency so an AI assistant can pick only the relevant data without blowing up its context window.

---

## 6. What Is Left to the Builder

- **In-memory cache implementation details:** Simple dictionary cache keyed by file path with a 30-second TTL, cleared on any write to that path.
- **Slug sanitization helper:** Convert spaces/underscores to hyphens and strip non-`[a-z0-9-_]` characters so tags like `"K8s Architecture!"` cleanly map to `k8s-architecture.md`.
- **MCP transport details:** Implement standard JSON-RPC 2.0 stdio loop in `mcp_server.py` using only standard library + `httpx` so it runs in Claude Desktop or Antigravity without extra binary dependencies.

