import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine_conformance import NotApplicable, RULE_IDS, check  # noqa: E402

from tests.conformance_connector import OxenConnector  # noqa: E402


@pytest.mark.parametrize("rule_id", RULE_IDS)
def test_conformance(rule_id, tmp_path, monkeypatch):
    try:
        check(rule_id, OxenConnector(), tmp_path, monkeypatch)
    except NotApplicable as exc:
        pytest.skip(str(exc))
