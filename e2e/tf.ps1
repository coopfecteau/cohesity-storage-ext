<#
Run terraform in e2e/terraform with the installer token taken from YOUR dotenv file.

    .\e2e\tf.ps1 -EnvFile C:\path\to\dt.env apply
    .\e2e\tf.ps1 -EnvFile C:\path\to\dt.env -TokenVar DT_OPERATOR_TOKEN destroy

Sets TF_VAR_dt_paas_token (from DT_OPERATOR_TOKEN by default) and TF_VAR_dt_environment_url
(from DT_API_URL, else derived from DT_APPS_HOST) for the terraform child process only, then
clears them. The token is never printed and never written to a tfvars file. A variable already
set in your session wins over the file. The file is parsed, not dot-sourced: nothing in it runs.
#>
# No positional binding: every bare word goes to terraform, never into -TokenVar and friends.
[CmdletBinding(PositionalBinding = $false)]
param(
    [Parameter(Mandatory = $true)][string]$EnvFile,
    [string]$TokenVar = "DT_OPERATOR_TOKEN",
    [string]$UrlVar = "DT_API_URL",
    [string]$AppsVar = "DT_APPS_HOST",
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$TerraformArgs
)
$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $EnvFile)) { throw "cannot read env file $EnvFile" }
$values = @{}
foreach ($raw in Get-Content -LiteralPath $EnvFile -Encoding UTF8) {
    $line = $raw.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { continue }
    if ($line.StartsWith("export ")) { $line = $line.Substring(7).TrimStart() }
    $eq = $line.IndexOf("=")
    if ($eq -lt 1) { continue }
    $key = $line.Substring(0, $eq).Trim()
    $value = $line.Substring($eq + 1).Trim()
    if ($value.Length -ge 2 -and ($value[0] -eq '"' -or $value[0] -eq "'") -and $value[-1] -eq $value[0]) {
        $value = $value.Substring(1, $value.Length - 2)
    }
    $values[$key] = $value
}

function Get-Setting([string]$Name) {
    $fromEnv = [Environment]::GetEnvironmentVariable($Name)
    if ($fromEnv) { return $fromEnv }
    return $values[$Name]
}

$hadToken = [bool]$env:TF_VAR_dt_paas_token
$hadUrl = [bool]$env:TF_VAR_dt_environment_url
try {
    if (-not $hadToken) {
        $token = Get-Setting $TokenVar
        if (-not $token) { throw "$TokenVar is not set in the environment or in $EnvFile" }
        $env:TF_VAR_dt_paas_token = $token
        Remove-Variable token
    }
    if (-not $hadUrl) {
        $url = Get-Setting $UrlVar
        if (-not $url) {
            $apps = Get-Setting $AppsVar
            if ($apps -match "\.apps\.dynatrace\.com") { $url = $apps -replace "\.apps\.dynatrace\.com", ".live.dynatrace.com" }
            elseif ($apps) { $url = $apps -replace "\.apps\.", "." }
        }
        if (-not $url) { throw "neither $UrlVar nor $AppsVar is set in the environment or in $EnvFile" }
        $url = $url.TrimEnd("/")
        if ($url.EndsWith("/api")) { $url = $url.Substring(0, $url.Length - 4) }
        if (-not $url.StartsWith("https://")) { $url = "https://$url" }
        $env:TF_VAR_dt_environment_url = $url
        Write-Host "tenant (classic host): $url"
    }
    & terraform "-chdir=$PSScriptRoot\terraform" @TerraformArgs
    exit $LASTEXITCODE
}
finally {
    # Scoped to this run: do not leave the token in the caller's session.
    if (-not $hadToken) { Remove-Item Env:\TF_VAR_dt_paas_token -ErrorAction SilentlyContinue }
    if (-not $hadUrl) { Remove-Item Env:\TF_VAR_dt_environment_url -ErrorAction SilentlyContinue }
}
