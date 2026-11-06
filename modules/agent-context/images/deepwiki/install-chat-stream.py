"""Replace only the reviewed upstream OpenAI chat streaming method."""

import hashlib
from pathlib import Path

path = Path("/app/api/chat/_stream.py")
source = path.read_bytes()
if hashlib.sha256(source).hexdigest() != "2cfd39a43cbf586aca4eaa56c145fe7dc2c9854658cb1bd7fac944658ee34fc7":
    raise SystemExit("DeepWiki chat streaming changed; review the protocol adapter before building")
marker = "        return OpenAIClient()\n"
text = source.decode()
if text.count(marker) != 1:
    raise SystemExit("Expected exactly one OpenAI chat client constructor")
# Bind the adapter as the method on OpenAIChatStreamer, preserving its factory
# registration, constructor, model settings and the other provider classes.
path.write_text(text.replace(marker, marker + "\n    from .openai_chat import respond_stream\n", 1))
