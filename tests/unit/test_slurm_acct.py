# Unit tests for the pure renderer/parser/planner in
# plugins/module_utils/slurm_acct.py. No cluster, no ansible.

import pytest

from slurm_acct import (
    FAIRSHARE_PARENT_SENTINEL,
    SlurmAcctError,
    canonical_text,
    compute_plan,
    merge_account_fragments,
    parse_flat,
    projected_state,
    render,
    resolve,
)


def resolve_sample(**overrides):
    data = dict(
        cluster="linux",
        qos={
            "standard": {"description": "Default QOS", "priority": 100,
                         "max_wall_pj": 1440},
            "debug": {"priority": 250, "max_wall_pj": 30},
        },
        accounts={
            "dept": {
                "description": "Dept",
                "organization": "org",
                "fairshare": 100,
                "user_associations": ["alice"],
            },
            "lab": {
                "parent_account": "dept",
                "default_qos": "standard",
                "allowed_qos": ["standard", "debug"],
                "association_defaults": {
                    "account_overrides": {"fairshare": "parent"},
                },
                "user_associations": [
                    "bob",
                    {"user": "alice",
                     "account_overrides": {"fairshare": 5, "max_jobs": 3},
                     "association": {"partition": "cpu", "max_wall_pj": 240}},
                ],
            },
        },
        admin_levels={"alice": "Operator"},
        default_accounts={"alice": "dept"},
        wckeys={"bob": "projx"},
    )
    data.update(overrides)
    return resolve(**data)


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------


def test_resolve_basic_records():
    state = resolve_sample()
    assert state["accounts"]["lab"]["parent"] == "dept"
    assert state["accounts"]["dept"]["parent"] == "root"
    # defaults filled in like sacctmgr does on create
    assert state["accounts"]["lab"]["fields"]["Description"] == "lab"
    assert state["accounts"]["lab"]["fields"]["Organization"] == "lab"
    assert state["accounts"]["dept"]["fields"]["Fairshare"] == "100"
    # QOS list canonically sorted
    assert state["accounts"]["lab"]["fields"]["QOS"] == "debug,standard"


def test_resolve_association_defaults_precedence():
    state = resolve_sample()
    # bare member inherits the account-wide default
    assert state["assocs"][("bob", "lab", "")]["Fairshare"] == "parent"
    # member's own value wins over the default
    assert state["assocs"][("alice", "lab", "cpu")]["Fairshare"] == "5"
    assert state["assocs"][("alice", "lab", "cpu")]["MaxJobs"] == "3"
    assert state["assocs"][("alice", "lab", "cpu")]["MaxWallDurationPerJob"] == "240"


def test_resolve_user_globals():
    state = resolve_sample()
    assert state["users"]["alice"]["DefaultAccount"] == "dept"
    assert state["users"]["alice"]["AdminLevel"] == "Operator"
    # single-account user's default is derived
    assert state["users"]["bob"]["DefaultAccount"] == "lab"
    assert state["users"]["bob"]["DefaultWCKey"] == "projx"
    assert state["users"]["bob"]["WCKeys"] == "projx"


def test_resolve_fairshare_default_is_one():
    state = resolve_sample()
    assert state["assocs"][("alice", "dept", "")]["Fairshare"] == "1"


def test_multi_account_user_requires_default_pin():
    with pytest.raises(SlurmAcctError) as exc:
        resolve_sample(default_accounts={})
    assert any("alice" in e and "default_accounts" in e for e in exc.value.errors)


def test_default_pin_must_be_member_account():
    with pytest.raises(SlurmAcctError) as exc:
        resolve_sample(default_accounts={"alice": "nonexistent"})
    assert any("nonexistent" in e for e in exc.value.errors)


def test_undefined_parent_refused():
    with pytest.raises(SlurmAcctError) as exc:
        resolve_sample(accounts={
            "child": {"parent_account": "ghost", "user_associations": []},
        }, admin_levels={}, default_accounts={}, wckeys={})
    assert any("ghost" in e and "not transactional" in e for e in exc.value.errors)


def test_parent_cycle_refused():
    with pytest.raises(SlurmAcctError) as exc:
        resolve_sample(accounts={
            "a": {"parent_account": "b", "user_associations": []},
            "b": {"parent_account": "a", "user_associations": []},
        }, admin_levels={}, default_accounts={}, wckeys={})
    assert any("cycle" in e for e in exc.value.errors)


def test_undeclared_qos_reference_refused():
    with pytest.raises(SlurmAcctError) as exc:
        resolve_sample(accounts={
            "acct": {"default_qos": "ghost_qos", "user_associations": []},
        }, admin_levels={}, default_accounts={}, wckeys={})
    assert any("ghost_qos" in e for e in exc.value.errors)


def test_normal_qos_reference_is_allowed():
    state = resolve_sample(accounts={
        "acct": {"default_qos": "normal", "user_associations": ["u"]},
    }, admin_levels={}, default_accounts={}, wckeys={})
    assert state["accounts"]["acct"]["fields"]["DefaultQOS"] == "normal"


def test_system_qos_declaration_refused():
    with pytest.raises(SlurmAcctError) as exc:
        resolve_sample(qos={"normal": {"priority": 1}})
    assert any("built-in system QOS" in e for e in exc.value.errors)


def test_root_account_and_user_refused():
    with pytest.raises(SlurmAcctError) as exc:
        resolve_sample(accounts={
            "root": {"user_associations": []},
            "acct": {"user_associations": ["root"]},
        }, admin_levels={}, default_accounts={}, wckeys={})
    errors = " ".join(exc.value.errors)
    assert "built-in root account" in errors
    assert "'root' user cannot be listed" in errors


def test_duplicate_association_refused():
    with pytest.raises(SlurmAcctError) as exc:
        resolve_sample(accounts={
            "acct": {"user_associations": ["u", "u"]},
        }, admin_levels={}, default_accounts={}, wckeys={})
    assert any("duplicate association" in e for e in exc.value.errors)


def test_fairshare_sentinel_literal_refused():
    with pytest.raises(SlurmAcctError) as exc:
        resolve_sample(accounts={
            "acct": {"fairshare": FAIRSHARE_PARENT_SENTINEL,
                     "user_associations": []},
        }, admin_levels={}, default_accounts={}, wckeys={})
    assert any("parent" in e for e in exc.value.errors)


def test_unsafe_characters_refused():
    with pytest.raises(SlurmAcctError) as exc:
        resolve_sample(accounts={
            "acct": {"description": "has 'quote' and : colon",
                     "user_associations": []},
        }, admin_levels={}, default_accounts={}, wckeys={})
    assert any("cannot be represented safely" in e for e in exc.value.errors)


def test_tres_list_of_objects():
    state = resolve_sample(accounts={
        "acct": {
            "max_tres_per_job": [
                {"type": "gres", "name": "gpu", "count": 8},
                {"type": "cpu", "count": 64},
            ],
            "user_associations": [],
        },
    }, admin_levels={}, default_accounts={}, wckeys={})
    # sorted output regardless of input order
    assert state["accounts"]["acct"]["fields"]["MaxTRESPerJob"] == "cpu=64,gres/gpu=8"


def test_tres_gres_requires_name():
    with pytest.raises(SlurmAcctError) as exc:
        resolve_sample(accounts={
            "acct": {"max_tres_per_job": [{"type": "gres", "count": 8}],
                     "user_associations": []},
        }, admin_levels={}, default_accounts={}, wckeys={})
    assert any("requires a 'name'" in e for e in exc.value.errors)


def test_exception_map_user_without_association_refused():
    with pytest.raises(SlurmAcctError) as exc:
        resolve_sample(admin_levels={"ghost": "Operator"}, default_accounts={},
                       wckeys={})
    assert any("ghost" in e and "no association" in e for e in exc.value.errors)


def test_coordinators_supported():
    state = resolve_sample(accounts={
        "acct": {"coordinators": ["u"], "user_associations": ["u"]},
    }, admin_levels={}, default_accounts={}, wckeys={})
    assert state["users"]["u"]["Coordinator"] == "acct"


# ---------------------------------------------------------------------------
# render()
# ---------------------------------------------------------------------------


def test_render_hierarchy_ordering():
    text = render(resolve_sample())
    lines = text.splitlines()
    # every Parent line references an already-defined account
    defined = {"root"}
    for line in lines:
        if line.startswith("Account - "):
            defined.add(line.split("'")[1])
        elif line.startswith("Parent - "):
            assert line.split("'")[1] in defined, line
    # QOS lines first, then Cluster, then the hierarchy
    assert lines[0].startswith("QOS - 'debug'")
    assert lines[1].startswith("QOS - 'standard'")
    assert lines[2].startswith("Cluster - 'linux'")
    assert lines[3] == "Parent - 'root'"
    assert lines[4].startswith("User - 'root':DefaultAccount='root'")
    # child account appears under its parent's section
    assert text.index("Parent - 'dept'") < text.index("Account - 'lab'")
    assert text.index("Account - 'lab'") < text.index("Parent - 'lab'")


def test_render_quoting_and_fairshare_parent():
    text = render(resolve_sample(accounts={
        "acct": {
            "description": "Physics Lab",
            "association_defaults": {"account_overrides": {"fairshare": "parent"}},
            "user_associations": ["u"],
        },
    }, admin_levels={}, default_accounts={}, wckeys={}))
    # sacctmgr lowercases descriptions on store, so the renderer emits the
    # canonical (lowercased) form up front; the space still needs quoting.
    assert "Description='physics lab'" in text
    # fairshare parent is rendered as the literal keyword, never the sentinel
    assert "Fairshare=parent" in text
    assert FAIRSHARE_PARENT_SENTINEL not in text


def test_render_partition_scoped_association():
    text = render(resolve_sample())
    assert "User - 'alice':Partition='cpu'" in text


def test_render_wckeys_on_user_line():
    text = render(resolve_sample())
    assert "DefaultWCKey='projx'" in text
    assert "WCKeys='projx'" in text


# ---------------------------------------------------------------------------
# parse_flat() + round-trip
# ---------------------------------------------------------------------------

SAMPLE_DUMP = """\
# comment line
QOS - 'normal':Description='Normal QOS default'
QOS - 'standard':Description='standard':MaxWallDurationPerJob=1440:Priority=10
Cluster - 'linux':Fairshare=1:QOS='normal'
Parent - 'root'
User - 'root':DefaultAccount='root':AdminLevel='Administrator':Fairshare=1
Account - 'dept':Description='science: dept':Organization='science':Fairshare=100
Parent - 'dept'
Account - 'lab':Description='lab':Organization='lab':DefaultQOS='standard':Fairshare=2147483647:QOS='standard,gpu_qos'
Parent - 'lab'
User - 'alice':DefaultAccount='lab':AdminLevel='Operator':Fairshare=2147483647
User - 'alice':Partition='cpu':DefaultAccount='lab':AdminLevel='Operator':Fairshare=5:MaxJobs=3
"""


def test_parse_dump_normalization():
    state = parse_flat(SAMPLE_DUMP)
    # fairshare sentinel normalized to "parent"
    assert state["accounts"]["lab"]["fields"]["Fairshare"] == "parent"
    assert state["assocs"][("alice", "lab", "")]["Fairshare"] == "parent"
    # QOS list sorted
    assert state["accounts"]["lab"]["fields"]["QOS"] == "gpu_qos,standard"
    # quoted colon survives splitting
    assert state["accounts"]["dept"]["fields"]["Description"] == "science: dept"
    # partition-scoped association keyed separately
    assert ("alice", "lab", "cpu") in state["assocs"]
    # user-global fields extracted off the association lines
    assert state["users"]["alice"]["AdminLevel"] == "Operator"
    assert "AdminLevel" not in state["assocs"][("alice", "lab", "")]
    # root entities pruned (they are constants on both sides)
    assert "root" not in state["users"]
    assert "root" not in state["accounts"]


def test_render_parse_round_trip():
    desired = resolve_sample()
    assert parse_flat(render(desired)) == desired


# ---------------------------------------------------------------------------
# compute_plan()
# ---------------------------------------------------------------------------


def live_like_desired():
    desired = resolve_sample()
    live = parse_flat(render(desired))
    # what `sacctmgr show` would list alongside (all entities + system extras)
    live_users = sorted(live["users"]) + ["root"]
    live_accounts = sorted(live["accounts"]) + ["root"]
    live_qos = sorted(live["qos"]) + ["normal"]
    return desired, live, live_users, live_accounts, live_qos


def test_plan_in_sync_no_changes():
    desired, live, users, accounts, qos = live_like_desired()
    for purge in (False, True):
        plan = compute_plan(desired, live, purge, live_users=users,
                            live_accounts=accounts, live_qos=qos)
        assert plan["changed"] is False, plan
        assert plan["needs_load"] is False


def test_plan_detects_update():
    desired, live, users, accounts, qos = live_like_desired()
    live["accounts"]["dept"]["fields"]["Fairshare"] = "50"
    plan = compute_plan(desired, live, False, live_users=users,
                        live_accounts=accounts, live_qos=qos)
    assert plan["accounts"]["update"] == ["dept"]
    assert plan["changed"] is True


def test_plan_purge_gates_deletions():
    desired, live, users, accounts, qos = live_like_desired()
    live["qos"]["stray"] = {"Description": "stray"}
    live["assocs"][("mallory", "dept", "")] = {"Fairshare": "1"}
    live["users"]["mallory"] = {"DefaultAccount": "dept"}
    users = users + ["mallory"]
    qos = qos + ["stray"]

    plan = compute_plan(desired, live, False, live_users=users,
                        live_accounts=accounts, live_qos=qos)
    assert plan["changed"] is False
    assert plan["qos"]["unmanaged"] == ["stray"]
    assert plan["users"]["unmanaged"] == ["mallory"]
    assert plan["qos"]["delete"] == []

    plan = compute_plan(desired, live, True, live_users=users,
                        live_accounts=accounts, live_qos=qos)
    assert plan["changed"] is True
    assert plan["qos"]["delete"] == ["stray"]
    assert plan["users"]["delete"] == ["mallory"]
    assert plan["orphan_users"] == ["mallory"]
    assert plan["assocs"]["delete"] == ["mallory@dept"]


def test_plan_never_deletes_protected():
    desired, live, users, accounts, qos = live_like_desired()
    # zero-association orphan visible only in the entity listings
    users = users + ["zombie"]
    plan = compute_plan(desired, live, True, live_users=users,
                        live_accounts=accounts, live_qos=qos)
    assert "zombie" in plan["orphan_users"]
    assert "root" not in plan["orphan_users"]
    assert "root" not in plan["orphan_accounts"]
    assert "normal" not in plan["qos"]["delete"]


def test_plan_qos_order_insensitive():
    desired, live, users, accounts, qos = live_like_desired()
    # a dump emitting the same QOS list in another order must not diff
    assert live["accounts"]["lab"]["fields"]["QOS"] == "debug,standard"
    reparsed = parse_flat(
        render(desired).replace("QOS='debug,standard'", "QOS='standard,debug'"))
    plan = compute_plan(desired, reparsed, True, live_users=users,
                        live_accounts=accounts, live_qos=qos)
    assert plan["changed"] is False


def test_plan_additive_ignores_undeclared_fields():
    # a field removed from inventory (live has it, desired doesn't) is left
    # alone in additive mode ("stop managing"), but resets with purge
    # (`load clean` verifiably resets omitted association fields).
    desired, live, users, accounts, qos = live_like_desired()
    live["assocs"][("alice", "lab", "cpu")]["MaxSubmitJobs"] = "99"
    plan = compute_plan(desired, live, False, live_users=users,
                        live_accounts=accounts, live_qos=qos)
    assert plan["changed"] is False
    plan = compute_plan(desired, live, True, live_users=users,
                        live_accounts=accounts, live_qos=qos)
    assert plan["assocs"]["update"] == ["alice@lab/partition=cpu"]


def test_plan_qos_fields_never_reset():
    # QOS definition fields do not reset when omitted from a load file
    # (verified live), so QOS always compares declared keys only.
    desired, live, users, accounts, qos = live_like_desired()
    live["qos"]["debug"]["GraceTime"] = "60"
    for purge in (False, True):
        plan = compute_plan(desired, live, purge, live_users=users,
                            live_accounts=accounts, live_qos=qos)
        assert plan["changed"] is False, plan


def test_plan_admin_demotion_only_with_purge():
    desired, live, users, accounts, qos = live_like_desired()
    live["users"]["bob"]["AdminLevel"] = "Operator"  # live-only admin right
    plan = compute_plan(desired, live, False, live_users=users,
                        live_accounts=accounts, live_qos=qos)
    assert plan["changed"] is False
    plan = compute_plan(desired, live, True, live_users=users,
                        live_accounts=accounts, live_qos=qos)
    assert plan["users"]["update"] == ["bob"]


def test_render_authoritative_pins_admin_level_none():
    desired = resolve_sample()
    text = render(desired, authoritative=True)
    # bob has no admin_levels entry -> purge render pins the explicit reset
    bob_line = next(l for l in text.splitlines() if l.startswith("User - 'bob'"))
    assert "AdminLevel='None'" in bob_line
    # alice keeps her declared level
    assert "AdminLevel='Operator'" in text
    # additive render leaves bob's line without AdminLevel
    assert "AdminLevel='None'" not in render(desired)


def test_render_wckeys_excluded_from_clean_pass():
    # the clean pass must never carry WCKeys: on 25.11 a WCKeys= on a User
    # line silently aborts the whole clean transaction (verified live).
    desired = resolve_sample()
    clean_text = render(desired, include_wckeys=False)
    assert "WCKey" not in clean_text
    full_text = render(desired)
    assert "DefaultWCKey='projx'" in full_text


def test_parse_drops_admin_level_none():
    state = parse_flat("Parent - 'root'\n"
                       "Account - 'a':Description='a':Organization='a':Fairshare=1\n"
                       "Parent - 'a'\n"
                       "User - 'u':DefaultAccount='a':AdminLevel='None':Fairshare=1\n")
    assert state["users"]["u"] == {"DefaultAccount": "a"}


# ---------------------------------------------------------------------------
# canonical_text() / projected_state() (--diff plumbing)
# ---------------------------------------------------------------------------


def test_diff_texts_equal_when_in_sync():
    desired, live, users, accounts, qos = live_like_desired()
    before = canonical_text(live)
    after = canonical_text(projected_state(desired, live, True))
    assert before == after


def test_diff_shows_purge_deletions_only_with_purge():
    desired, live, users, accounts, qos = live_like_desired()
    live["qos"]["stray"] = {"Description": "stray"}
    # additive mode: stray stays in the projection -> no diff for it
    after = canonical_text(projected_state(desired, live, False))
    assert "stray" in after
    # purge mode: stray disappears from the projection -> visible deletion
    after = canonical_text(projected_state(desired, live, True))
    assert "stray" not in after
    # but system QOS always survives the projection
    live["qos"]["normal"] = {"Description": "Normal QOS default"}
    after = canonical_text(projected_state(desired, live, True))
    assert "QOS normal" in after


def test_orphan_entities_in_diff_text():
    desired, live, users, accounts, qos = live_like_desired()
    text = canonical_text(live, extra_orphan_users=["zombie"])
    assert "User zombie: (no associations)" in text


# --- merge_account_fragments (one-file-per-account sharding) -----------------

def test_merge_fragments_combines_files():
    merged = merge_account_fragments(
        {},
        [
            ("admin.yml", {"admin": {"description": "admin"}}),
            ("lab_bio.yml", {"lab_bio": {"description": "bio"}}),
        ],
    )
    assert set(merged) == {"admin", "lab_bio"}
    assert merged["lab_bio"]["description"] == "bio"


def test_merge_fragments_multiple_accounts_per_file():
    merged = merge_account_fragments(
        {},
        [("labs.yml", {"a": {}, "b": {}})],
    )
    assert set(merged) == {"a", "b"}


def test_merge_fragments_empty_file_ignored():
    merged = merge_account_fragments({}, [("empty.yml", None)])
    assert merged == {}


def test_merge_fragments_inline_base_merges():
    merged = merge_account_fragments(
        {"inline_acct": {"description": "x"}},
        [("f.yml", {"file_acct": {}})],
    )
    assert set(merged) == {"inline_acct", "file_acct"}


def test_merge_fragments_duplicate_across_files_raises():
    with pytest.raises(SlurmAcctError) as exc:
        merge_account_fragments(
            {},
            [("a.yml", {"dup": {}}), ("b.yml", {"dup": {}})],
        )
    msg = "; ".join(exc.value.errors)
    assert "dup" in msg and "a.yml" in msg and "b.yml" in msg


def test_merge_fragments_duplicate_with_inline_raises():
    with pytest.raises(SlurmAcctError) as exc:
        merge_account_fragments({"dup": {}}, [("a.yml", {"dup": {}})])
    assert "inline" in "; ".join(exc.value.errors)


def test_merge_fragments_non_mapping_file_raises():
    with pytest.raises(SlurmAcctError) as exc:
        merge_account_fragments({}, [("bad.yml", ["not", "a", "map"])])
    assert "bad.yml" in "; ".join(exc.value.errors)
