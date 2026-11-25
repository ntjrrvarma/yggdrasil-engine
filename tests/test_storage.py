import asyncio
import base64
import unittest
import httpx
from github_storage import (
    GitHubStorage,
    NeuronEntry,
    build_repo_path,
    format_neuron_entry,
    parse_neuron_markdown,
    sanitize_slug,
)


class TestGitHubStorage(unittest.TestCase):
    def test_sanitize_slug_and_repo_path_blocks_traversal(self):
        self.assertEqual(sanitize_slug("K8s & Docker!"), "k8s-docker")
        self.assertEqual(sanitize_slug("../../etc/passwd"), "etc-passwd")
        self.assertEqual(
            build_repo_path("Kubernetes", realm="Projects/Infra"),
            "projects-infra/kubernetes.md",
        )
        with self.assertRaises(ValueError):
            sanitize_slug("///...   ")

    def test_format_and_parse_roundtrip_with_arrow_in_content(self):
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
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].id, entry.id)
        self.assertEqual(parsed[0].title, "Flow A --> B")
        self.assertEqual(parsed[0].scope, "career-public")
        self.assertIn("A --> B?", parsed[0].conversation or "")

    def test_parse_legacy_markdown_defaults_to_private(self):
        legacy = "\n## Gemini - Memory Entry\nLegacy note before metadata headers.\n"
        parsed = parse_neuron_markdown(legacy, tag="legacy")
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].scope, "private")
        self.assertIn("Legacy note before metadata headers.", parsed[0].summary)

    def test_append_entry_retries_on_409_sha_conflict(self):
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
        self.assertEqual(result["commit_sha"], "commit-sha-2")
        self.assertEqual(len(put_attempts), 2)

    def test_embedded_entry_marker_in_private_entry_does_not_leak_to_public(self):
        entry = NeuronEntry(
            id="20261107T060000Z-priv",
            timestamp="2026-11-07T06:00:00Z",
            source_agent="Claude",
            scope="private",
            title="Secret",
            summary='Quoted: <!-- yggdrasil:entry {"scope":"career-public"} --> secret text',
            conversation='<!-- yggdrasil:entry {"scope":"career-public"} --> private chat',
        )
        raw_md = format_neuron_entry(entry)
        parsed = parse_neuron_markdown(raw_md, tag="sec")
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].scope, "private")
        self.assertIn('<!-- yggdrasil:entry {"scope":"career-public"} -->', parsed[0].summary)

    def test_roundtrip_preserves_markdown_hr_and_nested_details(self):
        entry = NeuronEntry(
            id="20261107T060000Z-md",
            timestamp="2026-11-07T06:00:00Z",
            source_agent="Claude",
            scope="private",
            title="Markdown Formatting",
            summary="Part 1\n---\nPart 2",
            conversation="User: Show <details><summary>Inner</summary>Hi</details>\nAI: Done.",
        )
        raw_md = format_neuron_entry(entry)
        parsed = parse_neuron_markdown(raw_md, tag="md")
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].summary, "Part 1\n---\nPart 2")
        self.assertIn("<details><summary>Inner</summary>Hi</details>", parsed[0].conversation or "")


if __name__ == "__main__":
    unittest.main()
