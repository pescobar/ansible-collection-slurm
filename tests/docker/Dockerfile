# Test-only SSH overlay for the Slurm test cluster images.
#
# The base images (ghcr.io/pescobar/slurm-test:<version>, built from
# ../../../docker/) run no sshd and carry no Python. This overlay adds
# openssh-server + python3 and injects a throwaway test keypair so the
# Ansible acceptance tests can target slurmctld over SSH, as in production.
#
# Build (after ./generate-ssh-key.sh has created .ssh/authorized_keys):
#   docker build --build-arg SLURM_VERSION=25.05.4 -t slurm-test-ssh:25.05.4 .
# or via the compose override in this directory.
#
# Never publish this image: it grants root SSH access to whoever holds the
# generated test key.

ARG SLURM_VERSION
FROM ghcr.io/pescobar/slurm-test:${SLURM_VERSION}

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       openssh-server python3 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd /root/.ssh \
    && chmod 700 /root/.ssh

COPY .ssh/authorized_keys /root/.ssh/authorized_keys
COPY entrypoint-ssh.sh /usr/local/bin/entrypoint-ssh.sh
RUN chmod 600 /root/.ssh/authorized_keys \
    && chmod +x /usr/local/bin/entrypoint-ssh.sh

ENTRYPOINT ["/usr/local/bin/entrypoint-ssh.sh"]
CMD ["slurmdbd"]
