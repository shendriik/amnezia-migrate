# AmneziaWG VPS export

This first version implements **export only**. It runs on your PC, connects to
an AmneziaWG VPS over SSH, and writes one local backup archive. It does not
stop, delete, or reconfigure the running container. The Docker snapshot can
briefly pause it while the image is captured.

Requirements: Python 3.10+ and OpenSSH on the PC; SSH access with permission
to run Docker on the VPS. The SSH host key must already be trusted. The VPS
must have a running `amnezia-awg2` container with no data volumes. A bind mount
of `/lib/modules` is supported. This matches the installation described during
development; other Amnezia protocols and volume layouts are not yet supported.
The script reuses one SSH connection, so password authentication prompts once.

```bash
python3 amnezia_migrate.py export root@dusnet.duckdns.org ~/amnezia-backup.tar.gz
```

The command checks server files and mounts, creates a Docker snapshot, downloads
the image, and packages it with a `manifest.json` recording the port, networks,
container settings, architecture and SHA-256 checksum. It removes its temporary
Docker image from the VPS after the export. A failure leaves the original
container untouched. The output file is created with owner-only permissions.

The archive **contains private VPN keys**. Do not commit, publish, or share it.
`import` will be implemented and tested separately before relying on this
archive for an actual migration. Keep the original VPS until a restored server
has been tested.
