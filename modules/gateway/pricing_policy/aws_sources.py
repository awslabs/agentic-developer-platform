"""Strict parsers for AWS Bedrock publications, with no application dependencies.

Cards publish USD/million, catalog SKUs USD/1K. Endpoint regions are expanded
from the reviewed snapshot manifest, never guessed from a price geography.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from decimal import Decimal

from .policy import RateRow

CARD_SLUGS = {
    "openai.gpt-6-sol": "gpt-6-sol",
    "openai.gpt-6-luna": "gpt-6-luna",
    "openai.gpt-6-astra": "gpt-6-astra",
    "openai.gpt-5.6-sol": "gpt-56-sol",
    "openai.gpt-5.6-terra": "gpt-56-terra",
    "openai.gpt-5.6-luna": "gpt-56-luna",
    "openai.gpt-5.5": "gpt-55",
    "openai.gpt-5.4": "gpt-54",
    "openai.gpt-5.6-cyber": "gpt-56-cyber",
    "openai.gpt-daybreak-blue-5.6-sol": "gpt-daybreak-blue-56-sol",
}
CARD_BASE = "https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-"
CATALOG_BASE = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/current"
RATE_FIELDS = ("input_price_per_1k_tokens", "output_price_per_1k_tokens", "cache_read_price_per_1k_tokens", "cache_write_price_per_1k_tokens")


class SourceValidationError(ValueError):
    """Fetched content cannot safely be interpreted; reject the entire attempt."""


def _money(cell: str) -> Decimal | None:
    if cell in ("—", "–", "-", "N/A"):
        return None
    if not re.fullmatch(r"\$[0-9]+(?:\.[0-9]+)?", cell):
        raise SourceValidationError(f"unrecognized USD/million price {cell!r}")
    return Decimal(cell[1:]) / 1000


def _insert(rows: dict, row: RateRow) -> None:
    old = rows.get(row.variant_key)
    if old and any(getattr(old, field) != getattr(row, field) for field in RATE_FIELDS):
        raise SourceValidationError(f"conflicting duplicate variant {row.variant_key}")
    rows[row.variant_key] = row


def parse_model_card(content: bytes, model_id: str, templates: tuple[RateRow, ...], *, source_url: str, verified_at: str) -> tuple[RateRow, ...]:
    """Accept an entire recognizable Pricing section before returning any rows.

    AWS has published both bold scope paragraphs and Markdown scope headings.
    Context changes never reset an active GovCloud scope. Missing well-formed
    rows are retained by the publisher; malformed tables reject the source.
    """
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceValidationError("card is not UTF-8") from exc
    sections = re.split(r"(?m)^## Pricing\s*$", text)
    if len(sections) != 2:
        raise SourceValidationError("expected exactly one Pricing section")
    pricing = re.split(r"(?m)^## ", sections[1], maxsplit=1)[0]
    if not re.search(r"per 1 million tokens", pricing, re.I):
        raise SourceValidationError("card does not declare USD per million tokens")
    if "Standard tier" not in pricing:
        raise SourceValidationError("card tier is not recognizable")
    digest = hashlib.sha256(content).hexdigest()
    model_templates = tuple(row for row in templates if row.model_id == model_id)
    if not model_templates:
        raise SourceValidationError(f"no reviewed endpoint manifest for {model_id}")
    scope, context = "commercial", None
    header, separator, table_rows = False, False, 0
    parsed: dict = {}
    tables = 0

    def finish_table():
        if header and (not separator or not table_rows):
            raise SourceValidationError("incomplete pricing table")

    for raw in pricing.splitlines():
        line = raw.strip()
        label = re.sub(r"^[#*\s]+|[*\s]+$", "", line).lower()
        if line.startswith(("###", "**")) and not line.startswith("|:"):
            if label == "note":
                finish_table()
                header, separator, table_rows = False, False, 0
                continue
            recognized = label.startswith(("short context", "long context", "commercial regions", "aws govcloud"))
            if not recognized:
                raise SourceValidationError(f"unrecognized pricing scope {line!r}")
            finish_table()
            header, separator, table_rows = False, False, 0
            if "govcloud" in label:
                scope = "govcloud"
                context = None
            elif "commercial" in label:
                scope = "commercial"
                context = None
            if "short context" in label:
                if "272k" not in label:
                    raise SourceValidationError("unrecognized short-context threshold")
                context = "short"
            elif "long context" in label:
                if "more than" in label and "272k" not in label:
                    raise SourceValidationError("unrecognized long-context threshold")
                context = "long"
            continue
        if not line.startswith("|"):
            continue
        cells = [cell.strip().replace("**", "") for cell in line.strip("|").split("|")]
        if cells[0] == "Inference option":
            finish_table()
            write_heading = "Input — cache write" if model_id in {"openai.gpt-6-sol", "openai.gpt-6-luna"} else "Input — 30m cache write"
            if cells != ["Inference option", "Input", write_heading, "Input — cache read", "Output"]:
                raise SourceValidationError("unrecognized pricing table columns")
            header, separator, table_rows = True, False, 0
            tables += 1
            continue
        if all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            if not header or len(cells) != 5 or separator:
                raise SourceValidationError("invalid pricing table separator")
            separator = True
            continue
        if not header or not separator or len(cells) != 5:
            raise SourceValidationError("price row outside a complete recognized table")
        geography_map = {
            "In-Region": ("in_region",),
            "Mantle in-Region": ("in_region",),
            "US Geo CRIS": ("geo_cris",),
            "Geo CRIS": ("geo_cris",),
            "Global CRIS": ("global_cris",),
            "In-Region / Geo CRIS": ("in_region", "geo_cris"),
        }
        geographies = geography_map.get(cells[0])
        if geographies is None:
            raise SourceValidationError(f"unrecognized inference option {cells[0]!r}")
        if scope == "govcloud":
            if geographies != ("in_region",):
                raise SourceValidationError("unrecognized GovCloud inference mode")
            geographies = ("govcloud",)
        input_rate, write, read, output = map(_money, cells[1:])
        if input_rate is None or output is None:
            raise SourceValidationError("unpublished input/output price")
        policy = "full_rate"
        if write is None:
            if model_id not in ("openai.gpt-5.5", "openai.gpt-5.4"):
                raise SourceValidationError("unrecognized unpublished cache-write policy")
            # Explicit no-additional-fee contract for these two AWS model families:
            # cache creation tokens remain ordinary paid input, not free tokens.
            policy, write = "no_additional_fee", input_rate
        elif write != input_rate * Decimal("1.25"):
            raise SourceValidationError("published full cache-write rate is not 1.25x input")
        if read != input_rate * Decimal("0.10"):
            raise SourceValidationError("published cache-read rate is not 0.10x input")
        tier = context or "flat"
        matching = [row for row in model_templates if row.geography in geographies and row.service_tier == "standard" and row.context_tier == tier]
        for template in matching:
            row = replace(
                template,
                input_price_per_1k_tokens=input_rate,
                output_price_per_1k_tokens=output,
                cache_read_price_per_1k_tokens=read,
                cache_write_price_per_1k_tokens=write,
                cache_write_policy=policy,
                source="model_card",
                source_url=source_url,
                source_content_sha256=digest,
                source_effective_at=None,
                verified_at=verified_at,
                snapshot_version=None,
                generation_id=None,
            )
            _insert(parsed, RateRow.from_mapping(row.__dict__))
        table_rows += 1
    finish_table()
    if not tables or not parsed:
        raise SourceValidationError("required card contains no usable pricing tables")
    return tuple(parsed.values())


def parse_catalog(content: bytes, region: str, *, source_url: str, verified_at: str) -> tuple[RateRow, ...]:
    """Join real separate input/output SKUs; never require a nonexistent modelId."""
    try:
        catalog = json.loads(content, parse_float=Decimal)
        products, terms = catalog["products"], catalog["terms"]["OnDemand"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceValidationError("invalid Bedrock catalog structure") from exc
    joined: dict = {}
    for sku, product in products.items():
        attrs = product.get("attributes", {})
        if attrs.get("provider", "").lower() != "openai" or attrs.get("regionCode") != region:
            continue
        name = attrs.get("model", "").lower().replace(" ", "-")
        if name not in ("gpt-oss-20b", "gpt-oss-120b", "gpt-oss-safeguard-20b", "gpt-oss-safeguard-120b"):
            continue
        inference = attrs.get("inferenceType", "").lower()
        usage = attrs.get("usagetype", "").lower()
        if not re.fullmatch(r"(?:input|output) tokens(?: (?:standard|flex|priority|batch))?", inference):
            raise SourceValidationError(f"unrecognized inferenceType for {sku}")
        direction = inference.split()[0]
        derived = next((tier for tier in ("priority", "flex", "batch") if usage.endswith("-" + tier) or inference.endswith(" " + tier)), "standard")
        tier = attrs.get("service_tier", derived).lower()
        if tier != derived or tier not in ("standard", "priority", "flex", "batch"):
            raise SourceValidationError(f"conflicting service-tier evidence for {sku}")
        if attrs.get("feature") == "Batch Inference" and tier != "batch":
            raise SourceValidationError(f"conflicting batch evidence for {sku}")
        key = ("openai." + name, "govcloud" if region.startswith("us-gov-") else "in_region", tier, "flat", region)
        values = joined.setdefault(key, {})
        sku_terms = terms.get(sku, {})
        if not sku_terms:
            raise SourceValidationError(f"missing OnDemand term for {sku}")
        for term in sku_terms.values():
            dimensions = term.get("priceDimensions", {})
            if not dimensions:
                raise SourceValidationError(f"missing dimensions for {sku}")
            for dimension in dimensions.values():
                if dimension.get("unit") != "1K tokens" or dimension.get("beginRange") != "0" or dimension.get("endRange") != "Inf":
                    raise SourceValidationError(f"unrecognized catalog unit/range for {sku}")
                try:
                    value = Decimal(dimension["pricePerUnit"]["USD"])
                except (KeyError, ValueError, TypeError) as exc:
                    raise SourceValidationError(f"invalid USD price for {sku}") from exc
                if direction in values and values[direction] != value:
                    raise SourceValidationError(f"conflicting duplicate SKUs for {key}/{direction}")
                values[direction] = value
    digest = hashlib.sha256(content).hexdigest()
    rows = []
    for key, values in joined.items():
        if set(values) != {"input", "output"}:
            raise SourceValidationError(f"unpaired input/output SKU for {key}")
        row = dict(zip(("model_id", "geography", "service_tier", "context_tier", "region"), key))
        row.update(
            input_price_per_1k_tokens=values["input"],
            output_price_per_1k_tokens=values["output"],
            cache_read_price_per_1k_tokens=None,
            cache_write_price_per_1k_tokens=None,
            cache_write_policy="unpublished",
            source="bulk_catalog",
            source_url=source_url,
            source_content_sha256=digest,
            source_effective_at=catalog.get("publicationDate"),
            verified_at=verified_at,
        )
        rows.append(RateRow.from_mapping(row))
    if not rows:
        raise SourceValidationError(f"required catalog has no OpenAI prices for {region}")
    return tuple(rows)
