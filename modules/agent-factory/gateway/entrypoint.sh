#!/bin/sh
set -e
echo "=== Agent Gateway SQS Consumer ==="
echo "  Queue: ${INPUT_QUEUE_URL:-not set}"
echo "  Agent: ${AGENT_DIR:-/app/agent}"
exec python3 /app/app/sqs_consumer.py
