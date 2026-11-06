"""Fail-closed service admission until an L5 executor bridge is qualified."""

from fastapi import HTTPException


def reject_unqualified_transform(service_id):
    if service_id == "transform":
        raise HTTPException(503, detail={"code": "capability_unavailable"})
