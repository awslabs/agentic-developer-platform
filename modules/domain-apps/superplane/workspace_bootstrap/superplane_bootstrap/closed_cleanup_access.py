"""A disposable service-held EKS observer with an AWS-only teardown path.

There are no temporary Kubernetes objects to self-delete. The observer survives
the original registrar's deletion, reads the exact native verification roster,
then its sole EKS policy/entry are removed and independently observed absent.
The managed policy's broad read scope is an explicit installation prerequisite;
it is never called an object-scoped Kubernetes grant.
"""

import copy
import time

from botocore.exceptions import ClientError

from .closed_cleanup_permit import VIEW_POLICY, entry_spec, require
from .eks_grants import _scope


class VerifierAccess:
    def __init__(self, permit, journal, client, observer, revalidate, entry_client):
        self.permit, self.journal = permit, journal
        self.client, self.observer, self.revalidate = client, observer, revalidate
        self.spec = entry_spec(permit)
        self.entry_client = entry_client
        self.args = {k: self.spec[k] for k in ("clusterName", "principalArn")}

    def observed_after_write(self, read, expected):
        # EKS propagates accepted access-entry/policy writes asynchronously.
        # Retry observations only; never repeat the mutation to make it visible.
        for attempt in range(16):
            value = read()
            if value == expected:
                return value
            require(value is None or value == [])
            if attempt < 15:
                time.sleep(2)
        require(False)

    def observe(self):
        self.revalidate()
        try:
            value = self.observer.describe_access_entry(**self.args)["accessEntry"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ResourceNotFoundException":
                return None
            raise
        for key in (
            "clusterName",
            "principalArn",
            "type",
            "username",
            "kubernetesGroups",
            "tags",
        ):
            require(value.get(key) == self.spec[key])
        return value["accessEntryArn"]

    def policies(self):
        self.revalidate()
        result, token = [], None
        while True:
            page = self.observer.list_associated_access_policies(
                **self.args, **({"nextToken": token} if token else {})
            )
            result.extend(page["associatedAccessPolicies"])
            next_token = page.get("nextToken")
            require(next_token is None or next_token != token)
            token = next_token
            if not token:
                return [
                    {
                        "policyArn": x["policyArn"],
                        "accessScope": _scope(x["accessScope"]),
                    }
                    for x in result
                ]

    def acquire(self):
        state = self.journal.read()
        if state["phase"] == "new":
            require(self.observe() is None)
            state = self.journal.append(state, phase="verifier-create-intent")
            self.revalidate()
            answer = self.client.create_access_entry(
                **{k: v for k, v in self.spec.items() if k != "kubernetesGroups"},
                clientRequestToken=self.permit["permit_id"].split(":")[1],
            )
            arn = answer["accessEntry"]["accessEntryArn"]
            state = self.journal.append(
                state, phase="verifier-created", verifier_arn=arn
            )
            self.observed_after_write(self.observe, arn)
        elif state["phase"] == "verifier-create-intent":
            # Reconcile only an observed tagged object. An absent response after
            # a lost create reply is not permission to repeat the create.
            arn = self.observe()
            require(arn is not None)
            state = self.journal.append(
                state, phase="verifier-created", verifier_arn=arn
            )
        require(self.observe() == state.get("verifier_arn"))
        expected = [
            {
                "policyArn": VIEW_POLICY,
                "accessScope": {"type": "cluster"},
            }
        ]
        if state["phase"] == "verifier-created":
            require(self.policies() == [])
            state = self.journal.append(state, phase="verifier-policy-intent")
            self.revalidate()
            self.entry_client(state["verifier_arn"]).associate_access_policy(
                **self.args, policyArn=VIEW_POLICY, accessScope={"type": "cluster"}
            )
            self.observed_after_write(self.policies, expected)
            state = self.journal.append(state, phase="observer-ready")
        elif state["phase"] == "verifier-policy-intent":
            self.observed_after_write(self.policies, expected)
            state = self.journal.append(state, phase="observer-ready")
        require(state["phase"] == "observer-ready" and self.policies() == expected)
        return copy.deepcopy(state)

    def record_verified(self, result):
        state = self.journal.read()
        require(state["phase"] == "observer-ready")
        require(result["revoked"] is True and result["claim_released"] is True)
        require(result["generation"] == self.permit["qualification"]["generation"])
        return self.journal.append(
            state, phase="original-cleanup-verified", result=result
        )

    def teardown(self, *, abandon=False):
        state = self.journal.read()
        if state["phase"] in {"complete", "abandoned"}:
            require(self.observe() is None)
            return state
        require(
            abandon
            or state["phase"]
            in {
                "original-cleanup-verified",
                "verifier-disassociate-intent",
                "verifier-delete-intent",
            }
        )
        observed = self.observe()
        if "verifier_arn" not in state:
            # A create with an unknown outcome can only be removed if the exact
            # permit-tagged object is observed. Never adopt an unrelated entry.
            require(observed is not None or state["phase"] == "new")
            if observed is not None:
                state = self.journal.append(state, verifier_arn=observed)
        else:
            require(observed in {None, state["verifier_arn"]})
        if (
            observed is None
            and "verifier_arn" in state
            and state["phase"] != "verifier-delete-intent"
        ):
            # A newly acknowledged create may not yet be visible. Its absence
            # cannot become a teardown receipt before an actual delete intent.
            self.observed_after_write(self.observe, state["verifier_arn"])
            observed = state["verifier_arn"]
        if observed is not None:
            policies = self.policies()
            require(
                policies
                in (
                    [],
                    [
                        {
                            "policyArn": VIEW_POLICY,
                            "accessScope": {"type": "cluster"},
                        }
                    ],
                )
            )
            if policies:
                state = self.journal.append(state, phase="verifier-disassociate-intent")
                self.revalidate()
                self.entry_client(state["verifier_arn"]).disassociate_access_policy(
                    **self.args, policyArn=VIEW_POLICY
                )
                require(self.policies() == [])
            state = self.journal.append(state, phase="verifier-delete-intent")
            require(self.observe() == state["verifier_arn"])
            self.revalidate()
            self.entry_client(state["verifier_arn"]).delete_access_entry(**self.args)
            require(self.observe() is None)
        state = self.journal.append(state, phase="abandoned" if abandon else "complete")
        require(self.observe() is None)
        return state
