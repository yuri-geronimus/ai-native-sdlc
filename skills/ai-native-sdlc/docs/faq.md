---
title: FAQ - AI-native SDLC playbook and the ai-native-sdlc skill
description: Answers to common questions about the AI-native SDLC playbook and how this Claude Code skill implements it.
---

# FAQ

## What is the AI-native SDLC playbook?
An Anthropic article describing how to run the software lifecycle with coding agents: six stages (Plan, Design, Build, Test, Deploy, Maintain), each ending with a committed artifact that the next stage reads. Read it at <https://claude.com/resources/articles/the-ai-native-sdlc-playbook>.

## What is this repo?
An unofficial, open-source Claude Code skill and plugin that turns the playbook into enforced behaviour: artifacts, human approval gates, guard hooks, independent review subagents and CI workflows. It isn't affiliated with Anthropic.

## How do I install it?
As a plugin: `/plugin marketplace add yuri-geronimus/ai-native-sdlc`, then `/plugin install ai-native-sdlc@ai-native-sdlc`. Or clone it to `~/.claude/skills/ai-native-sdlc`. See the [guide](guide.md).

## What are intent.md, spec.md and plan.md?
Three committed artifacts per change. `intent.md` says what is wanted and why, `spec.md` says what done looks like, `plan.md` says how it will be built and proved. A human approves each; editing an approved artifact voids its approval. See [how it works](how-it-works.md).

## What stops the agent from skipping the plan or editing tests to pass?
Guard hooks: no source edits before the plan is approved, no test edits in bug-fix mode, no production deploy by the agent. The hooks run outside the model, so they hold even when it forgets.

## How does AI PR review work here?
Read-only subagents (`sdlc-reviewer`, `sdlc-gate`, `sdlc-verifier`) review the diff in fresh context against `REVIEW.md`, `spec.md` and `plan.md`, and return findings with file, line and confidence. CI workflow templates run them on pull requests.

## Can I use it in a regulated organisation?
Yes, that is the main audience: fintech, payments, medtech and other teams where changes need a named approver and an audit trail. It supports separation of duties via CODEOWNERS, managed settings, and a plugin build for orgs that only allow managed hooks. It supports your compliance process but does not certify you against any standard, and the project is alpha, so assess it before relying on it for controls. See [configuration](configuration.md) and `references/governance.md`.

## What are its limits?
See [design decisions and limits](design-decisions.md).
