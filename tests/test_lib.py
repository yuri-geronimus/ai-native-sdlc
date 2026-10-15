"""Unit tests for scripts/sdlc_lib.py. Run: python3 -m unittest discover -s tests -t ."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))
import sdlc_lib as L  # noqa: E402

PLAYBOOK_BANDS = """\
metric: ci_test_failure_rate
baseline: rolling_30d
rules: western_electric
tiers:
  1sigma: { action: log }
  2sigma: { action: diagnose,
            tools: "Read,Grep,Bash(gh run view *)" }
  3sigma: { action: propose,
            routes: [pull_request, runbook:rollback-deploy] }
"""


def _isolated_git_env() -> None:
    cfg = Path(tempfile.mkdtemp(prefix="sdlc-gitcfg-")) / "gitconfig"
    cfg.write_text("", encoding="utf-8")
    os.environ["GIT_CONFIG_GLOBAL"] = str(cfg)
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
    os.environ.pop("CI", None)
    os.environ.pop("SDLC_NONINTERACTIVE", None)  # the suite tests interactive behaviour unless a test sets it


def setUpModule() -> None:
    _isolated_git_env()


def make_repo() -> Path:
    d = Path(tempfile.mkdtemp(prefix="sdlc-lib-")).resolve()
    subprocess.run(["git", "init", "-q", "-b", "main", str(d)], check=True)
    for k, v in (("user.name", "Test User"), ("user.email", "t@example.com"), ("commit.gpgsign", "false")):
        subprocess.run(["git", "-C", str(d), "config", k, v], check=True)
    return d


def git(d: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(d)] + list(args), check=True, stdout=subprocess.PIPE,
                          universal_newlines=True).stdout


class YamlLiteTest(unittest.TestCase):
    def test_playbook_bands_flow_maps_spanning_lines(self):
        data = L.yaml_lite_load(PLAYBOOK_BANDS)
        self.assertEqual(data["metric"], "ci_test_failure_rate")
        self.assertEqual(data["baseline"], "rolling_30d")
        self.assertEqual(data["tiers"]["1sigma"], {"action": "log"})
        self.assertEqual(data["tiers"]["2sigma"], {"action": "diagnose", "tools": "Read,Grep,Bash(gh run view *)"})
        self.assertEqual(data["tiers"]["3sigma"]["routes"], ["pull_request", "runbook:rollback-deploy"])

    def test_yaml_load_matches_lite_without_pyyaml(self):
        self.assertEqual(L.yaml_load(PLAYBOOK_BANDS), L.yaml_lite_load(PLAYBOOK_BANDS))

    def test_scalars_lists_nesting_comments(self):
        text = """\
# leading comment
a: 1
b: -2.5
c: true
d: False
e: null
f: ~
g:
h: "quoted: # not a comment"
i: 'it''s'
j: plain text # trailing comment
k: [1, "two", three, [4, 5], {x: y}]
l:
  - one
  - two: 2
    three: 3
  - - nested
    - list
m:
- same-indent list
n: {}
o: []
url: http://example.com/a#b
rule_tiers: {WE1: 3sigma, WE4: 2sigma}
"""
        d = L.yaml_lite_load(text)
        self.assertEqual(d["a"], 1)
        self.assertEqual(d["b"], -2.5)
        self.assertIs(d["c"], True)
        self.assertIs(d["d"], False)
        self.assertIsNone(d["e"])
        self.assertIsNone(d["f"])
        self.assertIsNone(d["g"])
        self.assertEqual(d["h"], "quoted: # not a comment")
        self.assertEqual(d["i"], "it's")
        self.assertEqual(d["j"], "plain text")
        self.assertEqual(d["k"], [1, "two", "three", [4, 5], {"x": "y"}])
        self.assertEqual(d["l"], ["one", {"two": 2, "three": 3}, ["nested", "list"]])
        self.assertEqual(d["m"], ["same-indent list"])
        self.assertEqual(d["n"], {})
        self.assertEqual(d["o"], [])
        self.assertEqual(d["url"], "http://example.com/a#b")
        self.assertEqual(d["rule_tiers"], {"WE1": "3sigma", "WE4": "2sigma"})

    def test_list_of_maps_exclusions(self):
        d = L.yaml_lite_load('exclusions:\n  - from: "2026-01-01T00:00:00Z"\n    to: 2026-01-02\n')
        self.assertEqual(d, {"exclusions": [{"from": "2026-01-01T00:00:00Z", "to": "2026-01-02"}]})

    def test_errors_have_line_numbers(self):
        cases = {
            "a: 1\n\tb: 2\n": "line 2",
            "a: &anchor 1\n": "line 1",
            "a: |\n  text\n": "line 1",
            "a: [1, 2\nb: 3\n": "line 1",
            "a: 1\n  b: 2\n": "line 2",
            "a: 1\na: 2\n": "line 2",
        }
        for text, where in cases.items():
            with self.assertRaises(ValueError, msg=text) as cm:
                L.yaml_lite_load(text)
            self.assertIn(where, str(cm.exception), text)

    def test_empty_document(self):
        self.assertIsNone(L.yaml_lite_load("# only a comment\n\n"))


class FrontmatterTest(unittest.TestCase):
    def test_round_trip_tricky_values(self):
        meta = {
            "id": "20261008-x", "title": "Claims status: self-service", "author": "J. Ortiz",
            "created": "2026-10-08T19:40:00Z", "date": "2026-10-08", "record_id": "", "none": None,
            "num": 3, "flt": 0.5, "flag": True, "str_true": "true", "str_num": "123", "hash": "#tag",
            "dash": "- item", "quote": 'say "hi"', "unicode": "café — ok", "skills": ["security@2.1", "brand: v1"],
            "empty_list": [], "status": "draft",
        }
        body = "# Title\n\n## Problem\nText.\n"
        text = L.dump_frontmatter(meta, body)
        meta2, body2 = L.parse_frontmatter(text)
        self.assertEqual(meta2, meta)
        self.assertEqual(body2, body)
        self.assertEqual(L.dump_frontmatter(meta2, body2), text)

    def test_no_frontmatter(self):
        self.assertEqual(L.parse_frontmatter("# Hello\n"), ({}, "# Hello\n"))
        self.assertEqual(L.parse_frontmatter("---\nunterminated: 1\n"), ({}, "---\nunterminated: 1\n"))

    def test_invalid_frontmatter_raises(self):
        with self.assertRaises(ValueError):
            L.parse_frontmatter("---\na: [1, 2\n---\nbody\n")

    def test_update_frontmatter_preserves_body(self):
        d = Path(tempfile.mkdtemp())
        p = d / "x.md"
        body = "# T\n\n## Problem\nline  \n\n## Change log\n- a\n"
        p.write_text(L.dump_frontmatter({"id": "x", "status": "draft"}, body), encoding="utf-8")
        before = L.fingerprint(p.read_text(encoding="utf-8"))
        L.update_frontmatter(p, status="approved", approved_by="A. B")
        meta, body2 = L.parse_frontmatter(p.read_text(encoding="utf-8"))
        self.assertEqual(meta, {"id": "x", "status": "approved", "approved_by": "A. B"})
        self.assertEqual(body2, body)
        self.assertEqual(L.fingerprint(p.read_text(encoding="utf-8")), before)


class SectionsFingerprintTest(unittest.TestCase):
    DOC = ("---\nid: x\nstatus: draft\n---\n# Intent: x\n\n## Problem\nSomething.\n### Detail\nMore.\n\n"
           "```\n## not a heading\n```\n\n## Proposed outcome\nBetter.\n\n## Change log\n- 2026-10-01 a — b\n")

    def test_sections(self):
        s = L.sections(L.parse_frontmatter(self.DOC)[1])
        self.assertEqual(set(s), {"problem", "proposed outcome", "change log"})
        self.assertIn("### Detail", s["problem"])
        self.assertIn("## not a heading", s["problem"])
        self.assertEqual(s["proposed outcome"], "Better.")

    def test_fingerprint_ignores_frontmatter_and_change_log(self):
        fp = L.fingerprint(self.DOC)
        self.assertEqual(L.fingerprint(self.DOC.replace("status: draft", "status: approved\napproved_by: Z")), fp)
        self.assertEqual(L.fingerprint(self.DOC + "- 2026-10-09 another — reason\n"), fp)
        self.assertEqual(L.fingerprint(self.DOC.replace("## Change log", "## CHANGE LOG ")), fp)
        self.assertEqual(L.fingerprint(self.DOC.replace("\n", "\r\n")), fp)
        self.assertEqual(L.fingerprint(self.DOC.replace("Better.", "Better.   ")), fp)
        self.assertNotEqual(L.fingerprint(self.DOC.replace("Better.", "Best.")), fp)
        self.assertEqual(len(fp), 64)

    def test_frozen_part(self):
        fz = L.frozen_part(self.DOC)
        self.assertTrue(fz.startswith("# Intent: x"))
        self.assertTrue(fz.endswith("Better."))
        self.assertNotIn("2026-10-01", fz)


class GlobTest(unittest.TestCase):
    def test_double_star(self):
        yes = [("tests/a.py", "tests/**"), ("tests/x/y.py", "tests/**"), ("x", "**/x"), ("a/b/x", "**/x"),
               ("test_a.py", "**/test_*.py"), ("pkg/sub/test_a.py", "**/test_*.py"),
               ("tests/a", "**/tests/**"), ("src/tests/a/b", "**/tests/**"), ("web/a.test.ts", "**/*.test.*"),
               ("a/b/c/d.go", "a/**/d.go"), ("a/d.go", "a/**/d.go"), ("anything/at/all", "**"),
               ("src/__tests__/x.js", "**/__tests__/**"), ("./src/a.py", "src/*.py")]
        no = [("tests", "tests/**"), ("src/tests.py", "**/tests/**"), ("a/b.py", "*/*/b.py"),
              ("src/a/b.py", "src/*.py"), ("xtests/a", "tests/**"), ("a/xd.go", "a/**/d.go")]
        for p, pat in yes:
            self.assertTrue(L.glob_match(p, pat), (p, pat))
        for p, pat in no:
            self.assertFalse(L.glob_match(p, pat), (p, pat))

    def test_basename_rule_and_classes(self):
        self.assertTrue(L.glob_match("deep/dir/file.py", "*.py"))
        self.assertTrue(L.glob_match("a/b/c.md", "?.md"))
        self.assertFalse(L.glob_match("a/b/cc.md", "?.md"))
        self.assertTrue(L.glob_match("src/a1.py", "src/a[0-9].py"))
        self.assertFalse(L.glob_match("src/ab.py", "src/a[!b].py"))
        self.assertTrue(L.glob_match("vendor/x/y", "vendor/"))
        self.assertTrue(L.match_any("docs/a.md", ["src/**", "docs/**"]))
        self.assertFalse(L.match_any("docs/a.md", []))

    def test_default_test_globs(self):
        globs = L.DEFAULT_CONFIG["test_globs"]
        for p in ("tests/test_x.py", "pkg/foo_test.go", "web/src/a.spec.ts", "lib/test_util.py"):
            self.assertTrue(L.match_any(p, globs), p)
        for p in ("src/app.py", "README.md", "src/contest.py"):
            self.assertFalse(L.match_any(p, globs), p)


class AuditTest(unittest.TestCase):
    def test_append_read_escaping(self):
        d = Path(tempfile.mkdtemp()) / "20261008-x"
        d.mkdir()
        sha = "a" * 64
        long_ev = "x" * 400
        L.audit_append(d, "CHANGE_CREATED", tier="standard", by="J. Ortiz")
        L.audit_append(d, "GATE_APPROVED", gate="plan", sha256=sha, by="J. Ortiz", role="engineer",
                       evidence='yes | go "ahead"\nnow')
        L.audit_append(d, "NOTE", evidence=long_ev, empty="", skipped=None)
        text = (d / "audit.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# Audit log: 20261008-x\n\nAppend-only. Written by `sdlc.py`; do not edit by hand.\n\n"))
        lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
        self.assertEqual(len(lines), 3)
        self.assertRegex(lines[1], r"^- \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ \| GATE_APPROVED \| gate=plan \| sha256=a{64} \| ")
        recs = L.audit_read(d)
        self.assertEqual([r["event"] for r in recs], ["CHANGE_CREATED", "GATE_APPROVED", "NOTE"])
        self.assertEqual(recs[0]["by"], "J. Ortiz")
        self.assertEqual(recs[1]["sha256"], sha)
        self.assertEqual(recs[1]["evidence"], "yes / go 'ahead' now")
        self.assertEqual(len(recs[2]["evidence"]), 300)
        self.assertEqual(recs[2]["empty"], "")
        self.assertNotIn("skipped", recs[2])

    def test_rejects_unknown_event_and_bad_keys(self):
        d = Path(tempfile.mkdtemp())
        with self.assertRaises(L.SdlcError):
            L.audit_append(d, "MADE_UP")
        with self.assertRaises(L.SdlcError):
            L.audit_append(d, "NOTE", **{"Bad-Key": 1})

    def test_read_skips_garbage_and_handles_missing_newline(self):
        d = Path(tempfile.mkdtemp())
        (d / "audit.md").write_text("# Audit log: x\n\nrandom text\n- not | valid\n- 2026-01-01T00:00:00Z | NOTE | a=1",
                                    encoding="utf-8")
        L.audit_append(d, "NOTE", b="2")
        recs = L.audit_read(d)
        self.assertEqual([(r.get("a"), r.get("b")) for r in recs], [("1", None), (None, "2")])


class StateTest(unittest.TestCase):
    def test_active_and_human_events(self):
        root = Path(tempfile.mkdtemp())
        self.assertIsNone(L.get_active(root))
        L.set_active(root, "20261008-x")
        self.assertEqual(L.get_active(root), "20261008-x")
        self.assertTrue((root / ".claude/sdlc-state/.gitignore").is_file())
        L.set_active(root, None)
        self.assertIsNone(L.get_active(root))
        hp = L.human_events_path(root)
        hp.write_text('{"ts": "2026-10-08T10:00:00Z", "type": "prompt", "excerpt": "ok"}\nnot json\n[1]\n\n'
                      '{"ts": "2026-10-08T10:01:00Z", "type": "ask_answer"}\n', encoding="utf-8")
        self.assertEqual([e["type"] for e in L.human_events(root)], ["prompt", "ask_answer"])

    def test_config_merge_and_errors(self):
        root = Path(tempfile.mkdtemp())
        self.assertEqual(L.load_config(root), L.DEFAULT_CONFIG)
        (root / ".claude").mkdir()
        (root / ".claude/sdlc.json").write_text(json.dumps(
            {"verify_commands": ["make test"], "production_gate": {"approval_check": "true"}}), encoding="utf-8")
        cfg = L.load_config(root)
        self.assertEqual(cfg["verify_commands"], ["make test"])
        self.assertEqual(cfg["production_gate"]["approval_check"], "true")
        self.assertEqual(cfg["production_gate"]["approval_timeout_s"], 20)
        self.assertEqual(L.DEFAULT_CONFIG["production_gate"]["approval_check"], "")
        (root / ".claude/sdlc.json").write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError) as cm:
            L.load_config(root)
        self.assertIn("sdlc.json", str(cm.exception))

    def test_change_dir_errors(self):
        root = Path(tempfile.mkdtemp())
        with self.assertRaises(L.SdlcError):
            L.change_dir(root, L.DEFAULT_CONFIG, "../etc")
        with self.assertRaises(L.SdlcError):
            L.change_dir(root, L.DEFAULT_CONFIG, "20261008-missing")


class PlanFilesTest(unittest.TestCase):
    def test_formats(self):
        plan = """---
id: x
---
# Plan

## Files that change
<!-- - `ignored/in/comment.py` -->
- `services/api/routes/status.py`
- services/api/tests/test_status.py (new)
* web/src/StatusPanel.tsx
1. Makefile.ci
| File | Why |
|---|---|
| `db/migrations/0042_status.sql` | new column |
| lib/cache.py | TTL |
portal/src/claims/StatusPanel.tsx (new), claims-api/routes/status.py,
claims-api/tests/test_status.py
Also e.g. some prose words.

## Order of work
1. `not/a/file/here.py`
"""
        self.assertEqual(L.plan_files(plan), [
            "services/api/routes/status.py", "services/api/tests/test_status.py", "web/src/StatusPanel.tsx",
            "Makefile.ci", "db/migrations/0042_status.sql", "lib/cache.py", "portal/src/claims/StatusPanel.tsx",
            "claims-api/routes/status.py", "claims-api/tests/test_status.py"])

    def test_planned(self):
        entries = ["src/a.py", "web/", "lib/**/*.ts"]
        self.assertTrue(L.planned("src/a.py", entries))
        self.assertTrue(L.planned("web/x/y.css", entries))
        self.assertTrue(L.planned("lib/q/r.ts", entries))
        self.assertFalse(L.planned("src/b.py", entries))


class GitTest(unittest.TestCase):
    def test_repo_root_and_changed_files(self):
        d = make_repo()
        (d / "a.txt").write_text("1", encoding="utf-8")
        git(d, "add", "a.txt")
        git(d, "commit", "-qm", "init")
        self.assertEqual(L.repo_root(d / "."), d)
        git(d, "checkout", "-qb", "feature")
        (d / "src").mkdir()
        (d / "src/b.py").write_text("x", encoding="utf-8")
        git(d, "add", ".")
        git(d, "commit", "-qm", "b")
        (d / "a.txt").write_text("2", encoding="utf-8")
        (d / "new dir").mkdir()
        (d / "new dir/c.md").write_text("c", encoding="utf-8")
        self.assertEqual(L.changed_files(d, "main"), ["a.txt", "new dir/c.md", "src/b.py"])
        with self.assertRaises(L.SdlcError):
            L.changed_files(d, "no-such-branch")

    def test_repo_root_outside_git(self):
        d = Path(tempfile.mkdtemp()).resolve()
        self.assertEqual(L.repo_root(d), d)
        self.assertFalse(L.in_git_repo(d))

    def test_parse_ts(self):
        a = L.parse_ts("2026-10-08T19:40:00Z")
        b = L.parse_ts("2026-10-08T21:40:00+02:00")
        self.assertEqual(a, b)
        self.assertIsNone(L.parse_ts("garbage"))
        self.assertRegex(L.now_iso(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


class AssetSchemaTest(unittest.TestCase):
    def test_no_schema_declares_dollar_schema(self):
        # claude -p --json-schema rejects a draft 2020-12 "$schema" URI ("not a valid JSON Schema", exit 1)
        files = sorted((SKILL / "assets" / "schemas").glob("*.json"))
        self.assertTrue(files)
        for f in files:
            self.assertNotIn("$schema", json.loads(f.read_text(encoding="utf-8")), f.name)


class ReviewFixesTest(unittest.TestCase):
    def test_parse_ts_one_grammar(self):
        want = L.parse_ts("2026-10-09T10:15:30Z")
        for t in ("2026-10-09T10:15:30+0000", "2026-10-09T10:15:30+00:00", "2026-10-09 10:15:30 UTC",
                  "2026-10-09T12:15:30+02", "2026-10-09T10:15:30z"):
            self.assertEqual(L.parse_ts(t), want, t)
        self.assertEqual(L.parse_ts("2026-10-09T10:15:30.12345Z").microsecond, 123450)
        self.assertEqual(L.parse_ts("2026-10-09"), L.parse_ts("2026-10-09T00:00:00Z"))
        for bad in ("2026-10-09garbage", "20261009T101530", "2026-13-01", "2026-W41-5", "", None, "2026-10-09T10"):
            self.assertIsNone(L.parse_ts(bad), bad)

    def test_fenced_change_log_does_not_unfreeze_requirements(self):
        doc = ("---\nid: x\n---\n# Spec\n\n## Design\n```markdown\n## Change log\n- example\n```\n\n"
               "## Requirements\n- FR-001 THE SYSTEM SHALL work.\n\n## Change log\n- 2026-10-01 a — b\n")
        fp = L.fingerprint(doc)
        self.assertNotEqual(L.fingerprint(doc.replace("SHALL work", "SHALL not work")), fp)
        self.assertEqual(L.fingerprint(doc + "- 2026-10-02 c — d\n"), fp)
        # a section placed after the change log stays frozen
        tail = doc + "\n## Extra\nfrozen text\n"
        self.assertNotEqual(L.fingerprint(tail.replace("frozen text", "edited")), L.fingerprint(tail))

    def test_frontmatter_round_trip_yaml11_words_and_list_of_maps(self):
        d = Path(tempfile.mkdtemp())
        p = d / "x.md"
        p.write_text('---\ntitle: "no"\nreviewers:\n  - {name: Ana, role: security}\nnested: {a: {b: 1}}\n---\nbody\n',
                     encoding="utf-8")
        L.update_frontmatter(p, approved_by="Yes", status="approved")
        fm = L._split_frontmatter_raw(p.read_text(encoding="utf-8"))[0]
        want = {"title": "no", "reviewers": [{"name": "Ana", "role": "security"}], "nested": {"a": {"b": 1}},
                "approved_by": "Yes", "status": "approved"}
        self.assertEqual(L.yaml_lite_load(fm), want)
        try:
            import yaml  # type: ignore
        except ImportError:
            return
        self.assertEqual(yaml.safe_load(fm), want)

    def test_self_protected_paths(self):
        root = SKILL.parents[2]
        paths = L.self_protected_paths(root, L.DEFAULT_CONFIG)
        self.assertEqual(paths[:3], [".claude/sdlc.json", ".claude/settings.json", ".claude/settings.local.json"])
        self.assertIn(SKILL.relative_to(root).as_posix() + "/", paths)
        self.assertTrue(L.match_any(SKILL.relative_to(root).as_posix() + "/scripts/hooks.py", paths))
        outside = Path(tempfile.mkdtemp()).resolve()
        self.assertEqual(len(L.self_protected_paths(outside)), 4)
        self.assertTrue(L.match_any(".claude/agents/sdlc-gate.md", paths))
        self.assertFalse(L.match_any(".claude/agents/other.md", paths))
        self.assertEqual(L.self_protected_paths(root, {"self_protect": False}), [])
        self.assertTrue(L.DEFAULT_CONFIG["self_protect"])

    def test_template_config_matches_defaults(self):
        tpl = json.loads((SKILL / "assets/templates/sdlc.json").read_text(encoding="utf-8"))
        self.assertEqual(tpl, L.DEFAULT_CONFIG)

    def test_production_patterns(self):
        import re
        pats = [re.compile(p, re.I) for p in L.DEFAULT_CONFIG["production_gate"]["bash_patterns"]]
        hit = lambda c: any(rx.search(c) for rx in pats)  # noqa: E731
        for c in ("terraform -chdir=infra/prod apply -auto-approve", "terraform apply", "terraform destroy",
                  "kubectl --context prod-eu apply -f k8s/", "kubectl apply -f k8s/ --context=prod-eu",
                  "kubectl --cluster prod patch deploy x", "kubectl -n prod edit deploy x",
                  "helm --kube-context prod-eu upgrade api ./chart", "helm upgrade api ./chart --kube-context prod",
                  "make deploy ENV=prod", "npm run deploy:prod", "./deploy-prod.sh",
                  'git commit -m "$(./deploy.sh prod)"', "echo x && ./deploy.sh prod"):
            self.assertTrue(hit(c), c)
        for c in ("kubectl -n prod get pods", "kubectl --context prod-eu get pods", "terraform plan",
                  "helm --kube-context prod-eu list"):
            self.assertFalse(hit(c), c)


class HumanEventTrustTest(unittest.TestCase):
    def test_trusted_events_and_hooks_configured(self):
        import datetime as dt
        root = Path(tempfile.mkdtemp(prefix="sdlc-he-")).resolve()
        now = dt.datetime(2026, 10, 9, 12, 0, 0, tzinfo=dt.timezone.utc)
        recs = [{"ts": "2026-10-09T12:00:05Z", "type": "prompt", "session_id": "s", "excerpt": "ok"},  # +5 s: kept
                {"ts": "2026-10-09T12:00:06Z", "type": "prompt", "session_id": "s", "excerpt": "future"},
                {"ts": "2026-10-09T11:00:00Z", "type": "prompt", "excerpt": "no session"},
                {"ts": "2026-10-09T11:00:00Z", "type": "prompt", "session_id": 7, "excerpt": "odd session"},
                {"ts": "2026-10-09T11:00:00Z", "type": "prompt", "session_id": "s", "excerpt": " <task-notification>x"},
                {"ts": "2026-10-09T11:00:00Z", "type": "ask_answer", "session_id": "s", "excerpt": "<task-notification>"},
                {"ts": "garbage", "type": "prompt", "session_id": "s", "excerpt": "bad ts"}]
        L.state_dir(root)
        L.human_events_path(root).write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
        kept = [e["excerpt"] for e in L.trusted_human_events(root, now)]
        self.assertEqual(kept, ["ok", "<task-notification>"])
        self.assertEqual(len(L.human_events(root)), len(recs))  # the raw reader is unchanged
        self.assertFalse(L.hooks_configured(root))
        (root / ".claude" / "settings.local.json").write_text('{"hooks": {"Stop": [{"hooks": [{"type": "command", '
                                                             '"command": "python3 x/hooks.py stop"}]}]}}', encoding="utf-8")
        self.assertTrue(L.hooks_configured(root))
        self.assertIsNone(L.plugin_root())  # the source tree is not a packaged plugin

    def test_duplicate_hook_copies_count_once(self):
        root = Path(tempfile.mkdtemp(prefix="sdlc-he-")).resolve()
        rec = {"ts": "2020-01-01T11:00:00Z", "type": "prompt", "session_id": "s", "sha256": "a" * 64, "excerpt": "ok"}
        recs = [dict(rec, id="one"), dict(rec, id="two"),  # project hooks and a plugin copy both ran
                dict(rec, id="three", type="ask_answer"), dict(rec), dict(rec)]  # id-less records are left alone
        L.state_dir(root)
        L.human_events_path(root).write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
        kept = L.trusted_human_events(root)
        self.assertEqual([e.get("id") for e in kept], ["one", "three", None, None])


if __name__ == "__main__":
    unittest.main()
