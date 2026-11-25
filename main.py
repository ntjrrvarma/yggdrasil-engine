import hmac
import os
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from github_storage import (
    ALL_SCOPES,
    GitHubStorage,
    ScopeType,
    render_entries_markdown,
)

app = FastAPI(
    title="Yggdrasil Memory API — Project Cosmos",
    description=(
        "Centralized, Git-backed persistent memory trunk for AI assistants and "
        "scoped Digital Twins across Project Cosmos."
    ),
    version="2.0.0",
)

# Environment Variables
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_USER = os.getenv("GITHUB_USER")
MEMORY_REPO = os.getenv("MEMORY_REPO")  # e.g., "yggdrasil-memory"
API_KEY = os.getenv("YGGDRASIL_API_KEY")
PUBLIC_READ_KEY = os.getenv("YGGDRASIL_PUBLIC_READ_KEY")

# Security definition
api_key_header = APIKeyHeader(name="Authorization", auto_error=True)


def _safe_bearer_match(provided: str, expected_secret: Optional[str]) -> bool:
    if not expected_secret:
        return False
    return hmac.compare_digest(
        provided.encode("utf-8"), f"Bearer {expected_secret}".encode("utf-8")
    )


def verify_write_access(api_key: str = Security(api_key_header)) -> str:
    """Require the master YGGDRASIL_API_KEY for write operations."""
    if not _safe_bearer_match(api_key, API_KEY):
        raise HTTPException(status_code=403, detail="Invalid API Key")
    return api_key


def resolve_read_scopes(api_key: str = Security(api_key_header)) -> set[str]:
    """Resolve allowed memory scopes from either the master key or public read key."""
    if _safe_bearer_match(api_key, API_KEY):
        return set(ALL_SCOPES)
    if _safe_bearer_match(api_key, PUBLIC_READ_KEY):
        return {"career-public"}
    raise HTTPException(status_code=403, detail="Invalid API Key")


_storage_cache: Optional[tuple[tuple[str, str, str], GitHubStorage]] = None


def get_storage() -> GitHubStorage:
    """Return a shared GitHubStorage client (reusing its 30s TTL cache) for current env config."""
    global _storage_cache
    if not GITHUB_TOKEN or not GITHUB_USER or not MEMORY_REPO:
        raise HTTPException(
            status_code=500,
            detail="GitHub storage environment variables are not configured.",
        )
    key = (GITHUB_TOKEN, GITHUB_USER, MEMORY_REPO)
    if _storage_cache is None or _storage_cache[0] != key:
        _storage_cache = (
            key,
            GitHubStorage(token=GITHUB_TOKEN, user=GITHUB_USER, repo=MEMORY_REPO),
        )
    return _storage_cache[1]


@app.exception_handler(httpx.HTTPError)
async def github_http_error_handler(_request: Request, _exc: httpx.HTTPError):
    return JSONResponse(
        status_code=502,
        content={"detail": "Upstream GitHub storage request failed."},
    )


class MemoryPayload(BaseModel):
    tag: str = Field(..., description="Topic category slug, e.g. 'kubernetes' or 'nakama'.")
    summary: str = Field(..., description="Distilled technical decision, insight, or summary.")
    source_agent: str = Field(..., description="Originating AI assistant, e.g. 'Claude-Desktop'.")
    title: Optional[str] = Field(None, description="Optional human-readable title for this neuron entry.")
    conversation: Optional[str] = Field(
        None,
        description="Optional full topic conversation log stored in a collapsible details block.",
    )
    scope: ScopeType = Field(
        "private",
        description="Access scope: 'private', 'career-public', or 'commercial'.",
    )
    realm: Optional[str] = Field(
        None,
        description="Optional folder namespace in the memory repo, e.g. 'projects' or 'architecture'.",
    )


@app.get("/health", summary="Health check")
async def health_check():
    """Lightweight readiness probe for Render uptime monitoring."""
    return {"status": "ok", "engine": "yggdrasil", "project": "cosmos"}


@app.post(
    "/memory/add",
    dependencies=[Depends(verify_write_access)],
    summary="Commit a topic neuron to memory",
)
async def commit_to_memory(payload: MemoryPayload):
    """Append a structured neuron entry (summary + optional full conversation) to GitHub."""
    storage = get_storage()
    try:
        return await storage.append_entry(
            tag=payload.tag,
            summary=payload.summary,
            source_agent=payload.source_agent,
            realm=payload.realm,
            scope=payload.scope,
            title=payload.title,
            conversation=payload.conversation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/memory/tags", summary="List all memory topic tags")
async def list_memory_tags(
    allowed_scopes: set[str] = Depends(resolve_read_scopes),
):
    """List all Markdown topic neuron files visible to the caller's scope."""
    storage = get_storage()
    tags = await storage.list_tags(allowed_scopes=allowed_scopes)
    return {"tags": tags}


@app.get("/memory/read/{tag:path}", summary="Read a topic neuron dossier")
async def read_memory(
    tag: str,
    realm: Optional[str] = Query(None, description="Optional realm folder namespace."),
    scope: Optional[ScopeType] = Query(None, description="Optional scope filter."),
    include_conversation: bool = Query(
        True, description="Include full collapsible conversation transcripts."
    ),
    allowed_scopes: set[str] = Depends(resolve_read_scopes),
):
    """Read entries for a topic tag, filtered by caller permissions and optional scope."""
    storage = get_storage()
    try:
        entries = await storage.read_entries(
            tag=tag,
            realm=realm,
            allowed_scopes=allowed_scopes,
            scope_filter=scope,
            include_conversation=include_conversation,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    markdown = render_entries_markdown(entries, tag=tag)
    return {
        "tag": tag,
        "realm": realm,
        "entries": [entry.model_dump() for entry in entries],
        "markdown": markdown,
    }


@app.get("/memory/search", summary="Search relevant memory entries across topics")
async def search_memories(
    q: str = Query(..., min_length=1, description="Keywords to match across neuron entries."),
    tag: Optional[str] = Query(None, description="Optional topic tag to narrow search."),
    realm: Optional[str] = Query(None, description="Optional realm namespace to narrow search."),
    scope: Optional[ScopeType] = Query(None, description="Optional scope filter."),
    limit: int = Query(10, ge=1, le=50, description="Maximum number of entries to return."),
    allowed_scopes: set[str] = Depends(resolve_read_scopes),
):
    """Search across topic neurons and return the most relevant matching entries."""
    storage = get_storage()
    try:
        results = await storage.search_entries(
            query=q,
            allowed_scopes=allowed_scopes,
            tag=tag,
            realm=realm,
            scope_filter=scope,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "query": q,
        "results": [entry.model_dump() for entry in results],
    }
