#!/usr/bin/env python3
"""sdlc.py — change, artifact and gate management for the ai-native-sdlc skill.

Stdlib only, Python 3.9+. Shells out to `git` only (never the network).
Exit codes: 0 ok, 1 validation or gate failure, 2 usage or environment error.
Run `sdlc.py <command> -h` for each command's options.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.dont_write_bytecode = True  # keep scripts/__pycache__ out of the installed skill
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sdlc_lib as L  # noqa: E402

ASSETS = L.SKILL_DIR / "assets"
TEMPLATES = ASSETS / "templates"
SOURCES = ("human", "ticket", "incident", "scan", "review")
LOG_EVENTS = ("RULING", "VERIFIED", "NOTE", "PR_OPENED")
RESERVED_WORDS = {"claude", "anthropic", "assistant", "bot", "llm", "gpt", "copilot", "sdlc"}
RESERVED_WHOLE = {"ai", "agent", "model", "system"}  # whole-name only: real names like "Ai Nakamura" pass
APPROVAL_WORDS = re.compile(r"\b(approved?|approves|yes|yep|lgtm|ok|okay|ship(\s+it)?|go\s+ahead|sounds\s+good)\b", re.I)


def reserved_approver(name: str) -> bool:
    """True if name is not a human's: an agent/bot word anywhere, a -bot/[bot] suffix, or a generic name."""
    n = name.strip().lower()
    words = [w for w in re.split(r"[^a-z0-9]+", n) if w]
    return (not words or n in RESERVED_WHOLE or " ".join(words) in RESERVED_WHOLE
            or any(w in RESERVED_WORDS for w in words) or n.endswith("-bot") or n.endswith("[bot]"))
PLACEHOLDER = "_Not covered in plan mode — fill in_"


class GateError(Exception):
    """Validation or gate failure (exit 1)."""


def out(msg: str = "") -> None:
    print(msg)


# =========================================================================== context

class Ctx:
    def __init__(self, repo: Optional[str]):
        self.root = L.repo_root(repo or os.getcwd())
        self.cfg = L.load_config(self.root)
        self.cli = L.cli_prefix(self.root)

    def cdir(self, cid: str) -> Path:
        return L.change_dir(self.root, self.cfg, cid)

    def rel(self, p: Path) -> str:
        try:
            return p.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return str(p)


def _git_user(root: Path) -> str:
    try:
        proc = L.git(["-C", str(root), "config", "user.name"], check=False)
        return proc.stdout.strip()
    except L.SdlcError:
        return ""


def _ci_truthy() -> bool:
    return os.environ.get("CI", "").strip().lower() not in ("", "0", "false", "no", "off")


def _norm_gate(name: str) -> str:
    g = name.lower()
    if g.endswith(".md"):
        g = g[:-3]
    if g not in L.GATES:
        raise L.SdlcError("unknown gate/artifact %r (use one of: %s)" % (name, ", ".join(L.GATES)))
    return g


# =========================================================================== validation

INTENT_SECTIONS = [("Problem", "error"), ("Proposed outcome", "error"),
                   ("Affected users and systems", "error"), ("Constraints", "error"),
                   ("Assumptions", "error"), ("Open questions", "error"), ("Change log", None)]
BUG_SECTIONS = [("Current behavior", "error"), ("Expected behavior", "error"),
                ("Unchanged behavior", "error"), ("Evidence", "error"),
                ("Affected users and systems", "error"), ("Constraints", "error"),
                ("Open questions", "error"), ("Change log", None)]
SPEC_SECTIONS = [("Summary", "error"), ("Requirements", "error"), ("Success criteria", "error"),
                 ("Design", "error"), ("Policy conformance", "strict"), ("Flagged concerns", "strict"),
                 ("Out of scope", "strict"), ("Open questions", "strict"), ("Clarifications", None),
                 ("Change log", None)]
PLAN_SECTIONS = [("Goal", "error"), ("Files that change", "error"), ("Order of work", "error"),
                 ("Risks", "strict"), ("Proof", "strict"), ("Rollback", "warn"),
                 ("Review focus", "warn"), ("Change log", None)]
OPTIONAL_HEADINGS = {"change log", "clarifications"}
NONE_OK = {"constraints", "assumptions", "open questions", "out of scope", "flagged concerns",
           "policy conformance", "risks", "rollback", "review focus", "evidence"}
STATUS_VALUES = {"intent": ("draft", "approved", "rejected", "done", "dropped"),
                 "spec": ("draft", "approved", "rejected"),
                 "plan": ("draft", "approved", "rejected")}

FR_DEF = re.compile(r"^\s*(?:[-*+]|\d+[.)]|#{3,6})?\s*(?:\*\*|__)?\s*\[?(FR-\d{3})\b", re.M)
SC_DEF = re.compile(r"^\s*(?:[-*+]|\d+[.)]|#{3,6})?\s*(?:\*\*|__)?\s*\[?(SC-\d{3})\b", re.M)
ID_ANY = re.compile(r"\b((?:FR|SC)-\d{3})\b")
CLARIFY = re.compile(r"\[NEEDS CLARIFICATION:", re.I)
TASK_RE = re.compile(r"^\s*(?:\d+[.)]|[-*+]\s+\[[ xX]\])\s+\S", re.M)


class Finding:
    def __init__(self, level: str, artifact: str, where: str, msg: str, fix: str = ""):
        self.level, self.artifact, self.where, self.msg, self.fix = level, artifact, where, msg, fix

    def line(self) -> str:
        loc = self.artifact + ((" [%s]" % self.where) if self.where else "")
        return "%-5s %s: %s%s" % (self.level, loc, self.msg, (" Fix: " + self.fix) if self.fix else "")

    def as_dict(self) -> dict:
        return {"level": self.level, "artifact": self.artifact, "section": self.where,
                "message": self.msg, "fix": self.fix}


def parse_table(text: str) -> Tuple[List[str], List[Dict[str, str]]]:
    """First markdown table in text -> (lower-case headers, rows as dicts)."""
    headers: List[str] = []
    rows: List[Dict[str, str]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            if headers:
                break
            continue
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", s.strip().strip("|"))]
        if not headers:
            headers = [c.lower() for c in cells]
            continue
        if all(re.match(r"^:?-{2,}:?$", c) for c in cells if c):
            continue
        rows.append({headers[i] if i < len(headers) else str(i): c for i, c in enumerate(cells)})
    return headers, rows


def _is_none_text(text: str) -> bool:
    return bool(re.match(r"^\s*(none|n/a|no concerns?)\b", text, re.I))


def _clean(text: str) -> str:
    return L.strip_comments(text).strip()


def validate_artifact(ctx: Ctx, cid: str, gate: str, strict: bool) -> List[Finding]:
    cdir = ctx.cdir(cid)
    art = L.GATE_ARTIFACT[gate]
    path = cdir / art
    F: List[Finding] = []

    def add(level: str, where: str, msg: str, fix: str = "") -> None:
        F.append(Finding(level, art, where, msg, fix))

    def lvl(rule: str) -> str:
        return "ERROR" if rule == "error" or (rule == "strict" and strict) else "WARN"

    if not path.is_file():
        add("ERROR", "", "file missing", "create %s from assets/templates/%s" % (ctx.rel(path), art))
        return F
    text = path.read_text(encoding="utf-8")
    try:
        meta, body = L.parse_frontmatter(text)
    except ValueError as exc:
        add("ERROR", "frontmatter", "unparseable frontmatter (%s)" % exc, "fix the YAML between the '---' lines")
        meta, body = {}, L._split_frontmatter_raw(text)[1]

    # placeholders
    ph = sorted(set(re.findall(r"\{\{\s*([^}]*?)\s*\}\}", text)))
    if "{{" in text:
        add("ERROR", "", "unfilled template placeholders: %s" % ", ".join("{{%s}}" % p for p in ph[:8]),
            "replace every {{...}} with real content")

    # frontmatter
    if meta.get("id") not in (None, "") and str(meta.get("id")) != cid and "{{" not in str(meta.get("id")):
        add("ERROR", "frontmatter", "id %r does not match the change id %r" % (meta.get("id"), cid), "set id: %s" % cid)
    if not meta.get("id"):
        add("WARN", "frontmatter", "missing id", "add id: %s" % cid)
    st = meta.get("status")
    if st is not None and str(st) not in STATUS_VALUES[gate]:
        add("ERROR", "frontmatter", "status %r is not one of %s" % (st, "|".join(STATUS_VALUES[gate])), "set a valid status")
    if not meta.get("title"):
        add("ERROR" if gate == "intent" else "WARN", "frontmatter", "missing title", "add title: <short title>")

    secs = L.sections(body)
    is_bug = gate == "intent" and "current behavior" in secs
    spec_list = {"intent": BUG_SECTIONS if is_bug else INTENT_SECTIONS,
                 "spec": SPEC_SECTIONS, "plan": PLAN_SECTIONS}[gate]
    for name, rule in spec_list:
        key = name.lower()
        if key not in secs:
            level = "WARN" if key in OPTIONAL_HEADINGS else "ERROR"
            add(level, name, "heading '## %s' missing" % name, "add the section (template: assets/templates/%s)" % art)
            continue
        content = _clean(secs[key])
        if content == PLACEHOLDER:
            add("WARN", name, "placeholder text from plan mode", "fill in the section")
            continue
        if not content and rule:
            add(lvl(rule), name, "section is empty",
                "write it, or 'None' if it genuinely does not apply" if key in NONE_OK
                else "write it (the template comment says what belongs here)")

    cl_spans = L._changelog_spans(body.replace("\r\n", "\n").split("\n"))
    if len(cl_spans) > 1:
        add("WARN", "Change log", "'## Change log' appears %d times; only the last is outside the fingerprint"
            % len(cl_spans), "keep one '## Change log' section, at the end")
    elif cl_spans and cl_spans[-1][1] < len(body.replace("\r\n", "\n").split("\n")):
        add("WARN", "Change log", "'## Change log' is not the last section",
            "move it to the end of the file")
    clean = {k: _clean(v) for k, v in secs.items()}
    if gate == "intent":
        tier = meta.get("tier")
        if tier not in L.TIERS or tier == "trivial":
            add("ERROR", "frontmatter", "tier %r must be one of bounded|standard|high-risk" % (tier,),
                "set tier (trivial changes need no change folder)")
        src = meta.get("source")
        if src not in SOURCES:
            add("WARN", "frontmatter", "source %r is not one of %s" % (src, "|".join(SOURCES)), "set source")
        if not meta.get("author"):
            add("WARN", "frontmatter", "missing author", "add author: <originator>")
        prob = clean.get("current behavior" if is_bug else "problem", "")
        words = len(prob.split())
        if words > 250:
            add("WARN", "Problem", "%d words; intent is a proto-spec, not a spec" % words,
                "cut to the problem in under 250 words; move design to spec.md")
        if is_bug:
            if clean.get("expected behavior") and not re.search(r"\bSHALL\b", clean["expected behavior"]):
                add("WARN", "Expected behavior", "no 'SHALL' clause", "write 'WHEN <condition> THEN the system SHALL <behavior>'")
            if clean.get("unchanged behavior") and not re.search(r"SHALL CONTINUE TO", clean["unchanged behavior"]):
                add("WARN", "Unchanged behavior", "no 'SHALL CONTINUE TO' clause",
                    "write 'WHEN <condition> THEN the system SHALL CONTINUE TO <behavior>'")
    elif gate == "spec":
        _validate_spec(ctx, cdir, meta, body, clean, strict, add)
    else:
        _validate_plan(ctx, cdir, meta, text, clean, strict, add)
    return F


def _blocks(text: str, rx: "re.Pattern[str]") -> List[Tuple[str, str]]:
    ms = list(rx.finditer(text))
    return [(m.group(1), text[m.start():(ms[i + 1].start() if i + 1 < len(ms) else len(text))])
            for i, m in enumerate(ms)]


def _validate_spec(ctx, cdir, meta, body, clean, strict, add) -> None:
    risk = meta.get("risk")
    if risk in (None, ""):
        add("ERROR" if strict else "WARN", "frontmatter", "missing risk", "set risk: low|medium|high")
    elif risk not in ("low", "medium", "high") and "{{" not in str(risk):
        add("ERROR", "frontmatter", "risk %r is not low|medium|high" % (risk,), "set risk: low|medium|high")
    if "skills" in meta and not isinstance(meta.get("skills"), list):
        add("WARN", "frontmatter", "skills should be a list", "skills: [policy-skill@version, ...]")
    isha = meta.get("intent_sha")
    cur = L.file_fingerprint(cdir / "intent.md")
    if not isha:
        add("ERROR" if strict else "WARN", "frontmatter", "missing intent_sha",
            "set intent_sha to the intent fingerprint: %s" % (cur or "?"))
    elif cur and "{{" not in str(isha) and str(isha) != cur:
        add("WARN", "frontmatter", "intent.md changed since this spec was written (intent_sha differs)",
            "re-check the spec against intent.md, then set intent_sha: %s" % cur)

    req = clean.get("requirements", "")
    if req:
        if not ID_ANY.search(req) or not re.search(r"\bFR-\d{3}\b", req):
            add("ERROR", "Requirements", "no FR-### requirement IDs",
                "add requirements like '- FR-001 <name>. WHEN <event> THE SYSTEM SHALL <behavior>.'")
        seen: Dict[str, int] = {}
        for rid, block in _blocks(req, FR_DEF):
            seen[rid] = seen.get(rid, 0) + 1
            if not re.search(r"\bSHALL\b", block, re.I):
                add("WARN", "Requirements", "%s has no EARS 'SHALL' acceptance line" % rid,
                    "add 'WHEN … THE SYSTEM SHALL …' (or THE SYSTEM SHALL / IF … THEN / WHILE …)")
        for rid, n in seen.items():
            if n > 1:
                add("ERROR", "Requirements", "%s is defined %d times" % (rid, n), "give each requirement a unique ID")
    sc = clean.get("success criteria", "")
    if sc:
        if not re.search(r"\bSC-\d{3}\b", sc):
            add("ERROR", "Success criteria", "no SC-### success criteria IDs",
                "add measurable criteria like '- SC-001 p95 latency under 300 ms'")
        for sid, block in _blocks(sc, SC_DEF):
            if not re.search(r"\d", block[len(sid):]):
                add("WARN", "Success criteria", "%s has no number; is it measurable?" % sid,
                    "state a threshold, rate, count or time limit")

    n_clar = len(CLARIFY.findall(L.strip_comments(body)))
    if n_clar > 3:
        add("ERROR", "", "%d [NEEDS CLARIFICATION] markers (max 3)" % n_clar,
            "ask the product owner, or record informed guesses under Assumptions/Clarifications")
    elif n_clar and strict:
        add("ERROR", "", "%d [NEEDS CLARIFICATION] marker(s) left" % n_clar,
            "resolve them and record the answers under '## Clarifications'")
    elif n_clar:
        add("WARN", "", "%d [NEEDS CLARIFICATION] marker(s)" % n_clar, "resolve before approval")

    concern_policies: List[str] = []
    fc = clean.get("flagged concerns", "")
    if fc and not _is_none_text(fc):
        headers, rows = parse_table(fc)
        need = ["id", "concern", "policy", "owner", "status", "resolution"]
        if not headers or any(h not in headers for h in need):
            add("ERROR", "Flagged concerns", "expected a table | ID | Concern | Policy | Owner | Status | Resolution |",
                "use the template table, or write 'None'")
        else:
            for r in rows:
                s = r.get("status", "").strip().lower()
                concern_policies.append(r.get("policy", "").lower())
                if s not in ("open", "resolved", "accepted", "carried"):
                    add("ERROR", "Flagged concerns", "%s: status %r is not open|resolved|accepted|carried" % (r.get("id", "?"), s),
                        "set a valid status")
                elif s == "open":
                    add("ERROR" if strict else "WARN", "Flagged concerns", "%s is still open" % r.get("id", "?"),
                        "the policy owner resolves, accepts or carries it before approval")
    pc = clean.get("policy conformance", "")
    if pc and not _is_none_text(pc):
        headers, rows = parse_table(pc)
        if not headers or "status" not in headers:
            add("ERROR" if strict else "WARN", "Policy conformance", "expected a table | Policy / skill | Status | Notes |",
                "list each policy skill applied")
        else:
            if not rows:
                add("ERROR" if strict else "WARN", "Policy conformance", "table has no rows",
                    "add one row per policy skill applied, or write 'None' if no policy applies")
            pol_col = headers[0]
            for r in rows:
                s = r.get("status", "").strip().lower()
                if s not in ("compliant", "non-compliant", "n/a"):
                    add("ERROR", "Policy conformance", "%s: status %r is not compliant|non-compliant|n/a" % (r.get(pol_col, "?"), s),
                        "set a valid status")
                elif s == "non-compliant":
                    name = r.get(pol_col, "").lower().split("@")[0].strip()
                    if not any(name and (name in p or p in name) for p in concern_policies if p):
                        add("ERROR" if strict else "WARN", "Policy conformance",
                            "%s is non-compliant but has no Flagged concerns row" % r.get(pol_col, "?"),
                            "add a flagged concern naming this policy, with an owner")


def _validate_plan(ctx, cdir, meta, text, clean, strict, add) -> None:
    files = L.plan_files(text)
    if clean.get("files that change") and not files:
        add("ERROR", "Files that change", "no file paths found",
            "list repo-relative paths, one per line, e.g. '- `src/app/routes.py`'")
    for f in files:
        if f.startswith("/") or ".." in f.split("/"):
            add("WARN", "Files that change", "%r is not a repo-relative path" % f, "use a path relative to the repo root")
    ow = clean.get("order of work", "")
    if "order of work" in clean and ow and ow != PLACEHOLDER and not TASK_RE.search(ow):
        add("ERROR", "Order of work", "no numbered or checkbox tasks",
            "write tasks as '1. …' or '- [ ] T1 …' (tag parallel-safe tasks [P], cite FR IDs)")
    proof = clean.get("proof", "")
    if proof and proof != PLACEHOLDER:
        has_cmd = "`" in proof
        has_test = any(L.match_any(tok.strip("`.,;:()"), ctx.cfg.get("test_globs", [])) or "test" in tok.lower()
                       for tok in proof.split())
        if not (has_cmd or has_test):
            add("ERROR" if strict else "WARN", "Proof", "names no command (in backticks) or test file",
                "write the exact commands, e.g. `make test`, and the test files that prove each FR")
    spec_cur = L.file_fingerprint(cdir / "spec.md")
    ssha = meta.get("spec_sha")
    if spec_cur:
        if not ssha:
            add("WARN", "frontmatter", "missing spec_sha", "set spec_sha: %s" % spec_cur)
        elif "{{" not in str(ssha) and str(ssha) != spec_cur:
            add("WARN", "frontmatter", "spec.md changed since this plan was written (spec_sha differs)",
                "re-check the plan against spec.md, then set spec_sha: %s" % spec_cur)
        spec_text = (cdir / "spec.md").read_text(encoding="utf-8")
        spec_ids = set(ID_ANY.findall(L.strip_comments(spec_text)))
        for rid in sorted(set(ID_ANY.findall(L.strip_comments(text))) - spec_ids):
            add("WARN", "", "%s is referenced but not defined in spec.md" % rid, "fix the reference or the spec")
    isha = meta.get("intent_sha")
    icur = L.file_fingerprint(cdir / "intent.md")
    if icur and isha and "{{" not in str(isha) and str(isha) != icur:
        add("WARN", "frontmatter", "intent.md changed since this plan was written (intent_sha differs)",
            "re-check, then set intent_sha: %s" % icur)


def _print_findings(findings: List[Finding]) -> Tuple[int, int]:
    e = sum(1 for f in findings if f.level == "ERROR")
    w = len(findings) - e
    for f in sorted(findings, key=lambda f: (f.level != "ERROR",)):
        out(f.line())
    return e, w


# =========================================================================== commands

def cmd_validate(ctx: Ctx, a) -> int:
    cdir = ctx.cdir(a.id)
    if a.artifact:
        gates = [_norm_gate(a.artifact)]
    else:
        gates = [g for g in L.GATES if (cdir / L.GATE_ARTIFACT[g]).is_file()] or ["intent"]
        if "intent" not in gates:
            gates.insert(0, "intent")
    findings: List[Finding] = []
    for g in gates:
        findings.extend(validate_artifact(ctx, a.id, g, a.strict))
    if a.json:
        out(json.dumps({"id": a.id, "strict": a.strict, "findings": [f.as_dict() for f in findings],
                        "errors": sum(f.level == "ERROR" for f in findings)}, indent=2))
        return 1 if any(f.level == "ERROR" for f in findings) else 0
    e, w = _print_findings(findings)
    out("%s %s: %d error(s), %d warning(s)%s" % (
        "FAIL" if e else "OK", ", ".join(L.GATE_ARTIFACT[g] for g in gates), e, w, " (strict)" if a.strict else ""))
    return 1 if e else 0


# ---------------------------------------------------------------- new

def _slugify(s: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", s.lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")
    return slug[:40].rstrip("-")


def _fill_section(body: str, heading: str, content: str) -> str:
    lines = body.split("\n")
    out_lines: List[str] = []
    i = 0
    target = heading.lower()
    while i < len(lines):
        line = lines[i]
        out_lines.append(line)
        m = re.match(r"^##\s+(.*?)\s*$", line)
        if m and m.group(1).lower() == target:
            j = i + 1
            while j < len(lines) and not re.match(r"^#{1,2}\s", lines[j]):
                j += 1
            out_lines.extend([content.rstrip(), ""])
            i = j
            continue
        i += 1
    return "\n".join(out_lines)


def _bullets(items: Any, empty: str = "None") -> str:
    items = [str(x).strip() for x in (items or []) if str(x).strip()]
    return "\n".join("- %s" % x for x in items) if items else empty


def _check_schema(obj: Any, schema: dict, path: str = "$") -> List[str]:
    """Validate the JSON Schema subset used by assets/schemas/*.json."""
    errs: List[str] = []
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}
    if t and not isinstance(obj, types.get(t, object)) or (t in ("integer", "number") and isinstance(obj, bool)):
        return ["%s: expected %s" % (path, t)]
    if "enum" in schema and obj not in schema["enum"]:
        errs.append("%s: %r not in %s" % (path, obj, schema["enum"]))
    if isinstance(obj, str):
        if len(obj) < schema.get("minLength", 0):
            errs.append("%s: shorter than %d" % (path, schema["minLength"]))
        if "maxLength" in schema and len(obj) > schema["maxLength"]:
            errs.append("%s: longer than %d" % (path, schema["maxLength"]))
    if isinstance(obj, list):
        if len(obj) < schema.get("minItems", 0):
            errs.append("%s: fewer than %d items" % (path, schema["minItems"]))
        if "maxItems" in schema and len(obj) > schema["maxItems"]:
            errs.append("%s: more than %d items" % (path, schema["maxItems"]))
        for i, item in enumerate(obj):
            errs.extend(_check_schema(item, schema.get("items", {}), "%s[%d]" % (path, i)))
    if isinstance(obj, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in obj:
                errs.append("%s: missing required %r" % (path, k))
        for k, v in obj.items():
            if k in props:
                errs.extend(_check_schema(v, props[k], "%s.%s" % (path, k)))
            elif schema.get("additionalProperties") is False:
                errs.append("%s: unexpected property %r" % (path, k))
    return errs


def _load_diagnosis(path: str) -> dict:
    try:
        raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise L.SdlcError("cannot read --from-json %s: %s" % (path, exc))
    schema = json.loads((ASSETS / "schemas" / "intent.schema.json").read_text(encoding="utf-8"))
    errs = _check_schema(data, schema)
    if errs:
        raise GateError("--from-json does not match intent.schema.json:\n  " + "\n  ".join(errs[:20]))
    return data


def cmd_new(ctx: Ctx, a) -> int:
    diag = _load_diagnosis(a.from_json) if a.from_json else None
    tier = a.tier or (diag or {}).get("tier") or "standard"
    if tier == "trivial":
        raise GateError("trivial changes need no change folder: just make the change, verify it and open a PR")
    if tier not in L.TIERS:
        raise L.SdlcError("unknown tier %r (use one of %s)" % (tier, ", ".join(L.TIERS)))
    slug = _slugify(a.slug)
    if not slug:
        raise L.SdlcError("slug %r has no usable characters [a-z0-9-]" % a.slug)
    rec = a.record_id or ""
    created = L.now_iso()
    if rec:
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", rec):
            raise L.SdlcError("record id %r may contain only letters, digits, '.', '_' and '-'" % rec)
        pat = ctx.cfg.get("record_id_pattern") or ""
        if pat and not re.fullmatch(pat, rec):
            raise GateError("record id %r does not match record_id_pattern %r in .claude/sdlc.json" % (rec, pat))
        base = "%s-%s" % (rec, slug)
    else:
        base = "%s-%s" % (created[:10].replace("-", ""), slug)  # UTC date of `created`
    croot = L.changes_dir(ctx.root, ctx.cfg)
    cid, n = base, 2
    while (croot / cid).exists():
        cid, n = "%s-%d" % (base, n), n + 1
    title = a.title or (diag or {}).get("title") or slug.replace("-", " ").capitalize()
    author = a.author or _git_user(ctx.root) or "unknown"
    source = a.source or ("incident" if diag else "human")
    tpl_name = "intent-bug.md" if a.bug else "intent.md"
    tpl = (TEMPLATES / tpl_name)
    if not tpl.is_file():
        raise L.SdlcError("template missing: %s" % tpl)
    tmeta, tbody = L.parse_frontmatter(tpl.read_text(encoding="utf-8"))
    values = {"id": cid, "title": title, "author": author, "status": "draft", "tier": tier,
              "source": source, "record_id": rec, "created": created}
    meta = {k: values.get(k, v) for k, v in tmeta.items()}
    for k, v in values.items():
        meta.setdefault(k, v)
    body = tbody
    for k, v in values.items():
        body = body.replace("{{%s}}" % k, str(v))
    if diag:
        ev = _bullets(diag.get("evidence"))
        sev = "Severity (automated diagnosis): %s." % diag.get("severity")
        if a.bug:
            body = _fill_section(body, "Current behavior", diag["problem"])
            body = _fill_section(body, "Expected behavior", diag["proposed_outcome"])
            body = _fill_section(body, "Evidence", sev + "\n\n" + ev)
        else:
            body = _fill_section(body, "Problem", "%s\n\n%s Evidence:\n\n%s" % (diag["problem"], sev, ev))
            body = _fill_section(body, "Proposed outcome", diag["proposed_outcome"])
            body = _fill_section(body, "Assumptions",
                                 "Written from an automated diagnosis (source: %s). Verify the evidence before approving." % source)
        body = _fill_section(body, "Affected users and systems", _bullets(diag.get("affected_systems")))
        body = _fill_section(body, "Constraints", _bullets(diag.get("constraints")))
        body = _fill_section(body, "Open questions", _bullets(diag.get("open_questions")))
    cdir = croot / cid
    cdir.mkdir(parents=True)
    (cdir / "intent.md").write_text(L.dump_frontmatter(meta, body), encoding="utf-8")
    L.audit_append(cdir, "CHANGE_CREATED", tier=tier, source=source, by=author,
                   record_id=rec or None, template=tpl_name, from_json=("yes" if diag else None))
    if not a.no_activate:
        L.set_active(ctx.root, cid)
    rel = ctx.rel(cdir / "intent.md")
    if a.json:
        out(json.dumps({"id": cid, "path": rel, "tier": tier, "active": not a.no_activate}))
        return 0
    gates = L.required_gates(tier, source)
    out("id: %s" % cid)
    out("path: %s" % rel)
    out("tier: %s (%d gate(s): %s)%s" % (tier, len(gates), ", ".join(gates) or "none",
                                         "" if a.no_activate else "; now the active change"))
    try:
        p = L.git(["-C", str(ctx.root), "symbolic-ref", "-q", "--short", "HEAD"], check=False)
        branch = p.stdout.strip() if p.returncode == 0 else ""
    except L.SdlcError:
        branch = ""
    if branch != "change/" + cid:
        sw = ("git branch -m change/%s" if branch == "change/" + slug else "git switch -c change/%s") % cid
        out("branch: on %s; run `%s` before the first artifact commit" % (branch or "a detached HEAD", sw))
    fill = "fill in %s with Write/Edit (see references/stage-1-plan.md)" % rel
    if "intent" in gates:
        out("next: %s, then `%s present %s intent`" % (fill, ctx.cli, cid))
    else:
        out("next: %s, then the plan: plan mode and `%s plan-from-accepted %s --by \"<engineer>\"`, or write "
            "plan.md from assets/templates/plan.md%s and `%s present %s plan`"
            % (fill, ctx.cli, cid, " (task T1 = a failing test)" if a.bug else "", ctx.cli, cid))
    return 0


# ---------------------------------------------------------------- status

def _gate_word(st: dict) -> str:
    if not st["exists"]:
        return "missing"
    if st["current"]:
        return "approved"
    if st["pending"]:
        return "presented"
    if st["approved"]:
        return "stale"
    if st["rejected"]:
        return "rejected"
    return "draft"


def _gates_line(s: dict) -> str:
    parts = []
    for g in L.GATES:
        st = s["gates"][g]
        if g not in s["required_gates"] and not st["exists"]:
            continue
        parts.append("%s %s%s" % (g, _gate_word(st), "" if g in s["required_gates"] else " (optional)"))
    return ", ".join(parts) or "no gates (trivial)"


def _open_changes(ctx: Ctx, include_closed: bool) -> List[dict]:
    res = []
    for cid in L.list_changes(ctx.root, ctx.cfg):
        try:
            s = L.change_status(ctx.root, ctx.cfg, cid)
        except L.SdlcError:
            continue
        if include_closed or s["stage"] not in ("closed", "dropped"):
            res.append(s)
    return res


def cmd_status(ctx: Ctx, a) -> int:
    if a.id:
        s = L.change_status(ctx.root, ctx.cfg, a.id)
        if a.json:
            out(json.dumps(s, indent=2))
            return 0
        _print_change(ctx, s)
        return 0
    active = L.get_active(ctx.root)
    act = None
    if active:
        try:
            act = L.change_status(ctx.root, ctx.cfg, active)
        except L.SdlcError:
            act = None
    changes = _open_changes(ctx, a.all)
    if a.json:
        out(json.dumps({"root": str(ctx.root), "configured": (ctx.root / ".claude" / "sdlc.json").is_file(),
                        "active": act, "changes": changes}, indent=2))
        return 0
    if act:
        out("Active change:")
        _print_change(ctx, act, indent="  ")
    else:
        out("No active change%s." % (" (%s no longer exists)" % active if active else ""))
    out("")
    out("%s changes (%d):" % ("All" if a.all else "Open", len(changes)))
    for s in changes:
        out("  %-40s %-9s %-24s %s" % (s["id"], s["tier"], L.STAGE_LABEL.get(s["stage"], s["stage"]), s["title"][:50]))
    if not changes:
        out("  none. Start one with `%s new <slug> --title \"…\" --tier <tier>`" % ctx.cli)
    return 0


def _print_change(ctx: Ctx, s: dict, indent: str = "") -> None:
    out("%s%s — %s" % (indent, s["id"], s["title"]))
    out("%s  tier: %s   stage: %s   status: %s%s" % (indent, s["tier"], L.STAGE_LABEL.get(s["stage"], s["stage"]),
                                                    s["status"],
                                                    "   FIX MODE (tests read-only)" if s["fix_mode"] else ""))
    for g in L.GATES:
        st = s["gates"][g]
        req = g in s["required_gates"]
        if not req and not st["exists"]:
            continue
        extra = ""
        if st["current"] or (st["approved"] and not st["current"]):
            extra = " by %s (%s) at %s" % (st["by"], st["role"], st["ts"])
        out("%s  %-6s %-9s %-10s%s" % (indent, g, "required" if req else "optional", _gate_word(st), extra))
    if s["skipped"]:
        out("%s  skipped stages: %s" % (indent, ", ".join(s["skipped"])))
    if s.get("pr"):
        out("%s  PR: %s" % (indent, s["pr"]))
    out("%s  next: %s" % (indent, s["next"]))


def status_brief(repo: Optional[str]) -> str:
    """At most ~12 plain lines for SKILL.md injection. Never raises."""
    try:
        base = Path(repo or os.getcwd())
        if shutil.which("git") is None:
            return "sdlc: git is not installed or not on PATH; changes, gates and metrics need git."
        try:
            in_repo = L.in_git_repo(base)
        except Exception:
            in_repo = False
        if not in_repo:
            return "sdlc: not inside a git repository (cwd %s). Changes, gates and hooks need a git repo." % base
        root = L.repo_root(base)
        try:
            cfg = L.load_config(root)
        except Exception as exc:
            return "sdlc: config error: %s. Fix .claude/sdlc.json (or delete it to use defaults)." % exc
        cli = L.cli_prefix(root)
        lines = []
        configured = (root / ".claude" / "sdlc.json").is_file()
        hooks = L.hooks_configured(root)
        if not configured or not hooks:
            lines.append("sdlc: repo %s — %s%s. Offer `init` (dry run first)." % (
                root.name, "no .claude/sdlc.json" if not configured else "configured",
                "" if hooks else ", guard hooks not installed"))
        active = L.get_active(root)
        if active:
            try:
                s = L.change_status(root, cfg, active)
                lines.append("active change: %s — %s (tier %s, stage %s%s)" % (
                    s["id"], s["title"][:60], s["tier"], L.STAGE_LABEL.get(s["stage"], s["stage"]),
                    ", FIX MODE" if s["fix_mode"] else ""))
                lines.append("gates: " + _gates_line(s))
                lines.append("next: " + s["next"][:600])
            except Exception as exc:
                lines.append("active change %s unreadable: %s" % (active, str(exc)[:150]))
        else:
            lines.append("no active change.")
        try:
            opened = []
            for cid in L.list_changes(root, cfg):
                if cid == active:
                    continue
                try:
                    st = L.change_status(root, cfg, cid)
                except Exception:
                    continue
                if st["stage"] not in ("closed", "dropped"):
                    opened.append("%s (%s)" % (cid, st["stage"]))
            if opened:
                lines.append("other open changes (%d): %s%s" % (len(opened), ", ".join(opened[:5]),
                                                                ", …" if len(opened) > 5 else ""))
        except Exception:
            pass
        lines.append("cli: %s <command>" % cli)
        if L.headless():
            lines.insert(1, "session: headless — gates are PR reviews; do not approve here")
        return "\n".join(lines[:12])
    except Exception as exc:  # pragma: no cover - defensive
        return "sdlc: status unavailable (%s)" % str(exc)[:200]


# ---------------------------------------------------------------- gates

def _upstream_problems(s: dict, gate: str) -> List[str]:
    probs = []
    for g in L.GATES[: L.GATES.index(gate)]:
        if g in s["required_gates"] and not s["gates"][g]["current"]:
            probs.append("%s gate is %s" % (g, _gate_word(s["gates"][g])))
    return probs


def _open_items(cdir: Path, gate: str) -> List[str]:
    items: List[str] = []
    p = cdir / L.GATE_ARTIFACT[gate]
    try:
        _, body = L.parse_frontmatter(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return items
    secs = {k: _clean(v) for k, v in L.sections(body).items()}
    oq = secs.get("open questions", "")
    if oq and not _is_none_text(oq):
        items.extend("open question: " + ln.strip().lstrip("-*+ ").strip() for ln in oq.splitlines() if ln.strip())
    if gate == "spec":
        _, rows = parse_table(secs.get("flagged concerns", ""))
        for r in rows:
            if r.get("status", "").lower() != "resolved":
                items.append("concern %s (%s, owner %s): %s" % (r.get("id", "?"), r.get("status", "?"),
                                                               r.get("owner", "?"), r.get("concern", "")))
    if gate == "plan":
        risks = secs.get("risks", "")
        if risks:
            items.extend("risk: " + ln.strip().lstrip("-*+ ").strip() for ln in risks.splitlines() if ln.strip())
    return items[:12]


UNLOCKS = {
    "intent": "Stage 2 (Design): write spec.md under the policy skills",
    "spec": "Stage 3 (Build): plan mode → plan.md",
    "plan": "implementation (the plan guard hook allows source edits)",
}


def cmd_present(ctx: Ctx, a) -> int:
    gate = _norm_gate(a.gate)
    cdir = ctx.cdir(a.id)
    findings = validate_artifact(ctx, a.id, gate, strict=True)
    errors = [f for f in findings if f.level == "ERROR"]
    if errors:
        _print_findings(findings)
        out("NOT PRESENTED: fix the %d error(s) above, then run `%s present %s %s` again." % (
            len(errors), ctx.cli, a.id, gate))
        return 1
    s = L.change_status(ctx.root, ctx.cfg, a.id)
    ups = _upstream_problems(s, gate)
    if ups and gate in s["required_gates"]:
        out("NOT PRESENTED: earlier gates must be approved and current first: %s" % "; ".join(ups))
        out("next: %s" % s["next"])
        return 1
    sha = s["gates"][gate]["sha"]
    # Presenting an unchanged artifact again keeps the open window: an approval the human already
    # typed (e.g. after a headless hand-off) must not be pushed out by the agent re-presenting first.
    last = next((ev for ev in reversed(L.audit_read(cdir)) if ev.get("gate") == gate
                 and ev["event"] in ("GATE_PRESENTED", "GATE_APPROVED", "GATE_REJECTED")), None)
    again = bool(last and last["event"] == "GATE_PRESENTED" and last.get("sha256") == sha)
    if not again:
        L.audit_append(cdir, "GATE_PRESENTED", gate=gate, sha256=sha)
    tier = s["tier"]
    art = ctx.rel(cdir / L.GATE_ARTIFACT[gate])
    role = L.approver_role(gate, tier)
    out("=" * 72)
    out("GATE: %s — %s" % (gate, art))
    out("change:    %s — %s" % (a.id, s["title"]))
    out("tier:      %s%s" % (tier, "" if gate in s["required_gates"] else " (gate optional for this tier)"))
    out("approver:  %s" % role)
    out("sha256:    %s" % sha)
    warns = [f for f in findings if f.level == "WARN"]
    if warns:
        out("warnings:")
        for f in warns:
            out("  - " + f.line()[6:])
    items = _open_items(cdir, gate)
    if items:
        out("needs the approver's judgment:")
        for it in items:
            out("  - " + it[:200])
    out("approval unlocks: %s" % UNLOCKS[gate])
    if again:
        out("already presented at %s (unchanged): the approval window is still open, so an approval the "
            "human already gave since then counts" % last["ts"])
    out("-" * 72)
    if L.headless():
        h = L.HEADLESS_HANDOFF.format(gate=gate, id=a.id)
        out(h[0].upper() + h[1:] + ".")
    else:
        allowed = L.allowed_roles(gate, tier)
        rflags = (["product-owner", "tech-lead"] if gate == "spec" else ["tech-lead"]) if allowed else [role.split()[0]]
        out("Ask the approver now (AskUserQuestion: Approve / Request changes / Reject). Only after they answer:")
        for i, rf in enumerate(rflags):
            out("  %s %s approve %s %s --by \"<their name>\" --role %s" % (
                "approve:" if i == 0 else "and:    ", ctx.cli, a.id, gate, rf))
        if len(rflags) > 1:
            out("  (two approvals, each after its own answer: the product owner decides, a tech lead or architect "
                "also signs off)")
        out("  reject:  %s reject %s %s --by \"<their name>\" --reason \"<their reason>\"" % (ctx.cli, a.id, gate))
    out("Editing %s above '## Change log' invalidates this presentation." % L.GATE_ARTIFACT[gate])
    return 0


def _latest_presented(events: List[dict], gate: str, sha: str) -> Optional[dict]:
    match = None
    for ev in events:
        if ev["event"] == "GATE_PRESENTED" and ev.get("gate") == gate and ev.get("sha256") == sha:
            match = ev
    return match


def _answer_text(ev: dict) -> str:
    """The chosen option(s) of an AskUserQuestion answer (not the question text, which says 'Approve' too)."""
    raw = str(ev.get("excerpt") or "")
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if isinstance(data, dict):
        data = list(data.values())
    return " ".join(json.dumps(v) if not isinstance(v, str) else v for v in (data if isinstance(data, list) else [data]))


def _human_turn(ctx: Ctx, gate: str, since_ts: Any, evidence: str) -> Tuple[dict, str]:
    """The unused human event that approves this gate: newer than since_ts and not consumed by any
    decision in any change. An AskUserQuestion answer wins over a free prompt and must pick Approve."""
    used = L.used_human_events(ctx.root, ctx.cfg)
    after = [e for e in L.trusted_human_events(ctx.root)
             if L.parse_ts(e.get("ts")) and since_ts and L.parse_ts(e.get("ts")) > since_ts
             and L.human_event_id(e) not in used and (e.get("type") != "plan_accept" or gate == "plan")]
    if not after:
        raise GateError("not approved: no human turn recorded after the gate was presented or last decided (%s) "
                        "that no other decision has used. --evidence does not replace a recorded human turn while "
                        "hooks are installed. Ask the approver in an interactive Claude Code session and wait for "
                        "their answer (each turn approves one gate once); in a headless run, stop and hand off"
                        % (since_ts.strftime("%Y-%m-%dT%H:%M:%SZ") if since_ts else "?"))
    asks = [e for e in after if e.get("type") == "ask_answer"]
    if asks:
        ok = [e for e in asks if re.search(r"\bapprove", _answer_text(e), re.I)]
        if not ok:
            raise GateError("not approved: the approver's answer was %r, not Approve. Record it with reject, or "
                            "revise and present again" % str(asks[-1].get("excerpt") or "")[:120])
        return ok[-1], "ask_answer"
    last = after[-1]
    kind = str(last.get("type") or "prompt")
    if kind == "prompt" and not APPROVAL_WORDS.search(str(last.get("excerpt") or "")):
        # --evidence does not lift this: it would let any unrelated turn after `present` carry an approval
        raise GateError("not approved: the approver's last turn (%r) does not read as an approval. Ask again and "
                        "wait for their answer" % str(last.get("excerpt") or "")[:120])
    return last, kind


def cmd_approve(ctx: Ctx, a) -> int:
    gate = _norm_gate(a.gate)
    cdir = ctx.cdir(a.id)
    if L.headless() and not a.allow_ci:
        raise GateError("refusing to approve in a headless run (%s): the gate is a PR review of the artifact "
                        "PR by a code owner; the approver mirrors it into audit.md from an interactive Claude Code session"
                        % ("CI=%s" % os.environ.get("CI") if _ci_truthy() else "SDLC_NONINTERACTIVE is set"))
    by = (a.by or "").strip()
    if not by:
        raise L.SdlcError("--by NAME is required (the human approver's name)")
    if reserved_approver(by):
        raise GateError("%r cannot approve: approvals are recorded for a named human only" % by)
    findings = validate_artifact(ctx, a.id, gate, strict=True)
    if any(f.level == "ERROR" for f in findings):
        _print_findings(findings)
        raise GateError("not approved: %s fails strict validation" % L.GATE_ARTIFACT[gate])
    s = L.change_status(ctx.root, ctx.cfg, a.id)
    tier = s["tier"]
    ups = _upstream_problems(s, gate)
    if ups:
        raise GateError("not approved: earlier gates must be approved and current first (%s)" % "; ".join(ups))
    sha = s["gates"][gate]["sha"]
    events = L.audit_read(cdir)
    pres = _latest_presented(events, gate, sha)
    if not pres:
        any_pres = any(ev["event"] == "GATE_PRESENTED" and ev.get("gate") == gate for ev in events)
        raise GateError("not approved: %s %s; run `%s present %s %s` and show the card to the approver" % (
            L.GATE_ARTIFACT[gate], "changed since it was presented" if any_pres else "was never presented",
            ctx.cli, a.id, gate))
    role = (a.role or "").strip() or {"intent": "product-owner", "spec": "product-owner", "plan": "engineer"}[gate]
    allowed = L.allowed_roles(gate, tier)
    if allowed and role not in allowed:
        raise GateError("high-risk %s needs %s: pass --role %s with that person's name"
                        % (gate, L.approver_role(gate, tier), "|".join(allowed)))
    if role in s["gates"][gate]["approved_roles"]:
        raise GateError("%s already approved by a %s on this fingerprint; %s" % (
            L.GATE_ARTIFACT[gate], role, ("still needed: " + " and ".join(s["gates"][gate]["missing_roles"]))
            if s["gates"][gate]["missing_roles"] else "nothing to do"))
    evidence = (a.evidence or "").strip()
    hpath = L.human_events_path(ctx.root)
    # Each approval needs its own human turn: newer than the presentation and than any earlier
    # decision on this gate (e.g. the second high-risk approval), and not used by any other decision.
    since_ev = pres
    for ev in events[events.index(pres) + 1:]:  # audit.md is append-only, so list order is time order
        if ev["event"] in ("GATE_APPROVED", "GATE_REJECTED") and ev.get("gate") == gate:
            since_ev = ev
    fields: Dict[str, Any] = {}
    if hpath.is_file():
        turn, presence = _human_turn(ctx, gate, L.parse_ts(since_ev["ts"]), evidence)
        if not evidence:
            evidence = str(turn.get("excerpt") or "")
        fields["human_event"] = L.human_event_id(turn)
        if turn.get("sha256"):
            fields["human_sha256"] = turn["sha256"]
    elif L.hooks_configured(ctx.root):
        raise GateError("not approved: the guard hooks are installed (.claude/settings*.json runs hooks.py) but "
                        "%s is missing, so no human turn can be checked. Ask the approver to send any prompt in "
                        "this interactive session (the prompt hook recreates the file and records it), then "
                        "approve again; --evidence does not replace it" % ctx.rel(hpath))
    else:
        if not evidence:
            raise GateError("not approved: hooks are not installed (no .claude/sdlc-state/human-events.jsonl), so "
                            "--evidence \"<the approver's words>\" is mandatory")
        presence = "unverified"
    L.audit_append(cdir, "GATE_APPROVED", gate=gate, sha256=sha, by=by, role=role, evidence=evidence,
                   presence=presence, presented_at=pres["ts"], **fields)
    ts = L.audit_read(cdir)[-1]["ts"]
    s2 = L.change_status(ctx.root, ctx.cfg, a.id)
    st2 = s2["gates"][gate]
    if st2["current"]:
        L.update_frontmatter(cdir / L.GATE_ARTIFACT[gate], status="approved", approved_by=by, approved_at=ts)
    out("APPROVED %s for %s by %s (%s), presence=%s, sha256=%s" % (gate, a.id, by, role, presence, sha[:12]))
    if st2["current"]:
        out("gate %s is now current; unlocks: %s" % (gate, UNLOCKS[gate]))
    else:
        out("gate %s is not current yet: still needs a %s approval" % (gate, " and a ".join(st2["missing_roles"])))
    out("next: %s" % s2["next"])
    out("Commit %s with the trailer 'Change: %s'." % (ctx.rel(cdir / L.GATE_ARTIFACT[gate]), a.id))
    return 0


def cmd_reject(ctx: Ctx, a) -> int:
    gate = _norm_gate(a.gate)
    cdir = ctx.cdir(a.id)
    if not a.by.strip() or not a.reason.strip():
        raise L.SdlcError("--by and --reason are required")
    path = cdir / L.GATE_ARTIFACT[gate]
    if not path.is_file():
        raise GateError("%s does not exist" % L.GATE_ARTIFACT[gate])
    sha = L.file_fingerprint(path)
    events = L.audit_read(cdir)
    pres = next((ev for ev in reversed(events) if ev["event"] == "GATE_PRESENTED" and ev.get("gate") == gate), None)
    turn = None
    if pres:  # the turn that asked for changes cannot approve another gate later
        used = L.used_human_events(ctx.root, ctx.cfg)
        pts = L.parse_ts(pres["ts"])
        turn = next((e for e in reversed(L.trusted_human_events(ctx.root)) if L.human_event_id(e) not in used
                     and pts and L.parse_ts(e.get("ts")) and L.parse_ts(e.get("ts")) > pts), None)
    L.audit_append(cdir, "GATE_REJECTED", gate=gate, sha256=sha, by=a.by.strip(), reason=a.reason.strip(),
                   human_event=L.human_event_id(turn) if turn else None)
    L.update_frontmatter(path, status="rejected")
    out("REJECTED %s for %s by %s: %s" % (gate, a.id, a.by.strip(), a.reason.strip()))
    if gate == "intent":
        L.audit_append(cdir, "CHANGE_CLOSED", status="dropped", reason="intent rejected")
        if L.get_active(ctx.root) == a.id:
            L.set_active(ctx.root, None)
        out("Intent rejected: change %s is dropped (it counts against the survival rate)." % a.id)
    else:
        out("next: revise %s with the reviewer's feedback, then `%s present %s %s`" % (
            L.GATE_ARTIFACT[gate], ctx.cli, a.id, gate))
    return 0


def cmd_plan_from_accepted(ctx: Ctx, a) -> int:
    cdir = ctx.cdir(a.id)
    src = L.state_dir(ctx.root) / "accepted-plan.md"
    if not src.is_file():
        raise GateError("no accepted plan at %s: plan in plan mode and accept it (the PostToolUse ExitPlanMode "
                        "hook writes it), or write plan.md by hand and use present/approve" % ctx.rel(src))
    events = L.audit_read(cdir)
    created = next((ev["ts"] for ev in events if ev["event"] == "CHANGE_CREATED"), None)
    mtime = _dt.datetime.fromtimestamp(int(src.stat().st_mtime), _dt.timezone.utc)
    accepted_ts = mtime.strftime("%Y-%m-%dT%H:%M:%SZ")
    cts = L.parse_ts(created) if created else None
    if cts and mtime < cts:
        raise GateError("the accepted plan (%s) is older than change %s (created %s); accept a new plan in plan mode"
                        % (accepted_ts, a.id, created))
    text = src.read_text(encoding="utf-8").replace("\r\n", "\n").strip("\n")
    try:
        _, text = L.parse_frontmatter(text)
    except ValueError:
        pass
    s = L.change_status(ctx.root, ctx.cfg, a.id)
    by = (a.by or "").strip()
    if s["tier"] != "high-risk" and (not by or reserved_approver(by)):
        raise GateError("pass --by \"<name of the engineer who accepted the plan>\": the name they give you, "
                        "never git config or your own (e.g. `%s plan-from-accepted %s --by \"<name>\"`)"
                        % (ctx.cli, a.id))
    if not re.match(r"^#\s", text):
        text = "# Plan: %s\n\n%s" % (s["title"], text)
    secs = L.sections(text)
    missing = [n for n, _ in PLAN_SECTIONS if n.lower() not in secs and n != "Change log"]
    lines = text.split("\n")
    cl_idx = next((i for i, ln in enumerate(lines) if L._CHANGELOG_RE.match(ln)), None)
    head, tail = (lines, []) if cl_idx is None else (lines[:cl_idx], lines[cl_idx:])
    add = []
    for n in missing:
        add.extend(["", "## %s" % n, "", PLACEHOLDER])
    if not tail:
        tail = ["## Change log", ""]
    body = "\n".join(head).rstrip("\n") + "\n"
    if add:
        body += "\n".join(add) + "\n"
    body += "\n" + "\n".join(tail).rstrip("\n") + "\n"
    meta = {"id": a.id, "title": s["title"], "status": "draft",
            "spec_sha": s["gates"]["spec"]["sha"] or "", "intent_sha": s["gates"]["intent"]["sha"] or ""}
    plan_path = cdir / "plan.md"
    replaced = plan_path.is_file()
    L.write_text_atomic(plan_path, L.dump_frontmatter(meta, body))
    out("%s %s from the plan accepted at %s" % ("replaced" if replaced else "wrote", ctx.rel(plan_path), accepted_ts))
    for n in missing:
        out("WARN  plan.md [%s]: not covered in plan mode; placeholder added. Fix: fill it in (edits above "
            "'## Change log' need a new present/approve)" % n)
    findings = validate_artifact(ctx, a.id, "plan", strict=True)
    errors = [f for f in findings if f.level == "ERROR"]
    if errors:
        _print_findings(errors)
        raise GateError("plan.md written but not approved: fix the errors, then `%s present %s plan`" % (ctx.cli, a.id))
    sha = L.file_fingerprint(plan_path)
    ups = _upstream_problems(s, "plan")
    if ups:
        raise GateError("plan.md written but not presented: earlier gates first (%s), then `%s present %s plan`"
                        % ("; ".join(ups), ctx.cli, a.id))
    L.audit_append(cdir, "GATE_PRESENTED", gate="plan", sha256=sha, via="plan-mode")
    if s["tier"] == "high-risk":
        out("High-risk change: plan-mode acceptance is not enough; a tech-lead or architect approves:")
        out("  %s approve %s plan --by \"<name>\" --role tech-lead" % (ctx.cli, a.id))
        return 0
    L.audit_append(cdir, "GATE_APPROVED", gate="plan", sha256=sha, by=by, role="engineer",
                   evidence="accepted in plan mode at %s" % accepted_ts, presence="plan-mode")
    ts = L.audit_read(cdir)[-1]["ts"]
    L.update_frontmatter(plan_path, status="approved", approved_by=by, approved_at=ts)
    out("APPROVED plan for %s by %s (engineer), presence=plan-mode, sha256=%s" % (a.id, by, sha[:12]))
    out("next: %s" % L.change_status(ctx.root, ctx.cfg, a.id)["next"])
    return 0


# ---------------------------------------------------------------- tier / skip / fix / log / close

def cmd_raise_tier(ctx: Ctx, a) -> int:
    cdir = ctx.cdir(a.id)
    if a.tier not in L.TIERS:
        raise L.SdlcError("unknown tier %r" % a.tier)
    cur = L.change_status(ctx.root, ctx.cfg, a.id)["tier"]
    if L.TIERS.index(a.tier) < L.TIERS.index(cur):
        raise GateError("tiers only move up (%s → %s refused). If the change looks over-classified, ask the human; "
                        "a lower tier means a new change" % (cur, a.tier))
    if a.tier == cur:
        raise GateError("change %s is already %s" % (a.id, cur))
    L.audit_append(cdir, "TIER_RAISED", to=a.tier, reason=a.reason, **{"from": cur})
    L.update_frontmatter(cdir / "intent.md", tier=a.tier)
    s = L.change_status(ctx.root, ctx.cfg, a.id)
    out("tier %s → %s for %s. Required gates now: %s" % (cur, a.tier, a.id, ", ".join(s["required_gates"])))
    out("next: %s" % s["next"])
    return 0


def cmd_skip(ctx: Ctx, a) -> int:
    cdir = ctx.cdir(a.id)
    s = L.change_status(ctx.root, ctx.cfg, a.id)
    gate = next((g for g, st in L.GATE_STAGE.items() if st == a.stage), None)
    if gate and gate in s["required_gates"]:
        raise GateError("stage %s carries the required %s gate for tier %s; gates follow the tier and cannot be "
                        "skipped" % (a.stage, gate, s["tier"]))
    L.audit_append(cdir, "STAGE_SKIPPED", stage=a.stage, reason=a.reason)
    out("recorded: stage %s skipped for %s — %s" % (a.stage, a.id, a.reason))
    return 0


def _tests_digest(ctx: Ctx) -> str:
    """sha256 over (path, content sha256) of every tracked or untracked test file (test_globs).
    Lets drift check fix mode when the recorded commit is gone (rebase, force-push, shallow clone)."""
    cprefix = (ctx.cfg.get("changes_dir") or "changes").rstrip("/") + "/"
    h = hashlib.sha256()
    for rel in sorted(set(_git_ls(ctx.root))):
        if rel.startswith(cprefix) or not L.match_any(rel, ctx.cfg.get("test_globs", [])):
            continue
        try:
            data = (ctx.root / rel).read_bytes()
        except OSError:
            continue
        h.update(("%s\0%s\n" % (rel, hashlib.sha256(data).hexdigest())).encode("utf-8"))
    return h.hexdigest()


def cmd_fix_mode(ctx: Ctx, a) -> int:
    cdir = ctx.cdir(a.id)
    s = L.change_status(ctx.root, ctx.cfg, a.id)
    by = a.by or _git_user(ctx.root) or None
    if a.state == "off":
        if not s["fix_mode"]:
            out("fix mode is already off for %s" % a.id)
            return 0
        L.audit_append(cdir, "FIX_MODE_OFF", by=by)
        out("fix mode OFF for %s (recorded). Tests are editable again." % a.id)
        return 0
    if s["fix_mode"]:
        out("fix mode is already on for %s (since commit %s)" % (a.id, s["fix_commit"]))
        return 0
    head = L.git(["-C", str(ctx.root), "rev-parse", "--verify", "-q", "HEAD"], check=False)
    if head.returncode != 0:
        raise GateError("no commits yet: commit the failing test first, then turn fix mode on")
    st = L.git(["-C", str(ctx.root), "status", "--porcelain=v1", "-z", "-uall", "--no-renames"])
    dirty = [e[3:] for e in st.stdout.split("\0") if len(e) > 3 and e[2] == " "]
    tests = [p for p in dirty if L.match_any(p, ctx.cfg.get("test_globs", []))]
    if tests:
        raise GateError("uncommitted test files: %s. Commit the failing test first (it must fail on HEAD), "
                        "then turn fix mode on" % ", ".join(tests[:5]))
    sha = head.stdout.strip()
    L.audit_append(cdir, "FIX_MODE_ON", commit=sha, by=by, tests_sha256=_tests_digest(ctx))
    out("fix mode ON for %s at commit %s. Test files (test_globs) are now read-only for the agent." % (a.id, sha[:12]))
    out("Make the committed failing test pass by changing the code. To end: `%s fix-mode %s off`" % (ctx.cli, a.id))
    return 0


def cmd_log(ctx: Ctx, a) -> int:
    cdir = ctx.cdir(a.id)
    ev = a.event.upper()
    if ev in ("GATE_PRESENTED", "GATE_APPROVED", "GATE_REJECTED"):
        raise GateError("gate events are recorded only by present/approve/reject")
    if ev not in LOG_EVENTS:
        raise GateError("log accepts %s; use the dedicated command for %s" % ("|".join(LOG_EVENTS), ev))
    fields: Dict[str, Any] = {}
    for kv in a.fields:
        if "=" not in kv:
            raise L.SdlcError("expected key=value, got %r" % kv)
        k, v = kv.split("=", 1)
        if not re.match(r"^[a-z_][a-z0-9_]*$", k):
            raise L.SdlcError("invalid field name %r (use lowercase letters, digits, _)" % k)
        if k in ("ts", "event"):
            raise L.SdlcError("field name %r is reserved" % k)
        fields[k] = v
    if a.message:
        fields["message"] = a.message
    if ev == "PR_OPENED" and not (fields.get("url") or fields.get("pr")):
        raise L.SdlcError("PR_OPENED needs url=<pr-url>")
    L.audit_append(cdir, ev, **fields)
    out("logged %s for %s" % (ev, a.id))
    return 0


def cmd_close(ctx: Ctx, a) -> int:
    cdir = ctx.cdir(a.id)
    s = L.change_status(ctx.root, ctx.cfg, a.id)
    if s["stage"] in ("closed", "dropped"):
        raise GateError("change %s is already %s" % (a.id, s["stage"]))
    if a.status == "done":
        stale = [g for g in s["required_gates"] if not s["gates"][g]["current"]]
        if stale:
            out("WARN  closing as done with gates not approved/current: %s" % ", ".join(stale))
    L.audit_append(cdir, "CHANGE_CLOSED", status=a.status, pr=a.pr)
    if (cdir / "intent.md").is_file():
        L.update_frontmatter(cdir / "intent.md", status=a.status)
    if L.get_active(ctx.root) == a.id:
        L.set_active(ctx.root, None)
    out("closed %s (%s)%s" % (a.id, a.status, (" — PR %s" % a.pr) if a.pr else ""))
    return 0


def cmd_activate(ctx: Ctx, a) -> int:
    ctx.cdir(a.id)
    L.set_active(ctx.root, a.id)
    s = L.change_status(ctx.root, ctx.cfg, a.id)
    out("active change: %s (tier %s, stage %s)" % (a.id, s["tier"], L.STAGE_LABEL.get(s["stage"], s["stage"])))
    out("next: %s" % s["next"])
    return 0


def cmd_deactivate(ctx: Ctx, a) -> int:
    prev = L.get_active(ctx.root)
    L.set_active(ctx.root, None)
    out("no active change%s" % ((" (was %s)" % prev) if prev else ""))
    return 0


# ---------------------------------------------------------------- drift / trace

def _resolve_base(root: Path, base: str) -> str:
    for cand in (base, "origin/" + base):
        if L.git(["-C", str(root), "rev-parse", "--verify", "-q", cand + "^{commit}"], check=False).returncode == 0:
            return cand
    raise L.SdlcError("base ref %r not found (pass --base REF)" % base)


def cmd_review_local(ctx: Ctx, a) -> int:
    """Isolated review: one `claude -p` per pass on a clean snapshot of HEAD (no history, no session context)."""
    import concurrent.futures
    import subprocess
    import tempfile
    rcfg = ctx.cfg.get("review") or {}
    if rcfg.get("mode") != "isolated" and not a.force:
        out('review.mode is %r in .claude/sdlc.json; set it to "isolated" (or pass --force)' % rcfg.get("mode"))
        return 1
    if shutil.which("claude") is None:
        raise L.SdlcError("`claude` CLI not found on PATH")
    cdir = ctx.cdir(a.id)
    base = _resolve_base(ctx.root, a.base or ctx.cfg.get("default_branch") or "main")
    head = L.git(["-C", str(ctx.root), "rev-parse", "HEAD"]).stdout.strip()
    dirty = L.git(["-C", str(ctx.root), "status", "--porcelain", "-uno"]).stdout.strip()
    if dirty:
        out("WARNING: uncommitted tracked changes are not in the review (it reviews HEAD %s); commit first" % head[:10])
    work = Path(tempfile.mkdtemp(prefix="sdlc-review-"))
    snap = work / "snapshot"
    snap.mkdir()
    tar = subprocess.run(["git", "-C", str(ctx.root), "archive", head], stdout=subprocess.PIPE, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(snap)], input=tar, check=True)
    if not rcfg.get("project_docs"):
        for pat in ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md"):
            for f in snap.rglob(pat):
                f.unlink()
    diff = work / "review.diff"
    diff.write_text(L.git(["-C", str(ctx.root), "diff", "%s...%s" % (base, head)]).stdout, encoding="utf-8")
    review_md = work / "REVIEW.md"  # from the working tree: it may be git-excluded and so missing from the archive
    src_review = ctx.root / "REVIEW.md"
    review_md.write_text(src_review.read_text(encoding="utf-8") if src_review.is_file()
                         else "No REVIEW.md in this repo: use the defaults in your instructions.\n", encoding="utf-8")
    for k in ("spec", "plan"):
        src = cdir / (k + ".md")
        (work / (k + ".md")).write_text(
            src.read_text(encoding="utf-8") if src.is_file()
            else "No %s.md exists for this change (bounded changes have no spec): skip checks that need it.\n" % k,
            encoding="utf-8")
    size_kb = diff.stat().st_size // 1024
    if size_kb > 150:
        out("WARNING: the diff is %d KB: reviewers cannot read it whole. Pass --base <commit before this change's "
            "first commit> (the branch may carry other unmerged work)" % size_kb)
    agent = (L.SKILL_DIR / "assets" / "agents" / "sdlc-reviewer.md").read_text(encoding="utf-8")
    system = agent.split("---", 2)[2].strip()
    passes = rcfg.get("passes") or ["Bugs", "Security", "Compliance"]
    timeout = int(rcfg.get("timeout_s") or 900)

    def run_pass(name: str) -> Tuple[str, str]:
        prompt = ("pass: %s\ndiff: %s\nreview: %s\nspec: %s\nplan: %s\nThe repository snapshot of HEAD is your "
                  "working directory. Return the JSON findings object only."
                  % (name, diff, review_md, work / "spec.md", work / "plan.md"))
        cmd = ["claude", "-p", prompt, "--no-session-persistence", "--tools", "Read,Grep,Glob",
               "--permission-mode", "dontAsk", "--disable-slash-commands", "--add-dir", str(work),
               "--system-prompt", system]
        if rcfg.get("model"):
            cmd += ["--model", rcfg["model"]]
        env = dict(os.environ, SDLC_NONINTERACTIVE="1")
        try:
            p = subprocess.run(cmd, cwd=str(snap), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               universal_newlines=True, timeout=timeout)
            return name, p.stdout.strip() or ("(empty; stderr: %s)" % p.stderr.strip()[:500])
        except subprocess.TimeoutExpired:
            return name, "(timed out after %ss)" % timeout

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(passes)) as ex:
        results = list(ex.map(run_pass, passes))
    report = cdir / "review.md"
    lines = ["# Isolated review: %s" % a.id, "", "Reviewed HEAD %s against %s. Clean snapshot, no session history, "
             "project docs %s. Findings are unvalidated: confirm each against the code before acting." %
             (head, base, "included" if rcfg.get("project_docs") else "stripped"), ""]
    for name, text in results:
        lines += ["## %s pass" % name, "", "```json", text, "```", ""]
    report.write_text("\n".join(lines), encoding="utf-8")
    L.audit_append(cdir, "NOTE", message="isolated review done at %s, report %s/review.md" % (head[:10], a.id))
    shutil.rmtree(work, ignore_errors=True)
    out("review written to %s" % report)
    out("next: read it, confirm each finding against the code, fix the real ones, rerun verify")
    return 0


def cmd_drift(ctx: Ctx, a) -> int:
    cdir = ctx.cdir(a.id)
    plan = cdir / "plan.md"
    if not plan.is_file():
        out("no plan.md for %s: nothing to compare" % a.id)
        return 1 if a.strict else 0
    head_ok = L.git(["-C", str(ctx.root), "rev-parse", "--verify", "-q", "HEAD"], check=False).returncode == 0
    base = _resolve_base(ctx.root, a.base or ctx.cfg.get("default_branch") or "main") if head_ok else ""
    changed = L.changed_files(ctx.root, base)
    cprefix = (ctx.cfg.get("changes_dir") or "changes").rstrip("/") + "/"
    ignore = lambda p: p.startswith(cprefix) or p.startswith(L.STATE_DIRNAME + "/")  # noqa: E731
    changed = [p for p in changed if not ignore(p)]
    entries = L.plan_files(plan.read_text(encoding="utf-8"))
    unplanned = [p for p in changed if not L.planned(p, entries)]
    untouched = [e for e in entries if not any(ch in e for ch in "*?[") and not e.endswith("/")
                 and e not in changed]
    test_edits: List[str] = []
    errors: List[str] = []
    notes: List[str] = []
    s = L.change_status(ctx.root, ctx.cfg, a.id)
    if s["fix_mode"] and s["fix_commit"]:
        fc = s["fix_commit"]
        ok = L.git(["-C", str(ctx.root), "rev-parse", "--verify", "-q", fc + "^{commit}"], check=False).returncode == 0
        diff = L.git(["-C", str(ctx.root), "diff", "--name-only", "-z", "--no-renames", fc], check=False) if ok else None
        if diff is not None and diff.returncode == 0:
            cand = [x for x in diff.stdout.split("\0") if x]
            st = L.git(["-C", str(ctx.root), "status", "--porcelain=v1", "-z", "-uall"], check=False)
            cand += [e[3:] for e in st.stdout.split("\0") if e.startswith("?? ")]
            # Only files this change's own diff touches: a test edited on the base branch and merged in
            # after fix mode started shows up in `diff <fix commit>` but is not this change's edit.
            own = set(changed)
            test_edits = sorted({p for p in cand if p in own and L.match_any(p, ctx.cfg.get("test_globs", []))})
        else:
            on = [ev for ev in L.audit_read(cdir) if ev["event"] == "FIX_MODE_ON"]
            digest = on[-1].get("tests_sha256") if on else None
            if digest and digest == _tests_digest(ctx):
                notes.append("fix-mode base commit %s not found; test files verified unchanged by content digest"
                             % fc[:12])
            elif digest:
                errors.append("fix-mode base commit %s not found and the test files differ from the fix-mode start "
                              "(content digest); a test file was edited, added or removed" % fc[:12])
            else:
                errors.append("fix-mode base commit %s not found (history rewritten or shallow clone); cannot verify "
                              "test files are unchanged" % fc[:12])
    if a.json:
        out(json.dumps({"id": a.id, "base": base, "changed": changed, "planned": entries, "unplanned": unplanned,
                        "untouched": untouched, "fix_mode_test_edits": test_edits, "errors": errors}, indent=2))
    else:
        out("drift %s vs %s: %d changed, %d planned" % (a.id, base or "(no commits)", len(changed), len(entries)))
        for p in unplanned:
            out("UNPLANNED  %s  (add to plan.md '## Change log' with the reason, or revert)" % p)
        for p in untouched:
            out("UNTOUCHED  %s  (planned but not changed yet)" % p)
        for p in test_edits:
            out("TEST-EDIT  %s  (modified after fix mode started at %s)" % (p, (s["fix_commit"] or "")[:12]))
        for e in errors:
            out("ERROR  %s" % e)
        for n in notes:
            out("NOTE  %s" % n)
        if not (unplanned or untouched or test_edits or errors):
            out("OK: the diff matches plan.md")
    bad = bool(unplanned or test_edits or errors)
    return 1 if (a.strict and bad) else 0


def _git_ls(root: Path) -> List[str]:
    proc = L.git(["-C", str(root), "ls-files", "-z", "-co", "--exclude-standard"], check=False)
    return [x for x in proc.stdout.split("\0") if x]


def cmd_trace(ctx: Ctx, a) -> int:
    cdir = ctx.cdir(a.id)
    spec, plan = cdir / "spec.md", cdir / "plan.md"
    if not spec.is_file():
        out("no spec.md for %s (tier %s): nothing to trace" % (a.id, L.change_status(ctx.root, ctx.cfg, a.id)["tier"]))
        return 0
    sids = sorted(set(ID_ANY.findall(L.strip_comments(spec.read_text(encoding="utf-8")))))
    pids = set(ID_ANY.findall(L.strip_comments(plan.read_text(encoding="utf-8")))) if plan.is_file() else set()
    tests: Dict[str, List[str]] = {}
    if a.tests:
        globs = ctx.cfg.get("test_globs", [])
        cprefix = (ctx.cfg.get("changes_dir") or "changes").rstrip("/") + "/"
        for rel in _git_ls(ctx.root):
            if rel.startswith(cprefix) or not L.match_any(rel, globs):
                continue
            try:
                txt = (ctx.root / rel).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for rid in set(ID_ANY.findall(txt)):
                tests.setdefault(rid, []).append(rel)
    rows = []
    for rid in sids:
        rows.append({"id": rid, "status": "OK" if rid in pids else "GAP", "tests": sorted(tests.get(rid, []))})
    for rid in sorted(pids - set(sids)):
        rows.append({"id": rid, "status": "ORPHAN", "tests": sorted(tests.get(rid, []))})
    gaps = [r for r in rows if r["status"] == "GAP"]
    if a.json:
        out(json.dumps({"id": a.id, "rows": rows, "gaps": len(gaps)}, indent=2))
    else:
        if not plan.is_file():
            out("no plan.md yet: every spec ID is a GAP")
        for r in rows:
            t = ""
            if a.tests:
                t = ("  tests: " + ", ".join(r["tests"][:3])) if r["tests"] else "  tests: none"
            out("%-7s %s%s" % (r["status"], r["id"], t))
        out("%d id(s): %d OK, %d GAP, %d ORPHAN" % (len(rows), sum(r["status"] == "OK" for r in rows), len(gaps),
                                                    sum(r["status"] == "ORPHAN" for r in rows)))
        if gaps:
            out("Fix: reference each GAP id in plan.md (Order of work or Proof), or move it to spec Out of scope.")
    return 1 if gaps else 0


# ---------------------------------------------------------------- metrics

def _git_history(root: Path, cdirname: str) -> Dict[str, List[Tuple[str, str, str]]]:
    """path -> [(author_date_iso, status, commit)] newest first, for files under changes_dir."""
    proc = L.git(["-C", str(root), "log", "--no-renames", "--format=%x00%aI %H", "--name-status", "--", cdirname],
                 check=False)
    hist: Dict[str, List[Tuple[str, str, str]]] = {}
    if proc.returncode != 0:
        return hist
    for chunk in proc.stdout.split("\0"):
        lines = [ln for ln in chunk.split("\n") if ln.strip()]
        if not lines:
            continue
        date, _, commit = lines[0].strip().partition(" ")
        for ln in lines[1:]:
            parts = ln.split("\t")
            if len(parts) >= 2:
                hist.setdefault(parts[-1], []).append((date, parts[0][:1], commit))
    return hist


def _first_add_entry(entries: List[Tuple[str, str, str]]) -> Optional[Tuple[str, str, str]]:
    adds = [e for e in entries if e[1] == "A"]
    return adds[-1] if adds else (entries[-1] if entries else None)


def _secs(a: Optional[str], b: Optional[str]) -> Optional[float]:
    ta, tb = L.parse_ts(a), L.parse_ts(b)
    if ta is None or tb is None:
        return None
    return (tb - ta).total_seconds()


def _fmt_dur(s: Optional[float]) -> str:
    if s is None:
        return "—"
    h = s / 3600.0
    return "%.1fd" % (h / 24) if abs(h) >= 48 else "%.1fh" % h


def _p90(vals: List[float]) -> float:
    v = sorted(vals)
    k = max(0, int(-(-0.9 * len(v) // 1)) - 1)
    return v[k]


def cmd_metrics(ctx: Ctx, a) -> int:
    cname = ctx.cfg.get("changes_dir") or "changes"
    hist = _git_history(ctx.root, cname)
    since = L.parse_ts(a.since) if a.since else None
    if a.since and since is None:
        raise L.SdlcError("--since must be an ISO date (YYYY-MM-DD)")
    rows = []
    for cid in L.list_changes(ctx.root, ctx.cfg):
        cdir = L.changes_dir(ctx.root, ctx.cfg) / cid
        events = L.audit_read(cdir)
        s = L.change_status(ctx.root, ctx.cfg, cid)
        p = lambda n: "%s/%s/%s" % (cname, cid, n)  # noqa: E731
        firsts = [_first_add_entry(hist.get(p(n), [])) for n in ("intent.md", "spec.md", "plan.md")]
        ia, sa, pa = (f[0] if f else None for f in firsts)
        commits = [f[2] for f in firsts if f]
        collapsed = len(commits) != len(set(commits))  # squash merge: artifacts arrived in one commit
        created = s["created"]
        if since and (L.parse_ts(created or ia) or since) < since:
            continue

        def first(ev: str, **kw: str) -> Optional[str]:
            return next((e["ts"] for e in events if e["event"] == ev and all(e.get(k) == v for k, v in kw.items())), None)
        pr = first("PR_OPENED")
        closed = first("CHANGE_CLOSED")
        spec_rework = sum(1 for d, _, _ in hist.get(p("spec.md"), []) if pa and L.parse_ts(d) > L.parse_ts(pa))
        intent_rework = sum(1 for d, _, _ in hist.get(p("intent.md"), []) if sa and L.parse_ts(d) > L.parse_ts(sa))
        # Survival: accepted into Design vs closed. Dismissals (close --status dropped) count as closed;
        # tiers without an intent gate are reported separately; open drafts stay undecided.
        intent_ev = [e["event"] for e in events if e.get("gate") == "intent"]
        dropped = any(e["event"] == "CHANGE_CLOSED" and e.get("status") == "dropped" for e in events)
        if "intent" not in s["required_gates"]:
            decision = "n/a"
        elif "GATE_APPROVED" in intent_ev:
            decision = "approved"
        elif "GATE_REJECTED" in intent_ev or dropped:
            decision = "rejected"
        else:
            decision = None
        rows.append({
            "id": cid, "tier": s["tier"], "stage": s["stage"],
            "intent_committed": ia, "spec_committed": sa, "plan_committed": pa,
            "intent_approved": first("GATE_APPROVED", gate="intent"),
            "spec_approved": first("GATE_APPROVED", gate="spec"),
            "plan_approved": first("GATE_APPROVED", gate="plan"),
            "pr_opened": pr, "closed": closed,
            "capture_s": _secs(created, ia),
            "intent_to_spec_s": None if collapsed else _secs(ia, sa),
            "spec_to_plan_s": None if collapsed else _secs(sa or ia, pa),
            "plan_to_pr_s": _secs(pa, pr),
            "pr_to_close_s": _secs(pr, closed),
            "lead_time_s": _secs(ia or created, closed),
            "spec_commits_after_plan": None if collapsed else spec_rework,
            "intent_commits_after_spec": None if collapsed else intent_rework,
            "intent_decision": decision,
            "source": s["source"],
            "collapsed": collapsed,
        })
    keys = ["capture_s", "intent_to_spec_s", "spec_to_plan_s", "plan_to_pr_s", "pr_to_close_s", "lead_time_s"]
    summary: Dict[str, Any] = {}
    for k in keys:
        vals = [r[k] for r in rows if r[k] is not None]
        summary[k] = {"n": len(vals), "median": statistics.median(vals) if vals else None,
                      "p90": _p90(vals) if vals else None}
    appr = sum(1 for r in rows if r["intent_decision"] == "approved")
    rej = sum(1 for r in rows if r["intent_decision"] == "rejected")
    summary["survival_rate"] = (appr / float(appr + rej)) if (appr + rej) else None
    summary["intents_approved"], summary["intents_rejected"] = appr, rej
    summary["intents_without_gate"] = sum(1 for r in rows if r["intent_decision"] == "n/a")
    summary["collapsed"] = sum(1 for r in rows if r["collapsed"])
    summary["changes_with_spec_rework"] = sum(1 for r in rows if r["spec_commits_after_plan"])
    summary["changes"] = len(rows)
    if a.json:
        out(json.dumps({"changes": rows, "summary": summary}, indent=2))
        return 0
    out("| change | tier | stage | capture | intent→spec | spec→plan | plan→PR | PR→close | spec rework | intent rework | intent |")
    out("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        out("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["id"], r["tier"], r["stage"], _fmt_dur(r["capture_s"]), _fmt_dur(r["intent_to_spec_s"]),
            _fmt_dur(r["spec_to_plan_s"]), _fmt_dur(r["plan_to_pr_s"]), _fmt_dur(r["pr_to_close_s"]),
            "—" if r["spec_commits_after_plan"] is None else r["spec_commits_after_plan"],
            "—" if r["intent_commits_after_spec"] is None else r["intent_commits_after_spec"],
            r["intent_decision"] or "—"))
    out("")
    out("| metric | n | median | p90 |")
    out("|---|---|---|---|")
    for k in keys:
        sm = summary[k]
        out("| %s | %d | %s | %s |" % (k[:-2].replace("_", " "), sm["n"], _fmt_dur(sm["median"]), _fmt_dur(sm["p90"])))
    sr = summary["survival_rate"]
    out("")
    out("intent survival: %s (%d approved, %d rejected or dropped; %d without an intent gate excluded); "
        "changes with spec rework after plan: %d of %d" % (
            "—" if sr is None else "%.0f%%" % (sr * 100), appr, rej, summary["intents_without_gate"],
            summary["changes_with_spec_rework"], len(rows)))
    out("Durations use git author dates of the first commit adding each artifact; uncommitted artifacts show —.")
    if summary["collapsed"]:
        out("WARN  %d change(s) have intent/spec/plan in one commit (squash merge?): their stage durations and "
            "rework counts show —; see references/metrics.md (squash merges)." % summary["collapsed"])
    return 0


# ---------------------------------------------------------------- init

def _detect_verify(root: Path) -> List[str]:
    cmds: List[str] = []
    mk = root / "Makefile"
    if mk.is_file():
        txt = mk.read_text(encoding="utf-8", errors="ignore")
        for t in ("test", "lint", "build"):
            if re.search(r"^%s\s*:(?!=)" % t, txt, re.M):
                cmds.append("make %s" % t)
    pj = root / "package.json"
    if pj.is_file():
        try:
            scripts = json.loads(pj.read_text(encoding="utf-8")).get("scripts", {}) or {}
        except ValueError:
            scripts = {}
        runner = "pnpm" if (root / "pnpm-lock.yaml").exists() else "yarn" if (root / "yarn.lock").exists() else "npm"
        for t in ("test", "lint", "build"):
            if t in scripts:
                cmds.append("%s test" % runner if t == "test" else "%s run %s" % (runner, t))
    py = (root / "pytest.ini").is_file() or (root / "conftest.py").is_file()
    for f in ("pyproject.toml", "setup.cfg", "tox.ini"):
        p = root / f
        if p.is_file() and re.search(r"\[(tool[.:])?pytest", p.read_text(encoding="utf-8", errors="ignore")):
            py = True
    if py and not any(c.startswith("make test") for c in cmds):
        cmds.append("pytest")
    if (root / "go.mod").is_file():
        cmds.extend(["go test ./...", "go vet ./..."])
    if (root / "Cargo.toml").is_file():
        cmds.append("cargo test")
    return cmds


def _default_branch(root: Path) -> str:
    try:
        p = L.git(["-C", str(root), "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"], check=False)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip().split("/", 1)[-1]
        for b in ("main", "master"):
            if L.git(["-C", str(root), "rev-parse", "--verify", "-q", "refs/heads/" + b], check=False).returncode == 0:
                return b
        p = L.git(["-C", str(root), "symbolic-ref", "-q", "--short", "HEAD"], check=False)
        if p.returncode == 0 and p.stdout.strip() in ("main", "master"):
            return p.stdout.strip()
    except L.SdlcError:
        pass
    return "main"


def _skill_ref(root: Path) -> str:
    try:
        rel = L.SKILL_DIR.relative_to(root.resolve()).as_posix()
        return "$CLAUDE_PROJECT_DIR/" + rel
    except ValueError:
        return L.SKILL_DIR.as_posix()


def _subst(obj: Any, ref: str) -> Any:
    if isinstance(obj, dict):
        return {k: _subst(v, ref) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_subst(v, ref) for v in obj]
    if isinstance(obj, str):
        return obj.replace("__SKILL_DIR__", ref)
    return obj


ROUTING_HEADING = "## How work is done here"
ROUTING_BLOCK = (ROUTING_HEADING + "\n\n- For any feature idea, bug report, ticket, incident, deploy or release request, "
                 "or \"fix X\" request, invoke the `ai-native-sdlc` skill first and follow it (it right-sizes "
                 "trivial edits to no ceremony).\n")


def _hook_sub(cmd: Any) -> Optional[str]:
    """The hooks.py subcommand a handler runs ('pre-tool', ...), or None if it is not this skill's handler."""
    m = re.search(r"hooks\.py[\"']?\s+([a-z-]+)", cmd) if isinstance(cmd, str) else None
    return m.group(1) if m else None


def merge_hooks(existing: dict, template: dict) -> Tuple[dict, int]:
    """Merge template['hooks'] into existing settings; returns (merged, handlers added or updated).
    Other handlers are never touched. This skill's own handlers (matched by hooks.py subcommand) are kept
    exactly once per event, in a group with the template's matcher and with the template's command, so
    re-running init after the template changed updates the install instead of adding a second group."""
    merged = json.loads(json.dumps(existing))
    hooks = merged.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise L.SdlcError(".claude/settings.json 'hooks' is not an object; fix it by hand")
    changed = 0
    for event, groups in template.get("hooks", {}).items():
        cur = hooks.setdefault(event, [])
        if not isinstance(cur, list):
            raise L.SdlcError(".claude/settings.json hooks.%s is not a list; fix it by hand" % event)
        emptied: List[dict] = []
        for tg in groups:
            for th in tg.get("hooks", []):
                sub = _hook_sub(th.get("command"))
                mine = [(g, h) for g in cur if isinstance(g, dict) and isinstance(g.get("hooks"), list)
                        for h in g["hooks"] if isinstance(h, dict) and sub and _hook_sub(h.get("command")) == sub]
                keep = next(((g, h) for g, h in mine if g.get("matcher") == tg.get("matcher")), None)
                if keep is None and mine and all(_hook_sub(h.get("command")) for h in mine[0][0]["hooks"]):
                    keep = mine[0]  # a group holding only our handlers: move it to the new matcher
                    if tg.get("matcher") is None:
                        keep[0].pop("matcher", None)
                    else:
                        keep[0]["matcher"] = tg["matcher"]
                    changed += 1
                for g, h in mine:  # duplicates, or ours inside someone else's group under an old matcher
                    if keep is None or h is not keep[1]:
                        g["hooks"] = [x for x in g["hooks"] if x is not h]  # by identity: duplicates are equal
                        emptied.append(g)
                        changed += 1
                if keep is not None:
                    if keep[1] != th:
                        keep[1].clear()
                        keep[1].update(json.loads(json.dumps(th)))
                        changed += 1
                    continue
                if any(isinstance(x, dict) and x == th for g in cur if isinstance(g, dict) for x in g.get("hooks", [])):
                    continue
                match = next((g for g in cur if isinstance(g, dict) and g.get("matcher") == tg.get("matcher")), None)
                if match is None:
                    match = {k: v for k, v in json.loads(json.dumps(tg)).items() if k != "hooks"}
                    match["hooks"] = []
                    cur.append(match)
                match.setdefault("hooks", []).append(json.loads(json.dumps(th)))
                changed += 1
        cur[:] = [g for g in cur if not (any(g is e for e in emptied) and not g["hooks"])]
    return merged, changed


def cmd_init(ctx: Ctx, a) -> int:
    root = ctx.root
    actions: List[Tuple[str, str, str]] = []
    dry = a.dry_run

    def act(kind: str, rel: str, detail: str = "") -> None:
        actions.append((kind, rel, detail))

    def write(path: Path, text: str) -> None:
        if not dry:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    if not L.in_git_repo(root):
        act("WARN", str(root), "not a git repository; run `git init` first for gates and metrics to work")
    # 1. config
    cfgp = root / ".claude" / "sdlc.json"
    if cfgp.is_file():
        act("SKIP", ".claude/sdlc.json", "exists")
    else:
        tpl = TEMPLATES / "sdlc.json"
        try:
            base = json.loads(tpl.read_text(encoding="utf-8")) if tpl.is_file() else json.loads(json.dumps(L.DEFAULT_CONFIG))
        except ValueError:
            base = json.loads(json.dumps(L.DEFAULT_CONFIG))
        base["verify_commands"] = _detect_verify(root)
        base["default_branch"] = _default_branch(root)
        write(cfgp, json.dumps(base, indent=2) + "\n")
        act("CREATE", ".claude/sdlc.json", "verify_commands=%s, default_branch=%s" % (
            base["verify_commands"] or "[] (none detected: add yours)", base["default_branch"]))
    # 2. hooks
    setp = root / ".claude" / "settings.json"
    tplp = ASSETS / "settings" / "project-settings.json"
    if L.plugin_root():
        act("SKIP", ".claude/settings.json", "the guard hooks ship with the plugin (%s)" % L.plugin_root())
    elif not tplp.is_file():
        act("WARN", ".claude/settings.json", "hook template missing (%s)" % tplp)
    else:
        template = _subst(json.loads(tplp.read_text(encoding="utf-8")), _skill_ref(root))
        existing: dict = {}
        if setp.is_file():
            try:
                existing = json.loads(setp.read_text(encoding="utf-8") or "{}")
            except ValueError as exc:
                raise L.SdlcError(".claude/settings.json is not valid JSON (%s); fix it, then rerun init" % exc)
            if not isinstance(existing, dict):
                raise L.SdlcError(".claude/settings.json must be a JSON object")
        merged, added = merge_hooks(existing, template)
        if added:
            existed = setp.is_file()
            write(setp, json.dumps(merged, indent=2) + "\n")
            act("MERGE" if existed else "CREATE", ".claude/settings.json",
                "%d hook handler(s) added or updated (UserPromptSubmit, PreToolUse, PostToolUse, Stop)" % added)
        else:
            act("SKIP", ".claude/settings.json", "hooks already installed")
    # 3. agents
    adir = ASSETS / "agents"
    for src in sorted(adir.glob("*.md")) if adir.is_dir() else []:
        dst = root / ".claude" / "agents" / src.name
        rel = ".claude/agents/" + src.name
        if dst.is_file() and not a.force:
            same = dst.read_text(encoding="utf-8") == src.read_text(encoding="utf-8")
            act("SKIP", rel, "up to date" if same else "exists (use --force to overwrite)")
        else:
            act("UPDATE" if dst.is_file() else "CREATE", rel, "subagent")
            write(dst, src.read_text(encoding="utf-8"))
    # 4. REVIEW.md
    rv = TEMPLATES / "REVIEW.md"
    if rv.is_file():
        if (root / "REVIEW.md").exists():
            act("SKIP", "REVIEW.md", "exists")
        else:
            write(root / "REVIEW.md", rv.read_text(encoding="utf-8"))
            act("CREATE", "REVIEW.md", "review policy; tailor it with the tech lead")
    # 5. .gitignore
    gi = root / ".gitignore"
    gtxt = gi.read_text(encoding="utf-8") if gi.is_file() else ""
    if any(ln.strip().strip("/") == L.STATE_DIRNAME for ln in gtxt.splitlines()):
        act("SKIP", ".gitignore", "already ignores %s/" % L.STATE_DIRNAME)
    else:
        sep = "" if (not gtxt or gtxt.endswith("\n")) else "\n"
        existed = gi.is_file()
        write(gi, gtxt + sep + "# ai-native-sdlc local runtime state\n%s/\n" % L.STATE_DIRNAME)
        act("MERGE" if existed else "CREATE", ".gitignore", "ignore %s/" % L.STATE_DIRNAME)
    # 6. changes/
    cd = L.changes_dir(root, ctx.cfg)
    crel = ctx.cfg.get("changes_dir") or "changes"
    if cd.is_dir():
        act("SKIP", crel + "/", "exists")
    else:
        write(cd / ".gitkeep", "")
        act("CREATE", crel + "/.gitkeep", "home for changes/<id>/")
    # 7. CLAUDE.md routing block
    proposed = ""
    mds = [p for p in (root / "CLAUDE.md", root / ".claude" / "CLAUDE.md") if p.is_file()]
    if any(re.search(r"^%s\s*$" % re.escape(ROUTING_HEADING), p.read_text(encoding="utf-8", errors="replace"),
                     re.M | re.I) for p in mds):
        act("SKIP", "CLAUDE.md", "already has '%s'" % ROUTING_HEADING)
    else:
        md = mds[0] if mds else root / "CLAUDE.md"
        rel = ctx.rel(md)
        if not a.claude_md:
            proposed = ROUTING_BLOCK
            act("PROPOSE", rel, "routing block (printed below); rerun init with --claude-md to add it")
        else:
            existed = md.is_file()
            txt = md.read_text(encoding="utf-8") if existed else ""
            sep = "" if (not txt or txt.endswith("\n\n")) else "\n" if txt.endswith("\n") else "\n\n"
            write(md, txt + sep + ROUTING_BLOCK)
            act("MERGE" if existed else "CREATE", rel, "routing block '%s'" % ROUTING_HEADING)
    # 8. CI
    if a.with_ci:
        gdir = ASSETS / "github"
        for src in sorted(gdir.glob("*.yml")) if gdir.is_dir() else []:
            dst = root / ".github" / "workflows" / src.name
            rel = ".github/workflows/" + src.name
            if dst.exists():
                act("SKIP", rel, "exists")
            else:
                write(dst, src.read_text(encoding="utf-8"))
                act("CREATE", rel, "workflow")
    out("ai-native-sdlc init%s — %s" % (" (dry run: nothing written)" if dry else "", root))
    w = max([len(r) for _, r, _ in actions] + [10])
    for kind, rel, detail in actions:
        out("  %-7s  %-*s  %s" % (kind, w, rel, detail))
    if proposed:
        out("proposed CLAUDE.md block (add it with `init --claude-md`, or paste it yourself):")
        out(proposed.rstrip("\n"))
    if not dry:
        out("next: review and commit the files above (Claude Code loads new hooks at session start or via /hooks); "
            "then `%s new <slug> --title \"…\" --tier <tier>`" % ctx.cli)
    return 0


# ---------------------------------------------------------------- package-plugin

PLUGIN_DESCRIPTION = ("[Alpha] AI-native SDLC loop for regulated teams (fintech, payments, medtech): intent, spec, plan, build, verify, review, gated release and monitoring, "
                      "with human approval gates, guard hooks and sdlc-* subagents.")
PLUGIN_SKILL = "ai-native-sdlc"  # the hook commands below name this folder
_JUNK = {"__pycache__", ".pytest_cache", ".mypy_cache", ".DS_Store", ".git"}
_REPO_ONLY = {"tests", "examples", ".github", ".claude-plugin"}  # repo files that don't belong in an installed skill


def _skill_version() -> str:
    try:
        meta, _ = L.parse_frontmatter((L.SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    md = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    return str(md.get("version") or meta.get("version") or "0.0.0")


def _skill_files(src: Path) -> List[Path]:
    """Files of the skill to ship: nothing in _REPO_ONLY, no caches, and in evals/ only the top-level
    case and runner files (eval workspaces and results stay behind)."""
    keep = []
    for f in sorted(src.rglob("*")):
        rel = f.relative_to(src)
        if not f.is_file() or f.is_symlink() or any(p in _JUNK for p in rel.parts) or f.suffix == ".pyc":
            continue
        if rel.parts[0] in _REPO_ONLY or (rel.parts[0] == "evals" and (len(rel.parts) != 2 or f.suffix not in (".json", ".py"))):
            continue
        keep.append(f)
    return keep


def cmd_package_plugin(ctx: Ctx, a) -> int:
    name = (a.name or PLUGIN_SKILL).strip()
    if not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", name):
        raise L.SdlcError("--name must be kebab-case (lowercase letters, digits and hyphens)")
    version = (a.version or _skill_version()).strip()
    dest = Path(os.path.abspath(a.out)).resolve()
    src = L.SKILL_DIR
    if dest == src or src in dest.parents or dest in src.parents:
        raise L.SdlcError("--out must be outside the skill directory (%s)" % src)
    if dest.exists() and any(dest.iterdir()) and not (dest / ".claude-plugin" / "plugin.json").is_file():
        raise L.SdlcError("%s exists and is not a plugin directory written by package-plugin; pick an empty --out" % dest)
    for sub in (".claude-plugin", "skills", "agents", "hooks"):
        if (dest / sub).exists():
            shutil.rmtree(str(dest / sub))
    files = _skill_files(src)
    for f in files:
        target = dest / "skills" / PLUGIN_SKILL / f.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(f), str(target))
    agents = sorted((ASSETS / "agents").glob("sdlc-*.md"))
    (dest / "agents").mkdir(parents=True)
    for f in agents:
        shutil.copy2(str(f), str(dest / "agents" / f.name))
    template = json.loads((ASSETS / "settings" / "project-settings.json").read_text(encoding="utf-8"))
    hooks = _subst(template, "${CLAUDE_PLUGIN_ROOT}/skills/" + PLUGIN_SKILL)
    hooks = {"description": "ai-native-sdlc guard hooks: plan guard, fix mode, production gate, self_protect, "
                            "human-turn log and verify-on-stop", "hooks": hooks["hooks"]}
    (dest / "hooks").mkdir(parents=True)
    (dest / "hooks" / "hooks.json").write_text(json.dumps(hooks, indent=2) + "\n", encoding="utf-8")
    manifest: Dict[str, Any] = {"name": name, "version": version, "description": PLUGIN_DESCRIPTION}
    author = (a.author if a.author is not None else _git_user(ctx.root)).strip()
    if author:
        manifest["author"] = {"name": author}
    (dest / ".claude-plugin").mkdir(parents=True)
    (dest / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    out("PACKAGED plugin %s %s at %s: %d skill file(s), %d agent(s), %d hook event(s)" % (
        name, version, dest, len(files), len(agents), len(hooks["hooks"])))
    if not author:
        out("WARN  no author in plugin.json (git user.name is unset): pass --author \"<team>\"; "
            "`claude plugin validate --strict` fails without one")
    out("next: `claude plugin validate %s`, publish it in the org marketplace, and force-enable it in managed "
        "settings (\"enabledPlugins\": {\"%s@<marketplace>\": true}) so its hooks run under "
        "allowManagedHooksOnly. Remove init-installed hooks.py handlers from repos that enable it, or both run."
        % (dest, name))
    return 0


# =========================================================================== CLI

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sdlc.py", description="AI-native SDLC: changes, artifacts and gates.")
    p.add_argument("--repo", help="repository path (default: the git root of the current directory)")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")
    sub.required = True

    s = sub.add_parser("init", help="bootstrap the repo (idempotent)")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--with-ci", action="store_true", help="also copy GitHub workflows")
    s.add_argument("--force", action="store_true", help="overwrite installed subagents")
    s.add_argument("--claude-md", action="store_true", help="append the skill-routing block to CLAUDE.md")

    s = sub.add_parser("new", help="create changes/<id>/intent.md")
    s.add_argument("slug")
    s.add_argument("--title")
    s.add_argument("--tier", choices=L.TIERS)
    s.add_argument("--source", choices=SOURCES)
    s.add_argument("--bug", action="store_true", help="use the bug template (current/expected/unchanged)")
    s.add_argument("--record-id")
    s.add_argument("--author")
    s.add_argument("--from-json", metavar="FILE", help="Maintain-loop diagnosis matching intent.schema.json ('-' = stdin)")
    s.add_argument("--no-activate", action="store_true")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("status", help="active change, open changes, next action")
    s.add_argument("id", nargs="?")
    s.add_argument("--json", action="store_true")
    s.add_argument("--brief", action="store_true", help="short plain text; never fails")
    s.add_argument("--all", action="store_true", help="include closed changes")

    s = sub.add_parser("validate", help="lint artifacts")
    s.add_argument("id")
    s.add_argument("artifact", nargs="?", help="intent|spec|plan (default: all present)")
    s.add_argument("--strict", action="store_true", help="approval-time rules")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("present", help="record a gate presentation and print the gate card")
    s.add_argument("id")
    s.add_argument("gate")

    s = sub.add_parser("approve", help="record a human approval")
    s.add_argument("id")
    s.add_argument("gate")
    s.add_argument("--by", required=True)
    s.add_argument("--role")
    s.add_argument("--evidence")
    s.add_argument("--allow-ci", action="store_true")

    s = sub.add_parser("reject", help="record a human rejection")
    s.add_argument("id")
    s.add_argument("gate")
    s.add_argument("--by", required=True)
    s.add_argument("--reason", required=True)

    s = sub.add_parser("plan-from-accepted", help="write plan.md from the plan accepted in plan mode")
    s.add_argument("id")
    s.add_argument("--by")

    s = sub.add_parser("raise-tier", help="ratchet the tier up")
    s.add_argument("id")
    s.add_argument("tier", choices=L.TIERS)
    s.add_argument("--reason", required=True)

    s = sub.add_parser("skip", help="record a deliberately skipped stage")
    s.add_argument("id")
    s.add_argument("stage", choices=L.STAGES)
    s.add_argument("--reason", required=True)

    s = sub.add_parser("fix-mode", help="make tests read-only while fixing a bug")
    s.add_argument("id")
    s.add_argument("state", choices=("on", "off"))
    s.add_argument("--by")

    s = sub.add_parser("log", help="append RULING|VERIFIED|NOTE|PR_OPENED to the audit log")
    s.add_argument("id")
    s.add_argument("event")
    s.add_argument("fields", nargs="*", metavar="key=value")
    s.add_argument("--message")

    s = sub.add_parser("close", help="close a change")
    s.add_argument("id")
    s.add_argument("--status", choices=("done", "dropped"), default="done")
    s.add_argument("--pr")

    s = sub.add_parser("activate", help="set the active change")
    s.add_argument("id")
    sub.add_parser("deactivate", help="clear the active change")

    s = sub.add_parser("review-local", help="isolated clean-context review of HEAD (config: review in sdlc.json)")
    s.add_argument("id")
    s.add_argument("--base")
    s.add_argument("--force", action="store_true")

    s = sub.add_parser("drift", help="diff vs plan.md 'Files that change'")
    s.add_argument("id")
    s.add_argument("--base")
    s.add_argument("--strict", action="store_true")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("trace", help="FR/SC coverage: spec vs plan (and tests)")
    s.add_argument("id")
    s.add_argument("--tests", action="store_true")
    s.add_argument("--json", action="store_true")

    s = sub.add_parser("metrics", help="lead times, survival and rework from git + audit")
    s.add_argument("--json", action="store_true")
    s.add_argument("--since")

    s = sub.add_parser("package-plugin", help="write a Claude Code plugin (skill, agents, hooks) for a marketplace")
    s.add_argument("--out", required=True, metavar="DIR")
    s.add_argument("--name", default=PLUGIN_SKILL)
    s.add_argument("--version", help="default: metadata.version in SKILL.md")
    s.add_argument("--author", help="manifest author name (default: git user.name; '' to omit)")
    return p


COMMANDS = {
    "init": cmd_init, "new": cmd_new, "status": cmd_status, "validate": cmd_validate,
    "present": cmd_present, "approve": cmd_approve, "reject": cmd_reject,
    "plan-from-accepted": cmd_plan_from_accepted, "raise-tier": cmd_raise_tier, "skip": cmd_skip,
    "fix-mode": cmd_fix_mode, "log": cmd_log, "close": cmd_close, "activate": cmd_activate,
    "deactivate": cmd_deactivate, "drift": cmd_drift, "review-local": cmd_review_local,"trace": cmd_trace, "metrics": cmd_metrics,
    "package-plugin": cmd_package_plugin,
}


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    words = [w for i, w in enumerate(argv) if not w.startswith("-") and not (i and argv[i - 1] == "--repo")]
    if words[:1] == ["status"] and "--brief" in argv:
        # Injected into SKILL.md: must print something and exit 0 whatever happens.
        repo = None
        try:
            if "--repo" in argv:
                repo = argv[argv.index("--repo") + 1]
        except IndexError:
            repo = None
        try:
            print(status_brief(repo))
        except Exception:
            print("sdlc: status unavailable")
        return 0
    args = build_parser().parse_args(argv)
    try:
        ctx = Ctx(args.repo)
        return COMMANDS[args.cmd](ctx, args)
    except GateError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    except (L.SdlcError, ValueError) as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
