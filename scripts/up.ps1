param([switch]$ValidateOnly, [switch]$StrictIsolation)
$ErrorActionPreference = 'Stop'
$namespace = 'incident-lab'
function Run {
  $Command = @($args)
  $executable = $Command[0]; $arguments = @($Command | Select-Object -Skip 1)
  $previousPreference = $ErrorActionPreference
  try {
    $ErrorActionPreference = 'Continue'
    & $executable @arguments 2>&1 | ForEach-Object { Write-Output "$_" }
    $exitCode = $LASTEXITCODE
  } finally { $ErrorActionPreference = $previousPreference }
  if ($exitCode -ne 0) { throw "Command failed: $executable (exit $exitCode)" }
}

Push-Location (Split-Path $PSScriptRoot -Parent)
try {
  $existingNamespace = kubectl get namespace $namespace --ignore-not-found -o name
  if ($LASTEXITCODE -ne 0) { throw 'Cluster unavailable; deployment stopped.' }
  if (-not $existingNamespace) {
    $namespaceManifest = @{apiVersion='v1'; kind='Namespace'; metadata=@{name=$namespace}} | ConvertTo-Json -Depth 4
    $namespaceManifest | kubectl apply --dry-run=server -f -
    if ($LASTEXITCODE -ne 0) { throw 'Namespace server dry-run failed.' }
    if ($ValidateOnly) { throw 'Namespace absent: namespaced server dry-runs require it. No resources were created.' }
    $namespaceManifest | kubectl apply -f -
    if ($LASTEXITCODE -ne 0) { throw 'Namespace creation failed.' }
  }
  Run kubectl apply --dry-run=server -f .\deploy\k8s\postgres.yaml -f .\deploy\k8s\network-policy.yaml -f .\deploy\k8s\services.yaml -f .\deploy\k8s\investigator.yaml -f .\deploy\k8s\indexer-job.yaml -f .\deploy\k8s\chaos.yaml
  & .\scripts\load-env-secret.ps1 -Namespace $namespace -ProvisionDatabase -ValidateOnly
  $servicesConfig = if (Test-Path .\config\services.deployed.yaml) { '.\config\services.deployed.yaml' } else { '.\config\services.yaml' }
  Write-Output "Service registry: $servicesConfig"
  $config = kubectl create configmap services-config -n $namespace "--from-file=services.yaml=$servicesConfig" --dry-run=client -o json
  if ($LASTEXITCODE -ne 0) { throw 'ConfigMap generation failed.' }
  $config | kubectl apply --dry-run=server -f -
  if ($LASTEXITCODE -ne 0) { throw 'ConfigMap server dry-run failed.' }
  if ($ValidateOnly) { Write-Output 'All deployment server dry-runs passed; no resources changed.'; return }

  $initializers = @(
    '.\services\order-service\src\main\java\dev\incidentlab\orders\OrderSchema.java',
    '.\services\inventory-service\src\main\java\dev\incidentlab\inventory\InventorySchema.java',
    '.\services\payment-service\src\main\java\dev\incidentlab\payments\PaymentSchema.java',
    '.\investigator\investigator\store.py', '.\indexer\index_code.py'
  )
  if ((Select-String -Path $initializers -Pattern '(?i)create schema') -or
      (Select-String -Path '.\investigator\investigator\store.py' -Pattern '(?i)create table if not exists code_index')) {
    throw 'Deployment blocked: application initializers still create schemas and investigator still creates code_index tables. Move that DDL to bootstrap before deploying separated roles; broad CREATE grants are intentionally denied.'
  }
  Run docker run --rm --entrypoint sh pgvector/pgvector:pg17@sha256:ac08538c6f8b9904c33c8224c5e5706dbe760aca29db1d096972b4052c22a75d -c 'id -u postgres | grep -qx 999 && id -g postgres | grep -qx 999'
  & .\scripts\load-env-secret.ps1 -Namespace $namespace -ProvisionDatabase
  $config | kubectl apply -f -
  if ($LASTEXITCODE -ne 0) { throw 'ConfigMap apply failed.' }
  Run kubectl label node desktop-control-plane incidentlab/node-group=control --overwrite
  Run kubectl label node desktop-worker incidentlab/node-group=worker --overwrite
  Run kubectl apply -f .\deploy\k8s\network-policy.yaml
  Run kubectl apply -f .\deploy\k8s\postgres.yaml
  Run kubectl rollout status statefulset/postgres -n $namespace --timeout=180s
  Run kubectl wait --for=condition=ready pod/postgres-0 -n $namespace --timeout=180s
  & .\scripts\ensure-db-roles.ps1 -Namespace $namespace

  Run docker build -q -t docker.io/library/incident-lab-order-service:local .\services\order-service
  Run docker build -q -t docker.io/library/incident-lab-inventory-service:local .\services\inventory-service
  Run docker build -q -t docker.io/library/incident-lab-payment-service:local .\services\payment-service
  Run docker build -q -t docker.io/library/incident-lab-investigator:local .\investigator
  Run kubectl apply -f .\deploy\k8s\services.yaml
  Run kubectl rollout restart deployment/order-service deployment/inventory-service deployment/payment-service -n $namespace
  foreach ($app in 'order-service','inventory-service','payment-service') {
    Run kubectl rollout status deployment/$app -n $namespace --timeout=300s
  }
  if ($StrictIsolation) { & .\scripts\security-check.ps1 } else { & .\scripts\security-check.ps1 -AllowUnverifiedIsolation }
  Run kubectl get pods -n $namespace -o wide
  Write-Output 'Investigator was not deployed or restarted. Run scripts/index-code.ps1, then scripts/start-investigator.ps1.'
} finally { Pop-Location }
