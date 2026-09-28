import json

import pytest
import requests
from corroboration import (
    BrandReference,
    compare_brand,
    domain_relationship,
    lookup_virustotal,
)

REFERENCE = {
    "brand": "Example",
    "official_domains": ["example.test"],
    "authorized_identity_domains": ["identity.test"],
    "source_url": "https://example.test/providers",
    "verified_at": "2026-01-01T00:00:00Z",
    "verified_by": "Synthetic fixture author",
}


def test_brand_matching_uses_domain_boundaries_and_preserves_provider_counterevidence():
    reference = BrandReference.model_validate(REFERENCE)
    assert domain_relationship("login.example.test", reference) == "official_domain"
    assert (
        domain_relationship("login.identity.test", reference)
        == "authorized_identity_provider"
    )
    for host in ("notexample.test", "example.test.attacker.test", "example-login.test"):
        assert domain_relationship(host, reference) == "unverified_relationship"
    case = {
        "target_url": "https://example.test",
        "subject_sha256": "a" * 64,
        "observations": [
            {"id": "obs-001", "forms": [{"action": "https://identity.test/login"}]}
        ],
    }
    result = compare_brand(case, REFERENCE)
    assert result["comparisons"][-1]["relationship"] == "authorized_identity_provider"
    assert "verdict" not in result and result["reference"]["verified_by"]
    assert result["checked_at"]


@pytest.mark.parametrize(
    "domain",
    ["*.example.test", "example.test/path", "example.test@attacker.test", "com"],
)
def test_ambiguous_brand_references_are_rejected(domain):
    with pytest.raises(ValueError):
        BrandReference.model_validate({**REFERENCE, "official_domains": [domain]})


def test_reputation_skips_missing_credentials_without_calling_any_endpoint():
    def fail(*a, **k):
        raise AssertionError("Should not contact a provider")

    result = lookup_virustotal("https://sample.test", None, get=fail)
    assert (
        result["status"] == "skipped" and result["verdict_effect"] == "model_assessed"
    )


def test_lookup_is_read_only_bounded_and_keeps_original_analysis_time():
    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def iter_content(self, size):
            yield json.dumps(
                {
                    "data": {
                        "attributes": {
                            "last_analysis_date": 1700000000,
                            "last_analysis_stats": {"malicious": 3},
                        }
                    }
                }
            ).encode()

    def get(url, **kwargs):
        assert url.startswith("https://www.virustotal.com/api/v3/urls/")
        assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
        return Response()

    result = lookup_virustotal("https://sample.test", "synthetic-test-key", get=get)
    assert result["last_analysis_date"] == 1700000000
    assert result["stats"]["malicious"] == 3 and result["checked_at"]
    assert "synthetic-test-key" not in json.dumps(result)


def test_provider_error_never_leaks_key_in_exception_text():
    def fail(*a, **k):
        raise requests.RequestException("sensitive-key-in-library-error")

    result = lookup_virustotal("https://sample.test", "key", get=fail)
    assert result["status"] == "unavailable"
    assert "sensitive-key" not in json.dumps(result)
