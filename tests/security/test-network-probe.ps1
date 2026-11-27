$ErrorActionPreference = 'Stop'
. "$PSScriptRoot/../../scripts/probe-network.ps1" -FunctionsOnly
$script:work = Join-Path $env:TEMP ('probe-fixture-' + [Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($script:work) | Out-Null
$script:clock = [Diagnostics.Stopwatch]::StartNew(); $script:budgetSeconds = 30
try {
  $shell = Join-Path $PSHOME 'powershell.exe'
  $success = Invoke-BoundedProcess $shell @('-NoProfile', '-NonInteractive', '-Command', 'Write-Output "quoted value"; exit 0') 5
  if ($success.exitCode -ne 0 -or $success.stdout.Trim() -ne 'quoted value') { throw 'Process exit code or argument quoting broken' }
  $failure = Invoke-BoundedProcess $shell @('-NoProfile', '-NonInteractive', '-Command', 'exit 7') 5
  if ($failure.exitCode -ne 7) { throw 'Nonzero process exit lost' }
  $timeoutResult = Invoke-BoundedProcess $shell @('-NoProfile', '-NonInteractive', '-Command', 'while ($true) {}') 1
  if (-not $timeoutResult.timedOut -or $timeoutResult.exitCode -ne -1) { throw 'Process timeout not bounded' }
  $expected = 'netprobe-0123456789abcdef'
  $server = Start-Job -ScriptBlock {
    param($Body)
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    try {
      Write-Output $listener.LocalEndpoint.Port
      $client = $listener.AcceptTcpClient()
      try {
        $stream = $client.GetStream()
        $stream.ReadTimeout = 5000
        $reader = [IO.StreamReader]::new($stream)
        while ($reader.ReadLine()) { }
        $bytes = [Text.Encoding]::UTF8.GetBytes("HTTP/1.0 200 OK`r`nContent-Length: $($Body.Length)`r`n`r`n$Body")
        $stream.Write($bytes, 0, $bytes.Length)
      } finally { $client.Dispose() }
    } finally { $listener.Stop() }
  } -ArgumentList $expected
  try {
    $deadline = [Diagnostics.Stopwatch]::StartNew()
    $port = $null
    while (-not $port -and $deadline.Elapsed.TotalSeconds -lt 8) {
      $port = Receive-Job $server -Keep | Select-Object -First 1
      if (-not $port) { Wait-Job $server -Timeout 1 | Out-Null }
    }
    if (-not $port) { throw 'Loopback fixture did not start' }
    $requestScript = New-LoopbackRequestScript $port $expected
    $local = Invoke-BoundedProcess $shell @('-NoProfile', '-NonInteractive', '-Command', $requestScript) 8
    $verdict = Get-LoopbackVerdict $local $true
    if (-not $verdict.allowed) { throw "Headerless HTTP response rejected: $($verdict | ConvertTo-Json -Compress)" }
    if ((Get-LoopbackVerdict $local $false).allowed) { throw 'Non-loopback listener falsely accepted' }
  } finally { Stop-Job $server; Remove-Job $server -Force }
  foreach ($invalid in @(
    @{ exitCode = 0; timedOut = $false; stdout = 'not JSON' },
    @{ exitCode = 1; timedOut = $false; stdout = $local.stdout },
    @{ exitCode = 0; timedOut = $true; stdout = $local.stdout },
    @{ exitCode = 0; timedOut = $false; stdout = '{"outcome":"observed","status":200,"responseMatched":false,"proxyDisabled":true}' },
    @{ exitCode = 0; timedOut = $false; stdout = '{"outcome":"observed","status":302,"responseMatched":true,"proxyDisabled":true}' },
    @{ exitCode = 0; timedOut = $false; stdout = '{"outcome":"observed","status":200,"responseMatched":true,"proxyDisabled":false}' }
  )) {
    if ((Get-LoopbackVerdict $invalid $true).allowed) { throw 'Invalid loopback result falsely accepted' }
  }
} finally { Remove-Item $script:work -Recurse -Force }
$allowed = @{ outcome = 'allowed' }
$timeout = @{ outcome = 'timeout' }
$failed = @{ outcome = 'failed' }
$valid = @{ before = $allowed; outside = $timeout; after = $allowed }
$verdict = Get-ConnectivityVerdict @($valid)
if (-not $verdict.sameNamespaceAllowed -or -not $verdict.outsideNamespaceDenied) { throw 'Valid bracketed denial rejected' }
foreach ($attempt in @(
  @{ before = $failed; outside = $timeout; after = $allowed },
  @{ before = $allowed; outside = $timeout; after = $failed },
  @{ before = $allowed; outside = $failed; after = $allowed },
  @{ before = $allowed; outside = $allowed; after = $allowed },
  @{ before = $allowed; outside = @{ outcome = 'unavailable' }; after = $allowed }
)) {
  if ((Get-ConnectivityVerdict @($valid, $attempt)).outsideNamespaceDenied) { throw 'Failed/unavailable/uncontrolled request falsely certified' }
}
if ((Get-ConnectivityVerdict @()).outsideNamespaceDenied) { throw 'Empty evidence falsely certified' }
$pod = (New-ProbePod 'fixture' 'fixture-ns' 'fixture-node' 'fixture-id' 'python:3.12.12-slim-bookworm' 'import threading; threading.Event().wait()') | ConvertTo-Json -Depth 100 | ConvertFrom-Json
$podSnapshot = @{
  context = 'fixture'; namespace = 'fixture-ns'; services = @{ items = @() }; ingresses = @{ items = @() }
  policies = @{ items = @(@{ metadata = @{ name = 'deny-external-ingress' }; spec = @{ podSelector = @{}; policyTypes = @('Ingress') } }) }
  pods = @{ items = @($pod) }; systemPods = @{ items = @() }
}
$findings = @(Test-ClusterSnapshot $podSnapshot $null)
if (@($findings | Where-Object { $_ -notmatch 'CNI enforcement|CNI NetworkPolicy' }).Count) { throw "Probe pod violates hardening: $findings" }
if ($pod.spec.automountServiceAccountToken -ne $false -or $pod.spec.volumes[0].name -ne 'tmp') { throw 'Probe token/tmp hardening missing' }
$podSnapshot.policySha256 = 'fixture'; $podSnapshot.nodeUids = @('node'); $podSnapshot.podUids = @('pod')
$evidence = @{
  observedAt = [DateTimeOffset]::UtcNow.ToString('o'); context = 'fixture'; namespace = 'fixture-ns'
  policySha256 = 'fixture'; nodeUids = @('node'); targetPodUids = @('pod'); cni = 'kindnet'; probeDetails = @{ externalHost = @{ status = 'unavailable' } }
  outsideNamespaceDenied = $false; externalHostDenied = $null; sameNamespaceAllowed = $true; outboundInternetAllowed = $true; localhostPortForwardAllowed = $true
}
foreach ($denied in @($false, $true)) {
  $evidence.outsideNamespaceDenied = $denied
  $findings = @(Test-ClusterSnapshot $podSnapshot $evidence)
  foreach ($expected in @('externalHostDenied', 'unknown')) {
    if (-not ($findings -match $expected)) { throw "Fake kindnet snapshot did not block: $expected" }
  }
  if (-not $denied -and -not ($findings -match 'outsideNamespaceDenied')) { throw 'Failed namespace denial did not block' }
}
Write-Output 'PASS: headerless loopback HTTP, fail-closed connectivity controls, unavailable external evidence, strict kindnet gate and hardened probe pod.'