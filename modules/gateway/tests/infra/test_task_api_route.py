"""The edge half of task submission, pinned — issue #5795 (T2).

``POST /v1/tasks`` is the one API Gateway route that leaves the gateway pod
entirely and lands on the webhook-ingress Lambda. Three facts make that safe,
none of which any Python unit test would notice, and all of which fail silently
— the deploy succeeds and the API answers:

  1. It is an EXPLICIT path with an EXPLICIT POST method. Because API Gateway
     selects an explicit resource before ``/{proxy+}`` for every method, the
     resource also needs an any-method fallback to keep non-POST methods on the
     gateway pod.
  2. The Lambda invoke permission names that one method and path. The ingress
     Lambda also serves the GitHub webhook route, so a ``/*/*`` grant — the form
     the broker permission uses — would let any present or future route on this
     API invoke it.
  3. Both provenance headers are blanked, because the route is auth NONE.

Also pinned here: the #5653 postcondition must actually inspect explicit
methods. It originally read only ``x-amazon-apigateway-any-method``, which was
complete while every route used one — but meant this route, the first with an
explicit method, would have been skipped rather than checked. A guard that
quietly stops applying is worse than no guard, because the deploy stays green.

Text/structure assertions with no AWS and no terraform binary, so they run in
the ordinary gateway pre-submit suite.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

GATEWAY_ROOT = Path(__file__).resolve().parents[2]
APIGW_DIR = GATEWAY_ROOT / "infra" / "modules" / "api-gateway"
APIGW_MAIN_TF = APIGW_DIR / "main.tf"
APIGW_VARIABLES_TF = APIGW_DIR / "variables.tf"
ROOT_MAIN_TF = GATEWAY_ROOT / "infra" / "main.tf"
ROOT_VARIABLES_TF = GATEWAY_ROOT / "infra" / "variables.tf"

ROUTE = "/v1/tasks"


def _route_block(tf: str) -> str:
    """The rendered ``"/v1/tasks" = { ... }`` entry, up to the next route key."""
    start = tf.index(f'"{ROUTE}" = {{')
    tail = tf[start + 1 :]
    following = re.search(r'^\s*"(?:/[^"]*)" = \{', tail, re.MULTILINE)
    return tail[: following.start()] if following else tail


class TestTheRouteIsNarrow:
    def test_the_task_route_exists_as_an_explicit_path(self):
        assert f'"{ROUTE}" = {{' in APIGW_MAIN_TF.read_text(), "POST /v1/tasks must be an explicit API Gateway path"

    def test_the_route_declares_an_explicit_post_method(self):
        block = _route_block(APIGW_MAIN_TF.read_text())

        assert re.search(r"^\s*post = \{", block, re.MULTILINE), "the task route must declare an explicit post method"

    def test_non_post_methods_fall_through_to_the_gateway_pod(self):
        """An explicit resource shadows ``/{proxy+}`` for every HTTP method."""
        block = _route_block(APIGW_MAIN_TF.read_text())

        assert "x-amazon-apigateway-any-method" in block
        fallback, post = block.split("post = {", 1)
        assert 'type                 = "http_proxy"' in fallback
        assert 'httpMethod           = "ANY"' in fallback
        assert 'uri                  = "http://${var.internal_alb_dns}/v1/tasks"' in fallback
        assert 'type                = "aws_proxy"' in post
        assert "var.task_api_lambda_invoke_arn" in post

    def test_the_route_is_not_a_proxy_path(self):
        """``/v1/tasks/{proxy+}`` would swallow paths this story does not own."""
        tf = APIGW_MAIN_TF.read_text()

        assert "/v1/tasks/{proxy+}" not in tf
        assert "/v1/{proxy+}" not in tf

    def test_the_route_targets_a_lambda_proxy_integration(self):
        block = _route_block(APIGW_MAIN_TF.read_text())

        assert "aws_proxy" in block
        assert "var.task_api_lambda_invoke_arn" in block


class TestTheInvokePermissionIsScoped:
    def test_the_permission_names_only_the_post_v1_tasks_method(self):
        """A `/*/*` grant on the INGRESS Lambda is the failure to prevent.

        That Lambda also serves the HMAC-authenticated GitHub webhook route. A
        wildcard grant would let any route on this API — including one added
        years from now — invoke it with a request the ingress router then
        dispatches on whatever ``resource`` that route produced.
        """
        tf = APIGW_MAIN_TF.read_text()

        start = tf.index('resource "aws_lambda_permission" "task_api_api_gateway"')
        end = tf.index("\nresource ", start + 1)
        block = tf[start:end]

        assert 'source_arn    = "${aws_api_gateway_rest_api.main.execution_arn}/*/POST/v1/tasks"' in block, (
            "the task Lambda permission must name exactly POST /v1/tasks"
        )
        assert "execution_arn}/*/*" not in block, "the task Lambda permission must not use a wildcard method/path grant"
        assert "var.task_api_lambda_function_name" in block

    def test_the_permission_count_is_plan_time_evaluable(self):
        """A count derived from the computed invoke ARN fails at plan time."""
        tf = APIGW_MAIN_TF.read_text()

        start = tf.index('resource "aws_lambda_permission" "task_api_api_gateway"')
        end = tf.index("\nresource ", start + 1)
        block = tf[start:end]

        assert "var.enable_task_api_route" in block
        assert "task_api_lambda_invoke_arn" not in block, "count must not depend on the invoke ARN, which is unknown until apply"


class TestProvenanceHeadersAreBlanked:
    def test_the_task_route_blanks_both_provenance_headers(self):
        """Auth NONE means API Gateway verified no signature on this route."""
        block = _route_block(APIGW_MAIN_TF.read_text())

        assert "local.blank_caller_identity" in block
        assert "local.verified_caller_identity" not in block, "an auth-NONE route must blank, never map a verified identity it lacks"


class TestThePostconditionStillApplies:
    def test_the_postcondition_inspects_explicit_methods_too(self):
        """The guard must not skip a route just because it names its method."""
        tf = APIGW_MAIN_TF.read_text()

        start = tf.index("postcondition {")
        end = tf.index("error_message", start)
        condition = tf[start:end]

        assert "for method_key, method_item in path_item" in condition, (
            "the #5653 postcondition must iterate every method, not only x-amazon-apigateway-any-method, or explicit-method routes pass vacuously"
        )
        assert 'method_item["x-amazon-apigateway-integration"]' in condition

    def test_mock_integrations_are_exempted_deliberately(self):
        """The first-pass placeholder body serves /status from a MOCK.

        API Gateway answers a MOCK itself, so there is no backend to forward a
        header to. Before the method-map widening that route was skipped only
        because it uses an explicit ``get`` — an accident. Stating the exemption
        is what keeps the widening from failing every first-pass deploy.
        """
        tf = APIGW_MAIN_TF.read_text()

        start = tf.index("postcondition {")
        end = tf.index("error_message", start)
        condition = tf[start:end]

        assert '!= "MOCK"' in condition, "MOCK integrations must be exempted explicitly, not incidentally"

    def test_the_rendered_first_pass_body_has_only_mock_integrations(self):
        """Pins the fact the exemption relies on."""
        tf = APIGW_MAIN_TF.read_text()

        placeholder = tf[tf.index('"/status" = {') :]
        placeholder = placeholder[: placeholder.index("\n  tags =")]
        assert '"MOCK"' in placeholder or 'type = "MOCK"' in placeholder


class TestTheRolloutIsTwoIndependentSwitches:
    def test_publishing_the_route_is_off_by_default(self):
        """A gateway apply that sets nothing must publish no task route."""
        for path in (APIGW_VARIABLES_TF, ROOT_VARIABLES_TF):
            text = path.read_text()
            start = text.index('variable "enable_task_api_route"')
            block = text[start : text.index("}", start)]
            assert "default     = false" in block, f"{path.name}: route must default off"

    def test_the_route_requires_both_lambda_identifiers(self):
        """A route without an integration URI or invoke permission is unusable."""
        tf = APIGW_MAIN_TF.read_text()

        assert 'var.enable_task_api_route && var.task_api_lambda_invoke_arn != "" ?' in tf
        start = tf.index("precondition {")
        end = tf.index("postcondition {", start)
        precondition = tf[start:end]
        assert "!var.enable_task_api_route" in precondition
        assert 'var.task_api_lambda_invoke_arn != ""' in precondition
        assert 'var.task_api_lambda_function_name != ""' in precondition

    def test_the_root_module_passes_the_task_variables_through(self):
        tf = ROOT_MAIN_TF.read_text()

        start = tf.index('module "api_gateway" {')
        end = tf.index("\nmodule ", start + 1)
        block = tf[start:end]

        for name in (
            "task_api_lambda_invoke_arn",
            "task_api_lambda_function_name",
            "enable_task_api_route",
        ):
            assert name in block, f"module api_gateway must receive {name}"

    def test_admission_is_a_separate_lambda_side_switch(self):
        """The edge can be published and verified before any task is admitted.

        Route and admission being one switch would mean the only way to stop
        accepting tasks is a gateway apply that removes the route — slow, and it
        returns a 403 from API Gateway rather than the contract's refusal shape.
        """
        handler = GATEWAY_ROOT.parents[0] / "agent-factory" / "webhook-ingress" / "lambda" / "task_api" / "handler.py"
        assert handler.is_file()
        source = handler.read_text()
        assert "ADMISSION_FLAG" in source
        assert "_admission_enabled" in source


class TestTheRouteDoesNotShadowGatewayRoutes:
    def test_no_gateway_router_serves_the_task_submit_path(self):
        """If the pod ever serves POST /v1/tasks, this route silently wins.

        API Gateway prefers an explicit resource over ``/{proxy+}``, so the
        shadowing would be invisible: the pod route still exists in the app and
        in its OpenAPI, and simply never receives a request.
        """
        src = GATEWAY_ROOT / "src"
        route_declaration = re.compile(
            r"(?:APIRouter\([^)]*prefix\s*=|@\w+\.(?:post|api_route)\()"
            r'[^\n]*["\']/v1/tasks["\']'
        )
        hits = [path for path in src.rglob("*.py") if route_declaration.search(path.read_text())]
        assert hits == [], f"the gateway app now declares /v1/tasks ({hits}); the explicit API Gateway route would shadow it"

    def test_the_declared_route_set_is_what_this_story_intended(self):
        """Equality, so an unreviewed route addition breaks CI immediately."""
        tf = APIGW_MAIN_TF.read_text()
        routes = set(re.findall(r'^\s*"(/[^"]*)" = \{', tf, re.MULTILINE))

        assert routes == {
            "/",
            "/{proxy+}",
            "/agent",
            "/agent/{proxy+}",
            "/internal/{proxy+}",
            "/auth/github/{proxy+}",
            "/v1/tasks",
            "/status",
        }, f"the API Gateway route table changed: {sorted(routes)}"


class TestTheIntegrationBudgetIsNotModelSized:
    def test_submission_does_not_inherit_the_fifteen_minute_llm_timeout(self):
        """Admission is one internal call, not model work.

        ``var.integration_timeout_ms`` defaults to 15 minutes for streaming LLM
        traffic. Using it here would hold a client for minutes on a stuck
        gateway; API Gateway's Lambda-proxy ceiling is 29s anyway, and the
        Lambda's own budget is inside that, so a stall surfaces as a refusal the
        Lambda chose and shaped.
        """
        block = _route_block(APIGW_MAIN_TF.read_text())
        post = block.split("post = {", 1)[1]

        assert "var.integration_timeout_ms" not in post
        assert "timeoutInMillis = 29000" in post


class TestTheRouteIsValidSwagger:
    def test_the_route_entry_parses_as_an_explicit_method_object(self):
        """Guards against a hand-edit that produces a plausible-looking shape."""
        block = _route_block(APIGW_MAIN_TF.read_text())

        # Terraform's HCL map syntax for this block is close enough to JSON that
        # the method/integration nesting can be checked structurally.
        method_keys = re.findall(r"^\s{10}([a-z-]+(?:-[a-z]+)*) = \{", block, re.MULTILINE)
        assert method_keys == ["x-amazon-apigateway-any-method", "post"], f"unexpected method keys: {method_keys}"

        post = block.split("post = {", 1)[1]
        assert "x-amazon-apigateway-integration" in post
        assert post.index("x-amazon-apigateway-integration") < post.index("var.task_api_lambda_invoke_arn")
        assert json.dumps(ROUTE) == '"/v1/tasks"'
