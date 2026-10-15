# Contributing

Thanks for helping improve ai-native-sdlc.

## Reporting bugs

Open a [bug report](../../issues/new?template=bug_report.yml). Include:

- the command or prompt you ran, and what you expected;
- what happened instead, with the literal output;
- your Claude Code version, Python version and OS;
- the contents of `.claude/sdlc.json` (remove anything private).

Security problems: don't open a public issue. Contact the maintainers privately.

Feature ideas go in a [feature request](../../issues/new?template=feature_request.yml). Usage questions and "how do you…" go in [Discussions](../../discussions).

## Where to start

Issues labelled [good first issue](../../labels/good%20first%20issue) are small and scoped. Good places to begin if none are open:

- add an example change under [examples/](examples/) for a different tier (a bounded change, a bug fixed test-first, a high-risk change);
- add a `sdlc.json` detection rule for a build tool `init` doesn't recognise yet;
- improve a page in [docs/](docs/) where you got stuck during setup.

## Making a change

1. Open or find an issue first for anything larger than a typo.
2. Fork, then branch from `main`.
3. Make the change. Keep it small and focused. One concern per PR.
4. Run the unit tests (see [Maintaining](docs/maintaining.md)):
   ```
   python3 -m unittest discover -s tests -t .
   ```
5. If you edited the `description` in SKILL.md, re-run the trigger evals (they cost a few dollars):
   ```
   python3 evals/run_trigger_evals.py --help
   ```
6. Open a PR describing what changed and why, and link the issue.

## Guidelines

- Add or update tests for behaviour changes in `scripts/`.
- Keep the SKILL.md description under 1,024 characters, with no `: ` and no angle brackets.
- Update the matching page in `docs/` when behaviour or configuration changes.
- Python 3.9 must keep working.
