import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine_oxen.catalog import load_catalog  # noqa: E402


def test_both_families_are_catalogued():
    catalog = load_catalog()
    image = [s for s in catalog.specs() if s.category == "IMG"]
    video = [s for s in catalog.specs() if s.category == "VID"]
    assert len(image) >= 4
    assert len(video) >= 4


def test_images_are_sync_and_video_uses_the_queue():
    for spec in load_catalog().specs():
        if spec.category == "IMG":
            assert spec.mode == "sync"
            assert spec.api_path == "api/ai/images/generate"
        else:
            assert spec.mode == "async"
            assert spec.api_path == "api/ai/queue"
        assert spec.payload_style == "flat"


def test_seedance_offers_4k_and_the_short_ratios_are_not_invented():
    spec = load_catalog().get("bytedance-seedance-2-5-text-to-video")
    resolution = spec.params["resolution"]
    assert [option["value"] for option in resolution["options"]] == [
        "480p", "720p", "1080p", "4k"]
    assert spec.params["duration"]["min"] == 4
    assert spec.params["duration"]["max"] == 30


def test_every_entry_names_a_model_that_the_docs_capture_holds():
    root = Path(__file__).resolve().parent.parent.parent / "engine-oxen-docs" / "_sources"
    if not root.is_dir():
        return  # the docs repo is a sibling checkout; absence is not a failure
    pages = {path.stem for path in root.rglob("inference-api/reference/models/*.md")}
    for spec in load_catalog().specs():
        assert spec.model in pages, f"{spec.model} has no reference page in the capture"


def test_the_key_environment_variable_is_oxen():
    from engine_oxen import metadata

    assert metadata.API_KEY_ENV_VAR == "OXEN_API_KEY"
