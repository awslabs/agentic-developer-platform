"""Replace one unshaded dependency in Ray's unsigned distribution JAR."""

import hashlib
import json
import sys
import zipfile
from pathlib import Path

target, replacement = map(Path, sys.argv[1:])
before = hashlib.sha256(target.read_bytes()).hexdigest()
assert before == "dcd57b74fd625172a6a384e42b949349e1745eadf918a52c2a07a6b68f38597c"
assert (
    hashlib.sha256(replacement.read_bytes()).hexdigest()
    == "18bfbbabb478dfb67f31aeaf428c387f3c3df654582e1309f708ee1f3086830a"
)
prefixes = (
    "org/apache/hc/core5/",
    "META-INF/maven/org.apache.httpcomponents.core5/httpcore5/",
)
temporary = target.with_suffix(".repaired.jar")
with (
    zipfile.ZipFile(target) as old,
    zipfile.ZipFile(replacement) as fixed,
    zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as output,
):
    assert not any(n.endswith((".SF", ".RSA", ".DSA")) for n in old.namelist())
    kept = {}
    for item in old.infolist():
        if not item.filename.startswith(prefixes):
            data = old.read(item)
            output.writestr(item, data)
            kept[item.filename] = hashlib.sha256(data).hexdigest()
    replaced = []
    for item in fixed.infolist():
        if item.filename.startswith(prefixes):
            output.writestr(item, fixed.read(item))
            replaced.append(item.filename)
    assert len(replaced) > 600
with zipfile.ZipFile(temporary) as result:
    assert all(
        hashlib.sha256(result.read(name)).hexdigest() == digest
        for name, digest in kept.items()
    )
    assert (
        "version=5.4.3"
        in result.read(
            "META-INF/maven/org.apache.httpcomponents.core5/httpcore5/pom.properties"
        ).decode()
    )
licenses = Path("/opt/adp-security/high-dependencies/httpcore5")
licenses.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(replacement) as fixed:
    for name in ("META-INF/LICENSE", "META-INF/NOTICE"):
        (licenses / Path(name).name).write_bytes(fixed.read(name))
temporary.replace(target)
Path("/opt/adp-security/high-dependencies").mkdir(parents=True, exist_ok=True)
Path("/opt/adp-security/high-dependencies/ray-httpcore.json").write_text(
    json.dumps(
        {
            "before_sha256": before,
            "after_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "dependency": "org.apache.httpcomponents.core5:httpcore5:5.4.3",
            "unchanged_other_entries": len(kept),
            "replaced_entries": len(replaced),
        },
        indent=2,
    )
    + "\n"
)
