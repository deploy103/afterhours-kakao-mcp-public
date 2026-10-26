"""Offline regression tests. No Windows, network, credentials or live chats used.

Run on development VM with python3 -B -m unittest -v test_windows_agent.py.
The real read-only upstream parser is used; Windows/controller modules are dummy
implementations. AFTERHOURS_REFERENCE may point to another reviewed checkout.
"""

from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import ssl
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import windows_agent as agent


REFERENCE = Path(os.environ.get(
    "AFTERHOURS_REFERENCE",
    str(Path(__file__).resolve().parent / "vendor" / "kakaotalk-mcp"),
))
PARSER_PATH = REFERENCE / "src" / "kakao_mcp" / "parser.py"
spec = importlib.util.spec_from_file_location("reviewed_parser", PARSER_PATH)
parser_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parser_module)


class DummyWindows:
    def __init__(self):
        self.windows = {10: ("Owner test", "EVA_Window_Dblclk", 100)}
        self.children = {11: (10, 100), 12: (10, 100)}
        self.executables = {100: r"C:\Program Files\Kakao\KakaoTalk.exe"}
        self.foreground = 0
        self.focus = 0
        self.clip = "original Unicode 클립보드"
        self.raw = (
            "[Owner test] [대화상대 2명]\n"
            "2026년 10월 24일 토요일\n[A] [오후 11:59] first\ncontinuation\n"
            "--------------- 2026년 10월 25일 일요일 ---------------\n"
            "[B] [오전 12:01] second"
        )
        self.actions = []
        self.draft = ""
        self.lose_focus_on_paste = False
        self.copy_enabled = True
        self.missing_control = False

        def enum(callback, extra):
            for hwnd in list(self.windows):
                callback(hwnd, extra)

        def thread_pid(hwnd):
            pid = self.windows[hwnd][2] if hwnd in self.windows else self.children[hwnd][1]
            return 2, pid

        def combo(key):
            self.actions.append(("combo", key))
            if key == 0x43 and self.copy_enabled:
                self.clip = self.raw
            if key == 0x56 and self.lose_focus_on_paste:
                self.foreground = 999

        self.modules = {
            "win32gui": types.SimpleNamespace(
                EnumWindows=enum, IsWindowVisible=lambda hwnd: True,
                GetClassName=lambda hwnd: self.windows[hwnd][1],
                GetWindowText=lambda hwnd: self.windows[hwnd][0] if hwnd in self.windows else "",
                GetForegroundWindow=lambda: self.foreground,
                IsChild=lambda hwnd, child: child in self.children and self.children[child][0] == hwnd,
                SetFocus=lambda child: setattr(self, "focus", child),
                GetFocus=lambda: self.focus,
                SendMessageTimeout=lambda *args: (1, len(self.draft)),
            ),
            "win32api": types.SimpleNamespace(
                GetCurrentThreadId=lambda: 1,
                OpenProcess=lambda access, inherit, pid: pid,
                CloseHandle=lambda handle: None,
            ),
            "win32process": types.SimpleNamespace(
                GetWindowThreadProcessId=thread_pid,
                GetModuleFileNameEx=lambda pid, module: self.executables[pid],
                EnumProcesses=lambda: list(self.executables),
                AttachThreadInput=lambda *args: None,
            ),
            "win32clipboard": types.SimpleNamespace(
                CF_UNICODETEXT=13, OpenClipboard=lambda: None,
                CloseClipboard=lambda: None,
                IsClipboardFormatAvailable=lambda fmt: self.clip is not None,
                GetClipboardData=lambda fmt: self.clip,
                EmptyClipboard=lambda: setattr(self, "clip", None),
                SetClipboardText=lambda text, fmt: setattr(self, "clip", text),
            ),
            "kakao_mcp.controller": types.SimpleNamespace(
                bring_window_to_front=lambda hwnd: setattr(self, "foreground", hwnd),
                find_child_window_recursive=lambda hwnd, cls: None if self.missing_control else (
                    11 if cls == "RICHEDIT50W" else 12
                ),
                _send_ctrl_key_combo=combo,
                _press_key=lambda key: self.actions.append(("press", key)),
                # High-level, bulk and search calls are deliberately unavailable.
            ),
            "kakao_mcp.config": types.SimpleNamespace(
                KAKAO_CHAT_WINDOW_CLASS="EVA_Window_Dblclk",
                KAKAO_EDIT_CLASS="RICHEDIT50W", KAKAO_LIST_CONTROL_CLASS="EVA_VH_ListControl_Dblclk",
                WINDOW_ACTIVATE_WAIT_SEC=0, AFTER_PASTE_WAIT_SEC=0, KEY_COMBO_WAIT_SEC=0,
                VK_RETURN=13, VK_A=65, VK_C=67,
            ),
            "kakao_mcp.parser": parser_module,
        }

    def desktop(self, config=None):
        with patch.dict("sys.modules", self.modules):
            return agent.WindowsDesktop(config or {})


class DesktopTests(unittest.TestCase):
    def setUp(self):
        self.windows = DummyWindows()
        self.desktop = self.windows.desktop()

    def test_send_checks_process_exact_title_and_restores_unicode(self):
        result = self.desktop.send("Owner test", "dummy message")
        self.assertEqual(result, {"ok": True, "status": "dispatched", "delivery_confirmed": False})
        self.assertEqual(self.windows.actions, [("combo", 86), ("press", 13)])
        self.assertEqual(self.windows.clip, "original Unicode 클립보드")

    def test_wrong_process_is_rejected_before_keyboard_action(self):
        for executable in (r"C:\evil.exe", r"C:\KakaoTalk.exe.bak"):
            with self.subTest(executable=executable):
                self.windows.executables[100] = executable
                with self.assertRaisesRegex(agent.AgentError, "wrong_window_process"):
                    self.desktop.send("Owner test", "dummy")
                self.assertEqual(self.windows.actions, [])

    def test_duplicate_exact_title_fails_closed_even_with_impostor(self):
        for pid in (100, 200):
            with self.subTest(pid=pid):
                self.windows.windows[20] = ("Owner test", "EVA_Window_Dblclk", pid)
                self.windows.executables[200] = r"C:\impostor.exe"
                with self.assertRaisesRegex(agent.AgentError, "ambiguous_room"):
                    self.desktop.send("Owner test", "dummy")
                self.assertEqual(self.windows.actions, [])

    def test_no_partial_or_case_insensitive_room_match(self):
        for title in ("Owner", "owner test", "Owner test "):
            with self.subTest(title=title):
                with self.assertRaisesRegex(agent.AgentError, "room_not_open"):
                    self.desktop.send(title, "dummy")
        self.assertEqual(self.windows.actions, [])

    def test_wrong_child_process_rejected(self):
        self.windows.children[11] = (10, 200)
        self.windows.executables[200] = r"C:\other.exe"
        with self.assertRaisesRegex(agent.AgentError, "wrong_control_process"):
            self.desktop.send("Owner test", "dummy")
        self.assertEqual(self.windows.actions, [])

    def test_focus_loss_after_paste_never_presses_enter(self):
        self.windows.lose_focus_on_paste = True
        with self.assertRaisesRegex(agent.AgentError, "focus_not_verified"):
            self.desktop.send("Owner test", "dummy")
        self.assertEqual(self.windows.actions, [("combo", 86)])
        self.assertEqual(self.windows.clip, "original Unicode 클립보드")

    def test_user_draft_is_not_overwritten_or_sent(self):
        self.windows.draft = "unsent draft"
        with self.assertRaisesRegex(agent.AgentError, "draft_not_empty"):
            self.desktop.send("Owner test", "dummy")
        self.assertEqual(self.windows.actions, [])

    def test_missing_control_does_not_send(self):
        self.windows.missing_control = True
        with self.assertRaisesRegex(agent.AgentError, "control_not_found"):
            self.desktop.send("Owner test", "dummy")
        self.assertEqual(self.windows.actions, [])

    def test_read_keeps_each_messages_date_and_multiline_text(self):
        result = self.desktop.read("Owner test", 100)
        messages = result["messages"]
        self.assertEqual([m["date"] for m in messages], [
            "2026년 10월 24일 토요일", "2026년 10월 25일 일요일",
        ])
        self.assertEqual(messages[0]["text"], "first\ncontinuation")
        self.assertEqual(self.windows.clip, "original Unicode 클립보드")
        self.assertEqual(self.desktop.read("Owner test", 1)["messages"][0]["text"], "second")

    def test_unknown_date_is_not_invented(self):
        result = agent.parse_messages(parser_module, "[A] [오전 8:00] dummy", 10)
        self.assertIsNone(result[0]["date"])

    def test_copy_failure_does_not_return_stale_clipboard(self):
        self.windows.copy_enabled = False
        with self.assertRaisesRegex(agent.AgentError, "clipboard_read_empty"):
            self.desktop.read("Owner test", 10)
        self.assertEqual(self.windows.clip, "original Unicode 클립보드")

    def test_oversized_clipboard_fails_and_restores(self):
        self.windows.raw = "x" * (agent.MAX_RAW_CHARS + 1)
        with self.assertRaisesRegex(agent.AgentError, "clipboard_too_large"):
            self.desktop.read("Owner test", 10)
        self.assertEqual(self.windows.clip, "original Unicode 클립보드")

    def test_list_only_verified_allowlisted_open_rooms(self):
        self.windows.windows[20] = ("Private room", "EVA_Window_Dblclk", 100)
        self.assertEqual(self.desktop.list_rooms(["Owner test", "Closed"]), {
            "ok": True, "rooms": ["Owner test"],
        })
        self.windows.windows[30] = ("Owner test", "EVA_Window_Dblclk", 100)
        self.assertEqual(self.desktop.list_rooms(["Owner test"])["rooms"], [])

    def test_health_not_installed_not_running_and_running(self):
        self.windows.executables.clear()
        with patch.object(Path, "is_file", return_value=False):
            self.assertEqual(self.desktop.health()["status"], "not-installed")
        with patch.object(Path, "is_file", return_value=True):
            self.assertEqual(self.desktop.health()["status"], "not-running")
        self.windows.executables[100] = r"C:\KakaoTalk.exe"
        self.assertEqual(self.desktop.health()["status"], "running")


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.journal_path = self.root / "journal.json"
        self.desktop = Mock()
        self.desktop.send.return_value = {"ok": True, "status": "dispatched"}
        self.desktop.health.return_value = {"ok": True, "status": "not-installed"}
        self.transport = Mock()
        self.job = {"id": "send-001", "operation": "send", "room": "Owner test", "text": "dummy secret text"}
        self.transport.fetch.return_value = self.job
        self.config = {"allowed_rooms": ["Owner test"], "send_enabled": True}
        self.worker = agent.Agent(self.config, self.desktop, self.transport, agent.Journal(self.journal_path))

    def test_failed_result_submission_never_reexecutes_send(self):
        self.transport.submit.side_effect = [TimeoutError("lost response"), None, None]
        with self.assertRaises(TimeoutError):
            self.worker.tick()
        self.worker.tick()  # Retry POST only; no new GET.
        self.assertEqual(self.transport.fetch.call_count, 1)
        self.worker.tick()  # Server repeats the same ID: still POST only.
        self.assertEqual(self.desktop.send.call_count, 1)
        self.assertEqual(self.transport.submit.call_count, 3)
        self.assertNotIn("dummy secret text", self.journal_path.read_text())

    def test_concurrent_ticks_are_serialized_and_send_once(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(lambda _: self.worker.tick(), range(8)))
        self.desktop.send.assert_called_once()
        self.assertEqual(self.transport.submit.call_count, 8)

    def test_restart_after_completed_send_only_resubmits_result(self):
        self.transport.submit.side_effect = OSError("offline")
        with self.assertRaises(OSError):
            self.worker.tick()
        restarted = agent.Agent(self.config, Mock(), self.transport, agent.Journal(self.journal_path))
        self.transport.submit.side_effect = None
        restarted.tick()
        restarted.desktop.send.assert_not_called()
        self.assertEqual(self.transport.submit.call_args.args[1]["status"], "dispatched")

    def test_restart_of_started_send_marks_uncertain_never_retries(self):
        self.worker.journal.start("send-001", "send")
        restarted = agent.Agent(self.config, self.desktop, self.transport, agent.Journal(self.journal_path))
        restarted.tick()
        restarted.tick()  # A replayed same ID after acknowledgement.
        self.desktop.send.assert_not_called()
        self.assertEqual(self.transport.submit.call_args.args[1], {
            "ok": False, "error": "interrupted_result_unknown", "retry_safe": False,
        })

    def test_started_marker_is_durable_before_gui_action(self):
        def inspect_marker(*args):
            data = json.loads(self.journal_path.read_text())
            self.assertEqual(data["records"]["send-001"]["status"], "started")
            return {"ok": True}
        self.desktop.send.side_effect = inspect_marker
        self.worker.tick()
        self.desktop.send.assert_called_once()

    def test_journal_write_failure_prevents_send(self):
        with patch.object(self.worker.journal, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.worker.tick()
        self.desktop.send.assert_not_called()

    def test_controller_exception_is_sanitized_and_not_retried(self):
        self.desktop.send.side_effect = RuntimeError("dummy secret text")
        self.worker.tick()
        self.worker.tick()
        self.assertEqual(self.desktop.send.call_count, 1)
        result = self.transport.submit.call_args.args[1]
        self.assertEqual(result["error"], "operation_failed")
        self.assertFalse(result["retry_safe"])
        self.assertNotIn("dummy secret text", self.journal_path.read_text())

    def test_exact_allowlist_send_gate_and_restricted_operations(self):
        cases = [
            ({**self.job, "room": "Owner"}, "room_not_allowed"),
            ({**self.job, "room": "owner test"}, "room_not_allowed"),
            ({**self.job, "operation": "shell"}, "operation_not_allowed"),
            ({**self.job, "operation": "send_bulk_messages"}, "operation_not_allowed"),
            ({**self.job, "operation": "search_and_open_room"}, "operation_not_allowed"),
            ({**self.job, "path": "anything"}, "invalid_job_fields"),
            ({**self.job, "text": ""}, "invalid_text"),
            ({**self.job, "text": "x" * 10001}, "invalid_text"),
        ]
        for job, error in cases:
            with self.subTest(error=error):
                with self.assertRaisesRegex(agent.AgentError, error):
                    self.worker.execute(job)
        self.config["send_enabled"] = False
        with self.assertRaisesRegex(agent.AgentError, "send_disabled"):
            self.worker.execute(self.job)
        self.desktop.send.assert_not_called()

    def test_read_limit_boundaries(self):
        for limit in (0, 101, True, "10"):
            with self.subTest(limit=limit):
                with self.assertRaisesRegex(agent.AgentError, "invalid_limit"):
                    self.worker.execute({"operation": "read", "room": "Owner test", "limit": limit})
        self.worker.execute({"operation": "read", "room": "Owner test", "limit": 1})
        self.desktop.read.assert_called_once_with("Owner test", 1)

    def test_bounded_journal_never_evicts_send_tombstones(self):
        self.worker.journal.max_jobs = 1
        self.worker.tick()
        self.transport.fetch.return_value = {**self.job, "id": "send-002"}
        with self.assertRaisesRegex(agent.AgentError, "journal_full_operator_required"):
            self.worker.tick()
        self.assertEqual(self.desktop.send.call_count, 1)
        self.assertEqual(list(self.worker.journal.records), ["send-001"])

    def test_acknowledged_non_send_can_be_evicted(self):
        self.worker.journal.max_jobs = 1
        self.transport.fetch.return_value = {"id": "health-001", "operation": "health"}
        self.worker.tick()
        self.transport.fetch.return_value = self.job
        self.worker.tick()
        self.assertEqual(list(self.worker.journal.records), ["send-001"])

    def test_read_result_retried_in_memory_but_never_logged_to_journal(self):
        self.transport.fetch.return_value = {"id": "read-001", "operation": "read", "room": "Owner test"}
        self.desktop.read.return_value = {"ok": True, "messages": [{"text": "dummy private message"}]}
        self.transport.submit.side_effect = [OSError("offline"), None]
        with self.assertRaises(OSError):
            self.worker.tick()
        self.assertNotIn("dummy private message", self.journal_path.read_text())
        self.worker.tick()
        self.desktop.read.assert_called_once()
        self.assertEqual(self.transport.submit.call_args.args[1]["messages"][0]["text"], "dummy private message")

    def test_invalid_id_cannot_reach_gui_or_url(self):
        self.transport.fetch.return_value = {**self.job, "id": "../escape"}
        with self.assertRaisesRegex(agent.AgentError, "invalid_job_id"):
            self.worker.tick()
        self.desktop.send.assert_not_called()
        self.transport.submit.assert_not_called()

    def test_corrupt_journal_fails_closed(self):
        self.journal_path.write_text('{"version":1,"records":[]}')
        self.journal_path.chmod(0o600)
        with self.assertRaisesRegex(agent.AgentError, "invalid_journal"):
            agent.Journal(self.journal_path)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.token_file = self.root / "token.txt"
        self.token_file.write_text("dummy-agent-token-123456789")
        self.token_file.chmod(0o600)
        self.transport = agent.Transport(self.token_file)

    def response(self, body, status=200):
        response = io.BytesIO(body)
        response.status = status
        return response

    def test_authenticated_get_and_post_use_bounded_timeout_and_exact_endpoint(self):
        self.transport.opener.open = Mock(return_value=self.response(b'{"job":null}'))
        self.assertIsNone(self.transport.fetch())
        args, kwargs = self.transport.opener.open.call_args
        self.assertEqual(args[0].full_url, agent.ENDPOINT)
        self.assertEqual(args[0].get_header("Authorization"), "Bearer dummy-agent-token-123456789")
        self.assertEqual(kwargs["timeout"], 10)
        self.transport.opener.open.return_value = self.response(b'{"result":{}}')
        self.transport.submit("send-001", {"ok": True})
        request = self.transport.opener.open.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.full_url, agent.ENDPOINT + "/send-001")
        self.assertEqual(json.loads(request.data), {"result": {"ok": True}})

    def test_operator_configured_endpoint_preserves_https_and_fixed_queue_route(self):
        with patch.dict(os.environ, {"KAKAO_AGENT_ENDPOINT": "https://worker.example.invalid/afterhours/agent/jobs"}):
            transport = agent.Transport(self.token_file)
        transport.opener.open = Mock(return_value=self.response(b'{"job":null}'))
        self.assertIsNone(transport.fetch())
        self.assertEqual(transport.opener.open.call_args.args[0].full_url,
                         "https://worker.example.invalid/afterhours/agent/jobs")
        for endpoint in ("http://worker.example.invalid/afterhours/agent/jobs",
                         "https://user@worker.example.invalid/afterhours/agent/jobs",
                         "https://worker.example.invalid/other", "https://worker.example.invalid/afterhours/agent/jobs?token=x"):
            with self.subTest(endpoint=endpoint), patch.dict(os.environ, {"KAKAO_AGENT_ENDPOINT": endpoint}):
                with self.assertRaisesRegex(agent.AgentError, "invalid_endpoint"):
                    agent.Transport(self.token_file)

    def test_explicit_queue_ack_supported_but_false_ack_rejected(self):
        self.transport.opener.open = Mock(return_value=self.response(b'{"accepted":true}'))
        self.transport.submit("send-001", {"ok": True})
        self.transport.opener.open.return_value = self.response(b'{"accepted":false}')
        with self.assertRaisesRegex(agent.AgentError, "invalid_http_response"):
            self.transport.submit("send-001", {"ok": True})

    def test_token_rotation_is_read_from_file_not_environment(self):
        self.token_file.write_text("rotated-dummy-agent-token")
        self.transport.opener.open = Mock(return_value=self.response(b'{"job":null}'))
        with patch.dict(os.environ, {"AFTERHOURS_AGENT_TOKEN": "ignored-value"}):
            self.transport.fetch()
        request = self.transport.opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer rotated-dummy-agent-token")

    def test_unprotected_token_file_refused(self):
        self.token_file.chmod(0o644)
        with self.assertRaisesRegex(agent.AgentError, "unsafe_local_permissions"):
            agent.load_token(self.token_file)

    def test_windows_acl_accepts_only_current_user_and_system(self):
        acl = Mock()
        aces = [((0, 0), 0x1F01FF, "current-user"), ((0, 0), 0x1F01FF, "S-1-5-18")]
        acl.GetAceCount.side_effect = lambda: len(aces)
        acl.GetAce.side_effect = lambda index: aces[index]
        security = types.SimpleNamespace(
            OpenProcessToken=lambda *args: 1,
            GetTokenInformation=lambda *args: ("current-user", 0),
            ConvertSidToStringSid=lambda sid: sid,
            GetFileSecurity=lambda *args: types.SimpleNamespace(GetSecurityDescriptorDacl=lambda: acl),
            TokenUser=1, DACL_SECURITY_INFORMATION=4,
            ACCESS_DENIED_ACE_TYPE=1, ACCESS_ALLOWED_ACE_TYPE=0,
        )
        modules = {
            "win32api": types.SimpleNamespace(GetCurrentProcess=lambda: 1, CloseHandle=lambda handle: None),
            "win32con": types.SimpleNamespace(TOKEN_QUERY=8),
            "win32security": security,
        }
        with patch.dict("sys.modules", modules), patch.object(agent.os, "name", "nt"):
            agent.require_private(self.token_file)
            aces.append(((0, 0), 0x1F01FF, "S-1-1-0"))  # Everyone.
            with self.assertRaisesRegex(agent.AgentError, "unsafe_local_permissions"):
                agent.require_private(self.token_file)

    def test_config_refuses_implicit_send_and_unknown_options(self):
        path = self.root / "config.json"
        path.write_text('{"allowed_rooms":["Owner test"],"send_enabled":"true"}')
        path.chmod(0o600)
        with self.assertRaisesRegex(agent.AgentError, "invalid_config"):
            agent.load_config(path)
        path.write_text('{"allowed_rooms":[],"endpoint":"http://evil.example"}')
        with self.assertRaisesRegex(agent.AgentError, "invalid_config"):
            agent.load_config(path)

    def test_redirects_refused_and_tls_verification_not_disabled(self):
        with self.assertRaisesRegex(agent.AgentError, "redirect_refused"):
            agent.NoRedirect().redirect_request(None, None, 302, "", {}, "http://evil.invalid")
        handlers = self.transport.opener.handlers
        self.assertTrue(any(isinstance(handler, agent.NoRedirect) for handler in handlers))
        https = next(handler for handler in handlers if isinstance(handler, agent.urllib.request.HTTPSHandler))
        # Python versions differ on whether HTTPSHandler eagerly creates its
        # default context. Assert the actual TLS properties, not that detail.
        context = https._context or ssl.create_default_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_oversized_and_invalid_response_refused(self):
        cases = [
            (b"x" * (agent.MAX_HTTP_BYTES + 1), "response_too_large"),
            (b"[]", "invalid_http_response"),
            (b"{}", "invalid_http_response"),
            (b'{"job":[]}', "invalid_http_response"),
        ]
        for body, error in cases:
            with self.subTest(error=error):
                self.transport.opener.open = Mock(return_value=self.response(body))
                with self.assertRaisesRegex(agent.AgentError, error):
                    self.transport.fetch()


class SetupContractTests(unittest.TestCase):
    def test_setup_pins_exact_reviewed_source_bytes(self):
        setup = Path(__file__).with_name("windows_setup.ps1").read_text()
        pins = dict(re.findall(r"'([^']+\.py)' = '([0-9a-f]{64})'", setup))
        self.assertEqual(set(pins), {"__init__.py", "controller.py", "parser.py", "config.py"})
        for name, digest in pins.items():
            with self.subTest(module=name):
                actual = hashlib.sha256((REFERENCE / "src" / "kakao_mcp" / name).read_bytes()).hexdigest()
                self.assertEqual(actual, digest)


if __name__ == "__main__":
    unittest.main()
