"""Preserve upstream naive-UTC storage with SQLModel's explicit datetime type."""
import hashlib
from pathlib import Path

path = Path("/app/src/mcp_agent_mail/models.py")
source = path.read_bytes()
# Fail closed when the pinned upstream changes; review the compatibility patch.
assert hashlib.sha256(source).hexdigest() == "b5960ad286cb7668d17a29cb1e20df302b68435b9b8e8b3e65f2655206cc6a1a"
text = source.decode().replace("from sqlmodel import Field, SQLModel", "from sqlmodel import Field, SQLModel\nfrom pydantic import NaiveDatetime")
text = text.replace(": datetime", ": NaiveDatetime").replace("Optional[datetime]", "Optional[NaiveDatetime]")
path.write_text(text)
