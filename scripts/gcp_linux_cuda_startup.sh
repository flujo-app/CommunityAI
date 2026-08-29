#!/usr/bin/env bash
# Run-scoped GCE qualification bootstrap for a Linux G2/L4 host.
set -euo pipefail

readonly installer_url='https://storage.googleapis.com/compute-gpu-installation-us/installer/latest/cuda_installer.pyz?generation=1785935286399764'
readonly installer_sha256='876d7d02e3e1166c105bb0a9148993c3ea9b789a041f78143c928e7ab317c14f'
readonly install_root='/opt/communityai-gpu-bootstrap'
readonly installer_path="${install_root}/cuda_installer.pyz"

if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
  exit 0
fi

install -d -m 0700 "${install_root}"
trap 'rm -f "${installer_path}"' EXIT
curl --fail --location --silent --show-error --retry 5 --connect-timeout 20 --max-time 600 \
  "${installer_url}" --output "${installer_path}"
printf '%s  %s\n' "${installer_sha256}" "${installer_path}" | sha256sum --check --strict
python3 "${installer_path}" install_driver
