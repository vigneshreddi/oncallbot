param(
  [string]$Namespace = 'incident-lab',
  [int]$LocalPort = 15432,
  [string]$DBeaverPath = ''
)
$ErrorActionPreference = 'Stop'
if (-not $DBeaverPath) {
  $DBeaverPath = @(
    "$env:ProgramFiles\DBeaver\dbeaver.exe",
    "$env:LOCALAPPDATA\DBeaver\dbeaver.exe",
    "$env:LOCALAPPDATA\Programs\DBeaver\dbeaver.exe"
  ) | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $DBeaverPath -or -not (Test-Path $DBeaverPath)) { throw 'dbeaver.exe not found; pass -DBeaverPath.' }

$probe = New-Object Net.Sockets.TcpClient
try { $probe.Connect('127.0.0.1', $LocalPort); $listening = $true } catch { $listening = $false } finally { $probe.Dispose() }
if (-not $listening) {
  Start-Process kubectl -ArgumentList "port-forward -n $Namespace svc/postgres ${LocalPort}:5432"
  Write-Output "Opened a localhost-only connection on port $LocalPort in a new window; keep it open."
}

$encoded = kubectl get secret postgres-secret -n $Namespace -o jsonpath='{.data.VIEWER_PASSWORD}'
if ($LASTEXITCODE -ne 0 -or -not $encoded) { throw 'VIEWER_PASSWORD not found; run scripts/up.ps1 first.' }
$password = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encoded))
try {
  if ($password -match '[|=]') { throw 'Viewer password contains DBeaver separator characters; delete VIEWER_PASSWORD from postgres-secret and rerun up.ps1.' }
  $connection = "driver=postgres-jdbc|host=localhost|port=$LocalPort|database=incidents|user=db_viewer|password=$password" +
    "|name=OnCallBot (read-only)|folder=OnCallBot|savePassword=true|save=true|connect=true|prop.readOnly=true"
  Start-Process -FilePath $DBeaverPath -ArgumentList @('-con', "`"$connection`"")
} finally { $password = $null; $connection = $null; $encoded = $null }
Write-Output 'DBeaver connection "OnCallBot (read-only)" sent to DBeaver. Schemas: orders, inventory, payments, code_index, investigator.'
