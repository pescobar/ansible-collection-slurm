# pescobar.slurm — Slurm management for Ansible

An Ansible collection for managing Slurm. The collection is deliberately
scoped as `pescobar.slurm` (not `slurm_acct`) so future non-accounting
functionality can live here too. It provides a role that **installs and
configures a Slurm cluster** (Ubuntu 26.04, see
[Installing a cluster](#installing-a-cluster-slurm_install)) and **declarative Slurm
accounting** — accounts, users, associations, QOS — built on Slurm's own
declarative mechanism: the desired state is rendered into `sacctmgr`'s
flat-file format and converged with `sacctmgr -i load`, running **on the
slurmctld host over SSH**. No slurmrestd, no JWT tokens.

Verified end-to-end against Slurm **25.05.8, 25.11.6, and 26.05.1**. Every
behavioral claim in this README was confirmed against a live cluster —
see [docs/design.md](docs/design.md) for the full list of verified sacctmgr
semantics and the version differences found.

This project started as the Ansible alternative to
[terraform-provider-slurm](https://github.com/pescobar/terraform-provider-slurm)
and shares its account-centric data model, so a site can move between the
two by copying YAML values.

## Requirements

- **Slurm 25.05 or newer** on the target cluster. QOS entries only appear in
  the `sacctmgr` dump/load flat-file format from Slurm 25.05 onward; on 24.11
  and earlier an accounting load silently rejects QOS entries. The module
  probes `sacctmgr -V` and refuses to run on older releases with a clear
  message. Supported and tested: **25.05, 25.11, 26.05**.
- `sacctmgr` on the target host, configured against the cluster's slurmdbd
  (typically the slurmctld host), reachable over SSH as a Slurm admin.
- `python3` on the target host (for Ansible).

## Contents

| Content | Purpose |
|---|---|
| `pescobar.slurm.slurm_acct` (module) | converge Slurm accounting state; full check/diff support |
| `pescobar.slurm.slurm_acct` (role) | thin wrapper mapping `slurm_acct_*` inventory vars onto the module |
| `pescobar.slurm.slurm_install` (role) | install and configure munge, slurmdbd (MariaDB), slurmctld, slurmd and submit hosts from the Ubuntu 26.04 packages |
| `playbooks/install.yml` | runs `slurm_install` on the `slurm_controller`, `slurm_workers` and `slurm_submit` groups |
| `playbooks/site.yml` + `playbooks/inventory/` | ready-to-run accounting playbook and a fully worked sample inventory |

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

- The built-in **`normal` QOS** is never created or deleted. It MAY be
  converged in place: declared in `slurm_acct_qos` its fields are applied with
  `sacctmgr modify` (never a load file), declared-keys-only.
- The **`root` account and `root` user** are never deleted (root is always
  rendered into the load file and excluded from every deletion path). The
  `root` account's account-level attributes (fairshare, limits, allowed /
  default QOS) MAY be converged by declaring `root` in the accounts data with
  those override fields — they live on the cluster association and are applied
  by the clean-load, declared-keys-only.
- Structurally invalid data (an undeclared `parent_account`, a QOS
  reference not in `slurm_acct_qos`, a duplicate association, a
  multi-account user without a `default_accounts` pin, values containing
  `'`/`:`) is refused **before anything touches the cluster** — this
  matters because `sacctmgr load ... clean` is not transactional.

## Known limitations

- WCKey removal is not implemented (stop-managing only); removing a key
  from Slurm requires manual `sacctmgr` surgery.
- Cluster-level defaults on the Cluster line are not managed, except the
  `root` account's attributes declared in the accounts data (see
  *Protections*). The `root` user's `AdminLevel` is not managed.
- Zero-association users cannot be declared — a user exists only through
  some account's `user_associations`.
- Values containing `'` or `:` cannot be represented in the flat-file
  format and are refused at validation time.

## Installing a cluster (`slurm_install`)

The `slurm_install` role installs Slurm 25.11 from the **Ubuntu 26.04**
archive (the first Ubuntu LTS whose Slurm meets the accounting floor above)
and writes a **static** config: every host gets the same `slurm.conf`.
Configless mode, building packages from source and custom apt repositories
are planned (see Roadmap).

```yaml
# inventory/hosts.yml
slurm_controller:        # exactly one host: slurmctld (+ slurmdbd and MariaDB)
  hosts:
    slurm-master:
slurm_workers:           # slurmd
  hosts:
    slurm-worker-01:
    slurm-worker-02:
slurm_submit:            # client commands only
  hosts:
    login-node:
all:
  vars:
    slurm_install_cluster_name: linux
    slurm_install_dbd_storage_password: "{{ vault_slurmdbd_password }}"
```

```sh
ansible-playbook -i inventory/hosts.yml install.yml   # install + configure
ansible-playbook -i inventory/hosts.yml site.yml      # then load accounting
```

Inventory hostnames must be the machines' short hostnames, and the play must
gather facts on all cluster hosts (nodes are defined from each worker's
sockets, cores, threads and memory). What the role does:

- installs the packages per host type and copies the controller's munge key
  to every host;
- sets up MariaDB (settings sized from the host's RAM), the slurmdbd
  database and user, and slurmdbd; slurmctld registers the cluster itself;
- deploys `slurm.conf` (cons_tres, cgroup v2 task/proctrack plugins,
  accounting enforcement, one partition with all workers) and `cgroup.conf`
  (memory and core limits) to the workers — or, with
  `slurm_install_configless: true`, to the controller only, for slurmctld to
  serve to the rest of the cluster.

Options (see `roles/slurm_install/defaults/main.yml` for all variables):

| Variable | Effect |
|---|---|
| `slurm_install_manage_etc_hosts` | add every cluster host to `/etc/hosts` |
| `slurm_install_configless` | [configless mode](https://slurm.schedmd.com/configless_slurm.html): only the controller holds `slurm.conf`; workers (`slurmd`) and submit hosts (`sackd`) fetch it from slurmctld and cache it under `/run/slurm/conf`. The role adds `enable_configless` to `SlurmctldParameters`, writes `--conf-server` into `/etc/default/{slurmd,sackd}`, installs `sackd` on the submit hosts, removes the local `slurm.conf`/`cgroup.conf` from the non-controller hosts, and runs `scontrol reconfigure` when the config changes |
| `slurm_install_conf_server` | `host[:port]` the workers and submit hosts fetch from (default: the controller on 6817) |
| `slurm_install_slurmctld_parameters` | extra `SlurmctldParameters` (list), e.g. `['cloud_reg_addrs']` |
| `slurm_install_cloud_scheduling` | [elastic nodes](https://slurm.schedmd.com/elastic_computing.html) on OpenStack: Slurm creates a VM per node when jobs need one and deletes it after `slurm_install_cloud_suspend_time` idle seconds. Needs configless mode and DNS that resolves the node names. See the section below |
| `slurm_install_cloud_nodes` | the node groups Slurm may create (name expression, CPUs, memory, image, flavor, network, keypair, security groups) |
| `slurm_install_slurm_conf_template` (and `_cgroup_conf_`, `_slurmdbd_conf_`) | use your own template |
| `slurm_install_slurm_conf_extra` | extra lines appended to the built-in `slurm.conf` (e.g. `GresTypes=gpu`) |
| `slurm_install_config_git_repo` | take `/etc/slurm` from a git repo instead (all files except `slurmdbd.conf`, which always comes from the template because it holds the DB password) |
| `slurm_install_job_submit_lua_template` | deploy `job_submit.lua` and enable the Lua plugin; the role ships `job_submit_autoadd.lua.j2`, which adds unknown users to accounting on their first job (don't combine it with `slurm_acct_purge`) |
| `slurm_install_systemd_overrides` | systemd drop-ins per unit, e.g. `{slurmd: "[Service]\nLimitNOFILE=262144\n"}` |

The role depends on the `ansible.mariadb` collection (installed
automatically with this collection).

### Elastic OpenStack compute nodes

With `slurm_install_cloud_scheduling: true` the controller gets a
`ResumeProgram`/`SuspendProgram` pair that creates and deletes OpenStack VMs,
so idle compute nodes cost nothing. It needs `slurm_install_configless: true`
(a created node has no local `slurm.conf` and fetches it from slurmctld) and
DNS that resolves the node names to the new VMs, e.g. Neutron's internal DNS.

```yaml
slurm_install_configless: true
slurm_install_cloud_scheduling: true
slurm_install_cloud_auth_url: https://keystone.example.org/v3
slurm_install_cloud_application_credential_id: "{{ vaulted_id }}"
slurm_install_cloud_application_credential_secret: "{{ vaulted_secret }}"
slurm_install_cloud_region_name: myregion
slurm_install_cloud_nodes:
  - name: compute-[01-08]
    cpus: 2
    real_memory: 3500
    image: my-compute-image      # must have slurmd + munge key installed
    flavor: c002r004
    network: my-network
    keypair: my-keypair
    security_groups: [default, slurm]
```

Each node group becomes a `State=CLOUD` node line whose `Features` carry the
VM settings; the resume program reads them back with `scontrol show node`.
The role writes the credentials to `/etc/openstack/clouds.yaml` (0600,
SlurmUser) and builds a venv for the OpenStack sdk with
[uv](https://docs.astral.sh/uv/), which it downloads (checksum verified) when
the host has none — no distro python packages are involved, so the same
recipe works on other distros. Point
`slurm_install_cloud_python_interpreter` at another interpreter to skip that.

#### The compute-node image

A created node must boot ready to run jobs: slurmd, the munge key and
whatever the site needs (users, shared filesystems, software).
`playbooks/build_compute_image.yml` builds that image by booting a VM from a
base image, configuring it, cleaning it and snapshotting it to Glance:

```sh
ansible-playbook pescobar.slurm.build_compute_image -e @image-vars.yml
```

```yaml
# image-vars.yml
compute_image_name: my-compute-image-2026-09-20
compute_image_base: "Ubuntu 26.04"
compute_image_flavor: c002r004
compute_image_network: my-network
compute_image_keypair: my-keypair
slurm_compute_image_conf_server: slurm-master:6817
slurm_compute_image_munge_key_host: slurm-master   # or _munge_key_file
compute_image_extra_roles: [my.users, my.nfs_client, my.cvmfs]  # optional
```

The `slurm_compute_image` role does the Slurm side (slurmd in configless
mode, the munge key, slurmd enabled for boot) and, from its `cleanup` task
file, strips what must not be cloned: machine-id, SSH host keys, cloud-init
state, the journal, logs and the slurmd spool. Your own roles run in between,
via `compute_image_extra_roles`. The builder VM is deleted even if the build
fails.

**Known consequence: compute nodes change SSH host keys.** The image ships
without host keys (sharing one identity across every node would let anyone
who can boot the image impersonate a node), so cloud-init generates a fresh
set on each VM's first boot. A node deleted and re-created by the suspend and
resume cycle therefore comes back with a different host key under the same
name and address, and anyone with it in `known_hosts` gets the usual
mismatch warning. Give the users a `StrictHostKeyChecking no` (and
`UserKnownHostsFile /dev/null`) stanza for the compute nodes, which is what
an elastic cluster normally does. If you need stable identities, either have
your resume program inject a per-node key through cloud-init user-data (it
then sits in the instance metadata), or sign each node's freshly generated
key with an SSH certificate authority the clients trust, which needs no
per-node state.

`compute_image_when_exists` decides what an existing image of that name
means: `fail` (the default, so a build never replaces one silently), `skip`
(build nothing, which lets a deploy playbook call the build every time and
only pay for it once) or `replace`.

On a cloud whose flavors have no local disk (`disk=0`, every server is
volume-backed), set `compute_image_volume_size`: the builder then boots from
a volume, and the image is uploaded from that volume once the builder is
gone. Give the cloud nodes the matching `volume_size` so they boot the same
way.

The image is created **private**, and the playbook enforces that: it contains
the cluster's munge key, so anyone able to boot it can authenticate to the
cluster. Note that Glance's "private" means the owning **project**, not one
user; use a separate project or explicit image members if you need less than
that. Building an image needs the `openstack.cloud` collection and an
openstacksdk on the control host.

The programs log every event at INFO to
`/var/log/slurm/dynamic_nodes.log` (rotated weekly) and repeat warnings and
errors to syslog; Slurm does not capture their output itself. A node whose
VM cannot be created is logged and left for Slurm to mark DOWN after
`ResumeTimeout`, and a failure on one node does not stop the rest of the
batch. Add `DebugFlags=Power` to see slurmctld's side of the decisions.

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
SLURM_VERSION=25.05.8 docker compose -f tests/docker/docker-compose.yml up -d --wait --build
./tests/acceptance/run-tests.sh
```

The test-cluster image is self-contained (Slurm compiled from source) and
published to `ghcr.io/pescobar/ansible-collection-slurm/slurm-test:<version>`;
see [`tests/docker/README.md`](tests/docker/README.md) for building and
publishing images.

CI is split by concern:

- **`ci.yml`** — unit tests + ansible-lint, on every push/PR.
- **`acceptance-management.yml`** — the accounts/users/QOS acceptance suite
  across Slurm 25.05.8 / 25.11.6 / 26.05.1 (pulls the published
  images), asserting: initial convergence, full idempotency, check-mode
  accuracy (zero false positives + a correct pending-change diff), value
  modification, purge with `normal`/`root` protection, the malformed-file
  guard, and the `fairshare=parent` round-trip; plus the importer round-trip
  (`import-roundtrip.sh`) and the in-place `root`/`normal` test
  (`root-normal.sh`).
- **`acceptance-install.yml`** — a complete `slurm_install` deployment on an
  `ubuntu-26.04` runner VM (`tests/install/run-tests.sh`), asserting: install,
  a zero-change second run, the node comes up idle and runs jobs, a 1-CPU
  task is confined to one core, memory limits are enforced (a job over `--mem` ends `OUT_OF_MEMORY`), then
  `slurm_acct` loads the sample data (and is a `--check` no-op), a user with
  an association can submit and one without is rejected.
- **`build-test-images.yml`** — builds and pushes the test images to GHCR
  (manual trigger).

`tests/install/run-tests.sh` installs Slurm **on the machine it runs on** —
use a throwaway VM or a privileged systemd container (see `CLAUDE.md`).

## Importing from an existing cluster

To adopt this collection on a cluster that already has accounting state, the
importer reverses the data model: it reads `sacctmgr dump` and writes the
inventory this collection consumes — no hand-transcription.

```bash
ansible-playbook -i inventory/hosts.yml import.yml \
    -e import_output_dir=./imported_inventory
```

The aux playbook (`playbooks/import.yml`) gathers the dump over the same SSH
transport the collection uses and runs `tools/generate_inventory.py` on the
controller, producing a fresh, non-destructive tree:

```
imported_inventory/
  group_vars/slurm_controller/{slurm_cluster,slurm_qos,slurm_users}.yml
  host_vars/<host>/slurm_accounts.d/<account>.yml   # one per account
```

It emits bare-string members where an association has no overrides, splits
member fields into `account_overrides` / `association`, pins `default_account`
only for multi-account users, and warns about anything it cannot represent
(zero-association entities, the built-in `normal` QOS, unknown fields). Add
your `hosts.yml`, then verify the round-trip — it is designed to be a no-op:

```bash
ansible-playbook -i imported_inventory/hosts.yml site.yml --check --diff
```

`tools/generate_inventory.py` also runs standalone on any dump file
(`generate_inventory.py dump.cfg -o out/ --host <name>`).

## Roadmap

- `slurm_install`: CI deployment test on an Ubuntu 26.04 runner VM,
  building Slurm `.deb` packages from source, and custom apt repositories.
