# Plans

Design and implementation plans for Davestigator (Ghost Data Still Exists). Read in this order:

| File | Scope |
|---|---|
| [PLAN.md](PLAN.md) | Challenge pipeline: dataset recon, identity, evidence, timeline, policy, steps, output, evaluation, packaging |
| [PRODUCT_PLAN.md](PRODUCT_PLAN.md) | PRD + technical architecture: releases, package layout, data models, components, milestones |
| [CLI_PLAN.md](CLI_PLAN.md) | CLI commands, screens, terminal flows, exit codes, config |
| [HALLUCINATION_PLAN.md](HALLUCINATION_PLAN.md) | Gemma 3 4B via Ollama: allowed LLM tasks, validation guard, HAL test suite |
| [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) | What was built, and deviations from the plans with reasons |
| [CONNECTOR_PLAN.md](CONNECTOR_PLAN.md) | Universal read-only database connectors (proposed) |
| [CONNECTOR_CLI_FLOW.md](CONNECTOR_CLI_FLOW.md) | How the CLI behaves with connectors: commands, screens, errors (proposed) |

Later files override earlier ones where they say so (e.g. HALLUCINATION_PLAN.md replaces the LLM provider in PRODUCT_PLAN.md).
