import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agents import config, vcs
from agents.board import Board
from agents.cli import main
from agents.skill import install_skill


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        config.scaffold(self.root, project_name="demo")
        vcs.ensure_repo(self.root, "main")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            saved = sys.stdin
            sys.stdin = io.StringIO(stdin)
            try:
                code = main(["--root", str(self.root), *args])
            finally:
                sys.stdin = saved
        return code, out.getvalue(), err.getvalue()

    def test_add_creates_card(self):
        code, out, _ = self.run_cli(
            "add", "Add hello.py", "--file", "hello.py", "--criterion", "greet works", "--priority", "2"
        )
        self.assertEqual(code, 0)
        card = Board(self.root).find(out.strip())
        self.assertEqual(card.title, "Add hello.py")
        self.assertEqual(card.files_hint, ["hello.py"])
        self.assertEqual(card.acceptance_criteria, ["greet works"])
        self.assertEqual(card.priority, 2)

    def test_add_unknown_dependency_fails(self):
        code, _, err = self.run_cli("add", "Child", "--depends-on", "T-999")
        self.assertEqual(code, 1)
        self.assertIn("not found", err)

    def test_plan_maps_keys_and_dependencies(self):
        payload = {
            "tasks": [
                {"key": "1", "title": "First", "files": ["a.py"], "acceptance_criteria": ["a"]},
                {
                    "key": "2",
                    "title": "Second",
                    "files": ["b.py"],
                    "depends_on": ["1"],
                    "priority": 5,
                    "spec": "do b",
                },
            ]
        }
        code, out, _ = self.run_cli("plan", "--stdin", stdin=json.dumps(payload))
        self.assertEqual(code, 0)
        created = json.loads(out)["created"]
        self.assertEqual(created, ["T-001", "T-002"])
        board = Board(self.root)
        self.assertEqual(board.find("T-002").depends_on, ["T-001"])
        self.assertEqual(board.find("T-002").spec, "do b")

    def test_plan_depends_on_existing_card(self):
        _, out, _ = self.run_cli("add", "Base")
        base_id = out.strip()
        payload = {"tasks": [{"key": "1", "title": "Child", "files": ["c.py"], "depends_on": [base_id]}]}
        code, out, _ = self.run_cli("plan", "--stdin", stdin=json.dumps(payload))
        self.assertEqual(code, 0)
        child_id = json.loads(out)["created"][0]
        self.assertEqual(Board(self.root).find(child_id).depends_on, [base_id])

    def test_plan_rejects_cycle(self):
        payload = {
            "tasks": [
                {"key": "1", "title": "A", "files": ["a.py"], "depends_on": ["2"]},
                {"key": "2", "title": "B", "files": ["b.py"], "depends_on": ["1"]},
            ]
        }
        code, _, err = self.run_cli("plan", "--stdin", stdin=json.dumps(payload))
        self.assertEqual(code, 1)
        self.assertIn("cycle", err)
        self.assertEqual(Board(self.root).cards(), [])

    def test_plan_rejects_protected_path(self):
        payload = {"tasks": [{"key": "1", "title": "Bad", "files": [".agents/config.json"]}]}
        code, _, err = self.run_cli("plan", "--stdin", stdin=json.dumps(payload))
        self.assertEqual(code, 1)
        self.assertIn("protected", err)

    def test_install_skill_writes_both(self):
        written = install_skill(self.root, project=True)
        self.assertEqual(len(written), 2)
        names = sorted(path.parent.name for path in written)
        self.assertEqual(names, ["agents-add", "agents-init"])
        for path in written:
            self.assertTrue(path.is_file())


if __name__ == "__main__":
    unittest.main()
