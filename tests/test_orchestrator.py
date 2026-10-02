import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agents import config, vcs
from agents.board import Board
from agents.logs import read_log
from agents.orchestrator import Orchestrator


def pm_response(tasks):
    return json.dumps({"tasks": tasks})


def one_task(**overrides):
    task = {
        "key": "1",
        "title": "Add hello.py",
        "route": "backend",
        "files": ["hello.py"],
        "acceptance_criteria": ["greet() returns 'hello'"],
        "depends_on": [],
    }
    task.update(overrides)
    return task


class FakeLLM:
    def __init__(self, pm=None, worker=None, reviewer=None):
        self.pm = pm
        self.worker = worker
        self.reviewer = reviewer
        self.roles = []

    def __call__(self, config_dict, root, role, system, user):
        self.roles.append(role)
        if role == "pm":
            return self.pm(config_dict, root, user) if callable(self.pm) else self.pm
        if role == "worker":
            return self.worker(config_dict, root, user) if callable(self.worker) else self.worker
        if role == "reviewer":
            return self.reviewer(config_dict, root, user) if callable(self.reviewer) else self.reviewer
        raise AssertionError(role)


def approve_response():
    return json.dumps({"verdict": "approve", "summary": "looks good", "blocking_issues": [], "nitpicks": []})


def reject_response():
    return json.dumps(
        {"verdict": "request_changes", "summary": "fix it", "blocking_issues": ["missing test"], "nitpicks": []}
    )


class OrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        config.scaffold(self.root, project_name="demo")
        vcs.ensure_repo(self.root, "main")

    def tearDown(self):
        self.tmp.cleanup()

    def write_spec(self, text="Build hello.py", name="spec.md"):
        path = self.root / ".agents" / "specs" / name
        path.write_text(text + "\n")
        return path

    def orchestrator(self, llm, decompose=True, configure=None):
        cfg = config.load_config(self.root)
        cfg["governance"]["decompose_specs"] = decompose
        cfg["governance"]["poll_seconds"] = 0
        if configure:
            configure(cfg)
        return Orchestrator(self.root, config=cfg, llm=llm)

    def worker_by_id(self, config_dict, root, user):
        match = re.search(r"TASK (T-\d+)", user)
        task_id = match.group(1)
        return json.dumps(
            {"summary": "done", "files": [{"path": f"{task_id.lower()}.py", "content": f"# {task_id}\n"}]}
        )

    def test_end_to_end_card_closed(self):
        self.write_spec()
        llm = FakeLLM(
            pm=pm_response([one_task()]),
            worker=json.dumps({"summary": "done", "files": [{"path": "hello.py", "content": "print('hi')\n"}]}),
            reviewer=approve_response(),
        )
        self.orchestrator(llm).run(watch=False)
        card = Board(self.root).find("T-001")
        self.assertEqual(card.status, "closed")
        self.assertTrue((self.root / "hello.py").is_file())
        self.assertEqual((self.root / "hello.py").read_text(), "print('hi')\n")
        self.assertTrue((self.root / ".agents" / "specs" / "processed" / "spec.md").is_file())
        self.assertIn("pm", llm.roles)
        self.assertIn("worker", llm.roles)
        self.assertIn("reviewer", llm.roles)

    def test_decompose_disabled_ignores_spec(self):
        self.write_spec()
        llm = FakeLLM(pm=pm_response([one_task()]), worker="{}", reviewer=approve_response())
        self.orchestrator(llm, decompose=False).run(watch=False)
        self.assertEqual(Board(self.root).cards(), [])
        self.assertTrue((self.root / ".agents" / "specs" / "spec.md").is_file())

    def test_watch_loop_terminates(self):
        llm = FakeLLM(pm=pm_response([one_task()]), worker="{}", reviewer=approve_response())
        ticks = self.orchestrator(llm, decompose=False).run(watch=True, max_ticks=3)
        self.assertEqual(ticks, 3)

    def test_reject_then_approve(self):
        self.write_spec()
        counter = {"n": 0}

        def worker(config_dict, root, user):
            counter["n"] += 1
            return json.dumps(
                {"summary": "done", "files": [{"path": "hello.py", "content": f"# version {counter['n']}\n"}]}
            )

        reviews = {"n": 0}

        def reviewer(config_dict, root, user):
            reviews["n"] += 1
            return reject_response() if reviews["n"] == 1 else approve_response()

        llm = FakeLLM(pm=pm_response([one_task()]), worker=worker, reviewer=reviewer)
        self.orchestrator(llm).run(watch=False)
        card = Board(self.root).find("T-001")
        self.assertEqual(card.status, "closed")
        self.assertEqual(card.review_cycles, 1)
        self.assertEqual(card.attempts, 2)
        self.assertIn("version 2", (self.root / "hello.py").read_text())

    def test_protected_path_blocks(self):
        self.write_spec()
        llm = FakeLLM(
            pm=pm_response([one_task(files=["hello.py"])]),
            worker=json.dumps({"summary": "x", "files": [{"path": ".agents/config.json", "content": "{}"}]}),
            reviewer=approve_response(),
        )
        self.orchestrator(llm).run(watch=False)
        self.assertEqual(Board(self.root).find("T-001").status, "blocked")

    def test_diff_cap_blocks(self):
        self.write_spec()
        big = "\n".join(f"line {i}" for i in range(100)) + "\n"
        llm = FakeLLM(
            pm=pm_response([one_task()]),
            worker=json.dumps({"summary": "x", "files": [{"path": "hello.py", "content": big}]}),
            reviewer=approve_response(),
        )

        def configure(cfg):
            cfg["cost_controls"]["max_diff_lines"] = 5

        self.orchestrator(llm, configure=configure).run(watch=False)
        self.assertEqual(Board(self.root).find("T-001").status, "blocked")

    def test_cycle_in_plan_fails_spec(self):
        self.write_spec()
        cyclic = [
            one_task(key="1", title="A", files=["a.py"], depends_on=["2"]),
            one_task(key="2", title="B", files=["b.py"], depends_on=["1"]),
        ]
        llm = FakeLLM(pm=pm_response(cyclic), worker=self.worker_by_id, reviewer=approve_response())
        self.orchestrator(llm).run(watch=False)
        self.assertEqual(Board(self.root).cards(), [])
        self.assertTrue((self.root / ".agents" / "specs" / "failed" / "spec.md").is_file())

    def test_conflict_resolution(self):
        self.write_spec()

        def pm(config_dict, root, user):
            if "resolved file contents" in user:
                return json.dumps({"files": [{"path": "hello.py", "content": "merged\n"}]})
            return pm_response(
                [
                    one_task(key="1", title="First writer", files=["hello.py"]),
                    one_task(key="2", title="Second writer", files=["hello.py"]),
                ]
            )

        def worker(config_dict, root, user):
            match = re.search(r"TASK (T-\d+)", user)
            return json.dumps(
                {"summary": "done", "files": [{"path": "hello.py", "content": f"{match.group(1)}\n"}]}
            )

        llm = FakeLLM(pm=pm, worker=worker, reviewer=approve_response())
        self.orchestrator(llm).run(watch=False)
        board = Board(self.root)
        self.assertEqual(board.find("T-001").status, "closed")
        self.assertEqual(board.find("T-002").status, "closed")
        self.assertEqual((self.root / "hello.py").read_text(), "merged\n")

    def test_verify_failure_blocks(self):
        self.write_spec()
        llm = FakeLLM(
            pm=pm_response([one_task()]),
            worker=json.dumps({"summary": "done", "files": [{"path": "hello.py", "content": "x\n"}]}),
            reviewer=approve_response(),
        )

        def configure(cfg):
            cfg["verify"] = {"enabled": True, "commands": ["python3 -c \"import sys; sys.exit(1)\""]}

        self.orchestrator(llm, configure=configure).run(watch=False)
        self.assertEqual(Board(self.root).find("T-001").status, "blocked")

    def test_verify_success_closes(self):
        self.write_spec()
        llm = FakeLLM(
            pm=pm_response([one_task()]),
            worker=json.dumps({"summary": "done", "files": [{"path": "hello.py", "content": "x\n"}]}),
            reviewer=approve_response(),
        )

        def configure(cfg):
            cfg["verify"] = {"enabled": True, "commands": ["python3 -c \"print('ok')\""]}

        self.orchestrator(llm, configure=configure).run(watch=False)
        self.assertEqual(Board(self.root).find("T-001").status, "closed")

    def test_card_log_written(self):
        self.write_spec()
        llm = FakeLLM(
            pm=pm_response([one_task()]),
            worker=json.dumps({"summary": "done", "files": [{"path": "hello.py", "content": "x\n"}]}),
            reviewer=approve_response(),
        )
        self.orchestrator(llm).run(watch=False)
        log = read_log(self.root, "T-001")
        self.assertIn("worker started", log)
        self.assertIn("review verdict: approve", log)

    def test_dependency_ordering(self):
        self.write_spec()
        tasks = [
            one_task(key="1", title="First", files=["first.py"]),
            one_task(key="2", title="Second", files=["second.py"], depends_on=["1"]),
        ]
        llm = FakeLLM(pm=pm_response(tasks), worker=self.worker_by_id, reviewer=approve_response())
        self.orchestrator(llm).run(watch=False)
        board = Board(self.root)
        self.assertEqual(board.find("T-001").status, "closed")
        self.assertEqual(board.find("T-002").status, "closed")
        self.assertEqual(board.find("T-002").depends_on, ["T-001"])
        self.assertTrue((self.root / "t-001.py").is_file())
        self.assertTrue((self.root / "t-002.py").is_file())


if __name__ == "__main__":
    unittest.main()
