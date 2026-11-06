"""Run only from the installed dedicated reader Deployment."""

import json
import os
import time
from pathlib import Path

from .renewal import ReaderRefused, Renewal, require
from .transports import Issuer, Projector, Registry


def main():
    path = Path(os.environ["SUPERPLANE_DEDICATED_READER_CONFIG"])
    require(path.is_absolute(), "Reader requires an explicit installed configuration")
    config = json.loads(path.read_text())
    registry, issuer, projector = Registry(config), Issuer(config), Projector(config)
    renewal = Renewal(config, registry, issuer, projector)
    while True:
        healthy = True
        for selected in config["workspaces"]:
            try:
                status = renewal.reconcile(selected)
            except Exception as exc:  # noqa: BLE001 -- isolate provider errors without logging response bodies
                # Provider exceptions can contain response bodies/token material.
                # Emit only a type and the installed public workspace identity.
                status = "refused:" + type(exc).__name__
                if isinstance(exc, ReaderRefused):
                    status += ":" + str(exc)
                healthy = False
            print(
                json.dumps(
                    {"workspace_id": selected["workspace_id"], "status": status}
                ),
                flush=True,
            )
        ready = Path("/tmp/reader-ready")
        if healthy:
            ready.write_text(str(time.time()))
        else:
            ready.unlink(missing_ok=True)
        time.sleep(30)


if __name__ == "__main__":
    main()
