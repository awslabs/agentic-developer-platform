"""Offline packaged reader capability check; no installed authority is loaded."""

from .config import image, require

REPORT = {
    "dedicated_reader_contract_version": 1,
    "provider_health_contract_version": 1,
    "route": "/internal/readers/{workspace_id}",
    "configuration_verified": False,
    "authority_verified": False,
    "production_ready": False,
}

VERIFY_PROGRAM = """
import json
import os
import ssl

# Importing the API router constructs its SQLAlchemy engine. This credential-free
# loopback URL is only an import fixture; no connection or lifespan is started.
os.environ["DATABASE_URL"] = "postgresql+asyncpg://127.0.0.1:1/reader_image_probe"
os.environ["SUPERPLANE_DATABASE_CA"] = ssl.DER_cert_to_PEM_cert(
    ssl.create_default_context().get_ca_certs(binary_form=True)[0]
)
from workspace_provisioning.dedicated_readers.__main__ import main
from workspace_provisioning.dedicated_readers.renewal import Renewal
from workspace_provisioning.dedicated_readers.transports import Registry, Issuer, Projector
from superplane_bootstrap.grant_plan import BootstrapRelease
from app.routers.dedicated_readers import native_identity, reader_target, router
from app.endpoint_inventory import INTERNAL_ROUTES
from app.management import MANAGEMENT_ROUTES
from app.services.provider_health import installed_skypilot_health, project_provider_health
from app.skypilot_proxy import health_token
from kubernetes import client
import boto3
import httpx

route = ("GET", "/internal/readers/{workspace_id}")
def check(condition):
    if not condition:
        raise RuntimeError("Packaged dedicated reader contract differs")

check(callable(main) and callable(native_identity))
check(callable(installed_skypilot_health) and callable(project_provider_health) and callable(health_token))
check("reader_renewal" in BootstrapRelease.__dataclass_fields__)
for owner, methods in (
    (Renewal, ("reconcile",)),
    (Registry, ("read",)),
    (Issuer, ("issue", "verify_reader")),
    (Projector, ("read", "replace")),
):
    check(all(callable(getattr(owner, method, None)) for method in methods))
check(route in INTERNAL_ROUTES and route in MANAGEMENT_ROUTES)
check(any(r.path == route[1] and route[0] in r.methods and r.endpoint is reader_target for r in router.routes))
print(json.dumps({
    "dedicated_reader_contract_version": 1,
    "provider_health_contract_version": 1,
    "route": route[1],
    "configuration_verified": False,
    "authority_verified": False,
    "production_ready": False,
}))
"""


def verify(installer, probe=None):
    if not installer.env.get("dedicated_readers"):
        return
    command = ["python", "-c", VERIFY_PROGRAM]
    if probe is not None:
        result = probe.run("superplane-api", command)
    else:
        result = installer.commands.call(
            [
                "docker",
                "run",
                "--rm",
                "--network=none",
                "--entrypoint",
                "python",
                image(installer.lock, "superplane-api"),
                *command[1:],
            ],
            allow_failure=True,
        )
    require(result.returncode == 0, "Pinned API image lacks dedicated reader support")
    observed = installer.json(result)
    require(
        observed == REPORT, "Pinned API image refused the dedicated reader contract"
    )
    installer.receipt["dedicated_reader_image_contract"] = observed
    installer.save()
