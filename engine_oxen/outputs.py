"""Saving generated artifacts: naming, extension and an atomic write.

Split out of ``engine.py`` so the batch loop stays about control flow.

Two rules learned the hard way elsewhere in the fleet:

* the extension comes from the bytes that actually arrived, never from a
  guess — providers return JPEG for a model asked for PNG, and a file named
  from the request rather than the stream is a file no viewer opens;
* a file is written under a hidden ``.part`` name and renamed only once it is
  complete, so an interrupted download never leaves a half image that looks
  like a finished one.
"""

from __future__ import annotations

import os
import base64
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

DEFAULT_EXTENSIONS = {"image": ".png", "video": ".mp4", "text": ".txt", "audio": ".mp3"}

_MAGIC = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"ID3", ".mp3"),
    (b"OggS", ".ogg"),
    (b"fLaC", ".flac"),
    (b"\x1aE\xdf\xa3", ".webm"),
)


def clean_format(value) -> str:
    """A profile ``output_format`` reduced to a safe extension word ('' if odd)."""
    text = str(value or "").strip().lstrip(".").lower()
    return text if text and all(char.isalnum() for char in text) else ""


def default_extension(profile: dict, media_type: str) -> str:
    """Extension for a precomputed name (the error placeholder's path)."""
    output_format = clean_format((profile.get("parameters") or {}).get("output_format"))
    return f".{output_format}" if output_format else DEFAULT_EXTENSIONS.get(media_type, ".png")


def sniff_extension(data: bytes, media_type: str) -> str:
    """Real extension from the stream's magic bytes."""
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return ".wav"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[4:8] == b"ftyp":
        return ".mp4"
    if _looks_like_text(data):
        return ".txt"
    return DEFAULT_EXTENSIONS.get(media_type, ".png")


def _looks_like_text(data: bytes) -> bool:
    """True when the bytes decode as UTF-8 and carry no control noise."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return not any(ord(char) < 9 for char in text)


def output_name(stem: str, idx: int, suffix: str = "") -> str:
    """``<timestamp>-<stem>-<idx><suffix>`` — the fleet's sortable name."""
    timestamp = datetime.now().strftime("%y%m%d_%H%M%S")
    return f"{timestamp}-{stem}-{idx}{suffix}"


def save_results(
    client,
    dest_dir: Path,
    urls: list[str],
    media_type: str,
    stem: str,
    idx: int,
    *,
    on_download: Callable[[], None] | None = None,
    on_saved: Callable[[Path], None] | None = None,
) -> list[Path]:
    """Download every URL into ``dest_dir``; return the files written.

    Several results from one input get ``_0``, ``_1`` … so a six-image call
    never collides with itself.
    """
    if not urls:
        return []
    dest_dir.mkdir(parents=True, exist_ok=True)

    saved: list[Path] = []
    for index, url in enumerate(urls):
        if on_download:
            on_download()
        data = client.download(url)
        if not data:
            continue
        suffix = "" if len(urls) == 1 else f"_{index}"
        base = output_name(stem, idx, suffix)
        ext = sniff_extension(data, media_type)
        staged = dest_dir / f".{base}{ext}.part"
        final = dest_dir / f"{base}{ext}"
        with open(staged, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        staged.replace(final)
        if on_saved:
            on_saved(final)
        saved.append(final)
    return saved


def save_assets(
    client,
    dest_dir: Path,
    assets: list[dict],
    media_type: str,
    stem: str,
    idx: int,
    *,
    on_download: Callable[[], None] | None = None,
    on_saved: Callable[[Path], None] | None = None,
    decode=None,
) -> list[Path]:
    """Save assets that may be a URL, base64 text, or the bytes themselves.

    OpenAI returns images inline as ``b64_json`` and video as a separate content
    download, so one saver has to accept all three. A file is written under a
    hidden ``.part`` name and renamed only when complete.
    """
    if not assets:
        return []
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for index, asset in enumerate(assets):
        if on_download:
            on_download()
        if "bytes" in asset:
            data = asset["bytes"]
        elif "b64" in asset:
            data = decode(asset["b64"]) if decode else base64.b64decode(asset["b64"])
        else:
            data = client.download(str(asset.get("url") or ""))
        if not data:
            continue
        suffix = "" if len(assets) == 1 else f"_{index}"
        base = output_name(stem, idx, suffix)
        ext = sniff_extension(data, media_type)
        staged = dest_dir / f".{base}{ext}.part"
        final = dest_dir / f"{base}{ext}"
        with open(staged, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        staged.replace(final)
        if on_saved:
            on_saved(final)
        saved.append(final)
    return saved
