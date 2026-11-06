"""Pack, relocate and validate the Kiro Crew bundle (#8054).

scripts/build-crew-bundle.sh assembles a staging tree (venv/, python/, clients/,
host/, models/, default-agent.json) from the pins in image/crew-bundle.pins and
calls this helper to:

  normalize-venv  relocate the staged virtual environment to the installed
                  prefix (/opt/adp/crew/current) so the archive is independent
                  of the build directory,
  pack            stream crew.tar.gz deterministically (sorted members, fixed
                  mtime, root ownership, gzip without timestamp; never more than
                  one chunk in memory) and the profile.json manifest that
                  image/install_crew.py accepts,
  validate        re-check an existing bundle and manifest with the installer's
                  manifest validator and its member rules, iterating the archive
                  once without extracting it; reports size and peak RSS.

No network access happens here; downloads and builds are the shell script's job.
"""

import argparse
import gzip
import hashlib
import importlib.util
import json
import os
import re
import resource
import stat
import sys
import tarfile
from pathlib import Path

IMAGE_DIR = Path(__file__).resolve().parents[1] / "image"
PINS_FILE = IMAGE_DIR / "crew-bundle.pins"
REVISION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SOURCE_PATTERN = re.compile(r"[0-9a-f]{40}")


def load_installer():
    spec = importlib.util.spec_from_file_location("dev_box_install_crew", IMAGE_DIR / "install_crew.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_pins(path: Path = PINS_FILE) -> dict:
    pins = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"malformed pin line: {line}")
        pins[key] = value.strip()
    return pins


def required_members() -> tuple:
    """The members install_crew._unpack insists on, read from its source so the two never drift."""
    source = (IMAGE_DIR / "install_crew.py").read_text()
    match = re.search(r"required = \((.*?)\)\n", source, re.S)
    if not match:
        raise ValueError("install_crew.py no longer declares its required bundle members")
    return tuple(re.findall(r'"([^"]+)"', match.group(1)))


def _digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


# --- normalize-venv ----------------------------------------------------------
def normalize_venv(venv: Path, stage: Path, install_prefix: str) -> dict:
    """Relocate a venv created against <stage>/python so it runs from <install_prefix>.

    * console-script shebangs <stage>/venv/bin/python* -> <install_prefix>/venv/bin/python3
    * pyvenv.cfg paths under <stage> -> <install_prefix>
    * bin/python symlinks -> a relative link into ../../python/bin (the data
      filter used by install_crew refuses absolute link targets)
    * pip's direct_url.json (carries the build-time wheel path) and byte-code
      caches are removed so the archive does not depend on the build directory.
    """
    venv, stage = venv.resolve(), stage.resolve()
    if not (venv / "pyvenv.cfg").is_file():
        raise ValueError(f"{venv} is not a virtual environment")
    stage_prefix = str(stage)
    rewritten = {"shebangs": 0, "links": 0, "removed": 0}
    interpreters = sorted(p.name for p in (stage / "python" / "bin").glob("python3.*") if not p.is_symlink()) \
        if (stage / "python" / "bin").is_dir() else []
    target_interpreter = interpreters[0] if interpreters else "python3"
    for entry in sorted((venv / "bin").iterdir()):
        if entry.is_symlink():
            if entry.name.startswith("python"):
                entry.unlink()
                if entry.name == "python":
                    entry.symlink_to(Path("..") / ".." / "python" / "bin" / target_interpreter)
                else:
                    entry.symlink_to("python")
                rewritten["links"] += 1
            continue
        if not entry.is_file():
            continue
        head = entry.read_bytes()[:2048]
        if head.startswith(b"#!") and stage_prefix.encode() in head.split(b"\n", 1)[0]:
            body = entry.read_bytes()
            first, _, rest = body.partition(b"\n")
            entry.write_bytes(f"#!{install_prefix}/venv/bin/python3".encode() + b"\n" + rest)
            rewritten["shebangs"] += 1
    config = venv / "pyvenv.cfg"
    config.write_text(config.read_text().replace(stage_prefix, install_prefix))
    for direct_url in venv.glob("lib/python*/site-packages/*.dist-info/direct_url.json"):
        record = direct_url.with_name("RECORD")
        if record.is_file():
            record.write_text("".join(line for line in record.read_text().splitlines(keepends=True)
                                      if not line.startswith(f"{direct_url.parent.name}/direct_url.json,")))
        direct_url.unlink()
        rewritten["removed"] += 1
    for cache in sorted(venv.rglob("__pycache__"), reverse=True):
        for item in cache.iterdir():
            item.unlink()
        cache.rmdir()
    return rewritten


# --- pack --------------------------------------------------------------------
def _normalized_mode(path: Path) -> int:
    mode = path.lstat().st_mode
    if stat.S_ISDIR(mode):
        return 0o755
    return 0o755 if mode & stat.S_IXUSR else 0o644


def _check_link(stage: Path, member: Path) -> None:
    target = os.readlink(member)
    if os.path.isabs(target):
        raise ValueError(f"symlink {member.relative_to(stage)} has an absolute target; the installer refuses it")
    resolved = os.path.normpath(os.path.join(member.parent, target))
    if os.path.commonpath([str(stage), resolved]) != str(stage):
        raise ValueError(f"symlink {member.relative_to(stage)} escapes the bundle")


def pack(stage: Path, output_dir: Path, *, revision: str, source_revision: str, crew_version: str,
         source_date_epoch: int = 0) -> dict:
    stage = stage.resolve()
    if not REVISION_PATTERN.fullmatch(revision):
        raise ValueError("invalid Crew profile revision")
    if not SOURCE_PATTERN.fullmatch(source_revision):
        raise ValueError("Crew source must be pinned to a full revision")
    missing = [member for member in required_members() if not (stage / member).is_file()]
    if missing:
        raise ValueError("Crew bundle is incomplete: missing " + ", ".join(missing))
    output_dir.mkdir(parents=True, exist_ok=True)
    bundle = output_dir / "crew.tar.gz"
    manifest_path = output_dir / "profile.json"
    # Stream member by member straight into gzip: the bundle is ~1.5 GB
    # uncompressed and the build runs on a small CodeBuild host, so nothing
    # larger than one 1 MiB chunk may ever be held in memory.
    with bundle.open("wb") as target, \
            gzip.GzipFile(fileobj=target, mode="wb", mtime=0, filename="") as compressed, \
            tarfile.open(fileobj=compressed, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for path in sorted(p for p in stage.rglob("*")):
            relative = path.relative_to(stage)
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                _check_link(stage, path)
                info = tarfile.TarInfo(str(relative))
                info.type = tarfile.SYMTYPE
                info.linkname = os.readlink(path)
                info.mode = 0o777
            elif stat.S_ISDIR(mode):
                info = tarfile.TarInfo(str(relative))
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
            elif stat.S_ISREG(mode):
                info = tarfile.TarInfo(str(relative))
                info.size = path.stat().st_size
                info.mode = _normalized_mode(path)
            else:
                raise ValueError(f"{relative} is not a file, directory or symlink")
            info.mtime = source_date_epoch
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if info.isreg():
                with path.open("rb") as source:
                    archive.addfile(info, source)
            else:
                archive.addfile(info)
    manifest = {"schema_version": 1, "revision": revision, "source_revision": source_revision,
                "crew_version": crew_version, "bundle_sha256": _digest(bundle)}
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n")
    return validate(bundle, manifest_path, expected_revision=revision)


# --- validate ----------------------------------------------------------------
def inspect_members(bundle: Path) -> dict:
    """Walk the archive once with the installer's rules, without extracting anything.

    Mirrors install_crew._unpack: only files, directories and symlinks; every
    member must pass tarfile's "data" filter (no absolute or escaping paths or
    links); every required member must be a regular file. Returns the member
    count and the uncompressed size.
    """
    files, links, count, uncompressed = set(), {}, 0, 0
    # Random-access mode, not "r|gz": the stream reader decompresses whole input
    # chunks, which for highly compressible data can expand to hundreds of MB at
    # once; GzipFile seeks forward in bounded reads instead.
    with tarfile.open(bundle, "r:gz") as archive:
        for member in archive:
            count += 1
            if not (member.isfile() or member.isdir() or member.issym()):
                raise ValueError("Crew bundle cannot contain devices or hard links")
            try:
                tarfile.data_filter(member, "/nonexistent-crew-root")
            except tarfile.FilterError as error:
                raise ValueError("Crew bundle contains unsafe paths") from error
            if member.isfile():
                files.add(os.path.normpath(member.name))
                uncompressed += member.size
            elif member.issym():
                links[os.path.normpath(member.name)] = member.linkname

    def is_file(name: str, hops: int = 8) -> bool:  # like Path.is_file() after extraction: follow symlinks
        while hops and name in links:
            name = os.path.normpath(os.path.join(os.path.dirname(name), links[name]))
            hops -= 1
        return name in files

    missing = sorted(member for member in required_members() if not is_file(member))
    if missing:
        raise ValueError("Crew bundle is incomplete: missing " + ", ".join(missing))
    return {"members": count, "uncompressed_bytes": uncompressed}


def validate(bundle: Path, manifest_path: Path, *, expected_revision: str | None = None) -> dict:
    """Run the installer's manifest checks and its member rules without installing or extracting."""
    installer = load_installer()
    manifest = json.loads(manifest_path.read_text())
    revision, checksum = installer._profile(manifest)
    if expected_revision is not None and revision != expected_revision:
        raise ValueError("manifest revision differs from the requested profile revision")
    if not bundle.is_file() or _digest(bundle) != checksum:
        raise ValueError("Crew bundle checksum mismatch")
    inspected = inspect_members(bundle)
    peak_rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {"bundle": str(bundle), "manifest": str(manifest_path), "revision": revision,
            "source_revision": manifest["source_revision"], "crew_version": manifest["crew_version"],
            "bundle_sha256": checksum, "members": inspected["members"], "uncompressed_bytes": inspected["uncompressed_bytes"],
            "size_bytes": bundle.stat().st_size, "peak_rss_mb": round(peak_rss_kb / 1024, 1)}


def diff_pins(dockerfile: Path, pins: dict) -> list:
    """Lines of Dockerfile.sandbox that should carry each shared pin but do not."""
    text = dockerfile.read_text()
    expected = [pins["KIROCREW_WHEEL_URL"], pins["KIROCREW_WHEEL_SHA256"], f"awscli=={pins['AWSCLI_VERSION']}",
                pins["EMBEDDING_MODEL_URL"], pins["EMBEDDING_MODEL_SHA256"], pins["EMBEDDING_MODEL_FILE"],
                pins["CLIENTS_DIR"].split("/")[-1]]
    return [value for value in expected if value not in text]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    normalize = commands.add_parser("normalize-venv")
    normalize.add_argument("--venv", type=Path, required=True)
    normalize.add_argument("--stage", type=Path, required=True)
    normalize.add_argument("--install-prefix", default=None)
    packer = commands.add_parser("pack")
    packer.add_argument("--stage", type=Path, required=True)
    packer.add_argument("--output-dir", type=Path, required=True)
    packer.add_argument("--revision", required=True)
    packer.add_argument("--source-revision", required=True)
    packer.add_argument("--crew-version", default=None)
    packer.add_argument("--source-date-epoch", type=int, default=0)
    checker = commands.add_parser("validate")
    checker.add_argument("--bundle", type=Path, required=True)
    checker.add_argument("--manifest", type=Path, required=True)
    checker.add_argument("--expected-revision")
    pins_cmd = commands.add_parser("pins")
    pins_cmd.add_argument("--dockerfile", type=Path, default=None)
    arguments = parser.parse_args(argv)
    pins = read_pins()
    try:
        if arguments.command == "normalize-venv":
            result = normalize_venv(arguments.venv, arguments.stage, arguments.install_prefix or pins["INSTALL_PREFIX"])
        elif arguments.command == "pack":
            result = pack(arguments.stage, arguments.output_dir, revision=arguments.revision,
                          source_revision=arguments.source_revision,
                          crew_version=arguments.crew_version or pins["CREW_VERSION"],
                          source_date_epoch=arguments.source_date_epoch)
        elif arguments.command == "validate":
            result = validate(arguments.bundle, arguments.manifest, expected_revision=arguments.expected_revision)
        else:
            result = dict(pins)
            if arguments.dockerfile is not None:
                drift = diff_pins(arguments.dockerfile, pins)
                if drift:
                    raise ValueError("Dockerfile.sandbox does not carry: " + "; ".join(drift))
    except ValueError as error:
        print(f"crew_bundle.py: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
