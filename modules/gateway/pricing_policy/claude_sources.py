"""AWS Bedrock Claude rates: semantic pricing widgets joined to their USD map.

The bulk catalog and current Claude model cards do not publish these prices.
Widget headers establish units and dimensions; opaque tokens alone do not.
Endpoint availability is the reviewed, immutable model-card manifest. Missing
well-formed prices are omitted so publication retains their last verified rows.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from html.parser import HTMLParser

from .aws_sources import SourceValidationError
from .policy import RateRow

PRICING_PAGE_URL = "https://aws.amazon.com/bedrock/pricing/"
TOKEN_MAP_URL = "https://b0.p.awsstatic.com/pricing/2.0/meteredUnitMaps/bedrockfoundationmodels/USD/current/bedrockfoundationmodels.json"
SOURCE_URL = PRICING_PAGE_URL + "#anthropic;token-map=" + TOKEN_MAP_URL
HEADERS = (
    "Anthropic models",
    "Price per 1M input tokens",
    "Price per 1M output tokens",
    "Price per 1M input tokens (batch)",
    "Price per 1M output tokens (batch)",
    "Price per 1M input tokens (5m cache write)",
    "Price per 1M input tokens (1h cache write)",
    "Price per 1M input tokens (cache read)",
)
SCOPES = {"Global Cross-region Inference": "global_cris", "Geo and In-region Cross-region Inference": "regional"}
TOKEN = re.compile(r"\{priceOf!bedrockfoundationmodels/bedrockfoundationmodels!([A-Za-z0-9_-]+)(?:!opt)?\}")
MONEY = re.compile(r"[0-9]+(?:\.[0-9]+)?")
RATE_FIELDS = (
    "input_price_per_1k_tokens",
    "output_price_per_1k_tokens",
    "cache_read_price_per_1k_tokens",
    "cache_write_price_per_1k_tokens",
    "cache_write_1h_price_per_1k_tokens",
)


class _Widgets(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.markups = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        markup = attributes.get("data-pricing-markup")
        if markup and "aws-plc" in attributes.get("class", "").split():
            self.markups.append(markup)


class _Table(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.outside = [], []
        self.row = self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            if self.row is not None:
                raise SourceValidationError("nested pricing table row")
            self.row = []
        elif tag in ("td", "th"):
            if self.row is None or self.cell is not None:
                raise SourceValidationError("invalid pricing table cell")
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)
        elif data.strip():
            self.outside.append(data.strip())

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            if self.cell is None or self.row is None:
                raise SourceValidationError("unpaired pricing table cell")
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr":
            if self.row is None or self.cell is not None:
                raise SourceValidationError("incomplete pricing table row")
            self.rows.append(self.row)
            self.row = None


def source_digest(page: bytes, token_map: bytes) -> str:
    """Hash a canonical manifest of BOTH source documents, in a stable order."""
    hashes = {"pricing_page_sha256": hashlib.sha256(page).hexdigest(), "token_map_sha256": hashlib.sha256(token_map).hexdigest()}
    return hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SourceValidationError(f"duplicate token-map key {key!r}")
        result[key] = value
    return result


def _price_tables(content: bytes, models: dict) -> dict:
    try:
        page = _Widgets()
        page.feed(content.decode("utf-8"))
        page.close()
    except (UnicodeError, ValueError) as exc:
        raise SourceValidationError("invalid AWS pricing HTML") from exc
    names = {
        name: model
        for model, meta in models.items()
        if model.startswith("anthropic.") and "pricing_name" in meta
        for name in {meta["pricing_name"], meta["name"]}
    }
    tables = {}
    for markup in page.markups:
        # Reserved TPM/hour and latency-optimized widgets have different units;
        # they cannot be projected onto the standard token-rate dimensions.
        if not any(scope in markup for scope in SCOPES):
            continue
        table = _Table()
        table.feed(markup)
        table.close()
        if not table.rows or table.row is not None or table.cell is not None:
            raise SourceValidationError("incomplete Claude pricing widget")
        if table.rows[0][0] != "Anthropic models":
            continue
        reserved_headers = (
            "Anthropic models",
            "Price per hour per 1K Input TPM with 1-Month Commitment",
            "Price per hour per 1K Output TPM with 1-Month Commitment",
            "Price per hour per 1K Input TPM with 3-Month Commitment",
            "Price per hour per 1K Output TPM with 3-Month Commitment",
        )
        if tuple(table.rows[0]) == reserved_headers:
            continue
        if tuple(table.rows[0]) != HEADERS:
            raise SourceValidationError("unrecognized Claude pricing columns or units")
        scope_labels = [label for label in table.outside if label in SCOPES]
        if len(scope_labels) != 1:
            raise SourceValidationError("ambiguous Claude pricing scope")
        scope = SCOPES[scope_labels[0]]
        if scope in tables:
            raise SourceValidationError("duplicate Claude pricing scope")
        rows = {}
        for cells in table.rows[1:]:
            if len(cells) != len(HEADERS):
                raise SourceValidationError("incomplete Claude price row")
            model = names.get(cells[0].replace("**", ""))
            if model is None:
                continue  # New/legacy models require an audited availability manifest.
            tokens = []
            for value in cells[1:]:
                if value == "N/A":
                    tokens.append(None)
                else:
                    match = TOKEN.fullmatch(value)
                    if match is None:
                        raise SourceValidationError(f"unrecognized Claude price expression {value!r}")
                    tokens.append(match[1])
            if model in rows and rows[model] != tokens:
                raise SourceValidationError(f"conflicting duplicate Claude model {model}")
            rows[model] = tokens
        tables[scope] = rows
    if set(tables) != {"global_cris", "regional"}:
        raise SourceValidationError("both Claude regional and global pricing widgets are required")
    return tables


def _map(content: bytes):
    try:
        data = json.loads(content, object_pairs_hook=_unique_object)
        manifest, regions = data["manifest"], data["regions"]
        if manifest["serviceId"] != "bedrockfoundationmodels" or manifest["currencyCode"] != "USD":
            raise ValueError("incorrect service or currency")
        published = manifest["hawkFilePublicationDate"]
        if datetime.fromisoformat(published.replace("Z", "+00:00")).utcoffset() is None:
            raise ValueError("publication timestamp lacks timezone")
        if not isinstance(regions, dict) or not regions or not all(isinstance(value, dict) for value in regions.values()):
            raise ValueError("invalid regions")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise SourceValidationError("invalid AWS Claude token-map structure") from exc
    return regions, published


def _amount(region_map, token):
    if token is None or token not in region_map:
        return None
    item = region_map[token]
    if not isinstance(item, dict) or not isinstance(item.get("price"), str) or not MONEY.fullmatch(item["price"]):
        raise SourceValidationError("invalid USD Claude token-map price")
    if not isinstance(item.get("rateCode"), str) or not item["rateCode"]:
        raise SourceValidationError("missing AWS Claude SKU rate code")
    amount = Decimal(item["price"])
    if amount <= 0:
        raise SourceValidationError("Claude token-map rate must be positive")
    return amount / 1000  # Widget explicitly declares USD per one million tokens.


def parse_claude_pricing(page: bytes, token_map: bytes, templates: tuple[RateRow, ...], models: dict, *, verified_at: str) -> tuple[RateRow, ...]:
    """Resolve only reviewed model/region/tier keys using exact AWS map entries."""
    tables = _price_tables(page, models)
    regions, published = _map(token_map)
    digest = source_digest(page, token_map)
    parsed = {}
    for template in templates:
        if not template.model_id.startswith("anthropic."):
            continue
        metadata = models.get(template.model_id, {})
        if "pricing_name" not in metadata:
            continue
        if template.context_tier != "flat" or template.service_tier not in ("standard", "batch"):
            continue
        availability = [
            a
            for a in metadata.get("endpoint_availability", ())
            if a["endpoint_region"] == template.region
            and (a["inference_mode"] == template.geography or (template.geography == "govcloud" and template.region.startswith("us-gov-")))
        ]
        for endpoint in availability:
            scope = "global_cris" if endpoint["inference_mode"] == "global_cris" else "regional"
            tokens = tables[scope].get(template.model_id)
            region_map = regions.get(endpoint["pricing_region_label"])
            if tokens is None or region_map is None:
                continue
            if template.service_tier == "batch":
                input_rate, output = (_amount(region_map, token) for token in tokens[2:4])
                write5 = write1 = read = None
            else:
                input_rate, output = (_amount(region_map, token) for token in tokens[:2])
                write5, write1, read = (_amount(region_map, token) for token in tokens[4:])
            if input_rate is None or output is None:
                continue
            # A disappeared cache dimension must not erase the last verified
            # price or refresh its age. Retain the complete prior variant.
            if any(
                old is not None and fresh is None
                for old, fresh in (
                    (template.cache_write_price_per_1k_tokens, write5),
                    (template.cache_write_1h_price_per_1k_tokens, write1),
                    (template.cache_read_price_per_1k_tokens, read),
                )
            ):
                continue
            row = replace(
                template,
                input_price_per_1k_tokens=input_rate,
                output_price_per_1k_tokens=output,
                cache_read_price_per_1k_tokens=read,
                cache_write_price_per_1k_tokens=write5,
                cache_write_1h_price_per_1k_tokens=write1,
                cache_write_policy="full_rate" if write5 is not None else "unpublished",
                source="pricing_page",
                source_url=SOURCE_URL,
                source_content_sha256=digest,
                source_effective_at=published,
                verified_at=verified_at,
                snapshot_version=None,
                generation_id=None,
            )
            row = RateRow.from_mapping(row.__dict__)
            previous = parsed.get(row.variant_key)
            if previous and any(getattr(previous, field) != getattr(row, field) for field in RATE_FIELDS):
                raise SourceValidationError(f"conflicting Claude variant {row.variant_key}")
            parsed[row.variant_key] = row
    if not parsed:
        raise SourceValidationError("required Claude publication contains no usable reviewed prices")
    return tuple(parsed.values())
