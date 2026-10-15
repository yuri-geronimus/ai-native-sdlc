# Stage 3 (Build): plan mode, plan.md, auto mode, parallel sessions

Use this file to start implementation from an accepted plan and to keep building inside it. The rule of the stage: nothing is implemented without an accepted plan. plan.md is the Build artifact, written in plan mode at the start of this stage. Stage 1 (Plan) produces intent.md, not plan.md. This file covers the playbook plays "plan mode as the default starting point", "auto mode" and "Parallel sessions and subagents", plus continuous execution inside an approved plan.

CLAUDE.md, policy skills, hooks as build-time guardrails and the source-of-truth sidebar: references/stage-3-guardrails.md.

## Contents
- Play: plan mode as the default starting point
  - Writing the plan
  - Recording the plan gate (two routes)
  - The plan guard and deviations
  - Continuous execution: rulings and the four stop conditions
- Play: auto mode for routine work
- Play: parallel sessions and subagents
- Pitfalls

---

## Play: plan mode as the default starting point

**What changes.**
- **Traditional:** the engineer reads the design and starts coding. Which files change and which tests prove it stays in their head, so the first thing a reviewer sees is the finished diff, when rework is slow.
- **AI-native:** work starts with a written plan that Claude produces in plan mode, where it can read the codebase but change nothing. The engineer corrects the plan before any code is written, and the approved version is committed as plan.md for later stages to check against.

**Prerequisites.**
- The approved spec.md (standard and high-risk; `python3 <skill-dir>/scripts/sdlc.py status <id>` shows the intent and spec gates current) or, for a bounded change, the written intent.md. A bounded intent needs no gate when a human wrote it; an agent-written one (ticket, incident, scan, review) needs its intent gate current first.
- A CLAUDE.md helps.
- Infrastructure: Claude Code with access to the repository.

**Make plan mode the default.** Start with `claude --permission-mode plan`, use Shift+Tab, or prefix the prompt with `/plan`. To make it the repo default, set `"permissions": {"defaultMode": "plan"}` in `.claude/settings.json`. Project settings accept `plan`, but they ignore `auto` and `bypassPermissions`.

### Writing the plan

Copy this checklist:

```
- [ ] Give Claude intent.md and spec.md; ask for a plan naming the files that change, the order of work, and the tests that prove it
- [ ] Use the plan.md headings from ../assets/templates/plan.md (so nothing is missing at acceptance)
- [ ] Bug fix: T1 is the failing test (see "Bug-fix plans" below)
- [ ] Interrogate: what could this break? which step is riskiest? which options did you reject, and why?
- [ ] Iterate until an engineer who never saw this conversation could implement it from the plan alone
- [ ] Mark tasks [P] where they are parallel-safe; group them into waves
- [ ] Check the size budget (~900–1600 tokens); split if larger
- [ ] Record the plan gate (route a or b below); commit plan.md and audit.md before the first source edit
```

Prompt:

> Read changes/<id>/intent.md and spec.md. Produce an implementation plan using the headings Goal, Files that change, Order of work, Risks, Proof, Rollback, Review focus and Change log. Name every file that changes. Give an order of work as `- [ ] T1 …` tasks with `(FR-###)` references, marking `[P]` on tasks that touch disjoint files. Name the tests and commands that prove each requirement. Then tell me what this could break, which step is riskiest, and which alternatives you rejected.

**The zero-context bar.** A plan passes when it gives:
- exact paths, with line ranges where it matters;
- the interfaces each task consumes and produces;
- steps of a few minutes each (failing test → FAIL → implement → PASS → commit);
- the spec's global constraints, copied verbatim;
- a `Review focus` section listing the inputs and paths the reviewer should probe.

If the plan only makes sense together with the conversation, the context it depends on is lost the moment the session ends.

**Bug-fix plans.** A bug fix is at least `bounded` (`new <slug> --bug --tier bounded`). T1 is the failing test that reproduces the bug. After the plan gate: write it, run it, confirm it fails for the expected reason, commit it, then run `python3 <skill-dir>/scripts/sdlc.py fix-mode <id> on`. From then on tests are read-only; fix the code only (references/stage-4-test.md).

**Parallel-safe tasks and waves.**
- `[P]` means the task touches files no other task in its wave touches.
- Waves run in order. Tasks within a wave may run in parallel worktree sessions. Tasks that share files run one after another in one session.
- `(FR-001)` references let `python3 <skill-dir>/scripts/sdlc.py trace <id>` report GAPs (requirements with no task) and ORPHANs (tasks with no requirement).

**Size budget.** Keep a plan to roughly 900–1600 tokens, about 700–1200 words (`wc -w changes/<id>/plan.md`). Beyond that, the plan stops being reviewable and the implementing context degrades. Split by independently shippable goal: keep the first goal, and create follow-ups with `python3 <skill-dir>/scripts/sdlc.py new <slug> --tier <tier>`.

Example (continuing references/stage-1-plan.md):

```markdown
## Goal
Customers see shipment status, next milestone and ETA in the portal (FR-001..003).

## Files that change
- `tracking-api/routes/status.py` — GET /shipments/{id}/status behind gateway JWT
- `tracking-api/tests/test_status.py` (new)
- `portal/src/shipments/StatusPanel.tsx` (new)
- `portal/src/nav/menu.ts`

## Order of work
Wave 1
- [ ] T1 (FR-001, FR-002) Status endpoint with account scoping; tests first
Wave 2
- [ ] T2 [P] (FR-001, FR-003) StatusPanel against the endpoint; 60 s cache, stale banner
- [ ] T3 [P] Nav entry behind the `shipment-status` flag

## Risks
tracking-core rate-limits at 50 rps; the panel must cache. Riskiest step: T1 scoping (FR-002 leak).

## Proof
`make test` (test_status.py covers the four states and cross-account access); screenshot matches the approved mock.

## Rollback
Turn the `shipment-status` flag off; the endpoint is additive.

## Review focus
Cross-account IDs, tracking-core timeouts, missing ETA.

## Change log
```

### Recording the plan gate (two routes)

Plan mode itself enforces the order: Claude cannot edit files until the engineer accepts the plan. The audit log and the plan guard need that acceptance recorded.

**Route (a), accept in plan mode (default for interactive sessions).**
1. The engineer accepts the plan in the plan mode dialog ("Yes, and use auto mode", "Yes, auto-accept edits" or "Yes, manually approve edits"; Ctrl+G edits the plan first).
2. The PostToolUse hook on ExitPlanMode saves the accepted text to `.claude/sdlc-state/accepted-plan.md`.
3. Run `python3 <skill-dir>/scripts/sdlc.py plan-from-accepted <id> --by "<engineer>"` instead of present and approve. `--by` is the name the engineer gives you; always pass it rather than letting the script fall back to git config.
   - It writes `changes/<id>/plan.md` and records `GATE_PRESENTED` and `GATE_APPROVED` with `presence=plan-mode`.
   - If headings were missing, it appends them with "_Not covered in plan mode — fill in_" and warns.
   - Filling those in edits the frozen part of the plan, so the approval stops being current. Run `present` and ask again.
   - Using the template headings in plan mode avoids this round trip.

This route needs the hooks installed by `init`. It records no approver role, so for a high-risk change it records only the presentation; a tech lead or architect still approves with `approve <id> plan --by "<name>" --role tech-lead` (or `architect`) after their own answer.

**Route (b), write, present, approve (headless, high-risk, or no hooks).** Follow the Gate protocol in SKILL.md for gate `plan`.
1. Write `changes/<id>/plan.md` from [plan.md](../assets/templates/plan.md) with the Write tool. Set `spec_sha` and `intent_sha` from `status <id> --json`.
2. Run `python3 <skill-dir>/scripts/sdlc.py present <id> plan`. It runs strict validation first; `validate <id> plan --strict` is the same check without presenting.
3. Ask with AskUserQuestion, options Approve / Request changes / Reject. Continue only if the human explicitly picked Approve; on Request changes, revise and present again.
4. Run `python3 <skill-dir>/scripts/sdlc.py approve <id> plan --by "<their name>" --role engineer`. High-risk changes require `--role tech-lead` or `--role architect`.

Headless runs stop after step 2: `validate`, commit on branch `change/<id>`, open the artifact PR, and never approve. A code owner's review of that PR is the plan gate, mirrored into audit.md from an interactive session (`present`, then `approve --evidence "PR <url> approved by <name>"`). Until then the plan guard blocks implementation.

Commit plan.md and audit.md before implementing. The plan joins the audit trail, and the PR review (references/stage-5-deploy.md) checks the diff against it.

### The plan guard and deviations

- **The plan guard.** For an active change of tier bounded, standard or high-risk, the pre-tool hook blocks `Edit`, `Write` and `NotebookEdit`, and write-ish shell commands (Bash, Monitor, PowerShell: redirects, `tee`, `sed -i`, `cp`, `mv` and similar), on any path outside `changes/` and `plan_guard_exempt` until every required gate is current:
  - the plan approval;
  - for standard and high-risk changes, the intent and spec approvals;
  - for an agent-written bounded change, the intent approval.
  The deny message contains the exact next command. A blocked edit means a gate was skipped; don't look for another route.
  - Shell write detection is a heuristic (it can't see writes inside scripts or `python -c`). Only `sandbox.filesystem.denyWrite` stops every write (references/governance.md); CI's `drift --strict` is the backstop.
  - It fails open on internal errors, because the guard exists to catch skipped gates, not to stall work on a bug.
  - Turning it off (`"plan_guard": false`) or setting `"hooks_enabled": false` in `.claude/sdlc.json` is a human, reviewed config change: `self_protect` denies agent edits to that file. `hooks_enabled: false` turns off the fail-open guards only; the production gate stays on.
  - Never run `hooks.py` yourself; Claude Code runs it.
- **Deviations.** When implementation departs from the plan, update plan.md in the same commit:
  - **Small deviations** (an extra file, a reordered task): append `- <date> <what changed> — <why>` under `## Change log`. Text below that heading is outside the fingerprint, so the approval holds.
  - **A change of approach** (a different design, a new risk, a new interface): edit the plan body and present it again. That decision is the reason the gate exists.
- **Sync enforcement.** The post-tool hook reminds Claude once per file when it edits a file not listed in `Files that change`. `python3 <skill-dir>/scripts/sdlc.py drift <id>` lists unplanned and untouched files, and CI runs `drift --strict` on every PR (`sdlc-artifacts.yml`).

### Continuous execution: rulings and the four stop conditions

Inside an approved plan, don't stall on decisions the plan leaves open. Asking for confirmation at every fork puts a human back on the critical path. Make a ruling and log it:

```bash
python3 <skill-dir>/scripts/sdlc.py log <id> RULING --message "<what> — <why> — <cost if wrong>"
```

If a ruling changes the files or the order of work, also add a Change log line. Stop and ask only for:

1. **Irreversible actions:** deleting data, migrations on shared data, force-pushes, publishing artifacts.
2. **Security-sensitive actions:** authentication, authorization, secrets, cryptography or permissions beyond what the plan states.
3. **Side effects outside the repo or worktree:** shared environments, external services, other repositories, messages to people.
4. **A plan so broken that every path is a guess.** Explain why, propose a revision, and present the plan again.

If hidden complexity changes the tier, run `raise-tier` immediately. If the new tier needs gates that aren't current (for example, a tech lead on the plan), that counts as stop condition 4.

**Governance (plan mode play).**
- **Enforced:** design review happens before any code is generated, while changing course is still an edit to a document. Plan mode blocks edits until acceptance, and the plan guard blocks source edits until the approval is current and fingerprinted.
- **Evidence:** plan.md, its revisions, and `GATE_APPROVED` with `by`, `role` and `presence` in `audit.md`. Rulings are recorded as `RULING` lines.
- **Approver:** routine changes are approved by the engineer. Changes classed as higher risk go to a tech lead or architect.

**How to measure.**
- **Leading:** the share of changes that merge from the first implementation pass, and the time from plan approval to merged PR. Use `python3 <skill-dir>/scripts/sdlc.py metrics` for plan→PR and plan→close, and:
  ```bash
  gh pr view <n> --json reviews,mergedAt --jq '{rounds: [.reviews[]|select(.state=="CHANGES_REQUESTED")]|length, mergedAt}'
  ```
  zero rounds means a first-pass merge.
- **Lagging:** rework cycles per change (the same `rounds` figure, summed), and how often the merged diff still matches plan.md: the share of merged PRs whose final `drift --strict` check passed in `sdlc-artifacts.yml` (`gh pr checks <n>`).

---

## Play: auto mode for routine work

**What changes.** The engineer approves the plan, then Claude applies each change without a per-edit prompt, and a background classifier reviews each action. Attention moves from watching edits to reviewing artifacts after longer autonomous sessions. This enables parallelism across worktrees and is the basis of the closed loop in references/stage-6-maintain.md.

**Prerequisites.** Mature guardrails (references/stage-3-guardrails.md):
- a tuned CLAUDE.md;
- policy skills;
- hooks installed (`init`), including the plan guard;
- a test suite Claude can run (`verify_commands` set).

**When to use it.** Auto mode becomes the default for routine work: a tight spec.md or bounded intent, a small blast radius, and code the tests already cover. Keep manual edit approval for high-risk changes and for code without tests.

**Steps.**
1. Choose "Yes, and use auto mode" when accepting the plan, or switch with Shift+Tab.
2. Let the Stop hook enforce verification before "done".
3. Review the artifacts at the end: the diff, the verification output, the RULING lines and the drift report.

Notes:
- Newer Claude Code versions (v2.1.283+) start interactive sessions in auto mode, so starting in plan mode is a deliberate setting (above), not the default.
- Deny rules and hook denials (exit 2) still block in auto mode. A hook `ask` still prompts.
- Regulated orgs may disable auto mode through managed settings (`disableAutoMode`; see references/governance.md).

**Governance.** Every action still passes the hooks and permission rules in the repo. Mode switches are logged as OTel `permission_mode_changed` events.

**How to measure.**
- **Leading:** the share of routine changes completed in auto mode (OTel `permission_mode_changed`, `to_mode=auto`).
- **Lagging:** the rework rate of auto-mode changes compared with manual ones (`rounds` above). If auto-mode changes rework more, the guardrails are not mature yet.

---

## Play: parallel sessions and subagents

**What changes.**
- **Traditional:** one engineer works one task at a time and spends a large part of the day on builds, tests and reviews.
- **AI-native:** one engineer runs several Claude sessions at once, each in its own worktree on its own task. Repeated jobs become subagents with their own context and tool limits. The engineer's job shifts to orchestrating, and eventually to building and monitoring loops.
- **The two mechanisms:**
  - A parallel session is a full Claude Code instance in its own git worktree. It knows nothing of the others, and the engineer steering them is the only thing they share.
  - A subagent runs inside one session as a scoped helper with its own context window and tool limits.

**Prerequisites.**
- CLAUDE.md, because every session reads it (references/stage-3-guardrails.md).
- The Stage 4 feedback loop, so sessions can verify their own work.
- Infrastructure: a git repo, plus permission settings tuned so sessions don't wait on prompts for commands the org considers safe (project `permissions.allow` for the test and lint commands).

**Steps.**

```
- [ ] Split work using plan.md waves: [P] tasks in the same wave touch disjoint files; tasks that share files stay in one session, in order
- [ ] Commit the approved intent/spec/plan and audit.md first, so every worktree sees the current approvals
- [ ] One worktree per task: claude --worktree feature-auth (terminal 1), claude --worktree fix-rate-limit (terminal 2), or EnterWorktree inside a session
- [ ] In each worktree: python3 <skill-dir>/scripts/sdlc.py activate <id>
- [ ] Start with 2–3 sessions; add one only while your review keeps up
- [ ] Repeated jobs → subagents in .claude/agents/, checked into git
```

**Worktree notes.**
- `claude --worktree <name>` and the EnterWorktree tool create `.claude/worktrees/<name>` on its own branch.
- Hook state is per worktree: `.claude/sdlc-state/` is gitignored, and the hooks resolve the repo root from the session's cwd. Human events and the hook log stay in the worktree's own state.
- A linked worktree without its own active change inherits the main checkout's active change, read-only; the session context says so. Run `activate <id>` in each worktree to pin it.
- Complete gate approvals in the main checkout before fanning out, and commit them, so every worktree reads the same audit.md.
- If `changes/<id>/` is missing in a worktree, the plan guard denies source edits there. Merge the artifact commit into its branch first.
- Merge the wave branches back into the change branch before the final verification and `drift`.
- The practical ceiling is how many streams one person can review properly, not how many sessions the machine can run.

**Subagents.** `init` installs three into `.claude/agents/`. While `self_protect` is on, `.claude/agents/sdlc-*.md` is human-edited; ask the human for changes.

| Agent | Tools | Job |
|---|---|---|
| `sdlc-verifier` | Bash, Read | Fresh-context final check. Runs plan.md Proof and the verify commands, exercises the change and the two nearest flows, and reports. Never edits. |
| `sdlc-reviewer` | Read, Grep, Glob | One review pass (Bugs, Security or Compliance). It traces the code first and treats the plan as testimony to falsify. |
| `sdlc-gate` | Read, Grep, Glob | Independent `continue` or `escalate` verdict between headless stages. |

Add others from the playbook as they recur: a code simplifier that strips needless complexity after the main agent finishes, and a researcher that explores the codebase without flooding the main context. Each subagent has a name, a description of when to use it, and the tools it may touch. The playbook's verifier:

```markdown
---
name: verifier
description: Runs the app and checks the change works before the session
  reports done
tools: Bash, Read
---
Start the app with make run. Exercise the changed behavior and the two
nearest neighboring flows. Report what you ran, what you saw, and any
behavior that does not match plan.md. Do not fix anything; report only.
```

**Subagent constraints.**
- **Never set `isolation: worktree` on the verifier or the reviewer.** That worktree branches from the default branch, not from the session's HEAD, so it wouldn't see the code it is meant to check. Use it only for parallel Build workers.
- **Listing `Bash` removes Grep and Glob.** Read-only agents use `tools: Read, Grep, Glob` with no Bash.
- **`disallowedTools` with a specifier, such as `Bash(git push *)`, removes the whole tool.** Use permission deny rules for scoped denies.
- **Hooks in subagent frontmatter don't load in untrusted `claude -p` runs.** Keep guardrails in `.claude/settings.json`.

**Governance.**
- More sessions mean more output, so the controls must come from configuration in the repo. Hooks and permission settings there apply to every session and every worktree.
- What each session does is logged and attributed to the engineer who ran it (OTel `user.email`, `session.id`).

**How to measure.**
- **Leading:** concurrent sessions per engineer while review quality holds, from the OpenTelemetry export (`claude_code.session.count`, overlapping `session.id` per `user.email`). Also the share of the day spent steering rather than waiting (`active_time.total`).
- **Lagging:** changes merged per engineer per week, read alongside the rework rate:
  ```bash
  gh pr list --state merged --search "merged:>=<YYYY-MM-DD>" --limit 500 --json author --jq 'group_by(.author.login)|map({who:.[0].author.login,merged:length})'
  ```
  The date is the start of the measurement window. Read the result next to the `rounds` figure from the plan mode play.

---

## Pitfalls

- **Coding first and backfilling the plan.** This is why the plan guard exists. A denied edit means a gate was skipped, not that the hook is broken.
- **Accepting a plan with appended "fill in" headings and then editing them.** The edit voids the approval. Present the plan again.
- **Rewriting the plan body for small deviations.** Use the Change log. Rewrite the body only when the approach changes, and present it again.
- **Stalling on decisions the plan leaves open.** Rule, log and continue. Stop only for the four conditions.
- **Oversized plans.** Above about 1600 tokens, reviewers skim. Split the change.
- **More sessions than you can review.** Unreviewed output is not throughput.
