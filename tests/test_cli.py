import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agents import config, vcs
from agents.board import Board, BoardError
from agents.cli import main
from agents.skill import install_skill


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        config.scaffold(self.root, project_name="demo")
        vcs.ensure_repo(self.root, "main")
        vcs.commit_all(self.root, "chore: init")

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

    def _write_skill(self, name, text="rule"):
        directory = self.root / ".agents" / "skills"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{name}.md").write_text(text + "\n")

    def test_add_with_skill(self):
        self._write_skill("styling")
        _, out, _ = self.run_cli("add", "Styled", "--skill", "styling")
        self.assertEqual(Board(self.root).find(out.strip()).skills, ["styling"])

    def test_add_unknown_skill_fails(self):
        code, _, err = self.run_cli("add", "X", "--skill", "nope")
        self.assertEqual(code, 1)
        self.assertIn("unknown skill", err)

    def test_skills_command_lists(self):
        self._write_skill("styling")
        code, out, _ = self.run_cli("skills")
        self.assertEqual(code, 0)
        self.assertIn("styling", out)

    def test_plan_with_skill(self):
        self._write_skill("styling")
        payload = {"tasks": [{"key": "1", "title": "S", "files": ["a.ts"], "skills": ["styling"]}]}
        code, out, _ = self.run_cli("plan", "--stdin", stdin=json.dumps(payload))
        self.assertEqual(code, 0)
        card_id = json.loads(out)["created"][0]
        self.assertEqual(Board(self.root).find(card_id).skills, ["styling"])

    def test_add_group_and_conflicts(self):
        _, out, _ = self.run_cli("add", "Base")
        base_id = out.strip()
        _, out2, _ = self.run_cli("add", "Child", "--group", "ui", "--conflicts-with", base_id)
        card = Board(self.root).find(out2.strip())
        self.assertEqual(card.group, "ui")
        self.assertEqual(card.conflicts_with, [base_id])

    def test_plan_maps_conflicts_with(self):
        payload = {
            "tasks": [
                {"key": "1", "title": "A", "files": ["a.ts"]},
                {"key": "2", "title": "B", "files": ["b.ts"], "conflicts_with": ["1"]},
            ]
        }
        code, out, _ = self.run_cli("plan", "--stdin", stdin=json.dumps(payload))
        self.assertEqual(code, 0)
        ids = json.loads(out)["created"]
        self.assertEqual(Board(self.root).find(ids[1]).conflicts_with, [ids[0]])

    def test_plan_check_creates_nothing(self):
        payload = {"tasks": [{"key": "1", "title": "A", "files": ["a.ts"]}]}
        code, out, _ = self.run_cli("plan", "--check", "--stdin", stdin=json.dumps(payload))
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)["valid"])
        self.assertEqual(Board(self.root).cards(), [])

    def test_schedule_outputs_waves(self):
        self.run_cli("add", "A", "--file", "a.ts", "--group", "ui")
        self.run_cli("add", "B", "--file", "b.ts", "--group", "api")
        code, out, _ = self.run_cli("schedule")
        self.assertEqual(code, 0)
        self.assertIn("wave 1", out)

    def test_add_context_file(self):
        _, out, _ = self.run_cli("add", "Task", "--context-file", "src/api.ts")
        card = Board(self.root).find(out.strip())
        self.assertEqual(card.context_files, ["src/api.ts"])

    def test_cancel_removes_card(self):
        _, out, _ = self.run_cli("add", "Doomed")
        card_id = out.strip()
        Board(self.root).move(card_id, "blocked")
        code, _, _ = self.run_cli("cancel", card_id, "--reason", "duplicate")
        self.assertEqual(code, 0)
        with self.assertRaises(BoardError):
            Board(self.root).find(card_id)

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
                    "context_files": ["src/api.ts"],
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
        self.assertEqual(board.find("T-002").context_files, ["src/api.ts"])
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

    def test_retry_reopens_for_rework(self):
        _, out, _ = self.run_cli("add", "Task")
        card_id = out.strip()
        board = Board(self.root)
        board.move(card_id, "blocked")
        code, _, _ = self.run_cli("retry", card_id)
        self.assertEqual(code, 0)
        self.assertEqual(board.find(card_id).status, "not_started")

    def test_retry_deletes_branch(self):
        _, out, _ = self.run_cli("add", "Task")
        card_id = out.strip()
        board = Board(self.root)
        vcs.git(self.root, "branch", "agent/t-001")
        board.update(card_id, branch="agent/t-001")
        board.move(card_id, "blocked")
        code, _, _ = self.run_cli("retry", card_id)
        self.assertEqual(code, 0)
        self.assertEqual(board.find(card_id).branch, "")
        self.assertFalse(vcs.branch_exists(self.root, "agent/t-001"))

    def test_cancel_deletes_branch(self):
        _, out, _ = self.run_cli("add", "Doomed")
        card_id = out.strip()
        vcs.git(self.root, "branch", "agent/t-001")
        Board(self.root).update(card_id, branch="agent/t-001")
        code, _, _ = self.run_cli("cancel", card_id)
        self.assertEqual(code, 0)
        self.assertFalse(vcs.branch_exists(self.root, "agent/t-001"))

    def test_re_review_with_branch(self):
        _, out, _ = self.run_cli("add", "Task")
        card_id = out.strip()
        board = Board(self.root)
        board.update(card_id, branch="agent/t-001")
        board.move(card_id, "blocked")
        code, _, _ = self.run_cli("re-review", card_id)
        self.assertEqual(code, 0)
        self.assertEqual(board.find(card_id).status, "review")

    def test_unblock_without_branch_reopens(self):
        _, out, _ = self.run_cli("add", "Task")
        card_id = out.strip()
        board = Board(self.root)
        board.move(card_id, "blocked")
        self.run_cli("unblock", card_id)
        self.assertEqual(board.find(card_id).status, "not_started")

    def test_status_json_shape(self):
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(set(data), {"cards", "runner", "budget"})
        self.assertFalse(data["runner"]["running"])

    def test_install_skill_writes_both(self):
        written = install_skill(self.root, project=True)
        self.assertEqual(len(written), 2)
        names = sorted(path.parent.name for path in written)
        self.assertEqual(names, ["agents-add", "agents-init"])
        for path in written:
            self.assertTrue(path.is_file())
        init_text = (self.root / ".claude" / "skills" / "agents-init" / "SKILL.md").read_text()
        self.assertIn("agents run --detach", init_text)
        self.assertIn("git commit", init_text)
        self.assertIn("poll every", init_text.lower())


if __name__ == "__main__":
    unittest.main()
