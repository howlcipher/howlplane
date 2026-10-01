#!/usr/bin/env bash
set -euo pipefail

# This script links the local skills and rules to the global config
# directories for Gemini (AGY), Claude Code, and Codex, so the library
# is available in every project you work on regardless of agent.

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(dirname "$SCRIPT_DIR")

echo "Installing Python dependencies..."
if command -v pip3 &> /dev/null; then
  PIP_BIN=pip3
elif command -v pip &> /dev/null; then
  PIP_BIN=pip
else
  echo "Error: pip not found."
  echo "Please download and install Python and pip from https://www.python.org/downloads/ and try again."
  exit 1
fi

if ! "$PIP_BIN" install "$REPO_ROOT"; then
  echo "Error: '$PIP_BIN install' failed (see pip's error above)."
  echo "Hint: if this is an externally-managed or system Python, install inside a virtualenv"
  echo "  (python3 -m venv .venv && . .venv/bin/activate) or use pipx, then re-run this script."
  exit 1
fi

MARKER_START="<!-- ai_knowledge_library:start -->"
MARKER_END="<!-- ai_knowledge_library:end -->"

# refresh_block FILE MODE: refresh the managed marker block in FILE in place.
# MODE is "ref" (single @import line) or "inline" (full AGENTS.md contents).
# Aborts without modifying FILE if MARKER_END is missing after MARKER_START.
refresh_block() {
  local file="$1" mode="$2" tmp
  if ! awk -v start="$MARKER_START" -v end="$MARKER_END" '
    $0 == start {s=1; next}
    s && $0 == end {e=1}
    END {exit !(s && e)}
  ' "$file"; then
    echo "Error: $file has '$MARKER_START' without a matching '$MARKER_END'; refusing to modify it." >&2
    echo "Fix the markers manually and re-run." >&2
    exit 1
  fi
  tmp=$(mktemp)
  awk -v start="$MARKER_START" -v end="$MARKER_END" -v repo="$REPO_ROOT" -v mode="$mode" '
    $0 == start {
      print
      if (mode == "ref") {
        print "@" repo "/AGENTS.md"
      } else {
        rules = repo "/AGENTS.md"
        while ((getline line < rules) > 0) print line
        close(rules)
      }
      skip=1; next
    }
    $0 == end {skip=0}
    !skip {print}
  ' "$file" > "$tmp"
  # cat rather than mv so symlinks and file mode are preserved
  cat "$tmp" > "$file"
  rm -f "$tmp"
}

# --- Global Control Plane Configuration & Launcher ---
mkdir -p "$HOME/.config/howlplane"
HP_CONFIG="$HOME/.config/howlplane/config.toml"
if [ -f "$HP_CONFIG" ] && grep -qxF "path = \"$REPO_ROOT\"" "$HP_CONFIG"; then
  echo "Keeping existing $HP_CONFIG (already points at $REPO_ROOT)"
else
  cat << EOF > "$HP_CONFIG"
# HowlPlane Engineering Control Plane Configuration
[control_plane]
path = "$REPO_ROOT"
EOF
fi

# Legacy compatibility fallback path
mkdir -p "$HOME/.config/ai-control-plane"
cat << EOF > "$HOME/.config/ai-control-plane/config.toml"
# HowlPlane (legacy fallback path)
[control_plane]
path = "$REPO_ROOT"
EOF

mkdir -p "$HOME/.local/bin"
if [ -f "$REPO_ROOT/bin/ai" ]; then
  chmod +x "$REPO_ROOT/bin/ai"
  ln -sf "$REPO_ROOT/bin/ai" "$HOME/.local/bin/ai"
  echo "Installed 'ai' global launcher to $HOME/.local/bin/ai"
fi

if [ -f "$REPO_ROOT/bin/howlplane" ]; then
  chmod +x "$REPO_ROOT/bin/howlplane"
  ln -sf "$REPO_ROOT/bin/howlplane" "$HOME/.local/bin/howlplane"
  echo "Installed 'howlplane' global launcher to $HOME/.local/bin/howlplane"
fi

# --- Gemini / Antigravity (AGY) integration ---
AGY_DIR="$HOME/.gemini/antigravity-cli"

mkdir -p "$AGY_DIR/skills"
mkdir -p "$AGY_DIR/rules"

echo "Linking skills to global AGY configuration"
for skill in "$REPO_ROOT/.agents/skills"/*; do
  if [ -d "$skill" ]; then
    ln -sfn "$skill" "$AGY_DIR/skills/$(basename "$skill")"
  fi
done

echo "Linking rules to global AGY configuration"
for rule in "$REPO_ROOT/.agents/rules"/*; do
  if [ -f "$rule" ]; then
    ln -sf "$rule" "$AGY_DIR/rules/"
  fi
done

echo "Linking command skills to global AGY configuration"
for skill in "$REPO_ROOT/.agents/skill_commands"/*; do
  if [ -d "$skill" ]; then
    ln -sfn "$skill" "$AGY_DIR/skills/$(basename "$skill")"
  fi
done

# --- Claude Code integration ---
CLAUDE_DIR="$HOME/.claude"

mkdir -p "$CLAUDE_DIR/skills"

echo "Linking skills to global Claude Code configuration"
for skill in "$REPO_ROOT/.agents/skills"/*; do
  if [ -d "$skill" ]; then
    ln -sfn "$skill" "$CLAUDE_DIR/skills/$(basename "$skill")"
  fi
done

echo "Linking command skills (/work_next_item, /resume_task, /groom_backlogs) to global Claude Code configuration"
for skill in "$REPO_ROOT/.agents/skill_commands"/*; do
  if [ -d "$skill" ]; then
    ln -sfn "$skill" "$CLAUDE_DIR/skills/$(basename "$skill")"
  fi
done

echo "Registering library rulebook in global Claude memory"
CLAUDE_MEMORY="$CLAUDE_DIR/CLAUDE.md"

if [ -f "$CLAUDE_MEMORY" ] && grep -qF "$MARKER_START" "$CLAUDE_MEMORY"; then
  refresh_block "$CLAUDE_MEMORY" ref
else
  {
    echo ""
    echo "$MARKER_START"
    echo "@$REPO_ROOT/AGENTS.md"
    echo "$MARKER_END"
  } >> "$CLAUDE_MEMORY"
fi

# --- Codex integration ---
# Codex loads user skills from ~/.agents/skills and global guidance from
# $CODEX_HOME/AGENTS.md. CODEX_HOME defaults to ~/.codex.
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
CODEX_SKILLS_DIR="$HOME/.agents/skills"
CODEX_AGENTS="$CODEX_DIR/AGENTS.md"

mkdir -p "$CODEX_DIR"
mkdir -p "$CODEX_SKILLS_DIR"

echo "Linking skills to global Codex configuration"
for skill in "$REPO_ROOT/.agents/skills"/*; do
  if [ -d "$skill" ]; then
    target="$CODEX_SKILLS_DIR/$(basename "$skill")"
    if [ -e "$target" ] && [ ! -L "$target" ]; then
      echo "Warning: preserving existing Codex skill directory $target"
    else
      ln -sfn "$skill" "$target"
    fi
  fi
done

echo "Linking command workflows to global Codex configuration"
for skill in "$REPO_ROOT/.agents/skill_commands"/*; do
  if [ -d "$skill" ]; then
    target="$CODEX_SKILLS_DIR/$(basename "$skill")"
    if [ -e "$target" ] && [ ! -L "$target" ]; then
      echo "Warning: preserving existing Codex skill directory $target"
    else
      ln -sfn "$skill" "$target"
    fi
  fi
done

echo "Registering library rulebook in global Codex guidance"
if [ -f "$CODEX_AGENTS" ] && grep -qF "$MARKER_START" "$CODEX_AGENTS"; then
  refresh_block "$CODEX_AGENTS" inline
else
  {
    echo ""
    echo "$MARKER_START"
    cat "$REPO_ROOT/AGENTS.md"
    echo "$MARKER_END"
  } >> "$CODEX_AGENTS"
fi

if [ -s "$CODEX_DIR/AGENTS.override.md" ]; then
  echo "Warning: $CODEX_DIR/AGENTS.override.md takes precedence over the installed Codex guidance."
fi

# --- Devin CLI integration ---
DEVIN_DIR="$HOME/.config/devin"

mkdir -p "$DEVIN_DIR/skills"

echo "Linking skills to global Devin CLI configuration"
for skill in "$REPO_ROOT/.agents/skills"/*; do
  if [ -d "$skill" ]; then
    ln -sfn "$skill" "$DEVIN_DIR/skills/$(basename "$skill")"
  fi
done

echo "Linking command skills (/work_next_item, /resume_task, /groom_backlogs) to global Devin CLI configuration"
for skill in "$REPO_ROOT/.agents/skill_commands"/*; do
  if [ -d "$skill" ]; then
    ln -sfn "$skill" "$DEVIN_DIR/skills/$(basename "$skill")"
  fi
done

echo "Registering library rulebook in global Devin CLI configuration"
DEVIN_AGENTS="$DEVIN_DIR/AGENTS.md"

if [ -f "$DEVIN_AGENTS" ] && grep -qF "$MARKER_START" "$DEVIN_AGENTS"; then
  refresh_block "$DEVIN_AGENTS" inline
else
  {
    echo ""
    echo "$MARKER_START"
    cat "$REPO_ROOT/AGENTS.md"
    echo "$MARKER_END"
  } >> "$DEVIN_AGENTS"
fi

echo "Integration complete. HowlPlane is now globally accessible to Gemini, Claude, Codex, and Devin."
