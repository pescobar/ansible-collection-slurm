# Filter plugins for the pescobar.slurm collection.
#
# Thin Ansible-layer wrappers over the pure logic in module_utils/slurm_acct.py
# (which deliberately has no Ansible imports, so it stays unit-testable). The
# merge/validation itself lives there; here we only translate SlurmAcctError
# into AnsibleFilterError so a duplicate account name fails the play cleanly.
from __future__ import absolute_import, division, print_function

__metaclass__ = type

from ansible.errors import AnsibleFilterError

from ansible_collections.pescobar.slurm.plugins.module_utils.slurm_acct import (
    merge_account_fragments as _merge,
    SlurmAcctError,
)


def merge_account_fragments(base, fragments):
    """Merge per-account fragment files into one slurm_acct_accounts map.

    Usage: ``slurm_acct_accounts | pescobar.slurm.merge_account_fragments(frags)``
    where ``frags`` is a list of ``[source, content]`` pairs (the file path and
    its parsed YAML). Fails the play if any account name appears in two files.
    """
    try:
        return _merge(base or {}, fragments or [])
    except SlurmAcctError as exc:
        raise AnsibleFilterError("; ".join(exc.errors))


class FilterModule(object):
    def filters(self):
        return {"merge_account_fragments": merge_account_fragments}
