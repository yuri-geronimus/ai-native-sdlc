---
id: "{{id}}"
title: "{{title}}"
status: draft
intent_sha: "{{intent_sha}}"
skills: []
risk: "{{risk}}"
---
# Spec: {{title}}

<!-- Stage 2 (Design) artifact, written from the approved intent.md under the
     organization's policy skills (brand, security, compliance, UX).
     Frontmatter: intent_sha = fingerprint of intent.md at spec time
     (`sdlc.py status <id> --json` → gates.intent.sha); skills = policy skills
     applied, with versions if known, e.g. [security-policy@2.3, brand@1.4];
     risk = low | medium | high.
     At most 3 [NEEDS CLARIFICATION: ...] markers while drafting; none at approval. -->

## Summary
<!-- Two to four sentences: what will be built and how it answers the intent's Problem. -->

## Requirements
<!-- One block per requirement: an FR-### ID, a short name, then an EARS acceptance line.
     Forms: "THE SYSTEM SHALL ...", "WHEN <event> THE SYSTEM SHALL ...",
     "IF <unwanted condition> THEN THE SYSTEM SHALL ...", "WHILE <state> THE SYSTEM SHALL ...".
     Example:
     - FR-001 Status lookup. WHEN an authenticated customer opens one of their
       records THE SYSTEM SHALL show its current status, next step and expected date.
     - FR-002 Ownership check. IF the record does not belong to the caller THEN
       THE SYSTEM SHALL respond 404 without revealing that it exists. -->

## Success criteria
<!-- SC-### IDs, each measurable and technology-agnostic. Example:
     - SC-001 p95 status response time under 300 ms at 50 requests/s.
     - SC-002 Status-only support contacts drop by 25% within 60 days of release. -->

## Design
<!-- How it fits the existing codebase: components touched, interfaces and data
     contracts, data flow, migrations, feature flags. Link to mocks or diagrams.
     Enough for an engineer to write plan.md; not a task list. -->

## Policy conformance
<!-- One row per policy skill applied. Status: compliant | non-compliant | n/a.
     Every non-compliant row needs a matching row in Flagged concerns. -->
| Policy / skill | Status | Notes |
|---|---|---|

## Flagged concerns
<!-- Anything a policy can't satisfy, policies that contradict, or risks needing an
     owner's decision. Status: open | resolved | accepted | carried. No open rows at
     approval. Example row:
     | C1 | Caching status data may exceed retention policy | data-retention | Privacy lead | resolved | 15-minute TTL, no persistence | 
     Replace the table with "None" if there are no concerns. -->
| ID | Concern | Policy | Owner | Status | Resolution |
|---|---|---|---|---|---|

## Out of scope
<!-- What this change deliberately does not do, so reviewers don't expect it. -->

## Open questions
<!-- Questions carried forward from intent.md or raised here, each with an owner.
     Write "None" if there are none. -->

## Clarifications
<!-- Answers received while drafting, newest session last:
     ### Session YYYY-MM-DD
     - Q: ... → A: ... (who) -->

## Change log
<!-- Append-only: "- YYYY-MM-DD <what changed> — <why>". Edits below this heading
     do not invalidate approval. -->
