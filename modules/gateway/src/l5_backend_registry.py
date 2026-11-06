"""Load only installed service-owned Gateway backend exports."""

from importlib import import_module

SERVICE_CALLBACKS = {
    "devops": (
        "l5_devops_integration_observer",
        "l5_devops_verification_reader",
        "l5_devops_release_authority",
        "l5_devops_prevention_provider",
    ),
    "finops": ("l5_finops_authority",),
    "transform": (
        "l5_transform_work_authority",
        "l5_transform_executor_qualification_reader",
        "l5_transform_artifact_store",
    ),
    "well_architected": (
        "l5_wa_authority",
        "l5_wa_profile_client",
        "l5_wa_consumer_resolver",
        "l5_wa_qualify",
    ),
}


def install_l5_backends(app):
    for service_id, names in SERVICE_CALLBACKS.items():
        module_name = f"src.l5_backend_{service_id}"
        try:
            module = import_module(module_name)
        except ModuleNotFoundError as error:
            if error.name != module_name:
                raise
            continue
        factory = getattr(module, "gateway_backends", None)
        if not callable(factory):
            raise RuntimeError(f"{service_id} Gateway backend factory unavailable")
        callbacks = factory()
        if not isinstance(callbacks, dict) or set(callbacks) != set(names) or any(value is None for value in callbacks.values()):
            raise RuntimeError(f"{service_id} Gateway backend exports incomplete")
        for name, callback in callbacks.items():
            setattr(app.state, name, callback)
