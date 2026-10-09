$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$metadataHeaders = @{ "Metadata-Flavor" = "Google" }
$metadataRoot = "http://metadata.google.internal/computeMetadata/v1/instance/attributes"
$bootstrapRoot = "C:\Gate13Bootstrap"
$runRoot = "C:\Gate13Run"
$downloadRoot = "C:\Gate13Download"
New-Item -ItemType Directory -Force -Path $bootstrapRoot, $runRoot, $downloadRoot | Out-Null
function Set-Gate13KeyAcl {
    param([string]$Path, [string[]]$AllowedSids, [switch]$Directory)
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "SSH key path is a reparse point" }
    if ($Directory) { $acl = [Security.AccessControl.DirectorySecurity]::new() }
    else { $acl = [Security.AccessControl.FileSecurity]::new() }
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner([Security.Principal.SecurityIdentifier]::new($AllowedSids[0]))
    foreach ($sid in $AllowedSids) {
        $identity = [Security.Principal.SecurityIdentifier]::new($sid)
        if ($Directory) {
            $rule = [Security.AccessControl.FileSystemAccessRule]::new($identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        } else {
            $rule = [Security.AccessControl.FileSystemAccessRule]::new($identity, 'FullControl', 'Allow')
        }
        $acl.AddAccessRule($rule)
    }
    $item.SetAccessControl($acl)
    $actual = $item.GetAccessControl()
    $rules = @($actual.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
    if (!$actual.AreAccessRulesProtected -or $rules.Count -ne $AllowedSids.Count) { throw "SSH key ACL verification failed" }
    foreach ($rule in $rules) {
        if ($rule.IsInherited -or $rule.IdentityReference.Value -notin $AllowedSids -or $rule.AccessControlType -ne 'Allow' -or $rule.FileSystemRights -ne 'FullControl') { throw "SSH key ACL verification failed" }
    }
}

function Install-Gate13SshKey {
    $publicKey = (Invoke-RestMethod -Headers $metadataHeaders -Uri "$metadataRoot/gate13-ssh-public-key").Trim()
    if ($publicKey -notmatch '^ssh-(rsa|ed25519) [A-Za-z0-9+/]+={0,2}( [^\r\n]*)?$') { throw "Expected one exact SSH public key" }
    $ordinarySid = (Get-LocalUser -Name 'M').SID.Value
    $sshRoot = 'C:\Users\M\.ssh'
    $programDataSsh = Join-Path $env:ProgramData 'ssh'
    New-Item -ItemType Directory -Force -Path $sshRoot, $programDataSsh | Out-Null
    Set-Gate13KeyAcl -Path $sshRoot -AllowedSids @($ordinarySid, 'S-1-5-18') -Directory
    foreach ($name in @('authorized_keys')) {
        $path = Join-Path $sshRoot $name
        if (Test-Path -LiteralPath $path) { Set-Gate13KeyAcl -Path $path -AllowedSids @($ordinarySid, 'S-1-5-18') }
        [IO.File]::WriteAllText($path, $publicKey + "`n", [Text.UTF8Encoding]::new($false))
        Set-Gate13KeyAcl -Path $path -AllowedSids @($ordinarySid, 'S-1-5-18')
    }
    foreach ($name in @('administrators_authorized_keys', 'communityai_gate13_m_authorized_keys')) {
        $path = Join-Path $programDataSsh $name
        if (Test-Path -LiteralPath $path) { Set-Gate13KeyAcl -Path $path -AllowedSids @('S-1-5-32-544', 'S-1-5-18') }
        [IO.File]::WriteAllText($path, $publicKey + "`n", [Text.UTF8Encoding]::new($false))
        Set-Gate13KeyAcl -Path $path -AllowedSids @('S-1-5-32-544', 'S-1-5-18')
    }
}

$readyMarker = Join-Path $bootstrapRoot "ready.txt"
if (Test-Path -LiteralPath $readyMarker -PathType Leaf) {
    Set-Service -Name sshd -StartupType Automatic
    if ((Get-Service -Name sshd).Status -ne "Running") { Start-Service -Name sshd }
    $sshFirewall = Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue
    if ($null -eq $sshFirewall) {
        New-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -DisplayName "OpenSSH Server (sshd)" -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
    } else {
        Set-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -Enabled True -Profile Any
    }
    Install-Gate13SshKey
    return
}

$capability = Get-WindowsCapability -Online -Name "OpenSSH.Server~~~~0.0.1.0"
if ($capability.State -ne "Installed") {
    Add-WindowsCapability -Online -Name "OpenSSH.Server~~~~0.0.1.0" | Out-Null
}
Set-Service -Name sshd -StartupType Automatic
Start-Service -Name sshd
if (-not (Get-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -DisplayName "OpenSSH Server (sshd)" -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}
Set-NetFirewallRule -Name "OpenSSH-Server-In-TCP" -Enabled True -Profile Any

$randomBytes = New-Object byte[] 32
$randomGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
try {
    $randomGenerator.GetBytes($randomBytes)
} finally {
    $randomGenerator.Dispose()
}
$plainPassword = [Convert]::ToBase64String($randomBytes) + "aA1!"
$securePassword = ConvertTo-SecureString $plainPassword -AsPlainText -Force
if (-not (Get-LocalUser -Name "M" -ErrorAction SilentlyContinue)) {
    New-LocalUser -Name "M" -Password $securePassword -PasswordNeverExpires -UserMayNotChangePassword | Out-Null
} else {
    Set-LocalUser -Name "M" -Password $securePassword
}
if (-not (Get-LocalUser -Name "Gate13Admin" -ErrorAction SilentlyContinue)) {
    New-LocalUser -Name "Gate13Admin" -NoPassword -AccountNeverExpires -UserMayNotChangePassword | Out-Null
}
$administratorMemberNames = @(Get-LocalGroupMember -Group "Administrators" | ForEach-Object Name)
if ($administratorMemberNames -notcontains "$env:COMPUTERNAME\Gate13Admin") {
    Add-LocalGroupMember -Group "Administrators" -Member "Gate13Admin"
}
$openSshGroup = Get-LocalGroup -Name "OpenSSH Users"
$openSshMemberNames = @(Get-LocalGroupMember -Group $openSshGroup | ForEach-Object Name)
if ($openSshMemberNames -notcontains "$env:COMPUTERNAME\M") {
    Add-LocalGroupMember -Group $openSshGroup -Member "M"
}
Remove-LocalGroupMember -Group "Administrators" -Member "M" -ErrorAction SilentlyContinue

Install-Gate13SshKey
$programDataSsh = Join-Path $env:ProgramData 'ssh'
$sshdConfig = Join-Path $programDataSsh "sshd_config"
if (-not (Test-Path -LiteralPath $sshdConfig -PathType Leaf)) {
    Copy-Item -LiteralPath "$env:WINDIR\System32\OpenSSH\sshd_config_default" -Destination $sshdConfig
}
$marker = "# CommunityAI Gate13 ordinary user"
if (-not (Select-String -LiteralPath $sshdConfig -SimpleMatch $marker -Quiet)) {
    [IO.File]::AppendAllText(
        $sshdConfig,
        "`n$marker`nMatch User M`n    AuthorizedKeysFile __PROGRAMDATA__/ssh/communityai_gate13_m_authorized_keys`n",
        [Text.UTF8Encoding]::new($false)
    )
}
& "$env:WINDIR\System32\OpenSSH\sshd.exe" -t
if ($LASTEXITCODE -ne 0) { throw "OpenSSH configuration invalid" }
Restart-Service -Name sshd

$pythonRoot = "C:\Gate13Python"
if (-not (Test-Path -LiteralPath "$pythonRoot\python.exe" -PathType Leaf)) {
    $installer = Join-Path $downloadRoot "python-3.12.9-amd64.exe"
    & curl.exe -fL --retry 4 --retry-delay 5 "https://www.python.org/ftp/python/3.12.9/python-3.12.9-amd64.exe" -o $installer
    if ($LASTEXITCODE -ne 0) { throw "Python download failed" }
    if ((Get-Item -LiteralPath $installer).Length -ne 26923696) { throw "Python installer size changed" }
    if ((Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant() -cne "2a52993092a19cfdffe126e2eeac46a4265e25705614546604ad44988e040c0f") { throw "Python installer digest changed" }
    $process = Start-Process -FilePath $installer -ArgumentList "/quiet InstallAllUsers=1 TargetDir=$pythonRoot Include_pip=0 Include_test=0 PrependPath=0" -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "Python installation failed" }
    Remove-Item -LiteralPath $installer -Force
}

$packageUrl = (Invoke-RestMethod -Headers $metadataHeaders -Uri "$metadataRoot/package-url").Trim()
$packageSha256 = (Invoke-RestMethod -Headers $metadataHeaders -Uri "$metadataRoot/package-sha256").Trim().ToLowerInvariant()
$packageBytes = [int64](Invoke-RestMethod -Headers $metadataHeaders -Uri "$metadataRoot/package-bytes")
$wrapper = Join-Path $downloadRoot "artifact.zip"
& curl.exe -fL --retry 4 --retry-delay 5 $packageUrl -o $wrapper
if ($LASTEXITCODE -ne 0) { throw "Package wrapper download failed" }
$staging = Join-Path $downloadRoot "artifact"
New-Item -ItemType Directory -Force -Path $staging | Out-Null
& tar.exe -xf $wrapper -C $staging
if ($LASTEXITCODE -ne 0) { throw "Package wrapper extraction failed" }
$archive = Join-Path $staging "communityai-desktop-windows.zip"
if ((Get-Item -LiteralPath $archive).Length -ne $packageBytes) { throw "Package byte size changed" }
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $packageSha256) { throw "Package digest changed" }
$packageRoot = Join-Path $runRoot "package"
$installRoot = Join-Path $runRoot "install"
New-Item -ItemType Directory -Force -Path $packageRoot, $installRoot | Out-Null
Move-Item -LiteralPath $archive -Destination (Join-Path $packageRoot "communityai-desktop-windows.zip")
& tar.exe -xf (Join-Path $packageRoot "communityai-desktop-windows.zip") -C $installRoot
if ($LASTEXITCODE -ne 0) { throw "Product extraction failed" }
Remove-Item -LiteralPath $wrapper, $staging -Recurse -Force
& icacls.exe $runRoot /inheritance:r /grant:r "M:(OI)(CI)F" "Administrators:(OI)(CI)F" "SYSTEM:(OI)(CI)F" | Out-Null

$winlogon = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
Set-ItemProperty -Path $winlogon -Name AutoAdminLogon -Value "1" -Type String
Set-ItemProperty -Path $winlogon -Name DefaultUserName -Value "M" -Type String
Set-ItemProperty -Path $winlogon -Name DefaultDomainName -Value $env:COMPUTERNAME -Type String
Set-ItemProperty -Path $winlogon -Name DefaultPassword -Value $plainPassword -Type String
Set-ItemProperty -Path $winlogon -Name AutoLogonCount -Value 1 -Type DWord
$clearArgument = '-WindowStyle Hidden -Command "Start-Sleep -Seconds 60; $p = ''HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon''; Remove-ItemProperty -Path $p -Name DefaultPassword -ErrorAction SilentlyContinue; Set-ItemProperty -Path $p -Name AutoAdminLogon -Value ''0'' -Type String"'
$clearAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $clearArgument
$clearTrigger = New-ScheduledTaskTrigger -AtLogOn -User "M"
Register-ScheduledTask -TaskName "Gate13ClearAutoLogon" -Action $clearAction -Trigger $clearTrigger -User "SYSTEM" -RunLevel Highest -Force | Out-Null
$plainPassword = $null
$securePassword = $null
[IO.File]::WriteAllText($readyMarker, "ready`n", [Text.UTF8Encoding]::new($false))
Restart-Computer -Force
