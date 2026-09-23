import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import amnezia_migrate as migrate


class ImportTests(unittest.TestCase):
    def backup(self, directory):
        compressed = gzip.compress(b'docker image', mtime=0)
        manifest = {
            'schema_version': 1, 'image_sha256': hashlib.sha256(compressed).hexdigest(),
            'image_size': len(compressed), 'image_os': 'linux', 'image_architecture': 'amd64',
            'image_tag': 'amnezia-migrate:export-123abc',
            'container': {'container_name': 'amnezia-awg2',
                'port_bindings': {'999/udp': [{'HostIp': '', 'HostPort': '999'}]},
                'mounts': [{'source': '/lib/modules', 'destination': '/lib/modules'}],
                'networks': {'amnezia-dns-net': {'driver': 'bridge',
                    'ipam': [{'Subnet': '172.29.172.0/24', 'Gateway': '172.29.172.1'}],
                    'ipv4_address': '172.29.172.2'}}, 'restart_policy': {'Name': 'always'},
                'privileged': True, 'cap_add': ['CAP_NET_ADMIN'], 'sysctls': {},
                'log_driver': 'none'}}
        archive = directory / 'backup.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            for name, data in (('manifest.json', json.dumps(manifest).encode()), ('image.tar.gz', compressed)):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        return archive

    def test_import_preserves_port_and_starts(self):
        with tempfile.TemporaryDirectory() as temp:
            backup = self.backup(Path(temp))
            commands = []
            def remote(command, *args):
                commands.append(args)
                if args == ('id', '-u'): return '0'
                if args == ('uname', '-m'): return 'x86_64'
                if args[:3] == ('docker', 'inspect', '--format'): return 'true'
                return ''
            def process(args, **kwargs):
                cmd = args if isinstance(args, list) else args[-1]
                if cmd[:2] == ['docker', 'image']:
                    self.assertEqual(kwargs['stdin'].read(), gzip.compress(b'docker image', mtime=0))
                return type('Result', (), {'returncode': 1 if cmd[:3] in
                     (['docker', 'container', 'inspect'], ['docker', 'network', 'inspect']) else 0})()
            with patch.object(migrate, 'ssh_session') as ssh, patch.object(migrate, 'remote', side_effect=remote), \
                 patch.object(migrate.subprocess, 'run', side_effect=process):
                ssh.return_value.__enter__.return_value = lambda args: args
                migrate.import_backup(backup, 'root@new')
            create = next(c for c in commands if c[:2] == ('docker', 'create'))
            self.assertIn('999:999/udp', create)
            self.assertIn('--privileged', create)
            self.assertTrue(any(c[:3] == ('docker', 'start', 'amnezia-awg2') for c in commands))

    def test_existing_container_blocks_restore(self):
        with tempfile.TemporaryDirectory() as temp:
            backup = self.backup(Path(temp))
            calls = []
            def remote(command, *args):
                calls.append(args)
                return {'id': '0', 'uname': 'x86_64'}.get(args[0], '')
            with patch.object(migrate, 'ssh_session') as ssh, patch.object(migrate, 'remote', side_effect=remote), \
                 patch.object(migrate.subprocess, 'run', return_value=type('Result', (), {'returncode': 0})()):
                ssh.return_value.__enter__.return_value = lambda args: args
                with self.assertRaisesRegex(migrate.ExportError, 'already has'):
                    migrate.import_backup(backup, 'root@new')
            self.assertFalse(any(c[:2] == ('docker', 'create') for c in calls))

    def test_tampered_archive_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            backup = self.backup(Path(temp))
            with tarfile.open(backup, 'r:gz') as archive:
                manifest = json.load(archive.extractfile('manifest.json'))
            manifest['image_sha256'] = '0' * 64
            with tarfile.open(backup, 'w:gz') as archive:
                for name, data in (('manifest.json', json.dumps(manifest).encode()), ('image.tar.gz', gzip.compress(b'docker image', mtime=0))):
                    info = tarfile.TarInfo(name); info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            with self.assertRaisesRegex(migrate.ExportError, 'checksum'):
                migrate.read_backup(backup, Path(temp))
