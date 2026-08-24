# Authoring profile

The validator accepts one-line YAML-compatible scalar values for `name`,
`description`, `license`, and `compatibility`. Only `name` and `description` are
required. Generated values are JSON double-quoted. Unquoted input is accepted only
for the conservative token form `[A-Za-z][A-Za-z0-9._/-]*`; YAML reserved words
such as `true`, `null`, `yes`, and `off` are rejected.

It rejects unknown keys, duplicate keys, multiline values, collections, comments
after values, and empty bodies. This is intentionally narrower than the complete
Agent Skills specification. See the repository-level
`references/agent-skills-profile.md` for constraints and provenance.
