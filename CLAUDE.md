# pescobar.slurm Ansible collection — Project Context

## Overview

Ansible collection managing Slurm accounting (accounts, users, associations,
QOS) declaratively — the same persistent entities `sacctmgr` handles, the
same scope as the sibling project
[terraform-provider-slurm](https://github.com/pescobar/terraform-provider-slurm)
manages over slurmrestd. This collection instead drives Slurm's **native
declarative mechanism**: render desired state into `sacctmgr`'s flat-file
format, converge with `sacctmgr -i load` **on the slurmctld host over SSH**.
No slurmrestd, no JWT.

The collection is named `pescobar.slurm` (not `slurm_acct`) on purpose:
non-accounting Slurm functionality belongs here too. The accounting
module/role keep the `slurm_acct` name; the `slurm_install` role installs
and configures the cluster itself (see its section below). It replaces the
older `scicore.slurm` role (github.com/scicore-unibas-ch/ansible-role-slurm),
which is NOT modified — all new work happens in this collection.

**Every behavioral claim in this repo was verified against live clusters**
(25.05.8 / 25.11.6 / 26.05.1) — keep that discipline: when touching engine
behavior, verify against a live container cluster before writing code, and
record new findings in `docs/design.md`.

## Test environment

- Self-contained compose stack in `tests/docker/`: mariadb + slurmdbd +
  slurmctld. slurmdbd and slurmctld share ONE **self-contained** image
  (`tests/docker/Dockerfile` compiles Slurm from the SchedMD source tarball —
  recipe adapted from the sibling provider's `docker/`; the config is vendored
  under `tests/docker/config/`), adding **openssh-server + python3** for the
  Ansible-over-SSH tests. Published to
  `ghcr.io/pescobar/ansible-collection-slurm/slurm-test:<version>` by the
  `build-test-images.yml` workflow; the acceptance CI PULLS it (no per-run
  compile). No dependency on the provider repo's images anymore. See
  `tests/docker/README.md`.
- SSH key: the published image carries **none** (safe to publish). The
  throwaway key from `./tests/docker/generate-ssh-key.sh` (gitignored) is
  bind-mounted into slurmctld at runtime (`/run/ssh-pubkey/authorized_keys`)
  and installed by the entrypoint when `ENABLE_SSHD=true`.
- SSH: `root@127.0.0.1:2222`. Container names are `slurm-ansible-*` so the
  stack coexists with the provider repo's stack; hostnames stay
  `mysql`/`slurmdbd`/`slurmctld` (the vendored slurm.conf references them).
- Cluster name: `linux`. Supported/CI-tested Slurm versions: 25.05.8, 25.11.6,
  26.05.1. **Slurm 25.05 is the hard floor** — QOS entered the sacctmgr
  dump/load flat-file format in 25.05, so the module probes `sacctmgr -V`
  (`check_slurm_version` / `MIN_SLURM_VERSION`) and refuses 24.11 and older
  with a clear message pointing at the docs (verified live on 24.11.7).

Dev deps live in a project venv (`.venv/`, gitignored) —
`python3 -m venv .venv && .venv/bin/pip install -r tests/requirements.txt`
(ansible-core, pytest, ansible-lint). If `python3 -m venv` fails with
"ensurepip is not available" (no `python3-venv` package), use `uv` instead:
`uv venv --seed .venv && .venv/bin/pip install -r tests/requirements.txt`
(`--seed` puts pip in the venv). `python` is not on PATH here; use
`python3` / `.venv/bin/python`. Activate the venv (`source
.venv/bin/activate`) before `ansible-lint`, or it warns about an altered PATH.

```sh
./tests/docker/generate-ssh-key.sh
SLURM_VERSION=25.05.8 docker compose -f tests/docker/docker-compose.yml up -d --wait --build
./tests/acceptance/run-tests.sh          # 7-scenario converge/purge suite
./tests/acceptance/import-roundtrip.sh   # importer round-trip (converge → import → --check no-op)
./tests/acceptance/root-normal.sh        # in-place root account + normal QOS convergence
.venv/bin/python -m pytest tests/unit/   # pure logic, no cluster
```

## Architecture

- `plugins/module_utils/slurm_acct.py` — ALL pure logic, deliberately with
  **no Ansible imports** so `tests/unit/` drives it directly:
  `resolve()` (inventory data → normalized state records + validation),
  `render()` (records → loadable flat file), `parse_flat()` (dump → records,
  normalized), `compute_plan()` (desired vs live → categorized plan),
  `canonical_text()`/`projected_state()` (--diff plumbing).
- `plugins/modules/slurm_acct.py` — the module, runs on the slurmctld host.
- `plugins/filter/slurm_acct.py` — `merge_account_fragments` filter (thin
  wrapper over the module_utils function of the same name; used by the role
  to merge sharded per-account files).
- `roles/slurm_acct/` — thin wrapper: `slurm_acct_*` vars → module call +
  unmanaged-entities report. Also assembles sharded accounts (see below).
- `playbooks/` — `site.yml` (converge) + `import.yml` (aux: generate
  inventory from a running cluster) + sample inventory (fully worked data).
- `roles/slurm_install/` — installs/configures the cluster (see below).
- `tools/generate_inventory.py` — the importer: `sacctmgr dump` → inventory,
  reusing `parse_flat()`. Reverses the field-name maps; round-trip-tested.

### Inventory layout (sample, and what the importer emits)

- `group_vars/slurm_controller/` holds `slurm_cluster.yml`, `slurm_qos.yml`,
  `slurm_users.yml` (renamed with a `slurm_` prefix — the filename doesn't
  affect the var names inside).
- **Accounts are sharded one-file-per-account** under
  `host_vars/<host>/slurm_accounts.d/*.yml` (each file `account_name: {…}`).
  `slurm_acct_accounts_dir` (role default
  `{{ inventory_dir }}/host_vars/{{ inventory_hostname }}/slurm_accounts.d`)
  globs and merges them into `slurm_acct_accounts`, failing on a duplicate
  account name. The `.d` suffix is load-bearing: Ansible's DataLoader skips
  extensioned subdirs of `host_vars/<host>/`, so the fragments are NOT
  auto-loaded as stray host vars (a plain `slurm_accounts` dir would leak).
  Inline `slurm_acct_accounts` still works and merges under the fragments.

### The apply order is load-bearing (all steps verified empirically)

1. **Validate first, refuse early** — `load ... clean` is NOT transactional:
   a file that errors midway has already applied its deletions. Structural
   errors (undeclared parent_account, undeclared QOS reference, duplicate
   association, unpinned multi-account user, `'`/`:` in values) must never
   reach a load.
2. **Delete zero-association entities the file defines** — an account/user
   entity existing without a cluster association makes `sacctmgr load`
   silently SKIP creating its association (exit 0, nothing on stderr).
3. **QOS pre-load** (plain load, QOS-only file) — hierarchy lines cannot
   reference QOS created earlier in the same file ("You gave a bad qos";
   validation happens against process-start state), and a QOS line that
   *differs* from the live definition mid-file (e.g. built-in `normal`,
   whose init-time description bypasses sacctmgr's lowercasing) silently
   aborts the rest of the file. Hence: QOS converge separately, and the
   hierarchy file carries no QOS lines at all.
4. **Hierarchy `load ... clean` with a WCKey-less file** — clean is the only
   path that rebuilds the tree atomically (plain load cannot reliably create
   associations that depend on other additions in the same file, e.g. a
   DefaultAccount pointing at a section later in the file). Additive mode
   stays non-destructive by loading the **live ∪ desired merge** (clean has
   nothing to delete); purge mode loads exactly the inventory, rendered with
   an explicit `AdminLevel='None'` so demotions converge.
5. **Plain load of the full file** — applies WCKeys. On 25.11+ a `WCKeys=`
   on a User line silently aborts the whole clean transaction (the one
   version difference found); on 25.05 clean merely "flaps" them (drops
   declared WCKeys every other run). The two-pass split handles both.
6. **Purge deletions** (only with `slurm_acct_purge=true`): undeclared QOS
   (clean never deletes QOS — they're global), then orphaned users, then
   orphaned accounts. Never `normal`, never `root`.
7. **Convergence check** — re-dump, re-plan, fail loudly on residual diff.
   Essential: several load failure modes exit 0 with no stderr; the module
   also scans load output for error phrasings (`LOAD_ERROR_MARKERS`).

### Comparison semantics

- Normalization (both sides): fairshare sentinel `2147483647` ↔ `parent`;
  QOS/Flags/WCKeys lists sorted; TRES lists sorted; Description/Organization
  lowercased (sacctmgr lowercases them on store); key aliases canonicalized
  (`MaxJobsPerUser` → `MaxJobsPU`); `AdminLevel='None'` ≡ absent.
- Additive mode compares only inventory-declared keys ("stop managing",
  remove ≠ reset — matches the provider's Optional-only semantics). Purge
  mode compares exactly (clean verifiably resets omitted assoc fields).
- QOS fields compare declared-keys-only in BOTH modes: QOS definition fields
  never reset when omitted from a load file (verified). The built-in `normal`
  QOS is the same: declared in the `qos` map it is converged via `sacctmgr
  modify` (finding 15), never a load file, never deleted.
- The Cluster line's fields are re-rendered from the live dump as-is, EXCEPT
  the inventory-declared `root`-account override fields, which are overlaid
  onto it (declared-keys-only) and applied by the clean-load — that is how
  root's account-level attributes are managed (finding 14). Undeclared
  cluster-level defaults are still never fought over.

## slurm_install role

Ubuntu 26.04 only (archive Slurm 25.11.2 — Ubuntu 24.04 ships 23.11, below
the 25.05 floor), static config, munge auth. Facts verified by installing
the packages in `ubuntu:26.04`, and the role verified end-to-end in
privileged systemd containers (single node over `connection: local`, and a
controller + 2 workers + login node over `community.docker.docker`): fresh
install, zero-change second run, `--check --diff` clean, 2-node `srun`,
`--mem=100M` job allocating 600 MB ends `OUT_OF_MEMORY`, git-config mode,
Lua auto-add plugin, systemd drop-ins.

- Packages create the `slurm` user (uid/gid 64030), `/var/lib/slurm/{slurmctld,slurmd}`
  and `/var/log/slurm`; units are `Type=notify` (slurmctld, slurmd) /
  `Type=simple` (slurmdbd) with RuntimeDirectory `/run/slurmctld`,
  `/run/slurm`, `/run/slurmdbd` — the pid paths in `slurm.conf.j2` match.
  slurmctld runs as `User=slurm`. The slurmd unit already has
  `ConditionPathExists` commented out (configless-ready). `sackd` is packaged.
- The munge package generates a **different key per host**; the role copies
  the controller's. The key is binary: compared by checksum, written via
  `base64 -d` (`copy content=` is text-only).
- `/etc/slurm` is **not empty** after install (`plugstack.conf`), so git
  mode checks the repo out to `slurm_install_config_git_checkout` and
  rsyncs it over, excluding `.git` and `slurmdbd.conf` (always templated:
  holds the DB password; slurmdbd needs it 0600).
- slurmctld registers its cluster in slurmdbd on start (Slurm >= 20.02) —
  no `sacctmgr add cluster` task; the role only waits for port 6819.
- Restart ordering: munge, MariaDB and slurmdbd restart **inline** (state
  `restarted` when their config changed) because later tasks need them;
  slurmctld/slurmd restart in end-of-play handlers (`Slurm config changed`).
  Do not use `flush_handlers` — it would restart slurmctld/slurmd before
  slurmdbd exists on a fresh install.
- MariaDB modules: `ansible.mariadb` (the `community.mysql` modules are now
  redirects to `ansible.mysql`, which warns it drops MariaDB in 6.0.0).
  slurmdbd logs "not recommended values: innodb_buffer_pool_size" below
  4 GiB (SchedMD: >= 4 GiB and 5-50% of RAM) — the default is
  min(4096, 50% RAM) MB.
- Node lines come from each worker's facts (`processor_count`,
  `processor_cores`, `processor_threads_per_core`, `memtotal_mb`); they
  matched `slurmd -C` exactly in the containers.
  `slurm_install_node_memory_reserve_mb` is the knob if a VM reports less.
- The default `slurm.conf` sets no `MailProg`, so slurmctld logs
  "Configured MailProg is invalid" (no `/usr/bin/mail`) — harmless.
- In ansible-lint's template check, `inventory_hostname in <list>` fails;
  use `<group> in group_names`.
- slurmctld resolves accounting users to uids when it loads associations:
  a Unix user created *after* the accounting load is rejected ("Invalid
  account", slurmctld logs "User N not found") until `scontrol reconfigure`.
  The install test creates its users before running `site.yml`.

Local test recipe (no CI needed): build an image `FROM ubuntu:26.04` with
`systemd systemd-sysv python3 ansible-core sudo`, `CMD /sbin/init`, run it
`--privileged --cgroupns=private`, mount the installed collections, and run
`playbooks/install.yml` inside with `ansible_connection: local` — or
simply run `tests/install/run-tests.sh` inside it (mount the repo at
`/src`; it passes there too).

## Bugs found and fixed (all verified on live clusters)

- **Zero-association entity poisoning**: `sacctmgr load` sees the entity
  exists and skips adding its association — the engine's step 2 exists
  because of this. Symptom before the fix: load reports the full to-add
  table, exits 0, creates nothing for those entities.
- **Same-file QOS forward references fail** → QOS pre-load (step 3).
- **WCKeys abort clean on 25.11+ / flap on 25.05** → WCKey-less clean pass +
  plain second pass (steps 4–5).
- **Plain load can't create dependent additions** (user entity created in
  run N only becomes modifiable/associable in run N+1; a batch containing
  one permanently-invalid element rolls back the whole modification batch
  every run) → clean used in both modes, additive via live∪desired merge.
- **slurmdbd batch failures are silent client-side**: e.g.
  `error: no association for parent X on cluster linux` appears ONLY in
  slurmdbd's log while sacctmgr prints its normal tables and exits 0 → the
  convergence check (step 7) is the real safety net.

## Hazards / gotchas for future work

- **Never hand-delete parent accounts with children still referencing
  them** (`sacctmgr delete account` in the wrong order): this can leave
  soft-deleted assoc rows whose `id_parent` points at hard-deleted rows, and
  slurmdbd then rejects whole load batches with `no association for parent
  X` while the client reports success. Observed on an abused test cluster;
  fixed by `delete from linux_assoc_table where deleted=1` + daemon restart.
  The module's own deletion order does not produce this.
- The acceptance suite (`tests/acceptance/run-tests.sh`) copies the sample
  inventory to a scratch dir and edits the copy between phases — the
  committed sample is never modified. Account edits now add/remove/patch
  per-account files under `host_vars/slurmctld/slurm_accounts.d/` (not one
  dict). It asserts 7 scenarios; keep them in sync with the README's CI
  description when adding phases. The importer round-trip
  (`tests/acceptance/import-roundtrip.sh`) and the in-place root/normal test
  (`tests/acceptance/root-normal.sh`, self-contained inventory declaring only
  `root` + `normal`) are deliberately separate scripts, NOT extra scenarios,
  so the run-tests.sh count stays 7.
- `sacctmgr dump` emits only explicitly-stored values (inherited values do
  not appear on member lines), and emits user-global fields (DefaultAccount,
  AdminLevel, WCKeys, Coordinator) repeated on every line of the same user.
- The flat file cannot represent `'` or `:` inside values (the `:` field
  separator has no escaping); validation refuses them.

## Status / roadmap

- **One-file-per-account sharding** (DONE): filter + role assembly + sample
  migrated to `slurm_accounts.d/` + `slurm_`-prefixed group_vars. Live-verified.
- **Inventory importer** (DONE): `tools/generate_inventory.py` +
  `playbooks/import.yml`. Driven by `sacctmgr dump` (parsed with the shared
  `parse_flat()`) and `sacctmgr -nP show user/account` for zero-association
  detection — no slurmrestd. Reverses the field-name maps to emit the
  one-file-per-account layout + `slurm_{cluster,qos,users}` group_vars:
  bare-string members when an assoc has no overrides, `default_accounts`
  only for multi-account users, skips/ warns on `normal` and zero-assoc
  entities. **Shared member fields are hoisted into `association_defaults`**
  (`_hoist_defaults`): a field present on every member is pulled into the
  defaults block at its most common value when ≥2 members share it, dropped
  from the matching members (→ bare usernames) while the minority keep their
  explicit value — round-trip-safe, and it reproduces the hand-authored
  layout (e.g. `teaching`'s students collapse to bare names under a
  `fairshare: parent` default). `partition` is never hoisted; a non-hoisted
  fairshare of 1 is still dropped as resolve()'s default. Verified by pure
  round-trip unit tests (`tests/unit/test_generate_inventory.py`: import →
  `resolve()` → `compute_plan` vs `parse_flat(dump)` = no changes, incl. a
  hoisting case) and a live round-trip acceptance test
  (`tests/acceptance/import-roundtrip.sh`).
  Not yet done: per-account `coordinators` (warns; `Coordinator` is
  user-global in the dump).
- **Configure `root`/`normal` in place** (DONE): the declaration refusals in
  `resolve()` are lifted (deletion guard kept). `root` is declared in the
  `accounts` map with override fields only (fairshare, limits, allowed/default
  QOS) → its attributes live on the **Cluster line** (finding 14), so the
  existing clean-load applies them; no separate `modify` was needed. `normal`
  is declared in the `qos` map → routed to `state["system_qos"]` and converged
  with `sacctmgr modify` (finding 15, never a load file — avoids finding 7),
  its live state read via `sacctmgr show qos` + duration parsing. Both are
  declared-keys-only (omitted fields left as-is) and never deleted. Root-user
  AdminLevel is deliberately still not managed. Live-verified on 25.05.4 +
  25.11.5; unit tests + a dedicated acceptance script
  (`tests/acceptance/root-normal.sh`, separate from the 7-scenario suite).
- WCKey removal (currently stop-managing only).
- **Slurm install role** — phase 1 DONE (`slurm_install`: Ubuntu 26.04,
  archive packages, static config, see its section). Phase 2 — CI
  deployment test — is `acceptance-install.yml` running
  `tests/install/run-tests.sh` (below). Phase 3 DONE (2026-09-17): the
  scicore-courses-cloud repo (github.com/scicore-unibas-ch/scicore-courses-cloud,
  cloned next to this repo, see its CLAUDE.md) deploys its OpenStack course
  cluster with this role — verified live on a 5-VM dev cluster (Ubuntu 26.04,
  static config, lua auto-add plugin): jobs run as a course user, accounting
  auto-add works, over-`--mem` jobs are OOM-killed. Its PRs #29/#30 also had
  to bump ansible to 14.4.0 (the openstack inventory plugin bundled with
  ansible 10 breaks on openstacksdk 4) and willshersystems.sshd to v0.34.0
  (v0.27.1 does not know Ubuntu 26.04 and ends the play with `meta:
  end_host`). Next, in order: configless mode
  (`sackd` on login nodes), OpenStack elastic scheduling (resume/suspend
  scripts, `clouds.yaml`), an aux script to build compute-node images,
  building `.deb`s from source, custom apt repos. Features of the old role
  intentionally dropped: RedHat/EPEL/OpenHPC, creating the slurm user (the
  package does it), `GIT_SSL_NO_VERIFY`.
  CI design: its
  own workflow `acceptance-install.yml`, kept separate from
  `acceptance-management.yml` (the accounts/users/QOS suite). Use **tier 1 —
  run the role directly on the GitHub-hosted runner VM** (`hosts: localhost`,
  `connection: local`): the runner is a real ephemeral VM with systemd and a
  real package manager, so it exercises the actual install path (packages +
  systemd unit start) far more faithfully than a container — matrix over the
  OS runner images (now just `ubuntu-26.04`). The CI must also verify
  resource limits (a job exceeding `--mem` ends `OUT_OF_MEMORY`) and run
  `slurm_acct` against the installed cluster (the sample inventory, re-pointed
  at the runner; it needs partition `cpu`, cluster `linux` and
  `GresTypes=gpu` + `AccountingStorageTRES=gres/gpu` via
  `slurm_install_slurm_conf_extra`). The runner image ships MySQL 8.4
  (disabled) — the workflow purges it before MariaDB is installed. Only reach for tier 2
  (Vagrant + libvirt/KVM real VMs; `/dev/kvm` is available on Linux runners)
  if distros the runner images don't provide (RHEL/Rocky/…) or multi-node must
  be covered. NOT a container job — the management suite uses containers only
  because it just needs a *running* cluster to talk to, with no install to
  exercise.
- Non-accounting Slurm functionality generally (same rationale as the install
  role).

## Conventions

- Pure logic stays in `module_utils` with no Ansible imports; every new
  behavior gets a pytest unit test, and anything touching sacctmgr behavior
  gets verified live first (and documented in `docs/design.md`).
- ansible-lint must pass (config in `.ansible-lint`; run with the collection
  installed so FQCNs resolve — see README).
- CHANGELOG.rst is updated per release; galaxy.yml carries the version.
