# Restricted Agent Skills authoring profile

This repository implements a deliberately small profile of the Agent Skills
specification snapshot at commit
`69ef37e9424c0a7ea9dd2293b559e43ec8176379`.

## Supported layout

Every skill is a directory whose basename equals the `name` field and which has a
`SKILL.md`. Optional `scripts/`, `references/`, and `assets/` directories may be
present. Other ordinary files are allowed by the installer.

## Supported frontmatter

The stdlib parser supports a YAML-compatible subset:

- required scalar `name` (a conservative plain token or JSON double-quoted string);
- required scalar `description` (a conservative plain token or JSON double-quoted string);
- optional scalar `license`;
- optional scalar `compatibility`.

Plain values are limited to a single non-reserved token matching
`[A-Za-z][A-Za-z0-9._/-]*`; generated values are always JSON double-quoted.
Values must be on one physical line. Block scalars, anchors, aliases, tags,
collections, comments following a value, `metadata`, and `allowed-tools` are
outside this profile. This limitation avoids pretending that a handwritten parser
implements arbitrary YAML.

`name` is 1-64 lowercase ASCII letters, digits, or single hyphens; it cannot begin
or end with a hyphen or contain consecutive hyphens. `description` is 1-1024
characters. `compatibility`, when present, is 1-500 characters.

For the full format and current guidance, consult the authoritative Agent Skills
specification. This file documents only what the local tools validate.
