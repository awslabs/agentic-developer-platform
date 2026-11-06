"""Schedule registered assets without cloning content or holding cloud credentials."""

import os
from pathlib import Path

import requests


def main():
    endpoint = os.environ["INGESTION_REFRESH_URL"]
    token = Path(os.environ["INGESTION_REFRESH_TOKEN_FILE"]).read_text().strip()
    after = None
    total = 0
    while True:
        response = requests.post(
            endpoint,
            headers={"Authorization": "Bearer " + token},
            json={"after": after},
            timeout=(10, 300),
            allow_redirects=False,
        )
        if response.status_code != 200:
            raise RuntimeError(f"Registered ingestion refresh failed ({response.status_code})")
        result = response.json()
        total += result["queued"]
        after = result["after"]
        if after is None:
            break
    print(f"Queued {total} registered ingestion assets")


if __name__ == "__main__":
    main()
