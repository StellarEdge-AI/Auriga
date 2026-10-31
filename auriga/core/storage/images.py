"""RGB encoding only: preserve resolution, camera order and pixel coordinates."""

import base64
import hashlib
import io
import os
from functools import lru_cache
from pathlib import Path
import uuid

from PIL import Image, features, __version__ as pillow_version


def image_settings(value=None):
    from ..config import Config
    if value is None:
        return Config.load().images.to_dict()
    return Config.from_dict({"images": value}).images.to_dict()


def store_image_settings(store, settings=None):
    saved = store.read_state("runtime", "image_settings")
    if saved is not None:
        value = image_settings(saved)
        # A pre-policy run must retain the image behavior it actually used.
        missing = {key: off for key, off in (("reuse_exact_images", False), ("trim_max_inline_images", 0))
                   if key not in saved}
        if missing:
            value.update(missing)
            if not store.read_only:
                store.set_state("runtime", "image_settings", value)
        return value
    value = image_settings(settings)
    if not store.read_only:
        store.set_state("runtime", "image_settings", value)
    return value


def encode_image(source, settings, *, storage=False):
    """Encode from RGB or encoded bytes. JPEG fallback never retries a robot action."""
    if isinstance(source, bytes):
        with Image.open(io.BytesIO(source)) as original:
            rgb = original.convert("RGB")
    else:
        rgb = source.convert("RGB")
    codec = "jpeg" if storage else settings["transport_format"]
    fallback = None
    buffer = io.BytesIO()
    if codec == "webp":
        try:
            if not features.check("webp"):
                raise OSError("webp_encoder_unavailable")
            rgb.save(buffer, format="WEBP", quality=settings["webp_quality"], method=settings["webp_method"])
        except (OSError, ValueError, KeyError) as error:
            codec, fallback, buffer = "jpeg", type(error).__name__, io.BytesIO()
    quality = settings["storage_jpeg_quality"] if storage else settings[codec + "_quality"]
    if codec == "jpeg":
        rgb.save(buffer, format="JPEG", quality=quality, subsampling=2, optimize=True)
    data = buffer.getvalue()
    metadata = dict(format=codec, quality=quality, mime_type="image/" + codec,
                    width=rgb.width, height=rgb.height, byte_size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(), encoder="Pillow", encoder_version=pillow_version,
                    codec_version=features.version("webp" if codec == "webp" else "jpg"))
    if codec == "webp":
        metadata["method"] = settings["webp_method"]
    else:
        metadata.update(subsampling="4:2:0", optimize=True)
    if fallback:
        metadata["fallback_reason"] = fallback
    return data, metadata


def _image_url(data, settings):
    encoded, metadata = encode_image(data, settings)
    return "data:" + metadata["mime_type"] + ";base64," + base64.b64encode(encoded).decode(), metadata


@lru_cache(maxsize=16)
def _cached_image_url(data, settings):
    return _image_url(data, dict(settings))


def image_url(data, settings):
    # Cache only ordinary camera frames; large crops bypass the bounded cache.
    with Image.open(io.BytesIO(data)) as image:
        cacheable = len(data) <= 1024 * 1024 and image.width * image.height <= 640 * 480
    url, metadata = (_cached_image_url(data, tuple(sorted(settings.items()))) if cacheable
                     else _image_url(data, settings))
    if metadata.get("fallback_reason"):
        _cached_image_url.cache_clear()  # An encoder recovery must be observable.
    return url, dict(metadata)


def camera_path(camera):
    # Old runs and external fixtures used this PNG-specific field.
    return Path(camera.get("rgb_path") or camera["png_path"])


def share_capture_file(store, path, reference):
    """Keep new immutable capture paths usable without a second physical copy."""
    path = Path(path)
    if (path.is_symlink() or not path.resolve().is_relative_to(store.root)
            or path.parent.parent.name != "observations" or not path.parent.name.startswith("raw-")):
        return False
    _, stored = store.artifact(reference)
    if path.read_bytes() != stored.read_bytes():
        return False
    temporary = path.with_name(".shared-" + uuid.uuid4().hex)
    try:
        os.link(stored, temporary)
        os.replace(temporary, path)
        return True
    except OSError:
        # Cross-device/restricted filesystems retain two valid JPEG copies.
        return False
    finally:
        temporary.unlink(missing_ok=True)
