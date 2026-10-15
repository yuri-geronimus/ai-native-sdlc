# Stage 3 (Build): guardrails — CLAUDE.md, policy skills, hooks

Use this file to keep institutional knowledge and guardrails in files the agent reads and code the harness runs, so every Build session (references/stage-3-build.md) works inside them. It covers the playbook plays "The CLAUDE.md", "Skills as institutional knowledge" and "Hooks as build-time guardrails", plus a summary of the source-of-truth sidebar.

## Contents
- Play: CLAUDE.md
- Play: skills as institutional knowledge
- Play: hooks as build-time guardrails
- Sidebar: legacy systems and the source of truth
- Pitfalls

---

## Play: CLAUDE.md

**What changes.** Knowledge that sat in people's heads and on wikis becomes a file the agent reads at the start of every session. The whole team maintains it and updates it whenever a mistake recurs.

**Prerequisites.** None. Infrastructure: a repo, Claude Code, and one engineer who knows the codebase well.

**Steps.**

```
- [ ] Run /init; Claude drafts CLAUDE.md from the repo
- [ ] Cut it to what a new joiner needs on day one: build/test/lint commands, conventions that matter, things Claude gets wrong
- [ ] Add the verification block from ../assets/templates/CLAUDE-verification.md
- [ ] Add the routing block: python3 <skill-dir>/scripts/sdlc.py init --claude-md (below)
- [ ] Keep it under a page
- [ ] Commit it at the repo root; CODEOWNERS on CLAUDE.md
- [ ] Working rule: when Claude makes the same mistake twice, the correction goes into CLAUDE.md, as a PR
```

Claude reads all of CLAUDE.md at the start of every session, so stale lines cost context in every session. Keep it under a page. Corrections go in as PRs so that the agent evals run on them (references/stage-4-test.md).

The playbook's example:

```markdown
# Payments service

## Commands
- Build: make build
- Test: make test (unit), make itest (integration, needs docker)
- Lint: make lint (runs in CI; fix before pushing)

## Conventions
- Java 21, Spring Boot 3. No new Lombok.
- Money is always BigDecimal, never double.
- Every endpoint needs an integration test in src/itest.

## Architecture
- api/ holds REST controllers, core/ holds domain logic,
  adapters/ talks to external systems.
- Kafka events are defined in schemas/; never edit generated classes.

## Things Claude gets wrong
- Do not bump dependency versions; the platform team owns them.
- The legacy v1/ package is frozen; changes go in v2/.
```

Rules that must always hold ("never edit generated classes", "v1/ is frozen") also belong in `protected_paths` (hooks play below). CLAUDE.md alone is advisory.

**The routing block.** Without a line in CLAUDE.md, plain bug reports ("fix X") and deploy requests often don't load this skill, so nothing enforces gates, the failing test or verification. `init` prints the block under `--dry-run` and writes it only with `--claude-md` (it appends to CLAUDE.md, or creates it; a re-run skips an existing block):

```markdown
## How work is done here

- For any feature idea, bug report, ticket, incident, deploy/release request or "fix X" request, invoke the `ai-native-sdlc` skill first and follow it (it right-sizes trivial edits to no ceremony).
```

Changing CLAUDE.md changes agent behaviour, so run the evals on that PR (references/stage-4-test.md).

**Governance.** CLAUDE.md is version controlled, so the instructions the agent works to are reviewable and auditable. Code owners approve changes in PR review, and changes are logged in git history.

**How to measure.**
- **Leading:** how often Claude repeats a mistake CLAUDE.md should have caught. Commit each correction as `docs(claude-md): <rule>` and count:
  ```bash
  git log --since=90.days --format='%aI %s' -- CLAUDE.md
  ```
  A rule re-added or reworded repeatedly means it isn't working. Promote it to a hook.
- **Lagging:** time to first merged PR for a new team member:
  ```bash
  gh pr list --author <login> --state merged --limit 200 --json mergedAt --jq 'map(.mergedAt)|min'
  ```
  Compare it with their start date.

---

## Play: skills as institutional knowledge

**What changes.** Policy that used to be applied inconsistently from documents becomes explicit, version-controlled instructions. They are applied broadly and updated centrally when the policy changes.

**Prerequisites.** None. A CLAUDE.md helps. Infrastructure: one policy with a named owner and a written source of truth.

**Where knowledge goes.** Write a skill for institutional knowledge that must be applied consistently. Don't write a skill for content that belongs in CLAUDE.md or in a prompt.

| Put it in | When | Example |
|---|---|---|
| Skill | Must be applied consistently, has a policy owner, spans repos or tasks, loads only when relevant | API security standard, brand rules, compliance check |
| CLAUDE.md | Repo facts every session needs | commands, layout, recurring mistakes |
| Prompt | This task only | "keep the old endpoint for one release" |
| Hook or CI check | Must hold without exception | protected paths, no secrets in the diff |

**Steps.**

```
- [ ] Pick one piece of knowledge enforced inconsistently today (security standard, API convention, brand rule)
- [ ] An engineer writes it as .claude/skills/<name>/SKILL.md from the policy owner's source of truth, with Claude's help
- [ ] Frontmatter: description says when it triggers; metadata records owner, version, source
- [ ] Ship it in the repo (.claude/skills/<name>/) or org-wide through a plugin
- [ ] Test that it triggers (below)
- [ ] CODEOWNERS: the policy owner signs off every change to the skill folder
- [ ] Back every must-hold rule with a hook or a REVIEW.md pass
```

When the policy changes, change the skill and have the policy owner sign off. Engineers pick up the new version in their next session. Edits to a project SKILL.md are also picked up live within a running session.

The playbook's example, with the metadata the spec stage records (`.claude/skills/secure-api-review/SKILL.md`):

```markdown
---
name: secure-api-review
description: Apply the API security standard. Use whenever creating or
  modifying an external-facing endpoint, reviewing API code, or
  generating an OpenAPI spec.
metadata:
  owner: "S. Patel (AppSec)"
  version: "1.4.0"
  source: "https://wiki.example.com/appsec/api-standard"
---
# Secure API review

When you create or change an API endpoint:
1. Authentication: every endpoint requires the gateway JWT;
   no anonymous routes outside /health.
2. Input validation: validate request bodies against the OpenAPI
   schema and reject unknown fields.
3. Audit: every state-changing endpoint emits an audit event with
   actor, action, entity and timestamp.
4. Data classification: fields tagged pii in the schema must never
   appear in logs or error messages.

Run scripts/check-endpoints.sh and include its output in your summary.
```

**Trigger testing.** Ask for the relevant task in at least three phrasings, plus two near-miss requests that must not trigger it, and confirm the skill loads each time. Headless and read-only:

```bash
SDLC_NONINTERACTIVE=1 claude -p "add a PATCH /orders/{id} endpoint" --permission-mode dontAsk --tools "Read,Grep,Glob,Skill" \
  --output-format stream-json --verbose | grep -c '"skill": *"secure-api-review"'
```

- 0 means the skill did not fire. Sharpen the description with the words users actually say.
- Keep `Skill` in the `--tools` list. Without it no skill can fire, and the count is always 0.
- For repeated runs and near-misses, use `python3 <skill-dir>/evals/run_trigger_evals.py --skill-name secure-api-review --evals <queries.json>` with the format of [trigger-evals.json](../evals/trigger-evals.json). It reports a trigger rate per query. Add `--clean` in CI, and `--trace-dir <dir>` to see why a query missed.
- For a skill packaged as a plugin, `claude plugin eval` runs trigger evals. It loads no project config, so it doesn't replace the repo evals in references/stage-4-test.md.

**Advisory compared with deterministic.** A skill is a control, but an advisory one. It makes Claude likely to apply the policy, and nothing forces a session to comply.
- Back a rule that must always hold with a hook that blocks the action, a CI script, or a REVIEW.md pass that re-checks the policy at the PR.
- In the example, `check-endpoints.sh` should also run in CI, and rule 4 belongs in REVIEW.md's Security pass.
- The skill makes violations rare; the hook makes them close to impossible.

**Governance.**
- Skill invocations are logged in session traces (OTel `skill_activated` event).
- The policy owner reviews skill changes like code.
- spec.md records the skill versions in force (references/stage-2-design.md).

**How to measure.**
- **Leading:** time from the policy owner approving a policy change to the updated skill merging. Put `Policy approved: <date>` in the skill PR body, then:
  ```bash
  gh pr list --state merged --search "secure-api-review in:title" --json number,mergedAt,body
  ```
- **Lagging:** PR review findings that cite the policy. They should fall towards zero. If they don't, either the skill isn't triggering or its text has drifted from the policy.
  ```bash
  gh api --paginate 'repos/{owner}/{repo}/pulls/comments?since=<YYYY-MM-DD>T00:00:00Z' --jq '.[]|select(.body|test("secure-api-review|API security standard"))|.created_at[:7]' | sort | uniq -c
  ```
  The date is the start of the measurement window.

---

## Play: hooks as build-time guardrails

**What changes.** A skill is advisory; a hook is the deterministic layer behind it. Most of Claude's actions during implementation are file edits and shell commands, so build-phase hooks fire most often.

**Prerequisites.** `python3 <skill-dir>/scripts/sdlc.py init`, which merges the handlers into `.claude/settings.json`.

**What the installed hooks do.**

| Need | Mechanism |
|---|---|
| Block edits to protected paths (generated classes, a frozen package) | `protected_paths` in `.claude/sdlc.json`. The pre-tool hook denies Edit and Write, and Bash write commands that name those paths, with exit 2 and a reason. `audit.md` and `.claude/sdlc-state/` are always protected. |
| Run the formatter and linter after edits | `post_edit` entries run on the changed file only. A failure returns its output to Claude (exit 2). Write `{file}` unquoted: the hook passes the path as `"$SDLC_FILE"` in the environment, never in the command text, so a hostile file name can't run code. |
| Keep credentials out of the diff | A `post_edit` entry running the org's secret scanner on `{file}`, plus `permissions.deny` for `Read(./.env)` and `Read(./secrets/**)`, plus the CI secret scan as the backstop. |
| No code before the plan | The plan guard: Edit, Write, NotebookEdit and write-ish shell commands (references/stage-3-build.md). |
| Tests read-only during fixes | Fix mode (references/stage-4-test.md). |

Example `.claude/sdlc.json` (the scanner command is an example; check your tool's CLI):

```json
{
  "protected_paths": ["src/generated/**", "v1/**"],
  "post_edit": [
    {"glob": "**/*.py", "run": "ruff format {file} && ruff check {file}"},
    {"glob": "**/*", "run": "detect-secrets-hook --baseline .secrets.baseline {file}"}
  ]
}
```

**Rules.**
- **Back every must-hold skill rule with a hook.**
- **Keep build-phase hooks fast and scoped to the file that changed.** They run on every matching action. The full test suite belongs at the commit or the PR.
- **Hooks that ask a human for approval belong with the Stage 5 gates** (the production gate in references/stage-5-deploy.md). An approval prompt during Build puts a person back on the critical path of every parallel session.
- **A timed-out hook fails open.** That is why every guard except the production gate is designed to fail open, and why CI re-checks the same rules.
- **Bash can bypass Edit-only rules.** For paths that must never be written, add `sandbox.filesystem.denyWrite` (see references/governance.md).
- **The guard configuration protects itself (`self_protect`, on by default).** The pre-tool hook denies agent edits (file tools and shell writes) to `.claude/sdlc.json`, `.claude/settings.json`, `.claude/settings.local.json`, `.claude/agents/sdlc-*.md` and the skill directory when it lives in the repo, so a session cannot set `hooks_enabled: false`, empty `protected_paths` or unhook itself. If one needs changing, the agent asks the human; people edit these files by hand. `sdlc.py init` still works because it is a script, not an agent edit. Turning it off (`"self_protect": false`) is a reviewed config change. Add other agent definitions to `protected_paths`, and give all of them a CODEOWNERS entry so a change needs a named reviewer.
- **`hooks_enabled: false` turns off the fail-open guards, not the production gate.** That gate keeps running, and an unparseable `.claude/sdlc.json` denies every shell and MCP call until it is fixed.
- **Claude Code runs the hooks.** The agent never runs `hooks.py` itself; a guard that blocks is answered by the gate it names, not by another command.
- **Under `allowManagedHooksOnly`, project hooks don't run.** Ship them in a force-enabled org plugin (see references/governance.md).

**Governance.**
- The hooks live in repo config, so they apply to every session and are reviewed like code.
- Every decision is appended to `.claude/sdlc-state/hook-log.jsonl` locally, and to OTel as `hook_execution_complete` events.

**How to measure.**
- **Leading:** blocks per week, by reason. They should be low and stable:
  ```bash
  python3 -c "import json,collections;c=collections.Counter(json.loads(l).get('reason','')[:60] for l in open('.claude/sdlc-state/hook-log.jsonl') if '\"deny\"' in l);print(*c.most_common(10),sep='\n')"
  ```
- **Lagging:** lint and format failures in CI on agent-authored PRs should fall towards zero.

---

## Sidebar: legacy systems and the source of truth

When Jira, a regulated requirements tool, Figma or a change board already holds these records, name one system per artifact as the source of truth (repo, legacy system, or linkage through `--record-id` / frontmatter `record_id`) and set `source_of_truth` in `.claude/sdlc.json`. The three configurations and the per-artifact choice are in references/governance.md.

---

## Pitfalls

- **A CLAUDE.md that grows into a wiki.** Past a page, stale lines cost every session.
- **Treating a skill as enforcement.** Advisory controls need a hook or review pass behind them.
- **Slow hooks.** Running the test suite on every edit stalls every session. Scope hooks to the changed file.
