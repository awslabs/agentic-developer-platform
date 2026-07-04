#!/usr/bin/env python3
"""Backfill mis-tagged installation → tenant mappings (Issue #2769).

Context
-------
Before the auto-register write-guard (#2769) landed, the webhook
``_auto_register_installation()`` could clobber the Postgres-owned
``github_installation_id`` identity-index row with the GitHub *org login* as the
tenant. When that login is not a real ADP org (e.g. ``aws-innovate``), the
installation ends up tagged to a phantom tenant, so the owning org's admin sees
empty Activity and usage rolls up invisibly.

This script finds those mis-tagged rows and re-points them at the authoritative
Postgres tenant, then re-tags the historical ``webhook-events`` rows so past
activity/usage attributes to the correct org.

Authoritative source
---------------------
Postgres is the single source of truth. Rather than open a direct DB connection,
this script resolves the correct tenant through the gateway's
``POST /internal/v1/resolve-installation`` endpoint (added in #2769), which reads
``organizations.github_installation_ids``. This keeps the backfill Postgres-
authoritative without needing DB credentials.

Selection rule
--------------
A ``github_installation_id`` identity-index row is a backfill candidate when:
  * it carries ``auto_registered = True`` (Postgres-owned rows never do), AND
  * the gateway resolves the installation to a tenant that DIFFERS from the
    row's current ``org_id`` (i.e. the current tag is wrong / phantom).

Rows the gateway cannot resolve (404 — no org owns the installation) are left
untouched and logged: we must not guess a tenant.

Safety
------
  * Dry-run by default. Pass ``--apply`` to write.
  * Idempotent: re-running after a successful apply finds no candidates
    (rows now match Postgres, and the forward row loses ``auto_registered``).
  * Writes an undo file (JSON) capturing every changed row's prior state so the
    re-tagging can be reversed.

Usage
-----
    # dry run (default) — show what would change
    python backfill-installation-tenants.py \
        --gateway-url https://<gateway> \
        --internal-api-key-arn adp/dev/internal-api-key

    # apply
    python backfill-installation-tenants.py --gateway-url https://<gateway> --apply

    # limit to a single installation (261 seed: 144082554 → pranavsharma1000)
    python backfill-installation-tenants.py --gateway-url https://<gateway> \
        --installation-id 144082554 --apply
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import urllib.error
import urllib.request

import boto3
from boto3.dynamodb.conditions import Key

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill-installation-tenants")


def _resolve_internal_api_key(api_key: str | None, api_key_arn: str | None, region: str) -> str:
    """Resolve the internal API key from a literal value or a Secrets Manager ARN."""
    if api_key:
        return api_key
    if api_key_arn:
        sm = boto3.client("secretsmanager", region_name=region)
        return sm.get_secret_value(SecretId=api_key_arn)["SecretString"]
    return ""


def resolve_tenant_via_gateway(gateway_url: str, api_key: str, installation_id: str) -> str | None:
    """Return the Postgres-authoritative tenant for an installation, or None (404/error)."""
    url = f"{gateway_url.rstrip('/')}/internal/v1/resolve-installation"
    body = json.dumps({"installation_id": str(installation_id)}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "X-Internal-Api-Key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 201):
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("tenant_id") or None
            return None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        logger.error("resolve-installation HTTP %d for %s: %s", e.code, installation_id, e.reason)
        return None
    except Exception as e:  # noqa: BLE001
        logger.error("resolve-installation failed for %s: %s", installation_id, e)
        return None


def scan_auto_registered_installations(table, installation_id: str | None) -> list[dict]:
    """Return github_installation_id rows tagged auto_registered=True."""
    if installation_id:
        item = table.get_item(
            Key={
                "identity_type": "github_installation_id",
                "identity_value": str(installation_id),
            }
        ).get("Item")
        rows = [item] if item else []
    else:
        rows = []
        scan_kwargs = {
            "FilterExpression": Key("identity_type").eq("github_installation_id"),
        }
        while True:
            resp = table.scan(**scan_kwargs)
            rows.extend(resp.get("Items", []))
            if "LastEvaluatedKey" not in resp:
                break
            scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return [r for r in rows if r.get("auto_registered")]


def retag_webhook_events(
    events_table,
    installation_id: str,
    old_tenant: str,
    new_tenant: str,
    *,
    apply: bool,
    undo: list[dict],
) -> int:
    """Re-tag webhook-events rows for this installation from old_tenant → new_tenant.

    Queries the tenant-index (gsi1) by the OLD tenant, then updates matching items
    whose installation_id equals ours. Updates both tenant_id and GSI1PK so the
    tenant-index GSI re-projects. Returns the count of (would-be) updated rows.
    """
    count = 0
    query_kwargs = {
        "IndexName": "gsi1",
        "KeyConditionExpression": Key("GSI1PK").eq(old_tenant),
    }
    while True:
        resp = events_table.query(**query_kwargs)
        for item in resp.get("Items", []):
            if str(item.get("installation_id", "")) != str(installation_id):
                continue
            count += 1
            undo.append(
                {
                    "kind": "webhook_event",
                    "event_id": item["event_id"],
                    "arrived_at": item["arrived_at"],
                    "old_tenant_id": item.get("tenant_id"),
                    "old_GSI1PK": item.get("GSI1PK"),
                }
            )
            if apply:
                events_table.update_item(
                    Key={"event_id": item["event_id"], "arrived_at": item["arrived_at"]},
                    UpdateExpression="SET tenant_id = :t, GSI1PK = :t",
                    ExpressionAttributeValues={":t": new_tenant},
                )
        if "LastEvaluatedKey" not in resp:
            break
        query_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gateway-url",
        required=True,
        help="Gateway base URL (Postgres-authoritative resolve-installation)",
    )
    parser.add_argument("--internal-api-key", default=None, help="Internal API key (literal)")
    parser.add_argument(
        "--internal-api-key-arn", default=None, help="Secrets Manager ARN for the internal API key"
    )
    parser.add_argument("--identity-table", default="adp-dev-identity-index")
    parser.add_argument("--events-table", default="adp-dev-webhook-events")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--installation-id", default=None, help="Limit to a single installation_id")
    parser.add_argument("--apply", action="store_true", help="Write changes (default: dry-run)")
    parser.add_argument("--undo-file", default="backfill-installation-tenants-undo.json")
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    logger.info("Backfill installation→tenant mappings [%s]", mode)

    api_key = _resolve_internal_api_key(
        args.internal_api_key, args.internal_api_key_arn, args.region
    )
    if not api_key:
        logger.error("No internal API key provided (--internal-api-key or --internal-api-key-arn)")
        return 2

    dynamodb = boto3.resource("dynamodb", region_name=args.region)
    identity_table = dynamodb.Table(args.identity_table)
    events_table = dynamodb.Table(args.events_table)

    candidates = scan_auto_registered_installations(identity_table, args.installation_id)
    logger.info("Found %d auto_registered installation row(s) to inspect", len(candidates))

    undo: list[dict] = []
    fixed = 0
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    for row in candidates:
        installation_id = row["identity_value"]
        old_tenant = row.get("org_id", "")
        correct_tenant = resolve_tenant_via_gateway(args.gateway_url, api_key, installation_id)

        if not correct_tenant:
            logger.info(
                "skip installation_id=%s — gateway did not resolve a tenant (no org owns it)",
                installation_id,
            )
            continue
        if correct_tenant == old_tenant:
            logger.info(
                "ok   installation_id=%s already tagged to Postgres tenant=%s",
                installation_id,
                correct_tenant,
            )
            continue

        logger.info(
            "FIX  installation_id=%s  %s → %s",
            installation_id,
            old_tenant,
            correct_tenant,
        )
        fixed += 1

        # Forward row: re-point to Postgres tenant, drop auto_registered.
        undo.append(
            {
                "kind": "identity_forward",
                "identity_type": "github_installation_id",
                "identity_value": installation_id,
                "old_org_id": old_tenant,
                "old_auto_registered": row.get("auto_registered"),
            }
        )
        if args.apply:
            identity_table.put_item(
                Item={
                    "identity_type": "github_installation_id",
                    "identity_value": installation_id,
                    "org_id": correct_tenant,
                    "updated_at": now,
                }
            )

        # Reverse rows: point the correct tenant at this installation; remove the
        # stale phantom-tenant reverse row if present.
        undo.append(
            {
                "kind": "identity_reverse",
                "identity_type": "org_installation",
                "old_phantom_tenant": old_tenant,
                "new_tenant": correct_tenant,
                "installation_id": installation_id,
            }
        )
        if args.apply:
            identity_table.put_item(
                Item={
                    "identity_type": "org_installation",
                    "identity_value": correct_tenant,
                    "installation_id": int(installation_id),
                    "updated_at": now,
                }
            )
            if old_tenant:
                stale = identity_table.get_item(
                    Key={"identity_type": "org_installation", "identity_value": old_tenant}
                ).get("Item")
                if stale and str(stale.get("installation_id")) == str(installation_id):
                    identity_table.delete_item(
                        Key={"identity_type": "org_installation", "identity_value": old_tenant}
                    )

        # Re-tag historical webhook-events rows.
        n_events = retag_webhook_events(
            events_table,
            installation_id,
            old_tenant,
            correct_tenant,
            apply=args.apply,
            undo=undo,
        )
        logger.info(
            "     %d webhook-events row(s) re-tagged for installation_id=%s",
            n_events,
            installation_id,
        )

    logger.info("Summary: %d installation(s) fixed, %d undo record(s)", fixed, len(undo))

    if args.apply and undo:
        with open(args.undo_file, "w") as fh:
            json.dump({"created_at": now, "records": undo}, fh, indent=2, default=str)
        logger.info("Undo snapshot written to %s", args.undo_file)
    elif not args.apply:
        logger.info("DRY-RUN complete — re-run with --apply to write changes")

    return 0


if __name__ == "__main__":
    sys.exit(main())
