param(
  [int]$IncidentId = 0,
  [string]$BaseUrl = 'http://localhost:18090',
  [string]$OutDir = 'logs'
)
$ErrorActionPreference = 'Stop'
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
  New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
  $incidents = Invoke-RestMethod -Uri "$BaseUrl/incidents" -UseBasicParsing
  if ($IncidentId) { $incidents = @($incidents | Where-Object { $_.id -eq $IncidentId }) }
  if (-not $incidents) { throw 'No matching incidents. Is the investigator connection on port 18090 open?' }
  $utf8 = New-Object Text.UTF8Encoding $false
  foreach ($incident in $incidents) {
    $response = Invoke-WebRequest -Uri "$BaseUrl/incidents/$($incident.id)/audit" -UseBasicParsing
    $body = if ($response.Content -is [byte[]]) { $utf8.GetString($response.Content) } else { $response.Content }
    $path = Join-Path $OutDir ("incident-{0}-{1}.jsonl" -f $incident.id, $incident.service)
    [IO.File]::WriteAllText((Join-Path (Get-Location) $path), $body, $utf8)
    $lines = @($body -split "`n" | Where-Object { $_ })
    $chunks = @($lines | Where-Object { $_ -like '{"type": "code_chunk_sent"*' }).Count
    $rounds = @($lines | Where-Object { $_ -like '{"type": "llm_request"*' }).Count
    Write-Output ("{0}: {1} records, {2} AI rounds, {3} code pieces sent ({4})" -f $path, $lines.Count, $rounds, $chunks, $incident.status)
  }
} finally { Pop-Location }
