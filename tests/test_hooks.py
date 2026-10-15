"""Tests for scripts/hooks.py: canned hook JSON on stdin, temp git repos with an active change.

Run: python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
HOOKS = SKILL / "scripts" / "hooks.py"
SDLC = SKILL / "scripts" / "sdlc.py"
sys.path.insert(0, str(SKILL / "scripts"))
import sdlc_lib as L  # noqa: E402
import hooks as H  # noqa: E402

INTENT_FILL = {
    "Problem": "The status endpoint returns 500 when a claim has no handler.",
    "Proposed outcome": "It returns the status with handler 'unassigned'.",
    "Affected users and systems": "Portal customers; claims API.",
    "Constraints": "No schema change.",
    "Assumptions": "None",
    "Open questions": "None",
}

PLAN = """---
id: {id}
title: Status fix
status: draft
spec_sha: ""
intent_sha: "{intent_sha}"
---
# Plan: Status fix

## Goal
Return status for unassigned claims.

## Files that change
- `src/status.py`
- `tests/test_status.py`

## Order of work
1. Fix the handler lookup

## Risks
Low: one function.

## Proof
`pytest -q` covers it.

## Rollback
Revert the commit.

## Review focus
None lookup.

## Change log
"""


def setUpModule() -> None:
    cfg = Path(tempfile.mkdtemp(prefix="hooks-gitcfg-")) / "gitconfig"
    cfg.write_text("", encoding="utf-8")
    os.environ["GIT_CONFIG_GLOBAL"] = str(cfg)
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
    os.environ.pop("CI", None)
    os.environ.pop("SDLC_NONINTERACTIVE", None)  # the suite tests interactive behaviour unless a test sets it
    os.environ.pop("CLAUDE_PROJECT_DIR", None)


class Base(unittest.TestCase):
    config: dict = {}

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="hooks-")).resolve()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test Engineer")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "commit.gpgsign", "false")
        (self.root / "src").mkdir()
        (self.root / "src/status.py").write_text("def status():\n    return 1\n", encoding="utf-8")
        (self.root / "tests").mkdir()
        (self.root / "tests/test_status.py").write_text("def test_x():\n    assert False\n", encoding="utf-8")
        if self.config:
            self.set_config(self.config)
        self.git("add", ".")
        self.git("commit", "-qm", "init")

    # helpers -------------------------------------------------------------
    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root)] + list(args), check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True).stdout

    def set_config(self, cfg: dict) -> None:
        (self.root / ".claude").mkdir(exist_ok=True)
        (self.root / ".claude/sdlc.json").write_text(json.dumps(cfg), encoding="utf-8")

    def sdlc(self, *args: str, code: int = 0) -> subprocess.CompletedProcess:
        p = subprocess.run([sys.executable, str(SDLC)] + list(args), cwd=str(self.root), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(p.returncode, code, "sdlc %s: %s %s" % (args, p.stdout, p.stderr))
        return p

    def hook(self, sub: str, payload: dict, env: dict = None) -> subprocess.CompletedProcess:
        data = {"session_id": "s1", "cwd": str(self.root), "transcript_path": "", "hook_event_name": sub}
        data.update(payload)
        e = dict(os.environ)
        e.update(env or {})
        return subprocess.run([sys.executable, str(HOOKS), sub], input=json.dumps(data), cwd=str(self.root),
                              env=e, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

    def edit(self, rel: str, tool: str = "Edit", env: dict = None) -> subprocess.CompletedProcess:
        path = rel if rel.startswith("/") else str(self.root / rel)
        return self.hook("pre-tool", {"tool_name": tool, "tool_input": {"file_path": path, "old_string": "a",
                                                                        "new_string": "b"}}, env)

    def bash(self, cmd: str, env: dict = None) -> subprocess.CompletedProcess:
        return self.hook("pre-tool", {"tool_name": "Bash", "tool_input": {"command": cmd}}, env)

    def assertAllowed(self, p: subprocess.CompletedProcess) -> None:
        self.assertEqual(p.returncode, 0, "expected allow, got rc=%d stderr=%s" % (p.returncode, p.stderr))

    def assertDenied(self, p: subprocess.CompletedProcess, needle: str) -> None:
        self.assertEqual(p.returncode, 2, "expected deny, got rc=%d stderr=%s" % (p.returncode, p.stderr))
        self.assertIn(needle.lower(), p.stderr.lower())

    def new(self, tier: str = "bounded", slug: str = "status-fix") -> str:
        p = self.sdlc("new", slug, "--title", "Status fix", "--tier", tier, "--json")
        return json.loads(p.stdout)["id"]

    def cdir(self, cid: str) -> Path:
        return self.root / "changes" / cid

    def fill_intent(self, cid: str) -> None:
        p = self.cdir(cid) / "intent.md"
        text = p.read_text(encoding="utf-8")
        for k, v in INTENT_FILL.items():
            text = re.sub(r"(## %s\n)(<!--.*?-->\n)" % re.escape(k), lambda m: m.group(1) + v + "\n", text, flags=re.S)
        p.write_text(text, encoding="utf-8")

    def write_plan(self, cid: str) -> None:
        d = self.cdir(cid)
        (d / "plan.md").write_text(PLAN.format(id=cid, intent_sha=L.file_fingerprint(d / "intent.md")),
                                   encoding="utf-8")

    def approve_plan(self, cid: str) -> None:
        self.sdlc("present", cid, "plan")
        self.sdlc("approve", cid, "plan", "--by", "Eng One", "--evidence", "go ahead")

    def bounded_approved(self) -> str:
        cid = self.new("bounded")
        self.fill_intent(cid)
        self.write_plan(cid)
        self.approve_plan(cid)
        return cid

    def hook_log(self) -> list:
        p = self.root / ".claude/sdlc-state/hook-log.jsonl"
        if not p.is_file():
            return []
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]

    def human_events(self) -> list:
        return L.human_events(self.root)


class CliTest(Base):
    def test_usage_and_bad_input(self):
        p = subprocess.run([sys.executable, str(HOOKS), "nope"], input="{}", stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(p.returncode, 1)
        p = subprocess.run([sys.executable, str(HOOKS), "pre-tool"], input="not json", stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, cwd=str(self.root))
        self.assertEqual(p.returncode, 0)

    def test_fast_enough(self):
        cid = self.bounded_approved()
        self.assertTrue(cid)
        t = time.time()
        self.assertAllowed(self.edit("src/status.py"))
        self.assertLess(time.time() - t, 2.0)


class ProtectedPathTest(Base):
    config = {"protected_paths": ["src/generated/**", "v1/**"]}

    def test_protected_paths(self):
        p = self.edit("src/generated/api.py", "Write")
        self.assertDenied(p, "protected path")
        self.assertIn("ask the human", p.stderr.lower())
        self.assertAllowed(self.edit("src/status.py"))  # no active change: no plan guard
        self.assertDenied(self.bash("rm -rf v1/old"), "protected path")
        self.assertDenied(self.bash("echo x > src/generated/x.py"), "protected path")
        self.assertAllowed(self.bash("cat src/generated/api.py"))
        log = self.hook_log()
        self.assertTrue(log and log[-1]["decision"] == "deny")
        self.assertEqual(set(log[-1]), {"ts", "event", "tool", "decision", "reason", "change"})

    def test_audit_and_state_files(self):
        cid = self.new()
        self.assertDenied(self.edit("changes/%s/audit.md" % cid), "written only by sdlc.py")
        self.assertDenied(self.edit(".claude/sdlc-state/active.json", "Write"), "written only by sdlc.py")
        self.assertDenied(self.bash("echo '- x' >> changes/%s/audit.md" % cid), "audit")
        self.assertDenied(self.bash("rm -rf changes/%s" % cid), "audit")
        self.assertDenied(self.bash("rm -rf .claude/sdlc-state"), "runtime state")
        self.assertAllowed(self.bash("cat changes/%s/audit.md" % cid))

    def test_shell_tools_write_guards(self):
        cid = self.new()
        audit = "changes/%s/audit.md" % cid
        for tool in ("Monitor", "PowerShell"):
            p = self.hook("pre-tool", {"tool_name": tool, "tool_input": {"command": "echo x >> %s" % audit}})
            self.assertDenied(p, "audit")
        for cmd in ("Add-Content -Path %s -Value x" % audit, "'x' | Out-File %s" % audit,
                    "Set-Content %s 'x'" % audit, "Copy-Item /tmp/a.md -Destination %s" % audit,
                    "Remove-Item -Recurse changes/%s" % cid):
            p = self.hook("pre-tool", {"tool_name": "PowerShell", "tool_input": {"command": cmd}})
            self.assertDenied(p, "audit")
        self.assertAllowed(self.hook("pre-tool", {"tool_name": "PowerShell",
                                                  "tool_input": {"command": "Get-Content %s" % audit}}))
        self.assertAllowed(self.hook("pre-tool", {"tool_name": "PowerShell", "tool_input": {
            "command": "Copy-Item -Path %s -Destination /tmp/a.md" % audit}}))

    def test_case_variants_and_hard_links(self):
        cid = self.new()
        probe = self.root / ".CLAUDE"
        if not probe.exists():
            self.skipTest("case-sensitive filesystem")
        self.assertDenied(self.edit(".CLAUDE/sdlc-state/active.json", "Write"), "written only by sdlc.py")
        self.assertDenied(self.edit("CHANGES/%s/AUDIT.md" % cid), "audit")
        self.assertDenied(self.bash("echo x >> .CLAUDE/sdlc-state/human-events.jsonl"), "runtime state")
        self.assertDenied(self.edit(".Claude/SDLC.json"), "self_protect")

    def test_hard_link_to_guarded_file(self):
        cid = self.new()
        link = self.root / "notes.md"
        os.link(str(self.cdir(cid) / "audit.md"), str(link))
        self.assertDenied(self.edit("notes.md", "Write"), "hard link")
        self.assertDenied(self.bash("echo x >> notes.md"), "hard link")
        self.assertDenied(self.bash("ln changes/%s/audit.md other.md" % cid), "audit")
        self.assertAllowed(self.bash("ln -s changes/%s/audit.md /tmp/x-%s.md" % (cid, cid)))

    def test_edits_under_changes_and_outside_root(self):
        cid = self.new()
        self.assertAllowed(self.edit("changes/%s/intent.md" % cid))
        self.assertAllowed(self.edit("changes/%s/spec.md" % cid, "Write"))
        outside = Path(tempfile.mkdtemp(prefix="hooks-out-")) / "notes.py"
        self.assertAllowed(self.edit(str(outside), "Write"))
        self.assertAllowed(self.hook("pre-tool", {"tool_name": "NotebookEdit",
                                                  "tool_input": {"notebook_path": str(outside)}}))


class SelfProtectTest(Base):
    def test_config_files_guarded(self):
        self.set_config({})
        for rel in (".claude/sdlc.json", ".claude/settings.json", ".claude/settings.local.json",
                    ".claude/agents/sdlc-gate.md"):
            p = self.edit(rel, "Write")
            self.assertDenied(p, "self_protect")
            self.assertIn('set "self_protect": false in .claude/sdlc.json by hand', p.stderr)
        self.assertDenied(self.bash("echo '{}' >> .claude/sdlc.json"), "guardrail configuration")
        self.assertDenied(self.bash("sed -i '' s/true/false/ .claude/settings.json"), "self_protect")
        self.assertDenied(self.bash("rm -rf .claude"), "guarded path")
        self.assertAllowed(self.bash("cat .claude/sdlc.json"))
        self.assertAllowed(self.edit(".claude/agents/my-helper.md", "Write"))
        self.assertAllowed(self.edit(".claude/sdlc-json-notes.md", "Write"))
        self.assertAllowed(self.edit(".claude/commands/x.md", "Write"))

    def test_self_protect_false_allows(self):
        self.set_config({"self_protect": False})
        self.assertAllowed(self.edit(".claude/sdlc.json"))
        self.assertAllowed(self.bash("echo '{}' >> .claude/settings.json"))


class FixModeTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.cid = self.bounded_approved()
        self.git("add", ".")
        self.git("commit", "-qm", "artifacts")
        self.sdlc("fix-mode", self.cid, "on")

    def test_test_edits_denied(self):
        p = self.edit("tests/test_status.py")
        self.assertDenied(p, "fix task")
        self.assertIn("fix-mode %s off" % self.cid, p.stderr)
        self.assertDenied(self.edit("tests/test_new.py", "Write"), "fix task")
        self.assertAllowed(self.edit("src/status.py"))

    def test_bash_test_writes_denied(self):
        self.assertDenied(self.bash("sed -i 's/False/True/' tests/test_status.py"), "fix task")
        self.assertDenied(self.bash("sed -i.bak -e 's/a/b/' tests/test_status.py"), "fix task")
        self.assertDenied(self.bash("cd tests && perl -pi -e 's/False/True/' test_status.py"), "fix task")
        self.assertDenied(self.bash("echo 'assert True' >> tests/test_status.py"), "fix task")
        self.assertDenied(self.bash("git checkout -- tests/test_status.py"), "fix task")
        self.assertDenied(self.bash("rm -rf tests"), "fix task")
        self.assertDenied(self.bash("cp /tmp/x.py tests/"), "fix task")
        self.assertDenied(self.bash("git checkout HEAD~1 tests/test_status.py"), "fix task")
        self.assertDenied(self.bash("git checkout HEAD tests/"), "fix task")
        self.assertDenied(self.bash("touch tests/test_new.py"), "fix task")
        self.assertDenied(self.bash("cp -t tests/ /tmp/a.py"), "fix task")
        self.assertDenied(self.bash("rsync /tmp/x.py tests/test_status.py"), "fix task")
        self.assertDenied(self.bash("echo x > $PWD/tests/test_status.py"), "fix task")
        self.assertDenied(self.bash("git stash pop"), "fix task")
        self.assertDenied(self.bash("git reset --hard HEAD~1"), "fix task")
        (self.root / "fix.patch").write_text("--- a/tests/test_status.py\n+++ b/tests/test_status.py\n@@\n",
                                            encoding="utf-8")
        self.assertDenied(self.bash("git apply fix.patch"), "fix task")
        self.assertDenied(self.bash("patch -p1 < fix.patch"), "fix task")
        self.assertDenied(self.bash("git apply missing.patch"), "fix task")

    def test_bash_reads_and_runs_allowed(self):
        self.assertAllowed(self.bash("pytest tests/ 2>&1 | tee /tmp/out.log"))
        self.assertAllowed(self.bash("cat tests/test_status.py"))
        self.assertAllowed(self.bash("cp tests/test_status.py /tmp/backup.py"))
        self.assertAllowed(self.bash("sed -n 1,5p tests/test_status.py"))
        self.assertAllowed(self.bash("python -m pytest -q tests/test_status.py > /dev/null"))
        self.assertAllowed(self.bash("git stash"))
        self.assertAllowed(self.bash("git reset --hard"))
        self.assertAllowed(self.bash("git reset --hard HEAD"))
        self.assertAllowed(self.bash("git apply --check fix.patch"))
        (self.root / "src.patch").write_text("--- a/src/status.py\n+++ b/src/status.py\n@@\n", encoding="utf-8")
        self.assertAllowed(self.bash("git apply src.patch"))

    def test_off_restores(self):
        self.sdlc("fix-mode", self.cid, "off")
        self.assertAllowed(self.edit("tests/test_status.py"))


class PlanGuardTest(Base):
    def test_deny_until_approved_then_stale_then_change_log(self):
        cid = self.new("bounded")
        self.fill_intent(cid)
        p = self.edit("src/status.py")
        self.assertDenied(p, "plan guard")
        self.assertRegex(p.stderr, r"write \S*plan\.md")
        self.write_plan(cid)
        p = self.edit("src/status.py")
        self.assertDenied(p, "plan guard")
        self.assertIn("present %s plan" % cid, p.stderr)
        self.approve_plan(cid)
        self.assertAllowed(self.edit("src/status.py"))
        # edit below '## Change log' keeps the approval current
        plan = self.cdir(cid) / "plan.md"
        plan.write_text(plan.read_text(encoding="utf-8") + "- 2026-10-08 added helper — needed\n", encoding="utf-8")
        self.assertAllowed(self.edit("src/status.py"))
        # edit to the frozen part invalidates it
        plan.write_text(plan.read_text(encoding="utf-8").replace("Low: one function.", "Medium."), encoding="utf-8")
        p = self.edit("src/status.py")
        self.assertDenied(p, "plan guard")
        self.assertIn("changed after approval", p.stderr)
        self.assertAllowed(self.edit("changes/%s/plan.md" % cid))

    def test_bash_writes_plan_guarded(self):
        cid = self.new("bounded")
        for cmd in ("cat > src/status.py <<EOF\nx\nEOF", "sed -i '' s/1/2/ src/status.py",
                    "printf x | tee src/status.py"):
            self.assertDenied(self.bash(cmd), "plan guard")
        self.assertAllowed(self.bash("echo x > changes/%s/notes.md" % cid))
        self.assertAllowed(self.bash("echo x > /tmp/hooks-plan-guard.txt"))
        self.assertAllowed(self.bash("cat src/status.py"))

    def test_standard_lists_missing_gates_and_exempt(self):
        self.set_config({"plan_guard_exempt": ["docs/**"]})
        self.new("standard")
        p = self.edit("src/status.py")
        self.assertDenied(p, "intent, spec, plan")
        self.assertAllowed(self.edit("docs/readme.md"))
        self.set_config({"plan_guard": False})
        self.assertAllowed(self.edit("src/status.py"))

    def test_closed_or_missing_active_change(self):
        cid = self.new("bounded")
        self.sdlc("close", cid, "--status", "dropped")
        self.assertAllowed(self.edit("src/status.py"))
        L.set_active(self.root, "20990101-gone")
        self.assertAllowed(self.edit("src/status.py"))

    def test_hooks_disabled(self):
        self.new("bounded")
        self.set_config({"hooks_enabled": False})
        self.assertAllowed(self.edit("src/status.py"))
        self.assertAllowed(self.edit(".claude/sdlc.json"))
        p = self.bash("make deploy ENV=production")  # the fail-closed gate stays on
        self.assertDenied(p, "production gate")
        self.assertIn("hooks disabled; production gate still enforced", self.hook_log()[-1]["reason"])
        self.assertAllowed(self.bash("make test"))


class ProductionGateTest(Base):
    DEPLOY = "make deploy ENV=production"

    def gate(self, check: str, timeout: int = 20) -> None:
        self.set_config({"production_gate": {"approval_check": check, "approval_timeout_s": timeout}})

    def test_empty_check_denies(self):
        p = self.bash(self.DEPLOY, env={"RELEASE_APPROVAL": "approved"})
        self.assertDenied(p, "release authorization by a named release manager")
        self.assertIn("references/stage-5-deploy.md", p.stderr)
        for cmd in ("kubectl apply -f k.yaml -n prod", "kubectl -n prod apply -f k.yaml",
                    "helm upgrade api ./chart -f values-prod.yaml",
                    "terraform apply -auto-approve", "gh workflow run release.yml -f env=prod",
                    "git push production main", 'git commit -m "deploy prod" && git push prod HEAD:main'):
            self.assertDenied(self.bash(cmd), "production gate")
        self.assertAllowed(self.bash("make test"))
        self.assertAllowed(self.bash("git push origin change/fix-prod-typo"))
        self.assertAllowed(self.bash("kubectl -n staging apply -f k.yaml"))
        self.assertAllowed(self.bash("kubectl -n prod get pods"))

    def test_check_true_allows_and_logs(self):
        self.gate("true")
        self.assertAllowed(self.bash(self.DEPLOY))
        log = self.hook_log()
        self.assertEqual(log[-1]["decision"], "allow")
        self.assertIn("approval check passed", log[-1]["reason"])

    def test_check_false_denies_with_output(self):
        self.gate("echo 'no approved ticket for CHG123' >&2; exit 3")
        p = self.bash(self.DEPLOY)
        self.assertDenied(p, "no release authorization")
        self.assertIn("no approved ticket for CHG123", p.stderr)
        self.assertIn("exited 3", p.stderr)

    def test_env_passed_to_check(self):
        cid = self.new("bounded")
        self.gate('test "$SDLC_TOOL" = Bash && test "$SDLC_CHANGE_ID" = "%s" && '
                  'case "$SDLC_COMMAND" in *production*) exit 0;; *) exit 1;; esac' % cid)
        self.assertAllowed(self.bash(self.DEPLOY))

    def test_timeout_fails_closed(self):
        self.gate("sleep 10", timeout=1)
        t = time.time()
        p = self.bash(self.DEPLOY)
        self.assertDenied(p, "timed out")
        self.assertLess(time.time() - t, 6)

    def test_errors_fail_closed(self):
        self.set_config({"production_gate": {"bash_patterns": ["(unclosed"]}})
        self.assertDenied(self.bash("ls"), "production gate error")
        (self.root / ".claude/sdlc.json").write_text("{not json", encoding="utf-8")
        self.assertDenied(self.bash(self.DEPLOY), "does not parse")
        self.assertDenied(self.bash("ls"), "every Bash/MCP call is denied")  # fails closed, not just defaults
        self.assertDenied(self.hook("pre-tool", {"tool_name": "mcp__github__get_issue", "tool_input": {}}),
                          "does not parse")
        self.assertAllowed(self.bash("cat .claude/sdlc.json"))  # exact read-only diagnosis commands
        self.assertAllowed(self.edit("src/status.py"))  # file guards fail open: the config is fixed with Edit
        self.assertAllowed(self.edit(".claude/sdlc.json"))
        self.assertEqual(self.hook_log()[-1]["decision"], "error")

    def test_custom_pattern_with_broken_config(self):
        self.set_config({"production_gate": {"bash_patterns": ["release\\.sh"]}})
        self.assertDenied(self.bash("./scripts/release.sh v2"), "production gate")
        (self.root / ".claude/sdlc.json").write_text('{"production_gate": {"bash_patterns": ["release\\\\.sh"]},}',
                                                    encoding="utf-8")
        self.assertDenied(self.bash("./scripts/release.sh v2"), "does not parse")

    def test_monitor_and_powershell_gated(self):
        for tool in ("Monitor", "PowerShell"):
            p = self.hook("pre-tool", {"tool_name": tool, "tool_input": {"command": "./deploy.sh production"}})
            self.assertDenied(p, "production gate")
        # a Monitor WebSocket watch has no command: nothing to match, allowed
        self.assertAllowed(self.hook("pre-tool", {"tool_name": "Monitor", "tool_input": {"url": "wss://x"}}))
        self.gate('test "$SDLC_TOOL" = Monitor && test "$SDLC_COMMAND" = "./deploy.sh production"')
        self.assertAllowed(self.hook("pre-tool", {"tool_name": "Monitor",
                                                  "tool_input": {"command": "./deploy.sh production"}}))

    def test_inert_text_not_gated(self):
        cmds = ('git commit -m "docs: deploy to prod runbook"',
                'git add -A && git commit -m "fix: deploy prod notes" && git push -u origin change/x',
                'git commit -m "$(cat <<\'EOF\'\nfix(deploy): don\'t break prod (again)\nEOF\n)"',
                'grep -rn "deploy.*prod" docs/ | head -5', 'echo "deploy to production"',
                'gh pr create --title "Deploy prod docs" --body "see plan"')
        for cmd in cmds:
            self.assertAllowed(self.bash(cmd))
        self.assertDenied(self.bash('git commit -m "deploy prod" && ./run.sh'), "production gate")

    def test_mcp_tool_gate(self):
        p = self.hook("pre-tool", {"tool_name": "mcp__acme__deploy_production", "tool_input": {"service": "api"}})
        self.assertDenied(p, "MCP tool mcp__acme__deploy_production")
        self.assertAllowed(self.hook("pre-tool", {"tool_name": "mcp__github__get_issue", "tool_input": {}}))
        self.gate('case "$SDLC_COMMAND" in *api*) exit 0;; *) exit 1;; esac')
        self.assertAllowed(self.hook("pre-tool", {"tool_name": "mcp__acme__deploy_prod_v2",
                                                  "tool_input": {"service": "api"}}))


class PostToolTest(Base):
    def test_ask_user_question_recorded(self):
        p = self.hook("post-tool", {"tool_name": "AskUserQuestion",
                                    "tool_input": {"questions": [{"question": "Approve plan?"}]},
                                    "tool_response": {"questions": [{"question": "Approve plan?"}],
                                                      "answers": {"Approve plan?": "Yes"}}})
        self.assertAllowed(p)
        ev = self.human_events()[-1]
        self.assertEqual(ev["type"], "ask_answer")
        self.assertIn("Yes", ev["excerpt"])
        self.assertEqual(len(ev["sha256"]), 64)
        self.assertRegex(ev["id"], r"^[0-9a-f]{32}$")

    def test_ask_user_question_without_answers_or_scripted(self):
        q = [{"question": "Approve plan?"}]
        for resp in ({"questions": q, "answers": {}}, {"questions": q}, None, ""):
            self.assertAllowed(self.hook("post-tool", {"tool_name": "AskUserQuestion", "tool_input": {
                "questions": q, "answers": {"Approve plan?": "Approve"}}, "tool_response": resp}))
        self.assertEqual(self.human_events(), [])
        self.assertAllowed(self.hook("post-tool", {"tool_name": "AskUserQuestion", "tool_input": {"questions": q},
                                                   "tool_response": {"questions": q, "answers": {"x": "Approve"}}},
                                     env={"SDLC_NONINTERACTIVE": "1"}))
        self.assertEqual(self.human_events(), [])

    def test_exit_plan_mode_recorded_and_usable(self):
        cid = self.new("bounded")
        self.fill_intent(cid)
        plan = ("# Fix status\n\n## Goal\nFix it.\n\n## Files that change\n- `src/status.py`\n\n## Order of work\n"
                "1. Fix\n\n## Risks\nLow.\n\n## Proof\n`pytest -q`\n\n## Rollback\nRevert.\n\n## Review focus\nNone.\n")
        p = self.hook("post-tool", {"tool_name": "ExitPlanMode", "tool_input": {"plan": plan},
                                    "tool_response": {"plan": plan, "filePath": "/x/plans/p.md"}})
        self.assertAllowed(p)
        out = json.loads(p.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn("plan-from-accepted %s" % cid, out["hookSpecificOutput"]["additionalContext"])
        self.assertEqual((self.root / ".claude/sdlc-state/accepted-plan.md").read_text(encoding="utf-8"), plan)
        self.assertEqual(self.human_events()[-1]["type"], "plan_accept")
        self.assertRegex(self.human_events()[-1]["id"], r"^[0-9a-f]{32}$")
        self.sdlc("plan-from-accepted", cid, "--by", "Eng One")
        self.assertAllowed(self.edit("src/status.py"))

    def test_post_edit_formatter_failure(self):
        self.set_config({"post_edit": [{"glob": "**/*.py", "run": "grep -q GOOD {file} || "
                                                                  "{ echo 'lint: missing GOOD marker'; exit 1; }"}]})
        payload = {"tool_name": "Edit", "tool_input": {"file_path": str(self.root / "src/status.py")},
                   "tool_response": {"filePath": str(self.root / "src/status.py")}}
        p = self.hook("post-tool", payload)
        self.assertEqual(p.returncode, 2)
        self.assertIn("lint: missing GOOD marker", p.stderr)
        (self.root / "src/status.py").write_text("# GOOD\n", encoding="utf-8")
        self.assertAllowed(self.hook("post-tool", payload))
        p = self.hook("post-tool", {"tool_name": "Write", "tool_input": {"file_path": str(self.root / "a.md")}})
        self.assertAllowed(p)

    def test_post_edit_path_with_spaces_is_quoted(self):
        self.set_config({"post_edit": [{"glob": "**/*.py", "run": "test -f {file}"}]})
        f = self.root / "src" / "my file.py"
        f.write_text("x = 1\n", encoding="utf-8")
        self.assertAllowed(self.hook("post-tool", {"tool_name": "Write", "tool_input": {"file_path": str(f)}}))

    def test_post_edit_file_name_cannot_inject(self):
        f = self.root / "src" / "x$(touch PWNED).py"
        f.write_text("x = 1\n", encoding="utf-8")
        for tpl in ('test -f "{file}"', "test -f {file}", "test -f '{file}'"):
            self.set_config({"post_edit": [{"glob": "**/*.py", "run": tpl}]})
            self.assertAllowed(self.hook("post-tool", {"tool_name": "Write", "tool_input": {"file_path": str(f)}}))
            self.assertFalse((self.root / "PWNED").exists(), tpl)

    def test_not_in_plan_reminder_once(self):
        cid = self.bounded_approved()

        def post(rel: str) -> subprocess.CompletedProcess:
            return self.hook("post-tool", {"tool_name": "Edit", "tool_input": {"file_path": str(self.root / rel)}})

        p = post("src/other.py")
        self.assertEqual(p.returncode, 2)
        self.assertIn("`src/other.py` is not listed in plan.md 'Files that change'", p.stderr)
        self.assertAllowed(post("src/other.py"))
        self.assertAllowed(post("src/status.py"))
        self.assertAllowed(post("changes/%s/plan.md" % cid))
        self.assertEqual(post("src/third.py").returncode, 2)
        rem = json.loads((self.root / ".claude/sdlc-state/reminded.json").read_text(encoding="utf-8"))
        self.assertEqual(rem[cid], ["src/other.py", "src/third.py"])


def tool_use(uid: str, name: str, **inp) -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": uid, "name": name, "input": inp}]}}


def tool_result(uid: str, is_error: bool = False) -> dict:
    b = {"type": "tool_result", "tool_use_id": uid, "content": "ok"}
    if is_error:
        b["is_error"] = True
    return {"type": "user", "message": {"role": "user", "content": [b]}}


class StopTest(Base):
    config = {"verify_commands": ["pytest", "make test"]}

    def setUp(self) -> None:
        super().setUp()
        self.cid = self.bounded_approved()
        self.tpath = self.root.parent / ("%s-transcript.jsonl" % self.root.name)

    def transcript(self, *records: dict, junk: bool = False) -> None:
        lines = [json.dumps(r) for r in records]
        if junk:
            lines.insert(0, '{"type": "tool_use" broken')
        self.tpath.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def stop(self, active: bool = False) -> subprocess.CompletedProcess:
        return self.hook("stop", {"transcript_path": str(self.tpath), "stop_hook_active": active})

    def src_edit(self, uid: str) -> dict:
        return tool_use(uid, "Edit", file_path=str(self.root / "src/status.py"), old_string="1", new_string="2")

    def test_block_when_edit_after_verify(self):
        self.transcript(tool_use("a", "Bash", command="pytest -q"), tool_result("a"),
                        self.src_edit("b"), tool_result("b"), junk=True)
        p = self.stop()
        self.assertAllowed(p)
        out = json.loads(p.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("Source files changed after the last verification run", out["reason"])
        self.assertIn("`pytest`, `make test`", out["reason"])
        self.assertEqual(self.hook_log()[-1]["decision"], "block")

    def test_no_block_when_verified_after_edit(self):
        self.transcript(self.src_edit("a"), tool_result("a"),
                        tool_use("b", "Bash", command="cd sub && FOO=1 make test 2>&1 | tail -5"), tool_result("b"))
        p = self.stop()
        self.assertAllowed(p)
        self.assertEqual(p.stdout.strip(), "")

    def test_ignored_edits_and_non_verify_commands(self):
        self.transcript(tool_use("a", "Bash", command="pytest"), tool_result("a"),
                        tool_use("b", "Write", file_path=str(self.root / "changes" / self.cid / "plan.md")),
                        tool_result("b"), self.src_edit("c"), tool_result("c", is_error=True),
                        tool_use("d", "Write", file_path="/elsewhere/notes.md"), tool_result("d"),
                        tool_use("e", "Bash", command="echo pytest"), tool_result("e"))
        self.assertEqual(self.stop().stdout.strip(), "")
        self.transcript(self.src_edit("a"), tool_result("a"), tool_use("b", "Bash", command="echo pytest"))
        self.assertIn('"block"', self.stop().stdout)

    def test_stop_hook_active_and_no_change(self):
        self.transcript(self.src_edit("a"), tool_result("a"))
        p = self.stop(active=True)
        self.assertAllowed(p)
        self.assertEqual(p.stdout.strip(), "")
        self.sdlc("deactivate")  # verify_on_stop applies with no active change too (trivial tier, skipped skill)
        self.assertIn('"block"', self.stop().stdout)

    def test_plan_guard_exempt_edits_need_no_verify(self):
        self.set_config({"verify_commands": ["pytest"], "plan_guard_exempt": ["docs/**"]})
        self.transcript(tool_use("a", "Write", file_path=str(self.root / "docs/x.md")), tool_result("a"))
        self.assertEqual(self.stop().stdout.strip(), "")

    def test_no_verify_commands_or_missing_transcript(self):
        self.transcript(self.src_edit("a"), tool_result("a"))
        self.set_config({"verify_commands": []})
        self.assertEqual(self.stop().stdout.strip(), "")
        self.set_config({"verify_commands": ["pytest"], "verify_on_stop": False})
        self.assertEqual(self.stop().stdout.strip(), "")
        self.set_config({"verify_commands": ["pytest"]})
        self.tpath.unlink()
        p = self.stop()
        self.assertAllowed(p)
        self.assertEqual(p.stdout.strip(), "")


class UserPromptTest(Base):
    def test_records_prompt_and_context(self):
        p = self.hook("user-prompt", {"prompt": "no active change yet"})
        self.assertAllowed(p)
        self.assertEqual(p.stdout.strip(), "")
        cid = self.new("standard")
        long = "yes, approve it. " + "x" * 1000
        p = self.hook("user-prompt", {"prompt": long})
        self.assertAllowed(p)
        out = json.loads(p.stdout)
        self.assertEqual(set(out), {"hookSpecificOutput"})
        hso = out["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "UserPromptSubmit")
        ctx = hso["additionalContext"]
        self.assertLessEqual(len(ctx), 600)
        self.assertIn(cid, ctx)
        self.assertIn("tier standard", ctx)
        self.assertIn("stage %s" % L.STAGE_LABEL["plan"], ctx)
        ev = self.human_events()
        self.assertEqual([e["type"] for e in ev], ["prompt", "prompt"])
        self.assertTrue(all(len(e["id"]) == 32 for e in ev))
        self.assertNotEqual(ev[0]["id"], ev[1]["id"])
        self.assertEqual(len(ev[-1]["excerpt"]), 300)
        self.assertTrue(ev[-1]["excerpt"].startswith("yes, approve it."))
        self.assertEqual(ev[-1]["session_id"], "s1")
        self.hook("user-prompt", {"prompt": "   "})
        self.assertEqual(len(self.human_events()), 2)

    def test_prompt_enables_approve_presence(self):
        cid = self.new("bounded")
        self.fill_intent(cid)
        self.write_plan(cid)
        self.sdlc("present", cid, "plan")
        time.sleep(1.1)  # human event must be strictly after GATE_PRESENTED (1 s resolution)
        self.hook("user-prompt", {"prompt": "Approved, go ahead"})
        self.sdlc("approve", cid, "plan", "--by", "Eng One")
        ev = L.audit_read(self.cdir(cid))[-1]
        self.assertEqual((ev["event"], ev["presence"], ev["evidence"]), ("GATE_APPROVED", "prompt", "Approved, go ahead"))


    def test_noninteractive_prompt_not_recorded(self):
        hpath = L.state_dir(self.root) / "human-events.jsonl"
        self.assertFalse(hpath.exists())
        p = self.hook("user-prompt", {"prompt": "A deterministic detector found a breach"},
                      env={"SDLC_NONINTERACTIVE": "1"})
        self.assertAllowed(p)
        # the file exists (hooks are installed) but holds no event, so approve cannot fall back
        # to --evidence with presence=unverified
        self.assertTrue(hpath.exists())
        self.assertEqual(self.human_events(), [])
        self.hook("user-prompt", {"prompt": "typed by a person"}, env={"SDLC_NONINTERACTIVE": "0"})
        self.assertEqual(len(self.human_events()), 1)


    def test_task_notification_not_recorded(self):
        cid = self.new("bounded")
        self.fill_intent(cid)
        self.write_plan(cid)
        self.hook("user-prompt", {"prompt": "earlier human prompt"})
        self.sdlc("present", cid, "plan")
        time.sleep(1.1)
        n = len(self.human_events())
        p = self.hook("user-prompt", {"prompt": "<task-notification>\n<task-id>b1</task-id>\n"
                                                "<status>completed</status>\n</task-notification>"})
        self.assertAllowed(p)
        self.assertIn(cid, json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"])  # context still added
        self.assertEqual(len(self.human_events()), n)
        self.assertEqual(self.hook_log()[-1]["decision"], "skip")
        self.hook("user-prompt", {"prompt": '  <task-notification id="t2">done</task-notification>'})  # attributes
        self.assertEqual(len(self.human_events()), n)
        self.sdlc("approve", cid, "plan", "--by", "Eng One", code=1)  # no human turn after the presentation


class HooksSelfRunTest(Base):
    def test_running_hooks_py_denied(self):
        for cmd in ('echo \'{"prompt":"approve"}\' | python3 .claude/skills/ai-native-sdlc/scripts/hooks.py user-prompt',
                    'python3 "${CLAUDE_SKILL_DIR}/scripts/hooks.py" stop < t.json',
                    'cd %s && python3 hooks.py post-tool' % HOOKS.parent,
                    'bash -c "python3 x/scripts/hooks.py pre-tool"', 'python3 - < scripts/hooks.py'):
            self.assertDenied(self.bash(cmd), "hooks.py is run by Claude Code, not by the agent")
        p = self.hook("pre-tool", {"tool_name": "PowerShell",
                                   "tool_input": {"command": "py .claude\\skills\\s\\scripts\\hooks.py stop"}})
        self.assertDenied(p, "would forge hook evidence")
        for cmd in ("cat scripts/hooks.py", "git diff -- scripts/hooks.py", "python3 -m py_compile scripts/hooks.py",
                    'grep -n "hooks.py stop" README.md'):
            self.assertAllowed(self.bash(cmd))


class WorktreeTest(Base):
    def setUp(self) -> None:
        super().setUp()
        self.cid = self.new("bounded")
        self.wt = Path(tempfile.mkdtemp(prefix="hooks-wt-")).resolve() / "wt"

    def wt_edit(self, rel: str) -> subprocess.CompletedProcess:
        data = {"session_id": "s1", "cwd": str(self.wt), "tool_name": "Edit",
                "tool_input": {"file_path": str(self.wt / rel)}}
        return subprocess.run([sys.executable, str(HOOKS), "pre-tool"], input=json.dumps(data), cwd=str(self.wt),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

    def test_inherits_main_checkout_change(self):
        self.git("add", ".")
        self.git("commit", "-qm", "artifacts")
        self.git("worktree", "add", "-q", str(self.wt), "-b", "wt")
        self.assertDenied(self.wt_edit("src/status.py"), "plan guard")  # no activate run in the worktree
        self.assertFalse((self.wt / ".claude/sdlc-state/active.json").exists())  # read-only inheritance

    def test_missing_change_folder_denies(self):
        self.git("worktree", "add", "-q", str(self.wt), "-b", "wt")  # artifact commit not on this branch
        p = self.wt_edit("src/status.py")
        self.assertDenied(p, "has no changes/%s/ folder on this branch" % self.cid)


class UnitTest(unittest.TestCase):
    def test_is_verify(self):
        v = ["pytest", "make test"]
        for cmd in ("pytest", "pytest -q tests", "cd /r && pytest", "CI=1 make test", "ruff check . && pytest -x",
                    "(cd x; make test)"):
            self.assertTrue(H._is_verify(cmd, v), cmd)
        for cmd in ("echo pytest", "pytesting", "make tests", "cat Makefile"):
            self.assertFalse(H._is_verify(cmd, v), cmd)

    def test_gate_text(self):
        pats = [re.compile(x, re.I) for x in L.DEFAULT_CONFIG["production_gate"]["bash_patterns"]]
        gated = lambda c: any(x.search(H._gate_text(c)) for x in pats)  # noqa: E731
        for cmd in ('git commit -m "deploy to prod docs"', 'cd r && git commit -qm "deploy production fix"',
                    'git log --oneline | grep prod-deploy | head', 'rg -n "terraform apply"',
                    'git commit -m "deploy prod $(date +%F)"', 'git commit -m "deploy prod" && git push'):
            self.assertFalse(gated(cmd), cmd)
        for cmd in ("make deploy ENV=production", "export ENV=prod && ./deploy.sh", "cd /srv/prod && ./deploy.sh",
                    "echo prod | ./deploy.sh", 'echo "kubectl apply -n prod" | sh', "grep -q prod cfg && ./deploy.sh",
                    'echo "kubectl apply -n prod -f k" > x.sh; ./x.sh', 'git commit -m "deploy prod" && ./run.sh',
                    'git commit -m "kubectl apply -n prod" && git log -1 --format=%B | sh',
                    'git commit -m "$(kubectl apply -n prod -f k.yaml)"', "echo `kubectl apply -n prod -f k`",
                    'bash -c "kubectl apply -n prod -f k"', "cat <<EOF | bash\nkubectl apply -n prod -f k\nEOF",
                    "cat <<EOF\n$(kubectl apply -n prod -f k)\nEOF", 'X="kubectl apply -n prod"; $X',
                    'printf -v X "kubectl apply -n prod"; $X', "(echo prod) | ./deploy.sh",
                    'LESSOPEN="|kubectl apply -n prod" cat x', 'git commit -m "x"; terraform apply',
                    'echo "unterminated deploy prod', 'git -c alias.x="!kubectl apply -n prod" x',
                    "echo $(case a in a) kubectl apply -n prod;; esac)", 'sudo git commit -m "deploy prod"'):
            self.assertTrue(gated(cmd), cmd)

    def test_write_targets(self):
        base = Path("/r")
        self.assertEqual(H._write_targets("pytest 2>&1 | tee out.log", base), [("out.log", base)])
        self.assertEqual(H._write_targets("cat x >&2; echo 'a > b'", base), [])
        self.assertEqual(H._write_targets("dd if=a of=t/x", base), [("t/x", base)])
        self.assertEqual(H._write_targets("git checkout main", base), [])
        self.assertEqual(H._write_targets("cd t && sed -i s/a/b/ x.py", base)[-1], ("x.py", Path("/r/t")))
        w = lambda c: [t for t, _ in H._write_targets(c, base)]  # noqa: E731
        self.assertEqual(w("git checkout HEAD~1 tests/x.py"), ["tests/x.py"])
        self.assertEqual(w("git checkout -b x origin/main"), [])
        self.assertEqual(w("git stash"), [])
        self.assertEqual(w("git stash push -m wip"), [])
        self.assertEqual(w("git reset --hard"), [])
        self.assertTrue(w("git stash pop")[0].startswith(H.REVISION))
        self.assertEqual(w("touch a b"), ["a", "b"])
        self.assertEqual(w("cp -t dst/ s1 s2"), ["dst/"])
        self.assertEqual(w("cp --target-directory=dst s1"), ["dst"])
        self.assertEqual(w("rsync -a src/ dst/"), ["dst/"])
        self.assertEqual(w("ln -s a b"), ["b"])
        self.assertEqual(w("ln a b"), ["a", "b"])
        self.assertEqual(w("Set-Content -Path a.txt -Value hi"), ["a.txt"])
        self.assertEqual(w("Copy-Item a.txt b.txt"), ["b.txt"])


if __name__ == "__main__":
    unittest.main()
