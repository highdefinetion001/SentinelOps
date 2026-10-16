"""Version 1 server-reported state contract."""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ServerConfig(StrictModel):
    """Configuration fields whose values participate in drift comparison."""

    debug_mode: bool
    max_connections: int = Field(ge=1, le=100_000)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
    tls_min_version: Literal["TLS1.2", "TLS1.3"]


class EnvironmentMetadata(StrictModel):
    """Environment versions participate in fingerprinting, but are not app config."""

    runtime_version: str = Field(min_length=1, max_length=64)
    app_version: str = Field(min_length=1, max_length=64)

    @field_validator("runtime_version", "app_version")
    @classmethod
    def validate_nonblank_versions(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("version must not be blank")
        return value


class ServerStateV1(StrictModel):
    """Collected state. Collection timestamp is volatile and excluded from fingerprints."""

    schema_version: Literal["1.0"]
    server_id: str = Field(min_length=1, max_length=128)
    service: str = Field(min_length=1, max_length=128)
    config_version: str = Field(min_length=1, max_length=64)
    config: ServerConfig
    environment: EnvironmentMetadata
    collected_at: datetime

    @field_validator("server_id")
    @classmethod
    def validate_server_id(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value):
            raise ValueError("server_id must use stable ASCII letters, digits, '.', '_' or '-'")
        return value

    @field_validator("service", "config_version")
    @classmethod
    def validate_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("collected_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("collected_at must include a timezone")
        return value
