#!/usr/bin/env bash
# Generate the throwaway SSH test keypair the overlay image bakes in.
# Output lands in .ssh/ next to this script (gitignored — never commit keys).
set -euo pipefail

dir="$(cd "$(dirname "$0")" && pwd)/.ssh"
mkdir -p "$dir"

if [ ! -f "$dir/id_ed25519" ]; then
    ssh-keygen -t ed25519 -N "" -C "slurm-acct-test" -f "$dir/id_ed25519"
fi
cp "$dir/id_ed25519.pub" "$dir/authorized_keys"
chmod 600 "$dir/id_ed25519" "$dir/authorized_keys"
echo "Test keypair ready in $dir"
