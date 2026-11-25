import asyncio
import unittest
import httpx
import main
from mcp_server import handle_jsonrpc_message
from tests.test_api import make_mock_storage


class TestMCPServer(unittest.TestCase):
    def setUp(self):
        self._orig_api_key = main.API_KEY
        self._orig_get_storage = main.get_storage

    def tearDown(self):
        main.API_KEY = self._orig_api_key
        main.get_storage = self._orig_get_storage

    def test_mcp_initialize_tools_list_and_call(self):
        main.API_KEY = "admin-secret"
        mock_storage = make_mock_storage()
        main.get_storage = lambda: mock_storage

        async def _run():
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://yggdrasil"
            ) as client:
                init_res = await handle_jsonrpc_message(
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    client=client,
                    api_key="admin-secret",
                )
                self.assertEqual(
                    init_res["result"]["serverInfo"]["name"], "yggdrasil-cosmos-mcp"
                )

                tools_res = await handle_jsonrpc_message(
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                    client=client,
                    api_key="admin-secret",
                )
                tool_names = {t["name"] for t in tools_res["result"]["tools"]}
                self.assertEqual(
                    tool_names,
                    {
                        "commit_to_memory",
                        "recall_memory",
                        "search_memories",
                        "list_memory_tags",
                    },
                )

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
                self.assertFalse(call_add["result"]["isError"])

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
                self.assertIn("World Tree Architecture", text_out)

                # Slash in tag should be URL-encoded and non-dict JSON should return JSON-RPC error
                bad_json_res = await handle_jsonrpc_message(
                    ["not", "a", "dict"],
                    client=client,
                    api_key="admin-secret",
                )
                self.assertIn("error", bad_json_res)

                add_slash = await handle_jsonrpc_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 5,
                        "method": "tools/call",
                        "params": {
                            "name": "commit_to_memory",
                            "arguments": {
                                "tag": "Projects/K8s",
                                "summary": "Slash tag works",
                                "source_agent": "Antigravity",
                            },
                        },
                    },
                    client=client,
                    api_key="admin-secret",
                )
                self.assertFalse(add_slash["result"]["isError"])

                recall_slash = await handle_jsonrpc_message(
                    {
                        "jsonrpc": "2.0",
                        "id": 6,
                        "method": "tools/call",
                        "params": {
                            "name": "recall_memory",
                            "arguments": {"tag": "Projects/K8s"},
                        },
                    },
                    client=client,
                    api_key="admin-secret",
                )
                self.assertFalse(recall_slash["result"]["isError"])
                self.assertIn("Slash tag works", recall_slash["result"]["content"][0]["text"])

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
