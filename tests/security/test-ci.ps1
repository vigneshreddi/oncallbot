$ErrorActionPreference = 'Stop'
$root = Resolve-Path "$PSScriptRoot/../.."
$workflow = Get-Content "$root/.github/workflows/security.yml" -Raw
foreach ($required in @('contents: read', 'persist-credentials: false', 'pip-audit==2.9.0', '--strict', 'dependency-check-maven:12.1.0:check', '-DfailBuildOnCVSS=7', '-DfailOnError=true', 'gitleaks git', 'gitleaks dir', '--redact', '--severity HIGH,CRITICAL', '--exit-code 1', 'cyclonedx', 'if: always()', 'security-gate:', 'fail-fast: false')) {
  if (-not $workflow.Contains($required)) { throw "Missing CI control: $required" }
}
foreach ($action in [regex]::Matches($workflow, 'uses:\s*(\S+)')) {
  if ($action.Groups[1].Value -notmatch '@[a-f0-9]{40}$') { throw "Action is not SHA pinned: $action" }
}
if ($workflow -match 'continue-on-error|pull_request_target|ignore-unfixed|secrets\.|contents:\s*write|security-events:\s*write') {
  throw 'Unsafe CI permission, secret use or vulnerability bypass'
}
$installer = Get-Content "$root/tests/security/install-tools.ps1" -Raw
foreach ($required in @("version = '8.24.2'", "version = '0.75.0'", 'Get-FileHash', 'Checksum mismatch')) {
  if (-not $installer.Contains($required)) { throw "Scanner pin/checksum missing: $required" }
}
$postgres = Get-Content "$root/deploy/k8s/postgres.yaml" -Raw
$databaseImage = [regex]::Match($postgres, '(?m)^\s*image:\s*(\S+)').Groups[1].Value
if (-not $databaseImage -or -not $workflow.Contains("image: $databaseImage")) { throw 'CI database image differs from deployment' }
if (-not $workflow.Contains('install-maven.ps1')) { throw 'Maven CLI not pinned' }
foreach ($component in @('order-service', 'inventory-service', 'payment-service', 'demo-service', 'investigator', 'chaos', 'indexer', 'postgres')) {
  if (-not $workflow.Contains("name: $component")) { throw "Image coverage missing: $component" }
}
foreach ($component in @('order-service', 'inventory-service', 'payment-service', 'demo-service')) {
  $ignore = Get-Content "$root/services/$component/.dockerignore" -Raw
  foreach ($rule in @('**', '!pom.xml', '!src/**', '**/.env*', '**/application-local.*', '**/application-secret*')) {
    if (-not $ignore.Contains($rule)) { throw "Missing $component context exclusion: $rule" }
  }
}
foreach ($ignorePath in @('.dockerignore', 'investigator/.dockerignore', 'indexer/Dockerfile.dockerignore', 'tests/Dockerfile.rag.dockerignore')) {
  $ignore = Get-Content "$root/$ignorePath" -Raw
  if (-not $ignore.Contains('**/.env')) { throw "Missing credential exclusion: $ignorePath" }
}
Write-Output 'PASS: CI scanner pins, failure thresholds, permissions, SBOM and image/context coverage.'