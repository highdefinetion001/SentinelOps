from copy import deepcopy

import pytest

from backend.app.core.fingerprinting import fingerprint_config, fingerprint_state


def test_object_key_order_does_not_change_fingerprint(valid_state: dict[str, object]) -> None:
    reordered = dict(reversed(list(valid_state.items())))
    reordered["config"] = dict(reversed(list(valid_state["config"].items())))  # type: ignore[union-attr]
    assert fingerprint_state(valid_state) == fingerprint_state(reordered)


def test_collection_time_does_not_change_fingerprint(valid_state: dict[str, object]) -> None:
    later = deepcopy(valid_state)
    later["collected_at"] = "2026-10-17T09:45:12+05:30"
    assert fingerprint_state(valid_state) == fingerprint_state(later)


def test_configuration_change_changes_fingerprint(valid_state: dict[str, object]) -> None:
    changed = deepcopy(valid_state)
    changed["config"]["max_connections"] = 201  # type: ignore[index]
    assert fingerprint_state(valid_state) != fingerprint_state(changed)


def test_environment_change_changes_fingerprint(valid_state: dict[str, object]) -> None:
    changed = deepcopy(valid_state)
    changed["environment"]["runtime_version"] = "3.13"  # type: ignore[index]
    assert fingerprint_state(valid_state) != fingerprint_state(changed)


def test_invalid_configuration_is_rejected() -> None:
    with pytest.raises(ValueError, match="Invalid configuration"):
        fingerprint_config({"debug_mode": False, "max_connections": 2, "unrecognized": True})
