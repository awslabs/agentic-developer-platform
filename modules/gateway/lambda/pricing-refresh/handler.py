"""
Pricing Refresh Lambda Handler.

Runs daily via EventBridge schedule. Fetches real-time model pricing from
the AWS Pricing API and writes to the model_pricing table in RDS.

Issue #234: Budget Usage Tracking Lambda

Environment Variables:
    DB_HOST: RDS hostname
    DB_PORT: RDS port (default: 5432)
    DB_NAME: Database name (default: bedrockgateway)
    DB_USERNAME: Database username (default: bgadmin)
    AWS_REGION: AWS region (default: us-east-1)
"""

import json
import logging
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3

# Add shared module to path for local Lambda deployment
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))

from db import get_db_connection
from pricing_fallback import MODEL_PRICING

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# AWS Pricing API is only available in us-east-1 and ap-south-1
PRICING_API_REGION = "us-east-1"


def extract_model_pricing_from_product(product: dict[str, Any]) -> dict[str, Any] | None:
    """
    Extract model pricing from AWS Pricing API product data.

    The AWS Pricing API returns complex nested JSON with pricing dimensions.
    This function parses the structure to extract per-token pricing.

    Args:
        product: Product data from AWS Pricing API

    Returns:
        Dict with model_id, input_price, output_price, or None if not parseable
    """
    try:
        attributes = product.get("product", {}).get("attributes", {})
        model_id = attributes.get("modelId")

        if not model_id:
            return None

        # Extract pricing from terms
        terms = product.get("terms", {})
        on_demand = terms.get("OnDemand", {})

        if not on_demand:
            return None

        input_price = None
        output_price = None

        # Parse pricing dimensions
        for term_id, term_data in on_demand.items():
            price_dimensions = term_data.get("priceDimensions", {})

            for dim_id, dim_data in price_dimensions.items():
                description = dim_data.get("description", "").lower()
                unit = dim_data.get("unit", "").lower()
                price_per_unit = dim_data.get("pricePerUnit", {})
                usd_price = price_per_unit.get("USD")

                if usd_price is None:
                    continue

                price = Decimal(usd_price)

                # Normalize price to per-1000-tokens
                # AWS Pricing API may report per-token or per-1000-tokens
                if "1000" in unit or "1k" in unit:
                    pass  # Already per 1000
                elif "token" in unit:
                    price = price * Decimal("1000")

                # Determine if input or output pricing
                if "input" in description:
                    input_price = price
                elif "output" in description:
                    output_price = price

        if input_price is not None and output_price is not None:
            return {
                "model_id": model_id,
                "input_price": input_price,
                "output_price": output_price,
            }

        return None

    except Exception as e:
        logger.warning(f"Error parsing product: {e}")
        return None


def fetch_bedrock_pricing() -> list[dict[str, Any]]:
    """
    Fetch Bedrock model pricing from AWS Pricing API.

    Returns:
        List of dicts with model_id, input_price, output_price
    """
    pricing_client = boto3.client("pricing", region_name=PRICING_API_REGION)
    models = []

    try:
        # Paginate through all Bedrock products
        paginator = pricing_client.get_paginator("get_products")

        for page in paginator.paginate(
            ServiceCode="AmazonBedrock",
            FormatVersion="aws_v1",
        ):
            for price_item_str in page.get("PriceList", []):
                try:
                    product = json.loads(price_item_str)
                    model_pricing = extract_model_pricing_from_product(product)

                    if model_pricing:
                        models.append(model_pricing)
                        logger.debug(f"Found pricing for {model_pricing['model_id']}")

                except json.JSONDecodeError:
                    continue

        logger.info(f"Fetched pricing for {len(models)} models from AWS Pricing API")

    except Exception as e:
        logger.error(f"Error fetching from AWS Pricing API: {e}", exc_info=True)
        raise

    return models


def upsert_model_pricing(
    conn,
    model_id: str,
    input_price: Decimal,
    output_price: Decimal,
    source: str = "pricing_api",
):
    """
    Upsert model pricing into the database.

    Args:
        conn: Database connection
        model_id: Model ID
        input_price: Input price per 1000 tokens
        output_price: Output price per 1000 tokens
        source: Pricing source ('pricing_api' or 'fallback')
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO model_pricing (
                model_id, input_price_per_1k_tokens, output_price_per_1k_tokens,
                source, updated_at
            )
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (model_id)
            DO UPDATE SET
                input_price_per_1k_tokens = EXCLUDED.input_price_per_1k_tokens,
                output_price_per_1k_tokens = EXCLUDED.output_price_per_1k_tokens,
                source = EXCLUDED.source,
                updated_at = EXCLUDED.updated_at
            """,
            (
                model_id,
                float(input_price),
                float(output_price),
                source,
                datetime.now(UTC),
            ),
        )


def load_fallback_pricing(conn) -> int:
    """
    Load hardcoded fallback pricing into the database.

    Called when AWS Pricing API fails.

    Args:
        conn: Database connection

    Returns:
        Number of models loaded
    """
    count = 0
    for model_id, pricing in MODEL_PRICING.items():
        if model_id == "default":
            continue

        try:
            upsert_model_pricing(
                conn,
                model_id,
                pricing["input"],
                pricing["output"],
                source="fallback",
            )
            count += 1
        except Exception as e:
            logger.warning(f"Failed to insert fallback pricing for {model_id}: {e}")

    return count


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """
    Lambda handler for EventBridge scheduled events.

    Fetches model pricing from AWS Pricing API and updates the database.
    Falls back to hardcoded pricing if the API fails.

    Args:
        event: EventBridge event
        context: Lambda context

    Returns:
        Response dict with status
    """
    logger.info("Pricing refresh Lambda invoked")

    api_models_count = 0
    fallback_models_count = 0
    source_used = "pricing_api"
    error_message = None

    try:
        with get_db_connection() as conn:
            # Try to fetch from AWS Pricing API first
            try:
                models = fetch_bedrock_pricing()

                if models:
                    for model in models:
                        upsert_model_pricing(
                            conn,
                            model["model_id"],
                            model["input_price"],
                            model["output_price"],
                            source="pricing_api",
                        )
                        api_models_count += 1

                    logger.info(f"Updated {api_models_count} models from AWS Pricing API")
                else:
                    # No models found - fall back to hardcoded pricing
                    logger.warning("No models found from AWS Pricing API, using fallback")
                    fallback_models_count = load_fallback_pricing(conn)
                    source_used = "fallback"

            except Exception as e:
                # AWS Pricing API failed - use fallback
                logger.error(f"AWS Pricing API failed: {e}, using fallback")
                error_message = str(e)
                fallback_models_count = load_fallback_pricing(conn)
                source_used = "fallback"

    except Exception as e:
        logger.error(f"Database connection error: {e}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps(
                {
                    "error": "database_connection_failed",
                    "message": str(e),
                }
            ),
        }

    result = {
        "statusCode": 200,
        "body": json.dumps(
            {
                "source": source_used,
                "api_models_updated": api_models_count,
                "fallback_models_loaded": fallback_models_count,
                "error": error_message,
            }
        ),
    }

    logger.info(f"Completed: {result['body']}")
    return result
