param(
  [Parameter(Mandatory)][int]$IncidentId,
  [string]$BaseUrl = 'http://localhost:18090',
  [string]$OutDir = 'logs',
  [switch]$NoClipboard
)
$ErrorActionPreference = 'Stop'
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
  New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
  $response = Invoke-WebRequest -Uri "$BaseUrl/incidents/$IncidentId/handoff" -UseBasicParsing
  $utf8 = New-Object Text.UTF8Encoding $false
  $brief = if ($response.Content -is [byte[]]) { $utf8.GetString($response.Content) } else { $response.Content }
  $path = Join-Path (Get-Location) (Join-Path $OutDir "incident-$IncidentId-handoff.md")
  [IO.File]::WriteAllText($path, $brief, $utf8)
  Write-Output "Saved $path ($($brief.Length) characters)."
  if (-not $NoClipboard) {
    Set-Clipboard -Value $brief
    Write-Output 'Copied to the clipboard: paste it into Copilot Chat and ask it to continue.'
  }
} finally { Pop-Location }
