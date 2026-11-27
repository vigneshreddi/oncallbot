param([string]$Namespace = 'incident-lab', [string]$PythonExecutable = 'python')
$ErrorActionPreference = 'Stop'
if ($Namespace -ne 'incident-lab') { throw 'The existing investigator manifest is scoped to incident-lab.' }
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
  $configJson = kubectl get configmap services-config -n $Namespace -o json
  if ($LASTEXITCODE -ne 0) { throw 'Cannot read deployed services config; investigator was not deployed.' }
  $configuredJson = $configJson | & $PythonExecutable -c "import json,sys,yaml; print(json.dumps(yaml.safe_load(json.load(sys.stdin)['data']['services.yaml'])['services']))"
  if ($LASTEXITCODE -ne 0) { throw 'Cannot parse deployed services config; investigator was not deployed.' }
  $configured = $configuredJson | ConvertFrom-Json
  if (-not $configured) { throw 'No configured services; investigator was not deployed.' }
  $storage = kubectl exec -n $Namespace postgres-0 -- psql -U app -d incidents -X -q -A -t -v ON_ERROR_STOP=1 -c "BEGIN READ ONLY; SELECT count(*)=5 AND EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector') FROM unnest(ARRAY['rag_indexes','rag_sources','rag_chunks','rag_embedding_cache','rag_vectors']) AS names(name) WHERE to_regclass('code_index.' || name) IS NOT NULL; COMMIT;"
  if ($LASTEXITCODE -ne 0 -or ($storage -join '').Trim() -ne 't') {
    throw 'PostgreSQL/RAG bootstrap is not ready; investigator was not deployed.'
  }
  $readyJson = kubectl exec -n $Namespace postgres-0 -- psql -U app -d incidents -X -q -A -t -v ON_ERROR_STOP=1 -c "BEGIN READ ONLY; SELECT coalesce(json_agg(json_build_object('service',service,'commit_sha',commit_sha)), '[]'::json) FROM code_index.rag_indexes WHERE chunk_count>0; COMMIT;"
  if ($LASTEXITCODE -ne 0) { throw 'Cannot read ready RAG indexes; investigator was not deployed.' }
  $ready = $readyJson | ConvertFrom-Json
  foreach ($service in $configured) {
    if (-not ($ready | Where-Object { $_.service -ceq $service.name -and $_.commit_sha -ceq $service.prod_commit })) {
      throw "No ready RAG index for $($service.name)@$($service.prod_commit); run index-code.ps1 first. Investigator was not deployed."
    }
  }
  kubectl apply -n $Namespace -f .\deploy\k8s\investigator.yaml
  if ($LASTEXITCODE -ne 0) { throw 'Cannot deploy investigator.' }
  kubectl rollout restart deployment/investigator -n $Namespace
  if ($LASTEXITCODE -ne 0) { throw 'Cannot restart investigator.' }
  kubectl rollout status deployment/investigator -n $Namespace --timeout=300s
  if ($LASTEXITCODE -ne 0) { throw 'Investigator rollout failed.' }
  kubectl exec -n $Namespace deployment/investigator -- python -c "import json; from investigator.retrieval import index_status; rows=index_status(); print(json.dumps(rows, default=str)); raise SystemExit(0 if rows and all(row['ready'] for row in rows) else 1)"
  if ($LASTEXITCODE -ne 0) { throw 'Investigator is running but a configured commit has no ready RAG index; run index-code.ps1 first.' }
} finally { Pop-Location }