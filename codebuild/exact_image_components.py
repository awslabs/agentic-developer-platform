"""Opt-in Python component binding; never execute code or approve evidence.

Final components must be bytecode-free. The only supported producer transition
removes source-backed caches and their RECORD rows; it never treats old bytecode
as verified source. Source-to-build continuity requires independent approval.
"""

import base64
import copy
import csv
import email.parser
import io
import posixpath
import re
import struct
from urllib.parse import quote

STDLIB = "/usr/local/lib/python3.10"
SITE = STDLIB + "/site-packages"
PYTHON = "/usr/local/bin/python3.10"
LIBPYTHON = "/usr/local/lib/libpython3.10.so.1.0"
PROVIDERS = {"cpython-runtime/v1", "python-installed-distribution/v1"}
PTH = SITE + "/distutils-precedence.pth"
PTH_SHA = "2638ce9e2500e572a5e0de7faed6661eb569d1b696fcba07b0dd223da5f5d224"
ROLES = {"source", "build", "runtime", "independent_review"}
ADVISORIES = {
    "cpython-runtime/v1": {"CVE-2023-36632", "CVE-2026-7210", "CVE-2026-82049"},
    "python-installed-distribution/v1": {
        "GHSA-jwv3-5hgf-82ww",
        "GHSA-g6cj-pr64-35w5",
        "GHSA-537c-gmf6-5ccf",
    },
}


def beneath(path, root):
    return path == root or path.startswith(root + "/")


def strict_path(d, value):
    d.require(
        isinstance(value, str)
        and value.startswith("/")
        and d.normal_path(value) == value,
        "noncanonical component path",
    )
    return value


def pin(d, root, name, hashes):
    path = d.local_file(root, name)
    digest = d.file_sha(path)
    d.require(name not in hashes or hashes[name] == digest, "component input changed")
    hashes[name] = digest
    return path


def read_oci(d, archive, platform):
    # First pass determines all real metadata paths, never caller-selected subsets.
    _, _, files, _ = d.collect_oci(archive, platform)
    retain = {
        p
        for p, e in files.items()
        if e["kind"] == "file"
        and (p.endswith(("/RECORD", "/METADATA", "/WHEEL", "/direct_url.json", ".pth")))
    }
    anchors = {
        PYTHON,
        LIBPYTHON,
        SITE + "/cryptography/hazmat/bindings/_rust.abi3.so",
        STDLIB + "/lib-dynload/pyexpat.cpython-310-x86_64-linux-gnu.so",
        STDLIB + "/lib-dynload/_elementtree.cpython-310-x86_64-linux-gnu.so",
        "/usr/lib/x86_64-linux-gnu/libexpat.so.1",
        "/usr/lib/x86_64-linux-gnu/libssl.so.3",
        "/usr/lib/x86_64-linux-gnu/libcrypto.so.3",
    }
    for path in anchors:
        if path in files:
            retain.add(d.resolve_file(files, path)[0])
    layers = {}
    config, digest, files, contents = d.collect_oci(
        archive, platform, retain_paths=retain, file_layers=layers
    )
    return config, digest, files, contents, layers


def file_entry(d, files, path):
    strict_path(d, path)
    resolved, entry = d.resolve_file(files, path)
    d.require(entry["kind"] == "file", "component path must resolve to a regular file")
    return {"resolved": resolved, **entry}


def scope_files(d, files, paths):
    result = {}
    for path in sorted(paths):
        entry = files[path]
        if entry["kind"] == "directory":
            continue
        d.require(
            not path.endswith((".pyc", ".pyo")) and "/__pycache__/" not in path,
            "bytecode provenance unsupported; "
            "source/producer equality alone is insufficient",
        )
        resolved = file_entry(d, files, path)
        d.require(resolved["resolved"] in paths, "component link escapes owned scope")
        result[path] = {**resolved, "path_entry": copy.deepcopy(entry)}
    d.require(result, "empty component scope")
    return result


def csv_rows(d, raw):
    try:
        return list(csv.reader(io.StringIO(raw.decode("utf-8")), strict=True))
    except (csv.Error, UnicodeError) as error:
        raise d.Invalid("malformed Python RECORD") from error


def cache_source(d, path, files):
    if "/__pycache__/" in path:
        parent, name = path.rsplit("/__pycache__/", 1)
        match = re.fullmatch(r"([^/]+)\.cpython-310(?:\.opt-[12])?\.pyc", name)
        d.require(match is not None, "unsupported cache filename")
        source = parent + "/" + match[1] + ".py"
    else:
        d.require(path.endswith(".pyc"), "unsupported legacy bytecode")
        source = path[:-1]
    d.require(
        files.get(path, {}).get("kind") == "file"
        and files.get(source, {}).get("kind") == "file",
        "orphan or linked cache cannot be removed as a source-backed transition",
    )
    return source


def clean_producer(d, files, contents):
    """Virtual, finite transition only. Do not change the retained producer."""
    after, metadata_bytes = copy.deepcopy(files), dict(contents)
    removed = {}
    for path, entry in files.items():
        in_scope = (
            (beneath(path, STDLIB) and not beneath(path, SITE))
            or beneath(path, SITE + "/cryptography")
            or beneath(path, SITE + "/_distutils_hack")
        )
        if (
            in_scope
            and (path.endswith((".pyc", ".pyo")) or "/__pycache__/" in path)
            and entry["kind"] != "directory"
        ):
            source = cache_source(d, path, files)
            removed[path] = {
                "file": entry,
                "source": source,
                "source_file": files[source],
            }
            del after[path]
    changed_records = {}
    for path, raw in contents.items():
        if not path.startswith(SITE + "/") or not path.endswith(".dist-info/RECORD"):
            continue
        rows = csv_rows(d, raw)
        lines = raw.splitlines(keepends=True)
        d.require(len(rows) == len(lines), "multiline RECORD paths unsupported")
        kept = []
        for row, line in zip(rows, lines):
            d.require(
                len(row) == 3 and row[0] and not any(c in row[0] for c in "\r\n\x00"),
                "invalid producer RECORD row",
            )
            path_in_record = posixpath.normpath(SITE + "/" + row[0])
            if path_in_record not in removed:
                kept.append(line)
        cleaned = b"".join(kept)
        if cleaned != raw:
            d.require(
                path in after and after[path]["kind"] == "file",
                "producer RECORD must be regular",
            )
            after[path] = {
                **after[path],
                "sha256": d.sha(cleaned),
                "size": len(cleaned),
            }
            metadata_bytes[path] = cleaned
            changed_records[path] = {
                "before_sha256": d.sha(raw),
                "after_sha256": d.sha(cleaned),
            }
    return (
        after,
        metadata_bytes,
        {"removed_caches": removed, "changed_records": changed_records},
    )


def metadata(d, contents, path):
    d.require(path in contents, "missing installed Python metadata")
    return email.parser.BytesParser().parsebytes(contents[path])


def record(d, files, contents, path):
    strict_path(d, path)
    d.require(
        path.startswith(SITE + "/") and path.endswith(".dist-info/RECORD"),
        "unsupported installed RECORD root",
    )
    d.require(path in contents, "missing installed RECORD")
    records = {}
    for row in csv_rows(d, contents[path]):
        d.require(
            len(row) == 3 and row[0] and not row[0].startswith("/"),
            "invalid RECORD row",
        )
        full = SITE + "/" + row[0]
        strict_path(d, full)
        d.require(full not in records, "duplicate RECORD path")
        value = file_entry(d, files, full)
        if full == path:
            d.require(row[1:] == ["", ""], "RECORD self row must be unhashed")
        else:
            d.require(
                row[1].startswith("sha256=") and re.fullmatch(r"[0-9]+", row[2]),
                "unhashed or unsupported RECORD payload",
            )
            encoded = row[1][7:]
            d.require(
                re.fullmatch(r"[A-Za-z0-9_-]{43}", encoded), "invalid RECORD SHA256"
            )
            digest = base64.b64decode(
                encoded + "=", altchars=b"-_", validate=True
            ).hex()
            d.require(
                digest == value["sha256"] and int(row[2]) == value["size"],
                "installed RECORD digest/size mismatch",
            )
        records[full] = value
    d.require(path in records, "RECORD omits itself")
    return records


def import_context(d, files, contents):
    paths = {p for p in files if beneath(p, STDLIB) and not beneath(p, SITE)}
    for path, entry in files.items():
        if not beneath(path, STDLIB) or entry["kind"] == "directory":
            continue
        if path.endswith(".pth"):
            d.require(
                path == PTH and entry.get("sha256") == PTH_SHA,
                "unsupported active Python path hook",
            )
            paths.add(path)
        d.require(
            path.rsplit("/", 1)[-1]
            not in {
                "sitecustomize.py",
                "usercustomize.py",
                "sitecustomize.pyc",
                "usercustomize.pyc",
            },
            "unsupported Python customization hook",
        )
    if PTH in paths:
        hook = {p for p in files if beneath(p, SITE + "/_distutils_hack")}
        d.require(
            SITE + "/_distutils_hack/__init__.py" in hook,
            "path hook implementation missing",
        )
        paths.update(hook)
    # The interpreter's default site/path behavior is executable context too.
    for name in ("site.py", "os.py", "sysconfig.py"):
        path = STDLIB + "/" + name
        d.require(path in files, "missing Python import context")
        paths.add(path)
    paths.update(
        p
        for p in files
        if beneath(p, STDLIB + "/importlib") or beneath(p, STDLIB + "/encodings")
    )
    for path in (PYTHON, LIBPYTHON):
        d.require(path in files, "missing runtime interpreter")
        paths.add(path)
    return scope_files(d, files, paths)


def distribution_scope(d, artifact, files, contents):
    identity = d.package_identity(artifact)
    d.require(
        identity["name"] == "cryptography"
        and identity["type"] == "python"
        and artifact.get("foundBy") == "python-installed-package-cataloger"
        and artifact.get("metadataType") == "python-package",
        "unsupported Python distribution provenance",
    )
    version = identity["version"]
    d.require(
        re.fullmatch(r"[0-9][A-Za-z0-9.+_-]*", version),
        "unsupported distribution version",
    )
    dist = SITE + "/cryptography-" + version + ".dist-info"
    d.require(
        {p for p in files if re.search(r"/cryptography-[^/]+\.dist-info/METADATA$", p)}
        == {dist + "/METADATA"},
        "ambiguous cryptography installation",
    )
    headers = metadata(d, contents, dist + "/METADATA")
    d.require(
        headers.get_all("Name") == ["cryptography"]
        and headers.get_all("Version") == [version],
        "METADATA/scanner package mismatch",
    )
    d.require(
        dist + "/direct_url.json" in contents, "missing source installation provenance"
    )
    direct = d.parse(contents[dist + "/direct_url.json"])
    d.require(
        isinstance(direct, dict)
        and isinstance(direct.get("url"), str)
        and direct["url"]
        and not direct.get("dir_info", {}).get("editable"),
        "editable or unknown Python installation unsupported",
    )
    wheel = metadata(d, contents, dist + "/WHEEL")
    d.require(
        wheel.get_all("Wheel-Version") == ["1.0"]
        and wheel.get_all("Root-Is-Purelib") == ["false"]
        and wheel.get_all("Tag")
        and all(
            re.fullmatch(r"cp3(?:[7-9]|10)-abi3-(?:linux|manylinux[0-9_]+)_x86_64", tag)
            for tag in wheel.get_all("Tag")
        ),
        "unsupported installed wheel layout",
    )
    d.require(
        identity["purl"] == "pkg:pypi/cryptography@" + quote(version, safe=""),
        "distribution PURL mismatch",
    )
    records = record(d, files, contents, dist + "/RECORD")
    d.require(
        all(
            beneath(p, SITE + "/cryptography")
            or beneath(p, dist)
            or p in {SITE + "/CHANGELOG.rst", SITE + "/CONTRIBUTING.rst"}
            for p in records
        ),
        "RECORD claims unrelated installed files",
    )
    owned = {p for p in files if beneath(p, SITE + "/cryptography") or beneath(p, dist)}
    owned.update(records)
    metadata_names = {
        "METADATA",
        "WHEEL",
        "RECORD",
        "INSTALLER",
        "REQUESTED",
        "direct_url.json",
    }
    for path in owned:
        if beneath(path, dist) and files[path]["kind"] != "directory":
            relative = path[len(dist) + 1 :]
            allowed_data = re.fullmatch(
                r"licenses/LICENSE(?:\.(?:APACHE|BSD))?|sboms/cryptography-rust\.cyclonedx\.json",
                relative,
            )
            d.require(
                relative in metadata_names or allowed_data,
                "unsupported executable or unknown dist-info payload",
            )
            d.require(
                files[path]["kind"] == "file" and not files[path]["mode"] & 0o111,
                "dist-info payload must be non-executable regular metadata",
            )
    scope = scope_files(d, files, owned)
    d.require(
        set(scope) == set(records),
        "RECORD omits installed distribution code or metadata",
    )
    rust = SITE + "/cryptography/hazmat/bindings/_rust.abi3.so"
    d.require(rust in scope, "missing cryptography native consumer")
    # Any second RECORD claiming a selected path is ambiguous ownership, even
    # if both currently happen to contain the same bytes.
    for path, raw in contents.items():
        if (
            path == dist + "/RECORD"
            or not path.startswith(SITE + "/")
            or not path.endswith(".dist-info/RECORD")
        ):
            continue
        for row in csv_rows(d, raw):
            d.require(
                len(row) == 3 and row[0] and not row[0].startswith("/"),
                "invalid neighboring RECORD",
            )
            candidate = SITE + "/" + row[0]
            # Neighbor distributions may legitimately put scripts outside SITE;
            # canonicalize only for collision detection, never grant ownership.
            candidate = posixpath.normpath(candidate)
            d.require(candidate not in scope, "overlapping distribution ownership")
            try:
                resolved, entry = d.resolve_file(files, candidate)
            except d.MissingFile:
                continue
            d.require(
                resolved not in {e["resolved"] for e in scope.values()},
                "overlapping resolved distribution ownership",
            )
    return (
        scope,
        {dist + "/METADATA", dist + "/RECORD", dist + "/direct_url.json"},
        {
            "cryptography": SITE + "/cryptography/__init__.py",
            "cryptography.hazmat.bindings._rust": rust,
        },
    )


def runtime_scope(d, artifact, files, contents):
    identity = d.package_identity(artifact)
    d.require(
        identity["type"] == "binary"
        and identity["name"] == "python"
        and re.fullmatch(r"3\.10\.[0-9]+", identity["version"])
        and artifact.get("foundBy") == "binary-classifier-cataloger"
        and artifact.get("metadataType") == "binary-signature",
        "unsupported CPython runtime provenance",
    )
    d.require(
        identity["purl"] == "pkg:generic/python@" + identity["version"],
        "CPython PURL mismatch",
    )
    paths = {p for p in files if beneath(p, STDLIB) and not beneath(p, SITE)}
    paths.update((PYTHON, LIBPYTHON))
    for alias in (
        "/usr/local/bin/python",
        "/usr/local/bin/python3",
        "/usr/local/lib/libpython3.10.so",
    ):
        if alias in files:
            paths.add(alias)
    modules = {
        "tarfile": STDLIB + "/tarfile.py",
        "email.utils": STDLIB + "/email/utils.py",
        "pyexpat": STDLIB + "/lib-dynload/pyexpat.cpython-310-x86_64-linux-gnu.so",
        "_elementtree": STDLIB
        + "/lib-dynload/_elementtree.cpython-310-x86_64-linux-gnu.so",
    }
    mandatory_email = {
        STDLIB + "/email/" + name
        for name in (
            "__init__.py",
            "_parseaddr.py",
            "parser.py",
            "feedparser.py",
            "message.py",
            "errors.py",
            "policy.py",
            "_policybase.py",
        )
    }
    d.require(mandatory_email.issubset(files), "incomplete email parser closure")
    d.require(
        all(p in files for p in paths | set(modules.values())),
        "incomplete CPython runtime closure",
    )
    return scope_files(d, files, paths), {PYTHON, LIBPYTHON}, modules


def locations(d, artifact, expected, files, layers):
    found = artifact.get("locations", [])
    d.require(
        found
        and len(found) == len(expected)
        and {x["path"] for x in found} == expected,
        "incomplete or ambiguous component locations",
    )
    primary = [
        x["path"]
        for x in found
        if x.get("annotations", {}).get("evidence") == "primary"
    ]
    d.require(
        len(primary) == 1
        and (primary[0] == PYTHON or primary[0].endswith(".dist-info/METADATA")),
        "component primary evidence mismatch",
    )
    for location in found:
        path = strict_path(d, location["path"])
        access = strict_path(d, location.get("accessPath", path))
        actual = file_entry(d, files, path)
        d.require(
            actual == file_entry(d, files, access), "component access path mismatch"
        )
        d.require(
            location.get("layerID") == layers[actual["resolved"]],
            "component scanner location layer mismatch",
        )
    return copy.deepcopy(found)


def dependencies(d, root, claims, observation, files, hashes, provider):
    expected = (
        {"/usr/lib/x86_64-linux-gnu/libexpat.so.1"}
        if provider == "cpython-runtime/v1"
        else {
            "/usr/lib/x86_64-linux-gnu/libssl.so.3",
            "/usr/lib/x86_64-linux-gnu/libcrypto.so.3",
        }
    )
    result, seen = {}, set()
    for claim in claims:
        d.exact_keys(
            claim, {"package_purl", "artifact", "paths"}, "component dependency"
        )
        package = observation["packages"].get(claim["package_purl"])
        d.require(
            package is not None, "dependency requires verified root dpkg ownership"
        )
        selected = {}
        for path in claim["paths"]:
            d.require(
                path in expected and path not in seen,
                "unexpected or duplicate dependency path",
            )
            seen.add(path)
            actual = file_entry(d, files, path)
            d.require(
                package["files"].get(actual["resolved"]) == actual,
                "dependency not owned by selected root package",
            )
            selected[actual["resolved"]] = actual
            result[path] = {
                "package": package["package"],
                "file": actual,
                "artifact": claim["artifact"],
            }
        d.require(selected, "empty dependency artifact")
        artifact = pin(d, root, claim["artifact"], hashes)
        d.verify_deb(artifact, package, selected)
    d.require(seen == expected, "incomplete runtime dependency closure")
    return result


def elf_dynamic(d, data):
    """Read only ELF64 little-endian amd64 program headers; never load the file."""
    d.require(
        len(data) >= 64 and data[:7] == b"\x7fELF\x02\x01\x01",
        "unsupported component ELF header",
    )
    header = struct.unpack_from("<HHIQQQIHHHHHH", data, 16)
    kind, machine, version, _, phoff, _, _, ehsize, phsize, phnum, _, _, _ = header
    d.require(
        kind in (2, 3)
        and machine == 62
        and version == 1
        and ehsize == 64
        and phsize == 56
        and 0 < phnum < 1024
        and phoff + phnum * phsize <= len(data),
        "unsupported component ELF ABI",
    )
    headers = [
        struct.unpack_from("<IIQQQQQQ", data, phoff + i * phsize) for i in range(phnum)
    ]
    d.require(
        all(h[2] + h[5] <= len(data) and h[5] <= h[6] for h in headers),
        "component ELF segment bounds",
    )
    dynamic = [h for h in headers if h[0] == 2]
    d.require(
        len(dynamic) == 1 and dynamic[0][5] % 16 == 0,
        "component ELF requires one dynamic segment",
    )
    segment = dynamic[0]
    mapped = [
        h
        for h in headers
        if h[0] == 1 and h[3] <= segment[3] and segment[3] + segment[5] <= h[3] + h[5]
    ]
    d.require(
        len(mapped) == 1 and mapped[0][2] + segment[3] - mapped[0][3] == segment[2],
        "ELF dynamic virtual/file mapping differs",
    )
    entries, ended = [], False
    for offset in range(dynamic[0][2], dynamic[0][2] + dynamic[0][5], 16):
        tag, value = struct.unpack_from("<qQ", data, offset)
        if tag == 0:
            ended = True
            break
        entries.append((tag, value))
    d.require(ended, "unterminated component ELF dynamic table")

    def one(tag):
        found = [v for t, v in entries if t == tag]
        d.require(len(found) == 1, "ambiguous component ELF string table")
        return found[0]

    address, size = one(5), one(10)
    segments = [
        h
        for h in headers
        if h[0] == 1 and h[3] <= address and address + size <= h[3] + h[5]
    ]
    d.require(len(segments) == 1 and size > 0, "unmapped component ELF strings")
    offset = segments[0][2] + address - segments[0][3]
    strings = data[offset : offset + size]

    def string(index):
        d.require(
            index < len(strings) and b"\x00" in strings[index:],
            "component ELF string bounds",
        )
        value = strings[index:].split(b"\x00", 1)[0]
        d.require(
            value and all(32 < x < 127 for x in value),
            "invalid component ELF dependency string",
        )
        return value.decode("ascii")

    needed = [string(v) for tag, v in entries if tag == 1]
    soname = [string(v) for tag, v in entries if tag == 14]
    search = [string(v) for tag, v in entries if tag in (15, 29)]
    d.require(
        len(needed) == len(set(needed)) and len(soname) <= 1,
        "duplicate component ELF identity",
    )
    d.require(
        all(re.fullmatch(r"[A-Za-z0-9_.+-]+", name) for name in needed),
        "non-SONAME dependency path",
    )
    return {
        "needed": needed,
        "soname": soname,
        "search_paths": search,
        "machine": "amd64",
        "class": "ELF64",
    }


def binary_dependencies(d, files, contents, provider, imports, deps):
    paths = {PYTHON, LIBPYTHON, *deps}
    paths.update(p for p in imports.values() if p.endswith(".so"))
    result = {}
    for path in sorted(paths):
        resolved = file_entry(d, files, path)
        d.require(
            resolved["resolved"] in contents, "missing retained native component bytes"
        )
        data = contents[resolved["resolved"]]
        d.require(d.sha(data) == resolved["sha256"], "native component bytes changed")
        result[path] = {"file": resolved, **elf_dynamic(d, data)}
    d.require(
        "libpython3.10.so.1.0" in result[PYTHON]["needed"]
        and result[LIBPYTHON]["soname"] == ["libpython3.10.so.1.0"],
        "interpreter/libpython ABI binding mismatch",
    )
    for path in deps:
        d.require(
            result[path]["soname"] == [posixpath.basename(path)],
            "dependency SONAME mismatch",
        )
    if provider == "cpython-runtime/v1":
        d.require(
            "libexpat.so.1" in result[imports["pyexpat"]]["needed"],
            "XML adapter does not bind expected Expat ABI",
        )
    else:
        d.require(
            {"libssl.so.3", "libcrypto.so.3"}.issubset(
                result[imports["cryptography.hazmat.bindings._rust"]]["needed"]
            ),
            "cryptography lacks reviewed dynamic OpenSSL dependencies",
        )
    return result


def verify_runtime(
    d, root, name, hashes, observation, artifact, imports, dependencies, files, config
):
    path = pin(d, root, name, hashes)
    evidence = d.parse(path.read_bytes())
    d.exact_keys(
        evidence,
        {
            "schema",
            "platform_digest",
            "config_digest",
            "source_revision",
            "artifact_id",
            "interpreter",
            "imports",
            "libraries",
            "invocation",
            "execution",
        },
        "runtime binding evidence",
    )
    d.require(
        evidence["schema"] == "adp-python-runtime-evidence/v1"
        and evidence["artifact_id"] == artifact["id"],
        "runtime component mismatch",
    )
    for key in ("platform_digest", "config_digest", "source_revision"):
        d.require(
            evidence[key] == observation[key], "runtime evidence image/source mismatch"
        )
    d.require(
        evidence["interpreter"]
        == {"path": PYTHON, "sha256": file_entry(d, files, PYTHON)["sha256"]},
        "runtime interpreter mismatch",
    )
    expected_imports = {
        name: {"path": path, "sha256": file_entry(d, files, path)["sha256"]}
        for name, path in imports.items()
    }
    d.require(evidence["imports"] == expected_imports, "runtime imported code mismatch")
    expected_libraries = {path: item["file"] for path, item in dependencies.items()}
    expected_libraries[LIBPYTHON] = file_entry(d, files, LIBPYTHON)
    d.require(
        evidence["libraries"] == expected_libraries, "runtime library binding mismatch"
    )
    execution = evidence["execution"]
    d.exact_keys(
        execution,
        {
            "argv",
            "user",
            "working_directory",
            "sys_path",
            "isolated",
            "no_site",
            "environment_overrides",
        },
        "runtime execution context",
    )
    image_config = config.get("config", {})
    cwd = image_config.get("WorkingDir") or "/"
    d.require(
        execution["user"] == image_config.get("User", "")
        and execution["working_directory"] == cwd,
        "runtime user/working directory mismatch",
    )
    d.require(
        execution["isolated"] == 0
        and execution["no_site"] == 0
        and execution["environment_overrides"] == {},
        "runtime probe changed default import context",
    )
    argv = execution["argv"]
    d.require(
        isinstance(argv, list)
        and len(argv) == 3
        and argv[:2] == [PYTHON, "-c"]
        and isinstance(argv[2], str)
        and argv[2],
        "runtime must use ordinary interpreter invocation",
    )
    d.require(
        execution["sys_path"]
        == ["", "/usr/local/lib/python310.zip", STDLIB, STDLIB + "/lib-dynload", SITE],
        "unsupported runtime module search path",
    )
    d.require("/usr/local/lib/python310.zip" not in files, "unsupported zipped stdlib")
    shadows = {
        "cryptography",
        "cryptography.py",
        "email",
        "email.py",
        "tarfile",
        "tarfile.py",
        "sitecustomize.py",
        "usercustomize.py",
        "pyexpat",
        "_elementtree",
    }
    for path in files:
        if beneath(path, cwd) and path != cwd:
            top = path[len(cwd.rstrip("/")) + 1 :].split("/", 1)[0]
            d.require(
                top not in shadows
                and not any(
                    top.startswith(name + ".") and top.endswith((".so", ".pyc"))
                    for name in (
                        "pyexpat",
                        "_elementtree",
                        "tarfile",
                        "cryptography",
                        "email",
                    )
                ),
                "working directory shadows reviewed Python code",
            )
    # The independent reviewer must inspect this retained non-isolated, default
    # import-context invocation; JSON claims are never execution attestations.
    pin(d, root, evidence["invocation"], hashes)
    return evidence


def collect_components(d, root, paths, observation, sbom, native, sarif, filename):
    hashes = {}
    manifest = d.parse(pin(d, root, filename, hashes).read_bytes())
    d.exact_keys(manifest, {"schema", "bindings"}, "component manifest")
    d.require(
        manifest["schema"] == "adp-python-component-bindings/v1"
        and isinstance(manifest["bindings"], list)
        and manifest["bindings"],
        "unsupported or empty component manifest",
    )
    config, config_digest, files, contents, layers = read_oci(
        d, paths["archive"], observation["platform_digest"]
    )
    d.require(
        config_digest == observation["config_digest"]
        and d.object_sha(files) == observation["files_sha256"],
        "component image changed",
    )
    env = config.get("config", {}).get("Env", [])
    d.require(
        not any(
            e.split("=", 1)[0]
            in {
                "PYTHONPATH",
                "PYTHONHOME",
                "PYTHONPYCACHEPREFIX",
            }
            or e.split("=", 1)[0].startswith("LD_")
            for e in env
        ),
        "unsupported image import/loader override",
    )
    inventories = {}
    artifact_map = {a["id"]: a for a in sbom["artifacts"]}
    all_component_paths = set()
    for binding in manifest["bindings"]:
        d.exact_keys(
            binding,
            {"provider", "package", "producer", "evidence", "dependencies"},
            "component binding",
        )
        provider = binding["provider"]
        d.require(provider in PROVIDERS, "unsupported component provider")
        identity = binding["package"]
        d.exact_keys(
            identity, {"id", "name", "version", "type", "purl"}, "component identity"
        )
        artifact = artifact_map.get(identity["id"])
        d.require(
            artifact is not None
            and d.package_identity(artifact) == identity
            and identity["id"] not in inventories,
            "component identity missing, changed or duplicated",
        )
        d.require(
            sum(a["purl"] == identity["purl"] for a in sbom["artifacts"]) == 1,
            "multiple component installations",
        )
        d.exact_keys(
            binding["producer"],
            {"archive", "platform_digest", "source_revision", "transition"},
            "component producer",
        )
        producer = binding["producer"]
        d.require(
            re.fullmatch(r"[0-9a-f]{40}", producer["source_revision"]),
            "producer source revision required",
        )
        producer_path = pin(d, root, producer["archive"], hashes)
        d.require(
            producer["platform_digest"] != observation["platform_digest"]
            and d.file_sha(producer_path) != observation["inputs"]["archive"],
            "candidate repack is not independent producer provenance",
        )
        pc, producer_config, pf, pr, _ = read_oci(
            d, producer_path, producer["platform_digest"]
        )
        d.require(
            producer_config != observation["config_digest"],
            "candidate config repack is not producer provenance",
        )
        label = (pc.get("config", {}).get("Labels") or {}).get(
            "org.opencontainers.image.revision"
        )
        d.require(
            label is None or label == producer["source_revision"],
            "producer source label disagrees with reviewed provenance",
        )
        d.require(
            producer["transition"] in {"identical/v1", "remove-bytecode/v1"},
            "unsupported producer transition",
        )
        transition = {"removed_caches": {}, "changed_records": {}}
        if producer["transition"] == "remove-bytecode/v1":
            pf, pr, transition = clean_producer(d, pf, pr)
        scope_fn = (
            runtime_scope if provider == "cpython-runtime/v1" else distribution_scope
        )
        owned, expected_locations, imports = scope_fn(d, artifact, files, contents)
        previous, _, _ = scope_fn(d, artifact, pf, pr)
        d.require(owned == previous, "producer/final complete component scope differs")
        context = import_context(d, files, contents)
        d.require(
            context == import_context(d, pf, pr),
            "producer/final import context differs",
        )
        d.require(
            not all_component_paths.intersection(owned),
            "overlapping component bindings",
        )
        all_component_paths.update(owned)
        bound_locations = locations(d, artifact, expected_locations, files, layers)
        d.exact_keys(binding["evidence"], ROLES, "component provenance roles")
        for name in binding["evidence"].values():
            pin(d, root, name, hashes)
        source_review = d.parse(
            d.local_file(root, binding["evidence"]["independent_review"]).read_bytes()
        )
        d.exact_keys(
            source_review,
            {
                "schema",
                "archive_sha256",
                "platform_digest",
                "config_digest",
                "source_revision",
                "source_evidence",
                "build_evidence",
                "reviewer",
                "conclusion",
            },
            "producer source-output review",
        )
        d.require(
            source_review["schema"] == "adp-producer-source-review/v1"
            and source_review["conclusion"] == "source-output-bound"
            and isinstance(source_review["reviewer"], str)
            and source_review["reviewer"].strip(),
            "independent producer source-output review required",
        )
        d.require(
            source_review["archive_sha256"] == hashes[producer["archive"]]
            and source_review["platform_digest"] == producer["platform_digest"]
            and source_review["config_digest"] == producer_config
            and source_review["source_revision"] == producer["source_revision"],
            "producer review artifact/source mismatch",
        )
        d.require(
            source_review["source_evidence"] == binding["evidence"]["source"]
            and source_review["build_evidence"] == binding["evidence"]["build"],
            "producer source/build evidence mismatch",
        )
        for package in observation["packages"].values():
            owned_resolved = {entry["resolved"] for entry in owned.values()}
            dpkg_resolved = {
                entry["resolved"]
                for entry in package["files"].values()
                if entry["kind"] == "file"
            }
            d.require(
                not owned_resolved.intersection(dpkg_resolved),
                "component overlaps root dpkg ownership",
            )
        deps = dependencies(
            d, root, binding["dependencies"], observation, files, hashes, provider
        )
        binary_bindings = binary_dependencies(
            d, files, contents, provider, imports, deps
        )
        runtime = verify_runtime(
            d,
            root,
            binding["evidence"]["runtime"],
            hashes,
            observation,
            artifact,
            imports,
            deps,
            files,
            config,
        )
        inventories[identity["id"]] = {
            "package": identity,
            "provider": provider,
            "producer": copy.deepcopy(producer),
            "files": owned,
            "import_context": context,
            "locations": bound_locations,
            "dependencies": deps,
            "runtime": runtime,
            "binary_dependencies": binary_bindings,
            "evidence": copy.deepcopy(binding["evidence"]),
            "producer_source_review": source_review,
            "transition": transition,
        }
    raw_matches = {d.object_sha(m): m for m in native["matches"]}
    for occurrence in observation["occurrences"]:
        identity = occurrence["package"]
        if identity is None or identity["id"] not in inventories:
            continue
        inventory = inventories[identity["id"]]
        if occurrence["advisory"] not in ADVISORIES[inventory["provider"]]:
            continue
        match = raw_matches[occurrence["native_sha256"]]
        d.require(
            match["artifact"].get("locations") == inventory["locations"],
            "component native/SBOM locations mismatch",
        )
        d.require(
            occurrence["gate_severity"] == occurrence["severity"].lower(),
            "component gate/native severity mismatch",
        )
        # Validate exact native/SARIF logical locations for every selected result,
        # not merely the rule metadata shared by a presenter.
        source = native["source"].get("metadata", native["source"].get("target", {}))
        result = sarif["runs"][0]["results"][occurrence["result_index"]]
        d.require(
            d.native_locations(match, source.get("userInput"))
            == d.sarif_locations(result),
            "component native/SARIF locations mismatch",
        )
        occurrence["disposition_eligible"] = occurrence["severity"].lower() in {
            "critical",
            "high",
            "medium",
            "low",
        }
    d.require(
        all(
            d.file_sha(d.local_file(root, name)) == value
            for name, value in hashes.items()
        ),
        "component evidence changed during collection",
    )
    observation.update(
        schema="adp-exact-image-observation/v2",
        components=inventories,
        component_inputs=hashes,
    )


def verify_decision(d, root, receipt, observation, decision, occurrence, component):
    d.require(
        occurrence["advisory"] in ADVISORIES[component["provider"]],
        "unsupported component advisory",
    )
    d.require(
        component["package"] == occurrence["package"],
        "decision component identity mismatch",
    )
    d.require(
        decision["files"] == component["files"],
        "component decision must bind complete owned scope",
    )
    for name, digest in observation["component_inputs"].items():
        d.require(
            receipt["evidence"].get(name) == digest
            and d.file_sha(d.local_file(root, name)) == digest,
            "component provenance omitted or changed after approval",
        )
    if decision["status"] == "fixed":
        d.require(
            decision["package_artifact"] == component["producer"]["archive"],
            "fixed component requires its verified producer artifact",
        )
    else:
        d.require(
            decision["package_artifact"] is None,
            "not-affected replacement artifact must be null",
        )
