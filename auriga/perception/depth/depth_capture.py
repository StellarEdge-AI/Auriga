"""Bounded validation of immutable metric depth artifacts; no simulator imports."""
import io
import zipfile

import numpy as np

from ...core.contracts import validate_depth_metadata


def validate_depth_archive(data, camera):
    """Return (depth, valid) after checking metadata, archive structure and sensor validity."""
    validate_depth_metadata(camera, "depth_path" if "depth_path" in camera else "depth_ref")
    shape = tuple(camera["depth_shape"])
    count = shape[0] * shape[1]
    expected = {"depth.npy": np.dtype("float32"), "valid.npy": np.dtype("bool")}
    # NPZ contains two small NPY headers plus exactly five bytes per pixel.
    if not isinstance(data, (bytes, bytearray)) or not 0 < len(data) <= count * 5 + 16384:
        raise ValueError("invalid_depth_archive_size")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = archive.infolist()
            if len(members) != 2 or {item.filename for item in members} != set(expected):
                raise ValueError("invalid_depth_archive_members")
            for item in members:
                dtype = expected[item.filename]
                if (item.flag_bits & 1 or item.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                        or not count * dtype.itemsize <= item.file_size <= count * dtype.itemsize + 4096):
                    raise ValueError("invalid_depth_archive_member_size")
                with archive.open(item) as stream:
                    version = np.lib.format.read_magic(stream)
                    if version == (1, 0):
                        actual_shape, _, actual_dtype = np.lib.format.read_array_header_1_0(stream, max_header_size=4096)
                    elif version == (2, 0):
                        actual_shape, _, actual_dtype = np.lib.format.read_array_header_2_0(stream, max_header_size=4096)
                    else:
                        raise ValueError("unsupported_depth_array_version")
                    if actual_shape != shape or actual_dtype != dtype:
                        raise ValueError("invalid_depth_array_shape_or_dtype")
                    if stream.tell() + count * dtype.itemsize != item.file_size:
                        raise ValueError("invalid_depth_array_length")
        with np.load(io.BytesIO(data), allow_pickle=False, max_header_size=4096) as archive:
            depth, valid = archive["depth"], archive["valid"]
    except (OSError, EOFError, TypeError, zipfile.BadZipFile) as error:
        raise ValueError("invalid_depth_archive") from error
    near, far = camera["depth_clip_m"]
    expected_valid = np.isfinite(depth) & (depth > 0) & (depth >= near) & (depth <= far)
    if not np.array_equal(valid, expected_valid):
        raise ValueError("depth_validity_mask_mismatch")
    return depth, valid
