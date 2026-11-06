"""Compare offline boundary traces with raw denial and resource-state observations."""

from l5_services.transform_boundary_probe import SURFACES, probe_boundaries


def _require(condition, reason):
    if not condition:
        raise ValueError(reason)


async def verify_ec02(driver, *, owner, foreign, reference):
    report = await probe_boundaries(driver, owner=owner, foreign=foreign, reference=reference)
    oracle = await driver.read_denial_oracle(owner, foreign, reference)
    _require(isinstance(oracle, dict) and isinstance(oracle.get("traces"), list)
             and len(oracle["traces"]) == 2 * len(SURFACES),
             "independent boundary request log is incomplete")
    references = set(report["evidence_refs"].values())
    for trace, (surface, actor, allowed) in zip(
        oracle["traces"],
        ((surface, actor, allowed) for surface in SURFACES
         for actor, allowed in ((owner, True), (foreign, False))),
    ):
        _require(isinstance(trace, dict) and trace.get("surface") == surface
                 and trace.get("actor") == actor and trace.get("reference") == reference
                 and trace.get("evidence_ref") == report["evidence_refs"][
                     f"{surface}_{'owned' if allowed else 'foreign'}"]
                 and trace.get("effect_count") == 0
                 and trace.get("resource_ids") == ([reference["record_id"]] if allowed else [])
                 and isinstance(trace.get("body"), bytes),
                 "independent request log differs from the scoped probes")
        if allowed:
            _require(trace.get("status") == 200
                     and reference["record_id"].encode() in trace["body"],
                     "owned request did not return the selected resource")
        else:
            _require(trace.get("status") in (403, 404)
                     and trace["body"] in (b"", b'{"detail":{"code":"not_found"}}'),
                     "foreign request disclosed data or returned an ambiguous denial")

    hostile = oracle.get("hostile_source")
    _require(isinstance(hostile, dict)
             and hostile.get("path") == f"../{foreign['project_id']}/.credentials"
             and hostile.get("source") == "source-api"
             and hostile.get("status") in (403, 404)
             and hostile.get("body") in (b"", b'{"detail":{"code":"not_found"}}')
             and hostile.get("effect_count") == 0
             and isinstance(hostile.get("evidence_ref"), str)
             and bool(hostile["evidence_ref"]) and hostile["evidence_ref"] not in references,
             "hostile source path was not independently denied")
    references.add(hostile["evidence_ref"])
    inventory = oracle.get("inventory")
    expected = {"owned": [reference["record_id"]], "foreign": []}
    _require(isinstance(inventory, dict) and inventory.get("before") == expected
             and inventory.get("after") == expected and inventory.get("effect_count") == 0
             and isinstance(inventory.get("evidence_ref"), str)
             and bool(inventory["evidence_ref"]) and inventory["evidence_ref"] not in references,
             "independent resource inventory changed or is missing")
    return {"phase": "offline-boundary-denials", "live_qualified": False,
            "boundary_refs": report["evidence_refs"],
            "hostile_source_ref": hostile["evidence_ref"],
            "inventory_ref": inventory["evidence_ref"]}
