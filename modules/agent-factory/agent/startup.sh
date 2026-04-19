#!/bin/sh
set -e

echo "Starting ADP Agent (entrypoint: ${AGENT_ENTRYPOINT:-github})..."

case "${AGENT_ENTRYPOINT:-github}" in
  github)
    # Existing path: health server + GitHub ARC agent worker
    echo "Starting health server on port $PORT..."
    node dist/health-server.js &
    HEALTH_PID=$!

    sleep 2

    echo "Starting GitHub agent worker..."
    node dist/index.js &
    AGENT_PID=$!

    # Signal handler for graceful shutdown
    shutdown() {
        echo "Shutting down services..."
        if [ ! -z "$AGENT_PID" ]; then
            kill $AGENT_PID 2>/dev/null || true
        fi
        if [ ! -z "$HEALTH_PID" ]; then
            kill $HEALTH_PID 2>/dev/null || true
        fi
        wait
        exit 0
    }
    trap 'shutdown' TERM INT

    wait $AGENT_PID $HEALTH_PID
    ;;

  complex-task-chat)
    # New path: SQS FIFO consumer, process one message and exit
    # No health server needed — KEDA ScaledJob pods are ephemeral
    echo "Starting complex-task-chat agent..."
    node dist/complex-task-chat/complex-task-chat-agent.js
    ;;

  *)
    echo "Unknown AGENT_ENTRYPOINT: ${AGENT_ENTRYPOINT}" >&2
    exit 1
    ;;
esac
