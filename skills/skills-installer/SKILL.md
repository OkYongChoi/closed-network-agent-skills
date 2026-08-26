---
name: "skills-installer"
description: "List and securely install catalogued Agent Skills from local checkouts or Git remotes. Use for closed-network bootstrap, internal mirrors, or checksum-verified skill installation."
license: "Apache-2.0"
compatibility: "Requires Python 3.11+ and Git; remote sources require access to the selected Git server."
---

# Skills Installer

Use `scripts/skill_installer.py list` to inspect the approved catalog,
`scripts/skill_installer.py install NAME` to install, and
`scripts/skill_installer.py update NAME` to safely replace an installed skill.
The default target is `~/.agents/skills`; `--dest` or `--agent-home` can override it.

Settings resolve per field in this order: CLI; `AGENT_SKILLS_SOURCE` or
`AGENT_SKILLS_REF`; user `~/.agents/config.json`; system
`/etc/agent-tools/config.json` on Linux/macOS (Windows:
`%ProgramData%\AgentTools\config.json`); current checkout; embedded canonical
fallback. The shared strict-JSON config uses `skills.source`, `skills.ref`,
`skills.allowMutableRef`, and top-level `agentHome`. Local or bundled sources need
no ref. When a remote source is configured without a ref, the installer fetches
only `refs/heads/latest-approved`, strictly validates its manifest and source
binding, then separately fetches the immutable commit named by that manifest.
An explicitly configured ref still wins and must be a full commit SHA. Accept a branch or tag only when
effective config explicitly enables `allowMutableRef` or the user requests
development behavior and passes `--allow-mutable-ref`.

Use `scripts/skill_installer.py effective-config` to inspect resolved values and
per-field provenance without accessing the selected source. The command redacts
URL credentials, query strings, and fragments. Never ask ordinary users to copy
an approved SHA when their administrator has deployed central config.

Install rejects existing targets. Update compares a sidecar under
`.agent-install-metadata`, verifies the installed tree, and stages both content
and new provenance before swapping. If a catchable in-process swap step fails,
it restores the old directory and metadata. An uncatchable process or host
termination can leave hidden staging backups; rerun `update` when the target
exists, otherwise stop for operator inspection and restoration. The sidecar is
outside the skill, so it does not change the catalog digest.

The installer rejects links, hard-linked files, special files,
Windows junctions/reparse points, Win32-reserved or non-NFC names,
normalization/case-colliding paths, oversized trees, invalid frontmatter, and
digest mismatch. Tree digests exclude POSIX modes for NTFS portability; use a
full commit SHA to authenticate Git executable metadata.
It validates in a same-filesystem staging directory and publishes with an atomic
rename. Do not bypass those checks or manually merge into an existing target.
Stale locks are recovered only when they are old, regular, singly linked,
same-host locks whose recorded process no longer exists.
Windows process checks use the non-signalling `OpenProcess` API and fail closed
when process state cannot be determined.

For mirror bootstrap and the one-time canonical-ref publication step, read the
repository `README.md`.
