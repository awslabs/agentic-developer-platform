"""Normalize raw CE pages independently of a native answer."""

import asyncio
import json
import re
from datetime import UTC, datetime
from hashlib import sha256

from l5_services.finops_reconciliation import _sum, covers_period, decimal_amount


class CostExplorerAdapter:
    def __init__(self, bucket, s3, provider_session):
        self.bucket = bucket
        self.s3 = s3
        self.provider_session = provider_session

    def _scope(self, run, request, authority):
        if request != {"query": run.input["query"], "billing_scope": run.admission["billing_scope"]}:
            raise ValueError("CE request differs from pinned billing selection")
        scope = run.admission["scope"]
        billing = run.admission["billing_scope"]
        identity = run.admission["provider_identity"]
        view = billing.get("view_ref", "")
        match = re.fullmatch(r"arn:aws[a-z-]*:billing::([0-9]{12}):billingview/[A-Za-z0-9/_-]+", view)
        if (run.service_id != "finops" or any(scope.get(field) != getattr(run, field)
                 for field in ("tenant_id", "product_id", "project_id"))
                or not match or match.group(1) != identity.get("account_id")
                or billing.get("scope_generation") != run.binding_generation
                or not billing.get("scope_digest")
                or not isinstance(billing.get("account_ids"), list) or not billing["account_ids"]
                or any(not re.fullmatch(r"[0-9]{12}", account) for account in billing["account_ids"])
                or not isinstance(authority, dict)
                or authority.get("provider_identity") != identity
                or any(not authority.get(field) for field in (
                    "access_key_id", "secret_access_key", "session_token"))):
            raise ValueError("CE billing authority differs from pinned Product scope")
        try:
            deadline = datetime.fromisoformat(authority["expires_at"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("CE session expiry unavailable") from error
        if deadline.tzinfo is None or deadline <= datetime.now(UTC):
            raise ValueError("CE session expired")
        return billing, identity

    def _key(self, run, collection_id, digest):
        if not re.fullmatch(r"[0-9a-f-]{36}", run.run_id) or not re.fullmatch(r"[0-9a-f-]{36}", collection_id):
            raise ValueError("CE collection identity is invalid")
        return f"l5/finops/work/{run.run_id}/billing/{collection_id}/{digest}.json"

    async def collect_billing_page(self, run, claim, authority, request, cursor, collection_id):
        from l5_services.billing_collection import query_identity

        billing, identity = self._scope(run, request, authority)
        query = run.input["query"]
        parameters = {"TimePeriod": {"Start": query["start"], "End": query["end"]},
                      "Granularity": query["granularity"], "Metrics": [query["metric"]],
                      "Filter": {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": billing["account_ids"]}},
                      "BillingViewArn": billing["view_ref"]}
        if query["group_by"]:
            parameters["GroupBy"] = [{"Type": "DIMENSION", "Key": key} for key in query["group_by"]]
        if cursor is not None:
            parameters["NextPageToken"] = cursor
        client = self.provider_session(authority, identity["region"]).client("ce")
        response = await asyncio.to_thread(client.get_cost_and_usage, **parameters)
        response.pop("ResponseMetadata", None)
        contents = json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
        digest = sha256(contents).hexdigest()
        _, query_hash = query_identity(run)
        page = {"collection_id": collection_id, "scope_digest": billing["scope_digest"],
                "query_hash": query_hash, "request_token": cursor, "response": response}
        saved = json.dumps(page, sort_keys=True, separators=(",", ":")).encode()
        key = self._key(run, collection_id, digest)
        result = await asyncio.to_thread(self.s3.put_object, Bucket=self.bucket, Key=key, Body=saved)
        version = result.get("VersionId")
        if not version:
            raise ValueError("CE billing page requires versioned storage")
        return {"collection_id": collection_id, "request_token": cursor, "response": response,
                "artifact": {"artifact_id": key, "version": version, "sha256": digest}}

    async def read_billing_page(self, run, claim, authority, collection_id, reference):
        from l5_services.billing_collection import query_identity

        billing, _ = self._scope(run, {"query": run.input["query"],
                                        "billing_scope": run.admission["billing_scope"]}, authority)
        if (not isinstance(reference, dict) or set(reference) != {"artifact_id", "version", "sha256"}
                or not isinstance(reference.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", reference["sha256"])
                or reference.get("artifact_id") != self._key(run, collection_id, reference["sha256"])):
            raise ValueError("CE billing page belongs to another run or collection")
        result = await asyncio.to_thread(self.s3.get_object, Bucket=self.bucket,
                                         Key=reference["artifact_id"], VersionId=reference["version"])
        if result.get("VersionId") != reference["version"] or result.get("ContentLength", 1 << 30) > 4 * 1024 * 1024:
            raise ValueError("CE billing page storage version or size changed")
        contents = await asyncio.to_thread(result["Body"].read)
        if len(contents) > 4 * 1024 * 1024:
            raise ValueError("CE billing page storage size changed")
        page = json.loads(contents)
        _, query_hash = query_identity(run)
        if (page.get("collection_id") != collection_id or page.get("scope_digest") != billing["scope_digest"]
                or page.get("query_hash") != query_hash
                or sha256(json.dumps(page.get("response"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                != reference["sha256"]):
            raise ValueError("CE billing page differs from pinned query or content")
        return {"collection_id": collection_id, "request_token": page["request_token"],
                "response": page["response"], "artifact": reference}


def normalize_pages(query, scope_digest, query_hash, collection_id, pages, observed_at):
    """Only a closed, digest-verified page sequence can claim complete coverage."""
    if not isinstance(pages, list) or not collection_id or not scope_digest or not query_hash:
        raise ValueError("billing collection identity is required")
    series = []
    references = []
    estimated = set()
    seen = set()
    next_token = None
    tokens = set()
    complete = True
    for index, page in enumerate(pages):
        if page.get("request_token") != next_token:
            raise ValueError("billing page cursor differs from previous page")
        response = page["response"]
        reference = page["artifact"]
        if (set(reference) != {"artifact_id", "version", "sha256"}
                or not all(isinstance(value, str) and value for value in reference.values())):
            raise ValueError("billing page lacks a versioned evidence artifact")
        contents = json.dumps(response, sort_keys=True, separators=(",", ":")).encode()
        if sha256(contents).hexdigest() != reference["sha256"]:
            raise ValueError("billing page digest does not match raw response")
        references.append(reference)
        dimensions = response.get("GroupDefinitions", [])
        if dimensions != [{"Type": "DIMENSION", "Key": dimension} for dimension in query["group_by"]]:
            raise ValueError("billing group dimensions differ from pinned query")
        for interval in response.get("ResultsByTime", []):
            period = interval["TimePeriod"]
            if (period["Start"] < query["start"] or period["End"] > query["end"]
                    or period["Start"] >= period["End"]):
                raise ValueError("billing page period differs from pinned query")
            estimated.add(interval.get("Estimated"))
            groups = interval.get("Groups", []) if query["group_by"] else [
                {"Keys": [], "Metrics": interval["Total"]}]
            for group in groups:
                keys = group["Keys"]
                if len(keys) != len(query["group_by"]):
                    raise ValueError("billing page has unexpected group keys")
                identity = (period["Start"], period["End"], tuple(keys))
                if identity in seen:
                    raise ValueError("billing page repeats a cost group")
                seen.add(identity)
                measure = group["Metrics"][query["metric"]]
                if measure["Unit"] != query["currency"]:
                    raise ValueError("billing page currency differs from pinned query")
                amount = measure["Amount"]
                decimal_amount(amount)
                series.append({"period": {"start": period["Start"], "end": period["End"]},
                               "group": keys, "amount": amount, "metric": query["metric"],
                               "currency": measure["Unit"], "granularity": query["granularity"],
                               "group_by": query["group_by"]})
        next_token = response.get("NextPageToken")
        if next_token is not None:
            if not isinstance(next_token, str) or not next_token or next_token in tokens:
                raise ValueError("billing page repeats a cursor")
            tokens.add(next_token)
        elif index != len(pages) - 1:
            raise ValueError("billing pages continued after the final cursor")
    if next_token is not None:
        complete = False
    if not series or not covers_period(query, series):
        complete = False
    return {"collection_id": collection_id, "scope_digest": scope_digest,
            "query_hash": query_hash, "query": query, "pages": references,
            "observed_at": observed_at if pages else None, "series": series,
            "amount": format(_sum([decimal_amount(row["amount"]) for row in series]), "f") if complete else None,
            "coverage": "complete" if complete else "partial" if series else "unavailable",
            "currency": query["currency"],
            "estimated": next(iter(estimated)) if len(estimated) == 1 else None,
            "source_refreshed_at": None, "freshness_reason": "source_refresh_unknown"}
