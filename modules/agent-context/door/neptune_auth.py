"""Neptune IAM SigV4 authentication for bolt connections.

Generates signed auth tokens for the neo4j Python driver to connect to
Neptune's bolt endpoint using IAM SigV4 authentication (IRSA in EKS).

See: docs/agent-context/neptune-deep-graph-design.md (Connection section)
"""

from __future__ import annotations

import json
import logging
import os

from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session
from neo4j import Auth, GraphDatabase

log = logging.getLogger(__name__)


def get_neptune_auth(endpoint: str, region: str) -> Auth:
    """Generate a SigV4-signed NeptuneAuthToken for bolt connections.

    Parameters
    ----------
    endpoint:
        Neptune cluster endpoint (without port or protocol).
    region:
        AWS region (e.g. "us-east-1").

    Returns
    -------
    neo4j.Auth object suitable for GraphDatabase.driver().
    """
    session = Session()
    credentials = session.get_credentials().get_frozen_credentials()

    request = AWSRequest(
        method="GET",
        url=f"https://{endpoint}:8182/opencypher",
    )
    request.headers.add_header("Host", f"{endpoint}:8182")

    sigv4 = SigV4Auth(credentials, "neptune-db", region)
    sigv4.add_auth(request)

    auth_obj = {
        "Authorization": request.headers["Authorization"],
        "HttpMethod": "GET",
        "X-Amz-Date": request.headers["X-Amz-Date"],
        "Host": request.headers["Host"],
        "X-Amz-Security-Token": request.headers.get("X-Amz-Security-Token", ""),
    }
    return Auth("basic", "username", json.dumps(auth_obj), "realm")


def create_neptune_driver(
    endpoint: str | None = None,
    region: str | None = None,
    max_pool: int = 25,
    acquire_timeout: float = 30.0,
    connection_timeout: float = 5.0,
):
    """Create a neo4j driver connected to Neptune via bolt+s.

    Uses IAM SigV4 auth (IRSA). Returns None if endpoint not configured.

    Parameters
    ----------
    endpoint:
        Neptune cluster endpoint. Falls back to NEPTUNE_ENDPOINT env var.
    region:
        AWS region. Falls back to AWS_REGION env var.
    max_pool:
        Maximum connection pool size.
    acquire_timeout:
        Timeout (seconds) to acquire a connection from the pool.
    connection_timeout:
        Timeout (seconds) to establish a new connection.

    Returns
    -------
    neo4j.Driver instance, or None if endpoint is not configured.
    """
    endpoint = endpoint or os.environ.get("NEPTUNE_ENDPOINT", "")
    region = region or os.environ.get("AWS_REGION", "us-east-1")

    if not endpoint:
        log.debug("NEPTUNE_ENDPOINT not configured — Neptune driver not created")
        return None

    uri = f"bolt+s://{endpoint}:8182"
    auth = get_neptune_auth(endpoint, region)

    return GraphDatabase.driver(
        uri,
        auth=auth,
        encrypted=True,
        max_connection_pool_size=max_pool,
        connection_acquisition_timeout=acquire_timeout,
        connection_timeout=connection_timeout,
    )
