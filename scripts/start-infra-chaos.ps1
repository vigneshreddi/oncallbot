param(
    [switch]$Enable,
    [switch]$DryRun,
    [switch]$Restore,
    [switch]$ValidateOnly,
    [ValidateSet('scale-zero', 'pod-delete', 'oom', 'node-unavailable-simulated')]
    [string]$Scenario,
    [ValidateSet('order-service', 'inventory-service', 'payment-service')]
    [string]$Service,
    [ValidateRange(5, 120)][int]$DurationSeconds = 60,
    [string]$Context,
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
if ($ValidateOnly -and ($Enable -or $DryRun -or $Restore -or $Scenario -or $Service)) {
    throw '-ValidateOnly cannot be combined with scenario execution.'
}
if ($Restore -and ($Enable -or $DryRun -or $Scenario -or $Service)) {
    throw '-Restore cannot be combined with scenario execution.'
}
if (-not $Enable -and -not $DryRun -and -not $Restore -and -not $ValidateOnly) {
    Write-Output 'Infrastructure chaos is disabled. Use -DryRun, -ValidateOnly, or explicitly -Enable.'
    return
}
if (-not $Context) {
    $Context = kubectl config current-context
    if ($LASTEXITCODE -ne 0 -or -not $Context) { throw 'Explicit Kubernetes context required.' }
}
if ($ValidateOnly) {
    & kubectl --context $Context apply --dry-run=server -f (Join-Path $root 'deploy/k8s/infra-chaos-rbac.yaml') -f (Join-Path $root 'deploy/k8s/demo-fault-workloads.yaml')
    if ($LASTEXITCODE -ne 0) { throw 'Infrastructure manifest server dry-run failed.' }
    return
}
if (-not $Restore -and -not $Scenario) { throw '-Scenario is required.' }
if (($Scenario -eq 'scale-zero' -and -not $Service) -or ($Scenario -ne 'scale-zero' -and $Service)) {
    throw '-Service is required only for scale-zero.'
}
$runnerArgs = @((Join-Path $root 'chaos/runner/infra_runner.py'), '--context', $Context,
    '--state', (Join-Path $root 'tmp/infra-chaos-state.json'), '--duration', "$DurationSeconds")
if ($Scenario) { $runnerArgs += @('--scenario', $Scenario) }
if ($Service) { $runnerArgs += @('--service', $Service) }
if ($Restore) { $runnerArgs += '--restore' }
elseif ($DryRun) { $runnerArgs += '--dry-run' }
elseif ($Enable) { $runnerArgs += '--enable' }
& $Python @runnerArgs
if ($LASTEXITCODE -ne 0) {
    throw 'Infrastructure scenario failed. If a recovery journal remains, run -Restore with the same context; do not delete it.'
}