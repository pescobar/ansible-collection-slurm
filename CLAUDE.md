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
future non-accounting Slurm functionality belongs here too. The accounting
module/role keep the `slurm_acct` name.

**Every behavioral claim in this repo was verified against live clusters**
(25.05.4 / 25.11.5 / 26.05.1) — keep that discipline: when touching engine
behavior, verify against a live container cluster before writing code, and
record new findings in `docs/design.md`.

## Test environment

- Self-contained compose stack in `tests/docker/`: mariadb + slurmdbd +
  slurmctld. slurmctld runs an overlay image (built by
  `tests/docker/Dockerfile` FROM the public
  `ghcr.io/pescobar/slurm-test:<version>` images, which are produced by the
  sibling provider repo) adding **openssh-server + python3** (the base image
  has neither) and a throwaway test key (`./tests/docker/generate-ssh-key.sh`,
  output gitignored).
- SSH: `root@127.0.0.1:2222`. Container names are `slurm-ansible-*` so the
  stack coexists with the provider repo's stack; hostnames stay
  `mysql`/`slurmdbd`/`slurmctld` (the baked-in slurm.conf references them).
- Cluster name: `linux`. Supported/CI-tested Slurm versions: 25.05.4,
  25.11.5, 26.05.1.

```sh
./tests/docker/generate-ssh-key.sh
SLURM_VERSION=25.05.4 docker compose -f tests/docker/docker-compose.yml up -d --wait --build
./tests/acceptance/run-tests.sh          # needs ansible-core on PATH
python -m pytest tests/unit/             # pure logic, no cluster
```

## Architecture

- `plugins/module_utils/slurm_acct.py` — ALL pure logic, deliberately with
  **no Ansible imports** so `tests/unit/` drives it directly:
  `resolve()` (inventory data → normalized state records + validation),
  `render()` (records → loadable flat file), `parse_flat()` (dump → records,
  normalized), `compute_plan()` (desired vs live → categorized plan),
  `canonical_text()`/`projected_state()` (--diff plumbing).
- `plugins/modules/slurm_acct.py` — the module, runs on the slurmctld host.
- `roles/slurm_acct/` — thin wrapper: `slurm_acct_*` vars → module call +
  unmanaged-entities report.
- `playbooks/` — `site.yml` + sample inventory (fully worked example data).

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
  never reset when omitted from a load file (verified).
- The Cluster line and root's own attributes are never managed (live values
  re-rendered as-is).

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
  committed sample is never modified. It asserts 7 scenarios; keep them in
  sync with the README's CI description when adding phases.
- `sacctmgr dump` emits only explicitly-stored values (inherited values do
  not appear on member lines), and emits user-global fields (DefaultAccount,
  AdminLevel, WCKeys, Coordinator) repeated on every line of the same user.
- The flat file cannot represent `'` or `:` inside values (the `:` field
  separator has no escaping); validation refuses them.

## What's left / roadmap

- **Inventory importer** (DONE): `tools/generate_inventory.py` +
  `playbooks/import.yml`. Driven by `sacctmgr dump` (parsed with the shared
  `parse_flat()`) and `sacctmgr -nP show user/account` for zero-association
  detection — no slurmrestd. Reverses the field-name maps to emit the
  one-file-per-account layout + `slurm_{cluster,qos,users}` group_vars:
  bare-string members when an assoc has no overrides, `default_accounts`
  only for multi-account users, skips/ warns on `normal` and zero-assoc
  entities. Verified by a pure round-trip unit test
  (`tests/unit/test_generate_inventory.py`: import → `resolve()` →
  `compute_plan` vs `parse_flat(dump)` = no changes) and a live round-trip
  acceptance test (`tests/acceptance/import-roundtrip.sh`).
  Not yet done: hoisting unanimous member fields into `association_defaults`
  (currently emits explicit per-member overrides — round-trips, just more
  verbose) and per-account `coordinators` (warns; `Coordinator` is
  user-global in the dump).
- **Configure `root`/`normal` in place** (requested, not yet built): lift the
  declaration refusals in `resolve()` for the `root` account and `normal`
  QOS while keeping the deletion guard, converging their declared fields via
  targeted `sacctmgr modify` (not the load file — avoids finding 7 and root's
  special-casing). Scope agreed: root fairshare + account limits + cluster
  default/allowed QOS (NOT root-user AdminLevel); all QOS fields for `normal`.
  Verify `modify` persistence live on 25.05 + 25.11 first.
- WCKey removal (currently stop-managing only).
- Non-accounting Slurm functionality (the reason the collection is named
  `pescobar.slurm`).

## Conventions

- Pure logic stays in `module_utils` with no Ansible imports; every new
  behavior gets a pytest unit test, and anything touching sacctmgr behavior
  gets verified live first (and documented in `docs/design.md`).
- ansible-lint must pass (config in `.ansible-lint`; run with the collection
  installed so FQCNs resolve — see README).
- CHANGELOG.rst is updated per release; galaxy.yml carries the version.
