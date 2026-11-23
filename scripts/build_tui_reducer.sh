#!/usr/bin/env bash
# Rebuild the howlplane tui artifacts (the ADR 0009 reducer and the ADR 0010
# terminal_app), their packaged copies and provenance.
# HOWLFRAME_BIN must be a howlframe built from the CI runtime pin recorded in
# integrations/howlframe/tui/ARTIFACT_PROVENANCE.json (producer_source_revision).
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
hf="${HOWLFRAME_BIN:?set HOWLFRAME_BIN to the pinned howlframe build}"
dir="$root/integrations/howlframe/tui"
mkdir -p "$root/src/howlplane/control_plane/data/integrations/howlframe/tui"
for app in tui_reducer tui_app; do
  "$hf" build "$dir/$app.howl" -o "$dir/$app.hfbc"
  cp "$dir/$app.hfbc" "$root/src/howlplane/control_plane/data/integrations/howlframe/tui/$app.hfbc"
done
python3 - "$dir" <<'PY'
import hashlib, json, pathlib, sys
d = pathlib.Path(sys.argv[1]); p = d / "ARTIFACT_PROVENANCE.json"
doc = json.loads(p.read_text())
doc["files"] = {f"integrations/howlframe/tui/{n}": hashlib.sha256((d / n).read_bytes()).hexdigest()
                for n in ("tui_core.howl", "tui_reducer.howl", "tui_reducer.hfbc", "tui_app.howl", "tui_app.hfbc")}
p.write_text(json.dumps(doc, indent=2) + "\n")
PY
