"""Adapters for canonical membership and V01 owning-service retirement."""

import json
import re
from urllib.parse import quote

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


def checked_json(response, statuses=(200,)):
    if response.status not in statuses:
        raise RuntimeError(f"Protected Product operation refused: HTTP {response.status}")
    return response.json()


class Membership:
    def __init__(self, request, base_url, control, binding, *, baseline_key="membership", restore_key="restore_required", expected_team_id=""):
        self.request, self.base_url, self.binding = request, base_url, binding
        self.baseline_key, self.restore_key, self.expected_team_id = baseline_key, restore_key, expected_team_id
        self.headers = {"Authorization": "Bearer " + control["access_token"]}
        self.org_path = base_url + "/api/admin/organizations/" + quote(binding["tenant_id"], safe="")
        self.path = self.org_path + "/hierarchy/member/" + quote(binding["actor_id"], safe="")

    def read(self):
        value = checked_json(self.request.get(self.path, headers=self.headers, timeout=30_000))
        if (value.get("org_id") != self.binding["tenant_id"] or value.get("id") != self.binding["actor_id"]
                or value.get("kind") != "member" or not re.fullmatch(r"[a-f0-9]{64}", value.get("revision", ""))):
            raise ValueError("membership observation differs from the protected fixture")
        return value

    @staticmethod
    def shape(value):
        return {key: item for key, item in value["resource"].items() if key not in ("updated_at", "membership_status")}

    def baseline(self):
        value = self.read()
        resource = value["resource"]
        teams = resource.get("teams")
        expected = ([{"team_id": self.expected_team_id, "role": "member", "is_primary": True}]
                    if self.expected_team_id else [])
        if (resource.get("membership_status") != "active" or resource.get("role") != "member"
                or resource.get("team_id") != self.expected_team_id or teams != expected):
            raise ValueError("browser fixture membership differs from its ordinary team baseline")
        return value

    def revoke(self, state):
        before = self.read()
        if self.shape(before) != self.shape(state.document[self.baseline_key]) or before["resource"]["membership_status"] != "active":
            raise ValueError("fixture membership changed before revocation")
        state.save({**state.document, self.restore_key: True})
        checked_json(self.request.delete(self.path + "?expected_revision=" + before["revision"],
                                         headers=self.headers, timeout=30_000))
        after = self.read()
        if self.shape(after) != self.shape(before) or after["resource"]["membership_status"] != "revoked":
            raise RuntimeError("fixture revocation was not independently observed")

    def restore(self, state):
        if not state.document.get(self.restore_key):
            return
        before = self.read()
        if self.shape(before) != self.shape(state.document[self.baseline_key]):
            raise ValueError("refusing to overwrite an externally changed fixture membership")
        status = before["resource"]["membership_status"]
        if status == "revoked":
            restored = checked_json(self.request.post(
                self.org_path + "/members", headers=self.headers,
                data={"user_id": self.binding["actor_id"], "role": "member"}, timeout=30_000,
            ), (201,))
            if restored.get("id") != self.binding["actor_id"] or restored.get("org_id") != self.binding["tenant_id"]:
                raise ValueError("membership restoration returned a different person")
        elif status != "active":
            raise ValueError("unknown fixture membership status")
        after = self.baseline()
        if self.shape(after) != self.shape(state.document[self.baseline_key]):
            raise RuntimeError("fixture membership baseline was not restored")
        state.save({**state.document, self.restore_key: False})


class ProtectedRetirement:
    def __init__(self, request, session, *, account, region, role_arn, checks_role, origin):
        if (not re.fullmatch(rf"arn:aws:iam::{re.escape(account)}:role/[A-Za-z0-9_+=,.@/-]+", role_arn)
                or role_arn == checks_role or not re.fullmatch(
                    rf"https://[a-z0-9]+\.execute-api\.{re.escape(region)}\.amazonaws\.com/[A-Za-z0-9_-]+", origin)):
            raise ValueError("retirement requires the exact separate evaluator role and regional internal origin")
        self.request, self.session, self.region, self.origin = request, session, region, origin
        role_name = role_arn.rsplit("/", 1)[-1]
        identity = session.client("sts").get_caller_identity()
        if identity.get("Account") != account or not re.fullmatch(
            rf"arn:aws:sts::{re.escape(account)}:assumed-role/{re.escape(role_name)}/[^/]+", identity.get("Arn", "")
        ):
            raise ValueError("retirement is not using its selected evaluator role")

    def __call__(self, manifest):
        request = AWSRequest(
            method="POST", url=self.origin + "/internal/v1/product-evaluation/retire",
            data=json.dumps(manifest).encode(), headers={"Content-Type": "application/json"},
        )
        SigV4Auth(self.session.get_credentials().get_frozen_credentials(), "execute-api", self.region).add_auth(request)
        return checked_json(self.request.post(request.url, data=request.body, headers=dict(request.headers), timeout=45_000))
