param([string]$Destination = (Join-Path ([IO.Path]::GetTempPath()) 'incident-lab-maven'))
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
New-Item -ItemType Directory -Force $Destination | Out-Null
$suffix = 'tar.gz'
if ($env:OS -eq 'Windows_NT') { $suffix = 'zip' }
$asset = "apache-maven-3.9.9-bin.$suffix"
$base = 'https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/3.9.9'
$archive = Join-Path $Destination $asset
Invoke-WebRequest -UseBasicParsing "$base/$asset" -OutFile $archive
$checksum = (Invoke-WebRequest -UseBasicParsing "$base/$asset.sha512").Content.Trim()
if ($checksum -notmatch '^[a-fA-F0-9]{128}$' -or (Get-FileHash $archive -Algorithm SHA512).Hash -ne $checksum) {
  throw 'Maven checksum mismatch'
}
if ($suffix -eq 'zip') { Expand-Archive $archive -DestinationPath $Destination -Force }
else {
  & tar -xzf $archive -C $Destination
  if ($LASTEXITCODE -ne 0) { throw 'Maven extraction failed' }
}
Write-Output (Join-Path $Destination 'apache-maven-3.9.9/bin')