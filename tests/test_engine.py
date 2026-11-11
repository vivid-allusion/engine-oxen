"""Oxen.ai generation, with the provider faked."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine_oxen import Engine, EngineError, InputFile  # noqa: E402
from engine_oxen.transport import ProviderError  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"pixels"
MODEL = "black-forest-labs-flux-2-klein-4b"
VIDEO = "bytedance-seedance-2-5-text-to-video"


class FakeClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.downloads = []

    def request_json(self, method, path, payload=None, *, headers=None, params=None):
        self.calls.append({"method": method, "path": path, "payload": payload})
        if not self.script:
            raise AssertionError(f"unexpected request: {method} {path}")
        response = self.script.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def download(self, url):
        self.downloads.append(url)
        return PNG if url.endswith(".png") else MP4

    def request_form(self, *a, **k):
        raise AssertionError("Oxen takes JSON, never a form")

    def request_bytes(self, *a, **k):
        return PNG


def make_engine(tmp_path, monkeypatch, client, **profile):
    monkeypatch.setattr(Engine, "_client", lambda self: client)
    base = {
        "platform": "oxen",
        "endpoint": MODEL,
        "media_type": "image",
        "parameters": {},
        "poll_interval": 0,
        "timeout_seconds": 0,
        "retry_on_429": 0,
    }
    base.update(profile)
    return Engine(base, tmp_path, api_key="oxen-test")


def an_input(tmp_path, prompt="a paper lantern"):
    path = tmp_path / "shot.md"
    path.write_text(prompt + "\n")
    return InputFile(path=path, prompt=prompt)


def test_a_sync_image_is_saved(tmp_path, monkeypatch):
    client = FakeClient([{"images": [{"url": "https://cdn.invalid/out.png"}]}])
    results = make_engine(tmp_path, monkeypatch, client).run([an_input(tmp_path)])
    assert [r.status for r in results] == ["ok"]
    assert results[0].path.read_bytes() == PNG
    assert client.calls[0]["path"] == "api/ai/images/generate"
    assert client.calls[0]["payload"]["model"] == MODEL
    assert client.calls[0]["payload"]["prompt"] == "a paper lantern"


def test_reference_images_ride_as_a_list(tmp_path, monkeypatch):
    client = FakeClient([{"images": [{"url": "https://cdn.invalid/out.png"}]}])
    item = an_input(tmp_path, prompt="make it night")
    item.reference_urls = ["https://cdn.invalid/a.png", "https://cdn.invalid/b.png"]
    make_engine(tmp_path, monkeypatch, client).run([item])
    assert client.calls[0]["payload"]["input_image"] == [
        "https://cdn.invalid/a.png", "https://cdn.invalid/b.png"]


def test_an_empty_prompt_fails_before_spending(tmp_path, monkeypatch):
    client = FakeClient([])
    results = make_engine(tmp_path, monkeypatch, client).run([an_input(tmp_path, prompt="")])
    assert results[0].status == "error"
    assert client.calls == []


def test_an_empty_answer_is_an_error_not_a_success(tmp_path, monkeypatch):
    client = FakeClient([{"images": []}])
    results = make_engine(tmp_path, monkeypatch, client).run([an_input(tmp_path)])
    assert results[0].status == "error"
    assert "No output" in results[0].error_msg


def test_a_fatal_error_stops_the_batch(tmp_path, monkeypatch):
    client = FakeClient([ProviderError(401, "unauthenticated")])
    results = make_engine(tmp_path, monkeypatch, client).run([an_input(tmp_path), an_input(tmp_path)])
    assert len(results) == 1


def test_a_missing_key_is_a_preflight_error(tmp_path):
    engine = Engine({"endpoint": MODEL}, tmp_path)
    try:
        engine.run([an_input(tmp_path)])
    except EngineError as exc:
        assert "OXEN_API_KEY" in str(exc)
    else:
        raise AssertionError("expected EngineError")


def test_video_enqueues_and_polls_until_it_succeeds(tmp_path, monkeypatch):
    client = FakeClient([
        {"generations": [{"generation_id": "gen-1", "status": "queued"}]},
        {"generation_id": "gen-1", "status": "processing", "result_url": None},
        {"generation_id": "gen-1", "status": "succeeded",
         "result_url": "https://cdn.invalid/out.mp4"},
    ])
    engine = make_engine(tmp_path, monkeypatch, client, endpoint=VIDEO, media_type="video",
                         parameters={"duration": 5, "resolution": "4k"})
    results = engine.run([an_input(tmp_path, prompt="a fox in the snow")])
    assert [r.status for r in results] == ["ok"]
    submit = client.calls[0]
    assert submit["path"] == "api/ai/queue"
    assert submit["payload"] == {"model": VIDEO, "prompt": "a fox in the snow",
                                 "duration": 5, "resolution": "4k"}
    assert client.calls[1]["method"] == "GET"
    assert client.calls[1]["path"] == "api/ai/queue/gen-1"
    assert client.downloads == ["https://cdn.invalid/out.mp4"]


def test_a_failed_generation_is_reported_with_its_reason(tmp_path, monkeypatch):
    client = FakeClient([
        {"generations": [{"generation_id": "gen-2", "status": "queued"}]},
        {"generation_id": "gen-2", "status": "failed", "error_message": "Insufficient credits"},
    ])
    engine = make_engine(tmp_path, monkeypatch, client, endpoint=VIDEO, media_type="video")
    results = engine.run([an_input(tmp_path)])
    assert results[0].status == "error"
    assert "Insufficient credits" in results[0].error_msg


def test_an_unknown_parameter_is_dropped_with_a_warning(tmp_path, monkeypatch):
    client = FakeClient([{"images": [{"url": "https://cdn.invalid/out.png"}]}])
    engine = make_engine(tmp_path, monkeypatch, client, parameters={"not_a_param": 1})
    seen = []
    engine._on_progress = lambda event: seen.append(str(event))
    engine.run([an_input(tmp_path)])
    assert "not_a_param" not in client.calls[0]["payload"]
    assert any("not_a_param" in text for text in seen)


def test_a_draft_is_always_480p(tmp_path, monkeypatch):
    client = FakeClient([
        {"generations": [{"generation_id": "draft-1", "status": "queued"}]},
        {"generation_id": "draft-1", "status": "succeeded", "result_url": "https://cdn.invalid/d.mp4",
         "enqueued_at": 1794000000},
    ])
    engine = make_engine(tmp_path, monkeypatch, client, endpoint=VIDEO, media_type="video",
                         parameters={"draft": True, "resolution": "4k"})
    item = an_input(tmp_path, prompt="a fox")
    out = engine.run([item])[0]
    assert out.status == "ok"
    assert client.calls[0]["payload"]["draft"] is True
    assert client.calls[0]["payload"]["resolution"] == "480p"
    assert out.metadata["draft_task"] == {"id": "draft-1", "created_at": 1794000000,
                                          "expires_at": 1794000000 + 7 * 86400}


def test_finishing_a_draft_reuses_it_at_1080p(tmp_path, monkeypatch):
    client = FakeClient([
        {"generations": [{"generation_id": "final-1", "status": "queued"}]},
        {"generation_id": "final-1", "status": "succeeded", "result_url": "https://cdn.invalid/f.mp4"},
    ])
    engine = make_engine(tmp_path, monkeypatch, client, endpoint=VIDEO, media_type="video",
                         parameters={"resolution": "720p"})
    item = an_input(tmp_path, prompt="")
    item.metadata["draft_task_id"] = "draft-1"
    out = engine.run([item])[0]
    assert out.status == "ok"
    assert client.calls[0]["payload"] == {"model": VIDEO, "draft_task_id": "draft-1",
                                          "resolution": "1080p"}
    assert out.metadata["finished_from"] == "draft-1"


def test_finishing_a_draft_at_4k_is_honoured(tmp_path, monkeypatch):
    client = FakeClient([
        {"generations": [{"generation_id": "final-2", "status": "queued"}]},
        {"generation_id": "final-2", "status": "succeeded", "result_url": "https://cdn.invalid/f.mp4"},
    ])
    engine = make_engine(tmp_path, monkeypatch, client, endpoint=VIDEO, media_type="video",
                         parameters={"resolution": "4k", "watermark": False})
    item = an_input(tmp_path, prompt="")
    item.metadata["draft_task_id"] = "draft-1"
    engine.run([item])
    assert client.calls[0]["payload"] == {
        "model": VIDEO, "draft_task_id": "draft-1", "resolution": "4k", "watermark": False}
