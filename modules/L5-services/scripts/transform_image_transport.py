"""Keep registry authorization on the coordinator when Docker uses a remote daemon."""

import gzip
import hashlib
import json
import os
import re
import tarfile
from pathlib import Path


DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")
MANIFEST_TYPES = {
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def hashed(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


def verify_archive(path, *, image_id=None, descriptor=None, expected_config=None):
    """Verify a single image without extracting any archive entry to disk."""
    with tarfile.open(path, "r:") as archive:
        members = archive.getmembers()
        require(len({entry.name for entry in members}) == len(members), "Image archive has duplicate entries")

        def entry(name):
            require(isinstance(name, str) and not name.startswith("/")
                    and not {"", ".", ".."}.intersection(name.split("/")), "Image archive path is invalid")
            member = archive.getmember(name)
            require(member.isfile(), "Image archive entry is not a regular file")
            return member

        def read(name, limit):
            member = entry(name)
            require(member.size <= limit, "Image archive metadata exceeds its bound")
            return archive.extractfile(member).read()

        manifest = json.loads(read("manifest.json", 65536))
        require(isinstance(manifest, list) and len(manifest) == 1, "Image archive must contain one image")
        raw = read(manifest[0]["Config"], 1048576)
        config_digest = hashed(raw)
        config = json.loads(raw)
        require(config.get("os") == "linux" and config.get("architecture") == "amd64",
                "Image archive platform must be linux/amd64")
        if expected_config is not None:
            require(config_digest == expected_config, "Image archive differs from the pinned registry config")
        layers = manifest[0].get("Layers")
        diffs = config.get("rootfs", {}).get("diff_ids")
        require(isinstance(layers, list) and isinstance(diffs, list) and len(layers) == len(diffs),
                "Image archive layer list differs from its config")
        media_types = ["application/vnd.oci.image.layer.v1.tar"] * len(layers)
        if descriptor is None:
            if image_id is not None:
                require(image_id == config_digest, "Image archive differs from the inspected config ID")
        else:
            require(descriptor.get("mediaType") in MANIFEST_TYPES
                    and descriptor.get("digest") == image_id
                    and isinstance(image_id, str) and DIGEST.fullmatch(image_id),
                    "Docker descriptor must identify one immutable image manifest")
            manifest_raw = read("blobs/sha256/" + image_id.removeprefix("sha256:"), 65536)
            selected = json.loads(manifest_raw)
            require(hashed(manifest_raw) == image_id and len(manifest_raw) == descriptor.get("size")
                    and selected.get("schemaVersion") == 2 and "manifests" not in selected
                    and selected.get("mediaType") == descriptor["mediaType"]
                    and selected.get("config", {}).get("digest") == config_digest
                    and selected["config"].get("size") == len(raw),
                    "Image archive manifest/config differs from the inspected descriptor")
            selected_layers = selected.get("layers")
            require(isinstance(selected_layers, list) and len(selected_layers) == len(layers),
                    "Image archive layers differ from the inspected manifest")
            for layer, name in zip(selected_layers, layers):
                require(isinstance(layer, dict) and isinstance(layer.get("digest"), str)
                        and DIGEST.fullmatch(layer["digest"])
                        and name == "blobs/sha256/" + layer["digest"].removeprefix("sha256:")
                        and entry(name).size == layer.get("size")
                        and "sha256:" + hashlib.file_digest(archive.extractfile(entry(name)), "sha256").hexdigest()
                        == layer["digest"],
                        "Image archive layer differs from the inspected manifest")
            media_types = [layer.get("mediaType") for layer in selected_layers]
            annotation = descriptor.get("annotations", {}).get("config.digest")
            require(annotation in {None, config_digest}, "Docker config annotation differs from archive")
        # Legacy Docker archives have uncompressed layers; containerd exports
        # may retain gzip blobs. Authenticate both their stored and diff bytes.
        for name, expected, media in zip(layers, diffs, media_types):
            require(isinstance(expected, str) and DIGEST.fullmatch(expected), "Image layer digest is invalid")
            require(media in {"application/vnd.oci.image.layer.v1.tar", "application/vnd.oci.image.layer.v1.tar+gzip",
                              "application/vnd.docker.image.rootfs.diff.tar", "application/vnd.docker.image.rootfs.diff.tar.gzip"},
                    "Image layer compression is unsupported")
            stream = archive.extractfile(entry(name))
            if media.endswith(("+gzip", ".gzip")):
                stream = gzip.GzipFile(fileobj=stream)
            actual = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
            require(actual == expected, "Image archive layer differs from its config")
        return config_digest


class ImageTransport:
    def __init__(self, environment, workspace, run, registry, region):
        self.environment, self.workspace, self.run = environment, Path(workspace), run
        self.registry, self.region = registry, region
        self.mode = environment.get("L5_TRANSFORM_IMAGE_TRANSPORT", "docker")
        self.validate(environment)
        self.docker_config = self.workspace / "docker-config"
        self.docker_config.mkdir(mode=0o700)
        plugins = environment.get("L5_TRANSFORM_DOCKER_CLI_PLUGINS_DIR")
        configuration = {"auths": {}}
        if plugins:
            require(Path(plugins).is_absolute() and Path(plugins).is_dir(), "Select an existing Docker plugin directory")
            configuration["cliPluginsExtraDirs"] = [str(Path(plugins).resolve())]
        (self.docker_config / "config.json").write_text(json.dumps(configuration))
        (self.docker_config / "config.json").chmod(0o600)
        self.auth = None

    @staticmethod
    def validate(environment):
        mode = environment.get("L5_TRANSFORM_IMAGE_TRANSPORT", "docker")
        require(mode in {"docker", "archive"}, "Select docker or archive Transform image transport")
        host = environment.get("DOCKER_HOST", "")
        require(mode == "archive" or not host or host.startswith("unix://"),
                "Remote Docker requires archive transport; registry authorization must stay local")

    def client_environment(self, docker=False):
        allowed = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR"}
        if docker:
            allowed |= {"DOCKER_HOST", "SSH_AUTH_SOCK", "SSH_AGENT_PID"}
        return {key: value for key, value in self.environment.items() if key in allowed}

    def docker(self, command, **kwargs):
        return self.run(["docker", "--config", str(self.docker_config), *command[1:]],
                        env=self.client_environment(docker=True), **kwargs)

    def registry_auth(self):
        if self.auth is None:
            password = self.run(["aws", "ecr", "get-login-password", "--region", self.region])
            require(password.strip(), "Transform registry authorization is unavailable")
            self.auth = self.workspace / "registry-auth.json"
            descriptor = os.open(self.auth, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w") as target:
                target.write('{"auths": {}}')
            self.run(["skopeo", "login", "--authfile", str(self.auth), "--username", "AWS",
                      "--password-stdin", self.registry], input=password, env=self.client_environment())
            require(self.auth.is_file() and not self.auth.is_symlink() and not self.auth.stat().st_mode & 0o077,
                    "Registry auth file must remain private")
        return self.auth

    def login_docker(self):
        password = self.run(["aws", "ecr", "get-login-password", "--region", self.region])
        require(password.strip(), "Transform registry authorization is unavailable")
        self.docker(["docker", "login", "--username", "AWS", "--password-stdin", self.registry], input=password)

    def export(self, observed, label):
        path = self.workspace / (label + "-export.tar")
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            self.docker(["docker", "image", "save", observed["Id"]], stdout=output)
            output.flush()
            os.fsync(output.fileno())
        config_digest = verify_archive(path, image_id=observed["Id"], descriptor=observed.get("Descriptor"))
        return path, config_digest

    def import_pinned(self, reference, label, expected_config=None):
        """Only image bytes cross SSH; Skopeo resolves/authenticates locally."""
        auth = self.registry_auth()
        raw = self.run(["skopeo", "inspect", "--raw", "--authfile", str(auth), "docker://" + reference],
                       env=self.client_environment())
        manifest = json.loads(raw)
        require(hashed(raw) == reference.rsplit("@", 1)[1] and manifest.get("schemaVersion") == 2
                and "manifests" not in manifest and manifest.get("mediaType") in MANIFEST_TYPES,
                "Transform base must pin one linux/amd64 image manifest")
        config_digest = manifest.get("config", {}).get("digest")
        require(isinstance(config_digest, str) and DIGEST.fullmatch(config_digest)
                and expected_config in {None, config_digest}, "Pinned image config differs from registry readback")
        alias = "adp-l5-transform-input:" + config_digest.removeprefix("sha256:")
        path = self.workspace / (label + "-input.tar")
        self.run(["skopeo", "copy", "--src-authfile", str(auth), "--src-tls-verify=true",
                  "--override-os", "linux", "--override-arch", "amd64", "docker://" + reference,
                  "docker-archive:" + str(path) + ":" + alias], env=self.client_environment())
        require(not path.is_symlink(), "Image import archive cannot be a symlink")
        path.chmod(0o600)
        verify_archive(path, expected_config=config_digest)
        # --input is a local CLI file, streamed over the Docker API to the daemon.
        self.docker(["docker", "image", "load", "--input", str(path)])
        observed = json.loads(self.docker(["docker", "image", "inspect", alias]))
        require(len(observed) == 1, "Imported image is missing from the builder")
        _, imported_config = self.export(observed[0], label + "-imported")
        require(imported_config == config_digest, "Imported builder image differs from the pinned registry image")
        return alias

    def push_archive(self, archive, target):
        auth = self.registry_auth()
        self.run(["skopeo", "copy", "--dest-authfile", str(auth), "--dest-tls-verify=true", "--format", "v2s2",
                  "docker-archive:" + str(archive), "docker://" + target], env=self.client_environment())
