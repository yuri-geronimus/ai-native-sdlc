#!/usr/bin/env python3
"""Shared library for the ai-native-sdlc skill scripts (stdlib only, Python 3.9+).

Used by sdlc.py, hooks.py, bands.py and run_evals.py. Public names are a
contract (see DESIGN section 4); keep them stable.
"""
from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SKILL_DIR = Path(__file__).resolve().parent.parent  # symlinks resolved: use for containment checks


def _raw_skill_dir() -> Path:
    """The skill dir as the interpreter was given it (symlinks kept), so printed commands match the
    `${CLAUDE_SKILL_DIR}` allowed-tools rules and hook command paths textually."""
    try:
        a = sys.argv[0] if sys.argv else ""
        if a.endswith(".py"):
            base = os.getcwd()
            pwd = os.environ.get("PWD")
            if not os.path.isabs(a) and pwd and os.path.isdir(pwd) and os.path.samefile(pwd, base):
                base = pwd
            d = Path(os.path.normpath(os.path.join(base, a))).parent
            if d.resolve() == SKILL_DIR / "scripts":
                return d.parent
    except (OSError, ValueError):
        pass
    return Path(os.path.abspath(__file__)).parent.parent


SKILL_DIR_RAW = _raw_skill_dir()
TIERS = ("trivial", "bounded", "standard", "high-risk")
GATES = ("intent", "spec", "plan")
GATE_ARTIFACT = {"intent": "intent.md", "spec": "spec.md", "plan": "plan.md"}
REQUIRED_GATES = {
    "trivial": [],
    "bounded": ["plan"],
    "standard": ["intent", "spec", "plan"],
    "high-risk": ["intent", "spec", "plan"],
}
# The playbook stage each gate closes. Stage 1 is called "Plan" but produces
# intent.md; plan.md is written at the start of Build.
GATE_STAGE = {"intent": "plan", "spec": "design", "plan": "build"}
# Human-readable stage names (JSON keeps the bare stage values).
STAGE_LABEL = {"plan": "1-Plan (intent.md)", "design": "2-Design (spec.md)", "build": "3-Build (plan.md + code)",
               "review": "review (PR open)", "closed": "closed", "dropped": "dropped"}
GATE_REFERENCE = {
    "intent": "references/stage-1-plan.md",
    "spec": "references/stage-2-design.md",
    "plan": "references/stage-3-build.md",
}
STAGES = ("plan", "design", "build", "test", "deploy", "maintain")
EVENTS = (
    "CHANGE_CREATED", "GATE_PRESENTED", "GATE_APPROVED", "GATE_REJECTED",
    "TIER_RAISED", "STAGE_SKIPPED", "FIX_MODE_ON", "FIX_MODE_OFF", "RULING",
    "VERIFIED", "NOTE", "PR_OPENED", "CHANGE_CLOSED",
)
STATE_DIRNAME = ".claude/sdlc-state"

DEFAULT_CONFIG: Dict[str, Any] = {
    "changes_dir": "changes",
    "default_branch": "main",
    "source_of_truth": "repo",
    "record_id_pattern": "",
    "hooks_enabled": True,
    "verify_commands": [],
    "test_globs": [
        "tests/**", "test/**", "**/tests/**", "**/test_*.py", "**/*_test.py",
        "**/*_test.go", "**/*.test.*", "**/*.spec.*", "**/__tests__/**",
    ],
    "protected_paths": [],
    "self_protect": True,
    "plan_guard": True,
    "plan_guard_exempt": [],
    "verify_on_stop": True,
    # Local review before the PR (for repos without CI review). mode: "off" (default), "session" (the working
    # session runs `review` with subagents) or "isolated" (`sdlc.py review-local`: a separate `claude -p` per pass
    # on a clean snapshot of HEAD, no conversation history; CLAUDE.md/AGENTS.md stripped unless project_docs).
    "review": {"mode": "off", "passes": ["Bugs", "Security", "Compliance"], "project_docs": False,
               "model": "", "timeout_s": 900},
    "post_edit": [],
    "production_gate": {
        "bash_patterns": [
            r"\bdeploy\b.*\bprod(uction)?\b",
            r"\bprod(uction)?\b.*\bdeploy\b",
            r"kubectl\s+.*(apply|delete|rollout|scale|set|patch|replace|edit)\b.*(-n|--namespace)[ =]?prod",
            r"kubectl\s+[^|;&]*(-n|--namespace)[ =]?prod[^|;&]*\b(apply|delete|rollout|scale|set|patch|replace|edit)\b",
            r"kubectl\b[^|;&]*--(context|cluster)[ =]\S*prod[^|;&]*\b(apply|delete|rollout|scale|set|patch|replace|edit)\b",
            r"kubectl\b[^|;&]*\b(apply|delete|rollout|scale|set|patch|replace|edit)\b[^|;&]*--(context|cluster)[ =]\S*prod",
            r"\bhelm\s+(upgrade|install|rollback|uninstall)\b.*\bprod",
            r"\bhelm\b[^|;&]*--kube-context[ =]\S*prod[^|;&]*\b(upgrade|install|rollback|uninstall)\b",
            r"\bterraform\b(\s+-\S+)*\s+(apply|destroy)\b",
            r"\bgh\s+workflow\s+run\b.*\b(deploy|release)\b.*\bprod",
            r"\bgit\s+push\s+(-\S+\s+)*prod(uction)?\b",
        ],
        "mcp_tools": [r"mcp__.*__deploy_prod.*", r"mcp__.*__.*production.*"],
        "approval_check": "",
        "approval_timeout_s": 20,
    },
    "runbooks": {},
    "evals_dir": "evals",
}

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class SdlcError(Exception):
    """Usage or environment error (missing change, git unavailable, bad config)."""


# --------------------------------------------------------------------------- git

def git(args: List[str], cwd: Any = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run git; raise SdlcError with a clear message if git is missing or fails (check=True)."""
    try:
        proc = subprocess.run(
            ["git"] + list(args), cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
        )
    except FileNotFoundError:
        raise SdlcError("git is not installed or not on PATH; install git and retry")
    if check and proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise SdlcError("git %s failed: %s" % (" ".join(args), msg[-1] if msg else "exit %d" % proc.returncode))
    return proc


def repo_root(start: Any = None) -> Path:
    base = Path(start) if start else Path.cwd()
    try:
        proc = git(["-C", str(base), "rev-parse", "--show-toplevel"], check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return Path(proc.stdout.strip()).resolve()
    except SdlcError:
        pass
    return base.resolve()


def in_git_repo(path: Any) -> bool:
    try:
        return git(["-C", str(path), "rev-parse", "--git-dir"], check=False).returncode == 0
    except SdlcError:
        return False


# ------------------------------------------------------------------------ config

def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(root: Any) -> dict:
    path = Path(root) / ".claude" / "sdlc.json"
    if not path.is_file():
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError("invalid JSON in %s: %s" % (path, exc))
    if not isinstance(data, dict):
        raise ValueError("invalid config %s: top level must be a JSON object" % path)
    return _deep_merge(DEFAULT_CONFIG, data)


def state_dir(root: Any) -> Path:
    d = Path(root) / STATE_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"
    if not gi.exists():
        try:
            gi.write_text("# local runtime state of ai-native-sdlc; never commit\n*\n", encoding="utf-8")
        except OSError:
            pass
    return d


def changes_dir(root: Any, cfg: dict) -> Path:
    return Path(root) / (cfg.get("changes_dir") or "changes")


def list_changes(root: Any, cfg: dict) -> list:
    d = changes_dir(root, cfg)
    if not d.is_dir():
        return []
    return sorted(
        p.name for p in d.iterdir()
        if p.is_dir() and ((p / "intent.md").is_file() or (p / "audit.md").is_file())
    )


def change_dir(root: Any, cfg: dict, change_id: str) -> Path:
    if not change_id or not _ID_RE.match(change_id):
        raise SdlcError("invalid change id %r" % change_id)
    d = changes_dir(root, cfg) / change_id
    if not d.is_dir():
        known = list_changes(root, cfg)
        hint = (" Known changes: " + ", ".join(known[-8:])) if known else " No changes exist yet."
        raise SdlcError("change %r not found under %s/.%s" % (change_id, cfg.get("changes_dir", "changes"), hint))
    return d


# ------------------------------------------------------------------------- YAML

def yaml_load(text: str) -> Any:
    """PyYAML safe_load when importable, else the yaml-lite subset parser."""
    try:
        import yaml  # type: ignore
    except ImportError:
        return yaml_lite_load(text)
    return _normalize_dates(yaml.safe_load(text))


def _normalize_dates(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _normalize_dates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_dates(v) for v in obj]
    if isinstance(obj, _dt.datetime):
        if obj.tzinfo is not None:
            obj = obj.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        return obj.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(obj, _dt.date):
        return obj.isoformat()
    return obj


_INT_RE = re.compile(r"^[-+]?[0-9]+$")
_FLOAT_RE = re.compile(r"^[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)(?:[eE][-+]?[0-9]+)?$")


def _strip_comment(line: str) -> str:
    in_s = in_d = False
    i = 0
    while i < len(line):
        ch = line[i]
        if in_d:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_d = False
        elif in_s:
            if ch == "'":
                if i + 1 < len(line) and line[i + 1] == "'":
                    i += 2
                    continue
                in_s = False
        else:
            prev = line[i - 1] if i else " "
            if ch == "#" and prev in " \t":
                return line[:i].rstrip()
            if ch in "\"'" and (i == 0 or prev in " \t[{,:-"):
                if ch == '"':
                    in_d = True
                else:
                    in_s = True
        i += 1
    return line.rstrip()


def _bracket_balance(s: str) -> int:
    depth = 0
    in_s = in_d = False
    i = 0
    while i < len(s):
        ch = s[i]
        if in_d:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_d = False
        elif in_s:
            if ch == "'":
                in_s = False
        elif ch == '"':
            in_d = True
        elif ch == "'":
            in_s = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        i += 1
    return depth


def _scalar(tok: str, lineno: int) -> Any:
    t = tok.strip()
    if t == "" or t in ("~", "null", "Null", "NULL"):
        return None
    if t[0] == '"':
        if len(t) < 2 or not t.endswith('"'):
            raise ValueError("line %d: unterminated double-quoted string" % lineno)
        try:
            return json.loads(t)
        except ValueError:
            return t[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if t[0] == "'":
        if len(t) < 2 or not t.endswith("'"):
            raise ValueError("line %d: unterminated single-quoted string" % lineno)
        return t[1:-1].replace("''", "'")
    if t in ("true", "True", "TRUE"):
        return True
    if t in ("false", "False", "FALSE"):
        return False
    if t[0] in "&*!|>%@`":
        raise ValueError("line %d: unsupported YAML syntax %r (anchors, aliases, tags and block "
                         "scalars need PyYAML; quote the value instead)" % (lineno, t[:20]))
    if _INT_RE.match(t):
        return int(t)
    if _FLOAT_RE.match(t):
        return float(t)
    if t in (".inf", ".Inf", "+.inf"):
        return float("inf")
    if t in ("-.inf", "-.Inf"):
        return float("-inf")
    if t in (".nan", ".NaN"):
        return float("nan")
    return t


class _Flow:
    def __init__(self, s: str, lineno: int):
        self.s, self.i, self.lineno = s, 0, lineno

    def err(self, msg: str) -> ValueError:
        return ValueError("line %d: %s in flow collection %r" % (self.lineno, msg, self.s[:60]))

    def ws(self) -> None:
        while self.i < len(self.s) and self.s[self.i] in " \t\n":
            self.i += 1

    def value(self, ctx: str) -> Any:
        self.ws()
        if self.i >= len(self.s):
            raise self.err("unexpected end")
        ch = self.s[self.i]
        if ch == "{":
            return self.mapping()
        if ch == "[":
            return self.sequence()
        if ch in "\"'":
            return self.quoted()
        return self.plain(ctx)

    def quoted(self) -> Any:
        q = self.s[self.i]
        j = self.i + 1
        while j < len(self.s):
            if q == '"' and self.s[j] == "\\":
                j += 2
                continue
            if self.s[j] == q:
                if q == "'" and j + 1 < len(self.s) and self.s[j + 1] == "'":
                    j += 2
                    continue
                break
            j += 1
        if j >= len(self.s):
            raise self.err("unterminated string")
        tok = self.s[self.i:j + 1]
        self.i = j + 1
        return _scalar(tok, self.lineno)

    def plain(self, ctx: str) -> Any:
        j = self.i
        while j < len(self.s):
            ch = self.s[j]
            if ch in ",]}":
                break
            if ctx == "key" and ch == ":" and (j + 1 >= len(self.s) or self.s[j + 1] in " ,}]"):
                break
            j += 1
        tok = self.s[self.i:j].strip()
        self.i = j
        return _scalar(tok, self.lineno)

    def mapping(self) -> dict:
        self.i += 1
        out: Dict[str, Any] = {}
        while True:
            self.ws()
            if self.i >= len(self.s):
                raise self.err("missing '}'")
            if self.s[self.i] == "}":
                self.i += 1
                return out
            key = self.value("key")
            self.ws()
            val = None
            if self.i < len(self.s) and self.s[self.i] == ":":
                self.i += 1
                self.ws()
                if self.i < len(self.s) and self.s[self.i] not in ",}":
                    val = self.value("map")
            out[str(key) if key is not None else "null"] = val
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == ",":
                self.i += 1
            elif self.i < len(self.s) and self.s[self.i] == "}":
                continue
            else:
                raise self.err("expected ',' or '}'")

    def sequence(self) -> list:
        self.i += 1
        out: List[Any] = []
        while True:
            self.ws()
            if self.i >= len(self.s):
                raise self.err("missing ']'")
            if self.s[self.i] == "]":
                self.i += 1
                return out
            out.append(self.value("seq"))
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == ",":
                self.i += 1
            elif self.i < len(self.s) and self.s[self.i] == "]":
                continue
            else:
                raise self.err("expected ',' or ']'")


def _parse_inline(text: str, lineno: int) -> Any:
    t = text.strip()
    if t[:1] in ("{", "["):
        f = _Flow(t, lineno)
        val = f.value("top")
        f.ws()
        if f.i != len(t):
            raise ValueError("line %d: trailing text after flow collection: %r" % (lineno, t[f.i:]))
        return val
    return _scalar(t, lineno)


_QKEY_RE = re.compile(r"""^("(?:[^"\\]|\\.)*"|'(?:[^']|'')*')\s*:(?:\s+(.*))?$""")


def _split_key(content: str) -> Optional[Tuple[str, str]]:
    """Return (key, rest) if content is a 'key: value' mapping entry."""
    if content[:1] in ('"', "'"):
        m = _QKEY_RE.match(content)
        if not m:
            return None
        return str(_scalar(m.group(1), 0)), (m.group(2) or "")
    if content[:1] in "[{":
        return None
    for i, ch in enumerate(content):
        if ch == ":" and (i + 1 == len(content) or content[i + 1] in " \t"):
            key = content[:i].strip()
            if not key:
                return None
            return key, content[i + 1:].strip()
    return None


def _flow_value_start(content: str) -> bool:
    """True if the value part of a block line starts a flow collection."""
    c = content
    while _is_seq(c):
        c = c[1:].lstrip()
    kv = _split_key(c)
    if kv is not None:
        c = kv[1]
    return c[:1] in ("[", "{")


def yaml_lite_load(text: str) -> Any:
    """Parse the YAML subset used by this skill (no PyYAML needed).

    Supports block maps, block lists, flow maps/lists (also spanning lines),
    quoted/plain scalars, ints, floats, true/false/null and comments. Mapping
    keys are always strings. Raises ValueError with a line number otherwise.
    """
    lines: List[List[Any]] = []
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    pending: Optional[List[Any]] = None
    for n, line in enumerate(raw, 1):
        stripped = _strip_comment(line)
        if pending is not None:
            pending[2] += " " + stripped.strip()
            if _bracket_balance(pending[2]) <= 0:
                lines.append(pending)
                pending = None
            continue
        if not stripped.strip():
            continue
        if stripped.strip() in ("---", "..."):
            if lines:
                raise ValueError("line %d: multiple YAML documents are not supported" % n)
            continue
        lead = stripped[: len(stripped) - len(stripped.lstrip())]
        if "\t" in lead:
            raise ValueError("line %d: tab characters are not allowed in indentation" % n)
        if stripped.lstrip().startswith(("? ", "%")):
            raise ValueError("line %d: unsupported YAML syntax (complex keys/directives)" % n)
        entry = [n, len(lead), stripped.strip()]
        if _flow_value_start(entry[2]) and _bracket_balance(entry[2]) > 0:
            pending = entry
            continue
        lines.append(entry)
    if pending is not None:
        raise ValueError("line %d: unclosed flow collection" % pending[0])
    if not lines:
        return None
    val, i = _parse_block(lines, 0, lines[0][1])
    if i < len(lines):
        raise ValueError("line %d: unexpected content (bad indentation?)" % lines[i][0])
    return val


def _is_seq(content: str) -> bool:
    return content == "-" or content.startswith("- ")


def _parse_block(lines: List[List[Any]], i: int, indent: int) -> Tuple[Any, int]:
    n, ind, content = lines[i]
    if ind != indent:
        raise ValueError("line %d: bad indentation" % n)
    if _is_seq(content):
        return _parse_seq(lines, i, indent)
    if _split_key(content) is not None:
        return _parse_map(lines, i, indent)
    val = _parse_inline(content, n)
    return val, i + 1


def _child(lines: List[List[Any]], i: int, indent: int, allow_same_seq: bool) -> Tuple[Any, int]:
    if i < len(lines):
        n, ind, content = lines[i]
        if ind > indent:
            return _parse_block(lines, i, ind)
        if allow_same_seq and ind == indent and _is_seq(content):
            return _parse_seq(lines, i, indent)
    return None, i


def _parse_map(lines: List[List[Any]], i: int, indent: int) -> Tuple[dict, int]:
    out: Dict[str, Any] = {}
    while i < len(lines):
        n, ind, content = lines[i]
        if ind < indent:
            break
        if ind > indent:
            raise ValueError("line %d: unexpected indentation" % n)
        kv = _split_key(content)
        if kv is None:
            if _is_seq(content):
                break
            raise ValueError("line %d: expected 'key: value', got %r" % (n, content[:40]))
        key, rest = kv
        if key in out:
            raise ValueError("line %d: duplicate key %r" % (n, key))
        if rest == "":
            out[key], i = _child(lines, i + 1, indent, allow_same_seq=True)
        else:
            out[key] = _parse_inline(rest, n)
            i += 1
    return out, i


def _parse_seq(lines: List[List[Any]], i: int, indent: int) -> Tuple[list, int]:
    out: List[Any] = []
    while i < len(lines):
        n, ind, content = lines[i]
        if ind != indent or not _is_seq(content):
            if ind > indent:
                raise ValueError("line %d: unexpected indentation" % n)
            break
        rest = content[1:].lstrip()
        if rest == "":
            val, i = _child(lines, i + 1, indent, allow_same_seq=False)
            out.append(val)
            continue
        # "- key: v" or "- - x": re-parse the remainder as a nested block at its column.
        if _is_seq(rest) or _split_key(rest) is not None:
            col = ind + (len(content) - len(rest))
            lines[i] = [n, col, rest]
            val, i = _parse_block(lines, i, col)
            out.append(val)
            continue
        out.append(_parse_inline(rest, n))
        i += 1
    return out, i


# ------------------------------------------------------------------ frontmatter

def _split_frontmatter_raw(text: str) -> Tuple[Optional[str], str]:
    t = text.replace("\r\n", "\n")
    if not t.startswith("---\n"):
        return None, t
    end = re.search(r"^(?:---|\.\.\.)[ \t]*$\n?", t[4:], re.M)
    if not end:
        return None, t
    return t[4:4 + end.start()], t[4 + end.end():]


def parse_frontmatter(text: str) -> tuple:
    """(meta dict, body). No frontmatter -> ({}, text). Invalid YAML -> ValueError."""
    fm, body = _split_frontmatter_raw(text)
    if fm is None:
        return {}, text
    meta = yaml_load(fm) if fm.strip() else {}
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise ValueError("frontmatter must be a mapping")
    return meta, body


_NEEDS_QUOTE_START = tuple("-?:,[]{}#&*!|>'\"%@`")
_DATEISH = re.compile(r"^\d{4}-\d{2}-\d{2}")
# YAML 1.1 booleans/nulls: PyYAML reads these unquoted words as bool/None, yaml-lite as strings.
_YAML11_SPECIAL = {"y", "n", "yes", "no", "on", "off", "true", "false", "null", "~"}


def _yaml_str(s: str) -> str:
    if (s == "" or s != s.strip() or "\n" in s or ": " in s or " #" in s or s.endswith(":")
            or s.startswith(_NEEDS_QUOTE_START) or _DATEISH.match(s) or s.lower() in _YAML11_SPECIAL
            or not isinstance(_scalar(s, 0), str)):
        return json.dumps(s, ensure_ascii=False)
    return s


def _yaml_scalar(v: Any) -> str:
    if v is None:
        return ""
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (dict, list, tuple)):  # JSON is valid YAML flow; both parsers read it back
        return json.dumps(v, ensure_ascii=False, default=str)
    return _yaml_str(str(v))


def dump_frontmatter(meta: dict, body: str) -> str:
    out = ["---"]
    for k, v in meta.items():
        key = _yaml_str(str(k))
        if isinstance(v, (list, tuple)):
            if not v:
                out.append("%s: []" % key)
            else:
                out.append("%s:" % key)
                out.extend("  - %s" % _yaml_scalar(x) for x in v)
        elif isinstance(v, dict):
            out.append("%s: %s" % (key, _yaml_scalar(v)))
        else:
            sv = _yaml_scalar(v)
            out.append(("%s: %s" % (key, sv)) if sv else ("%s:" % key))
    out.append("---")
    return "\n".join(out) + "\n" + body


def write_text_atomic(path: Any, text: str) -> None:
    p = Path(path)
    tmp = p.with_name(".%s.tmp%d" % (p.name, os.getpid()))
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(p))


def update_frontmatter(path: Any, **updates: Any) -> None:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    meta, body = parse_frontmatter(text)
    meta.update(updates)
    write_text_atomic(p, dump_frontmatter(meta, body))


# --------------------------------------------------------------------- sections

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def sections(body: str) -> dict:
    """{heading lower-stripped: content} for '## ' headings (first occurrence wins)."""
    out: Dict[str, str] = {}
    cur: Optional[str] = None
    buf: List[str] = []
    fence = False
    for line in body.replace("\r\n", "\n").split("\n"):
        if _FENCE_RE.match(line):
            fence = not fence
        m = None if fence else _HEADING_RE.match(line)
        if m and len(m.group(1)) <= 2:
            if cur is not None and cur not in out:
                out[cur] = "\n".join(buf).strip("\n")
            cur = m.group(2).strip().lower() if len(m.group(1)) == 2 else None
            buf = []
            continue
        if cur is not None:
            buf.append(line)
    if cur is not None and cur not in out:
        out[cur] = "\n".join(buf).strip("\n")
    return out


def strip_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.S)


_CHANGELOG_RE = re.compile(r"^##\s+change log\s*$", re.I)


def _changelog_spans(lines: List[str]) -> List[Tuple[int, int]]:
    """[(start, end)) line spans of unfenced '## Change log' sections (heading to next H1/H2 or EOF)."""
    spans: List[Tuple[int, int]] = []
    fence = False
    start: Optional[int] = None
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            fence = not fence
            continue
        if fence:
            continue
        m = _HEADING_RE.match(line)
        if m and len(m.group(1)) <= 2:
            if start is not None:
                spans.append((start, i))
                start = None
            if _CHANGELOG_RE.match(line):
                start = i
    if start is not None:
        spans.append((start, len(lines)))
    return spans


def frozen_part(text: str) -> str:
    """The fingerprinted text: the body without the last unfenced '## Change log' section.
    Fenced look-alikes and any sections after the change log stay frozen."""
    _, body = _split_frontmatter_raw(text)
    lines = body.replace("\r\n", "\n").split("\n")
    spans = _changelog_spans(lines)
    if spans:
        a, b = spans[-1]
        lines = lines[:a] + lines[b:]
    out = [ln.rstrip() for ln in lines]
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def fingerprint(text: str) -> str:
    return hashlib.sha256(frozen_part(text).encode("utf-8")).hexdigest()


def file_fingerprint(path: Any) -> Optional[str]:
    p = Path(path)
    if not p.is_file():
        return None
    return fingerprint(p.read_text(encoding="utf-8"))


# ------------------------------------------------------------------------- globs

_GLOB_CACHE: Dict[str, "re.Pattern[str]"] = {}


def _glob_regex(pattern: str) -> "re.Pattern[str]":
    if pattern in _GLOB_CACHE:
        return _GLOB_CACHE[pattern]
    i, n, out = 0, len(pattern), []
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**", i):
                before_ok = i == 0 or pattern[i - 1] == "/"
                after = pattern[i + 2:i + 3]
                if before_ok and after == "/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                if before_ok and after == "":
                    out.append(".*")
                    i += 2
                    continue
                out.append("[^/]*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            j = pattern.find("]", i + 1)
            if j == -1:
                out.append(re.escape(c))
            else:
                cls = pattern[i + 1:j]
                if cls.startswith("!"):
                    cls = "^" + cls[1:]
                out.append("[" + cls.replace("\\", "\\\\") + "]")
                i = j
        else:
            out.append(re.escape(c))
        i += 1
    rx = re.compile("^" + "".join(out) + "$")
    _GLOB_CACHE[pattern] = rx
    return rx


def _norm_rel(path: str) -> str:
    p = str(path).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def glob_match(path: str, pattern: str) -> bool:
    """gitignore-like: '**' spans directories (incl. none), '*' and '?' stay within a segment.
    A pattern without '/' matches the basename; with '/' it matches the full relative path."""
    p = _norm_rel(path)
    pat = _norm_rel(pattern).lstrip("/")
    if not pat:
        return False
    if pat.endswith("/"):
        pat += "**"
    if "/" not in pat:
        return bool(_glob_regex(pat).match(p.rsplit("/", 1)[-1]))
    return bool(_glob_regex(pat).match(p))


def match_any(path: str, patterns: Iterable[str]) -> bool:
    return any(glob_match(path, pt) for pt in (patterns or []) if isinstance(pt, str))


def planned(rel: str, entries: Iterable[str]) -> bool:
    """True if rel is covered by a plan.md 'Files that change' entry (exact, dir/ prefix or glob)."""
    r = _norm_rel(rel)
    for e in entries:
        e = _norm_rel(e)
        if r == e:
            return True
        if e.endswith("/") and r.startswith(e):
            return True
        if any(ch in e for ch in "*?[") and _glob_regex(e.lstrip("/")).match(r):
            return True
    return False


# ------------------------------------------------------------------------- audit

def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d+))?)?"
                    r"\s*(Z|z|UTC|[+-]\d{2}(?::?\d{2})?)?)?$")


def parse_ts(ts: Any) -> Optional[_dt.datetime]:
    """Parse 'YYYY-MM-DD[(T| )hh:mm[:ss[.frac]][Z|UTC|+hh[:mm]]]' to an aware UTC datetime; None if invalid.
    One strict grammar (not fromisoformat), so Python 3.9 and 3.13 accept the same strings.
    No offset means UTC; a bare date is midnight UTC."""
    if not isinstance(ts, str):
        return None
    m = _TS_RE.match(ts.strip())
    if not m:
        return None
    y, mo, d, hh, mi, ss, frac, off = m.groups()
    tz = _dt.timezone.utc
    if off and off not in ("Z", "z", "UTC"):
        digits = off[1:].replace(":", "")
        delta = _dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
        if delta >= _dt.timedelta(hours=24):
            return None
        tz = _dt.timezone(-delta if off[0] == "-" else delta)
    try:
        dt = _dt.datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0),
                          int((frac or "0")[:6].ljust(6, "0")), tzinfo=tz)
    except ValueError:
        return None
    return dt.astimezone(_dt.timezone.utc)


AUDIT_HEADER = "# Audit log: {id}\n\nAppend-only. Written by `sdlc.py`; do not edit by hand.\n\n"
_FIELD_KEY_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def _audit_value(key: str, value: Any) -> str:
    s = str(value).replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    s = s.replace("|", "/").replace('"', "'")
    if key == "evidence" and len(s) > 300:
        s = s[:297] + "..."
    if s == "" or any(c.isspace() for c in s):
        s = '"%s"' % s
    return s


def audit_append(cdir: Any, event: str, **fields: Any) -> None:
    if event not in EVENTS:
        raise SdlcError("unknown audit event %r" % event)
    path = Path(cdir) / "audit.md"
    if not path.exists():
        path.write_text(AUDIT_HEADER.format(id=Path(cdir).name), encoding="utf-8")
    parts = [now_iso(), event]
    for k, v in fields.items():
        if v is None:
            continue
        if not _FIELD_KEY_RE.match(k):
            raise SdlcError("invalid audit field name %r" % k)
        parts.append("%s=%s" % (k, _audit_value(k, v)))
    line = "- " + " | ".join(parts) + "\n"
    with open(str(path), "a+", encoding="utf-8") as fh:
        fh.seek(0, os.SEEK_END)
        if fh.tell() > 0:
            fh.seek(fh.tell() - 1)
            last = fh.read(1)
            fh.seek(0, os.SEEK_END)
            if last != "\n":
                fh.write("\n")
        fh.write(line)


_AUDIT_LINE_RE = re.compile(r"^- (\S+) \| ([A-Z_]+)(?: \| (.*))?$")


def audit_read(cdir: Any) -> list:
    path = Path(cdir) / "audit.md"
    if not path.is_file():
        return []
    out: List[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _AUDIT_LINE_RE.match(line.rstrip())
        if not m:
            continue
        rec: Dict[str, Any] = {"ts": m.group(1), "event": m.group(2)}
        for part in (m.group(3) or "").split(" | "):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            k = k.strip()
            if len(v) >= 2 and v.startswith('"') and v.endswith('"'):
                v = v[1:-1]
            if k and k not in ("ts", "event"):
                rec[k] = v
        out.append(rec)
    return out


# ---------------------------------------------------------------- active change

def get_active(root: Any) -> Optional[str]:
    p = Path(root) / STATE_DIRNAME / "active.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    cid = data.get("id") if isinstance(data, dict) else None
    return cid if isinstance(cid, str) and cid else None


def is_linked_worktree(root: Any) -> bool:
    """True in a linked git worktree (its .git is a file pointing at the main repository)."""
    return (Path(root) / ".git").is_file()


def set_active(root: Any, change_id: Optional[str]) -> None:
    """Set or clear the active change. In a linked worktree, clearing writes {"id": null}: the hook
    inherits the main checkout's active change only while active.json is absent."""
    p = state_dir(root) / "active.json"
    if change_id is None and not is_linked_worktree(root):
        if p.exists():
            p.unlink()
        return
    write_text_atomic(p, json.dumps({"id": change_id, "ts": now_iso()}) + "\n")


def human_events_path(root: Any) -> Path:
    return Path(root) / STATE_DIRNAME / "human-events.jsonl"


def human_events(root: Any) -> list:
    """Records of human-events.jsonl; each gets '_line' (1-based) for human_event_id()."""
    p = human_events_path(root)
    if not p.is_file():
        return []
    out: List[dict] = []
    for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            rec["_line"] = n
            out.append(rec)
    return out


def plugin_root() -> Optional[Path]:
    """The plugin directory when this skill was installed from `sdlc.py package-plugin` output
    (<plugin>/skills/<skill>/ with <plugin>/hooks/hooks.json running hooks.py), else None."""
    root = SKILL_DIR.parent.parent
    hj = root / "hooks" / "hooks.json"
    try:
        ok = ((root / ".claude-plugin" / "plugin.json").is_file() and SKILL_DIR.parent.name == "skills"
              and "hooks.py" in hj.read_text(encoding="utf-8"))
    except OSError:
        ok = False
    return root if ok else None


def hooks_configured(root: Any) -> bool:
    """True if the guard hooks run here: .claude/settings.json or settings.local.json references
    hooks.py, or this skill came from a plugin that ships them."""
    if plugin_root():
        return True
    for name in ("settings.json", "settings.local.json"):
        p = Path(root) / ".claude" / name
        try:
            if p.is_file() and "hooks.py" in p.read_text(encoding="utf-8", errors="replace"):
                return True
        except OSError:
            continue
    return False


HARNESS_PROMPT_PREFIX = "<task-notification"  # synthetic turns Claude Code injects (background task done)
FUTURE_SLACK_S = 5


def trusted_human_events(root: Any, now: Optional[_dt.datetime] = None) -> list:
    """human_events() minus records that cannot be a real human turn: no string session_id (real hook
    payloads always carry one), a timestamp more than FUTURE_SLACK_S in the future (a far-future record
    would satisfy every later gate), a harness task-notification recorded as a prompt, or a second copy
    of a hook-written event (same second, session, type and text: the project hooks and a plugin copy
    both ran, and one human turn must not count twice)."""
    limit = (now or _dt.datetime.now(_dt.timezone.utc)) + _dt.timedelta(seconds=FUTURE_SLACK_S)
    out, seen = [], set()
    for e in human_events(root):
        ts = parse_ts(e.get("ts"))
        sid = e.get("session_id")
        if not ts or ts > limit or not isinstance(sid, str) or not sid:
            continue
        if (e.get("type") or "prompt") == "prompt" and str(e.get("excerpt") or "").lstrip().startswith(
                HARNESS_PROMPT_PREFIX):
            continue
        if e.get("id"):
            key = (e.get("ts"), sid, e.get("type"), e.get("sha256"))
            if key in seen:
                continue
            seen.add(key)
        out.append(e)
    return out


def human_event_id(rec: dict) -> str:
    """Identity of one human event (not its text: two identical 'approve' replies are two events).
    The hook's own 'id' when present, else timestamp + line number in the append-only file."""
    rid = rec.get("id")
    if isinstance(rid, str) and rid:
        return rid
    return "%s/%s" % (rec.get("ts"), rec.get("_line"))


def used_human_events(root: Any, cfg: dict) -> set:
    """human_event ids already consumed by a gate decision in any change."""
    used = set()
    for cid in list_changes(root, cfg):
        for ev in audit_read(changes_dir(root, cfg) / cid):
            if ev["event"] in ("GATE_APPROVED", "GATE_REJECTED") and ev.get("human_event"):
                used.add(ev["human_event"])
    return used


def headless() -> bool:
    """True in scripted runs (SDLC_NONINTERACTIVE or CI set): no human can answer a gate here."""
    return any(os.environ.get(k, "").strip().lower() not in ("", "0", "false", "no", "off")
               for k in ("SDLC_NONINTERACTIVE", "CI"))


HEADLESS_HANDOFF = ("headless run: do not approve here. The gate is a code owner's review of the artifact PR on "
                    "branch change/{id}. After it is approved, the approver mirrors it into audit.md from an "
                    "interactive Claude Code session in this repo by saying 'approve {gate} for {id}, PR <url>' "
                    "(or reads the artifact there and says 'approve {gate} for {id}')")


def self_protected_paths(root: Any, cfg: Optional[dict] = None) -> List[str]:
    """Repo-relative paths the agent must not edit when self_protect is on (the default): the skill's
    config and hook settings, the installed sdlc-* subagent definitions, and the skill's own directory
    (as 'dir/') when it lives inside the repo."""
    if cfg is not None and not cfg.get("self_protect", True):
        return []
    out = [".claude/sdlc.json", ".claude/settings.json", ".claude/settings.local.json", ".claude/agents/sdlc-*.md"]
    rroot = Path(root).resolve()
    for d in (SKILL_DIR_RAW, SKILL_DIR):
        for base in (Path(root), rroot):
            try:
                rel = Path(os.path.abspath(str(d))).relative_to(Path(os.path.abspath(str(base)))).as_posix() + "/"
            except ValueError:
                continue
            if rel not in out:
                out.append(rel)
    return out


# ----------------------------------------------------------------- gate/status

def gate_state(root: Any, cfg: dict, change_id: str, gate: str, _events: Optional[list] = None) -> dict:
    cdir = change_dir(root, cfg, change_id)
    art = cdir / GATE_ARTIFACT[gate]
    events = audit_read(cdir) if _events is None else _events
    sha = file_fingerprint(art)
    presented_ev = None
    decision = None
    pending = False
    roles: List[str] = []  # roles that approved the current fingerprint since its last presentation/rejection
    for ev in events:
        if ev.get("gate") != gate:
            continue
        if ev["event"] == "GATE_PRESENTED":
            presented_ev = ev
            pending = True
            if ev.get("sha256") != sha:
                roles = []
        elif ev["event"] in ("GATE_APPROVED", "GATE_REJECTED"):
            decision = ev
            pending = False
            if ev["event"] == "GATE_REJECTED":
                roles = []
            elif ev.get("sha256") == sha and ev.get("role") and ev["role"] not in roles:
                roles.append(ev["role"])
    approved = bool(decision and decision["event"] == "GATE_APPROVED")
    return {
        "gate": gate,
        "artifact": GATE_ARTIFACT[gate],
        "exists": art.is_file(),
        "sha": sha,
        "presented": bool(presented_ev and sha and presented_ev.get("sha256") == sha),
        "presented_ts": presented_ev["ts"] if presented_ev else None,
        "pending": bool(pending and sha and presented_ev.get("sha256") == sha),
        "approved": approved,
        "current": bool(approved and sha and decision.get("sha256") == sha),
        "approved_sha": decision.get("sha256") if approved else None,
        "by": decision.get("by") if decision else None,
        "role": decision.get("role") if decision else None,
        "ts": decision["ts"] if decision else None,
        "rejected": bool(decision and decision["event"] == "GATE_REJECTED"),
        "approved_roles": sorted(roles),
    }


def cli_prefix(root: Any = None) -> str:
    """The command prefix for sdlc.py: an absolute path, symlinks kept, so it textually matches the
    skill's `Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/sdlc.py *)` rule and works from any cwd.
    Quoted only when it contains a space (that form may still prompt)."""
    s = (SKILL_DIR_RAW / "scripts" / "sdlc.py").as_posix()
    return "python3 %s" % (('"%s"' % s) if " " in s else s)


HIGH_RISK_ROLES = ("tech-lead", "architect")
HIGH_RISK_ROLE_GATES = ("spec", "plan")
# A high-risk spec needs both: the product owner decides, consulting a tech lead or architect.
SPEC_HIGH_RISK_REQUIRED = ("product-owner", HIGH_RISK_ROLES)


def approver_role(gate: str, tier: str) -> str:
    if gate == "intent":
        return "product-owner"
    if gate == "spec":
        return "product-owner + tech-lead or architect" if tier == "high-risk" else "product-owner"
    return "tech-lead or architect" if tier == "high-risk" else "engineer"


def allowed_roles(gate: str, tier: str) -> Optional[Tuple[str, ...]]:
    """Roles approve accepts for this gate and tier; None = any named role."""
    if tier != "high-risk" or gate not in HIGH_RISK_ROLE_GATES:
        return None
    if gate == "spec":
        return (SPEC_HIGH_RISK_REQUIRED[0],) + SPEC_HIGH_RISK_REQUIRED[1]
    return HIGH_RISK_ROLES


def missing_roles(gate: str, tier: str, approved_roles: Iterable[str]) -> List[str]:
    """Approver roles still needed on the current fingerprint (empty = role requirement met)."""
    have = set(approved_roles or [])
    if tier != "high-risk" or gate not in HIGH_RISK_ROLE_GATES:
        return []
    miss = []
    if gate == "spec" and SPEC_HIGH_RISK_REQUIRED[0] not in have:
        miss.append(SPEC_HIGH_RISK_REQUIRED[0])
    if not have.intersection(HIGH_RISK_ROLES):
        miss.append(" or ".join(HIGH_RISK_ROLES))
    return miss


def required_gates(tier: str, source: Any = None) -> List[str]:
    """Gates for a tier; an agent-written intent (source other than human) always carries the intent gate."""
    req = list(REQUIRED_GATES[tier])
    if source not in (None, "", "human") and "intent" not in req:
        req.insert(0, "intent")
    return req


def effective_tier(meta_tier: Any, events: list) -> str:
    """Highest of the frontmatter tier and every recorded tier (one-way ratchet)."""
    best = meta_tier if meta_tier in TIERS else "standard"
    for ev in events:
        t = ev.get("to") if ev["event"] == "TIER_RAISED" else (
            ev.get("tier") if ev["event"] == "CHANGE_CREATED" else None)
        if t in TIERS and TIERS.index(t) > TIERS.index(best):
            best = t
    return best


def read_meta(path: Any) -> dict:
    """Frontmatter of path; {} if missing or unparseable."""
    p = Path(path)
    if not p.is_file():
        return {}
    try:
        return parse_frontmatter(p.read_text(encoding="utf-8"))[0]
    except (ValueError, OSError):
        return {}


def change_status(root: Any, cfg: dict, change_id: str) -> dict:
    cdir = change_dir(root, cfg, change_id)
    events = audit_read(cdir)
    meta = read_meta(cdir / "intent.md")
    tier = effective_tier(meta.get("tier"), events)
    gates = {g: gate_state(root, cfg, change_id, g, events) for g in GATES}
    for g, st in gates.items():
        # High-risk spec/plan gates need specific roles on the current fingerprint (spec: product owner
        # and a tech lead or architect; plan: tech lead or architect). Until then the gate is not current,
        # also when a lower role approved before raise-tier.
        st["missing_roles"] = missing_roles(g, tier, st["approved_roles"]) if st["current"] else []
        st["role_ok"] = not st["missing_roles"]
        if not st["role_ok"]:
            st["current"] = False
    # Source from the audit log: frontmatter is outside the fingerprint, so editing it voids nothing.
    created_ev = next((ev for ev in events if ev["event"] == "CHANGE_CREATED"), None)
    source = (created_ev or {}).get("source") or meta.get("source")
    required = required_gates(tier, source)
    skipped = [ev.get("stage") for ev in events if ev["event"] == "STAGE_SKIPPED" and ev.get("stage")]
    if tier == "bounded" and "design" not in skipped:
        skipped.append("design")
    fix_mode = False
    fix_commit = None
    pr_url = None
    closed = None
    for ev in events:
        if ev["event"] == "FIX_MODE_ON":
            fix_mode, fix_commit = True, ev.get("commit")
        elif ev["event"] == "FIX_MODE_OFF":
            fix_mode, fix_commit = False, None
        elif ev["event"] == "PR_OPENED":
            pr_url = ev.get("url") or ev.get("pr") or pr_url or ""
        elif ev["event"] == "CHANGE_CLOSED":
            closed = ev.get("status") or "done"
    created = next((ev["ts"] for ev in events if ev["event"] == "CHANGE_CREATED"), meta.get("created"))
    status = {
        "id": change_id,
        "path": cdir.relative_to(Path(root)).as_posix() if _is_under(cdir, root) else str(cdir),
        "title": str(meta.get("title") or change_id),
        "tier": tier,
        "source": source,
        "status": closed if closed in ("done", "dropped") else (meta.get("status") or "draft"),
        "record_id": meta.get("record_id") or None,
        "created": created,
        "artifacts": {g: (cdir / GATE_ARTIFACT[g]).is_file() for g in GATES},
        "gates": gates,
        "required_gates": required,
        "skipped": skipped,
        "fix_mode": fix_mode,
        "fix_commit": fix_commit,
        "pr": pr_url,
    }
    cli = cli_prefix(root)
    stage, nxt = _next_action(cli, change_id, tier, gates, required, closed, pr_url, status["artifacts"],
                              status["path"], cfg)
    status["stage"], status["next"] = stage, nxt
    return status


def _is_under(path: Path, root: Any) -> bool:
    try:
        path.resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def _write_hint(cli: str, cid: str, g: str, path: str, gates: dict) -> str:
    tpl = (SKILL_DIR_RAW / "assets" / "templates" / GATE_ARTIFACT[g]).as_posix()
    shas = "intent_sha: %s" % (gates["intent"]["sha"] or '""')
    if g == "plan":
        shas += ", spec_sha: %s" % (gates["spec"]["sha"] or '""')
    how = ("interactive: plan mode, then `%s plan-from-accepted %s`; headless or high-risk: " % (cli, cid)
           if g == "plan" else "")
    return ("write %s as %s/%s (%sfrom %s with %s), then `%s validate %s %s --strict` and `%s present %s %s` "
            "(see %s)" % (GATE_ARTIFACT[g], path, GATE_ARTIFACT[g], how, tpl, shas, cli, cid, g, cli, cid, g,
                          GATE_REFERENCE[g]))


def _next_action(cli: str, cid: str, tier: str, gates: dict, required: list,
                 closed: Optional[str], pr_url: Optional[str], artifacts: dict,
                 path: str = "", cfg: Optional[dict] = None) -> Tuple[str, str]:
    if closed:
        return ("dropped" if closed == "dropped" else "closed"), "none (change %s)" % ("dropped" if closed == "dropped" else "closed")
    if not artifacts["intent"]:
        return "plan", "write intent.md (see references/stage-1-plan.md)"
    for g in required:
        st = gates[g]
        stage = GATE_STAGE[g]
        art = GATE_ARTIFACT[g]
        if not st["exists"]:
            return stage, _write_hint(cli, cid, g, path or "changes/%s" % cid, gates)
        if st["current"]:
            continue
        if not st.get("role_ok", True):
            miss = st.get("missing_roles") or [approver_role(g, tier)]
            if headless():
                return stage, "high-risk %s still needs a %s approval; %s" % (
                    art, " and a ".join(miss), HEADLESS_HANDOFF.format(gate=g, id=cid))
            return stage, ("high-risk %s still needs a %s approval (so far: %s): ask them, then "
                           "`%s approve %s %s --by \"<name>\" --role %s`"
                           % (art, " and a ".join(miss), ", ".join(st.get("approved_roles") or []) or "none",
                              cli, cid, g, miss[0].split(" or ")[0]))
        if st["pending"]:
            if headless():
                return stage, "awaiting %s approval of %s by the %s; %s" % (
                    g, art, approver_role(g, tier), HEADLESS_HANDOFF.format(gate=g, id=cid))
            return stage, ("awaiting %s approval of %s: ask the %s; on approval run "
                           "`%s approve %s %s --by \"<name>\" --role <role>`"
                           % (g, art, approver_role(g, tier), cli, cid, g))
        if st["approved"]:
            return stage, ("%s changed after approval: re-present: `%s present %s %s`, then ask the %s"
                           % (art, cli, cid, g, approver_role(g, tier)))
        if st["rejected"]:
            return stage, ("%s was rejected: revise it, then `%s present %s %s`" % (art, cli, cid, g))
        return stage, ("`%s present %s %s`, then ask the %s to approve"
                       % (cli, cid, g, approver_role(g, tier)))
    if pr_url is not None:
        return "review", ("PR open%s: address review findings; after merge run `%s close %s --pr <url>`"
                          % ((" (%s)" % pr_url) if pr_url else "", cli, cid))
    mode = ((cfg or {}).get("review") or {}).get("mode", "off")
    review = ""
    if mode == "isolated":
        review = ", run `%s review-local %s` (isolated clean-context review, fix Important findings)" % (cli, cid)
    elif mode == "session":
        review = ", run local `review %s` (fix Important findings)" % cid
    return "build", ("implement plan.md, verify%s, open PR (then `%s log %s PR_OPENED url=<pr-url>`)"
                     % (review, cli, cid))


# ------------------------------------------------------------------- git files

def _git_z(root: Any, args: List[str]) -> List[str]:
    proc = git(["-C", str(root)] + args)
    return [x for x in proc.stdout.split("\0") if x]


def changed_files(root: Any, base: str) -> list:
    """Files changed on this branch vs base (base...HEAD) plus uncommitted and untracked files."""
    files: List[str] = []
    head_ok = git(["-C", str(root), "rev-parse", "--verify", "-q", "HEAD"], check=False).returncode == 0
    if base and head_ok:
        if git(["-C", str(root), "rev-parse", "--verify", "-q", base + "^{commit}"], check=False).returncode != 0:
            raise SdlcError("base ref %r not found (use --base REF)" % base)
        files.extend(_git_z(root, ["diff", "--name-only", "-z", "--no-renames", "%s...HEAD" % base]))
    entries = _git_z(root, ["status", "--porcelain=v1", "-z", "-uall", "--no-renames"])
    for e in entries:
        if len(e) > 3 and e[2] == " ":
            files.append(e[3:])
    seen: Dict[str, None] = {}
    for f in files:
        seen.setdefault(_norm_rel(f), None)
    return sorted(seen)


_PATHISH = re.compile(r"^[A-Za-z0-9_.*?\[\]{}@+\-/]+$")


def _clean_token(tok: str) -> str:
    t = tok.strip().strip("`'\"")
    m = re.match(r"^\[[^\]]*\]\(([^)]+)\)", t)
    if m:
        t = m.group(1)
    t = t.strip("*_").rstrip(",;:").strip("()")
    while t.startswith("./"):
        t = t[2:]
    return t


def _looks_like_path(t: str) -> bool:
    if not t or not _PATHISH.match(t) or t.endswith(".") or t.startswith("-"):
        return False
    if "/" in t:
        return not t.startswith("//") and t != "/"
    return bool(re.search(r"\.[A-Za-z0-9*]{1,10}$", t)) and not re.match(r"^[0-9.]+$", t)


def plan_files(plan_text: str) -> list:
    """Paths listed in plan.md '## Files that change' (ordered, deduped)."""
    try:
        _, body = parse_frontmatter(plan_text)
    except ValueError:
        _, body = _split_frontmatter_raw(plan_text)
    sec = strip_comments(sections(body).get("files that change", ""))
    found: List[str] = []

    def add(t: str) -> None:
        t = _clean_token(t)
        if _looks_like_path(t) and t not in found:
            found.append(t)

    for line in sec.splitlines():
        s = line.strip()
        if not s:
            continue
        ticks = re.findall(r"`([^`]+)`", s)
        if ticks:
            for t in ticks:
                add(t.split()[0] if t.split() else t)
            continue
        if s.startswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if cells and not re.match(r"^:?-{2,}:?$", cells[0]):
                add(cells[0])
            continue
        m = re.match(r"^(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(\S+)", s)
        if m:
            add(m.group(1))
            continue
        for tok in re.split(r"[\s,;]+", s):
            add(tok)
    return found
