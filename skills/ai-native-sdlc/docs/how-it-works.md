# How it works

Agents now write code faster than the process around the code can absorb it. Planning, review, approval and deployment still move at human speed. Controls that assumed a person wrote every line — line-by-line review, weekly change boards — either become a queue or get skipped.

The playbook keeps the old control objectives and changes how they are enforced:

- Each stage hands over a committed artifact instead of a meeting or a ticket.
- Policy lives in skills and is applied while the work is done.
- Deterministic hooks back the rules that must always hold.
- Humans review what the agent flagged, at named gates.
- The chain of commits is the audit trail: who asked, what the agent produced, who approved it.

The playbook describes the plays and gives example snippets. This skill turns them into a working process for a repository: a router Claude follows, scripts that keep state and check artifacts, hooks that enforce the gates, and the CI wiring around them.

## What it does

| Stage | Claude produces | Gate (who approves) |
|---|---|---|
| 1 Plan | `changes/<id>/intent.md` from an idea, ticket, incident or scan finding | intent (product owner) |
| 2 Design | `spec.md`: FR/SC requirements, policy conformance, flagged concerns | spec (product owner; plus tech lead for high-risk) |
| 3 Build | `plan.md` from plan mode, then the diff and its tests | plan (engineer; tech lead for high-risk) |
| 4 Test | verification output; eval runs when agent config changes | evidence attached to the PR |
| 5 Deploy | PR with review findings; a release hand-off | merge (code owner), release (release manager) |
| 6 Maintain | a new intent.md when a metric breaches its control band | back to Stage 1 |

Not every change needs every gate. Each request is first classed into a tier:

| Tier | Example | Artifacts | Gates |
|---|---|---|---|
| trivial | typo, formatting | none | merge only |
| bounded | most bug fixes, small features | short intent.md and plan.md | plan |
| standard | new user-visible behaviour | intent.md, spec.md, plan.md | intent, spec, plan |
| high-risk | auth, payments, PII, migrations, infra | as standard, plus security and compliance passes | intent, spec, plan with senior approvers |

## What's in the folder

```
SKILL.md                  router Claude follows: rules, modes, tiers, gate protocol
references/               one file per stage, plus governance, automation, metrics, adoption
assets/templates/         intent, intent-bug, spec, plan, REVIEW.md, bands.yaml, sdlc.json, eval case
assets/agents/            sdlc-verifier, sdlc-reviewer, sdlc-gate subagents
assets/schemas/           JSON schemas for claude -p --json-schema
assets/github/            six workflows: artifact checks, AI review, @claude fix loop, evals, control bands, build triage
assets/settings/          project hooks block; managed-settings profile for regulated orgs
scripts/sdlc.py           changes, gates, audit log, drift/trace/metrics, init, package-plugin
scripts/hooks.py          guard hooks (one dispatcher for four events)
scripts/bands.py          control-band detection (Western Electric / Nelson) and tiered response
scripts/run_evals.py      regression-tests CLAUDE.md, skills and hooks on real tasks
evals/                    the skill's own trigger and behaviour evals
tests/                    unit tests for all scripts
```

## Requirements

- Claude Code 2.1.269 or later.
- git and python3 (3.9+) on PATH. Scripts use the standard library only.
- `gh` for the GitHub workflows and PR steps (optional locally).
