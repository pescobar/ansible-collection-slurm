#!/usr/bin/env bash
# Round-trip acceptance test for the inventory importer (tools + import.yml).
#
# Converge the sample inventory onto the live cluster, import the cluster back
# into a fresh inventory, then run site.yml --check against the generated
# inventory: it must report zero changes. Proves the importer reverses the
# data model faithfully.
#
# Prereqs are the same as run-tests.sh (compose stack up, ansible on PATH,
# test keypair present).
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
ssh_key="${SSH_KEY:-$repo_root/tests/docker/.ssh/id_ed25519}"

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

log() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }
die() { echo "FAIL: $*" >&2; exit 1; }

export ANSIBLE_COLLECTIONS_PATH="$workdir/collections"
export ANSIBLE_FORCE_COLOR=0
export ANSIBLE_HOST_KEY_CHECKING=False

log "setup: install collection"
ansible-galaxy collection install --force "$repo_root" -p "$workdir/collections" >/dev/null

play() {
  ansible-playbook -i "$1" "$repo_root/playbooks/$2" \
    --private-key "$ssh_key" "${@:3}"
}

log "1. converge the sample inventory onto the live cluster"
play "$repo_root/playbooks/inventory/hosts.yml" site.yml >/dev/null \
  || die "initial converge failed"

log "2. import the live cluster into a fresh inventory"
play "$repo_root/playbooks/inventory/hosts.yml" import.yml \
  -e import_output_dir="$workdir/imported" >/dev/null \
  || die "import.yml failed"
[ -f "$workdir/imported/group_vars/slurm_controller/slurm_qos.yml" ] \
  || die "importer produced no slurm_qos.yml"
[ -d "$workdir/imported/host_vars/slurmctld/slurm_accounts.d" ] \
  || die "importer produced no accounts.d fragments"

log "3. --check against the generated inventory must be a no-op"
cp "$repo_root/playbooks/inventory/hosts.yml" "$workdir/imported/hosts.yml"
out="$(play "$workdir/imported/hosts.yml" site.yml --check --diff 2>&1)" \
  || { echo "$out"; die "check run against imported inventory failed"; }
echo "$out" | grep -Eq 'changed=0.*failed=0' \
  || { echo "$out"; die "round-trip was not a no-op (importer lost or altered state)"; }

log "ROUND-TRIP IMPORT TEST PASSED"
