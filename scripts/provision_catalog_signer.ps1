param(
    [Parameter(Mandatory = $true)][string]$Python,
    [Parameter(Mandatory = $true)][string]$BackupDirectory,
    [Parameter(Mandatory = $true)][string]$SecretName,
    [string]$GoogleProject = 'community-ai-506321',
    [string]$KeyDirectory = (Join-Path $env:LOCALAPPDATA 'CommunityAI\publisher-keys\catalog-20260906')
)
$ErrorActionPreference = 'Stop'
$Python = (Get-Command $Python -ErrorAction Stop).Source

# Working keys and the second-volume copy stay outside the repo. The owner also
# requested a third copy under the repo's explicitly ignored secret directory.
function New-ProtectedDirectory([string]$Target, [bool]$AllowIgnoredBackup = $false) {
    $resolved = [IO.Path]::GetFullPath($Target)
    $repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
    if ($AllowIgnoredBackup) {
        $allowed = Join-Path $repository '.publisher-secrets'
        if (-not $resolved.StartsWith($allowed + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Repository backup must remain inside .publisher-secrets.'
        }
        & git -C $repository check-ignore --quiet -- (Join-Path $resolved 'catalog-private.pem')
        if ($LASTEXITCODE -ne 0) { throw 'Repository backup is not ignored; refusing to create private material.' }
    } elseif ($resolved.StartsWith($repository + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Publisher keys must be outside the repository.'
    }
    if (Test-Path -LiteralPath $resolved) { throw "Refusing to replace existing directory: $resolved" }
    New-Item -ItemType Directory -Path $resolved | Out-Null
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetOwner($sid)
    $acl.SetAccessRuleProtection($true, $false)
    $rule = [Security.AccessControl.FileSystemAccessRule]::new(
        $sid, 'FullControl', 'ContainerInherit, ObjectInherit', 'None', 'Allow'
    )
    $acl.AddAccessRule($rule)
    Set-Acl -LiteralPath $resolved -AclObject $acl
    $checked = Get-Acl -LiteralPath $resolved
    if (-not $checked.AreAccessRulesProtected -or $checked.Access.Count -ne 1) {
        throw "Could not establish exclusive publisher-directory permissions: $resolved"
    }
    return $resolved
}

$recordPath = Join-Path (Split-Path -Parent ([IO.Path]::GetFullPath($KeyDirectory))) 'active-catalog.json'
if (Test-Path -LiteralPath $recordPath) { throw 'An active signer registry already exists; review rotation explicitly.' }
if ($SecretName -notmatch '^[a-zA-Z0-9_-]{1,255}$') { throw 'Invalid Google secret name.' }
Get-Command gcloud -ErrorAction Stop | Out-Null
$primary = New-ProtectedDirectory $KeyDirectory
$backup = New-ProtectedDirectory $BackupDirectory
$repositoryBackup = New-ProtectedDirectory (Join-Path (Join-Path $PSScriptRoot '..\.publisher-secrets') (Split-Path -Leaf $primary)) $true
$keyPath = Join-Path $primary 'catalog-private.pem'
$publicPath = Join-Path $primary 'catalog-public.json'
& $Python -m drift.cli.run_catalog keygen $keyPath --public-output $publicPath
if ($LASTEXITCODE -ne 0) { throw 'Catalog key generation failed.' }
foreach ($destination in @($backup, $repositoryBackup)) {
    Copy-Item -LiteralPath $keyPath -Destination (Join-Path $destination 'catalog-private.pem')
    Copy-Item -LiteralPath $publicPath -Destination (Join-Path $destination 'catalog-public.json')
    if ((Get-FileHash -LiteralPath $keyPath).Hash -ne (Get-FileHash -LiteralPath (Join-Path $destination 'catalog-private.pem')).Hash) {
        throw 'Publisher backup verification failed.'
    }
}
$publicKey = Get-Content -LiteralPath $publicPath -Raw | ConvertFrom-Json
& gcloud secrets create $SecretName --project=$GoogleProject --replication-policy=automatic --quiet
if ($LASTEXITCODE -ne 0) { throw 'Could not create a new emergency secret; local copies were retained.' }
$onlineVersion = & gcloud secrets versions add $SecretName --project=$GoogleProject --data-file=$keyPath '--format=value(name)' --quiet
if ($LASTEXITCODE -ne 0) { throw 'Emergency upload failed; local copies were retained.' }
$version = ($onlineVersion.Trim() -split '/')[-1]
$verification = @'
import sys
from catalog_key_backup import load_online_backup
key = load_online_backup(*sys.argv[1:])
challenge = b'CommunityAI publisher provisioning backup verification'
key.trusted_key.public_key_object.verify(key.sign(challenge), challenge)
'@
Push-Location $PSScriptRoot
try {
    & $Python -c $verification $GoogleProject $SecretName $version $publicKey.key_id
    if ($LASTEXITCODE -ne 0) { throw 'Emergency recovery verification failed; all created copies were retained.' }
} finally { Pop-Location }
$record = [ordered]@{
    schema_version = 1
    key_id = $publicKey.key_id
    private_key_path = $keyPath
    backup_private_key_path = (Join-Path $backup 'catalog-private.pem')
    repository_backup_private_key_path = (Join-Path $repositoryBackup 'catalog-private.pem')
    emergency_backup = @{ project = $GoogleProject; secret = $SecretName; version = $version }
    created_at = [DateTime]::UtcNow.ToString('o')
    backup_kind = 'Google Secret Manager, second local volume, gitignored repository copy'
}
$record | ConvertTo-Json | Set-Content -LiteralPath $recordPath -Encoding utf8
$record | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $backup 'signer-registry.json') -Encoding utf8
$record | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $repositoryBackup 'signer-registry.json') -Encoding utf8
Write-Output "Created and backed up publisher key $($publicKey.key_id). Registry: $recordPath"
