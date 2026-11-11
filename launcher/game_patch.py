"""Create a portable update ZIP containing complete files changed since a full game export."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import zipfile

from . import game_export, net


def _path(name):
    name = name.replace('\\', '/')
    parts = PurePosixPath(name).parts
    if not parts or name.startswith('/') or any(p in ('..', '.') or ':' in p for p in parts):
        raise ValueError(f'the selected ZIP has an unsafe path: {name}')
    return '/'.join(parts)


def _members(archive):
    members = {}
    for info in archive.infolist():
        name = _path(info.filename)
        if info.is_dir():
            continue
        key = name.casefold()
        if key in members or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
            raise ValueError(f'the selected ZIP has a duplicate, linked or encrypted file: {name}')
        members[key] = info
    roots = [name[:-len('ocarina.exe')] for name in members
             if name == 'ocarina.exe' or name.endswith('/ocarina.exe')]
    if len(roots) != 1:
        raise ValueError('select a complete exported game ZIP containing one ocarina.exe')
    prefix = roots[0]
    files = {name[len(prefix):]: info for name, info in members.items() if name.startswith(prefix)}
    if any(name.casefold() not in files for name in game_export.REQUIRED):
        raise ValueError('select the complete original game ZIP, including its RomFS and ExeFS')
    return files


def _managed(name):
    parts = name.split('/')
    if len(parts) == 1:
        return name.endswith(('.exe', '.dll', '-notice.txt', '-license.txt')) or name == 'romfs-index.bin'
    return parts[0] in ('romfs', 'exefs', 'shared-fonts', 'romfs-patches') or name.startswith('mods/damaged file stand-ins/')


def _signature(path):
    value = path.stat()
    return value.st_size, value.st_mtime_ns, value.st_ino


def _hash(stream, cancel, advance):
    digest = hashlib.sha256()
    while block := stream.read(game_export.CHUNK):
        if cancel():
            raise net.Cancelled()
        digest.update(block)
        advance(len(block))
    return digest.hexdigest()


def create(game, baseline, output, *, progress=lambda fraction, detail: None, cancel=lambda: False):
    game, baseline, output = (Path(p).resolve() for p in (game, baseline, output))
    if output.suffix.lower() != '.zip' or output == baseline or output.is_relative_to(game):
        raise ValueError('choose a new patch ZIP outside the game folder and different from the original ZIP')
    if cancel():
        raise net.Cancelled()
    # Only portable runtime files participate. Personal settings and shader caches have their own lifetimes.
    files = game_export._files(game, minimal=True)
    original = {name: _signature(path) for name, path in files.items()}
    baseline_before = _signature(baseline)
    pending = None
    with zipfile.ZipFile(baseline) as archive:
        base = _members(archive)
        work = sum(value[0] for value in original.values()) + sum(
            base[name.casefold()].file_size for name in files if name.casefold() in base
            and base[name.casefold()].file_size == original[name][0])
        done = 0

        def advance(size):
            nonlocal done
            done += size
            progress(0.85 * done / max(work, 1), f'Comparing files. {done / 1e9:.1f} of {work / 1e9:.1f} GB read')

        changed = {}
        for name, path in sorted(files.items()):
            if cancel():
                raise net.Cancelled()
            with path.open('rb') as current:
                digest = _hash(current, cancel, advance)
            old = base.get(name.casefold())
            same = False
            if old and old.file_size == original[name][0]:
                with archive.open(old) as previous:
                    same = digest == _hash(previous, cancel, advance)
            if not same:
                changed[name] = digest

        current_names = {name.casefold() for name in files}
        removed = []
        for name, info in sorted(base.items()):
            if name not in current_names and _managed(name):
                with archive.open(info) as previous:
                    digest = _hash(previous, cancel, lambda size: None)
                removed.append({'path': name, 'sha256': digest})

        needed = sum(original[name][0] for name in changed)
        if shutil.disk_usage(output.parent).free < needed + 65536:
            raise ValueError(f'the patch needs up to {needed / 1e9:.1f} GB free at the destination')
        manifest = {'format': 1, 'baseline': baseline.name, 'files': changed, 'removed': removed}
        readme = ('Ocarina of Time game patch\n\n'
                  f'Created for the game extracted from {baseline.name}.\n'
                  'Close the game. Extract this ZIP into the folder containing ocarina.exe\n'
                  'and replace the existing files when asked.\n\n'
                  f'{len(changed)} new or changed files.\n')
        if removed:
            readme += '\nRemove these obsolete files from that same game folder after copying the update.\n'
            readme += '\n'.join(item['path'] for item in removed) + '\n'
        readme += '\nKeep this patch for personal use. It contains files derived from your game.\n'
        try:
            with tempfile.NamedTemporaryFile(dir=output.parent, prefix=output.name + '.', suffix='.part', delete=False) as target:
                pending = Path(target.name)
                with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as patch:
                    patch.writestr('PATCH-README.txt', readme)
                    patch.writestr('PATCH-MANIFEST.json', json.dumps(manifest, indent=2) + '\n')
                    written = 0
                    for name, expected in changed.items():
                        info = zipfile.ZipInfo.from_file(files[name], name, strict_timestamps=False)
                        info.compress_type = zipfile.ZIP_DEFLATED
                        digest = hashlib.sha256()
                        with files[name].open('rb') as source, patch.open(info, 'w') as destination:
                            while block := source.read(game_export.CHUNK):
                                if cancel():
                                    raise net.Cancelled()
                                destination.write(block)
                                digest.update(block)
                                written += len(block)
                                progress(0.85 + 0.15 * written / max(needed, 1), f'Writing patch. {written / 1e6:.0f} MB')
                        if digest.hexdigest() != expected:
                            raise ValueError(f'{name} changed during the export. Close the game and retry')
            if cancel():
                raise net.Cancelled()
            if any(_signature(files[name]) != value for name, value in original.items()):
                raise ValueError('the game files changed during the comparison. Retry after the update finishes')
            if _signature(baseline) != baseline_before:
                raise ValueError('the original ZIP changed during the comparison. Retry when it finishes copying')
            os.replace(pending, output)
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)
    progress(1.0, f'{len(changed)} files, {output.stat().st_size / 1e6:.1f} MB')
    return {'path': output, 'baseline': baseline, 'changed': len(changed), 'removed': len(removed),
            'size': output.stat().st_size}
