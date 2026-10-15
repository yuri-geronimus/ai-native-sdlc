# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow `metadata.version` in [SKILL.md](SKILL.md).

## [0.1.0-alpha.1] - 2026-10-15

Initial alpha release: interfaces and file formats may change without notice.

### Added
- The skill: the six-stage loop (Plan, Design, Build, Test, Deploy, Maintain) with intent.md, spec.md and plan.md as committed gates, and an append-only audit log per change.
- `scripts/sdlc.py` for changes, gates, drift and trace checks, metrics and `package-plugin`; `scripts/hooks.py` guard hooks (plan guard, fix mode, production gate, self-protect, verify-on-stop); `scripts/bands.py` for judging metric spikes.
- Three read-only subagents: `sdlc-gate`, `sdlc-reviewer`, `sdlc-verifier`.
- GitHub workflow templates for artifact checks, AI review, the `@claude` loop, evals, control bands and build triage.
- Docs: how it works, guide, configuration, design decisions, maintaining.
- A worked example in [examples/refund-idempotency](examples/refund-idempotency).
- A Claude Code plugin marketplace entry (`/plugin marketplace add yuri-geronimus/ai-native-sdlc`), built to the `plugin` branch on each version tag.
- Unit-test workflow, consistency tests for the SKILL.md frontmatter limits and Claude Code version pins, issue templates and CONTRIBUTING.md.
- Discoverability: docs FAQ, GitHub Pages config (`docs/_config.yml`, `docs/index.md`, `llms.txt`, `robots.txt`), `CITATION.cff`, and a README title that names the playbook.
- Alpha status and the regulated-team audience (fintech, payments, medtech) stated in the README, docs home page, FAQ, SKILL.md, plugin marketplace entry, `CITATION.cff` and `llms.txt`.
