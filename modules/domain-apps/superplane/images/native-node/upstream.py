"""Produce native inputs from pinned published artifacts, never a prepared root."""

from pathlib import PurePosixPath
import posixpath
import shutil
import subprocess
import tarfile

import native_image as image

NODEADM_SOURCE_FILES = {
    "Makefile": "59e7ff23d97bd5826ef4e7d1d281cf18e5b6764ee201b28b6257d9d9375ddbb7",
    "vendor/modules.txt": "122ca3da0090962828234c1b00a724d493c4947f5f757abdd4bb64cbedef8686",
}
MAX_EMITTED_BYTES = 16 * 1024**3
MAX_EMITTED_FILES = 100000


def archive_entries(archive):
    entries = {}
    for member in archive.getmembers():
        name = posixpath.normpath(member.name)
        if name == "." and member.isdir():
            continue
        if (
            member.name.startswith("/")
            or name.startswith("../")
            or name == ".."
            or name in entries
        ):
            raise image.ImageRefused("unsafe or duplicate upstream archive member")
        if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
            raise image.ImageRefused("upstream archive contains a special file")
        entries[name] = member
    if (
        len(entries) > 100000
        or sum(member.size for member in entries.values()) > 16 * 1024**3
    ):
        raise image.ImageRefused("upstream archive exceeds build bound")
    return entries


def regular_member(entries, name, prefix, visited=()):
    if (
        name in visited
        or len(visited) > 40
        or not PurePosixPath(name).is_relative_to(prefix)
    ):
        raise image.ImageRefused("upstream link escapes its artifact or cycles")
    member = entries.get(name)
    if member is None:
        raise image.ImageRefused("upstream link target is missing")
    if member.issym() or member.islnk():
        if member.linkname.startswith("/"):
            raise image.ImageRefused("absolute upstream archive link refused")
        target = posixpath.normpath(
            posixpath.join(posixpath.dirname(name), member.linkname)
            if member.issym()
            else member.linkname
        )
        return regular_member(entries, target, prefix, (*visited, name))
    if not member.isfile():
        raise image.ImageRefused("directory aliases require separate artifact review")
    return member


def unpack_regular(archive_path, prefix, destination):
    """Normalize vetted internal file links; no target interpreter is executed."""
    prefix = PurePosixPath(prefix)
    destination.mkdir(parents=True, exist_ok=False)
    with tarfile.open(archive_path, "r:*") as archive:
        entries = archive_entries(archive)
        count = 0
        emitted_bytes = 0
        for name, original in entries.items():
            path = PurePosixPath(name)
            if not path.is_relative_to(prefix) or path == prefix:
                continue
            target = destination.joinpath(*path.relative_to(prefix).parts)
            if original.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            member = regular_member(entries, name, prefix)
            count += 1
            emitted_bytes += member.size
            if count > MAX_EMITTED_FILES or emitted_bytes > MAX_EMITTED_BYTES:
                raise image.ImageRefused(
                    "materialized upstream output exceeds build bound"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, target.open("xb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    if not count:
        raise image.ImageRefused("upstream prefix contained no regular files")


def pinned_archive(inputs, artifact):
    path = inputs / artifact["file"]
    if path.is_symlink() or image.file_sha(path) != artifact["sha256"]:
        raise image.ImageRefused("published input archive differs from reviewed digest")
    return path


def command(arguments, **kwargs):
    subprocess.run(arguments, check=True, timeout=900, **kwargs)


def verify_nodeadm_source(source):
    for name, expected in NODEADM_SOURCE_FILES.items():
        path = source / name
        if path.is_symlink() or not path.is_file() or image.file_sha(path) != expected:
            raise image.ImageRefused(
                "nodeadm vendored build source differs from pinned upstream"
            )


def cni_sources(plan):
    """Return a validated copy; never rewrite an approved canonical input plan."""
    upstream = plan["upstream"]
    if type(plan["version"]) is not int:
        raise image.ImageRefused("unsupported producer input")
    if plan["version"] == 1:
        sources = [{"image": upstream["cni_image"], "files": upstream["cni_files"]}]
    elif plan["version"] == 2:
        sources = upstream["cni_sources"]
        if not isinstance(sources, list) or not 1 <= len(sources) <= 2:
            raise image.ImageRefused("one or two explicit CNI sources required")
    else:
        raise image.ImageRefused("unsupported producer input")
    destinations = set()
    validated = []
    for source in sources:
        image.exact(source, {"image", "files"})
        image.pattern(
            source["image"], r"[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[a-f0-9]{64}"
        )
        files = source["files"]
        if not isinstance(files, dict) or not 1 <= len(files) <= 64:
            raise image.ImageRefused("bounded explicit CNI image paths required")
        for original, target in files.items():
            image.pattern(original, r"/[A-Za-z0-9._/-]+")
            image.pattern(target, r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
            if ".." in PurePosixPath(original).parts:
                raise image.ImageRefused("CNI image path escapes")
            if plan["version"] == 2 and (
                str(PurePosixPath(original)) != original or original.startswith("//")
            ):
                raise image.ImageRefused("canonical CNI image path required")
            if target in destinations:
                raise image.ImageRefused("duplicate global CNI destination")
            destinations.add(target)
        validated.append({"image": source["image"], "files": dict(files)})
    return validated


def assemble_cni(plan, cni):
    sources = cni_sources(plan)  # Refuse all map conflicts before commands/writes.
    cni.mkdir()
    provenance = []
    for source in sources:
        command(["docker", "pull", "--platform", "linux/amd64", source["image"]])
        created = subprocess.run(
            ["docker", "create", "--platform", "linux/amd64", source["image"]],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        ).stdout.strip()
        image.pattern(created, r"[a-f0-9]{64}")
        files = {}
        try:
            for original, name in source["files"].items():
                target = cni / name
                command(["docker", "cp", created + ":" + original, str(target)])
                if target.is_symlink() or not target.is_file():
                    raise image.ImageRefused(
                        "CNI image path is not a regular executable"
                    )
                target.chmod(0o755)
                files[original] = {
                    "destination": name,
                    "sha256": image.file_sha(target),
                }
        finally:
            command(["docker", "rm", created])
        provenance.append({"image": source["image"], "files": files})
    return provenance


def assemble(plan, inputs, stage):
    cni_sources(plan)
    """Run only in the approved remote build lane. No EC2 or target boot here."""
    upstream = plan["upstream"]
    # Verify every input before running the compiler or creating an output tree.
    archives = {
        key: pinned_archive(inputs, upstream[key])
        for key in ("python", "nodeadm", "crictl")
    }
    stage.mkdir(mode=0o700)
    unpack_regular(archives["python"], upstream["python"]["prefix"], stage / "python")
    unpack_regular(
        archives["nodeadm"], upstream["nodeadm"]["prefix"], stage / "nodeadm-source"
    )
    unpack_regular(
        archives["crictl"], upstream["crictl"]["prefix"], stage / "crictl-source"
    )
    source = stage / "nodeadm-source/nodeadm"
    verify_nodeadm_source(source)
    command(["docker", "pull", "--platform", "linux/amd64", upstream["go_image"]])
    command(
        [
            "docker",
            "run",
            "--rm",
            "--platform",
            "linux/amd64",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,size=2g",
            "--mount",
            "type=bind,source=" + str(source) + ",target=/src",
            "--workdir",
            "/src",
            "--env",
            "GOTOOLCHAIN=local",
            "--env",
            "CGO_ENABLED=0",
            "--env",
            "GOOS=linux",
            "--env",
            "GOARCH=amd64",
            "--env",
            "GOFLAGS=-mod=vendor",
            "--env",
            "GOCACHE=/tmp/gocache",
            "--env",
            "GOPATH=/tmp/gopath",
            upstream["go_image"],
            "make",
            "release",
        ]
    )
    binaries = stage / "binaries"
    binaries.mkdir()
    for name in ("nodeadm", "nodeadm-internal"):
        built = source / "_bin" / name
        if built.is_symlink() or not built.is_file():
            raise image.ImageRefused(
                "nodeadm build did not produce both fixed binaries"
            )
        shutil.copyfile(built, binaries / name)
        (binaries / name).chmod(0o755)
    crictl = stage / "crictl-source/crictl"
    if not crictl.is_file() or crictl.is_symlink():
        raise image.ImageRefused("pinned crictl executable missing")
    shutil.copyfile(crictl, binaries / "crictl")
    (binaries / "crictl").chmod(0o755)
    cni = stage / "cni"
    cni_provenance = assemble_cni(plan, cni)
    # The portable Python is used by the helper, so no target-side provisioning
    # Python/package installation is required. No shell or target init is started.
    command(
        [
            str(stage / "python/bin/python3"),
            "-I",
            "-B",
            "-S",
            "-c",
            "import sys,ssl,hashlib,urllib.request,tarfile; assert sys.version_info[:2] == (3,12)",
        ],
        env={"PATH": "/usr/bin:/bin", "LANG": "C"},
    )
    return {
        "source_revision": plan["source_revision"],
        "upstream": upstream,
        "nodeadm_source_commit": image.runner.NODEADM_COMMIT,
        "nodeadm_source_files": NODEADM_SOURCE_FILES,
        "binaries": {path.name: image.file_sha(path) for path in binaries.iterdir()},
        "cni": {path.name: image.file_sha(path) for path in cni.iterdir()},
        "cni_sources": cni_provenance,
        "python_files": {
            str(path.relative_to(stage / "python")): image.file_sha(path)
            for path in (stage / "python").rglob("*")
            if path.is_file()
        },
    }
