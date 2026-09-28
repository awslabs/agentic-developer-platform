"""Reconcile bounded credential revisions through the existing member journal."""

from datetime import UTC, datetime, timedelta
import hashlib

from superplane_bootstrap.errors import BootstrapRefused

from .. import membership_credential_journal as journal
from ..member_credentials import (
    CredentialBinding,
    MemberIssuer,
    ProjectionReceipt,
    SecretProjector,
    delegation_specs,
)
from ..member_credentials.binding import canonical
from . import components


def receipt(binding, row):
    if (
        not row.get("content_digest")
        or not row.get("projection_uid")
        or row.get("expires_at") is None
        or not row.get("service_account_uid")
    ):
        return None
    return ProjectionReceipt(
        canonical(binding.metadata(row["service_account_uid"], row["expires_at"])),
        row["content_digest"],
        row["projection_uid"],
        row.get("projection_version") or "pending",
    )


async def credential_rows(connection, member):
    return [
        dict(row)
        for row in await connection.fetch(
            "SELECT k.* FROM membership_credentials k JOIN cluster_memberships m ON m.id=k.membership_id "
            "WHERE m.org_id::text=$1 AND m.workspace_id::text=$2 AND m.cluster_id::text=$3 AND m.generation=$4 "
            "ORDER BY k.revision,k.scope",
            member.org_id,
            member.workspace_id,
            member.cluster_id,
            member.generation,
        )
    ]


class Renewal:
    def __init__(
        self, *, authority, store, authorize, issuer_grants, projector_grants, directory
    ):
        self.authority, self.store, self._authorize = authority, store, authorize
        self.issuer_grants, self.projector_grants, self.directory = (
            issuer_grants,
            projector_grants,
            directory,
        )
        self.issuer = MemberIssuer(
            issuer_grants, self.authorize, audience=authority.document["audience"]
        )

    def authorize(self, binding, action):
        self._authorize(binding, action)
        if action not in {"cleanup", "revoke", "unproject"}:
            from superplane_bootstrap.namespace_admission import NamespaceAdmission

            NamespaceAdmission(
                self.issuer_grants,
                self.authority.cluster_reference(),
                binding.membership.namespace,
                binding.namespace_uid,
            ).verify_policy()
            self._authorize(binding, action)

    def projector(self, scope):
        return SecretProjector(
            self.projector_grants, self.authorize, **self.authority.projection(scope)
        )

    def cleanup(self, binding, row, all_rows):
        fenced = self.store(journal.fence_revocation, binding)
        self.authorize(binding, "cleanup")
        projector = self.projector(binding.scope)
        recorded = receipt(binding, fenced)
        if recorded is not None:
            _, body = projector._read(binding, "unproject")
            key, annotation = projector._names(binding)
            if projector._owned(body, key, annotation, recorded):
                projector.remove(binding, recorded)
            elif not projector._owned(body, key, annotation, None):
                # An old revision may never remove a newer published revision.
                newer = [
                    r
                    for r in all_rows
                    if r["scope"] == binding.scope
                    and r["revision"] != binding.revision
                    and r["state"] in {"issued", "projected", "active"}
                ]
                if not any(
                    projector._owned(
                        body,
                        key,
                        annotation,
                        receipt(
                            CredentialBinding(
                                binding.membership,
                                r["namespace_uid"],
                                r["revision"],
                                r["scope"],
                            ),
                            r,
                        ),
                    )
                    for r in newer
                ):
                    raise BootstrapRefused(
                        "credential projection belongs to an unknown owner"
                    )
        if fenced.get("service_account_uid"):
            self.issuer.revoke(
                binding, service_account_uid=fenced["service_account_uid"]
            )
        components.cleanup(self.store, self.issuer_grants, self.authorize, binding)
        self.store(
            journal.revoked,
            binding,
            service_account_uid=fenced.get("service_account_uid"),
        )

    def reconcile(self, member, namespace_uid, *, retiring=False):
        rows = self.store(credential_rows, member)
        # Finish all old revocations before creating another SA. A crash cannot
        # accumulate an unbounded sequence of still-live credential principals.
        for row in rows:
            if row["state"] == "revoked":
                continue
            binding = CredentialBinding(
                member, row["namespace_uid"], row["revision"], row["scope"]
            )
            if (
                retiring
                or row["state"] == "revoking"
                or (
                    row["expires_at"] is not None
                    and row["expires_at"] <= datetime.now(UTC)
                )
            ):
                self.cleanup(binding, row, rows)
        if retiring:
            return
        rows = self.store(credential_rows, member)
        for scope in ("reader", "mutator"):
            selected = [r for r in rows if r["scope"] == scope]
            active = next((r for r in selected if r["state"] == "active"), None)
            pending = [
                r for r in selected if r["state"] in {"reserved", "issued", "projected"}
            ]
            if len(pending) > 1:
                raise BootstrapRefused(
                    "multiple credential revisions await reconciliation"
                )
            if (
                not pending
                and active
                and active["expires_at"] > datetime.now(UTC) + timedelta(minutes=5)
            ):
                continue
            revision = (
                pending[0]["revision"]
                if pending
                else max((r["revision"] for r in selected), default=0) + 1
            )
            binding = CredentialBinding(member, namespace_uid, revision, scope)
            row = pending[0] if pending else self.store(journal.reserve, binding)
            self.authorize(binding, "issue")
            projector = self.projector(scope)
            if row["state"] == "reserved":
                identities = components.establish(
                    self.store,
                    self.issuer_grants,
                    self.authorize,
                    binding,
                    delegation_specs(binding),
                )
                sa_uid = identities["ServiceAccount"]["uid"]
                self.store(journal.delegated, binding, service_account_uid=sa_uid)
                issued = self.issuer.issue(binding, service_account_uid=sa_uid)
                row = self.store(
                    journal.issued,
                    binding,
                    service_account_uid=sa_uid,
                    expires_at=issued.expires_at,
                )
                digest = hashlib.sha256(
                    issued.kubeconfig(
                        self.issuer_grants.target.certificate_authority_data
                    ).encode()
                ).hexdigest()
                projection = self.authority.projection(scope)
                row = self.store(
                    journal.projection_intent,
                    binding,
                    secret_uid=projection["secret_uid"],
                    namespace=projection["namespace"],
                    namespace_uid=projection["namespace_uid"],
                    secret_name=projection["secret_name"],
                    content_digest=digest,
                )
                previous = (
                    receipt(
                        CredentialBinding(
                            member, active["namespace_uid"], active["revision"], scope
                        ),
                        active,
                    )
                    if active
                    else None
                )
                projected = projector.publish(
                    issued,
                    certificate_authority_data=self.issuer_grants.target.certificate_authority_data,
                    previous=previous,
                )
                row = self.store(
                    journal.projected,
                    binding,
                    secret_uid=projected.secret_uid,
                    resource_version=projected.resource_version,
                    content_digest=projected.content_digest,
                )
            elif row["state"] == "issued":
                recorded = receipt(binding, row)
                if recorded is None:
                    self.cleanup(binding, row, rows)
                    return
                _, body = projector._read(binding, "project")
                key, annotation = projector._names(binding)
                if not projector._owned(body, key, annotation, recorded):
                    # Token bytes deliberately are not retained. Revoke this SA
                    # before attempting a fresh revision; never reissue in place.
                    self.cleanup(binding, row, rows)
                    return
                row = self.store(
                    journal.projected,
                    binding,
                    secret_uid=recorded.secret_uid,
                    resource_version=body["metadata"]["resourceVersion"],
                    content_digest=recorded.content_digest,
                )
            self.authorize(binding, "project")
            from ..shared_credential_verification import verify_projected_credential

            verified = receipt(binding, row)
            verify_projected_credential(
                projector,
                binding,
                verified,
                target=self.issuer_grants.target,
                directory=self.directory,
                verify=lambda: self.authorize(binding, "project"),
                expect_closed=False,
            )
            self.store(
                journal.activate,
                binding,
                service_account_uid=row["service_account_uid"],
                secret_uid=row["projection_uid"],
                resource_version=row["projection_version"],
            )
