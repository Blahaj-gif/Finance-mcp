# Finance MCP Server 1-Click Installer
$ErrorActionPreference = "Stop"

Write-Host "===================================================================" -ForegroundColor Cyan
Write-Host "   Finance MCP - Market Data, Macro & Filings Installer" -ForegroundColor Cyan
Write-Host "===================================================================" -ForegroundColor Cyan
Write-Host ""

# 1. Check for UV Package Manager
Write-Host "[1/3] Checking UV Python Package Manager..." -ForegroundColor Yellow
$uvPath = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvPath) {
    Write-Host "  -> Installing uv package manager..." -ForegroundColor Gray
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
} else {
    Write-Host "  -> UV is already installed." -ForegroundColor Green
}

# 2. Inject into Claude Desktop Config
Write-Host "[2/3] Configuring Claude Desktop MCP Integration..." -ForegroundColor Yellow
$claudeConfigDir = "$env:APPDATA\Claude"
$claudeConfigFile = "$claudeConfigDir\claude_desktop_config.json"

if (-not (Test-Path $claudeConfigDir)) {
    New-Item -ItemType Directory -Path $claudeConfigDir -Force | Out-Null
}

$config = @{ mcpServers = @{} }
if (Test-Path $claudeConfigFile) {
    try {
        $raw = Get-Content $claudeConfigFile -Raw
        if ($raw.Trim()) {
            $config = $raw | ConvertFrom-Json
        }
    } catch {
        Write-Host "  -> Warning: Could not parse existing Claude config. Creating new structure." -ForegroundColor Yellow
    }
}

if (-not $config.mcpServers) {
    $config | Add-Member -MemberType NoteProperty -Name "mcpServers" -Value @{} -Force
}

$scriptDir = $PSScriptRoot -replace '\\','/'
$financeConfig = [ordered]@{
    command = "uv"
    args = @(
        "run",
        "--with", "pandas",
        "--with", "numpy",
        "--with", "fastmcp",
        "--with", "yfinance",
        "--with", "tabulate",
        "--with", "lxml",
        "--with", "html5lib",
        "--with", "webull-openapi-python-sdk",
        "$scriptDir/finance_mcp.py"
    )
}

# Remove the pre-rename key so the old entry does not linger alongside the new one.
if ($config.mcpServers.PSObject.Properties.Name -contains "webull") {
    $config.mcpServers.PSObject.Properties.Remove("webull")
    Write-Host "  -> Removed legacy 'webull' server entry." -ForegroundColor DarkGray
}
$config.mcpServers | Add-Member -MemberType NoteProperty -Name "finance" -Value $financeConfig -Force
$config | ConvertTo-Json -Depth 100 | Set-Content $claudeConfigFile -Encoding UTF8
Write-Host "  -> Successfully configured Claude Desktop config: $claudeConfigFile" -ForegroundColor Green

# 2.5 Generate .env template if missing
$envFile = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path $envFile)) {
    Write-Host "[2.5] Creating default .env configuration file..." -ForegroundColor Yellow
    @"
# Webull OpenAPI Credentials
WEBULL_APP_KEY=YOUR_WEBULL_APP_KEY_HERE
WEBULL_APP_SECRET=YOUR_WEBULL_APP_SECRET_HERE
WEBULL_REGION_ID=th
WEBULL_ENVIRONMENT=prod
# Pin this if your login has more than one account; the server refuses to guess.
# WEBULL_ACCOUNT_ID=

# --- Public data sources ---
# SEC EDGAR has no API key, but its fair-access policy requires a real contact
# address. The filings tools will not send requests without this.
SEC_USER_AGENT=Your Name (you@example.com)

# Optional. BLS allows 25 queries/day with no key; a free key at
# https://data.bls.gov/registrationEngine/ raises it to 500/day.
BLS_API_KEY=
"@ | Set-Content $envFile -Encoding UTF8
    Write-Host "  -> Created $envFile template. Add your Webull keys and SEC_USER_AGENT contact address here." -ForegroundColor Green
} else {
    Write-Host "[2.5] .env configuration file already exists. Skipping." -ForegroundColor Green
}

# 3. Create Single Desktop Shortcut for Unified Product
Write-Host "[3/3] Creating Desktop Shortcut for Unified Product..." -ForegroundColor Yellow
$desktopDir = [System.Environment]::GetFolderPath([System.Environment+SpecialFolder]::Desktop)
$wsh = New-Object -ComObject WScript.Shell

$sc = $wsh.CreateShortcut("$desktopDir\MCP Dashboard.lnk")
$sc.TargetPath = "powershell.exe"
$sc.Arguments = "-NoExit -Command `"uv run --with pandas --with numpy --with plotly --with streamlit --with yfinance --with lxml --with html5lib --with webull-openapi-python-sdk streamlit run '$PSScriptRoot\dashboard\app.py'`""
$sc.IconLocation = "cmd.exe,0"
$sc.Save()

Write-Host "  -> Created 'MCP Dashboard' shortcut on Desktop." -ForegroundColor Green

# Success summary
Write-Host ""
Write-Host "===================================================================" -ForegroundColor Green
Write-Host " 🎉 INSTALLATION SUCCESSFUL!" -ForegroundColor Green
Write-Host "===================================================================" -ForegroundColor Green
Write-Host " 1. Restart your Claude Desktop app to load all 35 MCP tools." -ForegroundColor White
Write-Host " 2. Double-click 'MCP Dashboard' on Desktop." -ForegroundColor White
Write-Host "    (Includes interactive charts, forecasts, backtester & background alerts)." -ForegroundColor Gray
Write-Host " ☕ Support Open Source: https://github.com/sponsors" -ForegroundColor Cyan
Write-Host "===================================================================" -ForegroundColor Green
