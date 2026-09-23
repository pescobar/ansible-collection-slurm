"""The cloud NodeName lines of slurm.conf.j2 (no cluster, no Ansible).

CPUs/RealMemory/topology come from the slurm_install_cloud_nodes entry when
it sets them, and otherwise from slurm_install_cloud_node_facts, which the
probe_cloud_node playbook writes. Rendering the real template keeps this
honest.
"""

from pathlib import Path

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

ROLE = Path(__file__).resolve().parents[2] / "roles" / "slurm_install"
TEMPLATE = ROLE / "templates" / "slurm.conf.j2"
DEFAULTS = ROLE / "defaults" / "main.yml"


def _resolve(env, value, context):
    """Render a default that refers to other defaults; leave the rest alone."""
    if not isinstance(value, str) or "{{" not in value:
        return value
    return env.from_string(value).render(context)


def render(**overrides):
    env = Environment(undefined=StrictUndefined, keep_trailing_newline=True)
    context = yaml.safe_load(DEFAULTS.read_text())
    context.update(
        {
            # the defaults derive a few values from the inventory
            "groups": {
                "slurm_controller": ["slurm-master"],
                "slurm_workers": [],
                "slurm_submit": [],
            },
            "hostvars": {},
            "ansible_facts": {"architecture": "x86_64", "memtotal_mb": 4096},
            "ansible_managed": "Ansible managed",
            "slurm_install_cloud_scheduling": True,
        }
    )
    context.update(overrides)
    # defaults may reference each other ('{{ slurm_install_partition_name }}')
    for _ in range(3):
        context = {key: _resolve(env, value, context) for key, value in context.items()}
    return env.from_string(TEMPLATE.read_text()).render(context)


def node_line(output):
    return next(line for line in output.splitlines() if line.startswith("NodeName="))


GROUP = {
    "name": "compute-[01-04]",
    "image": "course-compute-node",
    "flavor": "c016r064",
    "network": "UNIBAS",
    "keypair": "opentofu_key",
    "security_groups": ["default"],
}
PROBED = {
    "c016r064": {
        "cpus": 16,
        "real_memory": 63200,
        "sockets": 1,
        "cores_per_socket": 16,
        "threads_per_core": 1,
    }
}


def test_probed_facts_fill_in_the_numbers():
    line = node_line(
        render(
            slurm_install_cloud_nodes=[GROUP],
            slurm_install_cloud_node_facts=PROBED,
        )
    )
    assert "CPUs=16" in line
    assert "RealMemory=63200" in line
    assert "Sockets=1 CoresPerSocket=16 ThreadsPerCore=1" in line
    assert "flavor=c016r064" in line


def test_the_entry_wins_over_the_probe():
    """A hand-written value is a deliberate override, probe or no probe."""
    line = node_line(
        render(
            slurm_install_cloud_nodes=[dict(GROUP, cpus=8, real_memory=32000)],
            slurm_install_cloud_node_facts=PROBED,
        )
    )
    assert "CPUs=8" in line
    assert "RealMemory=32000" in line
    # the topology still comes from the probe
    assert "Sockets=1" in line


def test_no_probe_needs_the_numbers_in_the_entry():
    """Unprobed flavor, values inline: renders, and no topology is invented."""
    line = node_line(
        render(slurm_install_cloud_nodes=[dict(GROUP, cpus=2, real_memory=3500)])
    )
    assert "CPUs=2 RealMemory=3500 Features=" in line


def test_neither_is_an_error():
    """The role asserts this before rendering; the template must not paper over it."""
    with pytest.raises(Exception):
        render(slurm_install_cloud_nodes=[GROUP])
