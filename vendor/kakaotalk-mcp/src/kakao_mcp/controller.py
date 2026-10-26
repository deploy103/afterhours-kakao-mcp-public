"""Win32 API wrapper for KakaoTalk PC automation."""
import os
import sys
import time
import ctypes
import shutil
import hashlib
import subprocess
import threading
from collections import deque
from typing import Optional, List, Dict

import win32gui
import win32api
import win32con
import win32clipboard
import win32process

from . import config


def _log(msg: str):
    """Write debug message to stderr (visible in MCP server logs)."""
    print(f"[kakao-controller] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Window discovery
# ---------------------------------------------------------------------------

def is_kakaotalk_running() -> Dict:
    """Check if KakaoTalk main window exists.

    Returns:
        Dict with running (bool), hwnd (int|None), pid (int|None).
    """
    hwnd = win32gui.FindWindow(
        config.KAKAO_MAIN_WINDOW_CLASS, config.KAKAO_MAIN_WINDOW_TITLE
    )
    if hwnd == 0:
        return {"running": False, "hwnd": None, "pid": None}
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    return {"running": True, "hwnd": hwnd, "pid": pid}


def find_chat_window(room_name: str) -> Optional[int]:
    """Find a chat window by exact title (room name).

    Returns hwnd or None.
    """
    results: List[int] = []

    def _cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if cls == config.KAKAO_CHAT_WINDOW_CLASS and title == room_name:
                results.append(hwnd)
        return True

    win32gui.EnumWindows(_cb, None)
    return results[0] if results else None


def list_chat_windows() -> List[Dict]:
    """List all currently open KakaoTalk chat windows.

    Returns list of dicts with hwnd and title.
    """
    main_hwnd = win32gui.FindWindow(
        config.KAKAO_MAIN_WINDOW_CLASS, config.KAKAO_MAIN_WINDOW_TITLE
    )
    windows: List[Dict] = []

    def _cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            cls = win32gui.GetClassName(hwnd)
            title = win32gui.GetWindowText(hwnd)
            if (
                cls == config.KAKAO_CHAT_WINDOW_CLASS
                and hwnd != main_hwnd
                and title
                and title != config.KAKAO_MAIN_WINDOW_TITLE
            ):
                windows.append({"hwnd": hwnd, "title": title})
        return True

    win32gui.EnumWindows(_cb, None)
    return windows


def find_child_window_recursive(parent_hwnd: int, class_name: str) -> Optional[int]:
    """Recursively search for a child window by class name.

    Needed because RICHEDIT50W may be nested several levels deep.
    Returns hwnd or None.
    """
    found: List[int] = []

    def _cb(hwnd, _):
        if win32gui.GetClassName(hwnd) == class_name:
            found.append(hwnd)
            return False  # stop enumeration
        return True

    try:
        win32gui.EnumChildWindows(parent_hwnd, _cb, None)
    except Exception:
        pass
    return found[0] if found else None


def bring_window_to_front(hwnd: int):
    """Restore and bring a window to the foreground.

    Uses ctypes directly (not pywin32) to avoid exceptions on failure,
    and simulates Alt keypress to bypass Windows foreground restrictions.
    """
    VK_MENU = 0x12  # Alt key
    SW_SHOW = 5
    SW_RESTORE = 9
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_SHOWWINDOW = 0x0040

    _user32.ShowWindow(hwnd, SW_RESTORE)

    # Simulate Alt keypress to unlock SetForegroundWindow
    _user32.keybd_event(VK_MENU, 0, 0, 0)
    _user32.keybd_event(VK_MENU, 0, config.KEYEVENTF_KEYUP, 0)

    # Use ctypes SetForegroundWindow (returns 0 on fail, no exception)
    _user32.SetForegroundWindow(hwnd)

    # Also temporarily set topmost to ensure visibility
    _user32.SetWindowPos(
        hwnd, HWND_TOPMOST, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
    )
    _user32.SetWindowPos(
        hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
    )


# ---------------------------------------------------------------------------
# Keyboard helpers
# ---------------------------------------------------------------------------

_user32 = ctypes.windll.user32


def _press_key(vk: int, shift: bool = False):
    """Press and release a single key via keybd_event, optionally with Shift."""
    VK_SHIFT = 0x10
    if shift:
        _user32.keybd_event(VK_SHIFT, 0, 0, 0)
        time.sleep(0.01)
    _user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.01)
    _user32.keybd_event(vk, 0, config.KEYEVENTF_KEYUP, 0)
    if shift:
        time.sleep(0.01)
        _user32.keybd_event(VK_SHIFT, 0, config.KEYEVENTF_KEYUP, 0)
    time.sleep(0.02)


def _send_ctrl_key_combo(vk_key: int):
    """Send Ctrl+<key> combo using keybd_event (requires foreground focus)."""
    _user32.keybd_event(config.VK_CONTROL, 0, 0, 0)
    time.sleep(0.02)
    _user32.keybd_event(vk_key, 0, 0, 0)
    time.sleep(0.02)
    _user32.keybd_event(vk_key, 0, config.KEYEVENTF_KEYUP, 0)
    time.sleep(0.02)
    _user32.keybd_event(config.VK_CONTROL, 0, config.KEYEVENTF_KEYUP, 0)


# ---------------------------------------------------------------------------
# Clipboard helpers
# ---------------------------------------------------------------------------

def _read_clipboard_text(max_retries: int = None, interval_sec: float = None) -> str:
    """Read text from clipboard with retry logic."""
    if max_retries is None:
        max_retries = config.CLIPBOARD_MAX_RETRIES
    if interval_sec is None:
        interval_sec = config.CLIPBOARD_RETRY_INTERVAL_SEC

    for _ in range(max_retries):
        try:
            win32clipboard.OpenClipboard()
            try:
                data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                return data if data else ""
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(interval_sec)
    return ""


# ---------------------------------------------------------------------------
# Message sending
# ---------------------------------------------------------------------------

def send_message_to_room(room_name: str, text: str) -> Dict:
    """Send a text message to a KakaoTalk chat room.

    Pastes text via clipboard into RICHEDIT50W and sends with keybd_event Enter.
    NOTE: This briefly brings the chat window to the foreground.

    Returns:
        Dict with success (bool) and message or error.
    """
    hwnd = find_chat_window(room_name)
    if hwnd is None:
        return {"success": False, "error": f"Chat window '{room_name}' not found"}

    edit_hwnd = find_child_window_recursive(hwnd, config.KAKAO_EDIT_CLASS)
    if edit_hwnd is None:
        return {"success": False, "error": f"Edit control not found in '{room_name}'"}

    # Bring window to foreground and focus the edit control
    bring_window_to_front(hwnd)
    time.sleep(config.WINDOW_ACTIVATE_WAIT_SEC)

    # Click on the edit control to ensure focus
    try:
        rect = win32gui.GetWindowRect(edit_hwnd)
        cx = (rect[0] + rect[2]) // 2
        cy = (rect[1] + rect[3]) // 2
        _user32.SetCursorPos(cx, cy)
        _user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
        _user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
        time.sleep(config.EDIT_CLICK_WAIT_SEC)
    except Exception:
        pass

    # Paste text via clipboard (handles Korean correctly, unlike WM_SETTEXT on some versions)
    win32clipboard.OpenClipboard()
    win32clipboard.EmptyClipboard()
    win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
    win32clipboard.CloseClipboard()
    time.sleep(config.CLIPBOARD_PASTE_WAIT_SEC)
    _send_ctrl_key_combo(0x56)  # Ctrl+V
    time.sleep(config.AFTER_PASTE_WAIT_SEC)

    # Press Enter using keybd_event (not WM_KEYDOWN — WM_KEYDOWN inserts newline in RICHEDIT)
    _user32.keybd_event(config.VK_RETURN, 0, 0, 0)
    _user32.keybd_event(config.VK_RETURN, 0, config.KEYEVENTF_KEYUP, 0)

    return {"success": True, "message": f"Message sent to '{room_name}'"}


# ---------------------------------------------------------------------------
# Bulk message sending
# ---------------------------------------------------------------------------

def send_bulk_messages(room_names: List[str], message: str, interval_sec: float = 0.5) -> Dict:
    """Send the same message to multiple chat rooms sequentially.

    Opens each room (if not already open) and sends the message.
    Maintains a safe interval between rooms to avoid issues.

    Args:
        room_names: List of chat room names to send to.
        message: The text message to send.
        interval_sec: Seconds to wait between rooms (default 0.5, min 0.3).

    Returns:
        Dict with overall result and per-room details.
    """
    if not room_names:
        return {"success": False, "error": "No room names provided"}
    if not message.strip():
        return {"success": False, "error": "Message cannot be empty"}

    interval_sec = max(0.3, interval_sec)
    results = []

    for i, room_name in enumerate(room_names):
        # Check if window is already open
        hwnd = find_chat_window(room_name)
        actual_room_name = room_name

        if hwnd is None:
            # Need to search and open
            open_result = search_and_open_room(room_name)
            if not open_result["success"]:
                results.append({
                    "room": room_name,
                    "success": False,
                    "detail": open_result["error"],
                })
                continue
            # Extract actual room name from open result message
            actual_room_name = open_result.get("message", "").split("'")[1] if "'" in open_result.get("message", "") else room_name
            hwnd = open_result.get("hwnd")

        # Send message
        send_result = send_message_to_room(actual_room_name, message)
        results.append({
            "room": actual_room_name,
            "success": send_result["success"],
            "detail": send_result.get("message") or send_result.get("error"),
        })

        # Wait between rooms (skip after last)
        if i < len(room_names) - 1:
            time.sleep(interval_sec)

    sent_count = sum(1 for r in results if r["success"])
    return {
        "success": sent_count > 0,
        "message": f"Sent to {sent_count}/{len(room_names)} room(s)",
        "results": results,
    }


# ---------------------------------------------------------------------------
# Image sending
# ---------------------------------------------------------------------------

def _copy_image_to_clipboard(file_path: str):
    """Copy an image file to clipboard as bitmap data (CF_DIB).

    Converts the image to BMP via PowerShell/.NET, then sets CF_DIB
    directly from Python so that:
      1. The format matches what screenshot capture tools produce.
      2. The Python process owns the clipboard and stays alive,
         so no OleFlushClipboard() is needed.
      3. Windows auto-synthesizes CF_BITMAP and CF_DIBV5 from CF_DIB.
    """
    abs_path = os.path.abspath(file_path)
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(f"File not found: {abs_path}")

    # Convert image to BMP byte stream using PowerShell/.NET
    ps_path = abs_path.replace("'", "''")
    ps_script = (
        "Add-Type -AssemblyName System.Drawing;"
        f"$img = [System.Drawing.Image]::FromFile('{ps_path}');"
        "$ms = New-Object System.IO.MemoryStream;"
        "$img.Save($ms, [System.Drawing.Imaging.ImageFormat]::Bmp);"
        "$img.Dispose();"
        "$bytes = $ms.ToArray();"
        "$ms.Dispose();"
        "[Console]::OpenStandardOutput().Write($bytes, 0, $bytes.Length)"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        capture_output=True, timeout=15,
    )
    if result.returncode != 0:
        raise OSError(
            f"Failed to convert image to BMP: {result.stderr.decode().strip()}"
        )

    bmp_data = result.stdout
    if len(bmp_data) < 54:  # 14 (file header) + 40 (info header) minimum
        raise OSError("Image conversion produced invalid BMP data")

    # CF_DIB = BMP data minus the 14-byte BITMAPFILEHEADER
    dib_data = bmp_data[14:]

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_DIB, dib_data)
    finally:
        win32clipboard.CloseClipboard()


def send_image_to_room(room_name: str, image_path: str) -> Dict:
    """Send an image file to a KakaoTalk chat room.

    Copies the image to clipboard as bitmap data (CF_DIB), pastes via
    Ctrl+V into the chat window, and confirms the send dialog.
    NOTE: This briefly brings the chat window to the foreground.
    """
    abs_path = os.path.abspath(image_path)
    if not os.path.isfile(abs_path):
        return {"success": False, "error": f"Image file not found: {abs_path}"}

    ext = os.path.splitext(abs_path)[1].lower()
    if ext not in config.SUPPORTED_IMAGE_EXTENSIONS:
        return {
            "success": False,
            "error": f"Unsupported image format '{ext}'. Supported: {', '.join(sorted(config.SUPPORTED_IMAGE_EXTENSIONS))}",
        }

    hwnd = find_chat_window(room_name)
    if hwnd is None:
        return {"success": False, "error": f"Chat window '{room_name}' not found"}

    edit_hwnd = find_child_window_recursive(hwnd, config.KAKAO_EDIT_CLASS)
    if edit_hwnd is None:
        return {"success": False, "error": f"Edit control not found in '{room_name}'"}

    # Copy image to clipboard as bitmap (CF_DIB) BEFORE bringing window
    # to front — clipboard operations don't need foreground focus.
    try:
        _copy_image_to_clipboard(abs_path)
    except Exception as e:
        return {"success": False, "error": f"Failed to copy image to clipboard: {e}"}

    # Bring KakaoTalk to front
    bring_window_to_front(hwnd)
    time.sleep(config.IMAGE_FOCUS_WAIT_SEC)

    # Set keyboard focus to the RICHEDIT edit control using SetFocus.
    # We use AttachThreadInput so that SetFocus works cross-process.
    # Mouse click is avoided because it can accidentally hit images
    # displayed in the chat list area above the edit control.
    kernel32 = ctypes.windll.kernel32
    my_tid = kernel32.GetCurrentThreadId()
    target_tid = _user32.GetWindowThreadProcessId(hwnd, None)
    _user32.AttachThreadInput(my_tid, target_tid, True)
    _user32.SetFocus(edit_hwnd)
    _user32.AttachThreadInput(my_tid, target_tid, False)
    time.sleep(config.IMAGE_SET_FOCUS_WAIT_SEC)

    # Paste (Ctrl+V) — triggers KakaoTalk's image send confirmation dialog
    _send_ctrl_key_combo(0x56)  # Ctrl+V

    # Wait for the confirmation dialog to appear (it's a separate window).
    # Poll until foreground changes from the chat window or timeout.
    dialog_hwnd = None
    for _ in range(40):  # up to ~6 seconds
        time.sleep(config.IMAGE_DIALOG_POLL_INTERVAL_SEC)
        fg = _user32.GetForegroundWindow()
        if fg != hwnd and fg != 0:
            dialog_hwnd = fg
            break

    if dialog_hwnd is None:
        _log("Image send dialog did not appear")
        return {"success": False, "error": "Image send confirmation dialog did not appear"}

    # The dialog is already the foreground window.  Give it time to fully
    # render, then press Enter to confirm.
    time.sleep(config.IMAGE_CONFIRM_WAIT_SEC)
    _user32.keybd_event(config.VK_RETURN, 0, 0, 0)
    time.sleep(0.05)
    _user32.keybd_event(config.VK_RETURN, 0, config.KEYEVENTF_KEYUP, 0)
    time.sleep(config.IMAGE_AFTER_CONFIRM_WAIT_SEC)

    return {"success": True, "message": f"Image sent to '{room_name}': {os.path.basename(abs_path)}"}


def send_images_to_room(room_name: str, image_paths: List[str]) -> Dict:
    """Send multiple image files to a KakaoTalk chat room, one at a time."""
    if not image_paths:
        return {"success": False, "error": "No image paths provided"}

    results = []
    for i, path in enumerate(image_paths):
        result = send_image_to_room(room_name, path)
        results.append({
            "path": path,
            "success": result["success"],
            "detail": result.get("message") or result.get("error"),
        })
        if i < len(image_paths) - 1 and result["success"]:
            time.sleep(config.IMAGE_BETWEEN_SEND_WAIT_SEC)

    sent_count = sum(1 for r in results if r["success"])
    return {
        "success": sent_count > 0,
        "message": f"Sent {sent_count}/{len(image_paths)} image(s) to '{room_name}'",
        "results": results,
    }


# ---------------------------------------------------------------------------
# Message reading
# ---------------------------------------------------------------------------

def read_chat_messages(room_name: str) -> Dict:
    """Read messages from a KakaoTalk chat room via Ctrl+A → Ctrl+C.

    NOTE: This briefly brings the chat window to the foreground.

    Returns:
        Dict with success (bool), raw_text (str), and error if failed.
    """
    hwnd = find_chat_window(room_name)
    if hwnd is None:
        return {"success": False, "error": f"Chat window '{room_name}' not found", "raw_text": ""}

    list_hwnd = find_child_window_recursive(hwnd, config.KAKAO_LIST_CONTROL_CLASS)
    if list_hwnd is None:
        return {
            "success": False,
            "error": f"List control not found in '{room_name}'",
            "raw_text": "",
        }

    # Bring the chat window to foreground — required for keybd_event
    bring_window_to_front(hwnd)
    time.sleep(config.WINDOW_ACTIVATE_WAIT_SEC)

    # Click on the list control to ensure it has focus
    try:
        rect = win32gui.GetWindowRect(list_hwnd)
        cx = (rect[0] + rect[2]) // 2
        cy = (rect[1] + rect[3]) // 2
        _user32.SetCursorPos(cx, cy)
        _user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
        _user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
        time.sleep(config.EDIT_CLICK_WAIT_SEC)
    except Exception:
        pass

    # Ctrl+A (select all)
    _send_ctrl_key_combo(config.VK_A)
    time.sleep(config.KEY_COMBO_WAIT_SEC)

    # Ctrl+C (copy)
    _send_ctrl_key_combo(config.VK_C)
    time.sleep(config.KEY_COMBO_WAIT_SEC)

    # Read clipboard
    raw_text = _read_clipboard_text()

    return {"success": True, "raw_text": raw_text}


# ---------------------------------------------------------------------------
# Room search/open
# ---------------------------------------------------------------------------

def _find_chat_list_view(main_hwnd: int) -> Optional[int]:
    """Find the ChatRoomListView window inside the main window."""
    chat_list_view = None

    def _find_view(hwnd, _):
        nonlocal chat_list_view
        cls = win32gui.GetClassName(hwnd)
        text = win32gui.GetWindowText(hwnd)
        if cls == "EVA_Window" and "ChatRoomListView" in text:
            chat_list_view = hwnd
            return False
        return True

    try:
        win32gui.EnumChildWindows(main_hwnd, _find_view, None)
    except Exception:
        pass
    return chat_list_view


def _find_search_strip(chat_list_view: int) -> Optional[int]:
    """Find the search strip (EVA_Window_Dblclk, ~390x40) inside ChatRoomListView.

    This strip contains the magnifying glass icon. Clicking it activates the search
    Edit control (which starts out hidden/invisible).
    """
    result = None

    def _cb(hwnd, _):
        nonlocal result
        if win32gui.GetParent(hwnd) != chat_list_view:
            return True
        cls = win32gui.GetClassName(hwnd)
        if cls == "EVA_Window_Dblclk":
            try:
                r = win32gui.GetWindowRect(hwnd)
                w, h = r[2] - r[0], r[3] - r[1]
                if w > 200 and 25 < h < 60:
                    result = hwnd
                    return False
            except Exception:
                pass
        return True

    try:
        win32gui.EnumChildWindows(chat_list_view, _cb, None)
    except Exception:
        pass
    return result


def _activate_search_and_get_edit(main_hwnd: int) -> Optional[int]:
    """Activate the chat search bar using Ctrl+F and return the Edit hwnd.

    KakaoTalk PC uses Ctrl+F to open the search bar in the chat list view.
    After Ctrl+F, the Edit control becomes visible and focused.
    """
    chat_list_view = _find_chat_list_view(main_hwnd)
    _log(f"ChatRoomListView hwnd: {chat_list_view}")
    if chat_list_view is None:
        return None

    # Press Ctrl+F to activate search
    _send_ctrl_key_combo(0x46)  # 0x46 = 'F'
    time.sleep(config.SEARCH_ACTIVATE_WAIT_SEC)

    # Find the Edit control — should now be visible and focused
    edit_hwnd = find_child_window_recursive(chat_list_view, "Edit")
    if edit_hwnd:
        vis = win32gui.IsWindowVisible(edit_hwnd)
        _log(f"Edit hwnd after Ctrl+F: {edit_hwnd}, visible: {vis}")
    else:
        _log("Edit not found after Ctrl+F")
    return edit_hwnd


def _ensure_foreground(hwnd: int) -> bool:
    """Ensure a window is in the foreground. Returns True if successful."""
    bring_window_to_front(hwnd)
    time.sleep(config.WINDOW_ACTIVATE_WAIT_SEC)
    fg = _user32.GetForegroundWindow()
    return fg == hwnd


def search_and_open_room(room_name: str) -> Dict:
    """Search for a chat room in KakaoTalk main window and open it.

    Uses clipboard paste into the search Edit box (for Korean IME support),
    then double-clicks the first search result in SearchListCtrl.

    Returns:
        Dict with success (bool) and message or error.
    """
    main_hwnd = win32gui.FindWindow(
        config.KAKAO_MAIN_WINDOW_CLASS, config.KAKAO_MAIN_WINDOW_TITLE
    )
    if main_hwnd == 0:
        return {"success": False, "error": "KakaoTalk main window not found"}

    # Ensure KakaoTalk is in the foreground before sending keyboard events
    if not _ensure_foreground(main_hwnd):
        _log("Warning: Could not bring KakaoTalk to foreground")

    # Ctrl+F activates the search bar (Edit becomes visible and focused)
    edit_hwnd = _activate_search_and_get_edit(main_hwnd)
    if edit_hwnd is None:
        return {"success": False, "error": "Search box not found in KakaoTalk main window"}

    # Clear any existing text in the Edit using EM_SETSEL + WM_CLEAR
    EM_SETSEL = 0x00B1
    WM_CLEAR = 0x0303
    win32api.SendMessage(edit_hwnd, EM_SETSEL, 0, -1)  # Select all
    win32api.SendMessage(edit_hwnd, WM_CLEAR, 0, 0)     # Delete selected
    time.sleep(config.EDIT_CLICK_WAIT_SEC)

    # Type search text character by character using WM_CHAR
    # This goes directly to the Edit control — no focus or clipboard needed
    for ch in room_name:
        win32api.SendMessage(edit_hwnd, config.WM_CHAR, ord(ch), 0)
        time.sleep(config.SEARCH_CHAR_INTERVAL_SEC)
    _log(f"Typed '{room_name}' into Edit via WM_CHAR")
    time.sleep(config.SEARCH_RESULTS_WAIT_SEC)  # Wait for search results to populate

    # Press Enter to open the first search result (already selected by default)
    _log("Pressing Enter to open first search result")
    _user32.keybd_event(config.VK_RETURN, 0, 0, 0)
    _user32.keybd_event(config.VK_RETURN, 0, config.KEYEVENTF_KEYUP, 0)
    time.sleep(config.SEARCH_OPEN_WAIT_SEC)

    # Do NOT press Escape here — it would close the newly opened chat window

    # Look for opened chat windows
    all_windows = list_chat_windows()
    _log(f"Open windows after search: {[w['title'] for w in all_windows]}")
    for w in all_windows:
        if w["title"] == room_name:
            return {"success": True, "message": f"Opened chat room '{room_name}'", "hwnd": w["hwnd"]}
    for w in all_windows:
        if room_name in w["title"]:
            return {
                "success": True,
                "message": f"Opened chat room '{w['title']}' (searched: '{room_name}')",
                "hwnd": w["hwnd"],
            }
    if all_windows:
        return {
            "success": True,
            "message": f"Opened a chat window (title: '{all_windows[0]['title']}')",
            "hwnd": all_windows[0]["hwnd"],
        }

    return {
        "success": False,
        "error": f"Chat room '{room_name}' not found after search. "
                 "The exact room name may differ from search results.",
    }


def _find_visible_search_list(main_hwnd: int) -> Optional[int]:
    """Find the visible SearchListCtrl in the main window."""
    result = None

    def _cb(hwnd, _):
        nonlocal result
        cls = win32gui.GetClassName(hwnd)
        text = win32gui.GetWindowText(hwnd)
        if cls == "EVA_VH_ListControl_Dblclk" and "SearchListCtrl" in text:
            r = win32gui.GetWindowRect(hwnd)
            w = r[2] - r[0]
            h = r[3] - r[1]
            if w > 100 and h > 100:
                result = hwnd
                return False
        return True

    try:
        win32gui.EnumChildWindows(main_hwnd, _cb, None)
    except Exception:
        pass
    return result


# ---------------------------------------------------------------------------
# Image download (cache-based)
# ---------------------------------------------------------------------------

def get_kakao_user_hash_dir() -> Optional[str]:
    """Find the first user hash directory under KakaoTalk users dir.

    KakaoTalk stores per-user data in a directory named with a SHA1 hash.
    Returns the full path or None.
    """
    users_dir = config.KAKAO_USERS_DIR
    if not os.path.isdir(users_dir):
        return None
    for entry in os.listdir(users_dir):
        full = os.path.join(users_dir, entry)
        if os.path.isdir(full) and len(entry) == 40:
            return full
    return None


def download_recent_images(
    room_name: str,
    output_dir: str,
    max_images: int = 10,
) -> Dict:
    """Download recent images from KakaoTalk cache.

    Collects image files from cache subdirectories sorted by modification time
    (newest first) and copies them to the output directory.

    Args:
        room_name: Chat room name (used for logging; cache is not per-room).
        output_dir: Directory to save images to.
        max_images: Max number of images to copy.

    Returns:
        Dict with message, images list, or error.
    """
    user_dir = get_kakao_user_hash_dir()
    if user_dir is None:
        return {"error": "KakaoTalk user data directory not found"}

    # Collect all image files from cache directories
    image_files: List[Dict] = []
    for subdir in config.IMAGE_CACHE_SUBDIRS:
        cache_path = os.path.join(user_dir, subdir)
        if not os.path.isdir(cache_path):
            continue
        for fname in os.listdir(cache_path):
            full_path = os.path.join(cache_path, fname)
            if not os.path.isfile(full_path):
                continue
            try:
                stat = os.stat(full_path)
                # Skip tiny files (< 1KB, likely not real images)
                if stat.st_size < 1024:
                    continue
                image_files.append({
                    "path": full_path,
                    "name": fname,
                    "mtime": stat.st_mtime,
                    "size": stat.st_size,
                })
            except OSError:
                continue

    if not image_files:
        return {
            "message": f"No cached images found for room '{room_name}'",
            "images": [],
        }

    # Sort by modification time (newest first)
    image_files.sort(key=lambda x: x["mtime"], reverse=True)
    image_files = image_files[:max_images]

    # Copy to output directory
    os.makedirs(output_dir, exist_ok=True)
    copied: List[Dict] = []
    for img in image_files:
        dest_name = img["name"]
        if not os.path.splitext(dest_name)[1]:
            dest_name += ".jpg"
        dest = os.path.join(output_dir, dest_name)
        try:
            shutil.copy2(img["path"], dest)
            copied.append({
                "source": img["path"],
                "destination": dest,
                "size": img["size"],
            })
        except OSError:
            continue

    return {
        "message": f"Downloaded {len(copied)} image(s) to {output_dir}",
        "images": copied,
    }


# ---------------------------------------------------------------------------
# Korean 2벌식 keyboard mapping (for @mention input)
# ---------------------------------------------------------------------------

# Map jamo → (VK code, needs_shift)
_KOREAN_KEY_MAP = {
    # Consonants (초성/종성)
    'ㅂ': (0x51, False), 'ㅈ': (0x57, False), 'ㄷ': (0x45, False),
    'ㄱ': (0x52, False), 'ㅅ': (0x54, False), 'ㅛ': (0x59, False),
    'ㅕ': (0x55, False), 'ㅑ': (0x49, False), 'ㅐ': (0x4F, False),
    'ㅔ': (0x50, False), 'ㅁ': (0x41, False), 'ㄴ': (0x53, False),
    'ㅇ': (0x44, False), 'ㄹ': (0x46, False), 'ㅎ': (0x47, False),
    'ㅗ': (0x48, False), 'ㅓ': (0x4A, False), 'ㅏ': (0x4B, False),
    'ㅣ': (0x4C, False), 'ㅋ': (0x5A, False), 'ㅌ': (0x58, False),
    'ㅊ': (0x43, False), 'ㅍ': (0x56, False), 'ㅠ': (0x42, False),
    'ㅜ': (0x4E, False), 'ㅡ': (0x4D, False),
    # Shift consonants (쌍자음)
    'ㅆ': (0x54, True), 'ㄲ': (0x52, True), 'ㄸ': (0x45, True),
    'ㅃ': (0x51, True), 'ㅉ': (0x57, True),
    # Shift vowels
    'ㅒ': (0x4F, True), 'ㅖ': (0x50, True),
}

_INITIALS = list('ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ')
_MEDIALS = list('ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ')
_FINALS = [''] + list('ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ')

_COMPOUND_MEDIALS = {
    'ㅘ': ['ㅗ', 'ㅏ'], 'ㅙ': ['ㅗ', 'ㅐ'], 'ㅚ': ['ㅗ', 'ㅣ'],
    'ㅝ': ['ㅜ', 'ㅓ'], 'ㅞ': ['ㅜ', 'ㅔ'], 'ㅟ': ['ㅜ', 'ㅣ'],
    'ㅢ': ['ㅡ', 'ㅣ'],
}

_COMPOUND_FINALS = {
    'ㄳ': ['ㄱ', 'ㅅ'], 'ㄵ': ['ㄴ', 'ㅈ'], 'ㄶ': ['ㄴ', 'ㅎ'],
    'ㄺ': ['ㄹ', 'ㄱ'], 'ㄻ': ['ㄹ', 'ㅁ'], 'ㄼ': ['ㄹ', 'ㅂ'],
    'ㄽ': ['ㄹ', 'ㅅ'], 'ㄾ': ['ㄹ', 'ㅌ'], 'ㄿ': ['ㄹ', 'ㅍ'],
    'ㅀ': ['ㄹ', 'ㅎ'], 'ㅄ': ['ㅂ', 'ㅅ'],
}


def _decompose_korean(text: str) -> List[tuple]:
    """Decompose Korean text into a sequence of (VK_code, shift) keypresses.

    Handles Hangul syllables (decomposed into jamo via 2벌식 mapping),
    spaces, and skips non-Hangul characters.
    """
    keys: List[tuple] = []
    for ch in text:
        if ch == ' ':
            keys.append((0x20, False))  # VK_SPACE
            continue

        code = ord(ch) - 0xAC00
        if code < 0 or code > 11171:
            # Non-Hangul character — skip
            continue

        initial = code // (21 * 28)
        medial = (code % (21 * 28)) // 28
        final = code % 28

        # Initial consonant
        ini = _INITIALS[initial]
        if ini in _KOREAN_KEY_MAP:
            keys.append(_KOREAN_KEY_MAP[ini])

        # Medial vowel (may be compound)
        med = _MEDIALS[medial]
        if med in _COMPOUND_MEDIALS:
            for m in _COMPOUND_MEDIALS[med]:
                keys.append(_KOREAN_KEY_MAP[m])
        elif med in _KOREAN_KEY_MAP:
            keys.append(_KOREAN_KEY_MAP[med])

        # Final consonant (may be compound or empty)
        if final > 0:
            fin = _FINALS[final]
            if fin in _COMPOUND_FINALS:
                for f in _COMPOUND_FINALS[fin]:
                    keys.append(_KOREAN_KEY_MAP[f])
            elif fin in _KOREAN_KEY_MAP:
                keys.append(_KOREAN_KEY_MAP[fin])

    return keys


# ---------------------------------------------------------------------------
# Mention message sending
# ---------------------------------------------------------------------------

def send_mention_message(room_name: str, mention_name: str, message: str) -> Dict:
    """Send a message with @mention to a KakaoTalk chat room.

    Uses keybd_event to type '@' (Shift+2) which activates the mention popup,
    then types the name using Korean 2벌식 keyboard simulation, selects the
    mention with Enter, and pastes the message text via clipboard.

    NOTE: This briefly brings the chat window to the foreground.

    Args:
        room_name: Exact title of the chat room window.
        mention_name: Display name of the person to mention.
        message: Text message to send after the mention.

    Returns:
        Dict with success (bool) and message or error.
    """
    hwnd = find_chat_window(room_name)
    if hwnd is None:
        return {"success": False, "error": f"Chat window '{room_name}' not found"}

    edit_hwnd = find_child_window_recursive(hwnd, config.KAKAO_EDIT_CLASS)
    if edit_hwnd is None:
        return {"success": False, "error": f"Edit control not found in '{room_name}'"}

    # Clear the edit control
    EM_SETSEL = 0x00B1
    WM_CLEAR = 0x0303
    win32api.SendMessage(edit_hwnd, EM_SETSEL, 0, -1)
    win32api.SendMessage(edit_hwnd, WM_CLEAR, 0, 0)

    # Bring window to foreground and click on edit control for focus
    bring_window_to_front(hwnd)
    time.sleep(config.MENTION_FOCUS_WAIT_SEC)
    try:
        rect = win32gui.GetWindowRect(edit_hwnd)
        cx = (rect[0] + rect[2]) // 2
        cy = (rect[1] + rect[3]) // 2
        _user32.SetCursorPos(cx, cy)
        _user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
        _user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
        time.sleep(config.MENTION_CLICK_WAIT_SEC)
    except Exception:
        pass

    # Type '@' using Shift+2 (keybd_event required to activate mention popup)
    _press_key(0x32, shift=True)
    time.sleep(config.MENTION_AT_WAIT_SEC)

    # Type mention name using Korean 2벌식 keyboard simulation
    keys = _decompose_korean(mention_name)
    for vk, shift in keys:
        _press_key(vk, shift=shift)
    time.sleep(config.MENTION_NAME_WAIT_SEC)

    # Press Enter to select the mention from the popup
    _press_key(config.VK_RETURN)
    time.sleep(config.MENTION_SELECT_WAIT_SEC)

    # Paste message text via clipboard (space prefix to separate from mention)
    win32clipboard.OpenClipboard()
    win32clipboard.EmptyClipboard()
    win32clipboard.SetClipboardText(' ' + message, win32clipboard.CF_UNICODETEXT)
    win32clipboard.CloseClipboard()
    time.sleep(config.CLIPBOARD_PASTE_WAIT_SEC)
    _send_ctrl_key_combo(0x56)  # Ctrl+V
    time.sleep(config.MENTION_PASTE_WAIT_SEC)

    # Press Enter to send the message
    _press_key(config.VK_RETURN)
    time.sleep(config.MENTION_SEND_WAIT_SEC)

    return {
        "success": True,
        "message": f"Mention message sent to @{mention_name} in '{room_name}'",
    }


# ---------------------------------------------------------------------------
# Chat room monitoring (background thread)
# ---------------------------------------------------------------------------

class ChatMonitor:
    """Background chat room monitor with keyword detection.

    Polls a single chat room at a configurable interval, detects new messages
    via hash-based diffing, and queues events when keywords are matched.
    """

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._events: deque = deque(maxlen=100)
        self._seen_hashes: set = set()
        self._room_name: str = ""
        self._keywords: List[str] = []
        self._poll_interval: float = 5.0
        self._running: bool = False

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def start(self, room_name: str, keywords: List[str], poll_interval: float = 5.0) -> Dict:
        """Start monitoring a chat room for keywords."""
        if self.is_running:
            return {"success": False, "error": "Monitor already running"}

        self._room_name = room_name
        self._keywords = [kw.lower() for kw in keywords]
        self._poll_interval = max(3.0, poll_interval)
        self._stop_event.clear()
        self._events.clear()
        self._seen_hashes.clear()
        self._running = True

        # Load existing messages so they don't trigger events
        self._load_initial_messages()

        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

        return {
            "success": True,
            "message": f"Monitoring '{room_name}' for keywords: {keywords} (interval: {self._poll_interval}s)",
        }

    def stop(self) -> Dict:
        """Stop the monitoring thread."""
        if not self.is_running:
            return {"success": False, "error": "Monitor not running"}
        self._stop_event.set()
        self._thread.join(timeout=10)
        self._running = False
        return {"success": True, "message": "Monitor stopped"}

    def get_events(self) -> List[Dict]:
        """Return and clear all pending keyword-match events."""
        events = list(self._events)
        self._events.clear()
        return events

    def _msg_hash(self, msg: Dict) -> str:
        """Create a hash to uniquely identify a message."""
        key = f"{msg.get('sender', '')}|{msg.get('time', '')}|{msg.get('text', '')[:80]}"
        return hashlib.md5(key.encode()).hexdigest()

    def _load_initial_messages(self):
        """Read current messages and store their hashes (baseline)."""
        try:
            result = read_chat_messages(self._room_name)
            if result["success"]:
                from . import parser
                parsed = parser.parse_chat_text(result["raw_text"])
                for msg in parsed["messages"]:
                    self._seen_hashes.add(self._msg_hash(msg))
                _log(f"Monitor baseline: {len(self._seen_hashes)} existing messages")
        except Exception as e:
            _log(f"Monitor baseline error: {e}")

    def _monitor_loop(self):
        """Background polling loop."""
        _log(f"Monitor started: room='{self._room_name}', keywords={self._keywords}")
        while not self._stop_event.is_set():
            self._stop_event.wait(self._poll_interval)
            if self._stop_event.is_set():
                break
            try:
                self._check_for_new_messages()
            except Exception as e:
                _log(f"Monitor poll error: {e}")
        _log("Monitor stopped")

    def _check_for_new_messages(self):
        """Poll for new messages and check for keyword matches."""
        result = read_chat_messages(self._room_name)
        if not result["success"]:
            return

        from . import parser
        parsed = parser.parse_chat_text(result["raw_text"])
        all_messages = parsed["messages"]

        new_messages = []
        for msg in all_messages:
            h = self._msg_hash(msg)
            if h not in self._seen_hashes:
                self._seen_hashes.add(h)
                new_messages.append(msg)

        if not new_messages:
            return

        _log(f"Monitor: {len(new_messages)} new message(s)")

        for msg in new_messages:
            text_lower = msg.get("text", "").lower()
            for kw in self._keywords:
                if kw in text_lower:
                    context_start = max(0, len(all_messages) - 10)
                    self._events.append({
                        "keyword": kw,
                        "trigger_message": msg,
                        "recent_context": all_messages[context_start:],
                        "room_name": self._room_name,
                    })
                    _log(f"Monitor: keyword '{kw}' matched in message from {msg.get('sender')}")
                    break  # One event per message


# Module-level singleton
_chat_monitor = ChatMonitor()
