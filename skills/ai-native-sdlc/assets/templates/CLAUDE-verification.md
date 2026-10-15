<!--
Paste both blocks below into CLAUDE.md (`sdlc.py init --claude-md` adds the first one).
In the second, replace {{build}}, {{test}} and {{lint}}
with this repo's single-command targets (each must exit non-zero on failure),
and replace each "healthy output" note with a real excerpt from a green run.
Delete a line only if the repo genuinely has no such step.
Hooks enforce part of this where installed: the Stop hook blocks "done" when
source changed after the last verification run, and fix mode blocks test edits.
-->
## How work is done here

- For any feature idea, bug report, ticket, incident, deploy or release request, or "fix X" request, invoke the `ai-native-sdlc` skill first and follow it (it right-sizes trivial edits to no ceremony).

## Verifying your work

- Build: `{{build}}` (exits 0; healthy output ends with the build tool's success line)
- Test: `{{test}}` (all green; never skip, delete or weaken a failing test)
- Lint: `{{lint}}` (zero warnings)

Run all three before reporting any task complete, and paste the output (the summary lines, plus the full failure text if anything failed). "It should pass" is not evidence; only output from a run after your last edit is.
If a test fails, fix the code, not the test.

- State the target before starting, in a form you can check without asking, e.g. "all tests in tests/test_status.py pass" or "GET /status returns 200 with the new field".
- Bug fixes: first write a test that reproduces the bug, run it, confirm it fails for the expected reason, and commit it. Then make it pass without editing that test.
- UI changes: compare a screenshot against the mock and iterate until it matches.
- If a check cannot run here (missing service, credentials, docker), say so and say what you ran instead. Never report an unrun check as passing.
- When Claude makes the same mistake twice, add the correction here.
