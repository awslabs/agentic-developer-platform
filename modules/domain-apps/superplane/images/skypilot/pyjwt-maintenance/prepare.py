"""Download the exact locked wheel; this does not build or publish an image."""
import hashlib
import json
from pathlib import Path
import re
import urllib.request

root = Path(__file__).resolve().parent
lock = json.loads((root / 'artifact-lock.json').read_text())
requirements = (root.parent / 'requirements-security.txt').read_text()
assert re.findall(r'^PyJWT==[^\s]+$', requirements, re.MULTILINE) == ['PyJWT==' + lock['version']]
with urllib.request.urlopen(lock['url'], timeout=60) as response:
    content = response.read()
assert len(content) == lock['size']
assert hashlib.sha256(content).hexdigest() == lock['sha256']
(root / 'artifacts').mkdir(exist_ok=True)
(root / 'artifacts' / lock['filename']).write_bytes(content)
print(json.dumps({'artifact': lock['filename'], 'sha256': lock['sha256']}))
