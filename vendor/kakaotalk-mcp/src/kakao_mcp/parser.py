"""Parse KakaoTalk clipboard text into structured messages."""
import re
from typing import List, Dict


# Regex: [Sender] [오전/오후 H:MM] Text
MESSAGE_PATTERN = re.compile(
    r'^\[(.+?)\]\s*\[(오전|오후)\s*(\d{1,2}:\d{2})\]\s*(.*)',
)

# Date separator:
#   "--------------- 2025년 2월 27일 목요일 ---------------"  (with dashes, from full export)
#   "2026년 2월 28일 토요일"  (bare date, from Ctrl+A clipboard copy)
DATE_SEPARATOR_PATTERN = re.compile(
    r'^-*\s*(\d{4}년\s*\d{1,2}월\s*\d{1,2}일\s*\S+요일)\s*-*$',
    re.MULTILINE,
)

# Chat header: [RoomName] [대화상대 N명] or [RoomName] [대화상대 N]
HEADER_PATTERN = re.compile(
    r'^\[(.+?)\]\s*\[대화상대\s*(\d+)',
)

# URL extraction
URL_PATTERN = re.compile(
    r'https?://[^\s<>"\')\]]+',
    re.IGNORECASE,
)


def parse_chat_text(raw_text: str) -> Dict:
    """Parse raw clipboard text from KakaoTalk chat into structured data.

    Returns:
        Dict with keys:
            - room_name: str or None
            - member_count: int or None
            - messages: List[Dict] each with sender, time, text, is_photo, is_video, is_file, urls
            - dates: List[str] date separators found
    """
    result: Dict = {
        "room_name": None,
        "member_count": None,
        "messages": [],
        "dates": [],
    }

    if not raw_text or not raw_text.strip():
        return result

    # Extract header
    header_match = HEADER_PATTERN.search(raw_text)
    if header_match:
        result["room_name"] = header_match.group(1)
        result["member_count"] = int(header_match.group(2))

    # Extract date separators
    for date_match in DATE_SEPARATOR_PATTERN.finditer(raw_text):
        result["dates"].append(date_match.group(1))

    # Extract messages
    lines = raw_text.split('\n')
    current_message = None

    for line in lines:
        stripped = line.strip()

        # Skip empty lines and date separators
        if not stripped or DATE_SEPARATOR_PATTERN.match(stripped):
            continue

        # Skip header line
        if HEADER_PATTERN.match(stripped):
            continue

        msg_match = MESSAGE_PATTERN.match(stripped)
        if msg_match:
            # Finalize previous message
            if current_message is not None:
                _finalize_message(current_message, result["messages"])

            current_message = {
                "sender": msg_match.group(1),
                "time": f"{msg_match.group(2)} {msg_match.group(3)}",
                "text": msg_match.group(4),
            }
        elif current_message is not None and stripped:
            # Continuation line of previous message
            current_message["text"] += "\n" + stripped

    # Finalize last message
    if current_message is not None:
        _finalize_message(current_message, result["messages"])

    return result


def _finalize_message(msg: Dict, messages_list: List[Dict]):
    """Add derived fields (is_photo, urls, etc.) and append to list."""
    text = msg["text"].strip()
    msg["is_photo"] = text == "사진"
    msg["is_video"] = text == "동영상"
    msg["is_file"] = text.startswith("파일:")
    msg["urls"] = URL_PATTERN.findall(msg["text"])
    messages_list.append(msg)


def extract_urls_from_messages(messages: List[Dict]) -> List[Dict]:
    """Extract all URLs from parsed messages.

    Returns:
        List of dicts with url, sender, time.
    """
    urls = []
    for msg in messages:
        for url in msg.get("urls", []):
            urls.append({
                "url": url,
                "sender": msg["sender"],
                "time": msg["time"],
            })
    return urls
