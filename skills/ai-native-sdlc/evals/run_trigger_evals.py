#!/usr/bin/env python3
"""Measure whether Claude Code invokes the ai-native-sdlc skill for each query in trigger-evals.json.

For every query and run this executes the real CLI (model API calls; costs money):

    claude -p <query> --output-format stream-json --verbose --max-turns 2 --permission-mode plan
           --no-session-persistence --settings '{"plansDirectory": ".claude/trigger-eval-plans"}'
           [--strict-mcp-config --setting-sources project,local] [--model M] [--max-budget-usd X]

and scans the stream for an assistant `tool_use` block named "Skill" whose input.skill (or
input.name / input.command) equals the skill name or ends with ":<name>" (plugin namespacing).
A `Read` of .../<name>/SKILL.md also counts (recorded as via=read). Only the main agent counts:
a hit from a subagent (event with parent_tool_use_id) is reported as subagent_via, not as a
trigger. The process is killed as soon as the skill fires.

Cost: triggered runs are killed before the result event, so they incur cost that is not
reported in cost_usd. Each run also records token counts from the assistant events (input and
cache tokens once per message id; output tokens are approximate, the stream only carries the
streaming-start value), and the summary counts killed_runs_without_cost. Those token counts
leave out background (e.g. haiku) calls, so every cost figure is a lower bound. With --prices
FILE ({"<model-id substring>": {"input": $/MTok, "output": .., "cache_read": .., "cache_write": ..}})
killed runs get est_cost_usd; --max-total-usd stops starting new runs once known plus estimated
cost passes the limit (cancelled runs are unscored).

Stream shape verified on Claude Code 2.1.294 (2026-10-08):
    {"type":"assistant","message":{"content":[{"type":"tool_use","name":"Skill",
     "input":{"skill":"ai-native-sdlc"}, ...}]}}
followed by a user tool_result "Launching skill: ai-native-sdlc". Hitting --max-turns ends with
{"type":"result","subtype":"error_max_turns","is_error":true} and exit 1; that is a normal run.

Isolation (default): runs in a detached git worktree of --repo at HEAD with uncommitted
CLAUDE.md / .claude/** / .mcp.json copied in, so project hooks (e.g. the human-event logger) do
not record eval queries in the real repo's .claude/sdlc-state/. --no-isolate runs in --repo.
Plan mode blocks source edits, but Claude still writes a plan file (redirected into the worktree
via plansDirectory; with --no-isolate the runner deletes the directory if it created it). Where
auto mode is available, classifier-approved shell commands can also run. Isolation is what
protects the real repo.

Environment: by default the user's ~/.claude skills, synced claude.ai skills, plugins and
claude.ai connectors load, as in real use, so competing skills are part of the measurement;
connectors can inject an authorization notice after the first tool call, so default results
depend on the machine. --clean (what CI should use for reproducible numbers) adds
--strict-mcp-config and --setting-sources project,local: no MCP servers or connectors, no user or
claude.ai skills; the stored login is kept. Bundled built-in skills still load and compete in
both modes. The output records the mode, the requested model and the models the sessions
reported (init event).

--trace-dir DIR writes DIR/q<NNN>-r<R>.txt per run: init model and permission mode, one line per
assistant text or tool_use block (subagent blocks marked [sub]), and how the run ended.

Output: JSON on stdout (and --out); a table on stderr. Exit 0, or 1 with --fail-under when
accuracy is below it, 2 on usage errors, 128+signal when interrupted (claude processes are
stopped and the worktree removed). Stdlib only; Python 3.9+.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
SKILL_DIR = HERE.parent
sys.dont_write_bytecode = True  # keep scripts/__pycache__ out of the installed skill
sys.path.insert(0, str(SKILL_DIR / "scripts"))
import sdlc_lib as L  # noqa: E402
import run_evals as RE  # noqa: E402  (worktree, process and signal helpers)

# Plan files go here (relative to the project root) instead of ~/.claude/plans.
PLANS_DIR = ".claude/trigger-eval-plans"
TOKEN_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
PRICE_KEYS = {"input_tokens": "input", "cache_read_input_tokens": "cache_read",
              "cache_creation_input_tokens": "cache_write", "output_tokens": "output"}
TRACE_CUT = 200

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        sys.stderr.write(msg.rstrip("\n") + "\n")
        sys.stderr.flush()


def load_queries(path: Path) -> List[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RE.UsageError("%s: %s" % (path, exc))
    if isinstance(data, dict):
        data = data.get("queries") or data.get("evals") or []
    out = []
    for i, item in enumerate(data if isinstance(data, list) else []):
        if not isinstance(item, dict) or not isinstance(item.get("query"), str) \
                or not isinstance(item.get("should_trigger"), bool):
            raise RE.UsageError("%s: item %d needs {\"query\": str, \"should_trigger\": bool}" % (path, i))
        out.append({"query": item["query"], "should_trigger": item["should_trigger"]})
    if not out:
        raise RE.UsageError("%s: no queries" % path)
    return out


def load_prices(path: Optional[str]) -> Optional[Dict[str, Dict[str, float]]]:
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        prices = {str(k): {pk: float(v.get(pk, 0)) for pk in PRICE_KEYS.values()} for k, v in data.items()}
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        raise RE.UsageError("--prices %s: need {\"<model substring>\": {\"input\": $/MTok, ...}}: %s" % (path, exc))
    return prices


def build_cmd(query: str, args: argparse.Namespace) -> List[str]:
    cmd = [args.claude_bin, "-p"]
    if not query.startswith("-"):
        cmd.append(query)          # otherwise sent on stdin so it is not parsed as a flag
    cmd += ["--output-format", "stream-json", "--verbose", "--max-turns", str(args.max_turns),
            "--permission-mode", "plan", "--no-session-persistence",
            "--settings", json.dumps({"plansDirectory": PLANS_DIR})]
    if args.clean:
        cmd += ["--strict-mcp-config", "--setting-sources", "project,local"]
    if args.model:
        cmd += ["--model", args.model]
    if args.max_budget_usd:
        cmd += ["--max-budget-usd", str(args.max_budget_usd)]
    return cmd


def skill_name_of(block: dict) -> Optional[str]:
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    for key in ("skill", "name", "command"):
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().lstrip("/").split()[0]
    return None


def matches(name: Optional[str], skill: str) -> bool:
    return bool(name) and (name == skill or name.endswith(":" + skill))


def scan_event(event: Any, skill: str) -> Dict[str, Any]:
    """-> {"fired", "via", "subagent_hit", "other_skills"} for one stream-json event. A hit from a
    subagent (parent_tool_use_id set) is "subagent-skill"/"subagent-read", never fired."""
    found: Dict[str, Any] = {"fired": False, "via": None, "subagent_hit": None, "other_skills": []}
    if not isinstance(event, dict) or event.get("type") != "assistant":
        return found
    sub = bool(event.get("parent_tool_use_id"))
    content = (event.get("message") or {}).get("content")
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name, via = block.get("name"), None
        if name == "Skill":
            s = skill_name_of(block)
            if matches(s, skill):
                via = "skill"
            elif s:
                found["other_skills"].append(s)
        elif name == "Read":
            fp = str((block.get("input") or {}).get("file_path") or "").replace("\\", "/")
            if fp.endswith("/%s/SKILL.md" % skill):
                via = "read"
        if via and sub:
            found["subagent_hit"] = "subagent-" + via
        elif via:
            found.update(fired=True, via=via)
    return found


def trace_lines(ev: dict) -> List[str]:
    out = []
    tag = "[sub] " if ev.get("parent_tool_use_id") else ""
    content = (ev.get("message") or {}).get("content")
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            out.append("%sTEXT %s" % (tag, str(block.get("text"))[:TRACE_CUT].replace("\n", " ")))
        elif block.get("type") == "tool_use":
            out.append("%sTOOL %s %s" % (tag, block.get("name"), json.dumps(block.get("input"))[:TRACE_CUT]))
    return out


def est_cost(by_model: Dict[str, Dict[str, int]], prices: Optional[dict]) -> Optional[float]:
    if not prices:
        return None
    total = 0.0
    for model, usage in by_model.items():
        rate = next((v for k, v in prices.items() if k in model), None)
        if rate:
            total += sum(usage.get(t, 0) * rate[pk] for t, pk in PRICE_KEYS.items()) / 1e6
    return round(total, 6)


def run_once(query: str, cwd: Path, args: argparse.Namespace, qi: int = 0, r: int = 0) -> Dict[str, Any]:
    cmd = build_cmd(query, args)
    rec: Dict[str, Any] = {"triggered": False, "via": None, "subagent_via": [], "other_skills": [],
                           "cost_usd": None, "est_cost_usd": None, "tokens": None, "error": None,
                           "subtype": None, "model": None, "permission_mode": None, "first_tool": None}
    start = time.time()
    # SDLC_NONINTERACTIVE keeps the repo's prompt hook from logging eval queries as human
    # presence (matters with --no-isolate).
    env = dict(os.environ, SDLC_NONINTERACTIVE="1")
    if args.clean:
        env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"
    try:
        proc = RE.spawn(cmd, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, universal_newlines=True, env=env)
    except OSError as exc:
        rec["error"] = "cannot start %s: %s" % (cmd[0], exc)
        return rec
    if proc is None:
        rec["error"] = "cancelled"
        return rec

    def kill() -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass

    timed_out = threading.Event()

    def on_timeout() -> None:
        timed_out.set()
        kill()

    timer = threading.Timer(args.timeout, on_timeout)
    timer.start()
    err_chunks: List[str] = []
    drain = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)
    drain.start()
    saw_assistant = False
    usage_by_id: Dict[str, dict] = {}   # one entry per message id: stream events repeat usage per block
    result_usage: Optional[dict] = None
    trace: List[str] = []
    try:
        try:
            if query.startswith("-"):
                proc.stdin.write(query)
            proc.stdin.close()
        except OSError:
            pass
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == "system" and ev.get("subtype") == "init":
                rec["model"], rec["permission_mode"] = ev.get("model"), ev.get("permissionMode")
                trace.append("INIT model=%s permissionMode=%s" % (rec["model"], rec["permission_mode"]))
                if isinstance(ev.get("skills"), list) and \
                        not any(matches(str(n), args.skill_name) for n in ev["skills"]):
                    rec["error"] = ("skill %s is not listed in this session's skills (init event); check --repo "
                                    "and where the skill is installed" % args.skill_name)
                    break
            if ev.get("type") == "assistant":
                saw_assistant = True
                msg = ev.get("message") or {}
                if isinstance(msg.get("usage"), dict):
                    usage_by_id[str(msg.get("id") or len(usage_by_id))] = {"model": msg.get("model"),
                                                                           "usage": msg["usage"]}
                if rec["first_tool"] is None and not ev.get("parent_tool_use_id"):
                    rec["first_tool"] = next((b.get("name") for b in msg.get("content") or []
                                              if isinstance(b, dict) and b.get("type") == "tool_use"), None)
                trace += trace_lines(ev)
            if ev.get("type") == "result":
                rec["cost_usd"] = ev.get("total_cost_usd")
                rec["subtype"] = ev.get("subtype")
                result_usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else None
            found = scan_event(ev, args.skill_name)
            rec["other_skills"] += found["other_skills"]
            if found["subagent_hit"] and found["subagent_hit"] not in rec["subagent_via"]:
                rec["subagent_via"].append(found["subagent_hit"])
            if found["fired"]:
                rec["triggered"], rec["via"] = True, found["via"]
                kill()
                break
    finally:
        timer.cancel()
        kill()
        proc.wait()
        RE.release(proc)
        drain.join(timeout=2)
    rec["duration_s"] = round(time.time() - start, 1)
    by_model: Dict[str, Dict[str, int]] = {}
    for m in usage_by_id.values():
        acc = by_model.setdefault(str(m["model"] or "unknown"), dict.fromkeys(TOKEN_KEYS, 0))
        for k in TOKEN_KEYS:
            acc[k] += int(m["usage"].get(k) or 0)
    if result_usage is not None:
        rec["tokens"] = dict({k: int(result_usage.get(k) or 0) for k in TOKEN_KEYS}, source="result")
    elif by_model:
        rec["tokens"] = dict({k: sum(u[k] for u in by_model.values()) for k in TOKEN_KEYS},
                             source="stream (output_tokens approximate)", by_model=by_model)
        rec["est_cost_usd"] = est_cost(by_model, args.price_table)
    if not rec["triggered"] and not rec["error"]:
        if timed_out.is_set():
            rec["error"] = "timeout after %ds" % args.timeout
        elif RE._stop.is_set():
            rec["error"] = "cancelled"
        elif not saw_assistant:   # auth, rate-limit or CLI errors: not a "did not trigger"
            rec["error"] = "no assistant turn (subtype %s, exit %s): %s" % (
                rec["subtype"], proc.returncode, RE.tail("".join(err_chunks), 300))
    if args.trace_dir:
        stop = "fired" if rec["triggered"] else ("timeout" if timed_out.is_set() else
                                                 ("error" if rec["error"] else "exit %s" % proc.returncode))
        trace.append("END subtype=%s cost_usd=%s stop=%s%s" % (rec["subtype"], rec["cost_usd"], stop,
                                                                (" error=%s" % rec["error"]) if rec["error"] else ""))
        try:
            Path(args.trace_dir, "q%03d-r%d.txt" % (qi, r)).write_text("\n".join(trace) + "\n", encoding="utf-8")
        except OSError as exc:
            log("warning: cannot write trace: %s" % exc)
    return rec


def prepare_cwd(repo: Path, isolate: bool):
    if not isolate:
        return repo, None
    if not L.in_git_repo(repo):
        log("warning: %s is not a git repo; running in place (--no-isolate)" % repo)
        return repo, None
    root = L.repo_root(repo)
    wt = RE.Worktree(root, "trigger-evals")
    try:
        wt.create()
        RE.apply_config_copies(root, wt.path, RE.uncommitted_config(root, ""))
    except BaseException:
        wt.remove()  # also deletes the mkdtemp directory
        raise
    rel = repo.resolve().relative_to(root) if repo.resolve() != root else Path(".")
    return wt.path / rel, wt


def plan_dirs(cwd: Path) -> List[Path]:
    """Where plan files can land: plansDirectory relative to cwd or to its git root."""
    dirs = [cwd / PLANS_DIR]
    if L.in_git_repo(cwd):
        dirs.append(L.repo_root(cwd) / PLANS_DIR)
    return sorted(set(dirs))


def _num(x: Any) -> float:
    return float(x) if isinstance(x, (int, float)) else 0.0


def add_tokens(recs: List[dict]) -> Dict[str, int]:
    return {k: sum(int((x.get("tokens") or {}).get(k) or 0) for x in recs) for k in TOKEN_KEYS}


def summarize(results: List[dict]) -> dict:
    tp = sum(1 for r in results if r["triggered"] is True and r["should_trigger"])
    fp = sum(1 for r in results if r["triggered"] is True and not r["should_trigger"])
    tn = sum(1 for r in results if r["triggered"] is False and not r["should_trigger"])
    fn = sum(1 for r in results if r["triggered"] is False and r["should_trigger"])
    scored = tp + fp + tn + fn

    def ratio(a: int, b: int) -> Optional[float]:
        return round(a / b, 4) if b else None

    precision, recall = ratio(tp, tp + fp), ratio(tp, tp + fn)
    f1 = round(2 * precision * recall / (precision + recall), 4) if precision and recall else None
    tokens = {k: sum(r["tokens"][k] for r in results) for k in TOKEN_KEYS}
    out = {"queries": len(results), "scored": scored, "unscored": len(results) - scored,
           "tp": tp, "fp": fp, "tn": tn, "fn": fn, "precision": precision, "recall": recall, "f1": f1,
           "accuracy": ratio(tp + tn, scored), "cost_usd_lower_bound": round(sum(r["cost_usd"] for r in results), 4),
           "killed_runs_without_cost": sum(r["killed_runs_without_cost"] for r in results),
           "tokens": tokens}
    est = [r["est_cost_usd"] for r in results if r["est_cost_usd"] is not None]
    if est:
        out["est_cost_usd_lower_bound"] = round(out["cost_usd_lower_bound"] + sum(est), 4)
    out["cost_note"] = ("killed runs report no cost_usd; their tokens come from assistant events, leave out "
                        "background (e.g. haiku) calls and count output tokens only approximately")
    return out


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="run_trigger_evals.py",
                                description="Trigger-rate evals for the ai-native-sdlc skill using the real claude CLI.")
    p.add_argument("--evals", default=str(HERE / "trigger-evals.json"), help="query file (default: trigger-evals.json next to this script)")
    p.add_argument("--repo", default=".", help="directory claude runs in; the skill must be discoverable there (default: cwd)")
    p.add_argument("--runs", type=int, default=3, help="runs per query (default 3)")
    p.add_argument("--jobs", type=int, default=4, help="parallel claude processes (default 4)")
    p.add_argument("--model", default=None, help="--model passed to claude")
    p.add_argument("--max-turns", type=int, default=2, help="--max-turns passed to claude (default 2)")
    p.add_argument("--max-budget-usd", type=float, default=None, help="--max-budget-usd per run (optional)")
    p.add_argument("--max-total-usd", type=float, default=None,
                   help="stop starting runs once known (+ --prices estimated) cost passes this; the rest are unscored")
    p.add_argument("--prices", default=None, help="JSON {model substring: {input, output, cache_read, cache_write} $/MTok} "
                   "to estimate killed runs' cost")
    p.add_argument("--timeout", type=int, default=180, help="seconds per run before it is killed (default 180)")
    p.add_argument("--trigger-threshold", type=float, default=0.5, help="trigger rate at or above which a query counts as triggered (default 0.5)")
    p.add_argument("--skill-name", default="ai-native-sdlc")
    p.add_argument("--claude-bin", default="claude")
    p.add_argument("--limit", type=int, default=None, help="only the first N queries (smoke test)")
    p.add_argument("--no-isolate", action="store_true", help="run directly in --repo instead of a throwaway worktree")
    p.add_argument("--clean", action="store_true", help="no user/claude.ai skills, plugins or MCP connectors "
                   "(--strict-mcp-config --setting-sources project,local); use in CI")
    p.add_argument("--trace-dir", default=None, help="write a per-run trace q<NNN>-r<R>.txt here")
    p.add_argument("--fail-under", type=float, default=None, help="exit 1 if accuracy is below this (0-1)")
    p.add_argument("--out", default=None, help="also write the JSON results here")
    p.add_argument("--dry-run", action="store_true", help="print the commands; run nothing")
    args = p.parse_args(argv)

    try:
        if args.runs < 1 or args.jobs < 1 or args.max_turns < 1 or args.timeout < 1:
            raise RE.UsageError("--runs, --jobs, --max-turns and --timeout must be >= 1")
        args.price_table = load_prices(args.prices)
        queries = load_queries(Path(args.evals))[: args.limit]
        repo = Path(args.repo).resolve()
        if not repo.is_dir():
            raise RE.UsageError("--repo %s is not a directory" % repo)
        if args.dry_run:
            print("# %d queries x %d runs in %s (%s%s)" % (len(queries), args.runs, repo,
                  "in place" if args.no_isolate else "isolated worktree of HEAD + uncommitted .claude/**, CLAUDE.md",
                  "; clean" if args.clean else ""))
            for q in queries:
                cmd = build_cmd(q["query"], args)
                print("%s   # expect %s" % (" ".join(shlex.quote(c) for c in cmd),
                                            "trigger" if q["should_trigger"] else "no trigger"))
            return 0
        if not (shutil.which(args.claude_bin) or Path(args.claude_bin).is_file()):
            raise RE.UsageError("claude executable %r not found (try PATH=\"$HOME/.local/bin:$PATH\" or --claude-bin)" % args.claude_bin)
        if args.trace_dir:
            Path(args.trace_dir).mkdir(parents=True, exist_ok=True)
        cwd, wt = prepare_cwd(repo, not args.no_isolate)
    except (RE.UsageError, L.SdlcError, OSError, ValueError) as exc:
        sys.stderr.write("run_trigger_evals: error: %s\n" % exc)
        return 2

    created_plans = [d for d in plan_dirs(cwd) if not d.exists()] if wt is None else []
    log("%d queries x %d runs, %d parallel, cwd %s%s" % (len(queries), args.runs, args.jobs, cwd,
                                                       " (clean)" if args.clean else ""))
    old_handlers = RE.install_signal_handlers()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs)
    futs: Dict[Any, concurrent.futures.Future] = {}
    recs: Dict[Any, dict] = {}
    try:
        futs = {(qi, r): pool.submit(run_once, q["query"], cwd, args, qi, r)
                for qi, q in enumerate(queries) for r in range(args.runs)}
        spent, capped = 0.0, False
        for fut in concurrent.futures.as_completed(list(futs.values())):
            if fut.cancelled():
                continue
            rec = fut.result()
            spent += _num(rec["cost_usd"]) + _num(rec["est_cost_usd"])
            if args.max_total_usd and not capped and spent >= args.max_total_usd:
                capped = True
                n = sum(1 for f in futs.values() if f.cancel())
                log("--max-total-usd %.2f reached ($%.2f known%s); %d run(s) not started" % (
                    args.max_total_usd, spent, " + estimated" if args.price_table else "", n))
    except KeyboardInterrupt:
        code = RE.stop_all()
        log("run_trigger_evals: interrupted; stopped claude processes, removing the worktree")
        pool.shutdown(wait=True, cancel_futures=True)
        return code
    finally:
        pool.shutdown(wait=True)
        if wt is not None:
            wt.remove()
        for d in created_plans:
            shutil.rmtree(str(d), ignore_errors=True)
        RE.restore_signal_handlers(old_handlers)

    results = []
    for (qi, r), fut in futs.items():
        recs[(qi, r)] = fut.result() if not fut.cancelled() else {
            "triggered": False, "via": None, "subagent_via": [], "other_skills": [], "cost_usd": None,
            "est_cost_usd": None, "tokens": None, "error": "cancelled: --max-total-usd reached",
            "subtype": None, "model": None, "permission_mode": None, "first_tool": None}
    for qi, q in enumerate(queries):
        runs = [recs[(qi, r)] for r in range(args.runs)]
        ok = [x for x in runs if not x["error"]]
        hits = sum(1 for x in ok if x["triggered"])
        rate = round(hits / len(ok), 4) if ok else None
        other: Dict[str, int] = {}
        for x in runs:
            for s in x["other_skills"]:
                other[s] = other.get(s, 0) + 1
        triggered = None if rate is None else rate >= args.trigger_threshold
        est = [x["est_cost_usd"] for x in runs if x["est_cost_usd"] is not None]
        res = {"query": q["query"], "should_trigger": q["should_trigger"], "runs": len(runs),
               "valid_runs": len(ok), "triggers": hits, "trigger_rate": rate, "triggered": triggered,
               "correct": None if triggered is None else triggered == q["should_trigger"],
               "via": sorted(set(x["via"] for x in runs if x["via"])),
               "subagent_via": sorted(set(v for x in runs for v in x["subagent_via"])),
               "first_tools": [x["first_tool"] for x in runs], "other_skills": other,
               "errors": [x["error"] for x in runs if x["error"]],
               "cost_usd": round(sum(_num(x["cost_usd"]) for x in runs), 4),
               "killed_runs_without_cost": sum(1 for x in runs if x["cost_usd"] is None
                                               and (x["triggered"] or x["tokens"] is not None)),
               "est_cost_usd": round(sum(est), 6) if est else None, "tokens": add_tokens(runs),
               "models": sorted(set(x["model"] for x in runs if x["model"]))}
        results.append(res)
        mark = "?" if res["correct"] is None else ("ok " if res["correct"] else "BAD")
        log("%s %s rate=%s expect=%s %s%s%s" % (
            mark, "T" if q["should_trigger"] else "F", "-" if rate is None else "%.2f" % rate,
            q["should_trigger"], q["query"][:70].replace("\n", " "),
            ("  other=%s" % other) if other else "",
            ("  subagent=%s" % res["subagent_via"]) if res["subagent_via"] else ""))

    all_runs = list(recs.values())
    models = sorted(set(x["model"] for x in all_runs if x["model"]))
    if len(models) > 1:
        log("warning: sessions reported more than one model: %s" % ", ".join(models))
    out = {"skill": args.skill_name, "model_requested": args.model, "models_seen": models,
           "permission_modes_seen": sorted(set(x["permission_mode"] for x in all_runs if x["permission_mode"])),
           "mode": "clean" if args.clean else "default", "runs_per_query": args.runs,
           "trigger_threshold": args.trigger_threshold, "generated_at": L.now_iso(),
           "summary": summarize(results), "results": results}
    text = json.dumps(out, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    s = out["summary"]
    log("precision=%s recall=%s accuracy=%s (tp=%d fp=%d tn=%d fn=%d, unscored=%d) cost>=$%.2f%s; "
        "%d killed run(s) without cost (tokens: %s)" % (
            s["precision"], s["recall"], s["accuracy"], s["tp"], s["fp"], s["tn"], s["fn"], s["unscored"],
            s["cost_usd_lower_bound"],
            (" (est>=$%.2f)" % s["est_cost_usd_lower_bound"]) if "est_cost_usd_lower_bound" in s else "",
            s["killed_runs_without_cost"], ", ".join("%s=%d" % kv for kv in s["tokens"].items())))
    log("note: " + s["cost_note"])
    if args.fail_under is not None and (s["accuracy"] is None or s["accuracy"] < args.fail_under):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
