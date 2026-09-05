#!/usr/bin/env bash
set -euo pipefail
umask 077

metadata_root=http://metadata.google.internal/computeMetadata/v1/instance/attributes
metadata() {
  curl -fsS -H 'Metadata-Flavor: Google' "$metadata_root/$1"
}

export DEBIAN_FRONTEND=noninteractive
bootstrap_status=/var/lib/gate13-bootstrap-status
printf '%s\n' starting >"$bootstrap_status"
trap 'rc=$?; if (( rc != 0 )); then printf "%s\\n" failed >"$bootstrap_status"; fi' EXIT

# The image's HTTP security mirror is unreachable from this GCP network.
sed -i 's|http://security\.ubuntu\.com/ubuntu|https://security.ubuntu.com/ubuntu|g' \
  /etc/apt/sources.list.d/ubuntu.sources

apt_deadline=$(( $(date +%s) + 300 ))
apt_options=(
  -o APT::Update::Error-Mode=any
  -o Acquire::ForceIPv4=true
  -o Acquire::Retries=1
  -o Acquire::http::Timeout=20
  -o Acquire::https::Timeout=20
  -o DPkg::Lock::Timeout=30
)

apt_updated=false
for attempt in 1 2 3; do
  remaining=$(( apt_deadline - $(date +%s) ))
  if (( remaining <= 0 )); then
    break
  fi
  attempt_timeout=$(( remaining / (4 - attempt) ))
  if (( attempt_timeout > 90 )); then
    attempt_timeout=90
  fi
  echo "Gate 13 APT update attempt ${attempt}/3 (${attempt_timeout}s maximum)"
  if timeout --signal=TERM --kill-after=10s "${attempt_timeout}s" \
    apt-get "${apt_options[@]}" update; then
    apt_updated=true
    break
  fi
  echo "Gate 13 APT update attempt ${attempt}/3 failed" >&2
  rm -rf -- /var/lib/apt/lists/partial
  install -d -m 0755 /var/lib/apt/lists/partial
done
if [[ "$apt_updated" != true ]]; then
  echo "Gate 13 APT update failed after three attempts" >&2
  exit 1
fi

remaining=$(( apt_deadline - $(date +%s) ))
if (( remaining <= 0 )); then
  echo "Gate 13 APT install had no time remaining" >&2
  exit 1
fi
echo "Gate 13 APT install (${remaining}s maximum)"
timeout --signal=TERM --kill-after=10s "${remaining}s" \
  apt-get "${apt_options[@]}" install -y \
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
    printf '%s\n' ready >"$bootstrap_status"
    touch /var/lib/gate13-bootstrap-ready
    trap - EXIT
    exit 0
  fi
  sleep 1
done
echo "Gate 13 X display did not become ready" >&2
exit 1
