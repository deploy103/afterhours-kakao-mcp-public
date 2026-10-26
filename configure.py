"""Create a fresh private, send-disabled local installation; never modify an existing one."""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, type=Path, help="New private directory outside the checkout")
    parser.add_argument("--public-origin", default="https://mcp.example.com")
    args = parser.parse_args()
    try:
        origin = urlsplit(args.public_origin)
        valid = bool(re.fullmatch(r"https://(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(?::443)?", args.public_origin))
        if valid and ":" in origin.hostname:
            ipaddress.IPv6Address(origin.hostname)
        elif valid:
            valid = len(origin.hostname) <= 253 and all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in origin.hostname.split("."))
    except ValueError:
        valid = False
    if not valid:
        parser.error("Use an HTTPS origin on port 443, without a path, credentials, query or fragment")
    args.public_origin = args.public_origin.lower()
    origin = urlsplit(args.public_origin)
    repo = Path(__file__).resolve().parent
    root = Path(os.path.abspath(args.directory.expanduser()))
    if root == repo or repo in root.parents:
        parser.error("Keep private configuration outside the source checkout")
    if root.resolve() != root:
        parser.error("The installation parent must not be a symlink")
    os.umask(0o077)
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        parser.error("Directory already exists; no files changed. Use a new directory or review an upgrade manually")
    except OSError:
        parser.error("Cannot create directory; ensure its parent exists and is writable")
    credentials = root / "credentials"
    state = root / "state"
    credentials.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    for name in ("mcp-token", "windows-agent-token"):
        (credentials / name).write_text(secrets.token_urlsafe(48) + "\n")
    config = root / "config.json"
    config.write_text(json.dumps({"kakao_rooms": [], "kakao_send_enabled": False,
                                 "kakao_owner_id": "", "event_intake_enabled": False}, indent=2) + "\n")
    host = f"[{origin.hostname}]" if ":" in origin.hostname else origin.hostname
    env = {"CREDENTIALS_DIRECTORY": str(credentials), "STATE_DIRECTORY": str(state),
           "KAKAO_CONFIG": str(config),
           "KAKAO_ALLOWED_HOSTS": f"{host},{host}:443,127.0.0.1:8001",
           "KAKAO_ALLOWED_ORIGINS": f"https://{host},https://{host}:443"}
    command = ["env", *(f"{key}={value}" for key, value in env.items()),
               str(repo / ".venv/bin/python"), "-m", "uvicorn", "server:create_app", "--factory",
               "--host", "127.0.0.1", "--port", "8001", "--workers", "1",
               "--no-access-log", "--no-proxy-headers"]
    launcher = root / "run.sh"
    launcher.write_text("#!/bin/sh\nset -eu\numask 077\ncd " + shlex.quote(str(repo))
                        + "\nexec " + shlex.join(command) + "\n")
    launcher.chmod(0o700)
    print(f"Created private installation: {root}\nRun: {launcher}\n"
          f"MCP token file: {credentials / 'mcp-token'}\n"
          "Provider not connected. Rooms empty; sending and event intake disabled.")


if __name__ == "__main__":
    main()
