# Closed-network Agent Skills

Portable [Agent Skills](https://agentskills.io/specification) tooling designed for
mirrored or fully disconnected environments. Runtime commands require only Python
3.11+ and the Git CLI. They do not call the GitHub API and do not install Python
packages.

## Contents

| Skill | Purpose |
| --- | --- |
| `skills-creator` | Create and validate skills using this repository's restricted, stdlib-only authoring profile. |
| `skills-installer` | List and securely install catalogued skills from a checkout or Git remote. |
| `repo-summary` | Produce a bounded, offline repository summary for an agent or human reviewer. |

The authoring profile accepts the required `name` and `description` frontmatter
fields plus scalar `license` and `compatibility` fields. It intentionally does not
claim to parse arbitrary YAML, `metadata`, or `allowed-tools`. Generated skills are
compatible with the Agent Skills specification; use a full YAML implementation if
you need the entire specification surface.

## Local use

```bash
python3 skills/skills-creator/scripts/skill_tool.py validate skills/repo-summary
python3 skills/skills-installer/scripts/skill_installer.py list
python3 skills/repo-summary/scripts/repo_summary.py .
python3 -m unittest discover -s tests -v
```

On Windows PowerShell, invoke every entry point through Python so executable-bit
differences between NTFS and POSIX do not matter:

```powershell
python -B skills/skills-creator/scripts/skill_tool.py validate skills/repo-summary
python -B skills/skills-installer/scripts/skill_installer.py list
python -B skills/repo-summary/scripts/repo_summary.py .
python -B scripts/verify_platform.py --autocrlf-clone
```

Create a skill:

```bash
python3 skills/skills-creator/scripts/skill_tool.py create-skill incident-brief \
  --output ./skills --description "Summarize an incident. Use during incident handoff."
```

Install from this checkout (the default while running the checked-in installer):

```bash
python3 skills/skills-installer/scripts/skill_installer.py install repo-summary \
  --dest /tmp/agent-skills
```

Remote sources require a full 40-character commit SHA. Mutable refs are accepted
only when `--allow-mutable-ref` is explicitly supplied for development:

```bash
python3 skills/skills-installer/scripts/skill_installer.py install repo-summary \
  --source https://github.example.com/agents/skills.git \
  --ref 0123456789abcdef0123456789abcdef01234567
```

Source, ref, and agent-home selection is deterministic and resolved per field:
CLI, environment (`AGENT_SKILLS_SOURCE`/`AGENT_SKILLS_REF`), user JSON config,
system JSON config, the current checkout, then the embedded canonical fallback.
The config locations are `~/.agents/config.json` and
`/etc/agent-tools/config.json` on Linux/macOS, or
`%USERPROFILE%\.agents\config.json` and
`%ProgramData%\AgentTools\config.json` on Windows. A local or bundled checkout
does not need a ref.

Administrators can centrally deploy one shared JSON file for both installers:

```json
{
  "skills": {
    "source": "https://gitlab.company.local/ai/skills.git",
    "ref": "0123456789abcdef0123456789abcdef01234567",
    "allowMutableRef": false
  },
  "plugins": {
    "source": "https://gitlab.company.local/ai/plugins.git",
    "ref": "abcdef0123456789abcdef0123456789abcdef01",
    "allowMutableRef": false,
    "defaultTarget": "portable"
  },
  "agentHome": "~/.agents"
}
```

Only JSON is supported. Unknown fields, duplicate keys, wrong types, malformed
JSON, linked config files, and oversized config files fail closed even if a CLI
argument would otherwise override them. Values are merged per field, so a user
source may combine with a system ref; administrators should normally deploy the
approved source and ref together. Mutable refs remain disabled unless
`allowMutableRef` is explicitly `true` in effective config or the development-only
`--allow-mutable-ref` flag is passed.

Inspect the resolved values and their provenance without contacting Git:

```bash
python3 skills/skills-installer/scripts/skill_installer.py effective-config
```

URL userinfo, query strings, and fragments are removed from this diagnostic.
After central configuration, ordinary users only need:

```bash
python3 skills/skills-installer/scripts/skill_installer.py install repo-summary
```

## Closed-network mirror and bootstrap

On a connected transfer host, mirror the repository and record the approved commit:

```bash
git clone --mirror https://github.com/OkYongChoi/skills.git skills.git
git --git-dir skills.git rev-parse refs/heads/main
```

Move `skills.git` through the approved transfer process and publish it to the
internal Git service. Administrators should expose only reviewed commits. On the
closed network:

```bash
git clone https://git.corp.example/agents/skills.git
cd skills
git checkout --detach APPROVED_FULL_COMMIT_SHA
python3 skills/skills-installer/scripts/skill_installer.py install skills-installer \
  --source . --dest "${AGENT_HOME:-$HOME/.agents}/skills"
```

After bootstrap, deploy the approved internal Git URL and full SHA in the system
config. Environment variables remain useful for ephemeral CI overrides; ordinary
users do not need to export a ref. Local filesystem mirrors are also supported
and do not need a ref.

PowerShell bootstrap against an internal GitLab mirror:

```powershell
git clone --mirror https://github.com/OkYongChoi/skills.git skills.git
git --git-dir skills.git rev-parse refs/heads/main
# Transfer skills.git through the approved process, then push it to GitLab.
git --git-dir skills.git push --mirror https://gitlab.corp.example/agents/skills.git

git clone https://gitlab.corp.example/agents/skills.git
Set-Location skills
git checkout --detach $env:APPROVED_SKILLS_COMMIT
python -B skills/skills-installer/scripts/skill_installer.py install skills-installer `
  --source . --agent-home "$env:USERPROFILE/.agents"
# Administrators then deploy %ProgramData%\AgentTools\config.json with the
# approved GitLab source and full commit SHA.
```

For a bare mirror already imported into GitLab, update it on the connected side
with `git remote update --prune`, review the new full commit SHA, and transfer the
mirror using the same approved process. The runtime installer talks only to the
configured Git remote and never calls GitHub or GitLab APIs.

## Release integrity

`catalog.json` contains a deterministic SHA-256 for every skill tree. The
`portable-tree-sha256-v2` digest covers portable paths, sizes, and contents, but
deliberately excludes POSIX mode bits because NTFS checkouts cannot reproduce
them. A pinned Git commit still authenticates executable bits for POSIX remote
installs; Windows invokes Python scripts explicitly with `python -B`. Python
bytecode,
`__pycache__`, and `.DS_Store` are excluded before any link traversal and are never
installed. Refresh the catalog
after an intentional skill change, review the diff, then run the test suite:

```bash
python3 scripts/refresh_catalog.py
python3 -m unittest discover -s tests -v
```

Runtime installation verifies both the checked-out commit (remote sources) and
the catalogued tree digest. Updating the canonical pin requires an explicit,
reviewed release change to both `repository.ref` and `CANONICAL_REF`.

Install locks record the local host, process ID, and creation time. A lock is
recovered only after the configured stale interval when it is a singly linked
regular file created on the same host and its recorded process no longer exists.
Malformed, foreign-host, live-process, linked, and recently modified locks fail
closed. Tune the one-hour default with `--stale-lock-seconds`.

## Linux, Windows, and GitLab verification

The portable profile rejects Win32 device names (`CON`, `NUL`, `COM1`, and
related names), reserved characters and alternate-data-stream colons, trailing
dots/spaces, non-NFC names, normalization/casefold collisions, overlong relative
paths, symbolic links, Windows junctions/reparse points, hard links, and special
files. Windows process liveness uses `OpenProcess`; it never calls `os.kill`,
whose Windows behavior is not a signal-zero existence probe.

`.gitattributes` forces all hashed text formats to LF even when
`core.autocrlf=true`; common binary formats are explicitly marked `binary`.
Run the complete offline acceptance suite with:

```bash
python -B scripts/verify_platform.py --autocrlf-clone
```

The command builds a temporary Git snapshot, clones it with
`core.autocrlf=true` and `core.eol=crlf`, then reruns unit tests, catalog digest
verification, and Git attribute checks without network access.

`.gitlab-ci.yml` defines separate `verify:linux` and `verify:windows` jobs for
shell runners tagged `linux` and `windows`; the Windows shell executor may use
its default PowerShell (`pwsh`). Adjust only the runner tags if your internal
GitLab uses different labels. Each runner needs Python 3.11+ and Git on `PATH`;
no package download, container image, or external Python dependency is used.

## Verified release snapshot

- Public repository: <https://github.com/OkYongChoi/skills>
- Verified implementation commit: `ce0f3e4ebbcdd12da6611d4f5ffd0c8f374cab2c`
- `skills-creator` source: `openai/skills@4ab6e0fd99c6667163bc34173e3ed3a3fed75ebc`
- `skills-installer` source: `openai/skills@49f948faa9258a0c61caceaf225e179651397431`
- Agent Skills specification snapshot: `69ef37e9424c0a7ea9dd2293b559e43ec8176379`

On 2026-08-24, catalog validation, all 42 tests, and an offline
`core.autocrlf=true` clean-clone check passed. The corresponding
[GitHub Actions run](https://github.com/OkYongChoi/skills/actions/runs/32706959494)
passed on Ubuntu and Windows with Python 3.11 and 3.13, including native Windows
hard-link, junction, and process-handle checks. A pinned `repo-summary` install
matched its catalogued tree and required no GitHub or GitLab API. Internal
GitLab runner execution remains an environment-specific acceptance step after
mirror import.

## Provenance and license

The implementation is adapted from the locked OpenAI system skills and the Agent
Skills specification listed in `UPSTREAM.lock.json`. Modifications replace
GitHub-specific download behavior with Git/local-source support, add offline
validation, and harden staged installation. See `THIRD_PARTY_NOTICES.md`.
