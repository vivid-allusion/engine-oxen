"""The endpoint TOML catalogue, read at run time.

One file per model, under ``endpoints/<category>-Models/``. Adding a model is a
data change, never a code change. The same files are the wizard's model list,
so a catalogue entry that lies to the Engine also lies to the user.

Two readers live here: the TOMLs, and ``status.json`` beside them — how much
real use each endpoint has actually seen (written by
``engine-mgmt/tools/engine_status.py``). Status is evidence, never opinion:
``experimental`` means written from the provider's schema and never called,
``beta`` means a call has worked, ``proven`` means a track record.

Standard library only. A missing or broken TOML is skipped, never raised: the
Engine falls back to an inferred spec so a hand-written profile still runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

ENDPOINTS_DIR = Path(__file__).parent / "endpoints"

#: Directory per category. IMG and VID are in scope; TXT and Vision exist so a
#: provider's other models can be recorded and skipped, never carried.
CATEGORY_DIRS = {
    "IMG": "IMG-Models",
    "VID": "VID-Models",
    "LLM": "LLM-Models",
    "AUD": "AUD-Models",
}

#: What each family produces. They coincide for three of the four; an LLM reads
#: images and still writes text.
CATEGORY_KINDS = {"IMG": "image", "VID": "video", "LLM": "text", "AUD": "audio"}

#: Profile keys that are generator plumbing, never provider inputs.
NON_PAYLOAD_KEYS = frozenset(
    {
        "prompt_prefix",
        "prompt_suffix",
        "kind",
        "api_path",
        "mode",
        "content_path",
        "concurrency",
        "media_type",
        "platform",
        "endpoint",
        "delay_between_requests",
        "circuit_breaker_max",
        "retry_on_429",
        "retry_base_delay",
        "poll_interval",
        "timeout_seconds",
        "http_timeout",
        "base_url",
        "api_base_url",
        "save_payloads",
        "cost_estimation",
        "force_png",
    }
)

#: The provider's prompt field, on every model that takes one.
PROMPT_KEY = "prompt"

STATUS_VALUES = ("experimental", "beta", "proven")


@dataclass
class EndpointSpec:
    """Everything the payload builder needs to know about one model."""

    id: str
    model: str
    category: str = ""
    kind: str = "video"
    label: str = ""
    description: str = ""
    api_path: str = ""
    payload_style: str = "flat"
    mode: str = "sync"
    content_path: str = ""
    image_url_param: str = ""
    slots: tuple[str, ...] = ()
    required_slots: tuple[str, ...] = ()
    media_lists: tuple[str, ...] = ()
    duration_param_name: str = "duration_seconds"
    prompt_mode: str = "required"
    draft_mode: bool = False
    media_hint: str = ""
    known: bool = True
    params: dict = field(default_factory=dict)
    status: str = "experimental"

    @property
    def prompt_required(self) -> bool:
        return self.prompt_mode == "required"

    @property
    def prompt_never(self) -> bool:
        return self.prompt_mode == "none"

    def array_media(self, key: str) -> bool:
        return key in self.media_lists

    def param_type(self, key: str) -> str:
        param = self.params.get(key)
        return str(param.get("type") or "") if isinstance(param, dict) else ""

    def is_list_param(self, key: str) -> bool:
        param = self.params.get(key)
        return bool(isinstance(param, dict) and param.get("list"))

    def declares(self, key: str) -> bool:
        """True when the TOML declares this key anywhere the Engine reads."""
        return (
            key in self.params
            or key == PROMPT_KEY
            or key == self.image_url_param
            or key in self.slots
        )


def infer_kind(model: str) -> str:
    """Best-effort kind for a model the catalogue does not carry."""
    mid = (model or "").lower()
    if any(token in mid for token in ("tts", "speech", "voice", "audio", "music", "sound")):
        return "audio"
    if any(token in mid for token in ("chat", "completion", "llm", "reason")):
        return "text"
    if any(token in mid for token in ("image", "upscale", "erase", "background", "edit")):
        return "image"
    if any(token in mid for token in ("video", "veo", "sora", "kling", "seedance")):
        return "video"
    return "video"


def fallback_spec(model: str, category: str = "") -> EndpointSpec:
    """A spec for a model the catalogue does not know.

    Unknown models keep the profile's word: nothing is dropped and no key is
    guessed, so a hand-written profile that knows the schema still runs.
    """
    return EndpointSpec(
        id=model,
        model=model,
        category=category,
        kind=infer_kind(model),
        label=model,
        image_url_param="image_url",
        prompt_mode="optional",
        known=False,
    )


class Catalog:
    """Model id → :class:`EndpointSpec`, loaded once."""

    def __init__(self, specs: dict[str, EndpointSpec] | None = None):
        self._specs = specs or {}

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, model: object) -> bool:
        return model in self._specs

    def get(self, model: str) -> EndpointSpec:
        spec = self._specs.get(model)
        return spec if spec is not None else fallback_spec(model)

    def models(self) -> list[str]:
        return sorted(self._specs)

    def specs(self) -> list[EndpointSpec]:
        return [self._specs[key] for key in sorted(self._specs)]

    def status(self, model: str) -> str:
        spec = self._specs.get(model)
        return spec.status if spec is not None else "experimental"


def _load_status(root: Path) -> dict:
    """Endpoint → status, from ``status.json`` beside the endpoint folders."""
    path = root.parent / "status.json"
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
    except Exception:
        return {}
    endpoints = raw.get("endpoints") if isinstance(raw, dict) else None
    if not isinstance(endpoints, dict):
        return {}
    out = {}
    for model, entry in endpoints.items():
        value = entry.get("status") if isinstance(entry, dict) else entry
        if isinstance(value, str) and value in STATUS_VALUES:
            out[str(model)] = value
    return out


def load_catalog(endpoints_dir: Path | None = None) -> Catalog:
    """Read every endpoint TOML under ``endpoints/``. Never raises."""
    if tomllib is None:  # pragma: no cover - Python 3.10 without tomli
        return Catalog()
    root = Path(endpoints_dir) if endpoints_dir else ENDPOINTS_DIR
    statuses = _load_status(root)
    specs: dict[str, EndpointSpec] = {}
    for category, dir_name in CATEGORY_DIRS.items():
        directory = root / dir_name
        if not directory.is_dir():
            continue
        for toml_file in sorted(directory.glob("*.toml")):
            spec = _read_spec(toml_file, category)
            if spec is not None and spec.model:
                spec.status = statuses.get(spec.model, spec.status)
                specs.setdefault(spec.model, spec)
    return Catalog(specs)


def _read_spec(path: Path, category: str) -> EndpointSpec | None:
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except Exception:
        return None
    if not isinstance(data, dict) or not data.get("id"):
        return None
    general = data.get("general") if isinstance(data.get("general"), dict) else {}
    model = str(data.get("endpoint") or data["id"])
    params = data.get("params") if isinstance(data.get("params"), dict) else {}
    return EndpointSpec(
        id=str(data["id"]),
        model=model,
        category=str(data.get("category") or category),
        kind=str(general.get("kind") or infer_kind(model)),
        label=str(data.get("label") or data["id"]),
        description=str(data.get("description") or ""),
        api_path=str(data.get("api_path") or ""),
        payload_style=str(data.get("payload_style") or "flat"),
        mode=str(data.get("mode") or "sync"),
        content_path=str(general.get("content_path") or data.get("content_path") or ""),
        image_url_param=str(general.get("image_url_param") or ""),
        slots=tuple(str(s) for s in general.get("slots") or ()),
        required_slots=tuple(str(s) for s in general.get("required_slots") or ()),
        media_lists=tuple(str(s) for s in general.get("media_lists") or ()),
        duration_param_name=str(general.get("duration_param_name") or "duration_seconds"),
        prompt_mode=str(general.get("prompt") or "required"),
        draft_mode=bool(general.get("draft_mode", False)),
        media_hint=str(general.get("media_hint") or ""),
        params=params,
    )
