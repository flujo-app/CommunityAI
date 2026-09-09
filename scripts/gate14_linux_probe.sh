#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 6 ]]; then
  echo "usage: gate14_linux_probe.sh PYTHON FACTS CHALLENGE PACKAGE RELEASE_METADATA OUTPUT" >&2
  exit 2
fi

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
exec "$1" "$script_dir/gate14_host_probe.py" \
  --platform linux \
  --facts "$2" \
  --challenge "$3" \
  --package "$4" \
  --release-metadata "$5" \
  --output "$6"
