# Project Cosmos — Yggdrasil Neuron Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade `yggdrasil-engine` from a write-only `GitPython` prototype into a stateless, async GitHub-backed Neuron Engine with full topic conversation storage, scoped recall/search for Phase 2 Digital Twins, and an MCP server bridge.

**Architecture:** `github_storage.py` manages slug sanitization, deterministic Markdown neuron formatting/parsing (using `<!-- yggdrasil:entry {...} -->` headers + collapsible `<details>` conversation logs), a 30s TTL read cache, and async `httpx` GitHub Contents API commits with SHA conflict retry. `main.py` exposes the FastAPI REST/OpenAPI interface (`POST /memory/add`, `GET /memory/tags`, `GET /memory/read/{tag}`, `GET /memory/search`, `GET /health`) protected by two-tier Bearer auth (`YGGDRASIL_API_KEY` for full read/write, `YGGDRASIL_PUBLIC_READ_KEY` for `career-public` read-only). `mcp_server.py` wraps these capabilities over JSON-RPC 2.0 stdio for Claude Desktop and Antigravity.

**Tech Stack:** Python 3.10+, FastAPI, Pydantic v2, `httpx`, `uvicorn`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-11-07-yggdrasil-cosmos-neuron-engine-design.md`

## Global Constraints

- **Runtime:** Python 3.10+, FastAPI, Pydantic v2, `httpx`, `uvicorn`.
- **No full `git clone` per HTTP request:** Never clone or wipe `/tmp` on request paths.
- **No breaking changes to `POST /memory/add`:** Existing callers sending only `tag`, `summary`, and `source_agent` must continue to work seamlessly.
- **Configuration (Environment Variables):** `GITHUB_TOKEN`, `GITHUB_USER`, `MEMORY_REPO`, `YGGDRASIL_API_KEY`, and optional `YGGDRASIL_PUBLIC_READ_KEY`.

## Review Focus

1. **Concurrent write SHA conflict (`409` / `422` from GitHub Contents API):** When two commits hit the same `{tag}.md` simultaneously, `GitHubStorage.append_entry` re-fetches the latest file content and `sha` once and retries the `PUT` so neither entry is lost.
2. **Path traversal or special characters in `tag` or `realm` (e.g., `../secrets`, `K8s & Docker!`):** `sanitize_slug` strips path separators and non-alphanumeric characters to produce safe slugs (`secrets`, `k8s-docker`), raising `ValueError` if empty.
3. **Legacy Markdown files without `<!-- yggdrasil:entry ... -->` markers:** Existing files written by the initial `main.py` prototype (`## Agent - Memory Entry\n...`) are parsed as a legacy entry with `scope="private"`, keeping old history readable to the admin key and never leaking to `YGGDRASIL_PUBLIC_READ_KEY`.
4. **Comment-closing sequences (`-->`) inside user `title`, `summary`, or `conversation`:** Metadata JSON inside `<!-- yggdrasil:entry ... -->` escapes `>` as `\u003e` so user text containing `-->` never corrupts entry boundaries.
5. **Unconfigured `YGGDRASIL_API_KEY` or `YGGDRASIL_PUBLIC_READ_KEY` (`None` or empty string):** Auth dependencies reject `Authorization: Bearer None` or `Bearer ` with HTTP `403` (never matching an unset environment variable).

---

### Task 1: Neuron Formatter, Parser, Slug Sanitizer, & Async GitHub Storage (`github_storage.py`)

**Files:**
- Create: `github_storage.py`
- Modify: `requirements.txt:1-4`
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: Environment variables `GITHUB_TOKEN`, `GITHUB_USER`, `MEMORY_REPO`
- Produces:
  - `ScopeType = Literal["private", "career-public", "commercial"]`
  - `class NeuronEntry(BaseModel)`: `id: str`, `timestamp: str`, `source_agent: str`, `scope: ScopeType`, `title: Optional[str] = None`, `summary: str`, `conversation: Optional[str] = None`, `tag: Optional[str] = None`, `realm: Optional[str] = None`
  - `sanitize_slug(raw: str) -> str`
  - `build_repo_path(tag: str, realm: Optional[str] = None) -> str`
  - `format_neuron_entry(entry: NeuronEntry) -> str`
  - `parse_neuron_markdown(markdown: str, tag: Optional[str] = None, realm: Optional[str] = None) -> list[NeuronEntry]`
  - `render_entries_markdown(entries: list[NeuronEntry], tag: str) -> str`
  - `class GitHubStorage`:
    - `async def append_entry(self, tag: str, summary: str, source_agent: str, realm: Optional[str] = None, scope: ScopeType = "private", title: Optional[str] = None, conversation: Optional[str] = None) -> dict`
    - `async def list_tags(self) -> list[dict]`
    - `async def read_entries(self, tag: str, realm: Optional[str] = None, allowed_scopes: Optional[set[str]] = None, scope_filter: Optional[str] = None, include_conversation: bool = True) -> list[NeuronEntry]`
    - `async def search_entries(self, query: str, allowed_scopes: set[str], tag: Optional[str] = None, scope_filter: Optional[str] = None, limit: int = 10) -> list[NeuronEntry]`

- [ ] **Step 1: Update `requirements.txt` and write the failing storage tests in `tests/test_storage.py`**

Update `requirements.txt` to replace `GitPython` with `httpx` and `pytest`:
```text
fastapi
uvicorn
pydantic
httpx
pytest
```

Create `tests/test_storage.py`:
```python
import asyncio
import base64
import httpx
import pytest
from github_storage import (
    GitHubStorage,
    NeuronEntry,
    build_repo_path,
    format_neuron_entry,
    parse_neuron_markdown,
    sanitize_slug,
)


def test_sanitize_slug_and_repo_path_blocks_traversal():
    assert sanitize_slug("K8s & Docker!") == "k8s-docker"
    assert sanitize_slug("../../etc/passwd") == "etc-passwd"
    assert build_repo_path("Kubernetes", realm="Projects/Infra") == "projects-infra/kubernetes.md"
    with pytest.raises(ValueError):
        sanitize_slug("///...   ")


def test_format_and_parse_roundtrip_with_arrow_in_content():
    entry = NeuronEntry(
        id="20261107T060000Z-abcd",
        timestamp="2026-11-07T06:00:00Z",
        source_agent="Claude-Desktop",
        scope="career-public",
        title="Flow A --> B",
        summary="Chose stateless httpx --> GitHub API.",
        conversation="User: A --> B?\nAssistant: Yes, A --> B works.",
    )
    raw_md = format_neuron_entry(entry)
    parsed = parse_neuron_markdown(raw_md, tag="arch")
    assert len(parsed) == 1
    assert parsed[0].id == entry.id
    assert parsed[0].title == "Flow A --> B"
    assert parsed[0].scope == "career-public"
    assert "A --> B?" in (parsed[0].conversation or "")


def test_parse_legacy_markdown_defaults_to_private():
    legacy = "\n## Gemini - Memory Entry\nLegacy note before metadata headers.\n"
    parsed = parse_neuron_markdown(legacy, tag="legacy")
    assert len(parsed) == 1
    assert parsed[0].scope == "private"
    assert "Legacy note before metadata headers." in parsed[0].summary


def test_append_entry_retries_on_409_sha_conflict():
    put_attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/contents/k8s.md"):
            existing_b64 = base64.b64encode(b"# k8s\n").decode("ascii")
            sha = "sha-1" if len(put_attempts) == 0 else "sha-2"
            return httpx.Response(200, json={"content": existing_b64, "sha": sha})
        if request.method == "PUT" and request.url.path.endswith("/contents/k8s.md"):
            put_attempts.append(request.read().decode("utf-8"))
            if len(put_attempts) == 1:
                return httpx.Response(409, json={"message": "sha conflict"})
            return httpx.Response(200, json={"commit": {"sha": "commit-sha-2"}})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    storage = GitHubStorage(
        token="tok", user="rahul", repo="brain", transport=transport
    )
    result = asyncio.run(
        storage.append_entry(
            tag="k8s",
            summary="Use Traefik",
            source_agent="Claude",
            scope="career-public",
        )
    )
    assert result["commit_sha"] == "commit-sha-2"
    assert len(put_attempts) == 2
```

- [ ] **Step 2: Run `pytest tests/test_storage.py -v` to verify it fails**

Run: `pytest tests/test_storage.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'github_storage'`

- [ ] **Step 3: Implement `github_storage.py`**

Implement `sanitize_slug`, `build_repo_path`, `NeuronEntry`, `format_neuron_entry`, `parse_neuron_markdown`, `render_entries_markdown`, and `GitHubStorage` in `github_storage.py`.
- `format_neuron_entry`: Serializes header metadata as compact JSON with `>` replaced by `\u003e` inside `<!-- yggdrasil:entry {...} -->`, followed by heading, summary, and optional `<details><summary>Full Topic Conversation</summary>\n\n{conversation}\n</details>`.
- `parse_neuron_markdown`: Splits on `<!-- yggdrasil:entry ({.*?}) -->` blocks; if any leading/legacy text exists outside entry blocks, wraps it in a `NeuronEntry(id="legacy", timestamp="legacy", source_agent="legacy", scope="private", summary=...)`.
- `GitHubStorage`: Accepts optional `transport: Optional[httpx.AsyncBaseTransport] = None` for testability, caches file reads for 30 seconds, invalidates cache on `append_entry`, retries `PUT` once on HTTP `409`/`422`, recursively lists `.md` files via GitHub Git Trees API (`/git/trees/HEAD?recursive=1`) with fallback to `/contents`, and ranks search matches by count of query tokens matched in `tag`, `title`, `summary`, and `conversation`.

- [ ] **Step 4: Run `pytest tests/test_storage.py -v` to verify it passes**

Run: `pytest tests/test_storage.py -v`
Expected: PASS (4 passed)

---

### Task 2: FastAPI Engine Routes & Scoped Digital Twin Authentication (`main.py`)

**Files:**
- Modify: `main.py:1-51`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `GitHubStorage`, `NeuronEntry`, `ScopeType`, `render_entries_markdown` from `github_storage.py`
- Produces:
  - `app: FastAPI`
  - `class MemoryPayload(BaseModel)`: `tag: str`, `summary: str`, `source_agent: str`, `title: Optional[str] = None`, `conversation: Optional[str] = None`, `scope: ScopeType = "private"`, `realm: Optional[str] = None`
  - `verify_write_access(api_key: str = Security(api_key_header)) -> str`
  - `resolve_read_scopes(api_key: str = Security(api_key_header)) -> set[str]`
  - Routes: `GET /health`, `POST /memory/add`, `GET /memory/tags`, `GET /memory/read/{tag}`, `GET /memory/search`

- [ ] **Step 1: Write the failing API & auth tests in `tests/test_api.py`**

Create `tests/test_api.py`:
```python
import base64
import httpx
from fastapi.testclient import TestClient
import main
from github_storage import GitHubStorage


def _make_mock_storage() -> GitHubStorage:
    files: dict[str, tuple[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and "/git/trees/" in path:
            tree = [{"path": k, "type": "blob", "size": len(v[0])} for k, v in files.items()]
            return httpx.Response(200, json={"tree": tree})
        if "/contents/" in path:
            rel = path.split("/contents/", 1)[1]
            if request.method == "GET":
                if rel not in files:
                    return httpx.Response(404, json={"message": "Not Found"})
                content, sha = files[rel]
                b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
                return httpx.Response(200, json={"content": b64, "sha": sha})
            if request.method == "PUT":
                import json
                body = json.loads(request.read().decode("utf-8"))
                decoded = base64.b64decode(body["content"]).decode("utf-8")
                files[rel] = (decoded, "sha-new")
                return httpx.Response(200, json={"commit": {"sha": "commit-123"}})
        return httpx.Response(404)

    return GitHubStorage(
        token="tok", user="rahul", repo="brain", transport=httpx.MockTransport(handler)
    )


def test_unset_api_key_rejects_bearer_none(monkeypatch):
    monkeypatch.setattr(main, "API_KEY", None)
    monkeypatch.setattr(main, "PUBLIC_READ_KEY", None)
    client = TestClient(main.app)
    res = client.post(
        "/memory/add",
        headers={"Authorization": "Bearer None"},
        json={"tag": "k8s", "summary": "s", "source_agent": "a"},
    )
    assert res.status_code == 403


def test_add_read_search_and_public_scope_isolation(monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "admin-secret")
    monkeypatch.setattr(main, "PUBLIC_READ_KEY", "public-twin-key")
    mock_storage = _make_mock_storage()
    monkeypatch.setattr(main, "get_storage", lambda: mock_storage)

    client = TestClient(main.app)

    # 1. Health check works without auth
    assert client.get("/health").status_code == 200

    # 2. Add a private neuron and a career-public neuron to the same tag
    r1 = client.post(
        "/memory/add",
        headers={"Authorization": "Bearer admin-secret"},
        json={
            "tag": "kubernetes",
            "summary": "Internal cluster root password rotation policy",
            "source_agent": "Claude-Desktop",
            "scope": "private",
        },
    )
    assert r1.status_code == 200
    assert r1.json()["status"] == "success"

    r2 = client.post(
        "/memory/add",
        headers={"Authorization": "Bearer admin-secret"},
        json={
            "tag": "kubernetes",
            "title": "Production Multi-Cluster Architecture",
            "summary": "Designed zero-downtime blue/green GKE ingress with ArgoCD",
            "conversation": "User: How should we route traffic?\nAI: Use weighted HTTPRoute.",
            "source_agent": "Gemini-Mobile",
            "scope": "career-public",
        },
    )
    assert r2.status_code == 200

    # 3. Public key CANNOT write
    r_denied = client.post(
        "/memory/add",
        headers={"Authorization": "Bearer public-twin-key"},
        json={"tag": "kubernetes", "summary": "hack", "source_agent": "bot"},
    )
    assert r_denied.status_code == 403

    # 4. Public key reading /memory/read/kubernetes ONLY sees career-public entry
    r_pub_read = client.get(
        "/memory/read/kubernetes",
        headers={"Authorization": "Bearer public-twin-key"},
    )
    assert r_pub_read.status_code == 200
    pub_data = r_pub_read.json()
    assert len(pub_data["entries"]) == 1
    assert pub_data["entries"][0]["scope"] == "career-public"
    assert "password" not in pub_data["markdown"]

    # 5. Admin key reading /memory/read/kubernetes sees both entries
    r_admin_read = client.get(
        "/memory/read/kubernetes",
        headers={"Authorization": "Bearer admin-secret"},
    )
    assert len(r_admin_read.json()["entries"]) == 2

    # 6. Search picks relevant data and respects scope
    r_search = client.get(
        "/memory/search",
        params={"q": "ArgoCD HTTPRoute"},
        headers={"Authorization": "Bearer public-twin-key"},
    )
    assert r_search.status_code == 200
    assert len(r_search.json()["results"]) == 1
    assert r_search.json()["results"][0]["title"] == "Production Multi-Cluster Architecture"
```

- [ ] **Step 2: Run `pytest tests/test_api.py -v` to verify it fails**

Run: `pytest tests/test_api.py -v`
Expected: FAIL (`AttributeError: module 'main' has no attribute 'PUBLIC_READ_KEY'` / `404` on `/health`, `/memory/read/kubernetes`, `/memory/search`)

- [ ] **Step 3: Implement the upgraded FastAPI routes and scoped auth in `main.py`**

Update `main.py` to:
- Read `API_KEY = os.getenv("YGGDRASIL_API_KEY")` and `PUBLIC_READ_KEY = os.getenv("YGGDRASIL_PUBLIC_READ_KEY")`.
- Use `hmac.compare_digest` in `verify_write_access` and `resolve_read_scopes`, explicitly requiring non-empty configured keys so `Bearer None` never passes.
- Implement `GET /health`, `POST /memory/add`, `GET /memory/tags`, `GET /memory/read/{tag}`, and `GET /memory/search` backed by `get_storage() -> GitHubStorage`.
- Keep `POST /memory/add` response backward-compatible (`"status": "success"`, `"message": f"Committed to {path}"`, plus `"path"`, `"entry_id"`, `"commit_sha"`).

- [ ] **Step 4: Run `pytest tests/test_api.py -v` to verify it passes**

Run: `pytest tests/test_api.py -v`
Expected: PASS (2 passed)

---

### Task 3: Stdio MCP Server Bridge (`mcp_server.py`)

**Files:**
- Create: `mcp_server.py`
- Test: `tests/test_mcp.py`

**Interfaces:**
- Consumes: `YGGDRASIL_URL` (defaults to `http://localhost:8000`) and `YGGDRASIL_API_KEY` environment variables, or an injected `httpx.AsyncClient` for in-process testing against `main.app`
- Produces:
  - `handle_jsonrpc_message(message: dict, client: httpx.AsyncClient, api_key: str) -> Optional[dict]`
  - MCP tools: `commit_to_memory`, `recall_memory`, `search_memories`, `list_memory_tags`

- [ ] **Step 1: Write the failing MCP JSON-RPC tests in `tests/test_mcp.py`**

Create `tests/test_mcp.py`:
```python
import asyncio
import httpx
import main
from mcp_server import handle_jsonrpc_message
from tests.test_api import _make_mock_storage


def test_mcp_initialize_tools_list_and_call(monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "admin-secret")
    mock_storage = _make_mock_storage()
    monkeypatch.setattr(main, "get_storage", lambda: mock_storage)

    async def _run():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://yggdrasil") as client:
            init_res = await handle_jsonrpc_message(
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                client=client,
                api_key="admin-secret",
            )
            assert init_res["result"]["serverInfo"]["name"] == "yggdrasil-cosmos-mcp"

            tools_res = await handle_jsonrpc_message(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                client=client,
                api_key="admin-secret",
            )
            tool_names = {t["name"] for t in tools_res["result"]["tools"]}
            assert tool_names == {
                "commit_to_memory",
                "recall_memory",
                "search_memories",
                "list_memory_tags",
            }

            call_add = await handle_jsonrpc_message(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": "commit_to_memory",
                        "arguments": {
                            "tag": "cosmos",
                            "title": "World Tree Architecture",
                            "summary": "Yggdrasil stores topic neurons in GitHub Markdown.",
                            "conversation": "User: Build Approach A.\nAI: On it.",
                            "source_agent": "Antigravity",
                            "scope": "career-public",
                        },
                    },
                },
                client=client,
                api_key="admin-secret",
            )
            assert call_add["result"]["isError"] is False

            call_recall = await handle_jsonrpc_message(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": "recall_memory",
                        "arguments": {"tag": "cosmos"},
                    },
                },
                client=client,
                api_key="admin-secret",
            )
            text_out = call_recall["result"]["content"][0]["text"]
            assert "World Tree Architecture" in text_out

    asyncio.run(_run())
```

- [ ] **Step 2: Run `pytest tests/test_mcp.py -v` to verify it fails**

Run: `pytest tests/test_mcp.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'mcp_server'`

- [ ] **Step 3: Implement `mcp_server.py`**

Create `mcp_server.py` implementing the Model Context Protocol JSON-RPC 2.0 handler (`initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call`) and a `main()` stdio loop so Claude Desktop, Antigravity, or Cursor can run `python mcp_server.py` directly.

- [ ] **Step 4: Run the full test suite (`pytest -v`)**

Run: `pytest -v`
Expected: PASS (all 7 tests across `test_storage.py`, `test_api.py`, and `test_mcp.py` pass)

