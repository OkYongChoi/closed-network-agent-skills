---
name: "skills-installer"
description: "List and securely install catalogued Agent Skills from local checkouts or Git remotes. Use for closed-network bootstrap, internal mirrors, or checksum-verified skill installation."
license: "Apache-2.0"
compatibility: "Requires Python 3.11+ and Git; remote sources require access to the selected Git server."
---

# Skills Installer

Use `scripts/skill_installer.py list` to inspect the selected catalog and
`scripts/skill_installer.py install NAME` to install one skill. The default target
is `${AGENT_HOME:-~/.agents}/skills`; `--dest` or `--agent-home` can override it.

Source precedence is `--source`, `AGENT_SKILLS_SOURCE`, this repository checkout,
then the canonical repository. Local sources need no ref. Remote ref precedence is
`--ref`, `AGENT_SKILLS_REF`, then the canonical catalog/embedded ref. Remote refs
must be full commit SHAs; accept a branch or tag only when the user explicitly
requests development behavior and passes `--allow-mutable-ref`.

The installer rejects existing targets, links, hard-linked files, special files,
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
