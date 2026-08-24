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

Source selection is deterministic: `--source`, then `AGENT_SKILLS_SOURCE`, then
the current repository checkout, then `https://github.com/OkYongChoi/skills.git`.
Ref selection is `--ref`, then `AGENT_SKILLS_REF`, then the canonical
`catalog.json` `repository.ref`/embedded `CANONICAL_REF`. The initial canonical
fallback requires `--ref` or `AGENT_SKILLS_REF` because this repository cannot
contain its own first commit SHA before that commit exists.

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

After bootstrap, set `AGENT_SKILLS_SOURCE` to the internal Git URL and
`AGENT_SKILLS_REF` to the approved full SHA. Local filesystem mirrors are also
supported and do not need a ref.

## Release integrity

`catalog.json` contains a deterministic SHA-256 for every skill tree. The digest
covers paths, sizes, normalized install modes, and contents. Python bytecode,
`__pycache__`, and `.DS_Store` are excluded before any link traversal and are never
installed. Refresh the catalog
after an intentional skill change, review the diff, then run the test suite:

```bash
python3 scripts/refresh_catalog.py
python3 -m unittest discover -s tests -v
```

The first publisher must replace `repository.ref` and the installer's
`CANONICAL_REF` with the published full commit SHA in a follow-up release. Runtime installation verifies
both the checked-out commit (remote sources) and the catalogued tree digest.

Install locks record the local host, process ID, and creation time. A lock is
recovered only after the configured stale interval when it is a singly linked
regular file created on the same host and its recorded process no longer exists.
Malformed, foreign-host, live-process, linked, and recently modified locks fail
closed. Tune the one-hour default with `--stale-lock-seconds`.

## Provenance and license

The implementation is adapted from the locked OpenAI system skills and the Agent
Skills specification listed in `UPSTREAM.lock.json`. Modifications replace
GitHub-specific download behavior with Git/local-source support, add offline
validation, and harden staged installation. See `THIRD_PARTY_NOTICES.md`.
