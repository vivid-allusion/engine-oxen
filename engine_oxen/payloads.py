"""Request-body construction — pure functions, no I/O.

Split out of ``engine.py`` so the mapping from (profile, catalogue entry, input
file) to a provider request is testable on its own.

The rules, all of them learned from a provider rejecting a payload:

* a profile parameter the endpoint does not declare is dropped **with a visible
  warning** — the provider's per-field message is easy to miss in a batch;
* an unset value (``""``, nothing, an empty list) is omitted, never sent: an
  empty ``negative_prompt`` is a different request from no negative prompt;
* media goes to the key the catalogue declares, and a model that declares no
  media input **refuses** media loudly rather than guessing a key;
* a key that accepts one URL and is handed two refuses loudly rather than
  dropping one.
"""

from __future__ import annotations

from typing import Any

from .catalog import NON_PAYLOAD_KEYS, PROMPT_KEY, EndpointSpec
from .datatypes import EngineError, InputFile

_TRUE = {"true", "yes", "1", "on"}
_FALSE = {"false", "no", "0", "off"}


def compose_prompt(profile: dict, item: InputFile) -> str:
    """``prompt_prefix`` + the file's prompt + ``prompt_suffix``, trimmed."""
    prefix = profile.get("prompt_prefix", "") or ""
    suffix = profile.get("prompt_suffix", "") or ""
    return f"{prefix}{item.prompt}{suffix}".strip()


def primary_key(profile: dict, spec: EndpointSpec) -> str:
    """Primary media key: the profile's, then the catalogue's, then a default."""
    for value in (profile.get("image_url_param"), spec.image_url_param):
        if value:
            return str(value)
    return "image_url"


def declared_media_key(profile: dict, spec: EndpointSpec) -> str:
    """The media key the catalogue declares — empty when the model takes none.

    Deliberately separate from :func:`primary_key`: the profile may carry a
    default the model does not accept, and "does this model take media at all"
    must be answered by the catalogue, not by a fallback.
    """
    return str(profile.get("image_url_param") or spec.image_url_param or "")


def build_input(
    profile: dict,
    spec: EndpointSpec,
    params: dict,
    prompt: str,
    item: InputFile,
) -> tuple[dict, list[str]]:
    """Build the provider's request body for one input file.

    Returns the body and any advisory warnings. Raises ``EngineError`` only for
    a request that cannot be made honestly (media where the model takes none,
    two URLs where one is allowed).
    """
    warnings: list[str] = []
    payload: dict[str, Any] = {}

    for key, value in params.items():
        if key in NON_PAYLOAD_KEYS or key == PROMPT_KEY:
            continue
        if is_unset(value):
            continue
        if spec.known and not spec.declares(key):
            warnings.append(
                f"{spec.model} does not declare '{key}' — dropped "
                f"(the provider would reject it)"
            )
            continue
        payload[key] = encode_param(key, value, spec)

    if prompt and not spec.prompt_never:
        payload[PROMPT_KEY] = prompt

    _add_media(payload, profile, spec, item)
    _add_duration(payload, profile, spec, item, warnings)
    return payload, warnings


def is_unset(value: Any) -> bool:
    """True for a value that means "not set" rather than a real input."""
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if isinstance(value, (list, dict, tuple)) and not value:
        return True
    return False


def encode_param(key: str, value: Any, spec: EndpointSpec) -> Any:
    """Coerce one scalar parameter to the type its catalogue entry declares."""
    if spec.is_list_param(key) and isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    param_type = spec.param_type(key)
    if param_type == "integer":
        return _to_int(value)
    if param_type == "number":
        return _to_float(value)
    if param_type == "boolean":
        return _to_bool(value)
    if param_type == "text" and not isinstance(value, str):
        return str(value)
    return value


def _add_media(payload: dict, profile: dict, spec: EndpointSpec, item: InputFile) -> None:
    declared = declared_media_key(profile, spec)
    if item.reference_urls:
        if spec.known and not declared:
            raise EngineError(
                f"{spec.model} takes no media input, but {item.path.name} carries "
                f"{len(item.reference_urls)} URL(s) — use a model that edits, "
                f"animates or continues them, or move them to that model's profile."
            )
        key = declared or primary_key(profile, spec)
        payload[key] = shape_media(key, item.reference_urls, spec)

    for slot, urls in (item.references or {}).items():
        if urls:
            payload[slot] = shape_media(slot, urls, spec)

    for slot in spec.required_slots:
        if not payload.get(slot):
            raise EngineError(
                f"{spec.model} requires the '{slot}' input, but {item.path.name} "
                f"does not provide it"
            )


def _add_duration(
    payload: dict, profile: dict, spec: EndpointSpec, item: InputFile, warnings: list[str]
) -> None:
    duration = item.metadata.get("duration")
    if duration is None:
        return
    key = str(profile.get("duration_param_name") or spec.duration_param_name or "duration_seconds")
    if spec.known and not spec.declares(key):
        warnings.append(
            f"{spec.model} takes no '{key}' input — the output keeps the source length"
        )
        return
    payload[key] = encode_param(key, duration, spec)


def shape_media(key: str, urls: list[str], spec: EndpointSpec) -> str | list[str]:
    """Shape URLs for one media key.

    Array keys take the whole list. A single-URL key takes one and refuses to
    silently drop the rest — the provider can only accept one, so the media
    must never be discarded quietly.
    """
    if not urls:
        return []
    if spec.array_media(key):
        return list(urls)
    if len(urls) > 1:
        raise EngineError(
            f"Param '{key}' accepts a single media URL but {len(urls)} were "
            f"provided — fix the markdown: {urls}"
        )
    return urls[0]


def _to_int(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return value


def _to_float(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return value


def _to_bool(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    token = str(value).strip().lower()
    if token in _TRUE:
        return True
    if token in _FALSE:
        return False
    return value
