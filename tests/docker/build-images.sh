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
# .github/workflows/acceptance-management.yml and the README table.
DEFAULT_VERSIONS=(24.11.7 25.05.8 25.11.6 26.05.1)

versions=("$@")
if [ ${#versions[@]} -eq 0 ]; then
    versions=("${DEFAULT_VERSIONS[@]}")
fi

for v in "${versions[@]}"; do
    tag="${REGISTRY}:${v}"
    echo "==> Building ${tag}"
    docker build --build-arg "SLURM_VERSION=${v}" -t "${tag}" .
    if [ "${PUSH:-0}" = "1" ]; then
        echo "==> Pushing ${tag}"
        docker push "${tag}"
    fi
done

echo "Done: ${versions[*]}${PUSH:+ (pushed)}"
