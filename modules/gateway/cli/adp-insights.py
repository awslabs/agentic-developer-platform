#!/usr/bin/env python3
"""Read authorized, dated Insights business context through the gateway."""

import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adp_common as common

RESULT_FIELDS = {
    "schema_version",
    "view",
    "project_id",
    "product_id",
    "as_of",
    "dataset_version",
    "mapping_versions",
    "coverage",
    "grouping",
    "work_count",
    "cost_by_currency",
    "allocated_by_currency",
    "unallocated_by_currency",
    "contexts",
    "associations",
    "allocation_versions",
    "next_cursor",
}


def parser():
    root = common.Parser(prog="adp insights")
    categories = root.add_subparsers(dest="category", required=True, parser_class=common.Parser)
    context = categories.add_parser("context")
    commands = context.add_subparsers(dest="verb", required=True, parser_class=common.Parser)
    show = commands.add_parser("show")
    show.add_argument("--project", required=True)
    show.add_argument("--product", required=True)
    show.add_argument("--as-of", required=True)
    show.add_argument("--portfolio", action="append", default=[])
    show.add_argument("--cursor")
    show.add_argument("--json", action="store_true")
    return root


def usage(message):
    raise common.CliError(message, "usage_error", 1)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", value):
        usage("Project, Product and Portfolio must be exact identifiers.")
    return value


def as_of(value):
    if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)", value):
        usage("--as-of must be a timezone-aware RFC 3339 instant.")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        usage("--as-of must be a timezone-aware RFC 3339 instant.")
    if instant.tzinfo is None:
        usage("--as-of must be a timezone-aware RFC 3339 instant.")
    return value


def execute(args, client):
    project = identifier(args.project)
    product = identifier(args.product)
    portfolios = [identifier(value) for value in args.portfolio]
    if len(portfolios) > 20 or len(set(portfolios)) != len(portfolios):
        usage("Use at most 20 distinct Portfolio IDs.")
    when = as_of(args.as_of)
    params = [("view", "business_context"), ("product_id", product), ("as_of", when)]
    params.extend(("portfolio_id", value) for value in portfolios)
    if args.cursor is not None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,512}", args.cursor):
            usage("--cursor must be a server-issued results cursor.")
        params.append(("cursor", args.cursor))
    path = "/engineering-insights/v1/projects/" + quote(project, safe="") + "/results?" + urlencode(params)
    try:
        result = client.request("GET", path)
    except common.CliError as error:
        if error.code == "unsupported_capability" or (error.status_code == 404 and error.code == "http_error"):
            raise common.CliError("This ADP deployment does not offer Insights results.", "unsupported_operation", 5) from None
        raise
    if (
        not isinstance(result, dict)
        or set(result) != RESULT_FIELDS
        or result["schema_version"] != "results/1.0"
        or result["view"] != "business_context"
        or result["coverage"] != "complete"
        or result["project_id"] != project
        or result["product_id"] != product
        or result["as_of"] != when
        or not isinstance(result["contexts"], list)
        or not isinstance(result["associations"], list)
        or not isinstance(result["mapping_versions"], list)
        or not result["mapping_versions"]
        or not isinstance(result["cost_by_currency"], dict)
    ):
        raise common.CliError("The server did not return a complete business-context result.", "invalid_response", 5)
    return common.envelope("ok", "insights context show", result)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        args = parser().parse_args(argv)
        return common.emit(execute(args, common.Api()), args.json)
    except common.CliError as error:
        if error.status_code == 409 and error.code in ("projection_pending", "projection_incomplete"):
            status = "pending" if error.code == "projection_pending" else "unavailable"
            response = common.envelope(status, "insights context show")
            response["error"] = {"code": error.code, "message": "No complete Insights projection is available."}
            return common.emit(response, "--json" in argv)
        return common.report_error(error, "insights context show", "--json" in argv)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return common.report_error(exc, "insights context show", "--json" in argv)


if __name__ == "__main__":
    sys.exit(main())
