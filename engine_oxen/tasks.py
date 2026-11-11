"""Oxen's two shapes: images answer inline, a render is a generation you poll.

Oxen is a gateway: one host serves every model, so the difference between models
is data (the catalogue) and the only structural difference is synchronous versus
queued. Both live here, next to the provider's own status vocabulary.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .transport import ProviderError

RequestFn = Callable[..., dict]
EmitFn = Callable[[str], None]

LIVE_STATES = frozenset({"queued", "processing", "starting", "in_progress", "running"})
SUCCESS_STATES = frozenset({"succeeded", "completed", "success"})


def is_terminal(status: object) -> bool:
    return str(status or "") not in LIVE_STATES


def call_sync(request: RequestFn, path: str, body: dict, *,
              emit: EmitFn = lambda _msg: None, prefix: str = "") -> dict:
    """One request that is also the answer — ``/ai/images/generate``."""
    record = request("POST", path, body)
    if not isinstance(record, dict):
        raise ProviderError(200, "Oxen returned an unreadable response")
    emit(f"{prefix} ✅ Response received")
    return record


def submit_async(request: RequestFn, path: str, body: dict, *,
                 emit: EmitFn = lambda _msg: None, prefix: str = "") -> dict:
    """Enqueue a generation.

    Oxen answers ``{"generations": [{"generation_id": ..., "status": ...}]}``;
    the engine speaks one job at a time, so this returns the first as
    ``{"id": ..., "status": ...}``.
    """
    record = request("POST", path, body)
    generations = record.get("generations") if isinstance(record, dict) else None
    if not isinstance(generations, list) or not generations:
        raise ProviderError(200, "Oxen accepted the request but returned no generation id")
    first = generations[0] if isinstance(generations[0], dict) else {}
    generation_id = str(first.get("generation_id") or first.get("id") or "")
    if not generation_id:
        raise ProviderError(200, "Oxen accepted the request but returned no generation id")
    emit(f"{prefix} 🧾 Generation {generation_id} queued")
    return {"id": generation_id, "status": first.get("status") or "queued"}


def await_async(request: RequestFn, job: dict, *, status_path: str, poll_interval: float,
                timeout: float, emit: EmitFn = lambda _msg: None, prefix: str = "") -> dict:
    job_id = str(job.get("id") or "")
    started = time.monotonic()
    last = str(job.get("status") or "")
    path = status_path.format(id=job_id)
    while True:
        if timeout and time.monotonic() - started > timeout:
            raise ProviderError(
                0,
                f"Generation {job_id} did not finish within {timeout:.0f}s — it keeps "
                f"running at Oxen; collect it later with GET /api/ai/queue/{job_id}",
            )
        if poll_interval:
            time.sleep(poll_interval)
        record = request("GET", path)
        status = str(record.get("status") or "")
        if status != last:
            last = status
            emit(f"{prefix} ⏳ Generation {status}")
        if not is_terminal(status):
            continue
        if status not in SUCCESS_STATES:
            raise ProviderError(200, _failure(record, job_id))
        emit(f"{prefix} ✅ Generation succeeded")
        return record


def assets_from(record: dict) -> list[dict]:
    """Normalise a finished response into ``{"url": ...}`` or ``{"b64": ...}``.

    The shapes Oxen documents: ``images[]`` and ``videos[]`` on the synchronous
    endpoints, ``result_url`` on a queued generation. Anything else is empty and
    the caller reports "no output returned" rather than pretending.
    """
    assets: list[dict] = []
    for key in ("images", "videos", "audio", "audios"):
        items = record.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("b64_json"):
                assets.append({"b64": str(item["b64_json"])})
            elif item.get("url"):
                assets.append({"url": str(item["url"])})
    # result_url repeats videos[0].url, so it only counts when nothing else was listed.
    if not assets and record.get("result_url"):
        assets.append({"url": str(record["result_url"])})
    return assets


def _failure(record: dict, job_id: str) -> str:
    detail = str(record.get("error_message") or "")
    if not detail:
        error = record.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message") or error.get("title") or "")
        elif error:
            detail = str(error)
    text = f"Generation {job_id} ended as {record.get('status') or 'failed'}"
    return f"{text} — {detail}" if detail else text
