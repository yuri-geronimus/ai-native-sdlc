"""CLI tests for scripts/sdlc.py. Each test builds a temp git repo; assets come from SKILL_DIR.

Run: python3 -m unittest discover -s tests -t .
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
SDLC = SKILL / "scripts" / "sdlc.py"
sys.path.insert(0, str(SKILL / "scripts"))
import sdlc_lib as L  # noqa: E402

INTENT_FILL = {
    "Problem": "Customers phone the contact center to ask where their claim is.",
    "Proposed outcome": "Customers see claim status, next step and expected date in the portal.",
    "Affected users and systems": "Claims handlers, portal team, claims-core API.",
    "Constraints": "No new PII in the portal session.",
    "Assumptions": "None",
    "Open questions": "Do third-party loss adjusters need access too?",
}

SPEC = """---
id: {id}
title: Claims status
status: draft
intent_sha: {intent_sha}
skills:
  - security-policy@2.3
risk: {risk}
---
# Spec: Claims status

## Summary
Show claim status in the portal.

## Requirements
- FR-001 Status lookup. WHEN an authenticated customer opens a claim THE SYSTEM SHALL show its status.
- FR-002 Ownership. IF the claim is not the caller's THEN THE SYSTEM SHALL respond 404.
{extra_req}

## Success criteria
- SC-001 p95 response under 300 ms.

## Design
New endpoint GET /claims/{{id}}/status behind existing auth.

## Policy conformance
| Policy / skill | Status | Notes |
|---|---|---|
| security-policy | compliant | existing auth |

## Flagged concerns
{concerns}

## Out of scope
Adjuster access.

## Open questions
None

## Clarifications

## Change log
"""

PLAN = """---
id: {id}
title: Claims status
status: draft
spec_sha: "{spec_sha}"
intent_sha: "{intent_sha}"
---
# Plan: Claims status

## Goal
Ship FR-001 and FR-002 within SC-001.

## Files that change
- `src/status.py`
- `tests/test_status.py` (new)

## Order of work
1. Failing tests for FR-001, FR-002
2. Endpoint (FR-001) [P]

## Risks
Upstream rate limit 50 rps.

## Proof
`make test` covers SC-001.

## Rollback
Revert the commit.

## Review focus
Ownership check.

## Change log
"""


def setUpModule() -> None:
    cfg = Path(tempfile.mkdtemp(prefix="sdlc-gitcfg-")) / "gitconfig"
    cfg.write_text("", encoding="utf-8")
    os.environ["GIT_CONFIG_GLOBAL"] = str(cfg)
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
    os.environ.pop("CI", None)
    os.environ.pop("SDLC_NONINTERACTIVE", None)  # the suite tests interactive behaviour unless a test sets it


def iso(delta_s: int = 0) -> str:
    t = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=delta_s)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class Repo(unittest.TestCase):
    """Base: a fresh git repo per test with helpers to drive sdlc.py."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="sdlc-cli-")).resolve()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Test Engineer")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "commit.gpgsign", "false")
        (self.root / "README.md").write_text("repo\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-qm", "init")

    # helpers -------------------------------------------------------------
    def git(self, *args: str, env: dict = None) -> str:
        e = dict(os.environ)
        e.update(env or {})
        return subprocess.run(["git", "-C", str(self.root)] + list(args), check=True, env=e,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True).stdout

    def run_cli(self, *args: str, env: dict = None, cwd: Path = None, code: int = None):
        e = {k: v for k, v in os.environ.items() if k not in ("CI", "SDLC_NONINTERACTIVE")}
        e.update(env or {})
        p = subprocess.run([sys.executable, str(SDLC)] + list(args), cwd=str(cwd or self.root), env=e,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        if code is not None:
            self.assertEqual(p.returncode, code, "rc=%d\nstdout:\n%s\nstderr:\n%s" % (p.returncode, p.stdout, p.stderr))
        return p

    def new(self, slug: str = "claims-status", tier: str = "standard", *extra: str) -> str:
        p = self.run_cli("new", slug, "--title", "Claims status", "--tier", tier, "--json", *extra, code=0)
        return json.loads(p.stdout)["id"]

    def cdir(self, cid: str) -> Path:
        return self.root / "changes" / cid

    def fill_intent(self, cid: str, fill: dict = None) -> None:
        p = self.cdir(cid) / "intent.md"
        text = p.read_text(encoding="utf-8")
        for k, v in (fill or INTENT_FILL).items():
            text = re.sub(r"(## %s\n)(<!--.*?-->\n)" % re.escape(k), lambda m: m.group(1) + v + "\n", text, flags=re.S)
        p.write_text(text, encoding="utf-8")

    def write_spec(self, cid: str, risk: str = "medium", concerns: str = "None", extra_req: str = "") -> None:
        sha = L.file_fingerprint(self.cdir(cid) / "intent.md")
        (self.cdir(cid) / "spec.md").write_text(
            SPEC.format(id=cid, intent_sha=sha, risk=risk, concerns=concerns, extra_req=extra_req), encoding="utf-8")

    def write_plan(self, cid: str) -> None:
        d = self.cdir(cid)
        (d / "plan.md").write_text(PLAN.format(id=cid, spec_sha=L.file_fingerprint(d / "spec.md") or "",
                                               intent_sha=L.file_fingerprint(d / "intent.md")), encoding="utf-8")

    def human(self, delta_s: int, excerpt: str = "yes, approve it", kind: str = "prompt", **extra) -> None:
        """Append a human event. Positive deltas are capped at 2 s: enough to sort after a presentation
        recorded this second, inside approve's 5 s future limit. File order still orders the events."""
        p = L.state_dir(self.root) / "human-events.jsonl"
        rec = {"ts": iso(min(delta_s, 2)), "type": kind, "session_id": "s1", "sha256": "f" * 64, "excerpt": excerpt}
        rec.update(extra)
        with open(str(p), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({k: v for k, v in rec.items() if v is not ...}) + "\n")

    def approve(self, cid: str, gate: str, role: str = None) -> None:
        self.run_cli("present", cid, gate, code=0)
        args = ["approve", cid, gate, "--by", "Pat Owner", "--evidence", "approved in review"]
        if role:
            args += ["--role", role]
        self.run_cli(*args, code=0)

    def status(self, cid: str) -> dict:
        return json.loads(self.run_cli("status", cid, "--json", code=0).stdout)

    def events(self, cid: str) -> list:
        return L.audit_read(self.cdir(cid))


class ImportTest(unittest.TestCase):
    def test_importable_without_side_effects(self):
        spec = importlib.util.spec_from_file_location("sdlc_cli_under_test", str(SDLC))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertTrue(callable(mod.main))
        self.assertIn("approve", mod.COMMANDS)
        # skill inside the repo -> hook paths are relative to $CLAUDE_PROJECT_DIR
        self.assertEqual(mod._skill_ref(SKILL.parent), "$CLAUDE_PROJECT_DIR/" + SKILL.name)
        self.assertEqual(mod._skill_ref(Path(tempfile.mkdtemp()).resolve()), SKILL.as_posix())


class NewTest(Repo):
    def test_new_creates_intent_audit_and_activates(self):
        cid = self.new()
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
        self.assertEqual(cid, today + "-claims-status")
        meta, body = L.parse_frontmatter((self.cdir(cid) / "intent.md").read_text(encoding="utf-8"))
        self.assertEqual(meta["id"], cid)
        self.assertEqual(meta["author"], "Test Engineer")
        self.assertEqual(meta["status"], "draft")
        self.assertEqual(meta["tier"], "standard")
        self.assertEqual(meta["source"], "human")
        self.assertIn("# Intent: Claims status", body)
        self.assertNotIn("{{", body)
        ev = self.events(cid)
        self.assertEqual(ev[0]["event"], "CHANGE_CREATED")
        self.assertEqual(L.get_active(self.root), cid)

    def test_collision_record_id_and_slug(self):
        a = self.new("Fix: The Thing!!")
        b = self.new("fix the thing", "standard", "--no-activate")
        self.assertTrue(a.endswith("-fix-the-thing"))
        self.assertEqual(b, a + "-2")
        self.assertEqual(L.get_active(self.root), a)
        c = self.new("x", "bounded", "--record-id", "EV-123")
        self.assertEqual(c, "EV-123-x")
        self.run_cli("new", "!!!", code=2)

    def test_trivial_refused(self):
        p = self.run_cli("new", "typo", "--tier", "trivial", code=1)
        self.assertIn("trivial changes need no change folder", p.stderr)
        self.assertFalse((self.root / "changes").exists())

    def test_record_id_pattern(self):
        (self.root / ".claude").mkdir()
        (self.root / ".claude/sdlc.json").write_text(json.dumps({"record_id_pattern": "EV-[0-9]+"}), encoding="utf-8")
        self.run_cli("new", "x", "--record-id", "JIRA-1", code=1)
        self.run_cli("new", "x", "--record-id", "EV-9", code=0)

    def test_bug_template(self):
        cid = self.new("crash", "bounded", "--bug")
        secs = L.sections((self.cdir(cid) / "intent.md").read_text(encoding="utf-8"))
        for h in ("current behavior", "expected behavior", "unchanged behavior", "evidence",
                  "affected users and systems", "constraints", "open questions", "change log"):
            self.assertIn(h, secs)

    def test_from_json(self):
        diag = {"title": "CI failure rate above 3 sigma", "problem": "Test failure rate jumped to 14% after the runner upgrade.",
                "evidence": ["WE1 hit at 2026-10-08T10:00Z, z=3.4", "run 123 failed in test_net"],
                "proposed_outcome": "Failure rate back inside the 1-sigma band.",
                "affected_systems": ["ci", "runner-image"], "constraints": ["no production access"],
                "open_questions": [], "tier": "bounded", "severity": "high"}
        f = self.root / "diag.json"
        f.write_text(json.dumps(diag), encoding="utf-8")
        p = self.run_cli("new", "ci-failures", "--from-json", str(f), "--no-activate", "--json", code=0)
        cid = json.loads(p.stdout)["id"]
        text = (self.cdir(cid) / "intent.md").read_text(encoding="utf-8")
        meta, body = L.parse_frontmatter(text)
        self.assertEqual(meta["tier"], "bounded")
        self.assertEqual(meta["source"], "incident")
        self.assertEqual(meta["title"], diag["title"])
        secs = L.sections(body)
        self.assertIn("z=3.4", secs["problem"])
        self.assertIn("- runner-image", secs["affected users and systems"])
        self.assertEqual(secs["open questions"], "None")
        self.assertIsNone(L.get_active(self.root))
        self.run_cli("validate", cid, "intent", "--strict", code=0)
        bad = dict(diag, extra=1, tier="trivial")
        f.write_text(json.dumps(bad), encoding="utf-8")
        p = self.run_cli("new", "bad", "--from-json", str(f), code=1)
        self.assertIn("unexpected property", p.stderr)


class ValidateTest(Repo):
    def test_template_intent_fails_then_passes(self):
        cid = self.new()
        p = self.run_cli("validate", cid, code=1)
        self.assertIn("[Problem]: section is empty", p.stdout)
        self.fill_intent(cid)
        self.run_cli("validate", cid, "intent", "--strict", code=0)

    def test_placeholder_and_tier_errors(self):
        cid = self.new()
        self.fill_intent(cid)
        p = self.cdir(cid) / "intent.md"
        p.write_text(p.read_text(encoding="utf-8").replace("No new PII", "{{constraint}}"), encoding="utf-8")
        self.assertIn("unfilled template placeholders", self.run_cli("validate", cid, code=1).stdout)
        p.write_text(p.read_text(encoding="utf-8").replace("{{constraint}}", "x").replace("tier: standard", "tier: trivial"),
                     encoding="utf-8")
        self.assertIn("tier", self.run_cli("validate", cid, code=1).stdout)

    def test_long_problem_warns(self):
        cid = self.new()
        self.fill_intent(cid, dict(INTENT_FILL, Problem="word " * 300))
        p = self.run_cli("validate", cid, code=0)
        self.assertIn("300 words", p.stdout)

    def test_spec_rules(self):
        cid = self.new()
        self.fill_intent(cid)
        self.write_spec(cid)
        self.run_cli("validate", cid, "spec", "--strict", code=0)
        # one clarification marker: warn normally, error strict
        self.write_spec(cid, extra_req="- FR-003 [NEEDS CLARIFICATION: retention?] THE SYSTEM SHALL keep data.")
        self.run_cli("validate", cid, "spec", code=0)
        self.run_cli("validate", cid, "spec", "--strict", code=1)
        many = "\n".join("- FR-00%d [NEEDS CLARIFICATION: q%d] THE SYSTEM SHALL x." % (i, i) for i in range(3, 7))
        self.write_spec(cid, extra_req=many)
        self.assertIn("max 3", self.run_cli("validate", cid, "spec", code=1).stdout)
        # open concern: warn normally, error strict; invalid status always error
        table = ("| ID | Concern | Policy | Owner | Status | Resolution |\n|---|---|---|---|---|---|\n"
                 "| C1 | cache vs retention | data-retention | Privacy lead | %s | |")
        self.write_spec(cid, concerns=table % "open")
        self.run_cli("validate", cid, "spec", code=0)
        self.run_cli("validate", cid, "spec", "--strict", code=1)
        self.write_spec(cid, concerns=table % "maybe")
        self.run_cli("validate", cid, "spec", code=1)
        self.write_spec(cid, concerns=table % "accepted")
        self.run_cli("validate", cid, "spec", "--strict", code=0)
        # no FR ids
        p = self.cdir(cid) / "spec.md"
        p.write_text(re.sub(r"FR-00\d", "REQ", p.read_text(encoding="utf-8")), encoding="utf-8")
        self.assertIn("no FR-### requirement IDs", self.run_cli("validate", cid, "spec", code=1).stdout)

    def test_plan_rules(self):
        cid = self.new()
        self.fill_intent(cid)
        self.write_spec(cid)
        self.write_plan(cid)
        self.run_cli("validate", cid, "plan", "--strict", code=0)
        p = self.cdir(cid) / "plan.md"
        text = p.read_text(encoding="utf-8")
        p.write_text(text.replace("- `src/status.py`\n- `tests/test_status.py` (new)", "Some files."), encoding="utf-8")
        self.assertIn("no file paths found", self.run_cli("validate", cid, "plan", code=1).stdout)
        p.write_text(text.replace("Upstream rate limit 50 rps.", ""), encoding="utf-8")
        self.run_cli("validate", cid, "plan", code=0)
        self.run_cli("validate", cid, "plan", "--strict", code=1)


class GateTest(Repo):
    def test_present_requires_valid_artifact(self):
        cid = self.new()
        self.run_cli("present", cid, "intent", code=1)
        self.assertFalse(any(e["event"] == "GATE_PRESENTED" for e in self.events(cid)))

    def test_approve_without_human_events_needs_evidence(self):
        cid = self.new()
        self.fill_intent(cid)
        self.run_cli("approve", cid, "intent", "--by", "Pat", "--evidence", "ok", code=1)  # not presented
        p = self.run_cli("present", cid, "intent", code=0)
        self.assertIn("approve %s intent --by" % cid, p.stdout)
        self.assertIn("product-owner", p.stdout)
        self.run_cli("approve", cid, "intent", "--by", "Pat", code=1)
        self.run_cli("approve", cid, "intent", "--by", "Claude", "--evidence", "ok", code=1)
        self.run_cli("approve", cid, "intent", "--by", "Pat Owner", "--evidence", "looks right | go", code=0)
        ev = self.events(cid)[-1]
        self.assertEqual(ev["event"], "GATE_APPROVED")
        self.assertEqual(ev["presence"], "unverified")
        self.assertEqual(ev["evidence"], "looks right / go")
        self.assertEqual(ev["sha256"], L.file_fingerprint(self.cdir(cid) / "intent.md"))
        meta = L.read_meta(self.cdir(cid) / "intent.md")
        self.assertEqual((meta["status"], meta["approved_by"]), ("approved", "Pat Owner"))
        st = self.status(cid)
        self.assertTrue(st["gates"]["intent"]["current"])
        self.assertEqual(st["stage"], "design")

    def test_approve_with_human_events(self):
        cid = self.new()
        self.fill_intent(cid)
        self.human(-3600, "earlier prompt")
        self.run_cli("present", cid, "intent", code=0)
        p = self.run_cli("approve", cid, "intent", "--by", "Pat Owner", code=1)
        self.assertIn("no human turn recorded", p.stderr)
        self.human(5, "yes, approve the intent", kind="ask_answer")
        self.run_cli("approve", cid, "intent", "--by", "Pat Owner", code=0)
        ev = self.events(cid)[-1]
        self.assertEqual(ev["presence"], "ask_answer")
        self.assertEqual(ev["evidence"], "yes, approve the intent")
        self.assertEqual(ev["human_sha256"], "f" * 64)

    def test_ci_refusal(self):
        cid = self.new()
        self.fill_intent(cid)
        self.run_cli("present", cid, "intent", code=0)
        p = self.run_cli("approve", cid, "intent", "--by", "Pat", "--evidence", "ok", env={"CI": "true"}, code=1)
        self.assertIn("PR review", p.stderr)
        p = self.run_cli("approve", cid, "intent", "--by", "Pat", "--evidence", "ok",
                         env={"SDLC_NONINTERACTIVE": "1"}, code=1)  # headless without hooks: still refused
        self.assertIn("SDLC_NONINTERACTIVE is set", p.stderr)
        self.run_cli("approve", cid, "intent", "--by", "Pat", "--evidence", "ok", "--allow-ci", env={"CI": "true"}, code=0)

    def test_new_prints_branch_and_next_gate(self):
        p = self.run_cli("new", "agent-fix", "--tier", "bounded", "--bug", "--source", "ticket", code=0)
        cid = re.search(r"^id: (\S+)", p.stdout, re.M).group(1)
        self.assertIn("git switch -c change/%s" % cid, p.stdout)
        self.assertIn("present %s intent" % cid, p.stdout)  # agent-written bounded intent carries the intent gate
        self.git("checkout", "-qb", "change/own-fix")
        p = self.run_cli("new", "own-fix", "--tier", "bounded", "--bug", code=0)
        cid = re.search(r"^id: (\S+)", p.stdout, re.M).group(1)
        self.assertIn("git branch -m change/%s" % cid, p.stdout)
        self.assertIn("T1 = a failing test", p.stdout)
        self.assertIn("present %s plan" % cid, p.stdout)

    def test_stale_after_edit_and_change_log_edit_keeps_approval(self):
        cid = self.new()
        self.fill_intent(cid)
        self.approve(cid, "intent")
        p = self.cdir(cid) / "intent.md"
        p.write_text(p.read_text(encoding="utf-8") + "- 2026-10-08 clarified wording — reviewer ask\n", encoding="utf-8")
        self.assertTrue(self.status(cid)["gates"]["intent"]["current"])
        p.write_text(p.read_text(encoding="utf-8").replace("No new PII", "No new personal data"), encoding="utf-8")
        st = self.status(cid)
        self.assertFalse(st["gates"]["intent"]["current"])
        self.assertTrue(st["gates"]["intent"]["approved"])
        self.assertIn("changed after approval", st["next"])
        # the old presentation no longer matches the edited artifact
        p2 = self.run_cli("approve", cid, "intent", "--by", "Pat", "--evidence", "ok", code=1)
        self.assertIn("changed since it was presented", p2.stderr)
        self.approve(cid, "intent")
        self.assertTrue(self.status(cid)["gates"]["intent"]["current"])

    def test_gate_order_and_full_standard_flow(self):
        cid = self.new()
        self.fill_intent(cid)
        self.write_spec(cid)
        p = self.run_cli("present", cid, "spec", code=1)  # upstream gates first: nothing recorded
        self.assertIn("earlier gates", p.stdout)
        self.assertFalse(any(e["event"] == "GATE_PRESENTED" and e.get("gate") == "spec" for e in self.events(cid)))
        p = self.run_cli("approve", cid, "spec", "--by", "Pat", "--evidence", "ok", code=1)
        self.assertIn("earlier gates", p.stderr)
        self.approve(cid, "intent")
        self.write_spec(cid)  # refresh intent_sha after approval (frontmatter unchanged → same)
        self.approve(cid, "spec")
        self.write_plan(cid)
        self.approve(cid, "plan")
        st = self.status(cid)
        self.assertEqual(st["stage"], "build")
        self.assertIn("PR_OPENED", st["next"])
        self.run_cli("log", cid, "PR_OPENED", "url=https://example.com/pr/1", code=0)
        self.assertEqual(self.status(cid)["stage"], "review")
        self.run_cli("close", cid, "--pr", "https://example.com/pr/1", code=0)
        st = self.status(cid)
        self.assertEqual((st["stage"], st["status"]), ("closed", "done"))
        self.assertIsNone(L.get_active(self.root))

    def test_high_risk_role(self):
        cid = self.new("auth", "high-risk")
        self.fill_intent(cid)
        self.approve(cid, "intent")
        self.write_spec(cid, risk="high")
        card = self.run_cli("present", cid, "spec", code=0).stdout
        self.assertIn("--role product-owner", card)
        self.assertIn("--role tech-lead", card)
        p = self.run_cli("approve", cid, "spec", "--by", "Dee Signer", "--role", "designer", "--evidence", "ok", code=1)
        self.assertIn("tech-lead", p.stderr)
        # the product owner decides; the gate stays open until a tech lead or architect also approves
        p = self.run_cli("approve", cid, "spec", "--by", "Pat Owner", "--evidence", "ok", code=0)
        self.assertIn("still needs a tech-lead or architect", p.stdout)
        st = self.status(cid)["gates"]["spec"]
        self.assertFalse(st["current"])
        self.assertEqual((st["approved_roles"], st["missing_roles"]), (["product-owner"], ["tech-lead or architect"]))
        self.assertIn("tech-lead", self.status(cid)["next"])
        p = self.run_cli("approve", cid, "spec", "--by", "Pat Owner", "--evidence", "again", code=1)
        self.assertIn("already approved by a product-owner", p.stderr)
        self.run_cli("approve", cid, "spec", "--by", "Tess Lead", "--role", "tech-lead", "--evidence", "ok", code=0)
        st = self.status(cid)["gates"]["spec"]
        self.assertTrue(st["current"])
        self.assertEqual(st["approved_roles"], ["product-owner", "tech-lead"])
        # a tech-lead alone (no product owner) is not enough either
        spec = self.cdir(cid) / "spec.md"
        spec.write_text(spec.read_text(encoding="utf-8").replace("behind existing auth", "behind the auth proxy"),
                        encoding="utf-8")
        self.run_cli("present", cid, "spec", code=0)
        self.run_cli("approve", cid, "spec", "--by", "Tess Lead", "--role", "tech-lead", "--evidence", "ok", code=0)
        st = self.status(cid)["gates"]["spec"]
        self.assertEqual((st["current"], st["missing_roles"]), (False, ["product-owner"]))

    def test_raise_to_high_risk_requires_senior_reapproval(self):
        cid = self.new("auth-later", "standard")
        self.fill_intent(cid)
        self.approve(cid, "intent")
        self.write_spec(cid)
        self.approve(cid, "spec")
        self.run_cli("raise-tier", cid, "high-risk", "--reason", "touches auth", code=0)
        st = self.status(cid)
        self.assertTrue(st["gates"]["spec"]["approved"])
        self.assertFalse(st["gates"]["spec"]["current"])
        self.assertFalse(st["gates"]["spec"]["role_ok"])
        self.assertIn("tech-lead", st["next"])
        # each approval needs its own human turn, newer than the earlier decision
        self.human(-100, "old turn")
        p = self.run_cli("approve", cid, "spec", "--by", "Tess Lead", "--role", "tech-lead", code=1)
        self.assertIn("last decided", p.stderr)
        self.human(2, "approved as tech lead")
        self.run_cli("approve", cid, "spec", "--by", "Tess Lead", "--role", "tech-lead", code=0)
        st = self.status(cid)
        self.assertTrue(st["gates"]["spec"]["current"])
        self.assertTrue(st["gates"]["spec"]["role_ok"])

    def test_reject_intent_drops_change(self):
        cid = self.new()
        self.fill_intent(cid)
        self.run_cli("reject", cid, "intent", "--by", "Pat", "--reason", "not now", code=0)
        st = self.status(cid)
        self.assertEqual(st["stage"], "dropped")
        self.assertTrue(st["gates"]["intent"]["rejected"])
        self.assertIsNone(L.get_active(self.root))

    def test_bounded_requires_only_plan(self):
        cid = self.new("small", "bounded")
        st = self.status(cid)
        self.assertEqual(st["required_gates"], ["plan"])
        self.assertIn("design", st["skipped"])
        self.fill_intent(cid)
        self.write_plan(cid)
        self.approve(cid, "plan")
        self.assertEqual(self.status(cid)["stage"], "build")


class TierSkipLogTest(Repo):
    def test_raise_tier_ratchet(self):
        cid = self.new("x", "bounded")
        self.run_cli("raise-tier", cid, "bounded", "--reason", "same", code=1)
        self.run_cli("raise-tier", cid, "standard", "--reason", "touches auth", code=0)
        self.run_cli("raise-tier", cid, "bounded", "--reason", "lower", code=1)
        st = self.status(cid)
        self.assertEqual(st["tier"], "standard")
        self.assertEqual(st["required_gates"], ["intent", "spec", "plan"])
        self.assertEqual(L.read_meta(self.cdir(cid) / "intent.md")["tier"], "standard")
        ev = [e for e in self.events(cid) if e["event"] == "TIER_RAISED"][0]
        self.assertEqual((ev["from"], ev["to"]), ("bounded", "standard"))
        # hand-editing the frontmatter down does not lower the effective tier
        L.update_frontmatter(self.cdir(cid) / "intent.md", tier="bounded")
        self.assertEqual(self.status(cid)["tier"], "standard")

    def test_skip(self):
        cid = self.new()
        self.run_cli("skip", cid, "design", "--reason", "obvious", code=1)
        self.run_cli("skip", cid, "maintain", "--reason", "internal tool, no metrics", code=0)
        self.assertEqual(self.status(cid)["skipped"], ["maintain"])
        self.run_cli("skip", cid, "nonsense", "--reason", "x", code=2)

    def test_log(self):
        cid = self.new()
        self.run_cli("log", cid, "GATE_APPROVED", "gate=plan", code=1)
        self.run_cli("log", cid, "CHANGE_CLOSED", code=1)
        self.run_cli("log", cid, "RULING", "--message", "used cache — rate limit — stale reads", code=0)
        self.run_cli("log", cid, "NOTE", "badkey", code=2)
        self.run_cli("log", cid, "PR_OPENED", code=2)
        self.assertEqual(self.events(cid)[-1]["message"], "used cache — rate limit — stale reads")

    def test_activate_deactivate(self):
        cid = self.new("a", "standard", "--no-activate")
        self.run_cli("activate", cid, code=0)
        self.assertEqual(L.get_active(self.root), cid)
        self.run_cli("deactivate", code=0)
        self.assertIsNone(L.get_active(self.root))
        self.run_cli("activate", "nope", code=2)


class PlanFromAcceptedTest(Repo):
    def test_bounded_plan_mode_acceptance(self):
        cid = self.new("small", "bounded")
        self.fill_intent(cid)
        self.run_cli("plan-from-accepted", cid, code=1)
        accepted = L.state_dir(self.root) / "accepted-plan.md"
        accepted.write_text("# Fix the status endpoint\n\n## Files that change\n- `src/status.py`\n\n"
                            "## Order of work\n1. Fix it\n\n## Proof\n`make test`\n\n## Risks\nLow.\n", encoding="utf-8")
        p = self.run_cli("plan-from-accepted", cid, "--by", "Eng One", code=0)
        self.assertIn("WARN", p.stdout)
        text = (self.cdir(cid) / "plan.md").read_text(encoding="utf-8")
        secs = L.sections(L.parse_frontmatter(text)[1])
        self.assertEqual(secs["goal"], "_Not covered in plan mode — fill in_")
        self.assertIn("change log", secs)
        self.assertEqual(list(secs)[-1], "change log")
        ev = self.events(cid)[-1]
        self.assertEqual((ev["event"], ev["presence"], ev["by"]), ("GATE_APPROVED", "plan-mode", "Eng One"))
        self.assertTrue(ev["evidence"].startswith("accepted in plan mode at"))
        self.assertTrue(self.status(cid)["gates"]["plan"]["current"])

    def test_by_is_required(self):
        self.git("config", "user.name", "Git Person")
        cid = self.new("small", "bounded")
        self.fill_intent(cid)
        (L.state_dir(self.root) / "accepted-plan.md").write_text(
            "## Files that change\n- `a.py`\n\n## Order of work\n1. Fix it\n", encoding="utf-8")
        p = self.run_cli("plan-from-accepted", cid, code=1)
        self.assertIn("never git config", p.stderr)
        self.assertFalse((self.cdir(cid) / "plan.md").exists())
        self.assertFalse(any(e["event"] == "GATE_APPROVED" for e in self.events(cid)))

    def test_old_accepted_plan_refused(self):
        cid = self.new("small", "bounded")
        accepted = L.state_dir(self.root) / "accepted-plan.md"
        accepted.write_text("## Files that change\n- `a.py`\n", encoding="utf-8")
        old = (dt.datetime.now() - dt.timedelta(days=2)).timestamp()
        os.utime(str(accepted), (old, old))
        p = self.run_cli("plan-from-accepted", cid, code=1)
        self.assertIn("older than change", p.stderr)


class FixDriftTraceTest(Repo):
    def _bounded_with_plan(self) -> str:
        cid = self.new("fix", "bounded")
        self.fill_intent(cid)
        (self.cdir(cid) / "spec.md").exists()
        self.write_plan(cid)
        self.git("add", ".")
        self.git("commit", "-qm", "artifacts")
        self.git("checkout", "-qb", "change/" + cid)
        return cid

    def test_fix_mode_and_drift(self):
        cid = self._bounded_with_plan()
        (self.root / "tests").mkdir()
        (self.root / "tests/test_status.py").write_text("def test_x():\n    assert False\n", encoding="utf-8")
        p = self.run_cli("fix-mode", cid, "on", code=1)
        self.assertIn("commit the failing test first", p.stderr.lower())
        self.git("add", ".")
        self.git("commit", "-qm", "failing test")
        self.run_cli("fix-mode", cid, "on", code=0)
        st = self.status(cid)
        self.assertTrue(st["fix_mode"])
        self.assertEqual(st["fix_commit"], self.git("rev-parse", "HEAD").strip())
        (self.root / "src").mkdir()
        (self.root / "src/status.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "src/other.py").write_text("y = 1\n", encoding="utf-8")
        d = json.loads(self.run_cli("drift", cid, "--json", code=0).stdout)
        self.assertEqual(d["unplanned"], ["src/other.py"])
        self.assertEqual(d["untouched"], [])
        self.assertEqual(d["fix_mode_test_edits"], [])
        self.assertNotIn("changes/%s/plan.md" % cid, d["changed"])
        self.run_cli("drift", cid, "--strict", code=1)
        (self.root / "src/other.py").unlink()
        self.run_cli("drift", cid, "--strict", code=0)
        (self.root / "tests/test_status.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
        p = self.run_cli("drift", cid, "--strict", code=1)
        self.assertIn("TEST-EDIT  tests/test_status.py", p.stdout)
        self.run_cli("fix-mode", cid, "off", code=0)
        self.assertFalse(self.status(cid)["fix_mode"])
        self.run_cli("drift", cid, "--strict", code=0)

    def test_trace(self):
        cid = self.new()
        self.fill_intent(cid)
        self.write_spec(cid)
        self.run_cli("trace", cid, code=1)  # no plan yet: all GAP
        self.write_plan(cid)
        p = self.run_cli("trace", cid, code=0)
        self.assertIn("OK      FR-001", p.stdout)
        self.write_spec(cid, extra_req="- FR-003 Audit. THE SYSTEM SHALL log lookups.")
        p = self.run_cli("trace", cid, "--json", code=1)
        rows = {r["id"]: r["status"] for r in json.loads(p.stdout)["rows"]}
        self.assertEqual(rows["FR-003"], "GAP")
        plan = self.cdir(cid) / "plan.md"
        plan.write_text(plan.read_text(encoding="utf-8").replace("Ownership check.", "FR-003 and FR-009."),
                        encoding="utf-8")
        rows = {r["id"]: r for r in json.loads(self.run_cli("trace", cid, "--json", "--tests", code=0).stdout)["rows"]}
        self.assertEqual(rows["FR-009"]["status"], "ORPHAN")
        (self.root / "tests").mkdir()
        (self.root / "tests/test_status.py").write_text("# covers FR-001\n", encoding="utf-8")
        rows = {r["id"]: r for r in json.loads(self.run_cli("trace", cid, "--json", "--tests", code=0).stdout)["rows"]}
        self.assertEqual(rows["FR-001"]["tests"], ["tests/test_status.py"])
        self.assertEqual(rows["FR-002"]["tests"], [])


class MetricsTest(Repo):
    def commit_at(self, when: str, msg: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-qm", msg, env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when})

    def test_metrics_from_git_and_audit(self):
        cid = self.new()
        self.fill_intent(cid)
        self.commit_at("2026-10-01T10:00:00+00:00", "intent")
        self.approve(cid, "intent")
        self.write_spec(cid)
        self.commit_at("2026-10-01T16:00:00+00:00", "spec")
        p = self.cdir(cid) / "intent.md"
        p.write_text(p.read_text(encoding="utf-8") + "- 2026-10-02 note — x\n", encoding="utf-8")
        self.commit_at("2026-10-02T09:00:00+00:00", "intent amend")
        self.write_plan(cid)
        self.commit_at("2026-10-03T10:00:00+00:00", "plan")
        s = self.cdir(cid) / "spec.md"
        s.write_text(s.read_text(encoding="utf-8") + "- 2026-10-04 clarified — y\n", encoding="utf-8")
        self.commit_at("2026-10-04T10:00:00+00:00", "spec rework")
        dropped = self.new("dropped-idea", "standard", "--no-activate")
        self.fill_intent(dropped)
        self.commit_at("2026-10-05T10:00:00+00:00", "intent 2")
        self.run_cli("reject", dropped, "intent", "--by", "Pat", "--reason", "no", code=0)
        m = json.loads(self.run_cli("metrics", "--json", code=0).stdout)
        rows = {r["id"]: r for r in m["changes"]}
        r = rows[cid]
        self.assertEqual(r["intent_to_spec_s"], 6 * 3600)
        self.assertEqual(r["spec_to_plan_s"], 42 * 3600)
        self.assertEqual(r["spec_commits_after_plan"], 1)
        self.assertEqual(r["intent_commits_after_spec"], 1)
        self.assertEqual(r["intent_decision"], "approved")
        self.assertEqual(rows[dropped]["intent_decision"], "rejected")
        self.assertEqual(m["summary"]["survival_rate"], 0.5)
        self.assertEqual(m["summary"]["intent_to_spec_s"]["median"], 6 * 3600)
        table = self.run_cli("metrics", code=0).stdout
        self.assertIn("| change | tier |", table)
        self.assertIn("intent survival: 50%", table)
        future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3)).strftime("%Y-%m-%d")
        self.assertEqual(json.loads(self.run_cli("metrics", "--json", "--since", future, code=0).stdout)["changes"], [])


class InitTest(Repo):
    def test_init_idempotent_and_merges_settings(self):
        claude = self.root / ".claude"
        claude.mkdir()
        user_settings = {
            "model": "opus",
            "permissions": {"allow": ["Bash(make test)"]},
            "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}],
                      "Stop": [{"hooks": [{"type": "command", "command": "echo stop-mine"}]}]},
        }
        (claude / "settings.json").write_text(json.dumps(user_settings), encoding="utf-8")
        (self.root / ".gitignore").write_text("node_modules/", encoding="utf-8")
        (self.root / "Makefile").write_text("test:\n\tpytest\nlint:\n\truff .\n", encoding="utf-8")
        before = sorted(str(p) for p in self.root.rglob("*") if ".git/" not in str(p))
        dry = self.run_cli("init", "--dry-run", code=0)
        self.assertIn("dry run", dry.stdout)
        self.assertEqual(sorted(str(p) for p in self.root.rglob("*") if ".git/" not in str(p)), before)
        self.assertEqual(json.loads((claude / "settings.json").read_text(encoding="utf-8")), user_settings)

        first = self.run_cli("init", code=0)
        self.assertIn("MERGE", first.stdout)
        second = self.run_cli("init", code=0)
        self.assertNotIn("CREATE", second.stdout)
        self.assertNotIn("MERGE", second.stdout)

        s = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(s["model"], "opus")
        self.assertEqual(s["permissions"], {"allow": ["Bash(make test)"]})
        cmds = [(ev, g.get("matcher"), h["command"]) for ev, groups in s["hooks"].items() for g in groups for h in g["hooks"]]
        self.assertIn(("PreToolUse", "Bash", "echo mine"), cmds)
        self.assertIn(("Stop", None, "echo stop-mine"), cmds)
        ours = [c for c in cmds if "hooks.py" in c[2]]
        self.assertEqual(sorted(c[0] for c in ours), ["PostToolUse", "PreToolUse", "Stop", "UserPromptSubmit"])
        self.assertEqual(len(cmds), len(set(cmds)))
        for _, _, c in ours:
            self.assertNotIn("__SKILL_DIR__", c)
            self.assertIn(str(SKILL / "scripts" / "hooks.py"), c)  # skill lives outside this temp repo
        stop_groups = s["hooks"]["Stop"]
        self.assertEqual(len(stop_groups), 1)  # merged into the existing matcher-less group

        cfg = json.loads((claude / "sdlc.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg["verify_commands"], ["make test", "make lint"])
        self.assertEqual(cfg["default_branch"], "main")
        gi = (self.root / ".gitignore").read_text(encoding="utf-8")
        self.assertEqual(gi.count(".claude/sdlc-state/"), 1)
        self.assertTrue(gi.startswith("node_modules/\n"))
        self.assertTrue((self.root / "changes/.gitkeep").is_file())
        if (SKILL / "assets/templates/REVIEW.md").is_file():
            self.assertTrue((self.root / "REVIEW.md").is_file())
        for a in (SKILL / "assets/agents").glob("*.md"):
            self.assertTrue((claude / "agents" / a.name).is_file())

    def test_init_does_not_overwrite_config(self):
        (self.root / ".claude").mkdir()
        (self.root / ".claude/sdlc.json").write_text('{"verify_commands": ["custom"]}', encoding="utf-8")
        self.run_cli("init", code=0)
        self.assertEqual(json.loads((self.root / ".claude/sdlc.json").read_text(encoding="utf-8")),
                         {"verify_commands": ["custom"]})

    def test_init_refuses_invalid_settings(self):
        (self.root / ".claude").mkdir()
        (self.root / ".claude/settings.json").write_text("{nope", encoding="utf-8")
        self.run_cli("init", code=2)
        self.assertEqual((self.root / ".claude/settings.json").read_text(encoding="utf-8"), "{nope")


class StatusTest(Repo):
    def test_status_json_and_text(self):
        cid = self.new()
        st = self.status(cid)
        for k in ("id", "path", "title", "tier", "source", "status", "record_id", "artifacts", "gates",
                  "required_gates", "skipped", "fix_mode", "stage", "next"):
            self.assertIn(k, st)
        self.assertEqual(st["path"], "changes/" + cid)
        self.assertEqual(set(st["gates"]["intent"]), {"gate", "artifact", "exists", "sha", "presented", "presented_ts",
                                                      "pending", "approved", "current", "approved_sha", "by", "role",
                                                      "ts", "rejected", "role_ok", "approved_roles",
                                                      "missing_roles"})
        overview = json.loads(self.run_cli("status", "--json", code=0).stdout)
        self.assertEqual(overview["active"]["id"], cid)
        self.assertIn(cid, self.run_cli("status", code=0).stdout)
        self.run_cli("status", "nope", code=2)

    def test_brief_inside_repo(self):
        cid = self.new()
        p = self.run_cli("status", "--brief", code=0)
        self.assertIn("active change: " + cid, p.stdout)
        self.assertLessEqual(len(p.stdout.strip().splitlines()), 12)

    def test_brief_never_fails(self):
        outside = Path(tempfile.mkdtemp(prefix="sdlc-nogit-")).resolve()
        p = self.run_cli("status", "--brief", cwd=outside, code=0)
        self.assertIn("not inside a git repository", p.stdout)
        (self.root / ".claude").mkdir()
        (self.root / ".claude/sdlc.json").write_text("{broken", encoding="utf-8")
        p = self.run_cli("status", "--brief", code=0)
        self.assertIn("config error", p.stdout)
        self.run_cli("status", code=2)
        p = self.run_cli("status", "--brief", env={"PATH": "/nonexistent"}, code=0)
        self.assertIn("git is not installed", p.stdout)
        p = self.run_cli("--repo", str(outside), "status", "--brief", code=0)
        self.assertIn("not inside a git repository", p.stdout)


class ApprovalIntegrityTest(Repo):
    """One human turn approves one gate once; names and answers are checked; headless runs hand off."""

    def _ready(self, slug: str = "claims-status") -> str:
        cid = self.new(slug, "standard", "--no-activate")
        self.fill_intent(cid)
        return cid

    def test_present_again_unchanged_keeps_window(self):
        cid = self._ready()
        self.run_cli("present", cid, "intent", code=0)
        self.human(1, "approve intent for this one")
        out = self.run_cli("present", cid, "intent", code=0).stdout  # the agent re-presents before approving
        self.assertIn("already presented", out)
        self.assertIn("Approve / Request changes / Reject", out)
        self.assertEqual(sum(e["event"] == "GATE_PRESENTED" for e in self.events(cid)), 1)
        self.run_cli("approve", cid, "intent", "--by", "Pat Owner", code=0)
        self.run_cli("present", cid, "intent", code=0)  # after a decision, presenting opens a new window
        self.assertEqual(sum(e["event"] == "GATE_PRESENTED" for e in self.events(cid)), 2)

    def test_one_turn_cannot_approve_two_gates_or_changes(self):
        a, b = self._ready("one"), self._ready("two")
        self.human(-10, "earlier")
        self.run_cli("present", a, "intent", code=0)
        self.run_cli("present", b, "intent", code=0)
        self.human(5, "what time is the standup? ok")
        self.run_cli("approve", a, "intent", "--by", "Pat Owner", code=0)
        p = self.run_cli("approve", b, "intent", "--by", "Pat Owner", code=1)
        self.assertIn("no other decision has used", p.stderr)
        self.human(6, "approve")
        self.run_cli("approve", b, "intent", "--by", "Pat Owner", code=0)
        used = [e["human_event"] for c in (a, b) for e in self.events(c) if e["event"] == "GATE_APPROVED"]
        self.assertEqual(len(set(used)), 2)  # identical text would still be two distinct events

    def test_answer_must_approve_and_prompt_must_read_as_approval(self):
        cid = self._ready()
        self.human(-10, "earlier")
        self.run_cli("present", cid, "intent", code=0)
        self.human(3, "what time is the standup?")
        p = self.run_cli("approve", cid, "intent", "--by", "Pat Owner", code=1)
        self.assertIn("does not read as an approval", p.stderr)
        p = self.run_cli("approve", cid, "intent", "--by", "Pat Owner", "--evidence", "PR 7 approved", code=1)
        self.assertIn("does not read as an approval", p.stderr)  # --evidence cannot turn a question into approval
        self.human(4, '{"Approve intent?": "Request changes"}', kind="ask_answer")
        self.human(5, "yes")
        p = self.run_cli("approve", cid, "intent", "--by", "Pat Owner", code=1)  # the gate's answer wins
        self.assertIn("not Approve", p.stderr)
        self.human(6, '{"Approve intent?": "Approve"}', kind="ask_answer")
        self.run_cli("approve", cid, "intent", "--by", "Pat Owner", code=0)
        self.assertEqual(self.events(cid)[-1]["presence"], "ask_answer")

    def test_reserved_names(self):
        sdlc = _load_cli()
        for bad in ("Claude", "Claude Code", "claude-bot", "release[bot]", "ai", "Agent", "GPT 5", "copilot"):
            self.assertTrue(sdlc.reserved_approver(bad), bad)
        for ok in ("Pat Owner", "Ai Nakamura", "Abbott", "Sam Agentson"):
            self.assertFalse(sdlc.reserved_approver(ok), ok)

    def test_reject_consumes_the_turn(self):
        a, b = self._ready("one"), self._ready("two")
        self.human(-10, "earlier")
        self.run_cli("present", a, "intent", code=0)
        self.run_cli("present", b, "intent", code=0)
        self.human(5, "ok, reject the first one")
        self.run_cli("reject", a, "intent", "--by", "Pat Owner", "--reason", "not now", code=0)
        self.assertTrue(self.events(a)[-2].get("human_event"))
        self.run_cli("approve", b, "intent", "--by", "Pat Owner", code=1)

    def test_headless_present_hands_off(self):
        cid = self._ready()
        hl = {"SDLC_NONINTERACTIVE": "1"}
        L.human_events_path(self.root).parent.mkdir(parents=True, exist_ok=True)
        L.human_events_path(self.root).touch()  # what the prompt hook does in a headless run
        out = self.run_cli("present", cid, "intent", env=hl, code=0).stdout
        self.assertNotIn("sdlc.py approve", out)
        self.assertIn("do not approve here", out)
        self.assertIn("interactive Claude Code session", out)
        p = self.run_cli("approve", cid, "intent", "--by", "Pat Owner", "--evidence", "said yes", env=hl, code=1)
        self.assertIn("refusing to approve in a headless run", p.stderr)
        self.assertIn("interactive Claude Code session", p.stderr)
        self.human(1, "yes, approve it")  # even a recorded turn does not make a headless approve legitimate
        self.run_cli("approve", cid, "intent", "--by", "Pat Owner", env=hl, code=1)
        nxt = json.loads(self.run_cli("status", cid, "--json", env=hl, code=0).stdout)["next"]
        self.assertIn("do not approve here", nxt)
        self.assertNotIn("sdlc.py approve", nxt)
        self.assertIn("sdlc.py approve", self.status(cid)["next"])  # interactive keeps the command


def _load_cli():
    spec = importlib.util.spec_from_file_location("sdlc_cli_names", str(SDLC))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CliPrefixTest(Repo):
    def _allowed(self) -> list:
        fm = L.parse_frontmatter((SKILL / "SKILL.md").read_text(encoding="utf-8"))[0]
        return re.findall(r"Bash\(([^)]*)\)", str(fm.get("allowed-tools", "")))

    def _matches(self, cmd: str, skill_dir: str) -> bool:
        for pat in self._allowed():
            pat = pat.replace("${CLAUDE_SKILL_DIR}", skill_dir)
            rx = "^" + ".*".join(re.escape(x) for x in pat.split("*")) + "$"
            if pat.count("*") == 1 and pat.endswith(" *"):  # a lone trailing " *" also matches the bare command
                rx = "^" + re.escape(pat[:-2]) + "( .*)?$"
            if re.match(rx, cmd):
                return True
        return False

    def _cli_line(self, script: Path) -> str:
        p = subprocess.run([sys.executable, str(script), "status", "--brief"], cwd=str(self.root),
                           stdout=subprocess.PIPE, universal_newlines=True)
        line = [ln for ln in p.stdout.splitlines() if ln.startswith("cli: ")][0]
        return line[len("cli: "):].replace(" <command>", "")

    def test_printed_commands_match_allowed_tools(self):
        cli = self._cli_line(SDLC)
        self.assertEqual(cli, "python3 %s" % SDLC.as_posix())
        self.assertTrue(self._matches(cli + " status", SKILL.as_posix()), (cli, self._allowed()))

    def test_symlinked_skill_dir_keeps_the_link_path(self):
        link = Path(tempfile.mkdtemp(prefix="sdlc-link-")) / "ai-native-sdlc"
        os.symlink(str(SKILL), str(link))
        cli = self._cli_line(link / "scripts" / "sdlc.py")
        self.assertEqual(cli, "python3 %s/scripts/sdlc.py" % link.as_posix())
        self.assertTrue(self._matches(cli + " status", link.as_posix()))


class NextHintTest(Repo):
    def test_missing_spec_and_plan_name_template_and_shas(self):
        cid = self.new()
        self.fill_intent(cid)
        self.approve(cid, "intent")
        st = self.status(cid)
        self.assertIn("assets/templates/spec.md", st["next"])
        self.assertIn("intent_sha: " + st["gates"]["intent"]["sha"], st["next"])
        self.assertIn("validate %s spec --strict" % cid, st["next"])
        self.write_spec(cid)
        self.approve(cid, "spec")
        st = self.status(cid)
        self.assertIn("assets/templates/plan.md", st["next"])
        self.assertIn("spec_sha: " + st["gates"]["spec"]["sha"], st["next"])
        self.assertIn("plan-from-accepted", st["next"])

    def test_stage_label_and_id_date(self):
        cid = self.new()
        meta = L.read_meta(self.cdir(cid) / "intent.md")
        self.assertTrue(cid.startswith(meta["created"][:10].replace("-", "")))
        out = self.run_cli("status", cid, code=0).stdout
        self.assertIn("stage: 1-Plan (intent.md)", out)
        self.assertEqual(self.status(cid)["stage"], "plan")  # JSON keeps the bare stage

    def test_agent_written_bounded_intent_needs_intent_gate(self):
        cid = self.new("probe", "bounded", "--source", "incident")
        st = self.status(cid)
        self.assertEqual(st["required_gates"], ["intent", "plan"])
        # the source comes from the audit log; editing the frontmatter does not drop the gate
        L.update_frontmatter(self.cdir(cid) / "intent.md", source="human")
        self.assertEqual(self.status(cid)["required_gates"], ["intent", "plan"])
        self.assertEqual(self.status(self.new("human", "bounded", "--no-activate"))["required_gates"], ["plan"])


class DriftRewriteTest(FixDriftTraceTest):
    def test_missing_fix_commit_fails_closed(self):
        cid = self._bounded_with_plan()
        (self.root / "tests").mkdir()
        (self.root / "tests/test_status.py").write_text("def test_x():\n    assert False\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "failing test")
        self.run_cli("fix-mode", cid, "on", code=0)
        (self.root / "src").mkdir()
        (self.root / "src/status.py").write_text("x = 1\n", encoding="utf-8")
        audit = self.cdir(cid) / "audit.md"
        text = audit.read_text(encoding="utf-8")
        audit.write_text(re.sub(r"commit=[0-9a-f]{40}", "commit=" + "0" * 40, text), encoding="utf-8")
        p = self.run_cli("drift", cid, "--strict", code=0)  # digest of test files still matches
        self.assertIn("verified unchanged by content digest", p.stdout)
        (self.root / "tests/test_status.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
        p = self.run_cli("drift", cid, "--strict", code=1)
        self.assertIn("ERROR  fix-mode base commit", p.stdout)
        d = json.loads(self.run_cli("drift", cid, "--json", code=0).stdout)
        self.assertTrue(d["errors"])
        audit.write_text(re.sub(r" \| tests_sha256=[0-9a-f]+", "", audit.read_text(encoding="utf-8")), encoding="utf-8")
        (self.root / "tests/test_status.py").write_text("def test_x():\n    assert False\n", encoding="utf-8")
        p = self.run_cli("drift", cid, "--strict", code=1)
        self.assertIn("cannot verify test files are unchanged", p.stdout)


class MetricsSurvivalTest(MetricsTest):
    def test_dropped_counts_and_bounded_excluded_and_squash(self):
        std = self.new("std-drop", "standard", "--no-activate")
        bnd = self.new("bnd-drop", "bounded", "--no-activate")
        self.run_cli("close", std, "--status", "dropped", code=0)
        self.run_cli("close", bnd, "--status", "dropped", code=0)
        sq = self.new("squashed", "standard", "--no-activate")
        self.fill_intent(sq)
        self.write_spec(sq)
        self.write_plan(sq)
        self.commit_at("2026-10-01T10:00:00+00:00", "squash merge")
        m = json.loads(self.run_cli("metrics", "--json", code=0).stdout)
        rows = {r["id"]: r for r in m["changes"]}
        self.assertEqual(rows[std]["intent_decision"], "rejected")
        self.assertEqual(rows[bnd]["intent_decision"], "n/a")
        self.assertEqual((m["summary"]["intents_rejected"], m["summary"]["intents_without_gate"]), (1, 1))
        self.assertEqual(m["summary"]["survival_rate"], 0.0)
        self.assertTrue(rows[sq]["collapsed"])
        self.assertIsNone(rows[sq]["intent_to_spec_s"])
        self.assertIsNone(rows[sq]["spec_commits_after_plan"])
        self.assertIn("squash merge?", self.run_cli("metrics", code=0).stdout)


class ApproveHardeningTest(Repo):
    """Records that cannot be a human turn are ignored; installed hooks without the log fail closed."""

    def _presented(self) -> str:
        cid = self.new("claims-status", "standard", "--no-activate")
        self.fill_intent(cid)
        self.human(-10, "earlier")
        self.run_cli("present", cid, "intent", code=0)
        return cid

    def _write(self, rec: dict) -> None:
        with open(str(L.human_events_path(self.root)), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def test_future_sessionless_and_task_notification_events_ignored(self):
        cid = self._presented()
        self._write({"ts": "2099-01-01T00:00:00Z", "type": "prompt", "session_id": "s1", "excerpt": "approve"})
        self.human(1, "approve", session_id=...)  # no session_id: not from a hook payload
        self.human(1, "approve", session_id=None)
        self.human(1, "<task-notification>\n<status>completed</status>\n</task-notification> approve")
        p = self.run_cli("approve", cid, "intent", "--by", "Pat Owner", code=1)
        self.assertIn("no human turn recorded", p.stderr)
        self.human(1, "approve")
        self.run_cli("approve", cid, "intent", "--by", "Pat Owner", code=0)
        self.assertEqual(self.events(cid)[-1]["evidence"], "approve")

    def test_reject_ignores_untrusted_events(self):
        cid = self._presented()
        self._write({"ts": "2099-01-01T00:00:00Z", "type": "prompt", "session_id": "s1", "excerpt": "no"})
        self.run_cli("reject", cid, "intent", "--by", "Pat Owner", "--reason", "not now", code=0)
        self.assertIsNone(self.events(cid)[-2].get("human_event"))

    def test_hooks_installed_but_log_missing_fails_closed(self):
        cid = self.new("claims-status", "standard", "--no-activate")
        self.fill_intent(cid)
        self.run_cli("present", cid, "intent", code=0)
        (self.root / ".claude").mkdir(exist_ok=True)
        hooks = json.dumps({"hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": "python3 \"/x/scripts/hooks.py\" stop"}]}]}})
        for name in ("settings.json", "settings.local.json"):
            with self.subTest(name=name):
                f = self.root / ".claude" / name
                f.write_text(hooks, encoding="utf-8")
                p = self.run_cli("approve", cid, "intent", "--by", "Pat Owner", "--evidence", "said yes", code=1)
                self.assertIn("send any prompt", p.stderr)
                self.assertNotIn("GATE_APPROVED", [e["event"] for e in self.events(cid)])
                f.unlink()
        self.run_cli("approve", cid, "intent", "--by", "Pat Owner", "--evidence", "said yes", code=0)
        self.assertEqual(self.events(cid)[-1]["presence"], "unverified")


class InitExtrasTest(Repo):
    def _ours(self) -> list:
        s = json.loads((self.root / ".claude/settings.json").read_text(encoding="utf-8"))
        return [(ev, g.get("matcher"), h["command"]) for ev, groups in s["hooks"].items() for g in groups
                for h in g["hooks"] if "hooks.py" in h["command"]]

    def test_claude_md_propose_create_skip(self):
        p = self.run_cli("init", code=0)
        self.assertIn("PROPOSE", p.stdout)
        self.assertIn("## How work is done here", p.stdout)
        self.assertIn("invoke the `ai-native-sdlc` skill first", p.stdout)
        self.assertFalse((self.root / "CLAUDE.md").exists())
        p = self.run_cli("init", "--claude-md", "--dry-run", code=0)
        self.assertRegex(p.stdout, r"CREATE\s+CLAUDE.md")
        self.assertFalse((self.root / "CLAUDE.md").exists())
        p = self.run_cli("init", "--claude-md", code=0)
        self.assertRegex(p.stdout, r"CREATE\s+CLAUDE.md")
        sdlc = _load_cli()
        self.assertEqual((self.root / "CLAUDE.md").read_text(encoding="utf-8"), sdlc.ROUTING_BLOCK)
        p = self.run_cli("init", "--claude-md", code=0)
        self.assertRegex(p.stdout, r"SKIP\s+CLAUDE.md")
        self.assertNotIn("PROPOSE", self.run_cli("init", code=0).stdout)
        tpl = (SKILL / "assets/templates/CLAUDE-verification.md").read_text(encoding="utf-8")
        self.assertIn(sdlc.ROUTING_BLOCK, tpl)
        self.assertLess(tpl.index(sdlc.ROUTING_HEADING), tpl.index("## Verifying your work"))

    def test_claude_md_merge_keeps_content(self):
        (self.root / "CLAUDE.md").write_text("# Repo\n\nBuild with make.", encoding="utf-8")
        p = self.run_cli("init", "--claude-md", code=0)
        self.assertRegex(p.stdout, r"MERGE\s+CLAUDE.md")
        text = (self.root / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Repo\n\nBuild with make.\n\n## How work is done here\n"))
        self.assertEqual(text.count("## How work is done here"), 1)

    def test_rerun_after_template_change_keeps_one_handler_per_event(self):
        claude = self.root / ".claude"
        claude.mkdir()
        ref = str(SKILL)
        old = {"hooks": {
            "PreToolUse": [{"matcher": "Edit|Write|Bash", "hooks": [
                {"type": "command", "command": "python3 \"%s/scripts/hooks.py\" pre-tool" % ref, "timeout": 30}]},
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"},
                                              {"type": "command", "command": "python3 \"%s/scripts/hooks.py\" pre-tool" % ref}]}],
            "Stop": [{"hooks": [{"type": "command", "command": "python3 \"/old/place/scripts/hooks.py\" stop"}]}]}}
        (claude / "settings.json").write_text(json.dumps(old), encoding="utf-8")
        for _ in range(3):
            self.run_cli("init", code=0)
        ours = self._ours()
        self.assertEqual(sorted(c[0] for c in ours), ["PostToolUse", "PreToolUse", "Stop", "UserPromptSubmit"])
        tpl = json.loads((SKILL / "assets/settings/project-settings.json").read_text(encoding="utf-8"))["hooks"]
        for ev, matcher, cmd in ours:
            self.assertEqual(matcher, tpl[ev][0].get("matcher"))
            self.assertIn(str(SKILL / "scripts" / "hooks.py"), cmd)
        s = json.loads((claude / "settings.json").read_text(encoding="utf-8"))
        self.assertIn({"matcher": "Bash", "hooks": [{"type": "command", "command": "echo mine"}]}, s["hooks"]["PreToolUse"])
        self.assertEqual(len(s["hooks"]["PreToolUse"]), 2)
        self.assertIn("SKIP", self.run_cli("init", code=0).stdout)

    def test_brief_headless_line(self):
        self.new()
        lines = self.run_cli("status", "--brief", env={"SDLC_NONINTERACTIVE": "1"}, code=0).stdout.splitlines()
        self.assertEqual(lines[1], "session: headless — gates are PR reviews; do not approve here")
        self.assertNotIn("headless", self.run_cli("status", "--brief", code=0).stdout)


class WorktreeDeactivateTest(Repo):
    def test_deactivate_in_linked_worktree_writes_null(self):
        cid = self.new()
        self.git("add", ".")
        self.git("commit", "-qm", "intent")
        wt = Path(tempfile.mkdtemp(prefix="sdlc-wt-")).resolve() / "wt"
        self.git("worktree", "add", "-q", "-b", "wt-branch", str(wt))
        self.run_cli("activate", cid, cwd=wt, code=0)
        self.run_cli("deactivate", cwd=wt, code=0)
        data = json.loads((wt / L.STATE_DIRNAME / "active.json").read_text(encoding="utf-8"))
        self.assertIsNone(data["id"])
        self.assertIsNone(L.get_active(wt))
        self.assertEqual(L.get_active(self.root), cid)  # the main checkout is untouched
        self.run_cli("deactivate", code=0)
        self.assertFalse((self.root / L.STATE_DIRNAME / "active.json").exists())


class DriftMergedBaseTest(FixDriftTraceTest):
    def test_test_edit_merged_from_base_is_not_flagged(self):
        cid = self._bounded_with_plan()
        (self.root / "tests").mkdir()
        (self.root / "tests/test_status.py").write_text("def test_x():\n    assert False\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "failing test")
        self.run_cli("fix-mode", cid, "on", code=0)
        self.git("checkout", "-q", "main")
        (self.root / "tests").mkdir(exist_ok=True)
        (self.root / "tests/test_other.py").write_text("def test_y():\n    assert True\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "someone else's test on main")
        self.git("checkout", "-q", "change/" + cid)
        self.git("merge", "-q", "--no-edit", "main")
        d = json.loads(self.run_cli("drift", cid, "--json", code=0).stdout)
        self.assertEqual(d["fix_mode_test_edits"], [])
        (self.root / "tests/test_status.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
        d = json.loads(self.run_cli("drift", cid, "--json", code=0).stdout)
        self.assertEqual(d["fix_mode_test_edits"], ["tests/test_status.py"])


class PackagePluginTest(Repo):
    def _package(self, *extra: str) -> Path:
        out = Path(tempfile.mkdtemp(prefix="sdlc-plugin-")).resolve() / "plugin"
        self.run_cli("package-plugin", "--out", str(out), *extra, code=0)
        return out

    def test_layout(self):
        out = self._package()
        fm = L.parse_frontmatter((SKILL / "SKILL.md").read_text(encoding="utf-8"))[0]
        man = json.loads((out / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual((man["name"], man["version"]), ("ai-native-sdlc", fm["metadata"]["version"]))
        self.assertTrue(man["description"])
        sk = out / "skills/ai-native-sdlc"
        for rel in ("SKILL.md", "scripts/sdlc.py", "scripts/hooks.py", "scripts/sdlc_lib.py",
                    "assets/settings/project-settings.json", "references/adoption.md"):
            self.assertTrue((sk / rel).is_file(), rel)
        for repo_only in ("tests", "examples", ".github", ".claude-plugin"):
            self.assertFalse((sk / repo_only).exists(), repo_only)
        self.assertEqual([p for p in sk.rglob("*") if p.name == "__pycache__" or p.suffix == ".pyc"], [])
        self.assertTrue(all(len(p.relative_to(sk / "evals").parts) == 1 for p in (sk / "evals").rglob("*")))
        self.assertEqual(sorted(p.name for p in (out / "agents").iterdir()),
                         sorted(p.name for p in (SKILL / "assets/agents").glob("sdlc-*.md")))
        hooks = json.loads((out / "hooks/hooks.json").read_text(encoding="utf-8"))
        tpl = json.loads((SKILL / "assets/settings/project-settings.json").read_text(encoding="utf-8"))["hooks"]
        self.assertEqual(set(hooks["hooks"]), set(tpl))
        for ev, groups in hooks["hooks"].items():
            self.assertEqual([g.get("matcher") for g in groups], [g.get("matcher") for g in tpl[ev]])
            for g in groups:
                for h in g["hooks"]:
                    self.assertIn('"${CLAUDE_PLUGIN_ROOT}/skills/ai-native-sdlc/scripts/hooks.py"', h["command"])
        self.assertNotIn("__SKILL_DIR__", json.dumps(hooks))

    def test_options_rerun_and_refusals(self):
        out = self._package("--name", "acme-sdlc", "--version", "9.9.9")
        man = json.loads((out / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual((man["name"], man["version"]), ("acme-sdlc", "9.9.9"))
        (out / "agents" / "stale.md").write_text("x", encoding="utf-8")
        self.run_cli("package-plugin", "--out", str(out), code=0)  # repackaging replaces the old tree
        self.assertFalse((out / "agents" / "stale.md").exists())
        busy = Path(tempfile.mkdtemp(prefix="sdlc-busy-")).resolve()
        (busy / "keep.txt").write_text("x", encoding="utf-8")
        self.run_cli("package-plugin", "--out", str(busy), code=2)
        self.assertEqual([p.name for p in busy.iterdir()], ["keep.txt"])
        self.run_cli("package-plugin", "--out", str(SKILL / "build"), code=2)
        self.run_cli("package-plugin", "--out", str(out), "--name", "Bad Name", code=2)

    def test_packaged_skill_knows_its_hooks_ship_with_the_plugin(self):
        out = self._package()
        cli = out / "skills/ai-native-sdlc/scripts/sdlc.py"
        p = subprocess.run([sys.executable, str(cli), "init"], cwd=str(self.root), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("ship with the plugin", p.stdout)
        self.assertFalse((self.root / ".claude/settings.json").exists())
        p = subprocess.run([sys.executable, str(cli), "status", "--brief"], cwd=str(self.root),
                           stdout=subprocess.PIPE, universal_newlines=True)
        self.assertNotIn("guard hooks not installed", p.stdout)


if __name__ == "__main__":
    unittest.main()
