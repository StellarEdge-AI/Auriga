"""Explicit, post-episode RGB compaction. Original artifact APIs stay byte-exact."""
from collections import defaultdict
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile

import cv2
from PIL import Image


def frame_png(path, index, width, height):
    if type(index) is not int or not 0 <= index < 16:
        raise ValueError("invalid_archive_frame_index")
    reader = cv2.VideoCapture(str(path))
    try:
        # ponytail: at most 16 frames per segment; sequential decode avoids
        # imprecise keyframe seeks without a custom video index.
        for _ in range(index + 1):
            ok, frame = reader.read()
            if not ok:
                raise ValueError("image_archive_frame_missing")
        if frame.shape != (height, width, 3):
            raise ValueError("image_archive_dimensions_mismatch")
        output = io.BytesIO()
        Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).save(output, format="PNG")
        return output.getvalue()
    finally:
        reader.release()


def resolve_frame(store, source, mapping):
    if (mapping.get("source_sha256") != source["sha256"] or source["kind"] != "rgb"
            or source["access_scope"] != "actor"):
        raise ValueError("image_archive_source_mismatch")
    video, _ = store.resolve_artifact(mapping["video_ref"], allowed_scopes=("observer",))
    if video["kind"] != "observation_archive_video":
        raise ValueError("image_archive_video_kind_mismatch")
    _, path = store.artifact(mapping["video_ref"])
    data = frame_png(path, mapping["frame_index"], mapping["width"], mapping["height"])
    sha = hashlib.sha256(data).hexdigest()
    if sha != mapping["reconstructed_sha256"]:
        raise ValueError("image_archive_decode_digest_mismatch")
    return dict(source, mime_type="image/png", sha256=sha, byte_size=len(data),
                reconstruction=mapping), data


def compact_images(store):
    """Caller owns an ended run's exclusive lock. Publish mappings before pruning."""
    store._require_writer()
    groups, seen, siblings_by_key, captures = defaultdict(list), set(), defaultdict(list), defaultdict(list)
    for value in store.artifacts():
        siblings_by_key[value["storage_key"]].append(value)
    for capture in (store.root / "private/robot/observations").glob("raw-*/*"):
        if (capture.is_file() and not capture.is_symlink() and capture.resolve().is_relative_to(store.root)):
            captures[hashlib.sha256(capture.read_bytes()).hexdigest()].append(capture)
    for observation in store.records("observation"):
        for camera in observation["cameras"]:
            ref = camera["rgb_ref"]
            info, path = store.artifact(ref)
            key = info["storage_key"]
            if key in seen or info["kind"] != "rgb" or info["access_scope"] != "actor":
                continue
            seen.add(key)
            # Never remove shared content that also backs a non-RGB artifact.
            siblings = siblings_by_key[info["storage_key"]]
            if any(v["kind"] != "rgb" or v["access_scope"] != "actor" for v in siblings):
                continue
            mapping = store.read_state("image_archive", info["artifact_id"])
            if mapping:
                resolve_frame(store, info, mapping)
                for sibling in siblings:
                    store.set_state("image_archive", sibling["artifact_id"], mapping)
                continue
            store.resolve_artifact(ref, allowed_scopes=("actor",))
            groups[(camera["camera_id"], camera["width"], camera["height"])].append((ref, info, path))
    result = dict(schema_version="1", lossy=True, originals_removed=0, source_bytes=0,
                  archive_bytes=0, skipped_segments=0, segments=[], cleanup_errors=[])
    for (camera, width, height), images in groups.items():
        for offset in range(0, len(images), 16):
            entries = images[offset:offset + 16]
            with tempfile.TemporaryDirectory(prefix="image-archive-", dir=store.root) as directory:
                path = Path(directory) / "frames.mp4"
                log = Path(directory) / "ffmpeg.log"
                command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
                    "-f", "rawvideo", "-pixel_format", "rgb24", "-video_size", f"{width}x{height}",
                    "-framerate", "5", "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "fast",
                    "-crf", "18", "-threads", "1", "-pix_fmt", "yuv444p" if width % 2 or height % 2 else "yuv420p",
                    "-movflags", "+faststart", str(path)]
                with log.open("wb") as stderr, subprocess.Popen(command, stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL, stderr=stderr) as encoder:
                    for ref, _, _ in entries:
                        _, data = store.resolve_artifact(ref, allowed_scopes=("actor",))
                        with Image.open(io.BytesIO(data)) as image:
                            if image.size != (width, height):
                                raise ValueError("image_archive_source_dimensions_mismatch")
                            encoder.stdin.write(image.convert("RGB").tobytes())
                if encoder.returncode:
                    raise ValueError("image_archive_encoding_failed")
                size = sum(info["byte_size"] for _, info, _ in entries)
                if path.stat().st_size >= size:
                    result["skipped_segments"] += 1
                    continue
                reconstructed = [frame_png(path, i, width, height) for i in range(len(entries))]
                probe = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-count_frames",
                    "-select_streams", "v:0", "-show_entries", "stream=nb_read_frames", "-of", "json", str(path)]))
                if int(probe["streams"][0]["nb_read_frames"]) != len(entries):
                    raise ValueError("image_archive_frame_count_mismatch")
                video = store.put_artifact(path.read_bytes(), "observation_archive_video", "video/mp4", "observer")
                # All frames must decode before any original is removed.
                for index, ((ref, info, original), decoded) in enumerate(zip(entries, reconstructed)):
                    mapping = dict(video_ref=video, frame_index=index, camera_id=camera, width=width, height=height,
                        source_sha256=info["sha256"], reconstructed_sha256=hashlib.sha256(decoded).hexdigest(),
                        decoder=f"OpenCV {cv2.__version__}", lossy=True)
                    resolve_frame(store, info, mapping)
                    for sibling in siblings_by_key[info["storage_key"]]:
                        store.set_state("image_archive", sibling["artifact_id"], mapping)
    # Encoding and verification finish before pruning. Persisted mappings make a
    # retry after interruption reuse its videos and finish any remaining cleanup.
    segments = {}
    for key, siblings in siblings_by_key.items():
        mappings = [store.read_state("image_archive", info["artifact_id"]) for info in siblings]
        if not all(mappings) or any(v["kind"] != "rgb" or v["access_scope"] != "actor" for v in siblings):
            continue
        for info, mapping in zip(siblings, mappings):
            resolve_frame(store, info, mapping)
        info, mapping = siblings[0], mappings[0]
        original = store.blobs / key
        if original.exists():
            store.resolve_artifact(info["artifact_id"], allowed_scopes=("actor",))
        try:
            for capture in captures[info["sha256"]]:
                if capture.exists() and hashlib.sha256(capture.read_bytes()).hexdigest() == info["sha256"]:
                    capture.unlink()
            original.unlink(missing_ok=True)
        except OSError as error:
            result["cleanup_errors"].append(dict(artifact_id=info["artifact_id"], error=type(error).__name__))
        if not original.exists():
            result["originals_removed"] += 1
            result["source_bytes"] += info["byte_size"]
        video = mapping["video_ref"]
        if video not in segments:
            segments[video] = dict(video_ref=video, camera_id=mapping["camera_id"], frames=0,
                source_bytes=0, archive_bytes=store.artifact(video)[0]["byte_size"])
        segments[video]["frames"] = max(segments[video]["frames"], mapping["frame_index"] + 1)
        segments[video]["source_bytes"] += info["byte_size"]
    result["segments"] = list(segments.values())
    result["archive_bytes"] = sum(segment["archive_bytes"] for segment in result["segments"])
    store.set_state("runtime", "image_archive", result)
    return result
