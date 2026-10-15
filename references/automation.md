# Automation: headless runs, GitHub Actions, triggers

Use this file when wiring Claude Code into CI, a scheduler or a webhook: `claude -p` recipes, `anthropics/claude-code-action@v1`, the triggers that turn the stages into a loop, and the gotchas that make headless runs silently do something other than intended. The rule behind every recipe: a headless run has nobody to answer a prompt and nobody to notice a surprise, so restrict tools explicitly, grant exactly what the step needs, bound the run, and check the result object rather than the exit code. Model output that drives a next step is structured (`--json-schema`), and a deterministic wrapper — not the model — performs the side effect.

## Contents
- Baseline for every headless run
- Recipe: read-only judgment step (diagnose, triage)
- Recipe: write step behind the gates
- Structured output and result checking
- `--bare`: when to use it, when never
- Fresh-checkout gotchas (workspace trust, MCP, Glob/Grep)
- `claude-code-action@v1` and the shipped workflows
- Trigger patterns that close the loop
- Managed runners
- Agent SDK service for webhooks
- Unverified items
- Pitfalls

---

## Baseline for every headless run

```
- [ ] --permission-mode set explicitly (dontAsk for read-only, acceptEdits for writes)
- [ ] --tools restricts the built-in set; --allowedTools grants within it (a grant is not a restriction)
- [ ] --output-format json; gate on .is_error and .subtype, not only the exit code
- [ ] --max-turns and --max-budget-usd set; job timeout-minutes set
- [ ] --json-schema when a script consumes the answer
- [ ] --strict-mcp-config (plus --mcp-config only for servers the step needs)
- [ ] No production credentials in the job environment; keep secrets out of the checkout and job env too. Where Bash is in --tools, set permissions.blockReadsOutsideWorkingDirectories plus permissions.deny Read rules or sandbox.filesystem.denyRead for credential paths
- [ ] Not --bare if the step relies on CLAUDE.md, skills, hooks or subagents
- [ ] SDLC_NONINTERACTIVE=1 in the child environment when it runs in a working copy with the hooks installed (its prompt is then not recorded as human presence; hooks inherit the `claude` process environment, checked on 2.1.294)
- [ ] Pinned Claude Code version (npm install -g @anthropic-ai/claude-code@<version>)
```

Why the permission mode must be explicit: a run where nothing sets a mode takes the built-in starting mode, which can be `auto`. In auto mode a bare `Bash` entry in `--allowedTools` is dropped as a broad allow and a classifier evaluates each command instead. Your recipe would behave differently from the one you reviewed.

## Recipe: read-only judgment step (diagnose, triage)

Used by the Maintain loop's 2σ/3σ diagnosis (`bands.py respond`) and by build triage.

```bash
claude -p "$(cat prompt.txt)" \
  --permission-mode dontAsk \
  --tools "Read,Grep,Glob,Bash" \
  --allowedTools "Bash(gh run view *)" \
  --strict-mcp-config \
  --output-format json \
  --json-schema "$(cat <skill-dir>/assets/schemas/intent.schema.json)" \
  --max-turns 25 --max-budget-usd 3 > out.json

jq -e '.is_error == false and .subtype == "success"' out.json >/dev/null || exit 1
jq '.structured_output' out.json > intent.json
python3 <skill-dir>/scripts/sdlc.py new <slug> --source incident --from-json intent.json --no-activate
```

- `--tools` removes Edit/Write from the session entirely; `dontAsk` denies any call that would prompt. File reads, the built-in read-only Bash set (`ls`, `cat`, `head`, `tail`, `grep`, `find`, read-only `git` and so on) and `--allowedTools` entries still run; here `--allowedTools` grants the one extra Bash prefix. The read-only set runs with no grant only inside the working directories; outside them it needs a grant like any other command.
- Naming Glob and Grep in `--tools` restores them (see the default tool-set gotcha below).
- The model never holds write or deploy tools. The wrapper writes intent.md; opening a PR or triggering a runbook is a separate deterministic step, only for runbooks pre-approved in `runbooks`. The shipped `control-bands.yml` does not pass `--execute-routes`: routes run in its model-free propose/runbook jobs, and the runbook job checks the deploy window itself (`SDLC_RUNBOOK_REQUIRE_DEPLOY_WITHIN_MIN`, `SDLC_DEPLOY_ENVIRONMENT`). `bands.py respond --execute-routes` is the path for non-GitHub wrappers.
- For build triage with no schema, drop `--json-schema` and take `.result`. The shipped `build-triage.yml` uses `--tools "Read,Grep,Glob"` with no Bash at all.

## Recipe: write step behind the gates

Fix lint, regenerate docs, address review comments. Everything the agent writes arrives as a PR through branch protection; it has no route to push to main.

```bash
claude -p "Fix the lint errors reported by make lint. Do not change behaviour." \
  --permission-mode acceptEdits \
  --tools "Read,Grep,Glob,Edit,Write,Bash" \
  --allowedTools "Bash(make lint)" "Bash(make test)" "Bash(git diff *)" \
  --strict-mcp-config \
  --output-format json --max-turns 30 --max-budget-usd 5 > out.json
```

- `acceptEdits` auto-approves file edits in scope; Bash still needs an allow rule. Keep the space before `*` in rules (`Bash(git diff *)`).
- On a host that can answer permission requests (an SDK `canUseTool` callback or `--permission-prompt-tool`), add `--permission-prompts none` (v2.1.259+) so anything that would prompt is denied instead of waiting; it also removes AskUserQuestion.
- Commit and push from the workflow step after the run, to a `claude/…` branch, then `gh pr create`. Don't grant `git push` to the model. Open the PR with a GitHub App token (as `control-bands.yml` propose does with `SDLC_APP_ID` / `SDLC_APP_PRIVATE_KEY`): `GITHUB_TOKEN` cannot create PRs while "Allow GitHub Actions to create and approve pull requests" is off, and PRs it opens trigger no CI.
- When the run goes through the skill (a headless `build`, `fix` or `verify`), list every `verify_commands` entry and every command in plan.md's `## Proof` in `--allowedTools`. Otherwise the verifier's Bash calls are denied and the run ends without evidence.
- A headless run that writes intent, spec or plan ends with `validate` and an artifact PR on `change/<id>`, never `approve` (SKILL.md, Headless runs). `--allowedTools "Bash(python3 .claude/skills/ai-native-sdlc/scripts/sdlc.py *)"` is broad enough to reach `approve`; `approve` still refuses without a recorded human turn, and with `SDLC_NONINTERACTIVE=1` none is recorded.

## Structured output and result checking

`--output-format json` returns one object: `type`, `subtype`, `is_error`, `result` (success only), `structured_output` (with `--json-schema`), `num_turns`, `total_cost_usd`, `session_id`, `permission_denials`, `errors` (error subtypes).

| `subtype` | Meaning | Action |
|---|---|---|
| `success` | Finished | Use `structured_output` / `result` |
| `error_max_turns` | Hit `--max-turns` | Fail the job; raise the cap only if the task is legitimately long |
| `error_max_budget_usd` | Hit `--max-budget-usd` (client-side estimate) | Fail the job |
| `error_during_execution` | Runtime failure | Fail; print `.errors` |
| `error_max_structured_output_retries` | Could not satisfy the schema | Fail; simplify the schema |

Failures inside the run are printed as the result on stdout, so the process exit code alone is not enough. A non-empty `permission_denials` on a success means the agent tried something the recipe did not grant; log it.

An invalid schema exits with `--json-schema is not a valid JSON Schema`; the `format` keyword is not enforced, so validate formats in the wrapper. A `$schema` keyword with the draft 2020-12 URI is enough to trigger that error: stderr says "not a valid JSON Schema", stdout is empty and the exit code is 1. The shipped schemas in `assets/schemas/` declare no `$schema`; strip it from any schema you add (`$id` is accepted).

## `--bare`: when to use it, when never

`--bare` skips hooks, skills, custom commands, subagents, plugins, MCP servers, auto memory and CLAUDE.md, sends no system reminders, and requires `ANTHROPIC_API_KEY` (or provider credentials).

| Use `--bare` | Never use `--bare` |
|---|---|
| Isolated judgment steps whose whole context you pass in: triage a log, summarize a flaky test, draft a changelog | Config-regression evals (`run_evals.py`, `agent-evals.yml`) — they exist to test CLAUDE.md, skills and hooks |
| Running against a repository you did not write | Anything that relies on the production-gate or test-protection hooks |
| | The PR review job (it runs the repo skill and reads REVIEW.md) |
| | The diagnose step when it needs repo skills; pass context with `--append-system-prompt-file` if you do go bare |

## Fresh-checkout gotchas (workspace trust, MCP, Glob/Grep)

Every CI checkout is a folder Claude Code has never trusted, and `-p` shows no trust dialog.

| Repository content | Used in `-p` on an untrusted folder? | Consequence |
|---|---|---|
| Hooks in `.claude/settings.json`, the `env` block, a project skill's `hooks` and `allowed-tools` | Used | The skill's hooks and `allowed-tools` work in CI. So does a malicious PR's hook — see below |
| `permissions.allow` in `.claude/settings.json` | Not used (stderr: "this workspace has not been trusted") | Pass `--allowedTools` (or `--settings`) in every recipe |
| Subagent frontmatter hooks, inline subagent `mcpServers`, project `@skills-dir` plugins | Not used | Put must-fire guardrails in settings hooks, not subagent frontmatter |
| `.mcp.json` servers | Connected without asking | Use `--strict-mcp-config`; add `--mcp-config` only for what the step needs |

- Alternative to flags: pre-seed `~/.claude.json` `projects["<repo root>"].hasTrustDialogAccepted: true` on the runner. Prefer explicit flags; they are visible in the workflow.
- Untrusted PR content (forks, external contributors): hooks from the PR's own `.claude/settings.json` run. Don't run Claude with secrets on fork code. If you must, use `--bare`, or `--setting-sources user` plus `--settings '{"disableAllHooks": true}'`. Never combine `pull_request_target` with a checkout of the PR head.
- **Glob and Grep are not in the default tool set on macOS, Linux and WSL.** Claude searches with `find`/`grep` through Bash, and those searches reach hooks and permission rules as `Bash` calls. Naming either tool in `--tools` or `--allowedTools` restores both. In a subagent they return only when `tools` lists them and omits Bash — hence `sdlc-reviewer` uses `tools: Read, Grep, Glob`.
- **AskUserQuestion and ExitPlanMode are never available in `-p`**, even when `--tools` names them (checked on 2.1.294). Headless runs can't record plan-mode acceptance, so `plan-from-accepted` and AskUserQuestion-based presence are interactive-only.
- Plain project skills (`.claude/skills/<name>/`) load in `-p`; a skills-directory plugin does not. A `/<name> ...` slash prompt needs no grant, but a plain-text prompt that makes the model call the Skill tool needs `Skill(<name>)` and `Skill(<name> *)` in `--allowedTools`.
- `$CLAUDE_PROJECT_DIR` stays at the original root even when the session works in a worktree; hook stdin `cwd` follows the worktree.

## `claude-code-action@v1` and the shipped workflows

Inputs the shipped workflows use: `prompt` (plain text or a skill invocation; omitting it gives interactive `@claude` mode), `claude_args` (any CLI flags: `--permission-mode`, `--allowedTools`, `--max-turns`, `--model`, `--json-schema`), `anthropic_api_key` or `claude_code_oauth_token`, `use_bedrock` / `use_vertex` / `use_foundry` (traffic on the organization's cloud agreement), `trigger_phrase`, `settings`, `allowed_bots`. Outputs: `conclusion`, `structured_output`, `session_id`, `execution_file`, `branch_name`.

| Workflow ([assets/github/](../assets/github/)) | Trigger | Mode | Permissions |
|---|---|---|---|
| `sdlc-artifacts.yml` | every PR: opened, synchronize, reopened, edited, ready_for_review (no paths filter) | Pure Python, no model: `validate --strict`, `trace`, `drift --strict` | `contents: read` |
| `claude-review.yml` | PR opened, synchronize, reopened, ready_for_review (non-draft, same-repo) | Job `review`: `prompt: "/ai-native-sdlc review <n>"`, `dontAsk`; `--allowedTools` names `Read,Grep,Glob`, `Skill(ai-native-sdlc)`, `Skill(ai-native-sdlc *)`, `Edit(/<runner.temp>/sdlc-review/**)`, `Bash(gh pr diff/view/comment *)`, `Bash(git diff/log/show/merge-base *)`, `Bash(python3 .claude/skills/ai-native-sdlc/scripts/sdlc.py *)`, `mcp__github_inline_comment__create_inline_comment`. With `SDLC_REVIEW_MODE=managed` only the separate model-free `managed-severity-gate` job runs instead | `contents: read`, `pull-requests: write` (to post comments only), `issues: read`, `id-token: write` (action auth); severity-gate job `checks: read` |
| `claude-mention.yml` | `@claude` in a PR comment, review comment or review (issues go through Stage 1) | Interactive fix loop; pushes go to the PR branch | `contents: write`, `pull-requests: write`, `issues: write`, `id-token: write`, `actions: read`; relies on "require approval of the most recent reviewable push" |
| `agent-evals.yml` | PR on `CLAUDE.md`, `.claude/**`, `evals/**`; push to the default branch (refreshes the baseline); nightly | `run_evals.py --threshold 0.9 --compare` (not bare) | `contents: read`, `actions: read` (baseline artifact download) |
| `control-bands.yml` | every 30 min; manual; inert until `SDLC_BANDS_ENABLED=true` | `bands.py check`, then `bands.py respond` (read-only diagnose + confidence gate); routes run in separate model-free jobs | per job: detect `contents/actions/issues/pull-requests: read`; diagnose `contents: read`, `actions: read`; triage `issues: write`; propose `contents: write`, `pull-requests: write`, `issues: write` (labels), opening PRs with the `SDLC_APP_ID` App token (an issue instead when it is not set); runbook `actions: write`, `deployments: read` in environment `sdlc-runbooks`, only with `SDLC_EXECUTE_RUNBOOKS=true`. No production credentials |
| `build-triage.yml` | `workflow_call` from a failed job, or `workflow_run` | Read-only `claude -p` triage; the workflow, not the model, posts the 3-line comment | `actions: read` (logs), `pull-requests: write` for the comment |

- The inline-comment MCP server starts only when `--allowedTools` in `claude_args` names it, even if the skill's `allowed-tools` lists it.
- Omit `github_token` so the action authenticates as the Claude GitHub App. Commits made with the default `GITHUB_TOKEN` don't trigger workflows, so the fix loop's pushes would skip CI.
- The action rejects bot actors unless listed in `allowed_bots`. That is your loop guard; keep the list short.
- The local and plugin `/code-review` do not read REVIEW.md. The review workflow therefore runs the repo skill, which loads REVIEW.md, spec.md and plan.md explicitly. The managed Code Review service reads REVIEW.md but its check run is always neutral; gate merges on its `bughunter-severity` tally if you use it (see references/stage-5-deploy.md).
- GitHub withholds secrets from fork-PR runs, so review runs only on same-repo branches unless someone triggers it.

## Trigger patterns that close the loop

Each accepted artifact fires the next step. Humans meet the work at the gates, not at the start. Rows naming a `.yml` are shipped by `init --with-ci`; rows marked (recipe) are yours to wire.

| Event | Trigger | Job | Output | Next gate |
|---|---|---|---|---|
| Accepted intent merged (recipe) | `push` to default branch, paths `changes/*/intent.md` | Spec pass: `prompt: "/ai-native-sdlc spec <id>"`, `acceptEdits`, writes only `changes/<id>/spec.md` | spec.md as a PR | spec (product owner reviews the PR) |
| Approved spec merged (recipe) | `push`, paths `changes/*/spec.md` | Notify the engineer, or draft with `--permission-mode plan` and post `.result` as a PR comment (ExitPlanMode doesn't exist in `-p`, so this draft is never an accepted plan; the gate stays interactive) | Draft plan | plan (engineer, interactively in plan mode) |
| PR merged (recipe) | `push` to default branch | Pipeline: build, deploy dev freely, staging gated, production prepared | Release candidate | release (release manager via environment approval) |
| Build failed | `if: failure()` job calling it (`workflow_call`), or `workflow_run` on the CI workflow | `build-triage.yml` | 3-line triage on the PR | — |
| `@claude` on a review comment | `issue_comment`, `pull_request_review_comment` | `claude-mention.yml` | Fix pushed to the PR branch | merge (re-approval after the push) |
| Agent config changed | PR paths `CLAUDE.md`, `.claude/**`, `evals/**` | `agent-evals.yml` | Pass rate vs baseline | merge (blocked on drop) |
| Band breached | schedule / monitoring webhook | `bands.py respond` | intent.md (`--source incident`), PR or pre-approved runbook | intent / merge |
| Scan finding | Claude Security webhook or export | bounded → patch PR; wider → `sdlc.py new --source scan` | PR or intent.md | merge / intent |

Find the change ids a push touched: `git diff --name-only "${{ github.event.before }}" "${{ github.sha }}" -- 'changes/*/intent.md' | cut -d/ -f2 | sort -u`. Make every trigger idempotent (skip when the downstream artifact already exists) so re-runs and merges of follow-up commits don't fan out.

Guard the recipe triggers per id with `python3 .claude/skills/ai-native-sdlc/scripts/sdlc.py status <id> --json`. A draft intent pushed straight to the default branch must not fire the job.
- **Spec job:** run only when the tier is `standard` or `high-risk` (bounded skips spec), `.gates.intent.current` is true (approved on the current fingerprint; the approval is committed in audit.md), and `changes/<id>/spec.md` does not exist.
- **Plan draft:** run only when `.gates.spec.current` is true and `plan.md` does not exist.

Between headless stages, put an independent confidence gate: `sdlc.py validate --strict` (deterministic) and the `sdlc-gate` subagent, which returns `continue` or `escalate` per [gate-verdict.schema.json](../assets/schemas/gate-verdict.schema.json) and defaults to `escalate` when uncertain. Escalation means a human, never a retry loop.

## Managed runners

When the runner has managed settings (see references/governance.md):

- `disableSideloadFlags` rejects `--agents`, `--mcp-config`, `--plugin-dir`, `--plugin-url`. Use committed `.claude/agents/` and managed MCP servers instead.
- `allowManagedPermissionRulesOnly` ignores `--allowedTools`, project rules and skill `allowed-tools`. Every CI command must be in the managed allow list.
- `allowManagedHooksOnly` runs only managed hooks and hooks from force-enabled plugins; install the skill's hooks as that plugin (`sdlc.py package-plugin`, see references/governance.md).
- Managed `ask` rules for the gate decisions fail closed in `-p`, which is intended: CI never records `approve`, `reject` or `fix-mode`.
- `requiredMinimumVersion` refuses older builds at startup; pin the installer at or above it.

## Agent SDK service for webhooks

Use the Agent SDK instead of a CI job when a monitoring webhook needs a response in seconds or the runner is a long-lived service. Run it in a sandboxed container with no production credentials and apply the same restrictions as the read-only recipe: explicit permission mode (`dontAsk`), restricted tools, a small allow list, a turn cap, a JSON-schema output, and a deterministic wrapper that calls `sdlc.py new --from-json`. The SDK loads `.mcp.json` and project settings only when its `settingSources` includes project settings; set it deliberately, because CLAUDE.md and skills depend on it. Check option names against the SDK version you pin.

## Unverified items

- Whether `--worktree`/`-w` works together with `-p` is not documented. For parallel headless runs, create worktrees with `git worktree add` yourself (as `run_evals.py` does).
- Whether `allowManagedHooksOnly` blocks skill-frontmatter hooks is implied, not stated.
- The OTel span `tool.blocked_on_user` is beta (`CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`); don't make gate-wait metrics depend on it alone.
- Flags evolve quickly. Re-check with `claude --help` on the pinned version before relying on a new one.

## Pitfalls

- **Treating `--allowedTools` as a sandbox.** It grants; `--tools` plus `dontAsk` restricts. The playbook's `bands.yaml` `tools:` string is a grant.
- **Relying on a subagent's `permissionMode`.** It is ignored when the parent session is in `acceptEdits`, `auto` (the interactive default from v2.1.283) or `bypassPermissions`. The `tools:` list is what keeps `sdlc-reviewer` and `sdlc-gate` read-only; their `permissionMode: dontAsk` applies only under a `default`, `dontAsk` or `plan` parent. `sdlc-verifier` inherits the parent's mode, so its no-edit rule is advisory: under `default` a write command prompts a human who can approve it, and only a `dontAsk` parent (with the verify commands allow-listed) denies it.
- **`disallowedTools` with a specifier.** In subagent frontmatter, `Bash(git push *)` removes the whole Bash tool. Use permission deny rules for scoped denies.
- **`ask` in CI.** A hook's `ask` fails closed in `-p` (nobody can answer). That is correct for a production gate, but it means a gate needing a named approver must check external evidence (environment approval, ticket state), not prompt.
- **Hook timeouts fail open.** A PreToolUse hook that times out does not block. Gates must enforce their own short internal timeout and exit 2 on error.
- **Cost drift.** Without `--max-budget-usd` a looping job spends until `--max-turns`. Report `total_cost_usd` per run (it includes subagents).
- **Model IDs hard-coded in workflows.** Pin per organization in one place (`claude_args` variable or settings).
