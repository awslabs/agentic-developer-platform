"""Verify exact Critical remediation packages and the loaded trust store."""

import json
import ssl
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import certifi


def main():
    if not __debug__:
        raise RuntimeError("Acceptance requires assertions")
    root = Path(__file__).resolve().parent
    assert metadata.version("certifi") == "2026.10.19"
    assert metadata.version("uv") == "0.13.4"
    assert metadata.version("pillow") in {"12.2.0+adp1", "12.3.0+adp1"}
    context = ssl.create_default_context(cafile=certifi.where())
    names = {
        value
        for cert in context.get_ca_certs()
        for rdn in cert["subject"]
        for key, value in rdn
        if key == "commonName"
    }
    assert len(names) > 100
    assert "emSign ECC Root CA - G3" not in names
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    subprocess.run([sys.executable, str(root / "pillow_regression.py")], check=True)
    print(
        json.dumps(
            {
                "certifi": certifi.__version__,
                "removed_root_absent": True,
                "uv": metadata.version("uv"),
                "pillow": metadata.version("pillow"),
            }
        )
    )


if __name__ == "__main__":
    main()
