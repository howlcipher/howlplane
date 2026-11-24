#!/usr/bin/env bash
# Rebuild the howlplane tui terminal_app artifact (ADR 0010), its packaged copy
# and provenance, with HowlFrame alone: `howlframe build`, then the HowlFrame
# program scripts/tui_provenance.howl (read and write grants on these two
# directories only).
# HOWLFRAME_BIN must be a howlframe built from the CI runtime pin recorded in
# integrations/howlframe/tui/ARTIFACT_PROVENANCE.json (producer_source_revision).
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
hf="${HOWLFRAME_BIN:?set HOWLFRAME_BIN to the pinned howlframe build}"
tui="$root/integrations/howlframe/tui"
pkg="$root/src/howlplane/control_plane/data/integrations/howlframe/tui"
"$hf" build "$tui/tui_app.howl" -o "$tui/tui_app.hfbc"
"$hf" run --allow-caps "filesystem:read=$tui,filesystem:write=$tui,filesystem:write=$pkg" \
  "$root/scripts/tui_provenance.howl" "$tui" "$pkg"
