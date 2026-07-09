#!/bin/bash
# Start sshd alongside the requested Slurm service, then hand off to the
# base image's entrypoint unchanged.
set -e

ssh-keygen -A >/dev/null
mkdir -p /run/sshd
/usr/sbin/sshd

exec /usr/local/bin/docker-entrypoint.sh "$@"
