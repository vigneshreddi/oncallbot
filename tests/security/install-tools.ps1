param([string]$Destination = (Join-Path ([IO.Path]::GetTempPath()) 'incident-lab-security-tools'))
$ErrorActionPreference = 'Stop'
$windows = $env:OS -eq 'Windows_NT'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
New-Item -ItemType Directory -Force $Destination | Out-Null
$tools = @(
  @{ name = 'gitleaks'; repo = 'gitleaks/gitleaks'; version = '8.24.2'; windows = 'windows_x64.zip'; linux = 'linux_x64.tar.gz'; sums = 'gitleaks_8.24.2_checksums.txt' },
  @{ name = 'trivy'; repo = 'aquasecurity/trivy'; version = '0.75.0'; windows = 'windows-64bit.zip'; linux = 'Linux-64bit.tar.gz'; sums = 'trivy_0.75.0_checksums.txt' }
)
foreach ($tool in $tools) {
  $suffix = $tool.linux
  if ($windows) { $suffix = $tool.windows }
  $asset = "$($tool.name)_$($tool.version)_$suffix"
  $base = "https://github.com/$($tool.repo)/releases/download/v$($tool.version)"
  $archive = Join-Path $Destination $asset
  $checksums = Join-Path $Destination $tool.sums
  Invoke-WebRequest -UseBasicParsing "$base/$asset" -OutFile $archive
  Invoke-WebRequest -UseBasicParsing "$base/$($tool.sums)" -OutFile $checksums
  $entry = @(Get-Content $checksums | Where-Object { ($_ -split '\s+')[-1] -eq $asset })
  if ($entry.Count -ne 1) { throw "Missing or ambiguous checksum for $asset" }
  $expected = ($entry[0] -split '\s+')[0]
  if ((Get-FileHash $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected.ToLowerInvariant()) {
    throw "Checksum mismatch for $asset"
  }
  if ($windows) { Expand-Archive $archive -DestinationPath $Destination -Force }
  else {
    & tar -xzf $archive -C $Destination $tool.name
    if ($LASTEXITCODE -ne 0) { throw "Extraction failed for $asset" }
  }
}
Write-Output $Destination