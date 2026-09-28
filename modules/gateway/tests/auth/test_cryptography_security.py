"""Offline regressions for the cryptography advisories fixed by the runtime pin."""

from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID
from cryptography.x509.verification import (
    Criticality,
    ExtensionPolicy,
    PolicyBuilder,
    Store,
    VerificationError,
)

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def certificate(subject, issuer, key, issuer_key, *, ca, extensions=()):
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer)]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    for extension, critical in extensions:
        builder = builder.add_extension(extension, critical)
    return builder.sign(issuer_key, hashes.SHA256())


def verifier(root, *, hostname, ca_callback=None):
    return (
        PolicyBuilder()
        .store(Store([root]))
        .time(NOW)
        .max_chain_depth(7)
        .extension_policies(
            ca_policy=ExtensionPolicy.permit_all().require_present(x509.BasicConstraints, Criticality.AGNOSTIC, ca_callback),
            ee_policy=ExtensionPolicy.permit_all().require_present(x509.SubjectAlternativeName, Criticality.AGNOSTIC, None),
        )
        .build_server_verifier(x509.DNSName(hostname))
    )


def test_wildcard_cannot_escape_permitted_dns_subtree():
    """CVE-2026-69248: a foo-only CA cannot certify *.example.com."""
    root_key = ec.generate_private_key(ec.SECP256R1())
    sub_key = ec.generate_private_key(ec.SECP256R1())
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    root = certificate("root", "root", root_key, root_key, ca=True)
    sub = certificate(
        "sub",
        "root",
        sub_key,
        root_key,
        ca=True,
        extensions=[(x509.NameConstraints([x509.DNSName("foo.example.com")], None), True)],
    )
    leaf = certificate(
        "leaf",
        "sub",
        leaf_key,
        sub_key,
        ca=False,
        extensions=[(x509.SubjectAlternativeName([x509.DNSName("*.example.com")]), False)],
    )
    with pytest.raises(VerificationError):
        verifier(root, hostname="bar.example.com").verify(leaf, [sub])
    permitted = certificate(
        "leaf",
        "sub",
        leaf_key,
        sub_key,
        ca=False,
        extensions=[(x509.SubjectAlternativeName([x509.DNSName("foo.example.com")]), False)],
    )
    assert len(verifier(root, hostname="foo.example.com").verify(permitted, [sub])) == 3


def test_duplicate_self_signed_intermediates_have_bounded_work():
    """CVE-2026-69249: bound issuer evaluations without a timing limit."""
    loop_key = ec.generate_private_key(ec.SECP256R1())
    root_key = ec.generate_private_key(ec.SECP256R1())
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    loop = certificate("loop", "loop", loop_key, loop_key, ca=True)
    unrelated = certificate("unrelated", "unrelated", root_key, root_key, ca=True)
    leaf = certificate(
        "leaf",
        "loop",
        leaf_key,
        loop_key,
        ca=False,
        extensions=[(x509.SubjectAlternativeName([x509.DNSName("example.com")]), False)],
    )
    evaluations = 0

    def count_issuer(policy, cert, extension):
        nonlocal evaluations
        evaluations += 1
        if evaluations > 1000:
            raise ValueError("issuer evaluation budget exhausted")

    with pytest.raises(VerificationError):
        verifier(unrelated, hostname="example.com", ca_callback=count_issuer).verify(leaf, [loop] * 4)
    assert evaluations <= 1000, "duplicate issuers caused excessive recursive validation"


def test_pkcs7_wrong_rsa_key_lengths_have_uniform_errors():
    """CVE-2026-69247: malformed encryptedKey must not disclose key lengths.

    Checks the error channel only, not timing or the inherent CBC padding oracle.
    All keys and data are ephemeral synthetic fixtures.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = certificate("recipient", "recipient", key, key, ca=False)
    envelope = (
        pkcs7.PKCS7EnvelopeBuilder()
        .set_data(b"synthetic security regression")
        .add_recipient(cert)
        .encrypt(serialization.Encoding.DER, [pkcs7.PKCS7Options.Binary])
    )
    assert pkcs7.pkcs7_decrypt_der(envelope, cert, key, []) == b"synthetic security regression"
    # This fixture has one 256-byte OCTET STRING: the RSA encryptedKey.
    marker = b"\x04\x82\x01\x00"
    assert envelope.count(marker) == 1
    start = envelope.index(marker) + len(marker)
    errors = []
    for recovered_length in (1, 2, 31):
        replacement = key.public_key().encrypt(b"x" * recovered_length, padding.PKCS1v15())
        malformed = envelope[:start] + replacement + envelope[start + len(replacement) :]
        try:
            pkcs7.pkcs7_decrypt_der(malformed, cert, key, [])
        except ValueError as error:
            errors.append(str(error))
        # A random fallback key can produce valid CBC padding by chance. This
        # unauthenticated format cannot guarantee rejection; compare failures.
    assert errors, "expected a malformed-key decryption error"
    assert len(set(errors)) == 1, "RSA recovered-key length leaks through decryption errors"
