#!/usr/bin/env python3
"""Export an AmneziaWG Docker container from a VPS over SSH.

The resulting archive contains private VPN keys. Never commit it to Git.
"""

import argparse
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


def ssh_command(host: str, args: list[str]) -> list[str]:
    # OpenSSH still verifies host keys using the caller's normal SSH settings.
    return ["ssh", "-T", host, shlex.join(args)]


def remote(host: str, *args: str) -> str:
    result = subprocess.run(ssh_command(host, list(args)), text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        raise ExportError(f"Remote command failed ({shlex.join(args)}):\n{result.stderr.strip()}")
    return result.stdout.strip()


def remote_json(host: str, *args: str):
    try:
        return json.loads(remote(host, *args))
    except json.JSONDecodeError as exc:
        raise ExportError(f"Invalid JSON from remote command: {shlex.join(args)}") from exc


def check_source(host: str, container: str) -> dict:
    info = remote_json(host, "docker", "inspect", container)[0]
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
        remote(host, "docker", "exec", container, "test", "-s",
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
        definition = remote_json(host, "docker", "network", "inspect", name)[0]
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


def save_remote_image(host: str, tag: str, path: Path) -> None:
    # docker save is a binary tar stream. gzip runs locally, so SSH stdout stays binary-clean.
    with path.open("wb") as target:
        process = subprocess.Popen(ssh_command(host, ["docker", "image", "save", tag]),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert process.stdout is not None
        assert process.stderr is not None
        try:
            with gzip.GzipFile(fileobj=target, mode="wb", mtime=0) as packed:
                shutil.copyfileobj(process.stdout, packed, 1024 * 1024)
            process.stdout.close()
            error = process.stderr.read().decode("utf-8", errors="replace")
            if process.wait() != 0:
                raise ExportError(f"Could not download Docker image: {error.strip()}")
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
    config = check_source(host, container)
    tag = f"amnezia-migrate:export-{uuid.uuid4().hex[:12]}"
    os.umask(0o077)
    print(f"Creating snapshot of {container} on {host}...", flush=True)
    committed = False
    try:
        remote(host, "docker", "commit", container, tag)
        committed = True
        image = remote_json(host, "docker", "image", "inspect", tag)[0]
        with tempfile.TemporaryDirectory(prefix="amnezia-export-", dir=destination.parent) as tmp:
            image_path = Path(tmp) / "image.tar.gz"
            print("Downloading image over SSH...", flush=True)
            save_remote_image(host, tag, image_path)
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
                remote(host, "docker", "image", "rm", tag)
            except ExportError as exc:
                print(f"Warning: temporary image {tag} remains on the VPS: {exc}",
                      file=sys.stderr)
    print(f"Saved: {destination} ({destination.stat().st_size:,} bytes)")
    print("Contains VPN private keys. Keep the archive private; never upload it to GitHub.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Export an AmneziaWG VPS to one local archive")
    subparsers = parser.add_subparsers(dest="action", required=True)
    command = subparsers.add_parser("export", help="Snapshot and download the server")
    command.add_argument("host", help="SSH destination, e.g. root@old-vps.example.com")
    command.add_argument("output", type=Path, help="Local .tar.gz output file")
    command.add_argument("--container", default="amnezia-awg2", help="Container name")
    args = parser.parse_args()
    try:
        export(args.host, args.output.expanduser().absolute(), args.container)
    except (ExportError, OSError, KeyError, IndexError) as exc:
        print(f"Export failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
