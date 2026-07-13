#!/bin/bash
# install.sh — remote installer for llm-provider-manager
# Usage:  curl -fsSL <raw-url>/install.sh | bash
# Idempotent: re-run to update code + re-check setup.
set -euo pipefail

REPO_URL="https://gitee.com/raverstern/llm-provider-manager.git"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/share/llm-provider-manager}"
BRANCH="master"
BIN_DIR="$HOME/.local/bin"

# ── prerequisite checks ───────────────────────────────────────────
command -v git    >/dev/null 2>&1 || { echo "error: git not found";    exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "error: python3 not found"; exit 1; }

# Save the caller's original PATH before we modify it
_ORIG_PATH="$PATH"
# Make ~/.local/bin visible to subsequent steps in THIS shell
export PATH="$BIN_DIR:$PATH"

# ── detect shell rc file ──────────────────────────────────────────
detect_rc_file() {
    local shell_name
    shell_name="$(basename "${SHELL:-bash}")"
    case "$shell_name" in
        zsh)  echo "$HOME/.zshrc" ;;
        bash) echo "$HOME/.bashrc" ;;
        *)    echo "$HOME/.bashrc" ;;  # fallback — hook syntax is bash-compatible
    esac
}
RC_FILE="$(detect_rc_file)"

# ── 1. clone / update ─────────────────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "Updating existing clone at $INSTALL_DIR ..."
    git -C "$INSTALL_DIR" fetch --depth 1 origin "$BRANCH"
    git -C "$INSTALL_DIR" reset --hard FETCH_HEAD
else
    echo "Cloning to $INSTALL_DIR ..."
    git clone --depth 1 -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

# ── 2. symlink wrapper → ~/.local/bin/lpm ─────────────────────────
mkdir -p "$BIN_DIR"
ln -sf "$INSTALL_DIR/llm-provider-manager" "$BIN_DIR/lpm"
echo "✓ linked lpm → $BIN_DIR/lpm"

# ── 3. ensure ~/.local/bin is on PATH ─────────────────────────────
case ":$_ORIG_PATH:" in
    *":$BIN_DIR:"*)
        echo "✓ ~/.local/bin already on PATH"
        ;;
    *)
        touch "$RC_FILE"
        {
            echo ""
            echo "# ~/.local/bin on PATH (added by lpm install)"
            echo 'export PATH="$HOME/.local/bin:$PATH"'
        } >> "$RC_FILE"
        echo "✓ added ~/.local/bin to PATH in $RC_FILE"
        ;;
esac

# ── 4. install shell hook (defines lpm() function) ────────────────
lpm init-shell-hook --rc "$RC_FILE"

# ── 5. next steps ─────────────────────────────────────────────────
cat <<EOF

Done! Next steps:

  1. Create ~/.config/llm-provider-manager/providers.jsonc
     (template: $INSTALL_DIR/providers.example.jsonc)

  2. Generate agent static configs:
     lpm agent --template all

  3. Activate the shell hook (defines lpm):
     source $RC_FILE

  4. Switch LLM at any time:
     lpm use <provider>                   # switch to provider / defaultKey
     lpm use <provider> <key>             # specific key
     lpm use                              # back to config default
     lpm use --agent opencode <provider>  # for opencode instead of claude
EOF
