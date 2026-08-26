# Authoring profile

The validator accepts one-line YAML-compatible scalar values for `name`,
`description`, `license`, and `compatibility`. Only `name` and `description` are
required. Generated values are JSON double-quoted. Unquoted input is accepted only
for the conservative token form `[A-Za-z][A-Za-z0-9._/-]*`; YAML reserved words
such as `true`, `null`, `yes`, and `off` are rejected.

All skill tree components must be portable to Linux, macOS, and Windows. Use NFC Unicode,
avoid Windows device names and reserved characters, keep each component at or
below 255 UTF-16 code units and the skill-relative path at or below 240. Names
that collide after NFC normalization and Unicode case folding are rejected.

It rejects unknown keys, duplicate keys, multiline values, collections, comments
after values, and empty bodies. This is intentionally narrower than the complete
Agent Skills specification. See the repository-level
`references/agent-skills-profile.md` for constraints and provenance.
