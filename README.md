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

An explicit remote ref must be a full 40-character commit SHA. Mutable refs are
accepted only when `--allow-mutable-ref` is explicitly supplied for development:

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
python3 skills/skills-installer/scripts/skill_installer.py update repo-summary
```

## Latest approved releases

For PR-driven delivery, configure `skills.source` but omit `skills.ref`. The
installer then fetches `refs/heads/latest-approved`, reads its manifest-only
`release-manifest.json`, verifies that its `source` exactly matches the configured
source, and separately fetches the immutable 40-character commit in `ref`.
`list`, `install NAME`, and `update NAME` all resolve the same approved snapshot.
An explicit CLI, environment, user-config, or system-config ref remains a pinned
override and does not consult the pointer.

The manifest contains `name`, `source`, `ref`, `version`, `updatedAt`, the exact
`catalog.json` SHA-256, every package name/tree digest, and a monotonic pipeline
`sequence`. The installer compares
all of these with the immutable checkout before copying content. The mutable
branch is therefore only a protected control-plane pointer; no skill content is
installed from it.

`update` records provenance in
`<skills-destination>/.agent-install-metadata/<name>.json`, outside the catalogued
skill tree. It verifies the current installation and approved digest, builds the
replacement in a same-filesystem staging directory, then swaps the directory and
sidecar. A catchable in-process failure during the swap restores the previous
directory and sidecar, including `KeyboardInterrupt`. An uncatchable process or
host termination can leave hidden staging/backup paths because a directory and
external sidecar cannot be committed in one filesystem rename; rerun `update`
when the target exists, or have an operator inspect and restore the retained
backup when it does not. Running update against an already matching install is a
no-op. Running it for a missing install fails with guidance to use `install`.

A typical centrally deployed config is therefore:

```json
{
  "skills": {
    "source": "https://gitlab.company.local/ai/skills.git",
    "allowMutableRef": false
  },
  "agentHome": "~/.agents"
}
```

The source spelling must match the credential-free URL stored by CI in the
manifest. The checkout containing the running installer and embedded images
continue to use their local `catalog.json` without a release pointer. A local
Git mirror selected through central config follows its local
`refs/heads/latest-approved` when that ref is present; its filesystem path is
treated as an alias for the credential-free source recorded in that manifest.
An ordinary non-bare working checkout without that ref remains a direct local
development source for backward compatibility.

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

After bootstrap, deploy the approved internal Git URL in the system config and
omit `skills.ref` to follow `latest-approved`. Keep a full SHA only when an
administrator intentionally wants to freeze clients on one snapshot.
Environment variables remain useful for ephemeral CI overrides; ordinary users
do not need to export a ref. A local bare or non-bare Git mirror is also
supported without a ref when it contains `refs/heads/latest-approved`.

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
# approved GitLab source and omit skills.ref to follow latest-approved.
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

After both verification jobs pass on the default branch,
`publish:latest-approved` runs `scripts/promote_release.py`. It serializes
promotion with the `latest-approved` resource group and appends a manifest-only
commit to that branch. Configure GitLab as follows:

1. Protect `latest-approved`; allow pushes only from the role used by approved
   default-branch pipelines, and prevent ordinary contributors from pushing it.
2. In **Settings > CI/CD > Job token permissions**, enable **Allow Git push
   requests to the repository**. This is off by default. A same-project job-token
   push does not create another pipeline, avoiding a release loop.
3. Ensure the user whose merge triggered the pipeline has the protected-branch
   push role. The job token has that triggering user's permissions.
4. Keep `${CI_PROJECT_URL}.git` credential-free as the manifest source. The
   authenticated `CI_REPOSITORY_URL` is read from the named environment variable,
   is not placed in the command line, and is never written to the manifest.

The pipeline IID is recorded as a monotonic sequence. Although `resource_group`
serializes pushes, GitLab does not guarantee its default queue order; an older
pipeline therefore skips promotion if a greater or equal sequence is already
published. Manual rollback explicitly bypasses that stale-pipeline check and
records a new rollback version and pointer commit.

To roll back, run the manual `rollback:latest-approved` job on the default branch
with `ROLLBACK_REF` set to an earlier reviewed full SHA. The job publishes a new
pointer commit, preserving an auditable history; users receive it on their next
`update`. If your GitLab does not permit job-token pushes, provide an equivalent
masked protected push URL to the script through CI configuration. Runtime clients
still need only read access to the repository and never call the GitLab API.

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
