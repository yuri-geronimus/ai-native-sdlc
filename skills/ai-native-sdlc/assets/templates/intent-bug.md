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
# Intent (bug): {{title}}

<!-- Stage 1 (Plan) artifact for a defect. Three-part form: what happens now,
     what should happen, and what must keep working. Each "Unchanged behavior"
     line becomes a regression test; each "Expected behavior" line becomes the
     failing test you commit before fix mode is turned on. -->

## Current behavior
<!-- WHEN <condition> THEN the system <incorrect behavior>.
     Example: "WHEN a claim has no assigned handler THEN the system returns 500
     from GET /claims/{id}/status." -->

## Expected behavior
<!-- WHEN <condition> THEN the system SHALL <correct behavior>.
     Example: "WHEN a claim has no assigned handler THEN the system SHALL return
     200 with status 'awaiting assignment'." -->

## Unchanged behavior
<!-- WHEN <condition> THEN the system SHALL CONTINUE TO <behavior that must not regress>.
     Example: "WHEN a claim has an assigned handler THEN the system SHALL CONTINUE TO
     return the handler's next step and expected date." -->

## Evidence
<!-- How you know: reproduction steps, log lines, alert or ticket links, failing
     command output, first-seen time. Link to sources rather than pasting secrets or PII. -->

## Affected users and systems
<!-- Who sees the bug and which systems or teams are involved. -->

## Constraints
<!-- Limits on the fix: no schema change, hotfix window, compatibility. "None" if none. -->

## Open questions
<!-- What is still unknown (root cause, blast radius, data repair). "None" if none. -->

## Change log
<!-- Append-only: "- YYYY-MM-DD <what changed> — <why>". Edits below this heading
     do not invalidate approval. -->
