"""Use the production registration list and FastAPI's mounted route objects."""

import ast
import importlib
from pathlib import Path

from fastapi import FastAPI
from fastapi.routing import APIRoute

from src.admin.audit_operation import CALLBACKS, MUTATIONS, NON_MUTATIONS, AuditedAdminRoute


def mounted_admin_app():
    source = Path(__file__).parents[2] / "src" / "app.py"
    registration = ast.parse(source.read_text())
    modules = next(
        ast.literal_eval(node.value)
        for node in registration.body
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "UNIT_MODULES" for target in node.targets)
    )
    app = FastAPI()
    for module_name in modules:
        if module_name.startswith("src.admin."):
            app.include_router(importlib.import_module(module_name).router)
    return app


def inventory(app):
    records = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        methods = (route.methods or set()) & MUTATIONS
        if route.endpoint.__name__ in CALLBACKS:
            methods = {"GET"}
        if not methods:
            continue
        module = route.endpoint.__module__
        endpoint = route.endpoint.__name__
        if module == "src.admin.onboarding.handler":
            classification = "A11-handoff"
        elif endpoint in NON_MUTATIONS:
            classification = "nonmutating-or-retired"
        elif isinstance(route, AuditedAdminRoute) and route.audit_mutation:
            classification = "durable-operation"
        elif module.startswith(("src.admin.persona_models.", "src.admin.bedrock_routing.")):
            classification = "existing-writer"
        else:
            classification = "UNCLASSIFIED"
        for method in sorted(methods):
            records.append({"method": method, "path": route.path, "module": module, "endpoint": endpoint, "classification": classification})
    return sorted(records, key=lambda row: (row["path"], row["method"]))
