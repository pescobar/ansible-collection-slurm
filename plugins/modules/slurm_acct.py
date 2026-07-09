#!/usr/bin/python
# -*- coding: utf-8 -*-

# Declaratively manage Slurm accounting (accounts, users, associations, QOS)
# through sacctmgr's flat-file load mechanism. See ansible/README.md for the
# empirically verified semantics this module is built on.

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: slurm_acct
short_description: Declaratively manage Slurm accounting via sacctmgr flat-file load
description:
  - Renders the desired accounting state (accounts, users, associations, QOS)
    into sacctmgr's flat-file format, diffs it against a live C(sacctmgr dump),
    and converges the cluster with C(sacctmgr -i load).
  - Without I(purge), runs in additive mode (creates and updates only — plain
    C(load), which never deletes anything).
  - With I(purge=true), runs C(load ... clean) followed by a plain C(load)
    (the second pass re-applies WCKeys, which C(clean) drops — verified
    behavior), then deletes undeclared QOS and orphaned zero-association
    users/accounts. The built-in C(normal) QOS and the C(root) account/user
    are never touched.
  - The rendered file is structurally validated first (parents defined before
    children, no undeclared QOS references, safe quoting) because
    C(load ... clean) is not transactional — a file that errors midway has
    already applied its deletions.
  - Fully supports check mode and C(--diff) as a complete, read-only preview,
    including the deletions a purge run would perform.
options:
  cluster:
    description: Slurm cluster name (must match the live cluster).
    type: str
    required: true
  qos:
    description:
      - Map of QOS name to definition. Keys per QOS — C(description),
        C(priority), C(max_wall_pj) (minutes), C(grace_time) (seconds),
        C(flags) (list), C(max_jobs_per_user), C(max_tres_per_job).
      - Slurm's built-in C(normal) QOS must not be declared here.
    type: dict
    default: {}
  accounts:
    description:
      - Account-centric map mirroring the provider's C(examples/big-cluster)
        YAML - account name to metadata (C(description), C(organization),
        C(parent_account), C(fairshare), C(default_qos), C(allowed_qos),
        C(max_jobs), TRES limits), optional C(association_defaults), optional
        C(coordinators), and a C(user_associations) list whose entries are a
        bare username or a map with C(user), C(account_overrides), and
        C(association) keys.
    type: dict
    default: {}
  admin_levels:
    description: Exception map of user name to C(Operator) or C(Administrator).
    type: dict
    default: {}
  default_accounts:
    description:
      - Exception map pinning multi-account users' login default account.
      - Required for every user that appears in more than one account.
    type: dict
    default: {}
  wckeys:
    description: Exception map of user name to their default WCKey.
    type: dict
    default: {}
  purge:
    description:
      - Enable deletions. Without it the run is additive-only and live
        entities missing from the inventory are only reported as unmanaged.
      - With it, C(load ... clean) removes undeclared associations, and
        follow-up steps delete undeclared QOS and orphaned zero-association
        users/accounts. C(normal)/C(root) always survive.
    type: bool
    default: false
  sacctmgr_path:
    description: Path to the sacctmgr binary.
    type: str
    default: sacctmgr
notes:
  - Must run on a host with sacctmgr configured against the target slurmdbd
    (typically the slurmctld host), as a user with Slurm admin rights.
  - After applying, the module re-dumps the cluster and fails if the live
    state still differs from the desired state (convergence check).
attributes:
  check_mode:
    support: full
  diff_mode:
    support: full
author:
  - Pablo Escobar Lopez (@pescobar)
"""

EXAMPLES = r"""
- name: Converge Slurm accounting (additive)
  pescobar.slurm.slurm_acct:
    cluster: linux
    qos: "{{ slurm_acct_qos }}"
    accounts: "{{ slurm_acct_accounts }}"
    admin_levels: "{{ slurm_acct_admin_levels }}"
    default_accounts: "{{ slurm_acct_default_accounts }}"
    wckeys: "{{ slurm_acct_wckeys }}"

- name: Converge and remove everything not in inventory
  pescobar.slurm.slurm_acct:
    cluster: linux
    accounts: "{{ slurm_acct_accounts }}"
    purge: true
"""

RETURN = r"""
plan:
  description: Categorized changes per entity type (create/update/delete/unmanaged),
    plus orphan_users/orphan_accounts purge candidates.
  returned: always
  type: dict
rendered:
  description: The flat file rendered from the inventory data.
  returned: always
  type: str
loaded:
  description: Whether a sacctmgr load was executed.
  returned: on change
  type: bool
deleted_qos:
  description: QOS deleted by the purge step.
  returned: when purging
  type: list
deleted_users:
  description: Orphaned users deleted by the purge step.
  returned: when purging
  type: list
deleted_accounts:
  description: Orphaned accounts deleted by the purge step.
  returned: when purging
  type: list
"""

import os

from ansible.module_utils.basic import AnsibleModule
from ansible_collections.pescobar.slurm.plugins.module_utils.slurm_acct import (
    PROTECTED_ACCOUNTS,
    PROTECTED_USERS,
    SYSTEM_QOS,
    SlurmAcctError,
    canonical_text,
    compute_plan,
    parse_flat,
    projected_state,
    render,
    render_qos_only,
    resolve,
)

# sacctmgr load reports some mid-file failures with exit code 0 (verified),
# so every load's output is scanned for its error phrasings too.
LOAD_ERROR_MARKERS = (
    "Problem with line",
    "Problem with requests",
    "Unknown option",
    "You need to add this parent",
    " error: ",
)


def run_sacctmgr(module, args, check_rc=True):
    cmd = [module.params["sacctmgr_path"]] + args
    rc, out, err = module.run_command(cmd)
    if check_rc and rc != 0:
        module.fail_json(
            msg="command failed: %s (rc=%d)" % (" ".join(cmd), rc),
            stdout=out, stderr=err, rc=rc,
        )
    return rc, out, err


def run_load(module, path, clean=False):
    args = ["-i", "load", path]
    if clean:
        args.append("clean")
    rc, out, err = run_sacctmgr(module, args, check_rc=False)
    combined = out + "\n" + err
    failed_marker = next((m for m in LOAD_ERROR_MARKERS if m in combined), None)
    if rc != 0 or failed_marker:
        module.fail_json(
            msg="sacctmgr load%s failed (rc=%d%s)%s" % (
                " clean" if clean else "", rc,
                ", matched %r" % failed_marker if failed_marker else "",
                " — NOTE: 'clean' is not transactional, deletions may already "
                "have been applied; re-run after fixing the cause to converge"
                if clean else ""),
            stdout=out, stderr=err, rc=rc,
        )
    return out


def read_live_state(module, cluster):
    """Read the complete live accounting state (read-only)."""
    dump_path = os.path.join(module.tmpdir, "slurm_acct_dump.cfg")
    if os.path.exists(dump_path):
        os.unlink(dump_path)
    rc, out, err = run_sacctmgr(module, ["dump", cluster, "file=%s" % dump_path],
                                check_rc=False)
    if rc != 0 or not os.path.exists(dump_path):
        module.fail_json(
            msg="sacctmgr dump %s failed — wrong cluster name, or sacctmgr "
                "cannot reach slurmdbd" % cluster,
            stdout=out, stderr=err, rc=rc,
        )
    with open(dump_path) as handle:
        dump_text = handle.read()
    os.unlink(dump_path)

    try:
        live = parse_flat(dump_text)
    except SlurmAcctError as exc:
        module.fail_json(
            msg="could not parse `sacctmgr dump` output — possibly a new "
                "Slurm flat-file construct this collection does not know",
            errors=exc.errors, dump=dump_text,
        )

    def listing(entity, fmt):
        _, out, _ = run_sacctmgr(module, ["-nP", "show", entity, "format=%s" % fmt])
        return [line.split("|")[0] for line in out.splitlines() if line.strip()]

    live_users = listing("user", "user")
    live_accounts = listing("account", "account")
    live_qos = listing("qos", "name")
    return live, live_users, live_accounts, live_qos


def main():
    module = AnsibleModule(
        argument_spec=dict(
            cluster=dict(type="str", required=True),
            qos=dict(type="dict", default={}),
            accounts=dict(type="dict", default={}),
            admin_levels=dict(type="dict", default={}),
            default_accounts=dict(type="dict", default={}),
            wckeys=dict(type="dict", default={}),
            purge=dict(type="bool", default=False),
            sacctmgr_path=dict(type="str", default="sacctmgr"),
        ),
        supports_check_mode=True,
    )
    purge = module.params["purge"]
    cluster = module.params["cluster"]

    # 1. Resolve + validate the inventory data. Any structural problem is
    # refused here, before anything touches the cluster: `load ... clean` is
    # not transactional, so a malformed file must never reach it.
    try:
        desired = resolve(
            cluster=cluster,
            qos=module.params["qos"],
            accounts=module.params["accounts"],
            admin_levels=module.params["admin_levels"],
            default_accounts=module.params["default_accounts"],
            wckeys=module.params["wckeys"],
        )
    except SlurmAcctError as exc:
        module.fail_json(
            msg="invalid slurm_acct inventory data (%d problem%s) — refusing "
                "to render a load file" % (len(exc.errors),
                                           "s" if len(exc.errors) != 1 else ""),
            errors=exc.errors,
        )

    # 2. Read live state (read-only, safe in check mode).
    live, live_users, live_accounts, live_qos = read_live_state(module, cluster)

    # Adopt the live Cluster line: cluster-level defaults (fairshare, default
    # QOS list) are deliberately not managed by this module.
    if live["cluster"]["name"]:
        desired["cluster"] = live["cluster"]

    # 3. Plan.
    plan = compute_plan(desired, live, purge,
                        live_users=live_users, live_accounts=live_accounts,
                        live_qos=live_qos)

    # The file handed to `load ... clean` in both modes:
    #  - purge: exactly the inventory, rendered authoritatively (explicit
    #    AdminLevel='None' so demotions converge) — clean deletes the rest.
    #  - additive: the live state with the inventory merged on top, so clean
    #    (needed because plain load cannot reliably create associations that
    #    depend on other additions in the same file — verified) has nothing
    #    to delete.
    # QOS definitions are always converged by a separate pre-load, never by
    # the hierarchy file (include_qos=False), and the clean pass never
    # carries WCKeys (aborts the whole transaction on 25.11) — they ride the
    # plain second pass. See render()'s docstring for both.
    if purge:
        hierarchy_state = desired
        rendered = render(desired, authoritative=True, include_qos=False)
    else:
        hierarchy_state = projected_state(desired, live, purge=False)
        rendered = render(hierarchy_state, include_qos=False)
    rendered_clean = render(hierarchy_state, authoritative=purge,
                            include_qos=False, include_wckeys=False)

    orphan_users_live = [u for u in live_users
                         if u not in live["users"] and u not in PROTECTED_USERS]
    orphan_accounts_live = [a for a in live_accounts
                            if a not in live["accounts"] and a not in PROTECTED_ACCOUNTS]
    before = canonical_text(live, orphan_users_live, orphan_accounts_live)
    after = canonical_text(
        projected_state(desired, live, purge),
        () if purge else orphan_users_live,
        () if purge else orphan_accounts_live,
    )
    diff = {"before": before, "after": after,
            "before_header": "live cluster %s" % cluster,
            "after_header": "inventory (purge=%s)" % str(purge).lower()}

    result = dict(changed=plan["changed"], plan=plan, rendered=rendered, diff=diff)

    if module.check_mode or not plan["changed"]:
        module.exit_json(**result)

    # 4. Apply.
    if plan["needs_load"]:
        # 4a. Pre-cleanup: delete zero-association entities the file is about
        # to define — sacctmgr load silently skips creating the cluster
        # association of an entity that already exists without one (verified).
        for name in plan["precleanup_users"]:
            run_sacctmgr(module, ["-i", "delete", "user", "name=%s" % name])
        for name in plan["precleanup_accounts"]:
            run_sacctmgr(module, ["-i", "delete", "account", "name=%s" % name])

        # 4b. QOS pre-load: hierarchy lines cannot reference QOS created in
        # the same load (validated against process-start state), so converge
        # the QOS definitions in their own load first.
        if plan["qos"]["create"] or plan["qos"]["update"]:
            qos_path = os.path.join(module.tmpdir, "slurm_acct_qos.cfg")
            with open(qos_path, "w") as handle:
                handle.write(render_qos_only(desired))
            run_load(module, qos_path)
            os.unlink(qos_path)

        # 4c. Hierarchy load: `clean` (atomic server-side rebuild, preserves
        # association ids of unchanged entries) with a WCKey-less file, then
        # a plain load of the full file to apply WCKeys — clean aborts on
        # them (25.11) or drops them (25.05 flap).
        clean_path = os.path.join(module.tmpdir, "slurm_acct_clean.cfg")
        with open(clean_path, "w") as handle:
            handle.write(rendered_clean)
        run_load(module, clean_path, clean=True)
        os.unlink(clean_path)
        load_path = os.path.join(module.tmpdir, "slurm_acct_load.cfg")
        with open(load_path, "w") as handle:
            handle.write(rendered)
        run_load(module, load_path)
        os.unlink(load_path)
        result["loaded"] = True

    # 5. Purge steps `clean` cannot do: QOS are global (never deleted by
    # clean), and removing an entity's last association leaves an orphaned
    # zero-association entity behind.
    if purge:
        deleted_qos = []
        for name in plan["qos"]["delete"]:
            if name in SYSTEM_QOS:
                continue
            run_sacctmgr(module, ["-i", "delete", "qos", "name=%s" % name])
            deleted_qos.append(name)
        deleted_users = []
        for name in plan["orphan_users"]:
            if name in PROTECTED_USERS:
                continue
            run_sacctmgr(module, ["-i", "delete", "user", "name=%s" % name])
            deleted_users.append(name)
        deleted_accounts = []
        for name in plan["orphan_accounts"]:
            if name in PROTECTED_ACCOUNTS:
                continue
            run_sacctmgr(module, ["-i", "delete", "account", "name=%s" % name])
            deleted_accounts.append(name)
        result.update(deleted_qos=deleted_qos, deleted_users=deleted_users,
                      deleted_accounts=deleted_accounts)

    # 6. Convergence check: re-read and re-plan. Anything still pending means
    # a semantic this collection doesn't model on this Slurm version.
    live2, live_users2, live_accounts2, live_qos2 = read_live_state(module, cluster)
    plan2 = compute_plan(desired, live2, purge,
                         live_users=live_users2, live_accounts=live_accounts2,
                         live_qos=live_qos2)
    if plan2["changed"]:
        module.fail_json(
            msg="cluster did not converge after apply — the live state still "
                "differs from the inventory. This usually means a sacctmgr "
                "flat-file semantic difference on this Slurm version; please "
                "report it.",
            residual_plan=plan2,
            residual_before=canonical_text(live2),
            residual_after=canonical_text(projected_state(desired, live2, purge)),
            **{k: v for k, v in result.items() if k != "diff"}
        )

    module.exit_json(**result)


if __name__ == "__main__":
    main()
