"""Canonical model JSON using the sandbox's finite binary64 number semantics."""

import rfc8785


def _binary64_numbers(value):
    if type(value) is int and not -(2**53 - 1) <= value <= 2**53 - 1:
        try:
            return float(value)
        except OverflowError as error:
            raise rfc8785.CanonicalizationError("model number exceeds finite binary64 range") from error
    if isinstance(value, dict):
        return {key: _binary64_numbers(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_binary64_numbers(item) for item in value]
    return value


def canonical_model_json(value) -> bytes:
    return rfc8785.dumps(_binary64_numbers(value))
