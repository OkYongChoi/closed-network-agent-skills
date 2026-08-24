---
name: "skills-creator"
description: "Create and validate portable Agent Skills with a dependency-free restricted authoring profile. Use when scaffolding a skill for mirrored or closed-network distribution."
license: "Apache-2.0"
compatibility: "Requires Python 3.11 or newer; no network access is required."
---

# Skills Creator

Create only the resources that materially support the requested workflow. Keep the
main instructions concise and route substantial conditional detail through
`references/`.

Use `scripts/skill_tool.py create-skill NAME --output DIR` to scaffold a skill.
Supply a discriminating `--description` that states what the skill does and when it
should activate. Add `--resource scripts`, `references`, or `assets` only when the
skill needs that directory.

Run `scripts/skill_tool.py validate PATH` after editing. The validator implements
the repository's restricted stdlib-only frontmatter profile, documented in
`references/authoring-profile.md`; it is not an arbitrary YAML validator.

Do not overwrite an existing skill. Review the generated instructions, replace
the starter body with task-specific guidance, and execute any generated scripts
before handing the skill off.
