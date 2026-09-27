"""Offline provider, image codec and Ray runtime dependency acceptance."""

import importlib.metadata
import io
import pyexpat
import json
import subprocess
import sys

import sky
from PIL import Image
from vastai_sdk import VastAI
from tarfile_regression import check

assert sky.__version__ == "0.12.3"
assert importlib.metadata.version("vastai") == "1.0.13+adp1"
assert importlib.metadata.version("pillow") == "12.3.0"
client = VastAI(api_key="offline-fixture")
assert client is not None
for encoding in ("PNG", "JPEG", "TIFF", "WEBP"):
    output = io.BytesIO()
    Image.new("RGB", (16, 16), (20, 40, 60)).save(output, format=encoding)
    output.seek(0)
    with Image.open(output) as decoded:
        decoded.load()
        assert decoded.size == (16, 16)
target = "/usr/local/lib/python3.10/site-packages/ray/_private/runtime_env/agent/thirdparty_files"
code = f"import sys;sys.path.insert(0,{target!r});import aiohttp;assert aiohttp.__version__=='3.14.3';print(aiohttp.__file__)"
subprocess.run([sys.executable, "-c", code], check=True)
check()
assert pyexpat.EXPAT_VERSION == "expat_2.8.5"
print(
    json.dumps(
        {
            "skypilot": sky.__version__,
            "vastai": "import/client passed",
            "image_codecs": "passed",
            "ray_aiohttp": "3.14.3",
        }
    )
)
