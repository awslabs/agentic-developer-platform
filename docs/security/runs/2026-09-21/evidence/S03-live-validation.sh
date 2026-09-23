#!/usr/bin/env bash
set -Eeuo pipefail
IFS=$'\n\t'

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly REPO_ROOT="$(cd "${SCRIPT_DIR}/../../../../.." && pwd)"
readonly SKY_REPOSITORY="${S03_SKYPILOT_REPOSITORY:?set S03_SKYPILOT_REPOSITORY to the S21-published derived-image repository}"
readonly SKY_DIGEST="sha256:f8d893cfabadff1946f7ccb8e2c0ba002322d1bfb0ee416cf6ce039f45de55ec"
readonly SKY_IMAGE="${SKY_REPOSITORY}@${SKY_DIGEST}"
readonly POSTGRES_IMAGE="${S03_POSTGRES_IMAGE:-postgres:16.15-alpine}"
readonly PROXY_PORT="${S03_PROXY_PORT:-46581}"
readonly CONTROLLER_DIR="${REPO_ROOT}/modules/domain-apps/superplane/src/superplane-controller"
readonly PROXY_SOURCE="${REPO_ROOT}/modules/domain-apps/superplane/src/superplane-api"
readonly GO_PROBE="${SCRIPT_DIR}/S03-controller-client-check.go"
readonly SUPERVISOR="${SCRIPT_DIR}/S03-skypilot-supervisor.sh"

for command_name in curl docker go grep python3; do
  command -v "${command_name}" >/dev/null || {
    printf 'missing required command: %s\n' "${command_name}" >&2
    exit 2
  }
done
docker info >/dev/null

if [[ -n "${S03_TRANSCRIPT:-}" ]]; then
  umask 077
  exec > >(tee "${S03_TRANSCRIPT}") 2>&1
fi

readonly RUN_ID="s03-$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
readonly NETWORK="${RUN_ID}"
readonly POSTGRES_CONTAINER="${RUN_ID}-postgres"
readonly SKY_CONTAINER="${RUN_ID}-skypilot"
readonly PROXY_CONTAINER="${RUN_ID}-proxy"
readonly SKY_HOME_VOLUME="${RUN_ID}-home"
readonly RESPONSE_FILE="$(mktemp "${TMPDIR:-/tmp}/${RUN_ID}-response.XXXXXX")"
readonly SERVICE_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(36))')"
readonly POSTGRES_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
readonly TEST_USER_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
readonly POSTGRES_USER="s03"
readonly POSTGRES_DATABASE="s03_skypilot"
readonly TEST_USER="s03-restart-proof"
readonly DATABASE_URI="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DATABASE}"

cleanup() {
  for container in "${PROXY_CONTAINER}" "${SKY_CONTAINER}" "${POSTGRES_CONTAINER}"; do
    if [[ "$(docker container inspect --format \
      '{{index .Config.Labels "adp.security-run"}}' "${container}" 2>/dev/null || true)" == "${RUN_ID}" ]]; then
      docker container rm --force "${container}" >/dev/null 2>&1 || true
    fi
  done
  if [[ "$(docker network inspect --format \
    '{{index .Labels "adp.security-run"}}' "${NETWORK}" 2>/dev/null || true)" == "${RUN_ID}" ]]; then
    docker network rm "${NETWORK}" >/dev/null 2>&1 || true
  fi
  if [[ "$(docker volume inspect --format \
    '{{index .Labels "adp.security-run"}}' "${SKY_HOME_VOLUME}" 2>/dev/null || true)" == "${RUN_ID}" ]]; then
    docker volume rm "${SKY_HOME_VOLUME}" >/dev/null 2>&1 || true
  fi
  rm -f "${RESPONSE_FILE}"
}
trap cleanup EXIT

wait_for_container_command() {
  local description="$1"
  shift
  for _ in $(seq 1 180); do
    if docker exec "${SKY_CONTAINER}" "$@" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  printf 'timed out waiting for %s\n' "${description}" >&2
  return 1
}

request_status() {
  curl --noproxy '*' --silent --show-error \
    --output "${RESPONSE_FILE}" --write-out '%{http_code}' "$@"
}

expect_status() {
  local expected="$1"
  shift
  local actual
  actual="$(request_status "$@")"
  if [[ "${actual}" != "${expected}" ]]; then
    printf 'expected HTTP %s, received %s: ' "${expected}" "${actual}" >&2
    cat "${RESPONSE_FILE}" >&2
    printf '\n' >&2
    return 1
  fi
}

assert_health_response() {
  python3 - "${RESPONSE_FILE}" <<'PY'
import json
import pathlib
import sys

response = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert response["status"] == "healthy", response
assert response["version"] == "0.12.3", response
assert response["commit"] == "9578bbb678b88b24c8244893afe6a4f967750ff4", response
assert response["external_proxy_auth_enabled"] is True, response
print(
    "authenticated_health=healthy "
    f"version={response['version']} "
    f"commit={response['commit']} "
    "external_proxy_auth_enabled=true"
)
PY
}

assert_test_user_response() {
  python3 - "${RESPONSE_FILE}" "${TEST_USER}" <<'PY'
import json
import pathlib
import sys

users = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
expected_name = sys.argv[2]
matches = [user for user in users if user.get("name") == expected_name]
assert len(matches) == 1, users
assert matches[0].get("user_type") == "basic", matches[0]
print(f"persisted_api_record={expected_name} user_type=basic")
PY
}

printf 'run_id=%s\n' "${RUN_ID}"
printf 'skypilot_image=%s\n' "${SKY_IMAGE}"
printf 'postgres_uri=postgresql://%s:<redacted>@postgres:5432/%s\n' \
  "${POSTGRES_USER}" "${POSTGRES_DATABASE}"

docker pull --platform linux/amd64 "${SKY_IMAGE}"
docker pull "${POSTGRES_IMAGE}"

repo_digests="$(docker image inspect \
  --format '{{range .RepoDigests}}{{println .}}{{end}}' "${SKY_IMAGE}")"
grep --fixed-strings --line-regexp "${SKY_IMAGE}" <<<"${repo_digests}" >/dev/null
[[ "$(docker image inspect --format '{{.Architecture}}' "${SKY_IMAGE}")" == "amd64" ]]
printf 'exact_digest_verified=%s platform=linux/amd64\n' "${SKY_DIGEST}"

docker network create --label "adp.security-run=${RUN_ID}" "${NETWORK}" >/dev/null
docker volume create --label "adp.security-run=${RUN_ID}" "${SKY_HOME_VOLUME}" >/dev/null

docker run --detach --name "${POSTGRES_CONTAINER}" \
  --label "adp.security-run=${RUN_ID}" \
  --network "${NETWORK}" --network-alias postgres \
  --env "POSTGRES_USER=${POSTGRES_USER}" \
  --env "POSTGRES_PASSWORD=${POSTGRES_PASSWORD}" \
  --env "POSTGRES_DB=${POSTGRES_DATABASE}" \
  --health-cmd "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DATABASE}" \
  --health-interval 1s --health-timeout 5s --health-retries 60 \
  "${POSTGRES_IMAGE}" >/dev/null

for _ in $(seq 1 60); do
  [[ "$(docker inspect --format '{{.State.Health.Status}}' "${POSTGRES_CONTAINER}")" == "healthy" ]] && break
  sleep 1
done
[[ "$(docker inspect --format '{{.State.Health.Status}}' "${POSTGRES_CONTAINER}")" == "healthy" ]]

docker run --rm --platform linux/amd64 --user 0:0 \
  --volume "${SKY_HOME_VOLUME}:/home/sky" \
  "${SKY_IMAGE}" sh -ceu \
  'mkdir -p /home/sky/.sky; printf "{}\n" > /home/sky/.sky/config.yaml; chown -R 1000:1000 /home/sky'

docker run --detach --platform linux/amd64 --name "${SKY_CONTAINER}" \
  --label "adp.security-run=${RUN_ID}" \
  --network "${NETWORK}" \
  --publish "127.0.0.1:${PROXY_PORT}:46581" \
  --user 1000:1000 --cap-drop ALL --security-opt no-new-privileges \
  --env HOME=/home/sky --env USER=skypilot \
  --env IS_SKYPILOT_SERVER=true \
  --env SKYPILOT_SKIP_CLOUD_IDENTITY_CHECK=0 \
  --env SKYPILOT_GLOBAL_CONFIG=/home/sky/.sky/config.yaml \
  --env "SKYPILOT_DB_CONNECTION_URI=${DATABASE_URI}" \
  --volume "${SKY_HOME_VOLUME}:/home/sky" \
  --volume "${SUPERVISOR}:/opt/s03/S03-skypilot-supervisor.sh:ro" \
  "${SKY_IMAGE}" sh /opt/s03/S03-skypilot-supervisor.sh >/dev/null

wait_for_container_command "SkyPilot health" python3 -c \
  "import json, urllib.request; assert json.load(urllib.request.urlopen('http://127.0.0.1:46580/api/health', timeout=5))['status'] == 'healthy'"
readonly FIRST_SERVER_PID="$(docker exec "${SKY_CONTAINER}" cat /home/sky/.sky/s03-server.pid)"

database_identity="$(docker exec --env "PGPASSWORD=${POSTGRES_PASSWORD}" \
  "${POSTGRES_CONTAINER}" psql --no-psqlrc --tuples-only --no-align \
  --username "${POSTGRES_USER}" --dbname "${POSTGRES_DATABASE}" \
  --command "SELECT current_database() || '|' || current_schema();")"
[[ "${database_identity}" == "${POSTGRES_DATABASE}|public" ]]
table_count="$(docker exec --env "PGPASSWORD=${POSTGRES_PASSWORD}" \
  "${POSTGRES_CONTAINER}" psql --no-psqlrc --tuples-only --no-align \
  --username "${POSTGRES_USER}" --dbname "${POSTGRES_DATABASE}" \
  --command "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';")"
(( table_count >= 27 ))
required_schema_count="$(docker exec --env "PGPASSWORD=${POSTGRES_PASSWORD}" \
  "${POSTGRES_CONTAINER}" psql --no-psqlrc --tuples-only --no-align \
  --username "${POSTGRES_USER}" --dbname "${POSTGRES_DATABASE}" \
  --command "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_name IN ('clusters', 'storage', 'users');")"
[[ "${required_schema_count}" == "3" ]]
printf 'postgres_identity=%s schema=public tables=%s required_schema=clusters,storage,users\n' \
  "${POSTGRES_DATABASE}" "${table_count}"

docker run --detach --platform linux/amd64 --name "${PROXY_CONTAINER}" \
  --label "adp.security-run=${RUN_ID}" \
  --network "container:${SKY_CONTAINER}" \
  --user 1000:1000 --read-only --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --cap-drop ALL --security-opt no-new-privileges \
  --env HOME=/tmp --env PYTHONPATH=/opt/superplane-api \
  --env "SKYPILOT_SERVICE_TOKEN=${SERVICE_TOKEN}" \
  --volume "${PROXY_SOURCE}:/opt/superplane-api:ro" \
  "${SKY_IMAGE}" python3 -m app.skypilot_proxy >/dev/null

for _ in $(seq 1 60); do
  if [[ "$(request_status "http://127.0.0.1:${PROXY_PORT}/api/health" || true)" == "401" ]]; then
    break
  fi
  sleep 1
done
expect_status 401 "http://127.0.0.1:${PROXY_PORT}/api/health"
printf 'unauthenticated_health=401\n'

expect_status 200 \
  --header "Authorization: Bearer ${SERVICE_TOKEN}" \
  "http://127.0.0.1:${PROXY_PORT}/api/health"
assert_health_response

(
  cd "${CONTROLLER_DIR}"
  S03_SKYPILOT_ENDPOINT="http://127.0.0.1:${PROXY_PORT}" \
    S03_SKYPILOT_SERVICE_TOKEN="${SERVICE_TOKEN}" \
    go run "${GO_PROBE}"
)

expect_status 200 --request POST \
  --header "Authorization: Bearer ${SERVICE_TOKEN}" \
  --header 'Content-Type: application/json' \
  --data "$(python3 -c 'import json,sys; print(json.dumps({"username": sys.argv[1], "password": sys.argv[2]}))' "${TEST_USER}" "${TEST_USER_PASSWORD}")" \
  "http://127.0.0.1:${PROXY_PORT}/users/create"

expect_status 200 \
  --header "Authorization: Bearer ${SERVICE_TOKEN}" \
  "http://127.0.0.1:${PROXY_PORT}/users"
assert_test_user_response

postgres_user_type="$(docker exec --env "PGPASSWORD=${POSTGRES_PASSWORD}" \
  "${POSTGRES_CONTAINER}" psql --no-psqlrc --tuples-only --no-align \
  --username "${POSTGRES_USER}" --dbname "${POSTGRES_DATABASE}" \
  --command "SELECT type FROM users WHERE name = '${TEST_USER}';")"
[[ "${postgres_user_type}" == "basic" ]]
printf 'postgres_record=%s user_type=basic\n' "${TEST_USER}"

docker exec "${SKY_CONTAINER}" sh -ceu \
  'kill -TERM "$(cat /home/sky/.sky/s03-server.pid)"'
wait_for_container_command "SkyPilot stop" grep -q '^stopped:' /home/sky/.sky/s03-server.state

for _ in $(seq 1 30); do
  if [[ "$(request_status --header "Authorization: Bearer ${SERVICE_TOKEN}" \
    "http://127.0.0.1:${PROXY_PORT}/api/health" || true)" == "503" ]]; then
    break
  fi
  sleep 1
done
expect_status 503 \
  --header "Authorization: Bearer ${SERVICE_TOKEN}" \
  "http://127.0.0.1:${PROXY_PORT}/api/health"
printf 'stopped_health=503\n'

docker exec "${SKY_CONTAINER}" sh -ceu ': > /home/sky/.sky/s03-server.restart'
for _ in $(seq 1 180); do
  current_pid="$(docker exec "${SKY_CONTAINER}" cat /home/sky/.sky/s03-server.pid 2>/dev/null || true)"
  if [[ -n "${current_pid}" && "${current_pid}" != "${FIRST_SERVER_PID}" ]]; then
    break
  fi
  sleep 1
done
readonly SECOND_SERVER_PID="$(docker exec "${SKY_CONTAINER}" cat /home/sky/.sky/s03-server.pid)"
[[ "${SECOND_SERVER_PID}" != "${FIRST_SERVER_PID}" ]]

for _ in $(seq 1 180); do
  if [[ "$(request_status --header "Authorization: Bearer ${SERVICE_TOKEN}" \
    "http://127.0.0.1:${PROXY_PORT}/api/health" || true)" == "200" ]]; then
    break
  fi
  sleep 1
done
expect_status 200 \
  --header "Authorization: Bearer ${SERVICE_TOKEN}" \
  "http://127.0.0.1:${PROXY_PORT}/api/health"
assert_health_response
printf 'server_process_replaced=before:%s after:%s\n' \
  "${FIRST_SERVER_PID}" "${SECOND_SERVER_PID}"

expect_status 200 \
  --header "Authorization: Bearer ${SERVICE_TOKEN}" \
  "http://127.0.0.1:${PROXY_PORT}/users"
assert_test_user_response
printf 'result=PASS exact-digest startup, authenticated client, and PostgreSQL restart persistence\n'
