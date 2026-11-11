"""Portable patches compare file content and preserve personal data."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from launcher import game_export, net


class GamePatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.game = self.root / 'game'
        self.baseline = self.root / 'old.zip'
        self.output = self.root / 'patch.zip'
        for name in (*game_export.REQUIRED, game_export.GRAPHICS_TOOL, 'Check Graphics.exe'):
            path = self.game / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
        self.write_baseline()

    def write_baseline(self, prefix=''):
        with zipfile.ZipFile(self.baseline, 'w') as z:
            for name, path in game_export._files(self.game, minimal=True).items():
                z.write(path, prefix + name)
            z.writestr(prefix + 'profile/save/slot', b'old save')
            z.writestr(prefix + 'nx2navi.ini', b'old settings')
            z.writestr(prefix + 'shader-cache/old.spv', b'old cache')

    def create(self, **kwargs):
        from launcher.game_patch import create

        return create(self.game, self.baseline, self.output, **kwargs)

    def test_changed_and_new_files_reproduce_the_update_without_personal_files(self):
        (self.game / 'ocarina.exe').write_bytes(b'new game')
        (self.game / 'libnew.dll').write_bytes(b'new dependency')
        (self.game / 'nx2navi.ini').write_bytes(b'private settings')
        result = self.create()
        self.assertEqual(result['changed'], 2)
        with zipfile.ZipFile(self.output) as z:
            self.assertEqual(set(z.namelist()), {'ocarina.exe', 'libnew.dll', 'PATCH-README.txt', 'PATCH-MANIFEST.json'})
            self.assertEqual(z.read('ocarina.exe'), b'new game')
            self.assertEqual(z.read('libnew.dll'), b'new dependency')
            self.assertEqual(json.loads(z.read('PATCH-MANIFEST.json'))['removed'], [])
            self.assertIsNone(z.testzip())

    def test_identical_content_is_omitted_even_when_timestamp_changed(self):
        path = self.game / 'ocarina.exe'
        data = path.read_bytes()
        path.write_bytes(data)
        self.assertEqual(self.create()['changed'], 0)

    def test_zip_with_single_top_level_folder_is_supported(self):
        self.write_baseline('Ocarina/')
        (self.game / 'ocarina.exe').write_bytes(b'new')
        self.assertEqual(self.create()['changed'], 1)
        with zipfile.ZipFile(self.output) as z:
            self.assertIn('ocarina.exe', z.namelist())

    def test_removed_runtime_files_are_reported_but_saves_and_caches_are_preserved(self):
        (self.game / 'old.dll').write_bytes(b'old runtime')
        self.write_baseline()
        (self.game / 'old.dll').unlink()
        result = self.create()
        self.assertEqual(result['removed'], 1)
        with zipfile.ZipFile(self.output) as z:
            manifest = json.loads(z.read('PATCH-MANIFEST.json'))
            self.assertEqual([r['path'] for r in manifest['removed']], ['old.dll'])
            self.assertIn('old.dll', z.read('PATCH-README.txt').decode())

    def test_cancel_preserves_existing_output_and_cleans_partial_files(self):
        self.output.write_bytes(b'previous patch')
        with self.assertRaises(net.Cancelled):
            self.create(cancel=lambda: True)
        self.assertEqual(self.output.read_bytes(), b'previous patch')
        self.assertEqual(list(self.root.glob('*.part')), [])

    def test_rejects_paths_that_would_escape_the_game_folder(self):
        with zipfile.ZipFile(self.baseline, 'a') as z:
            z.writestr('../outside.dll', b'bad')
        with self.assertRaises(ValueError):
            self.create()
        self.assertFalse(self.output.exists())

    def test_rejects_case_collisions_and_incomplete_base_archives(self):
        with zipfile.ZipFile(self.baseline, 'a') as z:
            z.writestr('OCARINA.EXE', b'other executable')
        with self.assertRaises(ValueError):
            self.create()
        with zipfile.ZipFile(self.baseline, 'w') as z:
            z.writestr('ocarina.exe', b'just a previous patch')
        with self.assertRaises(ValueError):
            self.create()


if __name__ == '__main__':
    unittest.main()
