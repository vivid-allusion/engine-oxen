"""The public door of an engine.

The TUI imports ``list_standby_profiles`` from here; the Generator imports
``Engine`` and the four contract types. Nothing else is public.
"""

from pathlib import Path

from .datatypes import EngineError, InputFile, OutputFile, ProgressEvent
from .engine import Engine

_STANDBY_DIR = Path(__file__).parent / "profiles" / "standby"


def list_standby_profiles(media_type: str | None = None) -> list[Path]:
    """Return sorted standby YAML paths, optionally filtered by media type.

    Standby profiles live in category subdirectories mirroring the endpoint
    catalogue: ``profiles/standby/IMG/`` and ``profiles/standby/VID/``. The
    loading Generator declares its media type and the engine seeds only the
    matching shelf.

    Args:
        media_type: ``'IMG'`` or ``'VID'`` (case-insensitive). ``None``
            returns every standby profile.

    Returns:
        Sorted list of standby YAML paths; empty for a category the engine
        does not carry.
    """
    if not _STANDBY_DIR.is_dir():
        return []
    if media_type:
        source = _STANDBY_DIR / str(media_type).upper()
        if not source.is_dir():
            return []
        return sorted(source.glob("*.yaml"))
    files = sorted(_STANDBY_DIR.glob("*.yaml"))
    for sub in sorted(_STANDBY_DIR.iterdir()):
        if sub.is_dir():
            files.extend(sorted(sub.glob("*.yaml")))
    return files


__all__ = [
    "Engine",
    "InputFile",
    "OutputFile",
    "ProgressEvent",
    "EngineError",
    "list_standby_profiles",
]
