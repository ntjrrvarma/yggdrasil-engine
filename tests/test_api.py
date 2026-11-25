import base64
import json
import unittest
import httpx
from fastapi.testclient import TestClient
import main
from github_storage import GitHubStorage


def make_mock_storage() -> GitHubStorage:
    files: dict[str, tuple[str, str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and "/git/trees/" in path:
            tree = [
                {"path": k, "type": "blob", "size": len(v[0])}
                for k, v in files.items()
            ]
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
                body = json.loads(request.read().decode("utf-8"))
                decoded = base64.b64decode(body["content"]).decode("utf-8")
                files[rel] = (decoded, "sha-new")
                return httpx.Response(200, json={"commit": {"sha": "commit-123"}})
        return httpx.Response(404)

    return GitHubStorage(
        token="tok", user="rahul", repo="brain", transport=httpx.MockTransport(handler)
    )


class TestYggdrasilAPI(unittest.TestCase):
    def setUp(self):
        self._orig_api_key = main.API_KEY
        self._orig_public_key = getattr(main, "PUBLIC_READ_KEY", None)
        self._orig_get_storage = getattr(main, "get_storage", None)

    def tearDown(self):
        main.API_KEY = self._orig_api_key
        if hasattr(main, "PUBLIC_READ_KEY"):
            main.PUBLIC_READ_KEY = self._orig_public_key
        if self._orig_get_storage is not None:
            main.get_storage = self._orig_get_storage

    def test_unset_api_key_rejects_bearer_none(self):
        main.API_KEY = None
        main.PUBLIC_READ_KEY = None
        client = TestClient(main.app)
        res = client.post(
            "/memory/add",
            headers={"Authorization": "Bearer None"},
            json={"tag": "k8s", "summary": "s", "source_agent": "a"},
        )
        self.assertEqual(res.status_code, 403)

    def test_add_read_search_and_public_scope_isolation(self):
        main.API_KEY = "admin-secret"
        main.PUBLIC_READ_KEY = "public-twin-key"
        mock_storage = make_mock_storage()
        main.get_storage = lambda: mock_storage

        client = TestClient(main.app)

        # 1. Health check works without auth
        self.assertEqual(client.get("/health").status_code, 200)

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
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.json()["status"], "success")

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
        self.assertEqual(r2.status_code, 200)

        # 3. Public key CANNOT write
        r_denied = client.post(
            "/memory/add",
            headers={"Authorization": "Bearer public-twin-key"},
            json={"tag": "kubernetes", "summary": "hack", "source_agent": "bot"},
        )
        self.assertEqual(r_denied.status_code, 403)

        # 4. Public key reading /memory/read/kubernetes ONLY sees career-public entry
        r_pub_read = client.get(
            "/memory/read/kubernetes",
            headers={"Authorization": "Bearer public-twin-key"},
        )
        self.assertEqual(r_pub_read.status_code, 200)
        pub_data = r_pub_read.json()
        self.assertEqual(len(pub_data["entries"]), 1)
        self.assertEqual(pub_data["entries"][0]["scope"], "career-public")
        self.assertNotIn("password", pub_data["markdown"])

        # 5. Admin key reading /memory/read/kubernetes sees both entries
        r_admin_read = client.get(
            "/memory/read/kubernetes",
            headers={"Authorization": "Bearer admin-secret"},
        )
        self.assertEqual(len(r_admin_read.json()["entries"]), 2)

        # 6. Search picks relevant data and respects scope
        r_search = client.get(
            "/memory/search",
            params={"q": "ArgoCD HTTPRoute"},
            headers={"Authorization": "Bearer public-twin-key"},
        )
        self.assertEqual(r_search.status_code, 200)
        self.assertEqual(len(r_search.json()["results"]), 1)
        self.assertEqual(
            r_search.json()["results"][0]["title"],
            "Production Multi-Cluster Architecture",
        )

    def test_non_ascii_auth_header_returns_403_not_500(self):
        from fastapi import HTTPException

        main.API_KEY = "admin-secret"
        with self.assertRaises(HTTPException) as ctx:
            main.verify_write_access("Bearer café")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_public_key_list_tags_hides_private_only_files_and_search_finds_realm(self):
        main.API_KEY = "admin-secret"
        main.PUBLIC_READ_KEY = "public-twin-key"
        mock_storage = make_mock_storage()
        main.get_storage = lambda: mock_storage
        client = TestClient(main.app)

        client.post(
            "/memory/add",
            headers={"Authorization": "Bearer admin-secret"},
            json={
                "tag": "secrets",
                "summary": "Private bank PIN",
                "source_agent": "Claude",
                "scope": "private",
            },
        )
        client.post(
            "/memory/add",
            headers={"Authorization": "Bearer admin-secret"},
            json={
                "tag": "k8s",
                "realm": "projects",
                "summary": "Public GKE cluster",
                "source_agent": "Claude",
                "scope": "career-public",
            },
        )

        pub_tags = client.get(
            "/memory/tags",
            headers={"Authorization": "Bearer public-twin-key"},
        )
        self.assertEqual(pub_tags.status_code, 200)
        tag_names = {t["tag"] for t in pub_tags.json()["tags"]}
        self.assertEqual(tag_names, {"k8s"})

        r_search = client.get(
            "/memory/search",
            params={"q": "GKE", "tag": "k8s"},
            headers={"Authorization": "Bearer public-twin-key"},
        )
        self.assertEqual(r_search.status_code, 200)
        self.assertEqual(len(r_search.json()["results"]), 1)

    def test_upstream_github_error_returns_502(self):
        main.API_KEY = "admin-secret"

        def fail_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"message": "GitHub Unavailable"})

        bad_storage = GitHubStorage(
            token="tok", user="rahul", repo="brain", transport=httpx.MockTransport(fail_handler)
        )
        main.get_storage = lambda: bad_storage
        client = TestClient(main.app)
        res = client.get(
            "/memory/tags",
            headers={"Authorization": "Bearer admin-secret"},
        )
        self.assertEqual(res.status_code, 502)

    def test_remote_mcp_http_endpoint(self):
        main.API_KEY = "admin-secret"
        main.PUBLIC_READ_KEY = "public-twin-key"
        mock_storage = make_mock_storage()
        main.get_storage = lambda: mock_storage
        client = TestClient(main.app)

        # Unauthenticated MCP request is rejected
        unauth = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        self.assertIn(unauth.status_code, (401, 403))

        # Initialize & tools/list
        init_res = client.post(
            "/mcp",
            headers={"Authorization": "Bearer admin-secret"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        self.assertEqual(init_res.status_code, 200)
        self.assertEqual(
            init_res.json()["result"]["serverInfo"]["name"], "yggdrasil-cosmos-mcp"
        )

        # Notification returns 202
        notif_res = client.post(
            "/mcp",
            headers={"Authorization": "Bearer admin-secret"},
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        self.assertEqual(notif_res.status_code, 202)

        # commit_to_memory & recall_memory over HTTP /mcp
        add_res = client.post(
            "/mcp",
            headers={"Authorization": "Bearer admin-secret"},
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "commit_to_memory",
                    "arguments": {
                        "tag": "remote-mcp",
                        "summary": "Claude, ChatGPT, and Gemini share one brain.",
                        "source_agent": "ChatGPT-Connector",
                        "scope": "career-public",
                    },
                },
            },
        )
        self.assertEqual(add_res.status_code, 200)
        self.assertFalse(add_res.json()["result"]["isError"])

        recall_res = client.post(
            "/mcp",
            headers={"Authorization": "Bearer public-twin-key"},
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "recall_memory",
                    "arguments": {"tag": "remote-mcp"},
                },
            },
        )
        self.assertEqual(recall_res.status_code, 200)
        self.assertIn(
            "Claude, ChatGPT, and Gemini share one brain.",
            recall_res.json()["result"]["content"][0]["text"],
        )


if __name__ == "__main__":
    unittest.main()
