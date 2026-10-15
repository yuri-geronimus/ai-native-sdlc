#!/usr/bin/env python3
"""Regression-test the repo's agent configuration (CLAUDE.md, skills, hooks, subagents) on real tasks.

Playbook play: Stage 4 "Continuous evals in CI". Each case in <dir>/*.json runs real
`claude -p` in a fresh detached git worktree of HEAD (never --bare: the point is to load
CLAUDE.md, .claude/skills, hooks and subagents), then deterministic checks.

    run_evals.py [--dir evals] [--case ID ...] [--jobs 2] [--threshold 0.9]
                 [--compare prev.json] [--max-drop 0.05] [--out results.json]
                 [--claude-bin claude] [--model M] [--max-budget-usd USD] [--repo PATH]
                 [--dry-run] [--keep]

Per case:
  1. git worktree add --detach <tmp>/wt-<id> HEAD
  2. copy uncommitted edits to CLAUDE.md (any dir), .claude/** (except sdlc-state/, worktrees/),
     .mcp.json, REVIEW.md and <dir>/** into the worktree, so local config edits are tested
  3. run "setup" commands (shell, cwd = worktree); then snapshot the worktree as a detached
     commit so diff checks see only what the agent changed
  4. SDLC_NONINTERACTIVE=1 claude -p --output-format json --permission-mode <mode>
     --max-turns N --max-budget-usd USD [--model M] [--allowedTools <rule> ...]
     (prompt on stdin, so prompts starting with "-" and the variadic --allowedTools cannot
     collide; SDLC_NONINTERACTIVE=1 so the repo's hooks never record the prompt as a human
     turn; the budget is the case's max_budget_usd, default 2.0, or --max-budget-usd for all)
  5. checks: command | file_exists | result_regex | diff_excludes | diff_includes
     (diff checks see tracked, untracked and gitignored files the agent added, changed or
     deleted, except .claude/sdlc-state/ (written by the repo's hooks on every run) and heavy
     trees such as node_modules/ and .venv/ unless a diff glob names them)
  6. git worktree remove (unless --keep)

Ctrl-C, SIGTERM and SIGHUP (e.g. a cancelled CI job) stop every claude/setup/check process
group and remove the worktrees, then exit 128+signal. After SIGKILL, list leftovers with
`git worktree list` and remove them with `git worktree remove --force <path>`.

A case passes only if claude returned subtype "success" with is_error false AND every check
passed. Exit: 0 ok; 1 pass_rate < threshold, or pass_rate dropped more than --max-drop vs
--compare; 2 usage/environment error (bad case file, not a git repo, claude not found).

Network: shells out to `claude` (model API) and `git` only. Stdlib only; Python 3.9+.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.dont_write_bytecode = True  # keep scripts/__pycache__ out of the installed skill
sys.path.insert(0, str(Path(__file__).resolve().parent))
import sdlc_lib as L  # noqa: E402

CHECK_TYPES = ("command", "file_exists", "result_regex", "diff_excludes", "diff_includes")
PERMISSION_MODES = ("default", "acceptEdits", "plan", "dontAsk", "auto")
CASE_KEYS = ("id", "description", "prompt", "setup", "allowed_tools", "permission_mode",
             "max_turns", "timeout_s", "max_budget_usd", "model", "checks")
# max_budget_usd 2.0: a 30-turn fix-and-test case typically costs well under $1; the cap stops
# a looping agent. claude ends the run with subtype error_max_budget_usd, which fails the case.
DEFAULTS = {"setup": [], "allowed_tools": [], "permission_mode": "acceptEdits",
            "max_turns": 30, "timeout_s": 900, "max_budget_usd": 2.0, "model": None}
# The claude child always gets this, so the repo's hooks treat the run as headless.
CLAUDE_ENV = {"SDLC_NONINTERACTIVE": "1"}
CHECK_TIMEOUT_S = 600
DETAIL_MAX = 600
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

_git_lock = threading.Lock()   # git worktree add/remove mutate shared .git/worktrees
_print_lock = threading.Lock()
# Interrupt handling: every child is spawned under _procs_lock unless _stop is set, so the
# stop sweep in stop_all() cannot miss a process started concurrently.
_stop = threading.Event()
_procs: set = set()
_procs_lock = threading.Lock()
_signum = [signal.SIGINT]
_SIGNALS = tuple(getattr(signal, n) for n in ("SIGINT", "SIGTERM", "SIGHUP") if hasattr(signal, n))


class UsageError(Exception):
    """Exit 2: bad arguments, bad case files, missing tools."""


def log(msg: str) -> None:
    with _print_lock:
        sys.stderr.write(msg.rstrip("\n") + "\n")
        sys.stderr.flush()


def tail(text: Any, n: int = DETAIL_MAX) -> str:
    s = (text or "") if isinstance(text, str) else str(text or "")
    s = s.strip()
    return s if len(s) <= n else "..." + s[-n:]


# ------------------------------------------------------------------ case loading

def _as_list(v: Any) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _budget(v: Any, where: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v < float("inf"):
        raise UsageError("%s must be a positive number of US dollars, not %r" % (where, v))
    return float(v)


def normalize_case(raw: Any, source: str) -> dict:
    if not isinstance(raw, dict):
        raise UsageError("%s: a case must be a JSON object" % source)
    case = dict(DEFAULTS)
    case.update({k: v for k, v in raw.items() if v is not None or k == "model"})
    case["source"] = source
    cid = case.get("id") or Path(source).stem
    if not isinstance(cid, str) or not _ID_RE.match(cid):
        raise UsageError("%s: id %r must match [A-Za-z0-9][A-Za-z0-9._-]* (max 80 chars)" % (source, cid))
    case["id"] = cid
    if not isinstance(case.get("prompt"), str) or not case["prompt"].strip():
        raise UsageError("%s: case %s needs a non-empty string 'prompt'" % (source, cid))
    case["setup"] = [str(c) for c in _as_list(case.get("setup"))]
    case["allowed_tools"] = [str(t) for t in _as_list(case.get("allowed_tools"))]
    mode = case.get("permission_mode")
    if mode == "bypassPermissions":
        raise UsageError("%s: case %s: permission_mode bypassPermissions is refused; eval agents run "
                         "real tools on this machine. Use acceptEdits or dontAsk with explicit "
                         "allowed_tools." % (source, cid))
    if mode not in PERMISSION_MODES:
        raise UsageError("%s: case %s: permission_mode must be one of %s (set it explicitly; an unset "
                         "mode can start in auto)" % (source, cid, ", ".join(PERMISSION_MODES)))
    for key in ("max_turns", "timeout_s"):
        try:
            case[key] = int(case[key])
            if case[key] <= 0:
                raise ValueError
        except (TypeError, ValueError):
            raise UsageError("%s: case %s: %s must be a positive integer" % (source, cid, key))
    case["max_budget_usd"] = _budget(case["max_budget_usd"], "%s: case %s: max_budget_usd" % (source, cid))
    if case.get("model") is not None and not isinstance(case["model"], str):
        raise UsageError("%s: case %s: model must be a string or null" % (source, cid))
    checks = case.get("checks")
    if not isinstance(checks, list) or not checks:
        raise UsageError("%s: case %s needs a non-empty 'checks' list (types: %s)"
                         % (source, cid, ", ".join(CHECK_TYPES)))
    for i, chk in enumerate(checks):
        where = "%s: case %s: checks[%d]" % (source, cid, i)
        if not isinstance(chk, dict) or chk.get("type") not in CHECK_TYPES:
            raise UsageError("%s: type must be one of %s" % (where, ", ".join(CHECK_TYPES)))
        t = chk["type"]
        need = {"command": "run", "file_exists": "path", "result_regex": "pattern",
                "diff_excludes": "glob", "diff_includes": "glob"}[t]
        if not chk.get(need):
            raise UsageError("%s: %s check needs '%s'" % (where, t, need))
        if t == "result_regex":
            try:
                re.compile(chk["pattern"])
            except re.error as exc:
                raise UsageError("%s: invalid regex: %s" % (where, exc))
    return case


def load_cases(cases_dir: Path, only: Optional[List[str]] = None) -> List[dict]:
    if not cases_dir.is_dir():
        raise UsageError("eval directory %s not found (create it, or pass --dir); case format: "
                         "%s" % (cases_dir, L.SKILL_DIR / "assets" / "templates" / "eval-case.json"))
    cases: List[dict] = []
    for path in sorted(cases_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise UsageError("%s: invalid JSON: %s" % (path, exc))
        items = data if isinstance(data, list) else [data]
        if len(items) > 1:
            for raw in items:
                if isinstance(raw, dict) and not raw.get("id"):
                    raise UsageError("%s: every case in a list needs an 'id'" % path)
        for raw in items:
            cases.append(normalize_case(raw, str(path)))
    seen: Dict[str, str] = {}
    for c in cases:
        if c["id"] in seen:
            raise UsageError("duplicate case id %s in %s and %s" % (c["id"], seen[c["id"]], c["source"]))
        seen[c["id"]] = c["source"]
    if only:
        missing = [i for i in only if i not in seen]
        if missing:
            raise UsageError("unknown case id(s): %s (known: %s)" % (", ".join(missing), ", ".join(sorted(seen))))
        cases = [c for c in cases if c["id"] in only]
    if not cases:
        raise UsageError("no eval cases in %s/*.json; start from %s"
                         % (cases_dir, L.SKILL_DIR / "assets" / "templates" / "eval-case.json"))
    return cases


# ------------------------------------------------------------------- processes

def spawn(cmd: Any, **kw: Any) -> Optional[subprocess.Popen]:
    """Popen in its own session, registered for stop_all(); None once a stop was requested."""
    with _procs_lock:
        if _stop.is_set():
            return None
        proc = subprocess.Popen(cmd, start_new_session=True, **kw)
        _procs.add(proc)
        return proc


def release(proc: subprocess.Popen) -> None:
    with _procs_lock:
        _procs.discard(proc)


def _on_signal(signum: int, _frame: Any) -> None:
    _signum[0] = signum
    raise KeyboardInterrupt


def install_signal_handlers() -> Dict[int, Any]:
    """SIGINT/SIGTERM/SIGHUP raise KeyboardInterrupt in the main thread (signals already
    ignored, e.g. under nohup, stay ignored). Returns the old handlers."""
    old: Dict[int, Any] = {}
    for sig in _SIGNALS:
        try:
            if signal.getsignal(sig) is not signal.SIG_IGN:
                old[sig] = signal.signal(sig, _on_signal)
        except (ValueError, OSError):   # not the main thread
            pass
    return old


def restore_signal_handlers(old: Dict[int, Any]) -> None:
    for sig, handler in old.items():
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, TypeError):
            pass


def stop_all(grace: float = 2.0) -> int:
    """Refuse new children, SIGTERM then SIGKILL every live child group. Further signals are
    ignored so cleanup (worktree removal) can finish. -> exit code 128 + signal."""
    for sig in _SIGNALS:
        try:
            signal.signal(sig, signal.SIG_IGN)
        except (ValueError, OSError):
            pass
    with _procs_lock:
        _stop.set()
        live = list(_procs)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for proc in live:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, sig)
                except OSError:
                    pass
        deadline = time.time() + grace
        while sig == signal.SIGTERM and time.time() < deadline and any(p.poll() is None for p in live):
            time.sleep(0.1)
    return 128 + int(_signum[0])


def run_proc(cmd: Any, cwd: Any, timeout: float, shell: bool = False,
             input_text: Optional[str] = None,
             env: Optional[Dict[str, str]] = None) -> Tuple[Optional[int], str, str, bool]:
    """Run in its own process group; on timeout kill the whole group. -> (rc, out, err, timed_out).
    env: extra variables on top of os.environ."""
    try:
        proc = spawn(cmd, cwd=str(cwd), shell=shell, universal_newlines=True,
                     env=dict(os.environ, **env) if env else None,
                     stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        return None, "", "cannot start %s: %s" % (cmd if shell else cmd[0], exc), False
    if proc is None:
        return None, "", "cancelled", False
    try:
        out, err = proc.communicate(input=input_text, timeout=timeout)
        return proc.returncode, out or "", err or "", False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            proc.kill()
        out, err = proc.communicate()
        return None, out or "", err or "", True
    finally:
        release(proc)


def git_ok(args: List[str], cwd: Any) -> str:
    return L.git(args, cwd=cwd).stdout


# ------------------------------------------------------------------- worktrees

_CONFIG_EXCLUDE = (".claude/sdlc-state/", ".claude/worktrees/")


def is_config_path(rel: str, evals_rel: str) -> bool:
    if rel.startswith(_CONFIG_EXCLUDE):
        return False
    if rel.rsplit("/", 1)[-1] == "CLAUDE.md" or rel.startswith(".claude/"):
        return True
    if rel in (".mcp.json", "REVIEW.md"):
        return True
    return bool(evals_rel) and rel.startswith(evals_rel.rstrip("/") + "/")


def uncommitted_config(root: Path, evals_rel: str) -> List[Tuple[str, str]]:
    """[(action, rel)] with action 'copy' or 'delete' for uncommitted config files in root."""
    out = git_ok(["status", "--porcelain=v1", "-z", "--untracked-files=all"], root)
    parts = out.split("\0")
    result: List[Tuple[str, str]] = []
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        xy, rel = entry[:2], entry[3:]
        if xy[0] in "RC":
            old = parts[i] if i < len(parts) else ""
            i += 1
            if xy[0] == "R" and is_config_path(old, evals_rel):
                result.append(("delete", old))
        if not is_config_path(rel, evals_rel):
            continue
        if "D" in xy and not (root / rel).exists():
            result.append(("delete", rel))
        elif (root / rel).is_file():
            result.append(("copy", rel))
    return result


def evals_rel_path(root: Path, cases_dir: Path) -> str:
    try:
        return cases_dir.resolve().relative_to(root).as_posix()
    except ValueError:
        return ""


def warn_leftover_worktrees(root: Path) -> None:
    """Prune stale entries; list sdlc-eval-* worktrees still on disk. Never removes them: they
    may be --keep worktrees or belong to a concurrent run."""
    with _git_lock:
        L.git(["worktree", "prune"], cwd=root, check=False)
        out = L.git(["worktree", "list", "--porcelain"], cwd=root, check=False).stdout or ""
    left = [l.split(" ", 1)[1] for l in out.splitlines()
            if l.startswith("worktree ") and "/sdlc-eval-" in l.replace("\\", "/")]
    if left:
        log("note: %d earlier eval worktree(s) still exist (kept with --keep, another run, or a killed "
            "run); remove with: %s" % (len(left), "; ".join("git worktree remove --force %s" % shlex.quote(w)
                                                            for w in left)))


class Worktree:
    def __init__(self, root: Path, case_id: str):
        self.root = root
        self.tmp = Path(tempfile.mkdtemp(prefix="sdlc-eval-"))
        self.path = self.tmp / ("wt-" + case_id)

    def create(self) -> None:
        with _git_lock:
            git_ok(["worktree", "add", "--detach", "--quiet", str(self.path), "HEAD"], self.root)

    def remove(self) -> None:
        with _git_lock:
            L.git(["worktree", "remove", "--force", str(self.path)], cwd=self.root, check=False)
            shutil.rmtree(str(self.tmp), ignore_errors=True)
            L.git(["worktree", "prune"], cwd=self.root, check=False)


def apply_config_copies(root: Path, wt: Path, actions: List[Tuple[str, str]]) -> None:
    for action, rel in actions:
        dst = wt / rel
        if action == "delete":
            if dst.is_file() or dst.is_symlink():
                dst.unlink()
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(root / rel), str(dst))


def snapshot(wt: Path) -> str:
    """Commit everything present after setup on the detached HEAD; return its sha (diff base)."""
    git_ok(["add", "-A"], wt)
    git_ok(["-c", "user.name=run_evals", "-c", "user.email=run_evals@localhost", "-c",
            "commit.gpgsign=false", "commit", "-q", "--no-verify", "--allow-empty",
            "-m", "run_evals: baseline after setup"], wt)
    return git_ok(["rev-parse", "HEAD"], wt).strip()


# Ignored trees too big to stat on every case; walked only when a diff glob names them.
_HEAVY_DIRS = ("node_modules", ".venv", "venv", ".tox", "__pycache__", ".mypy_cache", ".pytest_cache", ".git")


def ignored_manifest(wt: Path, globs: List[str]) -> Dict[str, Tuple[int, int]]:
    """{rel: (size, mtime_ns)} for gitignored files, minus .claude/sdlc-state/ and heavy trees."""
    heavy = set(d for d in _HEAVY_DIRS if not any(d in g for g in globs))
    state = L.STATE_DIRNAME.rstrip("/") + "/"
    out = git_ok(["ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory"], wt)
    manifest: Dict[str, Tuple[int, int]] = {}

    def add(path: Path) -> None:
        rel = path.relative_to(wt).as_posix()
        if rel.startswith(state) or heavy & set(rel.split("/")[:-1]):
            return
        try:
            st = path.lstat()
        except OSError:
            return
        manifest[rel] = (st.st_size, st.st_mtime_ns)

    for entry in (e for e in out.split("\0") if e):
        path = wt / entry
        if entry.startswith(state) or heavy & set(entry.rstrip("/").split("/")):
            continue
        if not entry.endswith("/"):
            add(path)
            continue
        for dirpath, dirnames, filenames in os.walk(str(path)):
            dirnames[:] = [d for d in dirnames if d not in heavy]
            for name in filenames:
                add(Path(dirpath) / name)
    return manifest


def agent_changes(wt: Path, base: str, ignored_before: Optional[Dict[str, Tuple[int, int]]] = None,
                  globs: Optional[List[str]] = None) -> List[str]:
    files = set(f for f in git_ok(["diff", "--name-only", "-z", base], wt).split("\0") if f)
    files.update(f for f in git_ok(["ls-files", "--others", "--exclude-standard", "-z"], wt).split("\0") if f)
    if ignored_before is not None:
        after = ignored_manifest(wt, globs or [])
        files.update(k for k in set(after) | set(ignored_before) if after.get(k) != ignored_before.get(k))
    return sorted(files)


# ---------------------------------------------------------------------- claude

def claude_cmd(case: dict, claude_bin: str, model_override: Optional[str],
               budget_override: Optional[float] = None) -> List[str]:
    budget = budget_override if budget_override is not None else case["max_budget_usd"]
    cmd = [claude_bin, "-p", "--output-format", "json", "--permission-mode", case["permission_mode"],
           "--max-turns", str(case["max_turns"]), "--max-budget-usd", "%g" % budget]
    model = model_override or case.get("model")
    if model:
        cmd += ["--model", model]
    if case["allowed_tools"]:
        cmd += ["--allowedTools"] + case["allowed_tools"]   # variadic: keep last
    return cmd


def parse_result(stdout: str) -> Optional[dict]:
    """--output-format json prints one result object; tolerate a list of events or JSON lines."""
    text = (stdout or "").strip()
    if not text:
        return None
    candidates: List[Any] = []
    try:
        data = json.loads(text)
        candidates = data if isinstance(data, list) else [data]
    except ValueError:
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    candidates.append(json.loads(line))
                except ValueError:
                    pass
    for obj in reversed(candidates):
        if isinstance(obj, dict) and (obj.get("type") == "result" or "subtype" in obj):
            return obj
    return None


# ---------------------------------------------------------------------- checks

def _globs(chk: dict) -> List[str]:
    return [str(g) for g in _as_list(chk.get("glob"))]


def run_check(chk: dict, wt: Path, result_text: str, changed: Optional[List[str]]) -> dict:
    t = chk["type"]
    rec: Dict[str, Any] = {"type": t, "passed": False, "detail": ""}
    if t == "command":
        timeout = int(chk.get("timeout_s") or CHECK_TIMEOUT_S)
        rc, out, err, timed_out = run_proc(chk["run"], wt, timeout, shell=True)
        rec["passed"] = rc == 0
        status = "timed out after %ds" % timeout if timed_out else "exit %s" % rc
        rec["detail"] = "%s: %s\n%s" % (chk["run"], status, tail(out + ("\n" + err if err else "")))
    elif t == "file_exists":
        p = wt / chk["path"]
        rec["passed"] = p.exists()
        rec["detail"] = "%s %s" % (chk["path"], "exists" if rec["passed"] else "missing")
    elif t == "result_regex":
        flags = re.MULTILINE | (re.IGNORECASE if chk.get("ignore_case") else 0)
        m = re.search(chk["pattern"], result_text or "", flags)
        rec["passed"] = m is not None
        rec["detail"] = ("matched %r" % tail(m.group(0), 120)) if m else "no match for %r in result" % chk["pattern"]
    else:
        if changed is None:
            rec["detail"] = "diff unavailable"
            return rec
        hits = [f for f in changed if L.match_any(f, _globs(chk))]
        if t == "diff_excludes":
            rec["passed"] = not hits
            rec["detail"] = ("changed but excluded: %s" % ", ".join(hits[:20])) if hits else \
                "no changes match %s" % ", ".join(_globs(chk))
        else:
            rec["passed"] = bool(hits)
            rec["detail"] = ("changed: %s" % ", ".join(hits[:20])) if hits else \
                "no changes match %s (changed: %s)" % (", ".join(_globs(chk)), ", ".join(changed[:20]) or "none")
    return rec


# ------------------------------------------------------------------ one case

def run_case(case: dict, root: Path, config_actions: List[Tuple[str, str]], args: argparse.Namespace) -> dict:
    start = time.time()
    res: Dict[str, Any] = {"id": case["id"], "passed": False, "checks": [], "subtype": None,
                           "is_error": None, "cost_usd": None, "num_turns": None, "duration_s": 0.0}
    if _stop.is_set():
        res["subtype"] = "cancelled"
        return res
    wt = Worktree(root, case["id"])
    try:
        try:
            wt.create()
            apply_config_copies(root, wt.path, config_actions)
        except (L.SdlcError, OSError) as exc:
            res["subtype"] = "worktree_error"
            res["checks"].append({"type": "worktree", "passed": False, "detail": tail(str(exc))})
            return res
        for cmd in case["setup"]:
            rc, out, err, timed_out = run_proc(cmd, wt.path, case["timeout_s"], shell=True)
            if _stop.is_set():
                res["subtype"] = "cancelled"
                return res
            if rc != 0:
                res["subtype"] = "setup_failed"
                res["checks"].append({"type": "setup", "passed": False, "detail": "%s: %s\n%s" % (
                    cmd, "timed out" if timed_out else "exit %s" % rc, tail(out + "\n" + err))})
                return res
        diff_globs = [g for c in case["checks"] if c["type"].startswith("diff_") for g in _globs(c)]
        ignored_before: Optional[Dict[str, Tuple[int, int]]] = None
        try:
            base: Optional[str] = snapshot(wt.path)
            ignored_before = ignored_manifest(wt.path, diff_globs)
        except L.SdlcError as exc:
            base = None
            log("[%s] warning: cannot snapshot worktree (%s); diff checks unavailable" % (case["id"], exc))

        cmd = claude_cmd(case, args.claude_bin, args.model, args.max_budget_usd)
        log("[%s] running claude (max %d turns, timeout %ds)" % (case["id"], case["max_turns"], case["timeout_s"]))
        rc, out, err, timed_out = run_proc(cmd, wt.path, case["timeout_s"], input_text=case["prompt"],
                                           env=CLAUDE_ENV)
        if _stop.is_set():
            res["subtype"] = "cancelled"
            return res
        parsed = parse_result(out)
        result_text = ""
        if timed_out:
            res["subtype"] = "timeout"
            ok = False
            detail = "claude timed out after %ds" % case["timeout_s"]
        elif parsed is None:
            res["subtype"] = "no_result"
            ok = False
            detail = "no result JSON from claude (exit %s): %s" % (rc, tail(err or out, 300))
        else:
            res["subtype"] = parsed.get("subtype")
            res["is_error"] = bool(parsed.get("is_error"))
            res["cost_usd"] = parsed.get("total_cost_usd")
            res["num_turns"] = parsed.get("num_turns")
            result_text = parsed.get("result") if isinstance(parsed.get("result"), str) else ""
            ok = res["subtype"] == "success" and not res["is_error"]
            detail = "subtype=%s is_error=%s turns=%s" % (res["subtype"], res["is_error"], res["num_turns"])
            if not ok and parsed.get("errors"):
                detail += " errors=%s" % tail(json.dumps(parsed.get("errors")), 200)
        res["checks"].append({"type": "claude", "passed": ok, "detail": detail})
        res["result_excerpt"] = tail(result_text, 400)

        changed: Optional[List[str]] = None
        if base is not None:
            try:
                changed = agent_changes(wt.path, base, ignored_before, diff_globs)
            except L.SdlcError as exc:
                log("[%s] warning: %s" % (case["id"], exc))
        res["changed_files"] = (changed or [])[:200]
        for chk in case["checks"]:
            if _stop.is_set():
                res["subtype"] = "cancelled"
                return res
            res["checks"].append(run_check(chk, wt.path, result_text, changed))
        res["passed"] = all(c["passed"] for c in res["checks"])
        return res
    finally:
        res["duration_s"] = round(time.time() - start, 1)
        if args.keep:
            res["worktree"] = str(wt.path)
            log("[%s] kept worktree %s (remove: git worktree remove --force %s)"
                % (case["id"], wt.path, shlex.quote(str(wt.path))))
        else:
            wt.remove()
        failed = [c["type"] for c in res["checks"] if not c["passed"]]
        log("[%s] %s in %.0fs%s" % (case["id"], "PASS" if res["passed"] else "FAIL", res["duration_s"],
                                    (" (failed: %s)" % ", ".join(failed)) if failed else ""))


# ------------------------------------------------------------------ dry run

def dry_run(cases: List[dict], root: Path, actions: List[Tuple[str, str]], args: argparse.Namespace) -> None:
    q = shlex.quote
    print("# run_evals dry run: %d case(s) in %s; nothing is executed" % (len(cases), root))
    if actions:
        print("# uncommitted config copied into each worktree: %s"
              % ", ".join("%s %s" % (a, r) for a, r in actions))
    for case in cases:
        wt = "<tmp>/wt-%s" % case["id"]
        print("\n## case %s (%s)" % (case["id"], case["source"]))
        print("git -C %s worktree add --detach %s HEAD" % (q(str(root)), wt))
        for cmd in case["setup"]:
            print("(cd %s && %s)" % (wt, cmd))
        print("git -C %s add -A && git -C %s commit -q --no-verify --allow-empty -m 'run_evals: baseline after setup'" % (wt, wt))
        cmd = claude_cmd(case, args.claude_bin, args.model, args.max_budget_usd)
        envs = " ".join("%s=%s" % kv for kv in sorted(CLAUDE_ENV.items()))
        print("(cd %s && %s %s <<'PROMPT'\n%s\nPROMPT\n)" % (wt, envs, " ".join(q(c) for c in cmd), case["prompt"]))
        for chk in case["checks"]:
            t = chk["type"]
            if t == "command":
                print("(cd %s && %s)   # check: exit 0" % (wt, chk["run"]))
            elif t == "file_exists":
                print("test -e %s/%s   # check: file_exists" % (wt, chk["path"]))
            elif t == "result_regex":
                print("# check: result matches /%s/" % chk["pattern"])
            else:
                print("# check: %s %s (git diff --name-only <baseline> + untracked + ignored)" % (t, ", ".join(_globs(chk))))
        if not args.keep:
            print("git -C %s worktree remove --force %s" % (q(str(root)), wt))


# ------------------------------------------------------------------ summary

def summarize(results: List[dict], threshold: float) -> dict:
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    costs = [r["cost_usd"] for r in results if isinstance(r.get("cost_usd"), (int, float))]
    return {"pass_rate": round(passed / total, 4) if total else 0.0, "passed": passed, "total": total,
            "cost_usd": round(sum(costs), 4), "threshold": threshold, "generated_at": L.now_iso(),
            "cases": results}


def compare(summary: dict, prev_path: str, max_drop: float) -> dict:
    try:
        prev = json.loads(Path(prev_path).read_text(encoding="utf-8"))
        prev_rate = float(prev["pass_rate"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise UsageError("--compare %s: cannot read a results file with pass_rate: %s" % (prev_path, exc))
    prev_cases = {c.get("id"): bool(c.get("passed")) for c in prev.get("cases", []) if isinstance(c, dict)}
    cur_cases = {c["id"]: c["passed"] for c in summary["cases"]}
    drop = round(prev_rate - summary["pass_rate"], 4)
    return {"baseline": prev_path, "baseline_pass_rate": prev_rate, "drop": drop, "max_drop": max_drop,
            "regressed": sorted(i for i, ok in cur_cases.items() if prev_cases.get(i) is True and not ok),
            "fixed": sorted(i for i, ok in cur_cases.items() if prev_cases.get(i) is False and ok),
            "new": sorted(i for i in cur_cases if i not in prev_cases),
            "removed": sorted(i for i in prev_cases if i not in cur_cases),
            "failed": drop > max_drop + 1e-9}


# ---------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_evals.py",
        description="Run the repo's agent-config eval cases (<dir>/*.json) with real `claude -p` in "
                    "throwaway git worktrees and gate on the pass rate. Every claude run gets "
                    "SDLC_NONINTERACTIVE=1 and --max-budget-usd (case key max_budget_usd, default $%.2f)."
                    % DEFAULTS["max_budget_usd"])
    p.add_argument("--dir", default=None, help="case directory relative to the repo root "
                   "(default: evals_dir from .claude/sdlc.json, i.e. evals)")
    p.add_argument("--case", nargs="+", action="extend", default=[], metavar="ID", help="run only these case ids")
    p.add_argument("--jobs", type=int, default=2, help="cases run in parallel (default 2)")
    p.add_argument("--threshold", type=float, default=0.9, help="minimum pass rate, 0-1 (default 0.9)")
    p.add_argument("--compare", metavar="PREV_JSON", help="earlier results.json to compare against")
    p.add_argument("--max-drop", type=float, default=0.05, help="allowed pass-rate drop vs --compare (default 0.05)")
    p.add_argument("--out", metavar="FILE", help="also write the results JSON here")
    p.add_argument("--claude-bin", default="claude", help="claude executable (default: claude on PATH)")
    p.add_argument("--model", default=None, help="override every case's model")
    p.add_argument("--max-budget-usd", type=float, default=None, metavar="USD",
                   help="override every case's max_budget_usd (per claude run; default: the case's "
                        "value, else $%.2f). A run that hits it ends error_max_budget_usd and fails"
                        % DEFAULTS["max_budget_usd"])
    p.add_argument("--repo", default=None, help="repository (default: git root of the current directory)")
    p.add_argument("--dry-run", action="store_true", help="validate cases and print the commands; run nothing")
    p.add_argument("--keep", action="store_true", help="keep each worktree for inspection")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not 0.0 <= args.threshold <= 1.0 or args.max_drop < 0 or args.jobs < 1:
            raise UsageError("--threshold must be 0-1, --max-drop >= 0, --jobs >= 1")
        if args.max_budget_usd is not None:
            _budget(args.max_budget_usd, "--max-budget-usd")
        start = Path(args.repo) if args.repo else Path.cwd()
        if not L.in_git_repo(start):
            raise UsageError("%s is not inside a git repository; evals run in git worktrees" % start)
        root = L.repo_root(start)
        try:
            cfg = L.load_config(root)
        except ValueError as exc:
            raise UsageError(str(exc))
        cases_dir = Path(args.dir or cfg.get("evals_dir") or "evals")
        if not cases_dir.is_absolute():
            cases_dir = root / cases_dir
        cases = load_cases(cases_dir, args.case)
        try:
            actions = uncommitted_config(root, evals_rel_path(root, cases_dir))
        except L.SdlcError as exc:
            raise UsageError(str(exc))
        if args.dry_run:
            dry_run(cases, root, actions, args)
            return 0
        if not (shutil.which(args.claude_bin) or Path(args.claude_bin).is_file()):
            raise UsageError("claude executable %r not found; install Claude Code (>= 2.1.269) or pass "
                             "--claude-bin" % args.claude_bin)
        L.git(["rev-parse", "--verify", "HEAD"], cwd=root)
        if args.compare and not Path(args.compare).is_file():
            raise UsageError("--compare %s: file not found" % args.compare)
    except (UsageError, L.SdlcError) as exc:
        sys.stderr.write("run_evals: error: %s\n" % exc)
        return 2

    if actions:
        log("copying uncommitted config into each worktree: %s" % ", ".join(r for _, r in actions))
    warn_leftover_worktrees(root)
    log("running %d case(s), %d in parallel" % (len(cases), args.jobs))
    old_handlers = install_signal_handlers()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs)
    futures: List[concurrent.futures.Future] = []
    try:
        futures = [pool.submit(run_case, c, root, actions, args) for c in cases]
        results = []
        for case, fut in zip(cases, futures):
            try:
                results.append(fut.result())
            except Exception as exc:  # a runner bug must not hide the other cases' results
                results.append({"id": case["id"], "passed": False, "subtype": "runner_error", "is_error": None,
                                "cost_usd": None, "num_turns": None, "duration_s": 0.0,
                                "checks": [{"type": "runner", "passed": False, "detail": tail(repr(exc))}]})
    except KeyboardInterrupt:
        code = stop_all()
        log("run_evals: interrupted; stopped running processes, removing worktrees")
        pool.shutdown(wait=True, cancel_futures=True)
        done = sum(1 for f in futures if f.done() and not f.cancelled())
        log("run_evals: interrupted after %d of %d case(s) started; no results written" % (done, len(cases)))
        return code
    finally:
        pool.shutdown(wait=True)
        restore_signal_handlers(old_handlers)

    summary = summarize(results, args.threshold)
    failed = summary["pass_rate"] < args.threshold - 1e-9
    if args.compare:
        try:
            summary["compare"] = compare(summary, args.compare, args.max_drop)
        except UsageError as exc:
            sys.stderr.write("run_evals: error: %s\n" % exc)
            return 2
        failed = failed or summary["compare"]["failed"]
    text = json.dumps(summary, indent=2, sort_keys=False)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    msg = "pass rate %.1f%% (%d/%d), threshold %.1f%%, cost $%.2f" % (
        100 * summary["pass_rate"], summary["passed"], summary["total"], 100 * args.threshold, summary["cost_usd"])
    if "compare" in summary:
        cmp_ = summary["compare"]
        msg += "; baseline %.1f%% (drop %.1f pts, max %.1f)%s" % (
            100 * cmp_["baseline_pass_rate"], 100 * cmp_["drop"], 100 * args.max_drop,
            ("; regressed: " + ", ".join(cmp_["regressed"])) if cmp_["regressed"] else "")
    log(("FAIL: " if failed else "OK: ") + msg)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
