# -*- coding: utf-8 -*-
# Pure, cluster-independent logic for the pescobar.slurm collection:
#
#   1. resolve()      account-centric inventory data  ->  desired-state records
#   2. render()       desired-state records           ->  sacctmgr flat file
#   3. parse_flat()   sacctmgr dump output            ->  live-state records
#   4. compute_plan() desired vs live                 ->  categorized change plan
#   5. canonical_text() records                       ->  stable text for --diff
#
# Everything in this file is side-effect free and unit-tested without a
# cluster (tests/unit/). All sacctmgr flat-file semantics used here were
# verified against live Slurm clusters — see the collection README.
#
# No Ansible imports on purpose: the unit tests import this file directly.

from __future__ import absolute_import, division, print_function

__metaclass__ = type

# Slurm stores "Fairshare=parent" as INT32_MAX (SLURMDB_FS_USE_PARENT) and
# `sacctmgr dump` emits the raw sentinel. Verified identical on 25.05/25.11/26.05.
FAIRSHARE_PARENT_SENTINEL = "2147483647"

# Built-in QOS that must never be created, modified, or deleted by us.
SYSTEM_QOS = ("normal",)

# Built-in entities that must never be deleted, no matter what the
# inventory says. `load ... clean` itself never touches them (they are
# always rendered into the file), and the purge steps skip them explicitly.
PROTECTED_ACCOUNTS = ("root",)
PROTECTED_USERS = ("root",)

VALID_ADMIN_LEVELS = ("None", "Operator", "Administrator")

# ---------------------------------------------------------------------------
# Field maps: inventory key -> flat-file key.
# The flat-file keys are exactly what `sacctmgr dump` emits, so a rendered
# file and a dump normalize to identical records when in sync.
# ---------------------------------------------------------------------------

# Keys valid at account level AND under a member's `account_overrides`
# (same split as examples/big-cluster — "fields slurm_account also has").
ACCOUNT_OVERRIDE_FIELDS = {
    "fairshare": "Fairshare",
    "default_qos": "DefaultQOS",
    "allowed_qos": "QOS",
    "max_jobs": "MaxJobs",
    "max_tres_per_job": "MaxTRESPerJob",
    "max_tres_per_node": "MaxTRESPerNode",
    "max_tres_mins_per_job": "MaxTRESMinsPerJob",
    "grp_tres": "GrpTRES",
    "grp_tres_mins": "GrpTRESMins",
    "grp_tres_run_mins": "GrpTRESRunMins",
}

# Keys valid only under a member's `association` sub-map (no account-level
# equivalent — declared, never overridden).
ASSOCIATION_ONLY_FIELDS = {
    "partition": "Partition",
    "priority": "Priority",
    "max_jobs_accrue": "MaxJobsAccrue",
    "max_submit_jobs": "MaxSubmitJobs",
    "max_wall_pj": "MaxWallDurationPerJob",
    "grp_jobs": "GrpJobs",
    "grp_jobs_accrue": "GrpJobsAccrue",
    "grp_submit_jobs": "GrpSubmitJobs",
    "grp_wall": "GrpWall",
}

# Account-level metadata (not valid under account_overrides).
ACCOUNT_META_FIELDS = {
    "description": "Description",
    "organization": "Organization",
}

TRES_FIELD_KEYS = (
    "max_tres_per_job",
    "max_tres_per_node",
    "max_tres_mins_per_job",
    "grp_tres",
    "grp_tres_mins",
    "grp_tres_run_mins",
)

# QOS definition fields. Flat-file key names are dump-canonical (verified:
# load accepts MaxJobsPerUser but dump re-emits MaxJobsPU — render the dump
# form so round-trips are stable).
QOS_FIELDS = {
    "description": "Description",
    "priority": "Priority",
    "max_wall_pj": "MaxWallDurationPerJob",
    "grace_time": "GraceTime",
    "flags": "Flags",
    "max_jobs_per_user": "MaxJobsPU",
    "max_tres_per_job": "MaxTRESPerJob",
}

# Aliases sacctmgr accepts on input but canonicalizes on dump; parsing maps
# them so hand-written files and dumps compare equal.
_KEY_ALIASES = {
    "maxjobsperuser": "MaxJobsPU",
    "fairshare": "Fairshare",
    "share": "Fairshare",
}

# Fields on a User line that describe the *user entity*, not one
# association. Dump repeats them on every line of the same user.
USER_GLOBAL_KEYS = ("DefaultAccount", "AdminLevel", "DefaultWCKey", "WCKeys", "Coordinator")

# Every flat-file key we know, keyed by lowercase, for canonicalization.
_ALL_KEYS = {}
for _m in (ACCOUNT_OVERRIDE_FIELDS, ASSOCIATION_ONLY_FIELDS, ACCOUNT_META_FIELDS, QOS_FIELDS):
    for _k in _m.values():
        _ALL_KEYS[_k.lower()] = _k
for _k in USER_GLOBAL_KEYS:
    _ALL_KEYS[_k.lower()] = _k


class SlurmAcctError(Exception):
    """Validation or parse error. .errors is a list of messages."""

    def __init__(self, errors):
        if isinstance(errors, str):
            errors = [errors]
        self.errors = list(errors)
        super(SlurmAcctError, self).__init__("; ".join(self.errors))


# ---------------------------------------------------------------------------
# Value canonicalization
# ---------------------------------------------------------------------------


def canon_key(key):
    k = key.strip()
    return _KEY_ALIASES.get(k.lower(), _ALL_KEYS.get(k.lower(), k))


def canon_fairshare(value):
    v = str(value).strip().strip("'")
    if v == FAIRSHARE_PARENT_SENTINEL:
        return "parent"
    return v


def canon_list(value):
    """Comma-separated list -> sorted, deduplicated (dump sorts QOS lists)."""
    items = [i for i in str(value).strip().strip("'").split(",") if i]
    return ",".join(sorted(set(items)))


def canon_tres(value):
    """'cpu=64,gres/gpu=8' -> sorted by TRES name."""
    parts = [p for p in str(value).strip().strip("'").split(",") if p]
    return ",".join(sorted(parts))


def canon_value(key, value):
    key = canon_key(key)
    v = str(value).strip()
    if v.startswith("'") and v.endswith("'") and len(v) >= 2:
        v = v[1:-1]
    if key == "Fairshare":
        return canon_fairshare(v)
    if key in ("QOS", "WCKeys", "Coordinator", "Flags"):
        return canon_list(v)
    if key.endswith("TRES") or "TRES" in key:
        return canon_tres(v)
    if key in ("Description", "Organization"):
        # sacctmgr lowercases these on store (verified: loading
        # Description='Biology Lab' dumps back as 'biology lab').
        return v.lower()
    return v


def tres_to_string(value, context):
    """Inventory TRES value -> 'cpu=64,gres/gpu=8'.

    Accepts the examples/big-cluster list-of-objects form
    ([{type: cpu, count: 64}, {type: gres, name: gpu, count: 8}]) or an
    already-flat string.
    """
    if isinstance(value, str):
        return canon_tres(value)
    if not isinstance(value, list):
        raise SlurmAcctError(
            "%s: TRES value must be a list of {type, name, count} maps or a "
            "'cpu=N,gres/gpu=N' string, got %r" % (context, value)
        )
    parts = []
    for entry in value:
        if not isinstance(entry, dict) or "type" not in entry or "count" not in entry:
            raise SlurmAcctError(
                "%s: each TRES entry needs 'type' and 'count' (and 'name' for "
                "generic resources like gres), got %r" % (context, entry)
            )
        tres = str(entry["type"])
        name = entry.get("name")
        if name:
            tres = "%s/%s" % (tres, name)
        elif tres == "gres":
            raise SlurmAcctError(
                "%s: TRES type 'gres' requires a 'name' (e.g. name: gpu)" % context
            )
        parts.append("%s=%s" % (tres, entry["count"]))
    return canon_tres(",".join(parts))


def quote(value):
    return "'%s'" % value


def _check_token(value, context, errors):
    v = str(value)
    if "'" in v or ":" in v or "\n" in v:
        errors.append(
            "%s: value %r contains a quote, colon, or newline — these cannot be "
            "represented safely in the sacctmgr flat-file format" % (context, v)
        )


# ---------------------------------------------------------------------------
# 1. resolve(): account-centric inventory data -> desired-state records
# ---------------------------------------------------------------------------
#
# State records (shared by resolve() and parse_flat()):
#   {
#     "cluster": {"name": str, "fields": {key: canonical str}},
#     "qos":     {name: {key: canonical str}},
#     "accounts": {name: {"parent": str, "fields": {key: str}}},
#     "assocs":  {(user, account, partition): {key: str}},   # partition "" if unscoped
#     "users":   {name: {"DefaultAccount": ..., "AdminLevel": ...,
#                         "DefaultWCKey": ..., "WCKeys": ..., "Coordinator": ...}},
#   }


def _member_entry(entry, account, errors):
    """Normalize a user_associations entry (bare string or object form)."""
    if isinstance(entry, str):
        return entry, {}, {}
    if isinstance(entry, dict) and "user" in entry:
        overrides = entry.get("account_overrides") or {}
        assoc = entry.get("association") or {}
        unknown = set(entry) - {"user", "account_overrides", "association"}
        if unknown:
            errors.append(
                "account %r member %r: unknown keys %s (fields go under "
                "'account_overrides' or 'association')"
                % (account, entry["user"], sorted(unknown))
            )
        for key in overrides:
            if key not in ACCOUNT_OVERRIDE_FIELDS:
                errors.append(
                    "account %r member %r: %r is not a valid account_overrides "
                    "key (valid: %s)"
                    % (account, entry["user"], key, ", ".join(sorted(ACCOUNT_OVERRIDE_FIELDS)))
                )
        for key in assoc:
            if key not in ASSOCIATION_ONLY_FIELDS:
                errors.append(
                    "account %r member %r: %r is not a valid association key "
                    "(valid: %s)"
                    % (account, entry["user"], key, ", ".join(sorted(ASSOCIATION_ONLY_FIELDS)))
                )
        return str(entry["user"]), overrides, assoc
    errors.append(
        "account %r: user_associations entries must be a bare username or a "
        "map with a 'user' key, got %r" % (account, entry)
    )
    return None, {}, {}


def _field_value(key, value, context):
    """Inventory value -> canonical flat-file string for one field."""
    if key in ("description", "organization"):
        # sacctmgr lowercases these on store; canonicalize up front so the
        # rendered file and the dump compare equal.
        return str(value).lower()
    if key in TRES_FIELD_KEYS:
        return tres_to_string(value, context)
    if key == "allowed_qos" or key == "flags":
        if isinstance(value, list):
            return canon_list(",".join(str(v) for v in value))
        return canon_list(value)
    if key == "fairshare":
        v = str(value)
        if v == FAIRSHARE_PARENT_SENTINEL:
            raise SlurmAcctError(
                "%s: fairshare %s is Slurm's internal sentinel for 'parent' — "
                "write fairshare: parent instead" % (context, v)
            )
        if v != "parent" and not v.isdigit():
            raise SlurmAcctError(
                "%s: fairshare must be an integer weight or the string "
                "'parent', got %r" % (context, value)
            )
        return v
    return str(value)


def merge_account_fragments(base, fragments):
    """Merge one-file-per-account inventory fragments into one accounts map.

    ``base`` is an inline ``slurm_acct_accounts`` mapping (may be empty/None).
    ``fragments`` is an iterable of ``(source, content)`` pairs, where
    ``source`` is a label (typically the file path) and ``content`` is a
    mapping of account-name -> attributes (usually one key, but several are
    allowed) or None for an empty file. Raises SlurmAcctError naming both
    sources if any account name is defined more than once — the guard that
    makes sharding safe, since a plain dict merge would silently drop one.
    """
    merged = {}
    origin = {}
    errors = []

    def take(source, content):
        if content is None:
            return
        if not isinstance(content, dict):
            errors.append(
                "account fragment %s must be a mapping of account name -> "
                "attributes, got %s" % (source, type(content).__name__))
            return
        for name, body in content.items():
            if name in origin:
                errors.append(
                    "account '%s' is defined in both %s and %s — each account "
                    "must appear in exactly one file" % (name, origin[name], source))
                continue
            merged[name] = body
            origin[name] = source

    if base:
        take("slurm_acct_accounts (inline)", base)
    for source, content in fragments:
        take(source, content)

    if errors:
        raise SlurmAcctError(errors)
    return merged


def resolve(cluster, qos=None, accounts=None, admin_levels=None,
            default_accounts=None, wckeys=None):
    """Build desired-state records from account-centric inventory data.

    Raises SlurmAcctError listing every validation problem found; on success
    the returned records are structurally safe to render and load.
    """
    qos = qos or {}
    accounts = accounts or {}
    admin_levels = admin_levels or {}
    default_accounts = default_accounts or {}
    wckeys = wckeys or {}
    errors = []

    state = {
        "cluster": {"name": str(cluster), "fields": {"Fairshare": "1", "QOS": "normal"}},
        "qos": {},
        "accounts": {},
        "assocs": {},
        "users": {},
    }

    # ---- QOS ----
    for name in sorted(qos):
        if name in SYSTEM_QOS:
            errors.append(
                "qos %r: Slurm's built-in system QOS must not be managed here "
                "(see the provider's Bug 3 / system-QOS notes)" % name
            )
            continue
        _check_token(name, "qos name", errors)
        spec = qos[name] or {}
        fields = {}
        for key in sorted(spec):
            if key not in QOS_FIELDS:
                errors.append(
                    "qos %r: unknown key %r (valid: %s)"
                    % (name, key, ", ".join(sorted(QOS_FIELDS)))
                )
                continue
            try:
                fields[QOS_FIELDS[key]] = _field_value(key, spec[key], "qos %r" % name)
            except SlurmAcctError as exc:
                errors.extend(exc.errors)
        fields.setdefault("Description", str(name).lower())
        _check_token(fields["Description"], "qos %r description" % name, errors)
        state["qos"][str(name)] = fields

    declared_qos = set(state["qos"]) | set(SYSTEM_QOS)

    def _check_qos_refs(fields, context):
        for key in ("DefaultQOS", "QOS"):
            if key in fields:
                for ref in fields[key].split(","):
                    if ref and ref not in declared_qos:
                        errors.append(
                            "%s references QOS %r which is not declared in the "
                            "qos map — a load would fail midway (and 'clean' is "
                            "not transactional)" % (context, ref)
                        )

    # ---- Accounts + associations ----
    membership = {}  # user -> [account, ...]

    for name in sorted(accounts):
        if name in PROTECTED_ACCOUNTS:
            errors.append(
                "account %r is Slurm's built-in root account and cannot be "
                "declared in the accounts map" % name
            )
            continue
        _check_token(name, "account name", errors)
        spec = accounts[name] or {}
        parent = str(spec.get("parent_account", "root"))
        fields = {}
        for key in sorted(spec):
            if key in ("parent_account", "user_associations", "association_defaults",
                       "coordinators", "name"):
                continue
            target = ACCOUNT_META_FIELDS.get(key) or ACCOUNT_OVERRIDE_FIELDS.get(key)
            if target is None:
                errors.append(
                    "account %r: unknown key %r (valid: %s)"
                    % (name, key,
                       ", ".join(sorted(list(ACCOUNT_META_FIELDS)
                                        + list(ACCOUNT_OVERRIDE_FIELDS)
                                        + ["parent_account", "user_associations",
                                           "association_defaults", "coordinators"])))
                )
                continue
            try:
                fields[target] = _field_value(key, spec[key], "account %r" % name)
            except SlurmAcctError as exc:
                errors.extend(exc.errors)
        # sacctmgr defaults both to the account name on create; render them
        # explicitly so dumps and renders always carry the same keys.
        fields.setdefault("Description", str(name).lower())
        fields.setdefault("Organization", str(name).lower())
        fields.setdefault("Fairshare", "1")
        for key in ("Description", "Organization"):
            _check_token(fields[key], "account %r %s" % (name, key.lower()), errors)
        _check_qos_refs(fields, "account %r" % name)
        state["accounts"][str(name)] = {"parent": parent, "fields": fields}

        # association_defaults: applied to every member that doesn't set the
        # field itself (same precedence as examples/big-cluster/generate.tf).
        defaults = spec.get("association_defaults") or {}
        default_overrides = defaults.get("account_overrides") or {}
        default_assoc = defaults.get("association") or {}
        unknown = set(defaults) - {"account_overrides", "association"}
        if unknown:
            errors.append(
                "account %r: association_defaults has unknown sub-maps %s"
                % (name, sorted(unknown))
            )

        seen = {}
        for entry in spec.get("user_associations", []) or []:
            user, overrides, assoc = _member_entry(entry, name, errors)
            if user is None:
                continue
            if user in PROTECTED_USERS:
                errors.append(
                    "account %r: the built-in %r user cannot be listed in "
                    "user_associations" % (name, user)
                )
                continue
            _check_token(user, "account %r member" % name, errors)

            merged = {}
            for key, target in ACCOUNT_OVERRIDE_FIELDS.items():
                value = overrides.get(key, default_overrides.get(key))
                if value is not None:
                    try:
                        merged[target] = _field_value(
                            key, value, "account %r member %r" % (name, user))
                    except SlurmAcctError as exc:
                        errors.extend(exc.errors)
            for key, target in ASSOCIATION_ONLY_FIELDS.items():
                value = assoc.get(key, default_assoc.get(key))
                if value is not None:
                    try:
                        merged[target] = _field_value(
                            key, value, "account %r member %r" % (name, user))
                    except SlurmAcctError as exc:
                        errors.extend(exc.errors)

            partition = merged.pop("Partition", "")
            merged.setdefault("Fairshare", "1")
            _check_qos_refs(merged, "account %r member %r" % (name, user))

            key = (user, str(name), partition)
            if key in seen:
                errors.append(
                    "account %r: duplicate association for user %r (partition %r)"
                    % (name, user, partition)
                )
                continue
            seen[key] = True
            state["assocs"][key] = merged
            membership.setdefault(user, [])
            if str(name) not in membership[user]:
                membership[user].append(str(name))

        # Optional per-account coordinators list (verified: Coordinator= on
        # User lines round-trips through dump/load).
        for coord in spec.get("coordinators", []) or []:
            coord = str(coord)
            membership.setdefault(coord, membership.get(coord, []))
            user_rec = state["users"].setdefault(coord, {})
            existing = user_rec.get("Coordinator", "")
            items = [i for i in existing.split(",") if i] + [str(name)]
            user_rec["Coordinator"] = canon_list(",".join(items))

    # ---- Account hierarchy checks (parents defined, no cycles) ----
    for name, rec in state["accounts"].items():
        parent = rec["parent"]
        if parent != "root" and parent not in state["accounts"]:
            errors.append(
                "account %r: parent_account %r is not declared in the accounts "
                "map — a load would fail midway (and 'clean' is not "
                "transactional), so this is refused up front" % (name, parent)
            )
    # cycle detection
    for name in state["accounts"]:
        seen_chain = set()
        node = name
        while node != "root" and node in state["accounts"]:
            if node in seen_chain:
                errors.append("account %r: parent_account chain forms a cycle" % name)
                break
            seen_chain.add(node)
            node = state["accounts"][node]["parent"]

    # ---- Users (entity-level attributes) ----
    for user in sorted(membership):
        accounts_of = membership[user]
        rec = state["users"].setdefault(user, {})
        if not accounts_of:
            # coordinator-only entry: coordinators do not need an association,
            # but the user entity must exist -> require a membership.
            errors.append(
                "user %r is named as a coordinator but has no association in "
                "any account's user_associations — zero-association users "
                "cannot be represented" % user
            )
            continue
        default = default_accounts.get(user)
        if default is not None:
            if str(default) not in accounts_of:
                errors.append(
                    "user %r: default_accounts pins %r but the user has no "
                    "association in that account" % (user, default)
                )
            rec["DefaultAccount"] = str(default)
        elif len(accounts_of) == 1:
            rec["DefaultAccount"] = accounts_of[0]
        else:
            errors.append(
                "user %r belongs to %d accounts (%s) — add an entry to the "
                "default_accounts map to pin their login default"
                % (user, len(accounts_of), ", ".join(sorted(accounts_of)))
            )

        level = admin_levels.get(user)
        if level is not None:
            if str(level) not in VALID_ADMIN_LEVELS:
                errors.append(
                    "user %r: admin_level %r invalid (valid: %s)"
                    % (user, level, ", ".join(VALID_ADMIN_LEVELS))
                )
            elif str(level) != "None":
                rec["AdminLevel"] = str(level)

        wckey = wckeys.get(user)
        if wckey is not None:
            _check_token(wckey, "user %r wckey" % user, errors)
            rec["DefaultWCKey"] = str(wckey)
            rec["WCKeys"] = str(wckey)

    for source, label in ((admin_levels, "admin_levels"), (default_accounts, "default_accounts"),
                          (wckeys, "wckeys")):
        for user in source:
            if user in PROTECTED_USERS:
                errors.append(
                    "%s: the built-in %r user cannot be managed here" % (label, user))
            elif user not in membership:
                errors.append(
                    "%s: user %r has no association in any account's "
                    "user_associations" % (label, user)
                )

    if errors:
        raise SlurmAcctError(errors)
    return state


# ---------------------------------------------------------------------------
# 2. render(): desired-state records -> loadable sacctmgr flat file
# ---------------------------------------------------------------------------


def _fields_str(fields, order=None):
    keys = sorted(fields, key=lambda k: (order.index(k) if order and k in order else 999, k))
    parts = []
    for key in keys:
        value = fields[key]
        if key in ("Description", "Organization", "DefaultQOS", "QOS", "AdminLevel",
                   "DefaultAccount", "DefaultWCKey", "WCKeys", "Coordinator", "Flags",
                   "Partition"):
            parts.append("%s=%s" % (key, quote(value)))
        else:
            parts.append("%s=%s" % (key, value))
    return ":".join(parts)


def render(state, authoritative=False, include_qos=True, include_wckeys=True):
    """Render desired-state records into a file `sacctmgr load` accepts.

    Ordering guarantees (the whole point of pre-validating): every Parent
    line references an account already defined above it, so a structurally
    valid render can never hit the mid-load "add this parent first" error.

    authoritative=True (purge mode) additionally emits an explicit
    AdminLevel='None' on every user line without one: `load ... clean` does
    not reset an omitted AdminLevel (verified), but applies an explicit None,
    so demotions converge.

    include_qos=False renders the hierarchy only. The module converges QOS in
    a separate pre-load (same-file QOS forward references fail, verified) and
    then must NOT repeat QOS lines in the hierarchy load: a QOS line that
    differs from the live definition (e.g. the built-in `normal`, whose
    init-time description bypasses sacctmgr's lowercasing) makes the load
    silently skip the rest of the file.

    include_wckeys=False omits DefaultWCKey/WCKeys from user lines. The
    `clean` pass must never carry them: on Slurm 25.11 a WCKeys= on a User
    line silently aborts the whole clean transaction (verified — nothing gets
    created, exit code 0), and even on 25.05 clean drops declared WCKeys
    every other run (the flap). WCKeys are applied by the plain second pass.
    """
    lines = []
    if include_qos:
        for name in sorted(state["qos"]):
            lines.append("QOS - %s:%s" % (quote(name), _fields_str(state["qos"][name])))
    lines.append("Cluster - %s:%s" % (quote(state["cluster"]["name"]),
                                      _fields_str(state["cluster"]["fields"])))

    children = {}
    for name, rec in sorted(state["accounts"].items()):
        children.setdefault(rec["parent"], []).append(name)
    members = {}
    for (user, account, partition), fields in state["assocs"].items():
        members.setdefault(account, []).append((user, partition, fields))

    def user_line(user, partition, fields):
        rec = dict(fields)
        if partition:
            rec["Partition"] = partition
        for key in USER_GLOBAL_KEYS:
            if not include_wckeys and key in ("DefaultWCKey", "WCKeys"):
                continue
            value = state["users"].get(user, {}).get(key)
            if value:
                rec[key] = value
        if authoritative:
            rec.setdefault("AdminLevel", "None")
        order = ["Partition", "DefaultAccount", "DefaultWCKey", "AdminLevel",
                 "WCKeys", "Coordinator", "Fairshare"]
        return "User - %s:%s" % (quote(user), _fields_str(rec, order))

    def emit_section(account):
        kids = sorted(children.get(account, []))
        assocs = sorted(members.get(account, []))
        if account != "root" and not kids and not assocs:
            return
        lines.append("Parent - %s" % quote(account))
        if account == "root":
            lines.append(
                "User - 'root':DefaultAccount='root':AdminLevel='Administrator':Fairshare=1")
        for kid in kids:
            order = ["Description", "Organization", "DefaultQOS", "Fairshare"]
            lines.append("Account - %s:%s"
                         % (quote(kid), _fields_str(state["accounts"][kid]["fields"], order)))
        for user, partition, fields in assocs:
            lines.append(user_line(user, partition, fields))
        for kid in kids:
            emit_section(kid)

    emit_section("root")
    return "\n".join(lines) + "\n"


def render_qos_only(state):
    """QOS definitions + Cluster line only, for the QOS pre-load.

    `sacctmgr load` validates QOS references against the QOS list read at
    process start, so QOS created by lines earlier in the same file are
    invisible to later Account/User lines (verified: "You gave a bad qos").
    Loading the QOS separately first makes the hierarchy load see them.
    """
    lines = []
    for name in sorted(state["qos"]):
        lines.append("QOS - %s:%s" % (quote(name), _fields_str(state["qos"][name])))
    lines.append("Cluster - %s:%s" % (quote(state["cluster"]["name"]),
                                      _fields_str(state["cluster"]["fields"])))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 3. parse_flat(): sacctmgr dump (or rendered file) -> state records
# ---------------------------------------------------------------------------


def _split_line(line):
    """Split 'Key=Value:Key=Value' on colons outside single quotes."""
    parts = []
    buf = []
    in_quote = False
    for ch in line:
        if ch == "'":
            in_quote = not in_quote
            buf.append(ch)
        elif ch == ":" and not in_quote:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


def parse_flat(text):
    """Parse a sacctmgr flat file into normalized state records.

    Normalization applied: fairshare sentinel -> "parent", QOS/WCKeys/Flags
    lists sorted, TRES lists sorted, key aliases canonicalized, quotes
    stripped. A parsed dump and a parsed render of in-sync data compare equal.
    """
    state = {"cluster": {"name": "", "fields": {}}, "qos": {}, "accounts": {},
             "assocs": {}, "users": {}}
    parent = "root"
    errors = []

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        head, _, rest = line.partition(" - ")
        head = head.strip()
        if head not in ("QOS", "Cluster", "Parent", "Account", "User"):
            errors.append("line %d: unrecognized line type %r" % (lineno, head))
            continue
        segments = _split_line(rest)
        name = segments[0].strip().strip("'")
        fields = {}
        for segment in segments[1:]:
            key, eq, value = segment.partition("=")
            if not eq:
                errors.append("line %d: malformed segment %r" % (lineno, segment))
                continue
            fields[canon_key(key)] = canon_value(key, value)

        if head == "Parent":
            parent = name
        elif head == "QOS":
            state["qos"][name] = fields
        elif head == "Cluster":
            state["cluster"] = {"name": name, "fields": fields}
        elif head == "Account":
            state["accounts"][name] = {"parent": parent, "fields": fields}
        elif head == "User":
            partition = fields.pop("Partition", "")
            # AdminLevel None is equivalent to absent (dump omits it).
            if fields.get("AdminLevel") == "None":
                del fields["AdminLevel"]
            user_rec = state["users"].setdefault(name, {})
            for key in USER_GLOBAL_KEYS:
                if key in fields:
                    user_rec[key] = fields.pop(key)
            state["assocs"][(name, parent, partition)] = fields

    if errors:
        raise SlurmAcctError(errors)
    # root's own association carries no meaningful config; drop it so both
    # sides compare on real content only (the render always emits the fixed
    # root User line).
    state["assocs"].pop(("root", "root", ""), None)
    state["users"].pop("root", None)
    state["accounts"].pop("root", None)
    return state


# ---------------------------------------------------------------------------
# 4. compute_plan(): desired vs live -> categorized change plan
# ---------------------------------------------------------------------------


def _fields_mismatch(desired_fields, live_fields, full):
    """Compare field dicts.

    full=False ("stop managing" semantics — additive mode, and QOS in every
    mode): only keys present in the desired record are compared, so a field
    removed from inventory is left alone rather than fought over. full=True
    (purge mode): exact match required — `load ... clean` verifiably resets
    omitted association/account fields, and the authoritative render pins
    AdminLevel, so a removed field genuinely resets.
    """
    if full:
        return desired_fields != live_fields
    return any(live_fields.get(k) != v for k, v in desired_fields.items())


def _records_mismatch(desired_rec, live_rec, full):
    if "fields" in desired_rec:
        if desired_rec.get("parent") != live_rec.get("parent"):
            return True
        return _fields_mismatch(desired_rec["fields"], live_rec.get("fields", {}), full)
    return _fields_mismatch(desired_rec, live_rec, full)


def _diff_records(desired, live, purge, protected=(), full=None):
    if full is None:
        full = purge
    plan = {"create": [], "update": [], "delete": [], "unmanaged": []}
    for name in sorted(desired):
        if name not in live:
            plan["create"].append(name)
        elif _records_mismatch(desired[name], live[name], full):
            plan["update"].append(name)
    for name in sorted(live):
        if name in desired or name in protected:
            continue
        (plan["delete"] if purge else plan["unmanaged"]).append(name)
    return plan


def _assoc_key_str(key):
    user, account, partition = key
    label = "%s@%s" % (user, account)
    if partition:
        label += "/partition=%s" % partition
    return label


def compute_plan(desired, live, purge, live_users=None, live_accounts=None,
                 live_qos=None):
    """Compare desired vs live records and produce the full action plan.

    live_users / live_accounts are complete entity lists (from `sacctmgr show`)
    used to find zero-association orphans, which never appear in a dump.
    live_qos is the authoritative live QOS list for the purge step.
    """
    plan = {}
    # QOS definition fields never reset when omitted from a load file
    # (verified), so QOS always uses "declared keys only" comparison —
    # removing a QOS field from inventory stops managing it.
    plan["qos"] = _diff_records(desired["qos"], live["qos"], purge,
                                protected=SYSTEM_QOS, full=False)
    # dump omits live QOS only if... it doesn't; but the `sacctmgr show qos`
    # list is authoritative for the delete step.
    if purge and live_qos is not None:
        extra = [q for q in sorted(live_qos)
                 if q not in desired["qos"] and q not in SYSTEM_QOS
                 and q not in plan["qos"]["delete"]]
        plan["qos"]["delete"].extend(extra)

    desired_accounts = {n: r for n, r in desired["accounts"].items()}
    live_accounts_rec = {n: r for n, r in live["accounts"].items()}
    plan["accounts"] = _diff_records(desired_accounts, live_accounts_rec, purge,
                                     protected=PROTECTED_ACCOUNTS)

    plan["assocs"] = {"create": [], "update": [], "delete": [], "unmanaged": []}
    raw = _diff_records(desired["assocs"], live["assocs"], purge)
    for action in raw:
        plan["assocs"][action] = [_assoc_key_str(k) for k in raw[action]]

    plan["users"] = _diff_records(desired["users"], live["users"], purge,
                                  protected=PROTECTED_USERS)

    # Orphan entities: exist server-side but have no association in the
    # desired state. Includes pre-existing zero-association entities that a
    # dump never shows. Only ever acted on with purge, and never root.
    plan["orphan_users"] = []
    plan["orphan_accounts"] = []
    if live_users is not None:
        plan["orphan_users"] = sorted(
            u for u in live_users
            if u not in desired["users"] and u not in PROTECTED_USERS)
    if live_accounts is not None:
        plan["orphan_accounts"] = sorted(
            a for a in live_accounts
            if a not in desired["accounts"] and a not in PROTECTED_ACCOUNTS)

    # Zero-association entities (in the entity listings but absent from the
    # dump) make `sacctmgr load` silently skip creating their cluster
    # association (verified) — they must be deleted before the load whenever
    # the file is about to define them. With purge, all of them go (they are
    # orphans anyway); without purge, only the ones the inventory declares.
    plan["precleanup_users"] = []
    plan["precleanup_accounts"] = []
    if live_users is not None:
        plan["precleanup_users"] = sorted(
            u for u in live_users
            if u not in live["users"] and u not in PROTECTED_USERS
            and (purge or u in desired["users"]))
    if live_accounts is not None:
        plan["precleanup_accounts"] = sorted(
            a for a in live_accounts
            if a not in live["accounts"] and a not in PROTECTED_ACCOUNTS
            and (purge or a in desired["accounts"]))

    # The Cluster line itself (cluster-level fairshare / default QOS list) is
    # deliberately not managed: the module re-renders whatever the live
    # cluster line says, so site-tuned cluster defaults are never fought over.
    file_changes = any(
        plan[section][action]
        for section in ("qos", "accounts", "assocs", "users")
        for action in ("create", "update")
    )
    deletions = any(
        plan[section]["delete"]
        for section in ("qos", "accounts", "assocs", "users")
    ) or bool(plan["orphan_users"]) or bool(plan["orphan_accounts"])

    plan["needs_load"] = file_changes or (purge and any(
        plan[section]["delete"] for section in ("accounts", "assocs", "users")))
    plan["changed"] = file_changes or (purge and deletions)
    return plan


# ---------------------------------------------------------------------------
# 5. canonical_text(): stable, human-readable text form for --diff
# ---------------------------------------------------------------------------


def canonical_text(state, extra_orphan_users=(), extra_orphan_accounts=()):
    """Deterministic listing of state records, for Ansible's --diff display.

    Not a loadable file — a normalized view in which two in-sync states
    render byte-identical.
    """
    lines = []
    for name in sorted(state["qos"]):
        lines.append("QOS %s: %s" % (name, _fields_str(state["qos"][name])))
    for name in sorted(state["accounts"]):
        rec = state["accounts"][name]
        lines.append("Account %s: Parent=%s:%s"
                     % (name, quote(rec["parent"]), _fields_str(rec["fields"])))
    for key in sorted(state["assocs"]):
        lines.append("Association %s: %s"
                     % (_assoc_key_str(key), _fields_str(state["assocs"][key])))
    for name in sorted(state["users"]):
        lines.append("User %s: %s" % (name, _fields_str(state["users"][name])))
    for name in sorted(extra_orphan_users):
        lines.append("User %s: (no associations)" % name)
    for name in sorted(extra_orphan_accounts):
        lines.append("Account %s: (no associations)" % name)
    return "\n".join(lines) + "\n"


def projected_state(desired, live, purge):
    """What live should look like after applying the plan.

    Non-purge mode keeps unmanaged live entities (additive semantics), so the
    --diff shown to the user only contains changes that will actually happen.
    """
    result = {
        "cluster": desired["cluster"],
        "qos": {}, "accounts": {}, "assocs": {}, "users": {},
    }

    def _merge_onto_live(desired_rec, live_rec):
        # additive/QOS semantics: only declared keys change; live-only keys
        # persist, so they must not show up as removals in the diff.
        if "fields" in desired_rec:
            fields = dict(live_rec.get("fields", {}))
            fields.update(desired_rec["fields"])
            return {"parent": desired_rec["parent"], "fields": fields}
        merged = dict(live_rec)
        merged.update(desired_rec)
        return merged

    for section in ("qos", "accounts", "assocs", "users"):
        full = purge and section != "qos"
        for name, rec in desired[section].items():
            if not full and name in live[section]:
                result[section][name] = _merge_onto_live(rec, live[section][name])
            else:
                result[section][name] = rec
        if not purge:
            for name, rec in live[section].items():
                result[section].setdefault(name, rec)
        else:
            # system/protected entities survive purge untouched
            protected = SYSTEM_QOS if section == "qos" else (
                PROTECTED_ACCOUNTS if section == "accounts" else PROTECTED_USERS)
            for name, rec in live[section].items():
                if name in protected:
                    result[section].setdefault(name, rec)
    return result
