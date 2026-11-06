"""Reviewed historical bootstrap code: every cluster mutation requires a journal.

This qualification is intentionally finite. The public-network path only reads
AWS before BootstrapAuthorityFactory/TemporaryAuthority durably establishes the
bootstrap journal. Gateway separately requires that no such journal, network
intent, reservation, component, membership or completed artifact exists, after
all original execution and session authority has expired. An empty directory or
an empty cloud listing cannot qualify this boundary.
"""

SOURCE_REVISION = "12bf1c9fbe5792670996733007a2528eec763cd9"
SOURCE_HASHES = {
    "workspace_provisioning/runtime.py": "da5d0c6c735d003976fbdc2ba2569deafb40aad9afbff2b074b2e3aae9d3475b",
    "workspace_provisioning/bootstrap_runtime.py": "784527077429b0041d50e802bdcee12fe008bfc087a930bd4988dc870c308c96",
    "workspace_provisioning/network.py": "84d124c105b5c3564f3b9c647bc646889a7b69b3ecd3212bc8ffc09387d70433",
    "workspace_provisioning/credentials.py": "14d0c70d973645873299f7ab3fb2ddacb1bdc82efef80ee4d7f26e9a7761cfd6",
    "workspace_provisioning/cluster_clients.py": "f7eed9c543f390a9dd79e466b5962ae478087ff25cbd2b3b7fd94ad271cb3fc6",
    "workspace_provisioning/process.py": "b16587aef5a5eae589af5fc71e5f44a6ab3f14875f4e295a1bba8a951163de4f",
    "workspace_bootstrap/superplane_bootstrap/workspace.py": "2f6ca486ad84c7c8a1e7c61130e25fa6281f4435f17307957a9399eeabbf7348",
    "workspace_bootstrap/superplane_bootstrap/authority_runtime.py": "9538408f2d93dfdececd7773ea86920f6d01a81eb7bf9b641b12af04bd7e4fac",
    "workspace_bootstrap/superplane_bootstrap/temporary_authority.py": "e8d0d9778e92c595a821cee0c311ddd5908d35b2b122b7a8b0c53a32379634e6",
    "workspace_bootstrap/superplane_bootstrap/authority_journal.py": "b9e822b50da2787ad85b24c9fba9f30fbd4fde6c457a7ea6c4d3e9b5d6b590a1",
    "workspace_bootstrap/superplane_bootstrap/authority_backend.py": "8e2dc3cc77e6eaa57aeac9df80b0908ac4e0d6cab2353d38c41d87009ab87c94",
    "workspace_bootstrap/superplane_bootstrap/adapters.py": "a815944003c545b49781b9f89a8e2efb5d937ac092a725c0b0c5cd125d34f06c",
    "workspace_bootstrap/superplane_bootstrap/registry.py": "d0accd4bf212c41bcaf4d8a95adbe767c9b49ab17babc29833acc14285a110b5",
}

SOURCE_IMAGE_DIGEST = "sha256:45349b74b297f367863ce368932b31d9a35c90ba728b8316db6a4bac94e9a8b0"

# The first recovered worker adds receipt-bound scratch selection and diagnostic
# chaining. Its public-network mutation barrier is unchanged: a registration
# reservation can be released on refusal, but every cloud/Kubernetes mutation
# requires a durable authority/effect journal which is never deleted here.
QUALIFIED_SOURCES = {
    SOURCE_IMAGE_DIGEST: {"revision": SOURCE_REVISION, "hashes": SOURCE_HASHES},
    "sha256:f0870380df7e74dabcd61e704b85e69cbabe6e2e821114c262d73c7498ebc2d4": {
        "revision": "95fe5b9ed65e591a341baba74bcceacdedaebb29",
        "hashes": {
            "workspace_provisioning/runtime.py": "04ae9dceaecbf10fbfc7508969823ce7db88a31d18844b981b6b991a44fbfd05",
            "workspace_provisioning/bootstrap_runtime.py": "784527077429b0041d50e802bdcee12fe008bfc087a930bd4988dc870c308c96",
            "workspace_provisioning/network.py": "84d124c105b5c3564f3b9c647bc646889a7b69b3ecd3212bc8ffc09387d70433",
            "workspace_provisioning/credentials.py": "14d0c70d973645873299f7ab3fb2ddacb1bdc82efef80ee4d7f26e9a7761cfd6",
            "workspace_provisioning/cluster_clients.py": "f7eed9c543f390a9dd79e466b5962ae478087ff25cbd2b3b7fd94ad271cb3fc6",
            "workspace_provisioning/process.py": "c95be08f81b1faf384adb55f3bf30bcbd935c7046fd56e2d492e048c12e795d2",
            "workspace_bootstrap/superplane_bootstrap/workspace.py": "2f6ca486ad84c7c8a1e7c61130e25fa6281f4435f17307957a9399eeabbf7348",
            "workspace_bootstrap/superplane_bootstrap/authority_runtime.py": "9538408f2d93dfdececd7773ea86920f6d01a81eb7bf9b641b12af04bd7e4fac",
            "workspace_bootstrap/superplane_bootstrap/temporary_authority.py": "e8d0d9778e92c595a821cee0c311ddd5908d35b2b122b7a8b0c53a32379634e6",
            "workspace_bootstrap/superplane_bootstrap/authority_journal.py": "b9e822b50da2787ad85b24c9fba9f30fbd4fde6c457a7ea6c4d3e9b5d6b590a1",
            "workspace_bootstrap/superplane_bootstrap/authority_backend.py": "8e2dc3cc77e6eaa57aeac9df80b0908ac4e0d6cab2353d38c41d87009ab87c94",
            "workspace_bootstrap/superplane_bootstrap/adapters.py": "a815944003c545b49781b9f89a8e2efb5d937ac092a725c0b0c5cd125d34f06c",
            "workspace_bootstrap/superplane_bootstrap/registry.py": "d0accd4bf212c41bcaf4d8a95adbe767c9b49ab17babc29833acc14285a110b5",
            "workspace_provisioning/bootstrap_retry.py": "dbd309db7bb4d0bf6e7c89efdb861236a81a4d6d60a8635806ce2269f9e373d7",
        },
    },
}


# Credential-refresh refusal occurred before this image created a new journal.
# Its retained-journal retry is separately anchored to the prior native revocation.
QUALIFIED_SOURCES["sha256:d78d5632848e47507db4a90b504c49b7108b168e41c92da721fe5ceec025ce64"] = {
    "revision": "119078f1598d3114133367fc0bc2d6593ed96b5d",
    "hashes": {
        "workspace_provisioning/runtime.py": "04ae9dceaecbf10fbfc7508969823ce7db88a31d18844b981b6b991a44fbfd05",
        "workspace_provisioning/bootstrap_runtime.py": "784527077429b0041d50e802bdcee12fe008bfc087a930bd4988dc870c308c96",
        "workspace_provisioning/network.py": "84d124c105b5c3564f3b9c647bc646889a7b69b3ecd3212bc8ffc09387d70433",
        "workspace_provisioning/credentials.py": "14d0c70d973645873299f7ab3fb2ddacb1bdc82efef80ee4d7f26e9a7761cfd6",
        "workspace_provisioning/cluster_clients.py": "f7eed9c543f390a9dd79e466b5962ae478087ff25cbd2b3b7fd94ad271cb3fc6",
        "workspace_provisioning/process.py": "c95be08f81b1faf384adb55f3bf30bcbd935c7046fd56e2d492e048c12e795d2",
        "workspace_bootstrap/superplane_bootstrap/workspace.py": "2f6ca486ad84c7c8a1e7c61130e25fa6281f4435f17307957a9399eeabbf7348",
        "workspace_bootstrap/superplane_bootstrap/authority_runtime.py": "9538408f2d93dfdececd7773ea86920f6d01a81eb7bf9b641b12af04bd7e4fac",
        "workspace_bootstrap/superplane_bootstrap/temporary_authority.py": "e8d0d9778e92c595a821cee0c311ddd5908d35b2b122b7a8b0c53a32379634e6",
        "workspace_bootstrap/superplane_bootstrap/authority_journal.py": "b9e822b50da2787ad85b24c9fba9f30fbd4fde6c457a7ea6c4d3e9b5d6b590a1",
        "workspace_bootstrap/superplane_bootstrap/authority_backend.py": "8e2dc3cc77e6eaa57aeac9df80b0908ac4e0d6cab2353d38c41d87009ab87c94",
        "workspace_bootstrap/superplane_bootstrap/adapters.py": "a815944003c545b49781b9f89a8e2efb5d937ac092a725c0b0c5cd125d34f06c",
        "workspace_bootstrap/superplane_bootstrap/registry.py": "d0accd4bf212c41bcaf4d8a95adbe767c9b49ab17babc29833acc14285a110b5",
        "workspace_provisioning/bootstrap_retry.py": "c71af4ba34efee2df072d0c4069ab730168f0a3961403a8882fc1c2b68cb4c84",
    },
}


# Packaged hashes were independently read from this stopped image with networking
# disabled and compared to its built revision. All fourteen barrier files match
# the preceding qualified image; the changed preflight code is read-only.
QUALIFIED_SOURCES["sha256:3af3f5ff386cf2ccc4b2bca3ff9738c3a50164eb1bf1f9951a9bd08e6e389e24"] = {
    "revision": "5ede34d21f364f34402443258d1930597061762e",
    "hashes": dict(QUALIFIED_SOURCES["sha256:d78d5632848e47507db4a90b504c49b7108b168e41c92da721fe5ceec025ce64"]["hashes"]),
}


# Exact stopped worker, independently read with networking disabled. The pinned
# grant plan and authority loop persist each intent before any Kubernetes write.
QUALIFIED_SOURCES["sha256:973f97e714f49acd37a9ad00aa761845fd12a17a9d26fd01282c7a60797c0008"] = {
    "revision": "79ecbb4b27661f46ba151275337d3b35865c157e",
    "registrar_prefix": True,
    "hashes": {
        "workspace_provisioning/runtime.py": "04ae9dceaecbf10fbfc7508969823ce7db88a31d18844b981b6b991a44fbfd05",
        "workspace_provisioning/bootstrap_runtime.py": "784527077429b0041d50e802bdcee12fe008bfc087a930bd4988dc870c308c96",
        "workspace_provisioning/network.py": "84d124c105b5c3564f3b9c647bc646889a7b69b3ecd3212bc8ffc09387d70433",
        "workspace_provisioning/credentials.py": "14d0c70d973645873299f7ab3fb2ddacb1bdc82efef80ee4d7f26e9a7761cfd6",
        "workspace_provisioning/cluster_clients.py": "f7eed9c543f390a9dd79e466b5962ae478087ff25cbd2b3b7fd94ad271cb3fc6",
        "workspace_provisioning/process.py": "c95be08f81b1faf384adb55f3bf30bcbd935c7046fd56e2d492e048c12e795d2",
        "workspace_bootstrap/superplane_bootstrap/workspace.py": "2f6ca486ad84c7c8a1e7c61130e25fa6281f4435f17307957a9399eeabbf7348",
        "workspace_bootstrap/superplane_bootstrap/authority_runtime.py": "9538408f2d93dfdececd7773ea86920f6d01a81eb7bf9b641b12af04bd7e4fac",
        "workspace_bootstrap/superplane_bootstrap/temporary_authority.py": "e8d0d9778e92c595a821cee0c311ddd5908d35b2b122b7a8b0c53a32379634e6",
        "workspace_bootstrap/superplane_bootstrap/authority_journal.py": "b9e822b50da2787ad85b24c9fba9f30fbd4fde6c457a7ea6c4d3e9b5d6b590a1",
        "workspace_bootstrap/superplane_bootstrap/authority_backend.py": "8e2dc3cc77e6eaa57aeac9df80b0908ac4e0d6cab2353d38c41d87009ab87c94",
        "workspace_bootstrap/superplane_bootstrap/adapters.py": "a815944003c545b49781b9f89a8e2efb5d937ac092a725c0b0c5cd125d34f06c",
        "workspace_bootstrap/superplane_bootstrap/registry.py": "d0accd4bf212c41bcaf4d8a95adbe767c9b49ab17babc29833acc14285a110b5",
        "workspace_provisioning/bootstrap_retry.py": "5d99f743c892ca63c73efca6af436ec3930def811bb057216e8bb8ef6d00ac31",
        "workspace_bootstrap/superplane_bootstrap/grant_plan.py": "74cf8985a01f66622e29bbb48cc8d1f4b397f111775653743d9f974e781e8e4d",
        "workspace_bootstrap/superplane_bootstrap/eks_grants.py": "7002828b7ffba523441a72316e84cf3a538da5ac61ed0c8c8eb3abe8c20462f8",
    },
}


# This stopped successor reached anonymous Kubernetes discovery after its EKS
# registrar grants. Native revocation retained all journal history. Offline reads
# of the exact image match c775; only EKS empty cluster-scope normalization differs
# among the preceding registrar-prefix barrier files. Recovery still requires
# expired authority, positive revocation and absence of every namespace intent.
QUALIFIED_SOURCES["sha256:9b77966b012974cd2610d1dc45ed1faaf2ebb33b2e68a7ba09d7c1c8883b4827"] = {
    "revision": "c775c32b98d64c8d9593e7d296fa20bb9e87b528",
    "registrar_prefix": True,
    "hashes": {
        **QUALIFIED_SOURCES["sha256:973f97e714f49acd37a9ad00aa761845fd12a17a9d26fd01282c7a60797c0008"]["hashes"],
        "workspace_bootstrap/superplane_bootstrap/eks_grants.py": "5eb39df58972494892c1d2fe03e5b9790b1c7fb2671efc70b9d46f57792de0d7",
    },
}


# Attempt 8 durably recorded namespace intent, then rejected Kubernetes' built-in
# namespace finalizer. Exact stopped-image files were checked offline against
# e0bfee9c. No grant after the namespace can run until its identity is verified.
QUALIFIED_SOURCES["sha256:042f6e3dbb6e7ac7336b0bab10e9b6992ae2e7b343add2d2984a71a1e0916adc"] = {
    "revision": "e0bfee9ca6577a8db2ab51124f91d71f50254c50",
    "registrar_prefix": True,
    "namespace_prefix": True,
    "hashes": {
        **QUALIFIED_SOURCES["sha256:9b77966b012974cd2610d1dc45ed1faaf2ebb33b2e68a7ba09d7c1c8883b4827"]["hashes"],
        "workspace_provisioning/cluster_clients.py": "bfa0964c8e07e83347aa2e643965080e3945a136bf8234ed8b0a3cfed76b75bd",
        "workspace_bootstrap/superplane_bootstrap/kube_grants.py": "45618489bc598c53304016c4599310cfb923119b0ed1760342009059b25aed6a",
    },
}


# The exact stopped image was read with networking disabled. Its acquisition
# loop persists every intent and cannot install components before becoming active.
QUALIFIED_SOURCES["sha256:5d2a7e122882b6590fce923bda9134ab1343271d5fe556e82e35cb56f5ad21ca"] = {
    "revision": "9744e518a3016aef685c501846fea0c3c74f20e2",
    "registrar_prefix": True,
    "namespace_prefix": True,
    "acquisition_prefix": True,
    "hashes": {
        "workspace_provisioning/runtime.py": "04ae9dceaecbf10fbfc7508969823ce7db88a31d18844b981b6b991a44fbfd05",
        "workspace_provisioning/bootstrap_runtime.py": "784527077429b0041d50e802bdcee12fe008bfc087a930bd4988dc870c308c96",
        "workspace_provisioning/network.py": "84d124c105b5c3564f3b9c647bc646889a7b69b3ecd3212bc8ffc09387d70433",
        "workspace_provisioning/credentials.py": "14d0c70d973645873299f7ab3fb2ddacb1bdc82efef80ee4d7f26e9a7761cfd6",
        "workspace_provisioning/cluster_clients.py": "f9050aee4d11e94de08a789a69252ba84b78be7aa65f4887dffd2315451614b6",
        "workspace_provisioning/process.py": "c95be08f81b1faf384adb55f3bf30bcbd935c7046fd56e2d492e048c12e795d2",
        "workspace_bootstrap/superplane_bootstrap/workspace.py": "2f6ca486ad84c7c8a1e7c61130e25fa6281f4435f17307957a9399eeabbf7348",
        "workspace_bootstrap/superplane_bootstrap/authority_runtime.py": "9538408f2d93dfdececd7773ea86920f6d01a81eb7bf9b641b12af04bd7e4fac",
        "workspace_bootstrap/superplane_bootstrap/temporary_authority.py": "e8d0d9778e92c595a821cee0c311ddd5908d35b2b122b7a8b0c53a32379634e6",
        "workspace_bootstrap/superplane_bootstrap/authority_journal.py": "b9e822b50da2787ad85b24c9fba9f30fbd4fde6c457a7ea6c4d3e9b5d6b590a1",
        "workspace_bootstrap/superplane_bootstrap/authority_backend.py": "8e2dc3cc77e6eaa57aeac9df80b0908ac4e0d6cab2353d38c41d87009ab87c94",
        "workspace_bootstrap/superplane_bootstrap/adapters.py": "a815944003c545b49781b9f89a8e2efb5d937ac092a725c0b0c5cd125d34f06c",
        "workspace_bootstrap/superplane_bootstrap/registry.py": "d0accd4bf212c41bcaf4d8a95adbe767c9b49ab17babc29833acc14285a110b5",
        "workspace_provisioning/bootstrap_retry.py": "5d99f743c892ca63c73efca6af436ec3930def811bb057216e8bb8ef6d00ac31",
        "workspace_bootstrap/superplane_bootstrap/grant_plan.py": "74cf8985a01f66622e29bbb48cc8d1f4b397f111775653743d9f974e781e8e4d",
        "workspace_bootstrap/superplane_bootstrap/eks_grants.py": "5eb39df58972494892c1d2fe03e5b9790b1c7fb2671efc70b9d46f57792de0d7",
        "workspace_bootstrap/superplane_bootstrap/kube_grants.py": "f9224c3687980b690ca948c3fb0a048128c0c43c3187636050975d7cb540b456",
    },
}


# Offline inspection of this exact stopped image matched all eighteen files at
# the recorded revision. Acquisition and revocation barriers are unchanged;
# only bracketed actor reads changed. Pin the dormant retirement-fence compiler
# as well: managed workspaces acquire those eight cleanup grants before active.
QUALIFIED_SOURCES["sha256:77695ebc2e5d141181908fa44020b88b0bdb6e1ff44f64cc0b23f1ee00fe6890"] = {
    "revision": "8f2f83756571f22b0480288781eb8e363360dcb2",
    "registrar_prefix": True,
    "namespace_prefix": True,
    "acquisition_prefix": True,
    "cleanup_acquisition_prefix": True,
    "hashes": {
        **QUALIFIED_SOURCES["sha256:5d2a7e122882b6590fce923bda9134ab1343271d5fe556e82e35cb56f5ad21ca"]["hashes"],
        "workspace_provisioning/bootstrap_runtime.py": "5ffc0e77516dedcc847955898dbc6221f76086ede84c2dea44754b443ad5e307",
        "workspace_provisioning/credentials.py": "dd518ccd6e714edfa9d573d7afa2a98a703908514c5a41232d902b4366703c0b",
        "workspace_bootstrap/superplane_bootstrap/authority_backend.py": "3267991940313960c37c90a2353460d2afcb1ccd8168ef635c360eaa012a5871",
        "workspace_bootstrap/superplane_bootstrap/retirement_fence.py": "abc31fcfa709ca346e3929792a95586b7b2365e9dc2a7ad512ac68a2c5bb6a68",
    },
}


# The exact image was independently inspected offline against its source.
# Intent journals, acquisition order and revocation barriers are unchanged;
# read-only proof batching and bounded transports preserve the same fences.
# Include diagnostics because it preserves the original failure chain.
QUALIFIED_SOURCES["sha256:dbbf2eb4b51c61e387e6e4d13a2b141625ade8fa265d1d293fa6fcf0b968f5f1"] = {
    "revision": "db0fba0869184ccd549c26149d27ebfec6838aea",
    "registrar_prefix": True,
    "namespace_prefix": True,
    "acquisition_prefix": True,
    "cleanup_acquisition_prefix": True,
    "hashes": {
        "workspace_provisioning/runtime.py": "04ae9dceaecbf10fbfc7508969823ce7db88a31d18844b981b6b991a44fbfd05",
        "workspace_provisioning/bootstrap_runtime.py": "a48df38ff49693dd74cce584a8116f921b6d70b241c38a97e815afb3e132a54c",
        "workspace_provisioning/network.py": "84d124c105b5c3564f3b9c647bc646889a7b69b3ecd3212bc8ffc09387d70433",
        "workspace_provisioning/credentials.py": "751f599d96748e69a4ac2ca3f271ab41a94bf2a8414ff7653789971df8c67fde",
        "workspace_provisioning/cluster_clients.py": "58d85532d2e97a3d6975db207697812fa5abb212f3fb29bc5d7d1a8c37b2f093",
        "workspace_provisioning/process.py": "c95be08f81b1faf384adb55f3bf30bcbd935c7046fd56e2d492e048c12e795d2",
        "workspace_bootstrap/superplane_bootstrap/workspace.py": "2f6ca486ad84c7c8a1e7c61130e25fa6281f4435f17307957a9399eeabbf7348",
        "workspace_bootstrap/superplane_bootstrap/authority_runtime.py": "9538408f2d93dfdececd7773ea86920f6d01a81eb7bf9b641b12af04bd7e4fac",
        "workspace_bootstrap/superplane_bootstrap/temporary_authority.py": "e8d0d9778e92c595a821cee0c311ddd5908d35b2b122b7a8b0c53a32379634e6",
        "workspace_bootstrap/superplane_bootstrap/authority_journal.py": "b9e822b50da2787ad85b24c9fba9f30fbd4fde6c457a7ea6c4d3e9b5d6b590a1",
        "workspace_bootstrap/superplane_bootstrap/authority_backend.py": "2599dff2a26e663ae755d7d9960ce75ea2ef9564011e9bdc724f657a1d19ddd3",
        "workspace_bootstrap/superplane_bootstrap/adapters.py": "cbc7945cbdd9981bb747a0f5d77a1ea80e76ce6b74e04a149796b68a7689cd9a",
        "workspace_bootstrap/superplane_bootstrap/registry.py": "d0accd4bf212c41bcaf4d8a95adbe767c9b49ab17babc29833acc14285a110b5",
        "workspace_provisioning/bootstrap_retry.py": "5d99f743c892ca63c73efca6af436ec3930def811bb057216e8bb8ef6d00ac31",
        "workspace_bootstrap/superplane_bootstrap/grant_plan.py": "74cf8985a01f66622e29bbb48cc8d1f4b397f111775653743d9f974e781e8e4d",
        "workspace_bootstrap/superplane_bootstrap/eks_grants.py": "5eb39df58972494892c1d2fe03e5b9790b1c7fb2671efc70b9d46f57792de0d7",
        "workspace_bootstrap/superplane_bootstrap/kube_grants.py": "f9224c3687980b690ca948c3fb0a048128c0c43c3187636050975d7cb540b456",
        "workspace_bootstrap/superplane_bootstrap/retirement_fence.py": "abc31fcfa709ca346e3929792a95586b7b2365e9dc2a7ad512ac68a2c5bb6a68",
        "workspace_provisioning/diagnostics.py": "cadcc0bd044b2498fb791bbc3e37c64663238634c05a6bb471e6f914f86d3615",
    },
}


def qualified_source(image):
    image_digest = image.rsplit("@", 1)[-1]
    if image_digest == "sha256:12be55191d1a95e3d116e54c3651638b1c4132ddea2c98e745505053a2e58bf9":
        import hashlib
        import json
        from pathlib import Path

        here = Path(__file__).resolve()
        path = here.parents[2] / "contracts/superplane-bootstrap-partial-reader-qualification.json"
        if not path.is_file():
            path = here.parents[4] / "modules/domain-apps/superplane/installation/evidence/bootstrap-reader-worker-c8f44a0b.json"
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != "717f0e7a1e9470022e387821e5638f4bf4830372fb25e169905d0b5be09933fc":
            raise ValueError("partial reader bootstrap source qualification changed")
        evidence = json.loads(raw)
        if evidence["image_digest"] != image_digest or evidence["revision"] != "c8f44a0b20510a1184a69f61d8fb263480e9bcfa":
            raise ValueError("partial reader bootstrap source identity changed")
        return {
            "revision": evidence["revision"],
            "hashes": evidence["hashes"],
            "reader_management_components_recovery": True,
            "partial_reader_components_recovery": True,
        }
    if image_digest == "sha256:c1c26cc85a737b818b7d2b70483de8650483d0aa6edc18be1a80728c6f5e9a6c":
        import hashlib
        import json
        from pathlib import Path

        here = Path(__file__).resolve()
        path = here.parents[2] / "contracts/superplane-bootstrap-reader-qualification.json"
        if not path.is_file():
            path = here.parents[4] / "modules/domain-apps/superplane/installation/evidence/bootstrap-reader-worker-1eb49977.json"
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != "4b410f4f9f5f423050a70814d1de2d7b840926c4ead330b29937c016f954dc81":
            raise ValueError("reader bootstrap source qualification changed")
        evidence = json.loads(raw)
        if evidence["image_digest"] != image_digest or evidence["revision"] != "1eb49977c32e6a9c763257ee8f3dbbdce92292dd":
            raise ValueError("reader bootstrap source identity changed")
        return {"revision": evidence["revision"], "hashes": evidence["hashes"], "reader_management_components_recovery": True}
    if image_digest == "sha256:845ce1dc5c4d3a1379c548e48d8fd269b1977118793738fc02a6883413fd6117":
        import hashlib
        import json
        from pathlib import Path

        here = Path(__file__).resolve()
        path = here.parents[2] / "contracts/superplane-bootstrap-management-qualification.json"
        if not path.is_file():
            path = here.parents[4] / "modules/domain-apps/superplane/installation/evidence/bootstrap-management-worker-0025522e.json"
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != "92b2be8ad3fd8f542dc064a9f84f5fd3340a3b953abd440619f81ed63505ad49":
            raise ValueError("management bootstrap source qualification changed")
        evidence = json.loads(raw)
        if evidence["image_digest"] != image_digest or evidence["revision"] != "0025522e4a21c5373aa24aa3f0473ccbef4d5f66":
            raise ValueError("management bootstrap source identity changed")
        # This source is qualified only for the distinct, fully revoked original
        # management-component boundary; it does not widen acquisition readers.
        return {"revision": evidence["revision"], "hashes": evidence["hashes"], "management_components_revoked": True}
    if image_digest != "sha256:9fd2a13be7c5e147fe00cf5433fcd792ab751d27cd5ce86206c1d9bc326212bb":
        return QUALIFIED_SOURCES.get(image_digest)
    # App-owned offline evidence ships through stage-contracts into the image.
    # Only these exact bytes qualify this one source; caller evidence cannot add
    # images or replace the established acquisition barrier.
    import hashlib
    import json
    from pathlib import Path

    here = Path(__file__).resolve()
    path = here.parents[2] / "contracts/superplane-bootstrap-qualification.json"
    if not path.is_file():
        path = here.parents[4] / "modules/domain-apps/superplane/installation/evidence/bootstrap-worker-61fcfc.json"
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != "963989940765f029749141332eb0c31f55afd558dbbdcfb121d60440d831f81a":
        raise ValueError("bootstrap source qualification manifest changed")
    evidence = json.loads(raw)
    prior = QUALIFIED_SOURCES["sha256:dbbf2eb4b51c61e387e6e4d13a2b141625ade8fa265d1d293fa6fcf0b968f5f1"]
    if evidence["image_digest"] != image_digest or evidence["hashes"] != prior["hashes"]:
        raise ValueError("bootstrap source differs from reviewed barrier")
    return {
        **prior,
        "revision": evidence["revision"],
        "hashes": {**prior["hashes"], **evidence["permission_boundary_hashes"]},
        "precomponent_permission_refusal": True,
    }


def validate_attempt_extension(extension, subject=None, attempts=None, ceiling=None, *, acquisition=False):
    """An explicit bounded authorization for an exhausted retained bootstrap."""
    if (
        not isinstance(extension, dict)
        or set(extension) != {"authorized_by", "authorization", "previous_limit", "recorded_attempts", "additional_attempts"}
        or not isinstance(extension["authorized_by"], str)
        or not extension["authorized_by"].strip()
        or not isinstance(extension["authorization"], str)
        or not 1 <= len(extension["authorization"].strip()) <= 2048
        or any(type(extension[k]) is not int for k in ("previous_limit", "recorded_attempts", "additional_attempts"))
        or not 1 <= extension["additional_attempts"] <= (10 if acquisition else 5)
        or not 1 <= extension["recorded_attempts"] == extension["previous_limit"]
        or (
            (extension["previous_limit"], extension["recorded_attempts"]) != (10, 10)
            if acquisition
            else extension["previous_limit"] + extension["additional_attempts"] > 10
        )
        or subject is not None
        and extension["authorized_by"] != subject
        or attempts is not None
        and extension["recorded_attempts"] != attempts
        or ceiling is not None
        and extension["previous_limit"] != ceiling
    ):
        raise ValueError("bootstrap attempt extension differs from explicit authorization")
    return extension
