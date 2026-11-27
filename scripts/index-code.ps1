param(
	[string]$Namespace = 'incident-lab',
	[string]$ConfigPath = '',
	[string]$PythonExecutable = 'python',
	[string]$Image = 'docker.io/library/incident-lab-indexer:local',
	[string]$JobName = 'code-indexer',
	[string]$KindCluster = '',
	[switch]$PushImage
)
$ErrorActionPreference = 'Stop'
function Run {
	param([Parameter(ValueFromRemainingArguments)]$Command)
	$executable = $Command[0]
	$arguments = @($Command | Select-Object -Skip 1)
	& $executable @arguments
	if ($LASTEXITCODE -ne 0) { throw "Command failed: $executable (exit $LASTEXITCODE)" }
}

Push-Location (Split-Path $PSScriptRoot -Parent)
$previousRoot = $env:SOURCE_ROOT
$previousConfig = $env:SERVICES_CONFIG
$previousManifest = $env:SOURCE_MANIFEST_B64
try {
	if (-not $ConfigPath) { $ConfigPath = if (Test-Path 'config/services.deployed.yaml') { 'config/services.deployed.yaml' } else { 'config/services.yaml' } }
	$resolvedConfig = (Resolve-Path $ConfigPath).Path
	$env:SOURCE_ROOT = (Get-Location).Path
	$env:SERVICES_CONFIG = $resolvedConfig
	$env:SOURCE_MANIFEST_B64 = ''
	$manifest = & $PythonExecutable -m indexer.index_code manifest
	if ($LASTEXITCODE -ne 0) { throw 'Commit/source verification failed; image was not built.' }
	$encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($manifest -join "`n")))
	Run docker build -f .\indexer\Dockerfile --build-arg "SOURCE_MANIFEST_B64=$encoded" -t $Image .
	if ($PushImage) {
		Run docker push $Image
	} else {
		if (-not $KindCluster) {
			$context = kubectl config current-context
			if ($LASTEXITCODE -ne 0) { throw 'Cannot identify the Kubernetes context.' }
			if ($context -like 'kind-*') { $KindCluster = $context.Substring(5) }
		}
		if ($KindCluster) { Run kind load docker-image $Image --name $KindCluster }
	}
	$configName = "$JobName-services"
	$configMap = kubectl create configmap $configName -n $Namespace "--from-file=services.yaml=$resolvedConfig" --dry-run=client -o json
	if ($LASTEXITCODE -ne 0) { throw 'Cannot generate indexer ConfigMap.' }
	$configMap | kubectl apply -f -
	if ($LASTEXITCODE -ne 0) { throw 'Cannot apply indexer ConfigMap.' }
	$jobJson = kubectl create --dry-run=client -f .\deploy\k8s\indexer-job.yaml -o json
	if ($LASTEXITCODE -ne 0) { throw 'Cannot read the existing indexer Job template.' }
	$job = $jobJson | ConvertFrom-Json
	$job.metadata.name = $JobName
	$job.metadata.namespace = $Namespace
	$job.spec.template.spec.containers[0].image = $Image
	foreach ($volume in $job.spec.template.spec.volumes) {
		if ($volume.name -eq 'services-config') { $volume.configMap.name = $configName }
	}
	Run kubectl delete job $JobName -n $Namespace --ignore-not-found --wait=true
	$job | ConvertTo-Json -Depth 30 | kubectl apply -f -
	if ($LASTEXITCODE -ne 0) { throw 'Cannot apply indexer Job.' }
	kubectl wait --for=condition=complete "job/$JobName" -n $Namespace --timeout=600s
	if ($LASTEXITCODE -ne 0) {
		kubectl logs -n $Namespace "job/$JobName"
		throw 'Indexer failed; deployed service commit metadata was not changed.'
	}
	Run kubectl logs -n $Namespace "job/$JobName"
} finally {
	$env:SOURCE_ROOT = $previousRoot
	$env:SERVICES_CONFIG = $previousConfig
	$env:SOURCE_MANIFEST_B64 = $previousManifest
	Pop-Location
}
