"""Fresh-user CLI contracts; only disposable local configuration and loopback HTTP."""

import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request


class Configure(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "private installation 'with spaces'"

    def configure(self, *arguments):
        return subprocess.run([sys.executable, "configure.py", "--directory", str(self.root), *arguments],
                              capture_output=True, text=True, timeout=10)

    def test_new_install_is_private_send_disabled_and_runs(self):
        result = self.configure("--public-origin", "https://owner.example:443")
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in (self.root, self.root / "credentials", self.root / "state"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        tokens = [(self.root / "credentials" / name).read_text().strip()
                  for name in ("mcp-token", "windows-agent-token")]
        self.assertNotEqual(*tokens)
        for token in tokens:
            self.assertGreaterEqual(len(token), 32)
            self.assertNotIn(token, result.stdout + result.stderr)
            self.assertNotIn(token, (self.root / "run.sh").read_text())
        for path in (self.root / "credentials").iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        config = json.loads((self.root / "config.json").read_text())
        self.assertEqual(config["kakao_rooms"], [])
        self.assertFalse(config["kakao_send_enabled"])
        self.assertFalse(config["event_intake_enabled"])
        process = subprocess.Popen([str(self.root / "run.sh")], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            base = "http://127.0.0.1:8001"
            for attempt in range(80):
                if process.poll() is not None:
                    self.fail("Generated launcher exited: " + process.communicate()[1].decode())
                try:
                    with urllib.request.urlopen(base + "/healthz", timeout=1) as response:
                        self.assertEqual(json.load(response)["status"], "ok")
                    break
                except urllib.error.URLError:
                    time.sleep(0.05)
            else:
                self.fail("Generated launcher did not become healthy")
            with self.assertRaises(urllib.error.HTTPError) as unauthorized:
                urllib.request.urlopen(base + "/mcp", timeout=2)
            self.assertEqual(unauthorized.exception.code, 401)
            unauthorized.exception.close()
            request = urllib.request.Request(base + "/mcp", data=json.dumps({"jsonrpc": "2.0", "id": 1,
                "method": "tools/call", "params": {"name": "kakao_status", "arguments": {}}}).encode(),
                headers={"Authorization": "Bearer " + tokens[0], "Content-Type": "application/json",
                         "Host": "owner.example", "Origin": "https://owner.example",
                         "Accept": "application/json, text/event-stream"})
            with urllib.request.urlopen(request, timeout=3) as response:
                result = json.load(response)["result"]
            self.assertFalse(result.get("isError"), result)
            status = result.get("structuredContent") or json.loads(result["content"][0]["text"])
            self.assertFalse(status["send_enabled"])
            self.assertEqual(status["rooms"], [])
        finally:
            process.terminate()
            process.communicate(timeout=5)

    def test_existing_directory_and_bad_origins_never_modify_files(self):
        self.root.mkdir()
        sentinel = self.root / "credentials"
        sentinel.write_text("operator data")
        self.assertNotEqual(self.configure().returncode, 0)
        self.assertEqual(sentinel.read_text(), "operator data")
        self.assertEqual(list(self.root.iterdir()), [sentinel])
        self.root = self.root.parent / "not-created"
        for origin in ("http://owner.example", "https://owner.example/path", "https://user@owner.example",
                       "https://owner.example:8443", "https://owner.example?token=x", "https://owner.example#x",
                       "https://@owner.example", "https://-invalid.example", "https://owner..example"):
            with self.subTest(origin=origin):
                self.assertNotEqual(self.configure("--public-origin", origin).returncode, 0)
                self.assertFalse(self.root.exists())
        private_parent = self.root.parent
        self.root = Path.cwd().parent / "scratch" / ".." / Path.cwd().name / "private-config"
        rejection = self.configure()
        self.assertNotEqual(rejection.returncode, 0)
        self.assertIn("outside the source checkout", rejection.stderr)
        self.root = private_parent / "configure-alias"
        self.root.symlink_to(Path.cwd(), target_is_directory=True)
        self.addCleanup(self.root.unlink)
        self.assertNotEqual(self.configure().returncode, 0)


if __name__ == "__main__":
    unittest.main()
