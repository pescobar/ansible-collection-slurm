#!/usr/bin/env bash
# Acceptance test for converging the built-in `root` account and `normal` QOS
# in place (the two entities the collection never creates or deletes, but may
# modify when declared).
#
# root's account-level attributes live on the Cluster line and are applied by
# the hierarchy clean-load; `normal` is applied field-by-field with
# `sacctmgr modify` (never a load file — a differing built-in QOS line silently
# aborts a load, finding 7). This exercises both, plus idempotency, check-mode
# preview without mutation, and that a purge run leaves both intact.
#
# Prereqs are the same as run-tests.sh (compose stack up, ansible on PATH,
# test keypair present). Deliberately separate from run-tests.sh's 7 scenarios.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
ssh_key="${SSH_KEY:-$repo_root/tests/docker/.ssh/id_ed25519}"
ssh_opts=(-i "$ssh_key" -p 2222 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null)

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

log() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }
die() { echo "FAIL: $*" >&2; exit 1; }
sacct() { ssh "${ssh_opts[@]}" root@127.0.0.1 "sacctmgr -nP $*" 2>/dev/null; }

export ANSIBLE_COLLECTIONS_PATH="$workdir/collections"
export ANSIBLE_FORCE_COLOR=0
export ANSIBLE_HOST_KEY_CHECKING=False

log "setup: install collection + write a self-contained inventory"
ansible-galaxy collection install --force "$repo_root" -p "$workdir/collections" >/dev/null
cp "$repo_root/playbooks/inventory/hosts.yml" "$workdir/hosts.yml"

# A minimal play that declares only root + normal, so this test is independent
# of the sample inventory (and of run-tests.sh's scenarios).
cat > "$workdir/play.yml" <<'YAML'
---
- hosts: slurm_controller
  gather_facts: false
  tasks:
    - name: Converge root account + normal QOS in place
      pescobar.slurm.slurm_acct:
        cluster: linux
        purge: "{{ purge | default(false) }}"
        qos:
          normal:
            priority: 50
            max_wall_pj: 720          # minutes -> 12:00:00
            grace_time: 300           # seconds -> 00:05:00
            flags: [DenyOnLimit]
        accounts:
          root:
            fairshare: 30
            max_jobs: 15
            allowed_qos: [normal]
YAML

play() { ansible-playbook -i "$workdir/hosts.yml" "$workdir/play.yml" \
           --private-key "$ssh_key" "$@"; }
run_play() {
  if ! out="$(play "$@" 2>&1)"; then echo "$out"; die "playbook failed (args: $*)"; fi
}

log "1. initial apply converges root (cluster line) and normal (modify)"
run_play
echo "$out" | grep -Eq 'changed=1.*failed=0' || { echo "$out"; die "initial apply not changed"; }
[ "$(sacct show account root withassoc format=fairshare | head -1)" = "30" ] \
  || die "root fairshare not applied"
[ "$(sacct show qos normal format=priority)" = "50" ] || die "normal priority not applied"
[ "$(sacct show qos normal format=maxwall)" = "12:00:00" ] || die "normal maxwall not applied"
[ "$(sacct show qos normal format=gracetime)" = "00:05:00" ] || die "normal gracetime not applied"
echo "PASS 1"

log "2. second run is fully idempotent"
run_play
echo "$out" | grep -Eq 'changed=0.*failed=0' || { echo "$out"; die "not idempotent"; }
echo "PASS 2"

log "3. check --diff previews a pending change without mutating"
sed 's/priority: 50/priority: 65/' -i "$workdir/play.yml"
run_play --check --diff
echo "$out" | grep -Eq 'changed=1' || { echo "$out"; die "check mode missed the change"; }
echo "$out" | grep -q "Priority=65" || { echo "$out"; die "diff missing new normal priority"; }
[ "$(sacct show qos normal format=priority)" = "50" ] || die "check mode mutated the cluster"
echo "PASS 3"

log "4. the pending change applies, then is idempotent"
run_play
echo "$out" | grep -Eq 'changed=1.*failed=0' || { echo "$out"; die "change did not apply"; }
[ "$(sacct show qos normal format=priority)" = "65" ] || die "normal priority 65 not live"
run_play
echo "$out" | grep -Eq 'changed=0.*failed=0' || { echo "$out"; die "not idempotent after change"; }
echo "PASS 4"

log "5. purge run leaves root and normal intact"
run_play -e purge=true
echo "$out" | grep -Eq 'failed=0' || { echo "$out"; die "purge run failed"; }
[ -n "$(sacct show account root format=account)" ] || die "root account deleted by purge"
[ -n "$(sacct show qos normal format=name)" ] || die "normal QOS deleted by purge"
[ "$(sacct show qos normal format=priority)" = "65" ] || die "normal not converged under purge"
[ "$(sacct show account root withassoc format=fairshare | head -1)" = "30" ] \
  || die "root not converged under purge"
echo "PASS 5"

log "ROOT + NORMAL IN-PLACE TEST PASSED"
