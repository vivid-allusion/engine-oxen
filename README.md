# engine-oxen

Engine wrapper for **Oxen.ai** — a gateway that serves image, video, audio and
language models from many labs behind one API.

**8 endpoints, from Oxen's own documentation (captured 2026-11-11):** 4 image
and 4 video. Every entry names a model whose reference page is in the
`engine-oxen-docs` capture, so an entry can always be traced back to a document
rather than a memory.

Images: `black-forest-labs-flux-2-klein-4b`, `flux-2-pro`,
`bytedance-seedream-5-pro`, `qwen-image-3`.

Video: `bytedance-seedance-2-5-text-to-video`,
`kling-video-v2-6-pro-text-to-video`, `google-veo-3`,
`wan-v2-7-text-to-video`.

Every endpoint is `experimental` until a real call proves it.

## Setup

```
export OXEN_API_KEY=...
```

Base URL: `https://hub.oxen.ai` (their documented inference host). The API
prefix `api/ai/` is part of each catalogue entry's `api_path`, so the base
stays the host and nothing is guessed.

## Two shapes, one interface

- **Images answer inline.** `POST /api/ai/images/generate` returns `images[]`
  with a URL or base64 bytes. No job, no polling.
- **Video is a queued generation.** `POST /api/ai/queue` returns
  `generations[0].generation_id`; `GET /api/ai/queue/{id}` reports
  `queued`, `processing`, `succeeded`, `failed` or `cancelled`; the file is at
  `result_url`, which is signed again on every read.

The engine hides the difference: `run()` returns the same `OutputFile` list
either way, and an empty answer is an error, never an empty success.

## The catalogue is a starter set, on purpose

Oxen serves 200+ models through the same two endpoints. Adding one is a TOML
file and a standby profile, never a code change:

1. `GET /api/ai/models/<id>` returns the model's `request_schema`.
2. `GET /api/ai/models/<id>` also carries the pricing fields
   (`cost_per_image`, `cost_per_second`, token rates).
3. Write the entry from that schema — do not copy parameters from a sibling
   model. Two models in one family often disagree.

`engine-oxen-docs/_sources/2026-11-11/inference-api/reference/models/` holds one
page per model with its parameter table, so a new entry can be written and
checked offline.

## Draft mode (Seedance 2.5 only)

A draft is a **fixed 480p preview** — there is no resolution choice for it. Set
`draft = true` and the engine sends `draft: true` with `resolution: 480p`, then
keeps the draft's id (Oxen's `videos[0].draft_task_id`) in the output metadata
under `draft_task`, with the expiry taken from `videos[0].draft_expires_at`.

Render the final by giving the next input `draft_task_id` in its metadata. That
call sends the draft id and nothing else, and the resolution is **1080p or 4k**
only: 4k when asked for 4k, otherwise 1080p. Asking for 480p or 720p on a final
is rendered at 1080p and said out loud in the run log, never silently.

## Resolutions

`resolution` is a select wherever the model documents one. Seedance 2.5 offers
`4k` because Oxen's reference for that model lists it — unlike BytePlus's own
public Seedance 2.5 page, which stops at 1080p. Where a provider and a gateway
disagree, the gateway is what this engine calls, so the gateway's page is the
one that decides the entry.

## Tests

```
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
```

This engine's own tests, plus the shared conformance suite.

## Not carried yet

- **Text and audio models.** There is no `messages[]` shape in this engine and
  `/api/ai/audio/generate` has no verified response envelope yet. Both are
  honest absences, not half-supported paths.
- **Async batching.** `num_generations` (1-4 per enqueue) is one job per call
  here; the engine keeps one file per input.
