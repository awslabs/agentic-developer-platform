"""Exact registry-tag and OCI identity for an optional reviewed API Python base."""

from dataclasses import dataclass
import re

from .config import require


@dataclass(frozen=True)
class ImageProvenance:
    component: str
    source_revision: str
    python_image: str | None = None

    @classmethod
    def from_source(cls, component, source):
        revision = source.get("source_revision")
        require(
            isinstance(revision, str) and re.fullmatch(r"[a-f0-9]{40}", revision),
            "Image provenance needs an exact source revision: " + component,
        )
        base = source.get("python_image")
        if "python_image" in source:
            require(
                component == "superplane-api"
                and isinstance(base, str)
                and len(base) <= 2048
                and re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", base)
                and not base.endswith("@sha256:" + "0" * 64),
                "Selected Python base must be an exact API image reference",
            )
        return cls(component, revision, base)

    @property
    def registry_tags(self):
        if self.python_image is None:
            return (self.source_revision, self.source_revision[:12])
        return (self.source_revision + "-py-" + self.python_image.rsplit(":", 1)[1],)

    def verify_tags(self, tags):
        require(
            isinstance(tags, list) and any(tag in self.registry_tags for tag in tags),
            "Registry does not bind image to source and selected base: "
            + self.component,
        )

    def verify_labels(self, labels):
        require(
            isinstance(labels, dict)
            and labels.get("org.opencontainers.image.revision") == self.source_revision
            and (
                self.python_image is None
                or labels.get("org.opencontainers.image.base.name") == self.python_image
            ),
            "Image OCI provenance does not match source and selected base: "
            + self.component,
        )
