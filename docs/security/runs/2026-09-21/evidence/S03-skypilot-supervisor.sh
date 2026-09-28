#!/bin/sh
set -eu

readonly CONTROL_DIR=/home/sky/.sky
readonly PID_FILE="${CONTROL_DIR}/s03-server.pid"
readonly STATE_FILE="${CONTROL_DIR}/s03-server.state"
readonly RESTART_FILE="${CONTROL_DIR}/s03-server.restart"
child_pid=''

terminate() {
  if [ -n "${child_pid}" ]; then
    kill -TERM "${child_pid}" 2>/dev/null || true
    wait "${child_pid}" 2>/dev/null || true
  fi
  exit 0
}
trap terminate INT TERM

while :; do
  rm -f "${PID_FILE}" "${STATE_FILE}" "${RESTART_FILE}"
  python3 -m sky.server.server \
    --host=127.0.0.1 --port=46580 --metrics-port=9090 &
  child_pid=$!
  printf '%s\n' "${child_pid}" >"${PID_FILE}"

  set +e
  wait "${child_pid}"
  child_status=$?
  set -e
  child_pid=''
  printf 'stopped:%s\n' "${child_status}" >"${STATE_FILE}"

  while [ ! -e "${RESTART_FILE}" ]; do
    sleep 1
  done
done
