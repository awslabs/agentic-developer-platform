"""Operation-fenced shared bootstrap credential effects using the domain journal."""

from contextlib import contextmanager
from datetime import datetime
import hashlib

from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.shared_authority import SharedBootstrapServices

from . import membership_credential_journal as journal
from .member_credentials import CredentialBinding, MemberIssuer, SecretProjector
from .member_credentials.binding import canonical
from .member_credentials.issuer import delegation_specs
from .member_credentials.projection import ProjectionReceipt
from .shared_credential_verification import verify_projected_credential
from .shared_membership import verify as verify_membership


class SharedCredentialServices:
    """Only the original authority journal may admit provider effects.

    Reserve/delegation/projection intent commits precede the corresponding I/O.
    All journal calls use the authority's existing SQL connection so membership
    locks and original-claim locks cannot be acquired in conflicting transactions.
    """

    def __init__(
        self,
        installation,
        issuer,
        management,
        bridge,
        directory,
        verify,
        dependencies,
        principals,
    ):
        self.installation, self.issuer_grants, self.management = (
            installation,
            issuer,
            management,
        )
        self.bridge, self.directory, self.verify = bridge, directory, verify
        self.dependencies, self.principals = dependencies, principals
        if not all(callable(value) for value in (verify, dependencies, principals)):
            raise BootstrapRefused("shared credential dependencies are unconfigured")

    def services(self):
        return SharedBootstrapServices(
            self.verify_member,
            self.prepare,
            self.observe,
            self.withdraw,
            self.dependencies,
            self.principals,
        )

    def verify_member(self, membership, *, recovery=False):
        self.verify()

        async def read():
            if self.bridge.connection is not None:
                return await verify_membership(
                    self.bridge.connection, membership, states={"reserved", "active"}
                )
            async with self.bridge.connect() as connection:
                return await verify_membership(
                    connection, membership, states={"reserved", "active"}
                )

        self.bridge.wait(read())

    @contextmanager
    def fenced(self, authority, *, recovery=False, observing=False):
        with authority.journal.fenced(recovery=recovery):
            if authority.journal.store is not self.bridge:
                raise BootstrapRefused(
                    "credential journal differs from bootstrap transaction"
                )
            _, progress = authority.journal.read_locked()
            if recovery:
                if not progress.get("member_recovery_started"):
                    raise BootstrapRefused("credential cleanup was not durably fenced")
            elif observing and progress.get("phase") == "revoked":
                authority._require_openable(progress)
            else:
                authority._require_phase(progress, "active")
            self.verify()
            yield progress

    def tools(self, authority, binding):
        def authorize(actual, action):
            if actual != binding or action not in {
                "issue",
                "project",
                "verify",
                "revoke",
                "unproject",
            }:
                raise BootstrapRefused(
                    "member credential action differs from its admitted binding"
                )
            self.verify()
            self.verify_member(
                binding.membership, recovery=action in {"revoke", "unproject"}
            )

        issuer = MemberIssuer(
            self.issuer_grants,
            authorize,
            audience=self.installation.document["audience"],
        )
        projector = SecretProjector(
            self.management, authorize, **self.installation.projection(binding.scope)
        )
        return issuer, projector

    def row(self, binding):
        rows = self.bridge.execute(
            "SELECT k.* FROM membership_credentials k JOIN cluster_memberships m ON m.id=k.membership_id "
            "WHERE m.org_id::text=:org AND m.workspace_id::text=:workspace AND m.generation=:generation "
            "AND k.namespace_uid=:uid AND k.scope=:scope AND k.revision=:revision",
            {
                "org": binding.membership.org_id,
                "workspace": binding.membership.workspace_id,
                "generation": binding.membership.generation,
                "uid": binding.namespace_uid,
                "scope": binding.scope,
                "revision": binding.revision,
            },
        )
        if len(rows) != 1:
            raise BootstrapRefused("original member credential journal is absent")
        return rows[0]

    @staticmethod
    def receipt(binding, row):
        expiry = row["expires_at"]
        if isinstance(expiry, str):
            expiry = datetime.fromisoformat(expiry)
        return ProjectionReceipt(
            canonical(binding.metadata(row["service_account_uid"], expiry)),
            row["content_digest"],
            row["projection_uid"],
            row["projection_version"] or "unacknowledged-intent",
        )

    def prepare(self, authority, namespace_uid):
        member = authority.backend.membership
        for scope in ("reader", "mutator"):
            with self.fenced(authority) as progress:
                rows = self.bridge.execute(
                    "SELECT COALESCE(max(k.revision),0)+1 AS revision FROM membership_credentials k "
                    "JOIN cluster_memberships m ON m.id=k.membership_id WHERE m.workspace_id::text=:workspace "
                    "AND m.generation=:generation AND k.scope=:scope",
                    {
                        "workspace": member.workspace_id,
                        "generation": member.generation,
                        "scope": scope,
                    },
                )
                binding = CredentialBinding(
                    member, namespace_uid, rows[0]["revision"], scope
                )
                self.bridge.wait(journal.reserve(self.bridge.connection, binding))
                progress.setdefault("member_credentials", {})[scope] = binding.revision
                authority.journal.write_locked(progress)
            records = authority.establish_components(delegation_specs(binding))
            sa_uid = records[0]["identity"]["uid"]
            with self.fenced(authority):
                self.bridge.wait(
                    journal.delegated(
                        self.bridge.connection, binding, service_account_uid=sa_uid
                    )
                )
            issuer, projector = self.tools(authority, binding)
            # The prior block committed SA ownership before TokenRequest.
            # Re-entering the fence rejects recovery winning this durable gap.
            with self.fenced(authority):
                credential = issuer.issue(binding, service_account_uid=sa_uid)
            with self.fenced(authority):
                self.bridge.wait(
                    journal.issued(
                        self.bridge.connection,
                        binding,
                        service_account_uid=sa_uid,
                        expires_at=credential.expires_at,
                    )
                )
                digest = hashlib.sha256(
                    credential.kubeconfig(
                        self.issuer_grants.target.certificate_authority_data
                    ).encode()
                ).hexdigest()
                self.bridge.wait(
                    journal.projection_intent(
                        self.bridge.connection,
                        binding,
                        secret_uid=projector.secret_uid,
                        namespace=projector.namespace,
                        namespace_uid=projector.namespace_uid,
                        secret_name=projector.secret_name,
                        content_digest=digest,
                    )
                )
            # issued + exact content/target intent are committed before Secret I/O.
            # A provider-side write followed by transaction rollback is recoverable.
            with self.fenced(authority):
                receipt = projector.publish(
                    credential,
                    certificate_authority_data=self.issuer_grants.target.certificate_authority_data,
                )
            with self.fenced(authority):
                self.bridge.wait(
                    journal.projected(
                        self.bridge.connection,
                        binding,
                        secret_uid=receipt.secret_uid,
                        resource_version=receipt.resource_version,
                        content_digest=receipt.content_digest,
                    )
                )
        return "membership:" + member.generation

    def observe(self, authority, namespace_uid):
        with self.fenced(authority, observing=True) as progress:
            revisions = progress.get("member_credentials", {})
            if set(revisions) != {"reader", "mutator"}:
                raise BootstrapRefused(
                    "both member credential scopes must be projected"
                )
            proven = []
            for scope, revision in revisions.items():
                binding = CredentialBinding(
                    authority.backend.membership, namespace_uid, revision, scope
                )
                row = self.row(binding)
                if row["state"] not in {"projected", "active"}:
                    raise BootstrapRefused("member credential is no longer projected")
                _, projector = self.tools(authority, binding)
                receipt = self.receipt(binding, row)
                verify_projected_credential(
                    projector,
                    binding,
                    receipt,
                    target=self.issuer_grants.target,
                    directory=self.directory,
                    verify=self.verify,
                    expect_closed=True,
                )
                proven.append((binding, row))
            # The second management observation occurs after normal revocation.
            # Activate both scopes in ONE transaction only after both live proofs.
            if progress.get("phase") == "revoked":
                for binding, row in proven:
                    self.bridge.wait(
                        journal.activate(
                            self.bridge.connection,
                            binding,
                            service_account_uid=row["service_account_uid"],
                            secret_uid=row["projection_uid"],
                            resource_version=row["projection_version"],
                        )
                    )

    def withdraw(self, authority, namespace_uid):
        with self.fenced(authority, recovery=True) as progress:
            bindings = [
                CredentialBinding(
                    authority.backend.membership, namespace_uid, revision, scope
                )
                for scope, revision in progress.get("member_credentials", {}).items()
            ]
            for binding in bindings:
                self.bridge.wait(
                    journal.fence_revocation(self.bridge.connection, binding)
                )
        for binding in bindings:
            with self.fenced(authority, recovery=True):
                row = self.row(binding)
                issuer, projector = self.tools(authority, binding)
                if row["content_digest"]:
                    # Even after lost publication acknowledgement, the durable
                    # digest owns exactly these bytes. remove re-reads actual RV.
                    projector.remove(binding, self.receipt(binding, row))
                if row["service_account_uid"]:
                    issuer.revoke(
                        binding, service_account_uid=row["service_account_uid"]
                    )
        # Handles lost SA-create acknowledgements before delegated() persisted UID.
        authority.remove_owned_components()
        for binding in bindings:
            with self.fenced(authority, recovery=True):
                if self.issuer_grants._get(delegation_specs(binding)[0]) is not None:
                    raise BootstrapRefused("member credential ServiceAccount remains")
                row = self.row(binding)
                self.bridge.wait(
                    journal.revoked(
                        self.bridge.connection,
                        binding,
                        service_account_uid=row["service_account_uid"],
                    )
                )
