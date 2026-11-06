import os


MASTER = "FEATURE_DEVLAKE_CONNECTORS_ENABLED"
COLLECTION = "DEVLAKE_COLLECTION_ENABLED"


def parse(value, name):
    if not isinstance(value, str):
        raise ValueError(f"invalid optional deployment flag {name}")
    if value == "" or value.casefold() == "false":
        return False
    if value.casefold() == "true":
        return True
    raise ValueError(f"invalid optional deployment flag {name}")


def render(master, collection=None):
    if type(master) is not bool or (collection is not None and type(collection) is not bool):
        raise ValueError("optional deployment selections must be explicit booleans")
    if collection and not master:
        raise ValueError("collection requires enabled connectors")
    selected = {MASTER: master}
    if collection is not None:
        selected[COLLECTION] = collection
    for name in (MASTER, COLLECTION):
        if name in os.environ:
            observed = parse(os.environ[name], name)
            if name == COLLECTION and observed and not master:
                raise ValueError("collection requires enabled connectors")
            if name in selected and observed != selected[name]:
                raise ValueError(f"optional deployment flag {name} differs from private selection")
    return {name: "true" if enabled else "false" for name, enabled in selected.items()}
