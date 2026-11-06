"""Source-only Agent Profile operations using an already verified provider client."""

import hashlib
import json
import re
from dataclasses import dataclass

PILLARS = frozenset({"COST_OPTIMIZATION", "SECURITY", "RESILIENCE", "PERFORMANCE", "OPERATIONAL_EXCELLENCE"})


class ProfileIdentityError(ValueError):
    """Selected authority or native readback does not prove source-only ownership."""


@dataclass(frozen=True)
class ProfileIdentitySelection:
    tenant_id: str
    product_id: str
    project_id: str
    account_id: str
    region: str
    broker_role_arn: str
    execution_role_arn: str
    pillars: tuple[str, ...]

    def scope_fingerprint(self) -> str:
        identity = (self.tenant_id, self.product_id, self.project_id, self.account_id,
                    self.region, self.broker_role_arn, self.execution_role_arn)
        if any(not isinstance(value, str) or not value for value in identity):
            raise ProfileIdentityError("incomplete verified Profile selection")
        if not re.fullmatch(r"[0-9]{12}", self.account_id) or not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", self.region):
            raise ProfileIdentityError("unverified account or Region")
        if not re.fullmatch(rf"arn:aws:iam::{self.account_id}:role/[A-Za-z0-9+=,.@_/-]+", self.execution_role_arn):
            raise ProfileIdentityError("execution role belongs to another account")
        if (not isinstance(self.pillars, tuple) or not 1 <= len(self.pillars) <= 5
                or len(set(self.pillars)) != len(self.pillars) or not set(self.pillars) <= PILLARS):
            raise ProfileIdentityError("unsupported Profile pillars")
        return hashlib.sha256(json.dumps((self.tenant_id, self.product_id, self.project_id,
                                          self.account_id, self.region, self.execution_role_arn,
                                          self.pillars), separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class ProfileSelection(ProfileIdentitySelection):
    role_qualification: dict

    def fingerprint(self) -> str:
        fingerprint = self.scope_fingerprint()
        qualification = self.role_qualification
        if (not isinstance(qualification, dict)
                or qualification.get("account_id") != self.account_id
                or qualification.get("region") != self.region
                or qualification.get("broker_role_arn") != self.broker_role_arn
                or qualification.get("execution_role_arn") != self.execution_role_arn
                or qualification.get("trust") != "qualified"
                or qualification.get("source_permissions") != "qualified"
                or qualification.get("support") != "eligible"):
            raise ProfileIdentityError("source-only role or provider eligibility not qualified")
        return fingerprint


@dataclass(frozen=True)
class ProfileReadback:
    profile_arn: str
    snapshot_hash: str
    state: str
    gaps: tuple[str, ...]
    eligibility: bool | None
    scheduled_eligibility: bool | None
    deletion_protection: bool | None


class ProfileAdapter:
    def __init__(self, client, *, account_id: str, region: str):
        self.client = client
        self.account_id = account_id
        self.region = region

    def _check_target(self, selection: ProfileSelection) -> str:
        fingerprint = selection.fingerprint()
        if (selection.account_id, selection.region) != (self.account_id, self.region):
            raise ProfileIdentityError("selected connection does not match verified account and Region")
        return fingerprint

    @staticmethod
    def _created_name(fingerprint: str, operation_id: str) -> str:
        return f"adp-wa-{fingerprint}-{hashlib.sha256(operation_id.encode()).hexdigest()[:32]}"

    def _read(self, selection: ProfileSelection, profile_arn: str, *, created: bool,
              fingerprint: str | None = None, operation_id: str | None = None) -> ProfileReadback:
        if not isinstance(profile_arn, str) or not re.fullmatch(
                rf"arn:aws:wellarchitected:{re.escape(self.region)}:{self.account_id}:agent-profile/[A-Za-z0-9_-]+",
                profile_arn):
            raise ProfileIdentityError("native Profile belongs to another account or Region")
        profile = self.client.get_agent_profile(profileArn=profile_arn)
        if not isinstance(profile, dict) or profile.get("arn") != profile_arn:
            raise ProfileIdentityError("native Profile readback does not match selected identity")
        tags = profile.get("tags")
        if created and (profile.get("name") != self._created_name(fingerprint, operation_id)
                        or not isinstance(tags, list)
                        or sum(isinstance(tag, dict) and tag.get("key") == "adp:scope"
                               and tag.get("value") == fingerprint for tag in tags) != 1):
            raise ProfileIdentityError("owned Profile does not match durable setup identity")
        contexts = []
        next_token = None
        seen_tokens = set()
        for _ in range(20):
            request = {"profileArn": profile_arn}
            if next_token:
                request["nextToken"] = next_token
            page = self.client.list_agent_contexts(**request)
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                raise ProfileIdentityError("native contexts cannot be verified")
            contexts.extend(page["items"])
            if any(not isinstance(item, dict) or item.get("profileArn") != profile_arn for item in contexts):
                raise ProfileIdentityError("native context belongs to a different Profile")
            next_token = page.get("nextToken")
            if not next_token:
                break
            if not isinstance(next_token, str) or next_token in seen_tokens:
                raise ProfileIdentityError("native context pagination cannot be verified")
            seen_tokens.add(next_token)
        else:
            raise ProfileIdentityError("native context listing is incomplete")
        gaps = []
        if profile.get("aggregationConfiguration") != []:
            gaps.append("resource_discovery_enabled")
        if profile.get("executionRoleArn") != selection.execution_role_arn:
            gaps.append("execution_role_mismatch")
        if (not isinstance(profile.get("pillars"), list)
                or len(profile["pillars"]) != len(selection.pillars)
                or set(profile["pillars"]) != set(selection.pillars)):
            gaps.append("pillars_mismatch")
        if contexts:
            gaps.append("unrequested_native_contexts")
        if profile.get("eligibleForArchitectureGeneration") is not True:
            gaps.append("provider_eligibility_unverified")
        if profile.get("fieldErrors"):
            gaps.append("native_field_errors")
        evidence = {"arn": profile_arn, "aggregation": profile.get("aggregationConfiguration"),
                    "role": profile.get("executionRoleArn"), "pillars": profile.get("pillars"),
                    "contexts": contexts, "eligible": profile.get("eligibleForArchitectureGeneration"),
                    "scheduled": profile.get("eligibleForScheduledGeneration"),
                    "field_errors": profile.get("fieldErrors")}
        snapshot_hash = hashlib.sha256(json.dumps(evidence, sort_keys=True, default=str).encode()).hexdigest()
        return ProfileReadback(profile_arn, snapshot_hash, "ready" if not gaps else "blocked", tuple(gaps),
                               profile.get("eligibleForArchitectureGeneration"),
                               profile.get("eligibleForScheduledGeneration"), profile.get("deletionProtection"))

    def adopt(self, selection: ProfileSelection, profile_arn: str) -> ProfileReadback:
        self._check_target(selection)
        return self._read(selection, profile_arn, created=False)

    def create(self, selection: ProfileSelection, operation_id: str) -> ProfileReadback:
        fingerprint = self._check_target(selection)
        if not isinstance(operation_id, str) or not operation_id:
            raise ProfileIdentityError("durable setup operation required")
        token = hashlib.sha256(f"{fingerprint}:{operation_id}".encode()).hexdigest()
        response = self.client.create_agent_profile(
            name=self._created_name(fingerprint, operation_id), pillars=list(selection.pillars),
            executionRoleArn=selection.execution_role_arn, aggregationConfiguration=[],
            clientToken=token, tags=[{"key": "adp:scope", "value": fingerprint}],
        )
        if not isinstance(response, dict):
            raise ProfileIdentityError("native create response has no identity")
        return self._read(selection, response.get("arn"), created=True,
                          fingerprint=fingerprint, operation_id=operation_id)

    def _list_profiles(self) -> list[dict]:
        items = []
        next_token = None
        seen_tokens = set()
        for _ in range(20):
            request = {"nextToken": next_token} if next_token else {}
            page = self.client.list_agent_profiles(**request)
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                raise ProfileIdentityError("native Profile listing cannot be verified")
            if any(not isinstance(item, dict) for item in page["items"]):
                raise ProfileIdentityError("native Profile listing cannot be verified")
            items.extend(page["items"])
            next_token = page.get("nextToken")
            if not next_token:
                return items
            if not isinstance(next_token, str) or next_token in seen_tokens:
                raise ProfileIdentityError("native Profile listing is incomplete")
            seen_tokens.add(next_token)
        raise ProfileIdentityError("native Profile listing is incomplete")

    def reconcile(self, selection: ProfileSelection, operation_id: str,
                  candidate_id: str | None) -> ProfileReadback | None:
        from botocore.exceptions import ClientError

        fingerprint = self._check_target(selection)
        name = self._created_name(fingerprint, operation_id)
        matches = []
        for item in self._list_profiles():
            if item.get("name") != name:
                continue
            try:
                matches.append(self._read(selection, item.get("arn"), created=True,
                                          fingerprint=fingerprint, operation_id=operation_id))
            except ProfileIdentityError:
                continue
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                    raise
        if len(matches) != 1 or (candidate_id is not None and matches[0].profile_arn != candidate_id):
            return None
        return matches[0]

    def absent(self, profile_arn: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.get_agent_profile(profileArn=profile_arn)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise
        else:
            return False
        return all(item.get("arn") != profile_arn for item in self._list_profiles())


class ProfileBindingProvider:
    """Adapt read-only adoption and receipt-backed creation to shared setup work."""

    def __init__(self, client, selection: ProfileSelection, connection_ref: dict):
        self.selection = selection
        self.connection_ref = connection_ref
        self.adapter = ProfileAdapter(client, account_id=selection.account_id, region=selection.region)

    def _identity(self, verified):
        self.adapter._check_target(self.selection)
        if ((verified["account_id"], verified["region"], verified["role_arn"], verified["connection_ref"])
                != (self.selection.account_id, self.selection.region,
                    self.selection.broker_role_arn, self.connection_ref)):
            raise ProfileIdentityError("current selected connection or role changed")
        return {"account_id": self.selection.account_id, "region": self.selection.region,
                "role_arn": self.selection.broker_role_arn,
                "execution_role_arn": self.selection.execution_role_arn}

    def _result(self, readback, reference, verified):
        gaps = list(readback.gaps)
        if reference["record_revision"] != readback.snapshot_hash:
            gaps.append("native_profile_drift")
        return {"native_resource_ref": reference, "provider_identity": self._identity(verified),
                "state": "blocked" if gaps else "ready", "gaps": gaps,
                "readiness": [{"snapshot_hash": readback.snapshot_hash,
                               "eligible": readback.eligibility,
                               "scheduled_eligibility": readback.scheduled_eligibility}]}

    async def read(self, reference, verified):
        import asyncio

        self._identity(verified)
        if reference.get("record_type") != "agent_profile" or reference.get("source_connection_id") != self.connection_ref["record_id"]:
            raise ProfileIdentityError("native Profile reference is not from selected connection")
        observed = await asyncio.to_thread(self.adapter.adopt, self.selection, reference["record_id"])
        return self._result(observed, reference, verified)

    def _created_result(self, intent, verified, observed):
        identity = self._identity(verified)
        reference = {"tenant_id": intent.tenant_id, "project_id": intent.project_id,
                     "source_connection_id": self.connection_ref["record_id"],
                     "record_type": "agent_profile", "record_id": observed.profile_arn,
                     "record_revision": observed.snapshot_hash}
        result = self._result(observed, reference, verified)
        result["creation_receipt"] = {"intent_hash": intent.intent_hash,
                                      "operation_id": intent.operation_id,
                                      "native_resource_ref": reference,
                                      "provider_identity": identity}
        return result

    async def create(self, intent, verified):
        import asyncio

        self._identity(verified)
        observed = await asyncio.to_thread(self.adapter.create, self.selection, intent.operation_id)
        return self._created_result(intent, verified, observed)

    async def reconcile(self, intent, verified, candidate_id):
        import asyncio

        self._identity(verified)
        observed = await asyncio.to_thread(self.adapter.reconcile, self.selection, intent.operation_id, candidate_id)
        if observed is None:
            return None
        return self._created_result(intent, verified, observed)

    def _cleanup(self, reference, receipt):
        from botocore.exceptions import ClientError

        profile_arn = reference["record_id"]
        try:
            if self.adapter.absent(profile_arn):
                return True
        except ClientError:
            raise ProfileIdentityError("native_cleanup_readback_unavailable") from None
        try:
            observed = self.adapter.reconcile(self.selection, receipt["operation_id"], profile_arn)
            if observed is None or observed.state != "ready" or observed.snapshot_hash != reference["record_revision"]:
                raise ProfileIdentityError("native_owned_identity_unconfirmed")
            current = self.adapter.client.get_agent_profile(profileArn=profile_arn)
            if current.get("deletionProtection") is not False:
                raise ProfileIdentityError("native_deletion_protected")
            self.adapter.client.delete_agent_profile(profileArn=profile_arn)
            return self.adapter.absent(profile_arn)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "AccessDeniedException":
                raise ProfileIdentityError("native_cleanup_denied") from None
            raise ProfileIdentityError("native_cleanup_readback_unavailable") from None

    async def cleanup(self, reference, verified, receipt):
        import asyncio

        identity = self._identity(verified)
        if (not isinstance(receipt, dict) or receipt.get("provider_identity") != identity
                or receipt.get("native_resource_ref") != reference
                or not isinstance(receipt.get("operation_id"), str) or not receipt["operation_id"]
                or reference.get("record_type") != "agent_profile"
                or reference.get("source_connection_id") != self.connection_ref["record_id"]):
            raise ProfileIdentityError("ownership_required")
        return await asyncio.to_thread(self._cleanup, reference, receipt)


class ProfileCleanupProvider:
    """Cleanup-only driver; retained identity is never source-role qualification."""

    def __init__(self, client, selection: ProfileIdentitySelection, connection_ref: dict, owned: dict):
        self.selection = selection
        self.connection_ref = connection_ref
        self.owned = owned
        self.fingerprint = selection.scope_fingerprint()
        self.adapter = ProfileAdapter(client, account_id=selection.account_id, region=selection.region)

    def _cleanup(self, reference, receipt):
        from botocore.exceptions import ClientError

        arn = reference["record_id"]
        try:
            if self.adapter.absent(arn):
                return True
            name = self.adapter._created_name(self.fingerprint, receipt["operation_id"])
            matches = [item for item in self.adapter._list_profiles() if item.get("name") == name]
            if len(matches) != 1 or matches[0].get("arn") != arn:
                raise ProfileIdentityError("native_owned_identity_unconfirmed")
            observed = self.adapter._read(self.selection, arn, created=True,
                                          fingerprint=self.fingerprint, operation_id=receipt["operation_id"])
            if observed.state != "ready" or observed.snapshot_hash != reference["record_revision"]:
                raise ProfileIdentityError("native_owned_identity_unconfirmed")
            if observed.deletion_protection is not False:
                raise ProfileIdentityError("native_deletion_protected")
            self.adapter.client.delete_agent_profile(profileArn=arn)
            return self.adapter.absent(arn)
        except ClientError as error:
            code = "native_cleanup_denied" if error.response.get("Error", {}).get("Code") == "AccessDeniedException" else "native_cleanup_readback_unavailable"
            raise ProfileIdentityError(code) from None

    async def cleanup(self, reference, verified, receipt):
        import asyncio

        identity = {"account_id": self.selection.account_id, "region": self.selection.region,
                    "role_arn": self.selection.broker_role_arn, "execution_role_arn": self.selection.execution_role_arn}
        if (verified.get("authorization", {}).get("control_action") != "cleanup"
                or verified.get("owned_cleanup") != self.owned
                or self.owned.get("native_resource_ref") != reference
                or self.owned.get("creation_receipt") != receipt
                or self.owned.get("provider_identity") != identity
                or not isinstance(receipt, dict) or receipt.get("provider_identity") != identity
                or receipt.get("native_resource_ref") != reference
                or not isinstance(receipt.get("operation_id"), str) or not receipt["operation_id"]
                or reference.get("record_type") != "agent_profile"
                or reference.get("source_connection_id") != self.connection_ref["record_id"]
                or verified.get("connection_ref") != self.connection_ref
                or any(verified.get(field) != identity[field] for field in ("account_id", "region", "role_arn"))):
            raise ProfileIdentityError("ownership_required")
        return await asyncio.to_thread(self._cleanup, reference, receipt)
