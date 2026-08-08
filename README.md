# 🚀 Replicant Quant MCP

An **institutional-grade, Human-In-The-Loop (HITL) AI Quantitative Trading System** built natively for Claude Desktop. 

Replicant Quant bridges the gap between massive LLM reasoning capabilities (Claude 3.5 Sonnet) and physical market execution (Webull OpenAPI). It provides Claude with 25 deep market intelligence tools while maintaining a strict, localized safety firewall via a Streamlit Dashboard.

---

## ✨ Features
* **🧠 Comprehensive AI Market Brain:** Endpoints for live OHLCV data, Technical Indicators, Short Interest, Unusual Options Activity, Live News, and Analyst Consensus.
* **⚡ Master Payload Endpoint:** The `get_comprehensive_profile` tool fetches technicals, fundamentals, news, and consensus in a single round-trip, drastically cutting LLM latency.
* **🛡️ Human-In-The-Loop Execution Desk:** Claude **cannot** execute trades autonomously. All AI trade suggestions are written to a localized Draft JSON, which you must manually approve via the visual dashboard.
* **📉 Naked Short-Selling Firewall:** Deep mathematical safeguards. The system physically inspects your Webull account inventory to block naked shorts and verifies your Buying Power before allowing the AI to even draft a limit order.
* **📊 Streamlit Visual Analytics:** A beautiful, responsive dashboard featuring Plotly interactive charts, quantitative backtesting, live portfolio analytics, and adaptive signal breakdown.
* **🚨 Background Alert Daemon:** Native Windows balloon notifications trigger when background volatility or price alerts are met.

---

## 📥 1-Click Installation
You do **not** need to install Python, SDKs, or manually configure Claude. The installer dynamically downloads and wires everything for you.

1. Unzip this package anywhere on your computer (e.g. your Desktop).
2. Double-click the **`install.bat`** file.
3. The automated installer will:
   - Install the `uv` python engine globally if missing.
   - Inject the MCP server configuration dynamically into your `claude_desktop_config.json`.
   - Drop an **"MCP Dashboard"** shortcut on your Desktop.
   - Generate a clean `.env` template in the folder for your API keys.

---

## 🔑 Authentication
1. Open the newly generated `.env` file located in this folder.
2. Paste your Webull `WEBULL_APP_KEY` and `WEBULL_APP_SECRET`.
3. Save the file.

*(Note: Never commit your `.env` file to version control. Keep your keys safe!)*

---

## 🧠 Usage Architecture

### 1. The Brain (Claude Desktop)
Restart your Claude Desktop application. Ask Claude to analyze a ticker (e.g., *"Run a comprehensive scan on NVDA and draft a trade if the MACD is crossing over"*). Claude will dynamically ingest the live Webull data and reason through the logic.

### 2. The Command Center (Streamlit)
Double-click the **MCP Dashboard** shortcut on your desktop. This is your visual interface.
- **Charts:** Review the AI's technical analysis overlays visually.
- **Portfolio:** Check your live P&L and Net Liquidation.
- **Execution Desk:** Review Claude's drafted trades and click **"Execute"** to send them to Webull.

---
*Disclaimer: This is an open-source project for educational and experimental quantitative research. Algorithmic trading carries significant financial risk.*
