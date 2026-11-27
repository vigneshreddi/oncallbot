$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/../../scripts/security-check.ps1" -FunctionsOnly
$policyFirst = '{"items":[{"metadata":{"name":"b","namespace":"incident-lab","uid":"b","resourceVersion":"1"},"spec":{"policyTypes":["Ingress"],"podSelector":{}}},{"metadata":{"name":"a","uid":"a"},"spec":{"podSelector":{},"policyTypes":["Ingress"]}}]}' | ConvertFrom-Json
$policySecond = '{"items":[{"spec":{"policyTypes":["Ingress"],"podSelector":{}},"metadata":{"uid":"a","name":"a"}},{"spec":{"podSelector":{},"policyTypes":["Ingress"]},"metadata":{"resourceVersion":"99","uid":"b","namespace":"incident-lab","name":"b"}}]}' | ConvertFrom-Json
if ((Get-PolicySha256 $policyFirst) -ne (Get-PolicySha256 $policySecond)) { throw 'Policy hash is order/metadata dependent' }
$policySecond.items[0].spec.policyTypes = @('Ingress', 'Egress')
if ((Get-PolicySha256 $policyFirst) -eq (Get-PolicySha256 $policySecond)) { throw 'Policy spec change did not change hash' }
$mixedPods = @{ items = @(
  @{ metadata = @{ uid = 'running' }; status = @{ phase = 'Running' } },
  @{ metadata = @{ uid = 'completed' }; status = @{ phase = 'Succeeded' } },
  @{ metadata = @{ uid = 'terminating'; deletionTimestamp = 'now' }; status = @{ phase = 'Running' } }
) }
if ((@(Get-RunningPodUids $mixedPods) -join ',') -ne 'running') { throw 'Evidence must identify live running pods only' }
$security = [pscustomobject]@{
  runAsNonRoot = $true; runAsUser = 10001; allowPrivilegeEscalation = $false
  readOnlyRootFilesystem = $true; capabilities = [pscustomobject]@{ drop = @('ALL'); add = @() }
  seccompProfile = @{ type = 'RuntimeDefault' }
}
$snapshot = [pscustomobject]@{
  context = 'fixture'; namespace = 'incident-lab'; policySha256 = 'fixture-hash'
  nodeUids = @('node-1'); podUids = @('pod-1')
  services = @{ items = @(@{ metadata = @{ name = 'app' }; spec = @{ type = 'ClusterIP' } }) }
  ingresses = @{ items = @() }
  policies = @{ items = @(@{ metadata = @{ name = 'deny-external-ingress' }; spec = @{ podSelector = @{}; policyTypes = @('Ingress') } }) }
  pods = @{ items = @(@{ metadata = @{ name = 'app' }; status = @{ phase = 'Pending' }; spec = @{ containers = @(@{ name = 'app'; securityContext = $security }) } }) }
  systemPods = @{ items = @(@{ spec = @{ containers = @(@{ image = 'quay.io/cilium/cilium:v1.17.2' }) } }) }
}
$evidence = [pscustomobject]@{
  observedAt = [DateTimeOffset]::UtcNow.ToString('o'); context = 'fixture'; namespace = 'incident-lab'
  policySha256 = 'fixture-hash'; nodeUids = @('node-1'); targetPodUids = @('pod-1')
  cni = 'fixture configuration'; probeDetails = 'fixture positive controls and denied requests'
  outsideNamespaceDenied = $true; externalHostDenied = $true; sameNamespaceAllowed = $true
  outboundInternetAllowed = $true; localhostPortForwardAllowed = $true
}
function Assert-Rejected {
  param($Actual, [string]$Pattern)
  if (-not ($Actual -match $Pattern)) { throw "Expected rejection matching: $Pattern; got: $Actual" }
}
$baseline = @(Test-ClusterSnapshot $snapshot $evidence)
if ($baseline.Count) { throw "Valid fixture rejected: $($baseline -join '; ')" }
Assert-Rejected (Test-ClusterSnapshot $snapshot $null) 'unverified'
$snapshot.systemPods.items = @()
Assert-Rejected (Test-ClusterSnapshot $snapshot $evidence) 'unknown'
$snapshot.systemPods.items = @(@{ spec = @{ containers = @(@{ image = 'calico/node:v3.29.3' }) } })
$evidence.externalHostDenied = $false
Assert-Rejected (Test-ClusterSnapshot $snapshot $evidence) 'externalHostDenied'
$evidence.externalHostDenied = $true
$evidence.observedAt = [DateTimeOffset]::UtcNow.AddHours(-2).ToString('o')
Assert-Rejected (Test-ClusterSnapshot $snapshot $evidence) 'stale'
$evidence.observedAt = [DateTimeOffset]::UtcNow.ToString('o')
$evidence.nodeUids = @('different-node')
Assert-Rejected (Test-ClusterSnapshot $snapshot $evidence) 'does not match'
$evidence.nodeUids = @('node-1')
$snapshot.services.items[0].spec.externalIPs = @('192.0.2.1')
Assert-Rejected (Test-ClusterSnapshot $snapshot $evidence) 'exposed Service'
$snapshot.services.items[0].spec.Remove('externalIPs')
$snapshot.ingresses.items = @(@{ metadata = @{ name = 'public' } })
Assert-Rejected (Test-ClusterSnapshot $snapshot $evidence) 'Ingress resources'
$snapshot.ingresses.items = @()
$snapshot.policies.items[0].spec.policyTypes += 'Egress'
Assert-Rejected (Test-ClusterSnapshot $snapshot $evidence) 'Outbound'
$snapshot.policies.items[0].spec.policyTypes = @('Ingress')
$security.allowPrivilegeEscalation = $true
Assert-Rejected (Test-ClusterSnapshot $snapshot $evidence) 'Privilege escalation'
$security.allowPrivilegeEscalation = $false
$security.runAsUser = 0
Assert-Rejected (Test-ClusterSnapshot $snapshot $evidence) 'Non-root'
$security.runAsUser = 10001
$snapshot.pods.items[0].spec.initContainers = @(@{ name = 'init'; securityContext = @{ privileged = $true } })
Assert-Rejected (Test-ClusterSnapshot $snapshot $evidence) 'Privileged: app/init'
$snapshot.pods.items[0].spec.Remove('initContainers')
$snapshot.pods.items[0].spec.ephemeralContainers = @(@{ name = 'debug'; securityContext = @{ privileged = $true } })
Assert-Rejected (Test-ClusterSnapshot $snapshot $evidence) 'Privileged: app/debug'
$snapshot.pods.items[0].spec.Remove('ephemeralContainers')
$security.capabilities.drop = @()
Assert-Rejected (Test-ClusterSnapshot $snapshot $evidence) 'Capabilities'
$security.capabilities.drop = @('ALL')
$security.seccompProfile.type = 'Unconfined'
Assert-Rejected (Test-ClusterSnapshot $snapshot $evidence) 'seccomp'
$security.seccompProfile.type = 'RuntimeDefault'
$security.readOnlyRootFilesystem = $false
Assert-Rejected (Test-ClusterSnapshot $snapshot $evidence) 'Writable root'
$security.readOnlyRootFilesystem = $true
$snapshot.pods.items[0].spec.volumes = @(@{ hostPath = @{ path = '/var/run/docker.sock' } })
Assert-Rejected (Test-ClusterSnapshot $snapshot $evidence) 'Host mount'
$snapshot.pods.items[0].spec.Remove('volumes')
$snapshot.pods.items[0].spec.hostNetwork = $true
Assert-Rejected (Test-ClusterSnapshot $snapshot $evidence) 'Host namespace'
$snapshot.pods.items[0].spec.Remove('hostNetwork')
$snapshot.pods.items[0].spec.containers[0].ports = @(@{ hostPort = 8080 })
Assert-Rejected (Test-ClusterSnapshot $snapshot $evidence) 'Host port'
Write-Output 'PASS: security fixtures, including pending/init/ephemeral containers and fail-closed enforcement.'