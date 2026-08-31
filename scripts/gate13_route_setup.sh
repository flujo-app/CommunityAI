#!/usr/bin/env bash
set -euo pipefail
umask 077
root=/tmp/gate13-route
wheel="$root/drift-2.3.0.dev2-py3-none-any.whl"
test "$(stat -c %s "$wheel")" = "389107"
test "$(sha256sum "$wheel" | cut -d' ' -f1)" = "2a4f30bad7ae897fed019bc7da330a09965adb35685d11abaeaebf7a1d40aa60"
test "$(sha256sum "$root/configure_product_route_node.py" | cut -d' ' -f1)" = "fc385f74e02ca955203b1fc5e8ae493c7f4ccd31bd7383c2ae0a1c461c91363e"
test "$(sha256sum "$root/gate11_product_node_acceptance.py" | cut -d' ' -f1)" = "bdcc9f499a7cd6b727c0e33a0c4c2b0e71e76e28f3f21cb99804a8f39edfa0d2"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv python3-pip curl
if ! id communityai >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /srv/communityai --shell /usr/sbin/nologin communityai
fi
install -d -m 0755 /opt/communityai
if [ ! -x /opt/communityai/venv/bin/drift ]; then
  python3 -m venv /opt/communityai/venv
  /opt/communityai/venv/bin/pip install --no-cache-dir "$wheel[api]"
fi
chmod -R a+rX /opt/communityai/venv
install -d -o communityai -g communityai -m 0700 /srv/communityai/qwen /srv/communityai/gemma /srv/communityai/cache
install -d -o root -g root -m 0755 /opt/communityai/bootstrap
cp -a "$root/catalog-v1/." /opt/communityai/bootstrap/
public_ip="$(curl -fsS -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip)"
test -n "$public_ip"
for role in qwen gemma; do
  data="/srv/communityai/$role"
  sudo -u communityai /opt/communityai/venv/bin/drift bootstrap /opt/communityai/bootstrap/catalog-bootstrap.json --data_dir "$data" --node_config "$data/node-config.json" >/dev/null
done
/opt/communityai/venv/bin/python "$root/configure_product_route_node.py" --config /srv/communityai/qwen/node-config.json --role primary --public-ip "$public_ip" --cache-root /srv/communityai/cache >/dev/null
/opt/communityai/venv/bin/python "$root/configure_product_route_node.py" --config /srv/communityai/gemma/node-config.json --role standby --public-ip "$public_ip" --cache-root /srv/communityai/cache >/dev/null
chown -R communityai:communityai /srv/communityai
cat >/etc/systemd/system/communityai-qwen.service <<'UNIT'
[Unit]
Description=CommunityAI Qwen public route
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=communityai
Group=communityai
WorkingDirectory=/srv/communityai/qwen
ExecStart=/opt/communityai/venv/bin/drift node --config /srv/communityai/qwen/node-config.json --data_dir /srv/communityai/qwen --host 127.0.0.1 --port 8081
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
LimitCORE=0
[Install]
WantedBy=multi-user.target
UNIT
cat >/etc/systemd/system/communityai-gemma.service <<'UNIT'
[Unit]
Description=CommunityAI Gemma public route
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=communityai
Group=communityai
WorkingDirectory=/srv/communityai/gemma
ExecStart=/opt/communityai/venv/bin/drift node --config /srv/communityai/gemma/node-config.json --data_dir /srv/communityai/gemma --host 127.0.0.1 --port 8082
Restart=on-failure
RestartSec=5
TimeoutStopSec=30
LimitCORE=0
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now communityai-qwen.service communityai-gemma.service
rm -rf "$root"
printf 'route-setup=started\n'
