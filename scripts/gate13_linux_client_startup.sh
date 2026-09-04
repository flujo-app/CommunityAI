#!/usr/bin/env bash
set -euo pipefail
umask 077

metadata_root=http://metadata.google.internal/computeMetadata/v1/instance/attributes
metadata() {
  curl -fsS -H 'Metadata-Flavor: Google' "$metadata_root/$1"
}

export DEBIAN_FRONTEND=noninteractive
apt_deadline=$(( $(date +%s) + 300 ))
run_apt() {
  local phase="$1"
  shift
  local remaining=$(( apt_deadline - $(date +%s) ))
  if (( remaining <= 0 )); then
    echo "Gate 13 APT ${phase} exceeded the five-minute startup bound" >&2
    exit 1
  fi
  if ! timeout --signal=TERM --kill-after=15s "${remaining}s" apt-get \
    -o Acquire::Retries=3 \
    -o Acquire::http::Timeout=30 \
    -o Acquire::https::Timeout=30 \
    -o DPkg::Lock::Timeout=60 \
    "$@"; then
    echo "Gate 13 APT ${phase} failed within the five-minute startup bound" >&2
    exit 1
  fi
}
run_apt update update -qq
run_apt install install -y -qq \
  python3 xvfb xauth x11-utils xdotool imagemagick dbus-x11 \
  gnome-keyring libsecret-tools libsecret-1-0 libdbus-1-3 \
  libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 libxcb-shape0 \
  libxkbcommon0 libxkbcommon-x11-0 libegl1 libgl1 libpulse0 libfontconfig1 \
  unzip curl

if ! id gate13 >/dev/null 2>&1; then
  useradd --create-home --home-dir /home/gate13 --shell /bin/bash gate13
fi

run_root=/qualification
download_root=/var/tmp/gate13-download
install -d -m 0700 "$download_root"
install -d -o gate13 -g gate13 -m 0700 \
  "$run_root" "$run_root/package" "$run_root/install" "$run_root/runtime"

package_url="$(metadata package-url)"
package_sha256="$(metadata package-sha256)"
package_bytes="$(metadata package-bytes)"
wrapper="$download_root/artifact.zip"
curl -fL --retry 4 --retry-delay 5 "$package_url" -o "$wrapper"
unzip -q "$wrapper" -d "$download_root/artifact"
archive="$download_root/artifact/communityai-desktop-linux.tar.gz"
test "$(stat -c %s "$archive")" = "$package_bytes"
test "$(sha256sum "$archive" | cut -d' ' -f1)" = "$package_sha256"
mv "$archive" "$run_root/package/communityai-desktop-linux.tar.gz"
tar -xzf "$run_root/package/communityai-desktop-linux.tar.gz" -C "$run_root/install"
rm -rf "$wrapper" "$download_root/artifact"
chown -R gate13:gate13 "$run_root"

systemd-run --quiet --collect --service-type=exec \
  --unit=communityai-gate13-display \
  --property=User=gate13 \
  --property=Group=gate13 \
  --property=Restart=no \
  --property=KillMode=control-group \
  --property=UMask=0077 \
  --property=RuntimeMaxSec=21600 \
  /usr/bin/Xvfb :99 -screen 0 1280x900x24 -nolisten tcp
for _ in $(seq 1 30); do
  if sudo -u gate13 env DISPLAY=:99 xdpyinfo >/dev/null 2>&1; then
    touch /var/lib/gate13-bootstrap-ready
    exit 0
  fi
  sleep 1
done
echo "Gate 13 X display did not become ready" >&2
exit 1
