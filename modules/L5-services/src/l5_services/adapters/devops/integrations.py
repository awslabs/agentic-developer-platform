"""Resolve project-selected integrations against scoped Product and provider readbacks."""

import asyncio
from urllib.parse import urlsplit


class IntegrationAccessDenied(ValueError):
    """Selected source identity or independent content access was not verified."""


def github_repository_configuration(reference, repository):
    if not isinstance(repository, dict) or not isinstance(repository.get("owner"), dict):
        raise IntegrationAccessDenied("native_repository_unverified")
    owner = repository["owner"].get("login")
    owner_type = repository["owner"].get("type")
    name = repository.get("name")
    repository_id = repository.get("id")
    if (not isinstance(owner, str) or not owner or not isinstance(name, str) or not name
            or owner_type not in {"Organization", "User"}
            or type(repository_id) not in {str, int} or not str(repository_id).isascii()
            or not str(repository_id).isdigit() or int(repository_id) <= 0
            or reference.get("record_id") not in {str(repository_id), f"{owner}/{name}"}
            or repository.get("full_name") != f"{owner}/{name}"):
        raise IntegrationAccessDenied("native_repository_unverified")
    try:
        address = urlsplit(repository.get("html_url", ""))
        valid = (address.scheme == "https" and address.hostname and not address.username
                 and not address.password and address.port in (None, 443)
                 and not address.query and not address.fragment and address.path == f"/{owner}/{name}")
    except (TypeError, ValueError):
        valid = False
    if not valid:
        raise IntegrationAccessDenied("native_repository_unverified")
    return {"repoName": name, "repoId": str(repository_id), "owner": owner,
            "ownerType": owner_type.lower(), "instanceIdentifier": address.hostname}


class IntegrationAdapter:
    def __init__(self, product_client, github_reader, cloudwatch_reader):
        self.product = product_client
        self.github = github_reader
        self.cloudwatch = cloudwatch_reader

    async def read(self, tenant_id: str, product_id: str, project_id: str,
                   integrations: list[dict]) -> list[dict]:
        sources = await self.product.list_sources(product_id, project_id)
        bindings = await self.product.list_bindings(product_id, project_id)
        if (sources.get("coverage") != "complete" or sources.get("next_cursor") is not None
                or bindings.get("next_cursor") is not None
                or not isinstance(sources.get("items"), list)
                or not isinstance(bindings.get("items"), list)):
            raise IntegrationAccessDenied("product_source_discovery_incomplete")

        def selected_ref(reference, connection_id, kind):
            if (not isinstance(reference, dict)
                    or reference.get("tenant_id") != tenant_id
                    or reference.get("project_id") != project_id
                    or reference.get("source_connection_id") != connection_id
                    or reference.get("record_type") != kind
                    or not reference.get("record_id") or not reference.get("record_revision")):
                raise IntegrationAccessDenied("source_outside_selected_project")

        def binding_for(reference, kind, connection_id):
            matches = [binding for binding in bindings["items"]
                       if binding.get("status") == "healthy"
                       and binding.get("project_id") == project_id
                       and (binding.get("selection") or {}).get("kind") == kind
                       and (binding.get("selection") or {}).get("native_id") == reference["record_id"]
                       and (binding.get("selection") or {}).get("connection_ref", {}).get("id") == connection_id
                       and binding.get("source_revision") == reference["record_revision"]]
            if len(matches) != 1:
                raise IntegrationAccessDenied("selected_binding_not_verified")
            return matches[0]["selection"]

        result = []
        for integration in integrations:
            kind = integration.get("kind")
            connection = integration.get("connection_ref") or {}
            connection_id = connection.get("record_id")
            selected_ref(connection, connection_id, "connection")
            matches = [source for source in sources["items"]
                       if source.get("provider") == ("github" if kind == "github" else "aws")
                       and (source.get("connection_ref") or {}).get("id") == connection_id]
            if len(matches) != 1:
                raise IntegrationAccessDenied("selected_connection_unavailable")
            source = matches[0]
            if source.get("coverage") in {"denied", "unavailable"}:
                raise IntegrationAccessDenied("selected_connection_unavailable")
            allowed = source.get("resources") or []
            if kind == "github":
                if not integration.get("repository_refs"):
                    raise IntegrationAccessDenied("selected_repositories_missing")
                repositories = {}
                for reference in integration["repository_refs"]:
                    selected_ref(reference, connection_id, "repository")
                    if reference not in allowed:
                        raise IntegrationAccessDenied("repository_not_authorized")
                    selection = binding_for(reference, "repository", connection_id)
                    observed = await self.github.read_repository(connection, selection["native_id"],
                                                                  reference["record_revision"])
                    if (observed.get("repository_id") != reference["record_id"]
                            or observed.get("revision") != reference["record_revision"]
                            or observed.get("content_readable") is not True):
                        raise IntegrationAccessDenied("repository_readback_mismatch")
                    repositories[reference["record_id"]] = {
                        "source_ref": reference, "content_revision": observed["revision"],
                        "content_readable": True,
                        **({"native_configuration": github_repository_configuration(reference, observed["repository"])}
                           if "repository" in observed else {}),
                    }
                workflows = []
                for reference in integration.get("workflow_refs", []):
                    selected_ref(reference, connection_id, "pipeline")
                    if reference not in allowed:
                        raise IntegrationAccessDenied("workflow_not_authorized")
                    selection = binding_for(reference, "pipeline", connection_id)
                    repository_id = selection.get("repository_id")
                    if repository_id not in repositories:
                        raise IntegrationAccessDenied("workflow_repository_not_selected")
                    observed = await self.github.read_workflow(connection, repository_id,
                                                                reference["record_id"], reference["record_revision"])
                    if (observed.get("repository_id") != repository_id
                            or observed.get("workflow_id") != reference["record_id"]
                            or observed.get("source_revision") != reference["record_revision"]
                            or not observed.get("deployment_revision")
                            or observed.get("deployment_readable") is not True):
                        raise IntegrationAccessDenied("workflow_readback_mismatch")
                    workflows.append({"source_ref": reference, "repository_id": repository_id,
                                      "deployment_revision": observed["deployment_revision"],
                                      "deployment_readable": True})
                result.append({"version": 1, "kind": kind, "connection_ref": connection,
                               "repositories": list(repositories.values()), "workflows": workflows})
            elif kind == "cloudwatch":
                if not integration.get("resource_refs") or not allowed:
                    raise IntegrationAccessDenied("selected_telemetry_missing")
                resources = []
                for reference in integration["resource_refs"]:
                    selected_ref(reference, connection_id, "telemetry")
                    observed = await self.cloudwatch.read_resource(connection, reference["record_id"],
                                                                    reference["record_revision"])
                    if (observed.get("resource_id") != reference["record_id"]
                            or observed.get("revision") != reference["record_revision"]
                            or observed.get("events_readable") is not True):
                        raise IntegrationAccessDenied("telemetry_readback_mismatch")
                    resources.append({"source_ref": reference, "telemetry_revision": observed["revision"],
                                      "events_readable": True})
                result.append({"version": 1, "kind": kind, "connection_ref": connection,
                               "resources": resources})
            else:
                raise IntegrationAccessDenied("unsupported_integration_kind")
        return result

    async def reconcile(self, tenant_id, product_id, project_id, integrations, space_id,
                        operation_id, native, *, reconcile_only=False, allow_registration_create=False):
        observations = await self.read(tenant_id, product_id, project_id, integrations)
        association_adapter = NativeIntegrationAssociation(native)
        receipts = []
        for integration, observation in zip(integrations, observations, strict=True):
            if integration.get("kind") != "github":
                raise IntegrationAccessDenied("unsupported_native_registration_kind")
            for repository in observation["repositories"]:
                receipts.append(await asyncio.to_thread(
                    association_adapter.reconcile, space_id, integration, operation_id,
                    repository=repository, reconcile_only=reconcile_only,
                    allow_registration_create=allow_registration_create))
        return receipts


class NativeIntegrationAssociation:
    disabled_capabilities = {"RELEASE_READINESS_REVIEW": {"enabled": False},
                             "RELEASE_READINESS_REVIEW_AUTOMATED_TESTING": {"enabled": False}}

    def __init__(self, native):
        self.native = native

    @staticmethod
    def _pages(method, field, **arguments):
        results = []
        token = None
        visited = set()
        while True:
            response = method(**arguments, **({"nextToken": token} if token else {}))
            if (not isinstance(response, dict) or not isinstance(response.get(field), list)
                    or any(not isinstance(item, dict) for item in response[field])):
                raise IntegrationAccessDenied("native_discovery_incomplete")
            results.extend(response[field])
            token = response.get("nextToken")
            if not token:
                return results
            if not isinstance(token, str) or token in visited or len(visited) >= 99:
                raise IntegrationAccessDenied("native_discovery_incomplete")
            visited.add(token)

    @staticmethod
    def _service_matches(service, repository):
        if not isinstance(service, dict) or service.get("serviceType") != "github":
            return False
        details = service.get("additionalServiceDetails")
        github = details.get("github") if isinstance(details, dict) else None
        if (not isinstance(github, dict) or github.get("owner") != repository["owner"]
                or github.get("ownerType") != repository["ownerType"]):
            return False
        if not github.get("targetUrl"):
            return repository["instanceIdentifier"] == "github.com"
        try:
            address = urlsplit(github["targetUrl"])
            return (address.scheme == "https" and address.hostname == repository["instanceIdentifier"]
                    and not address.username and not address.password and address.port in (None, 443)
                    and address.path in ("", "/") and not address.query and not address.fragment)
        except (TypeError, ValueError):
            return False

    def reconcile(self, space_id, selection, operation_id, *, reconcile_only=False,
                  allow_registration_create=False, repository=None):
        kind = selection.get("kind")
        connection = selection.get("connection_ref")
        if kind != "github":
            raise IntegrationAccessDenied("unsupported_native_registration_kind")
        refs = (selection.get("repository_refs") or []) + (selection.get("workflow_refs") or [])
        if (not isinstance(connection, dict)
                or not connection.get("record_id") or not space_id or not operation_id or not refs):
            raise IntegrationAccessDenied("native_selection_incomplete")
        github = repository.get("native_configuration") if isinstance(repository, dict) else None
        if (not isinstance(github, dict)
                or set(github) != {"repoName", "repoId", "owner", "ownerType", "instanceIdentifier"}
                or any(not isinstance(value, str) or not value for value in github.values())
                or repository.get("source_ref") not in selection.get("repository_refs", [])
                or repository.get("content_readable") is not True
                or repository.get("content_revision") != repository["source_ref"].get("record_revision")):
            raise IntegrationAccessDenied("native_repository_unverified")
        registrations = self._pages(self.native.list_services, "services",
                                    filterServiceType="github", maxResults=100)
        matches = [item for item in registrations if self._service_matches(item, github)]
        if len(matches) > 1:
            raise IntegrationAccessDenied("duplicate_native_registrations")
        if not matches:
            raise IntegrationAccessDenied("native_registration_requires_consent")
        registration_id = matches[0].get("serviceId")
        if not isinstance(registration_id, str) or not registration_id:
            raise IntegrationAccessDenied("native_registration_identity_unverified")
        observed_registration = self.native.get_service(serviceId=registration_id).get("service") or {}
        if (observed_registration.get("serviceId") != registration_id
                or not self._service_matches(observed_registration, github)):
            raise IntegrationAccessDenied("native_registration_readback_mismatch")

        configuration = {"github": github}
        associations = self._pages(self.native.list_associations, "associations", agentSpaceId=space_id, maxResults=100)
        matches = [item for item in associations if item.get("serviceId") == registration_id
                   and isinstance(item.get("configuration"), dict)
                   and isinstance(item["configuration"].get("github"), dict)
                   and (item["configuration"]["github"].get("repoId") == github["repoId"]
                        or (item["configuration"]["github"].get("owner"), item["configuration"]["github"].get("repoName"))
                        == (github["owner"], github["repoName"]))]
        if len(matches) > 1:
            raise IntegrationAccessDenied("duplicate_native_associations")
        association_ownership = "shared"
        if matches:
            if matches[0].get("agentSpaceId") != space_id or matches[0].get("configuration") != configuration:
                raise IntegrationAccessDenied("native_association_scope_conflict")
            association_id = matches[0].get("associationId")
        else:
            if reconcile_only:
                raise IntegrationAccessDenied("association_outcome_unknown")
            from botocore.exceptions import BotoCoreError, ClientError

            try:
                created = self.native.associate_service(agentSpaceId=space_id, serviceId=registration_id,
                                                        configuration=configuration,
                                                        capabilities=self.disabled_capabilities)
            except (BotoCoreError, ClientError, ConnectionError, TimeoutError) as exc:
                raise IntegrationAccessDenied("association_outcome_unknown") from exc
            association_id = (created.get("association") or {}).get("associationId")
            association_ownership = "adp_created"
        if not association_id:
            raise IntegrationAccessDenied("association_outcome_unknown")
        observed = self.native.get_association(agentSpaceId=space_id, associationId=association_id)
        association = observed.get("association") or {}
        if (association.get("associationId") != association_id
                or association.get("agentSpaceId") != space_id
                or association.get("serviceId") != registration_id
                or association.get("configuration") != configuration
                or association.get("status") != "valid"):
            raise IntegrationAccessDenied("native_association_readback_mismatch")
        capabilities = association.get("capabilities")
        if association_ownership == "adp_created" and (
                not isinstance(capabilities, dict) or set(capabilities) != set(self.disabled_capabilities)
                or any(not isinstance(value, dict) or value.get("enabled") is not False for value in capabilities.values())):
            raise IntegrationAccessDenied("native_automation_unverified")
        return {"status": "ready", "kind": kind, "space_id": space_id,
                "registration_id": registration_id, "registration_ownership": "shared",
                "association_id": association_id, "association_ownership": association_ownership,
                "configuration": configuration, "connection_ref": connection,
                "source_ref": repository["source_ref"]}

    def cleanup(self, receipt):
        if receipt.get("association_ownership") != "adp_created":
            return "preserved"
        observed = self.native.get_association(agentSpaceId=receipt["space_id"],
                                               associationId=receipt["association_id"]).get("association") or {}
        if (observed.get("associationId") != receipt["association_id"]
                or observed.get("agentSpaceId") != receipt["space_id"]
                or observed.get("serviceId") != receipt["registration_id"]
                or observed.get("configuration") != receipt["configuration"]):
            raise IntegrationAccessDenied("native_association_scope_conflict")
        self.native.disassociate_service(agentSpaceId=receipt["space_id"],
                                         associationId=receipt["association_id"])
        return "association_removed_registration_preserved"
