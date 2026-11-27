param(
	[switch]$Enable,
	[switch]$DryRun,
	[string]$Seed = '',
	[ValidateRange(0, 86400)][int]$CooldownSeconds = 180,
	[string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
if ($DryRun) {
	$runnerArgs = @((Join-Path $root 'chaos/runner/chaos_runner.py'), '--catalog', (Join-Path $root 'chaos/faults.yaml'), '--dry-run')
	if ($Seed) { $runnerArgs += @('--seed', $Seed) }
	& $Python @runnerArgs
	if ($LASTEXITCODE -ne 0) { throw 'Chaos dry-run failed.' }
	return
}
if (-not $Enable) {
	Write-Output 'Chaos is disabled by default. Pass -Enable to deploy active chaos, or -DryRun for a local HTTP plan.'
	return
}
docker build -t docker.io/library/incident-lab-chaos:local (Join-Path $root 'chaos')
if ($LASTEXITCODE -ne 0) { throw 'Chaos image build failed.' }
kubectl apply -f (Join-Path $root 'deploy/k8s/chaos.yaml')
if ($LASTEXITCODE -ne 0) { throw 'Chaos deployment apply failed.' }
kubectl set env deployment/chaos-runner -n incident-lab 'CHAOS_ENABLED=true' 'CHAOS_DRY_RUN=false' "CHAOS_SEED=$Seed" "CHAOS_COOLDOWN_SECONDS=$CooldownSeconds"
if ($LASTEXITCODE -ne 0) { throw 'Chaos environment update failed.' }
kubectl scale deployment/chaos-runner -n incident-lab --replicas=1
if ($LASTEXITCODE -ne 0) { throw 'Chaos deployment scale failed.' }
kubectl rollout status deployment/chaos-runner -n incident-lab --timeout=120s
if ($LASTEXITCODE -ne 0) { throw 'Chaos runner did not become ready.' }
