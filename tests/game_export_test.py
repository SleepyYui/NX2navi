"""Portable game exports with synthetic game files."""
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class GameExportTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.game = self.root / 'installed'
        self.out = self.root / 'portable.zip'
        self.files = {
            'ocarina.exe', 'SDL3.dll', 'libopus-0.dll', 'nx2navi_xlate.exe', 'nx2navi_xlate.id',
            'exefs/main.npdm', 'exefs/main', 'exefs/rtld', 'exefs/sdk', 'exefs/subsdk0',
            'romfs/romfs.bin', 'romfs-index.bin',
            'romfs-patches-steamdeck/manifest.tsv', 'shader-cache/program.spv', 'shader-programs.bin',
            'profile/account.uid', 'profile/save/user/slot1', 'profile/save-tx/ready-7/slot1',
        }
        for name in self.files | {
            'logs/private.log', 'profile/instance.lock', 'profile/save-tx/work/slot1', 'pipeline-cache.bin',
            'build-cache/obj/large.o', 'vulkan-1.dll', 'prod.keys', 'profile/save/user/title.keys',
        }:
            path = self.game / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
        (self.game / 'nx2navi.ini').write_text(
            '[input]\nbind_a = key:default, pad:south\nROMFS = "D:\\original"\n', encoding='utf-8', newline='\n')

    def test_cli_exports_playable_files_without_source_paths_or_build_cache(self):
        result = subprocess.run([sys.executable, '-m', 'launcher.cli', '--root', str(self.root / 'launcher'),
                                 'export', '--game', str(self.game), '--out', str(self.out)],
                                cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(self.out) as archive:
            self.assertEqual(set(archive.namelist()), self.files | {'README.txt', 'nx2navi.ini', 'profile/'})
            for name in self.files:
                self.assertEqual(archive.read(name), name.encode())
            self.assertEqual(archive.read('nx2navi.ini'), b'[input]\nbind_a = key:default, pad:south\n')
            self.assertIn(b'wine ocarina.exe', archive.read('README.txt'))

    def test_incomplete_game_leaves_an_existing_export_intact(self):
        from launcher import game_export

        self.out.write_bytes(b'previous export')
        (self.game / 'romfs/romfs.bin').unlink()
        with self.assertRaisesRegex(ValueError, 'romfs/romfs.bin'):
            game_export.export(self.game, self.out)
        self.assertEqual(self.out.read_bytes(), b'previous export')

    def test_large_members_use_zip64(self):
        from launcher import game_export

        payload = b'image' * 250
        (self.game / 'romfs/romfs.bin').write_bytes(payload)
        with patch.object(zipfile, 'ZIP64_LIMIT', 1024):
            game_export.export(self.game, self.out)
        with zipfile.ZipFile(self.out) as archive:
            self.assertEqual(archive.getinfo('romfs/romfs.bin').extract_version, 45)
            self.assertEqual(archive.read('romfs/romfs.bin'), payload)

    def test_custom_profile_moves_into_the_portable_profile_folder(self):
        from launcher import game_export

        profile = self.root / 'saved games'
        (profile / 'save').mkdir(parents=True)
        (profile / 'save/slot1').write_bytes(b'custom save')
        with (self.game / 'nx2navi.ini').open('a', encoding='utf-8') as ini:
            ini.write('profile_dir = "../saved games"\n')
        game_export.export(self.game, self.out)
        with zipfile.ZipFile(self.out) as archive:
            self.assertEqual(archive.read('profile/save/slot1'), b'custom save')
            self.assertNotIn('profile/save/user/slot1', archive.namelist())
            self.assertNotIn(b'profile_dir', archive.read('nx2navi.ini'))

    def test_cancelling_during_copy_removes_partial_zip(self):
        from launcher import game_export, net

        copied = []
        with self.assertRaises(net.Cancelled):
            game_export.export(self.game, self.out, progress=lambda done, total: copied.append(done),
                               cancel=lambda: bool(copied and copied[-1]))
        self.assertTrue(copied and copied[-1])
        self.assertFalse(self.out.exists())
        self.assertEqual(list(self.root.glob('*.part')), [])


if __name__ == '__main__':
    unittest.main()
