"""Compare input validation while retaining the reviewed schema for callers."""

from tool_adapters.atlassian_observe import canonical

_SCHEMA_MAPS = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
_SCHEMA_LISTS = {"allOf", "anyOf", "oneOf", "prefixItems"}
_SCHEMA_VALUES = {
    "items",
    "additionalItems",
    "additionalProperties",
    "unevaluatedItems",
    "unevaluatedProperties",
    "contains",
    "propertyNames",
    "not",
    "if",
    "then",
    "else",
    "contentSchema",
}


def _without_descriptions(schema):
    if not isinstance(schema, dict):
        return schema
    result = {}
    for key, value in schema.items():
        if key == "description" and isinstance(value, str):
            continue
        if key in _SCHEMA_MAPS and isinstance(value, dict):
            value = {name: _without_descriptions(child) for name, child in value.items()}
        elif key in _SCHEMA_LISTS and isinstance(value, list):
            value = [_without_descriptions(child) for child in value]
        elif key in _SCHEMA_VALUES:
            value = (
                [_without_descriptions(child) for child in value]
                if key == "items" and isinstance(value, list)
                else _without_descriptions(value)
            )
        result[key] = value
    return result


def same_input_contract(actual, reviewed):
    """Ignore description annotations only at known JSON Schema locations.

    Properties named description, defaults, const/enum values, extensions and
    all validation keywords remain exact. Unknown schema vocabularies fail
    conservatively rather than treating their contents as annotations.
    """
    return (
        isinstance(actual, dict)
        and isinstance(reviewed, dict)
        and canonical(_without_descriptions(actual)) == canonical(_without_descriptions(reviewed))
    )
