"""Per-binding TLS trust for private domain recovery; never global CA overrides."""

import json
import ssl

from fastapi import HTTPException


def transport(value: str):
    try:
        value = value.strip()
        if value.startswith("{"):
            bundle = json.loads(value)
            if set(bundle) != {"authorization", "ca_pem"}:
                raise ValueError()
            credential, ca = bundle["authorization"], bundle["ca_pem"]
            if not isinstance(ca, str) or not ca.strip():
                raise ValueError()
            # This binding supplies its own trust anchor. Keep normal certificate
            # and hostname verification without Python 3.13's newly enabled
            # strict extension requirements: existing installer certificates
            # predate Authority/Subject Key Identifier extensions. Loading only
            # this CA also keeps public system roots outside this private trust.
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_verify_locations(cadata=ca)
        else:
            credential, context = value, ssl.create_default_context()
        if not isinstance(credential, str) or not credential or any(ord(c) < 32 or ord(c) > 126 for c in credential):
            raise ValueError()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return credential, context
    except (ValueError, TypeError, KeyError, ssl.SSLError):
        raise HTTPException(503, "domain recovery transport unavailable") from None
