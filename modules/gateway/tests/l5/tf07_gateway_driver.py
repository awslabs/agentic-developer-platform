"""Offline TF07 driver exercising the installed L5 HTTP, CLI, MCP and event contracts."""

import asyncio

import httpx
from fastapi import FastAPI
from l5_services.api.router import create_router
from l5_services.events.schedules import receive_schedule_tick
from l5_services.models.run import Run, view_run
from sqlalchemy import select

from src.internal import tools_l5


class GatewayClientDriver:
    def __init__(self, *, backend, cli, sessions, scope, scheduled_at, scm_reader):
        self.backend = backend
        self.cli = cli
        self.sessions = sessions
        self.scope = scope
        self.scheduled_at = scheduled_at
        self.scm_reader = scm_reader
        self.app = FastAPI()
        self.app.include_router(create_router(lambda: backend))

    async def _request(self, path):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://l5.internal", trust_env=False) as client:
            response = await client.get(path)
        return response

    async def read_client(self, surface, run_id, result_id):
        arguments = {"product_id": self.scope["product_id"], "project_id": self.scope["project_id"], "service_id": "transform"}
        run_args = {**arguments, "assessment_ref": run_id}
        result_args = {**arguments, "result_id": result_id}
        run_path = tools_l5.l5_http_request("l5___get_run", run_args)[1]
        result_path = tools_l5.l5_http_request("l5___get_result", result_args)[1]
        if surface == "mcp":
            run_response = await tools_l5.dispatch_l5("l5___get_run", run_args, self.backend)
            result_response = await tools_l5.dispatch_l5("l5___get_result", result_args, self.backend)
            run = run_response["structuredContent"]
            result = result_response["structuredContent"]
            status = max(run["http_status"], result["http_status"])
            run, result = run.get("data"), result.get("data")
        elif surface == "ui":
            run_response = await self._request(run_path)
            result_response = await self._request(result_path)
            status = max(run_response.status_code, result_response.status_code)
            run, result = run_response.json(), result_response.json()
        elif surface == "cli":

            def invoke():
                requests = []
                driver = self

                class Transport:
                    def request(self, method, path, body=None, **options):
                        requests.append((method, path))
                        if method != "GET" or body is not None:
                            raise ValueError("CLI evidence probe attempted a mutation")
                        response = asyncio.run(driver._request(path))
                        if response.status_code != 200:
                            raise ValueError("CLI evidence probe could not read the gateway")
                        return response.json()

                transport = Transport()
                scope = ["--product", arguments["product_id"], "--project", arguments["project_id"], "--service", "transform", "--json"]
                run = self.cli.execute(self.cli.parser().parse_args(["status", "--run-id", run_id, *scope]), transport)["detail"]
                result = self.cli.execute(self.cli.parser().parse_args(["result", "show", "--result-id", result_id, *scope]), transport)["detail"]
                return run, result, requests

            run, result, requests = await asyncio.to_thread(invoke)
            if requests != [("GET", run_path), ("GET", result_path)]:
                raise ValueError("CLI did not use the scoped gateway contract")
            status = 200
        else:
            raise ValueError("unsupported client surface")
        return {
            "surface": surface,
            "evidence_ref": f"offline/gateway/{surface}",
            "requests": [("GET", run_path), ("GET", result_path)],
            "status": status,
            "run": run,
            "result": result,
        }

    async def read_schedule_tick(self, trigger_id, *, replay):
        async with self.sessions() as session:
            receipt = await receive_schedule_tick(
                session,
                tenant_id=self.scope["tenant_id"],
                product_id=self.scope["product_id"],
                project_id=self.scope["project_id"],
                trigger_id=trigger_id,
                scheduled_at=self.scheduled_at,
                now=self.scheduled_at,
                service_id="transform",
            )
            await session.commit()
            return {
                key: getattr(receipt, key)
                for key in (
                    "tenant_id",
                    "product_id",
                    "project_id",
                    "service_id",
                    "trigger_id",
                    "event_id",
                    "event_kind",
                    "idempotency_key",
                    "run_id",
                    "state",
                    "reason",
                )
            } | {"evidence_ref": f"offline/sql-event/{receipt.event_id}"}

    async def read_event_runs(self, receipt):
        async with self.sessions() as session:
            runs = (
                await session.scalars(
                    select(Run).where(
                        Run.tenant_id == receipt["tenant_id"],
                        Run.product_id == receipt["product_id"],
                        Run.project_id == receipt["project_id"],
                        Run.service_id == "transform",
                        Run.trigger_id == receipt["trigger_id"],
                    )
                )
            ).all()
            return [view_run(run) for run in runs if (run.admission or {}).get("event_id") == receipt["event_id"]]

    async def read_scm(self):
        return await self.scm_reader()

    def verify_no_pr_handoff(self):
        class NoTransport:
            def request(self, *args, **kwargs):
                raise AssertionError("Transform inspection attempted an SCM operation")

        scope = ["--product", self.scope["product_id"], "--project", self.scope["project_id"], "--service", "transform", "--json"]
        for verb in ("show", "retry"):
            args = ["publication", verb, "--publication-id", "result-example", *scope]
            if verb == "retry":
                args += ["--input", "not-read.json", "--idempotency-key", "no-pr-handoff-12345678"]
            try:
                self.cli.execute(self.cli.parser().parse_args(args), NoTransport())
            except self.cli.common.CliError as error:
                if error.code != "capability_unavailable":
                    raise
            else:
                raise ValueError("Transform PR action lacked a separate authorization")
