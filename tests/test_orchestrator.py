import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agents import config, vcs
from agents.board import Board, BoardError
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
        self.users = []
        self.kwargs = []

    def __call__(self, config_dict, root, role, system, user, **kwargs):
        self.roles.append(role)
        self.users.append((role, user))
        self.kwargs.append((role, kwargs))
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
        vcs.commit_all(self.root, "chore: init")

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

    def test_lockfile_diffs_are_collapsed(self):
        lock = self.root / "package-lock.json"
        lock.write_text("\n".join(f"line {i}" for i in range(500)) + "\n")
        vcs.commit_all(self.root, "lock v1")
        vcs.git(self.root, "checkout", "-b", "feature")
        lock.write_text("\n".join(f"other {i}" for i in range(500)) + "\n")
        vcs.commit_all(self.root, "lock v2")
        diff = vcs.diff(self.root, "main", "feature")
        vcs.git(self.root, "checkout", "main")
        self.assertIn("package-lock.json", diff)
        self.assertLess(len(diff.splitlines()), 20)

    def test_tick_resets_call_counter(self):
        from agents import llm

        llm.CALLS_THIS_RUN = 999
        orch = self.orchestrator(FakeLLM(pm="{}", worker="{}", reviewer=approve_response()), decompose=False)
        orch.tick()
        self.assertEqual(llm.CALLS_THIS_RUN, 0)

    def test_requires_first_commit(self):
        empty = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(empty, ignore_errors=True))
        config.scaffold(empty, project_name="fresh")
        vcs.ensure_repo(empty, "main")
        board = Board(empty)
        board.create("Do a thing")
        llm = FakeLLM(pm="{}", worker="{}", reviewer=approve_response())
        orch = Orchestrator(empty, config=config.load_config(empty), llm=llm)
        with self.assertRaises(BoardError):
            orch.tick()

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

    def test_skills_injected_into_prompts(self):
        skills = self.root / ".agents" / "skills"
        skills.mkdir(parents=True, exist_ok=True)
        (skills / "styling.md").write_text("Always use 1px borders.")
        Board(self.root).create("Style it", files_hint=["src/app.ts"], skills=["styling"])
        llm = FakeLLM(
            pm="{}",
            worker=json.dumps({"summary": "x", "files": [{"path": "src/app.ts", "content": "// ok\n"}]}),
            reviewer=approve_response(),
        )
        self.orchestrator(llm, decompose=False).run(watch=False)
        worker_prompts = [text for role, text in llm.users if role == "worker"]
        reviewer_prompts = [text for role, text in llm.users if role == "reviewer"]
        self.assertTrue(any("Always use 1px borders." in text for text in worker_prompts))
        self.assertTrue(any("Always use 1px borders." in text for text in reviewer_prompts))

    def test_context_file_write_rejected(self):
        Board(self.root).create("Use the API", files_hint=["src/app.ts"], context_files=["src/api.ts"])
        llm = FakeLLM(
            pm="{}",
            worker=json.dumps({"summary": "x", "files": [{"path": "src/api.ts", "content": "changed"}]}),
            reviewer=approve_response(),
        )
        self.orchestrator(llm, decompose=False).run(watch=False)
        self.assertEqual(Board(self.root).find("T-001").status, "blocked")

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

    def test_integrate_resolves_conflict(self):
        (self.root / "f.txt").write_text("base\n")
        vcs.commit_all(self.root, "base")
        vcs.git(self.root, "checkout", "-b", "feature")
        (self.root / "f.txt").write_text("feature\n")
        vcs.commit_all(self.root, "feature")
        vcs.git(self.root, "checkout", "main")
        (self.root / "f.txt").write_text("main\n")
        vcs.commit_all(self.root, "main move")
        result = vcs.integrate(self.root, "main", "feature", lambda conflicts: {"f.txt": "merged\n"})
        self.assertEqual(result, "merged")
        self.assertEqual((self.root / "f.txt").read_text(), "merged\n")

    def test_overlap_exclusion(self):
        board = Board(self.root)
        board.create("A", files_hint=["src/x.ts"])
        board.create("B", files_hint=["src/x.ts"])
        cfg = config.load_config(self.root)
        cfg["governance"]["max_concurrency"] = 2
        orch = Orchestrator(self.root, config=cfg, llm=FakeLLM())
        self.assertEqual(orch._claim(), 1)
        self.assertEqual(len(board.cards(("in_progress",))), 1)

    def test_group_serializes_same_lane(self):
        board = Board(self.root)
        board.create("A", files_hint=["a.ts"], group="ui")
        board.create("B", files_hint=["b.ts"], group="ui")
        cfg = config.load_config(self.root)
        cfg["governance"]["max_concurrency"] = 3
        self.assertEqual(Orchestrator(self.root, config=cfg, llm=FakeLLM())._claim(), 1)

    def test_different_groups_run_together(self):
        board = Board(self.root)
        board.create("A", files_hint=["a.ts"], group="ui")
        board.create("B", files_hint=["b.ts"], group="api")
        cfg = config.load_config(self.root)
        cfg["governance"]["max_concurrency"] = 3
        self.assertEqual(Orchestrator(self.root, config=cfg, llm=FakeLLM())._claim(), 2)

    def test_route_is_default_lane(self):
        board = Board(self.root)
        board.create("A", files_hint=["a.ts"], route="frontend")
        board.create("B", files_hint=["b.ts"], route="frontend")
        cfg = config.load_config(self.root)
        cfg["governance"]["max_concurrency"] = 3
        self.assertEqual(Orchestrator(self.root, config=cfg, llm=FakeLLM())._claim(), 1)

    def test_conflicts_with_blocks_co_scheduling(self):
        board = Board(self.root)
        board.create("A", files_hint=["a.ts"], group="ui")
        child = board.create("B", files_hint=["b.ts"], group="api")
        board.update(child.id, conflicts_with=["T-001"])
        cfg = config.load_config(self.root)
        cfg["governance"]["max_concurrency"] = 3
        self.assertEqual(Orchestrator(self.root, config=cfg, llm=FakeLLM())._claim(), 1)

    def test_shared_path_runs_exclusively(self):
        board = Board(self.root)
        board.create("A", files_hint=["package.json"], group="deps", priority=1)
        board.create("B", files_hint=["b.ts"], group="ui", priority=2)
        cfg = config.load_config(self.root)
        cfg["governance"]["max_concurrency"] = 3
        self.assertEqual(Orchestrator(self.root, config=cfg, llm=FakeLLM())._claim(), 1)

    def test_plan_waves(self):
        board = Board(self.root)
        board.create("A", files_hint=["a.ts"], group="ui", priority=1)
        board.create("B", files_hint=["b.ts"], group="api", priority=2)
        board.create("C", files_hint=["c.ts"], group="ui", priority=3, depends_on=["T-001"])
        cfg = config.load_config(self.root)
        cfg["governance"]["max_concurrency"] = 3
        waves = Orchestrator(self.root, config=cfg, llm=FakeLLM()).plan_waves()
        self.assertEqual(waves, [["T-001", "T-002"], ["T-003"]])

    def test_per_card_diff_override(self):
        Board(self.root).create("Small cap", files_hint=["big.ts"], max_diff_lines=5)
        big = "\n".join(f"line {i}" for i in range(20)) + "\n"
        llm = FakeLLM(
            pm="{}",
            worker=json.dumps({"summary": "x", "files": [{"path": "big.ts", "content": big}]}),
            reviewer=approve_response(),
        )
        self.orchestrator(llm, decompose=False).run(watch=False)
        self.assertEqual(Board(self.root).find("T-001").status, "blocked")

    def test_integrate_dirty_and_agents_stash(self):
        (self.root / "f.txt").write_text("base\n")
        vcs.commit_all(self.root, "base")
        (self.root / "f.txt").write_text("changed\n")
        self.assertEqual(vcs.integrate(self.root, "main", "main"), "dirty")
        vcs.git(self.root, "checkout", "--", "f.txt")
        vcs.git(self.root, "checkout", "-b", "feature2")
        (self.root / "g.txt").write_text("g\n")
        vcs.commit_all(self.root, "g")
        vcs.git(self.root, "checkout", "main")
        config_path = self.root / ".agents" / "config.json"
        config_path.write_text("{}\n")
        result = vcs.integrate(self.root, "main", "feature2")
        self.assertEqual(result, "merged")
        self.assertEqual(config_path.read_text(), "{}\n")

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
