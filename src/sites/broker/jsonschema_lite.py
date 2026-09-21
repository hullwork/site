"""The JSON Schema subset the Broker needs: enough for flow/tool input and output.

Supports ``type``, ``properties``, ``required``, ``items``, ``enum``,
``additionalProperties: false``, ``minLength``/``maxLength``, ``minItems``/``maxItems``.
Everything else is out of scope for this slice. Rejecting a config that uses an
unsupported keyword is C's job at config-apply time; this module only exposes
``supported()`` so C (and E, the manifest validator) can call it before accepting a
compiled config that D would otherwise silently under-validate at call time.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Keywords this module enforces.
_VALIDATION_KEYWORDS = frozenset({
    "type", "properties", "required", "items", "enum",
    "additionalProperties", "minLength", "maxLength", "minItems", "maxItems",
})
# Keywords that are pure metadata: harmless to carry, never enforced, never worth
# rejecting a config over.
_IGNORED_KEYWORDS = frozenset({"title", "description", "$schema", "$id", "examples", "default"})
_ALLOWED_KEYWORDS = _VALIDATION_KEYWORDS | _IGNORED_KEYWORDS

_TYPE_NAMES = frozenset({"string", "number", "integer", "boolean", "object", "array", "null"})


@dataclass(frozen=True)
class ValidationError:
    path: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"{self.path}: {self.message}"


def supported(schema: Any) -> list[str]:
    """Return the sorted, deduplicated list of keywords this schema uses that this
    validator does not implement. An empty list means ``validate()`` will enforce
    everything the schema asks for; a non-empty list is a reason to reject the
    config rather than silently under-validate at call time.
    """
    unsupported: set[str] = set()
    _walk_supported(schema, unsupported)
    return sorted(unsupported)


def _walk_supported(node: Any, unsupported: set[str]) -> None:
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if key == "additionalProperties":
            if not isinstance(value, bool):
                # A schema-shaped additionalProperties ("only extra keys matching
                # this schema are allowed") is not implemented; the only supported
                # value is a boolean, and only False changes behavior.
                unsupported.add("additionalProperties(schema)")
            continue
        if key not in _ALLOWED_KEYWORDS:
            unsupported.add(key)
        if key == "properties" and isinstance(value, dict):
            for prop_schema in value.values():
                _walk_supported(prop_schema, unsupported)
        elif key == "items":
            if isinstance(value, list):
                unsupported.add("items(array)")
            else:
                _walk_supported(value, unsupported)


def is_valid(instance: Any, schema: Any) -> bool:
    return not validate(instance, schema)


def validate(instance: Any, schema: Any) -> list[ValidationError]:
    """Validate ``instance`` against ``schema``; return every violation found."""
    errors: list[ValidationError] = []
    _validate_node(instance, schema, "$", errors)
    return errors


def _type_matches(instance: Any, type_name: str) -> bool:
    if type_name == "null":
        return instance is None
    if type_name == "boolean":
        return isinstance(instance, bool)
    if type_name == "integer":
        return isinstance(instance, int) and not isinstance(instance, bool)
    if type_name == "number":
        return isinstance(instance, (int, float)) and not isinstance(instance, bool)
    if type_name == "string":
        return isinstance(instance, str)
    if type_name == "object":
        return isinstance(instance, dict)
    if type_name == "array":
        return isinstance(instance, list)
    return False


def _validate_node(instance: Any, schema: Any, path: str, errors: list[ValidationError]) -> None:
    if not isinstance(schema, dict):
        return

    schema_type = schema.get("type")
    if schema_type is not None:
        type_names = schema_type if isinstance(schema_type, list) else [schema_type]
        if not any(_type_matches(instance, name) for name in type_names if name in _TYPE_NAMES):
            errors.append(ValidationError(path, f"expected type {schema_type!r}, got {_describe(instance)}"))
            return  # further structural checks would just add noise once the shape is wrong

    if "enum" in schema:
        allowed = schema["enum"]
        if not any(instance == candidate and type(instance) is type(candidate) for candidate in allowed):
            errors.append(ValidationError(path, f"value is not one of {allowed!r}"))

    if isinstance(instance, str):
        _validate_string(instance, schema, path, errors)
    if isinstance(instance, list):
        _validate_array(instance, schema, path, errors)
    if isinstance(instance, dict):
        _validate_object(instance, schema, path, errors)


def _validate_string(instance: str, schema: dict, path: str, errors: list[ValidationError]) -> None:
    min_length = schema.get("minLength")
    if isinstance(min_length, int) and len(instance) < min_length:
        errors.append(ValidationError(path, f"length must be at least {min_length}"))
    max_length = schema.get("maxLength")
    if isinstance(max_length, int) and len(instance) > max_length:
        errors.append(ValidationError(path, f"length must be at most {max_length}"))


def _validate_array(instance: list, schema: dict, path: str, errors: list[ValidationError]) -> None:
    min_items = schema.get("minItems")
    if isinstance(min_items, int) and len(instance) < min_items:
        errors.append(ValidationError(path, f"must have at least {min_items} item(s)"))
    max_items = schema.get("maxItems")
    if isinstance(max_items, int) and len(instance) > max_items:
        errors.append(ValidationError(path, f"must have at most {max_items} item(s)"))
    item_schema = schema.get("items")
    if isinstance(item_schema, dict):
        for index, item in enumerate(instance):
            _validate_node(item, item_schema, f"{path}[{index}]", errors)


def _validate_object(instance: dict, schema: dict, path: str, errors: list[ValidationError]) -> None:
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for name, prop_schema in properties.items():
            if name in instance:
                _validate_node(instance[name], prop_schema, f"{path}.{name}", errors)
    required = schema.get("required")
    if isinstance(required, list):
        for name in required:
            if name not in instance:
                errors.append(ValidationError(f"{path}.{name}", "is required"))
    # No "properties" with additionalProperties: false means no key is allowed.
    declared = properties if isinstance(properties, dict) else {}
    if schema.get("additionalProperties") is False:
        for name in instance:
            if name not in declared:
                errors.append(ValidationError(f"{path}.{name}", "is not a declared property"))


def _describe(instance: Any) -> str:
    if instance is None:
        return "null"
    if isinstance(instance, bool):
        return "boolean"
    if isinstance(instance, int):
        return "integer"
    if isinstance(instance, float):
        return "number"
    if isinstance(instance, str):
        return "string"
    if isinstance(instance, list):
        return "array"
    if isinstance(instance, dict):
        return "object"
    return type(instance).__name__
