#!/usr/bin/env bash
# Provision only the pinned Linux amd64 artifact producer into a caller-owned dir.
set -euo pipefail
if [[ "$#" != 1 || -z "$1" ]]; then
  echo 'usage: fetch_frame_artifact_compiler.sh DESTINATION' >&2
  exit 1
fi
mkdir -p "$1"
cd "$1"
asset=howlframe_v0.1.0_linux_amd64.tar.gz
base=https://github.com/howlcipher/howlframe/releases/download/v0.1.0
curl -fsSLo "$asset" "$base/$asset"
curl -fsSLo SHA256SUMS "$base/SHA256SUMS"
printf '%s  %s\n' d44006c23cb9c152c815b5acc8762a53f06a93777877b86bfea57d173cc9d907 "$asset" | sha256sum --check -
sha256sum --check --ignore-missing SHA256SUMS
tar -xzf "$asset" howlframe
printf '%s  %s\n' 3396379ffdbe7823f47e9fd161d11239e886b3b656757203d3063f54a87b882c howlframe | sha256sum --check -
