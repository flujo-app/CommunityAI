#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || ! $1 =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  echo "usage: install_bootstrap.sh PUBLIC_IPV4" >&2
  exit 2
fi

public_ip=$1
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install --yes --no-install-recommends ca-certificates python3-venv

if ! id communityai >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/communityai --create-home --shell /usr/sbin/nologin communityai
fi

if [[ ! -f /swapfile ]]; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
fi
if ! swapon --show=NAME --noheadings | grep -Fxq /swapfile; then
  swapon /swapfile
fi
if ! grep -Fq '/swapfile none swap sw 0 0' /etc/fstab; then
  printf '%s\n' '/swapfile none swap sw 0 0' >> /etc/fstab
fi

install -d -m 0755 /opt/communityai
install -d -o communityai -g communityai -m 0700 /var/lib/communityai
if [[ ! -x /opt/communityai/venv/bin/python ]]; then
  python3 -m venv /opt/communityai/venv
fi
/opt/communityai/venv/bin/python -m pip install --no-cache-dir --upgrade 'pip==26.2'
/opt/communityai/venv/bin/python -m pip install --no-cache-dir \
  --index-url https://download.pytorch.org/whl/cpu 'torch==2.6.0'
/opt/communityai/venv/bin/python -m pip install --no-cache-dir 'hivemind==1.1.12'

install -m 0755 "$source_dir/bootstrap_node.py" /opt/communityai/bootstrap_node.py
sed "s/__PUBLIC_IP__/${public_ip}/g" "$source_dir/communityai-bootstrap.service" \
  > /etc/systemd/system/communityai-bootstrap.service
chmod 0644 /etc/systemd/system/communityai-bootstrap.service

systemctl daemon-reload
systemctl enable --now communityai-bootstrap.service
sleep 5
systemctl is-active --quiet communityai-bootstrap.service
journalctl --unit communityai-bootstrap.service --no-pager --lines=30
