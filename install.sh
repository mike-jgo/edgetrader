#!/usr/bin/env bash
# install.sh — install EdgeTrader CLI
# Works regardless of where you invoke it from.
set -euo pipefail

# Resolve the directory containing this script, following symlinks.
# This is the canonical bash idiom — POSIX-portable and handles spaces.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
SCRIPT_PATH="$SCRIPT_DIR/edgetrader.py"

if [[ ! -f "$SCRIPT_PATH" ]]; then
    echo "✗ edgetrader.py not found in $SCRIPT_DIR"
    echo "  Place install.sh next to edgetrader.py and run it again."
    exit 1
fi

echo "⬡ Installing EdgeTrader CLI..."
echo "  Source: $SCRIPT_PATH"

# Check Python 3
if ! command -v python3 &> /dev/null; then
    echo "✗ Python 3 not found. Install Python 3.8+ first."
    exit 1
fi

# Install Python deps (--user avoids needing sudo)
echo "→ Installing dependencies (requests, rich)..."
python3 -m pip install --user --quiet requests rich

# Make the script executable
chmod +x "$SCRIPT_PATH"

# Determine install location
INSTALL_DIR="${HOME}/.local/bin"
mkdir -p "$INSTALL_DIR"
TARGET="$INSTALL_DIR/edgetrader"

# Create symlink (force overwrite if it exists)
ln -sf "$SCRIPT_PATH" "$TARGET"

echo "✓ Installed to $TARGET"
echo ""

# Check if INSTALL_DIR is on PATH
if [[ ":$PATH:" != *":$INSTALL_DIR:"* ]]; then
    echo "⚠ $INSTALL_DIR is not on your PATH. Add it with:"
    echo ""
    echo "  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
    echo "  source ~/.bashrc"
    echo ""
fi

echo "Then run: edgetrader configure"
