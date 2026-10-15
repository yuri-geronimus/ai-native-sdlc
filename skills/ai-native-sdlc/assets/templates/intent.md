---
id: "{{id}}"
title: "{{title}}"
author: "{{author}}"
status: draft
tier: "{{tier}}"
source: "{{source}}"
record_id: "{{record_id}}"
created: "{{created}}"
---
# Intent: {{title}}

<!-- Stage 1 (Plan) artifact: a proto-spec in the originator's own words.
     What is wanted, why, and under which constraints. No solution design here;
     that is spec.md (Stage 2, Design). Keep it short: Problem under ~250 words.
     Author, status and timestamps live in the frontmatter, which sdlc.py manages. -->

## Problem
<!-- What can't users do today, who is affected, and what does it cost?
     Plain language, with numbers if known. Example: "Customers phone the contact
     center to ask where their claim is. Handlers spend roughly a third of call
     time on status-only queries." -->

## Proposed outcome
<!-- What better looks like, from the user's side, not the implementation.
     Example: "Customers see claim status, next step and expected date in the portal." -->

## Affected users and systems
<!-- Who is affected and which systems or teams are touched.
     Example: "Claims handlers, portal team, claims-core API." -->

## Constraints
<!-- Limits the solution must respect: policy, security, data, budget, deadlines.
     Example: "No new PII in the portal session. Existing authentication only."
     Write "None" if there are none. -->

## Assumptions
<!-- What you are taking as true without having checked. Each one is a candidate
     question for the product owner. Write "None" if there are none. -->

## Open questions
<!-- Questions the product owner or Stage 2 (Design) must answer. Example:
     "Do third-party loss adjusters need access too?" Write "None" if there are none. -->

## Change log
<!-- Append-only, one line per amendment after approval:
     "- YYYY-MM-DD <what changed> — <why>". Edits below this heading do not
     invalidate approval; edits above it do. -->
