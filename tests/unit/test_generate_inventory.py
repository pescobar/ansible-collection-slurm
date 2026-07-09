# Unit tests for tools/generate_inventory.py (the inventory importer).
# Pure, cluster-free: the core proof is an in-process round-trip — a dump
# parsed, reversed into inventory, fed back through resolve(), and planned
# against the same dump must show no creates/updates/deletes.

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))

from slurm_acct import compute_plan, parse_flat, resolve  # noqa: E402
from generate_inventory import build_inventory  # noqa: E402


# A dump exercising every reverse-mapping path: parent account, account-level
# limits, account_overrides (QOS list + TRES), association-only fields,
# partition scoping, fairshare=1 (bare member) and fairshare=parent, a
# multi-account user, an admin level, a wckey, and the built-in normal QOS.
DUMP = """\
QOS - 'normal':Description='Normal QOS default'
QOS - 'standard':Description='default qos':MaxWallDurationPerJob=1440:Priority=100
QOS - 'gpu':Description='gpu':MaxWallDurationPerJob=2880:Priority=200
Cluster - 'linux':Fairshare=1:QOS='normal'
Parent - 'root'
User - 'root':DefaultAccount='root':AdminLevel='Administrator':Fairshare=1
Account - 'dept':Description='dept':Organization='org':Fairshare=100:MaxJobs=50
Parent - 'dept'
Account - 'lab':Description='lab':Organization='org':DefaultQOS='standard':Fairshare=80:MaxTRESPerJob=cpu=64,gres/gpu=8:QOS='gpu,standard'
User - 'alice':DefaultAccount='dept':AdminLevel='Operator':Fairshare=1
Parent - 'lab'
User - 'alice':DefaultAccount='dept':Fairshare=2147483647
User - 'bob':DefaultAccount='lab':DefaultWCKey='projx':WCKeys='projx':Fairshare=5:MaxWallDurationPerJob=240:Priority=20:QOS='gpu,standard':DefaultQOS='gpu'
User - 'carol':Partition='cpu':DefaultAccount='lab':Fairshare=1
"""


def _plan_from_import(dump):
    inv = build_inventory(dump)
    desired = resolve(
        cluster=inv["cluster"], qos=inv["qos"], accounts=inv["accounts"],
        admin_levels=inv["admin_levels"],
        default_accounts=inv["default_accounts"], wckeys=inv["wckeys"],
    )
    live = parse_flat(dump)
    return compute_plan(desired, live, purge=False), inv


def test_roundtrip_no_changes():
    plan, _ = _plan_from_import(DUMP)
    # additive plan: nothing to create, update, or delete anywhere
    for section in ("qos", "accounts", "assocs", "users"):
        assert plan[section]["create"] == [], (section, plan[section]["create"])
        assert plan[section]["update"] == [], (section, plan[section]["update"])
        assert plan[section]["delete"] == [], (section, plan[section]["delete"])


def test_normal_qos_skipped():
    plan, inv = _plan_from_import(DUMP)
    assert "normal" not in inv["qos"]              # never imported (declaring it is refused)
    assert "normal" not in plan["qos"]["delete"]   # protected: never scheduled for deletion


def test_bare_member_when_no_overrides():
    inv = build_inventory(DUMP)
    # carol has only a partition; alice-in-dept has only default fairshare=1
    dept_members = inv["accounts"]["dept"]["user_associations"]
    assert "alice" in dept_members  # bare string, fairshare=1 omitted


def test_override_and_association_split():
    inv = build_inventory(DUMP)
    bob = next(m for m in inv["accounts"]["lab"]["user_associations"]
               if isinstance(m, dict) and m["user"] == "bob")
    # account-level fields land under account_overrides...
    assert bob["account_overrides"]["default_qos"] == "gpu"
    assert bob["account_overrides"]["allowed_qos"] == ["gpu", "standard"]
    # ...association-only fields under association
    assert bob["association"]["priority"] == 20
    assert bob["association"]["max_wall_pj"] == 240


def test_partition_scoped_member():
    inv = build_inventory(DUMP)
    carol = next(m for m in inv["accounts"]["lab"]["user_associations"]
                 if isinstance(m, dict) and m["user"] == "carol")
    assert carol["association"]["partition"] == "cpu"


def test_fairshare_parent_preserved():
    inv = build_inventory(DUMP)
    alice = next(m for m in inv["accounts"]["lab"]["user_associations"]
                 if isinstance(m, dict) and m["user"] == "alice")
    assert alice["account_overrides"]["fairshare"] == "parent"


def test_default_accounts_only_for_multi_account_users():
    inv = build_inventory(DUMP)
    # alice is in dept + lab -> pinned; bob/carol single-account -> derived
    assert inv["default_accounts"] == {"alice": "dept"}


def test_admin_levels_and_wckeys():
    inv = build_inventory(DUMP)
    assert inv["admin_levels"] == {"alice": "Operator"}
    assert inv["wckeys"] == {"bob": "projx"}


def test_parent_account_emitted():
    inv = build_inventory(DUMP)
    assert inv["accounts"]["lab"]["parent_account"] == "dept"
    assert "parent_account" not in inv["accounts"]["dept"]  # parent is root


def test_zero_association_entity_warned(capsys):
    build_inventory(DUMP, known_users=["ghost"], known_accounts=["empty_acct"])
    err = capsys.readouterr().err
    assert "ghost" in err and "empty_acct" in err
