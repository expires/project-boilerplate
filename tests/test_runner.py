import sys
import tempfile
import unittest
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=ResourceWarning)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agents import runner
from agents.board import BoardError

SLEEP = [sys.executable, "-c", "import time; time.sleep(30)"]


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".agents").mkdir()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            runner.stop(self.root)
        finally:
            self.tmp.cleanup()

    def test_start_stop_cycle(self):
        pid = runner.start_detached(self.root, command=SLEEP)
        self.assertTrue(runner.is_running(self.root))
        self.assertEqual(runner.read_pid(self.root), pid)
        state = runner.runner_state(self.root)
        self.assertTrue(state["running"])
        self.assertEqual(state["pid"], pid)
        self.assertTrue(runner.stop(self.root))
        self.assertFalse(runner.is_running(self.root))
        self.assertIsNone(runner.read_pid(self.root))

    def test_double_start_refuses(self):
        runner.start_detached(self.root, command=SLEEP)
        with self.assertRaises(BoardError):
            runner.start_detached(self.root, command=[sys.executable, "-c", "pass"])

    def test_stale_pid_reclaimed(self):
        (self.root / ".agents" / "runner.pid").write_text("999999")
        self.assertFalse(runner.is_running(self.root))
        pid = runner.start_detached(self.root, command=SLEEP)
        self.assertNotEqual(pid, 999999)
        self.assertTrue(runner.is_running(self.root))

    def test_stop_without_runner(self):
        self.assertFalse(runner.stop(self.root))


if __name__ == "__main__":
    unittest.main()
