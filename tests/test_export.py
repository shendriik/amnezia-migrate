import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


FAKE_SSH = r'''#!/usr/bin/env python3
import json, os, shlex, sys
from pathlib import Path

command = shlex.split(sys.argv[3])
with Path(os.environ["FAKE_LOG"]).open("a") as log:
    log.write(json.dumps(command) + "\n")
if command[:2] == ["docker", "inspect"]:
    mounts = [{"Type": "bind", "Source": "/lib/modules", "Destination": "/lib/modules"}]
    if os.environ.get("FAKE_VOLUME"):
        mounts.append({"Type": "volume", "Source": "important", "Destination": "/data"})
    print(json.dumps([{"State": {"Running": True}, "Mounts": mounts,
        "HostConfig": {"NetworkMode": "bridge", "PortBindings": {"999/udp": [{"HostIp":"", "HostPort":"999"}]},
            "Privileged": True, "CapAdd": ["CAP_NET_ADMIN", "CAP_SYS_MODULE"],
            "Sysctls": {"net.ipv4.conf.all.src_valid_mark": "1"},
            "RestartPolicy": {"Name":"always"}, "LogConfig":{"Type":"none"}},
        "NetworkSettings": {"Networks": {"bridge": {"IPAddress":"172.17.0.2"},
            "amnezia-dns-net": {"IPAddress":"172.29.172.2"}}}}]))
elif command[:3] == ["docker", "network", "inspect"]:
    print(json.dumps([{"Driver":"bridge", "IPAM":{"Config":[{"Subnet":"172.29.172.0/24", "Gateway":"172.29.172.1"}]}}]))
elif command[:3] == ["docker", "image", "inspect"]:
    print(json.dumps([{"Architecture":"amd64", "Os":"linux"}]))
elif command[:3] == ["docker", "image", "save"]:
    sys.stdout.buffer.write(b"test docker image tar")
elif command[:2] in (["docker", "commit"], ["docker", "exec"], ["docker", "image"]):
    pass
else:
    print("Unexpected command: " + repr(command), file=sys.stderr)
    sys.exit(2)
'''


class ExportIntegrationTests(unittest.TestCase):
    def run_export(self, volume=False):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            fake_ssh = directory / "ssh"
            fake_ssh.write_text(FAKE_SSH)
            fake_ssh.chmod(0o755)
            output = directory / "backup.tar.gz"
            log = directory / "commands.jsonl"
            env = dict(os.environ, PATH=str(directory) + os.pathsep + os.environ["PATH"],
                       FAKE_LOG=str(log), FAKE_VOLUME="1" if volume else "")
            result = subprocess.run([sys.executable, str(ROOT / "amnezia_migrate.py"),
                                     "export", "root@vps", str(output)], env=env,
                                    capture_output=True, text=True)
            commands = [json.loads(line) for line in log.read_text().splitlines()]
            if volume:
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(output.exists())
                self.assertFalse(any(c[:2] == ["docker", "commit"] for c in commands))
                return
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with tarfile.open(output, "r:gz") as archive:
                self.assertEqual(sorted(archive.getnames()), ["image.tar.gz", "manifest.json"])
                manifest = json.load(archive.extractfile("manifest.json"))
                compressed_image = archive.extractfile("image.tar.gz").read()
            self.assertEqual(manifest["image_sha256"], hashlib.sha256(compressed_image).hexdigest())
            self.assertEqual(manifest["container"]["port_bindings"]["999/udp"][0]["HostPort"], "999")
            self.assertEqual(manifest["container"]["networks"]["amnezia-dns-net"]["ipv4_address"], "172.29.172.2")
            self.assertTrue(any(c[:2] == ["docker", "commit"] for c in commands))
            self.assertTrue(any(c[:3] == ["docker", "image", "rm"] for c in commands))
            self.assertFalse(any(c[1] in ("stop", "rm", "restart") for c in commands))

    def test_export_packages_recoverable_state(self):
        self.run_export()

    def test_data_volume_stops_export_before_snapshot(self):
        self.run_export(volume=True)


if __name__ == "__main__":
    unittest.main()
