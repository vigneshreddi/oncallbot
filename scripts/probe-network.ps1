param(
  [string]$Namespace = 'incident-lab',
  [string]$EvidencePath = (Join-Path $env:TEMP 'incident-lab-network-evidence.json'),
  [ValidateRange(60, 600)][int]$TimeoutSeconds = 240,
  [string]$Image = 'python:3.12.12-slim-bookworm',
  [switch]$FunctionsOnly
)

$ErrorActionPreference = 'Stop'
$loadOnly = $FunctionsOnly
. "$PSScriptRoot/security-check.ps1" -Namespace $Namespace -FunctionsOnly

function Get-ConnectivityVerdict {
  param($Attempts)
  $pairs = @($Attempts)
  $controls = @($pairs | Where-Object { $_.before.outcome -ne 'allowed' -or $_.after.outcome -ne 'allowed' })
  return [pscustomobject]@{
    sameNamespaceAllowed = ($pairs.Count -gt 0 -and $controls.Count -eq 0)
    outsideNamespaceDenied = ($pairs.Count -gt 0 -and $controls.Count -eq 0 -and
      @($pairs | Where-Object { $_.outside.outcome -ne 'timeout' }).Count -eq 0)
  }
}

function New-ProbePod {
  param([string]$Name, [string]$PodNamespace, [string]$Node, [string]$RunId, [string]$PodImage, [string]$Code)
  return @{
    apiVersion = 'v1'; kind = 'Pod'
    metadata = @{ name = $Name; namespace = $PodNamespace; labels = @{ 'incident-lab-probe' = $RunId } }
    spec = @{
      nodeSelector = @{ 'kubernetes.io/hostname' = $Node }; restartPolicy = 'Never'
      tolerations = @(@{ key = 'node-role.kubernetes.io/control-plane'; operator = 'Exists'; effect = 'NoSchedule' })
      automountServiceAccountToken = $false; enableServiceLinks = $false
      activeDeadlineSeconds = 660; terminationGracePeriodSeconds = 1
      securityContext = @{ runAsNonRoot = $true; runAsUser = 10001; runAsGroup = 10001; seccompProfile = @{ type = 'RuntimeDefault' } }
      containers = @(@{
        name = 'probe'; image = $PodImage; imagePullPolicy = 'IfNotPresent'
        command = @('python', '-B', '-u', '-c', $Code)
        securityContext = @{ allowPrivilegeEscalation = $false; readOnlyRootFilesystem = $true; capabilities = @{ drop = @('ALL') } }
        resources = @{ requests = @{ cpu = '10m'; memory = '32Mi' }; limits = @{ cpu = '200m'; memory = '96Mi' } }
        volumeMounts = @(@{ name = 'tmp'; mountPath = '/tmp' })
      })
      volumes = @(@{ name = 'tmp'; emptyDir = @{ sizeLimit = '8Mi' } })
    }
  }
}

function Invoke-BoundedProcess {
  param([string]$Executable, [string[]]$Arguments, [int]$LimitSeconds = 20)
  $remaining = $script:budgetSeconds - $script:clock.Elapsed.TotalSeconds
  if ($remaining -le 0) { throw 'Probe time budget exhausted' }
  $milliseconds = [int]([Math]::Min($remaining, $LimitSeconds) * 1000)
  $stdout = Join-Path $script:work ([Guid]::NewGuid().ToString('N') + '.out')
  $stderr = $stdout + '.err'
  $quoted = @($Arguments | ForEach-Object { '"' + ($_ -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"' })
  $process = Start-Process -FilePath $Executable -ArgumentList $quoted -PassThru -NoNewWindow -RedirectStandardOutput $stdout -RedirectStandardError $stderr
  try {
    $null = $process.Handle
    $finished = $process.WaitForExit($milliseconds)
    if (-not $finished) { $process.Kill(); $process.WaitForExit(2000) | Out-Null }
    $process.Refresh()
    return [pscustomobject]@{
      exitCode = $(if ($finished) { $process.ExitCode } else { -1 }); timedOut = (-not $finished)
      stdout = [IO.File]::ReadAllText($stdout); stderr = [IO.File]::ReadAllText($stderr)
    }
  } finally { $process.Dispose() }
}

function Invoke-ProbeKube {
  param([string[]]$Arguments, [int]$LimitSeconds = 20, [switch]$AllowFailure)
  Write-Host "Probe kubectl: $($Arguments[0]) $($Arguments[1]) (limit ${LimitSeconds}s)"
  $prefix = @('--request-timeout=10s')
  if ($script:probeContext) { $prefix += @('--context', $script:probeContext) }
  $result = Invoke-BoundedProcess $script:kubectl ($prefix + $Arguments) $LimitSeconds
  if (-not $AllowFailure -and ($result.timedOut -or $result.exitCode -ne 0)) {
    throw "kubectl failed or timed out: $($Arguments -join ' '): $($result.stderr)"
  }
  return $result
}

function Get-ProbeJson {
  param([string[]]$Arguments)
  return ((Invoke-ProbeKube $Arguments).stdout | ConvertFrom-Json)
}

function New-LoopbackRequestScript {
  param([int]$Port, [string]$Expected)
  if ($Port -lt 1 -or $Port -gt 65535 -or $Expected -notmatch '^netprobe-[a-f0-9]{16}$') { throw 'Invalid loopback request parameters' }
  return @"
`$ErrorActionPreference = 'Stop'
`$response = `$null
`$reader = `$null
try {
  `$request = [Net.HttpWebRequest]::Create('http://127.0.0.1:$Port/')
  `$request.Proxy = `$null
  `$request.AllowAutoRedirect = `$false
  `$request.Timeout = 4000
  `$request.ReadWriteTimeout = 4000
  `$response = `$request.GetResponse()
  `$reader = [IO.StreamReader]::new(`$response.GetResponseStream(), [Text.Encoding]::UTF8)
  `$body = `$reader.ReadToEnd()
  @{ outcome = 'observed'; status = [int]`$response.StatusCode; responseMatched = (`$body -ceq '$Expected'); proxyDisabled = `$true } | ConvertTo-Json -Compress
} catch {
  @{ outcome = 'failed'; errorType = `$_.Exception.GetType().Name; proxyDisabled = `$true } | ConvertTo-Json -Compress
  exit 1
} finally {
  if (`$reader) { `$reader.Dispose() }
  if (`$response) { `$response.Dispose() }
}
"@
}

function Get-LoopbackVerdict {
  param($Local, [bool]$LoopbackOnly)
  $result = $null
  try { $result = $Local.stdout | ConvertFrom-Json } catch { }
  $matched = ($null -ne $result -and $result.responseMatched -eq $true)
  return @{
    allowed = ($LoopbackOnly -and -not $Local.timedOut -and $Local.exitCode -eq 0 -and
      $result.outcome -eq 'observed' -and $result.status -eq 200 -and $result.proxyDisabled -eq $true -and $matched)
    responseMatched = $matched; exitCode = $Local.exitCode; timedOut = $Local.timedOut
    httpStatus = $result.status; outcome = $result.outcome; errorType = $result.errorType
    proxyDisabled = ($result.proxyDisabled -eq $true)
  }
}

function Invoke-PodRequest {
  param([string]$SourceNamespace, [string]$SourcePod, [string]$Url, [string]$Expected = '')
  $code = @'
import json, socket, sys, urllib.request, urllib.error
url, expected = sys.argv[1:3]
try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=4) as response:
        body = response.read(1024).decode('utf-8')
        result = {'outcome': 'allowed' if not expected or body == expected else 'unexpected-response', 'status': response.status}
except Exception as error:
    cause = getattr(error, 'reason', error)
    result = {'outcome': 'timeout' if isinstance(cause, (TimeoutError, socket.timeout)) else 'failed', 'errorType': type(cause).__name__, 'error': str(cause)}
print(json.dumps(result))
'@
  $command = @('exec', '-n', $SourceNamespace, $SourcePod, '--', 'python', '-B', '-c', $code, $Url, $Expected)
  if (-not $Expected) { $command[-1] = '-' ; $command[-3] = $code.Replace('url, expected = sys.argv[1:3]', "url, expected = sys.argv[1], ''") }
  $raw = Invoke-ProbeKube $command 12 -AllowFailure
  $result = if ($raw.exitCode -eq 0 -and -not $raw.timedOut) {
    try { $raw.stdout | ConvertFrom-Json } catch { @{ outcome = 'unavailable'; error = 'Invalid exec output' } }
  } else { @{ outcome = 'unavailable'; error = $raw.stderr; execTimedOut = $raw.timedOut } }
  return @{ sourceNamespace = $SourceNamespace; sourcePod = $SourcePod; url = $Url; command = $command; result = $result; outcome = $result.outcome }
}

if ($loadOnly) { return }
if ($Namespace -notmatch '^[a-z0-9]([-a-z0-9]*[a-z0-9])?$') { throw 'Invalid namespace' }
$script:kubectl = (Get-Command kubectl -ErrorAction Stop).Source
$script:probeContext = $null
$script:work = Join-Path $env:TEMP ('incident-network-' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($script:work) | Out-Null
$script:clock = [Diagnostics.Stopwatch]::StartNew()
$script:budgetSeconds = $TimeoutSeconds
$runId = 'netprobe-' + [Guid]::NewGuid().ToString('N').Substring(0, 16)
$outsideNamespace = $runId
$createdPods = [System.Collections.Generic.List[string]]::new()
$forward = $null
$evidence = [ordered]@{
  schemaVersion = 1; observedAt = [DateTimeOffset]::UtcNow.ToString('o'); context = $null; namespace = $Namespace
  policySha256 = $null; nodeUids = @(); targetPodUids = @(); cni = $null
  outsideNamespaceDenied = $null; externalHostDenied = $null; sameNamespaceAllowed = $null
  outboundInternetAllowed = $null; localhostPortForwardAllowed = $null
  probeDetails = [ordered]@{
    runId = $runId; timeoutSeconds = $TimeoutSeconds; attempts = @(); targets = @(); outbound = @()
    externalHost = @{ status = 'unavailable'; reason = 'No genuine external source supplied. Pod probes, node addresses and loopback forwarding cannot establish external-host denial.' }
    scope = 'Temporary TCP/HTTP targets on ready schedulable nodes; not exhaustive workload/port or external-host certification.'
    cleanup = @(); errors = @()
  }
}
try {
  $script:probeContext = (Invoke-ProbeKube @('config', 'current-context')).stdout.Trim()
  $evidence.context = $script:probeContext
  $namespaceObject = Get-ProbeJson @('get', 'namespace', $Namespace, '-o', 'json')
  if ($namespaceObject.status.phase -ne 'Active') { throw 'Target namespace is not Active' }
  $policies = Get-ProbeJson @('get', 'networkpolicy', '-n', $Namespace, '-o', 'json')
  if (@($policies.items | Where-Object { $_.metadata.name -eq 'deny-external-ingress' }).Count -ne 1) { throw 'Current ingress policy missing; no policy will be installed by this probe' }
  $evidence.policySha256 = Get-PolicySha256 $policies
  $nodes = Get-ProbeJson @('get', 'nodes', '-o', 'json')
  $evidence.nodeUids = @($nodes.items.metadata.uid | Sort-Object)
  $initialPods = Get-ProbeJson @('get', 'pods', '-n', $Namespace, '-o', 'json')
  $evidence.targetPodUids = @(Get-RunningPodUids $initialPods)
  if (-not $evidence.targetPodUids.Count) { throw 'No running workload pods' }
  $systemPods = Get-ProbeJson @('get', 'pods', '-n', 'kube-system', '-o', 'json')
  $evidence.cni = @{ observation = 'Read-only kube-system image inventory; not node CNI filesystem inspection'; images = @($systemPods.items.spec.containers.image | Sort-Object -Unique) }
  $evidence.probeDetails.exposure = @{
    services = (Get-ProbeJson @('get', 'services', '-n', $Namespace, '-o', 'json')).items
    ingresses = (Get-ProbeJson @('get', 'ingresses', '-n', $Namespace, '-o', 'json')).items
    nodes = @($nodes.items | ForEach-Object { @{ name = $_.metadata.name; uid = $_.metadata.uid; addresses = $_.status.addresses } })
    apiServer = (Get-ProbeJson @('config', 'view', '--minify', '-o', 'json')).clusters[0].cluster.server
    workloadHostExposure = @($initialPods.items | ForEach-Object { @{ name = $_.metadata.name; hostNetwork = $_.spec.hostNetwork; ports = @($_.spec.containers.ports) } })
  }
  $listenerScript = "Get-NetTCPConnection -State Listen -ErrorAction Stop | Select-Object LocalAddress,LocalPort,OwningProcess | ConvertTo-Json -Compress"
  $listeners = Invoke-BoundedProcess (Join-Path $PSHOME 'powershell.exe') @('-NoProfile', '-NonInteractive', '-Command', $listenerScript) 20
  $evidence.probeDetails.hostListeners = @{ status = 'unavailable'; note = 'OS TCP listeners are observations only, not an external reachability test.' }
  if ($listeners.exitCode -eq 0) {
    $listenerItems = $listeners.stdout | ConvertFrom-Json
    $evidence.probeDetails.hostListeners = @{ status = 'observed'; listeners = @($listenerItems); note = 'Includes API/node/forward-related listeners if visible on this OS. VM/NAT/firewall reachability remains untested.' }
  }
  $readyNodes = @($nodes.items | Where-Object { -not $_.spec.unschedulable -and @($_.status.conditions | Where-Object { $_.type -eq 'Ready' -and $_.status -eq 'True' }).Count -gt 0 })
  if (-not $readyNodes.Count -or $readyNodes.Count -gt 4) { throw 'Probe supports 1..4 ready schedulable nodes within its bounded budget' }
  $namespaceManifest = @{ apiVersion = 'v1'; kind = 'Namespace'; metadata = @{ name = $outsideNamespace; labels = @{ 'incident-lab-probe' = $runId; 'pod-security.kubernetes.io/enforce' = 'restricted' } } }
  $manifestPath = Join-Path $script:work 'resource.json'
  [IO.File]::WriteAllText($manifestPath, ($namespaceManifest | ConvertTo-Json -Depth 100))
  Invoke-ProbeKube @('create', '-f', $manifestPath) | Out-Null
  $targetNames = @(); $outsideNames = @()
  foreach ($node in $readyNodes) {
    $index = $targetNames.Count
    $targetName = "$runId-target-$index"
    $outsideName = "$runId-client-$index"
    $serverCode = @"
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'$runId')
HTTPServer(('0.0.0.0', 8080), Handler).serve_forever()
"@
    $pod = New-ProbePod $targetName $Namespace $node.metadata.labels.'kubernetes.io/hostname' $runId $Image $serverCode
    $pod.spec.containers[0].readinessProbe = @{ httpGet = @{ path = '/'; port = 8080 }; periodSeconds = 1; timeoutSeconds = 1 }
    $createdPods.Add($targetName)
    [IO.File]::WriteAllText($manifestPath, ($pod | ConvertTo-Json -Depth 100))
    Invoke-ProbeKube @('create', '-f', $manifestPath) | Out-Null
    $pod = New-ProbePod $outsideName $outsideNamespace $node.metadata.labels.'kubernetes.io/hostname' $runId $Image 'import threading; threading.Event().wait()'
    [IO.File]::WriteAllText($manifestPath, ($pod | ConvertTo-Json -Depth 100))
    Invoke-ProbeKube @('create', '-f', $manifestPath) | Out-Null
    $targetNames += $targetName; $outsideNames += $outsideName
  }
  foreach ($podNamespace in @($Namespace, $outsideNamespace)) {
    Invoke-ProbeKube @('wait', '-n', $podNamespace, 'pod', '-l', "incident-lab-probe=$runId", '--for=condition=Ready', '--timeout=60s') 65 | Out-Null
  }
  $targets = Get-ProbeJson @('get', 'pods', '-n', $Namespace, '-l', "incident-lab-probe=$runId", '-o', 'json')
  $clients = Get-ProbeJson @('get', 'pods', '-n', $outsideNamespace, '-l', "incident-lab-probe=$runId", '-o', 'json')
  $evidence.probeDetails.targets = @($targets.items | ForEach-Object { @{ name = $_.metadata.name; uid = $_.metadata.uid; ip = $_.status.podIP; node = $_.spec.nodeName; imageIDs = @($_.status.containerStatuses.imageID) } })
  $evidence.probeDetails.clients = @($clients.items | ForEach-Object { @{ name = $_.metadata.name; uid = $_.metadata.uid; ip = $_.status.podIP; node = $_.spec.nodeName; imageIDs = @($_.status.containerStatuses.imageID) } })
  foreach ($target in $targets.items) {
    $address = $target.status.podIP
    if ($address.Contains(':')) { $address = "[$address]" }
    $url = "http://${address}:8080/"
    for ($index = 0; $index -lt $targetNames.Count; $index++) {
      $before = Invoke-PodRequest $Namespace $targetNames[$index] $url $runId
      $outside = Invoke-PodRequest $outsideNamespace $outsideNames[$index] $url $runId
      $after = Invoke-PodRequest $Namespace $targetNames[$index] $url $runId
      $evidence.probeDetails.attempts += @{ target = $target.metadata.name; targetUid = $target.metadata.uid; before = $before; outside = $outside; after = $after }
    }
  }
  $verdict = Get-ConnectivityVerdict $evidence.probeDetails.attempts
  $evidence.sameNamespaceAllowed = $verdict.sameNamespaceAllowed
  $evidence.outsideNamespaceDenied = $verdict.outsideNamespaceDenied
  foreach ($source in $targetNames) { $evidence.probeDetails.outbound += Invoke-PodRequest $Namespace $source 'https://example.com/' }
  $evidence.outboundInternetAllowed = (@($evidence.probeDetails.outbound | Where-Object { $_.outcome -ne 'allowed' }).Count -eq 0)
  $forwardLog = Join-Path $script:work 'forward.out'
  $forward = Start-Process -FilePath $script:kubectl -ArgumentList @('--context', $script:probeContext, '--request-timeout=10s', 'port-forward', '-n', $Namespace, "pod/$($targetNames[0])", '--address', '127.0.0.1', ':8080') -PassThru -NoNewWindow -RedirectStandardOutput $forwardLog -RedirectStandardError (Join-Path $script:work 'forward.err')
  $forwardDeadline = [Math]::Min($TimeoutSeconds, $script:clock.Elapsed.TotalSeconds + 12)
  $localPort = $null
  while ($script:clock.Elapsed.TotalSeconds -lt $forwardDeadline -and -not $forward.HasExited) {
    $logStream = [IO.File]::Open($forwardLog, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    $logReader = [IO.StreamReader]::new($logStream)
    try { $output = $logReader.ReadToEnd() } finally { $logReader.Dispose() }
    if ($output -match 'Forwarding from 127\.0\.0\.1:(\d+)') { $localPort = [int]$Matches[1]; break }
    $forward.WaitForExit(100) | Out-Null
  }
  if (-not $localPort) { throw 'Loopback port-forward did not start in time' }
  $listenerScript = "Get-NetTCPConnection -State Listen -OwningProcess $($forward.Id) -ErrorAction Stop | Select-Object LocalAddress,LocalPort,OwningProcess | ConvertTo-Json -Compress"
  $listeners = Invoke-BoundedProcess (Join-Path $PSHOME 'powershell.exe') @('-NoProfile', '-NonInteractive', '-Command', $listenerScript) 15
  if ($listeners.exitCode -ne 0) { throw 'Cannot verify port-forward OS listening addresses' }
  $listenerItems = $listeners.stdout | ConvertFrom-Json
  $forwardListeners = @($listenerItems)
  $loopbackOnly = ($forwardListeners.Count -gt 0 -and @($forwardListeners | Where-Object { $_.LocalAddress -notin @('127.0.0.1', '::1') }).Count -eq 0)
  $localScript = New-LoopbackRequestScript $localPort $runId
  $local = Invoke-BoundedProcess (Join-Path $PSHOME 'powershell.exe') @('-NoProfile', '-NonInteractive', '-Command', $localScript) 8
  $localVerdict = Get-LoopbackVerdict $local $loopbackOnly
  $evidence.localhostPortForwardAllowed = $localVerdict.allowed
  $evidence.probeDetails.portForward = @{ listeners = $forwardListeners; loopbackOnly = $loopbackOnly; responseMatched = $localVerdict.responseMatched; request = $localVerdict; port = $localPort; target = $targetNames[0] }
} catch {
  Write-Host "Probe error: $($_.Exception.Message)"
  $evidence.probeDetails.errors += $_.Exception.Message
} finally {
  if ($forward) { if (-not $forward.HasExited) { $forward.Kill(); $forward.WaitForExit(2000) | Out-Null }; $forward.Dispose() }
  $evidence.probeDetails.probeElapsedSeconds = [Math]::Round($script:clock.Elapsed.TotalSeconds, 2)
  $script:clock.Restart(); $script:budgetSeconds = 60
  foreach ($podName in $createdPods) {
    try {
      $pod = Invoke-ProbeKube @('get', 'pod', $podName, '-n', $Namespace, '-o', 'json', '--ignore-not-found') 5
      if ($pod.stdout.Trim()) {
        $identity = $pod.stdout | ConvertFrom-Json
        if ($identity.metadata.labels.'incident-lab-probe' -ne $runId) { throw 'Ownership mismatch; refusing cleanup' }
        Invoke-ProbeKube @('delete', 'pod', $podName, '-n', $Namespace, '--wait=true', '--timeout=8s') 10 | Out-Null
      }
      $evidence.probeDetails.cleanup += @{ resource = "$Namespace/pod/$podName"; status = 'removed-or-absent' }
    } catch { $evidence.probeDetails.errors += "Cleanup $podName : $($_.Exception.Message)" }
  }
  try {
    $probeNamespace = Invoke-ProbeKube @('get', 'namespace', $outsideNamespace, '-o', 'json', '--ignore-not-found') 5
    if ($probeNamespace.stdout.Trim()) {
      $identity = $probeNamespace.stdout | ConvertFrom-Json
      if ($identity.metadata.labels.'incident-lab-probe' -ne $runId) { throw 'Ownership mismatch; refusing namespace deletion' }
      Invoke-ProbeKube @('delete', 'namespace', $outsideNamespace, '--wait=true', '--timeout=20s') 22 | Out-Null
    }
    $evidence.probeDetails.cleanup += @{ resource = "namespace/$outsideNamespace"; status = 'removed-or-absent' }
  } catch { $evidence.probeDetails.errors += "Namespace cleanup: $($_.Exception.Message)" }
  try {
    $finalPods = Get-ProbeJson @('get', 'pods', '-n', $Namespace, '-o', 'json')
    $finalNodes = Get-ProbeJson @('get', 'nodes', '-o', 'json')
    $finalPolicies = Get-ProbeJson @('get', 'networkpolicy', '-n', $Namespace, '-o', 'json')
    $stable = ((@(Get-RunningPodUids $finalPods) -join ',') -eq ($evidence.targetPodUids -join ',') -and
      (@($finalNodes.items.metadata.uid | Sort-Object) -join ',') -eq ($evidence.nodeUids -join ',') -and
      (Get-PolicySha256 $finalPolicies) -eq $evidence.policySha256)
    $evidence.probeDetails.snapshotStable = $stable
    if (-not $stable) { $evidence.probeDetails.errors += 'Workload, nodes or policies changed during the probe; rerun required' }
  } catch { $evidence.probeDetails.errors += "Final snapshot unavailable: $($_.Exception.Message)" }
  $evidence.observedAt = [DateTimeOffset]::UtcNow.ToString('o')
  $evidence.probeDetails.cleanupElapsedSeconds = [Math]::Round($script:clock.Elapsed.TotalSeconds, 2)
  if ($evidence.probeDetails.errors.Count) {
    $evidence.outsideNamespaceDenied = $null
    $evidence.probeDetails.status = 'incomplete'
  } else { $evidence.probeDetails.status = 'observed' }
  $fullEvidencePath = [IO.Path]::GetFullPath($EvidencePath)
  [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($fullEvidencePath)) | Out-Null
  [IO.File]::WriteAllText($fullEvidencePath, ($evidence | ConvertTo-Json -Depth 100), [Text.UTF8Encoding]::new($false))
  Remove-Item $script:work -Recurse -Force
}
Write-Output "Evidence: $fullEvidencePath"
Write-Output "Outside namespace denied: $($evidence.outsideNamespaceDenied); same namespace allowed: $($evidence.sameNamespaceAllowed); outbound HTTPS: $($evidence.outboundInternetAllowed); loopback forward: $($evidence.localhostPortForwardAllowed); external host: unavailable"
if ($evidence.probeDetails.errors.Count) { throw "Incomplete probe: $($evidence.probeDetails.errors -join '; ')" }
if ($evidence.outsideNamespaceDenied -ne $true) { throw 'NetworkPolicy denial was not demonstrated. Security gate remains blocked.' }
Write-Output 'Namespace checks completed; external-host evidence remains unavailable. This is not isolation approval.'