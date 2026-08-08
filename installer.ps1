# Finance MCP Server 1-Click Installer
$ErrorActionPreference = "Stop"

Write-Host "===================================================================" -ForegroundColor Cyan
Write-Host "   Finance MCP - Market Data, Macro & Filings Installer" -ForegroundColor Cyan
Write-Host "===================================================================" -ForegroundColor Cyan
Write-Host ""

# 1. Check for UV Package Manager
Write-Host "[1/5] Checking UV Python Package Manager..." -ForegroundColor Yellow
$uvPath = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvPath) {
    Write-Host "  -> Installing uv package manager..." -ForegroundColor Gray
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
} else {
    Write-Host "  -> UV is already installed." -ForegroundColor Green
}

# 2. Inject into Claude Desktop Config
Write-Host "[2/5] Configuring Claude Desktop MCP Integration..." -ForegroundColor Yellow
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
    Write-Host "[3/5] Creating default .env configuration file..." -ForegroundColor Yellow
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
    Write-Host "[3/5] .env configuration file already exists. Skipping." -ForegroundColor Green
}

# 3. Create Desktop Shortcut for the dashboard
Write-Host "[4/5] Creating Desktop Shortcut..." -ForegroundColor Yellow
$desktopDir = [System.Environment]::GetFolderPath([System.Environment+SpecialFolder]::Desktop)
$wsh = New-Object -ComObject WScript.Shell

# Run from the repo root, not the dashboard folder: app.py resolves its sibling
# modules and .streamlit/config.toml relative to the working directory, and
# launching from elsewhere loses the theme.
$dashArgs = "-NoExit -Command `"Set-Location '$PSScriptRoot'; uv run " +
            "--with pandas --with numpy --with plotly --with streamlit " +
            "--with yfinance --with lxml --with html5lib --with tabulate " +
            "--with webull-openapi-python-sdk " +
            "streamlit run '$PSScriptRoot\dashboard\app.py'`""

$sc = $wsh.CreateShortcut("$desktopDir\Finance MCP Dashboard.lnk")
$sc.TargetPath = "powershell.exe"
$sc.Arguments = $dashArgs
$sc.WorkingDirectory = $PSScriptRoot
$sc.IconLocation = "cmd.exe,0"
$sc.Save()

# The pre-rename shortcut would otherwise sit alongside the new one, pointing
# at the same app under a stale name.
$oldShortcut = "$desktopDir\MCP Dashboard.lnk"
if (Test-Path $oldShortcut) {
    Remove-Item $oldShortcut -Force
    Write-Host "  -> Removed the old 'MCP Dashboard' shortcut." -ForegroundColor DarkGray
}
Write-Host "  -> Created 'Finance MCP Dashboard' shortcut on Desktop." -ForegroundColor Green

# 4. Verify the install actually works before claiming success
Write-Host "[5/5] Checking configuration..." -ForegroundColor Yellow
$toolCount = (Select-String -Path "$PSScriptRoot\finance_mcp.py" -Pattern '^@mcp\.tool\(\)').Count
if ($toolCount -lt 1) {
    Write-Host "  -> WARNING: could not read the tool list from finance_mcp.py." -ForegroundColor Yellow
    $toolCount = "the"
} else {
    Write-Host "  -> $toolCount tools found in finance_mcp.py." -ForegroundColor Green
}

$secLine = Select-String -Path $envFile -Pattern '^SEC_USER_AGENT=(.+)$' | Select-Object -First 1
$secSet = $secLine -and ($secLine.Matches[0].Groups[1].Value -notmatch 'you@example\.com')
$keyLine = Select-String -Path $envFile -Pattern '^WEBULL_APP_KEY=(.+)$' | Select-Object -First 1
$keySet = $keyLine -and ($keyLine.Matches[0].Groups[1].Value -ne 'YOUR_WEBULL_APP_KEY_HERE')

# Success summary
Write-Host ""
Write-Host "===================================================================" -ForegroundColor Green
Write-Host " INSTALLATION COMPLETE" -ForegroundColor Green
Write-Host "===================================================================" -ForegroundColor Green
Write-Host ""
if (-not $keySet) {
    Write-Host " ACTION NEEDED - add your Webull keys to:" -ForegroundColor Yellow
    Write-Host "   $envFile" -ForegroundColor White
    Write-Host "   Without them the price feed falls back to Yahoo and no account" -ForegroundColor Gray
    Write-Host "   or order tools will work." -ForegroundColor Gray
    Write-Host ""
}
if (-not $secSet) {
    Write-Host " ACTION NEEDED - set SEC_USER_AGENT to a real contact address in:" -ForegroundColor Yellow
    Write-Host "   $envFile" -ForegroundColor White
    Write-Host "   The SEC's fair-access policy requires it; the EDGAR tools refuse" -ForegroundColor Gray
    Write-Host "   to send requests without one rather than risk an IP ban." -ForegroundColor Gray
    Write-Host ""
}
Write-Host " 1. Restart Claude Desktop to load $toolCount MCP tools." -ForegroundColor White
Write-Host " 2. Double-click 'Finance MCP Dashboard' on the Desktop for charts," -ForegroundColor White
Write-Host "    the backtester, the portfolio view and the order approval desk." -ForegroundColor White
Write-Host ""
Write-Host " Claude drafts orders. Nothing is ever submitted without you" -ForegroundColor White
Write-Host " approving it in the dashboard's Execution tab." -ForegroundColor White
Write-Host "===================================================================" -ForegroundColor Green
