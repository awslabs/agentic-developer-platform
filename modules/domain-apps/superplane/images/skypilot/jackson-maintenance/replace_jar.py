"""Replace actual Jackson bytecode/resources while preserving other Ray entries."""

import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parent
NAMESPACE = "io/ray/shaded/com/fasterxml/jackson/"
MAVEN = "META-INF/maven/com.fasterxml.jackson.core/"
VERSIONED = re.compile(
    r"^META-INF/versions/(11|17|21)/(?:io/ray/shaded/)?com/fasterxml/jackson/"
)
SERVICES = {
    "META-INF/services/" + prefix + suffix
    for prefix in ("", "io.ray.shaded.")
    for suffix in (
        "com.fasterxml.jackson.core.JsonFactory",
        "com.fasterxml.jackson.core.ObjectCodec",
    )
}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def owned(name):
    return (
        name.startswith((NAMESPACE, MAVEN))
        or bool(VERSIONED.match(name))
        or name in SERVICES
    )


def records(archive):
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise ValueError("duplicate jar entry")
    return {name: archive.read(name) for name in names}


def merge(original, shaded, destination):
    lock = json.loads((ROOT / "artifact-lock.json").read_text())
    if digest(original.read_bytes()) != lock["original_jar_sha256"]:
        raise ValueError("original Ray jar differs from reviewed baseline")
    replacements = {}
    with zipfile.ZipFile(shaded) as archive:
        for name, raw in records(archive).items():
            if name.endswith("/"):
                continue
            if name.startswith(NAMESPACE) or name.startswith(MAVEN) or name in SERVICES:
                replacements[name] = raw
            elif VERSIONED.match(name):
                # Shade remaps these class bytes but leaves their entry path
                # unrelocated. Match the entry to the relocated JVM binary name.
                renamed = re.sub(
                    r"^(META-INF/versions/\d+/)com/fasterxml/",
                    r"\1io/ray/shaded/com/fasterxml/",
                    name,
                )
                if b"io/ray/shaded/com/fasterxml/jackson" not in raw:
                    raise ValueError("versioned class bytecode was not relocated")
                replacements[renamed] = raw
            elif not (
                name.startswith("META-INF/maven/org.adp.maintenance/")
                or name == "META-INF/MANIFEST.MF"
                or name.startswith("META-INF/")
                and ("LICENSE" in name or "NOTICE" in name)
            ):
                raise ValueError("unexpected shaded artifact member: " + name)
    for component in ("core", "databind", "annotations"):
        filename = f"jackson-{component}-{lock['jackson_version']}.jar"
        item = next(i for i in lock["artifacts"] if i["filename"] == filename)
        source = ROOT / "artifacts" / filename
        if digest(source.read_bytes()) != item["sha256"]:
            raise ValueError("vendor jar differs from lock")
        with zipfile.ZipFile(source) as archive:
            for name in archive.namelist():
                if name.startswith("META-INF/") and (
                    "LICENSE" in name or "NOTICE" in name
                ):
                    replacements[
                        MAVEN + "jackson-" + component + "/" + Path(name).name
                    ] = archive.read(name)
        properties = replacements[MAVEN + "jackson-" + component + "/pom.properties"]
        if ("version=" + lock["jackson_version"]).encode() not in properties:
            raise ValueError("shaded version metadata differs from actual input")
    if not any(
        name.startswith(NAMESPACE) and name.endswith(".class") for name in replacements
    ):
        raise ValueError("no replacement library bytecode")
    with zipfile.ZipFile(original) as old:
        before = records(old)
        if b"Multi-Release: true" not in before["META-INF/MANIFEST.MF"]:
            raise ValueError("expected original multi-release manifest")
        if any(name.upper().endswith((".SF", ".RSA", ".DSA")) for name in before):
            raise ValueError("signed original jar requires separate signing review")
        with zipfile.ZipFile(destination, "w") as result:
            for info in old.infolist():
                if not owned(info.filename):
                    result.writestr(info, before[info.filename])
            for name, raw in sorted(replacements.items()):
                info = zipfile.ZipInfo(name, (2026, 10, 6, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                result.writestr(info, raw)
    with zipfile.ZipFile(destination) as output:
        after = records(output)
    if any(after.get(name) != raw for name, raw in before.items() if not owned(name)):
        raise ValueError("unrelated Ray bytes changed")
    if any(not owned(name) and name not in before for name in after):
        raise ValueError("unexpected new Ray entry")
    return {
        "original_sha256": digest(original.read_bytes()),
        "replacement_sha256": digest(destination.read_bytes()),
        "replacement_entries": len(replacements),
        "changed_entries": sorted(
            name
            for name in before.keys() | after.keys()
            if before.get(name) != after.get(name)
        ),
        "unrelated_entries_preserved": sum(not owned(name) for name in before),
    }
