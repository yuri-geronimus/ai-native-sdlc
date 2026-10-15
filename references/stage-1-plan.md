# Stage 1 (Plan): capture intent as intent.md

Use this file to turn an idea, ticket, bug report, incident or scan finding into `changes/<id>/intent.md`, then take it through the `intent` gate. intent.md is a proto-spec in the originator's own words: what is wanted, why, and under which constraints. A product owner and an agent can both read it and act on it. Stage 1 is called Plan, but it produces intent.md. plan.md belongs to Build (see references/stage-3-build.md). This file covers the playbook play "Capture as intent.md", plus right-sizing, the question budget, claim tags, bug intents and the route for non-engineers.

## Contents
- What changes
- Prerequisites and the intent home
- Step 1: classify the tier out loud
- Step 2: capture an intent from a person
  - Question budget
  - Claim tags and assumptions
  - What goes in each section
- Bug intents
- Intents from tickets, incidents and scans
- Non-engineers: claude.ai or Cowork with a VCS connector
- The intent gate: product owner review
- Governance
- How to measure
- Worked example
- Pitfalls

---

## What changes

**Traditional.** An idea goes through backlog entries, user stories, story points and refinement meetings before anyone can act on it. Ownership changes hands at each step, so what reaches engineering is several steps removed from what the originator meant.

**AI-native.** The originator brainstorms with Claude and the result is committed as intent.md, in the originator's own terms. Repeated processes, such as the template and the interview, are encoded as skills. The product owner reviews and corrects the agent-written intent.md before it is committed, whatever its source.

## Prerequisites and the intent home

- **Prerequisites:** none. This play is a root of the adoption graph (see references/adoption.md).
- **Infrastructure:**
  - Claude access for people who are not engineers (claude.ai or Cowork).
  - The agreed templates: [intent.md](../assets/templates/intent.md) and [intent-bug.md](../assets/templates/intent-bug.md).
  - A version-controlled home for intent that the product owner watches.
- **The home is `changes/<id>/` in the product repo** (config key `changes_dir`). The playbook suggests an `intent/` folder. This skill keeps intent.md, spec.md, plan.md and audit.md together in one folder per change, so the artifact chain sits next to the code derived from it.
  - Use a dedicated intent repo only when intents routinely span many repositories.
  - In a monorepo, the home is just a directory.
- **One-time setup by the platform or engineering team:**
  - Run `python3 <skill-dir>/scripts/sdlc.py init` (see references/adoption.md).
  - Decide who can write to the home. Many contributors will come from outside engineering.
  - Add CODEOWNERS so that `changes/**/intent.md` requires the product owner's review.
- **Source of truth.** If Jira, ServiceNow or a requirements tool already holds the record, decide per artifact which system is authoritative before the first intent is written. Use `--record-id` for linkage. The rules are in references/governance.md.

## Step 1: classify the tier out loud

Classify before asking anything else. Ceremony must match risk: a typo that generates three markdown files teaches people to bypass the process.

The tiers, their signals, artifacts and gates are in the table in SKILL.md (Step 1). A `trivial` change gets no change folder; `new` refuses that tier.

Say the tier and its cost in one line, then let the human override:

> Treating this as **standard** (new portal behaviour, touches the tracking API): intent.md → spec.md → plan.md, 3 gates. Say "bounded" or "high-risk" to change it.

- **When in doubt, pick the higher tier.** One extra gate costs minutes. An under-classified change skips review exactly where it matters.
- **The tier is a one-way ratchet.** If hidden complexity shows up later, such as a new data flow or a permission change, run `python3 <skill-dir>/scripts/sdlc.py raise-tier <id> high-risk --reason "<what you found>"` and redo any gate the new tier requires.
  - The script refuses to lower a tier, so a change cannot quietly shed its review after the fact.
  - If a change looks over-classified, ask the human. A lower-tier replacement is a new change.

## Step 2: capture an intent from a person

Copy this checklist:

```
- [ ] Classify the tier and state it (Step 1)
- [ ] python3 <skill-dir>/scripts/sdlc.py new <slug> --title "<title>" --tier <tier> --source human [--record-id ID]
- [ ] Let the originator describe the problem in their own words; don't interrupt with a form
- [ ] Brainstorm until concrete: at most 5 analyst questions, one at a time, each with a recommended answer
- [ ] Fill every section of changes/<id>/intent.md with the Write and Edit tools; tag claims; list assumptions
- [ ] Show the draft; the originator corrects anything misunderstood
- [ ] python3 <skill-dir>/scripts/sdlc.py validate <id> intent   → fix ERRORs
- [ ] On branch change/<id> (git switch -c change/<id>); commit intent.md and audit.md (author and timestamp join the record)
- [ ] Intent gate, if the tier or source needs one: present → ask (Approve / Request changes / Reject) → approve or reject (below)
```

- **Create the change when the conversation starts, not when it ends.**
  - The `CHANGE_CREATED` timestamp approximates "first conversation", which the leading indicator measures.
  - `new` also makes the change active, so the hooks and status lines follow it.
  - The id is `YYYYMMDD-<slug>`, where the date is the UTC date of `created` (so a change opened just after local midnight east of UTC carries the previous day), or `<record-id>-<slug>` when `--record-id` is given.
- **No formal language is required from the originator.** Useful prompts for them: what they cannot do today, who is affected, what better looks like, what is out of scope.
- **"Concrete" means you can state five things without guessing:**
  - the problem, with evidence;
  - who is affected;
  - an observable outcome;
  - at least one constraint;
  - what is out of scope.
  Stop brainstorming when you can. Don't continue into design.
- **The template may be wrapped in an org skill.** A technical team member can encode the organization's template and interview as a skill, signed off by a lead (see the skills play in references/stage-3-guardrails.md). This file is the default when no such skill exists.
- **Replace every `{{placeholder}}`.** `validate` reports any leftover `{{` as an ERROR. HTML comments are guidance and may stay.

### Question budget

Unlimited questions turn an interview into a questionnaire. Originators abandon those, or answer carelessly.

- Ask **at most 5 questions per intent**, **one at a time**.
- Ask only questions whose answer changes the outcome, the scope or the tier.
- Give a **recommended answer** with each question, and the reason for it, so "yes" is a complete reply.
- Use the AskUserQuestion tool when it is available. When the hooks are installed, they record its answers as human events, which strengthens the gate evidence.
- If a question isn't worth asking, make an informed guess. Record it under `## Assumptions` and tag it `[assumption]`. The bug template (`intent-bug.md`) has no Assumptions section; there, list assumptions under `## Open questions`.
- If a question is still open when the budget runs out, put it under `## Open questions` with the person who can answer it.

Question format:

> **Q2 of at most 5:** Do third-party brokers need to see shipment status too?
> **Recommended:** No, customers only for now. Brokers use a different login and would widen the authentication scope. Reply "yes" to accept, or tell me otherwise.

### Claim tags and assumptions

Tag each substantive claim in Problem, Proposed outcome, Affected users and systems, and Constraints with where it came from. The tags let the product owner review assumptions first, and they tell Stage 2 which statements it may rely on.

| Tag | Meaning |
|---|---|
| `[user]` | The originator said it. |
| `[Q3]` | The answer to question 3. |
| `[source:<record-id>]` | Taken from a ticket, incident, alert or scan record. |
| `[policy:<skill-or-doc>]` | Required by a named policy. |
| `[assumption]` | Claude inferred it. It must also appear under `## Assumptions` (`## Open questions` in a bug intent). |

`validate` does not check tags; the product owner checks them at the gate. Treat a claim with no tag as an assumption.

### What goes in each section

The section headings are a contract with `validate`. Use them exactly as written.

| Section | Contents | Keep out |
|---|---|---|
| Problem | What can't be done today, who suffers, and evidence (numbers if known). Keep it under 250 words; `validate` warns above that. | solutions |
| Proposed outcome | Observable end state: what a user sees or can do, and what "better" looks like as a measure. | file names, endpoints, frameworks |
| Affected users and systems | Roles, teams, services and APIs touched. | — |
| Constraints | Non-negotiables (data, auth, budget, deadline, regulation), plus an `Out of scope:` line. | preferences presented as constraints |
| Assumptions | Every `[assumption]`, one per line, phrased so it can be confirmed or corrected. | — |
| Open questions | Real questions, each with an owner. "None" is allowed. | questions you could answer by reading the code |
| Change log | Append-only notes after approval (`- <date> <what> — <why>`). | — |

An intent is a proto-spec, not a spec. Requirements, EARS lines and design belong to Stage 2. If you start writing file names, stop.

## Bug intents

Use `--bug` to get [intent-bug.md](../assets/templates/intent-bug.md). Its sections are:

- **Current behavior:** `WHEN <condition> THEN the system <does the wrong thing>`.
- **Expected behavior:** `WHEN <condition> THEN the system SHALL <correct behaviour>`.
- **Unchanged behavior:** `WHEN <neighbouring condition> THEN the system SHALL CONTINUE TO <current correct behaviour>`.
  - List the two or three behaviours a careless fix would most likely break.
  - Each clause becomes a regression test in Stage 4.
- **Evidence:** reproduction steps, log lines, ticket or incident IDs, affected versions.
- **Affected users and systems**, **Constraints**, **Open questions** and **Change log**, as in a feature intent.

Every bug fix, even a one-line one, is at least `bounded`, because fix mode needs a change folder: `new <slug> --bug --tier bounded`. Data loss, a security impact or a regulated flow makes it `high-risk`. Task 1 of the approved plan is the failing test: write it after the plan gate, confirm it fails for the expected reason, commit it, then run `fix-mode <id> on`. The full procedure is in references/stage-4-test.md.

## Intents from tickets, incidents and scans

Every source produces the same artifact and goes through the same gate. The product owner reviews and corrects the agent-written intent before it is approved. Any source other than `human` carries the intent gate on every tier, `bounded` included; `status` lists it under the required gates.

| Source | Create with | Notes |
|---|---|---|
| Ticket | `python3 <skill-dir>/scripts/sdlc.py new <slug> --source ticket --record-id EV-123` | Read the record through its MCP connector. Tag claims `[source:EV-123]`. |
| Incident (control band, alert) | `bands.py respond` calls `new … --source incident --from-json <file> --no-activate` | Arrives as a draft holding the diagnosis. Never auto-approved (see references/stage-6-maintain.md). |
| Security scan | `new <slug> --source scan --record-id <finding-id>` | A one-PR fix is a bug fix: add `--bug --tier bounded` and use `fix` mode. |
| Review finding | `new <slug> --source review` | Out-of-scope issues found while reviewing another PR. |

Agent-written intents tag almost everything `[source:…]` or `[assumption]`, which tells the product owner where to look first. `--no-activate` keeps headless runs from hijacking the active change in someone's session. A headless run ends with `validate`, commits on branch `change/<id>` and opens an artifact PR; it never approves (SKILL.md, Headless runs).

## Non-engineers: claude.ai or Cowork with a VCS connector

Contributors without git experience never touch git. Claude commits the markdown for them through a connector to the version-control system (for example, GitHub).

One-time platform setup:

- [ ] Provide the intent template and interview to claude.ai or Cowork as an organization skill, signed off by a lead.
- [ ] Enable the GitHub connector for the product repo.
- [ ] Protect the default branch so connector writes land as pull requests. Add CODEOWNERS so `changes/**/intent.md` requires the product owner.

Per intent:

1. The originator brainstorms in claude.ai or Cowork. The same question budget and claim tags apply.
2. Claude writes `changes/<YYYYMMDD-slug>/intent.md` with the full frontmatter: `id`, `title`, `author`, `status: draft`, `tier`, `source: human`, `record_id`, `created`.
3. Claude commits it on branch `change/<id>` through the connector and opens a PR. The PR author and timestamp are the record.
4. The product owner reviews the PR. Approving and merging it is the intent gate in git. Closing it is a rejection.
5. The approval is mirrored into the audit log from an interactive Claude Code session (the plan guard and CI read only audit.md):
   - `python3 <skill-dir>/scripts/sdlc.py present <id> intent`
   - `python3 <skill-dir>/scripts/sdlc.py approve <id> intent --by "<product owner>" --role product-owner --evidence "PR <url> approved by <product owner>"`
   - `--by` is the product owner's name as the human in the session gives it. The evidence must cite the PR, because the product owner may not be the person typing in that session.

The scripts can't run in claude.ai, so the change folder has no audit.md until an engineer runs a command on it.

## The intent gate: product owner review

The product owner checks:

- [ ] The problem is stated in the originator's terms, with evidence.
- [ ] The outcome is observable, and it solves the stated problem.
- [ ] Affected users and systems are complete; the obvious neighbours are named.
- [ ] Every `[assumption]` is confirmed or corrected.
- [ ] Open questions are real and each has an owner.
- [ ] The tier is right.

Procedure: follow the Gate protocol in SKILL.md with gate `intent` and `--role product-owner`. What is specific to this gate:

- `present <id> intent` runs strict validation, records the fingerprint and prints the gate card. Show the card with the two to four points that most need judgment (usually the assumptions).
- Ask with AskUserQuestion, options **Approve**, **Request changes** and **Reject**. `approve` needs a human event recorded after `present` (the answer or the next prompt). Run it only when the human explicitly picked Approve; an empty, partial or timed-out answer is not approval, and neither is your own reading that the intent "looks fine".
- `--by` is the name the human gives you, never git config or your own name. Ask for it in the same question if you don't know it.
- **Request changes:** edit, `validate`, then `present` again. Any edit above `## Change log` changes the fingerprint and voids an earlier approval. Frontmatter is excluded from the fingerprint.
- **Reject**, only when the human picked it: `reject <id> intent --by "<name>" --reason "<why>"` also closes the change as dropped. Corrections are Request changes, not Reject.
- Commit intent.md and audit.md together.

In headless runs, never run `approve` or `reject`. The gate is a code owner's review of the artifact PR, mirrored into audit.md from an interactive session as in the non-engineer route above. `approve` also refuses to run when `CI` is set.

What approval unlocks:

- **Standard and high-risk:** Stage 2 (references/stage-2-design.md). When automated, merging the intent PR triggers the spec job (see references/automation.md).
- **Bounded:** a human-written (`source: human`) bounded intent has no intent gate; go straight to plan mode. An agent-written bounded intent (ticket, incident, scan, review) goes to plan mode only after its intent gate.

## Governance

| Control | Detail |
|---|---|
| Enforced | No spec before a current intent approval: `status` routes there, and the plan guard hook blocks source edits until every required gate is current (intent, spec and plan for standard and high-risk; intent and plan for an agent-written bounded change). An approval is bound to the sha256 of the frozen part of intent.md. |
| Evidence | The committed intent.md: author, timestamp and full revision history. `audit.md` adds `CHANGE_CREATED`, `GATE_PRESENTED`, and `GATE_APPROVED` or `GATE_REJECTED` with sha256, approver, role and the human's words. |
| Where logged | The git history of `changes/<id>/`, plus the merged or closed PR when the intent arrived through one. |
| Who approves | The product owner (`--role product-owner`), by name. On the PR routes (headless, connector) their decision is the review of the artifact PR, mirrored in `audit.md` from an interactive session. |
| Source of truth | One system per artifact (repo, legacy tool, or linkage through `record_id`). See references/governance.md. |

## How to measure

**Leading indicator: time from first conversation to committed intent.md.** It should fall from a multi-week elicitation cycle to hours.

```bash
ID=<id>
grep -m1 'CHANGE_CREATED' changes/$ID/audit.md | cut -d'|' -f1      # conversation start
git log --diff-filter=A --format=%aI -- changes/$ID/intent.md | tail -1   # first commit
python3 <skill-dir>/scripts/sdlc.py metrics --since <YYYY-MM-DD>   # medians and p90 across changes since the window start
```

**Lagging indicator 1: survival rate.** The share of intents the product owner accepts into Design rather than closes. `python3 <skill-dir>/scripts/sdlc.py metrics` prints it as `intent survival`:

- approved: an intent `GATE_APPROVED`;
- closed: an intent `GATE_REJECTED`, or `close --status dropped` (triage dismissals included);
- excluded: changes whose tier carries no intent gate (human-written bounded), reported as "without an intent gate", and open drafts.

**Lagging indicator 2: intent churn.** The number of intent.md commits dated after the first spec.md commit for the same change, read from the `metrics` output. Frequent late edits mean intents are approved before they are concrete.

## Worked example

The conversation:

> **Originator (support lead):** Customers keep phoning to ask where their shipment is.
>
> **Claude:** Treating this as **standard** (new portal behaviour, reads the tracking API): intent.md → spec.md → plan.md, 3 gates.
>
> **Q1 of at most 5:** Should customers see only their own shipments? **Recommended:** yes, scoped to the signed-in account.
>
> **Originator:** yes
>
> **Q2:** Do brokers need access? **Recommended:** no, not in this change.
>
> **Originator:** Not sure. Ask the partnerships lead.

The resulting `changes/20261008-shipment-status/intent.md`:

```markdown
---
id: 20261008-shipment-status
title: Shipment status self-service
author: R. Silva
status: draft
tier: standard
source: human
record_id: ""
created: 2026-10-08T09:12:00Z
---
## Problem
Customers phone support to ask where their shipment is [user]. Agents spend
roughly 40% of call time on status-only queries [user] (support dashboard).

## Proposed outcome
A signed-in customer sees each of their shipments' current status, next
milestone and estimated delivery date in the portal [user]. Status-only
calls fall measurably [user].

## Affected users and systems
Customers, support agents [user]; portal team, tracking-core API [assumption].

## Constraints
Customers see only their own shipments [Q1]. No new PII in the portal
session; existing sign-on only [policy:secure-api-review].
Out of scope: notifications, broker access, changing delivery dates [user].

## Assumptions
- tracking-core already exposes status and ETA per shipment [assumption]
- ETA accuracy is acceptable to show as-is [assumption]

## Open questions
- Do third-party brokers need access too? Owner: partnerships lead [Q2]

## Change log
```

## Pitfalls

- **Solutioning in the intent.** Endpoints, components and schemas belong to the spec and the plan. They anchor Design to an unreviewed choice.
- **Question floods.** More than five questions, or several at once, wears the originator out. Assume, tag and move on.
- **Untagged inference.** A guess written as fact passes review and becomes a requirement.
- **Approving on the agent's word.** Only a recorded human reply after `present` counts. In CI, only a PR review counts.
- **Editing after approval without re-presenting.** The plan guard then reports the approval as not current, and Build stalls.
- **Lowering the tier to save a gate.** The script refuses it on purpose. Ask the human, or open a new change.
- **Change folders for trivial work.** They add noise to the audit trail and to every metric.
- **An intent home nobody watches.** If the product owner doesn't receive the PR or notification, the leading indicator measures waiting, not capture.
