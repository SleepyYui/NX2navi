"""Identify portable game versions, including executables exported before manifests existed."""
import json
from pathlib import Path
import struct

from . import build, versions

MANIFEST = 'GAME-MANIFEST.json'


def clean(value):
    versions.parse(value)
    return versions.display(value).removeprefix('v')


def metadata(version):
    return json.dumps({'format': 1, 'version': version}, indent=2) + '\n'


def read_manifest(stream):
    try:
        value = json.loads(stream.read(4096))
        return clean(value['version']) if value.get('format') == 1 else ''
    except (ValueError, KeyError, TypeError, AttributeError):
        return ''


def executable(stream):
    """Read the compiled rt_port_version array through the PE COFF symbol table, when present."""
    try:
        def read(offset, count):
            stream.seek(offset)
            data = stream.read(count)
            if len(data) != count:
                raise ValueError('truncated executable')
            return data

        if read(0, 2) != b'MZ':
            return ''
        pe = struct.unpack('<I', read(60, 4))[0]
        if read(pe, 4) != b'PE\0\0':
            return ''
        _, sections, _, symbols, count, optional, _ = struct.unpack('<HHIIIHH', read(pe + 4, 20))
        if not symbols or not 0 < sections <= 96 or not 0 < count <= 2_000_000:
            return ''
        headers = read(pe + 24 + optional, sections * 40)
        table = read(symbols, count * 18)
        strings_at = symbols + count * 18
        size = struct.unpack('<I', read(strings_at, 4))[0]
        if not 4 <= size <= 64 * 1024 * 1024:
            return ''
        strings = read(strings_at, size)
        index = 0
        while index < count:
            row = table[index * 18:(index + 1) * 18]
            if row[:4] == b'\0' * 4:
                offset = struct.unpack_from('<I', row, 4)[0]
                name = strings[offset:offset + 64].split(b'\0', 1)[0] if 4 <= offset < size else b''
            else:
                name = row[:8].split(b'\0', 1)[0]
            if name in (b'rt_port_version', b'_rt_port_version'):
                value, section = struct.unpack_from('<Ih', row, 8)
                if not 1 <= section <= sections:
                    return ''
                raw_size, raw_at = struct.unpack_from('<II', headers, (section - 1) * 40 + 16)
                if value >= raw_size:
                    return ''
                data = read(raw_at + value, min(128, raw_size - value))
                return clean(data.split(b'\0', 1)[0].decode('ascii'))
            index += 1 + row[17]
    except (OSError, ValueError, struct.error):
        pass
    return ''


def installed(game):
    game = Path(game)
    version = build.built_version(game)
    if version:
        return clean(version)
    try:
        with (game / MANIFEST).open('rb') as source:
            version = read_manifest(source)
        if version:
            return version
    except OSError:
        pass
    with (game / 'ocarina.exe').open('rb') as source:
        return executable(source)
