"""Restricted Windows KakaoTalk worker; run in the user's interactive session.

Config (JSON): allowed_rooms: list[str], send_enabled: bool (default false),
optional kakao_exe: absolute path. token.txt lives alongside the config, never in
arguments/environment. That directory and its files must be private to the user
and SYSTEM (windows_setup.ps1 prepares them). No job can select paths or code.
Set KAKAO_AGENT_ENDPOINT to the operator's HTTPS queue URL before launch.

The bounded journal is a safety ledger, not a message log. Read results remain in
memory only. Send IDs are never evicted: when the ledger fills, processing stops
until an operator archives it *after* ensuring old server jobs cannot reappear.
Do not delete the ledger to troubleshoot a pending/uncertain send.
"""

from __future__ import annotations

import argparse
from collections import OrderedDict
from contextlib import contextmanager
import importlib
import json
import ntpath
import os
from pathlib import Path
import re
import stat
import threading
import time
import urllib.parse
import urllib.request


ENDPOINT = "https://mcp.example.com/afterhours/agent/jobs"
MAX_HTTP_BYTES = 1024 * 1024
MAX_JOBS = 256
MAX_RAW_CHARS = 512 * 1024
JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
GUI_LOCK = threading.RLock()


class AgentError(Exception):
    """Only fixed, non-content-bearing codes may cross this boundary."""


def require_private(path: Path) -> None:
    """Reject symlinks and broadly readable/writable token/config/state paths."""
    if path.is_symlink():
        raise AgentError("unsafe_local_permissions")
    if os.name != "nt":  # Enables safe, disposable Linux fixtures.
        info = path.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise AgentError("unsafe_local_permissions")
        return
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32con.TOKEN_QUERY
    )
    try:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        win32api.CloseHandle(token)
    allowed = {win32security.ConvertSidToStringSid(sid), "S-1-5-18"}
    acl = win32security.GetFileSecurity(
        str(path), win32security.DACL_SECURITY_INFORMATION
    ).GetSecurityDescriptorDacl()
    if acl is None or not acl.GetAceCount():
        raise AgentError("unsafe_local_permissions")
    for index in range(acl.GetAceCount()):
        ace = acl.GetAce(index)
        ace_type = ace[0][0]
        if ace_type == win32security.ACCESS_DENIED_ACE_TYPE:
            continue
        if ace_type != win32security.ACCESS_ALLOWED_ACE_TYPE:
            raise AgentError("unsafe_local_permissions")
        if win32security.ConvertSidToStringSid(ace[2]) not in allowed:
            raise AgentError("unsafe_local_permissions")


def load_config(path: Path) -> dict:
    require_private(path.parent)
    require_private(path)
    if path.stat().st_size > 65536:
        raise AgentError("invalid_config")
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(config, dict) or set(config) - {
        "allowed_rooms", "send_enabled", "kakao_exe"
    }:
        raise AgentError("invalid_config")
    rooms = config.get("allowed_rooms", [])
    if (
        not isinstance(rooms, list) or len(rooms) > 100
        or any(not isinstance(room, str) or not room or len(room) > 256 for room in rooms)
        or len(set(rooms)) != len(rooms)
        or type(config.get("send_enabled", False)) is not bool
    ):
        raise AgentError("invalid_config")
    if "kakao_exe" in config and (
        not isinstance(config["kakao_exe"], str)
        or not ntpath.isabs(config["kakao_exe"])
        or ntpath.basename(config["kakao_exe"]).casefold() != "kakaotalk.exe"
    ):
        raise AgentError("invalid_config")
    return config


def load_token(path: Path) -> str:
    require_private(path.parent)
    require_private(path)
    if not path.is_file() or path.stat().st_size > 4096:
        raise AgentError("invalid_token_file")
    token = path.read_text(encoding="utf-8").strip()
    if not token or not re.fullmatch(r"[A-Za-z0-9._~+/=-]{16,4096}", token):
        raise AgentError("invalid_token_file")
    return token


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AgentError("redirect_refused")


class Transport:
    def __init__(self, token_file: Path):
        self.token_file = token_file
        self.endpoint = os.environ.get("KAKAO_AGENT_ENDPOINT", ENDPOINT)
        parsed = urllib.parse.urlsplit(self.endpoint)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path != "/afterhours/agent/jobs" or parsed.query or parsed.fragment
                or parsed.port not in (None, 443)):
            raise AgentError("invalid_endpoint")
        # No ambient proxy configuration; normal HTTPSHandler verifies TLS.
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

    def request(self, method: str, job_id: str | None = None, result=None) -> dict:
        url = self.endpoint
        if job_id is not None:
            if not JOB_ID.fullmatch(job_id):
                raise AgentError("invalid_job_id")
            url += "/" + urllib.parse.quote(job_id, safe="")
        data = None if result is None else json.dumps(
            {"result": result}, ensure_ascii=False
        ).encode("utf-8")
        if data is not None and len(data) > MAX_HTTP_BYTES:
            raise AgentError("result_too_large")
        request = urllib.request.Request(
            url, data=data, method=method,
            headers={
                "Authorization": "Bearer " + load_token(self.token_file),
                "Content-Type": "application/json", "Accept": "application/json",
                "User-Agent": "AfterhoursKakaoAgent/1.0",
            },
        )
        with self.opener.open(request, timeout=10) as response:
            if response.status != 200:
                raise AgentError("unexpected_http_status")
            raw = response.read(MAX_HTTP_BYTES + 1)
        if len(raw) > MAX_HTTP_BYTES:
            raise AgentError("response_too_large")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise AgentError("invalid_http_response")
        return parsed

    def fetch(self):
        reply = self.request("GET")
        if "job" not in reply or (reply["job"] is not None and not isinstance(reply["job"], dict)):
            raise AgentError("invalid_http_response")
        return reply["job"]

    def submit(self, job_id, result):
        reply = self.request("POST", job_id, result)
        # The ASGI queue acknowledges with accepted=true; result envelopes are
        # also supported by the documented REST contract. Never accept false.
        if reply.get("accepted") is not True and not isinstance(reply.get("result"), dict):
            raise AgentError("invalid_http_response")


def parse_messages(parser, raw: str, limit: int) -> list[dict]:
    """Parse sections separately: upstream drops message-to-date association."""
    messages = []
    date = None
    section = []

    def flush():
        for message in parser.parse_chat_text("\n".join(section))["messages"]:
            messages.append({**message, "date": date})
        section.clear()

    for line in raw.splitlines():
        match = parser.DATE_SEPARATOR_PATTERN.match(line.strip())
        if match:
            flush()
            date = match.group(1)
        else:
            section.append(line)
    flush()
    return messages[-limit:]


class WindowsDesktop:
    """Only already-open, exact-title rooms owned by KakaoTalk.exe are usable.

    The upstream high-level send/read routines are deliberately NOT called:
    their title-only lookup and unchecked global keyboard focus are unsafe.
    """

    def __init__(self, config: dict):
        self.settings = config
        self.gui = importlib.import_module("win32gui")
        self.api = importlib.import_module("win32api")
        self.process = importlib.import_module("win32process")
        self.clipboard = importlib.import_module("win32clipboard")
        self.controller = importlib.import_module("kakao_mcp.controller")
        self.parser = importlib.import_module("kakao_mcp.parser")
        self.constants = importlib.import_module("kakao_mcp.config")

    def process_path(self, pid):
        # QUERY_INFORMATION | VM_READ; inaccessible processes are never trusted.
        handle = self.api.OpenProcess(0x0410, False, pid)
        try:
            return self.process.GetModuleFileNameEx(handle, 0)
        finally:
            self.api.CloseHandle(handle)

    def is_kakao(self, hwnd):
        try:
            _, pid = self.process.GetWindowThreadProcessId(hwnd)
            return ntpath.basename(self.process_path(pid)).casefold() == "kakaotalk.exe"
        except Exception:
            return False

    def candidates(self, room=None):
        windows = []

        def visit(hwnd, unused):
            if (
                self.gui.IsWindowVisible(hwnd)
                and self.gui.GetClassName(hwnd) == self.constants.KAKAO_CHAT_WINDOW_CLASS
            ):
                title = self.gui.GetWindowText(hwnd)
                if title and (room is None or title == room):
                    windows.append((hwnd, title))
            return True

        self.gui.EnumWindows(visit, None)
        return windows

    def verified_window(self, room):
        windows = self.candidates(room)
        # Count all same-title/class windows, including impostors: fail closed.
        if len(windows) != 1:
            raise AgentError("room_not_open" if not windows else "ambiguous_room")
        hwnd = windows[0][0]
        if not self.is_kakao(hwnd):
            raise AgentError("wrong_window_process")
        return hwnd

    def health(self):
        running = False
        for pid in self.process.EnumProcesses():
            try:
                if ntpath.basename(self.process_path(pid)).casefold() == "kakaotalk.exe":
                    running = True
                    break
            except Exception:
                continue
        paths = [self.settings["kakao_exe"]] if self.settings.get("kakao_exe") else [
            str(Path(os.environ.get(variable, default)) / "Kakao" / "KakaoTalk" / "KakaoTalk.exe")
            for variable, default in (
                ("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                ("ProgramFiles", r"C:\Program Files"),
                ("LOCALAPPDATA", r"C:\Users\Default\AppData\Local"),
            )
        ]
        installed = running or any(Path(path).is_file() for path in paths)
        return {
            "ok": True, "installed": installed, "running": running,
            "status": "running" if running else "not-running" if installed else "not-installed",
            "send_enabled": self.settings.get("send_enabled", False),
        }

    def list_rooms(self, allowed):
        rooms = []
        for room in sorted(allowed):
            try:
                self.verified_window(room)
                rooms.append(room)
            except AgentError:
                continue
        return {"ok": True, "rooms": rooms}

    def focus_control(self, room, hwnd, child):
        if self.verified_window(room) != hwnd or not self.gui.IsChild(hwnd, child):
            raise AgentError("window_changed")
        if not self.is_kakao(child):
            raise AgentError("wrong_control_process")
        self.controller.bring_window_to_front(hwnd)
        time.sleep(self.constants.WINDOW_ACTIVATE_WAIT_SEC)
        self.check_focus(room, hwnd, child, set_focus=True)

    def check_focus(self, room, hwnd, child, set_focus=False):
        if self.verified_window(room) != hwnd or self.gui.GetForegroundWindow() != hwnd:
            raise AgentError("focus_not_verified")
        if not self.gui.IsChild(hwnd, child) or not self.is_kakao(child):
            raise AgentError("focus_not_verified")
        current = self.api.GetCurrentThreadId()
        target, _ = self.process.GetWindowThreadProcessId(hwnd)
        attached = current != target
        if attached:
            self.process.AttachThreadInput(current, target, True)
        try:
            if set_focus:
                self.gui.SetFocus(child)
            if self.gui.GetFocus() != child:
                raise AgentError("focus_not_verified")
        finally:
            if attached:
                self.process.AttachThreadInput(current, target, False)

    def clipboard_text(self):
        self.clipboard.OpenClipboard()
        try:
            if self.clipboard.IsClipboardFormatAvailable(self.clipboard.CF_UNICODETEXT):
                return self.clipboard.GetClipboardData(self.clipboard.CF_UNICODETEXT)
            return None
        finally:
            self.clipboard.CloseClipboard()

    def set_clipboard(self, text):
        self.clipboard.OpenClipboard()
        try:
            self.clipboard.EmptyClipboard()
            if text is not None:
                self.clipboard.SetClipboardText(text, self.clipboard.CF_UNICODETEXT)
        finally:
            self.clipboard.CloseClipboard()

    @contextmanager
    def saved_clipboard(self):
        # If snapshot is unavailable, abort before touching GUI/clipboard.
        previous = self.clipboard_text()
        try:
            yield
        finally:
            for _ in range(5):
                try:
                    self.set_clipboard(previous)
                    break
                except Exception:
                    time.sleep(0.1)
            # Other formats are not preserved; never print clipboard text.

    def control(self, hwnd, class_name):
        child = self.controller.find_child_window_recursive(hwnd, class_name)
        if child is None:
            raise AgentError("control_not_found")
        return child

    def send(self, room, text):
        with GUI_LOCK, self.saved_clipboard():
            hwnd = self.verified_window(room)
            child = self.control(hwnd, self.constants.KAKAO_EDIT_CLASS)
            self.focus_control(room, hwnd, child)
            # Do not append to an unsent user draft, or silently destroy one.
            # GetWindowText cannot read another process's edit control text.
            _, draft_length = self.gui.SendMessageTimeout(child, 0x000E, 0, 0, 2, 1000)
            if draft_length:
                raise AgentError("draft_not_empty")
            self.set_clipboard(text)
            self.check_focus(room, hwnd, child)
            self.controller._send_ctrl_key_combo(0x56)
            time.sleep(self.constants.AFTER_PASTE_WAIT_SEC)
            self.check_focus(room, hwnd, child)
            self.controller._press_key(self.constants.VK_RETURN)
            # UI dispatch is not a delivery acknowledgement from KakaoTalk.
            return {"ok": True, "status": "dispatched", "delivery_confirmed": False}

    def read(self, room, limit):
        with GUI_LOCK, self.saved_clipboard():
            hwnd = self.verified_window(room)
            child = self.control(hwnd, self.constants.KAKAO_LIST_CONTROL_CLASS)
            self.focus_control(room, hwnd, child)
            self.set_clipboard("")  # Never accidentally return an old clipboard.
            for key in (self.constants.VK_A, self.constants.VK_C):
                self.check_focus(room, hwnd, child)
                self.controller._send_ctrl_key_combo(key)
                time.sleep(self.constants.KEY_COMBO_WAIT_SEC)
            raw = self.clipboard_text()
            if not isinstance(raw, str) or not raw:
                raise AgentError("clipboard_read_empty")
            if len(raw) > MAX_RAW_CHARS:
                raise AgentError("clipboard_too_large")
            messages = parse_messages(self.parser, raw, limit)
            result = {"ok": True, "room": room, "messages": messages}
            if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_HTTP_BYTES - 64:
                raise AgentError("result_too_large")
            return result


class Journal:
    def __init__(self, path: Path, max_jobs=MAX_JOBS):
        self.path = path
        self.max_jobs = max_jobs
        self.records = OrderedDict()
        if path.exists():
            require_private(path)
            if path.stat().st_size > 4 * MAX_HTTP_BYTES:
                raise AgentError("invalid_journal")
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1:
                raise AgentError("invalid_journal")
            records = data.get("records")
            if not isinstance(records, dict) or len(records) > max_jobs:
                raise AgentError("invalid_journal")
            for job_id, record in records.items():
                if (
                    not JOB_ID.fullmatch(job_id) or not isinstance(record, dict)
                    or record.get("operation") not in {"health", "list_rooms", "read", "send", "invalid"}
                    or record.get("status") not in {"started", "done", "acknowledged"}
                    or ("result" in record and not isinstance(record["result"], dict))
                ):
                    raise AgentError("invalid_journal")
                self.records[job_id] = record
        # Results of reads never hit disk. Interrupted jobs never run again.
        for record in self.records.values():
            if record["status"] == "started" or "result" not in record:
                record["result"] = {"ok": False, "error": "interrupted_result_unknown", "retry_safe": False}
                if record["status"] != "acknowledged":
                    record["status"] = "done"

    def save(self):
        records = OrderedDict()
        for job_id, record in self.records.items():
            records[job_id] = {
                key: value for key, value in record.items()
                if not (record["operation"] == "read" and key == "result")
            }
        payload = json.dumps({"version": 1, "records": records}, ensure_ascii=False).encode("utf-8")
        temporary = self.path.with_suffix(".tmp")
        # O_EXCL prevents following a pre-existing temporary symlink/file.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if temporary.exists():
                temporary.unlink()

    def start(self, job_id, operation):
        # Never evict sends, including acknowledged sends (replay tombstones).
        while len(self.records) >= self.max_jobs:
            removable = next((
                key for key, value in self.records.items()
                if value["operation"] != "send" and value["status"] == "acknowledged"
            ), None)
            if removable is None:
                raise AgentError("journal_full_operator_required")
            del self.records[removable]
        self.records[job_id] = {"operation": operation, "status": "started"}
        self.save()  # Must be durable BEFORE any possible send action.


class Agent:
    def __init__(self, config, desktop, transport, journal):
        self.config = config
        self.desktop = desktop
        self.transport = transport
        self.journal = journal
        self.lock = threading.Lock()

    def execute(self, job):
        operation = job.get("operation")
        if operation not in {"health", "list_rooms", "read", "send"}:
            raise AgentError("operation_not_allowed")
        expected = {"id", "operation"}
        if operation in {"read", "send"}:
            expected.add("room")
        if operation == "read":
            expected.add("limit")
        if operation == "send":
            expected.add("text")
        if set(job) - expected:
            raise AgentError("invalid_job_fields")
        if operation == "health":
            return self.desktop.health()
        allowed = self.config.get("allowed_rooms", [])
        if operation == "list_rooms":
            return self.desktop.list_rooms(allowed)
        room = job.get("room")
        if not isinstance(room, str) or room not in allowed:
            raise AgentError("room_not_allowed")
        if operation == "read":
            limit = job.get("limit", 30)
            if type(limit) is not int or not 1 <= limit <= 100:
                raise AgentError("invalid_limit")
            return self.desktop.read(room, limit)
        if self.config.get("send_enabled") is not True:
            raise AgentError("send_disabled")
        text = job.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 10000 or "\x00" in text:
            raise AgentError("invalid_text")
        return self.desktop.send(room, text)

    def submit(self, job_id, record):
        self.transport.submit(job_id, record["result"])
        record["status"] = "acknowledged"
        self.journal.save()

    def tick(self):
        with self.lock:
            # Do not fetch/execute new jobs while any result is unacknowledged.
            for job_id, record in self.journal.records.items():
                if record["status"] != "acknowledged":
                    self.submit(job_id, record)
                    return
            job = self.transport.fetch()
            if job is None:
                return
            job_id = job.get("id")
            if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
                raise AgentError("invalid_job_id")
            if job_id in self.journal.records:
                self.submit(job_id, self.journal.records[job_id])
                return
            operation = job.get("operation")
            operation = operation if operation in {"health", "list_rooms", "read", "send"} else "invalid"
            self.journal.start(job_id, operation)
            record = self.journal.records[job_id]
            try:
                result = self.execute(job)
            except AgentError as exc:
                result = {"ok": False, "error": str(exc)}
                if operation == "send":
                    result["retry_safe"] = False
            except Exception:
                # Never expose exception text: upstream/Windows errors may echo content.
                result = {"ok": False, "error": "operation_failed", "retry_safe": False}
            record.update(status="done", result=result)
            self.journal.save()
            self.submit(job_id, record)


@contextmanager
def single_instance():
    import win32api
    import win32event

    mutex = win32event.CreateMutex(None, True, r"Local\AfterhoursKakaoAgent")
    try:
        if win32api.GetLastError() == 183:
            raise AgentError("agent_already_running")
        yield
    finally:
        win32api.CloseHandle(mutex)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("Windows interactive session required")
    config_path = args.config.absolute()
    try:
        with single_instance():
            config = load_config(config_path)
            token_path = config_path.parent / "token.txt"
            load_token(token_path)
            agent = Agent(
                config, WindowsDesktop(config), Transport(token_path),
                Journal(config_path.parent / "journal.json"),
            )
            while True:
                started = time.monotonic()
                try:
                    agent.tick()
                except Exception:
                    # Fixed diagnostic only: no token, URL, job, room or message.
                    print("Agent cycle failed; pending work retained.", flush=True)
                time.sleep(max(0.1, 2.0 - (time.monotonic() - started)))
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("Agent startup failed; check private configuration and dependencies.", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
