"""Install the adapter only into the reviewed upstream client export module."""

import hashlib
from pathlib import Path

path = Path("/app/api/clients/__init__.py")
source = path.read_bytes()
if hashlib.sha256(source).hexdigest() != "cad66a0055bde7a8d0e22d71211d0d73618110d228ffcaadd1cad5c680efbe51":
    raise SystemExit("DeepWiki client exports changed; review the proxy adapter before building")
text = source.decode().replace("    OpenAIClient,\n", "", 1)
text = text.replace("from .anthropic import", "from .proxy_openai import OpenAIClient\n\nfrom .anthropic import", 1)
path.write_text(text)
