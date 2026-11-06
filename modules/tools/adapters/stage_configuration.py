"""Bind deployment-owned release generations to the newly published image."""

import argparse
import os
import re
from pathlib import Path

from tool_adapters.config import AdapterConfiguration, ObservationProfile, load_configuration


def configuration(publication, *, source, group_generation, backend_generation):
    if (
        publication.get("schema_version") != "1.0"
        or publication.get("source_revision") != source
        or not isinstance(publication.get("image_digest"), str)
        or re.fullmatch(r"[0-9a-f]{40}", source) is None
    ):
        raise ValueError("adapter_stage_publication_changed")
    generations = (group_generation, backend_generation)
    if any(not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,9}", value) for value in generations):
        raise ValueError("adapter_stage_generation_invalid")
    return AdapterConfiguration.model_validate(
        {
            "schema_version": "1.0",
            "mode": "foundation",
            "release": {
                "contract_version": "1.0",
                "source_revision": source,
                "image_digest": publication["image_digest"],
                "group": "atlassian-foundation",
                "group_generation": int(group_generation),
                "backend_generation": int(backend_generation),
            },
        }
    )


def main():
    from release_evidence import read_document

    parser = argparse.ArgumentParser()
    parser.add_argument("--publication", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    configured = configuration(
        read_document(args.publication),
        source=os.environ["ADP_SOURCE_REVISION"],
        group_generation=os.environ["ADP_TOOLS_ADAPTER_GROUP_GENERATION"],
        backend_generation=os.environ["ADP_TOOLS_ADAPTER_BACKEND_GENERATION"],
    )
    observation_file = os.environ.get("ADP_TOOLS_ADAPTER_OBSERVATION_FILE")
    if observation_file is not None:
        document = read_document(observation_file)
        if "source_revision" in document:
            profile = ObservationProfile.model_validate(document)
            if profile.source_revision != configured.release.source_revision:
                raise ValueError("adapter_observation_release_mismatch")
            observed = AdapterConfiguration(
                schema_version="1.0", mode="observation", release=configured.release, observation=profile.observation
            )
        else:
            observed = load_configuration(observation_file)
        if observed is None or observed.mode != "observation" or observed.release != configured.release:
            raise ValueError("adapter_observation_release_mismatch")
        configured = observed
    path = Path(args.output)
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(configured.model_dump_json() + "\n")


if __name__ == "__main__":
    main()
