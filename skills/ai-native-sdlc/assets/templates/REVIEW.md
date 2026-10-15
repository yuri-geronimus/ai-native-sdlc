# Review instructions

<!--
Owner: the tech lead. Keep this file short; every reviewer reads all of it.
Who reads it: the managed Code Review service reads REVIEW.md from the repo root.
The local /code-review command and the code-review@claude-code-plugins plugin do
NOT read it (they read CLAUDE.md only). The repo's review skill
(/ai-native-sdlc review) and the sdlc-reviewer subagent read it explicitly.
Tune monthly: rate findings, adjust the caps and the skip list below.
-->

## Passes

Run three passes and tag each finding with its pass:

- **Bugs**: logic errors, broken edge cases, off-by-one and null handling, error paths that swallow failures, concurrency and ordering, subtle regressions in behaviour the diff did not mean to change.
- **Security**: injection (SQL, shell, template, path), authentication and authorization gaps, secrets or PII in code, logs or error messages, unsafe deserialization, missing input validation at trust boundaries, new dependencies with known issues.
- **Compliance**: the change matches `changes/<id>/spec.md` (every `FR-`/`SC-` it claims is implemented and tested), follows `changes/<id>/plan.md` ("Files that change", "Order of work"; deviations are recorded in its "Change log"), and respects our design principles and CLAUDE.md. Read the code first, then treat plan.md and the PR description as claims to check, not as evidence.

## What Important means here

Reserve **Important** for findings that would break behaviour, leak data or breach a policy, including an unmet `FR-` requirement or a test weakened to make it pass. Style and naming are **Nit**. A real bug that this diff did not introduce is **Pre-existing**. A violation of a CLAUDE.md rule written as "must" or "never" is Important; other CLAUDE.md drift is a Nit.

## CLAUDE.md upkeep

- **Outdated CLAUDE.md.** Grep CLAUDE.md for every command, path, script or convention the diff renames or removes. Each hit is a Nit, "CLAUDE.md outdated: <line> refers to <old>", with the replacement text. Put it in the summary; inline only when CLAUDE.md is in the diff.
- **Second occurrence.** When a confirmed finding repeats a class of mistake flagged on an earlier PR (CI provides `prior-findings.json`; locally, check recent review comments) and CLAUDE.md "Things Claude gets wrong" does not cover it yet, propose the CLAUDE.md line in the summary. Make it a committable suggestion only if CLAUDE.md is in the diff; otherwise suggest "@claude add this to CLAUDE.md" so the edit goes through PR review and the evals.

## Cap the nits

Report at most five nits per review; summarize the rest as a count.

## Do not report

- Generated files, lockfiles and vendored code (adjust paths for this repo, e.g. `src/gen/**`).
- Anything CI already enforces: formatting, lint rules, type checks, coverage thresholds.
- Issues on lines the diff did not touch, unless the diff makes them reachable (then report as Pre-existing).
- Code where a linter rule is explicitly silenced with a justification comment.
- Speculation without a concrete failure path, and suggestions that are matters of taste.

## Verification bar

Every finding cites `file:line` and a concrete failure scenario: the input or state, and the wrong output, crash, leak or breach that follows. Trace the call path in the code before reporting; a finding you could not confirm by reading the code is dropped, not softened. Report only findings you hold at confidence 80 or higher.

## Re-review

After the first review of a PR, report Important findings only, plus whether each earlier Important finding is fixed. Do not re-raise a finding a human resolved or dismissed in the thread.
