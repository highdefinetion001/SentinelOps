"""Strict YAML policy loading and deterministic policy-rule evaluation."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, StrictBool, StrictInt, StrictStr, ValidationError, model_validator

from backend.app.schemas.server_state import StrictModel

Severity = Literal["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
PolicyValue = StrictBool | StrictInt | StrictStr
_FIELD_PATH = re.compile(r"^(config|environment)\.[A-Za-z][A-Za-z0-9_]*$")


class SeverityDefaults(StrictModel):
    changed_value: Severity
    missing_field: Severity
    unexpected_field: Severity
    peer_outlier: Severity


class PolicyRule(StrictModel):
    id: str = Field(min_length=1, max_length=128)
    field: str
    operator: Literal["equals", "at_least", "range", "in"]
    severity: Severity
    value: PolicyValue | None = None
    minimum: StrictInt | None = None
    maximum: StrictInt | None = None
    values: list[PolicyValue] | None = None
    context: str | None = None

    @model_validator(mode="after")
    def validate_operator_arguments(self) -> "PolicyRule":
        if not _FIELD_PATH.fullmatch(self.field):
            raise ValueError("field must be a canonical config.* or environment.* path")
        if self.operator == "equals":
            valid = (
                self.value is not None
                and self.minimum is None
                and self.maximum is None
                and self.values is None
            )
        elif self.operator == "at_least":
            valid = (
                isinstance(self.value, str)
                and self.minimum is None
                and self.maximum is None
                and self.values is None
            )
        elif self.operator == "range":
            valid = (
                self.value is None
                and self.minimum is not None
                and self.maximum is not None
                and self.values is None
            )
            valid = valid and self.minimum <= self.maximum
        else:
            valid = (
                self.value is None
                and self.minimum is None
                and self.maximum is None
                and bool(self.values)
            )
        if not valid:
            raise ValueError(f"invalid arguments for policy operator {self.operator!r}")
        return self


class PolicyApplicability(StrictModel):
    service_group: str = Field(min_length=1)


class DetectionPolicy(StrictModel):
    policy_version: str = Field(min_length=1)
    applies_to: PolicyApplicability
    severity_categories: dict[Severity, str]
    severity_defaults: SeverityDefaults
    rules: list[PolicyRule]
    unknown_fields: Literal["reject"]

    @model_validator(mode="after")
    def validate_policy_consistency(self) -> "DetectionPolicy":
        expected = {"INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"}
        if set(self.severity_categories) != expected:
            raise ValueError(
                "severity_categories must define INFO, LOW, MEDIUM, HIGH, and CRITICAL"
            )
        rule_ids = [rule.id for rule in self.rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("policy rule IDs must be unique")
        return self


@dataclass(frozen=True)
class PolicyRuleViolation:
    policy_id: str
    field_path: str
    severity: Severity
    observed_value: Any


def load_policy(path: str | Path) -> DetectionPolicy:
    """Load policy YAML and fail clearly on invalid syntax or contract values."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Unable to read policy at {path}: {exc}") from exc
    try:
        return DetectionPolicy.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"Invalid policy at {path}: {exc}") from exc


def policy_violations(
    values: dict[str, object], policy: DetectionPolicy, *, service_group: str
) -> list[PolicyRuleViolation]:
    """Return policy rules violated by these observed flat field values."""
    if service_group != policy.applies_to.service_group:
        return []
    violations = []
    for rule in sorted(policy.rules, key=lambda item: item.id):
        if rule.field not in values:
            continue
        if not _rule_satisfied(rule, values[rule.field]):
            violations.append(
                PolicyRuleViolation(
                    policy_id=rule.id,
                    field_path=rule.field,
                    severity=rule.severity,
                    observed_value=values[rule.field],
                )
            )
    return violations


def _rule_satisfied(rule: PolicyRule, actual: object) -> bool:
    if rule.operator == "equals":
        return type(actual) is type(rule.value) and actual == rule.value
    if rule.operator == "at_least":
        actual_version = _version_tuple(actual)
        minimum_version = _version_tuple(rule.value)
        return (
            actual_version is not None
            and minimum_version is not None
            and actual_version >= minimum_version
        )
    if rule.operator == "range":
        return (
            isinstance(actual, int)
            and not isinstance(actual, bool)
            and rule.minimum is not None
            and rule.maximum is not None
            and rule.minimum <= actual <= rule.maximum
        )
    return any(type(actual) is type(allowed) and actual == allowed for allowed in rule.values or [])


def _version_tuple(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"TLS(\d+(?:\.\d+)*)", value)
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))
