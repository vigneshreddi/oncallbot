param(
  [string]$Namespace = 'incident-lab',
  [string]$EnvFile = '.env',
  [switch]$ProvisionDatabase,
  [switch]$ValidateOnly
)
$ErrorActionPreference = 'Stop'

function Send-Secret {
  param([string]$Name, [hashtable]$Data)
  $manifest = @{apiVersion='v1'; kind='Secret'; metadata=@{name=$Name; namespace=$Namespace}; type='Opaque'; data=$Data} | ConvertTo-Json -Depth 8 -Compress
  $arguments = @('apply', '-f', '-')
  if ($ValidateOnly) { $arguments += '--dry-run=server' }
  try {
    $ErrorActionPreference = 'Continue'
    $manifest | kubectl @arguments 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Secret validation/apply failed for $Name; details suppressed to protect credentials." }
  } finally { $ErrorActionPreference = 'Stop'; $manifest = $null }
}

function New-EncodedPassword {
  param([switch]$UrlSafe)
  $bytes = New-Object byte[] 32
  $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
  try {
    $rng.GetBytes($bytes)
    $password = [Convert]::ToBase64String($bytes)
    # DBeaver's -con syntax uses '=' and '|' as separators.
    if ($UrlSafe) { $password = $password.TrimEnd('=').Replace('+', '-').Replace('/', '_') }
    return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($password))
  } finally { $rng.Dispose(); [Array]::Clear($bytes, 0, $bytes.Length); $password = $null }
}

if ($ProvisionDatabase) {
  $raw = kubectl get secret postgres-secret -n $Namespace --ignore-not-found -o json 2>$null
  if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect postgres-secret; refusing to generate replacements.' }
  $data = @{}
  if ($raw) {
    $existing = ($raw -join "`n") | ConvertFrom-Json
    foreach ($property in $existing.data.PSObject.Properties) { $data[$property.Name] = $property.Value }
  }
  if (-not $data['POSTGRES_PASSWORD']) {
    $claims = kubectl get pvc -n $Namespace -l app=postgres -o name 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect PostgreSQL storage.' }
    $legacyClaim = kubectl get pvc data-postgres-0 -n $Namespace --ignore-not-found -o name 2>$null
    if ($LASTEXITCODE -ne 0 -or $claims -or $legacyClaim) {
      throw 'Existing PostgreSQL storage has no admin credential. Restore its original Secret; no data reset is permitted.'
    }
  }
  foreach ($key in 'POSTGRES_PASSWORD','ORDER_PASSWORD','INVENTORY_PASSWORD','PAYMENT_PASSWORD','INVESTIGATOR_PASSWORD','INDEXER_PASSWORD','CHAOS_PASSWORD','RO_PASSWORD') {
    if (-not $data[$key]) { $data[$key] = New-EncodedPassword }
  }
  if (-not $data['VIEWER_PASSWORD']) { $data['VIEWER_PASSWORD'] = New-EncodedPassword -UrlSafe }
  try { Send-Secret -Name postgres-secret -Data $data }
  finally { $data = $null; $existing = $null; $raw = $null }
}

$appData = @{}
if (Test-Path -LiteralPath $EnvFile) {
  foreach ($line in Get-Content -LiteralPath $EnvFile) {
    $entry = $line.Trim()
    if (-not $entry -or $entry.StartsWith('#')) { continue }
    if ($entry -notmatch '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { throw 'Invalid .env entry; values suppressed.' }
    $key = $Matches[1]; $value = $Matches[2]
    if ($key -match '^(DB_.*|PG.*|POSTGRES_.*|DATABASE_URL|READONLY_DATABASE_URL|RO_PASSWORD|ORDER_PASSWORD|INVENTORY_PASSWORD|PAYMENT_PASSWORD|INVESTIGATOR_PASSWORD|INDEXER_PASSWORD|CHAOS_PASSWORD|VIEWER_PASSWORD)$') {
      throw "Database credentials/settings must use deployment-generated postgres-secret, not .env: $key"
    }
    if ($value) { $appData[$key] = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($value)) }
  }
} else {
  Write-Warning 'No .env found; LLM credentials will not be loaded.'
}
if (-not $appData.ContainsKey('LLM_PROVIDER')) { $appData['LLM_PROVIDER'] = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('none')) }
try { Send-Secret -Name app-secrets -Data $appData }
finally { $appData = $null; $value = $null; $line = $null; $entry = $null; if ($Matches) { $Matches.Clear() } }
Write-Output 'Secrets processed without displaying credential values.'
