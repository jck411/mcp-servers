# mcp-servers

Standalone MCP servers deployed to Proxmox LXCs with systemd.

The former Knowledge MCP/API/database stack has been retired. This repository
keeps only the active non-Knowledge MCP services.

## Layout

```text
servers/          MCP server modules
shared/           Shared helpers
deploy/           Systemd and deploy scripts
tests/            Focused service tests
```

## Current deployment: CT 117

Read-only verification on **October 2, 2026** established that this repository
owns the account/home service code on **CT117 (`mcp-accounts`)**, at
`/opt/mcp-accounts`, origin `https://github.com/jck411/mcp-servers.git`.
The live HEAD and fetched `origin/main` both identified
`c2fe6b0b04c19516081f0678b2ea6af914fff646` at inspection. This is a deployment
checkpoint, not a version pin for future changes.

| Server | Port | Active systemd unit |
|---|---:|---|
| `calendar` | 9004 | `mcp-server@calendar` |
| `gmail` | 9005 | `mcp-server@gmail` |
| `gdrive` | 9006 | `mcp-server@gdrive` |
| `monarch` | 9008 | `mcp-server@monarch` |
| `spotify` | 9010 | `mcp-server@spotify` |
| `tv` | 9013 | `mcp-server@tv` |
| `hue` | 9015 | `mcp-server@hue` |

Selected live process arguments verified `servers.<name>`, streamable HTTP,
`0.0.0.0` and each listed port; all seven listeners and units were present.
`hue-event-collector.service` was also active. The template and collector run as
`mcp:mcp` with working directory `/opt/mcp-accounts`; live units are in
`/etc/systemd/system/`. Service-active/listener evidence does not prove provider
authentication, tool discovery or household-account functionality; no MCP action
or private-content read was performed.

The live checkout is **not clean**: tracked `README.md` and `AGENTS.md` edits
remove retirement warnings; untracked `.bash_logout`, `.bashrc`, `.profile` and
`.local/` also exist. Preserve and reconcile these independently; they do not
re-authorize the retired Knowledge stack. No live file was changed by this review.
Private environment, provider authentication and runtime state remain on the
guest, outside Git; they are not replaceable from an upstream checkout alone.

## Retired CT110 target

CT110 was absent from the inspected Proxmox inventory. It is not a target for
this repository's current account/home workload. `web_search` remains source
code, not an approved CT117 service. Do not recreate retired guests/services.
The unsafe CT110 deploy implementation has been replaced, not repointed.

## Local Setup

```bash
uv sync --extra all --extra dev
uv run pytest
```

## Scoped CT117 deployment procedure

`deploy/deploy.sh` is now **read-only**. There is intentionally no apply mode:

```bash
bash deploy/deploy.sh --dry-run calendar hue  # offline plan, no SSH
bash deploy/deploy.sh --preflight calendar hue  # read-only SSH/pct checks
uv run --extra dev pytest tests/test_deploy.py -q
```

Choose explicit names from the table above; no arguments, legacy flags, unknown
services and shell fragments are rejected. The port map is an expected-state
check, never a request to overwrite `.env` or kill a listener. Preflight checks
origin identity without printing its URL, checkout dirtiness (including
untracked files), runtime existence, selected unit user/group/working directory,
active state, expected listening ports, and Hue collector active state. It never
fetches, syncs dependencies, commits, pushes, writes source/configuration,
restarts or refreshes consumers. It suppresses raw transport errors. A listening
port is not proof the selected unit owns it; unit command/drop-in review and
consumer acceptance remain manual gates. A pass is not deployment approval.

`setup-systemd.sh` now fails closed: there is no supported bootstrap for this
existing guest. The repository template is a CT117 **reference**, not an exact
copy of the installed unit; do not install it during a source rollout.

### Before separately authorized source-only rollout

1. Test and publish the exact reviewed commit through the shared workflow; record
   its full 40-character SHA, the explicit service scope, and expected consumers.
   Confirm that the SHA is published on `origin/main`, not just a local branch.
2. Run preflight for that scope. **Any dirty live checkout blocks rollout.**
   Preserve/reconcile tracked edits and untracked/private files with their owner
   independently, outside this procedure. Do not stash, discard, clean, reset,
   commit or ignore them automatically to make the gate pass. Ignored credentials,
   tokens, databases, caches and private configuration must remain untouched too.
3. Inventory installed unit/drop-in metadata and selected executable/module/port
   arguments without dumping environment, provider credentials or raw logs.
   Confirm expected port ownership. Verify an independent recovery path and a
   current private-state backup under the guest's existing backup policy; do not
   copy private data into Git. Record old HEAD and installed unit metadata.
4. Review the **entire** old-to-candidate diff, including path collisions with
   ignored private state. Only service-source changes whose effects fit the
   selected scope belong in this procedure. Shared helpers require all affected
   services in scope. Hue code may also affect `hue-event-collector.service`;
   include it in explicit authorization/recovery when affected. Dependency/lock,
   unit/drop-in, configuration or state-schema changes require a separate plan
   for dependency synchronization, units, migration and rollback. Do not use
   `uv sync --extra all` as an implicit source-deploy step.

### Apply only after those gates and explicit CT117 authorization

Use `ssh -o BatchMode=yes -o ConnectTimeout=8 proxmox` and `pct exec 117 -- ...`.
In `/opt/mcp-accounts`, recheck clean status immediately before mutation, record
`git rev-parse HEAD` as `OLD_SHA`, fetch `origin main`, verify the exact approved
`NEW_SHA` exists on fetched `origin/main`, and confirm
`git merge-base --is-ancestor "$OLD_SHA" "$NEW_SHA"`. Re-review the full diff.
Then use **`git merge --ff-only "$NEW_SHA"`**, never a reset or an unpinned pull.
Use the checkout's owning Unix user for Git operations; do not change ownership
or global safe-directory settings to bypass an error. Serialize against other
operators; stop on concurrent source/configuration changes.

Verify HEAD equals `NEW_SHA` and status is still clean. Compile changed source
with the existing runtime without importing provider modules or reading private
content. Restart **only** the explicitly authorized units, e.g.
`systemctl restart mcp-server@calendar.service` for a calendar-only change.
Do not enable new units, rewrite port files, kill port holders or reload the
shared template. Restart the collector only when separately included in scope.
Check selected active units, their expected listeners and read-only MCP tool
listing through each relevant consumer. Provider authentication/functionality
needs its own authorized acceptance; an active service alone does not prove it.
There is no assumed CT111 backend refresh endpoint: use the actual consumer's
supported discovery path, with separate approval for any mutating refresh.

### Recovery

On failed acceptance, stop further rollout. With a clean source checkout and no
concurrent changes, restore source to recorded `OLD_SHA` using
`git switch --detach "$OLD_SHA"`, verify that exact HEAD, and restart only the
same authorized scope. This leaves a detached recovery checkout deliberately;
reconcile its branch before the next rollout, never force it to upstream. Preserve
private state/units unchanged and repeat the same unit/listener/consumer checks.
If cleanliness, configuration, dependency or state compatibility has changed,
stop and use the separately reviewed recovery plan—do not blindly restore data
or reset source. No automatic rollback or provider-content test is implemented.

### Audit acceptance (October 5, 2026)

Read-only preflight of all seven services verified expected user/group/workdir,
active units and listening ports, plus active Hue collector. HEAD remained
`c2fe6b0b04c19516081f0678b2ea6af914fff646`. Preflight correctly exited nonzero
for the existing dirty checkout documented above. No rollout or live
reconciliation was performed; the source-only procedure remains unexercised.
`uv` was not on the administrative shell or the guest inspection's default PATH;
local checks used a scratch-installed `uv`. A future dependency rollout must
resolve the guest's supported executable explicitly, not guess its PATH.

## Retired Knowledge Stack

Do not restore the old `servers/knowledge`, `servers/knowledge_admin`,
`servers/knowledge_api.py`, Knowledge systemd units, maintenance/wiki backup
jobs, SQLite database, or Knowledge Qdrant collections unless Jack asks for a
new replacement system.
