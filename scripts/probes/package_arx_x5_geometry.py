"""Package complete X5A geometry without changing its geometric data.

Usage: python scripts/probes/package_arx_x5_geometry.py /path/to/Assets/Robots/x5
Uses only the Python standard library. The archive contains the original URDF
assembly and its referenced meshes, with documentary exporter metadata removed.
URDF numbers and paths, STL triangle records, GLB numeric JSON tokens and binary
buffers are preserved. No mesh import/export, coordinate conversion or topology
change occurs. Future assets containing unreviewed GLB extras fail closed.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import io
import json
from pathlib import Path, PurePosixPath
import re
import struct
import xml.etree.ElementTree as ET
import zipfile


MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
JSON_CHUNK = 0x4E4F534A
BIN_CHUNK = 0x004E4942
EXPECTED_MESHES = {
    'meshes/base_link.STL',
    *(f'meshes/link{i}.STL' for i in range(1, 9)),
    'meshes/camera.glb',
    'meshes/camera_base.glb',
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def mesh_references(urdf):
    root = ET.fromstring(urdf)
    require(len(root.findall('link')) == 11, 'Expected eleven robot links')
    names = {node.attrib['filename'] for node in root.findall('.//mesh')}
    require(names == EXPECTED_MESHES, 'Unexpected set of URDF mesh references')
    for name in names:
        path = PurePosixPath(name)
        require(not path.is_absolute() and '..' not in path.parts, 'Unsafe mesh path')
        require(str(path) == name, 'Mesh paths must retain their canonical relative form')
    return names


def glb_chunks(data):
    require(len(data) >= 20, 'Truncated GLB')
    magic, version, length = struct.unpack_from('<III', data)
    require((magic, version, length) == (0x46546C67, 2, len(data)), 'Invalid GLB 2 header')
    result, offset = [], 12
    while offset < len(data):
        require(offset + 8 <= len(data), 'Truncated GLB chunk header')
        size, kind = struct.unpack_from('<II', data, offset)
        start, end = offset + 8, offset + 8 + size
        require(size % 4 == 0 and end <= len(data), 'Invalid GLB chunk length')
        result.append((kind, start, end))
        offset = end
    require([kind for kind, _, _ in result] == [JSON_CHUNK, BIN_CHUNK],
            'Expected one embedded JSON chunk and one embedded binary chunk')
    return result


def object_members(text, start):
    """Return member spans without rewriting JSON or its numeric spellings."""
    decoder = json.JSONDecoder()

    def skip_space(position):
        while position < len(text) and text[position] in ' \t\r\n':
            position += 1
        return position

    require(text[start] == '{', 'Expected a JSON object')
    position, previous_comma, members = skip_space(start + 1), None, []
    while text[position] != '}':
        key_start = position
        key, position = decoder.raw_decode(text, position)
        require(isinstance(key, str), 'Invalid JSON member name')
        position = skip_space(position)
        require(text[position] == ':', 'Missing JSON colon')
        value_start = skip_space(position + 1)
        value, value_end = decoder.raw_decode(text, value_start)
        position = skip_space(value_end)
        following_comma = position if text[position] == ',' else None
        members.append((key, key_start, value_start, value_end, previous_comma, following_comma, value))
        if following_comma is None:
            require(text[position] == '}', 'Invalid JSON object separator')
            break
        previous_comma = following_comma
        position = skip_space(position + 1)
    return members


def inspect_glb(document):
    require(document['asset']['version'] == '2.0', 'Unexpected glTF version')
    require(len(document.get('buffers', [])) == 1, 'Expected one embedded buffer')
    require(all('uri' not in entry for category in ('buffers', 'images')
                for entry in document.get(category, [])), 'GLB contains an external dependency')
    require(not document.get('extensionsRequired'), 'Unreviewed required GLB extension')

    def visit(value):
        if isinstance(value, dict):
            require('extras' not in value, 'GLB extras require review before packaging')
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(document)


def clean_glb(data):
    chunks = glb_chunks(data)
    _, start, end = chunks[0]
    raw_json = data[start:end]
    text = raw_json.decode('utf-8')
    document = json.loads(text)
    inspect_glb(document)
    root_members = object_members(text, len(text) - len(text.lstrip()))
    asset_start = next(member[2] for member in root_members if member[0] == 'asset')
    result = bytearray(data)
    # Replace one member at a time with equal-length whitespace. This keeps the
    # entire GLB layout and every other byte intact, including number spellings.
    for key in ('generator', 'copyright'):
        current_text = bytes(result[start:end]).decode('utf-8')
        match = next((member for member in object_members(current_text, asset_start) if member[0] == key), None)
        if match is None:
            continue
        _, key_start, _, value_end, preceding_comma, following_comma, _ = match
        left, right = key_start, value_end
        if following_comma is not None:
            right = following_comma + 1
        elif preceding_comma is not None:
            left = preceding_comma
        byte_start = start + len(current_text[:left].encode('utf-8'))
        byte_end = start + len(current_text[:right].encode('utf-8'))
        result[byte_start:byte_end] = b' ' * (byte_end - byte_start)
    cleaned = bytes(result)
    expected = deepcopy(document)
    expected['asset'].pop('generator', None)
    expected['asset'].pop('copyright', None)
    require(json.loads(cleaned[start:end]) == expected, 'GLB metadata removal changed other JSON data')
    require(len(cleaned) == len(data), 'GLB layout changed')
    require(cleaned[end:] == data[end:], 'GLB binary chunk changed')
    return cleaned


def clean_member(name, data):
    if name == 'X5A.urdf':
        cleaned = re.sub(rb'<!--.*?-->', b'', data, flags=re.DOTALL)
        require(ET.tostring(ET.fromstring(cleaned)) == ET.tostring(ET.fromstring(data)),
                'URDF geometry or assembly changed')
        return cleaned
    if name.lower().endswith('.stl'):
        require(len(data) >= 84, 'Truncated STL')
        count = struct.unpack_from('<I', data, 80)[0]
        require(len(data) == 84 + count * 50, 'Expected binary STL triangle records')
        return b'\0' * 80 + data[80:]
    if name.lower().endswith('.glb'):
        return clean_glb(data)
    raise ValueError('Unexpected archive member')


def verify_archive(archive_bytes, asset):
    original_urdf = (asset / 'X5A.urdf').read_bytes()
    expected = {'X5A.urdf', *mesh_references(original_urdf)}
    require(len(archive_bytes) < MAX_ARCHIVE_BYTES, 'Archive exceeds broker size limit')
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        require(len(archive.infolist()) == 12 and set(archive.namelist()) == expected,
                'Archive must contain exactly the URDF and eleven meshes')
        require(archive.testzip() is None, 'Archive CRC verification failed')
        require(mesh_references(archive.read('X5A.urdf')) == expected - {'X5A.urdf'},
                'Packaged URDF references do not match archive members')
        for name in sorted(expected):
            original, packaged = (asset / name).read_bytes(), archive.read(name)
            require(packaged == clean_member(name, original), 'Unexpected archive bytes')
            if name.lower().endswith('.stl'):
                require(packaged[80:] == original[80:], 'STL triangle data changed')
            elif name.lower().endswith('.glb'):
                original_chunks, packaged_chunks = glb_chunks(original), glb_chunks(packaged)
                require(original_chunks == packaged_chunks, 'GLB chunk layout changed')
                for kind, start, end in original_chunks:
                    if kind == BIN_CHUNK:
                        require(original[start:end] == packaged[start:end], 'GLB binary buffer changed')
                _, start, end = packaged_chunks[0]
                inspect_glb(json.loads(packaged[start:end]))
    return len(expected)


def package(asset, output):
    original_urdf = (asset / 'X5A.urdf').read_bytes()
    names = ['X5A.urdf', *sorted(mesh_references(original_urdf))]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in names:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, clean_member(name, (asset / name).read_bytes()), compresslevel=9)
    archive_bytes = buffer.getvalue()
    count = verify_archive(archive_bytes, asset)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(archive_bytes)
    print(f'{output}: {len(archive_bytes)} bytes; {count} members; geometry preservation and dependency checks passed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('asset', type=Path)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[2] /
                        'auriga/robot_information_data/arx_x5/geometry_model.zip')
    args = parser.parse_args()
    package(args.asset, args.output)
