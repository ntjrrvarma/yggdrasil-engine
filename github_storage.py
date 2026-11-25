import base64
import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, Optional
import httpx
from pydantic import BaseModel

ScopeType = Literal["private", "career-public", "commercial"]
ALL_SCOPES: set[str] = {"private", "career-public", "commercial"}

ENTRY_MARKER_RE = re.compile(r"<!--\s*yggdrasil:entry\s+(\{.*?\})\s*-->")
OUTER_DETAILS_RE = re.compile(
    r"\n?<details>\s*<summary>Full Topic Conversation</summary>\s*(.*)\s*</details>\s*\Z",
    re.DOTALL,
)
SUMMARY_SECTION_RE = re.compile(
    r"### Distilled Summary\s*\n(.*)\Z",
    re.DOTALL,
)


def _escape_embedded_comments(text: str) -> str:
    """Prevent user text containing HTML comments from spoofing entry boundaries."""
    return text.replace("<!--", "&lt;!--")


def _unescape_embedded_comments(text: str) -> str:
    return text.replace("&lt;!--", "<!--")


class NeuronEntry(BaseModel):
    id: str
    timestamp: str
    source_agent: str
    scope: ScopeType = "private"
    title: Optional[str] = None
    summary: str
    conversation: Optional[str] = None
    tag: Optional[str] = None
    realm: Optional[str] = None


def sanitize_slug(raw: str) -> str:
    """Convert raw tag or realm into a safe lowercase slug without path traversal."""
    cleaned = raw.replace("..", " ").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", cleaned).strip("-")
    if not slug:
        raise ValueError(f"Invalid empty slug from input: {raw!r}")
    return slug


def build_repo_path(tag: str, realm: Optional[str] = None) -> str:
    """Build a safe relative repository path for a neuron markdown file."""
    safe_tag = sanitize_slug(tag)
    if realm and realm.strip():
        safe_realm = sanitize_slug(realm)
        return f"{safe_realm}/{safe_tag}.md"
    return f"{safe_tag}.md"


def format_neuron_entry(entry: NeuronEntry) -> str:
    """Format a NeuronEntry as human-readable Markdown with an invisible metadata header."""
    meta = {
        "id": entry.id,
        "timestamp": entry.timestamp,
        "source_agent": entry.source_agent,
        "scope": entry.scope,
        "title": entry.title,
    }
    meta_json = json.dumps(meta, separators=(",", ":")).replace(">", "\\u003e")
    display_time = entry.timestamp.replace("T", " ").replace("Z", " UTC")
    heading_title = entry.title if entry.title else f"{entry.source_agent} - Memory Entry"

    lines = [
        f"<!-- yggdrasil:entry {meta_json} -->",
        f"## [{display_time}] {heading_title}",
        f"- **Source:** `{entry.source_agent}` | **Scope:** `{entry.scope}`",
        "",
        "### Distilled Summary",
        _escape_embedded_comments(entry.summary.strip()),
    ]

    if entry.conversation and entry.conversation.strip():
        lines.extend(
            [
                "",
                "<details>",
                "<summary>Full Topic Conversation</summary>",
                "",
                _escape_embedded_comments(entry.conversation.strip()),
                "</details>",
            ]
        )

    lines.extend(["", "---", ""])
    return "\n".join(lines)


def parse_neuron_markdown(
    markdown: str, tag: Optional[str] = None, realm: Optional[str] = None
) -> list[NeuronEntry]:
    """Parse a neuron Markdown file into structured NeuronEntry objects, preserving legacy text."""
    if not markdown or not markdown.strip():
        return []

    matches = list(ENTRY_MARKER_RE.finditer(markdown))
    entries: list[NeuronEntry] = []

    if not matches:
        return [
            NeuronEntry(
                id="legacy",
                timestamp="legacy",
                source_agent="legacy",
                scope="private",
                title="Legacy Memory Entries",
                summary=markdown.strip(),
                tag=tag,
                realm=realm,
            )
        ]

    leading = markdown[: matches[0].start()].strip()
    # Ignore simple top-level "# tag" title lines, but preserve any real legacy entries
    leading_without_h1 = re.sub(r"^#\s+[^\n]+\n*", "", leading).strip()
    if leading_without_h1:
        entries.append(
            NeuronEntry(
                id="legacy",
                timestamp="legacy",
                source_agent="legacy",
                scope="private",
                title="Legacy Memory Entries",
                summary=leading_without_h1,
                tag=tag,
                realm=realm,
            )
        )

    for idx, match in enumerate(matches):
        raw_meta = match.group(1)
        block_start = match.end()
        block_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown)
        body = markdown[block_start:block_end].strip()
        body = re.sub(r"\n---\s*\Z", "", body).strip()

        try:
            meta = json.loads(raw_meta)
        except json.JSONDecodeError:
            meta = {}

        conv_match = OUTER_DETAILS_RE.search(body)
        conversation = (
            _unescape_embedded_comments(conv_match.group(1).strip())
            if conv_match
            else None
        )
        body_without_conv = OUTER_DETAILS_RE.sub("", body).strip()

        sum_match = SUMMARY_SECTION_RE.search(body_without_conv)
        if sum_match:
            summary = _unescape_embedded_comments(sum_match.group(1).strip())
        else:
            summary = _unescape_embedded_comments(body_without_conv)

        scope_val = meta.get("scope", "private")
        if scope_val not in ALL_SCOPES:
            scope_val = "private"

        entries.append(
            NeuronEntry(
                id=str(meta.get("id") or f"entry-{idx}"),
                timestamp=str(meta.get("timestamp") or "unknown"),
                source_agent=str(meta.get("source_agent") or "unknown"),
                scope=scope_val,
                title=meta.get("title"),
                summary=summary,
                conversation=conversation,
                tag=tag,
                realm=realm,
            )
        )

    return entries


def render_entries_markdown(entries: list[NeuronEntry], tag: str) -> str:
    """Render a filtered list of NeuronEntries back into clean Markdown."""
    if not entries:
        return ""
    blocks = [f"# {tag}\n"]
    for entry in entries:
        blocks.append(format_neuron_entry(entry))
    return "\n".join(blocks)


class GitHubStorage:
    """Stateless async GitHub Contents API client with TTL read cache and SHA conflict retry."""

    def __init__(
        self,
        token: Optional[str],
        user: Optional[str],
        repo: Optional[str],
        transport: Optional[httpx.AsyncBaseTransport] = None,
        cache_ttl_seconds: float = 30.0,
    ):
        self.token = token or ""
        self.user = user or ""
        self.repo = repo or ""
        self.transport = transport
        self.cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[str, tuple[float, str, str]] = {}

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="https://api.github.com",
            headers=self._headers(),
            transport=self.transport,
            timeout=15.0,
        )

    async def _fetch_file(
        self, client: httpx.AsyncClient, path: str, use_cache: bool = True
    ) -> tuple[Optional[str], Optional[str]]:
        now = time.monotonic()
        if use_cache and path in self._cache:
            ts, cached_content, cached_sha = self._cache[path]
            if now - ts < self.cache_ttl_seconds:
                return cached_content, cached_sha

        url = f"/repos/{self.user}/{self.repo}/contents/{path}"
        resp = await client.get(url)
        if resp.status_code == 404:
            return None, None
        resp.raise_for_status()
        data = resp.json()
        raw_b64 = data.get("content", "")
        decoded = base64.b64decode(raw_b64).decode("utf-8") if raw_b64 else ""
        sha = data.get("sha")
        if use_cache:
            self._cache[path] = (now, decoded, sha or "")
        return decoded, sha

    async def append_entry(
        self,
        tag: str,
        summary: str,
        source_agent: str,
        realm: Optional[str] = None,
        scope: ScopeType = "private",
        title: Optional[str] = None,
        conversation: Optional[str] = None,
    ) -> dict[str, Any]:
        path = build_repo_path(tag, realm)
        now_dt = datetime.now(timezone.utc)
        timestamp = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        entry_id = f"{now_dt.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"

        entry = NeuronEntry(
            id=entry_id,
            timestamp=timestamp,
            source_agent=source_agent,
            scope=scope,
            title=title,
            summary=summary,
            conversation=conversation,
            tag=sanitize_slug(tag),
            realm=sanitize_slug(realm) if realm else None,
        )
        entry_block = format_neuron_entry(entry)

        async with self._client() as client:
            for attempt in range(2):
                existing_content, sha = await self._fetch_file(
                    client, path, use_cache=False
                )
                if existing_content is None:
                    new_content = f"# {sanitize_slug(tag)}\n\n{entry_block}"
                else:
                    sep = "" if existing_content.endswith("\n") else "\n"
                    new_content = f"{existing_content}{sep}\n{entry_block}"

                payload: dict[str, Any] = {
                    "message": f"chore(memory): update from {source_agent} regarding {tag}",
                    "content": base64.b64encode(new_content.encode("utf-8")).decode(
                        "ascii"
                    ),
                }
                if sha:
                    payload["sha"] = sha

                url = f"/repos/{self.user}/{self.repo}/contents/{path}"
                put_resp = await client.put(url, json=payload)
                if put_resp.status_code in (409, 422) and attempt == 0:
                    continue
                put_resp.raise_for_status()
                put_data = put_resp.json()
                commit_sha = (put_data.get("commit") or {}).get("sha", "")
                self._cache.pop(path, None)
                return {
                    "status": "success",
                    "message": f"Committed to {path}",
                    "path": path,
                    "entry_id": entry_id,
                    "commit_sha": commit_sha,
                }

        raise RuntimeError(f"Failed to commit memory to {path} after SHA conflict retry")

    async def list_tags(
        self, allowed_scopes: Optional[set[str]] = None
    ) -> list[dict[str, Any]]:
        async with self._client() as client:
            tree_url = f"/repos/{self.user}/{self.repo}/git/trees/HEAD?recursive=1"
            resp = await client.get(tree_url)
            items: list[dict[str, Any]] = []
            if resp.status_code == 200:
                for node in resp.json().get("tree", []):
                    path = node.get("path", "")
                    if node.get("type") == "blob" and path.endswith(".md"):
                        parts = path[:-3].split("/")
                        tag_name = parts[-1]
                        realm_name = parts[0] if len(parts) > 1 else None
                        try:
                            sanitize_slug(tag_name)
                        except ValueError:
                            continue
                        items.append(
                            {
                                "tag": tag_name,
                                "realm": realm_name,
                                "path": path,
                                "size": node.get("size", 0),
                            }
                        )
            else:
                # Fallback to root /contents
                c_resp = await client.get(f"/repos/{self.user}/{self.repo}/contents")
                if c_resp.status_code == 404:
                    return []
                c_resp.raise_for_status()
                for item in c_resp.json():
                    path = item.get("path", "")
                    if item.get("type") == "file" and path.endswith(".md"):
                        tag_name = path[:-3]
                        try:
                            sanitize_slug(tag_name)
                        except ValueError:
                            continue
                        items.append(
                            {
                                "tag": tag_name,
                                "realm": None,
                                "path": path,
                                "size": item.get("size", 0),
                            }
                        )

        if allowed_scopes is not None and allowed_scopes != ALL_SCOPES:
            visible: list[dict[str, Any]] = []
            for item in items:
                entries = await self.read_entries(
                    tag=item["tag"],
                    realm=item.get("realm"),
                    allowed_scopes=allowed_scopes,
                    include_conversation=False,
                )
                if entries:
                    visible.append(item)
            return visible

        return items

    async def read_entries(
        self,
        tag: str,
        realm: Optional[str] = None,
        allowed_scopes: Optional[set[str]] = None,
        scope_filter: Optional[str] = None,
        include_conversation: bool = True,
    ) -> list[NeuronEntry]:
        path = build_repo_path(tag, realm)
        safe_tag = sanitize_slug(tag)
        safe_realm = sanitize_slug(realm) if realm else None

        async with self._client() as client:
            content, _ = await self._fetch_file(client, path, use_cache=True)
            if content is None:
                return []

        entries = parse_neuron_markdown(content, tag=safe_tag, realm=safe_realm)
        scopes = allowed_scopes if allowed_scopes is not None else ALL_SCOPES
        filtered: list[NeuronEntry] = []
        for entry in entries:
            if entry.scope not in scopes:
                continue
            if scope_filter and entry.scope != scope_filter:
                continue
            if not include_conversation:
                entry = entry.model_copy(update={"conversation": None})
            filtered.append(entry)
        return filtered

    async def search_entries(
        self,
        query: str,
        allowed_scopes: set[str],
        tag: Optional[str] = None,
        realm: Optional[str] = None,
        scope_filter: Optional[str] = None,
        limit: int = 10,
    ) -> list[NeuronEntry]:
        tokens = [t.lower() for t in re.split(r"\s+", query.strip()) if t]
        if not tokens:
            return []

        if tag and realm and realm.strip():
            candidate_tags = [{"tag": sanitize_slug(tag), "realm": sanitize_slug(realm)}]
        elif tag:
            target_slug = sanitize_slug(tag)
            all_tags = await self.list_tags()
            candidate_tags = [
                item for item in all_tags if sanitize_slug(item["tag"]) == target_slug
            ]
        else:
            candidate_tags = await self.list_tags()

        scored: list[tuple[int, str, NeuronEntry]] = []
        for item in candidate_tags:
            entries = await self.read_entries(
                tag=item["tag"],
                realm=item.get("realm"),
                allowed_scopes=allowed_scopes,
                scope_filter=scope_filter,
                include_conversation=True,
            )
            for entry in entries:
                haystack = " ".join(
                    filter(
                        None,
                        [
                            entry.tag or "",
                            entry.realm or "",
                            entry.title or "",
                            entry.summary,
                            entry.conversation or "",
                        ],
                    )
                ).lower()
                score = sum(1 for tok in tokens if tok in haystack)
                if score > 0:
                    scored.append((score, entry.timestamp, entry))

        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [item[2] for item in scored[:limit]]
