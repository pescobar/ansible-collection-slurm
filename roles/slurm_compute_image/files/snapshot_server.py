#!/usr/bin/env python3
"""Turn a prepared builder into a private Glance image.

Run on the Ansible control host by playbooks/build_compute_image.yml; needs
the openstacksdk that the openstack.cloud modules already require.

Two ways in, because clouds differ:

--server  snapshot a running (image-backed) server, the classic instance
          snapshot;
--volume  upload a detached volume, for clouds whose flavors have no local
          disk (disk=0), where every server is volume-backed. The builder is
          deleted first so its volume is 'available', and the resulting image
          is a normal image any node can boot from.

The image is forced to private visibility: a compute-node image carries the
cluster's munge key, so anyone who can boot it can authenticate to the
cluster. Glance's "private" means the owning project, not one user; share it
with other projects explicitly if that is what you want.
"""

import argparse
import sys
import time

import openstack


def wait_for_image(conn, image_id, timeout, name=None):
    """Return the image once it leaves 'queued'/'saving', or None on timeout."""
    deadline = time.time() + timeout
    status = "missing"
    while time.time() < deadline:
        # cinder hands back the image id before glance has a record of it, so
        # a 404 here means 'not yet', not 'never': keep polling, and look the
        # name up as well in case the image lands under another id.
        try:
            image = conn.image.get_image(image_id)
        except openstack.exceptions.NotFoundException:
            image = conn.image.find_image(name, ignore_missing=True) if name else None
            if image is None:
                time.sleep(5)
                continue
            image_id = image.id
        status = image.status
        if status == "active":
            return image
        if status in ("killed", "deleted", "pending_delete"):
            print(f"image {image_id} ended in status '{status}'", file=sys.stderr)
            return None
        time.sleep(5)
    print(f"image {image_id} still '{status}' after {timeout}s", file=sys.stderr)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cloud", help="clouds.yaml entry; omit to use the OS_* environment"
    )
    parser.add_argument("--server", help="server id or name to snapshot")
    parser.add_argument("--volume", help="volume id to upload (must be available)")
    parser.add_argument("--name", required=True, help="name for the new image")
    parser.add_argument(
        "--timeout", type=int, default=1800, help="seconds to wait for the image (default 1800)"
    )
    args = parser.parse_args()
    if bool(args.server) == bool(args.volume):
        parser.error("pass exactly one of --server or --volume")

    conn = openstack.connect(cloud=args.cloud) if args.cloud else openstack.connect()

    if args.server:
        server = conn.compute.find_server(args.server, ignore_missing=True)
        if server is None:
            print(f"server '{args.server}' not found", file=sys.stderr)
            return 1
        image = conn.compute.create_server_image(
            server=server, name=args.name, wait=True, timeout=args.timeout
        )
        image_id = getattr(image, "id", image)
    else:
        volume = conn.block_storage.find_volume(args.volume, ignore_missing=True)
        if volume is None:
            print(f"volume '{args.volume}' not found", file=sys.stderr)
            return 1
        if volume.status != "available":
            print(
                f"volume {volume.id} is '{volume.status}', not 'available': "
                "delete the builder server first",
                file=sys.stderr,
            )
            return 1
        # Returns the volume-image metadata, including the new image's id.
        result = conn.block_storage.upload_volume_to_image(
            volume, image_name=args.name, disk_format="qcow2", container_format="bare"
        )
        image_id = getattr(result, "image_id", None) or result["image_id"]

    image = wait_for_image(conn, image_id, args.timeout, args.name)
    if image is None:
        return 1

    # The volume stays 'uploading' for a while after the image goes active;
    # deleting it before it is 'available' again fails.
    if args.volume:
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            volume = conn.block_storage.get_volume(volume.id)
            if volume.status == "available":
                break
            time.sleep(5)
        else:
            print(
                f"volume {volume.id} is still '{volume.status}' after the upload",
                file=sys.stderr,
            )

    if image.visibility != "private":
        image = conn.image.update_image(image, visibility="private")
        print(f"visibility was '{image.visibility}', set to private")

    print(f"image {image.name} id={image.id} status={image.status} visibility={image.visibility}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
