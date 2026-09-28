"""Strict Kimi K3 pricing-card reader; endpoint coverage comes from the seed."""

import hashlib
import re
from dataclasses import replace
from decimal import Decimal

from .aws_sources import SourceValidationError, _money

KIMI_MODEL = "moonshotai.kimi-k3"
KIMI_CARD_URL = "https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-moonshot-ai-kimi-k3.md"


def parse_kimi_card(content, templates, *, verified_at):
    text = content.decode("utf-8")
    sections = re.split(r"(?m)^## Pricing\s*$", text)
    if len(sections) != 2:
        raise SourceValidationError("Kimi card requires one Pricing section")
    pricing = re.split(r"(?m)^## ", sections[1], maxsplit=1)[0].replace("**", "")
    if "per 1 million tokens" not in pricing or "Standard tier" not in pricing:
        raise SourceValidationError("Kimi price unit or tier missing")
    header = ["Inference option", "Input", "Output", "Cache read", "Cache write (30 min)"]
    prices = {}
    table = [line for line in pricing.splitlines() if line.startswith("|")]

    def cells(line):
        return [part.strip() for part in line.strip().strip("|").split("|")]

    if len(table) != 4 or cells(table[0]) != header or not all(re.fullmatch(r":?-+:?", x) for x in cells(table[1])):
        raise SourceValidationError("Kimi pricing table changed")
    for line in table[2:]:
        row = cells(line)
        if len(row) != 5 or row[0] not in ("Global CRIS", "US CRIS") or row[0] in prices:
            raise SourceValidationError("Kimi pricing geography changed")
        rates = tuple(_money(x) for x in row[1:])
        if any(x is None or x <= 0 for x in rates):
            raise SourceValidationError("Kimi price missing")
        prices[row[0]] = rates
    if set(prices) != {"Global CRIS", "US CRIS"}:
        raise SourceValidationError("Kimi pricing incomplete")
    if not re.search(r"Priority is billed at 1\.75x", pricing) or not re.search(r"Flex at 0\.5x", pricing):
        raise SourceValidationError("Kimi tier multipliers changed; review required")
    digest = hashlib.sha256(content).hexdigest()
    rows = []
    for row in templates:
        if row.model_id != KIMI_MODEL:
            continue
        if row.geography not in ("global_cris", "geo_cris") or row.context_tier != "flat":
            raise SourceValidationError("unsupported Kimi variant")
        multiplier = {"standard": Decimal(1), "priority": Decimal("1.75"), "flex": Decimal("0.5")}.get(row.service_tier)
        if multiplier is None:
            raise SourceValidationError("unsupported Kimi tier")
        inp, out, read, write = (x * multiplier for x in prices["Global CRIS" if row.geography == "global_cris" else "US CRIS"])
        rows.append(
            replace(
                row,
                input_price_per_1k_tokens=inp,
                output_price_per_1k_tokens=out,
                cache_read_price_per_1k_tokens=read,
                cache_write_price_per_1k_tokens=write,
                cache_write_policy="full_rate",
                source="model_card",
                source_url=KIMI_CARD_URL,
                source_content_sha256=digest,
                source_effective_at=None,
                verified_at=verified_at,
                snapshot_version=None,
            )
        )
    if not rows:
        raise SourceValidationError("no reviewed Kimi endpoint manifest")
    return tuple(rows)
