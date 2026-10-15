# Stage 2 (Design): intent.md to spec.md under the policy skills

Use this file to turn an approved intent.md into `changes/<id>/spec.md` in one working session, constrained by the organization's policy skills (brand, security, compliance, UX). Every concern Claude cannot resolve is flagged, and the product owner resolves each one with its named policy owner before engineering sees the spec. Requirements and design collapse into one session: policy is applied while the spec is written, instead of being found in a review weeks later. This file covers the playbook play "Requirements and design", plus EARS requirements, the question and clarification budget, a read-only consistency pass, and automating the pass. Stage 2 applies to the `standard` and `high-risk` tiers. `bounded` changes skip it.

## Contents
- What changes
- Prerequisites
- Procedure
- The prompt (the playbook's, and the one to use)
- Requirements in EARS
- Policy conformance and flagged concerns
- Front-end work: Claude Design to Claude Code
- The analyze pass (read-only)
- The spec gate
- Codify, then automate
- Governance
- How to measure
- Pitfalls

---

## What changes

**Traditional.** Requirements and design are separate phases run by separate teams. Analysts formalize the idea into requirements, and designers parse those back into a design. The separation exists for accountability, but it is slow and loses information.

**AI-native.** Both phases happen in one prompted session. Claude reads intent.md and produces a requirements and design spec, constrained by the organization's skills, with areas of concern flagged. The product owner reviews the spec but doesn't write it. The goal is a spec the engineering team can plan against, with its risks already named.

## Prerequisites

- **An approved intent.** `python3 <skill-dir>/scripts/sdlc.py status <id>` must show the intent gate approved and current. If the intent was edited after approval, it has to be presented again first. A spec built on an unapproved intent would carry the unreviewed claims forward.
- **Brand, security, compliance and UX policies written as skills, each with a named owner** (see the skills play in references/stage-3-guardrails.md).
  - If a policy area has no skill yet, proceed anyway.
  - Give that area a `Policy conformance` row with status `n/a` and a `Flagged concerns` row: "policy not encoded as a skill", owner = the policy owner, status `carried`.
- **Infrastructure:** a product owner with Claude access. No engineering skill is required.

## Procedure

Copy this checklist:

```
- [ ] python3 <skill-dir>/scripts/sdlc.py status <id> --json   → intent gate current; note gates.intent.sha
- [ ] Load the policy skills; record each with its version in spec.md frontmatter `skills`
- [ ] Set frontmatter `intent_sha` (the intent fingerprint) and `risk` (low|medium|high)
- [ ] Read the codebase read-only for integration points (no code edits in Design)
- [ ] Write spec.md from ../assets/templates/spec.md with the prompt below (Write and Edit tools, not shell heredocs)
- [ ] At most 5 questions, one at a time, each with a recommended answer; answers go under ## Clarifications
- [ ] At most 3 [NEEDS CLARIFICATION: …] markers; everything else is an informed guess, recorded
- [ ] Answer or carry forward every intent open question
- [ ] Analyze pass: spec vs intent (read-only); fix CRITICAL and HIGH findings
- [ ] Product owner resolves each flagged concern with its policy owner
- [ ] python3 <skill-dir>/scripts/sdlc.py validate <id> spec --strict   → fix ERRORs
- [ ] Commit spec.md next to intent.md on branch change/<id> (commit body: the prompt or command used, and the skill versions)
- [ ] Spec gate: present → ask (Approve / Request changes / Reject) → approve (twice for high-risk) or reject
```

**Skill versions in force.** Take `metadata.version` from each skill's SKILL.md frontmatter. If it has none, use the git short SHA of the skill folder: `git log -1 --format=%h -- .claude/skills/<name>`. For skills shipped in an org plugin, use the plugin version. Example frontmatter:

```yaml
skills: [secure-api-review@1.4.0, brand-voice@a1b2c3d, ux-standards@2.0.1]
```

The pair of files records what was asked for and what was decided. Recording the skill versions makes it possible to replay the decision against the policy that was in force at the time.

## The prompt (the playbook's, and the one to use)

The playbook's prompt, verbatim:

```
Read the attached intent.md and produce a requirements and design spec for integrating it into our existing codebase. Apply the skills available to you so the plan conforms to our brand guidelines, security policies and UX standards. Document the spec fully as spec.md, ready to hand to the engineering team. Describe clearly any areas of concern, especially where you cannot satisfy contradicting policies.
```

Use this version instead. It keeps the playbook's intent and adds the contract that `validate` and `trace` check:

```
Read changes/<id>/intent.md (approved; fingerprint <intent-sha>) and write
changes/<id>/spec.md from <skill-dir>/assets/templates/spec.md: a requirements
and design spec for integrating this change into our existing codebase.

- Apply every policy skill available to you (brand, security, compliance, UX).
  List each in frontmatter `skills` with its version and give each a row in
  "Policy conformance" (compliant | non-compliant | n/a, with a note).
- Requirements: FR-001, FR-002 … each with one EARS acceptance line
  (WHEN … THE SYSTEM SHALL …, IF … THEN THE SYSTEM SHALL …, WHILE …).
  Success criteria: SC-001 … measurable and technology-agnostic.
- Every Problem and Proposed-outcome claim in intent.md maps to an FR or SC,
  or is listed under "Out of scope". Add nothing the intent doesn't ask for.
- Answer or carry forward every open question from intent.md.
- Ask me at most 5 questions, one at a time, each with your recommended answer.
  Use at most 3 [NEEDS CLARIFICATION: …] markers; for anything else make an
  informed guess and record it under "Clarifications" as an assumption.
- Flag every area of concern in "Flagged concerns": contradicting policies,
  conflicts between policy and intent, unclear policy, regulated data. Name the
  policy and its owner. Do not resolve a policy contradiction yourself.
- Do not edit code. Read it only to find integration points and constraints.
Before presenting, run a read-only consistency pass of spec.md against
intent.md and report the findings.
```

## Requirements in EARS

EARS (Easy Approach to Requirements Syntax) makes each requirement testable and unambiguous. `trace` then links every FR and SC ID to plan tasks and tests.

| Pattern | Form |
|---|---|
| Ubiquitous | `THE SYSTEM SHALL <behaviour>` |
| Event | `WHEN <trigger> THE SYSTEM SHALL <behaviour>` |
| State | `WHILE <state> THE SYSTEM SHALL <behaviour>` |
| Unwanted | `IF <condition> THEN THE SYSTEM SHALL <behaviour>` |
| Optional | `WHERE <feature is enabled> THE SYSTEM SHALL <behaviour>` |
| Bug, unchanged behaviour | `WHEN <condition> THE SYSTEM SHALL CONTINUE TO <behaviour>` |

Rules:

- **IDs:** `FR-###` and `SC-###`, three digits, never reused. If a requirement is dropped, strike it through and keep its ID.
- **One behaviour per FR.** Each FR is independently testable, and each traces back to an intent claim.
- **Success criteria measure outcomes, not implementation.** Example: "status-only calls fall 30% within 60 days", not "uses Redis".
- **`validate` checks:**
  - no FR ID → ERROR;
  - an FR without "SHALL" → WARN;
  - more than 3 `[NEEDS CLARIFICATION` markers → ERROR;
  - any marker at all under `--strict`, which is what `present` uses → ERROR.
- **Clarifications** use the form `### Session YYYY-MM-DD`, then `- Q: … → A: …`. Assumptions are recorded under `### Assumptions` in the same section.

Example (continuing the Stage 1 example):

```markdown
## Requirements
- FR-001: WHEN a signed-in customer opens a shipment on their account THE SYSTEM SHALL show its current status, next milestone and estimated delivery date.
- FR-002: IF the shipment belongs to another account THEN THE SYSTEM SHALL respond as if it does not exist.
- FR-003: WHILE tracking-core is unavailable THE SYSTEM SHALL show the last known status with its timestamp.

## Success criteria
- SC-001: Status-only support calls fall by 30% within 60 days of launch.
- SC-002: 95% of status panel loads complete in under 1.5 s.
```

## Policy conformance and flagged concerns

The table formats are part of the template contract:

```markdown
## Policy conformance
| Policy / skill | Status | Notes |
|---|---|---|
| secure-api-review@1.4.0 | compliant | New route behind gateway JWT; audit event not needed (read-only) |
| ux-standards@2.0.1 | non-compliant | Standard requires live updates; see C-1 |

## Flagged concerns
| ID | Concern | Policy | Owner | Status | Resolution |
|---|---|---|---|---|---|
| C-1 | Live updates would exceed tracking-core rate limit | ux-standards | A. Novak (UX lead) | resolved | 2026-10-09 A. Novak: 60 s refresh acceptable for v1 |
```

- **Status values:** `open`, `resolved`, `accepted` (the owner accepts the risk), or `carried` (deliberately carried into the plan, with the owner's agreement). `present` refuses any `open` row.
- **Every `non-compliant` conformance row needs a concern row.**
- **Flag a concern for any of these:**
  - two policies contradict each other;
  - a policy contradicts the intent;
  - a policy is silent or ambiguous;
  - regulated or PII data is involved;
  - a policy area has no skill.
- **The owner is a named person, not a team.** A team can't be asked a question or held to a decision. Take the name from the skill's `metadata.owner` or from the governance register.
- **The product owner resolves concerns first,** with each policy owner, before engineering sees the spec. These are the points an analyst would have escalated. Write the decision, the name and the date in `Resolution`.
- **Claude never resolves a policy contradiction.** Claude describes it and proposes options; the policy owner decides.

## Front-end work: Claude Design to Claude Code

Front-end work is the clearest example of collapsing the phases:

1. Once intent.md is accepted, the product owner mocks the design up in Claude Design (beta), working from the intent.
2. The product owner iterates on the mock.
3. The product owner exports it to Claude Code to build.

Reference the mock (link or exported path) in spec.md `## Design`. The mock is reviewed as part of the spec gate, and the Stage 4 visual check compares the build against it (see references/stage-4-test.md). Claude Design is in beta, and its export mechanics are not verified here; follow its current documentation.

## The analyze pass (read-only)

Before presenting, check spec.md against intent.md without editing either. A fresh context catches what the author's context explains away. The default is to delegate to the `sdlc-gate` subagent: "Use the sdlc-gate agent on changes/<id>/spec.md with upstream changes/<id>/intent.md." It returns `continue` or `escalate`, with reasons.

Checks, by severity:

- **CRITICAL:**
  - a requirement contradicts intent.md or a policy skill;
  - `intent_sha` differs from the current intent fingerprint (`status --json`, `gates.intent.sha`).
- **HIGH:**
  - an intent claim or constraint (for example, "no new PII") is not reflected in any FR or SC, and not listed in Out of scope;
  - an intent open question is neither answered nor carried.
- **MEDIUM:**
  - an FR has no basis in the intent (scope creep);
  - a `non-compliant` row has no concern;
  - an SC is not measurable.
- **LOW:** wording, duplicates, untestable phrasing.

Fix every CRITICAL and HIGH finding before `present`. List the rest in your gate message. Cap the report at 50 findings: past that, the spec needs rewriting, not a findings list.

## The spec gate

Follow the Gate protocol in SKILL.md with gate `spec`:

```
- [ ] python3 <skill-dir>/scripts/sdlc.py present <id> spec   (runs validate --strict first)
- [ ] Show the card, the flagged-concerns table and the analyze summary
- [ ] AskUserQuestion: "Approve spec for <id>?" (Approve / Request changes / Reject)
- [ ] Approve only → python3 <skill-dir>/scripts/sdlc.py approve <id> spec --by "<their name>" --role product-owner
- [ ] High-risk only: a second answer from the tech lead or architect → approve <id> spec --by "<their name>" --role tech-lead (or architect)
```

- **The product owner reviews the spec against the idea.** Does it solve the stated problem? Are the intent's open questions answered or carried forward?
- **`--by` is the name the human gives you.** Never use git config or your own name; ask for it in the same question if needed.
- **High-risk changes need two approvals on the same fingerprint:** `--role product-owner` (the decider) and `--role tech-lead` or `architect`, each after its own human answer. `status` lists the role still missing; the gate is current only when both are recorded. Editing the spec between them resets both: present again.
- **A human always decides whether the spec progresses to Build.** Accepting the spec starts plan mode (references/stage-3-build.md).
- **Request changes** sends the spec back for revision: edit, then `present` again (any edit above `## Change log` needs a new `present`).
- **Reject**, only when the human picked it: `reject <id> spec --by … --reason …` records the rejection; revise and present again.

## Codify, then automate

1. **By hand.** Run the prompt above in a session with the organization's skills loaded until the specs it produces stop needing structural corrections.
2. **As a command.** Codify the prompt as an organization-level slash command, or use this skill's `spec` mode: `/ai-native-sdlc spec <id>`. Distribute it org-wide through the plugin marketplace so every product owner runs the same version.
3. **As a trigger.** Accepting intent.md (the merge of the intent PR into the intent home) triggers a non-interactive job. The job runs the pass with the organization's skills loaded and commits spec.md as a pull request. From then on, the product owner's first involvement is the review. The recipe is in references/automation.md. Constraints that hold in every variant:
   - **Never use `--bare`.** It skips the skills and CLAUDE.md that constrain the spec (references/automation.md).
   - **Pass `--permission-mode` and `--allowedTools` explicitly** (fresh-checkout rules in references/automation.md).
   - **The job cannot ask questions.** Everything becomes a `[NEEDS CLARIFICATION]` marker (at most 3) or a recorded assumption, and the `sdlc-gate` verdict goes in the PR body. On `escalate`, label the PR for a human.
   - **The job never approves.** It ends with `validate`, commits on branch `change/<id>` and opens the artifact PR. A code owner's review of that PR is the spec gate (for high-risk, the product owner's and the tech lead's or architect's). The approver mirrors each approval into `audit.md` from an interactive session: `present`, then `approve --evidence "PR <url> approved by <name>"`. A review of the implementation PR never satisfies the spec gate.

## Governance

| Control | Detail |
|---|---|
| Enforced | Strict validation blocks `present` while any concern is `open` or a clarification marker remains. The plan guard blocks source edits for standard and high-risk changes until the spec approval is current. The approval is bound to the spec fingerprint. |
| Evidence | spec.md next to intent.md; the prompt or command that produced it (commit body); the skill versions in force (frontmatter `skills`); `intent_sha`; concern resolutions with owner names and dates; `GATE_APPROVED` in `audit.md`. |
| Where logged | Version control: the git history of `changes/<id>/` and the spec PR. |
| Who approves | The product owner signs off the spec and routes each flagged concern to its named policy owner. A high-risk spec needs a second approval, from a tech lead or architect, on the same fingerprint. |

## How to measure

**Leading indicator: intent to spec time.** The elapsed time between the intent.md commit and the spec.md commit for the same change, compared with the old requirements-plus-design cycle.

```bash
ID=<id>
git log --diff-filter=A --format=%aI -- changes/$ID/intent.md | tail -1
git log --diff-filter=A --format=%aI -- changes/$ID/spec.md | tail -1
```

**Lagging indicator: requirements rework after Build starts.** The number of spec.md commits dated after the first plan.md commit for the same change.

```bash
P=$(git log --diff-filter=A --format=%aI -- changes/$ID/plan.md | tail -1)
git log --since="$P" --format=%h -- changes/$ID/spec.md | wc -l
python3 <skill-dir>/scripts/sdlc.py metrics      # both, as medians and p90 across changes
```

## Pitfalls

- **Restating the code as a "spec".** Describe the change, not the existing system. Link to existing behaviour instead of regenerating it.
- **Implementation detail in the spec.** File lists and task order belong to plan.md, where the engineer can still change them.
- **Claude settling a policy conflict.** The result looks compliant and has no owner. Flag the conflict instead.
- **Unversioned skills.** Without versions you can't tell, months later, which policy the spec conformed to.
- **Editing intent.md during Design.** It voids the intent approval and `intent_sha`. Present the intent again first, then update the spec.
- **Using markers as a to-do list.** More than three means the intent wasn't concrete. Send it back to Stage 1.
- **Engineering starting before concerns are resolved.** The first rework cycle then lands in Build, where it costs the most.
