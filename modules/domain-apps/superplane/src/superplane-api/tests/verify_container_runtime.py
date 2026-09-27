"""Packaged runtime smoke check; execute only inside its isolated test container.

See docs/security/runs/2026-09-27/curl-removal.md for exact invocation.
"""

import json
import os
import shutil
from pathlib import Path


def main():
    assert os.getuid() == 65532 and shutil.which("curl") is None
    assert not list(Path("/usr/lib/x86_64-linux-gnu").glob("libcurl*"))
    from app.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        r = c.get("/health")
        assert r.status_code == 200, r.text
        assert r.json()["domain_auth_enforced"] is True
        r = c.get("/readyz")
        assert r.status_code == 503, r.text
    print(
        json.dumps(
            {
                "health": "passed",
                "domain_auth_enforced": True,
                "missing_database_readiness": 503,
                "curl_and_libcurl": "absent",
                "uid": os.getuid(),
                "external_network": "disabled",
            }
        )
    )


if __name__ == "__main__":
    main()
