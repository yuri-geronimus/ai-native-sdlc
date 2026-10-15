---
id: "{{id}}"
title: "{{title}}"
status: draft
spec_sha: "{{spec_sha}}"
intent_sha: "{{intent_sha}}"
---
# Plan: {{title}}

<!-- Build artifact, produced in plan mode at the start of Stage 3 (Build) from the
     approved spec.md (or intent.md for a bounded change). Write it so an engineer
     who never saw the conversation could implement the change from it alone.
     spec_sha / intent_sha = fingerprints at plan time (`sdlc.py status <id> --json`);
     leave spec_sha empty ("") for a bounded change without a spec. -->

## Goal
<!-- One or two sentences: the outcome when this plan is done, citing FR/SC IDs. -->

## Files that change
<!-- One path per line, relative to the repo root; mark new files. `sdlc.py drift`
     compares the diff with this list. Example:
     - `services/api/routes/status.py`
     - `services/api/tests/test_status.py` (new)
     - `web/src/status/StatusPanel.tsx` (new) -->

## Order of work
<!-- Numbered or checkbox tasks in dependency order. Tag tasks that touch disjoint
     files with [P] (parallel-safe) and cite requirement IDs. Example:
     - [ ] T1 Write failing tests for the status endpoint (FR-001, FR-002)
     - [ ] T2 Add the endpoint behind existing auth (FR-001, FR-002)
     - [ ] T3 [P] Status panel against the endpoint (FR-001)
     - [ ] T4 [P] Cache responses for 60 s to stay under the upstream rate limit (SC-001) -->

## Risks
<!-- What the change could break, the riskiest step, and the options rejected.
     Example: "Upstream API rate-limits at 50 rps; the panel must cache." -->

## Proof
<!-- The commands and tests that show it works, written as commands in backticks.
     Example: `make test` (test_status.py covers all four states); `npm run e2e -- status`;
     screenshot compared with the approved mock. -->

## Rollback
<!-- How to undo safely: revert commit, feature flag off, migration down, data repair. -->

## Review focus
<!-- Where the reviewer should look hardest: unhandled inputs, concurrency,
     security boundaries, anything you were unsure about. -->

## Change log
<!-- Deviations during Build, append-only: "- YYYY-MM-DD <what changed> — <why>".
     Edits below this heading do not invalidate the plan approval. -->
