"""Build the unchanged Vast.ai SDK against the security-fixed Pillow release."""

import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
project = root / "pyproject.toml"
before = project.read_text()
assert before.count('"pillow==12.2.0"') == 1
assert before.count('"cryptography==46.0.5"') == 1
assert before.count('version = "1.0.13"') == 1
before_crypto = before.replace('"cryptography==46.0.5"', '"cryptography==46.0.7"')
after = before_crypto.replace('"pillow==12.2.0"', '"pillow==12.3.0"').replace(
    'version = "1.0.13"', 'version = "1.0.13+adp1"'
)
project.write_text(after)
(root / "adp-security-source.json").write_text(
    json.dumps(
        {
            "upstream": "vastai 1.0.13",
            "change": "Pillow 12.3.0, cryptography 46.0.7 dependencies and local package version only",
            "before_sha256": hashlib.sha256(before.encode()).hexdigest(),
            "after_sha256": hashlib.sha256(after.encode()).hexdigest(),
        },
        indent=2,
    )
    + "\n"
)
