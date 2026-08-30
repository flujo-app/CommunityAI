#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

READY_ROOT=/var/lib/communityai-cache
READY_FILE="${READY_ROOT}/cache-ready.json"
METADATA=http://metadata.google.internal/computeMetadata/v1/instance/attributes
REGISTRY_PREFIX=us-central1-docker.pkg.dev/community-ai-506321/communityai-ghcr-cache/flujo-app

mkdir -p "${READY_ROOT}"
rm -f "${READY_FILE}"

metadata() {
  curl --fail --silent --show-error --max-time 30     --header 'Metadata-Flavor: Google' "${METADATA}/$1"
}

primary_image="$(metadata primary-image)"
standby_image="$(metadata standby-image)"
primary_pattern="^${REGISTRY_PREFIX}/communityai-public-route-qwen3\.5-2b@sha256:[0-9a-f]{64}$"
standby_pattern="^${REGISTRY_PREFIX}/communityai-public-route-gemma-4-e2b@sha256:[0-9a-f]{64}$"
[[ "${primary_image}" =~ ${primary_pattern} ]]
[[ "${standby_image}" =~ ${standby_pattern} ]]

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install --yes --no-install-recommends ca-certificates docker.io
systemctl enable --now docker
docker info >/dev/null

pull_one() {
  timeout 14400 docker pull --quiet "$1" >/dev/null
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

printf '{"images_prefetched":2,"result":"passed","scope":"communityai-public-route-cache-bootstrap","schema_version":1}\n'   >"${READY_FILE}"
chmod 0600 "${READY_FILE}"
