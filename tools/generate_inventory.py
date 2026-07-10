#!/usr/bin/env python3
# Generate pescobar.slurm inventory variables from a running cluster.
#
# Reverses the collection's own data model: it parses a `sacctmgr dump`
# (via the shared, normalized parse_flat() from module_utils) and re-splits
# the flat records back into the account-centric inventory this collection
# consumes — the one-file-per-account layout plus the qos / users / cluster
# group_vars. The acceptance criterion is a round-trip: import a converged
# cluster, then `--check` against the generated inventory reports no changes.
#
# Driven entirely by `sacctmgr dump` (+ optional entity listings to flag
# zero-association entities). No slurmrestd, no JWT — same transport as the
# rest of the collection. Run it directly against a dump file, or via the
# aux playbook playbooks/import.yml which gathers the dump over SSH.
import argparse
import os
import sys
from collections import Counter

# Import the shared pure logic. parse_flat() already normalizes every record
# (fairshare sentinel -> "parent", sorted lists, canonical keys), so the
# importer only has to reverse the field-name maps.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..",
                                "plugins", "module_utils"))
from slurm_acct import (  # noqa: E402
    parse_flat,
    ACCOUNT_META_FIELDS,
    ACCOUNT_OVERRIDE_FIELDS,
    ASSOCIATION_ONLY_FIELDS,
    QOS_FIELDS,
    SYSTEM_QOS,
)

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required (pip install pyyaml)")

# Reverse maps: flat-file (dump-canonical) key -> inventory snake_case key.
_ACCOUNT_LEVEL_REV = {v: k for k, v in
                      dict(ACCOUNT_META_FIELDS, **ACCOUNT_OVERRIDE_FIELDS).items()}
_OVERRIDE_REV = {v: k for k, v in ACCOUNT_OVERRIDE_FIELDS.items()}
_ASSOC_REV = {v: k for k, v in ASSOCIATION_ONLY_FIELDS.items()}
_QOS_REV = {v: k for k, v in QOS_FIELDS.items()}

# Inventory keys whose value is a list (dump emits comma-joined, sorted).
_LIST_KEYS = {"allowed_qos", "flags"}

# Association fairshare defaults to 1 (resolve() fills it in); omit it so a
# member with no other settings imports as a bare username.
_DEFAULT_MEMBER_FAIRSHARE = "1"

# Order account-body keys for readable, stable output.
_ACCOUNT_KEY_ORDER = [
    "description", "organization", "parent_account", "fairshare",
    "default_qos", "allowed_qos", "max_jobs",
    "max_tres_per_job", "max_tres_per_node", "max_tres_mins_per_job",
    "grp_tres", "grp_tres_mins", "grp_tres_run_mins",
    "coordinators", "association_defaults", "user_associations",
]


def _warn(msg):
    sys.stderr.write("WARNING: %s\n" % msg)


def _value(inv_key, raw):
    """Flat-file value string -> inventory YAML value."""
    if inv_key in _LIST_KEYS:
        return raw.split(",") if raw else []
    if raw.lstrip("-").isdigit():
        return int(raw)
    return raw


def _split_assoc_fields(fields, context):
    """Split one association's fields into account_overrides / association."""
    overrides, assoc = {}, {}
    for canon, raw in sorted(fields.items()):
        if canon in _OVERRIDE_REV:
            key = _OVERRIDE_REV[canon]
            overrides[key] = _value(key, raw)
        elif canon in _ASSOC_REV:
            key = _ASSOC_REV[canon]
            assoc[key] = _value(key, raw)
        else:
            _warn("%s: unsupported association field %s=%s (dropped)"
                  % (context, canon, raw))
    return overrides, assoc


def _hashable(value):
    return tuple(value) if isinstance(value, list) else value


def _hoist_defaults(members, submap, skip=None):
    """Pull member fields shared across an account into an association_defaults
    sub-map, so the common case collapses to bare usernames.

    ``members`` is the list of materialized member records; ``submap`` is
    ``"overrides"`` or ``"assoc"``. A field is hoisted only if it is present on
    *every* member (otherwise a member that never had it would wrongly inherit
    the default), taking its most common value and only when at least two
    members share it (so the defaults block removes more lines than it adds).
    The field is then dropped from every member whose value matches; members
    that differ keep their own explicit value. ``partition`` is association
    identity and never hoisted; ``skip(field, value)`` vetoes a specific hoist.
    Mutates ``members`` and returns the defaults dict.
    """
    if len(members) < 2:
        return {}
    defaults = {}
    common = set.intersection(*[set(m[submap]) for m in members]) - {"partition"}
    for field in sorted(common):
        pairs = [(_hashable(m[submap][field]), m[submap][field]) for m in members]
        counts = Counter(key for key, _ in pairs)
        top = max(counts.values())
        if top < 2:
            continue
        winner_key = sorted((k for k, c in counts.items() if c == top), key=str)[0]
        winner_val = next(val for key, val in pairs if key == winner_key)
        if skip and skip(field, winner_val):
            continue
        defaults[field] = winner_val
        for m in members:
            if _hashable(m[submap][field]) == winner_key:
                del m[submap][field]
    return defaults


def build_inventory(dump_text, known_users=None, known_accounts=None):
    """sacctmgr dump text -> dict of inventory data structures."""
    state = parse_flat(dump_text)
    cluster = state["cluster"]["name"]

    # --- which accounts each user associates with (for default_accounts) ---
    user_accounts = {}
    for (user, account, _part) in state["assocs"]:
        user_accounts.setdefault(user, set()).add(account)

    # --- QOS (skip the built-in system QOS; declaring it is refused) --------
    qos = {}
    for name in sorted(state["qos"]):
        if name in SYSTEM_QOS:
            continue
        body = {}
        for canon, raw in sorted(state["qos"][name].items()):
            key = _QOS_REV.get(canon)
            if key is None:
                _warn("qos %s: unsupported field %s=%s (dropped)" % (name, canon, raw))
                continue
            body[key] = _value(key, raw)
        qos[name] = body

    # --- accounts + members ------------------------------------------------
    accounts = {}
    for name in sorted(state["accounts"]):
        rec = state["accounts"][name]
        body = {}
        for canon, raw in sorted(rec["fields"].items()):
            key = _ACCOUNT_LEVEL_REV.get(canon)
            if key is None:
                _warn("account %s: unsupported field %s=%s (dropped)" % (name, canon, raw))
                continue
            body[key] = _value(key, raw)
        if rec["parent"] and rec["parent"] != "root":
            body["parent_account"] = rec["parent"]

        # Materialize every member's fields first, then hoist values shared
        # across members into association_defaults — otherwise the common case
        # (e.g. every member at fairshare=parent) repeats a per-member override
        # block on each line. Round-trip-safe: hoisted fields inherit back to
        # exactly the value each member had (see _hoist_defaults).
        assocs = sorted((u, p, f) for (u, a, p), f in state["assocs"].items()
                        if a == name)
        member_rows = []
        for user, part, fields in assocs:
            overrides, assoc = _split_assoc_fields(
                dict(fields), "account %s member %s" % (name, user))
            member_rows.append({"user": user, "partition": part,
                                "overrides": overrides, "assoc": assoc})

        default_over = _hoist_defaults(
            member_rows, "overrides",
            skip=lambda field, val: field == "fairshare"
            and val == int(_DEFAULT_MEMBER_FAIRSHARE))
        default_assoc = _hoist_defaults(member_rows, "assoc")

        # A member fairshare of 1 that was NOT hoisted is resolve()'s own
        # default; drop it so the member imports as a bare username.
        if "fairshare" not in default_over:
            for m in member_rows:
                if m["overrides"].get("fairshare") == int(_DEFAULT_MEMBER_FAIRSHARE):
                    del m["overrides"]["fairshare"]

        defaults = {}
        if default_over:
            defaults["account_overrides"] = default_over
        if default_assoc:
            defaults["association"] = default_assoc
        if defaults:
            body["association_defaults"] = defaults

        members = []
        for m in member_rows:
            overrides, assoc = m["overrides"], dict(m["assoc"])
            if m["partition"]:
                assoc["partition"] = m["partition"]
            if not overrides and not assoc:
                members.append(m["user"])
            else:
                entry = {"user": m["user"]}
                if overrides:
                    entry["account_overrides"] = overrides
                if assoc:
                    entry["association"] = assoc
                members.append(entry)
        if members:
            body["user_associations"] = members
        accounts[name] = _ordered(body, _ACCOUNT_KEY_ORDER)

    # --- user exception maps ----------------------------------------------
    admin_levels, default_accounts, wckeys = {}, {}, {}
    for user in sorted(state["users"]):
        u = state["users"][user]
        lvl = u.get("AdminLevel")
        if lvl and lvl != "None":
            admin_levels[user] = lvl
        if u.get("DefaultWCKey"):
            wckeys[user] = u["DefaultWCKey"]
        # Only multi-account users need their default pinned; single-account
        # users derive it from their one membership.
        if u.get("DefaultAccount") and len(user_accounts.get(user, ())) > 1:
            default_accounts[user] = u["DefaultAccount"]
        if u.get("Coordinator"):
            _warn("user %s has Coordinator=%s — per-account coordinators are "
                  "not imported yet; add them by hand" % (user, u["Coordinator"]))

    # --- zero-association entities (unrepresentable) -----------------------
    for a in sorted(set(known_accounts or ()) - set(state["accounts"]) - {"root"}):
        _warn("account %s exists live but has no association — cannot be "
              "represented in inventory (skipped)" % a)
    assoc_users = {u for (u, _a, _p) in state["assocs"]}
    for u in sorted(set(known_users or ()) - assoc_users - {"root"}):
        _warn("user %s exists live but has no association — cannot be "
              "represented in inventory (skipped)" % u)

    return {
        "cluster": cluster,
        "qos": qos,
        "accounts": accounts,
        "admin_levels": admin_levels,
        "default_accounts": default_accounts,
        "wckeys": wckeys,
    }


def _ordered(body, order):
    out = {}
    for key in order:
        if key in body:
            out[key] = body[key]
    for key in body:  # anything not in the order list, appended stably
        if key not in out:
            out[key] = body[key]
    return out


_HEADER = "# Generated by tools/generate_inventory.py from `sacctmgr dump`.\n"


def _dump_yaml(path, data, header=True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        if header:
            fh.write(_HEADER)
        yaml.safe_dump(data, fh, sort_keys=False, default_flow_style=False,
                       allow_unicode=True)


def write_inventory(inv, out_dir, host):
    """Write the inventory tree; return the list of paths written."""
    gv = os.path.join(out_dir, "group_vars", "slurm_controller")
    accts_dir = os.path.join(out_dir, "host_vars", host, "slurm_accounts.d")
    written = []

    _dump_yaml(os.path.join(gv, "slurm_cluster.yml"),
               {"slurm_acct_cluster": inv["cluster"]})
    _dump_yaml(os.path.join(gv, "slurm_qos.yml"),
               {"slurm_acct_qos": inv["qos"]})
    _dump_yaml(os.path.join(gv, "slurm_users.yml"), {
        "slurm_acct_admin_levels": inv["admin_levels"],
        "slurm_acct_default_accounts": inv["default_accounts"],
        "slurm_acct_wckeys": inv["wckeys"],
    })
    written += [os.path.join(gv, f) for f in
                ("slurm_cluster.yml", "slurm_qos.yml", "slurm_users.yml")]

    for name in sorted(inv["accounts"]):
        path = os.path.join(accts_dir, "%s.yml" % name)
        _dump_yaml(path, {name: inv["accounts"][name]})
        written.append(path)
    return written


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("dump", nargs="?", default="-",
                   help="path to `sacctmgr dump <cluster>` output (default: stdin)")
    p.add_argument("-o", "--output", default="imported_inventory",
                   help="output inventory directory (default: ./imported_inventory)")
    p.add_argument("--host", default="slurmctld",
                   help="inventory hostname for host_vars/<host>/ (default: slurmctld)")
    p.add_argument("--users-list",
                   help="file of live usernames (sacctmgr -nP show user format=user) "
                        "to flag zero-association users")
    p.add_argument("--accounts-list",
                   help="file of live account names to flag zero-association accounts")
    args = p.parse_args(argv)

    dump_text = sys.stdin.read() if args.dump == "-" else open(args.dump).read()

    def _read_list(path):
        if not path:
            return None
        with open(path) as fh:
            return [ln.strip() for ln in fh if ln.strip()]

    inv = build_inventory(dump_text,
                          known_users=_read_list(args.users_list),
                          known_accounts=_read_list(args.accounts_list))
    written = write_inventory(inv, args.output, args.host)
    sys.stderr.write("Wrote %d files to %s (cluster %r, %d accounts, %d qos)\n"
                     % (len(written), args.output, inv["cluster"],
                        len(inv["accounts"]), len(inv["qos"])))


if __name__ == "__main__":
    main()
