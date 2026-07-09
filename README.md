# pescobar.slurm — Slurm management for Ansible

An Ansible collection for managing Slurm. The collection is deliberately
scoped as `pescobar.slurm` (not `slurm_acct`) so future non-accounting
functionality can live here too; today it provides **declarative Slurm
accounting** — accounts, users, associations, QOS — built on Slurm's own
declarative mechanism: the desired state is rendered into `sacctmgr`'s
flat-file format and converged with `sacctmgr -i load`, running **on the
slurmctld host over SSH**. No slurmrestd, no JWT tokens.

Verified end-to-end against Slurm **25.05.4, 25.11.5, and 26.05.1**. Every
behavioral claim in this README was confirmed against a live cluster —
see [docs/design.md](docs/design.md) for the full list of verified sacctmgr
semantics and the version differences found.

This project started as the Ansible alternative to
[terraform-provider-slurm](https://github.com/pescobar/terraform-provider-slurm)
and shares its account-centric data model, so a site can move between the
two by copying YAML values.

## Contents

| Content | Purpose |
|---|---|
| `pescobar.slurm.slurm_acct` (module) | converge Slurm accounting state; full check/diff support |
| `pescobar.slurm.slurm_acct` (role) | thin wrapper mapping `slurm_acct_*` inventory vars onto the module |
| `playbooks/site.yml` + `playbooks/inventory/` | ready-to-run playbook and a fully worked sample inventory |

## Quick start

```sh
ansible-galaxy collection install .          # from a checkout
cd playbooks

# Dry run — the equivalent of `tofu plan` (read-only, complete preview):
ansible-playbook -i inventory/hosts.yml site.yml --check --diff

# Apply (additive-only: creates and updates, never deletes):
ansible-playbook -i inventory/hosts.yml site.yml

# Apply including deletions (explicit opt-in):
ansible-playbook -i inventory/hosts.yml site.yml -e slurm_acct_purge=true
```

## Data model

All data lives in inventory variables (see
`playbooks/inventory/` — `group_vars/slurm_controller/` plus the per-account
`host_vars/slurmctld/slurm_accounts.d/` — for a fully worked example):

| Variable | Content |
|---|---|
| `slurm_acct_cluster` | Slurm cluster name (required) |
| `slurm_acct_qos` | map: QOS name → `description`, `priority`, `max_wall_pj`, `grace_time`, `flags`, `max_jobs_per_user`, `max_tres_per_job` |
| `slurm_acct_accounts` | account-centric map: account name → metadata + limits + `association_defaults` + `coordinators` + `user_associations` (usually sharded one-file-per-account, see below) |
| `slurm_acct_accounts_dir` | directory of per-account fragment files merged into `slurm_acct_accounts` (default `host_vars/<host>/slurm_accounts.d`; see below) |
| `slurm_acct_admin_levels` | exceptions map: user → `Operator`/`Administrator` |
| `slurm_acct_default_accounts` | exceptions map: multi-account user → login default account |
| `slurm_acct_wckeys` | exceptions map: user → default WCKey |
| `slurm_acct_purge` | enable deletions (default `false`) |

Each `user_associations` entry is a bare username (no overrides) or the
object form:

```yaml
slurm_acct_accounts:
  lab_physics:
    description: Physics Lab
    fairshare: 100
    default_qos: standard
    allowed_qos: [standard, high, long, gpu]
    max_tres_per_job:
      - type: cpu
        count: 64
      - type: gres
        name: gpu
        count: 8
    association_defaults:          # applied to every member that doesn't
      account_overrides:           # set the field itself
        fairshare: parent
    user_associations:
      - alice                      # bare: no overrides
      - user: john
        account_overrides:         # fields the account also has
          max_tres_per_job:
            - {type: cpu, count: 64}
            - {type: gres, name: gpu, count: 2}
        association:               # association-only fields
          partition: cpu
```

`account_overrides` carries the fields an account also has (`fairshare`,
`default_qos`, `allowed_qos`, `max_jobs`, the six TRES limits);
`association` carries association-only fields (`partition`, `priority`,
job-count and wall-clock limits). `fairshare: parent` is supported
everywhere fairshare is.

### Sharding accounts (one file per account)

A single `slurm_acct_accounts` dict does not scale to hundreds of accounts:
it becomes a multi-thousand-line file where one stray indentation shifts a
key into the wrong account. Instead, drop **one file per account** in
`host_vars/<host>/slurm_accounts.d/` (each file a `account_name: {…}` map);
the role globs and merges them into `slurm_acct_accounts`, **failing the play
if any account name is defined in two files**. This is the default layout of
the sample inventory (`playbooks/inventory/host_vars/slurmctld/slurm_accounts.d/`).

```
inventory/
  hosts.yml
  group_vars/slurm_controller/{slurm_cluster,slurm_qos,slurm_users}.yml
  host_vars/slurmctld/slurm_accounts.d/
    admin.yml        # admin: { … }
    lab_bio.yml      # lab_bio: { … }
    ...              # one per account
```

The directory is `slurm_acct_accounts_dir`, defaulting to
`{{ inventory_dir }}/host_vars/{{ inventory_hostname }}/slurm_accounts.d`. The
`.d` suffix is load-bearing: Ansible skips extensioned subdirectories of
`host_vars/<host>/`, so the fragments are read only by the role's merge, not
auto-loaded as host vars. Fragments merge on top of any inline
`slurm_acct_accounts`, so you can mix the two during a migration; set
`slurm_acct_accounts_dir: ""` to use a single inline dict only.

Two things this collection manages that the REST API cannot:

- **`coordinators`** (per-account list of usernames) — the flat file's
  `Coordinator=` round-trips through dump/load.
- **WCKeys are fully readable**: dumps include `WCKeys=`/`DefaultWCKey=`, so
  drift on `slurm_acct_wckeys` is detected and shown in `--check --diff`.

## The dry-run contract

`ansible-playbook --check --diff` is a complete, safe preview:

- in check mode only `sacctmgr dump` / `sacctmgr show` reads run — nothing
  can mutate slurmdbd;
- `--diff` shows the full normalized before/after, including the
  associations, QOS, and orphan users/accounts that **would** be deleted
  when `slurm_acct_purge=true`;
- an inventory matching the live cluster reports zero changes (asserted by
  the acceptance tests on every supported Slurm version);
- the module result's `plan` field carries the same information as
  machine-readable per-entity `create`/`update`/`delete`/`unmanaged` lists.

## Additive vs purge mode

- **Additive (default)**: creates and updates only. Entities or fields
  present live but absent from inventory are reported as `unmanaged` and
  left alone — removing a field from inventory means "stop managing it"
  (remove ≠ reset).
- **Purge (`slurm_acct_purge=true`)**: everything not in inventory is
  removed — undeclared associations (the server-side diff **preserves
  association ids, and therefore fairshare usage, of unchanged entries**),
  undeclared QOS, and orphaned zero-association users/accounts. Field
  comparison is exact, so a field removed from inventory genuinely resets.
  Exception: QOS definition fields never reset when omitted (a verified
  sacctmgr behavior) — change QOS limits by setting them explicitly.

### Protections (unconditional)

- The built-in **`normal` QOS** can neither be declared, modified, nor
  deleted.
- The **`root` account and `root` user** are always rendered into the load
  file and excluded from every deletion path.
- Structurally invalid data (an undeclared `parent_account`, a QOS
  reference not in `slurm_acct_qos`, a duplicate association, a
  multi-account user without a `default_accounts` pin, values containing
  `'`/`:`) is refused **before anything touches the cluster** — this
  matters because `sacctmgr load ... clean` is not transactional.

## Known limitations

- WCKey removal is not implemented (stop-managing only); removing a key
  from Slurm requires manual `sacctmgr` surgery.
- The Cluster line (cluster-level fairshare / default QOS list) and
  `root`'s own attributes are not managed.
- Zero-association users cannot be declared — a user exists only through
  some account's `user_associations`.
- Values containing `'` or `:` cannot be represented in the flat-file
  format and are refused at validation time.

## Development / testing

```sh
pip install -r tests/requirements.txt

# unit tests for the pure render/parse/plan logic (no cluster needed):
python -m pytest tests/unit/

# lint:
ansible-galaxy collection install --force . -p /tmp/collections
ANSIBLE_COLLECTIONS_PATH=/tmp/collections ansible-lint

# full acceptance suite against a local docker cluster over SSH:
./tests/docker/generate-ssh-key.sh
SLURM_VERSION=25.05.4 docker compose -f tests/docker/docker-compose.yml up -d --wait --build
./tests/acceptance/run-tests.sh
```

CI (`.github/workflows/ci.yml`) runs the unit tests, ansible-lint, and the
acceptance suite across Slurm 25.05.4 / 25.11.5 / 26.05.1, asserting:
initial convergence, full idempotency, check-mode accuracy (zero false
positives + a correct pending-change diff), value modification, purge with
`normal`/`root` protection, the malformed-file guard, and the
`fairshare=parent` round-trip.

## Roadmap

- An inventory **importer** driven by native Slurm commands (`scontrol`,
  `sacctmgr`) to bootstrap the group_vars from an existing cluster —
  planned, not yet implemented.
- Non-accounting Slurm functionality (hence the collection name).
