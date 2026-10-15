"""Tests for scripts/run_evals.py. A fake `claude` (a small Python script on a temp PATH) prints a
canned result JSON, records argv/cwd/stdin, and optionally writes files in its cwd (the worktree).

Run: python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
RUN_EVALS = SKILL / "scripts" / "run_evals.py"
RUN_TRIGGER = SKILL / "evals" / "run_trigger_evals.py"
sys.dont_write_bytecode = True  # keep scripts/__pycache__ out of the skill
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL / "evals"))
import run_evals as R  # noqa: E402
import run_trigger_evals as T  # noqa: E402

FAKE_CLAUDE = textwrap.dedent('''\
    #!{python}
    import json, os, sys, time
    prompt = sys.stdin.read()
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        seen = {{}}
        for rel in json.loads(os.environ.get("FAKE_CLAUDE_PEEK", "[]")):
            p = os.path.join(os.getcwd(), rel)
            seen[rel] = open(p).read() if os.path.isfile(p) else None
        with open(log, "a") as fh:
            fh.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": prompt, "peek": seen,
                                 "pid": os.getpid(),
                                 "noninteractive": os.environ.get("SDLC_NONINTERACTIVE")}}) + "\\n")
    time.sleep(float(os.environ.get("FAKE_CLAUDE_SLEEP", "0")))
    for rel, content in json.loads(os.environ.get("FAKE_CLAUDE_WRITE", "{{}}")).items():
        p = os.path.join(os.getcwd(), rel)
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w") as fh:
            fh.write(content)
    default = {{"type": "result", "subtype": "success", "is_error": False, "num_turns": 3,
               "result": "Fixed it. 4 passed in 0.12s", "total_cost_usd": 0.25}}
    print(os.environ.get("FAKE_CLAUDE_RESULT") or json.dumps(default))
    sys.exit(int(os.environ.get("FAKE_CLAUDE_EXIT", "0")))
''')


def git(cwd, *args):
    return subprocess.run(["git", "-c", "commit.gpgsign=false"] + list(args), cwd=str(cwd), check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True).stdout


class RunEvalsCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name).resolve()
        self.repo = base / "repo"
        self.bin = base / "bin"
        self.log = base / "claude-log.jsonl"
        self.repo.mkdir()
        self.bin.mkdir()
        fake = self.bin / "claude"
        fake.write_text(FAKE_CLAUDE.format(python=sys.executable))
        fake.chmod(0o755)
        self.env = dict(os.environ)
        self.env.update({
            "PATH": "%s%s%s" % (self.bin, os.pathsep, os.environ.get("PATH", "")),
            "FAKE_CLAUDE_LOG": str(self.log),
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "CI": "",
        })
        for k in ("FAKE_CLAUDE_RESULT", "FAKE_CLAUDE_WRITE", "FAKE_CLAUDE_SLEEP", "FAKE_CLAUDE_EXIT",
                  "FAKE_CLAUDE_PEEK", "SDLC_NONINTERACTIVE"):
            self.env.pop(k, None)
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "src").mkdir()
        (self.repo / "tests").mkdir()
        (self.repo / "src" / "app.py").write_text("def status():\n    return 500\n")
        (self.repo / "tests" / "test_app.py").write_text("def test_status():\n    assert True\n")
        (self.repo / "CLAUDE.md").write_text("# Project\n\nRun make test.\n")
        (self.repo / "evals").mkdir()
        git(self.repo, "add", "-A")
        self._git_env_commit("init")

    def tearDown(self):
        subprocess.run(["git", "worktree", "prune"], cwd=str(self.repo), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        self._tmp.cleanup()

    def _git_env_commit(self, msg):
        subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", msg], cwd=str(self.repo),
                       check=True, env=self.env)

    def write_case(self, case, name=None):
        path = self.repo / "evals" / ("%s.json" % (name or case["id"]))
        path.write_text(json.dumps(case, indent=1))
        return path

    def base_case(self, **over):
        case = {
            "id": "status-fix", "prompt": "Make status() return 503 when the DB is down.",
            "setup": [], "allowed_tools": ["Read", "Edit", "Bash(make test)"],
            "permission_mode": "acceptEdits", "max_turns": 7, "timeout_s": 60, "model": None,
            "checks": [
                {"type": "command", "run": "grep -q 503 src/app.py"},
                {"type": "file_exists", "path": "src/app.py"},
                {"type": "result_regex", "pattern": "\\b[0-9]+ passed\\b"},
                {"type": "diff_includes", "glob": "src/**"},
                {"type": "diff_excludes", "glob": "tests/**"},
            ],
        }
        case.update(over)
        return case

    def run_evals(self, *args, **env):
        e = dict(self.env)
        e.update(env)
        proc = subprocess.run([sys.executable, str(RUN_EVALS)] + list(args), cwd=str(self.repo), env=e,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                              timeout=120)
        return proc

    def results(self, proc):
        return json.loads(proc.stdout)

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def worktrees(self):
        out = git(self.repo, "worktree", "list", "--porcelain")
        return [line.split(" ", 1)[1] for line in out.splitlines() if line.startswith("worktree ")]

    FIX = {"FAKE_CLAUDE_WRITE": json.dumps({"src/app.py": "def status():\n    return 503\n"})}


class TestRun(RunEvalsCase):
    def test_passing_case_runs_in_worktree_and_cleans_up(self):
        self.write_case(self.base_case())
        out = self.repo.parent / "results.json"
        proc = self.run_evals("--out", str(out), **self.FIX)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        res = self.results(proc)
        self.assertEqual((res["pass_rate"], res["passed"], res["total"]), (1.0, 1, 1))
        self.assertEqual(res["cost_usd"], 0.25)
        case = res["cases"][0]
        self.assertTrue(case["passed"], case)
        self.assertEqual(case["subtype"], "success")
        self.assertEqual([c["type"] for c in case["checks"]],
                         ["claude", "command", "file_exists", "result_regex", "diff_includes", "diff_excludes"])
        self.assertEqual(case["changed_files"], ["src/app.py"])
        self.assertEqual(json.loads(out.read_text())["pass_rate"], 1.0)
        # the agent ran in a separate worktree, not in the repo, and the repo is untouched
        (call,) = self.calls()
        self.assertNotEqual(Path(call["cwd"]).resolve(), self.repo)
        self.assertIn("wt-status-fix", call["cwd"])
        self.assertIn("return 500", (self.repo / "src" / "app.py").read_text())
        self.assertEqual(self.worktrees(), [str(self.repo)])
        self.assertFalse(Path(call["cwd"]).exists())
        # command line contract
        argv = call["argv"]
        self.assertEqual(argv[0], "-p")
        self.assertEqual(call["stdin"], "Make status() return 503 when the DB is down.")
        self.assertNotIn("--bare", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(argv[argv.index("--max-turns") + 1], "7")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "2")
        self.assertEqual(call["noninteractive"], "1")
        self.assertNotIn("--model", argv)
        self.assertEqual(argv[argv.index("--allowedTools"):], ["--allowedTools", "Read", "Edit", "Bash(make test)"])

    def test_model_from_case_and_override(self):
        self.write_case(self.base_case(model="claude-sonnet-5"))
        self.run_evals(**self.FIX)
        self.run_evals("--model", "haiku", **self.FIX)
        a, b = self.calls()
        self.assertEqual(a["argv"][a["argv"].index("--model") + 1], "claude-sonnet-5")
        self.assertEqual(b["argv"][b["argv"].index("--model") + 1], "haiku")

    def test_budget_from_case_and_override(self):
        self.write_case(self.base_case(max_budget_usd=0.5))
        self.run_evals(**self.FIX)
        self.run_evals("--max-budget-usd", "3.25", **self.FIX)
        a, b = self.calls()
        self.assertEqual(a["argv"][a["argv"].index("--max-budget-usd") + 1], "0.5")
        self.assertEqual(b["argv"][b["argv"].index("--max-budget-usd") + 1], "3.25")

    def test_budget_exhausted_fails_case(self):
        self.write_case(self.base_case())
        result = json.dumps({"type": "result", "subtype": "error_max_budget_usd", "is_error": True,
                             "num_turns": 4, "total_cost_usd": 2.01})
        proc = self.run_evals(**dict(self.FIX, FAKE_CLAUDE_RESULT=result, FAKE_CLAUDE_EXIT="1"))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        case = self.results(proc)["cases"][0]
        self.assertFalse(case["passed"])
        self.assertEqual(case["subtype"], "error_max_budget_usd")

    def test_test_edit_fails_diff_excludes_and_threshold(self):
        self.write_case(self.base_case())
        env = {"FAKE_CLAUDE_WRITE": json.dumps({"src/app.py": "return 503\n", "tests/test_app.py": "# weakened\n"})}
        proc = self.run_evals(**env)
        self.assertEqual(proc.returncode, 1, proc.stderr)
        case = self.results(proc)["cases"][0]
        self.assertFalse(case["passed"])
        bad = [c for c in case["checks"] if not c["passed"]]
        self.assertEqual([c["type"] for c in bad], ["diff_excludes"])
        self.assertIn("tests/test_app.py", bad[0]["detail"])
        self.assertIn("FAIL", proc.stderr)
        self.assertEqual(self.run_evals("--threshold", "0", **env).returncode, 0)
        self.assertEqual(self.worktrees(), [str(self.repo)])

    def test_diff_checks_see_gitignored_files(self):
        (self.repo / ".gitignore").write_text(".env\nbuild/\n")
        git(self.repo, "add", ".gitignore")
        self._git_env_commit("ignore")
        self.write_case(self.base_case(setup=["printf A > .env"], checks=[
            {"type": "diff_excludes", "glob": ".env"}, {"type": "diff_excludes", "glob": "build/**"},
            {"type": "diff_excludes", "glob": ".claude/**"}]))
        writes = {".env": "BB\n", "build/out.txt": "x", ".claude/sdlc-state/.gitignore": "*\n",
                  ".claude/sdlc-state/hook-log.jsonl": "{}\n"}
        proc = self.run_evals(FAKE_CLAUDE_WRITE=json.dumps(writes))
        case = self.results(proc)["cases"][0]
        self.assertEqual(case["changed_files"], [".env", "build/out.txt"])
        self.assertEqual([c["passed"] for c in case["checks"][1:]], [False, False, True])
        proc = self.run_evals()   # setup's own ignored file is the baseline, not a change
        self.assertEqual(self.results(proc)["cases"][0]["changed_files"], [])

    def test_sigterm_kills_claude_and_removes_worktrees(self):
        import signal
        self.write_case(self.base_case())
        self.write_case(self.base_case(id="second"))
        e = dict(self.env, FAKE_CLAUDE_SLEEP="40")
        proc = subprocess.Popen([sys.executable, str(RUN_EVALS), "--jobs", "1"], cwd=str(self.repo), env=e,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        deadline = time.time() + 20
        while not self.calls() and time.time() < deadline:
            time.sleep(0.1)
        self.assertTrue(self.calls(), "claude never started")
        start = time.time()
        proc.send_signal(signal.SIGTERM)
        _out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM, err)
        self.assertLess(time.time() - start, 15)
        self.assertIn("interrupted", err)
        self.assertEqual(len(self.calls()), 1, "a queued case still ran")
        self.assertEqual(self.worktrees(), [str(self.repo)])
        pid = self.calls()[0]["pid"]
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.1)
        else:
            self.fail("claude child %d still running" % pid)

    def test_is_error_result_fails_case_even_if_checks_pass(self):
        self.write_case(self.base_case())
        result = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 8,
                             "result": "4 passed", "total_cost_usd": 0.5, "errors": ["Reached maximum number of turns (7)"]})
        env = dict(self.FIX, FAKE_CLAUDE_RESULT=result, FAKE_CLAUDE_EXIT="1")
        proc = self.run_evals(**env)
        self.assertEqual(proc.returncode, 1)
        case = self.results(proc)["cases"][0]
        self.assertEqual(case["subtype"], "error_max_turns")
        self.assertFalse(case["checks"][0]["passed"])
        self.assertIn("maximum number of turns", case["checks"][0]["detail"])
        self.assertTrue(all(c["passed"] for c in case["checks"][1:]))

    def test_unparseable_output_fails(self):
        self.write_case(self.base_case())
        proc = self.run_evals(FAKE_CLAUDE_RESULT="not json", **self.FIX)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self.results(proc)["cases"][0]["subtype"], "no_result")

    def test_setup_runs_before_claude_and_is_not_counted_as_agent_diff(self):
        case = self.base_case(setup=["printf 'fixture\\n' > fixture.txt", "printf '# reintroduced\\n' >> tests/test_app.py"])
        case["checks"] += [{"type": "file_exists", "path": "fixture.txt"},
                           {"type": "diff_excludes", "glob": "fixture.txt"}]
        self.write_case(case)
        proc = self.run_evals(FAKE_CLAUDE_PEEK=json.dumps(["fixture.txt"]), **self.FIX)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.calls()[0]["peek"]["fixture.txt"], "fixture\n")
        self.assertEqual(self.results(proc)["cases"][0]["changed_files"], ["src/app.py"])
        self.assertFalse((self.repo / "fixture.txt").exists())

    def test_failing_setup_skips_claude(self):
        self.write_case(self.base_case(setup=["exit 3"]))
        proc = self.run_evals(**self.FIX)
        self.assertEqual(proc.returncode, 1)
        case = self.results(proc)["cases"][0]
        self.assertEqual(case["subtype"], "setup_failed")
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.worktrees(), [str(self.repo)])

    def test_uncommitted_config_is_copied_into_worktree(self):
        (self.repo / "CLAUDE.md").write_text("# Project\n\nLOCAL EDIT\n")
        skill = self.repo / ".claude" / "skills" / "demo" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: demo\n---\n")
        state = self.repo / ".claude" / "sdlc-state" / "active.json"
        state.parent.mkdir(parents=True)
        state.write_text("{}")
        (self.repo / "notes.txt").write_text("not config\n")
        case = self.base_case()
        case["checks"].append({"type": "diff_excludes", "glob": "CLAUDE.md"})
        self.write_case(case)  # untracked case file under evals/ is copied too
        peek = ["CLAUDE.md", ".claude/skills/demo/SKILL.md", ".claude/sdlc-state/active.json",
                "notes.txt", "evals/status-fix.json"]
        proc = self.run_evals(FAKE_CLAUDE_PEEK=json.dumps(peek), **self.FIX)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        seen = self.calls()[0]["peek"]
        self.assertIn("LOCAL EDIT", seen["CLAUDE.md"])
        self.assertEqual(seen[".claude/skills/demo/SKILL.md"], "---\nname: demo\n---\n")
        self.assertIsNone(seen[".claude/sdlc-state/active.json"])
        self.assertIsNone(seen["notes.txt"])
        self.assertIsNotNone(seen["evals/status-fix.json"])
        self.assertIn("copying uncommitted config", proc.stderr)

    def test_compare_drop_exit_code(self):
        self.write_case(self.base_case())
        prev = self.repo.parent / "prev.json"
        prev.write_text(json.dumps({"pass_rate": 1.0, "cases": [{"id": "status-fix", "passed": True}]}))
        # current run fails (no fix written); threshold 0 isolates the compare gate
        proc = self.run_evals("--threshold", "0", "--compare", str(prev))
        self.assertEqual(proc.returncode, 1, proc.stderr)
        cmp_ = self.results(proc)["compare"]
        self.assertEqual(cmp_["drop"], 1.0)
        self.assertEqual(cmp_["regressed"], ["status-fix"])
        self.assertTrue(cmp_["failed"])
        proc = self.run_evals("--threshold", "0", "--compare", str(prev), "--max-drop", "1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # equal pass rate is not a drop
        proc = self.run_evals("--compare", str(prev), **self.FIX)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.results(proc)["compare"]["failed"])

    def test_compare_unreadable_baseline_is_usage_error(self):
        self.write_case(self.base_case())
        bad = self.repo.parent / "bad.json"
        bad.write_text("{}")
        self.assertEqual(self.run_evals("--compare", str(bad), **self.FIX).returncode, 2)
        self.assertEqual(self.run_evals("--compare", str(bad) + ".missing", **self.FIX).returncode, 2)

    def test_parallel_cases_case_filter_and_list_file(self):
        self.write_case([self.base_case(id="a"), self.base_case(id="b"), self.base_case(id="c")], name="suite")
        proc = self.run_evals("--jobs", "2", "--case", "a", "c", **self.FIX)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        res = self.results(proc)
        self.assertEqual([c["id"] for c in res["cases"]], ["a", "c"])
        self.assertEqual(res["cost_usd"], 0.5)
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(len({c["cwd"] for c in self.calls()}), 2)
        self.assertEqual(self.worktrees(), [str(self.repo)])

    def test_timeout_kills_claude(self):
        self.write_case(self.base_case(timeout_s=1))
        proc = self.run_evals(FAKE_CLAUDE_SLEEP="30", **self.FIX)
        self.assertEqual(proc.returncode, 1)
        case = self.results(proc)["cases"][0]
        self.assertEqual(case["subtype"], "timeout")
        self.assertLess(case["duration_s"], 20)

    def test_keep_leaves_worktree(self):
        self.write_case(self.base_case())
        proc = self.run_evals("--keep", **self.FIX)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        wt = self.results(proc)["cases"][0]["worktree"]
        self.assertTrue(Path(wt, "src", "app.py").read_text().endswith("503\n"))
        self.assertEqual(len(self.worktrees()), 2)
        git(self.repo, "worktree", "remove", "--force", wt)

    def test_dry_run_prints_commands_without_running(self):
        self.write_case(self.base_case(setup=["git apply evals/fixtures/x.patch"]))
        proc = self.run_evals("--dry-run", PATH="/usr/bin:/bin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("worktree add --detach", proc.stdout)
        self.assertIn("git apply evals/fixtures/x.patch", proc.stdout)
        self.assertIn("SDLC_NONINTERACTIVE=1 claude -p --output-format json --permission-mode acceptEdits "
                      "--max-turns 7 --max-budget-usd 2", proc.stdout)
        self.assertIn("'Bash(make test)'", proc.stdout)
        self.assertIn("Make status() return 503", proc.stdout)
        self.assertIn("worktree remove", proc.stdout)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.worktrees(), [str(self.repo)])


class TestUsageErrors(RunEvalsCase):
    def assertUsage(self, *args, **env):
        proc = self.run_evals(*args, **env)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("run_evals: error:", proc.stderr)
        self.assertEqual(self.calls(), [])
        return proc.stderr

    def test_no_cases(self):
        self.assertIn("no eval cases", self.assertUsage())

    def test_missing_dir(self):
        self.assertIn("not found", self.assertUsage("--dir", "nope"))

    def test_bad_budget_flag(self):
        self.write_case(self.base_case())
        self.assertIn("--max-budget-usd must be a positive number", self.assertUsage("--max-budget-usd", "0"))

    def test_invalid_json(self):
        (self.repo / "evals" / "x.json").write_text("{")
        self.assertIn("invalid JSON", self.assertUsage())

    def test_missing_checks(self):
        self.write_case(self.base_case(checks=[]))
        self.assertIn("checks", self.assertUsage())

    def test_unknown_check_type(self):
        self.write_case(self.base_case(checks=[{"type": "vibes"}]))
        self.assertIn("type must be one of", self.assertUsage())

    def test_bad_regex(self):
        self.write_case(self.base_case(checks=[{"type": "result_regex", "pattern": "("}]))
        self.assertIn("invalid regex", self.assertUsage())

    def test_bypass_permissions_refused(self):
        self.write_case(self.base_case(permission_mode="bypassPermissions"))
        self.assertIn("refused", self.assertUsage())

    def test_duplicate_ids(self):
        self.write_case(self.base_case(), name="one")
        self.write_case(self.base_case(), name="two")
        self.assertIn("duplicate case id", self.assertUsage())

    def test_unknown_case_filter(self):
        self.write_case(self.base_case())
        self.assertIn("unknown case id", self.assertUsage("--case", "nope"))

    def test_claude_missing(self):
        self.write_case(self.base_case())
        self.assertIn("not found", self.assertUsage("--claude-bin", "definitely-not-claude"))

    def test_not_a_git_repo(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIn("not inside a git repository", self.assertUsage("--repo", d))

    def test_bad_threshold(self):
        self.write_case(self.base_case())
        self.assertUsage("--threshold", "1.5")


class TestHelpers(unittest.TestCase):
    def test_parse_result_shapes(self):
        obj = {"type": "result", "subtype": "success", "is_error": False, "result": "ok"}
        self.assertEqual(R.parse_result(json.dumps(obj)), obj)
        self.assertEqual(R.parse_result(json.dumps([{"type": "system"}, obj])), obj)
        self.assertEqual(R.parse_result('{"type": "system"}\n' + json.dumps(obj) + "\n"), obj)
        self.assertIsNone(R.parse_result(""))
        self.assertIsNone(R.parse_result("garbage"))

    def test_is_config_path(self):
        yes = ["CLAUDE.md", "backend/CLAUDE.md", ".claude/settings.json", ".claude/skills/x/SKILL.md",
               ".mcp.json", "REVIEW.md", "evals/a.json", "evals/fixtures/a.patch"]
        no = [".claude/sdlc-state/active.json", ".claude/worktrees/x/a.py", "src/app.py", "evalsx/a.json",
              "docs/REVIEW.md"]
        for p in yes:
            self.assertTrue(R.is_config_path(p, "evals"), p)
        for p in no:
            self.assertFalse(R.is_config_path(p, "evals"), p)

    def test_claude_cmd_puts_variadic_allowed_tools_last(self):
        case = R.normalize_case({"id": "x", "prompt": "-starts with dash", "allowed_tools": "Read",
                                 "permission_mode": "dontAsk", "checks": [{"type": "file_exists", "path": "a"}]}, "x.json")
        cmd = R.claude_cmd(case, "claude", None)
        self.assertEqual(cmd[-2:], ["--allowedTools", "Read"])
        self.assertNotIn("-starts with dash", cmd)
        self.assertEqual(case["max_turns"], 30)
        self.assertEqual(case["timeout_s"], 900)
        self.assertEqual(case["max_budget_usd"], 2.0)
        over = R.claude_cmd(case, "claude", None, 0.75)
        self.assertEqual(over[over.index("--max-budget-usd") + 1], "0.75")

    def test_bad_budget_is_refused(self):
        base = {"id": "x", "prompt": "p", "checks": [{"type": "file_exists", "path": "a"}]}
        for bad in (0, -1, "2", True, float("inf"), float("nan")):
            with self.subTest(bad):
                with self.assertRaises(R.UsageError):
                    R.normalize_case(dict(base, max_budget_usd=bad), "x.json")

    def test_unset_permission_mode_defaults_to_accept_edits(self):
        case = R.normalize_case({"prompt": "p", "checks": [{"type": "file_exists", "path": "a"}]}, "dir/my-case.json")
        self.assertEqual(case["id"], "my-case")
        self.assertEqual(case["permission_mode"], "acceptEdits")

    def test_template_case_is_valid(self):
        data = json.loads((SKILL / "assets" / "templates" / "eval-case.json").read_text())
        case = R.normalize_case(data, "eval-case.json")
        self.assertEqual(case["id"], "status-endpoint-db-down")


FAKE_STREAM_CLAUDE = textwrap.dedent('''\
    #!{python}
    import json, os, sys, time
    query = sys.argv[2]
    with open(os.environ["FAKE_CLAUDE_LOG"], "a") as fh:
        fh.write(json.dumps({{"argv": sys.argv[1:], "cwd": os.getcwd(),
                             "clean_env": os.environ.get("ENABLE_CLAUDEAI_MCP_SERVERS")}}) + "\\n")
    def emit(ev):
        print(json.dumps(ev), flush=True)
    usage = {{"input_tokens": 2, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50, "output_tokens": 6}}
    emit({{"type": "system", "subtype": "init", "model": "claude-test-1", "permissionMode": "plan",
           "skills": json.loads(os.environ.get("FAKE_SKILLS", '["ai-native-sdlc", "code-review"]'))}})
    if "SUBREAD" in query:   # a subagent reads SKILL.md: not a trigger
        emit({{"type": "assistant", "parent_tool_use_id": "toolu_1", "message": {{"id": "m0", "model": "claude-test-1",
               "usage": usage, "content": [{{"type": "tool_use", "name": "Read",
               "input": {{"file_path": "/r/.claude/skills/ai-native-sdlc/SKILL.md"}}}}]}}}})
    if "TRIGGER" in query:
        for block in ({{"type": "text", "text": "loading"}}, {{"type": "tool_use", "name": "Skill", "input": {{"skill": "ai-native-sdlc"}}}}):
            emit({{"type": "assistant", "message": {{"id": "m1", "model": "claude-test-1", "usage": usage, "content": [block]}}}})
        time.sleep(30)   # the runner must kill us as soon as the skill fired
    if "SLEEP" in query:
        time.sleep(30)
    if "OTHER" in query:
        emit({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "name": "Skill", "input": {{"skill": "code-review"}}}}]}}}})
    emit({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "answer"}}]}}}})
    emit({{"type": "result", "subtype": "error_max_turns", "is_error": True, "total_cost_usd": 0.01}})
    sys.exit(1)
''')


class TestTriggerRunner(RunEvalsCase):
    QUERIES = [{"query": "TRIGGER please", "should_trigger": True},
               {"query": "plain question", "should_trigger": True},
               {"query": "OTHER review this diff", "should_trigger": False},
               {"query": "TRIGGER but should not", "should_trigger": False}]

    def setUp(self):
        super().setUp()
        (self.bin / "claude").write_text(FAKE_STREAM_CLAUDE.format(python=sys.executable))
        self.qfile = self.repo.parent / "q.json"
        self.qfile.write_text(json.dumps(self.QUERIES))

    def trigger(self, *args, **env):
        e = dict(self.env)
        e.update(env)
        return subprocess.run([sys.executable, str(RUN_TRIGGER), "--evals", str(self.qfile), "--repo", str(self.repo)]
                              + list(args), cwd=str(self.repo), env=e, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True, timeout=120)

    def test_scan_event(self):
        def ev(name, inp):
            return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}
        self.assertTrue(T.scan_event(ev("Skill", {"skill": "ai-native-sdlc"}), "ai-native-sdlc")["fired"])
        self.assertTrue(T.scan_event(ev("Skill", {"skill": "org-plugin:ai-native-sdlc"}), "ai-native-sdlc")["fired"])
        self.assertTrue(T.scan_event(ev("Skill", {"command": "/ai-native-sdlc review 12"}), "ai-native-sdlc")["fired"])
        self.assertTrue(T.scan_event(ev("Skill", {"name": "ai-native-sdlc"}), "ai-native-sdlc")["fired"])
        read = T.scan_event(ev("Read", {"file_path": "/r/.claude/skills/ai-native-sdlc/SKILL.md"}), "ai-native-sdlc")
        self.assertEqual((read["fired"], read["via"]), (True, "read"))
        other = T.scan_event(ev("Skill", {"skill": "not-ai-native-sdlc"}), "ai-native-sdlc")
        self.assertEqual((other["fired"], other["other_skills"]), (False, ["not-ai-native-sdlc"]))
        self.assertFalse(T.scan_event({"type": "user", "message": {"content": []}}, "ai-native-sdlc")["fired"])

    def test_scan_event_ignores_subagent_hits(self):
        ev = {"type": "assistant", "parent_tool_use_id": "toolu_1", "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/r/.claude/skills/ai-native-sdlc/SKILL.md"}}]}}
        found = T.scan_event(ev, "ai-native-sdlc")
        self.assertEqual((found["fired"], found["subagent_hit"]), (False, "subagent-read"))

    def test_trace_tokens_models_subagent_and_clean(self):
        self.qfile.write_text(json.dumps([{"query": "TRIGGER please", "should_trigger": True},
                                          {"query": "SUBREAD plain", "should_trigger": False}]))
        traces = self.repo.parent / "traces"
        proc = self.trigger("--runs", "1", "--clean", "--trace-dir", str(traces), "--no-isolate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        res = json.loads(proc.stdout)
        self.assertEqual((res["mode"], res["model_requested"], res["models_seen"], res["permission_modes_seen"]),
                         ("clean", None, ["claude-test-1"], ["plan"]))
        trig, sub = res["results"]
        self.assertEqual((trig["triggered"], trig["killed_runs_without_cost"], trig["first_tools"]), (True, 1, ["Skill"]))
        self.assertEqual(trig["tokens"]["cache_read_input_tokens"], 100)   # counted once per message id
        self.assertEqual((sub["triggered"], sub["subagent_via"], sub["first_tools"]), (False, ["subagent-read"], [None]))
        self.assertEqual(res["summary"]["killed_runs_without_cost"], 1)
        text = (traces / "q000-r0.txt").read_text()
        self.assertIn("INIT model=claude-test-1 permissionMode=plan", text)
        self.assertIn('TOOL Skill {"skill": "ai-native-sdlc"}', text)
        self.assertIn("stop=fired", text)
        self.assertIn("[sub] TOOL Read", (traces / "q001-r0.txt").read_text())
        call = self.calls()[0]
        argv = call["argv"]
        self.assertEqual(json.loads(argv[argv.index("--settings") + 1]), {"plansDirectory": ".claude/trigger-eval-plans"})
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project,local")
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(call["clean_env"], "false")
        self.assertFalse((self.repo / ".claude" / "trigger-eval-plans").exists())

    def test_sigterm_stops_runs_and_removes_worktree(self):
        import signal
        self.qfile.write_text(json.dumps([{"query": "SLEEP %d" % i, "should_trigger": False} for i in range(3)]))
        proc = subprocess.Popen([sys.executable, str(RUN_TRIGGER), "--evals", str(self.qfile), "--repo", str(self.repo),
                                 "--runs", "1", "--jobs", "1"], cwd=str(self.repo), env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        deadline = time.time() + 20
        while not self.calls() and time.time() < deadline:
            time.sleep(0.1)
        proc.send_signal(signal.SIGTERM)
        _out, err = proc.communicate(timeout=20)
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM, err)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.worktrees(), [str(self.repo)])

    def test_max_total_usd_cancels_unstarted_runs(self):
        self.qfile.write_text(json.dumps([{"query": "plain %d" % i, "should_trigger": False} for i in range(4)]))
        proc = self.trigger("--runs", "1", "--jobs", "1", "--max-total-usd", "0.015")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        res = json.loads(proc.stdout)
        started = len(self.calls())   # the next run may already have started when the cap is hit
        self.assertIn(started, (2, 3))
        self.assertEqual(res["summary"]["unscored"], 4 - started)
        self.assertIn("max-total-usd", res["results"][3]["errors"][0])

    def test_rates_precision_recall_and_isolation(self):
        out = self.repo.parent / "trig.json"
        start = time.time()
        proc = self.trigger("--runs", "2", "--jobs", "4", "--model", "haiku", "--out", str(out))
        self.assertLess(time.time() - start, 25, "triggered runs were not killed early")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        res = json.loads(out.read_text())
        rates = [r["trigger_rate"] for r in res["results"]]
        self.assertEqual(rates, [1.0, 0.0, 0.0, 1.0])
        self.assertEqual(res["results"][2]["other_skills"], {"code-review": 2})
        s = res["summary"]
        self.assertEqual((s["tp"], s["fn"], s["tn"], s["fp"]), (1, 1, 1, 1))
        self.assertEqual((s["precision"], s["recall"], s["accuracy"]), (0.5, 0.5, 0.5))
        calls = self.calls()
        self.assertEqual(len(calls), 8)
        argv = calls[0]["argv"]
        for flag in ("--output-format", "stream-json", "--verbose", "--max-turns", "2", "--permission-mode",
                     "plan", "--no-session-persistence", "--model", "haiku"):
            self.assertIn(flag, argv)
        self.assertTrue(all(Path(c["cwd"]).resolve() != self.repo for c in calls))
        self.assertEqual(self.worktrees(), [str(self.repo)])
        self.assertEqual(self.trigger("--runs", "1", "--fail-under", "0.9").returncode, 1)

    def test_no_isolate_runs_in_repo(self):
        proc = self.trigger("--runs", "1", "--limit", "1", "--no-isolate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(Path(self.calls()[0]["cwd"]).resolve(), self.repo)

    def test_skill_not_listed_is_unscored(self):
        proc = self.trigger("--runs", "1", "--limit", "2", FAKE_SKILLS='["code-review"]')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        res = json.loads(proc.stdout)
        self.assertEqual(res["summary"]["unscored"], 2)
        self.assertIsNone(res["results"][0]["triggered"])
        self.assertIn("not listed", res["results"][0]["errors"][0])

    def test_dry_run_and_bad_file(self):
        proc = self.trigger("--dry-run", PATH="/usr/bin:/bin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("claude -p 'TRIGGER please' --output-format stream-json --verbose --max-turns 2 "
                      "--permission-mode plan", proc.stdout)
        self.assertEqual(self.calls(), [])
        self.qfile.write_text(json.dumps([{"query": "x"}]))
        self.assertEqual(self.trigger().returncode, 2)

    def test_shipped_trigger_set_is_balanced(self):
        items = T.load_queries(SKILL / "evals" / "trigger-evals.json")
        pos = sum(1 for i in items if i["should_trigger"])
        self.assertGreaterEqual(pos, 8)
        self.assertGreaterEqual(len(items) - pos, 8)
        self.assertEqual(len({i["query"] for i in items}), len(items))


class TestNoBytecode(unittest.TestCase):
    def test_entry_points_write_no_pycache(self):
        import shutil
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "skill"
            shutil.copytree(str(SKILL), str(copy), ignore=shutil.ignore_patterns("__pycache__", "tests"))
            env = dict(os.environ)
            env.pop("PYTHONDONTWRITEBYTECODE", None)
            for script in ("scripts/bands.py", "scripts/run_evals.py", "evals/run_trigger_evals.py",
                           "scripts/sdlc.py"):
                subprocess.run([sys.executable, str(copy / script), "--help"], env=env, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # hooks.py with no event prints usage and exits 1, after importing sdlc_lib
            subprocess.run([sys.executable, str(copy / "scripts/hooks.py")], env=env, cwd=tmp,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.assertEqual(sorted(str(p.relative_to(copy)) for p in copy.rglob("__pycache__")), [])


class TestSkillEvalsFile(unittest.TestCase):
    def test_skill_creator_format(self):
        data = json.loads((SKILL / "evals" / "evals.json").read_text())
        self.assertEqual(data["skill_name"], "ai-native-sdlc")
        self.assertTrue(5 <= len(data["evals"]) <= 8)  # small enough to grade every run by hand
        ids = [e["id"] for e in data["evals"]]
        self.assertEqual(len(set(ids)), len(ids))
        for e in data["evals"]:
            for key in ("prompt", "expected_output", "files", "expectations", "assertions"):
                self.assertIn(key, e)
            self.assertEqual(e["expectations"], e["assertions"])
            self.assertGreaterEqual(len(e["expectations"]), 3)


if __name__ == "__main__":
    unittest.main()
