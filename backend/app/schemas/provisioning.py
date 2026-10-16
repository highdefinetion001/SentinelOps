"""Validated contracts for fixed-template simulated server provisioning."""

import re
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from backend.app.schemas.fleet import FleetModel
from backend.app.schemas.server_state import EnvironmentMetadata, ServerConfig

ServerRole = Literal["web", "application", "database"]
TemplateId = Literal["web-default-v1", "application-default-v1", "database-default-v1"]


class ProvisionServerRequest(FleetModel):
    request_id: UUID = Field(strict=False)
    server_id: str = Field(min_length=1, max_length=63)
    display_name: str = Field(min_length=1, max_length=128)
    role: ServerRole
    template_id: TemplateId
    description: str | None = Field(default=None, max_length=500)
    demo_mode: Literal[True] = True

    @field_validator("server_id")
    @classmethod
    def validate_server_id(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
            raise ValueError("server_id must use lowercase letters, digits, and single hyphens")
        return value

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        value = value.strip()
        if not value or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]*", value):
            raise ValueError("display name contains unsupported characters")
        return value

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if any(ord(char) < 32 for char in value):
            raise ValueError("description contains control characters")
        return value or None


class ServerTemplate(FleetModel):
    template_id: TemplateId
    version: str
    role: ServerRole
    service: str
    config_version: str
    config: ServerConfig
    environment: EnvironmentMetadata


class ServerTemplateList(FleetModel):
    items: list[ServerTemplate]


class ProvisioningResult(FleetModel):
    request_id: UUID
    server_id: str
    display_name: str
    role: ServerRole
    template_id: TemplateId
    status: Literal[
        "pending_baseline",
        "provisioning",
        "starting",
        "healthy",
        "failed",
        "registered",
        "active",
        "unavailable",
    ]
    health_status: Literal["healthy", "unknown", "unhealthy"]
    container_name: str | None = None
    baseline_status: Literal["pending_approval", "approved", "not_created"]
    message: str
    error_step: str | None = None
