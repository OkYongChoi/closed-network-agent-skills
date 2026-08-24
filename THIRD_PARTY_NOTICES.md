# Third-party notices

This repository contains adaptations informed by the following Apache-2.0 works.
The original sources are not copied verbatim except for small structural and
interface conventions. All adapted files have been substantially changed for
portable, closed-network operation.

## OpenAI Skills

- Source: https://github.com/openai/skills
- `skill-creator`: commit `4ab6e0fd99c6667163bc34173e3ed3a3fed75ebc`,
  path `skills/.system/skill-creator`
- `skill-installer`: commit `49f948faa9258a0c61caceaf225e179651397431`,
  path `skills/.system/skill-installer`
- License: Apache License 2.0
- Changes: public names use the requested plural form; arbitrary YAML validation,
  GitHub API/download logic, UI assets, and product-specific installation have
  been replaced with a restricted stdlib validator, Git-only source acquisition,
  catalog integrity checks, and safe staged installation.

## Agent Skills specification

- Source: https://github.com/agentskills/agentskills
- Commit: `69ef37e9424c0a7ea9dd2293b559e43ec8176379`
- Source document: `docs/specification.mdx`
- License: Apache License 2.0
- Changes: `references/agent-skills-profile.md` is a concise implementation profile,
  not a verbatim or complete specification copy.

The complete Apache License 2.0 text is included in `LICENSE`.

