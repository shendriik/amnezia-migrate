# AmneziaWG VPS migration

Move an `amnezia-awg2` Docker installation between VPS servers using your PC as the intermediary. Run both commands **on your PC**:

```bash
python3 amnezia_migrate.py export root@old-vps.example.com ~/amnezia-backup.tar.gz
python3 amnezia_migrate.py import ~/amnezia-backup.tar.gz root@new-vps.example.com
```

Requires Python 3.10+ and OpenSSH on the PC, root SSH access to both servers, and a trusted SSH host key for each server. The target needs Debian or Ubuntu on x86_64. The importer installs Docker Engine from Docker's official apt repository when missing, loads the saved image, recreates its supported Docker network, creates and starts `amnezia-awg2`, and checks its essential configuration files. If a container named `amnezia-awg2` already exists on the target, import stops with an error. The original published UDP port and peer keys are preserved. Import does not modify the old server or DuckDNS; after testing, point DuckDNS at the new VPS IP. Open the existing UDP port in your hosting provider's firewall.

The source needs a running `amnezia-awg2` container. Its only supported bind mount is `/lib/modules`; Docker data volumes and other protocols are not supported. The export copies its writable layer with `docker commit` (which may briefly pause the running container), saves the image into a local compressed archive, records settings and checksum in `manifest.json`, and removes the temporary image on the source VPS. The SSH connection is reused so password login normally prompts once per command.

**The backup contains private VPN keys.** Keep it private and do not upload it to GitHub. Keep the source VPS until you have verified that clients connect through the destination. Import validation runs before loading the image; a later failure can leave a newly created Docker image, network, or stopped container on the destination for diagnosis.
