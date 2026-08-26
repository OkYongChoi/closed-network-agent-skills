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

## Get and use this repository

Clone the connected-side source, validate it, and install only the skill you
need. Installation from the checkout is fully local:

```bash
git clone https://github.com/OkYongChoi/air-gapped-agent-skills.git
cd air-gapped-agent-skills
python3 -B scripts/verify_platform.py --autocrlf-clone
python3 -B skills/skills-installer/scripts/skill_installer.py list
python3 -B skills/skills-installer/scripts/skill_installer.py install repo-summary
```

On Windows PowerShell:

```powershell
git clone https://github.com/OkYongChoi/air-gapped-agent-skills.git
Set-Location air-gapped-agent-skills
python -B scripts/verify_platform.py --autocrlf-clone
python -B skills/skills-installer/scripts/skill_installer.py install repo-summary
```

The default install root is `~/.agents/skills` on Linux/macOS and
`%USERPROFILE%\.agents\skills` on Windows. Use `--agent-home` or `--dest` to
relocate it. For a closed network, follow the mirror/bootstrap procedure below
and configure the internal GitLab URL rather than the public GitHub URL.

### Bring both repositories into an internal GitLab

On the connected transfer host, mirror both public repositories and push the
approved bundles to the closed-network GitLab:

```bash
git clone --mirror https://github.com/OkYongChoi/air-gapped-agent-skills.git
git clone --mirror https://github.com/OkYongChoi/air-gapped-agent-plugins.git
git --git-dir air-gapped-agent-skills.git push --mirror \
  https://gitlab.company.local/ai/air-gapped-agent-skills.git
git --git-dir air-gapped-agent-plugins.git push --mirror \
  https://gitlab.company.local/ai/air-gapped-agent-plugins.git
```

Deploy the following source-only JSON as `/etc/agent-tools/config.json` on
Linux/macOS, or `%ProgramData%\AgentTools\config.json` on Windows. Omitting `ref`
makes both installers follow the immutable commit recorded by
`latest-approved`:

```json
{
  "skills": {
    "source": "https://gitlab.company.local/ai/air-gapped-agent-skills.git",
    "allowMutableRef": false
  },
  "plugins": {
    "source": "https://gitlab.company.local/ai/air-gapped-agent-plugins.git",
    "allowMutableRef": false,
    "defaultTarget": "portable"
  },
  "agentHome": "~/.agents"
}
```

After the one-time installer bootstrap, ordinary users invoke the installed
installer and only choose package names. On Linux/macOS:

```bash
python3 -B ~/.agents/skills/skills-installer/scripts/skill_installer.py list
python3 -B ~/.agents/skills/skills-installer/scripts/skill_installer.py install repo-summary
python3 -B ~/.agents/skills/skills-installer/scripts/skill_installer.py update repo-summary
```

On Windows PowerShell:

```powershell
$installer = "$env:USERPROFILE\.agents\skills\skills-installer\scripts\skill_installer.py"
python -B $installer list
python -B $installer install repo-summary
python -B $installer update repo-summary
```

The companion Plugins repository documents its portable, Codex, and Claude
installation commands.

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
  --source https://github.example.com/agents/air-gapped-agent-skills.git \
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
    "source": "https://gitlab.company.local/ai/air-gapped-agent-skills.git",
    "ref": "0123456789abcdef0123456789abcdef01234567",
    "allowMutableRef": false
  },
  "plugins": {
    "source": "https://gitlab.company.local/ai/air-gapped-agent-plugins.git",
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
    "source": "https://gitlab.company.local/ai/air-gapped-agent-skills.git",
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
git clone --mirror https://github.com/OkYongChoi/air-gapped-agent-skills.git air-gapped-agent-skills.git
git --git-dir air-gapped-agent-skills.git rev-parse refs/heads/main
```

Move `air-gapped-agent-skills.git` through the approved transfer process and publish it to the
internal Git service. Administrators should expose only reviewed commits. On the
closed network:

```bash
git clone https://git.corp.example/agents/air-gapped-agent-skills.git
cd air-gapped-agent-skills
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
git clone --mirror https://github.com/OkYongChoi/air-gapped-agent-skills.git air-gapped-agent-skills.git
git --git-dir air-gapped-agent-skills.git rev-parse refs/heads/main
# Transfer the mirror through the approved process, then push it to GitLab.
git --git-dir air-gapped-agent-skills.git push --mirror https://gitlab.corp.example/agents/air-gapped-agent-skills.git

git clone https://gitlab.corp.example/agents/air-gapped-agent-skills.git
Set-Location air-gapped-agent-skills
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

### Optional: make an internal mirror the embedded fallback

The central configuration above is the normal deployment model. It keeps the
public upstream metadata intact while making every managed client use the
internal GitLab source. Use the following procedure only when this repository
will be rebuilt and distributed as an internal-only product, and an installer
with no configuration must never fall back to the public GitHub URL.

Changing `CANONICAL_SOURCE` alone does **not** make clients automatically follow
newly approved releases. It is only the source fallback used when no CLI option,
environment variable, central configuration, or local checkout is available.
Keep the centrally deployed `skills.source` set to the internal URL and omit
`skills.ref`; that is what makes clients resolve `latest-approved` and the
immutable SHA in its release manifest.

For an internal-only rebuild, make one reviewed release change that keeps these
values aligned:

1. Change `CANONICAL_SOURCE` in
   `skills/skills-installer/scripts/skill_installer.py` to the credential-free
   internal Git URL.
2. Change `catalog.json.repository.url` to the same internal URL so repository
   metadata describes the distributed product.
3. Keep `CANONICAL_REF` and `catalog.json.repository.ref` at a reviewed,
   reachable 40-character commit SHA. If the internal product has diverged from
   the imported history, update both to its reviewed release commit.
4. Ensure the GitLab `publish:latest-approved` job continues to pass
   `${CI_PROJECT_URL}.git` to `promote_release.py`; it writes the matching
   internal source and approved SHA to each release manifest.
5. Refresh and validate the release before publishing it:

   ```bash
   python3 -B scripts/refresh_catalog.py
   python3 -B scripts/verify_platform.py --autocrlf-clone
   ```

Do not put credentials in `CANONICAL_SOURCE`, `catalog.json`, or a release
manifest. Give runtime clients read-only repository access through the approved
internal authentication mechanism.

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

## macOS, Linux, Windows, and GitLab verification

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

GitHub Actions validates Ubuntu, macOS, and Windows. `.gitlab-ci.yml` defines
required `verify:linux` and `verify:windows` jobs for shell runners tagged
`linux` and `windows`, plus an optional manual `verify:macos` job for a runner
tagged `macos`; the Windows shell executor may use its default PowerShell
(`pwsh`). Rename the tags to match your internal runner inventory. Each runner
needs Python 3.11+ and Git on `PATH`; no package download, container image, or
external Python dependency is used.

After both verification jobs pass on a protected default-branch push pipeline,
`publish:latest-approved` runs `scripts/promote_release.py`. It serializes
promotion with the `latest-approved` resource group and appends a manifest-only
commit to that branch. Configure GitLab as follows:

1. Protect `latest-approved`; allow pushes only from the role used by approved
   default-branch pipelines, and prevent ordinary contributors from pushing it.
2. Protect the default branch as well: disable direct pushes while retaining
   merge permission for the authorized Merge Request reviewers. This makes a
   default-branch `push` pipeline an MR merge result rather than an ad-hoc push.
3. In **Settings > CI/CD > Job token permissions**, enable **Allow Git push
   requests to the repository**. This is off by default. A same-project job-token
   push does not create another pipeline, avoiding a release loop.
4. Ensure the user whose merge triggered the pipeline has the protected-branch
   push role. The job token has that triggering user's permissions.
5. Keep `${CI_PROJECT_URL}.git` credential-free as the manifest source. The
   authenticated `CI_REPOSITORY_URL` is read from the named environment variable,
   is not placed in the command line, and is never written to the manifest.

Scheduled, web, API, and manual pipelines do not automatically publish a
release. The pipeline IID is recorded as a monotonic sequence. Although `resource_group`
serializes pushes, GitLab does not guarantee its default queue order; an older
pipeline therefore skips promotion if a greater or equal sequence is already
published. Manual rollback receives a sequence greater than the current pointer,
even when the rollback job came from an older pipeline, so queued or retried
pipelines cannot undo it.

To roll back, run the manual `rollback:latest-approved` job on the default branch
with `ROLLBACK_REF` set to an earlier reviewed full SHA. The job publishes a new
pointer commit only when that SHA appears in the existing approval history,
preserving an auditable history; users receive it on their next `update`. If your
GitLab does not permit job-token pushes, provide an equivalent
masked protected push URL to the script through CI configuration. Runtime clients
still need only read access to the repository and never call the GitLab API.

## Verified release snapshot

- Public repository: <https://github.com/OkYongChoi/air-gapped-agent-skills>
- Verified implementation commit: `a8257ac34b9f9ca9af2658abbefe5068f24f4060`
- `skills-creator` source: `openai/skills@4ab6e0fd99c6667163bc34173e3ed3a3fed75ebc`
- `skills-installer` source: `openai/skills@49f948faa9258a0c61caceaf225e179651397431`
- Agent Skills specification snapshot: `69ef37e9424c0a7ea9dd2293b559e43ec8176379`

On 2026-08-25, catalog validation, all 58 tests, and an offline
`core.autocrlf=true` clean-clone check passed. The corresponding
[GitHub Actions run](https://github.com/OkYongChoi/air-gapped-agent-skills/actions/runs/32809451046)
passed on Ubuntu and Windows with Python 3.11 and 3.13, including native Windows
hard-link, junction, and process-handle checks. The same offline suite also
passes on macOS. A pinned `repo-summary` install matched its catalogued tree and
required no GitHub or GitLab API. Internal GitLab runner execution remains an
environment-specific acceptance step after mirror import.

## Provenance and license

The implementation is adapted from the locked OpenAI system skills and the Agent
Skills specification listed in `UPSTREAM.lock.json`. Modifications replace
GitHub-specific download behavior with Git/local-source support, add offline
validation, and harden staged installation. See `THIRD_PARTY_NOTICES.md`.
