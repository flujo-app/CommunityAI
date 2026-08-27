# Run-scoped GCE qualification bootstrap for a Windows G2/L4 host.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$InstallerUrl = 'https://raw.githubusercontent.com/GoogleCloudPlatform/compute-gpu-installation/e4d32d90993a17795b9f6bc411d2ae6d767052ca/windows/install_gpu_driver.ps1'
$InstallerSha256 = '9d3eb7064a19aaf8e043c6eb863a490054105f0c7f8f121cdab76b100a092897'
$InstallerPath = Join-Path ([System.IO.Path]::GetTempPath()) 'communityai_install_gpu_driver.ps1'

if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    & nvidia-smi | Out-Null
    if ($LASTEXITCODE -eq 0) { exit 0 }
}

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$PreviousProgressPreference = $ProgressPreference
try {
    $ProgressPreference = 'SilentlyContinue'
    Invoke-WebRequest -UseBasicParsing -Uri $InstallerUrl -OutFile $InstallerPath
    $ActualSha256 = (Get-FileHash -LiteralPath $InstallerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($ActualSha256 -ne $InstallerSha256) {
        throw "GPU bootstrap checksum mismatch: expected $InstallerSha256, got $ActualSha256"
    }
    & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $InstallerPath
    if ($LASTEXITCODE -ne 0) { throw "GPU bootstrap exited with code $LASTEXITCODE" }
}
finally {
    $ProgressPreference = $PreviousProgressPreference
    Remove-Item -LiteralPath $InstallerPath -Force -ErrorAction SilentlyContinue
}
