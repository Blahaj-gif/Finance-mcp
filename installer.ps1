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

# 2. Register the server with every MCP client on this machine
Write-Host "[2/5] Registering with MCP clients..." -ForegroundColor Yellow

# Nothing about this server is Claude-specific. It speaks MCP over stdio, so any
# client that speaks MCP can run it -- Claude Desktop, Claude Code, Cursor,
# Windsurf, VS Code, Codex. Only this registration step ever knew about Claude,
# which is why it is the only thing that had to change.
#
# Config files are only touched where the client is already installed. Writing a
# config for an app someone does not have leaves litter they will never find.

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

# name -> config path. Each entry keeps an mcpServers object at the top level.
$targets = [ordered]@{
    "Claude Desktop" = "$env:APPDATA\Claude\claude_desktop_config.json"
    "Cursor"         = "$env:USERPROFILE\.cursor\mcp.json"
    "Windsurf"       = "$env:USERPROFILE\.codeium\windsurf\mcp_config.json"
}

function Register-McpServer {
    param([string]$Name, [string]$Path, $ServerConfig)

    $dir = Split-Path -Parent $Path
    if (-not (Test-Path $dir)) {
        Write-Host "  -> $Name not installed; skipped." -ForegroundColor DarkGray
        return $false
    }

    $config = [pscustomobject]@{ mcpServers = [pscustomobject]@{} }
    if (Test-Path $Path) {
        try {
            $raw = Get-Content $Path -Raw
            if ($raw.Trim()) { $config = $raw | ConvertFrom-Json }
        } catch {
            # Refuse rather than overwrite. Someone's other MCP servers live in
            # this file, and replacing it because we could not parse it would
            # silently remove them.
            Write-Host "  -> $Name config exists but could not be parsed; left untouched." -ForegroundColor Yellow
            Write-Host "     Add the server by hand, or fix the JSON and re-run." -ForegroundColor DarkGray
            return $false
        }
    }

    if (-not $config.mcpServers) {
        $config | Add-Member -MemberType NoteProperty -Name "mcpServers" -Value ([pscustomobject]@{}) -Force
    }
    # The pre-rename key, so the old entry does not linger beside the new one.
    if ($config.mcpServers.PSObject.Properties.Name -contains "webull") {
        $config.mcpServers.PSObject.Properties.Remove("webull")
    }
    $config.mcpServers | Add-Member -MemberType NoteProperty -Name "finance" -Value $ServerConfig -Force

    $backup = "$Path.bak"
    if (Test-Path $Path) { Copy-Item $Path $backup -Force }
    $config | ConvertTo-Json -Depth 100 | Set-Content $Path -Encoding UTF8
    Write-Host "  -> $Name configured: $Path" -ForegroundColor Green
    if (Test-Path $backup) { Write-Host "     (previous config saved as $backup)" -ForegroundColor DarkGray }
    return $true
}

$registered = 0
foreach ($name in $targets.Keys) {
    if (Register-McpServer -Name $name -Path $targets[$name] -ServerConfig $financeConfig) {
        $registered++
    }
}

# Claude Code keeps its own registry and owns the format, so ask it rather than
# writing the file ourselves.
if (Get-Command claude -ErrorAction SilentlyContinue) {
    try {
        $argList = ($financeConfig.args | ForEach-Object { $_ }) -join ' '
        claude mcp add finance -- uv $argList 2>&1 | Out-Null
        Write-Host "  -> Claude Code configured via 'claude mcp add'." -ForegroundColor Green
        $registered++
    } catch {
        Write-Host "  -> Claude Code found but registration failed; run 'claude mcp add' by hand." -ForegroundColor Yellow
    }
} else {
    Write-Host "  -> Claude Code CLI not found; skipped." -ForegroundColor DarkGray
}

if ($registered -eq 0) {
    Write-Host "  -> No MCP client detected. The server still works: point any" -ForegroundColor Yellow
    Write-Host "     MCP client at $scriptDir/finance_mcp.py (see README)." -ForegroundColor Yellow
}

# 2.5 Generate .env template if missing
$envFile = Join-Path $PSScriptRoot ".env"
if (-not (Test-Path $envFile)) {
    Write-Host "[3/5] Creating default .env configuration file..." -ForegroundColor Yellow
    @"
# Webull OpenAPI Credentials
WEBULL_APP_KEY=YOUR_WEBULL_APP_KEY_HERE
WEBULL_APP_SECRET=YOUR_WEBULL_APP_SECRET_HERE
WEBULL_REGION_ID=th

# prod = your real account. This is the default on purpose: the point of the
# tool is your actual portfolio and actual quotes, and reads are safe -- no
# order can be sent without you approving it in the dashboard.
#
# paper does NOT mean "live data, simulated orders". It repoints the entire
# client at Webull's sandbox, quotes included, and the sandbox is a separate
# deployment with its own app registry -- so production keys return 401 there
# and nothing works at all. Use paper only with sandbox credentials, to
# rehearse the approval flow.
WEBULL_ENVIRONMENT=prod

# Webull's sandbox is a SEPARATE deployment with its own app registry, so a
# production key authenticates against it as 401. Register a sandbox app and put
# its pair here; paper mode uses these when set.
# WEBULL_PAPER_APP_KEY=
# WEBULL_PAPER_APP_SECRET=

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
$ready = $keySet -and $secSet
if ($ready) {
    Write-Host " 1. Restart your MCP client to load $toolCount tools." -ForegroundColor White
    Write-Host " 2. Open the dashboard and work through the one-time briefing." -ForegroundColor White
} else {
    Write-Host " 1. Fill in the values above in:" -ForegroundColor White
    Write-Host "      $envFile" -ForegroundColor White
    Write-Host " 2. Restart your MCP client to load $toolCount tools." -ForegroundColor White
    Write-Host " 3. Open the dashboard and work through the one-time briefing." -ForegroundColor White
}
Write-Host ""
Write-Host " The assistant drafts orders. Nothing is ever submitted without you" -ForegroundColor White
Write-Host " approving it in the dashboard's Execution tab." -ForegroundColor White
Write-Host "===================================================================" -ForegroundColor Green
Write-Host ""

# Offer to open the dashboard -- but only once it can actually work. Launching
# it before the keys are in would open a window full of errors, which reads as
# "the install failed" rather than "you have one step left". Offered, never
# automatic: an installer that opens windows on its own is a nuisance when you
# are installing on someone else's behalf or re-running it.
if ($ready) {
    $answer = Read-Host " Open the dashboard now? [y/N]"
    if ($answer -match '^(y|yes)$') {
        Start-Process -FilePath "$desktopDir\Finance MCP Dashboard.lnk"
        Write-Host " -> Starting. It takes a few seconds to build the first chart." -ForegroundColor Green
    }
} else {
    Write-Host " Not opening the dashboard yet -- it needs the values above first." -ForegroundColor DarkGray
    Write-Host " Re-run this installer once .env is filled in and it will offer to." -ForegroundColor DarkGray
}
