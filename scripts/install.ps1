# DRIFT-LLM one-line installer for Windows (PowerShell).
#
#   powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1
#
# Detects your accelerator, provisions a checksum-verified portable Go toolchain
# when needed, builds the patched hivemind wheel (PyPI ships none for Windows),
# installs a matching PyTorch build into a local .venv, and installs DRIFT-LLM
# (the `drift` package). Override the accelerator with
# $env:DRIFT_DEVICE = 'cpu' | 'cuda' | 'xpu' before running.
#
# Requires uv (https://docs.astral.sh/uv/). Linux/macOS users: use
# scripts/install.sh instead.
$ErrorActionPreference = 'Stop'

$RepoUrl   = if ($env:DRIFT_REPO_URL) { $env:DRIFT_REPO_URL } else { 'https://github.com/flujo-app/CommunityAI' }
$Device    = if ($env:DRIFT_DEVICE)   { $env:DRIFT_DEVICE }   else { 'auto' }
$TorchSpec = 'torch>=2.6,<2.7'
$GoVersion = '1.27.0'
$GoArchiveSha256 = 'f0c0a0d33ba94f4d2c5dbc887334ce678b21813504ddb3aafcb06e60a5a667c4'

function Log($msg) { Write-Host "[drift] $msg" -ForegroundColor Cyan }
function Die($msg) { Write-Host "[drift] error: $msg" -ForegroundColor Red; exit 1 }
function Has($cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }

function Enable-PortableGo {
    if (Has go) {
        Log "using $(go version)"
        return
    }

    $toolRoot = Join-Path (Get-Location) '.tools'
    $goRoot = Join-Path $toolRoot 'go'
    $goExe = Join-Path $goRoot 'bin\go.exe'
    $archive = Join-Path $toolRoot "go$GoVersion.windows-amd64.zip"

    if (-not (Test-Path -LiteralPath $goExe)) {
        New-Item -ItemType Directory -Path $toolRoot -Force | Out-Null
        Log "Go was not found; downloading portable Go $GoVersion from go.dev"
        Invoke-WebRequest -Uri "https://go.dev/dl/go$GoVersion.windows-amd64.zip" -OutFile $archive

        $actualHash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $GoArchiveSha256) {
            Remove-Item -LiteralPath $archive -Force
            Die "portable Go checksum mismatch (expected $GoArchiveSha256, got $actualHash)"
        }
        Expand-Archive -LiteralPath $archive -DestinationPath $toolRoot -Force
    }

    $env:Path = (Join-Path $goRoot 'bin') + ';' + $env:Path
    if (-not (Has go)) { Die 'portable Go was extracted but go.exe is not available' }
    Log "using portable $(go version)"
}

# 1. Get the code: reuse the current checkout if we're in one, otherwise clone.
if ((Test-Path pyproject.toml) -and (Select-String -Path pyproject.toml -Pattern '^name = "drift"' -Quiet)) {
    Log "using the checkout in $(Get-Location)"
} else {
    if (-not (Has git)) { Die 'git is required to fetch the code' }
    Log "cloning $RepoUrl"
    git clone --depth 1 $RepoUrl drift
    Set-Location drift
}

# 2. Prerequisites.
if (-not (Has uv)) { Die 'uv is required (https://docs.astral.sh/uv/). Install it and re-run.' }
Enable-PortableGo

# 3. Detect the accelerator (best effort; set $env:DRIFT_DEVICE = 'xpu' for Intel Arc).
if ($Device -eq 'auto') {
    $Device = if (Has nvidia-smi) { 'cuda' } else { 'cpu' }
}
Log "target device: $Device"

# 4. Create the environment.
Log 'creating .venv with uv'
uv venv --python 3.12

# 5. Build and install the patched hivemind wheel (must precede `pip install -e .`,
#    which does not pull hivemind on Windows).
Log 'building the patched hivemind wheel (needs Go on PATH)'
uv run python scripts/build_hivemind_windows.py --out-dir dist
$wheel = Get-ChildItem .\dist\hivemind-1.1.12-*-win_amd64.whl | Select-Object -Last 1
if (-not $wheel) { Die 'hivemind wheel build produced no artifact in .\dist' }
uv pip install $wheel.FullName

# 6. Install a PyTorch build for the chosen device.
Log "installing PyTorch ($Device)"
switch ($Device) {
    'cpu'  { uv pip install $TorchSpec }  # default Windows wheels are CPU-only
    'cuda' { uv pip install --index-url https://download.pytorch.org/whl/cu124 $TorchSpec }
    'xpu'  { uv pip install --index-url https://download.pytorch.org/whl/xpu 'torch==2.6.0+xpu' }
    default { Die "unknown DRIFT_DEVICE=$Device (expected cpu|cuda|xpu)" }
}

# 7. Install DRIFT-LLM itself.
Log 'installing DRIFT-LLM with the OpenAI-compatible API runtime'
uv pip install -e '.[api]'

Log 'done.'
Write-Host "`nStart a swarm on this machine:`n`n    drift up meta-llama/Llama-3.1-8B-Instruct`n" -ForegroundColor Green
Write-Host 'It prints a "drift up ... --join drift://..." command; run that on your other machines to add their compute.'
