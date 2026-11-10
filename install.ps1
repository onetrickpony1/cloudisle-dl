# cloudisle-dl installer for Windows. No admin rights needed.
#
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/onetrickpony1/cloudisle-dl/main/install.ps1 | iex"
#
# What it does:
#   1. installs uv (https://docs.astral.sh/uv/) if missing: it manages Python for cloudisle-dl
#   2. installs cloudisle-dl in its own isolated environment, with its own Python if needed
#   3. puts cloudisle-dl on your PATH
# No browser is required. Existing workers and data are not changed.
# Run it again to update. Uninstall: uv tool uninstall cloudisle-dl

# "Continue": Windows PowerShell turns harmless messages from programs into errors under "Stop";
# every step checks $LASTEXITCODE instead
$ErrorActionPreference = "Continue"
$Source = if ($env:CLOUDISLE_DL_SOURCE) { $env:CLOUDISLE_DL_SOURCE } else { "cloudisle-dl @ https://github.com/onetrickpony1/cloudisle-dl/archive/refs/heads/main.zip" }
$Python = if ($env:CLOUDISLE_DL_PYTHON) { $env:CLOUDISLE_DL_PYTHON } else { "3.12" }

function Say($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
# throw, not exit: when run with "irm ... | iex", exit would close the user's PowerShell window
function Fail($msg) { Write-Host "`ncloudisle-dl installer: $msg" -ForegroundColor Red; throw "cloudisle-dl was not installed" }

Say "Installing cloudisle-dl"

# 1. uv
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($uv) { $UV = $uv.Source }
else {
    $cand = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (-not (Test-Path $cand)) {
        Say "Installing uv (Python manager, from astral.sh)"
        try { Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression }
        catch { Fail "could not download uv: $($_.Exception.Message)" }
    }
    if (Test-Path $cand) { $UV = $cand } else { Fail "uv was installed but can't be found; open a new window and run this again" }
}

# 2. cloudisle-dl (+ Python if needed)
Say "Installing cloudisle-dl and its packages (Python $Python is downloaded if needed)"
& $UV tool install --force --refresh --python $Python $Source
if ($LASTEXITCODE -ne 0) { Fail "installing cloudisle-dl failed (see above)" }

# 3. PATH
try { & $UV tool update-shell 2>&1 | Out-Null } catch { }
$Bin = (& $UV tool dir --bin).Trim()
$CDL = Join-Path $Bin "cloudisle-dl.exe"
if (-not (Test-Path $CDL)) { Fail "cloudisle-dl was installed but $CDL is missing" }

Say ("Done: " + (& $CDL --version))
if (-not (($env:Path -split ";") -contains $Bin)) {
    Write-Host "Open a NEW terminal window so the commands are found (they are in $Bin)."
}
Write-Host ""
Write-Host "Configure:   cloudisle-dl init --destination PATH"
Write-Host "Background:  cloudisle-dl start"
Write-Host "Status:      cloudisle-dl status"
Write-Host "Stop/resume: cloudisle-dl stop / cloudisle-dl resume"
Write-Host "Local disk:  add --allow-local to init."
Write-Host "No browser or GitHub login is required. Installation does not start or restart downloads."
Write-Host "After updating, stop and resume a packaged worker to load the new version."
