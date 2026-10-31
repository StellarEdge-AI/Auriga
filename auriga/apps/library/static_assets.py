"""Resolve one web build in source checkouts and installed distributions."""
from pathlib import Path


def static_directory():
    package = Path(__file__).resolve().parents[2]
    source = package.parent / "web" / "dist"
    return source if (source / "index.html").is_file() else package / "web_assets"
