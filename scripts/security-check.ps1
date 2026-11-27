param(
  [string]$Namespace = 'incident-lab',
  [switch]$Static,
  [string]$EnforcementEvidence,
  [switch]$AllowUnverifiedIsolation,
  [switch]$FunctionsOnly
)

$ErrorActionPreference = 'Stop'

function ConvertTo-StableJson {
  param($Value)
  if ($null -eq $Value) { return 'null' }
  if ($Value -is [System.Collections.IDictionary] -or $Value -is [pscustomobject]) {
    $names = if ($Value -is [System.Collections.IDictionary]) { @($Value.Keys) } else { @($Value.PSObject.Properties.Name) }
    $entries = foreach ($name in ($names | Sort-Object -CaseSensitive)) {
      $key = ConvertTo-Json -InputObject ([string]$name) -Compress
      $child = if ($Value -is [System.Collections.IDictionary]) { $Value[$name] } else { $Value.$name }
      $key + ':' + (ConvertTo-StableJson $child)
    }
    return '{' + ($entries -join ',') + '}'
  }
  if ($Value -is [System.Collections.IEnumerable] -and $Value -isnot [string]) {
    $entries = foreach ($child in $Value) { ConvertTo-StableJson $child }
    return '[' + ($entries -join ',') + ']'
  }
  return (ConvertTo-Json -InputObject $Value -Depth 100 -Compress)
}

function Get-PolicySha256 {
  param($Policies)
  $identities = @($Policies.items | Sort-Object { $_.metadata.name } | ForEach-Object {
    @{ name = $_.metadata.name; namespace = $_.metadata.namespace; uid = $_.metadata.uid; spec = $_.spec }
  })
  $sha = [Security.Cryptography.SHA256]::Create()
  try {
    $bytes = [Text.Encoding]::UTF8.GetBytes((ConvertTo-StableJson $identities))
    return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
  } finally { $sha.Dispose() }
}

function Get-RunningPodUids {
  param($Pods)
  return @($Pods.items | Where-Object { $_.status.phase -eq 'Running' -and -not $_.metadata.deletionTimestamp } |
    ForEach-Object { $_.metadata.uid } | Sort-Object)
}

function Test-ClusterSnapshot {
  param($Snapshot, $Evidence)
  $failures = [System.Collections.Generic.List[string]]::new()
  foreach ($service in $Snapshot.services.items) {
    if ($service.spec.type -ne 'ClusterIP' -or $service.spec.externalIPs -or $service.spec.loadBalancerIP) {
      $failures.Add("Externally exposed Service: $($service.metadata.name)")
    }
  }
  if (@($Snapshot.ingresses.items).Count -gt 0) { $failures.Add('Ingress resources exist') }
  $policy = @($Snapshot.policies.items | Where-Object { $_.metadata.name -eq 'deny-external-ingress' })
  if ($policy.Count -ne 1) { $failures.Add('deny-external-ingress policy missing') }
  else {
    if ($policy[0].spec.podSelector.matchLabels -or $policy[0].spec.podSelector.matchExpressions) {
      $failures.Add('Ingress policy does not select every pod')
    }
    if ($policy[0].spec.policyTypes -notcontains 'Ingress') { $failures.Add('Ingress policy type missing') }
  }
  foreach ($networkPolicy in $Snapshot.policies.items) {
    if ($networkPolicy.spec.policyTypes -contains 'Egress' -or $null -ne $networkPolicy.spec.egress) {
      $failures.Add("Outbound traffic is restricted by policy: $($networkPolicy.metadata.name)")
    }
  }
  if (@($Snapshot.pods.items).Count -eq 0) { $failures.Add('No workload pods available to inspect') }
  foreach ($pod in $Snapshot.pods.items) {
    if ($pod.spec.hostNetwork -or $pod.spec.hostPID -or $pod.spec.hostIPC) { $failures.Add("Host namespace: $($pod.metadata.name)") }
    foreach ($volume in $pod.spec.volumes) {
      if ($null -ne $volume.hostPath) { $failures.Add("Host mount: $($pod.metadata.name)") }
    }
    $containers = @($pod.spec.containers) + @($pod.spec.initContainers) + @($pod.spec.ephemeralContainers)
    foreach ($container in $containers) {
      if ($null -eq $container) { continue }
      $label = "$($pod.metadata.name)/$($container.name)"
      $security = $container.securityContext
      $podSecurity = $pod.spec.securityContext
      if ($security.privileged) { $failures.Add("Privileged: $label") }
      if ($security.allowPrivilegeEscalation -ne $false) { $failures.Add("Privilege escalation not disabled: $label") }
      if ($security.readOnlyRootFilesystem -ne $true) { $failures.Add("Writable root filesystem: $label") }
      $nonRoot = $security.runAsNonRoot
      if ($null -eq $nonRoot) { $nonRoot = $podSecurity.runAsNonRoot }
      $uid = $security.runAsUser
      if ($null -eq $uid) { $uid = $podSecurity.runAsUser }
      if ($nonRoot -ne $true -or $null -eq $uid -or $uid -le 0) { $failures.Add("Non-root UID not enforced: $label") }
      if ($security.capabilities.drop -notcontains 'ALL' -or $security.capabilities.add) { $failures.Add("Capabilities not dropped: $label") }
      $seccomp = $security.seccompProfile.type
      if (-not $seccomp) { $seccomp = $podSecurity.seccompProfile.type }
      if ($seccomp -ne 'RuntimeDefault') { $failures.Add("RuntimeDefault seccomp missing: $label") }
      foreach ($port in $container.ports) {
        if ($port.hostPort) { $failures.Add("Host port: $label") }
      }
    }
  }
  $cniImages = @($Snapshot.systemPods.items | ForEach-Object { $_.spec.containers } | ForEach-Object { $_.image } | Where-Object { $_ -match '(cilium|calico|antrea|kube-router)' })
  if ($cniImages.Count -eq 0) { $failures.Add('CNI NetworkPolicy enforcement unknown: no recognized policy engine observed') }
  if ($null -eq $Evidence) { $failures.Add('CNI enforcement unverified: independent connectivity probe evidence required') }
  else {
    $observed = [DateTimeOffset]::MinValue
    if (-not [DateTimeOffset]::TryParse([string]$Evidence.observedAt, [ref]$observed) -or
        $observed -lt [DateTimeOffset]::UtcNow.AddHours(-1) -or $observed -gt [DateTimeOffset]::UtcNow.AddMinutes(5)) {
      $failures.Add('Enforcement evidence is stale or has an invalid timestamp')
    }
    if ($Evidence.context -ne $Snapshot.context -or $Evidence.namespace -ne $Snapshot.namespace -or
        $Evidence.policySha256 -ne $Snapshot.policySha256 -or
        ($Evidence.nodeUids -join ',') -ne ($Snapshot.nodeUids -join ',')) {
      $failures.Add('Enforcement evidence does not match cluster nodes, context, namespace and policies')
    }
    foreach ($probe in @('outsideNamespaceDenied', 'externalHostDenied', 'sameNamespaceAllowed', 'outboundInternetAllowed', 'localhostPortForwardAllowed')) {
      if ($Evidence.$probe -ne $true) { $failures.Add("Enforcement probe missing or failed: $probe") }
    }
    if (-not $Evidence.cni -or -not $Evidence.probeDetails -or -not $Evidence.targetPodUids -or
        ($Evidence.targetPodUids -join ',') -ne ($Snapshot.podUids -join ',')) {
      $failures.Add('Evidence needs CNI configuration, probe details and matching target pod UIDs')
    }
  }
  return $failures.ToArray()
}

function Invoke-KubeJson {
  param([string[]]$Arguments)
  $result = & kubectl @Arguments
  if ($LASTEXITCODE -ne 0) { throw "kubectl failed: $($Arguments -join ' ')" }
  return ($result | ConvertFrom-Json)
}

function Test-RepositorySecurity {
  param([string]$Root)
  $tracked = & git -C $Root ls-files --full-name -- .
  if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect tracked files; git metadata required' }
  if ($tracked -match '(^|/)(\.env($|\.(?!example$))|[^/]+\.(pem|key|p12|pfx|jks|token|secret|secrets)$)') {
    throw 'Credential-like file is tracked; inspect with gitleaks before proceeding'
  }
  & git -C $Root check-ignore -q -- .env
  if ($LASTEXITCODE -ne 0) { throw '.env must be git-ignored' }
  & git -C $Root check-ignore -q -- .env.security-test
  if ($LASTEXITCODE -ne 0) { throw '.env variants must be git-ignored' }
  foreach ($script in Get-ChildItem (Join-Path $Root 'scripts') -Filter '*.ps1') {
    $content = Get-Content $script.FullName -Raw
    if ($content -match '(?i)--address(?:=|\s+)[\x27\x22]?(0\.0\.0\.0|::|\*)') {
      throw "Non-localhost port-forward address in $($script.Name)"
    }
  }
  Write-Output 'Repository hygiene passed; this is not a substitute for gitleaks or vulnerability scans.'
}

if ($FunctionsOnly) { return }
$root = Split-Path $PSScriptRoot -Parent
Test-RepositorySecurity $root
if ($Static) { return }
$context = & kubectl config current-context
if ($LASTEXITCODE -ne 0) { throw 'Cannot identify Kubernetes context' }
$policies = Invoke-KubeJson @('get', 'networkpolicy', '-n', $Namespace, '-o', 'json')
$policyHash = Get-PolicySha256 $policies
$nodes = Invoke-KubeJson @('get', 'nodes', '-o', 'json')
$pods = Invoke-KubeJson @('get', 'pods', '-n', $Namespace, '-o', 'json')
$snapshot = [pscustomobject]@{
  context = $context; namespace = $Namespace; policySha256 = $policyHash
  nodeUids = @($nodes.items.metadata.uid | Sort-Object)
  podUids = @(Get-RunningPodUids $pods)
  services = Invoke-KubeJson @('get', 'services', '-n', $Namespace, '-o', 'json')
  ingresses = Invoke-KubeJson @('get', 'ingresses', '-n', $Namespace, '-o', 'json')
  policies = $policies; pods = $pods
  systemPods = Invoke-KubeJson @('get', 'pods', '-n', 'kube-system', '-o', 'json')
}
$evidence = $null
if ($EnforcementEvidence) { $evidence = Get-Content $EnforcementEvidence -Raw | ConvertFrom-Json }
Write-Output "Context: $context; namespace: $Namespace; policy SHA256: $policyHash"
Write-Output "Node UIDs: $($snapshot.nodeUids -join ','); target pod UIDs: $($snapshot.podUids -join ',')"
Write-Output "kube-system images: $(@($snapshot.systemPods.items.spec.containers.image | Sort-Object -Unique) -join ', ')"
$failures = @(Test-ClusterSnapshot $snapshot $evidence)
$warnings = @()
if ($AllowUnverifiedIsolation -and -not $EnforcementEvidence) {
  $warnings = @($failures | Where-Object { $_ -like 'CNI *' })
  $failures = @($failures | Where-Object { $_ -notlike 'CNI *' })
}
foreach ($warning in $warnings) { Write-Warning $warning }
foreach ($failure in $failures) { Write-Output "FAIL: $failure" }
if ($failures.Count) { throw "Security gate failed ($($failures.Count) findings). No isolation approval issued." }
if ($warnings.Count) {
  Write-Warning 'Network isolation is NOT proven: this cluster has no recognized NetworkPolicy engine (Docker Desktop uses kindnet). Services remain ClusterIP-only. Acceptable for a local demo; do not use on a shared or public machine.'
  Write-Output 'All other runtime checks passed. Isolation approval is not issued.'
  return
}
Write-Output 'Runtime configuration and supplied enforcement evidence passed for this snapshot only; CVE gates run separately in CI.'
