---
title: AI-native SDLC playbook implementation for Claude Code
description: Open-source Claude Code skill and plugin implementing Anthropic's AI-native SDLC playbook, with approval gates, guard hooks and an audit trail in git.
---

# AI-native SDLC playbook: a Claude Code implementation

`ai-native-sdlc` is an open-source Claude Code skill and plugin that implements the ideas in Anthropic's [The AI-native SDLC playbook](https://claude.com/resources/articles/the-ai-native-sdlc-playbook). Every change goes through **intent.md → spec.md → plan.md → build → verify → PR review → gated release**, a human approves each gate, and hooks enforce the rules even when the model forgets them.

**Alpha.** This project is an alpha release: interfaces, `sdlc.json` keys, script commands and hook behaviour may change between versions without notice.

It is aimed at regulated teams (fintech, payments, medtech and similar), where every change needs a named approver and an audit trail. It supports your compliance process; it does not certify you against any standard.

Unofficial community implementation, not affiliated with Anthropic.

- [How it works](how-it-works.md): stages, gates, tiers and folder layout
- [Guide](guide.md): set up a repo and use the loop day to day
- [FAQ](faq.md): answers to common questions about the playbook and this skill
- [Configuration](configuration.md): every `.claude/sdlc.json` key
- [Design decisions and limits](design-decisions.md)
- [Source on GitHub](https://github.com/yuri-geronimus/ai-native-sdlc)

```
/plugin marketplace add yuri-geronimus/ai-native-sdlc
/plugin install ai-native-sdlc@ai-native-sdlc
```
