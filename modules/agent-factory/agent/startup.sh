#!/bin/sh
set -e

echo "Starting MCP Agent Mail..."

# Start health server in background
echo "Starting health server on port $PORT..."
node dist/health-server.js &
HEALTH_PID=$!

# Wait a moment for health server to start
sleep 2

# Start main agent application
echo "Starting main agent application..."
node dist/index.js &
AGENT_PID=$!

# Function to handle shutdown
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

# Set up signal handlers
trap 'shutdown' TERM INT

# Wait for either process to exit
wait $AGENT_PID $HEALTH_PID