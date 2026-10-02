import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agents import config
from agents.board import BoardError, find_project_root, iso, parse_iso, utcnow
from agents.validate import is_protected, normalize_path


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.board = config.scaffold(self.root, project_name="demo")

    def tearDown(self):
        self.tmp.cleanup()

    def test_scaffold_layout(self):
        agents = self.root / ".agents"
        for name in ("project.md", "config.json", "usage.json"):
            self.assertTrue((agents / name).is_file(), name)
        for column in ("not_started", "in_progress", "review", "blocked", "closed"):
            self.assertTrue((agents / "board" / column).is_dir(), column)
        self.assertTrue((self.root / ".env").is_file())
        self.assertIn("AI_ECONOMY_API_KEY=", (self.root / ".env").read_text())
        self.assertTrue((self.root / ".gitignore").is_file())
        self.assertIn(".agents/worktrees/", (self.root / ".gitignore").read_text())
        self.assertTrue((self.root / ".gitattributes").is_file())
        self.assertIn("package-lock.json -diff", (self.root / ".gitattributes").read_text())

    def test_gitignore_appended_and_idempotent(self):
        path = self.root / ".gitignore"
        path.write_text("custom-ignore\n")
        config.scaffold(self.root)
        text = path.read_text()
        self.assertIn("custom-ignore\n", text)
        self.assertIn(".agents/worktrees/", text)
        config.scaffold(self.root)
        self.assertEqual(path.read_text(), text)

    def test_gitattributes_appended_and_idempotent(self):
        path = self.root / ".gitattributes"
        path.write_text("*.txt text\n")
        config.scaffold(self.root)
        text = path.read_text()
        self.assertIn("*.txt text", text)
        self.assertIn("package-lock.json -diff", text)
        config.scaffold(self.root)
        self.assertEqual(path.read_text(), text)

    def test_find_project_root_walks_up(self):
        nested = self.root / "a" / "b"
        nested.mkdir(parents=True)
        self.assertEqual(find_project_root(nested), self.root.resolve())

    def test_create_and_sequence_ids(self):
        first = self.board.create("First task", body="do a thing")
        second = self.board.create("Second task")
        self.assertEqual(first.id, "T-001")
        self.assertEqual(second.id, "T-002")
        files = list((self.root / ".agents" / "board" / "not_started").glob("*.md"))
        self.assertEqual(len(files), 2)
        reloaded = self.board.find("T-001")
        self.assertEqual(reloaded.title, "First task")
        self.assertEqual(reloaded.body, "do a thing")

    def test_move_updates_status(self):
        card = self.board.create("Move me")
        moved = self.board.move(card.id, "review", note="ready")
        self.assertEqual(moved.status, "review")
        self.assertEqual(self.board.find(card.id).status, "review")
        self.assertFalse((self.root / ".agents" / "board" / "not_started" / f"{card.id}-move-me.md").exists())

    def test_claim_sets_lease_and_blocks_closed(self):
        card = self.board.create("Claim me")
        claimed = self.board.claim(card.id, lease_minutes=30)
        self.assertEqual(claimed.status, "in_progress")
        lease = parse_iso(claimed.lease_until)
        self.assertIsNotNone(lease)
        self.assertGreater(lease, utcnow())
        self.board.move(card.id, "closed")
        with self.assertRaises(BoardError):
            self.board.claim(card.id)

    def test_dependency_gating_and_priority(self):
        dep = self.board.create("Dependency")
        blocked = self.board.create("Blocked child", priority=1, depends_on=[dep.id])
        free = self.board.create("Free", priority=5)
        eligible = [card.id for card in self.board.claimable(self.board.closed_ids())]
        self.assertNotIn(blocked.id, eligible)
        self.assertIn(free.id, eligible)
        self.board.move(dep.id, "closed")
        eligible = [card.id for card in self.board.claimable(self.board.closed_ids())]
        self.assertEqual(eligible, [blocked.id, free.id])

    def test_requeue_expired_lease(self):
        card = self.board.create("Stale")
        self.board.claim(card.id)
        past = iso(utcnow() - timedelta(minutes=5))
        self.board.update(card.id, lease_until=past)
        requeued = self.board.requeue_expired()
        self.assertEqual(requeued, [card.id])
        self.assertEqual(self.board.find(card.id).status, "not_started")

    def test_unknown_card_raises(self):
        with self.assertRaises(BoardError):
            self.board.find("T-999")

    def test_protected_paths(self):
        cfg = config.load_config(self.root)
        self.assertTrue(is_protected(cfg, ".agents/config.json"))
        self.assertTrue(is_protected(cfg, "./.env"))
        self.assertFalse(is_protected(cfg, "src/app.py"))
        self.assertEqual(normalize_path("./src//app.py"), "src//app.py")


if __name__ == "__main__":
    unittest.main()
