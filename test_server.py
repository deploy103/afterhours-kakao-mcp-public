"""Standalone HTTP/MCP contracts using disposable data and dummy Win32 only."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

from server import create_app
from windows_agent import Agent, Journal
from test_windows_agent import DummyWindows


class Contract(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tokens = {role: secrets.token_urlsafe(48) for role in ("mcp", "agent", "event")}
        for role, filename in (("mcp", "mcp-token"), ("agent", "windows-agent-token"), ("event", "event-token")):
            (self.root / filename).write_text(self.tokens[role])
        self.config = {"kakao_rooms": ["Owner test"], "kakao_owner_id": "owner",
                       "kakao_send_enabled": False, "event_intake_enabled": True}
        (self.root / "config.json").write_text(json.dumps(self.config))
        self.environment = {"CREDENTIALS_DIRECTORY": str(self.root), "STATE_DIRECTORY": str(self.root / "state"),
                            "KAKAO_CONFIG": str(self.root / "config.json")}
        with patch.dict(os.environ, self.environment):
            self.app = create_app()
        self.service = self.app.service
        self.client = TestClient(self.app, base_url="https://mcp.example.com")
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def headers(self, role):
        return {"Authorization": "Bearer " + self.tokens[role]}

    def rpc(self, method, params=None):
        response = self.client.post("/mcp", headers={**self.headers("mcp"),
            "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"},
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["result"]

    def tool(self, name, **arguments):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        self.assertFalse(result.get("isError"), result)
        return result.get("structuredContent") or json.loads(result["content"][0]["text"])

    def test_identity_tool_surface_and_default_gates(self):
        initialized = self.rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                               "clientInfo": {"name": "fixture", "version": "1"}})
        self.assertEqual(initialized["serverInfo"]["name"], "afterhours-kakao-mcp")
        tools = {tool["name"] for tool in self.rpc("tools/list")["tools"]}
        self.assertEqual(tools, {"kakao_status", "kakao_health", "kakao_list_open_rooms", "kakao_read",
                                "kakao_reply", "job_status", "pending_message_events", "acknowledge_message_event"})
        self.assertFalse(self.tool("kakao_status")["send_enabled"])
        for name, arguments in [("kakao_read", {"room": "private"}),
                                ("kakao_reply", {"room": "Owner test", "text": "dummy"}),
                                ("kakao_read", {"room": "Owner test", "limit": 51})]:
            self.assertTrue(self.rpc("tools/call", {"name": name, "arguments": arguments})["isError"])
        self.assertIsNone(self.service.claim()["job"])

    def test_credentials_hosts_origins_and_body_limits(self):
        for path, correct in [("/mcp", "mcp"), ("/afterhours/agent/jobs", "agent"), ("/events", "event")]:
            self.assertEqual(self.client.get(path).status_code, 401)
            for role in self.tokens:
                if role != correct:
                    self.assertEqual(self.client.get(path, headers=self.headers(role)).status_code, 401)
            self.assertEqual(self.client.get(path, headers={**self.headers(correct), "Host": "evil.invalid"}).status_code, 403)
        for path, role in [("/afterhours/agent/jobs", "agent"), ("/events", "event")]:
            self.assertEqual(self.client.get(path, headers={**self.headers(role), "Origin": "https://mcp.example.com"}).status_code, 403)
        self.assertEqual(self.client.post("/mcp", headers={**self.headers("mcp"), "Origin": "https://evil.invalid"}, json={}).status_code, 403)
        for body in ([], {"result": []}, {"result": {}, "extra": 1}):
            self.assertEqual(self.client.post("/afterhours/agent/jobs/missing", headers=self.headers("agent"), json=body).status_code, 400)
        self.assertEqual(self.client.post("/events", headers=self.headers("event"), content=b"x" * 1048577).status_code, 413)
        self.assertEqual(self.client.get("/healthz").json()["service"], "afterhours-kakao-mcp")

    def test_jobs_claim_once_first_result_wins_and_expiration(self):
        identity = self.service.enqueue("health", {})
        endpoint = "/afterhours/agent/jobs"
        self.assertEqual(self.client.get(endpoint, headers=self.headers("agent")).json()["job"]["id"], identity)
        self.assertIsNone(self.client.get(endpoint, headers=self.headers("agent")).json()["job"])
        for result in ({"ok": True}, {"ok": False}):
            self.assertTrue(self.client.post(endpoint + "/" + identity, headers=self.headers("agent"), json={"result": result}).json()["accepted"])
        self.assertEqual(self.tool("job_status", job_id=identity), {"status": "done", "result": {"ok": True}})
        expired = self.service.enqueue("health", {})
        with self.service.connect() as db:
            db.execute("UPDATE jobs SET created=created-61 WHERE id=?", (expired,))
        self.assertIsNone(self.service.claim()["job"])
        self.assertEqual(self.tool("job_status", job_id=expired)["status"], "expired")

    def test_unresolved_jobs_survive_cleanup_and_bound_the_queue(self):
        from mcp.server.mcpserver.exceptions import ToolError
        for _ in range(20):
            self.service.enqueue("health", {})
            self.service.claim()
        with self.service.connect() as db:
            db.execute("UPDATE jobs SET created=created-90000")
        with self.assertRaisesRegex(ToolError, "queue is full"):
            self.service.enqueue("health", {})
        with self.service.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE status='running'").fetchone()[0], 20)

    def test_timeout_never_requeues_and_disabled_events_stay_off(self):
        with patch("kakao.asyncio.sleep", new_callable=AsyncMock):
            result = self.tool("kakao_health")
        self.assertEqual(result["status"], "unconfirmed")
        self.assertEqual(self.tool("job_status", job_id=result["job_id"])["status"], "expired")
        self.assertIsNone(self.service.claim()["job"])
        (self.root / "config.json").write_text('{"event_intake_enabled":false}')
        with patch.dict(os.environ, self.environment), TestClient(create_app(), base_url="https://mcp.example.com") as client:
            self.assertEqual(client.post("/events", headers=self.headers("event"), json=self.event()).status_code, 503)

    def test_uvicorn_starts_and_serves_authenticated_mcp_over_tcp(self):
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        environment = {**os.environ, **self.environment}
        process = subprocess.Popen([sys.executable, "-B", "-m", "uvicorn", "server:create_app", "--factory",
                                    "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
                                   env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def request(path, data=None, auth=False):
            return opener.open(urllib.request.Request(f"http://127.0.0.1:{port}" + path, data=data,
                headers={"Host": "mcp.example.com", "Accept": "application/json, text/event-stream",
                         "Content-Type": "application/json", **(self.headers("mcp") if auth else {})}), timeout=2)

        try:
            deadline = time.monotonic() + 5
            while True:
                try:
                    with request("/healthz") as reply:
                        self.assertEqual(json.load(reply)["status"], "ok")
                    break
                except urllib.error.URLError:
                    if time.monotonic() >= deadline or process.poll() is not None:
                        self.fail("Standalone uvicorn did not start")
                    time.sleep(0.05)
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                request("/mcp", b"{}")
            self.assertEqual(rejected.exception.code, 401)
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
            with request("/mcp", body, auth=True) as reply:
                self.assertEqual(len(json.load(reply)["result"]["tools"]), 8)
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_mcp_queue_guarded_agent_roundtrip(self):
        client, headers = self.client, self.headers("agent")

        class Transport:
            def fetch(self):
                return client.get("/afterhours/agent/jobs", headers=headers).json()["job"]

            def submit(self, identity, result):
                reply = client.post("/afterhours/agent/jobs/" + identity, headers=headers, json={"result": result})
                if reply.status_code != 200 or reply.json().get("accepted") is not True:
                    raise RuntimeError("fixture submission failed")

        windows = DummyWindows()
        worker = Agent({"allowed_rooms": ["Owner test"], "send_enabled": True}, windows.desktop(),
                       Transport(), Journal(self.root / "journal.json"))
        self.service.config["kakao_send_enabled"] = True
        with ThreadPoolExecutor(max_workers=1) as executor:
            for name, arguments in [("kakao_health", {}), ("kakao_list_open_rooms", {}),
                                    ("kakao_read", {"room": "Owner test", "limit": 1}),
                                    ("kakao_reply", {"room": "Owner test", "text": "dummy"})]:
                future = executor.submit(self.tool, name, **arguments)
                deadline = time.monotonic() + 5
                while not future.done() and time.monotonic() < deadline:
                    worker.tick()
                    time.sleep(0.01)
                result = future.result(timeout=2)
                self.assertTrue(result["ok"])
                if name == "kakao_read":
                    self.assertEqual(result["messages"][0]["text"], "second")
                if name == "kakao_reply":
                    self.assertEqual(result["status"], "dispatched")
                    self.assertFalse(result["delivery_confirmed"])
        self.assertEqual(windows.actions.count(("press", 13)), 1)

    def event(self, **changes):
        return {"version": 1, "kind": "MSG", "channelId": "9007199254740993", "logId": "9007199254740995",
                "senderId": "coworker", "room": "Owner test", "text": "업무 확인",
                "occurred_at": "2026-10-25T17:00:00+09:00", **changes}

    def post_event(self, **changes):
        return self.client.post("/events", headers=self.headers("event"), json=self.event(**changes))

    def test_event_filter_native_mentions_string_ids_dedup_and_ack(self):
        accepted = self.post_event().json()
        self.assertTrue(accepted["accepted"])
        self.assertTrue(self.post_event(text="duplicate changed text").json()["duplicate"])
        mention = self.post_event(logId="2", room="Unselected", mention_source="native", mention_user_ids=["owner"])
        self.assertTrue(mention.json()["accepted"])
        for changes in [{"kind": "FEED"}, {"senderId": "owner"}, {"room": "Unselected", "text": "@owner"},
                        {"room": "Unselected", "mention_user_ids": ["owner"], "mention_source": "unknown"}]:
            self.assertFalse(self.post_event(**changes).json()["accepted"])
        self.assertEqual(self.post_event(channelId=9007199254740993).status_code, 400)
        self.assertEqual(self.post_event(occurred_at="2026-10-25T17:00:00").status_code, 400)
        pending = self.tool("pending_message_events")
        self.assertFalse(pending["execution_automatic"])
        self.assertEqual([row["reason"] for row in pending["events"]], ["selected_room", "owner_mention"])
        self.assertEqual(pending["events"][0]["event"]["channelId"], "9007199254740993")
        self.assertEqual(pending["events"][0]["event"]["text"], "업무 확인")
        self.assertTrue(self.tool("acknowledge_message_event", event_id=accepted["event_id"], status="done")["updated"])
        self.assertFalse(self.tool("acknowledge_message_event", event_id=accepted["event_id"], status="ignored")["updated"])
        self.assertTrue(self.post_event().json()["duplicate"])
        self.assertEqual(len(self.tool("pending_message_events")["events"]), 1)
        self.assertTrue(self.rpc("tools/call", {"name": "kakao_read", "arguments": {"room": "Unselected"}})["isError"])

    def test_startup_rejects_shared_credentials_and_implicit_send(self):
        with patch.dict(os.environ, self.environment):
            (self.root / "windows-agent-token").write_text(self.tokens["mcp"])
            with self.assertRaisesRegex(ValueError, "distinct"):
                create_app()
            (self.root / "windows-agent-token").write_text(self.tokens["agent"])
            (self.root / "config.json").write_text('{"kakao_send_enabled":"true"}')
            with self.assertRaises(ValueError):
                create_app()


if __name__ == "__main__":
    unittest.main()
