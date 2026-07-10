# Slurm test-cluster images (pescobar.slurm)

These are the container images the **accounts/users/QOS management** acceptance
tests run against: a minimal Slurm control plane (mysql + slurmdbd + slurmctld,
no compute nodes) with `sshd` and `python3` added so Ansible can drive
`sacctmgr` on the controller over SSH — exactly as in production.

The image is **self-contained**: Slurm is compiled from the official SchedMD
source tarball (recipe adapted from the sibling
[terraform-provider-slurm](https://github.com/pescobar/terraform-provider-slurm)
`docker/`), so this repository no longer depends on that project's published
images. It is published to this repository's GitHub Container Registry:

```
ghcr.io/pescobar/ansible-collection-slurm/slurm-test:<slurm-version>
```

`slurmdbd` and `slurmctld` share the same image (the service is chosen by the
compose `command:`); only `slurmctld` starts `sshd`.

## Supported / tested versions

| Slurm version | Notes                          |
|---------------|--------------------------------|
| 24.11.7       | oldest supported               |
| 25.05.8       |                                |
| 25.11.6       |                                |
| 26.05.1       | newest supported               |

The list lives in three places that must stay in sync: `DEFAULT_VERSIONS` in
`build-images.sh`, the matrix in
`.github/workflows/acceptance-management.yml`, and the build workflow
`.github/workflows/build-test-images.yml`.

## SSH key handling (why the published image is safe)

The image ships **no authorized key**, so publishing it grants nobody access.
The throwaway test keypair is created locally by `./generate-ssh-key.sh` (output
under `.ssh/`, gitignored) and the **public** key is bind-mounted into
`slurmctld` at runtime (`docker-compose.yml`) at
`/run/ssh-pubkey/authorized_keys`. The entrypoint installs it into
`/root/.ssh/authorized_keys` with strict perms and starts `sshd` only when
`ENABLE_SSHD=true`. In CI, `generate-ssh-key.sh` runs fresh each job.

## Running the cluster locally

```bash
./generate-ssh-key.sh
# pull the published image (fast):
SLURM_VERSION=25.05.8 docker compose up -d --wait
# or build it locally instead of pulling:
SLURM_VERSION=25.05.8 docker compose up -d --wait --build
```

SSH lands on `root@127.0.0.1:2222`. Verify and tear down:

```bash
docker exec slurm-ansible-slurmctld scontrol ping
docker exec slurm-ansible-slurmctld sacctmgr -V
docker compose down -v
```

## Building a new image

From inside this directory:

```bash
# one version
docker build --build-arg SLURM_VERSION=26.05.1 \
  -t ghcr.io/pescobar/ansible-collection-slurm/slurm-test:26.05.1 .

# or the whole matrix via the helper
./build-images.sh                 # build all DEFAULT_VERSIONS
./build-images.sh 25.05.8 26.05.1 # build specific versions
```

The build compiles Slurm from source — expect ~15-20 min on a cold build; the
Ubuntu and build-dependency layers cache across versions afterwards.

`slurm.conf` is version-independent across the tested releases, so a single
`config/slurm.conf` serves all of them. If a future Slurm release needs a
different `slurm.conf`, add a version switch in the Dockerfile's config-copy
step (see the sibling provider's `docker/Dockerfile`, which keys config off
`MAJOR_MINOR`, for the pattern).

## Publishing to GHCR

### Option A — the build-and-push workflow (recommended)

`.github/workflows/build-test-images.yml` builds and pushes the matrix using the
workflow's `GITHUB_TOKEN` (no PAT needed). Trigger it from the Actions tab
("Run workflow"), optionally overriding the version list. This is the normal way
to publish new images.

### Option B — manually from a workstation

Log in with a Personal Access Token that has the `write:packages` scope:

```bash
echo "$GHCR_PAT" | docker login ghcr.io -u pescobar --password-stdin
PUSH=1 ./build-images.sh 26.05.1
```

The first push of a new package name creates it as **private**; make it public
once (repo → Packages → the package → Package settings → Change visibility) so
CI and users can pull without authenticating. Link it to this repo via the
package settings if GHCR does not auto-link it from the
`org.opencontainers.image.source` label.

## Adding a new Slurm version

1. Confirm the tarball exists at
   `https://download.schedmd.com/slurm/slurm-<version>.tar.bz2`.
2. Add it to `DEFAULT_VERSIONS` in `build-images.sh`, to the matrix in
   `.github/workflows/acceptance-management.yml`, and to the default in
   `.github/workflows/build-test-images.yml`.
3. Build + push (Option A or B).
4. Update the table above and the version list in the top-level `README.md` /
   `CLAUDE.md`.
