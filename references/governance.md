# Governance: audit trail, gates, separation of duties, managed settings

Use this file when someone asks who approved what, how a gate is enforced, how the process fits a regulated organization, or how it coexists with Jira, ServiceNow or a requirements tool. The governing idea from the playbook: the old control objectives stay, the enforcement changes. Every stage commits an artifact, and the chain of commits is the audit trail: who asked for what, what the agent produced, and who approved it. The agent acts up to the production gate and cannot pass it. Local scripts and hooks make the process hard to skip by accident. Git-side controls (branch protection, CODEOWNERS, protected environments) and managed settings make it hard to skip on purpose. Know which layer you are relying on.

## Contents
- The artifact chain as audit trail
- Gate table
- Fingerprinted approvals and human presence
- What each layer actually stops
- Separation of duties and the GitHub settings that enforce it
- Legacy systems and the source of truth
- Managed settings for a regulated organization (line by line)
- Packaging the guardrail hooks as an org plugin
- Shadow mode for new AI reviewers and sampling automated approvals
- Exporting decisions to the SIEM (OpenTelemetry)
- Pitfalls

---

## The artifact chain as audit trail

| Audit question | Where the answer lives | How to read it |
|---|---|---|
| Who asked, and why | `changes/<id>/intent.md` frontmatter `author`, `source`, `record_id`, `created`, plus the commit that added it | `git log --diff-filter=A --format='%h %an %aI' -- changes/<id>/intent.md` |
| What was decided | `spec.md` (requirements, flagged concerns and their resolution, `skills` in force), `plan.md` (files, order, risks, proof, rollback) | `python3 <skill-dir>/scripts/sdlc.py status <id> --json` |
| What the agent produced | Commits carrying the `Co-Authored-By: Claude …` trailer and `Change: <id>`; the session transcript in the OTel export | `git log --grep='Change: <id>' --format='%h %an %aI %s%n%(trailers)'` |
| Under which instructions | CLAUDE.md, skills and hooks at the commit the work was based on, all versioned | `git log -1 --format=%H <base> -- CLAUDE.md .claude/` |
| Who approved each gate | `changes/<id>/audit.md` `GATE_APPROVED` lines (sha256, by, role, evidence); PR reviews; environment approvals | `cat changes/<id>/audit.md`; `gh pr view <n> --json reviews,mergedBy,mergedAt`; `gh api repos/O/R/actions/runs/<run>/approvals` |
| What was found and fixed in review | PR review threads, check runs, `@claude` fix commits | `gh pr view <n> --comments`; `gh api repos/O/R/pulls/<n>/comments` |
| What happened in production | Incident record, `bands-log.jsonl`, the incident-sourced intent.md | `grep -l '^source: incident' changes/*/intent.md` |

Audit lines are one event per line, written only by `sdlc.py` (the pre-tool hook denies agent edits to `audit.md` and `.claude/sdlc-state/`):

```
- 2026-10-08T19:40:00Z | GATE_APPROVED | gate=plan | sha256=3f2a…(64 hex) | by="J. Ortiz" | role=engineer | evidence="yes, go ahead"
```

The playbook also asks that "the spec, the prompt that produced it, and the skill versions in force" are logged. `spec.md` frontmatter carries `skills` (with versions). Put the prompt in the spec PR body, or record it with `python3 <skill-dir>/scripts/sdlc.py log <id> NOTE --message "spec prompt: …"`.

## Gate table

Required gates per tier: `trivial` none (merge only); `bounded` plan, plus intent when the intent is agent-written (`source` ticket, incident, scan or review); `standard` and `high-risk` intent, spec and plan. Every bug fix is at least `bounded`. `merge` and `release` apply to every tier.

| Gate | Artifact | Approver role | Evidence | Where logged | Enforced by |
|---|---|---|---|---|---|
| `intent` | `changes/<id>/intent.md` | product owner | `GATE_APPROVED` with fingerprint and the human's words; or the merged intent PR | audit.md, git history, PR | `sdlc.py approve` (strict validation, fingerprint, presence); plan-guard hook blocks source edits until approved; CODEOWNERS on `changes/**/intent.md` + branch protection; `sdlc-artifacts.yml` runs `validate --strict` |
| `spec` | `spec.md` | product owner; for high-risk, two approvals on the same fingerprint, each after its own human answer: product owner and tech lead or architect; each flagged concern resolved with its named policy owner | `GATE_APPROVED` (two lines for high-risk); Flagged concerns table with no `open` row; no `[NEEDS CLARIFICATION]` | audit.md, git, PR | `validate --strict` (refuses open concerns and markers); for high-risk, `approve` and `status` require both a `product-owner` and a `tech-lead`/`architect` approval on the same fingerprint before the gate is current; plan guard; CODEOWNERS |
| `plan` | `plan.md` | engineer; tech lead or architect for high-risk | Plan-mode acceptance (`presence=plan-mode`, via `plan-from-accepted`) or `GATE_APPROVED` | audit.md, git | Plan mode itself (no edits until accepted); plan-guard hook; `drift --strict` in CI compares the diff with `## Files that change` |
| `merge` | the PR | code owner: a human who is neither the author nor the agent | Approving review after the most recent push; review findings resolved; required checks green | PR history (the PR is the audit record) | Branch protection or ruleset, CODEOWNERS, required status checks (see settings list below) |
| `release` | production deployment | release manager | GitHub Environment `production` approval, or an approved change ticket | Deployment log, `hook-log.jsonl`, OTel `tool_decision` | Production-gate hook (fails closed; checks `production_gate.approval_check`); environment required reviewers; no production credentials in agent jobs |

Controls that are not gates but are logged the same way: `FIX_MODE_ON/OFF` (tests read-only during a fix), `TIER_RAISED` (one-way ratchet), `STAGE_SKIPPED` (with a reason), `RULING` (in-plan decisions), `VERIFIED`.

## Fingerprinted approvals and human presence

Copy this when explaining or auditing an approval:

```
- [ ] Fingerprint = sha256 of the artifact body above "## Change log" (frontmatter excluded)
- [ ] GATE_PRESENTED sha == GATE_APPROVED sha == current sha  → approval is current
- [ ] A human event (prompt, AskUserQuestion answer, plan-mode accept) exists after GATE_PRESENTED
- [ ] presence=unverified on the line → hooks were not installed; the evidence text is the only proof
- [ ] The same artifact merged through a PR approved by a code owner
```

- **Why fingerprints.** An approval is of specific bytes. Editing anything above `## Change log` changes the sha, `status` reports "artifact changed after approval: re-present", and the plan guard blocks source edits again. Frontmatter (status, approver) and Change log entries are excluded, so recording an approval or a build deviation does not invalidate it.
- **Why presence.** The UserPromptSubmit hook appends every prompt to `.claude/sdlc-state/human-events.jsonl`; the PostToolUse hook records AskUserQuestion answers and ExitPlanMode acceptance. `approve` requires a human event newer than the `GATE_PRESENTED` (and newer than any earlier decision on that gate) that no other decision in any change has used, so each human turn approves one gate once; an AskUserQuestion answer counts only if it picks Approve. It quotes the event as evidence. That stops an agent from presenting and approving in one breath. Presenting an unchanged artifact again adds no `GATE_PRESENTED` while the gate is still open, so an approval the human typed after the first presentation (for example after a headless hand-off) is not pushed out by the agent re-presenting. If the project hooks and a plugin copy of them both run, the duplicate record of a turn is ignored. The approved event is strictly later, not equal: timestamps are whole seconds.
- **Why the role is re-checked.** After `raise-tier` to high-risk, a spec or plan approved under the lower tier stays on record but is no longer current: the spec needs a product-owner and a tech-lead or architect approval on the same fingerprint, the plan a tech-lead or architect approval. `status` names the missing role, the gate is presented again, and the plan guard blocks source edits until the approvals are recorded.
- **Why `approve` refuses in headless runs.** No human types in a `claude -p` run, so `approve` refuses whenever `CI` or `SDLC_NONINTERACTIVE` is set, even with `--evidence`. In CI the gate is a PR review or an environment approval, never a script call. `--allow-ci` is an explicit override; treat any audit line written under it as needing a matching PR review.

### Honest limits

UserPromptSubmit carries no signal about *who* typed, and it cannot tell "approve" from "what does FR-002 mean?". A prompt passed to a local `claude -p` also fires the hook and counts as a human event; the skill's own headless callers (`bands.py respond`, `run_trigger_evals.py`) set `SDLC_NONINTERACTIVE=1` on the child process so their prompts are not recorded. Set it in any other script that drives `claude -p` in a working copy with pending gates.

- **Timed-out questions.** With `askUserQuestionTimeout` set, an unanswered AskUserQuestion closes on its own and submits whatever options were already selected, so an `ask_answer` event can exist with nobody at the keyboard. `approve` accepts an answer only if it picks Approve; still, leave the setting unset in repos with gates.
- **The agent's own shell could write presence.** `human-events.jsonl` is a plain file, so a Bash call could append a fake "approve" event. What now stands in the way: the pre-tool hook denies writes to `.claude/sdlc-state/` (file tools and the shell writes it detects) and denies shell commands that run `hooks.py` (so the agent cannot feed it a forged UserPromptSubmit payload); `approve` ignores events with no session id, events timestamped in the future, harness `<task-notification>` turns and events already used by another decision. What still gets through: writes the command-line parser cannot see, such as `python3 -c 'open(...).write(...)'`, `bash -c` strings or a script written elsewhere and then run. `sandbox.filesystem.denyWrite` on `.claude/sdlc-state/` would stop those, but it also breaks the `sdlc.py` commands the agent runs through Bash, which write there; so the real backstop is the code-owner PR review.
- **Local files.** The state files are writable by anyone with a shell. Treat the audit.md line as a fast, structured record of a decision, and the PR review by a code owner as the authoritative approval.

The skill's `allowed-tools` deliberately leaves out the gate decisions (`approve`, `reject`, `fix-mode`, `raise-tier`, `skip`, `plan-from-accepted`, `init`), so Claude Code asks the person at the keyboard before running them. A broad allow rule elsewhere (`Bash(python3 *sdlc.py *)` in project or managed settings) removes that click. Restore it with `ask` rules, which win over allow rules and are never auto-approved in any mode; the regulated asset ships them:

```json
{ "permissions": { "ask": ["Bash(python3 *sdlc.py* approve*)", "Bash(python3 *sdlc.py* reject*)", "Bash(python3 *sdlc.py* fix-mode*)",
  "Bash(python3 *sdlc.py* raise-tier*)", "Bash(python3 *sdlc.py* skip*)", "Bash(python3 *sdlc.py* plan-from-accepted*)",
  "Bash(python3 *sdlc.py* init*)"] } }
```

## What each layer actually stops

| Failure | Hooks and scripts | What actually stops it |
|---|---|---|
| Agent starts coding before the plan is approved | Plan guard denies Edit/Write and the shell writes it detects (a heuristic), with the next step | `sandbox.filesystem.denyWrite` for paths no command may write; `drift --strict` in CI |
| Agent edits the spec after approval | Fingerprint mismatch re-blocks build | — |
| Agent weakens a test during a fix | Fix-mode guard on Edit/Write and on write-ish Bash | CI diff check; `sandbox.filesystem.denyWrite` is the only layer that stops every Bash write |
| Agent "approves" after an unrelated human message | With or without `--evidence`, `approve` refuses a free-text turn without an approval word ("yes", "ok", "approve", "go ahead" …) and an AskUserQuestion answer that is not Approve; a question that merely contains "yes" still passes | The approve command is not pre-approved, so Claude Code asks first (keep the `ask` rules above); CODEOWNERS review of `changes/**` |
| The agent appends a forged event to `human-events.jsonl` from its shell | State-dir write guard and the `hooks.py` invocation deny; interpreter and script writes are missed (see Honest limits) | PR review by a code owner |
| Someone forges `audit.md` or `human-events.jsonl` by hand | Nothing (local files) | audit.md changes are visible in the PR diff; CODEOWNERS; signed commits |
| Hooks removed or `disableAllHooks` set locally | Nothing | `allowManagedHooksOnly` + the force-enabled org plugin (see Packaging the guardrail hooks as an org plugin); CI re-runs `validate --strict`, `trace`, `drift --strict` |
| Deploy through a command the regexes don't match | Missed | No production credentials in the agent's environment; protected environment; deploy only via scoped MCP tools |
| Hook timeout or crash | Production gate fails closed (and stays on with `hooks_enabled: false`; an unparseable `.claude/sdlc.json` denies every shell and MCP call); other guards fail open by design | Managed settings; CI |
| Agent switches off its own guardrails (edits `.claude/sdlc.json`, the hook wiring or this skill's scripts) | `self_protect` denies the edit | CODEOWNERS on `/.claude/`; managed `Edit(...)` deny rules for the same paths; `allowManagedHooksOnly` |

**`self_protect`** (`.claude/sdlc.json`, default `true`). The pre-tool hook denies agent writes (file tools and the Bash writes it detects; reads stay allowed) to the files that define the guardrails: `.claude/sdlc.json`, `.claude/settings.json`, `.claude/settings.local.json`, `.claude/agents/sdlc-*.md`, the skill's own directory (scripts included) when it sits inside the repo, and hard links to any of these. Why: every other guard (plan guard, fix mode, `protected_paths`, the production-gate patterns) is read from these files, so an agent that could rewrite them could turn the gates off in one edit. `sdlc.py init` still works because it is a script run by a person, not an agent edit. To turn it off, a human edits `.claude/sdlc.json` by hand and sets `"self_protect": false`, then commits it so the change shows in review. Like every hook rule it stops accidents, not abuse.

Hooks stop accidents. Branch protection, CODEOWNERS and managed settings stop abuse. Never sell the first as the second.

## Separation of duties and the GitHub settings that enforce it

The playbook's rule is that the agent that wrote the code has no way to approve it. That is a property of repository settings, not of agent design. Apply these on the default branch (ruleset or branch protection):

```
- [ ] Require a pull request before merging; at least 1 approval (2 where CODEOWNERS marks regulated paths)
- [ ] Require review from Code Owners (owners are human teams; the Claude GitHub App is never a code owner)
- [ ] Dismiss stale approvals when new commits are pushed
- [ ] Require approval of the most recent reviewable push (an @claude fix push re-requires approval; the last pusher cannot approve)
- [ ] Require conversation resolution (Important findings stay open threads until resolved)
- [ ] Required status checks: CI, sdlc-artifacts, agent-evals (see pitfall on path filters)
- [ ] Block force pushes and branch deletion; restrict direct pushes to nobody
- [ ] Bypass list empty or break-glass admins only; never the Claude App, never github-actions
- [ ] Actions → Workflow permissions: leave "Allow GitHub Actions to create and approve pull requests" off
- [ ] Workflows declare least-privilege permissions; review-only jobs get contents: read
- [ ] Environment "production": required reviewers = release managers, prevent self-review, deployment branches = default branch, production secrets only in this environment
- [ ] CODEOWNERS itself is owned by the platform team / tech leads
- [ ] Bot identities: the Claude GitHub App and the App behind vars.SDLC_APP_ID / secrets.SDLC_APP_PRIVATE_KEY (control-bands propose; contents and pull-requests write only) are never code owners or bypass actors, and their PRs need a human code-owner approval
```

Example CODEOWNERS (last matching line wins):

```
/changes/**/intent.md   @org/product-owners
/changes/**/spec.md     @org/product-owners
/changes/**/plan.md     @org/engineers
/CLAUDE.md              @org/tech-leads
/REVIEW.md              @org/tech-leads
/.claude/               @org/platform
/.claude/skills/secure-api-review/  @org/security
/.github/               @org/platform
/.github/CODEOWNERS     @org/platform
```

`control-bands.yml` opens its 3σ PRs with its own App because `GITHUB_TOKEN` cannot open PRs while the Actions setting above is off, and PRs it opens trigger no CI. Without the App it files an issue instead.

With CODEOWNERS on `changes/**`, the intent, spec and plan gates can also live entirely in git as PR reviews. That is the recommended authoritative form for regulated teams.

Roles and what each may not do:

| Role | Approves | Must not |
|---|---|---|
| Originator | — (writes intent with Claude) | Approve their own intent if they are also product owner of a high-risk change; get a second reviewer |
| Product owner | intent, spec | Write the spec (Claude writes it; the owner reviews it) |
| Policy owner | flagged concerns, skill changes | — |
| Engineer | plan (routine) | Approve the merge of their own PR |
| Tech lead / architect | plan and spec for high-risk; REVIEW.md, CLAUDE.md | — |
| Code owner | merge | Approve a push they made |
| Release manager | release | Prepare and authorize the same release alone |
| Agent (any session, the GitHub App, the Maintain loop) | nothing | Hold production credentials; approve; merge |

The Maintain loop follows the security companion article's principle: draw boundaries around access and actions, not around model instructions. The diagnosing agent runs read-only (`--tools` plus `dontAsk`); a deterministic wrapper writes intent.md and opens the PR (see references/automation.md).

## Legacy systems and the source of truth

For every artifact, name one system as the source of truth; everything else holds a copy or a link. Choose per artifact and record the choice in `.claude/sdlc.json`:

| `source_of_truth` | Meaning | Session obligations |
|---|---|---|
| `repo` (default) | Markdown in git is authoritative; the legacy system references files and commits | Put the commit or PR URL in the ticket. One timestamp authority: git |
| `legacy` | Jira / ServiceNow / the requirements tool is authoritative; markdown is a working copy | Read the record through its MCP connector at session start; write the outcome back through MCP in the same session that produced the spec or plan; log `sdlc.py log <id> NOTE --message "synced to <record url> at <sha>"` |
| `linkage` | Two sources, linked both ways. The minimum bar and the easiest start | Every artifact carries `record_id`; every legacy record carries the commit SHA of the markdown file |

- `source_of_truth` is either one string (`repo`, `legacy` or `linkage`) that applies to every artifact, or an object mapping artifact names to one of those values, for example `"source_of_truth": {"intent": "legacy", "spec": "legacy", "plan": "repo"}`. An artifact not in the map defaults to `repo`.
- `source_of_truth` documents the choice for people and CI; `sdlc.py` does not change its behaviour on it.
- Start changes with `python3 <skill-dir>/scripts/sdlc.py new <slug> --record-id EV-123`. The id becomes `EV-123-<slug>` and the frontmatter carries `record_id`.
- Set `record_id_pattern` to the tracker's ID format (a regex such as `^EV-\d+$`). `sdlc.py new --record-id` refuses an ID that does not fully match it. `new` without `--record-id` is still allowed, so add a CI step if every change must carry a record ID.
- SHA to write back: `git log -1 --format=%H -- changes/<id>/spec.md`.
- Auditors accept what they already accept. Keep the change board, regulatory traceability tool or Figma as the system of record where they expect it, and link to it. Don't fight it.

## Managed settings for a regulated organization (line by line)

Asset: [managed-settings.regulated.json](../assets/settings/managed-settings.regulated.json). It is a starting point to tailor, not a recommendation to copy. Every deny trades against capability, and the right balance depends on the data classification of the repo.

Deploy it where engineers cannot edit it: `/Library/Application Support/ClaudeCode/managed-settings.json` (macOS), `/etc/claude-code/managed-settings.json` (Linux/WSL), `C:\Program Files\ClaudeCode\managed-settings.json` (Windows), MDM (`com.anthropic.claudecode`), or server-managed settings from the admin console. Managed beats `--settings`, local, project and user settings. Allow and deny arrays merge across scopes, so developers can add rules but not remove managed ones. A managed file that does not parse stops Claude Code from starting. Confirm the result with `/status` ("Setting sources") and `claude doctor`.

| Key | What it buys, in control terms | Caveat |
|---|---|---|
| `permissions.deny`: `Read(.env*)`, `Read(./secrets/**)`, `WebFetch`, `Bash(curl *)`, `Bash(wget *)` | Keeps secrets out of the agent's context and blocks tool-level network egress. Deny wins in every mode, including bypass | A `Read` deny also blocks Edit/Write on the path. It does not stop a shell command; the sandbox does |
| `permissions.deny`: `Bash(git push --force *)`, `Bash(git push -f *)` | History on shared branches can't be rewritten by the agent, so the commit chain stays an audit trail | Branch protection blocks force pushes server-side too; this stops attempts earlier |
| `permissions.deny`: `Bash(terraform apply *)`, `Bash(terraform destroy *)` | Infrastructure changes go through the pipeline and its release gate, never a session | Add the org's other prod CLIs (`kubectl`, `helm`, cloud CLIs) to match the production-gate patterns in `.claude/sdlc.json` |
| `permissions.deny`: `Edit(**/.claude/sdlc-state/**)`, `Edit(**/changes/*/audit.md)` | Managed backstop for the hook rule that only `sdlc.py` writes the audit log and runtime state | Covers the file tools only; a shell redirect is caught by the pre-tool hook, not by this rule. To back `self_protect` the same way, add `Edit(**/.claude/sdlc.json)`, `Edit(**/.claude/settings*.json)` and `Edit(**/.claude/agents/sdlc-*.md)` |
| `permissions.allow`: `Bash(git *)`, `make build/test/lint`, `Bash(python3 *ai-native-sdlc/scripts/sdlc.py*)` and the same for `bands.py`, `run_evals.py` | Pre-approves the inner loop so the deny list doesn't turn into prompt fatigue | Under `allowManagedPermissionRulesOnly` this is the only allow list, because skill `allowed-tools` is ignored (v2.1.282+). It must cover the skill's calls, including the quoted `!` status injection (`sdlc.py" status`, hence `sdlc.py*` and not `sdlc.py *`) and the plugin install path. With `autoAllowBashIfSandboxed: true` sandboxed Bash runs without a prompt anyway, so the sandbox is the control and this list matters mainly when auto-allow is off. Neither rule is a boundary: the wildcard after `python3` also matches `python3 -c '<code>' …/sdlc.py` (arbitrary Python), and `Bash(git *)` allows `git -c core.fsmonitor=<program>`, aliases and `git push` (branch protection stops pushes to main). If you set auto-allow to `false`, use per-subcommand git rules (status, diff, log, add, commit, switch) and pin the script rules to an absolute path where the install location is fixed; otherwise rely on the sandbox plus a managed PreToolUse hook that rejects `python3 -c` and `git -c`/`-C`. A `Bash(git -c *)` deny alone is not enough |
| `permissions.ask`: `sdlc.py` `approve`, `reject`, `fix-mode`, `raise-tier`, `skip`, `plan-from-accepted`, `init` | Keeps the second check SKILL.md relies on: every gate decision needs a click from the person at the keyboard even though the allow row above matches `sdlc.py*` | Ask beats allow (checked on 2.1.294: with the allow row above, `approve` was denied under `dontAsk` while `status` ran) and is never auto-approved, including under sandbox auto-allow. In `-p` an ask fails closed, which is right: CI never records gate decisions |
| `permissions.disableBypassPermissionsMode: "disable"` | Rejects `--dangerously-skip-permissions` and agent-frontmatter `bypassPermissions` | — |
| `permissions.disableAutoMode: "disable"` | Auto mode replaces rule-based approval with a classifier the organization has not assessed as a control. Disable it for regulated or critical repos | The playbook makes auto-accept the default for routine work once guardrails mature. Leave it enabled on non-regulated repos; record the decision per repo class |
| `allowManagedPermissionRulesOnly: true` | No engineer, project file, `--settings` or `--allowedTools` can widen the rules | CI recipes that pass `--allowedTools` stop working; put CI commands in the managed allow list |
| `sandbox.enabled`, `network.allowedDomains` | OS-level egress allowlist. A tool deny on WebFetch doesn't stop `python -c urllib…`; the sandbox does | Add your git host, package registries and internal mirrors, nothing else |
| `sandbox.autoAllowBashIfSandboxed: true` (the default, stated explicitly) | Sandboxed Bash runs without a prompt, so the sandbox boundary replaces per-command clicks; `deny` rules and content-scoped `ask` rules (the `sdlc.py approve` rule above) still apply | Only a bare `Bash` ask rule is skipped. Set `false` to send sandboxed commands through the normal permission flow |
| `sandbox.failIfUnavailable: true`, `allowUnsandboxedCommands: false` | Make the sandbox a gate: refuse to start without it; a command that fails inside cannot be retried outside | Test on every OS image you support |
| `sandbox.credentials` (`~/.ssh`, `~/.aws/credentials` deny; `GITHUB_TOKEN` env deny) | Closes the gap deny rules leave: sandboxed shell commands can't read key files, and named secrets are stripped from their environment | `mask` mode only from user, managed or `--settings` (v2.1.191+) |
| `allowManagedHooksOnly: true` | The approval gates are the only hooks that run; nothing local can add to or replace them | It also blocks this skill's project hooks from `sdlc.py init` and agent-frontmatter hooks (skill-frontmatter hooks are implied blocked, not verified). `/goal` stops working. Pair it with `enabledPlugins` |
| `enabledPlugins: {"<plugin>@<marketplace>": true}` | Force-enables the org plugin that ships these hooks, so the guardrails survive `allowManagedHooksOnly` | Matched on the full `plugin@marketplace` ID; a same-named plugin from another marketplace stays blocked |
| `disableSideloadFlags: true` | Every skill, agent, hook and MCP server arrived through the approved marketplace, never a home directory | Rejects `--agents`, `--mcp-config`, `--plugin-dir`, `--plugin-url` (v2.1.193+); CI recipes must use committed `.claude/agents/` and managed MCP |
| `strictKnownMarketplaces` | Only the organization's marketplace can supply plugins (`[]` = full lockdown) | — |
| `extraKnownMarketplaces` | Declares that marketplace from the managed tier so the force-enabled plugin can be installed without user action | Repository-declared marketplaces are ignored in untrusted folders and `-p`; declare it here |
| `allowManagedMcpServersOnly: true` + `allowedMcpServers` | The tool surface is an allowlist owned by the platform team | Set both. With `allowedMcpServers` unset, every server is allowed; `[]` allows none. The asset lists servers by `serverUrl` (a URL pattern with `*` wildcards; `https://api.githubcopilot.com/mcp/*` is GitHub's remote server), because `serverName` is a user-chosen label and not a security control. Once any `serverUrl` or `serverCommand` entry exists, servers of that transport must match one. With no `serverCommand` entry, as in the asset, every stdio server is blocked until you add it (exact command and arguments) |
| `requiredMinimumVersion: "2.1.282"` | Refuses to start below the assessed build, so the controls run on a version the organization tested | The playbook's 2.1.193 predates features this skill relies on: `--permission-prompts` (2.1.259), `plugin eval` (2.1.269), and the managed-only handling of skill `allowed-tools` (2.1.282) that the allow list above is written for. Never below 2.1.269. Checked at startup only |
| `env`: `CLAUDE_CODE_ENABLE_TELEMETRY`, `OTEL_METRICS_EXPORTER`/`OTEL_LOGS_EXPORTER` `otlp`, `OTEL_EXPORTER_OTLP_PROTOCOL`, `OTEL_EXPORTER_OTLP_ENDPOINT` | OTel export developers cannot switch off, so every tool decision and hook block reaches the SIEM (see the SIEM section) | Replace the placeholder endpoint; add `OTEL_EXPORTER_OTLP_HEADERS` (collector auth) and `OTEL_RESOURCE_ATTRIBUTES` as needed. `OTEL_*` in repo settings is ignored; it must be managed. `OTEL_LOG_TOOL_DETAILS` is left out of the asset; add it after the data-protection review |

Tailor before deploying:

```
- [ ] Replace make targets with the repo's real build/test/lint commands
- [ ] Set allowedDomains to the git host, registries and mirrors actually used
- [ ] Set the plugin@marketplace ID and marketplace repo to your organization's, and publish the plugin (next section)
- [ ] Replace the example serverUrl with the MCP servers actually used (serverUrl / serverCommand), including deploy tools scoped per environment
- [ ] Decide permissions.disableAutoMode per repo class and write the decision down
- [ ] Add sandbox.filesystem.denyWrite for paths no command may write (frozen packages, generated code); it is the only layer that stops Bash writes
- [ ] Point OTEL_EXPORTER_OTLP_ENDPOINT at your collector (plus OTEL_EXPORTER_OTLP_HEADERS if it needs auth); decide OTEL_LOG_TOOL_DETAILS with the data-protection owner
- [ ] Pilot on one machine per OS: claude doctor, /status, run a session, trigger each hook once (the plugin's plan guard must deny a source edit)
- [ ] Roll out through MDM or server-managed settings; keep the JSON in a reviewed repo
```

## Packaging the guardrail hooks as an org plugin

`allowManagedHooksOnly` blocks the project hooks that `sdlc.py init` writes into `.claude/settings.json`. Only managed hooks, SDK hooks and hooks from plugins force-enabled by managed `enabledPlugins` still run. On managed machines, ship the hooks as a plugin:

```
- [ ] python3 <skill-dir>/scripts/sdlc.py package-plugin --out <dir>   # --name to rename, --version to override
      → <dir>/.claude-plugin/plugin.json (name "ai-native-sdlc"), skills/ai-native-sdlc/ (no tests),
        agents/sdlc-*.md, hooks/hooks.json (same events, matchers and timeouts as
        assets/settings/project-settings.json; commands run "${CLAUDE_PLUGIN_ROOT}/skills/ai-native-sdlc/scripts/hooks.py")
- [ ] claude plugin validate <dir>
- [ ] Commit <dir> to the org marketplace repo (the asset's example-corp/approved-plugins) and add an
      entry named "ai-native-sdlc" to its .claude-plugin/marketplace.json, whose name is "approved-plugins"
- [ ] Managed settings: extraKnownMarketplaces key "approved-plugins", strictKnownMarketplaces with the same
      repo, enabledPlugins "ai-native-sdlc@approved-plugins": true
- [ ] Pilot: run the adoption.md smoke test on a managed machine; the plugin's plan guard must deny the edit
```

- **Names must match exactly.** The `enabledPlugins` key is `<plugin name>@<marketplace name>`: the plugin's `name` in `plugin.json` and the marketplace entry (`--name`, default `ai-native-sdlc`), then the `name` in `marketplace.json`, which is also the `extraKnownMarketplaces` key (`approved-plugins` in the asset). A plugin with the same name from another marketplace stays blocked.
- **Keep the project skill committed.** `/ai-native-sdlc`, the `claude-review.yml` prompt and the `.claude/skills/ai-native-sdlc/scripts/…` paths in the workflows depend on it, and CI runners usually have no managed settings. The plugin's copy appears as `ai-native-sdlc:ai-native-sdlc`.
- **One copy of the hooks per machine.** Where the plugin is enabled, the project hooks must not also run (`allowManagedHooksOnly` blocks them). Two copies would record every prompt twice, and each record can approve a gate.
- **Detection depends on which copy runs.** Run from the plugin's copy, `status` and `approve` know the hooks ship with the plugin, and `init` skips the `.claude/settings.json` hook merge. Run from the committed project copy, they look only at `.claude/settings*.json`: `status` may report "guard hooks not installed" while the plugin's hooks run, and `approve` falls back to `--evidence` if `human-events.jsonl` is missing. Trust the smoke test.
- **`self_protect` does not cover the plugin cache**, which sits outside the repo. The sandbox keeps Bash writes inside the working directory, and file-tool edits outside it prompt.

## Shadow mode for new AI reviewers and sampling automated approvals

From the security companion article: trust an automated control only after it has earned it, and keep checking it after.

- **Shadow mode.** A new AI reviewer (a new REVIEW.md pass, a new policy skill, the `sdlc-gate` confidence gate) posts findings that need human approval to count. Run it this way for at least two weeks or 50 PRs, rate each finding (valid / invalid / nit), and promote it only when the invalid rate is acceptable to the tech lead. Red-team it before promotion: open PRs with planted bugs and policy breaches and confirm it catches them.
- **Require proof.** A finding counts only with `file:line` and a concrete failure scenario (REVIEW.md "Verification bar").
- **Keep human review where risk demands it.** Tier code by risk (CODEOWNERS paths). Regulated or critical code keeps a human code owner regardless of review scores. Invariant tests (for example, one user can never read another user's data) force manual review when touched.
- **Sample automated approvals.** Each week, sample a risk-weighted share (default 10%, 100% of high-risk) of decisions no human made in the moment: plan-mode acceptances, `sdlc-gate` `continue` verdicts, scan dismissals, band `log` decisions, PRs merged with zero Important findings. Reviewer records agree/disagree; disagreements become eval cases.

```bash
grep -h 'GATE_APPROVED' changes/*/audit.md | grep 'presence=plan-mode' > approvals.txt
python3 -c 'import random,sys; L=open(sys.argv[1]).read().splitlines(); print("\n".join(random.sample(L, max(1, len(L)//10))))' approvals.txt
```

## Exporting decisions to the SIEM (OpenTelemetry)

Send every automated approval, tool call and agent-to-agent message to the SIEM, and treat agents as a potential insider. Set telemetry in the managed `env` block so it cannot be turned off. The asset ships the first five keys; the full form:

```json
{ "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_LOGS_EXPORTER": "otlp", "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "https://otel.internal.example.com",
    "OTEL_LOG_TOOL_DETAILS": "1",
    "OTEL_RESOURCE_ATTRIBUTES": "team=claims,service=portal" } }
```

- `OTEL_LOG_TOOL_DETAILS=1` adds `tool_parameters`, which you need to see *which* command a hook blocked. It also exports command lines; clear it with the data-protection owner. Prompt and response content stay off unless `OTEL_LOG_USER_PROMPTS` / `OTEL_LOG_ASSISTANT_RESPONSES` are set.
- Alert on: `claude_code.tool_decision` with `source=hook` and `decision=reject` (gate blocks); `claude_code.permission_mode_changed` (bypass attempts); `claude_code.hook_execution_complete` with `num_blocking > 0`; `claude_code.managed_settings_resolved` with `error.type`; unexpected `claude_code.mcp_server_connection`.
- Ship the repo-side records too. `audit.md` is in git already. `.claude/sdlc-state/hook-log.jsonl` and `bands-log.jsonl` are local; collect them with the log shipper on developer machines and upload them as CI artifacts in workflows.
- Claude Code emits raw events only. Baselines and alerting are the backend's job (the `bands.py` detector can consume an exported series).

## Pitfalls

- **Gates in prose only.** A frameworks survey documented agents writing code first and backfilling the plan. Every gate here has a hook, a script check or a GitHub setting behind it. Keep it that way.
- **Required checks with path filters.** A required workflow that only triggers on `CLAUDE.md` changes leaves other PRs pending forever. Make the job always run and no-op when paths don't match.
- **Treating `presence` as identity.** It proves a human turn happened, not who took it.
- **Copying the managed example verbatim.** `allowManagedHooksOnly` without `enabledPlugins` and a published plugin silently disables the skill's guardrails; `allowManagedMcpServersOnly` without `allowedMcpServers` restricts nothing.
- **Two sources of truth with no link.** Pick `repo`, `legacy` or `linkage` per artifact; an unlinked copy is an audit finding.
- **Agents with standing production credentials.** The production gate hook is a backstop. The control is that the credentials are not there.
