# ai-native-sdlc (alpha): the AI-native SDLC playbook for Claude Code, for regulated teams

[![tests](https://github.com/yuri-geronimus/ai-native-sdlc/actions/workflows/tests.yml/badge.svg)](https://github.com/yuri-geronimus/ai-native-sdlc/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![release](https://img.shields.io/github/v/release/yuri-geronimus/ai-native-sdlc)](https://github.com/yuri-geronimus/ai-native-sdlc/releases)

> **Alpha.** This project is an alpha release for regulated teams (fintech, payments, medtech and similar). Interfaces, `sdlc.json` keys, script commands and hook behaviour may change between versions without notice. Try it on non-critical work first.

**Let Claude Code do the work, and keep a human in charge of what ships.**

Agents are fast, but a fast agent without gates can edit tests until they pass, skip the plan, or reach production. This Claude Code skill puts every change through a committed paper trail: **intent.md → spec.md → plan.md → build → verify → PR review → gated release**. A human approves each gate, hooks enforce the rules even when the model forgets them, and the git history shows who asked for what, what the agent produced and who signed off.

- **Gates that hold.** No source edits before the plan is approved. No test edits while fixing a bug. No production deploy by the agent, ever.
- **Audit trail in git.** Artifacts and an append-only `audit.md` per change. Editing an approved artifact voids its approval.
- **Independent checks.** Fresh-context subagents review the diff, verify the proof commands and decide when to escalate to a human.
- **Production feedback loop.** Release findings and metric spikes come back as new intent files.
- **Built for regulated teams** (fintech, payments, medtech and similar). Separation of duties via CODEOWNERS, managed-settings support, and a plugin build for orgs that allow only managed hooks.

See what a change looks like in the [worked example](examples/refund-idempotency/changes/20261015-refund-idempotency/intent.md): a payments API change from request to plan, with every requirement traced.

The skill implements Anthropic's *The AI-native SDLC playbook* (August 2026): six stages (Plan, Design, Build, Test, Deploy, Maintain), each ending with a committed artifact that the next stage reads. Unofficial community implementation, not affiliated with Anthropic. Playbook: https://claude.com/resources/articles/the-ai-native-sdlc-playbook

This file is for people who install and review the skill. Claude doesn't read it; Claude reads [SKILL.md](SKILL.md).

## Install

**As a plugin** (recommended; includes the guard hooks and subagents):

```
/plugin marketplace add yuri-geronimus/ai-native-sdlc
/plugin install ai-native-sdlc@ai-native-sdlc
```

**As a plain skill**, for one user or one repo:

```sh
git clone https://github.com/yuri-geronimus/ai-native-sdlc ~/.claude/skills/ai-native-sdlc
```

Or clone to `<repo>/.claude/skills/ai-native-sdlc`. Then ask Claude to run `sdlc.py init --dry-run` (see [Quick start](#quick-start)). With the plugin, the hooks and agents ship with it: ask Claude to run `init` for you (it knows where the plugin lives) and use that path wherever Quick start shows `.claude/skills/ai-native-sdlc`.

Needs Claude Code 2.1.269 or later, git and Python 3.9+.

Note: `evals/` use a payments backend (transactions API, admin frontend) as the example repo; adapt them to your own.

## Quick start

The skill loads by itself when you describe work ("customers want…", "fix this bug", "is this spike real?"), or explicitly with `/ai-native-sdlc`.

1. **Preview the setup.** Nothing is written:
   ```
   python3 .claude/skills/ai-native-sdlc/scripts/sdlc.py init --dry-run
   ```
2. **Install.**
   ```
   python3 .claude/skills/ai-native-sdlc/scripts/sdlc.py init --claude-md
   ```
   This writes:
   - `.claude/sdlc.json`, with test commands detected from your Makefile, package.json or pytest config;
   - the guard hooks into `.claude/settings.json`, merged with what's already there;
   - the three subagents into `.claude/agents/`;
   - `REVIEW.md`;
   - a `changes/` folder;
   - a `.gitignore` entry for local state;
   - a routing block in CLAUDE.md (with `--claude-md`).

   Add `--with-ci` to also copy the GitHub workflows into `.github/workflows/`.
3. **Check `verify_commands`** in `.claude/sdlc.json`. The Stop hook and the verifier use them.
4. **Set up GitHub:** branch protection with CODEOWNERS on `changes/**`, and require approval of the most recent push. See [references/adoption.md](references/adoption.md).
5. **Try it:** "I have an idea: …", then `/ai-native-sdlc status`.

For orgs whose managed settings use `allowManagedHooksOnly`, project hooks don't run. Package the skill as a plugin instead and publish it to the org marketplace:

```
python3 .claude/skills/ai-native-sdlc/scripts/sdlc.py package-plugin --out ../ai-native-sdlc-plugin --author "Platform team"
```

See [references/governance.md](references/governance.md).

## Documentation

| Doc | For | Contents |
|---|---|---|
| [How it works](docs/how-it-works.md) | everyone | why the playbook, stages, gates, tiers, folder layout, requirements |
| [FAQ](docs/faq.md) | everyone | common questions about the playbook and this skill |
| [Guide](docs/guide.md) | people using it in a repo | step-by-step setup, day-to-day commands, what to do when blocked |
| [Configuration](docs/configuration.md) | repo owners | every `.claude/sdlc.json` key, defaults, an example, the production approval check |
| [Design decisions and limits](docs/design-decisions.md) | reviewers | what was chosen and why, what doesn't work yet |
| [Maintaining](docs/maintaining.md) | contributors | running tests, evals, editing the skill |
| [Examples](examples/) | everyone | a complete change folder to read before you start |
| [Changelog](CHANGELOG.md) | everyone | what changed in each version |
| [Contributing](CONTRIBUTING.md) | contributors | reporting bugs, proposing and submitting changes |
| [references/](references/) | Claude and reviewers | per-stage detail, governance, automation, metrics, adoption |
