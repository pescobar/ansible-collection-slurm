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
  (``slurm_acct_purge``), and unconditional protection for the ``normal``
  QOS and the ``root`` account/user.
- ``slurm_acct`` role wrapping the module behind ``slurm_acct_*`` inventory
  variables (account-centric data model shared with
  terraform-provider-slurm's ``examples/big-cluster``).
- One-file-per-account sharding: accounts can be split across
  ``host_vars/<host>/slurm_accounts.d/*.yml`` (default
  ``slurm_acct_accounts_dir``) instead of one large ``slurm_acct_accounts``
  dict; the role merges them and fails on a duplicate account name. The
  ``.d`` suffix keeps Ansible from auto-loading the fragments as host vars.
- Sample playbook + fully worked sample inventory under ``playbooks/``.
- Self-contained docker test stack (``tests/docker/``), 7-scenario live
  acceptance suite (``tests/acceptance/``), pytest unit suite for the pure
  engine logic (``tests/unit/``), and a CI matrix across Slurm 25.05.4,
  25.11.5, and 26.05.1.
- Verified sacctmgr semantics and version differences documented in
  ``docs/design.md``.
