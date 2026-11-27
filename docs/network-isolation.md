# Network isolation: observations and strict gate

These are measured checks, not isolation approval. The security gate remains
strict, and no policy engine, deployment, host exposure, taint or host networking
configuration was changed during this investigation.

`scripts/up.ps1` calls the gate with `-AllowUnverifiedIsolation`: only the two
`CNI ...` findings become warnings when no enforcement evidence is supplied; every
other finding still fails deployment, and no isolation approval is issued. Use
`up.ps1 -StrictIsolation` (or run `security-check.ps1` directly) for the strict gate.

## Original evidence

The existing local `tmp/network-evidence.json` was preserved. Its observation
time is `2026-10-03T20:11:50.9866807+00:00`, context `docker-desktop`, namespace
`incident-lab`.

| Check | Recorded result | Meaning |
| --- | --- | --- |
| `outsideNamespaceDenied` | `true` | Four temporary pod-to-pod HTTP requests timed out, each bracketed by successful same-namespace controls. |
| `sameNamespaceAllowed` | `true` | Controls succeeded across the two-node target/source matrix. |
| `outboundInternetAllowed` | `true` | Both target pods reached `https://example.com/`; this is not exhaustive Internet reachability. |
| `localhostPortForwardAllowed` | `false` | OS listener was loopback-only, but the old response comparison did not match. |
| `externalHostDenied` | `null` | No genuine external source was supplied. |

The original snapshot was stable, had no recorded errors, and recorded cleanup
of the two target pods and temporary client namespace. Targets used TCP/HTTP
port 8080; these observations do not cover every workload, protocol or port.

## Loopback response bug and repair

The temporary HTTP server sends no `Content-Type`. A bounded local reproduction
on Windows PowerShell 5.1 showed that `Invoke-WebRequest -UseBasicParsing`
returned `.Content` as `System.Byte[]`, not a string. Decoding those bytes yielded
the exact expected probe identifier. Emitting the byte array to child-process
stdout therefore did not yield the text that the old comparison expected.
Trimming stdout was already present and cannot repair that representation.
The old evidence did not record the HTTP exit status, so this reproduction
explains the code defect without claiming to reconstruct that historical request.

The repair uses `HttpWebRequest` with `Proxy = $null` explicitly disabling proxy
use, redirects disabled, bounded connection/read timeouts and a UTF-8 stream
reader. This avoids relying on `Invoke-WebRequest -Proxy $null` semantics or
environmental proxy settings. Approval of this check still requires HTTP 200,
an exact body match, successful non-timed-out child execution and OS-confirmed
loopback-only listeners. Whitespace inside the response body is not discarded.
Only structured status, match, timeout and exception-type diagnostics are saved;
response bodies, proxy URLs, credentials and raw HTTP exception messages are not.

## Bounded rerun

The final rerun wrote actual evidence to `tmp/network-evidence-rerun.json` at
`2026-10-03T20:17:23.9712698+00:00`. Probe time was 55.28 seconds and cleanup
16.97 seconds, within the 240-second probe and separate 60-second cleanup budgets.

| Check | Recorded result |
| --- | --- |
| `outsideNamespaceDenied` | `null` |
| `sameNamespaceAllowed` | `true` |
| `outboundInternetAllowed` | `true` |
| `localhostPortForwardAllowed` | `true` |
| `externalHostDenied` | `null` |

Loopback evidence recorded HTTP 200, exact response match, exit code 0, no timeout,
proxy disabled and a sole `127.0.0.1` listener. All four namespace request pairs
again recorded allowed/timeout/allowed. However, `snapshotStable` was `false`:
the workload pod identity set changed, and the probe correctly invalidated the
overall denial verdict and exited with an incomplete-probe error. Subsequent
read-only comparison found unchanged node identities and policy hash, but a
different inventory-service pod UID. This is not a fresh stable isolation proof.

The final run recorded removal of both target pods and its temporary namespace.
An earlier attempt returned before writing evidence; its remaining uniquely
labeled client namespace was ownership-checked and removed. That interrupted
attempt is not treated as evidence. No further proof reruns were performed.

## Policy engine remains unverified

Read-only controller image inventory across all namespaces found the `kindnet`
DaemonSet using `docker.io/kindest/kindnetd:v20260528-9350166c` and `kube-proxy`.
No Cilium, Calico, Antrea or kube-router controller was observed. Image inventory
alone does not inspect node CNI configuration, establish the running enforcement
implementation, or attribute the observed timeouts to NetworkPolicy rather than
another filtering mechanism.

The upstream [kindnetd README](https://github.com/kubernetes-sigs/kind/blob/main/images/kindnetd/README.md)
documents masquerading, pod-CIDR routes and a simple CNI configuration. It does
not establish NetworkPolicy support for this deployed image. An upstream
reference to another project is not evidence that this image runs that project's
policy engine. No privileged node inspection or installation was attempted.

The existing recognized-engine gate in `scripts/security-check.ps1` is unchanged.
It requires a recognized engine plus all five successful checks and fresh,
matching context, namespace, policy, node and running-pod identities. It rejected
the rerun for unknown engine, missing namespace/external denial and mismatched
pod identities. Before considering any recognition change, obtain read-only
evidence tying the actual deployed engine/version/configuration to documented
NetworkPolicy support and repeat stable controlled tests. Measured namespace
timeouts alone do not meet that requirement.

## External-source challenge

This probe currently has no genuine external-host test input. Its explicit
`externalHostDenied = null` must remain unavailable, not be changed to `true`.
An outside-namespace pod is still inside the cluster. Loopback port-forwarding
uses authenticated API access and is not an external ingress denial test.
ClusterIP-only Services, absent Ingress, listener observations, and an
unreachable private address from Windows are configuration/routing observations,
not independent external denial proof.

The [kind documentation](https://kind.sigs.k8s.io/docs/user/known-issues/#docker-desktop-for-macos-and-windows)
notes that Docker Desktop's Linux VM hides container networks from the Windows
host. Without a separately controlled external source and a valid, demonstrably
routable target path, failure may show VM/NAT/firewall routing rather than policy
denial. A genuine test needs source/path identification, timestamps and positive
reachability controls without adding public Services, Ingress, host ports or
non-loopback forwarding. If that cannot be provided under the existing exposure
rules, full isolation approval remains unavailable. Do not waive the requirement
or install privileged tooling to manufacture a pass.

## Reproduction and validation

```powershell
& .\tests\security\test-network-probe.ps1
& .\tests\security\test-security.ps1
& .\tests\security\test-ci.ps1
& .\scripts\probe-network.ps1 -TimeoutSeconds 240 -EvidencePath .\tmp\network-evidence-rerun.json
& .\scripts\security-check.ps1 -EnforcementEvidence .\tmp\network-evidence-rerun.json
```

The probe creates only hardened temporary targets/clients and an explicitly
loopback-bound forward, cleans up owned resources and does not install policies.
Evidence files are local, git-ignored runtime artifacts. Unit tests exercise a
real headerless loopback response and fail-closed verdicts; synthetic fixtures
are not cluster evidence. The final two commands above are instructions for a
future stable measurement, not a promise that either gate will pass.