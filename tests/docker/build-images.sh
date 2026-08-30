#!/usr/bin/env bash
# Build (and optionally push) the pescobar.slurm test-cluster images for one or
# more Slurm versions, tagged for the repository's GitHub Container Registry.
#
#   ./build-images.sh                       # build the default version matrix
#   ./build-images.sh 25.05.8 26.05.1       # build only these versions
#   PUSH=1 ./build-images.sh                # build AND push to GHCR
#
# Pushing requires a prior `docker login ghcr.io` (see README.md). The images
# are self-contained (Slurm compiled from source) — expect ~15-20 min each on a
# cold build; the Ubuntu/build layers cache across versions after the first.
set -euo pipefail

cd "$(dirname "$0")"

REGISTRY="ghcr.io/pescobar/ansible-collection-slurm/slurm-test"

# Keep in sync with the CI matrix in
# .github/workflows/acceptance-management.yml and the README table. Slurm 25.05
# is the supported floor (QOS entered the sacctmgr flat-file format then).
DEFAULT_VERSIONS=(25.05.8 25.11.6 26.05.1)

# SHA256 of each tarball at https://download.schedmd.com/slurm/slurm-<version>.tar.bz2
# (verified against SchedMD's published checksum when a version is added — see
# https://www.schedmd.com/downloads.php). Keep in sync with the versions above
# and with the "sha256" matrix field in
# .github/workflows/build-test-images.yml.
declare -A TARBALL_SHA256=(
    [25.05.8]="21ddc614396e7e0b1ba792841f182b4c5b24949b116cb509a3a650fc6fcc22b6"
    [25.11.6]="6695aee51a36799917a4db4b1d787610af926b27b17c2e4246bf14c0fd029664"
    [26.05.1]="c2de57f0b5cdcb5e50543706ab9cb812263a9366e5152c1e716aaf66d307e07c"
)

versions=("$@")
if [ ${#versions[@]} -eq 0 ]; then
    versions=("${DEFAULT_VERSIONS[@]}")
fi

for v in "${versions[@]}"; do
    sha256="${TARBALL_SHA256[$v]:-}"
    if [ -z "$sha256" ]; then
        echo "No known SHA256 for Slurm ${v} — add it to TARBALL_SHA256 in this script." >&2
        exit 1
    fi
    tag="${REGISTRY}:${v}"
    echo "==> Building ${tag}"
    docker build \
        --build-arg "SLURM_VERSION=${v}" \
        --build-arg "SLURM_TARBALL_SHA256=${sha256}" \
        -t "${tag}" .
    if [ "${PUSH:-0}" = "1" ]; then
        echo "==> Pushing ${tag}"
        docker push "${tag}"
    fi
done

echo "Done: ${versions[*]}${PUSH:+ (pushed)}"
