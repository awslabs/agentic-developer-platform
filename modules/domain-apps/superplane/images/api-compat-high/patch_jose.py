"""Require the cryptography backend and reject DER public keys as HMAC secrets."""

from pathlib import Path
import sys

root = Path(sys.argv[1])
cfg = root / "setup.cfg"
text = cfg.read_text()
assert text.count("ecdsa != 0.15") == 1
cfg.write_text(text.replace("ecdsa != 0.15", "cryptography ==50.0.1"))
init = root / "jose/__init__.py"
text = init.read_text()
assert text.count('__version__ = "3.5.0"') == 1
init.write_text(text.replace('__version__ = "3.5.0"', '__version__ = "3.5.0+adp2"'))
backend = root / "jose/backends/__init__.py"
text = backend.read_text()
old = """try:
    from jose.backends.cryptography_backend import CryptographyECKey as ECKey  # noqa: F401
except ImportError:
    from jose.backends.ecdsa_backend import ECDSAECKey as ECKey  # noqa: F401"""
assert text.count(old) == 1
backend.write_text(
    text.replace(
        old,
        "from jose.backends.cryptography_backend import CryptographyECKey as ECKey  # noqa: F401",
    )
)
(root / "jose/backends/ecdsa_backend.py").unlink()

# GHSA-3qf3-8w2g-rqmx: the upstream PEM/SSH checks miss DER public keys.
# cryptography is already a required dependency of this maintained wheel.
utils = root / "jose/utils.py"
text = utils.read_text()
assert "def is_der_public_key(" not in text
utils.write_text(
    text
    + '''

def is_der_public_key(key):
    """Recognize asymmetric public keys without rejecting opaque HMAC secrets."""
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    try:
        load_der_public_key(key)
    except (ValueError, TypeError):
        return False
    except UnsupportedAlgorithm:
        # A recognized but unsupported asymmetric algorithm is not a secret.
        return True
    return True
'''
)
for name in ("native.py", "cryptography_backend.py"):
    path = root / "jose/backends" / name
    text = path.read_text()
    old = "if is_pem_format(key) or is_ssh_key(key):"
    assert text.count(old) == 1, f"Unexpected upstream HMAC implementation: {name}"
    text = text.replace(
        old, "if is_pem_format(key) or is_ssh_key(key) or is_der_public_key(key):"
    )
    path.write_text("from jose.utils import is_der_public_key\n" + text)
