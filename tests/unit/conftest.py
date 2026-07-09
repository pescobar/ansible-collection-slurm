import os
import sys

# Import plugins/module_utils/slurm_acct.py directly — it deliberately has no
# Ansible imports so the pure logic is testable without a cluster or ansible.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "plugins", "module_utils"),
)
