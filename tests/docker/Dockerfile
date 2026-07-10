# Self-contained Slurm test cluster image for the pescobar.slurm acceptance
# tests. Compiles Slurm from the official SchedMD source tarball on Ubuntu
# 24.04 (the build recipe is adapted from the sibling terraform-provider-slurm
# `docker/Dockerfile`), then adds the two things this collection needs that the
# provider's REST-only image does not: an SSH server and python3, so Ansible
# can drive `sacctmgr` on the controller over SSH exactly as in production.
#
# Published to ghcr.io/pescobar/ansible-collection-slurm/slurm-test:<version>.
# The image ships NO authorized key — it is safe to publish. The throwaway test
# key is injected at RUNTIME (bind-mounted, see docker-compose.yml), and sshd is
# started only when ENABLE_SSHD=true. Services (slurmdbd / slurmctld) are
# selected via CMD; the same image runs both.
#
#   docker build --build-arg SLURM_VERSION=25.05.8 \
#     -t ghcr.io/pescobar/ansible-collection-slurm/slurm-test:25.05.8 .
#
# See README.md for building the whole version matrix and pushing to GHCR.

ARG SLURM_VERSION
ARG GOSU_VERSION=1.19

# ============================================================================
# Stage 1: Build gosu from source (avoids CVEs in pre-built binaries)
# https://github.com/tianon/gosu/issues/136
# ============================================================================
FROM golang:1.24-bookworm AS gosu-builder

ARG GOSU_VERSION
ARG TARGETOS
ARG TARGETARCH

RUN git clone --branch ${GOSU_VERSION} --depth 1 \
      https://github.com/tianon/gosu.git /go/src/github.com/tianon/gosu \
    && cd /go/src/github.com/tianon/gosu \
    && go mod download \
    && CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} \
       go build -v -trimpath -ldflags '-d -w' -o /go/bin/gosu . \
    && chmod +x /go/bin/gosu

# ============================================================================
# Stage 2: Compile Slurm and install to /build (used as DESTDIR)
# ============================================================================
FROM ubuntu:24.04 AS builder

ARG SLURM_VERSION
ARG DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    wget \
    bzip2 \
    pkg-config \
    libmunge-dev \
    libssl-dev \
    libmariadbd-dev \
    libhwloc-dev \
    libjwt-dev \
    libhttp-parser-dev \
    libjson-c-dev \
    libyaml-dev \
    liblua5.3-dev \
    liblz4-dev \
    libreadline-dev \
    libpam0g-dev \
    libnuma-dev \
    python3 \
    && rm -rf /var/lib/apt/lists/*

RUN wget --no-check-certificate -q -O /tmp/slurm-${SLURM_VERSION}.tar.bz2 \
      https://download.schedmd.com/slurm/slurm-${SLURM_VERSION}.tar.bz2 \
    && tar xf /tmp/slurm-${SLURM_VERSION}.tar.bz2 -C /tmp \
    && cd /tmp/slurm-${SLURM_VERSION} \
    && ./configure \
         --prefix=/usr \
         --sysconfdir=/etc/slurm \
         --localstatedir=/var \
         --with-munge \
         --with-jwt \
         --with-mysql_config \
         --with-slurmrestd \
         --without-ofed \
    && make -j$(nproc) \
    && make install DESTDIR=/build \
    && rm -rf /tmp/slurm-*

# ============================================================================
# Stage 3: Minimal runtime image + sshd + python3
# ============================================================================
FROM ubuntu:24.04

ARG SLURM_VERSION
ARG DEBIAN_FRONTEND=noninteractive

LABEL org.opencontainers.image.source="https://github.com/pescobar/ansible-collection-slurm" \
      org.opencontainers.image.title="slurm-test" \
      org.opencontainers.image.description="Slurm ${SLURM_VERSION} test cluster (Ubuntu 24.04) with sshd + python3 for the pescobar.slurm Ansible acceptance tests — no compute nodes"

RUN apt-get update && apt-get install -y --no-install-recommends \
    munge \
    libssl3 \
    mariadb-client \
    libhwloc15 \
    libjwt2 \
    libhttp-parser2.9 \
    libjson-c5 \
    libyaml-0-2 \
    liblua5.3-0 \
    liblz4-1 \
    libreadline8 \
    libpam0g \
    libnuma1 \
    gettext-base \
    curl \
    jq \
    openssh-server \
    python3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=gosu-builder /go/bin/gosu /usr/local/bin/gosu
COPY --from=builder /build /

RUN groupadd -r --gid=990 slurm \
    && useradd -r -g slurm --uid=990 slurm \
    && groupadd -r --gid=991 slurmrest \
    && useradd -r -g slurmrest --uid=991 slurmrest \
    && dd if=/dev/urandom of=/etc/munge/munge.key bs=1 count=1024 \
    && chmod 400 /etc/munge/munge.key \
    && chown munge:munge /etc/munge/munge.key \
    && mkdir -m 0755 -p \
         /var/run/slurm \
         /var/spool/slurm \
         /var/lib/slurm \
         /var/log/slurm \
         /etc/slurm \
    && chown slurm:slurm \
         /var/run/slurm \
         /var/spool/slurm \
         /var/lib/slurm \
         /var/log/slurm \
         /etc/slurm \
    && mkdir -m 0755 -p /run/sshd \
    && mkdir -m 0700 -p /root/.ssh

# Slurm config (control plane only; slurm.conf is version-independent across
# the tested releases). slurmdbd.conf carries ${MYSQL_*} placeholders that the
# entrypoint substitutes at startup.
COPY config/ /etc/slurm/
RUN chown slurm:slurm /etc/slurm/slurm.conf /etc/slurm/slurmdbd.conf \
        /etc/slurm/cgroup.conf /etc/slurm/gres.conf \
    && chmod 644 /etc/slurm/slurm.conf /etc/slurm/cgroup.conf /etc/slurm/gres.conf \
    && chmod 600 /etc/slurm/slurmdbd.conf

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["slurmdbd"]
