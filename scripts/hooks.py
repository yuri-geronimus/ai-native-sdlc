#!/usr/bin/env python3
"""Claude Code hook dispatcher for the ai-native-sdlc skill (stdlib only, Python 3.9+).

    python3 hooks.py user-prompt   # UserPromptSubmit: record human presence, inject active-change context
    python3 hooks.py pre-tool      # PreToolUse (file tools, Bash|Monitor|PowerShell, mcp__.*): guards + production gate
    python3 hooks.py post-tool     # PostToolUse (file tools, AskUserQuestion, ExitPlanMode)
    python3 hooks.py stop          # Stop: block "done" when source changed after the last verify run

Reads the hook JSON on stdin. `sdlc.py init` installs these handlers into
.claude/settings.json (see assets/settings/project-settings.json).

Contract (DESIGN section 7):
- Deny = exit 2 with the reason on stderr. A PreToolUse exit 2 blocks the call even if a
  permission rule allows it; on PostToolUse exit 2 only shows stderr to Claude (the tool
  already ran).
- The production gate fails CLOSED: any error while evaluating it denies the call, an
  unparseable .claude/sdlc.json denies every shell/MCP call, and `hooks_enabled: false` does
  not switch it off. Every other guard fails OPEN (exit 0) and writes the error to the hook log.
- production_gate.bash_patterns are matched against the command minus inert text (commit messages,
  echo, grep; see _gate_text), and against the raw command whenever that text could be executed.
- A shell command that runs hooks.py itself is denied: only Claude Code runs the hooks.
- Claude Code fails OPEN when a hook times out, so the approval check is capped below the
  pre-tool hook timeout (30 s in project-settings.json); see APPROVAL_TIMEOUT_CAP_S.
- Decisions (deny, gate allow, block, reminder, record, error) are appended as JSON lines
  to .claude/sdlc-state/hook-log.jsonl: {ts, event, tool, decision, reason, change}.
  Pass-through calls where no guard applied are not logged.

Known limits (heuristics, not a sandbox): shell write detection (Bash, Monitor, PowerShell)
parses the command line only, so writes through scripts, interpreters (`python -c`), `bash -c`
strings or variables other than $PWD/$HOME are not seen; PowerShell coverage is best-effort
(redirects and the common *-Content/*-Item/Out-File cmdlets). Use `sandbox.filesystem.denyWrite` for paths that must never be
written, and the CI checks (`sdlc.py drift --strict`) as the backstop.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.dont_write_bytecode = True  # keep scripts/__pycache__ out of the installed skill
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sdlc_lib as L  # noqa: E402

FILE_TOOLS = ("Edit", "Write", "NotebookEdit", "MultiEdit")
SHELL_TOOLS = ("Bash", "Monitor", "PowerShell")  # all read tool_input.command; Monitor shares Bash's rules
EXCERPT = 300
CONTEXT_MAX = 600
APPROVAL_TIMEOUT_CAP_S = 25
POST_EDIT_TIMEOUT_S = 60
TAIL = 1500
STAGE5 = "references/stage-5-deploy.md"
NO_APPROVAL_MSG = (
    "Production actions need a release authorization by a named release manager. The agent "
    "prepares the release; the release pipeline (GitHub Environment `production` with required "
    "reviewers) performs it. See references/stage-5-deploy.md."
)
# Guardrail configuration the agent must not rewrite (self_protect in .claude/sdlc.json, default on;
# the list is sdlc_lib.self_protected_paths). Written by `sdlc.py init` or by a human.
SELF_PROTECT_MSG = ("Guardrail configuration is changed by a human: edit it yourself, or set \"self_protect\": false "
                    "in .claude/sdlc.json by hand. sdlc.py init still works because it is a script, not an agent edit.")


class Deny(Exception):
    """Raised by a guard to block the tool call (exit 2, message on stderr)."""


# ------------------------------------------------------------------ context

def _case_fix(base: Path, parts: Any) -> List[str]:
    """On-disk spelling of `parts` below `base` on case-insensitive filesystems (APFS, NTFS), so
    `.CLAUDE/x` and `Tests/x` compare equal to `.claude/x` and `tests/x`. Components that do not
    exist (or are ambiguous) are kept as typed from there on."""
    parts = [x for x in parts if x not in ("", ".")]
    out: List[str] = []
    cur = base
    for i, name in enumerate(parts):
        try:
            entries = os.listdir(str(cur))
        except OSError:
            return out + parts[i:]
        if name not in entries:
            hits = [e for e in entries if e.casefold() == name.casefold()]
            if len(hits) != 1 or not os.path.lexists(str(cur / name)):  # lexists False: case-sensitive fs
                return out + parts[i:]
            name = hits[0]
        out.append(name)
        cur = cur / name
    return out


def _true_case(p: Path) -> Path:
    return Path(p.anchor).joinpath(*_case_fix(Path(p.anchor), p.parts[1:])) if p.is_absolute() else p


def _main_checkout(root: Path) -> Optional[Path]:
    """The main checkout when root is a linked git worktree (its .git is a file), else None."""
    if not (root / ".git").is_file():
        return None
    try:
        proc = L.git(["-C", str(root), "rev-parse", "--git-dir", "--git-common-dir"], check=False)
    except L.SdlcError:
        return None
    lines = proc.stdout.split("\n")
    if proc.returncode != 0 or len(lines) < 2:
        return None
    gd, cd = (Path(os.path.realpath(str(root / x.strip()))) for x in lines[:2])
    main = cd.parent
    return main if gd != cd and main != root and (main / L.STATE_DIRNAME).is_dir() else None


class Ctx:
    def __init__(self, event: str, payload: dict):
        self.event = event
        self.payload = payload
        self.tool = str(payload.get("tool_name") or "")
        cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
        self.cwd = Path(str(cwd))
        self.root = _true_case(L.repo_root(self.cwd if self.cwd.is_dir() else None))
        self.cfg_error: Optional[str] = None
        try:
            self.cfg = L.load_config(self.root)
        except ValueError as exc:
            self.cfg_error = str(exc)
            self.cfg = copy.deepcopy(L.DEFAULT_CONFIG)
        self._active: Any = False  # False = not loaded yet
        self.change: Optional[str] = None
        self.inherited = False  # active change read from the main checkout (linked worktree)
        self.inherit_missing: Optional[str] = None  # ...whose folder is missing on this branch

    # The active change's status dict, or None (no active change, missing folder, closed).
    # A linked worktree without its own active.json inherits the main checkout's active change
    # (read-only); state, human events and change folders still come from the worktree itself.
    def active(self) -> Optional[dict]:
        if self._active is not False:
            return self._active
        self._active = None
        cid = L.get_active(self.root)
        if not cid and not (self.root / L.STATE_DIRNAME / "active.json").exists():
            main = _main_checkout(self.root)
            cid = L.get_active(main) if main else None
            self.inherited = bool(cid)
        if cid:
            if self.inherited and not (L.changes_dir(self.root, self.cfg) / cid).is_dir():
                self.inherit_missing = self.change = cid
                return None
            try:
                st = L.change_status(self.root, self.cfg, cid)
            except (L.SdlcError, OSError, ValueError):
                st = None
            if st and st["stage"] not in ("closed", "dropped"):
                self._active = st
                self.change = cid
        return self._active

    def rel(self, path: Any) -> Optional[str]:
        """Repo-relative POSIX path in its on-disk case, or None if path is outside the repo root."""
        if not path:
            return None
        p = Path(str(path)).expanduser()
        if not p.is_absolute():
            p = self.cwd / p
        rparts = self.root.parts
        # normpath: root itself may be reached through a symlink that realpath resolved differently
        for cand in (Path(os.path.realpath(str(p))), Path(os.path.normpath(str(p)))):
            cp = cand.parts
            if len(cp) >= len(rparts) and all(a == b or a.casefold() == b.casefold() for a, b in zip(cp, rparts)):
                return "/".join(_case_fix(self.root, cp[len(rparts):])) or "."
        return None

    def changes_prefix(self) -> str:
        return (self.cfg.get("changes_dir") or "changes").strip("/")

    def under_changes(self, rel: str) -> bool:
        cd = self.changes_prefix()
        return rel == cd or rel.startswith(cd + "/")

    def cli(self) -> str:
        return L.cli_prefix(self.root)

    def log(self, decision: str, reason: str = "", **extra: Any) -> None:
        rec = {"ts": L.now_iso(), "event": self.event, "tool": self.tool, "decision": decision,
               "reason": reason[:1000], "change": self.change}
        rec.update(extra)
        try:
            with open(str(L.state_dir(self.root) / "hook-log.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass


def _append_jsonl(path: Path, rec: dict) -> None:
    with open(str(path), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _human_event(ctx: "Ctx", kind: str, text: str) -> None:
    """Append one human event; `id` lets approve consume each event once (sdlc_lib.human_event_id)."""
    _append_jsonl(L.state_dir(ctx.root) / "human-events.jsonl", {
        "id": uuid.uuid4().hex, "ts": L.now_iso(), "type": kind, "session_id": ctx.payload.get("session_id"),
        "sha256": _sha(text), "excerpt": text.strip()[:EXCERPT]})


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _tail(text: str, n: int = TAIL) -> str:
    t = (text or "").strip()
    return t if len(t) <= n else "..." + t[-n:]


# ------------------------------------------------------------- user-prompt

# Set by the skill's own headless callers (bands.py respond, run_trigger_evals.py) on the `claude -p`
# they start. Hooks inherit the claude process environment, so their prompts are not recorded as human
# presence. The variable can only withhold evidence, never create it, so trusting it is safe.
NONINTERACTIVE_ENV = "SDLC_NONINTERACTIVE"
# Turns that Claude Code injects itself also fire UserPromptSubmit (seen in real transcripts: a
# background Bash/Monitor/subagent finishing). They are not human presence; sdlc_lib's approve filter
# uses the same prefix for older logs.
HARNESS_PROMPT_PREFIX = L.HARNESS_PROMPT_PREFIX  # "<task-notification"


def _noninteractive() -> bool:
    return os.environ.get(NONINTERACTIVE_ENV, "").strip().lower() not in ("", "0", "false", "no")


def user_prompt(ctx: Ctx) -> int:
    prompt = ctx.payload.get("prompt")
    if _noninteractive():
        ctx.log("skip", "%s set: scripted prompt not recorded as a human event" % NONINTERACTIVE_ENV)
        hpath = L.state_dir(ctx.root) / "human-events.jsonl"
        if not hpath.exists():  # hooks are installed: keep approve from falling back to presence=unverified
            hpath.touch()
    elif isinstance(prompt, str) and prompt.lstrip().startswith(HARNESS_PROMPT_PREFIX):
        ctx.log("skip", "harness task-notification not recorded as a human event")
    elif isinstance(prompt, str) and prompt.strip():
        _human_event(ctx, "prompt", prompt)
    st = ctx.active()
    text = None
    if st:
        fix = ", fix mode ON (test files read-only)" if st["fix_mode"] else ""
        inh = ("; inherited from the main checkout, run `%s activate %s` here to pin it" % (ctx.cli(), st["id"])
               if ctx.inherited else "")
        text = ("ai-native-sdlc active change %s (%s): tier %s, stage %s%s%s. Next: %s"
                % (st["id"], st["title"], st["tier"], L.STAGE_LABEL.get(st["stage"], st["stage"]), fix, inh, st["next"]))
    elif ctx.inherit_missing:
        text = ("ai-native-sdlc: " + _worktree_missing_reason(ctx))
    if text:
        if len(text) > CONTEXT_MAX:
            text = text[:CONTEXT_MAX - 3] + "..."
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                 "additionalContext": text}}))
    return 0


def _worktree_missing_reason(ctx: Ctx) -> str:
    return ("worktree: the main checkout's active change %s has no %s/%s/ folder on this branch, so source edits "
            "are denied. Merge the artifact commit into this worktree branch, or run `%s activate <id>` here for "
            "the change this worktree works on." % (ctx.inherit_missing, ctx.changes_prefix(), ctx.inherit_missing,
                                                    ctx.cli()))


# ---------------------------------------------------------------- pre-tool

def _compile_all(patterns: Any, key: str) -> List[Tuple[str, "re.Pattern[str]"]]:
    if not isinstance(patterns, list):
        raise ValueError("production_gate.%s must be a list of regexes" % key)
    out = []
    for p in patterns:
        try:
            out.append((p, re.compile(p, re.I)))
        except (re.error, TypeError) as exc:
            raise ValueError("invalid regex %r in production_gate.%s: %s" % (p, key, exc))
    return out


def _run_approval(ctx: Ctx, gate: dict, subject: str) -> None:
    """Allow (return) only if the configured approval_check exits 0; otherwise raise Deny."""
    check = gate.get("approval_check") or ""
    if not isinstance(check, str) or not check.strip():
        raise Deny("production gate: %s. %s" % (subject, NO_APPROVAL_MSG))
    try:
        timeout = float(gate.get("approval_timeout_s") or 20)
    except (TypeError, ValueError):
        timeout = 20.0
    timeout = max(1.0, min(timeout, APPROVAL_TIMEOUT_CAP_S))
    env = dict(os.environ)
    env["SDLC_CHANGE_ID"] = ctx.change or (L.get_active(ctx.root) or "")
    env["SDLC_TOOL"] = ctx.tool
    ti = ctx.payload.get("tool_input") or {}
    env["SDLC_COMMAND"] = (str(ti.get("command") or "") if ctx.tool in SHELL_TOOLS
                           else json.dumps(ti, ensure_ascii=False)[:4000])
    proc = subprocess.Popen(check, shell=True, cwd=str(ctx.root), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                            start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        proc.communicate()
        raise Deny("production gate: %s. The approval check timed out after %gs, so the action is denied "
                   "(the gate fails closed). Check production_gate.approval_check in .claude/sdlc.json; %s"
                   % (subject, timeout, NO_APPROVAL_MSG))
    if proc.returncode != 0:
        detail = _tail((err or "") + ("\n" + out if out and not err else ""), 400) or "no output"
        raise Deny("production gate: %s. No release authorization found (approval check exited %d: %s). "
                   "Ask the release manager to approve the release in the pipeline; see %s."
                   % (subject, proc.returncode, detail, STAGE5))
    ctx.log("allow", "production gate: approval check passed for %s" % subject)


def _gated(tool: str) -> bool:
    return tool in SHELL_TOOLS or tool.startswith("mcp__")


def _shell_cmd(ctx: Ctx) -> Optional[str]:
    ti = ctx.payload.get("tool_input")
    cmd = ti.get("command") if isinstance(ti, dict) else None
    return cmd if isinstance(cmd, str) and cmd.strip() else None  # None e.g. for a Monitor ws watch


# Exact read-only commands still allowed while .claude/sdlc.json is broken, to diagnose it.
CFG_ERROR_READONLY = {"git status", "git diff", "git diff .claude/sdlc.json", "cat .claude/sdlc.json",
                      "python3 -m json.tool .claude/sdlc.json"}


# What production_gate.bash_patterns are matched against (Bash, Monitor) ------
# The command minus its *inert* simple commands: ones that only print, search or store text (echo,
# grep, git commit -m, gh pr create --body ...), so a commit message or a grep that says "deploy ...
# prod" is not a production action. Every other simple command is matched as typed, joined in order
# with its separators (so `export ENV=prod && ./deploy.sh` still matches), and $(...)/`...` bodies of a
# dropped command are matched too. The raw command is matched instead whenever text could flow from a
# dropped command into something that runs it: an interpreter, eval, xargs, ssh or wrapper anywhere,
# a dynamic command word ($X), a file write (> file, tee), ( ) grouping, or anything that doesn't parse.
# An inert command also stays when its output is piped into a non-inert one, a predicate (grep,
# diff) stays when a non-inert command follows, and a store (git commit, gh pr/issue create|comment|
# edit) stays when anything but an inert command or a plain `git push` follows.
_GATE_EXEC = re.compile(r"^(sh|bash|zsh|dash|ksh|mksh|fish|csh|tcsh|busybox|eval|source|\.|exec|xargs|parallel|"
                        r"env|sudo|doas|su|command|builtin|nohup|time|timeout|nice|ionice|stdbuf|watch|script|"
                        r"expect|at|batch|ssh|trap|alias|osascript|pwsh|powershell|python[\d.]*|pypy[\d.]*|perl|"
                        r"ruby|node|deno|bun|php|lua|tclsh|awk|gawk|mawk|nawk)$")
_GATE_DATA = {"echo", "printf", "cat", "head", "tail", "wc", "ls", "cut", "uniq", "true", "false", ":", "pwd"}
_GATE_PRED = {"grep", "egrep", "fgrep", "rg", "diff", "cmp"}
_GATE_GIT = {"log": "data", "show": "data", "status": "data", "blame": "data", "shortlog": "data", "add": "data",
             "diff": "pred", "grep": "pred", "commit": "store"}
_GATE_GH = {"create": "store", "comment": "store", "edit": "store", "view": "data", "list": "data",
            "diff": "data", "checks": "data", "status": "data"}
_GATE_BAD_OPT = re.compile(r"^(--output|--pre|--open-files-in-pager|-O|--exec|--receive-pack|--upload-pack)")
_GATE_REDIR = re.compile(r"^\d*(>>?|>\||&>>?|>&|<>)$")


def _subst(s: str, i: int) -> Tuple[List[dict], int]:
    """Parse the $(...), <(...), >(...) or `...` at s[i]; (body segments, index after it)."""
    if s[i] == "`":
        j = i + 1
        while j < len(s) and s[j] != "`":
            j += 2 if s[j] == "\\" else 1
        if j >= len(s):
            raise ValueError("unclosed backtick")
        return _shell_split(s[i + 1:j])[0], j + 1
    segs, j = _shell_split(s, i + 2, nested=True)
    if any(re.match(r"\s*case\s", x["skel"]) for x in segs):  # `pattern)` would end the body early
        raise ValueError("case inside $(...)")
    return segs, j + 1


def _shell_split(s: str, i: int = 0, nested: bool = False) -> Tuple[List[dict], int]:
    """Split shell text into simple commands on unquoted ; & && || | |& ( ) and newlines. Each segment
    has text (as typed, heredoc bodies included), sep (the separator after it), skel (substitutions as
    $S, heredoc bodies left out; for shlex), subs (parsed substitution bodies) and live (an unquoted
    heredoc body that expands $ or `). nested=True stops at the `)` closing a $(. ValueError when unsure."""
    segs: List[dict] = []
    n = len(s)
    st = {"start": i, "skel": [], "subs": [], "live": False}

    def close(end: int, sep: str, nxt: int) -> None:
        segs.append({"text": s[st["start"]:end], "sep": sep, "skel": "".join(st["skel"]), "subs": st["subs"],
                     "live": st["live"]})
        st.update(start=nxt, skel=[], subs=[], live=False)

    quote: Optional[str] = None
    heredocs: List[Tuple[str, bool, bool]] = []
    depth = 0
    while i < n:
        c, two = s[i], s[i:i + 2]
        if quote == "'":
            quote = None if c == "'" else quote
            st["skel"].append(c)
            i += 1
        elif c == "\\":
            st["skel"].append(" " if two == "\\\n" else two)
            i += 2
        elif c == "`" or two == "$(" or (quote is None and two in ("<(", ">(")):
            body, i = _subst(s, i)
            st["subs"].append(body)
            st["skel"].append("$S")
        elif quote == '"' or c in "'\"":
            quote = None if quote == c else (quote or c)
            st["skel"].append(c)
            i += 1
        elif two == "<<" and s[i:i + 3] != "<<<":
            m = re.match(r"<<(-?)\s*(['\"]?)([A-Za-z0-9_.-]+)\2", s[i:])
            if not m:
                raise ValueError("heredoc delimiter")
            heredocs.append((m.group(3), bool(m.group(1)), bool(m.group(2))))
            st["skel"].append(m.group(0))
            i += m.end()
        elif c == "\n" and heredocs:
            pos = i + 1
            for delim, tabs, quoted in heredocs:
                while pos < n:  # an unterminated heredoc runs to the end, as in bash
                    e = s.find("\n", pos)
                    e = n if e < 0 else e
                    line, pos = s[pos:e], e + 1
                    if (line.lstrip("\t") if tabs else line) == delim:
                        break
                    st["live"] = st["live"] or (not quoted and ("$" in line or "`" in line))
            heredocs = []
            close(min(pos, n), "\n", min(pos, n))
            i = pos
        elif two in ("&&", "||", "|&", ";;"):
            close(i, two, i + 2)
            i += 2
        elif c in ";\n" or (c == "|" and s[i - 1:i] != ">") or (c == "&" and s[i - 1:i] != ">" and two != "&>"):
            close(i, c, i + 1)
            i += 1
        elif c == ")" and nested and depth == 0:
            close(i, "", i)
            return segs, i
        elif c in "()":
            depth += 1 if c == "(" else -1
            close(i, c, i + 1)
            i += 1
        else:
            st["skel"].append(c)
            i += 1
    if quote or nested:
        raise ValueError("unclosed quote or $(")
    close(n, "", n)
    return segs, n


def _gate_words(seg: dict) -> Tuple[List[str], bool]:
    """(words without redirections, writes a file?) of one segment."""
    toks = _tokens(seg["skel"])
    words: List[str] = []
    writes = False
    i = 0
    while i < len(toks):
        t = toks[i]
        tgt = toks[i + 1] if i + 1 < len(toks) else ""
        if _GATE_REDIR.match(t) or t == "&>":
            writes = writes or not (tgt == "/dev/null" or (t.endswith("&") and re.match(r"^\d*-?$", tgt)))
            i += 2
        elif t.startswith("<"):
            i += 2
        else:
            words.append(t)
            i += 1
    return words, writes


def _gate_kind(words: List[str]) -> str:
    """'data', 'pred' or 'store' for an inert simple command, '' for one that may run something."""
    if not words:
        return "data"
    name = words[0]
    if _ASSIGN.match(name) or any(_GATE_BAD_OPT.match(w) for w in words[1:]):
        return ""
    if name in _GATE_DATA:
        return "" if name == "printf" and "-v" in words else "data"
    if name in _GATE_PRED:
        return "pred"
    if name == "git" and len(words) > 1:
        return _GATE_GIT.get(words[1], "")
    if name == "gh" and len(words) > 2 and words[1] in ("pr", "issue"):
        return _GATE_GH.get(words[2], "")
    return ""


def _gate_unsafe(segs: List[dict]) -> bool:
    for seg in segs:
        if seg["sep"] in ("(", ")") or any(_gate_unsafe(b) for b in seg["subs"]):
            return True
        words, writes = _gate_words(seg)
        cmdw = next((w for w in words if not _ASSIGN.match(w)), "")
        if writes or cmdw == "tee" or "$" in cmdw or "`" in cmdw or _GATE_EXEC.match(os.path.basename(cmdw)):
            return True
    return False


def _gate_sanitize(segs: List[dict]) -> str:
    kinds = ["" if seg["live"] else _gate_kind(_gate_words(seg)[0]) for seg in segs]
    plain_push = [_gate_words(seg)[0][:2] == ["git", "push"] and not any(
        _GATE_BAD_OPT.match(w) for w in _gate_words(seg)[0]) for seg in segs]
    parts = []
    for k, seg in enumerate(segs):
        kind, later = kinds[k], range(k + 1, len(segs))
        drop = bool(kind)
        if kind == "pred":
            drop = all(kinds[j] for j in later)
        elif kind == "store":
            drop = all(kinds[j] or plain_push[j] for j in later)
        j = k
        while drop and j + 1 < len(segs) and segs[j]["sep"] in ("|", "|&"):
            j += 1
            drop = bool(kinds[j])
        if drop:
            parts.append(" ; ".join(x for x in (_gate_sanitize(b) for b in seg["subs"]) if x.strip()) + seg["sep"])
        else:
            parts.append(seg["text"] + seg["sep"])
    return "".join(parts)


def _gate_text(cmd: str) -> str:
    """The text production_gate.bash_patterns are matched against; the raw command when unsure."""
    try:
        segs = _shell_split(cmd)[0]
        return cmd if _gate_unsafe(segs) else _gate_sanitize(segs)
    except Exception:  # noqa: BLE001 - fail closed: match everything as typed
        return cmd


def _production_gate(ctx: Ctx) -> None:
    """Raise Deny unless the call is not a production action or an approval exists. Fails closed."""
    try:
        if ctx.cfg_error:
            if ctx.tool == "Bash" and (_shell_cmd(ctx) or "").strip() in CFG_ERROR_READONLY:
                return
            raise Deny("production gate: .claude/sdlc.json does not parse (%s); the gate cannot see the repo's "
                       "production patterns, so every Bash/MCP call is denied until the config is fixed with "
                       "Edit/Write." % ctx.cfg_error)
        gate = ctx.cfg.get("production_gate")
        if not isinstance(gate, dict):
            raise ValueError("production_gate must be an object")
        ti = ctx.payload.get("tool_input") or {}
        subject = None
        if ctx.tool in SHELL_TOOLS:
            cmd = _shell_cmd(ctx)
            if cmd:
                text = cmd if ctx.tool == "PowerShell" else _gate_text(cmd)
                for pat, rx in _compile_all(gate.get("bash_patterns", []), "bash_patterns"):
                    if rx.search(text):
                        subject = "`%s` matches production_gate.bash_patterns %r" % (_tail(cmd, 200), pat)
                        break
        elif ctx.tool.startswith("mcp__"):
            for pat, rx in _compile_all(gate.get("mcp_tools", []), "mcp_tools"):
                if rx.fullmatch(ctx.tool):
                    subject = "MCP tool %s matches production_gate.mcp_tools %r" % (ctx.tool, pat)
                    break
        if subject is None:
            return
        ctx.active()
        _run_approval(ctx, gate, subject)
    except Deny:
        raise
    except Exception as exc:  # fail closed
        raise Deny("production gate error: %s: %s. The gate fails closed, so the call is denied; fix the "
                   "configuration (.claude/sdlc.json production_gate) or ask the release manager. See %s."
                   % (type(exc).__name__, exc, STAGE5))


def _protected_reason(ctx: Ctx, rel: str, is_dir: bool = False) -> Optional[str]:
    st_dir = L.STATE_DIRNAME
    cd = ctx.changes_prefix()
    if rel == st_dir or rel.startswith(st_dir + "/") or (is_dir and st_dir.startswith(rel + "/")):
        return ("`%s` is skill runtime state (.claude/sdlc-state/): audit/state files are written only by "
                "sdlc.py and its hooks. Use `%s status` to read state." % (rel, ctx.cli()))
    if L.glob_match(rel, cd + "/*/audit.md") or (is_dir and (rel == cd or L.glob_match(rel, cd + "/*"))):
        return ("`%s` holds an append-only audit log: audit/state files are written only by sdlc.py. "
                "Record events with `%s log <id> NOTE --message ...` (or present/approve for gates)."
                % (rel, ctx.cli()))
    prot = ctx.cfg.get("protected_paths") or []
    if L.match_any(rel, prot) or (is_dir and L.match_any(rel + "/x", prot)):
        return ("`%s` is a protected path (protected_paths in .claude/sdlc.json). Ask the human to make this "
                "change or to change the policy; the agent does not edit it." % rel)
    return None


def _self_protect_globs(ctx: Ctx) -> List[str]:
    return L.self_protected_paths(ctx.root, ctx.cfg)  # 'dir/' entries cover a whole directory


def _self_protect_reason(ctx: Ctx, rel: str, is_dir: bool = False) -> Optional[str]:
    globs = _self_protect_globs(ctx)
    hit = L.match_any(rel, globs) or (is_dir and (rel == "." or any(g.startswith(rel + "/") for g in globs)))
    return ("`%s` is guardrail configuration (self_protect). %s" % (rel, SELF_PROTECT_MSG)) if hit else None


def _hardlink_reason(ctx: Ctx, rel: str, is_dir: bool = False) -> Optional[str]:
    """Deny writes to a hard link of a guarded file (same inode under another name)."""
    try:
        st = os.stat(str(ctx.root / rel))
    except OSError:
        return None
    if is_dir or st.st_nlink < 2:
        return None
    sd = ctx.root / L.STATE_DIRNAME
    cands = list(sd.iterdir()) if sd.is_dir() else []
    cands += list((ctx.root / ctx.changes_prefix()).glob("*/audit.md"))
    cands += [ctx.root / g for g in _self_protect_globs(ctx) if "*" not in g and not g.endswith("/")]
    for c in cands:
        try:
            cs = os.stat(str(c))
        except OSError:
            continue
        if (cs.st_dev, cs.st_ino) == (st.st_dev, st.st_ino):
            return ("`%s` is a hard link to `%s`, which agents do not write; ask the human."
                    % (rel, c.relative_to(ctx.root).as_posix()))
    return None


def _fix_text(ctx: Ctx, st: dict, what: str) -> str:
    return ("fix task (change %s, fix mode on since commit %s): %s. Make the committed failing test pass "
            "without editing tests. If the test itself is wrong, ask the human; ending fix mode is "
            "`%s fix-mode %s off` and is recorded in the audit log."
            % (st["id"], (st.get("fix_commit") or "?")[:12], what, ctx.cli(), st["id"]))


def _fix_reason(ctx: Ctx, rel: str, is_dir: bool = False) -> Optional[str]:
    st = ctx.active()
    if not st or not st["fix_mode"]:
        return None
    globs = ctx.cfg.get("test_globs") or []
    if not (L.match_any(rel, globs) or (is_dir and L.match_any(rel + "/x", globs))):
        return None
    return _fix_text(ctx, st, "`%s` matches test_globs" % rel)


def _plan_guard_reason(ctx: Ctx, rel: str, is_dir: bool = False) -> Optional[str]:
    if not ctx.cfg.get("plan_guard", True):
        return None
    st = ctx.active()
    if ctx.under_changes(rel) or L.match_any(rel, ctx.cfg.get("plan_guard_exempt") or []):
        return None
    if ctx.inherit_missing:
        return _worktree_missing_reason(ctx)
    if not st or st["tier"] not in ("bounded", "standard", "high-risk"):
        return None
    missing = [g for g in st["required_gates"] if not st["gates"][g]["current"]]
    if not missing:
        return None
    return ("plan guard (change %s, tier %s): source edits wait until the %s gate(s) are approved and current "
            "(the approved artifact must not have changed since). Next: %s. Edits under %s/ are always allowed; "
            "plan_guard_exempt in .claude/sdlc.json lists other exempt paths."
            % (st["id"], st["tier"], ", ".join(missing), st["next"], ctx.changes_prefix()))


CHECKS = (_protected_reason, _self_protect_reason, _hardlink_reason, _fix_reason, _plan_guard_reason)


def _file_tool(ctx: Ctx) -> None:
    ti = ctx.payload.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path")
    rel = ctx.rel(path)
    if rel is None:
        return
    for check in CHECKS:
        reason = check(ctx, rel)
        if reason:
            raise Deny(reason)


# Shell write detection (Bash, Monitor, PowerShell) ---------------------------

_SEPARATORS = {"|", "||", "&&", ";", "&", "(", ")", ";;", "|&", "\n"}
_REDIRECTS = {">", ">>", ">|", "&>", "&>>", "<>"}
_WRITE_CMDS = {"tee", "mv", "rm", "truncate", "rmdir", "unlink", "shred", "touch"}
_WRAPPERS = {"sudo", "command", "env", "nohup", "time", "xargs", "exec", "builtin"}
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# PowerShell cmdlets that write; best-effort (aliases and the pipeline object model are not modelled).
_PS_WRITE = {"set-content", "add-content", "out-file", "clear-content", "new-item", "remove-item",
             "rename-item", "copy-item", "move-item"}
_PS_VALUE_PARAMS = {"-value", "-encoding", "-itemtype", "-inputobject", "-width", "-stream", "-filter",
                    "-include", "-exclude", "-name", "-credential"}
# Pseudo-target for commands that can rewrite committed test files without naming them; only
# denied in fix mode.
REVISION = "\0revision:"


def _tokens(cmd: str) -> List[str]:
    try:
        lex = shlex.shlex(cmd.replace("\n", " ; "), posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""
        return list(lex)
    except ValueError:
        out: List[str] = []
        for t in re.split(r"\s+", cmd):
            out.extend(x for x in re.split(r"(&&|\|\||[|;&()<>]+)", t) if x)
        return out


def _opt_value(args: List[str], short: str, long: str) -> Optional[str]:
    for i, a in enumerate(args):
        if a in (short, long):
            return args[i + 1] if i + 1 < len(args) else None
        if a.startswith(long + "="):
            return a[len(long) + 1:]
        if a.startswith(short) and not a.startswith("--") and len(a) > len(short):
            return a[len(short):]
    return None


def _ps_targets(name: str, args: List[str]) -> List[str]:
    copy_like = name in ("copy-item", "move-item")
    dest_keys = {"-destination"} if copy_like else {"-path", "-literalpath", "-filepath", "-newname"}
    named: List[str] = []
    pos: List[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-") and len(a) > 1:
            key, sep, val = a.partition(":")
            key = key.lower()
            takes = key in dest_keys or key in _PS_VALUE_PARAMS or (copy_like and key in ("-path", "-literalpath"))
            if takes and not sep:
                val = args[i + 1] if i + 1 < len(args) else ""
                i += 1
            if key in dest_keys and val:
                named.append(val)
        else:
            pos.append(a)
        i += 1
    if copy_like:
        return named or pos[1:2]
    if name == "rename-item":
        return named + pos[:2]
    return named or pos[:1]


def _patch_paths(operands: List[str], base: Path, own_targets: bool) -> List[str]:
    """Files a patch touches, from its ---/+++ headers; REVISION when a patch file can't be read."""
    out: List[str] = []
    for op in operands:
        p = Path(os.path.expanduser(op))
        p = p if p.is_absolute() else base / p
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            out.append(REVISION + "patch " + op)
            continue
        found = [ln[4:].split("\t")[0].strip() for ln in lines if ln.startswith(("+++ ", "--- "))]
        found = [f[2:] if f[:2] in ("a/", "b/") else f for f in found if f and f != "/dev/null"]
        out.extend(found if found else ([op] if own_targets else []))
    return out or [REVISION + "patch from stdin"]


def _write_targets(cmd: str, cwd: Path) -> List[Tuple[str, Path]]:
    """(token, base dir) pairs that a write-ish shell command may write to. Heuristic."""
    toks = _tokens(cmd)
    segs: List[List[str]] = [[]]
    targets: List[Tuple[str, Path]] = []
    pending_redirect = False
    redirect_tokens: List[Tuple[int, str]] = []
    for t in toks:
        if pending_redirect:
            pending_redirect = False
            if not re.match(r"^-?\d*-?$", t):  # '>&2', '>&-' are fd duplications
                redirect_tokens.append((len(segs) - 1, t))
            continue
        if t in _SEPARATORS:
            segs.append([])
            continue
        if t in _REDIRECTS or re.match(r"^\d*(>>?|>\||&>>?)$", t):
            pending_redirect = True
            continue
        if t in (">&", "&>") or re.match(r"^\d*>&$", t):
            pending_redirect = True
            continue
        if t.startswith("<"):
            continue
        segs[-1].append(t)
    base = cwd
    bases: List[Path] = []
    for seg in segs:
        bases.append(base)
        words = list(seg)
        while words and (_ASSIGN.match(words[0]) or words[0] in _WRAPPERS):
            words.pop(0)
            while words and words[0].startswith("-"):  # options of the wrapper (env -i, sudo -E)
                words.pop(0)
        if not words:
            continue
        name = os.path.basename(words[0]).lower()
        args = words[1:]
        operands = [a for a in args if a and not a.startswith("-")]
        if name == "cd":
            if operands:
                d = Path(os.path.expanduser(operands[0]))
                base = d if d.is_absolute() else base / d
                bases[-1] = base
            continue
        written: List[str] = []
        if name in ("cp", "install", "ln", "rsync"):
            tdir = _opt_value(args, "-t", "--target-directory") if name != "rsync" else None
            written = [tdir] if tdir else operands[-1:]  # the destination; sources are only read...
            hard = (name == "ln" and not any(a == "--symbolic" or re.match(r"^-[A-Za-z]*s", a) for a in args)) or \
                (name == "cp" and any(a == "--link" or re.match(r"^-[A-Za-z]*l", a) for a in args))
            if hard:  # ...unless hard-linked: then the source is writable through the new name
                written = operands
        elif name == "dd":
            written = [a[3:] for a in args if a.startswith("of=")]
        elif name in _WRITE_CMDS:
            written = operands
        elif name in _PS_WRITE:
            written = _ps_targets(name, args)
        elif name in ("sed", "gsed", "perl"):
            if any(a == "--in-place" or a.startswith("--in-place=") or re.match(r"^-[A-Za-z]*i", a) for a in args):
                written = operands
        elif name == "patch" and "--dry-run" not in args:
            written = _patch_paths(operands, bases[-1], own_targets=True)
        elif name == "git" and operands:
            verb, rest = operands[0], operands[1:]
            if verb in ("restore", "rm", "mv", "clean") or (verb == "checkout" and "--" in args):
                written = rest
            elif verb == "checkout" and len(rest) >= 2 and not any(
                    a in ("-b", "-B", "--orphan", "--detach") for a in args):
                written = rest[1:]  # git checkout <tree-ish> <paths...>
            elif verb == "apply" and not any(a in ("--check", "--stat", "--numstat", "--summary") for a in args):
                written = _patch_paths(rest, bases[-1], own_targets=False)
            elif verb == "stash" and rest[:1] in (["pop"], ["apply"]):
                written = [REVISION + "git stash " + rest[0]]
            elif verb == "reset" and "--hard" in args and rest and rest[0] not in ("HEAD", "@"):
                written = [REVISION + "git reset --hard " + rest[0]]
        targets.extend((a, bases[-1]) for a in written if a != "--")
    for idx, tok in redirect_tokens:
        targets.append((tok, bases[idx] if idx < len(bases) else base))
    return targets


def _expand(tok: str, base: Path) -> str:
    home = os.path.expanduser("~")
    for var, val in (("${PWD}", str(base)), ("$PWD", str(base)), ("${HOME}", home), ("$HOME", home)):
        if tok == var or tok.startswith(var + "/"):
            return val + tok[len(var):]
    return os.path.expanduser(tok)


def _bash_writes(ctx: Ctx, cmd: str) -> None:
    for tok, base in _write_targets(cmd, ctx.cwd):
        if tok.startswith(REVISION):
            st = ctx.active()
            if st and st["fix_mode"]:
                raise Deny("Shell command can rewrite committed test files: " + _fix_text(
                    ctx, st, "`%s` can restore or revert files matching test_globs" % tok[len(REVISION):]))
            continue
        p = Path(_expand(tok, base))
        if not p.is_absolute():
            p = base / p
        rel = ctx.rel(p)
        if rel is None:
            continue
        rel = rel.rstrip("/") or rel
        is_dir = (ctx.root / rel).is_dir() or tok.endswith("/")
        for check in CHECKS:
            reason = check(ctx, rel, is_dir)
            if reason:
                raise Deny("Bash command writes to a guarded path: " + reason)


# Running hooks.py by hand (e.g. `echo '{"prompt":"approve"}' | python3 .../hooks.py user-prompt`) would
# write human events and hook decisions that no human or Claude Code produced.
HOOKS_SELF_MSG = ("hooks.py is run by Claude Code, not by the agent; running it by hand would forge hook evidence.")
_HOOKS_FILE = Path(__file__).resolve()
_HOOK_EVENT = re.compile(r"^(%s)\b" % "|".join(("user-prompt", "pre-tool", "post-tool", "stop")))
_INTERP = re.compile(r"^(python[\d.]*|py|pypy[\d.]*|sh|bash|zsh|dash|ksh|uv|source|\.)$")


def _is_hooks_py(tok: str, base: Path) -> bool:
    t = _expand(tok.strip("\"'()"), base).replace("\\", "/")
    if t.endswith("scripts/hooks.py"):
        return True
    if os.path.basename(t) != "hooks.py":
        return False
    try:
        return (Path(t) if os.path.isabs(t) else base / t).resolve() == _HOOKS_FILE
    except (OSError, RuntimeError):
        return False


def _seg_runs_hooks(words: List[str], base: Path, depth: int) -> bool:
    for a, b in zip(words, words[1:]):  # `hooks.py <event>` anywhere
        if os.path.basename(a.strip("\"'")) == "hooks.py" and _HOOK_EVENT.match(b):
            return True
    while words and (_ASSIGN.match(words[0]) or words[0] in _WRAPPERS or words[0] in ("timeout", "nice")):
        words = words[1:]
        while words and (words[0].startswith("-") or re.match(r"^\d+(\.\d+)?[smhd]?$", words[0])):
            words = words[1:]  # wrapper options and the timeout duration
    if not words:
        return False
    if _is_hooks_py(words[0], base):
        return True
    name = os.path.basename(words[0]).lower()
    if not _INTERP.match(name):
        return False
    args, i, script = words[1:], 0, False
    while i < len(args):
        a = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if a.startswith("<"):  # python3 - < hooks.py
            if _is_hooks_py(nxt, base):
                return True
            i += 2
        elif script:
            i += 1
        elif a == "-c":
            if name.startswith("py") and ("hooks.py" in nxt or re.search(r"\b(import|from)\s+hooks\b", nxt)):
                return True
            if not name.startswith("py") and depth < 3 and _runs_hooks(nxt, base, depth + 1):
                return True
            script, i = True, i + 2
        elif a in ("-m", "-W", "-X"):
            script, i = a == "-m", i + 2
        elif (a.startswith("-") and a != "-") or (name == "uv" and a == "run"):
            i += 1
        elif _is_hooks_py(a, base):
            return True
        else:
            script, i = True, i + 1
    return False


def _runs_hooks(cmd: str, cwd: Path, depth: int = 0) -> bool:
    """True when a shell command runs this skill's hooks.py (any path ending in scripts/hooks.py)."""
    base: Path = cwd
    seg: List[str] = []
    for t in _tokens(cmd) + [";"]:
        if t not in _SEPARATORS:
            seg.append(t)
            continue
        if _seg_runs_hooks(seg, base, depth):
            return True
        words = [w for w in seg if not _ASSIGN.match(w)]
        if words[:1] == ["cd"] and len(words) > 1:
            d = Path(_expand(words[1], base))
            base = d if d.is_absolute() else base / d
        seg = []
    return False


def _gate_or_deny(ctx: Ctx, note: str = "") -> int:
    try:
        _production_gate(ctx)
    except Deny as d:
        ctx.log("deny", str(d) + note)
        sys.stderr.write(str(d) + "\n")
        return 2
    return 0


def pre_tool(ctx: Ctx) -> int:
    if _gated(ctx.tool) and _gate_or_deny(ctx):
        return 2
    if ctx.cfg_error:
        ctx.log("error", "config error; guards skipped (fail open): %s" % ctx.cfg_error)
        return 0
    try:
        if ctx.tool in FILE_TOOLS:
            _file_tool(ctx)
        elif ctx.tool in SHELL_TOOLS:
            cmd = _shell_cmd(ctx)
            if cmd and _runs_hooks(cmd.replace("\\", "/") if ctx.tool == "PowerShell" else cmd, ctx.cwd):
                raise Deny(HOOKS_SELF_MSG)
            if cmd:
                _bash_writes(ctx, cmd)
    except Deny as d:
        ctx.log("deny", str(d))
        sys.stderr.write(str(d) + "\n")
        return 2
    return 0


# --------------------------------------------------------------- post-tool

def _ask_answer(ctx: Ctx) -> None:
    # PostToolUse passes the tool's Output object (hooks docs); for AskUserQuestion real transcripts
    # show {questions, answers: {question: label}}. Only `answers` counts: an empty one (e.g. a question
    # that auto-closed with nothing picked) is no answer. tool_input.answers is set programmatically, and
    # an answer a PreToolUse hook supplies through updatedInput can't be told from a human's here.
    if _noninteractive():
        ctx.log("skip", "%s set: AskUserQuestion answer not recorded as a human event" % NONINTERACTIVE_ENV)
        return
    resp = ctx.payload.get("tool_response")
    answers: Any = resp.get("answers") if isinstance(resp, dict) else resp
    if isinstance(answers, str):
        answers = answers.strip()
    if not answers or not isinstance(answers, (dict, str)):
        ctx.log("skip", "AskUserQuestion without answers; nothing recorded")
        return
    text = answers if isinstance(answers, str) else json.dumps(answers, ensure_ascii=False, sort_keys=True)
    _human_event(ctx, "ask_answer", text)
    ctx.log("record", "AskUserQuestion answer recorded as a human event")


def _plan_accept(ctx: Ctx) -> Optional[str]:
    # Hooks doc: in PostToolUse read tool_response.plan; tool_input.plan is injected too.
    resp = ctx.payload.get("tool_response")
    ti = ctx.payload.get("tool_input") or {}
    plan = None
    if isinstance(resp, dict) and isinstance(resp.get("plan"), str) and resp["plan"].strip():
        plan = resp["plan"]
    elif isinstance(ti, dict) and isinstance(ti.get("plan"), str) and ti["plan"].strip():
        plan = ti["plan"]
    elif isinstance(resp, str) and resp.strip():
        plan = resp
    if not plan:
        ctx.log("error", "ExitPlanMode without plan text; nothing recorded")
        return None
    sd = L.state_dir(ctx.root)
    L.write_text_atomic(sd / "accepted-plan.md", plan.rstrip("\n") + "\n")
    _human_event(ctx, "plan_accept", plan)
    ctx.log("record", "accepted plan written to .claude/sdlc-state/accepted-plan.md")
    st = ctx.active()
    if st:
        return ("The accepted plan was saved to .claude/sdlc-state/accepted-plan.md. Record it as the change's "
                "plan.md with `%s plan-from-accepted %s --by \"<engineer who accepted it>\"` before editing "
                "source files." % (ctx.cli(), st["id"]))
    return None


def _load_reminded(path: Path) -> Dict[str, List[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _post_edit(ctx: Ctx) -> List[str]:
    ti = ctx.payload.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path")
    rel = ctx.rel(path)
    if rel is None:
        return []
    msgs: List[str] = []
    abs_path = str(ctx.root / rel)
    entries = ctx.cfg.get("post_edit") or []
    for ent in entries if isinstance(entries, list) else []:
        if not isinstance(ent, dict) or not ent.get("run") or not L.glob_match(rel, str(ent.get("glob") or "")):
            continue
        # The path goes in through the environment, never into the command text, so a file name with
        # $(...) or backticks can't run code however the template quotes {file}.
        tpl = str(ent["run"])
        run = tpl.replace('"{file}"', "{file}").replace("'{file}'", "{file}").replace("{file}", '"$SDLC_FILE"')
        cmd = tpl.replace("{file}", rel)  # for messages
        try:
            proc = subprocess.run(run, shell=True, cwd=str(ctx.root), env=dict(os.environ, SDLC_FILE=abs_path),
                                  stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                                  timeout=POST_EDIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            msgs.append("post_edit check `%s` timed out after %ds on %s." % (cmd, POST_EDIT_TIMEOUT_S, rel))
            continue
        if proc.returncode != 0:
            msgs.append("post_edit check `%s` failed (exit %d) on %s. Fix the file and keep it passing:\n%s"
                        % (cmd, proc.returncode, rel, _tail(proc.stdout) or "(no output)"))
    st = ctx.active()
    if st and st["artifacts"]["plan"] and not ctx.under_changes(rel) and not rel.startswith(L.STATE_DIRNAME + "/"):
        cdir = L.change_dir(ctx.root, ctx.cfg, st["id"])
        planned = L.plan_files((cdir / "plan.md").read_text(encoding="utf-8"))
        if not L.planned(rel, planned):
            rpath = L.state_dir(ctx.root) / "reminded.json"
            reminded = _load_reminded(rpath)
            done = reminded.get(st["id"]) if isinstance(reminded.get(st["id"]), list) else []
            if rel not in done:
                reminded[st["id"]] = done + [rel]
                L.write_text_atomic(rpath, json.dumps(reminded, indent=1) + "\n")
                msgs.append("`%s` is not listed in plan.md 'Files that change'. If this is a deviation, add a line "
                            "to plan.md '## Change log' (does not invalidate approval)" % rel)
    return msgs


def post_tool(ctx: Ctx) -> int:
    if ctx.cfg_error:
        ctx.log("error", "config error; post-tool hooks skipped (fail open): %s" % ctx.cfg_error)
        return 0
    if ctx.tool == "AskUserQuestion":
        _ask_answer(ctx)
        return 0
    if ctx.tool == "ExitPlanMode":
        note = _plan_accept(ctx)
        if note:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": note}}))
        return 0
    if ctx.tool in FILE_TOOLS:
        msgs = _post_edit(ctx)
        if msgs:
            text = "\n\n".join(msgs)
            ctx.log("block", text)
            sys.stderr.write(text + "\n")
            return 2
    return 0


# -------------------------------------------------------------------- stop

_LEAD_CD = re.compile(r"^\s*cd\s+(?:\"[^\"]*\"|'[^']*'|\S+)\s*(?:&&|;)\s*")
_LEAD_ENV = re.compile(r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|'[^']*'|\S*)\s+)+")


def _is_verify(cmd: str, verify: List[str]) -> bool:
    s = cmd.strip()
    prev = None
    while s != prev:
        prev = s
        s = _LEAD_CD.sub("", s)
        s = _LEAD_ENV.sub("", s)
        if s.startswith("(") and not s.startswith("(("):
            s = s[1:].lstrip()
    for seg in re.split(r"&&|\|\||;|\|", s):
        seg = _LEAD_ENV.sub("", seg.strip().strip("()").strip())
        for v in verify:
            v = v.strip()
            if v and (seg == v or seg.startswith(v + " ") or seg.startswith(v + "\t")):
                return True
    return False


def _scan_transcript(ctx: Ctx, path: str, verify: List[str]) -> Tuple[int, int, Optional[str]]:
    """(index of last source edit, index of last verify run, last edited rel); -1 when absent."""
    uses: List[Tuple[str, str, dict]] = []
    errored = set()
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"tool_use"' not in line and '"tool_result"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            msg = rec.get("message") if isinstance(rec, dict) else None
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    uses.append((str(b.get("id") or ""), str(b.get("name") or ""), inp))
                elif b.get("type") == "tool_result" and b.get("is_error"):
                    errored.add(str(b.get("tool_use_id") or ""))
    last_edit, last_verify, edited = -1, -1, None
    exempt = ctx.cfg.get("plan_guard_exempt") or []  # e.g. docs/** need no verification run either
    for i, (uid, name, inp) in enumerate(uses):
        if name in FILE_TOOLS:
            if uid in errored:
                continue  # denied or failed edits changed nothing
            rel = ctx.rel(inp.get("file_path") or inp.get("notebook_path"))
            if rel and not ctx.under_changes(rel) and not rel.startswith(L.STATE_DIRNAME + "/") \
                    and not L.match_any(rel, exempt):
                last_edit, edited = i, rel
        elif name in SHELL_TOOLS and isinstance(inp.get("command"), str) and _is_verify(inp["command"], verify):
            last_verify = i
    return last_edit, last_verify, edited


def stop(ctx: Ctx) -> int:
    if ctx.payload.get("stop_hook_active"):
        return 0
    if ctx.cfg_error or not ctx.cfg.get("verify_on_stop", True):
        return 0
    verify = [v for v in (ctx.cfg.get("verify_commands") or []) if isinstance(v, str) and v.strip()]
    if not verify:  # applies to every session, active change or not (trivial tier has none)
        return 0
    tpath = ctx.payload.get("transcript_path")
    if not isinstance(tpath, str) or not os.path.isfile(os.path.expanduser(tpath)):
        return 0
    try:
        last_edit, last_verify, edited = _scan_transcript(ctx, os.path.expanduser(tpath), verify)
    except (OSError, ValueError):
        return 0
    if last_edit > last_verify:
        cmds = ", ".join("`%s`" % v for v in verify)
        reason = ("Source files changed after the last verification run. Run %s and paste the output before "
                  "reporting done (references/stage-4-test.md)." % cmds)
        ctx.log("block", reason, file=edited)
        print(json.dumps({"decision": "block", "reason": reason}))
    return 0


# -------------------------------------------------------------------- main

HANDLERS = {"user-prompt": user_prompt, "pre-tool": pre_tool, "post-tool": post_tool, "stop": stop}


def main(argv: Optional[List[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in HANDLERS:
        sys.stderr.write("usage: hooks.py {%s} < hook-input.json\n" % "|".join(HANDLERS))
        return 1  # non-blocking error for Claude Code
    event = args[0]
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        if not isinstance(payload, dict):
            raise ValueError("hook input is not a JSON object")
    except ValueError as exc:
        sys.stderr.write("hooks.py %s: unreadable hook input (%s); allowing\n" % (event, exc))
        return 0
    ctx = None
    try:
        ctx = Ctx(event, payload)
        if not ctx.cfg.get("hooks_enabled", True):  # turns off the fail-open guards, never the production gate
            if event == "pre-tool" and _gated(ctx.tool):
                return _gate_or_deny(ctx, " (hooks disabled; production gate still enforced)")
            return 0
        return HANDLERS[event](ctx)
    except Exception as exc:  # every guard except the production gate fails open
        if ctx is not None:
            ctx.log("error", "%s: %s (failed open)" % (type(exc).__name__, exc))
        sys.stderr.write("hooks.py %s: internal error, allowing: %s: %s\n" % (event, type(exc).__name__, exc))
        return 0


if __name__ == "__main__":
    sys.exit(main())
