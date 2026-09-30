"""Path mapping and safe placement of the ebook next to the audiobook."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path, PurePosixPath

AUDIO_EXTS = {".m4b", ".m4a", ".mp3", ".aac", ".ogg", ".opus", ".flac", ".wav", ".wma", ".mp4"}
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class PlacementError(RuntimeError):
    pass


def map_path(container_path: str, mappings: list[tuple[str, str]]) -> str:
    """Translate a path as seen by another container into one this process can see.

    ``mappings`` must be sorted longest-prefix-first (Settings.path_mappings does this).
    Paths with no matching prefix are returned unchanged.
    """
    p = str(PurePosixPath(container_path))
    for src, dst in mappings:
        if p == src or p.startswith(src.rstrip("/") + "/"):
            rest = p[len(src):].lstrip("/")
            return str(PurePosixPath(dst) / rest) if rest else dst
    return p


def safe_filename(title: str, author: str = "", ext: str = "epub") -> str:
    base = title.strip() or "Unknown"
    if author.strip():
        base = f"{base} - {author.strip()}"
    base = _BAD_CHARS.sub("", base)
    base = re.sub(r"\s+", " ", base).strip().rstrip(". ")
    if len(base) > 180:
        base = base[:180].rstrip(". ")
    return f"{base or 'ebook'}.{ext.lstrip('.')}"


def _owner_reference(folder: Path) -> Path:
    for f in sorted(folder.iterdir()):
        if f.is_file() and f.suffix.lower() in AUDIO_EXTS:
            return f
    return folder


def match_ownership(target: Path, folder: Path) -> None:
    if not hasattr(os, "chown"):
        return
    ref = _owner_reference(folder).stat()
    try:
        os.chown(target, ref.st_uid, ref.st_gid)
        os.chmod(target, 0o664 if ref.st_mode & 0o020 else 0o644)
    except PermissionError:
        pass  # not root; leave as-is


def prepare_target(folder: str, filename: str) -> tuple[Path, Path]:
    """Return (final_path, temp_path) after validating the destination."""
    d = Path(folder)
    if not d.is_dir():
        raise PlacementError(f"Audiobook folder not found or not a directory: {d} (check PATH_MAP)")
    final = d / filename
    if final.exists():
        raise PlacementError(f"Refusing to overwrite existing file: {final}")
    return final, d / (filename + ".part")


def commit(temp: Path, final: Path) -> Path:
    """Atomically move the finished temp file into place and fix ownership."""
    if not temp.exists() or temp.stat().st_size == 0:
        temp.unlink(missing_ok=True)
        raise PlacementError("Downloaded file is empty")
    if final.exists():
        temp.unlink(missing_ok=True)
        raise PlacementError(f"Refusing to overwrite existing file: {final}")
    os.replace(temp, final)
    match_ownership(final, final.parent)
    return final


def copy_into(src: str, temp: Path) -> None:
    s = Path(src)
    if not s.is_file():
        raise PlacementError(f"Shelfmark file not found at {s}")
    shutil.copyfile(s, temp)
