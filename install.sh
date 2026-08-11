#!/usr/bin/env bash
# Finance MCP — installer for Linux and macOS.
#
# The Windows path is install.bat -> installer.ps1. This is the same work in the
# shell: install uv, install the package, write a config template, and register
# the server with whatever MCP clients are on this machine.
set -euo pipefail

say()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32m->\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m->\033[0m %s\n' "$*"; }
skip() { printf '  \033[90m->\033[0m %s\n' "$*"; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say "==================================================================="
say "   Finance MCP — Market Data, Macro Calendar & SEC Filings"
say "==================================================================="
say ""

# --- 1. uv ------------------------------------------------------------------
say "[1/5] Checking uv..."
if command -v uv >/dev/null 2>&1; then
    ok "uv is already installed."
else
    ok "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v uv >/dev/null 2>&1; then
        warn "uv install failed; see the output above."
        exit 1
    fi
fi

# --- 2. Config --------------------------------------------------------------
# Unlike the Windows installer this does not assume the code stays put. An
# installed package cannot keep its .env in site-packages -- that directory is
# not somewhere anyone edits and it is wiped on upgrade -- so the per-user
# config directory is the durable home for it.
say "[2/5] Configuration..."
if [ "$(uname -s)" = "Darwin" ]; then
    CONFIG_DIR="$HOME/Library/Application Support/finance-mcp"
else
    CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/finance-mcp"
fi
mkdir -p "$CONFIG_DIR"
ENV_FILE="$CONFIG_DIR/.env"

if [ -f "$ENV_FILE" ]; then
    skip ".env already exists at $ENV_FILE — left alone."
else
    cat > "$ENV_FILE" <<'ENVTEMPLATE'
# Which broker the account and order tools use, and where prices come from:
# webull, saxo or ibkr. Only the tools your broker can serve are registered.
# Webull is the only adapter run against a live account; saxo and ibkr are
# written from published docs and say so in every result. See HELP-WANTED.md.
FINANCE_BROKER=webull

# Webull OpenAPI credentials. Without these, prices fall back to Yahoo and the
# account and order tools do not work; everything else still does.
WEBULL_APP_KEY=
WEBULL_APP_SECRET=
WEBULL_REGION_ID=us

# prod = your real account, and that is the point: reading it is what the tool
# is for, and no order is sent without you approving it in the dashboard.
#
# paper does NOT mean "live data, simulated orders". It repoints the entire
# client at Webull's sandbox, quotes included, and the sandbox has its own app
# registry -- production keys return 401 there and nothing works at all.
WEBULL_ENVIRONMENT=prod
# WEBULL_PAPER_APP_KEY=
# WEBULL_PAPER_APP_SECRET=

# Required for SEC filings. Their fair-access policy asks for a real contact
# address, and the tools refuse to send a request without one.
SEC_USER_AGENT=Your Name (you@example.com)

# Optional. 25 macro queries/day without a key; a free key at
# https://data.bls.gov/registrationEngine/ raises it to 500.
BLS_API_KEY=

# --- Saxo (only if FINANCE_BROKER=saxo). UNVERIFIED ADAPTER. --------------
# A 24-hour simulation token comes from the Developer Portal, no approval
# needed and no live money.
# SAXO_ENVIRONMENT=sim
# SAXO_ACCESS_TOKEN=

# --- IBKR (only if FINANCE_BROKER=ibkr). UNVERIFIED ADAPTER. --------------
# The Client Portal *Web* API, not the TWS socket API. Run IBKR's Client Portal
# Gateway, then log in at https://localhost:5000 in a browser. A paper account
# works and is the better choice; paper ids start DU. The gateway's certificate
# is self-signed, and accepting it unverified is your decision to make.
# IBKR_BASE_URL=https://localhost:5000/v1/api
# IBKR_ACCOUNT_ID=
# IBKR_TLS_INSECURE=
ENVTEMPLATE
    ok "Wrote $ENV_FILE"
fi

# --- 3. Install -------------------------------------------------------------
say "[3/5] Installing finance-mcp..."
if uv tool install --force --with streamlit --with plotly "$HERE" >/dev/null 2>&1; then
    ok "Installed as a uv tool."
elif uv pip install --system "$HERE[dashboard]" >/dev/null 2>&1; then
    ok "Installed into the system environment."
else
    warn "Install failed. Try it directly to see why:"
    warn "  uv pip install '$HERE[dashboard]'"
    exit 1
fi

# --- 4. MCP clients ---------------------------------------------------------
say "[4/5] Registering with MCP clients..."
python3 - "$HERE" <<'REGISTER_CLIENTS'
import json, os, shutil, sys

home = os.path.expanduser("~")
darwin = sys.platform == "darwin"
targets = {
    "Claude Desktop": (
        os.path.join(home, "Library/Application Support/Claude/claude_desktop_config.json")
        if darwin else os.path.join(home, ".config/Claude/claude_desktop_config.json")),
    "Cursor":   os.path.join(home, ".cursor/mcp.json"),
    "Windsurf": os.path.join(home, ".codeium/windsurf/mcp_config.json"),
}
server = {"command": "finance-mcp", "args": []}

registered = 0
for name, path in targets.items():
    if not os.path.isdir(os.path.dirname(path)):
        print("  \033[90m->\033[0m %s not installed; skipped." % name)
        continue
    config = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read().strip()
            config = json.loads(raw) if raw else {}
        except Exception:
            # Someone's other MCP servers live in this file. Refusing beats
            # replacing it because we could not read it.
            print("  \033[33m->\033[0m %s config could not be parsed; left untouched." % name)
            continue
        shutil.copyfile(path, path + ".bak")
    config.setdefault("mcpServers", {})
    config["mcpServers"].pop("webull", None)
    config["mcpServers"]["finance"] = server
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
    print("  \033[32m->\033[0m %s configured: %s" % (name, path))
    registered += 1

if shutil.which("claude"):
    if os.system("claude mcp add finance -- finance-mcp >/dev/null 2>&1") == 0:
        print("  \033[32m->\033[0m Claude Code configured via 'claude mcp add'.")
        registered += 1

if registered == 0:
    print("  \033[33m->\033[0m No MCP client found. The server still works — point any")
    print("     MCP client at the 'finance-mcp' command (see README).")
REGISTER_CLIENTS

# --- 5. Summary -------------------------------------------------------------
say "[5/5] Checking configuration..."
READY=1
grep -q '^WEBULL_APP_KEY=.\+' "$ENV_FILE" || READY=0
grep -q 'you@example.com' "$ENV_FILE" && READY=0

say ""
say "==================================================================="
say " Installation complete."
say "==================================================================="
say ""
if [ "$READY" -eq 0 ]; then
    warn "ACTION NEEDED — fill in WEBULL_APP_KEY, WEBULL_APP_SECRET and"
    warn "SEC_USER_AGENT in: $ENV_FILE"
    say ""
    say " 1. Fill in the values above."
    say " 2. Restart your MCP client."
    say " 3. Run: finance-mcp-dashboard"
else
    say " 1. Restart your MCP client."
    say " 2. Run: finance-mcp-dashboard"
fi
say ""
say " The assistant drafts orders. Nothing is submitted without you approving"
say " it in the dashboard's Execution tab."
say ""
say " Not sure where configuration is read from?  finance-mcp-config"
