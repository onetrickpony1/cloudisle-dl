param([switch]$Local)
$ErrorActionPreference = 'Stop'
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    $uvPath = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (-not (Test-Path $uvPath)) { Invoke-Expression (Invoke-RestMethod https://astral.sh/uv/install.ps1) }
} else { $uvPath = $uv.Source }
if ($Local) {
    & $uvPath run --no-project --python 3.12 python "$PSScriptRoot/scripts/install_update.py" --uv $uvPath --local $PSScriptRoot
    if ($LASTEXITCODE -ne 0) { throw 'Installation failed' }
    exit
}
$env:PATH = "$env:USERPROFILE\.local\bin;" + $env:PATH
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    & $uvPath run --no-project --python 3.12 python "$PSScriptRoot/scripts/bootstrap_gh.py"
    if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI installation failed' }
}
gh auth status 2>$null
if ($LASTEXITCODE -ne 0) { gh auth login --web --git-protocol https; if ($LASTEXITCODE -ne 0) { throw 'GitHub login required' } }
$tmp = Join-Path ([IO.Path]::GetTempPath()) ('cloudisle-dl-install-' + [Guid]::NewGuid())
New-Item -ItemType Directory $tmp | Out-Null
try {
    $encoded = gh api repos/onetrickpony1/cloudisle-dl/contents/scripts/install_update.py --jq .content
    if ($LASTEXITCODE -ne 0) { throw 'Cannot access private repository' }
    $helper = Join-Path $tmp 'install_update.py'
    [IO.File]::WriteAllBytes($helper, [Convert]::FromBase64String(($encoded -join "`n")))
    & $uvPath run --no-project --python 3.12 python $helper --uv $uvPath
    if ($LASTEXITCODE -ne 0) { throw 'Installation failed' }
} finally { Remove-Item -Recurse -Force $tmp }
