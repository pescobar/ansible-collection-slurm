#!/usr/bin/env bash
# Acceptance tests for the pescobar.slurm collection, run against the
# live Docker test cluster over SSH (see ../docker/ for the stack).
#
# Prerequisites:
#   - the compose stack is up with the SSH overlay (slurmctld on 127.0.0.1:2222)
#   - ansible-playbook / ansible-galaxy on PATH (ansible-core >= 2.15)
#   - the test keypair exists (../docker/generate-ssh-key.sh)
#
# The script copies the sample inventory to a scratch dir and edits the copy
# between phases; the committed sample is never modified.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
ssh_key="${SSH_KEY:-$repo_root/tests/docker/.ssh/id_ed25519}"
ssh_opts=(-i "$ssh_key" -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

log() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }
die() { echo "FAIL: $*" >&2; exit 1; }

sacct() { ssh "${ssh_opts[@]}" root@127.0.0.1 "sacctmgr -nP $*" 2>/dev/null; }

# --- setup: install the collection and copy the sample inventory ------------
log "setup: install collection + copy sample inventory"
ansible-galaxy collection install --force "$repo_root" -p "$workdir/collections"
export ANSIBLE_COLLECTIONS_PATH="$workdir/collections"
export ANSIBLE_FORCE_COLOR=0
cp -r "$repo_root/playbooks/inventory" "$workdir/inventory"
accounts_dir="$workdir/inventory/host_vars/slurmctld/slurm_accounts.d"  # one file per account
qos_yml="$workdir/inventory/group_vars/slurm_controller/slurm_qos.yml"

play() {
  ansible-playbook -i "$workdir/inventory/hosts.yml" \
    "$repo_root/playbooks/site.yml" \
    --private-key "$ssh_key" "$@"
}

# run a play that must succeed; on failure print its full output and die
run_play() {
  if ! out="$(play "$@" 2>&1)"; then
    echo "$out"
    die "playbook run failed (args: $*)"
  fi
}

# Edit the inventory copy with real YAML handling (PyYAML ships with ansible).
# Account actions take the accounts.d/ dir and edit/add/remove per-account
# files; the qos action still edits the single slurm_qos.yml.
edit_inventory() { python3 - "$@" <<'PYEOF'
import os, sys, yaml

target, action = sys.argv[1], sys.argv[2]

def load(p):
    with open(p) as f:
        return yaml.safe_load(f)

def dump(p, data):
    with open(p, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False)

if action == "set_teaching_fairshare":
    p = os.path.join(target, "teaching.yml")
    d = load(p); d["teaching"]["fairshare"] = int(sys.argv[3]); dump(p, d)
elif action == "remove_for_purge_accounts":
    os.remove(os.path.join(target, "lab_chem.yml"))      # account (+ john's assoc)
    p = os.path.join(target, "teaching.yml")
    d = load(p)
    t = d["teaching"]
    t["user_associations"] = [u for u in t["user_associations"] if u != "erin"]
    dump(p, d)
elif action == "remove_for_purge_qos":
    d = load(target); del d["slurm_acct_qos"]["low"]; dump(target, d)
elif action == "add_malformed":
    dump(os.path.join(target, "bad_child.yml"),
         {"bad_child": {"parent_account": "ghost_parent", "user_associations": []}})
    os.remove(os.path.join(target, "shared.yml"))  # a deletion that must NOT happen
elif action == "remove_malformed":
    os.remove(os.path.join(target, "bad_child.yml"))
else:
    raise SystemExit("unknown action " + action)
PYEOF
}

recap_assert() {  # recap_assert <output> <pattern> <label>
  echo "$1" | grep -Eq "$2" || { echo "$1"; die "$3"; }
}

# --- 1. initial apply converges ---------------------------------------------
log "1. initial apply creates the sample accounts/users/qos"
run_play
recap_assert "$out" 'failed=0' "initial apply failed"
[ -n "$(sacct show account lab_physics format=account)" ] || die "lab_physics missing"
[ "$(sacct show user john format=defaultaccount)" = "lab_physics" ] || die "john default account wrong"
[ -n "$(sacct show qos short format=name)" ] || die "qos short missing"
[ "$(sacct show user dave format=defaultwckey)" = "genomics" ] || die "dave wckey missing"
echo "PASS 1"

# --- 2. idempotency ----------------------------------------------------------
log "2. second run is fully idempotent"
run_play
recap_assert "$out" 'changed=0.*failed=0' "second run was not idempotent"
echo "PASS 2"

# --- 3. check mode: no false positives, accurate diff for a pending change ---
log "3a. check mode on an in-sync cluster reports zero changes"
run_play --check --diff
recap_assert "$out" 'changed=0.*failed=0' "check mode reported spurious changes"
echo "PASS 3a"

log "3b. check --diff previews a pending change without applying it"
edit_inventory "$accounts_dir" set_teaching_fairshare 55
run_play --check --diff
recap_assert "$out" 'changed=1' "check mode did not flag the pending change"
echo "$out" | grep -q "Fairshare=55" || { echo "$out"; die "diff missing new value"; }
echo "$out" | grep -q "Fairshare=50" || { echo "$out"; die "diff missing old value"; }
[ "$(sacct show assoc account=teaching user= format=fairshare)" = "50" ] \
  || die "check mode mutated the cluster"
echo "PASS 3b"

# --- 4. a modified value is applied ------------------------------------------
log "4. apply the modified fairshare"
run_play
recap_assert "$out" 'changed=1.*failed=0' "modified value did not apply"
[ "$(sacct show assoc account=teaching user= format=fairshare)" = "55" ] \
  || die "fairshare 55 not live"
run_play
recap_assert "$out" 'changed=0' "not idempotent after modification"
echo "PASS 4"

# --- 5. purge removes inventory-removed entities, protects built-ins ---------
log "5. purge flag removes account/user/association/QOS deleted from inventory"
edit_inventory "$accounts_dir" remove_for_purge_accounts
edit_inventory "$qos_yml" remove_for_purge_qos
# without purge: additive mode must NOT delete anything
run_play
recap_assert "$out" 'failed=0' "additive run failed"
[ -n "$(sacct show account lab_chem format=account)" ] || die "additive mode deleted lab_chem"
[ -n "$(sacct show qos low format=name)" ] || die "additive mode deleted qos low"
# with purge: deletions happen
run_play -e slurm_acct_purge=true
recap_assert "$out" 'failed=0' "purge run failed"
[ -z "$(sacct show account lab_chem format=account)" ] || die "lab_chem survived purge"
[ -z "$(sacct show user erin format=user)" ] || die "erin survived purge (orphan user)"
[ -z "$(sacct show qos low format=name)" ] || die "qos low survived purge"
[ -n "$(sacct show qos normal format=name)" ] || die "system QOS normal was deleted"
[ -n "$(sacct show account root format=account)" ] || die "root account was deleted"
[ -n "$(sacct show user root format=user)" ] || die "root user was deleted"
run_play -e slurm_acct_purge=true
recap_assert "$out" 'changed=0.*failed=0' "purge run not idempotent"
echo "PASS 5"

# --- 6. malformed inventory is refused before anything runs ------------------
log "6. malformed render (undefined parent) refuses to load — nothing deleted"
edit_inventory "$accounts_dir" add_malformed
set +e
out="$(play -e slurm_acct_purge=true 2>&1)"
rc=$?
set -e
[ "$rc" -ne 0 ] || { echo "$out"; die "malformed inventory was not refused"; }
echo "$out" | grep -q "not transactional" || { echo "$out"; die "missing refusal message"; }
# the account removed in the same edit must still exist: proof the failure
# happened before any load/purge, not midway through one
[ -n "$(sacct show account shared format=account)" ] \
  || die "shared was deleted despite the refused run — guard failed"
edit_inventory "$accounts_dir" remove_malformed
# recovery: without bad_child the purge applies, legitimately removing 'shared'
run_play -e slurm_acct_purge=true
recap_assert "$out" 'failed=0' "recovery run failed"
[ -z "$(sacct show account shared format=account)" ] || die "shared not purged in recovery"
echo "PASS 6"

# --- 7. fairshare=parent round-trips without spurious diffs -------------------
log "7. fairshare=parent (teaching association_defaults) round-trip"
[ "$(sacct show assoc account=teaching user=carol format=fairshare)" = "parent" ] \
  || die "carol's fairshare is not parent"
run_play --check --diff -e slurm_acct_purge=true
recap_assert "$out" 'changed=0.*failed=0' "fairshare=parent shows spurious diff"
echo "PASS 7"

log "ALL ACCEPTANCE TESTS PASSED"
