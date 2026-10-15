# Stage 4 (Test): feedback loop, fix mode, continuous evals

Use this file to make every session verify its own work before a human sees it, to make bug fixes prove themselves with a test the agent could not rewrite, and to regression-test the agent's configuration (CLAUDE.md, skills, hooks, subagents) the way code is tested. It covers two plays from the playbook. The first is "Give Claude a feedback loop". The second is "Continuous evals in CI". It also gives the procedure for the `verify` and `fix` modes and the `evals` mode. Stage 4 has no human gate of its own. Its evidence feeds the `merge` gate (PR approval by a code owner, see references/stage-5-deploy.md).

## Contents
- Play: give Claude a feedback loop
  - Feedback loop vs verifier subagent
  - Evidence before claims
- Procedure: fix mode (failing test first)
- Procedure: `verify` mode
- Play: continuous evals in CI
  - Case format and runner
  - Why not `--bare`, why not `claude plugin eval`
- Pitfalls

---

## Play: give Claude a feedback loop

**What changes.** Traditional: the signal that code works arrives late, from CI minutes later, a tester days later or production weeks later, so a person has to check all agent output. AI-native: the session runs the tests, the build or the screenshot itself and iterates until the check passes, so what reaches the engineer has already passed it.

**Prerequisites.** None. Infrastructure: a test suite and a build that each run locally with one command. For UI work, a way for Claude to see the result: a browser tool or a screenshot utility connected via MCP.

### Steps

The engineer running the session owns the setup.

```
- [ ] 1. Wrap verification in single targets that exit non-zero on failure (make test / npm test / make lint / make build)
- [ ] 2. List each command in CLAUDE.md with an example of healthy output (template: ../assets/templates/CLAUDE-verification.md)
- [ ] 3. Record the same commands in .claude/sdlc.json "verify_commands" (sdlc.py init detects them)
- [ ] 4. State a quantifiable target in every task and in plan.md "## Proof"
- [ ] 5. Bugs: failing test first, committed, then fix mode (procedure below)
- [ ] 6. UI: give Claude the mock and a screenshot/browser tool; iterate 2-3 rounds
- [ ] 7. Make verification part of "done": CLAUDE.md instruction + Stop hook
- [ ] 8. Protect the loop: test-file guard in fix mode + CI drift check
```

1. **One command per check.** If checking the work takes a sequence of commands and environment knowledge, wrap it in one target such as `make test` or `npm test` that exits non-zero on failure. Claude can then interpret one exit code, and the hooks can recognise one command prefix.
2. **CLAUDE.md commands with healthy output.** Paste the block from [CLAUDE-verification.md](../assets/templates/CLAUDE-verification.md) under `## Verifying your work` and fill in `{{build}}`, `{{test}}` and `{{lint}}`. A literal example of success (`"Build succeeded"`, `"0 warnings"`) lets Claude tell a pass from a silent skip without asking you. The block carries two rules: "If a test fails, fix the code, not the test" and "When Claude makes the same mistake twice, add the correction here".
3. **`verify_commands`.** Set them in `.claude/sdlc.json`, for example `["make test", "make lint", "make build"]`. `python3 <skill-dir>/scripts/sdlc.py init --dry-run` shows what it detects. The Stop hook only treats a Bash call that starts with one of these prefixes as verification, so an empty list disables it.
4. **Quantifiable targets.** Write targets Claude can check without you. Examples: "All tests in `test_status.py` pass", "the screenshot matches `mocks/status.png`", "`GET /claims/{id}/status` returns 200 with field `eta`". Put them in plan.md `## Proof` as backticked commands or test files, because `sdlc.py validate --strict` requires that and the verifier reads it.
5. **Bugs.** Follow the fix-mode procedure below. A test that existed before the fix, and that the agent could not rewrite, is the proof that the bug is gone.
6. **UI loop.** Give Claude the mock and a browser or screenshot tool, then let it implement, screenshot, compare and adjust. Two or three rounds is normal, and each round should be visibly closer. If round three is not converging, stop and ask the human which difference matters. More rounds rarely help at that point. Keep the final screenshot, or the diff image, as evidence in the PR.
7. **Verification is part of "done".** The CLAUDE.md instruction is the advisory layer: "Run all three before reporting any task complete, and paste the output". The Stop hook (`hooks.py stop`, installed by `sdlc.py init`) is the deterministic layer. It applies to every session in the repo, with or without an active change (so trivial-tier work is covered too), whenever `verify_on_stop` is true and `verify_commands` is not empty. Edits to `plan_guard_exempt` paths (for example `docs/**`) do not count. If any other file was edited after the last verify command, it blocks the stop with: "Source files changed after the last verification run. Run … and paste the output". After 8 consecutive blocks with no tool call in between, Claude Code overrides a Stop hook (the cap is set by `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`). Treat repeated blocks as a broken command that needs fixing. Raising the cap is not the fix.
8. **Protect the loop.** An agent fixing code must not be able to weaken the check on that code. See the layers in the fix-mode procedure.

### Feedback loop vs verifier subagent

They are different controls. Do not substitute one for the other.

| | Feedback loop | `sdlc-verifier` subagent |
|---|---|---|
| When | Throughout the task, as many times as there is work | Once, after the session believes it is done |
| Context | The implementing session's own context | Fresh context, so the verdict is not coloured by the assumptions that produced the code |
| Does | Runs checks, fixes, reruns | Runs plan.md `## Proof` and `verify_commands`, exercises the changed behaviour plus the 2 nearest flows, reports mismatches with plan.md |
| May edit | Yes | No, by instruction (`tools: Bash, Read`; see below) |
| Definition | CLAUDE.md + hooks | [sdlc-verifier.md](../assets/agents/sdlc-verifier.md), installed to `.claude/agents/` |

Run the verifier in the same checkout, never with `isolation: worktree`. A worktree subagent branches from the default branch, so it would not see the uncommitted code it is meant to verify.

**The verifier's no-edit rule is advisory.** `sdlc-verifier` has no `permissionMode` and inherits the parent session's mode. Under a default-mode parent, a write command prompts a human, who can still approve it. Under acceptEdits, auto (the interactive default from v2.1.283) or bypassPermissions, nothing but its instructions stops it. Only a dontAsk parent with the verify commands allow-listed denies writes. The same applies to `sdlc-reviewer` and `sdlc-gate`: their `permissionMode: dontAsk` is ignored under an acceptEdits, auto or bypassPermissions parent, and their `tools:` list is what keeps them read-only. The verifier runs one check per Bash call, using each command exactly as written in Proof or `verify_commands`. In headless runs every one of those commands must be pre-approved with `--allowedTools`, or the verifier reports it as not verified ([automation.md](automation.md)).

### Evidence before claims

Never report a state without the evidence in the right-hand column, produced in this session after the last edit. "The agent said it passed" is not evidence.

| Claim | Required evidence |
|---|---|
| "Tests pass" | Literal tail of the test command output showing the pass count and exit 0, run after the last source edit |
| "Bug fixed" | The committed failing test (commit SHA from FIX_MODE_ON) now passes; output before and after |
| "Build/lint clean" | Command output with "Build succeeded" / zero warnings, as CLAUDE.md defines healthy |
| "UI matches the mock" | Final screenshot or diff image plus the round count |
| "Endpoint works" | The request and response (status code and body excerpt) |
| "No unplanned changes" | `sdlc.py drift <id>` output |
| "Requirements covered" | `sdlc.py trace <id> --tests` output with no GAP |
| "Done" | All of the above that apply, plus a VERIFIED audit line |

Record the final verification in the audit log:

```bash
python3 <skill-dir>/scripts/sdlc.py log <id> VERIFIED command="make test" result=pass --message "142 passed in 8.1s"
```

### Governance

| | |
|---|---|
| Enforced | Verification before "done" (Stop hook); no test-file edits during a fix (PreToolUse hook + CI drift check). Use hooks wherever the org wants a guarantee rather than an instruction. |
| Evidence | Toolchain output pasted by Claude: test output, build log, screenshot diff. Nothing paraphrased. |
| Logged | The session transcript, forwarded by the OpenTelemetry export to the org's observability stack; the PR's check runs; `VERIFIED` and `FIX_MODE_*` lines in `changes/<id>/audit.md`; hook decisions in `.claude/sdlc-state/hook-log.jsonl`. |
| Approves | The code owner reviewing the PR (`merge` gate). The mechanical evidence is already attached, so they can concentrate on intent and risk. |

### Measure

- **Leading: first-pass CI success rate for agent-written changes.** This is the share of agent PRs whose first CI run passed. Agent PRs are identified here by the `claude/` branch prefix (the claude-code-action default). Use a label instead if your org marks them that way.
  ```bash
  gh run list --workflow ci.yml --event pull_request --limit 500 --json headBranch,conclusion,createdAt \
  | jq '[group_by(.headBranch)[] | sort_by(.createdAt) | .[0] | select(.headBranch|startswith("claude/"))]
        | {prs: length, first_pass: (map(select(.conclusion=="success"))|length)} | .rate = (.first_pass / (.prs|if .==0 then 1 else . end))'
  ```
- **Lagging: review time per PR**, which should fall once the tests catch what reviewers used to catch. The query below gives the median hours from PR open to first human approval:
  ```bash
  gh pr list --state merged --limit 200 --json createdAt,reviews \
  | jq '[.[] | (.reviews|map(select(.state=="APPROVED"))|.[0].submittedAt) as $a | select($a)
        | (($a|fromdateiso8601) - (.createdAt|fromdateiso8601))/3600] | sort | .[length/2|floor]'
  ```
- **Lagging: change failure rate**, taken from the incident tracker. For git-derived proxies, see references/metrics.md.

---

## Procedure: fix mode (failing test first)

Every bug fix, even a one-line one, is at least `bounded`, because fix mode needs a change folder and `trivial` has none. With the default `plan_guard`, writing the test is itself an edit, so task T1 of plan.md is "reproduce as a failing test", and the plan gate comes first. A bug intent that came from a ticket, incident or scan (`--source`) is agent-written, so it also needs the intent gate before the plan. If your team wants reproduction before planning, add the test globs to `plan_guard_exempt` in `.claude/sdlc.json`.

```
- [ ] 1. sdlc.py new <slug> --bug --tier bounded      (intent-bug.md: Current / Expected / Unchanged behavior)
- [ ] 2. plan.md: T1 = failing test; lists the test file and the fix files; plan gate (SKILL.md Gate protocol)
- [ ] 3. Write the test that reproduces the bug; one test per Expected clause, plus Unchanged clauses as regression tests
- [ ] 4. Run it; confirm it FAILS, and for the expected reason (assertion message, not ImportError/typo)
- [ ] 5. Commit the test alone: git commit -m "test(<scope>): reproduce <bug>"
- [ ] 6. sdlc.py fix-mode <id> on --by "<name>"        (records FIX_MODE_ON commit=<HEAD sha>)
- [ ] 7. Make it pass by changing code only; hooks deny test edits
- [ ] 8. Run the full verify_commands; paste output
- [ ] 9. sdlc.py drift <id> --strict                   (exit 1 if any test file changed after the FIX_MODE_ON commit)
- [ ] 10. sdlc.py fix-mode <id> off; log VERIFIED; open PR
```

Commands:

```bash
python3 <skill-dir>/scripts/sdlc.py new status-500-on-empty --bug --tier bounded --title "500 on empty claim list"
python3 <skill-dir>/scripts/sdlc.py fix-mode <id> on --by "J. Ortiz"
python3 <skill-dir>/scripts/sdlc.py drift <id> --strict
python3 <skill-dir>/scripts/sdlc.py fix-mode <id> off --by "J. Ortiz"
```

`fix-mode` is a gate decision and is deliberately not pre-approved, so Claude Code asks the human before it runs. `--by` is the human's name, as for `approve`.

Step 4 matters most. A test that fails for the wrong reason, such as a missing import, a fixture error or a wrong path, "passes" after any fix and proves nothing. Quote the failure line when you report it.

**If the test itself is wrong**, do not edit around the guard. Stop and ask the human. Ending fix mode is a recorded decision (`fix-mode <id> off`, FIX_MODE_OFF in the audit log), and the reviewer will see it.

**Layers that protect the test.** Use as many as the risk warrants. Each one covers a gap left by the one before.

| Layer | Stops | Gap |
|---|---|---|
| `hooks.py pre-tool`, file tools | `Edit`/`Write`/`NotebookEdit` on any `test_globs` path while fix mode is on | Bash writes |
| `hooks.py pre-tool`, Bash | Write-ish commands (`>`, `tee`, `sed -i`, `mv`, `cp`, `rm`, `git checkout --`, `git restore` …) that name a test path | Heuristic: misses writes through scripts or indirection |
| `permissions.deny: ["Edit(/tests/**)"]` (optional, static) | File-tool edits regardless of mode | Always on, so it blocks normal test writing too; does not stop `sed -i` |
| `sandbox.filesystem.denyWrite` on test paths | Bash writes at OS level | Static; needs the sandbox |
| CI `drift --strict` (`sdlc-artifacts.yml`) | Any committed test change after the FIX_MODE_ON commit | After the fact, but deterministic and unskippable via branch protection |

The CI check is the backstop the playbook names ("check the diff in review and reject any change that touches a test"). Make it a required status check.

---

## Procedure: `verify` mode

Run this when the user says "verify", when the session is about to report done, or before opening a PR.

```
- [ ] 1. sdlc.py status <id> --brief; read plan.md "## Proof"
- [ ] 2. Run every verify_command and every Proof command; capture literal output tails
- [ ] 3. sdlc.py drift <id> and sdlc.py trace <id> --tests; explain any unplanned file or GAP
- [ ] 4. Spawn sdlc-verifier (fresh context) with the change id; do not pass your own conclusions
- [ ] 5. If FAIL: fix, return to step 2. If PASS: log VERIFIED with the command and result
- [ ] 6. Report using the evidence table above; include the verifier's PASS/FAIL table verbatim
```

At step 4, give the verifier the change id, the plan path and the commands, and nothing else. Your summary of what you did is exactly the testimony it must not inherit. If `sdlc-verifier` is not installed (or was added this session), use a `general-purpose` agent with the body of `<skill-dir>/assets/agents/sdlc-verifier.md` and tell it never to edit files.

---

## Play: continuous evals in CI

**What changes.** Traditional: QA gates at stage boundaries. AI-native: a suite of real tasks runs whenever the agent's configuration changes, and on a schedule, so a model swap or a rewritten skill shows whether the agent still does the work to the same standard. Some teams run the suite offline on a set cadence instead. The steps below are for continuous runs.

**Prerequisites.** CLAUDE.md and the feedback loop (this file). Infrastructure: CI that can run Claude Code non-interactively, and an API key or workload identity with a budget for eval runs.

### Steps

```
- [ ] 1. Platform engineer collects 20-50 real tasks from recent work, each with its accepted outcome
- [ ] 2. Write each as evals/<id>.json: prompt + setup + checks that define "acceptable"
- [ ] 3. Dry-run locally: run_evals.py --dry-run, then one real case
- [ ] 4. Install ../assets/github/agent-evals.yml (sdlc.py init --with-ci): PRs touching CLAUDE.md, .claude/**, evals/** + nightly
- [ ] 5. Make the eval job a required check for those paths; a pass-rate drop blocks merge until reviewed
- [ ] 6. Every production incident: the owning team adds evals/incident-<ID>.json; it stays as a regression test
- [ ] 7. Quarterly: retire or harden cases that no longer discriminate; add new ones from monitoring
```

1. **Real tasks.** Draw them from merged PRs and closed incidents, not invented exercises. Take the accepted diff as the reference outcome. Cover the task mix the agent actually gets: bug fix, small feature, refactor, policy-sensitive change, and an "it should refuse or ask" case.
2. **Checks define acceptable.** Map the playbook's criteria onto check types:

   | Acceptable means | Check |
   |---|---|
   | Tests pass | `{"type": "command", "run": "make test"}` |
   | Lint clean | `{"type": "command", "run": "make lint"}` |
   | Behaviour unchanged | `command` running the pre-existing tests, plus `diff_excludes` on `tests/**` |
   | Policy followed | `diff_excludes` on protected paths, a `command` running the policy linter, or `result_regex` on the required wording |
   | Right files touched | `diff_includes` / `file_exists` |

   Diff checks see tracked, untracked and gitignored files the agent added, edited or deleted (the baseline is taken after `setup`). They skip `.claude/sdlc-state/`, which the repo's hooks write on every run, and heavy trees such as `node_modules/` and `.venv/` unless a diff glob names them. Use a `command` check for anything else.

3. **Run it.** The commands below run the suite, then a single case, then the CI form:
   ```bash
   python3 <skill-dir>/scripts/run_evals.py --dry-run
   python3 <skill-dir>/scripts/run_evals.py --case status-endpoint-db-down --keep
   python3 <skill-dir>/scripts/run_evals.py --jobs 2 --threshold 0.9 --compare baseline.json --max-drop 0.05 --out results.json
   ```
   The runner exits 1 if `pass_rate` is below the threshold or has dropped more than `--max-drop` against the baseline. If the working tree has uncommitted edits to CLAUDE.md or `.claude/**`, it copies them into each worktree, so local config edits are tested before you push them.
4. **Triggers.** Run on PRs touching `CLAUDE.md`, `.claude/**` or `evals/**`, and nightly. The nightly run catches model and provider changes that no PR touches. The workflow compares against the last default-branch result.
5. **Gate config changes.** A configuration change that lowers the pass rate gets reviewed before it merges. A drop is not an automatic veto: the team that owns the configuration change decides, with the per-case diff in hand.
6. **Incident → permanent eval.** The team that owned the incident writes the case, reproducing the triggering condition in `setup` (a patch or fixture) and checking the corrected behaviour. Name it `evals/incident-<ID>.json` so the latency metric below can find it. Never retire incident cases.
7. **Live suite.** As models improve, cases that once discriminated start passing every run. A case that has passed on every run for a quarter is no longer telling you anything, so harden it or replace it with a new case drawn from monitoring. Keep the ones that guard incidents and policy.

### Case format and runner

Start from [eval-case.json](../assets/templates/eval-case.json). The fields are `id`, `prompt`, `setup` (commands run in the worktree), `allowed_tools`, `permission_mode`, `max_turns`, `timeout_s`, `max_budget_usd` (default 2.0), `model` and `checks` (`command`, `file_exists`, `result_regex`, `diff_excludes`, `diff_includes`). For each case, [run_evals.py](../scripts/run_evals.py) creates a detached worktree at HEAD, runs `setup`, and then runs:

```
SDLC_NONINTERACTIVE=1 claude -p --output-format json --permission-mode <mode> --max-turns N --max-budget-usd USD [--model M] --allowedTools <...>   # prompt on stdin
```

`SDLC_NONINTERACTIVE=1` keeps the repo's hooks from recording the eval prompt as a human turn. `--max-budget-usd` on the runner overrides every case's `max_budget_usd`. The runner then runs the checks in the worktree. A case fails on `is_error`, on a non-`success` `subtype` (including `error_max_turns` and `error_max_budget_usd`), or on any failed check, so never judge by exit code alone. Results go to `results.json` (`pass_rate`, `passed`, `total`, `cost_usd`, per-case checks).

Always set `permission_mode` and `allowed_tools` explicitly in each case. A run with no mode set can start in `auto`, which drops a bare `Bash` allow entry, and on a fresh CI checkout project `permissions.allow` is ignored in `-p` ([automation.md](automation.md)). Keep deploy-capable servers out of `.mcp.json` in repos you run evals on, because they connect without asking in `-p`.

### Why not `--bare`, why not `claude plugin eval`

- **`--bare` skips what the evals exist to test:** CLAUDE.md, skills, hooks and subagents. A run under `--bare` would pass even if a CLAUDE.md edit broke every session ([automation.md](automation.md#--bare-when-to-use-it-when-never)).
- **`claude plugin eval` cannot test project configuration.** Its runs are isolated: no `.claude/` directory, no CLAUDE.md and no `.mcp.json` load, even ones a scaffold script writes. Use it for a skill's own trigger and behaviour evals, packaged as a plugin. Use `run_evals.py` for the repo's configuration.

### Governance

| | |
|---|---|
| Enforced | Pass-rate threshold and maximum drop, as a required merge check on configuration paths. |
| Evidence | `results.json` per run: pass rate, per-case check results, cost. |
| Logged | CI run artifacts, kept so runs can be compared over time; the PR check. |
| Approves | The team that owns the configuration change, through the normal PR review. |

### Measure

- **Leading: eval pass rate over time.** Read it from each run's `results.json`. The loop below downloads the last 30 runs. Use the artifact name that `agent-evals.yml` uploads.
  ```bash
  for id in $(gh run list --workflow agent-evals.yml --limit 30 --json databaseId --jq '.[].databaseId'); do
    gh run download "$id" -n agent-evals-results -D "runs/$id" >/dev/null 2>&1 && jq -r --arg id "$id" '"\($id) \(.pass_rate) \(.cost_usd)"' "runs/$id/results.json"
  done
  ```
- **Leading: incident → permanent eval latency.** Measure from the incident close date in the tracker to the date the case was added:
  ```bash
  git log --diff-filter=A --format=%aI -- evals/incident-<ID>.json | tail -1
  ```
- **Lagging: regressions caught in CI vs regressions found in production.** For CI, count failing `agent-evals` runs on PRs:
  ```bash
  gh run list --workflow agent-evals.yml --event pull_request --status failure --limit 500 --json databaseId --jq length
  ```
  Compare that count with the incidents in the tracker that were tagged as agent-configuration regressions over the same window.

---

## Pitfalls

- **Weakening the check.** The agent "fixes" a failure by loosening an assertion, skipping a test or adding a retry. Fix mode, the hooks and the CI drift check exist for this. Outside fix mode, the CLAUDE.md rule and review are the controls.
- **A test that fails for the wrong reason.** It proves nothing. Read the failure line before committing.
- **Healthy output not defined.** Without an example of success, Claude accepts "0 tests collected" as green.
- **Verify commands that are not single prefixes.** The Stop hook matches command prefixes (after stripping `cd … &&` and env assignments). A pipeline built from five ad-hoc commands is never recognised as verification.
- **Evals under `--bare` or `claude plugin eval`.** They test nothing about CLAUDE.md or `.claude/**`.
- **Evals without an explicit permission mode.** Results then vary with the runner's starting mode.
- **A frozen eval suite.** A green suite that no longer discriminates gives false confidence. Feed it from incidents and monitoring.
- **Too many UI rounds.** If three rounds don't converge, the target is ambiguous. Ask the human.
