# Gate 13 native Windows localhost-inference adapter.
#
# Run this file with Windows PowerShell 5.1 from an ordinary clean-host user
# session. It accepts no arguments. The packaged CommunityAI desktop and node
# must already be running on their fixed loopback port.
#
# The privileged control token and the temporary OpenAI API key exist only in
# this process's memory. This script never places either secret in argv, the
# environment, a file, a transcript, diagnostic output, or evidence.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$VerbosePreference = "SilentlyContinue"
$DebugPreference = "SilentlyContinue"
$InformationPreference = "SilentlyContinue"

$script:CredentialService = "org.communityai.desktop"
$script:CredentialAccount = "local-node-control-v1"
$script:LoopbackOrigin = "http://127.0.0.1:8080"
$script:QualificationKeyLabel = "gate13-windows-localhost-inference"
$script:MaximumResponseBytes = 1048576
$script:RequestTimeoutSeconds = 3600
$script:ControlKeyPattern = "^drift_control_[A-Za-z0-9_-]{43,115}$"
$script:ApiKeyPattern = "^drift_[A-Za-z0-9_-]{43,115}$"
$script:KeyIdPattern = "^key_[0-9a-f]{16}$"
$script:DigestPattern = "^sha256:[0-9a-f]{64}$"
$script:Profiles = @{
    "Qwen3.5 2B" = [pscustomobject]@{
        ManifestDigest = "3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33"
        RevisionCommit = "15852e8c16360a2fea060d615a32b45270f8a8fc"
        SelectedCount = 8
        SelectedBytes = [int64]4571197320
    }
    "Gemma 4 E2B IT" = [pscustomobject]@{
        ManifestDigest = "2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd"
        RevisionCommit = "3e22461f65e89153144f8adb70e3b8c2cc9845a7"
        SelectedCount = 5
        SelectedBytes = [int64]10278818149
    }
}

function Assert-Gate13NoTranscript {
    $transcriptVariable = Get-Variable -Name Transcript -Scope Global -ErrorAction SilentlyContinue
    if ($null -ne $transcriptVariable -and -not [string]::IsNullOrEmpty([string]$transcriptVariable.Value)) {
        throw "unsafe host state"
    }

    $policyRoots = @(
        "HKLM:\Software\Policies\Microsoft\Windows\PowerShell\Transcription",
        "HKCU:\Software\Policies\Microsoft\Windows\PowerShell\Transcription",
        "HKLM:\Software\Policies\Microsoft\PowerShellCore\Transcription",
        "HKCU:\Software\Policies\Microsoft\PowerShellCore\Transcription"
    )
    foreach ($policyRoot in $policyRoots) {
        if (Test-Path -LiteralPath $policyRoot) {
            $policy = Get-ItemProperty -LiteralPath $policyRoot -ErrorAction Stop
            if ($null -ne $policy.EnableTranscripting -and [int]$policy.EnableTranscripting -ne 0) {
                throw "unsafe host state"
            }
        }
    }
}

function Initialize-Gate13CredentialInterop {
    if ($null -ne ("Gate13.NativeCredential" -as [type])) {
        return
    }

    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text.RegularExpressions;

namespace Gate13
{
    public static class NativeCredential
    {
        private const UInt32 CredTypeGeneric = 1;

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct Credential
        {
            public UInt32 Flags;
            public UInt32 Type;
            public IntPtr TargetName;
            public IntPtr Comment;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
            public UInt32 CredentialBlobSize;
            public IntPtr CredentialBlob;
            public UInt32 Persist;
            public UInt32 AttributeCount;
            public IntPtr Attributes;
            public IntPtr TargetAlias;
            public IntPtr UserName;
        }

        [DllImport("Advapi32.dll", EntryPoint = "CredReadW", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CredRead(
            string target,
            UInt32 type,
            UInt32 flags,
            out IntPtr credentialPointer
        );

        [DllImport("Advapi32.dll", SetLastError = false)]
        private static extern void CredFree(IntPtr credentialPointer);

        private static string ReadExact(string targetName, string account, bool usernameMismatchMeansMissing)
        {
            IntPtr credentialPointer = IntPtr.Zero;
            IntPtr blobPointer = IntPtr.Zero;
            UInt32 blobSize = 0;
            try
            {
                if (!CredRead(targetName, CredTypeGeneric, 0, out credentialPointer) ||
                    credentialPointer == IntPtr.Zero)
                {
                    return null;
                }

                Credential credential =
                    (Credential)Marshal.PtrToStructure(credentialPointer, typeof(Credential));
                string target = Marshal.PtrToStringUni(credential.TargetName);
                string userName = Marshal.PtrToStringUni(credential.UserName);
                if (credential.Type != CredTypeGeneric ||
                    !String.Equals(target, targetName, StringComparison.Ordinal))
                {
                    throw new InvalidOperationException("credential identity mismatch");
                }
                if (!String.Equals(userName, account, StringComparison.Ordinal))
                {
                    return EvaluateTestCandidate(
                        targetName,
                        account,
                        target,
                        userName,
                        null,
                        usernameMismatchMeansMissing
                    );
                }

                blobPointer = credential.CredentialBlob;
                blobSize = credential.CredentialBlobSize;
                if (blobPointer == IntPtr.Zero || blobSize < 2 || blobSize > 512 ||
                    blobSize % 2 != 0)
                {
                    throw new InvalidOperationException("credential payload invalid");
                }

                string secret = Marshal.PtrToStringUni(blobPointer, checked((int)blobSize / 2));
                return EvaluateTestCandidate(
                    targetName,
                    account,
                    target,
                    userName,
                    secret,
                    usernameMismatchMeansMissing
                );
            }
            finally
            {
                if (blobPointer != IntPtr.Zero && blobSize > 0 && blobSize <= 512)
                {
                    for (int index = 0; index < checked((int)blobSize); index++)
                    {
                        Marshal.WriteByte(blobPointer, index, 0);
                    }
                }
                if (credentialPointer != IntPtr.Zero)
                {
                    CredFree(credentialPointer);
                }
            }
        }

        private static string EvaluateTestCandidate(
            string expectedTarget,
            string account,
            string actualTarget,
            string actualUser,
            string secret,
            bool usernameMismatchMeansMissing
        )
        {
            if (actualTarget == null)
            {
                return null;
            }
            if (!String.Equals(actualTarget, expectedTarget, StringComparison.Ordinal))
            {
                throw new InvalidOperationException("credential identity mismatch");
            }
            if (!String.Equals(actualUser, account, StringComparison.Ordinal))
            {
                if (usernameMismatchMeansMissing)
                {
                    return null;
                }
                throw new InvalidOperationException("credential identity mismatch");
            }
            if (secret == null || !Regex.IsMatch(
                secret,
                @"^drift_control_[A-Za-z0-9_-]{43,115}$",
                RegexOptions.CultureInvariant
            ))
            {
                throw new InvalidOperationException("credential payload invalid");
            }
            return secret;
        }

        public static string ResolveControlKeyForTest(
            string service,
            string account,
            string primaryTarget,
            string primaryUser,
            string primarySecret,
            string compoundTarget,
            string compoundUser,
            string compoundSecret
        )
        {
            string secret = EvaluateTestCandidate(
                service,
                account,
                primaryTarget,
                primaryUser,
                primarySecret,
                true
            );
            if (secret != null)
            {
                return secret;
            }
            secret = EvaluateTestCandidate(
                account + "@" + service,
                account,
                compoundTarget,
                compoundUser,
                compoundSecret,
                false
            );
            if (secret == null)
            {
                throw new InvalidOperationException("credential unavailable");
            }
            return secret;
        }

        public static string ReadControlKey(string service, string account)
        {
            string secret = ReadExact(service, account, true);
            if (secret != null)
            {
                return secret;
            }
            secret = ReadExact(account + "@" + service, account, false);
            if (secret == null)
            {
                throw new InvalidOperationException("credential unavailable");
            }
            return secret;
        }
    }
}
"@ -Language CSharp -ErrorAction Stop | Out-Null
}

function Read-Gate13ControlKey {
    $secret = [Gate13.NativeCredential]::ReadControlKey(
        $script:CredentialService,
        $script:CredentialAccount
    )
    if (
        -not ($secret -is [string]) -or
        $secret.Length -gt 128 -or
        $secret -notmatch $script:ControlKeyPattern
    ) {
        throw "credential rejected"
    }
    return $secret
}

function Get-Gate13Property {
    param(
        [Parameter(Mandatory = $true)] [object] $InputObject,
        [Parameter(Mandatory = $true)] [string] $Name
    )
    if ($null -eq $InputObject) {
        throw "invalid response"
    }
    $property = $InputObject.PSObject.Properties[$Name]
    if ($null -eq $property) {
        throw "invalid response"
    }
    return $property.Value
}

function Assert-Gate13ExactProperties {
    param(
        [Parameter(Mandatory = $true)] [object] $InputObject,
        [Parameter(Mandatory = $true)] [string[]] $Names
    )
    if ($null -eq $InputObject) {
        throw "invalid response"
    }
    $actual = @($InputObject.PSObject.Properties.Name | Sort-Object)
    $expected = @($Names | Sort-Object)
    if ($actual.Count -ne $expected.Count) {
        throw "invalid response"
    }
    for ($index = 0; $index -lt $expected.Count; $index++) {
        if ($actual[$index] -cne $expected[$index]) {
            throw "invalid response"
        }
    }
}

function ConvertFrom-Gate13Json {
    param([Parameter(Mandatory = $true)] [string] $Payload)
    if ([string]::IsNullOrWhiteSpace($Payload)) {
        throw "invalid response"
    }
    $result = ConvertFrom-Json -InputObject $Payload -ErrorAction Stop
    if ($null -eq $result -or $result -is [array] -or $result -is [string]) {
        throw "invalid response"
    }
    return $result
}

function Read-Gate13BoundedResponse {
    param([Parameter(Mandatory = $true)] [System.Net.Http.HttpContent] $Content)

    $declaredLength = $Content.Headers.ContentLength
    if ($null -ne $declaredLength -and (
        [int64]$declaredLength -lt 1 -or
        [int64]$declaredLength -gt $script:MaximumResponseBytes
    )) {
        throw "invalid response"
    }

    $stream = $null
    $memory = $null
    $buffer = New-Object byte[] 8192
    $payload = $null
    $text = $null
    try {
        $stream = $Content.ReadAsStreamAsync().GetAwaiter().GetResult()
        $memory = New-Object System.IO.MemoryStream
        while ($true) {
            $remaining = $script:MaximumResponseBytes + 1 - [int]$memory.Length
            if ($remaining -le 0) {
                throw "invalid response"
            }
            $readSize = [Math]::Min($buffer.Length, $remaining)
            $read = $stream.Read($buffer, 0, $readSize)
            if ($read -eq 0) {
                break
            }
            $memory.Write($buffer, 0, $read)
        }
        if ($memory.Length -lt 1 -or $memory.Length -gt $script:MaximumResponseBytes) {
            throw "invalid response"
        }
        $payload = $memory.ToArray()
        $utf8 = New-Object System.Text.UTF8Encoding($false, $true)
        $text = $utf8.GetString($payload)
        return ConvertFrom-Gate13Json -Payload $text
    }
    finally {
        if ($null -ne $payload) {
            [Array]::Clear($payload, 0, $payload.Length)
        }
        [Array]::Clear($buffer, 0, $buffer.Length)
        if ($null -ne $memory) {
            $memory.Dispose()
        }
        if ($null -ne $stream) {
            $stream.Dispose()
        }
        $text = $null
    }
}

function Invoke-Gate13LoopbackJson {
    param(
        [Parameter(Mandatory = $true)] [ValidateSet("GET", "POST", "PUT", "DELETE")] [string] $Method,
        [Parameter(Mandatory = $true)] [string] $Path,
        [Parameter(Mandatory = $true)] [string] $BearerToken,
        [object] $Body = $null
    )

    $fixedPath = $Path -in @(
        "/control/v1/status",
        "/control/v1/keys",
        "/control/v1/contribution-policy",
        "/control/v1/workers",
        "/v1/completions"
    )
    $keyDeletePath = $Path -match "^/control/v1/keys/key_[0-9a-f]{16}$"
    $workerActionPath = $Path -match "^/control/v1/workers/automatic/(start|pause|restart)$"
    if (-not $fixedPath -and -not $keyDeletePath -and -not $workerActionPath) {
        throw "endpoint rejected"
    }
    if (
        ($Path -eq "/control/v1/status" -and $Method -ne "GET") -or
        ($Path -eq "/v1/completions" -and $Method -ne "POST") -or
        ($Path -eq "/control/v1/keys" -and $Method -notin @("GET", "POST")) -or
        ($Path -eq "/control/v1/contribution-policy" -and $Method -notin @("GET", "PUT")) -or
        ($Path -eq "/control/v1/workers" -and $Method -ne "GET") -or
        ($keyDeletePath -and $Method -ne "DELETE") -or
        ($workerActionPath -and $Method -ne "POST")
    ) {
        throw "method rejected"
    }
    if (
        -not ($BearerToken -is [string]) -or
        $BearerToken.Length -gt 128 -or
        ($BearerToken -notmatch $script:ControlKeyPattern -and
         $BearerToken -notmatch $script:ApiKeyPattern)
    ) {
        throw "credential rejected"
    }

    Add-Type -AssemblyName System.Net.Http -ErrorAction Stop
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.AllowAutoRedirect = $false
    $handler.UseProxy = $false
    $handler.UseCookies = $false
    $client = New-Object System.Net.Http.HttpClient($handler)
    $client.Timeout = [TimeSpan]::FromSeconds($script:RequestTimeoutSeconds)
    $request = $null
    $response = $null
    $jsonBody = $null
    try {
        $uri = New-Object System.Uri($script:LoopbackOrigin + $Path, [System.UriKind]::Absolute)
        if (
            $uri.Scheme -cne "http" -or
            $uri.Host -cne "127.0.0.1" -or
            $uri.Port -ne 8080 -or
            -not [string]::IsNullOrEmpty($uri.UserInfo) -or
            -not [string]::IsNullOrEmpty($uri.Query) -or
            -not [string]::IsNullOrEmpty($uri.Fragment)
        ) {
            throw "endpoint rejected"
        }

        $httpMethod = New-Object System.Net.Http.HttpMethod($Method)
        $request = New-Object System.Net.Http.HttpRequestMessage($httpMethod, $uri)
        $request.Headers.Authorization =
            New-Object System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", $BearerToken)
        [void]$request.Headers.Accept.ParseAdd("application/json")
        $request.Headers.ExpectContinue = $false
        if ($null -ne $Body) {
            $jsonBody = ConvertTo-Json -InputObject $Body -Compress -Depth 8
            $request.Content = New-Object System.Net.Http.StringContent(
                $jsonBody,
                [System.Text.Encoding]::UTF8,
                "application/json"
            )
        }

        $response = $client.SendAsync(
            $request,
            [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead
        ).GetAwaiter().GetResult()

        $expectedStatus = if ($Path -eq "/control/v1/keys" -and $Method -eq "POST") { 201 } else { 200 }
        if ([int]$response.StatusCode -ne $expectedStatus) {
            throw "request rejected"
        }
        if ($null -eq $response.Content.Headers.ContentType -or
            $response.Content.Headers.ContentType.MediaType -cne "application/json") {
            throw "invalid response"
        }
        if (@($response.Content.Headers.ContentEncoding).Count -ne 0) {
            throw "invalid response"
        }
        return Read-Gate13BoundedResponse -Content $response.Content
    }
    finally {
        $jsonBody = $null
        if ($null -ne $response) {
            $response.Dispose()
        }
        if ($null -ne $request) {
            $request.Dispose()
        }
        $client.Dispose()
        $handler.Dispose()
    }
}

function Get-Gate13ActiveKeySnapshot {
    param([Parameter(Mandatory = $true)] [string] $ControlToken)

    $response = Invoke-Gate13LoopbackJson `
        -Method "GET" `
        -Path "/control/v1/keys" `
        -BearerToken $ControlToken
    Assert-Gate13ExactProperties -InputObject $response -Names @("keys")
    $keysValue = Get-Gate13Property -InputObject $response -Name "keys"
    if ($null -eq $keysValue) {
        $keys = @()
    }
    else {
        $keys = @($keysValue)
    }
    if ($keys.Count -gt 64) {
        throw "invalid response"
    }

    $activeIds = New-Object "System.Collections.Generic.HashSet[string]" ([StringComparer]::Ordinal)
    $qualificationIds = New-Object "System.Collections.Generic.HashSet[string]" ([StringComparer]::Ordinal)
    foreach ($key in $keys) {
        Assert-Gate13ExactProperties `
            -InputObject $key `
            -Names @("id", "label", "fingerprint", "created_at", "revoked_at")
        $keyId = Get-Gate13Property -InputObject $key -Name "id"
        $label = Get-Gate13Property -InputObject $key -Name "label"
        $fingerprint = Get-Gate13Property -InputObject $key -Name "fingerprint"
        $createdAt = Get-Gate13Property -InputObject $key -Name "created_at"
        $revokedAt = Get-Gate13Property -InputObject $key -Name "revoked_at"
        if (
            -not ($keyId -is [string]) -or $keyId -notmatch $script:KeyIdPattern -or
            -not ($label -is [string]) -or [string]::IsNullOrWhiteSpace($label) -or $label.Length -gt 64 -or
            -not ($fingerprint -is [string]) -or $fingerprint -notmatch "^[0-9a-f]{12}$" -or
            -not ($createdAt -is [int] -or $createdAt -is [long]) -or [int64]$createdAt -lt 0
        ) {
            throw "invalid response"
        }
        if ($null -ne $revokedAt -and (
            -not ($revokedAt -is [int] -or $revokedAt -is [long]) -or
            [int64]$revokedAt -lt [int64]$createdAt
        )) {
            throw "invalid response"
        }
        if ($null -eq $revokedAt) {
            if (-not $activeIds.Add($keyId)) {
                throw "invalid response"
            }
            if ($label -ceq $script:QualificationKeyLabel) {
                if (-not $qualificationIds.Add($keyId)) {
                    throw "invalid response"
                }
            }
        }
    }
    return [pscustomobject]@{
        ActiveIds = $activeIds
        ActiveCount = $activeIds.Count
        QualificationIds = $qualificationIds
        QualificationLabelPresent = ($qualificationIds.Count -ne 0)
    }
}

function Assert-Gate13SameActiveKeys {
    param(
        [Parameter(Mandatory = $true)] [object] $Before,
        [Parameter(Mandatory = $true)] [object] $After
    )
    if ($Before.ActiveCount -ne $After.ActiveCount) {
        throw "temporary key cleanup failed"
    }
    foreach ($keyId in $Before.ActiveIds) {
        if (-not $After.ActiveIds.Contains($keyId)) {
            throw "temporary key cleanup failed"
        }
    }
}

function Get-Gate13SelectedProfile {
    param([Parameter(Mandatory = $true)] [object] $Status)

    if (
        (Get-Gate13Property -InputObject $Status -Name "api_version") -ne 1 -or
        (Get-Gate13Property -InputObject $Status -Name "status") -cne "running" -or
        (Get-Gate13Property -InputObject $Status -Name "openai_base_url") -cne
            "http://127.0.0.1:8080/v1"
    ) {
        throw "invalid status"
    }

    $selection = Get-Gate13Property -InputObject $Status -Name "auto_selection"
    if (
        (Get-Gate13Property -InputObject $selection -Name "selector") -cne "auto" -or
        (Get-Gate13Property -InputObject $selection -Name "status") -cne "selected"
    ) {
        throw "automatic selection unavailable"
    }
    $modelId = Get-Gate13Property -InputObject $selection -Name "model"
    $digestId = Get-Gate13Property -InputObject $selection -Name "manifest_digest"
    if (
        -not ($modelId -is [string]) -or
        -not $script:Profiles.ContainsKey($modelId) -or
        -not ($digestId -is [string]) -or
        $digestId -notmatch $script:DigestPattern
    ) {
        throw "selected model rejected"
    }
    $profile = $script:Profiles[$modelId]
    if ($digestId -cne ("sha256:" + $profile.ManifestDigest)) {
        throw "selected manifest rejected"
    }

    $modelsValue = Get-Gate13Property -InputObject $Status -Name "models"
    $models = @($modelsValue)
    $matchingModels = @(
        $models | Where-Object {
            $null -ne $_ -and
            (Get-Gate13Property -InputObject $_ -Name "id") -ceq $modelId -and
            (Get-Gate13Property -InputObject $_ -Name "manifest_digest") -ceq $digestId
        }
    )
    if ($matchingModels.Count -ne 1) {
        throw "selected model snapshot rejected"
    }
    $download = Get-Gate13Property -InputObject $matchingModels[0] -Name "download"
    if (
        (Get-Gate13Property -InputObject $download -Name "schema_version") -ne 1 -or
        [int64](Get-Gate13Property -InputObject $download -Name "selected_whole_shard_bytes") -ne
            [int64]$profile.SelectedBytes
    ) {
        throw "selected byte estimate rejected"
    }
    return [pscustomobject]@{
        ModelId = $modelId
        ManifestDigest = $profile.ManifestDigest
        RevisionCommit = $profile.RevisionCommit
        SelectedCount = $profile.SelectedCount
        SelectedBytes = $profile.SelectedBytes
    }
}

function Invoke-Gate13WindowsLocalhostInference {
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    $controlToken = $null
    $apiToken = $null
    $createdKeyId = $null
    $baseline = $null
    $createAttempted = $false
    $cleanupFailed = $false
    $phase = $null
    try {
        Assert-Gate13NoTranscript
        Initialize-Gate13CredentialInterop
        $controlToken = Read-Gate13ControlKey

        $statusBefore = Invoke-Gate13LoopbackJson `
            -Method "GET" `
            -Path "/control/v1/status" `
            -BearerToken $controlToken
        $selectedBefore = Get-Gate13SelectedProfile -Status $statusBefore
        $statusBefore = $null

        $baseline = Get-Gate13ActiveKeySnapshot -ControlToken $controlToken
        if ($baseline.ActiveCount -lt 1 -or $baseline.QualificationLabelPresent) {
            throw "API key baseline rejected"
        }

        $createAttempted = $true
        $created = Invoke-Gate13LoopbackJson `
            -Method "POST" `
            -Path "/control/v1/keys" `
            -BearerToken $controlToken `
            -Body ([ordered]@{ label = $script:QualificationKeyLabel })
        Assert-Gate13ExactProperties -InputObject $created -Names @("key", "secret")
        $createdKey = Get-Gate13Property -InputObject $created -Name "key"
        Assert-Gate13ExactProperties `
            -InputObject $createdKey `
            -Names @("id", "label", "fingerprint", "created_at", "revoked_at")
        $candidateKeyId = Get-Gate13Property -InputObject $createdKey -Name "id"
        $createdLabel = Get-Gate13Property -InputObject $createdKey -Name "label"
        $createdRevokedAt = Get-Gate13Property -InputObject $createdKey -Name "revoked_at"
        $apiToken = Get-Gate13Property -InputObject $created -Name "secret"
        if (
            -not ($candidateKeyId -is [string]) -or
            $candidateKeyId -notmatch $script:KeyIdPattern -or
            $baseline.ActiveIds.Contains($candidateKeyId) -or
            $createdLabel -cne $script:QualificationKeyLabel -or
            $null -ne $createdRevokedAt -or
            -not ($apiToken -is [string]) -or
            $apiToken.Length -gt 128 -or
            $apiToken -notmatch $script:ApiKeyPattern
        ) {
            throw "temporary API key rejected"
        }
        $createdKeyId = $candidateKeyId
        $candidateKeyId = $null
        $created = $null
        $createdKey = $null

        $completion = Invoke-Gate13LoopbackJson `
            -Method "POST" `
            -Path "/v1/completions" `
            -BearerToken $apiToken `
            -Body ([ordered]@{
                model = "auto"
                prompt = "1"
                max_tokens = 8
                temperature = 0
                stream = $false
                n = 1
            })
        Assert-Gate13ExactProperties `
            -InputObject $completion `
            -Names @("id", "object", "created", "model", "choices", "usage")
        if (
            (Get-Gate13Property -InputObject $completion -Name "object") -cne "text_completion" -or
            (Get-Gate13Property -InputObject $completion -Name "model") -cne $selectedBefore.ModelId
        ) {
            throw "completion identity rejected"
        }
        $choices = @((Get-Gate13Property -InputObject $completion -Name "choices"))
        if ($choices.Count -ne 1) {
            throw "completion response rejected"
        }
        Assert-Gate13ExactProperties `
            -InputObject $choices[0] `
            -Names @("index", "text", "finish_reason")
        $responseText = Get-Gate13Property -InputObject $choices[0] -Name "text"
        if (
            (Get-Gate13Property -InputObject $choices[0] -Name "index") -ne 0 -or
            -not ($responseText -is [string]) -or
            [string]::IsNullOrWhiteSpace($responseText)
        ) {
            throw "completion response rejected"
        }
        $usage = Get-Gate13Property -InputObject $completion -Name "usage"
        Assert-Gate13ExactProperties `
            -InputObject $usage `
            -Names @("prompt_tokens", "completion_tokens", "total_tokens")
        $generatedTokens = Get-Gate13Property -InputObject $usage -Name "completion_tokens"
        if (
            -not ($generatedTokens -is [int] -or $generatedTokens -is [long]) -or
            [int64]$generatedTokens -lt 1 -or
            [int64]$generatedTokens -gt 8
        ) {
            throw "completion token count rejected"
        }
        $responseText = $null
        $choices = $null
        $usage = $null
        $completion = $null

        $statusAfter = Invoke-Gate13LoopbackJson `
            -Method "GET" `
            -Path "/control/v1/status" `
            -BearerToken $controlToken
        $selectedAfter = Get-Gate13SelectedProfile -Status $statusAfter
        if (
            $selectedAfter.ModelId -cne $selectedBefore.ModelId -or
            $selectedAfter.ManifestDigest -cne $selectedBefore.ManifestDigest
        ) {
            throw "automatic selection changed during inference"
        }
        $statusAfter = $null

        $phase = [ordered]@{
            completion_count = 1
            duration_seconds = 0.0
            generated_token_count = [int64]$generatedTokens
            loopback_only = $true
            manifest_digest = $selectedBefore.ManifestDigest
            model_id = $selectedBefore.ModelId
            passed = $true
            phase = "localhost_inference"
            response_content_retained = $false
            source_imports_used = $false
            token_identifier_count = 0
        }
    }
    finally {
        if ($createAttempted -and $null -ne $controlToken -and $null -ne $baseline) {
            try {
                $current = Get-Gate13ActiveKeySnapshot -ControlToken $controlToken
                if ($null -eq $createdKeyId) {
                    $reservedIds = @(
                        $current.QualificationIds |
                            Where-Object { -not $baseline.QualificationIds.Contains($_) }
                    )
                    if ($reservedIds.Count -ne 1) {
                        throw "temporary key cleanup failed"
                    }
                    $createdKeyId = $reservedIds[0]
                }
                if ($null -ne $createdKeyId -and $current.ActiveIds.Contains($createdKeyId)) {
                    $revoked = Invoke-Gate13LoopbackJson `
                        -Method "DELETE" `
                        -Path ("/control/v1/keys/" + $createdKeyId) `
                        -BearerToken $controlToken
                    $revoked = $null
                }
                $afterCleanup = Get-Gate13ActiveKeySnapshot -ControlToken $controlToken
                Assert-Gate13SameActiveKeys -Before $baseline -After $afterCleanup
                $current = $null
                $afterCleanup = $null
            }
            catch {
                $cleanupFailed = $true
            }
        }
        $apiToken = $null
        $controlToken = $null
        $candidateKeyId = $null
        $createdKeyId = $null
        $baseline = $null
        [GC]::Collect()
        [GC]::WaitForPendingFinalizers()
        [GC]::Collect()
        $timer.Stop()
    }

    if ($cleanupFailed -or $null -eq $phase) {
        throw "qualification action failed"
    }
    $duration = [Math]::Round($timer.Elapsed.TotalSeconds, 6)
    if ([double]::IsNaN($duration) -or [double]::IsInfinity($duration) -or
        $duration -lt 0 -or $duration -gt 86400) {
        throw "duration rejected"
    }
    $phase.duration_seconds = $duration
    return $phase
}

function ConvertTo-Gate13CanonicalJson {
    param([Parameter(Mandatory = $true)] [object] $Record)
    return ConvertTo-Json -InputObject $Record -Compress -Depth 8
}

function Start-Gate13WindowsLocalhostInference {
    $record = $null
    $exitCode = 2
    try {
        $record = Invoke-Gate13WindowsLocalhostInference
        $exitCode = 0
    }
    catch {
        $record = [ordered]@{
            failure_code = "windows_localhost_inference_failed"
            result = "failed"
            schema_version = 1
        }
    }
    $rendered = ConvertTo-Gate13CanonicalJson -Record $record
    [Console]::Out.WriteLine($rendered)
    $rendered = $null
    $record = $null
    return $exitCode
}

if ($MyInvocation.InvocationName -ne ".") {
    if ($args.Count -ne 0) {
        $failure = [ordered]@{
            failure_code = "windows_localhost_inference_failed"
            result = "failed"
            schema_version = 1
        }
        [Console]::Out.WriteLine((ConvertTo-Gate13CanonicalJson -Record $failure))
        exit 2
    }
    $code = Start-Gate13WindowsLocalhostInference
    exit $code
}
