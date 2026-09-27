"""Require python-jose's existing cryptography EC backend, removing its fallback."""

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
init.write_text(text.replace('__version__ = "3.5.0"', '__version__ = "3.5.0+adp1"'))
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
