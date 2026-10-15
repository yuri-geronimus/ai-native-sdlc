---
name: ai-native-sdlc
description: Runs software work through the AI-native SDLC loop - intent.md, spec.md, plan.md, build, verify, PR review, gated release, and monitoring that feeds new intent - with human approval gates and git-committed evidence. Load it before starting the work itself, even when the task looks small enough to just do - a feature idea, ticket, bug report or security finding to turn into work, including a bug to fix test-first; a request to plan a change, refactor or migration before touching any code, which produces plan.md; writing, reviewing or approving intent.md, spec.md, plan.md, REVIEW.md or bands.yaml; setting up CLAUDE.md, hooks, subagents, CI evals, AI PR review or the @claude fix loop; checking whether a CLAUDE.md, hook, skill or model change broke the agent or made it worse; preparing a release or rollback; judging whether a metric jump or error-rate spike after a deploy is real or noise and what to do; or how to run agentic development with an audit trail, governance or metrics - even if they never say SDLC.
argument-hint: "[status|init|intent|spec|plan|build|fix|verify|review|ship|diagnose|evals|metrics] [change-id|pr-number|version]"
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/sdlc.py status *) Bash(python3 "${CLAUDE_SKILL_DIR}/scripts/sdlc.py" status *) Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/sdlc.py validate *) Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/sdlc.py new *) Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/sdlc.py present *) Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/sdlc.py log *) Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/sdlc.py trace *) Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/sdlc.py drift *) Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/sdlc.py metrics *) Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/sdlc.py activate *) Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/bands.py check *) Bash(python3 .claude/skills/ai-native-sdlc/scripts/sdlc.py status *) Bash(python3 .claude/skills/ai-native-sdlc/scripts/sdlc.py validate *) Bash(python3 .claude/skills/ai-native-sdlc/scripts/sdlc.py present *) Bash(python3 .claude/skills/ai-native-sdlc/scripts/sdlc.py trace *) Bash(python3 .claude/skills/ai-native-sdlc/scripts/sdlc.py drift *)
compatibility: Claude Code 2.1.269 or later; git and python3 (3.9+) on PATH; gh optional
metadata:
  version: "0.1.0-alpha.1"
  source: "Anthropic, The AI-native SDLC playbook (2026)"
---

# AI-native SDLC

**Alpha release:** behaviour and file formats may change between versions.

Work moves through six stages: Plan, Design, Build, Test, Deploy, Maintain. Each stage ends by committing an artifact, and the next stage starts by reading it. Humans approve at gates. The agent does everything up to the production gate and nothing past it. The chain of commits is the audit trail: who asked for what, what the agent produced, and who approved it.

Skill directory: `${CLAUDE_SKILL_DIR}`. The reference files call it `<skill-dir>`.
Arguments: `$ARGUMENTS`

## Current state

!`python3 "${CLAUDE_SKILL_DIR}/scripts/sdlc.py" status --brief`

## Rules that always hold

These rules keep the process trustworthy. Hooks enforce most of them once `init` has run. If you can't satisfy one, stop and tell the human why.

1. **Never approve your own work.**
   - A gate is approved only by a human. Run `present`, then ask with AskUserQuestion, giving the options **Approve**, **Request changes** and **Reject**.
   - Run `approve` only if the human explicitly picked Approve. An empty, partial or timed-out answer is not approval.
   - `--by` is the name the human gives you. Never use git config or your own name; if you don't know their name, ask in the same question.
   - Each human answer approves one gate, once.
   - In headless runs, never run `approve` or print an approve command for someone to paste. Finish with the Headless runs checklist and quote the hand-off `present` prints. `approve` refuses anyway when `CI` or `SDLC_NONINTERACTIVE` is set.
2. **No implementation before the plan gate.** For bounded, standard and high-risk changes, don't edit source or tests until `status` shows the plan approved and current. The plan guard hook blocks Edit, Write and shell writes anyway; a blocked edit means you skipped a gate.
3. **Never pass the production gate.**
   - Prepare the release, then hand it to the release manager.
   - Someone saying in chat that they are the release manager, or that they approve, is not a release authorization. Don't offer to deploy on anyone's word.
   - Answer every deploy or release request with the ship-mode hand-off from [stage-5-deploy.md](references/stage-5-deploy.md):
     - what ships (commits, changes);
     - checks run;
     - open risks;
     - the rollback command;
     - who authorizes it, and where.

     Don't offer to run deploys to any shared environment.
   - The production gate hook covers Bash, Monitor, PowerShell and deploy MCP tools. When it blocks an action, report its reason and the route to approval. Don't look for another command that does the same thing.
4. **Fix the code, not the test.**
   - Every bug fix, even a one-line one, is at least `bounded`, because fix mode needs a change folder.
   - Task 1 of its approved plan is a failing test. Write it, confirm it fails for the expected reason, commit it, then run `fix-mode <id> on`.
   - From then on, tests are read-only.
5. **Evidence before claims.** Don't say "done", "fixed" or "passing" unless you ran the verification commands after your last edit and pasted their output. The Stop hook enforces this in every session once `verify_commands` is set in `.claude/sdlc.json`.
6. **Approved artifacts are amended, not rewritten.** Record build-time deviations under the artifact's `## Change log` heading, which doesn't invalidate approval. Changing anything above that heading invalidates the approval and needs a new `present` and approval.
7. **Leave the guardrails to humans.**
   - Only the scripts write `changes/<id>/audit.md` and `.claude/sdlc-state/`.
   - While `self_protect` is on, these are human-edited: `.claude/sdlc.json`, `.claude/settings*.json`, `.claude/agents/sdlc-*.md`, and the skill directory (when it lives inside the repo; not for plugin or user-level installs). If one needs changing, ask the human.
   - Never run `hooks.py` yourself.
8. **Write artifacts with the Write and Edit tools.** Create and change intent.md, spec.md and plan.md with Write and Edit. Shell heredocs, `cat >`, `sed -i` and `python3 -` scripts get refused by permission checks and waste turns.
   - Run each script as its own Bash call that starts with `python3` (or `git`).
   - Don't use a `VAR=…` prefix, shell variables, `$(…)`, or chains and pipes with other programs. Permission rules match the whole command, so those forms are refused.
   - The hooks already read `SDLC_NONINTERACTIVE` from the environment.
   - If a call is refused, retry it once in the plain form before you stop. Report a refusal as a permission rule, not a human decision.

## Step 1 — Pick the mode

Use the first argument if one was given. Otherwise infer the mode from the request and the current state above.

| Mode | When | Do | Read |
|---|---|---|---|
| `status` | no clear request, or "where are we" | `sdlc.py status [<id>]`, then explain the next action | — |
| `init` | first use in a repo (status says no config) | `sdlc.py init --dry-run`, show the plan, run `init` (add `--claude-md` for the routing line) only after the human agrees | [adoption.md](references/adoption.md) |
| `intent` | idea, ticket, bug report, incident, scan finding | Right-size, then brainstorm and write intent.md (Stage 1, Plan) | [stage-1-plan.md](references/stage-1-plan.md) |
| `spec` | intent approved (standard or high-risk) | Write spec.md under the policy skills, with concerns flagged (Stage 2, Design) | [stage-2-design.md](references/stage-2-design.md) |
| `plan` | spec approved, or a bounded change with intent.md written | Interactive: plan mode, then `plan-from-accepted`. Headless or high-risk: write plan.md from the template, then `present` | [stage-3-build.md](references/stage-3-build.md) |
| `build` | plan approved and current | Build procedure below | [stage-3-build.md](references/stage-3-build.md) |
| `fix` | a bug, with or without a reproduction | `new <slug> --bug --tier bounded [--source ticket\|incident\|scan --record-id ID]` → intent gate if the source isn't `human` → plan with T1 = failing test → plan gate → write, confirm and commit the test → `fix-mode <id> on` → fix the code only → verify → `drift <id> --strict` → `fix-mode <id> off` | [stage-4-test.md](references/stage-4-test.md) |
| `verify` | before reporting done | Run the verify commands, then the `sdlc-verifier` subagent | [stage-4-test.md](references/stage-4-test.md) |
| `evals` | CLAUDE.md, a skill, a hook or the model changed; set up CI evals | `run_evals.py` against real tasks | [stage-4-test.md](references/stage-4-test.md) |
| `review` | a PR or diff to review, review comments to address, or babysitting Claude's own PR to green | Bugs, Security and Compliance passes per REVIEW.md; on Claude's own PR, run the babysit-to-green checklist | [stage-5-deploy.md](references/stage-5-deploy.md) |
| `ship` | PR merged or ready to release, or any request to deploy, push to prod or "release now" | Prepare the release and send the ship-mode hand-off message; never deploy to prod | [stage-5-deploy.md](references/stage-5-deploy.md) |
| `diagnose` | alert, metric jump, error spike, incident, "real or noise?" | `bands.py check`, then a read-only diagnosis written as intent.md | [stage-6-maintain.md](references/stage-6-maintain.md) |
| `metrics` | "is this working", lead time, survival rate | `sdlc.py metrics` | [metrics.md](references/metrics.md) |

Where to read for setup and advice requests:
- CLAUDE.md, policy skills or build-time hooks: [stage-3-guardrails.md](references/stage-3-guardrails.md).
- Parallel sessions and subagents: [stage-3-build.md](references/stage-3-build.md).
- AI PR review, the @claude fix loop, release gates, autonomy tiers and rollback: [stage-5-deploy.md](references/stage-5-deploy.md), and [automation.md](references/automation.md) for the workflow files.
- Recurring scans and on-call: [stage-6-maintain.md](references/stage-6-maintain.md).
- "How do we start", and which plays to adopt first: [adoption.md](references/adoption.md).
- Governance, audit, managed settings and source of truth: [governance.md](references/governance.md).

## Step 2 — Right-size new work

Classify new work only: `intent`, `fix`, or `plan`/`build` when there is no change folder yet. Modes that act on an existing change read the tier from `status`. State the tier and the reason in one line, for example: *"Treating this as **bounded** (single endpoint, existing tests cover it): short intent.md and plan.md, one gate (plan). Say if you want it heavier."* Let the human override.

| Tier | Signals | Artifacts | Gates (approver) |
|---|---|---|---|
| `trivial` | typo, copy, formatting, comment, config hygiene; no behaviour change | none; verify and open a PR (`new` refuses trivial) | merge only (code owner) |
| `bounded` | one component, clear fix or small feature, no new data, auth or API surface, easy to revert; most bug fixes | intent.md (short) and plan.md; spec skipped | plan (engineer); plus intent when the intent is agent-written (ticket, incident, scan, review) |
| `standard` | new behaviour, several components, user-visible or cross-team change | intent.md, spec.md, plan.md | intent (product owner), spec (product owner), plan (engineer) |
| `high-risk` | auth, payments, PII, migrations, infra, regulated code, public API, anything hard to roll back | as standard, plus security and compliance passes and a rehearsed rollback | intent (product owner); spec (product owner **and** tech lead or architect); plan (tech lead or architect) |

The tier only moves up. If hidden complexity appears mid-task, run `sdlc.py raise-tier <id> <tier> --reason "..."` and redo the missing gates. Raising a change to high-risk voids spec and plan approvals that no longer meet the role rule, so present those gates again. Never lower a tier; if it looks over-classified, ask the human.

## Headless runs (CI, `claude -p`, loops)

You are headless when the prompt comes from a workflow or script, `SDLC_NONINTERACTIVE` or `CI` is set, or nobody can answer.
- Ask nothing. Record what you would have asked as assumptions (see Asking questions). A spec may carry at most 3 `[NEEDS CLARIFICATION: …]` markers.
- Never run `approve` or `reject`. For a change created headless, use `new … --no-activate`.
- Finish with this checklist:
  ```
  - [ ] sdlc.py new <slug> … --no-activate, then git switch -c change/<id> using the id it printed
  - [ ] Write the artifact(s) with Write/Edit, then sdlc.py validate <id> --strict
  - [ ] sdlc.py present <id> <gate>, and quote its hand-off line in your final message
  - [ ] git add changes/<id>, commit (trailer "Change: <id>"), push and open the artifact PR; if there is no remote, say so
  ```
- The gate is a code owner's review of that **artifact PR**, before any build. The approver then mirrors the approval into audit.md from an interactive session: they say "approve <gate> for <id>, PR <url>" there, and the agent runs `approve` with `--by` set to their name and `--evidence` naming the PR. The plan guard and CI read only audit.md.
- A review of the implementation PR never satisfies the intent, spec or plan gate.
- Any automation that launches `claude -p` in a repo with these hooks must set `SDLC_NONINTERACTIVE=1` in the job environment (never as a command prefix), so its prompt isn't recorded as a human turn. Pre-approve every verify and Proof command with `--allowedTools`, or the verifier can't run them ([automation.md](references/automation.md)).

## The loop

| Stage | Starts from | Commits | Gate → what it unlocks |
|---|---|---|---|
| 1 Plan | an idea, ticket, incident or scan finding | `changes/<id>/intent.md` | intent → Design |
| 2 Design | approved intent.md | `spec.md` (FR/SC IDs, policy conformance, flagged concerns) | spec → plan mode |
| 3 Build | approved spec.md, or for bounded the written intent.md | `plan.md`, then the diff and its tests | plan → implementation |
| 4 Test | every edit | verification output and the eval suite | evidence attached to the PR |
| 5 Deploy | open PR | review findings and approvals in the PR | merge (code owner), then release (release manager) |
| 6 Maintain | band breach, alert, ticket, scan | a new intent.md (`--source incident\|scan\|ticket`) | back to Stage 1 |

## Gate protocol

Copy this checklist for every gate (interactive sessions):

```
- [ ] On branch change/<id>? If not: git switch -c change/<id> (from the default branch) before the first artifact commit
- [ ] sdlc.py present <id> <gate>        → runs strict validation; fix every ERROR it lists, then present again
- [ ] AskUserQuestion: "Approve <gate> for <id>?" (Approve / Request changes / Reject), with the 2-4 points that most need their judgment
- [ ] Approve  → sdlc.py approve <id> <gate> --by "<their name>" --role <role>
      Request changes → edit the artifact, then present again
      Reject (only if they chose Reject) → sdlc.py reject <id> <gate> --by "<name>" --reason "..."  (for intent this drops the change)
- [ ] Commit the artifact (repo commit conventions, trailer "Change: <id>")
```

- **If AskUserQuestion isn't available,** ask in plain text and end your turn. The human's next message is the event.
- **Already presented:** running `present` again on an unchanged artifact keeps the approval window open. If the human's message already approves the gate, run `approve` next.
- **`--evidence`** is only for mirroring a PR approval, or for repos without the hooks.
- **Plan gate through plan mode:** after the engineer accepts the plan, run `sdlc.py plan-from-accepted <id> --by "<engineer>"` instead of present and approve. For a high-risk change it records only the presentation; a tech lead or architect still approves.
- **Roles:**
  - intent: `product-owner`;
  - spec: `product-owner`;
  - high-risk spec: two approvals on the same fingerprint, each after its own answer: `--role product-owner`, and `--role tech-lead` or `architect`;
  - plan: `engineer`;
  - high-risk plan: `tech-lead` or `architect`.
- **Flagged concerns:** a named policy owner resolves each one before the spec is presented.

## Asking questions

Ask at most five questions per artifact, one at a time. Ask only what changes the artifact. Give a recommended answer with each question so "yes" is enough. Record anything you would otherwise ask about as an assumption and move on:
- feature intent: `## Assumptions`;
- bug intent: `## Open questions`;
- spec.md: `### Assumptions` under `## Clarifications`.

Write the answers back into the artifact (`## Clarifications` in spec.md).

## Build procedure (plan approved)

```
- [ ] status shows the plan gate approved and current; you are on branch change/<id>
- [ ] Work in plan order; tasks marked [P] touch disjoint files and may run in parallel worktree sessions (run sdlc.py activate <id> in each)
- [ ] After each task, run the verify commands; fix failures before moving on
- [ ] Deviation from plan.md → add a line under its "## Change log" and run sdlc.py log <id> RULING --message "<what> — <why> — <cost if wrong>"
- [ ] Final check: delegate to the sdlc-verifier subagent (fresh context, report only), then sdlc.py log <id> VERIFIED --message "<commands and result>"
- [ ] sdlc.py drift <id> and sdlc.py trace <id>: explain every unplanned file and every GAP
- [ ] Local review, only when `review.mode` in .claude/sdlc.json is not "off": "isolated" → `sdlc.py review-local <id>` (separate clean-context `claude -p` per pass, writes changes/<id>/review.md); "session" → `review <id>` with subagents. Confirm each finding against the code, fix the real ones, rerun verify
- [ ] Commit with trailer "Change: <id>"; open a PR whose body links intent/spec/plan and pastes the verification output
- [ ] sdlc.py log <id> PR_OPENED url=<pr-url>
```

Keep working through the approved plan without stopping for confirmation. Stop and ask only for:
- an irreversible action;
- a security-sensitive action;
- a side effect outside the repo or worktree;
- a plan so wrong that every path forward is a guess.

## Scripts

Run them; don't read them. Every command prints the next step, using the same absolute `python3 ${CLAUDE_SKILL_DIR}/scripts/…` path as this table.

| Command | Purpose |
|---|---|
| `python3 ${CLAUDE_SKILL_DIR}/scripts/sdlc.py status [<id>] [--json]` | Stage, gates, next action |
| `… sdlc.py new <slug> --title "…" --tier <tier> [--bug] [--source …] [--record-id ID] [--no-activate]` | Create `changes/<id>/` with intent.md (active unless `--no-activate`) |
| `… sdlc.py validate <id> [<artifact>] [--strict]` | Lint an artifact against its template contract |
| `… sdlc.py present <id> <gate>` / `approve` / `reject` | Gate protocol (fingerprinted, human answer required) |
| `… sdlc.py plan-from-accepted <id> --by NAME` (required; never git config) | Turn a plan accepted in plan mode into plan.md and record the gate |
| `… sdlc.py fix-mode <id> on\|off` | Make tests read-only during a bug fix (after the failing test is committed) |
| `… sdlc.py raise-tier <id> <tier> --reason "…"` / `skip <id> <stage> --reason "…"` | Ratchet the tier up; record a deliberately skipped stage |
| `… sdlc.py log <id> RULING\|VERIFIED\|NOTE\|PR_OPENED …` | Append to the audit log |
| `… sdlc.py drift <id>` / `trace <id>` | Diff vs plan.md files; FR/SC coverage in plan and tests |
| `… sdlc.py activate <id>` / `close <id> --status done\|dropped --pr URL` | Set the active change; close it |
| `… sdlc.py metrics` | Lead times, survival and rework from git history |
| `… sdlc.py init [--dry-run] [--claude-md] [--with-ci]` | Install config, hooks, subagents; `--claude-md` adds the CLAUDE.md routing block |
| `… sdlc.py deactivate` | Clear the active change |
| `… sdlc.py package-plugin --out DIR [--author NAME]` | Package skill, subagents and hooks as an org plugin (for managed settings with `allowManagedHooksOnly`; see [governance.md](references/governance.md)) |
| `python3 ${CLAUDE_SKILL_DIR}/scripts/bands.py check\|respond --config bands.yaml --series data.json` | Deterministic control-band detection and tiered response |
| `python3 ${CLAUDE_SKILL_DIR}/scripts/run_evals.py --threshold 0.9` | Regression-test CLAUDE.md, skills and hooks on real tasks (paid `claude -p` runs; asks first) |

Gate decisions (`approve`, `reject`, `fix-mode`, `raise-tier`, `skip`, `plan-from-accepted`, `init`) are deliberately not in this skill's allowed-tools. Unless your own settings allow `Bash(python3 *)`, Claude Code asks before running them, which is a second check. The recorded-human-turn check in `approve` applies either way. Templates live in [assets/templates/](assets/templates/), and subagents (`sdlc-verifier`, `sdlc-reviewer`, `sdlc-gate`) in [assets/agents/](assets/agents/).

## When something is missing

- **No `.claude/sdlc.json`, or hooks not installed.** The process still works, but nothing enforces the rules. Say so once and offer `init`. Without the hooks, `approve` requires `--evidence` with the human's words. With the hooks installed, it needs a recorded human turn that reads as an approval, `--evidence` or not. It refuses while `human-events.jsonl` is missing, until the human sends a prompt.
- **The Current state block shows the command text, an execution-disabled notice, or nothing.** Run `python3 ${CLAUDE_SKILL_DIR}/scripts/sdlc.py status` yourself before choosing a mode. If python3 isn't on PATH, say so: the scripts and hooks need it.
- **Subagents not installed** (`.claude/agents/sdlc-*.md` missing, or added this session, which needs a restart; in a plugin install they ship with the plugin instead). Don't skip the fresh-context check:
  - for `sdlc-reviewer` and `sdlc-gate`, use the built-in read-only `Explore` agent;
  - for `sdlc-verifier`, use `general-purpose` and tell it never to edit files;
  - in both cases, give it the body of `${CLAUDE_SKILL_DIR}/assets/agents/<name>.md` plus the inputs, and none of your conclusions.
- **No test command.** Stage 4 is blocked. Propose a single command that exits non-zero on failure, add it to CLAUDE.md and to `verify_commands` in `.claude/sdlc.json`, and let the human confirm.
- **An existing tracker (Jira, ServiceNow) holds the record.** Pass `--record-id` and follow the source-of-truth rules in [governance.md](references/governance.md).

## Glossary

- **Stage:** one of the six playbook stages. Stage 1 is called Plan but produces intent.md; plan.md belongs to Build.
- **Artifact:** a committed file one stage hands to the next (intent.md, spec.md, plan.md, the PR, the incident record).
- **Gate:** a human approval point: intent, spec and plan (recorded in audit.md), then merge and release.
- **Merge gate:** code-owner approval of the PR, enforced by branch protection and CODEOWNERS. The references also call it the review gate or PR gate; it's the same gate.
- **Production gate:** the `hooks.py` guard that denies the agent production actions. It backs up the release gate. It isn't a human approval point; a named release manager authorizes the release in the release pipeline.
- **Change:** one unit of work, kept in one folder, `changes/<id>/`.
- **Active change:** the change this session works on; the hooks guard it.
- **Tier:** how much ceremony a change gets: trivial, bounded, standard or high-risk.
- **Band:** a metric's control limits (1σ, 2σ, 3σ) from a rolling baseline.
- **Autonomy tier:** how far the agent may act per environment: dev free, staging gated, prod never.
