"""Export a built Windows game as a private, portable ZIP64 archive."""
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

from . import build, gameini, net

REQUIRED = ('ocarina.exe', 'SDL3.dll', 'libopus-0.dll', 'nx2navi_xlate.exe', 'romfs-index.bin', 'romfs/romfs.bin',
            'exefs/main.npdm', 'exefs/main', 'exefs/rtld', 'exefs/sdk', 'exefs/subsdk0')
CACHE_FILES = ('shader-answers.bin', 'shader-programs.bin', 'shader-prepared.bin', 'seed-answers.bin',
               'seed-programs.bin', 'compute-used.bin')
OPTIONAL_GRAPHICS = {'amd_fidelityfx_vk.dll', 'nvngx_dlss.dll', 'nx2navi_dlssg.dll', 'nvngx_dlssg.dll',
                     'nvngx_dlss-notice.txt', 'dlssg-notice.txt', 'nvidia-rtx-sdks-license.txt',
                     'streamline-license.txt'}
CHUNK = 8 * 1024 * 1024


def _linked(path):
    return path.is_symlink() or path.is_junction()


def _collect(root, files, folders):
    def linked(path):
        return any(_linked(p) for p in (path, *path.parents) if p != root and p.is_relative_to(root))

    candidates = {root / name for name in files}
    for folder in folders:
        tree = root / folder
        if linked(tree):
            continue
        for directory, dirs, names in os.walk(tree):
            base = Path(directory)
            dirs[:] = [d for d in dirs if not _linked(base / d)]
            candidates.update(base / name for name in names)
    return {p.relative_to(root).as_posix(): p for p in candidates
            if not p.name.lower().endswith(('.keys', '.keys.txt', '.tmp', '.part')) and not linked(p) and p.is_file()}


def _files(game, minimal=False):
    names = [*REQUIRED, 'nx2navi_xlate.id']
    folders = ['exefs', 'shared-fonts']
    if minimal:
        folders += ['romfs-patches', 'mods/Damaged file stand-ins']
    else:
        names.extend(CACHE_FILES)
        folders += ['shader-cache', 'mods', *(p.name for p in game.glob('romfs-patches*'))]
    for path in game.iterdir():
        name = path.name.lower()
        if minimal and (name in OPTIONAL_GRAPHICS or name.startswith('sl.')):
            continue
        if (name.endswith('.dll') and name != 'vulkan-1.dll') or name.endswith(('-notice.txt', '-license.txt')):
            names.append(path.name)
    files = _collect(game, names, folders)
    missing = set(REQUIRED) - files.keys()
    if missing:
        raise ValueError('the game folder is incomplete. Missing ' + ', '.join(sorted(missing)))
    if minimal:
        return files

    # The runtime resolves profile_dir relative to the executable (runtime/main.c portable_paths).
    override = os.environ.get('NX2NAVI_PROFILE_DIR') or gameini.get(game, 'PROFILE_DIR')
    profile = (game / (override or 'profile')).resolve()
    recovery = [f'save-tx/{p.name}' for p in (profile / 'save-tx').glob('ready-*')
                if p.name.removeprefix('ready-').isdigit()]
    saves = _collect(profile, ('account.uid',), ('save', 'save-import', 'save-tx/new', 'save-tx/old', *recovery))
    files.update({f'profile/{name}': path for name, path in saves.items()})
    return files


def _readme(game):
    try:
        level = json.loads((game / 'build-cache/obj/lifted-build.json').read_text(encoding='utf-8'))['level']
    except (OSError, ValueError, KeyError, TypeError):
        level = None
    cpu = {0: 'generic x86-64', 2: 'x86-64-v2 (SSE4.2)', 3: 'x86-64-v3 (AVX2 and FMA)'}.get(
        level if isinstance(level, int) else None)
    return (
        'Ocarina of Time portable game\n\n'
        'Extract the entire ZIP with a ZIP64-capable extractor such as 7-Zip.\n'
        'Windows: launch ocarina.exe.\n'
        'Linux: run wine ocarina.exe from the extracted folder, or add ocarina.exe\n'
        'to Steam as a non-Steam game and enable Proton.\n'
        'Steam Frame: upload the extracted folder with the SteamOS Devkit Client,\n'
        'using Steam Play (Proton) for Windows and ocarina.exe as the launch command.\n\n'
        + (f'CPU target: {cpu}.\n' if cpu else 'The CPU target is inherited from the installed build.\n')
        + 'The destination needs a compatible Vulkan 1.3 driver with\n'
        'VK_EXT_external_memory_host and VK_EXT_depth_clip_control.\n'
        'Press F1 in the game to open settings, then choose Controls.\n\n'
        'This archive contains files derived from your own game. Keep it for\n'
        'personal use and do not publish or share it.\n'
    )


def export(game, out, *, minimal=False, progress=lambda done, total: None, cancel=lambda: False):
    """Stream a snapshot to a temporary ZIP and publish it after the copy finishes."""
    game, out = Path(game).resolve(), Path(out).resolve()
    if out.suffix.lower() != '.zip' or out.is_relative_to(game):
        raise ValueError('choose a ZIP file outside the installed game folder')
    if build.game_running(game, clear_readonly=False):
        raise ValueError('close the game before exporting and make sure ocarina.exe is writable')
    if not minimal and _linked(game / 'nx2navi.ini'):
        raise ValueError('replace the nx2navi.ini link with a regular copy before exporting')
    files = _files(game, minimal=minimal)
    total = sum(p.stat().st_size for p in files.values())
    if shutil.disk_usage(out.parent).free < total:
        raise ValueError(f'the export needs at least {total / 1e9:.1f} GB of free space')

    ini = '' if minimal else gameini.portable_text(game)
    tmp = None
    done = 0
    try:
        with tempfile.NamedTemporaryFile(dir=out.parent, prefix=out.name + '.', suffix='.part', delete=False) as f:
            tmp = Path(f.name)
            with zipfile.ZipFile(f, 'w') as archive:
                archive.writestr('README.txt', _readme(game))
                archive.mkdir('profile')
                if ini:
                    archive.writestr('nx2navi.ini', ini.encode('utf-8', 'surrogateescape'))
                for name, path in sorted(files.items()):
                    if cancel():
                        raise net.Cancelled()
                    info = zipfile.ZipInfo.from_file(path, name, strict_timestamps=False)
                    with path.open('rb') as src, archive.open(info, 'w') as dst:
                        while chunk := src.read(CHUNK):
                            if cancel():
                                raise net.Cancelled()
                            dst.write(chunk)
                            done += len(chunk)
                            progress(done, total)
        if cancel():
            raise net.Cancelled()
        if build.game_running(game, clear_readonly=False):
            raise ValueError('the game folder is in use. Close the game and try again')
        os.replace(tmp, out)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    return out
