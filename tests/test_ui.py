import http.server
import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agents import config, ui
from agents.board import Board

SLEEP = [sys.executable, "-c", "import time; time.sleep(30)"]


class UiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        config.scaffold(self.root, project_name="demo")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            ui.stop(self.root)
        finally:
            self.tmp.cleanup()

    def test_build_payload(self):
        Board(self.root).create("A")
        payload = ui.build_board_payload(self.root)
        self.assertEqual(payload["columns"][0], "not_started")
        self.assertEqual(len(payload["cards"]), 1)
        self.assertIn("runner", payload)
        self.assertIn("waves", payload)

    def test_http_endpoints(self):
        Board(self.root).create("A")
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ui._handler_for(self.root))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_address[1]
        try:
            index = urllib.request.urlopen(f"http://127.0.0.1:{port}/").read().decode()
            self.assertIn("agent-board", index)
            data = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/board").read())
            self.assertEqual(len(data["cards"]), 1)
            log = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/log/T-001").read())
            self.assertEqual(log["id"], "T-001")
        finally:
            server.shutdown()
            server.server_close()

    def test_detach_and_stop(self):
        ui.start_detached(self.root, command=SLEEP)
        self.assertTrue(ui.is_running(self.root))
        self.assertIsNotNone(ui.read_pid(self.root))
        self.assertTrue(ui.stop(self.root))
        self.assertFalse(ui.is_running(self.root))

    def test_stop_without_ui(self):
        self.assertFalse(ui.stop(self.root))


if __name__ == "__main__":
    unittest.main()
