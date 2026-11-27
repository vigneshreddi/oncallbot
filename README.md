# OnCallBot

[Interactive architecture and incident-flow explorer](docs/architecture.html) - standalone HTML with component details, source references, and build-time RAG flow. Open locally in a browser; no server or external dependencies required.

## Acceptance Update (2026-10-03)

- Network probes across both nodes passed same-namespace connectivity, outside-namespace denial, outbound HTTPS, and loopback-only forwarding. Run `scripts/probe-network.ps1` to collect fresh evidence. Genuine external-host denial and policy-engine identification remain unresolved; the strict isolation gate is not bypassed. See [docs/network-isolation.md](docs/network-isolation.md).
- Isolated infrastructure workflows support pod deletion, bounded OOM, service scale-to-zero, and explicitly simulated node unavailability. Live OOM produced `OOMKilled` and restored zero replicas. Actual node failure/drain requires an operator; the runner never modifies nodes. See [docs/infra-chaos.md](docs/infra-chaos.md).
- Real JPA lazy-loading and optimistic-lock failures passed against PostgreSQL. Demo activation was removed afterward; endpoints are disabled by default. Separate Postman exports are `api/postman/inventory-jpa.collection.json` and `api/postman/inventory-jpa.environment.json`. See [services/inventory-service/JPA-FAULTS.md](services/inventory-service/JPA-FAULTS.md).
- Integrated Python suite: 180 passed, 2 skipped, 81 subtests passed. Five Hibernate/JPA tests also passed. Skips require a disposable RAG test database.
- `scripts/live-acceptance.py` defaults to configuration checks without network calls. Locally supplied credentials and explicit provider enablement are required for billed/live probes; no secrets should be shared through chat. No credentials are currently configured, so live-provider acceptance and measured token savings remain blocked.

Evidence-driven incident investigation, from production signals to reviewable fixes.

Kubernetes-native incident investigator for three independent Spring Boot microservices (order → inventory → payment, backed by three PostgreSQL schemas). Real fault scenarios exercise business paths; an in-cluster investigator collects pod logs, takes a deterministic snapshot, classifies incidents, optionally uses an LLM with bounded tools, and writes an evidence-linked report.

## Credentials — read this first

**Nothing in this repository contains credentials, and nothing you add may either.**

1. Copy `.env.example` to `.env` and fill in what you need. `.env` is git-ignored.
2. `scripts/up.ps1` loads `.env` into a Kubernetes Secret (`app-secrets`) and generates the PostgreSQL password into another Secret. Values are never printed.
3. Without a `.env`, the stack still runs with the LLM disabled; reports contain the deterministic snapshot and rule-based classification only.

Database passwords are generated or reused in `postgres-secret` by deployment, never supplied in `.env`.
Bootstrap creates schemas and the indexer's `code_index.rag_*` tables under their dedicated owners,
then grants the investigator read access to current and future index tables. Application initializers
create only their own tables in existing schemas. Business SQL tools require the Secret's `RO_PASSWORD`
for `investigator_ro`; without it they fail closed, never falling back to the application DSN.
Existing PostgreSQL storage and credential values are preserved on subsequent deployments.

| Key | Purpose | Required |
|---|---|---|
| `LLM_PROVIDER` / `LLM_MODEL` / `LLM_API_KEY` / `LLM_BASE_URL` | LLM for the investigation loop (`anthropic`, `openai`, `ollama`, or `none`) | optional |
| `EMBEDDING_PROVIDER` / `EMBEDDING_MODEL` / `EMBEDDING_API_KEY` / `EMBEDDING_BASE_URL` | Embeddings for the build-time code index | optional |
| `GITHUB_TOKEN` | Read-only token so `read_file` can fetch code at `prod_commit` from your fork | optional |
| `DATADOG_API_KEY` / `DATADOG_APP_KEY` | Datadog keys with log read permission, required for `LOG_PROVIDER=datadog` | provider-specific |
| `LOKI_USERNAME` / `LOKI_PASSWORD` or `LOKI_TOKEN` | Loki read-only basic authentication or bearer token; both modes cannot be combined | provider-specific |
| `SPLUNK_TOKEN` | Bearer token with read-only search access to `SPLUNK_INDEX` | required for Splunk |

Rules for contributors are in `.github/copilot-instructions.md`; the deployment gate (`scripts/security-check.ps1`) fails if `.env` is ever tracked.

## Security model

- Intended configuration: `ClusterIP` Services only, no Ingress, external IPs, host networking, host ports, host mounts or privileged containers. Workloads must use fixed non-root UIDs, `RuntimeDefault` seccomp, no privilege escalation, all capabilities dropped and read-only root filesystems. The runtime gate checks deployed pods, including init and ephemeral containers, not just manifests.
- NetworkPolicies express ingress restrictions; their existence does **not** prove isolation. The current Docker Desktop cluster was observed using `kindnet`, without a recognized NetworkPolicy enforcement engine. Treat external ingress as **unverified**, and the deployment security gate as failed, until actual enforcement is demonstrated. Kubernetes policy implementations can also exempt node/host traffic, so namespace-only probes are insufficient.
- Outbound internet remains allowed for LLM APIs, GitHub and registries. Keep manual port-forwards bound to localhost (kubectl's default, or explicit `--address 127.0.0.1`); never use `--address 0.0.0.0`. Local forwards bypass normal Service exposure and are not proof of policy enforcement. Fault endpoints are not authenticated and must not be exposed publicly.
- Run `./scripts/security-check.ps1 -Static`, `./tests/security/test-security.ps1` and `./tests/security/test-ci.ps1` for repository/fixture checks, then `./scripts/security-check.ps1` for read-only live inspection. Failure or unavailable evidence is not approval. No privileged probes, host mounts or node filesystem access are used.
- To evaluate a policy-capable CNI, supply independently collected JSON with `-EnforcementEvidence <local-file>`. Evidence must be less than one hour old, match the gate's `context`, `namespace`, `policySha256`, sorted `nodeUids` and sorted `targetPodUids`, and include `observedAt`, inspected `cni` configuration and detailed `probeDetails` (source, destination, ports, commands, positive controls and observed results). Boolean results `outsideNamespaceDenied`, `externalHostDenied`, `sameNamespaceAllowed`, `outboundInternetAllowed` and `localhostPortForwardAllowed` must all be true. Test every target pod/node and exposed port from an unauthorized namespace and an external host, including direct node/pod paths; a timeout without a reachable positive control is inconclusive. This is operator-supplied evidence, not an automatic certification. Keep evidence and reports outside the repository; never include credentials.
- [Security CI](.github/workflows/security.yml) uses SHA-pinned actions, read-only repository permissions and version-pinned, checksum-verified gitleaks/Trivy/Maven tools. It scans current files and full Git history with redacted gitleaks reports, resolves Python transitive dependencies using pip-audit, and runs Maven Dependency-Check (CVSS >= 7 fails). Every runtime image, including the pinned PostgreSQL image, gets a CycloneDX SBOM and Trivy scan; HIGH/CRITICAL findings, including unfixed CVEs, fail. Reports, SBOMs and image identities are retained for 14 days even when a scan fails. Feed/download/build errors also fail; there are no suppressions or automatic waivers.
- Configure branch protection to require `Security / security-gate`; adding the workflow alone does not prevent merges. CI has no cluster credentials and cannot certify live CNI enforcement. Dependabot proposes dependency, image and action updates, but scanner CLI pins require reviewed updates. Publisher checksums detect corrupted downloads, not a compromised publisher. This lab is **not fully secured**, and green static checks do not resolve live exposure, CVEs, authorization gaps or other untested risks.

## Prerequisites

Docker Desktop with a **2-node Kubernetes cluster** named `desktop-control-plane` and `desktop-worker`, `kubectl`, and Windows PowerShell. `scripts/up.ps1` labels these nodes for workload placement.

## Deploy and index

```powershell
kubectl config use-context docker-desktop
.\scripts\up.ps1            # postgres and 3 services; unproven CNI isolation is a warning (-StrictIsolation makes it fatal)
.\scripts\index-code.ps1    # build and run the code-indexer Job
.\scripts\start-investigator.ps1 # refuses to start without ready indexes
```

The first script builds separate service images and bootstraps schema-scoped database roles without resetting existing data. New investigator deployment follows manual indexing. The reference Jenkinsfile runs the same indexing stage in CI. Embeddings are optional; lexical retrieval works without a provider. See [docs/rag.md](docs/rag.md) for atomic indexing, source provenance, semantic retrieval, and comparison commands.

Optional: `.\scripts\start-chaos.ps1` deploys the chaos runner. It selects eligible entries in `chaos/faults.yaml`, enables a service fault, provokes an order request, disables the fault after its configured duration, and records the run in `investigator.chaos_events`.

Separate, default-disabled infrastructure scenarios are available through
[`scripts/start-infra-chaos.ps1`](scripts/start-infra-chaos.ps1): allowlisted demo-service
scale-zero, isolated pod deletion, memory-limit OOM and **simulated** node unavailability.
See [docs/infra-chaos.md](docs/infra-chaos.md) for scoped RBAC, installation, evidence,
restoration and safety limits. Actual node drain/failure is unsupported and requires
an operator; there is no infrastructure HTTP/Postman mutation endpoint.

## Cause a real incident

```powershell
Start-Process kubectl -ArgumentList 'port-forward -n incident-lab svc/order-service 18080:8080'
Start-Process kubectl -ArgumentList 'port-forward -n incident-lab svc/inventory-service 18081:8080'
Start-Process kubectl -ArgumentList 'port-forward -n incident-lab svc/payment-service 18082:8080'
Start-Process kubectl -ArgumentList 'port-forward -n incident-lab svc/investigator 18090:8080'
.\scripts\trigger.ps1 -Fault null-legacy      # enables a fault and sends a real POST /orders request
curl.exe http://localhost:18090/incidents
curl.exe http://localhost:18090/incidents/<id>/report
```

The trigger script uses the existing service forwards above (order `18080`, inventory `18081`, payment `18082`, matching the Postman environment); keep them running. Install `chaos/requirements.txt` first. Available faults are `null-legacy`, `retry-dup-payment`, `status-enum`, `schema-drift`, `health-down`, and `slow-endpoint` — see [`chaos/faults.yaml`](chaos/faults.yaml). The Postman collection in [`api/postman/`](api/postman/) drives service admin endpoints; use `scripts/postman-port-forward.ps1` to expose a service locally.

The investigator also exposes `GET /health`, `GET /logs`, and `POST /webhooks/incident`. The generic webhook remains available alongside explicit vendor endpoints described below. Reports combine the collected snapshot with rule-based classification and, when configured, bounded LLM tool calls. Database rows returned to the investigator represent current observed state, not a reconstruction of state at incident time.

Inventory's separate JPA demos require both `SPRING_PROFILES_ACTIVE=demo` and
`INVENTORY_DEMO_FAULTS_ENABLED=true`; the manifest explicitly defaults to disabled.
See [services/inventory-service/JPA-FAULTS.md](services/inventory-service/JPA-FAULTS.md)
for bounded prepare/provoke/cleanup, PostgreSQL acceptance and restoration details.
Use `scripts/trigger.ps1 -Catalog chaos/inventory-faults.yaml -Fault jpa-lazy -DurationSeconds 0`
(or `jpa-optimistic-lock`) after explicit activation.
The separate [api/postman/inventory-jpa.collection.json](api/postman/inventory-jpa.collection.json)
and [api/postman/inventory-jpa.environment.json](api/postman/inventory-jpa.environment.json)
leave the existing Postman exports untouched.

## Vendor logs and alerts

Set `LOG_PROVIDER=kubernetes|datadog|loki|splunk` in `.env`; Kubernetes is the default.
Datadog uses `DATADOG_SITE` (an allowlisted Datadog domain) and `/api/v2/logs/events/search`.
Loki uses `LOKI_BASE_URL`, optional `LOKI_TENANT_ID`, and `/loki/api/v1/query_range`;
streams must have a `service` label matching the configured service registry.
Splunk uses `SPLUNK_BASE_URL` (HTTPS management endpoint), `SPLUNK_INDEX`, and a single
synchronous `/services/search/jobs/export` request. Splunk events must expose a `service` field.
There are no job-creation calls, polling loops, or vendor pagination.

Incidents are grouped per issue, not per alert. The fingerprint is service + error type +
message with numbers and ids removed, so the same error from collected logs, Datadog, Loki or
Splunk joins one incident row; alerts without error details are grouped by alerting rule
(Datadog monitor, Loki `alertname`, Splunk saved search). Each occurrence is stored in
`investigator.incident_occurrences` with source, alert/event id, pod, `trace_id`, `span_id`,
`correlation_id` and `request_id` (from log fields, `key:value` tags or the optional Datadog
`span_id`/`correlation_id`/`request_id`/`error_type` payload fields). Re-delivered alerts
(same source and id) are ignored. `GET /incidents/{id}/occurrences` lists them.

`GET /logs` and the log tools accept `service`, literal `text`, `limit`, and `time_window`
(lookback seconds ending now). Default lookback is `LOG_QUERY_WINDOW_SECONDS=900`, maximum
86400 seconds; sources cap results at 200 and `search_logs` at 100. API out-of-range values
are rejected; direct source/tool inputs are clamped. HTTP timeout is
`LOG_HTTP_TIMEOUT_SECONDS` (1..60 seconds), with a 2 MiB response cap and a streaming deadline
checked between chunks. Provider errors are sanitized. TLS certificate verification is always
enabled, redirects and environment proxies are disabled, and neither models nor webhooks can
choose query endpoints. Private certificate authorities must be trusted by the runtime OS;
there is no insecure TLS switch.

Vendor query results retain `service`, `pod`, `event_at`, `observed_at`, `level`, `trace_id`,
`message`, and `raw`. Vendor selection stops the Kubernetes log collector only; the existing
investigation flow still uses Kubernetes/database triage and local incident storage.
Historical webhook event times are recorded as evidence, not used to move the agent's
current-time log query window. No agent/store changes are included here.

Explicit POST endpoints validate payloads against configured services before persistence:

- `/webhooks/datadog`: configure a custom monitor webhook with `alert_id` or `monitor_id`,
	`monitor_name`, `alert_title` or `title`, `date` (Unix seconds/milliseconds or ISO-8601),
	optional event `id`, `trace_id`, and `tags` (comma-separated string or list).
	Exactly one `service:<name>` tag or explicit `service` is required; conflicting services
	are rejected. Optional `alert_status`, `alert_transition`, `event_type`, and `text` are retained.
- `/webhooks/loki`: Alertmanager-style `alerts` array (maximum 20), each with
	`labels.service`, `startsAt`, `fingerprint`, optional `labels.trace_id`, and annotations.
	Firing and resolved events are accepted and labeled as such in evidence. Standard envelope
	metadata is accepted; `generatorURL` and `externalURL` are never fetched.
- `/webhooks/splunk`: `search_name`, `sid`, and `result` containing `service`, `_time`,
	optional `message`, `_raw`, and `trace_id`. `results_link` is never fetched.

Unknown payload fields are rejected. Deduplication includes provider, service, event identity,
and event time. Webhooks do not authenticate vendor senders; keep them behind the existing
private network boundary or an authenticated operator-managed relay. Manifests and deployment
scripts are unchanged; ensure the investigator receives the selected environment settings.

Mock-only tests (no provider credentials or live requests):

```powershell
python -m unittest discover -s investigator/tests -p 'test_vendor*.py' -v
```

## Provider acceptance

[scripts/live-acceptance.py](scripts/live-acceptance.py) defaults to offline configuration
checks. It loads the root `.env` locally if present; process environment values take
precedence, including empty values. The loader supports literal `KEY=value`, quoted
values and `export`, but does not expand variables or execute shell expressions.
There is no requirement to create `.env`. Credential values are never printed.
Git HTTPS authentication, credential helpers and `GH_TOKEN` do not establish that
the application's `GITHUB_TOKEN` is configured.

```powershell
python scripts/live-acceptance.py --check-only
python -m pytest tests/test_live_acceptance.py -q
```

The CLI prints JSON and writes the same safe report to the git-ignored
[tmp/](tmp/) directory, defaulting to `tmp/live-acceptance.json`. `--report` may select
another JSON path only inside that directory. Reports contain configuration presence,
fixed status/error codes, numeric HTTP status and validated usage, never credentials,
endpoints, model names, user/repository identities, provider bodies or exception text.
`configured` means locally present and structurally valid, not authenticated or a
verified model. Failed checks and invalid configuration exit 1; missing providers are
skipped and do not fail the run. No network client is imported for check-only mode.

Live calls require a separate explicit `--enable` for each provider. These commands
can incur provider charges; use only models/endpoints you intend to test:

```powershell
python scripts/live-acceptance.py --enable llm
python scripts/live-acceptance.py --enable embeddings
python scripts/live-acceptance.py --enable datadog --enable loki --enable splunk
python scripts/live-acceptance.py --enable github --github-repo owner/repo
python scripts/live-acceptance.py --enable rag
```

LLM acceptance uses the native OpenAI-compatible or Anthropic client sequentially:
one forced synthetic lookup tool turn and one final answer, at most 128 output tokens
per request, no retries. Only a mocked marker question and fixtures are sent, never
incident data. Unexpected tools/arguments, additional tools, incorrect final answers
and missing/invalid reported usage fail the check. Embeddings use the native query
helper for one short synthetic string and validate the returned vector. Vendor adapters
perform one read-only query for a synthetic service/text, over 60 seconds with limit 1;
empty results are valid connectivity acceptance. No vendor results are sent to models
or reports. GitHub uses only GET `/user` and optional GET `/repos/owner/repo`, with
no Git commands, branches, writes or PRs. TLS is verified, redirects and environment
proxies are disabled, HTTP timeouts are 15 seconds and responses are size capped.

`--enable rag` independently authorizes four LLM requests: the same synthetic task
and model with a full fixture corpus (off), then native lexical fixture retrieval (on).
Both answers must pass, and only actual provider-reported input/output token counts
are recorded. This verifies a synthetic retrieval comparison, not the deployed
PostgreSQL/vector index, production investigations or answer quality in general.
LLM inference cost and embedding cost are separate fields and remain `null` because
there is no verified pricing source; the lexical RAG comparison makes zero embedding
calls. No savings are fabricated or claimed. Enabling both `llm` and `rag` authorizes
six LLM requests in total. Embedding acceptance needs its own `--enable embeddings`.

## Layout

- `services/order-service`, `services/inventory-service`, `services/payment-service` — independent Spring Boot applications and images, each with its own database schema and fault/admin endpoints
- `services/demo-service` — earlier single-image prototype; it is not built or deployed by `scripts/up.ps1`
- `investigator/` — collector (`sources.py`), store, triage (`triage.py`), LLM interface (`llm.py`), tools with enforced limits (`tools.py`), agent loop and report (`agent.py`), API (`api.py`)
- `indexer/` — build-time source chunker with optional embeddings, writing to `code_index.chunks`
- `chaos/` — `faults.yaml` catalog and the runner
- `config/services.yaml` — service registry (repo, `prod_commit`, schema, pod selector); mounted as a ConfigMap
- `deploy/k8s/` — manifests; `scripts/` — up, index, chaos, trigger, security gate
- `docs/plan-incident-investigator-monorepo.md` — the plan this repo implements

## Verified Status (2026-10-03)

- Local deployment: three independent services across two nodes; successful order returned `CONFIRMED`; a bounded schema-drift scenario produced an analyzed data incident.
- Database: schema-scoped application/indexer/investigator roles, restricted chaos role, and read-only diagnostic role. All seven roles are non-superuser; diagnostic SELECT succeeds while UPDATE is denied.
- Manual indexing: 19 order, 23 inventory, and 20 payment AST chunks marked ready. Full original source preserves line references; retrieval supports pgvector similarity when configured and lexical fallback otherwise.
- Investigator: bounded native provider tool conversations, trace-context log retention, evidence, status/error handling, webhook validation, vendor adapters, and reviewed draft-PR endpoint. Draft PRs never auto-merge; see [docs/draft-fixes.md](docs/draft-fixes.md). The registry targets `vigneshreddi/oncallbot` with `pr_target_branch: main`; after pushing, `scripts/use-pushed-commit.ps1` switches `prod_commit` from `local-demo` to the pushed SHA (git-ignored `config/services.deployed.yaml`). The model writes fixes through the validate-only `propose_patch` tool, and the investigator opens them as AI-labelled draft PRs (`FIX_PR_MODE=auto`, or `approve` to wait for `POST /incidents/{id}/open-pr`). If a draft PR is not good enough, `scripts/incident-handoff.ps1 -IncidentId <id>` (or `GET /incidents/{id}/handoff`) produces a Markdown brief of the saved investigation to continue in Copilot Chat.
- Fault generators: Postman exports generated from catalog workflows and opt-in seeded chaos with cleanup. Generator ground truth is private and unavailable to the investigator.
- Regression suite: 106 tests and 75 subtests passed; two disposable pgvector integration tests skipped without their test database. Static repository hygiene passed.
- CI includes dependency, image, secret, and SBOM checks. A scanner failure or findings are not treated as approval.

### Operational Blockers

Gaps observed in the first full local run (incident 15, `null-legacy`, 2026-10-04), not yet fixed:

- Investigator Role lacks `apps/deployments` and cluster-scoped `nodes` read; kind has no metrics-server, so usage is unavailable.
- Rule-based mode collects no database evidence (for example the `inventory` row with null `reserved_qty`), so root cause stays unconfirmed.
- The snapshot is stored three times in the report (top level, `services`, evidence step 2).
- Readiness-probe failures from a recent rollout are not separated from incident evidence.
- Demo sources contain `BUG:` comments that the RAG index returns verbatim.
- `trace_id` is null in logs, so cross-service correlation is unavailable.

- The current Docker Desktop cluster uses kindnet. Presence of NetworkPolicy objects does not prove enforcement. The full local security gate correctly fails until a policy-capable CNI and independent connectivity probes establish isolation. ClusterIP and loopback-only forwards remain required, but are not an absolute security guarantee.
- Live OpenAI acceptance passed on 2026-10-05 (`gpt-5.4-mini` tool call, `text-embedding-3-small` at 1536 dimensions); the index for commit `66c74c7` uses OpenAI embeddings. Newer OpenAI models require `max_completion_tokens`. Datadog, Loki, Splunk and GitHub flows are still covered by mocks only. No measured live-model token savings are claimed.
- Demo revisions are explicitly `synthetic-local-demo`. Real Git SHAs and a configured repository are required for verified commit provenance and draft PR creation.
- Live vendor webhook access needs an authenticated private relay; public exposure is not enabled by this repository.
- Inventory's two opt-in JPA workflows passed bounded local PostgreSQL acceptance and were disabled again. Additional fault categories, actual node-loss acceptance, and exhaustive scenario evaluation remain beyond the verified workflows.
