#!/usr/bin/env python3
"""Snapshot an OpenStack server into a private Glance image.

Run on the Ansible control host by playbooks/build_compute_image.yml; needs
the openstacksdk that the openstack.cloud modules already require.

The image is forced to private visibility: a compute-node image carries the
cluster's munge key, so anyone who can boot it can authenticate to the
cluster. Glance's "private" means the owning project, not one user; share it
with other projects explicitly if that is what you want.
"""

import argparse
import sys

import openstack


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud", required=True, help="clouds.yaml entry")
    parser.add_argument("--server", required=True, help="server id or name")
    parser.add_argument("--name", required=True, help="name for the new image")
    parser.add_argument(
        "--timeout", type=int, default=1800, help="seconds to wait for the image (default 1800)"
    )
    args = parser.parse_args()

    conn = openstack.connect(cloud=args.cloud)

    server = conn.compute.find_server(args.server, ignore_missing=True)
    if server is None:
        print(f"server '{args.server}' not found", file=sys.stderr)
        return 1

    image_id = conn.compute.create_server_image(
        server=server, name=args.name, wait=True, timeout=args.timeout
    )
    # Depending on the sdk version this is an Image or just an id.
    image_id = getattr(image_id, "id", image_id)
    image = conn.image.get_image(image_id)

    if image.visibility != "private":
        image = conn.image.update_image(image, visibility="private")
        print(f"visibility was '{image.visibility}', set to private")

    print(f"image {image.name} id={image.id} status={image.status} visibility={image.visibility}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
