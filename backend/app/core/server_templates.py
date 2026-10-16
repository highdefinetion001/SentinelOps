"""Load and validate the versioned, safe server configuration templates."""

import json
from functools import lru_cache
from pathlib import Path

from backend.app.schemas.provisioning import ServerTemplate

PROJECT_ROOT = Path(__file__).resolve().parents[3]


@lru_cache(maxsize=1)
def load_server_templates() -> dict[str, ServerTemplate]:
    try:
        payload = json.loads(
            (PROJECT_ROOT / "config" / "server-templates.json").read_text(encoding="utf-8")
        )
        if not isinstance(payload, dict) or set(payload) != {"templates"}:
            raise ValueError
        templates = [ServerTemplate.model_validate(item) for item in payload["templates"]]
        result = {template.template_id: template for template in templates}
        if len(result) != len(templates) or not result:
            raise ValueError
        if any(
            template.role != template.template_id.removesuffix("-default-v1")
            for template in templates
        ):
            raise ValueError
        return result
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        raise RuntimeError("Server configuration templates are invalid") from None


def resolve_server_template(template_id: str, role: str) -> ServerTemplate:
    template = load_server_templates().get(template_id)
    if template is None or template.role != role:
        raise ValueError("The selected template is not supported for this server role")
    return template
