#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

READY_ROOT=/var/lib/communityai-cache
READY_FILE="${READY_ROOT}/cache-ready.json"
READY_TEMP="${READY_FILE}.tmp"
METADATA=http://metadata.google.internal/computeMetadata/v1/instance/attributes
SERVICE_METADATA=http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default
REGISTRY_HOST=us-central1-docker.pkg.dev
REGISTRY_PREFIX=${REGISTRY_HOST}/community-ai-506321/communityai-ghcr-cache/flujo-app
REGISTRY_CONFIG=/run/communityai-cache-registry

access_token=''
token_payload=''
cleanup_registry() {
  access_token=''
  token_payload=''
  if [[ -d "${REGISTRY_CONFIG}" ]] && command -v docker >/dev/null 2>&1; then
    docker --config "${REGISTRY_CONFIG}" logout "${REGISTRY_HOST}" >/dev/null 2>&1 || true
  fi
  rm -rf -- "${REGISTRY_CONFIG}"
}

write_acknowledgement() {
  local payload=$1
  mkdir -p "${READY_ROOT}"
  rm -f "${READY_TEMP}"
  printf '%s\n' "${payload}" >"${READY_TEMP}"
  chmod 0600 "${READY_TEMP}"
  mv -f -- "${READY_TEMP}" "${READY_FILE}"
}

fail_closed() {
  local status=$?
  trap - EXIT
  cleanup_registry
  if [[ ${status} -ne 0 ]]; then
    write_acknowledgement \
      '{"failure_code":"cache_bootstrap_failed","registry_credentials_removed":true,"result":"failed","scope":"communityai-public-route-cache-bootstrap","schema_version":1}'
  fi
  exit "${status}"
}
trap fail_closed EXIT

mkdir -p "${READY_ROOT}"
rm -f "${READY_FILE}" "${READY_TEMP}"
rm -rf -- "${REGISTRY_CONFIG}"

metadata() {
  curl --fail --silent --show-error --max-time 30 \
    --header 'Metadata-Flavor: Google' "${METADATA}/$1"
}

service_metadata() {
  curl --fail --silent --show-error --max-time 30 \
    --header 'Metadata-Flavor: Google' "${SERVICE_METADATA}/$1"
}

primary_image="$(metadata primary-image)"
standby_image="$(metadata standby-image)"
primary_pattern="^${REGISTRY_PREFIX}/communityai-public-route-qwen3\\.5-2b@sha256:[0-9a-f]{64}$"
standby_pattern="^${REGISTRY_PREFIX}/communityai-public-route-gemma-4-e2b@sha256:[0-9a-f]{64}$"
[[ "${primary_image}" =~ ${primary_pattern} ]]
[[ "${standby_image}" =~ ${standby_pattern} ]]

service_account="$(service_metadata email)"
[[ "${service_account}" =~ ^ca-[0-9a-f]{20}@community-ai-506321\.iam\.gserviceaccount\.com$ ]]

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install --yes --no-install-recommends ca-certificates docker.io python3
systemctl enable --now docker
docker info >/dev/null

token_payload="$(service_metadata token)"
access_token="$(
  python3 -c '
import json
import sys
value = json.load(sys.stdin)
token = value.get("access_token")
expires = value.get("expires_in")
kind = value.get("token_type")
if (
    not isinstance(token, str)
    or not 1 <= len(token) <= 4096
    or any(ord(character) < 0x21 or ord(character) > 0x7E for character in token)
    or kind != "Bearer"
    or isinstance(expires, bool)
    or not isinstance(expires, int)
    or not 60 <= expires <= 3600
):
    raise SystemExit(2)
sys.stdout.write(token)
' <<<"${token_payload}"
)"
token_payload=''
install -d -m 0700 "${REGISTRY_CONFIG}"
printf '%s\n' "${access_token}" |
  docker --config "${REGISTRY_CONFIG}" login "${REGISTRY_HOST}" \
    --username oauth2accesstoken --password-stdin >/dev/null
access_token=''

pull_one() {
  timeout 14400 docker --config "${REGISTRY_CONFIG}" pull --quiet "$1" >/dev/null
}

pull_one "${primary_image}" &
primary_pid=$!
pull_one "${standby_image}" &
standby_pid=$!
primary_status=0
standby_status=0
wait "${primary_pid}" || primary_status=$?
wait "${standby_pid}" || standby_status=$?
[[ ${primary_status} -eq 0 && ${standby_status} -eq 0 ]]

cleanup_registry
trap - EXIT
[[ ! -e "${REGISTRY_CONFIG}" ]]

write_acknowledgement \
  '{"images_prefetched":2,"registry_credentials_removed":true,"result":"passed","scope":"communityai-public-route-cache-bootstrap","schema_version":1}'
