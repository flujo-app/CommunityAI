#!/usr/bin/env bash
# Source-bound fresh-VM bootstrap for the finite Gate 11 G2/L4 route host.
set -euo pipefail
umask 077

readonly installer_url='https://storage.googleapis.com/compute-gpu-installation-us/installer/latest/cuda_installer.pyz?generation=1785935286399764'
readonly installer_sha256='876d7d02e3e1166c105bb0a9148993c3ea9b789a041f78143c928e7ab317c14f'
readonly driver_version='570.211.01'
readonly ca_certificates_version='20260601~24.04.1'
readonly curl_version='8.5.0-2ubuntu10.13'
readonly gnupg_version='2.4.4-2ubuntu17.4'
readonly docker_version='29.1.3-0ubuntu3~24.04.2'
readonly containerd_version='2.2.1-0ubuntu1~24.04.3'
readonly toolkit_version='1.20.0-1'
readonly toolkit_key_url='https://nvidia.github.io/libnvidia-container/gpgkey'
readonly toolkit_key_sha256='c880576d6cf75a48e5027a871bac70fd0421ab07d2b55f30877b21f1c87959c9'
readonly toolkit_list_url='https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list'
readonly toolkit_list_sha256='bc6cb10d15243cfcf5c1506c46d7fed4037ce16f84a3f592482cb4ac874c528f'
readonly install_root='/var/lib/communityai-bootstrap'
readonly installer_path="${install_root}/cuda_installer.pyz"
readonly key_path="${install_root}/nvidia-container-toolkit.gpgkey"
readonly list_path="${install_root}/nvidia-container-toolkit.list"
readonly ready_path="${install_root}/runtime-ready.json"
readonly temporary_ready="${install_root}/.runtime-ready.$$.json"

if [[ "${EUID}" -ne 0 ]]; then
  printf '%s\n' 'public-route bootstrap requires root' >&2
  exit 2
fi

install -d -m 0700 "${install_root}"
rm -f "${ready_path}" "${temporary_ready}"
trap 'rm -f "${installer_path}" "${key_path}" "${list_path}" "${temporary_ready}"' EXIT

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install --yes --no-install-recommends \
  "ca-certificates=${ca_certificates_version}" \
  "curl=${curl_version}" \
  "gnupg=${gnupg_version}"

installed_driver=''
if command -v nvidia-smi >/dev/null 2>&1; then
  installed_driver="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null || true)"
fi
if [[ "${installed_driver}" != "${driver_version}" ]]; then
  curl --fail --location --silent --show-error --retry 5 --connect-timeout 20 --max-time 600 \
    "${installer_url}" --output "${installer_path}"
  printf '%s  %s\n' "${installer_sha256}" "${installer_path}" | sha256sum --check --strict
  python3 "${installer_path}" install_driver --force-version "${driver_version}"
fi
nvidia-smi >/dev/null
test "$(nvidia-smi --query-gpu=driver_version --format=csv,noheader)" = "${driver_version}"

apt-get install --yes --no-install-recommends \
  "containerd=${containerd_version}" \
  "docker.io=${docker_version}"
apt-mark hold containerd docker.io >/dev/null

curl --fail --location --silent --show-error --retry 5 --connect-timeout 20 --max-time 120 \
  "${toolkit_key_url}" --output "${key_path}"
printf '%s  %s\n' "${toolkit_key_sha256}" "${key_path}" | sha256sum --check --strict
gpg --batch --yes --dearmor --output /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg "${key_path}"

curl --fail --location --silent --show-error --retry 5 --connect-timeout 20 --max-time 120 \
  "${toolkit_list_url}" --output "${list_path}"
printf '%s  %s\n' "${toolkit_list_sha256}" "${list_path}" | sha256sum --check --strict
sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  "${list_path}" > /etc/apt/sources.list.d/nvidia-container-toolkit.list

apt-get update
apt-get install --yes --no-install-recommends \
  "libnvidia-container1=${toolkit_version}" \
  "libnvidia-container-tools=${toolkit_version}" \
  "nvidia-container-toolkit-base=${toolkit_version}" \
  "nvidia-container-toolkit=${toolkit_version}"
apt-mark hold \
  libnvidia-container1 \
  libnvidia-container-tools \
  nvidia-container-toolkit-base \
  nvidia-container-toolkit >/dev/null

nvidia-ctk runtime configure --runtime=docker --set-as-default
systemctl enable --now containerd docker
systemctl restart docker
systemctl is-active --quiet containerd
systemctl is-active --quiet docker

test "$(dpkg-query -W -f='${Version}' docker.io)" = "${docker_version}"
test "$(dpkg-query -W -f='${Version}' containerd)" = "${containerd_version}"
test "$(dpkg-query -W -f='${Version}' nvidia-container-toolkit)" = "${toolkit_version}"
docker version --format '{{.Server.Version}}' >/dev/null
docker info --format '{{json .Runtimes}}' | grep -q '"nvidia"'
test "$(docker info --format '{{.DefaultRuntime}}')" = 'nvidia'

printf '%s\n' \
  '{"container_runtime":"docker","containerd_version":"2.2.1-0ubuntu1~24.04.3","docker_version":"29.1.3-0ubuntu3~24.04.2","gpu_driver_version":"570.211.01","nvidia_container_toolkit_version":"1.20.0-1","ready":true,"schema_version":1,"scope":"communityai-public-route-bootstrap"}' \
  > "${temporary_ready}"
chmod 0600 "${temporary_ready}"
mv -f "${temporary_ready}" "${ready_path}"
