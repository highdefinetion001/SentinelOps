"""Operator-declared server identities and private collector endpoints."""

import ipaddress
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from backend.app.collector.fleet import parse_server_registry


class ServerRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    server_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    display_name: str = Field(min_length=1, max_length=128)
    role: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    endpoint: str = Field(min_length=1, max_length=512)

    @field_validator("endpoint")
    @classmethod
    def require_private_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("endpoint must be a credential-free HTTP(S) base URL")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("endpoint has an invalid port") from exc
        if port == 0:
            raise ValueError("endpoint port must be nonzero")
        host = parsed.hostname
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*(?:\.(?:local|internal))?", host):
                raise ValueError(
                    "endpoint host must be a private service or internal domain"
                ) from exc
        else:
            if not (address.is_private or address.is_loopback or address.is_link_local):
                raise ValueError("endpoint IP address must be private")
        return value.rstrip("/")


def load_server_registry(path: str | Path) -> list[ServerRegistration]:
    """Load explicit declarations and reject unsafe or duplicate endpoint identities."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if (
            not isinstance(raw, dict)
            or set(raw) != {"servers"}
            or not isinstance(raw["servers"], list)
        ):
            raise ValueError("registry must contain only a 'servers' array")
        registrations = [ServerRegistration.model_validate(item) for item in raw["servers"]]
        if not registrations:
            raise ValueError("registry must declare at least one server")
        ids = [item.server_id for item in registrations]
        if len(ids) != len(set(ids)):
            raise ValueError("registry contains duplicate server IDs")
        endpoints = [item.endpoint for item in registrations]
        if len(endpoints) != len(set(endpoints)):
            raise ValueError("registry contains duplicate collector endpoints")
        for item in registrations:
            validated = parse_server_registry(f"{item.server_id}={item.endpoint}")
            if validated[item.server_id] != item.endpoint.rstrip("/"):
                raise ValueError("registry endpoint is not a valid base URL")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", item.display_name.replace(" ", "")):
                raise ValueError(
                    "display names may contain letters, digits, spaces, '.', '_' or '-'"
                )
        return registrations
    except (OSError, json.JSONDecodeError, ValidationError):
        raise ValueError("Unable to load the configured server registry") from None
