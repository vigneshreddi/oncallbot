param(
  [Parameter(Mandatory)][string]$Fault,
  [int]$OrderPort = 18080,
  [int]$InventoryPort = 18081,
  [int]$PaymentPort = 18082,
  [ValidateRange(0, 86400)][int]$DurationSeconds = 120,
  [string]$Catalog = 'chaos/faults.yaml',
  [string]$Python = 'python',
  [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
if (-not [System.IO.Path]::IsPathRooted($Catalog)) { $Catalog = Join-Path $root $Catalog }
$runnerArgs = @(
  (Join-Path $root 'chaos/runner/chaos_runner.py'),
  '--catalog', $Catalog,
  '--fault', $Fault,
  '--base-url', "order-service=http://localhost:$OrderPort",
  '--base-url', "inventory-service=http://localhost:$InventoryPort",
  '--base-url', "payment-service=http://localhost:$PaymentPort",
  '--duration', $DurationSeconds,
  '--events', (Join-Path $root 'tmp/chaos-events.jsonl')
)
if ($DryRun) { $runnerArgs += '--dry-run' }
Write-Output "Using existing port-forwards: order $OrderPort, inventory $InventoryPort, payment $PaymentPort."
Write-Output 'Python requires chaos/requirements.txt. Each run attempts cleanup before exiting.'
& $Python @runnerArgs
if ($LASTEXITCODE -ne 0) { throw "Scenario failed (exit $LASTEXITCODE); inspect tmp/chaos-events.jsonl for cleanup status." }
