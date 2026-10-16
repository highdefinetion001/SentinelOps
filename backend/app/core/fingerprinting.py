"""Canonical SHA-256 fingerprints for validated configuration and environment."""

import hashlib
import json

from pydantic import ValidationError

from backend.app.schemas.server_state import (
    EnvironmentMetadata,
    ServerConfig,
    ServerStateV1,
)


def canonical_json(value: object) -> str:
    """Serialize JSON data deterministically (UTF-8 text, sorted keys, compact separators)."""
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Value is not canonical JSON data: {exc}") from exc


def fingerprint_state(state: ServerStateV1 | dict[str, object]) -> str:
    """Hash only config and environment; exclude identity, versions, and collection metadata."""
    try:
        validated = (
            state
            if isinstance(state, ServerStateV1)
            else ServerStateV1.model_validate_json(canonical_json(state))
        )
    except ValidationError as exc:
        raise ValueError(f"Invalid server state cannot be fingerprinted: {exc}") from exc
    return fingerprint_config_environment(validated.config, validated.environment)


def fingerprint_config_environment(
    config: ServerConfig | dict[str, object],
    environment: EnvironmentMetadata | dict[str, object],
) -> str:
    """Fingerprint the exact validated configuration/environment scope used for server state."""
    try:
        validated_config = (
            config
            if isinstance(config, ServerConfig)
            else ServerConfig.model_validate_json(canonical_json(config))
        )
        validated_environment = (
            environment
            if isinstance(environment, EnvironmentMetadata)
            else EnvironmentMetadata.model_validate_json(canonical_json(environment))
        )
    except ValidationError as exc:
        raise ValueError(
            f"Invalid configuration or environment cannot be fingerprinted: {exc}"
        ) from exc
    payload = {
        "config": validated_config.model_dump(mode="json"),
        "environment": validated_environment.model_dump(mode="json"),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def fingerprint_config(config: ServerConfig | dict[str, object]) -> str:
    """Hash a configuration-only contract, rejecting missing and unknown fields."""
    try:
        validated = (
            config if isinstance(config, ServerConfig) else ServerConfig.model_validate(config)
        )
    except ValidationError as exc:
        raise ValueError(f"Invalid configuration cannot be fingerprinted: {exc}") from exc
    canonical = canonical_json(validated.model_dump(mode="json")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
