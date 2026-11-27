# Infrastructure Faults

This is a separate, script-only family. It does not modify the HTTP fault catalog,
Postman exports, the investigator, or the normal deployment workflow. Everything
is disabled by default. No arbitrary infrastructure HTTP endpoint is provided;
Postman continues to import and exercise only the existing HTTP collection.

| Script scenario | Effect | Required evidence |
|---|---|---|
| `scale-zero` | Temporarily scales one allowlisted demo service to zero | Original scale journaled, then restored |
| `pod-delete` | Deletes only `demo-fault-pod-0`, an isolated StatefulSet pod | Replacement pod has a different UID |
| `oom` | Starts isolated `demo-fault-oom`; touches at most 96 MiB against a 64 MiB cgroup limit | Kubernetes termination reason `OOMKilled`, not just exit 137 |
| `node-unavailable-simulated` | Starts isolated `demo-fault-node` with required affinity to a nonexistent node name | `PodScheduled=False`, `Unschedulable`, affinity mismatch |

Node unavailability is **simulated**, not actual node failure. Never create a node
named `incidentlab-simulated-nonexistent-node`. If the pod schedules or no matching
evidence appears, the scenario fails and restores. Real node drain, stop, deletion,
cordon, or control-plane disruption is explicitly unsupported and requires an
operator's separate plan. This runner never changes a real node. PostgreSQL and
the investigator are not mutation targets. Service scale-zero can disrupt dependent
demo requests; pod deletion and OOM are intentionally isolated, not business-service
OOM acceptance or actual node-loss acceptance.

## Preparation

Use an existing `incident-lab` namespace and the worker label provisioned by the
normal deployment. An operator must install the following separate manifests;
all three workloads have zero replicas. Review the context before every operation.

```powershell
.\scripts\start-infra-chaos.ps1 -ValidateOnly -Context docker-desktop
kubectl --context docker-desktop apply -f deploy/k8s/infra-chaos-rbac.yaml -f deploy/k8s/demo-fault-workloads.yaml
```

Do not reapply workload manifests during a scenario: they reset replicas to zero.
There is no automatic deployment, continuous loop, node permission, Job scheduler,
new Service, Ingress, or port-forward. The StatefulSet's governing service name
does not require a Service here: its pod has no listener and is addressed only
through the Kubernetes API. Containers are non-root, non-privileged, read-only,
CPU/memory limited, and have only a small `/tmp` emptyDir. No host mounts, sockets,
credentials, or service-account tokens are mounted. The image is version-pinned;
run the repository image vulnerability gate before live execution. Server dry-run
does not pull the image or prove it is vulnerability-free.

The local runner always uses `--as system:serviceaccount:incident-lab:infra-chaos`.
The operator's kubeconfig must permit impersonating this identity; no impersonation
privilege is granted by these manifests. Without it, execution fails closed. The
Role permits named demo Deployment scale subresources and only the isolated
StatefulSet scale subresource; pod deletion is restricted by `resourceNames` to
`demo-fault-pod-0`. Pod get/list is namespace-wide for observation (RBAC cannot
restrict list by label). No create, exec, Secret access, workload-template patch,
ClusterRole, nodes, or PostgreSQL/investigator mutation is granted.

## Commands

Python 3.12+ and `kubectl` are required; the runner uses only the Python standard
library. Pass `-Python <interpreter-path>` when needed. Dry-run is local and does
not invoke the Kubernetes API.

```powershell
.\scripts\start-infra-chaos.ps1 -DryRun -Scenario oom -Context docker-desktop
.\scripts\start-infra-chaos.ps1 -Enable -Scenario scale-zero -Service inventory-service -DurationSeconds 30 -Context docker-desktop
.\scripts\start-infra-chaos.ps1 -Enable -Scenario pod-delete -Context docker-desktop
.\scripts\start-infra-chaos.ps1 -Enable -Scenario oom -Context docker-desktop
.\scripts\start-infra-chaos.ps1 -Enable -Scenario node-unavailable-simulated -Context docker-desktop
```

Run only one scenario from this checkout at a time. Duration is 5..120 seconds,
including the evidence wait, not an unbounded stress loop. Image pull or worker
placement failures are scenario failures, never proof of OOM or node failure.
OOM restarts can recur until restoration; each process allocates at most 96 MiB,
and each container is confined to its 64 MiB limit. Kubernetes API commands have
timeouts; cleanup has up to three bounded attempts and may extend beyond duration.

Before mutation, the runner durably writes original replica count, target UID,
namespace and context to the git-ignored local journal in `tmp/infra-chaos-state.json`.
Its exclusive reservation blocks overlapping runs using this journal. `finally`,
SIGINT and SIGTERM trigger restoration; isolated workloads must originally be zero
and are confirmed pod-free during cleanup. Service cleanup confirms restored desired
scale, not application readiness. Operator rollout checks are still required.

Forced process termination, machine loss, Kubernetes outages, SIGKILL, or deleting
the journal cannot guarantee automatic restoration. Keep this checkout and journal
until recovery finishes. A retained journal blocks new faults; retry explicitly:

```powershell
.\scripts\start-infra-chaos.ps1 -Restore -Context docker-desktop
kubectl --context docker-desktop -n incident-lab rollout status deployment/inventory-service --timeout=180s
kubectl --context docker-desktop -n incident-lab get pods -l incidentlab/infra-fault=true
```

Recovery refuses a changed context or replaced target UID. If the process died
during initial reservation (journal is `{}`), no mutation could yet have occurred;
an operator should inspect workload scales before removing that invalid reservation.
Do not change context definitions, modify targets concurrently, use other state
paths to bypass exclusion, or invoke multiple clones simultaneously. Journal state
is private operational metadata, not investigator ground truth or credentials.

Existing NetworkPolicies apply but do not establish real enforcement on the current
kindnet cluster. These files add no listeners or exposure; live execution still
requires the existing security gate and operator approval for known CNI limitations.

## Verification

```powershell
python -m unittest discover -s chaos/tests -p test_infra.py -v
.\scripts\start-infra-chaos.ps1 -ValidateOnly -Context docker-desktop
```

Tests mock the API and cover disabled/dry-run behavior, target restrictions,
interruption, partial failure, persistent recovery, UID/context checks and evidence.
They do not certify live OOM behavior, CNI enforcement or node-loss acceptance.