#!/usr/bin/env python3
"""Export an AmneziaWG Docker container from a VPS over SSH.

The resulting archive contains private VPN keys. Never commit it to Git.
"""

import argparse
from contextlib import contextmanager
import datetime as dt
import gzip
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid


class ExportError(Exception):
    pass


@contextmanager
def ssh_session(host: str):
    # Reuse the first authenticated SSH connection. With password login this
    # avoids prompting again for every inspect, commit and image transfer.
    with tempfile.TemporaryDirectory(prefix="amnezia-ssh-") as directory:
        socket_path = str(Path(directory) / "socket")
        options = ["-o", f"ControlPath={socket_path}", "-o", "ControlMaster=auto",
                   "-o", "ControlPersist=60"]

        def command(args: list[str]) -> list[str]:
            # Normal host-key verification remains enabled.
            return ["ssh", "-T", *options, host, shlex.join(args)]

        try:
            yield command
        finally:
            subprocess.run(["ssh", "-S", socket_path, "-O", "exit", host],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def remote(command, *args: str) -> str:
    # stderr stays on the terminal so OpenSSH can show a password prompt.
    result = subprocess.run(command(list(args)), text=True,
                            stdout=subprocess.PIPE)
    if result.returncode:
        raise ExportError(f"Remote command failed ({shlex.join(args)}), exit {result.returncode}")
    return result.stdout.strip()


def remote_json(command, *args: str):
    try:
        return json.loads(remote(command, *args))
    except json.JSONDecodeError as exc:
        raise ExportError(f"Invalid JSON from remote command: {shlex.join(args)}") from exc


def check_source(command, container: str) -> dict:
    info = remote_json(command, "docker", "inspect", container)[0]
    if not info.get("State", {}).get("Running"):
        raise ExportError(f"Container {container} is not running")
    mounts = info.get("Mounts", [])
    unsupported = [m for m in mounts if not (
        m.get("Type") == "bind"
        and m.get("Source") == "/lib/modules"
        and m.get("Destination") == "/lib/modules"
    )]
    if unsupported:
        raise ExportError("Container has data mounts outside /lib/modules. "
                          "Exporting its image alone would lose data; no archive was created.")
    for filename in ("awg0.conf", "clientsTable", "wireguard_server_private_key.key",
                     "wireguard_server_public_key.key", "wireguard_psk.key"):
        remote(command, "docker", "exec", container, "test", "-s",
               f"/opt/amnezia/awg/{filename}")
    hc = info["HostConfig"]
    if hc.get("NetworkMode") != "bridge":
        raise ExportError("Only containers with the default bridge as primary network are supported")
    ports = hc.get("PortBindings") or {}
    if not ports or any(not key.endswith("/udp") for key in ports):
        raise ExportError("Expected published UDP port bindings")
    networks = {}
    for name, attachment in (info.get("NetworkSettings", {}).get("Networks") or {}).items():
        if name == "bridge":
            continue
        definition = remote_json(command, "docker", "network", "inspect", name)[0]
        if definition.get("Driver") != "bridge":
            raise ExportError(f"Unsupported network driver for {name}")
        networks[name] = {
            "driver": "bridge",
            "ipam": definition.get("IPAM", {}).get("Config") or [],
            "ipv4_address": attachment.get("IPAddress", ""),
        }
    return {
        "container_name": container,
        "port_bindings": ports,
        "privileged": hc.get("Privileged", False),
        "cap_add": hc.get("CapAdd") or [],
        "sysctls": hc.get("Sysctls") or {},
        "restart_policy": hc.get("RestartPolicy") or {},
        "log_driver": (hc.get("LogConfig") or {}).get("Type", ""),
        "networks": networks,
        "mounts": [{"source": "/lib/modules", "destination": "/lib/modules"}] if mounts else [],
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def save_remote_image(command, tag: str, path: Path) -> None:
    # docker save is a binary tar stream. gzip runs locally, so SSH stdout stays binary-clean.
    with path.open("wb") as target:
        process = subprocess.Popen(command(["docker", "image", "save", tag]),
                                   stdout=subprocess.PIPE)
        assert process.stdout is not None
        try:
            with gzip.GzipFile(fileobj=target, mode="wb", mtime=0) as packed:
                shutil.copyfileobj(process.stdout, packed, 1024 * 1024)
            process.stdout.close()
            if process.wait() != 0:
                raise ExportError("Could not download Docker image; see SSH error above")
            target.flush()
            os.fsync(target.fileno())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def export(host: str, destination: Path, container: str) -> None:
    if not destination.parent.is_dir():
        raise ExportError(f"Output directory does not exist: {destination.parent}")
    if destination.exists():
        raise ExportError(f"Refusing to overwrite existing file: {destination}")
    tag = f"amnezia-migrate:export-{uuid.uuid4().hex[:12]}"
    os.umask(0o077)
    with ssh_session(host) as command:
        config = check_source(command, container)
        print(f"Creating snapshot of {container} on {host}...", flush=True)
        committed = False
        try:
            remote(command, "docker", "commit", container, tag)
            committed = True
            image = remote_json(command, "docker", "image", "inspect", tag)[0]
            with tempfile.TemporaryDirectory(prefix="amnezia-export-", dir=destination.parent) as tmp:
                image_path = Path(tmp) / "image.tar.gz"
                print("Downloading image over SSH...", flush=True)
                save_remote_image(command, tag, image_path)
                manifest = {
                    "schema_version": 1,
                    "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "image_tag": tag,
                    "image_architecture": image["Architecture"],
                    "image_os": image["Os"],
                    "image_sha256": sha256(image_path),
                    "image_size": image_path.stat().st_size,
                    "container": config,
                }
                manifest_path = Path(tmp) / "manifest.json"
                manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
                partial = Path(tmp) / "package.tar.gz"
                with tarfile.open(partial, "w:gz") as archive:
                    archive.add(manifest_path, arcname="manifest.json")
                    archive.add(image_path, arcname="image.tar.gz")
                # On the same filesystem, rename makes the completed archive visible atomically.
                if destination.exists():
                    raise ExportError(f"Output appeared during export: {destination}")
                os.replace(partial, destination)
                destination.chmod(0o600)
        finally:
            if committed:
                try:
                    remote(command, "docker", "image", "rm", tag)
                except ExportError as exc:
                    print(f"Warning: temporary image {tag} remains on the VPS: {exc}",
                          file=sys.stderr)
    print(f"Saved: {destination} ({destination.stat().st_size:,} bytes)")
    print("Contains VPN private keys. Keep the archive private; never upload it to GitHub.")


def read_backup(archive_path: Path, directory: Path) -> tuple[dict, Path]:
    """Read only the two expected regular files and verify the image stream."""
    image_path = directory / "image.tar.gz"
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        if sorted(m.name for m in members) != ["image.tar.gz", "manifest.json"] or any(
            not m.isfile() for m in members
        ):
            raise ExportError("Archive must contain only regular manifest.json and image.tar.gz files")
        manifest_member = next(m for m in members if m.name == "manifest.json")
        if manifest_member.size > 1024 * 1024:
            raise ExportError("Manifest is too large")
        manifest = json.load(archive.extractfile(manifest_member))
        image_member = next(m for m in members if m.name == "image.tar.gz")
        digest = hashlib.sha256()
        size = 0
        with archive.extractfile(image_member) as source, image_path.open("wb") as target:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                target.write(block)
                digest.update(block)
                size += len(block)
    if manifest.get("schema_version") != 1 or manifest.get("image_sha256") != digest.hexdigest() or manifest.get("image_size") != size:
        raise ExportError("Unsupported archive format or image checksum mismatch")
    config = manifest.get("container")
    if not isinstance(config, dict) or config.get("container_name") != "amnezia-awg2":
        raise ExportError("This importer supports only amnezia-awg2")
    if manifest.get("image_os") != "linux" or manifest.get("image_architecture") != "amd64":
        raise ExportError("Only Linux amd64 images are supported")
    tag = manifest.get("image_tag")
    if not isinstance(tag, str) or not tag.startswith("amnezia-migrate:export-") or not tag.split("-")[-1].isalnum():
        raise ExportError("Invalid image tag in archive")
    ports = config.get("port_bindings")
    if not isinstance(ports, dict) or not ports or any(
        not key.endswith("/udp") or not key[:-4].isdigit() or not isinstance(values, list)
        or not values or any(not isinstance(value, dict)
                             or value.get("HostIp", "") not in ("", "0.0.0.0")
                             or not str(value.get("HostPort", "")).isdigit()
                             for value in values)
        for key, values in ports.items()
    ):
        raise ExportError("Invalid UDP port bindings in manifest")
    if any(m != {"source": "/lib/modules", "destination": "/lib/modules"}
           for m in config.get("mounts", [])):
        raise ExportError("Unsupported mount in manifest")
    return manifest, image_path


def install_docker(command) -> None:
    # Use Docker's official apt repository for a clean Debian/Ubuntu host.
    script = """set -eu
. /etc/os-release
case "$ID" in ubuntu|debian) ;; *) echo 'Docker installer requires Ubuntu or Debian' >&2; exit 1;; esac
command -v apt-get >/dev/null
apt-get update
apt-get install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
ARCH=$(dpkg --print-architecture)
printf 'Types: deb\nURIs: https://download.docker.com/linux/%s\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' "$ID" "${UBUNTU_CODENAME:-${VERSION_CODENAME}}" "$ARCH" > /etc/apt/sources.list.d/docker.sources
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker
"""
    remote(command, "sh", "-c", script)


def restore_networks(command, networks: dict) -> None:
    for name, definition in networks.items():
        if not name or name in ("bridge", "host", "none") or not all(
            ch.isalnum() or ch in "_.-" for ch in name
        ):
            raise ExportError("Invalid custom Docker network name")
        ipam = definition.get("ipam") or []
        if definition.get("driver") != "bridge" or not isinstance(ipam, list):
            raise ExportError(f"Unsupported network configuration: {name}")
        existing = subprocess.run(command(["docker", "network", "inspect", name]),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if existing.returncode == 0:
            current = remote_json(command, "docker", "network", "inspect", name)[0]
            if current.get("Driver") != "bridge" or (current.get("IPAM") or {}).get("Config") != ipam:
                raise ExportError(f"Existing network {name} differs from backup")
        else:
            args = ["docker", "network", "create", "--driver", "bridge"]
            for address in ipam:
                for key, option in (("Subnet", "--subnet"), ("Gateway", "--gateway"),
                                    ("IPRange", "--ip-range")):
                    if address.get(key):
                        args.extend([option, address[key]])
            remote(command, *args, name)


def import_backup(archive_path: Path, host: str) -> None:
    if not archive_path.is_file():
        raise ExportError(f"Archive does not exist: {archive_path}")
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="amnezia-import-") as directory:
        manifest, image_path = read_backup(archive_path, Path(directory))
        config = manifest["container"]
        with ssh_session(host) as command:
            if remote(command, "id", "-u") != "0":
                raise ExportError("SSH user must be root (use root@server)")
            if remote(command, "uname", "-m") != "x86_64":
                raise ExportError("Target must be x86_64")
            found = subprocess.run(command(["sh", "-c", "command -v docker"]),
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if found.returncode:
                print("Installing Docker on target...", flush=True)
                install_docker(command)
            else:
                remote(command, "systemctl", "start", "docker")
            remote(command, "docker", "info", "--format", "{{.ServerVersion}}")
            existing = subprocess.run(command(["docker", "container", "inspect", "amnezia-awg2"]),
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if existing.returncode == 0:
                raise ExportError("Target already has an amnezia-awg2 container; nothing was restored")
            print("Uploading Docker image over SSH...", flush=True)
            with image_path.open("rb") as source:
                result = subprocess.run(command(["docker", "image", "load"]), stdin=source)
            if result.returncode:
                raise ExportError("Docker could not load the saved image")
            networks = config.get("networks") or {}
            restore_networks(command, networks)
            args = ["docker", "create", "--name", "amnezia-awg2", "--network", "bridge"]
            if config.get("privileged"):
                args.append("--privileged")
            for capability in config.get("cap_add") or []:
                args.extend(["--cap-add", capability])
            for key, value in (config.get("sysctls") or {}).items():
                args.extend(["--sysctl", f"{key}={value}"])
            restart = (config.get("restart_policy") or {}).get("Name", "no")
            if restart not in ("no", "always", "unless-stopped", "on-failure"):
                raise ExportError(f"Unsupported restart policy: {restart}")
            args.extend(["--restart", restart])
            if config.get("log_driver"):
                args.extend(["--log-driver", config["log_driver"]])
            for container_port, bindings in config["port_bindings"].items():
                for binding in bindings:
                    args.extend(["-p", f"{binding['HostPort']}:{container_port}"])
            for mount in config.get("mounts") or []:
                args.extend(["-v", f"{mount['source']}:{mount['destination']}"])
            remote(command, *args, manifest["image_tag"])
            for name, definition in networks.items():
                connect = ["docker", "network", "connect"]
                if definition.get("ipv4_address"):
                    connect.extend(["--ip", definition["ipv4_address"]])
                remote(command, *connect, name, "amnezia-awg2")
            remote(command, "docker", "start", "amnezia-awg2")
            if remote(command, "docker", "inspect", "--format", "{{.State.Running}}", "amnezia-awg2") != "true":
                raise ExportError("Restored container failed to start")
            for filename in ("awg0.conf", "clientsTable", "wireguard_server_private_key.key",
                             "wireguard_server_public_key.key", "wireguard_psk.key"):
                remote(command, "docker", "exec", "amnezia-awg2", "test", "-s",
                       f"/opt/amnezia/awg/{filename}")
    print("Import complete. Check the UDP firewall and update DuckDNS to the new VPS IP when ready.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Export or import an AmneziaWG VPS via SSH")
    subparsers = parser.add_subparsers(dest="action", required=True)
    command = subparsers.add_parser("export", help="Snapshot and download the server")
    command.add_argument("host", help="SSH destination, e.g. root@old-vps.example.com")
    command.add_argument("output", type=Path, help="Local .tar.gz output file")
    command.add_argument("--container", default="amnezia-awg2", help="Container name")
    command = subparsers.add_parser("import", help="Restore archive to a fresh VPS")
    command.add_argument("archive", type=Path, help="Local archive from export")
    command.add_argument("host", help="SSH destination, e.g. root@new-vps.example.com")
    args = parser.parse_args()
    try:
        if args.action == "export":
            export(args.host, args.output.expanduser().absolute(), args.container)
        else:
            import_backup(args.archive.expanduser().absolute(), args.host)
    except (ExportError, OSError, KeyError, IndexError, ValueError, tarfile.TarError,
            json.JSONDecodeError) as exc:
        print(f"{args.action.capitalize()} failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
