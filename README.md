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
| `playbooks/probe_cloud_node.yml` | boot one throwaway VM per cloud-node flavor, read its CPUs/memory/topology and write them to a vars file |

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
and configures the cluster. Two shapes are supported, and both are best run
in [configless mode](https://slurm.schedmd.com/configless_slurm.html), where
only the controller holds `slurm.conf` and every other host fetches it from
slurmctld:

- a **static cluster**, whose compute nodes are machines you keep running;
- an **elastic cluster** on OpenStack, whose compute nodes slurmctld creates
  when jobs need them and deletes when they go idle.

Both use the same three inventory groups: the controller (exactly one host,
running slurmctld, slurmdbd and MariaDB), the workers (slurmd) and the submit
hosts (the client commands). Inventory hostnames must be the machines' short
hostnames.

### A static cluster

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

    # Only the controller keeps slurm.conf; the workers (slurmd) and the
    # submit hosts (sackd) fetch it from slurmctld and cache it under
    # /run/slurm/conf. Leave this out for a local slurm.conf on every host.
    slurm_install_configless: true

    # The cluster hostnames must resolve. With DNS in place leave this off;
    # set it to true to have the role write /etc/hosts on every node instead.
    slurm_install_manage_etc_hosts: false
```

```sh
ansible-playbook -i inventory/hosts.yml install.yml   # install + configure
ansible-playbook -i inventory/hosts.yml site.yml      # then load accounting
```

The node definitions come from each worker's facts (sockets, cores, threads,
memory), so the play must gather facts on all cluster hosts. Changing the
config later is the same command: the role runs `scontrol reconfigure` so the
hosts pick up the new `slurm.conf` — restarting slurmctld alone does not
refresh their cached copies.

### An elastic cluster (compute nodes created on demand)

Here the workers group is empty or absent: the compute nodes are
`State=CLOUD` entries that exist only in `slurm.conf` until a job needs them,
at which point slurmctld runs the resume program to create a VM from a
prepared image. After `slurm_install_cloud_suspend_time` idle seconds the
suspend program deletes it again. Configless is **required**: a created node
has no config of its own.

```yaml
# inventory/hosts.yml
slurm_controller:
  hosts:
    slurm-master:
slurm_submit:
  hosts:
    login-node:
# no slurm_workers group: the compute nodes are created on demand
all:
  vars:
    slurm_install_cluster_name: linux
    slurm_install_dbd_storage_password: "{{ vault_slurmdbd_password }}"
    slurm_install_configless: true          # required here
    slurm_install_manage_etc_hosts: false   # a created node is in no /etc/hosts

    slurm_install_cloud_scheduling: true
    slurm_install_cloud_nodes:
      - name: compute-[01-08]
        cpus: 2
        real_memory: 3500
        image: my-compute-image     # built by build_compute_image.yml, below
        flavor: c002r004
        network: my-network
        keypair: my-keypair
        security_groups: [default, slurm]
    slurm_install_cloud_suspend_time: 900   # delete after 15 idle minutes

    # Credentials for the resume/suspend programs, written to
    # /etc/openstack/clouds.yaml (0600, SlurmUser) on the controller.
    slurm_install_cloud_auth_url: https://keystone.example.org/v3
    slurm_install_cloud_region_name: myregion
    slurm_install_cloud_application_credential_id: "{{ vault_os_app_cred_id }}"
    slurm_install_cloud_application_credential_secret: "{{ vault_os_app_cred_secret }}"
```

The compute nodes must resolve in DNS, because a re-created node gets a new
IP; the role sets `CommunicationParameters=NoAddrCache` so slurmctld looks
the address up on every connection. Build the image **before** the first job
(see below), then:

```sh
ansible-playbook -i inventory/hosts.yml install.yml
sinfo            # the cloud nodes show as 'idle~': known, not running
srun -N1 hostname
```

`/var/log/slurm/dynamic_nodes.log` on the controller records every create and
delete; warnings and errors also go to syslog. A node is usable once the VM has booted and
slurmd has registered - about two minutes on SWITCH's OpenStack - so keep
`ResumeTimeout` comfortably above that.

The two shapes mix: keep a `slurm_workers` group *and* declare cloud nodes to
get permanent workers plus burst capacity, and use
`slurm_install_cloud_suspend_exc_nodes` for nodes that must never be
suspended.

What the role does:

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

### How the elastic nodes work

Each node group becomes a `State=CLOUD` node line whose `Features` carry the
VM settings; the resume program reads them back with `scontrol show node`.
The role writes the credentials to `/etc/openstack/clouds.yaml` (0600,
SlurmUser) and builds a venv for the OpenStack sdk with
[uv](https://docs.astral.sh/uv/), which it downloads (checksum verified) when
the host has none — no distro python packages are involved, so the same
recipe works on other distros. Point
`slurm_install_cloud_python_interpreter` at another interpreter to skip that.

#### Cleaning up afterwards

The compute nodes and the image are **not** managed by your infrastructure
tool: slurmctld creates the nodes, the build creates the image, so
`tofu destroy` (or the equivalent) leaves them behind, still costing money
and quota. `playbooks/cleanup_cloud_resources.yml` removes them:

```sh
# preview
ansible-playbook -i inventory/hosts.yml pescobar.slurm.cleanup_cloud_resources --check
# delete the leftover nodes and any image builder
ansible-playbook -i inventory/hosts.yml pescobar.slurm.cleanup_cloud_resources
# and the image too
ansible-playbook -i inventory/hosts.yml pescobar.slurm.cleanup_cloud_resources \
  -e '{"slurm_cleanup_images": ["my-compute-image"]}'
```

It matches servers by the cloud node definitions: `compute-[01-04]` deletes
names that are `compute-` followed by digits, so a VM called
`compute-node-other` is left alone. Images are never deleted unless named,
and detached volumes only with `slurm_cleanup_orphan_volumes=true` (a node's
own volume goes with the node; anything else detached may not be yours).

#### What a cloud node has: `probe_cloud_node.yml`

`cpus` and `real_memory` on a `slurm_install_cloud_nodes` entry describe a
node that does not exist yet, so nothing can measure them at configure time -
and they matter: Slurm compares `RealMemory` with what slurmd reports, so a
value taken from the flavor's advertised RAM (the kernel keeps some of it)
leaves the node drained with *Low RealMemory*.

`playbooks/probe_cloud_node.yml` boots one throwaway VM per flavor used in
`slurm_install_cloud_nodes` - same image, network, keypair, security groups
and volume size the real nodes get - reads its facts, deletes it, and writes:

```sh
ansible-playbook -i inventory/hosts.yml pescobar.slurm.probe_cloud_node
# only one flavor
ansible-playbook -i inventory/hosts.yml pescobar.slurm.probe_cloud_node \
  -e slurm_cloud_probe_flavors='[c016r064]'
```

```yaml
# <inventory>/group_vars/all/slurm_cloud_node_facts.yml, written by the run
slurm_install_cloud_node_facts:
  c016r064:
    cpus: 16
    real_memory: 63200      # measured, minus slurm_install_node_memory_reserve_mb
    sockets: 1
    cores_per_socket: 16
    threads_per_core: 1
```

`slurm.conf.j2` uses those where an entry sets nothing itself, so a node group
can be just `name`, `image`, `flavor`, `network`, `keypair` and
`security_groups`; a `cpus:` or `real_memory:` written by hand still wins.
`slurm_install` asserts that every group has both from one source or the
other, so a forgotten probe fails the run with a clear message instead of an
unstartable slurmctld. Run it again when a group's flavor changes, then
re-run the playbook that applies `slurm_install`: the role renders
`slurm.conf` again and its handlers restart slurmctld and push the new config.

An existing facts file means 'already measured': the playbook stops without
booting anything, so a deploy playbook can import it every run and only pay
once. `slurm_cloud_probe_refresh=true` probes again.

Variables: `slurm_cloud_probe_cloud` (clouds.yaml entry; unset uses the `OS_*`
environment), `slurm_cloud_probe_flavors`, `slurm_cloud_probe_ssh_user`
(default `ubuntu`), `slurm_cloud_probe_facts_file`,
`slurm_cloud_probe_refresh`. The probes join the group
`_slurm_cloud_probe`, so a jump host or a different key goes in
`group_vars/_slurm_cloud_probe/`.

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
mode, the munge key, the client commands, slurmd enabled for boot) and, from its `cleanup` task
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

**Authenticating**: the playbook talks to OpenStack from the control host.
By default it passes no cloud name, so openstacksdk uses the `OS_*`
environment — source an openrc file, or `export OS_CLOUD=<entry>` for a
clouds.yaml entry. Set `compute_image_cloud` to name a clouds.yaml entry
explicitly. (`slurm_install_cloud_name` is a different thing: the entry in
the `clouds.yaml` the role writes **on the controller**, for the
resume/suspend programs.) `cleanup_cloud_resources.yml` reads
`compute_image_cloud` the same way.

`compute_image_enabled: false` makes the playbook do nothing at all, so a
deploy playbook can import it unconditionally and a static cluster (no cloud
nodes, no image needed) skips it.

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
