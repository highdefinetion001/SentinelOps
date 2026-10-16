from pathlib import Path

import pytest

from backend.app.config.baseline import load_baseline

ROOT = Path(__file__).parents[1]


def test_baseline_loads_and_is_separate_from_reported_state() -> None:
    baseline = load_baseline(ROOT / "config" / "baseline.json")
    assert baseline.baseline_id == "checkout-production"
    assert baseline.expected_config.debug_mode is False
    assert baseline.policy_version == "1.0"


def test_invalid_baseline_rejected(tmp_path: Path) -> None:
    path = tmp_path / "invalid.json"
    path.write_text('{"baseline_id": "incomplete"}', encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid baseline"):
        load_baseline(path)


def test_null_baseline_value_rejected(tmp_path: Path) -> None:
    path = tmp_path / "null.json"
    path.write_text('{"baseline_id": null}', encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid baseline"):
        load_baseline(path)
