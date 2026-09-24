"""Tenant-boundary tests for the shared object-access resolver (issue #5616).

These assert the invariant from finding #4730 directly: an analysis job can
only reach objects inside the requesting org/team/user space, and a rejection
happens before any storage call.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sample_access as sa  # noqa: E402

BUCKET = "adp-dev-chat-artifacts"


@pytest.fixture(autouse=True)
def _configure(monkeypatch):
    monkeypatch.setenv("CYBER_ALLOWED_BUCKETS", f"{BUCKET},adp-dev-cape-assets")
    monkeypatch.delenv("CYBER_SCRIPT_PREFIXES", raising=False)


@pytest.fixture
def ctx() -> sa.JobContext:
    return sa.JobContext(org_id="acme", team_id="team-a", user_id="user-1")


def _uri(key: str, bucket: str = BUCKET) -> str:
    return f"s3://{bucket}/{key}"


class TestJobContext:
    def test_identity_is_extracted(self):
        ctx = sa.job_context({"org_id": "acme", "team_id": "t1", "user_id": "u1"})
        assert ctx.tenant_prefix == "o/acme/t/t1/u/u1/"

    @pytest.mark.parametrize("missing", ["org_id", "team_id", "user_id"])
    def test_job_without_identity_is_rejected(self, missing):
        body = {"org_id": "acme", "team_id": "t1", "user_id": "u1"}
        del body[missing]
        with pytest.raises(sa.AccessDenied) as e:
            sa.job_context(body)
        assert e.value.reason == sa.REASON_MISSING_IDENTITY

    @pytest.mark.parametrize("value", ["../other", "a/b", "o/acme", "-lead", "x" * 200])
    def test_identity_that_could_relocate_the_prefix_is_rejected(self, value):
        with pytest.raises(sa.AccessDenied) as e:
            sa.job_context({"org_id": value, "team_id": "t1", "user_id": "u1"})
        assert e.value.reason == sa.REASON_MALFORMED_IDENTITY

    def test_non_string_identity_is_rejected(self):
        with pytest.raises(sa.AccessDenied) as e:
            sa.job_context({"org_id": {"a": 1}, "team_id": "t1", "user_id": "u1"})
        assert e.value.reason == sa.REASON_MALFORMED_IDENTITY


class TestSampleWithinTenantSpace:
    def test_own_sample_is_accepted(self, ctx):
        key = "o/acme/t/team-a/u/user-1/s/sess-1/task-1/in/sample.bin"
        ref = sa.resolve_sample({"sample_s3_uri": _uri(key)}, ctx)
        assert ref.bucket == BUCKET
        assert ref.key == key

    @pytest.mark.parametrize(
        "key",
        [
            "o/other-org/t/team-a/u/user-1/in/x.bin",
            "o/acme/t/other-team/u/user-1/in/x.bin",
            "o/acme/t/team-a/u/other-user/in/x.bin",
        ],
    )
    def test_other_tenant_sample_is_rejected(self, ctx, key):
        with pytest.raises(sa.AccessDenied) as e:
            sa.resolve_sample({"sample_s3_uri": _uri(key)}, ctx)
        assert e.value.reason == sa.REASON_OUTSIDE_TENANT_PREFIX

    @pytest.mark.parametrize(
        "key",
        [
            # Shares opening characters with the permitted org but is a
            # different org — the case a str.startswith check would allow.
            "o/acme-evil/t/team-a/u/user-1/in/x.bin",
            "o/acme2/t/team-a/u/user-1/in/x.bin",
            "o/acme/t/team-attacker/u/user-1/in/x.bin",
            "o/acme/t/team-a/u/user-10/in/x.bin",
        ],
    )
    def test_textual_prefix_neighbour_is_outside(self, ctx, key):
        with pytest.raises(sa.AccessDenied) as e:
            sa.resolve_sample({"sample_s3_uri": _uri(key)}, ctx)
        assert e.value.reason == sa.REASON_OUTSIDE_TENANT_PREFIX

    @pytest.mark.parametrize(
        "key",
        [
            "o/acme/t/team-a/u/user-1/../../../../../../o/other/t/t/u/u/in/x.bin",
            "o/acme/t/team-a/u/user-1/./in/x.bin",
            "../o/other/t/t/u/u/in/x.bin",
        ],
    )
    def test_relative_segments_are_rejected(self, ctx, key):
        """A traversal placed after the prefix leaves leading segments
        matching, so it must be refused by the parser, not the boundary test."""
        with pytest.raises(sa.AccessDenied) as e:
            sa.resolve_sample({"sample_s3_uri": _uri(key)}, ctx)
        assert e.value.reason == sa.REASON_MALFORMED_LOCATION

    def test_prefix_itself_is_not_a_readable_object(self, ctx):
        with pytest.raises(sa.AccessDenied):
            sa.resolve_sample({"sample_s3_uri": _uri("o/acme/t/team-a/u/user-1")}, ctx)

    def test_bucket_outside_configuration_is_rejected(self, ctx):
        key = "o/acme/t/team-a/u/user-1/in/x.bin"
        with pytest.raises(sa.AccessDenied) as e:
            sa.resolve_sample({"sample_s3_uri": _uri(key, "attacker-bucket")}, ctx)
        assert e.value.reason == sa.REASON_BUCKET_NOT_ALLOWED

    def test_unset_bucket_config_fails_closed(self, ctx, monkeypatch):
        monkeypatch.delenv("CYBER_ALLOWED_BUCKETS", raising=False)
        key = "o/acme/t/team-a/u/user-1/in/x.bin"
        with pytest.raises(sa.AccessDenied) as e:
            sa.resolve_sample({"sample_s3_uri": _uri(key)}, ctx)
        assert e.value.reason == sa.REASON_BUCKET_NOT_ALLOWED

    @pytest.mark.parametrize(
        "uri",
        [
            "https://evil.example/x",
            "o/acme/t/team-a/u/user-1/in/x.bin",
            "s3://",
            "s3://bucket-only",
            "",
        ],
    )
    def test_malformed_location_is_rejected(self, ctx, uri):
        with pytest.raises(sa.AccessDenied) as e:
            sa.resolve_sample({"sample_s3_uri": uri}, ctx)
        assert e.value.reason == sa.REASON_MALFORMED_LOCATION

    def test_absent_location_is_rejected(self, ctx):
        with pytest.raises(sa.AccessDenied) as e:
            sa.resolve_sample({}, ctx)
        assert e.value.reason == sa.REASON_MALFORMED_LOCATION


class TestScriptLocation:
    """Mode B scripts get the tenant rule *and* a script-prefix allowlist."""

    def test_registered_script_location_is_accepted(self, ctx):
        key = "o/acme/t/team-a/u/user-1/scripts/artifact-1/stage-3.py"
        assert sa.resolve_script({"script_s3_uri": _uri(key)}, ctx).key == key

    def test_other_tenant_script_is_rejected(self, ctx):
        key = "o/other-org/t/team-a/u/user-1/scripts/a/stage-3.py"
        with pytest.raises(sa.AccessDenied) as e:
            sa.resolve_script({"script_s3_uri": _uri(key)}, ctx)
        assert e.value.reason == sa.REASON_OUTSIDE_TENANT_PREFIX

    def test_script_outside_script_prefix_is_rejected(self, ctx):
        """An uploaded sample inside the tenant's own space is not executable."""
        key = "o/acme/t/team-a/u/user-1/in/uploaded.py"
        with pytest.raises(sa.AccessDenied) as e:
            sa.resolve_script({"script_s3_uri": _uri(key)}, ctx)
        assert e.value.reason == sa.REASON_SCRIPT_PREFIX_NOT_ALLOWED

    def test_script_prefix_neighbour_is_rejected(self, ctx):
        key = "o/acme/t/team-a/u/user-1/scripts-evil/stage-3.py"
        with pytest.raises(sa.AccessDenied) as e:
            sa.resolve_script({"script_s3_uri": _uri(key)}, ctx)
        assert e.value.reason == sa.REASON_SCRIPT_PREFIX_NOT_ALLOWED

    def test_script_prefixes_are_configurable(self, ctx, monkeypatch):
        monkeypatch.setenv("CYBER_SCRIPT_PREFIXES", "approved-scripts/")
        key = "o/acme/t/team-a/u/user-1/approved-scripts/s.py"
        assert sa.resolve_script({"script_s3_uri": _uri(key)}, ctx).key == key
        with pytest.raises(sa.AccessDenied):
            other = "o/acme/t/team-a/u/user-1/scripts/s.py"
            sa.resolve_script({"script_s3_uri": _uri(other)}, ctx)


class TestRejectionDoesNotLeak:
    def test_reason_and_message_omit_the_rejected_location(self, ctx):
        secret = "o/victim-org/t/secret-team/u/victim/in/confidential.docx"
        with pytest.raises(sa.AccessDenied) as e:
            sa.resolve_sample({"sample_s3_uri": _uri(secret)}, ctx)
        rendered = f"{e.value.reason} {e.value.detail} {e.value}"
        for fragment in ("victim-org", "secret-team", "confidential", secret):
            assert fragment not in rendered
