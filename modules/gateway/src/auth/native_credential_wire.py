"""Private native credential wire v1. Public bindings only; never persist payloads."""

import base64
import hashlib
import json
import re
from typing import Literal
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

VERSION = "adp-native-private-delivery-v1"
AUDIENCE = "adp-native-private-host"
MAX_WIRE_BYTES = 196608


def canonical(value):
    def valid(item):
        if isinstance(item, dict):
            return all(type(k) is str and valid(v) for k, v in item.items())
        if isinstance(item, list):
            return all(valid(v) for v in item)
        return type(item) in {str, int}

    if not valid(value):
        raise ValueError("unsupported wire value")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def decode(value, length=None):
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError("invalid binary wire value")
    result = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if encode(result) != value or length is not None and len(result) != length:
        raise ValueError("invalid binary wire length")
    return result


def parse(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    if len(raw) > MAX_WIRE_BYTES:
        raise ValueError("wire too large")
    value = json.loads(raw, object_pairs_hook=pairs)
    canonical(value)
    return value


class CreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str
    credential_id: str = Field(min_length=1, max_length=36)
    operation: Literal["assume_role", "materialize"]
    service: str = Field(min_length=1, max_length=255)
    label: str = Field(min_length=1, max_length=255)
    argv: list[str] = Field(min_length=1, max_length=128)
    cwd: str = Field(min_length=1, max_length=4096)
    use_seconds: int = Field(ge=1, le=900)
    purpose: str = Field(default="", max_length=1000)

    @field_validator("request_id")
    @classmethod
    def uuid(cls, value):
        if str(UUID(value)) != value:
            raise ValueError("canonical UUID required")
        return value

    @model_validator(mode="after")
    def bounded(self):
        if not self.cwd.startswith("/") or not self.argv[0].startswith("/"):
            raise ValueError("absolute command and cwd required")
        if any("\x00" in v or len(v) > 4096 for v in self.argv) or "\x00" in self.cwd:
            raise ValueError("invalid command")
        if self.operation == "assume_role" and self.use_seconds != 900 or self.operation == "materialize" and self.purpose:
            raise ValueError("invalid operation lifetime or purpose")
        if len(canonical(self.model_dump())) > 32768:
            raise ValueError("request too large")
        return self

    def arguments(self):
        result = dict(service=self.service, label=self.label)
        if self.operation == "assume_role":
            result["purpose"] = self.purpose
        return result


def validate_offer(offer, selection):
    expected = {
        "version",
        "selection",
        "command_digest",
        "sink_digest",
        "nonce",
        "encryption_public_key",
        "proof_public_key",
        "recipient_fingerprint",
    }
    if not isinstance(offer, dict) or set(offer) != expected or offer["version"] != VERSION or offer["selection"] != selection:
        raise ValueError("offer binding changed")
    for key in ("command_digest", "sink_digest", "recipient_fingerprint"):
        if not isinstance(offer[key], str) or re.fullmatch(r"[a-f0-9]{64}", offer[key]) is None:
            raise ValueError("invalid binding digest")
    decode(offer["nonce"], 32)
    key = x25519.X25519PublicKey.from_public_bytes(decode(offer["encryption_public_key"], 32))
    # Reject low-order recipients before any provider issuance is possible.
    x25519.X25519PrivateKey.generate().exchange(key)
    ed25519.Ed25519PublicKey.from_public_bytes(decode(offer["proof_public_key"], 32))
    if offer["recipient_fingerprint"] != digest({k: offer[k] for k in ("encryption_public_key", "proof_public_key")}):
        raise ValueError("recipient changed")
    return digest(offer)


def verify_proof(body, offer, challenge):
    if not isinstance(body, dict) or set(body) != {"type", "proof", "signature"} or body["type"] != "proof":
        raise ValueError("invalid proof")
    expected = dict(version=VERSION, offer=offer, challenge=challenge)
    if body["proof"] != expected:
        raise ValueError("proof binding changed")
    try:
        ed25519.Ed25519PublicKey.from_public_bytes(decode(offer["proof_public_key"], 32)).verify(decode(body["signature"], 64), canonical(expected))
    except InvalidSignature:
        raise ValueError("invalid proof") from None


def encrypt_payload(*, offer, challenge, issued_at, credential_expires_at, payload, nonce):
    sender = x25519.X25519PrivateKey.generate()
    selection = offer["selection"]
    body = dict(
        version=VERSION,
        issuer=selection["issuer"],
        audience=AUDIENCE,
        key_id=selection["key_id"],
        offer=offer,
        challenge=challenge,
        issued_at=issued_at,
        delivery_expires_at=issued_at + 90,
        credential_expires_at=credential_expires_at,
        ephemeral_public_key=encode(sender.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)),
        iv=encode(nonce),
    )
    aad = canonical(body)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=decode(offer["nonce"], 32), info=VERSION.encode() + hashlib.sha256(aad).digest()).derive(
        sender.exchange(x25519.X25519PublicKey.from_public_bytes(decode(offer["encryption_public_key"], 32)))
    )
    body["ciphertext"] = encode(AESGCM(key).encrypt(nonce, canonical(payload), aad))
    return body
