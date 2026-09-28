"""Exact EKS grant operations for the trusted temporary-authority service.

Clients and role identities come from service composition, never request JSON.
These operations create, observe and remove only a journal's pinned grant. No
credential is persisted. Absence requires an explicit provider NotFound response.
"""

from __future__ import annotations

from .errors import BootstrapRefused


def _absent(error):
    response = getattr(error, "response", {})
    return response.get("Error", {}).get("Code") == "ResourceNotFoundException"


def _pages(client, method, key, **kwargs):
    values, seen = [], set()
    while True:
        response = getattr(client, method)(**kwargs)
        items = response.get(key)
        if not isinstance(items, list):
            raise BootstrapRefused("EKS authority enumeration was not answered")
        values.extend(items)
        token = response.get("nextToken")
        if not token:
            return values
        if token in seen:
            raise BootstrapRefused("EKS authority pagination did not advance")
        seen.add(token)
        kwargs["nextToken"] = token


class EksGrants:
    def __init__(self, client, target, *, entry_client):
        self.client = client
        self.target = target
        # The trusted broker narrows deletion credentials to the immutable ARN.
        # EKS delete takes a principal alias; a read/compare cannot exclude an
        # entry replacement between that read and the provider mutation.
        self.entry_client = entry_client

    def _args(self, spec):
        if spec.get("cluster_arn") != self.target.cluster_arn:
            raise BootstrapRefused("EKS grant targets a different cluster")
        principal = spec.get("principal_arn", "")
        if not principal.startswith(f"arn:aws:iam::{self.target.account_id}:role/"):
            raise BootstrapRefused("EKS grant principal is not a target-account role")
        if not spec.get("generation"):
            raise BootstrapRefused("EKS grant lacks its reservation generation")
        return {"clusterName": self.target.cluster_name, "principalArn": principal}

    def _entry(self, spec):
        try:
            value = self.client.describe_access_entry(**self._args(spec)).get(
                "accessEntry"
            )
        except Exception as exc:
            if _absent(exc):
                return None
            raise
        if not isinstance(value, dict):
            raise BootstrapRefused("EKS access entry was not answered")
        return value

    def _entry_identity(self, spec, entry):
        arn = entry.get("accessEntryArn", "")
        prefix = self.target.cluster_arn.replace(":cluster/", ":access-entry/") + "/"
        if (
            entry.get("principalArn") != spec["principal_arn"]
            or entry.get("clusterName") != self.target.cluster_name
            or entry.get("type") != "STANDARD"
            or not arn.startswith(prefix)
            or len(arn[len(prefix) :].split("/")) < 4
        ):
            raise BootstrapRefused("EKS access entry immutable identity differs")
        return {
            "arn": arn,
            "generation": entry.get("tags", {}).get("superplane-generation"),
            "groups": sorted(entry.get("kubernetesGroups", [])),
            "username": entry.get("username"),
        }

    def observe(self, spec):
        entry = self._entry(spec)
        if entry is None:
            return None
        identity = self._entry_identity(spec, entry)
        if spec["kind"] == "eks-entry":
            return identity
        if spec["kind"] != "eks-policy":
            raise BootstrapRefused("unknown temporary EKS grant kind")
        matches = [
            p
            for p in _pages(
                self.client,
                "list_associated_access_policies",
                "associatedAccessPolicies",
                **self._args(spec),
            )
            if p.get("policyArn") == spec["policy_arn"]
        ]
        if not matches:
            return None
        if len(matches) != 1 or not matches[0].get("associatedAt"):
            raise BootstrapRefused("EKS policy association identity is ambiguous")
        policy = matches[0]
        return {
            **identity,
            "policy_arn": policy["policyArn"],
            "scope": policy.get("accessScope"),
            "associated_at": str(policy["associatedAt"]),
        }

    def verify(self, spec, identity):
        if (
            identity.get("generation") != spec["generation"]
            or identity.get("groups") != sorted(spec["groups"])
            or identity.get("username") != spec["username"]
        ):
            raise BootstrapRefused("EKS grant is not owned by this generation")
        if spec["kind"] == "eks-policy" and (
            identity.get("policy_arn") != spec["policy_arn"]
            or identity.get("scope") != spec["scope"]
        ):
            raise BootstrapRefused("EKS policy association has different authority")

    def create(self, spec):
        args = self._args(spec)
        if spec["kind"] == "eks-entry":
            # Never update an existing entry or treat AlreadyExists as successful.
            response = self.client.create_access_entry(
                **args,
                type="STANDARD",
                **({"kubernetesGroups": spec["groups"]} if spec["groups"] else {}),
                username=spec["username"],
                clientRequestToken=spec["client_token"],
                tags={
                    "superplane-generation": spec["generation"],
                    "OrgId": self.target.org_id,
                    "WorkspaceId": self.target.workspace_id,
                },
            )
            identity = self._entry_identity(spec, response.get("accessEntry", {}))
        elif spec["kind"] == "eks-policy":
            # Policy association cannot be created unless the entry is ours.
            entry = self._entry(spec)
            if entry is None:
                raise BootstrapRefused("EKS policy parent entry is missing")
            self.verify(
                {**spec, "kind": "eks-entry"}, self._entry_identity(spec, entry)
            )
            self.client.associate_access_policy(
                **args, policyArn=spec["policy_arn"], accessScope=spec["scope"]
            )
            identity = self.observe(spec)
            if identity is None:
                raise BootstrapRefused("EKS policy grant is not yet observable")
        else:
            raise BootstrapRefused("unknown temporary EKS grant kind")
        self.verify(spec, identity)
        return identity

    def delete(self, spec, identity):
        # The journal holds the reservation lock. Re-read the immutable identity at
        # the last possible point; a replacement must not inherit deletion authority.
        if self.observe(spec) != identity:
            raise BootstrapRefused("EKS grant changed before revocation")
        self.verify(spec, identity)
        args = self._args(spec)
        scoped = self.entry_client(identity["arn"])
        if spec["kind"] == "eks-entry":
            policies = _pages(
                self.client,
                "list_associated_access_policies",
                "associatedAccessPolicies",
                **args,
            )
            if policies:
                raise BootstrapRefused("EKS entry has residual policy associations")
            scoped.delete_access_entry(**args)
        elif spec["kind"] == "eks-policy":
            scoped.disassociate_access_policy(**args, policyArn=spec["policy_arn"])
        else:
            raise BootstrapRefused("unknown temporary EKS grant kind")
