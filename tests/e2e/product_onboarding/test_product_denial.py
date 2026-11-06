"""Synthetic API responses; these tests do not assert deployed denial behavior."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy

import pytest

from tests.e2e.product_onboarding.product_profile_api import (
    api_denial_oracle as check_denials,
)


class Reply:
    def __init__(self, status, body):
        self.status_code = status
        self.body = deepcopy(body)
        self.headers = {"Profile-Revision": str(body["revision"])} if "revision" in body else {}

    def json(self):
        return self.body


class DenialAPI:
    def __init__(self, *, defect=None):
        self.defect = defect
        self.calls = []
        self.actor = None
        self.profile = {
            "schema_version": "1.0", "tenant_id": "tenant-owned", "product_id": "product-owned",
            "project_id": "project-owned", "revision": 2,
            "product": {"name": "eval-product-test-edited", "purpose": "Ship a focused update"},
        }
        self.edited_fields = {"name": self.profile["product"]["name"],
                              "purpose": self.profile["product"]["purpose"]}
        self.history = [{"revision": revision, "tenant_id": "tenant-owned",
                         "product_id": "product-owned", "event_id": f"change-{revision}"}
                        for revision in (1, 2)]

    @contextmanager
    def client(self, actor):
        self.actor = actor
        try:
            yield self
        finally:
            self.actor = None

    def get(self, path):
        self.calls.append((self.actor, "GET", path))
        if self.actor == "revoked-token":
            body = {"detail": {"code": "authentication_required"}}
            if self.defect == "revoked_disclosure":
                body["product_id"] = "product-owned"
            return Reply(401, body)
        if self.actor == "foreign-token":
            if path == "/v1/products":
                items = [{"id": "product-owned", "project_ids": ["project-owned"]}] if self.defect == "foreign_list" else []
                return Reply(200, {"items": items})
            if self.defect == "foreign_read":
                return Reply(200, self.profile)
            return Reply(404, {"detail": {"code": "not_found"}})
        if path == "/v1/products":
            return Reply(200, {"items": [{"id": "product-owned", "project_ids": ["project-owned"]}]})
        if path.endswith("/changes"):
            return Reply(200, {"items": self.history})
        return Reply(200, self.profile)

    def patch(self, path, *, json, headers):
        self.calls.append((self.actor, "PATCH", path))
        if self.actor == "revoked-token":
            return Reply(401, {"detail": {"code": "authentication_required"}})
        if self.actor == "foreign-token":
            return Reply(404, {"detail": {"code": "not_found"}})
        if headers["Idempotency-Key"] == "product-edit-original":
            if self.defect == "replay_mutates":
                self.profile["revision"] += 1
                self.history.append({"revision": 3, "tenant_id": "tenant-owned", "product_id": "product-owned"})
            return Reply(200, self.profile)
        if self.defect == "stale_allowed":
            return Reply(200, self.profile)
        if self.defect == "stale_mutates":
            self.profile["product"]["purpose"] = json["purpose"]
        return Reply(412, {"detail": {"code": "stale_revision", "conflict_paths": ["/product/name"]}})


def exercise(fixture, *, expected_product_id="product-owned"):
    return check_denials(
        fixture.client, tenant_id="tenant-owned", other_tenant_id="tenant-foreign",
        owner_token="owner-token", foreign_token="foreign-token", revoked_token="revoked-token",
        product_id="product-owned", project_id="project-owned", edit_key="product-edit-original",
        edited_fields=fixture.edited_fields, expected_product_id=expected_product_id,
    )


def test_denial_oracle_preserves_owner_profile_and_history():
    fixture = DenialAPI()
    assert exercise(fixture) == {"product_id": "product-owned", "revision": 2, "history_count": 2}
    assert ("foreign-token", "GET", "/v1/products/product-owned/projects/project-owned/revisions/1") in fixture.calls
    assert ("revoked-token", "GET", "/v1/products") in fixture.calls
    assert len([call for call in fixture.calls if call[1] == "PATCH"]) == 5


@pytest.mark.parametrize("defect", ["stale_allowed", "stale_mutates", "foreign_read", "foreign_list",
                                     "revoked_disclosure", "replay_mutates"])
def test_denial_oracle_refuses_disclosure_or_mutation(defect):
    with pytest.raises(AssertionError):
        exercise(DenialAPI(defect=defect))


def test_deliberately_wrong_product_id_fails_before_access():
    fixture = DenialAPI()
    with pytest.raises(AssertionError):
        exercise(fixture, expected_product_id="deliberately-wrong-product")
    assert fixture.calls == []
