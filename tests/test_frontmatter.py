import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agents import frontmatter


class FrontmatterTests(unittest.TestCase):
    def test_round_trip(self):
        data = {
            "id": "T-001",
            "title": "Add hello: world's script",
            "priority": 3,
            "depth": 1,
            "flag": True,
            "empty": None,
            "depends_on": ["T-000", "T-002"],
            "history": [{"at": "2026-10-02T12:00:00Z", "note": "created"}],
        }
        text = "---\n" + frontmatter.dumps(data) + "\n---\n\nbody here\n"
        parsed, body = frontmatter.parse(text)
        self.assertEqual(parsed["title"], "Add hello: world's script")
        self.assertEqual(parsed["priority"], 3)
        self.assertIs(parsed["flag"], True)
        self.assertIsNone(parsed["empty"])
        self.assertEqual(parsed["depends_on"], ["T-000", "T-002"])
        self.assertEqual(parsed["history"][0]["note"], "created")
        self.assertEqual(body, "body here")

    def test_missing_frontmatter(self):
        parsed, body = frontmatter.parse("just a body\n")
        self.assertEqual(parsed, {})
        self.assertEqual(body, "just a body")

    def test_int_float_and_bare(self):
        parsed = frontmatter.loads("---\ncount: 12\nratio: 0.5\nword: hello\n---\n")
        self.assertEqual(parsed["count"], 12)
        self.assertEqual(parsed["ratio"], 0.5)
        self.assertEqual(parsed["word"], "hello")


if __name__ == "__main__":
    unittest.main()
