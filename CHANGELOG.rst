====================
pescobar.slurm 0.1.0
====================

Initial release, extracted from the ``ansible/`` subtree of
`terraform-provider-slurm <https://github.com/pescobar/terraform-provider-slurm>`_
and renamed from ``pescobar.slurm_acct`` to ``pescobar.slurm`` so future
non-accounting Slurm functionality can live in the same collection.

- ``slurm_acct`` module: declarative Slurm accounting (accounts, users,
  associations, QOS) via ``sacctmgr`` flat-file dump/load over SSH to the
  slurmctld host, with full check and diff mode support, opt-in purge
  (``slurm_acct_purge``), and unconditional protection from deletion for the
  ``normal`` QOS and the ``root`` account/user. Requires **Slurm 25.05 or
  newer** (QOS entered the sacctmgr flat-file format in 25.05); the module
  probes ``sacctmgr -V`` and refuses older releases with a clear message.
  Supported and tested: 25.05, 25.11, 26.05.
- In-place convergence of the built-in ``root`` account and ``normal`` QOS:
  declare ``root`` in the accounts data (override fields only — fairshare,
  limits, allowed/default QOS; applied via the cluster association on the
  clean-load) or ``normal`` in ``slurm_acct_qos`` (applied with
  ``sacctmgr modify``, never a load file). Both are declared-keys-only and are
  still never created or deleted. Verified live on 25.05.4 and 25.11.5.
- ``slurm_acct`` role wrapping the module behind ``slurm_acct_*`` inventory
  variables (account-centric data model shared with
  terraform-provider-slurm's ``examples/big-cluster``).
- One-file-per-account sharding: accounts can be split across
  ``host_vars/<host>/slurm_accounts.d/*.yml`` (default
  ``slurm_acct_accounts_dir``) instead of one large ``slurm_acct_accounts``
  dict; the role merges them and fails on a duplicate account name. The
  ``.d`` suffix keeps Ansible from auto-loading the fragments as host vars.
- Inventory importer: ``tools/generate_inventory.py`` plus the aux playbook
  ``playbooks/import.yml`` reverse a running cluster's ``sacctmgr dump`` into
  this collection's inventory layout (per-account fragments +
  ``slurm_{cluster,qos,users}`` group_vars). Round-trip verified — a
  ``site.yml --check`` against the generated inventory is a no-op — by both a
  pure unit test and a live acceptance test.
- ``slurm_install`` role and ``playbooks/install.yml``: install and configure
  a static Slurm 25.11 cluster from the Ubuntu 26.04 archive — munge key
  distribution, MariaDB + slurmdbd, slurmctld, slurmd with ``cgroup.conf``
  memory/core limits, submit hosts — with optional ``/etc/hosts`` management,
  custom templates, extra ``slurm.conf`` lines, config from a git repo, a
  ``job_submit.lua`` plugin (an auto-add-users script is included), and
  systemd drop-ins, and optional `configless mode
  <https://slurm.schedmd.com/configless_slurm.html>`_
  (``slurm_install_configless``: only the controller holds ``slurm.conf``,
  workers and submit hosts fetch it from slurmctld over ``--conf-server``,
  ``sackd`` is installed on the submit hosts, and config changes are pushed
  with ``scontrol reconfigure``), and optional `elastic OpenStack compute
  nodes <https://slurm.schedmd.com/elastic_computing.html>`_
  (``slurm_install_cloud_scheduling``: ``State=CLOUD`` nodes whose VM settings
  ride on the node Features, a resume/suspend program using the OpenStack sdk
  from a uv-built venv, credentials in ``/etc/openstack/clouds.yaml``, and
  logging to ``/var/log/slurm/dynamic_nodes.log`` plus syslog for warnings
  and errors). Tested by a full deployment on an Ubuntu 26.04 runner VM
  (``acceptance-install.yml``: idempotency, core/memory limits, accounting
  enforcement with ``slurm_acct``). Adds a dependency
  on ``ansible.mariadb`` and raises the minimum ansible-core to 2.16.
- ``slurm_compute_image`` role and ``playbooks/build_compute_image.yml``:
  build a compute-node image for the elastic nodes on OpenStack - boot a VM
  from a base image, apply the Slurm side (slurmd in configless mode plus the
  munge key) and your own roles, strip the machine identity and state, then
  snapshot it to a **private** Glance image and delete the builder.
- Sample playbook + fully worked sample inventory under ``playbooks/``.
- Self-contained docker test stack (``tests/docker/``), 7-scenario live
  acceptance suite (``tests/acceptance/``), pytest unit suite for the pure
  engine logic (``tests/unit/``), and a CI matrix across Slurm 25.05.8,
  25.11.6, and 26.05.1.
- Verified sacctmgr semantics and version differences documented in
  ``docs/design.md``.
