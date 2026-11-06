"""Context owns its PostgreSQL records; Gateway retains current Product authority."""

import json

from adp_platform_common.l5_contributions.http import receiver_router
from adp_platform_common.l5_contributions.stores import PostgresStore
from fastapi import HTTPException

from .config import config


def router(state):
    def store():
        if state.db_pool is None:
            raise HTTPException(503, detail={"code": "consumer_storage_unavailable"})
        return PostgresStore(state.db_pool)

    return receiver_router(consumer="context", store_factory=store,
                           keys_factory=lambda: json.loads(config.door_verification_keys))
