import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from backend.app.schemas.server_state import ServerStateV1


def test_valid_server_state(valid_state: dict[str, object]) -> None:
    state = ServerStateV1.model_validate_json(json.dumps(valid_state))
    assert state.server_id == "checkout-03"
    assert state.collected_at.utcoffset().total_seconds() == 0


@pytest.mark.parametrize(
    "remove", ["schema_version", "server_id", "config", "environment", "collected_at"]
)
def test_missing_required_field_rejected(valid_state: dict[str, object], remove: str) -> None:
    del valid_state[remove]
    with pytest.raises(ValidationError):
        ServerStateV1.model_validate_json(json.dumps(valid_state))


def test_invalid_types_and_null_rejected(valid_state: dict[str, object]) -> None:
    data = deepcopy(valid_state)
    data["config"]["debug_mode"] = None  # type: ignore[index]
    with pytest.raises(ValidationError):
        ServerStateV1.model_validate_json(json.dumps(data))


def test_naive_timestamp_rejected(valid_state: dict[str, object]) -> None:
    valid_state["collected_at"] = "2026-10-16T00:00:00"
    with pytest.raises(ValidationError):
        ServerStateV1.model_validate_json(json.dumps(valid_state))


def test_unknown_configuration_field_rejected(valid_state: dict[str, object]) -> None:
    valid_state["config"]["unrecognized"] = "ignored?"  # type: ignore[index]
    with pytest.raises(ValidationError):
        ServerStateV1.model_validate_json(json.dumps(valid_state))
