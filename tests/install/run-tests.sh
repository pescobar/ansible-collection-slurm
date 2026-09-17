#!/usr/bin/env bash
# Deployment test for the slurm_install role: installs and configures a
# single-node Slurm cluster ON THIS MACHINE (controller + slurmdbd + worker),
# then loads the sample accounting data with slurm_acct and submits jobs.
#
# Meant for a throwaway Ubuntu 26.04 machine: the GitHub runner VM
# (acceptance-install.yml) or a privileged systemd container. Needs
# ansible-playbook/ansible-galaxy on PATH, and root or passwordless sudo.
#
# Asserts: install succeeds, a second run changes nothing, the node comes up
# idle, jobs run, a 1-CPU task is confined to one core, memory limits are
# enforced (a job over --mem ends OUT_OF_MEMORY), slurm_acct converges and is
# then a --check no-op, a user with an association can submit and a user
# without one is rejected.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
host="$(hostname -s)"
workdir="$(mktemp -d)"
chmod 755 "$workdir"
trap 'rm -rf "$workdir"' EXIT

log() { printf '\n\033[1m=== %s ===\033[0m\n' "$*"; }
die() { echo "FAIL: $*" >&2; exit 1; }
as_root() { if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo "$@"; fi; }

# --- setup: collection + inventory for this host ----------------------------
log "setup: install collection, build inventory for $host"
ansible-galaxy collection install --force "$repo_root" -p "$workdir/collections"
export ANSIBLE_COLLECTIONS_PATH="$workdir/collections"
export ANSIBLE_FORCE_COLOR=0

# The sample accounting inventory, re-pointed at this host.
cp -r "$repo_root/playbooks/inventory" "$workdir/inventory"
mv "$workdir/inventory/host_vars/slurmctld" "$workdir/inventory/host_vars/$host"
cat > "$workdir/inventory/hosts.yml" <<EOF
slurm_controller:
  hosts:
    $host:
slurm_workers:
  hosts:
    $host:
all:
  vars:
    ansible_connection: local
    ansible_python_interpreter: /usr/bin/python3
    ansible_become: true
    # must match the sample data: slurm_acct_cluster, partition 'cpu', gres/gpu limits
    slurm_install_cluster_name: linux
    slurm_install_partition_name: cpu
    slurm_install_dbd_storage_password: ci-only-password
    slurm_install_slurm_conf_extra: |
      GresTypes=gpu
      AccountingStorageTRES=gres/gpu
EOF

# run a play that must succeed; prints its output; sets $recap_changed
play() {
  local out
  if ! out="$(ansible-playbook -i "$workdir/inventory/hosts.yml" "$@" 2>&1)"; then
    echo "$out"
    die "playbook failed: $*"
  fi
  echo "$out"
  recap_changed="$(grep -oP "^$host\s*:.*changed=\K[0-9]+" <<<"$out")"
}

# submit a batch job and wait for it; prints the job id (dies if refused)
submit() {
  local id
  id="$(as_root "$@" sbatch --parsable --wait --chdir=/tmp "${sbatch_args[@]}" || true)"
  [ -n "$id" ] || die "sbatch refused the job: $* ${sbatch_args[*]}"
  echo "$id"
}

# final state of a job, waiting for accounting to record it
job_state() {
  local state=""
  for _ in $(seq 30); do
    state="$(as_root sacct -n -X -P -j "$1" -o state | head -1)"
    case "$state" in ""|PENDING|RUNNING|COMPLETING) sleep 2 ;; *) break ;; esac
  done
  echo "$state"
}

# --- 1. install ---------------------------------------------------------------
log "1. install"
play "$repo_root/playbooks/install.yml"

log "2. idempotency: second install run changes nothing"
play "$repo_root/playbooks/install.yml"
[ "$recap_changed" -eq 0 ] || die "second install run reported changed=$recap_changed"

log "3. node comes up idle, jobs run"
state=""
for _ in $(seq 30); do
  state="$(sinfo -h -N -n "$host" -o %t)"
  [ "$state" = idle ] && break
  sleep 2
done
[ "$state" = idle ] || { as_root scontrol show node "$host"; die "node state is '$state', expected idle"; }
out="$(as_root srun -N1 hostname)"
[ "$out" = "$host" ] || die "srun hostname returned '$out'"

# CR_Core allocates whole cores: a 1-CPU task gets one core, i.e.
# ThreadsPerCore CPUs (2 on the hyperthreaded GitHub runners), never more.
log "4. core limit: a 1-CPU task is confined to one core"
tpc="$(scontrol show node "$host" | grep -oP 'ThreadsPerCore=\K[0-9]+')"
out="$(as_root srun -n1 -c1 nproc)"
[ "$out" = "$tpc" ] && [ "$out" -lt "$(nproc)" ] \
  || die "srun -n1 -c1 nproc returned '$out' (ThreadsPerCore=$tpc, host CPUs=$(nproc))"

log "5. memory limit: within --mem completes, over --mem is OOM-killed"
alloc() { echo "python3 -c 'b = bytearray($1 * 1024 * 1024); import time; time.sleep(2)'"; }
sbatch_args=(--mem=300M --wrap "$(alloc 50)")
id="$(submit)"
state="$(job_state "$id")"
[ "$state" = COMPLETED ] || die "job $id within its memory limit ended '$state'"
sbatch_args=(--mem=100M --wrap "$(alloc 600)")
id="$(submit)"
state="$(job_state "$id")"
[ "$state" = OUT_OF_MEMORY ] || die "job $id over its memory limit ended '$state', expected OUT_OF_MEMORY"

# --- 2. accounting on the installed cluster -----------------------------------
# slurmctld resolves accounting users to uids when it loads associations,
# so the Unix accounts must exist first (as on a real site).
for u in alice mallory; do id "$u" >/dev/null 2>&1 || as_root useradd -m "$u"; done

log "6. slurm_acct converges the sample data"
play "$repo_root/playbooks/site.yml"

log "7. slurm_acct --check is a no-op"
play "$repo_root/playbooks/site.yml" --check
[ "$recap_changed" -eq 0 ] || die "site.yml --check after converge reported changed=$recap_changed"

log "8. enforcement: user with an association runs, user without is rejected"
sbatch_args=(--wrap hostname)
id="$(submit sudo -u alice)"
state="$(job_state "$id")"
account="$(as_root sacct -n -X -P -j "$id" -o account | head -1)"
[ "$state" = COMPLETED ] && [ "$account" = lab_physics ] \
  || die "alice's job $id ended '$state' under account '$account'"
if out="$(as_root sudo -u mallory sbatch --chdir=/tmp --wrap hostname 2>&1)"; then
  die "user without an association could submit: $out"
fi
grep -qi "invalid account" <<<"$out" || die "unexpected rejection message: $out"

log "all install tests passed"
