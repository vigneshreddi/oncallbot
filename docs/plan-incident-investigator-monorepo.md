# Plan: Incident Investigator Monorepo (k8s-native, vendor-agnostic logs, 3 services / 3 schemas)

Build a Python investigator that runs as a Kubernetes pod, ingests logs from three Spring Boot microservices through a vendor-agnostic `LogSource` interface (pods now, Datadog/Loki/Splunk later), detects incidents through a pluggable `IncidentTrigger`, and runs an LLM tool-use loop over logs, pod events, build-time code chunks (RAG), the deployed commit's code (GitHub API), and read-only PostgreSQL queries. Output is an evidence-linked report, then (Phase 4) a draft PR. Everything ships as one downloadable monorepo with a three-command demo.

## Requirements

- Log access is vendor-agnostic after collection: pod logs now; Datadog, Grafana Loki, Splunk later via the same interface.
- Incident trigger is pluggable: log-anomaly detector now; Datadog webhook later.
- Investigator runs as a k8s pod, not on the host.
- Monorepo, downloadable, README with very few manual steps; scripts kickstart everything.
- 3 Spring Boot microservices + 3 DB schemas. Trigger endpoints cause real, logical, RCA-able, fixable errors (mutate DB state/flags so normal business flows fail) - not fake log lines.
- Independent `config/services.yaml`: service name, prod commit, GitHub URL, repo path, DB schema, pod selector. Build tools can update it.
- Investigator watches logs of all configured services.
- Local demo cluster uses at least 2 Kubernetes nodes so cross-node scheduling, networking, and node-failure diagnosis can be tested; workloads must be deliberately distributed rather than relying on the scheduler by chance.
- Build-time RAG: code chunks created during the build (Jenkins in production, manual step in the demo) and stored in PostgreSQL; investigator uses them to reduce token usage.
- One PostgreSQL instance hosts business schemas, investigator store, and code index.
- Each service exposes fault-injection admin endpoints covering real runtime failures (OOM, service down, JPA errors, dependency down, malformed JSON from an upstream service, DB pool/lock/schema issues). The catalog is importable into Postman or any API client.
- A chaos runner triggers random faults from that catalog on a schedule, with auto-revert, and records ground truth.
- The investigator documents as much as it can (trace ids, service chain, pods/commits, timeline, k8s events, DB rows with update timestamps); for infra faults it does a quick health check and documents that without reading code; for DB faults it documents the relevant DB state.
- Stack: Python investigator; Java 21 Spring Boot services; LLM hosted + Ollama behind one interface; GitHub API for code; Windows 10 Home laptop with Docker Desktop Kubernetes (fallback: kind inside WSL 2).

## Security non-negotiables

- No public exposure: every Kubernetes Service is `ClusterIP`; never use `NodePort`, `LoadBalancer`, host networking, host ports, Ingress, or public DNS. A local registry, if needed, binds to `127.0.0.1` only.
- Outbound Internet is allowed for required functions (LLM APIs, GitHub, registries, package downloads); this is not a zero-egress project. The security boundary is inbound: NetworkPolicies prevent public Internet sources from initiating traffic into the cluster or pods. Services remain ClusterIP-only; no NodePort, LoadBalancer, Ingress, host networking, or host ports. `kubectl port-forward` is intentional local developer access.
- No host access: no `privileged`, `hostNetwork`, `hostPID`, `hostIPC`, hostPath mounts, Docker socket mounts, device mounts, or host process/filesystem access. The investigator runs inside Kubernetes with namespace-scoped RBAC only.
- Least privilege: service accounts are not automounted into application pods; investigator access is limited to `pods`, `pods/log`, and `events` read/watch in `incident-lab`; chaos permissions are separate, namespace-scoped, and disabled by default.
- Pod hardening: non-root users, `seccompProfile: RuntimeDefault`, no privilege escalation, drop all Linux capabilities, read-only root filesystems with explicit temporary volumes only, resource requests/limits, and pinned image digests in production.
- Secrets: no passwords, tokens, GitHub keys, or LLM keys in manifests, source, images, logs, Postman exports, or reports. Demo secrets are generated locally and preserved across restarts; production uses an external secret manager. Database access is read-only for investigator queries with allowlists, timeouts, row limits, and redaction.
- Supply chain: pin Maven/Python dependencies and base images; run `osv-scanner`/Dependabot or equivalent, `pip-audit`, Maven dependency vulnerability checks, container image scanning, SBOM generation, and secret scanning in CI. Builds fail on critical/high vulnerabilities unless explicitly reviewed and documented.
- Unsafe capabilities are test-only and isolated: fault endpoints are internal-only, disabled by default, rate-limited, non-production builds only, and cannot execute arbitrary commands, accept arbitrary SQL, or modify host/node state.
- Verification gates: fail CI and the local deployment check if a Service is externally exposed, external ingress is allowed, a container is privileged/root, a secret is committed, or a dependency/image scan is unavailable. Outbound Internet is not considered a failure.

## Key design

- Query-oriented `LogSource` protocol: `search(filters, time_range, limit)`, `get_context(trace_id | request_id, window)`, `list_services()`. K8s adapter = collector (stream pods -> store) + query impl over the store. Vendor adapters = query impl over the vendor API, no local store.
- Normalized `LogRecord`: ts, service, pod/host, level, message, logger, exception, stack, trace_id, request_id, attrs, raw.
- `IncidentTrigger` protocol -> `IncidentEvent`: source, services, window, title, hints{trace_ids, request_ids}, raw. Now: `LogAnomalyTrigger`. Also now: generic `POST /webhooks/incident` (vendor payload mapping in Phase 5).
- Cross-service correlation: Micrometer Tracing (OTel bridge) in all 3 services -> `traceId`/`spanId` in ECS JSON logs, propagated via W3C headers.
- Commit source of truth: `services.yaml` `prod_commit`; pod annotation `git-commit` cross-checked; mismatch reported as a finding.
- LLM tools are enforced in application code, not by the model: permissions, time windows, row caps, timeouts, redaction, step/token budgets.
- Every tool call and result reference is written to an evidence table with timestamps and an observed-at marker; DB rows are labelled as current state, not incident-time state.
- Scheduling policy: use pod anti-affinity or topology spread constraints for the three services, and a node selector/affinity for selected workloads so the demo proves which node hosted each pod.
- Node identity is part of the deterministic snapshot: pod name, node name, node condition, placement, restart history, and whether the incident followed a node drain or loss.

## Storage: one PostgreSQL instance

- `postgres` StatefulSet using a pgvector-enabled image, single init SQL file.
- Schemas: `orders`, `inventory`, `payments` (business) | `investigator` (incidents, evidence, log_lines, k8s_events, deployments) | `code_index` (chunks + embeddings).
- Roles:
  - `order_svc`, `inventory_svc`, `payment_svc` own their schema only.
  - `indexer_app` owns `code_index`.
  - `investigator_app` owns `investigator`, SELECT on `code_index`.
  - `investigator_ro` SELECT-only on the 3 business schemas with `statement_timeout`; the only role used by `run_readonly_sql`.

## Build-time code index (RAG)

- `code-indexer` (Python) runs in CI as a Jenkinsfile stage (build -> test -> image -> index-code -> update `services.yaml` `prod_commit`) and locally as a k8s Job launched by `scripts/index-code`.
- Input per service: `repo_url`, `repo_path`, commit. Output: chunks in `code_index` keyed by (service, commit).
- Chunking: Java-aware (class/method) via tree-sitter; DDL/migration files as schema chunks; `services.yaml` dependency graph. Metadata: path, start/end line, symbol, kind, commit, content_hash (unchanged chunks skipped across commits).
- `EmbeddingClient` interface: hosted (OpenAI/Voyage) or Ollama (`nomic-embed-text`). Embedding cost paid once per build.
- Agent tools: `search_code(query, service)` -> top-k chunks at `prod_commit`; `read_file(path, start, end)` fetches exact range from GitHub only when needed. Fallback when no index exists for `prod_commit`: GitHub-direct reads + finding "code index missing for commit X".
- Token accounting per investigation (input/output tokens, tool calls). Verification: run scenarios with RAG on vs off; record reduction in `docs/results.md`.
- CI operational notes: the index-code stage is blocking with retry (a silently missing index degrades the investigator to GitHub-direct); lock on `(service, commit)` for concurrent builds; write all chunks then mark `(service, commit)` ready - the investigator only queries ready indexes; update `services.yaml` `prod_commit` after deploy success, not at build time; Jenkins secrets for PostgreSQL and the embedding provider; build agents need egress to both.
- Embedding cost estimate (verify current provider prices in README): full index of the 3 demo services ~150k-400k tokens, about a cent with a small hosted model (e.g., OpenAI `text-embedding-3-small` ~$0.02/1M tokens); incremental builds ~2k-20k tokens; Ollama `nomic-embed-text` in-cluster $0; a ~1M-line codebase ~10-15M tokens, roughly $0.20-$2 for a full index. Embedding is one-off per commit; recurring spend is investigator LLM calls.

## Incident generators (inputs to the system - not investigator output)

Two ways to cause real incidents for the investigator to respond to. Both are driven by the same catalog, `chaos/faults.yaml`, and neither depends on the investigator.

### Manual: Postman collection

- `api/postman/collection.json` + `api/postman/environment.json`, generated from `faults.yaml`. Import into Postman, Bruno, or Insomnia (via the OpenAPI files) and click a request to enable or disable a fault on a service; then watch the investigator pick it up.

### Automated: chaos runner

- `chaos-runner` (Python Deployment; optional CronJob) reads `chaos/faults.yaml`, picks a weighted-random fault, applies it via the service admin API or the k8s API, auto-reverts after `duration`, then waits a cooldown. One active fault at a time by default.
- Controls: seed for reproducible sequences, dry-run, kill switch (ConfigMap flag), exclude list, max faults per hour. RBAC scoped to the demo namespace only (pods delete; deployments/statefulsets scale).
- Writes `investigator.chaos_events` (fault_id, category, service, started_at, reverted_at, params). This is a log of what the runner did; it is used only to check the investigator's answers afterwards.

### Fault catalog (all three services)

- Uniform admin API per service: `GET /admin/faults` (list + state), `POST /admin/faults/{id}/enable` (optional `duration`, params), `POST /admin/faults/{id}/disable`, `POST /admin/faults/reset`. Faults change state or configuration; normal traffic then fails through real code paths. Faults auto-revert when `duration` elapses.
- Single source of truth `chaos/faults.yaml`: id, service, category (`infra` | `dependency` | `data` | `code`), mechanism, expected symptoms (logs, k8s events, DB), expected root cause, fix type, revert, blast radius. Generated from it: `api/openapi/<service>.yaml`, `api/postman/collection.json` + `environment.json` (Bruno/Insomnia import the OpenAPI), `docs/faults.md`.
- App-level faults v1:
  - `oom-leak` - unbounded cache grows until the container is OOMKilled (infra symptom, code cause).
  - `health-down` - readiness reports DOWN; pod leaves the Service; callers get 503 / connection refused (next service down).
  - `crash-loop` - startup flag makes the process exit; CrashLoopBackOff.
  - `slow-endpoint` - injected latency; callers time out and retry.
  - `jpa-lazy` - lazy association accessed outside a transaction -> `LazyInitializationException`.
  - `jpa-optimistic-lock` - stale `@Version` -> `OptimisticLockException`.
  - `n-plus-one` - fetch strategy switch -> slow queries, pool pressure.
  - `pool-exhaust` - connections held open -> pool acquisition timeout.
  - `row-lock-hold` - long transaction holds row locks -> lock timeout (`55P03`).
  - `schema-drift` - query references a column not in the schema (`42703`).
  - `unique-violation` - duplicate insert (`23505`).
  - `malformed-json` - upstream returns non-JSON / renamed fields; downstream Jackson parse fails.
  - `null-legacy` (scenario A), `retry-dup-payment` (scenario B), `status-enum` (scenario C).
- Infra-level faults applied only by the chaos runner via the k8s API: `pod-delete`, `scale-to-zero` (dependency down), `postgres-down` (StatefulSet to 0), `limit-squeeze` (lower CPU/memory limits).

## Triage-first investigation (investigator output)

- Stage 0 - deterministic snapshot, no LLM: pod phase, restart counts, last termination reason (OOMKilled), k8s events, ready endpoint count per service, recent rollouts and commit annotations, resource usage vs limits (metrics-server), PostgreSQL health (`pg_stat_activity` counts, waits/locks, connections vs `max_connections`, long-running transactions), dependency reachability along the trace chain. Always included in the report.
- Stage 1 - classify: `infra` | `dependency` | `data` | `code` | `unknown`. Rules: OOMKilled / CrashLoopBackOff / ImagePull -> infra; ready endpoints = 0 or 503 / connection refused from a dependency -> dependency; SQLSTATE `23xxx`, `42xxx`, `40P01`, `55P03`, `08xxx` or pool timeout -> data; application stack trace inside `repo_path` -> code. The LLM receives the snapshot and classification with a class-specific budget: infra/dependency use a small budget and do not read code unless evidence points to code (e.g., a leak); data documents DB state; code uses RAG and `read_file`.
- Stage 2 - documentation requirements: trace ids, service chain derived from the trace, per-service pods and commits, timeline, k8s events, involved tables (columns and types from `information_schema`), rows referenced by IDs found in logs with `created_at` / `updated_at` and `observed_at`, row counts, active locks and waits, entity-vs-schema mismatch for `42703`. Values redacted per config; every DB observation labelled observed-at.
- DB tools for the agent: `describe_table`, `find_rows_by_ids`, `db_health`, in addition to `run_readonly_sql`; all use `investigator_ro`.
- All business tables carry trigger-maintained `created_at` / `updated_at` so row update times can be cited.
- Scoring: investigator category and root cause vs `chaos_events` -> per-category precision recorded in `docs/results.md`.

## Demo flow (README, three commands)

1. `up` - deploys PostgreSQL, the 3 services, and the traffic generator.
2. `index-code` - manual, explicit step: indexer Job chunks and embeds each service at its `prod_commit` from `services.yaml`.
3. `start-investigator` - deploys the investigator; on startup it verifies an index exists per service/`prod_commit`, otherwise warns, falls back to GitHub-direct, and records a finding.

Optional: `start-chaos` deploys the chaos runner; `trigger <fault-id>` applies one fault manually; the Postman collection in `api/postman/` does the same from an API client.

README states: in production, `index-code` is a Jenkins/build-tool stage after the image build (reference Jenkinsfile included); it is manual here only to make the step visible.

## Scenarios (realistic, fixable)

- A - legacy import null: `POST /inventory/admin/import-legacy` creates SKUs with `reserved_qty = NULL`; order-service unboxes `int` -> NPE. Fix: null-safe default. 2 services, `inventory` schema.
- B - retry duplicate payment: `POST /payments/admin/customers/{id}/tier=slow` makes charge slow; order-service times out and retries; payment-service inserts a second row -> unique violation on `order_id`; order stuck `PENDING`. Fix: idempotency check. 2 services, `payments` + `orders`.
- C - unknown status: `POST /payments/admin/flags/authorize-first=true` returns `AUTHORIZED`; order-service enum deserialization fails; inventory reservation never released. Fix: enum + tolerant mapping + release-on-failure. 3 services, 3 schemas.
- Traffic generator Deployment runs healthy order flows continuously so failures appear amid normal traffic.

## Monorepo layout

- `README.md`, `.env.example`, `Jenkinsfile` (reference)
- `scripts/up.ps1|sh`, `scripts/index-code.ps1|sh`, `scripts/start-investigator.ps1|sh`, `scripts/start-chaos.ps1|sh`, `scripts/trigger.ps1|sh <fault-id>`
- `config/services.yaml` (mounted ConfigMap)
- `chaos/faults.yaml` (fault catalog, source of truth), `chaos/runner/` (Python), `scripts/gen-api-docs` (faults.yaml -> OpenAPI, Postman, docs)
- `api/openapi/*.yaml`, `api/postman/collection.json`, `api/postman/environment.json`
- `deploy/kind/cluster.yaml`; `deploy/k8s/` (kustomize base + overlays/local): postgres StatefulSet + init SQL, 3 services, traffic-gen, indexer Job, chaos-runner (Deployment, SA, Role scoped to demo namespace), investigator (Deployment, SA, Role/RoleBinding for pods, pods/log, events get/list/watch, ConfigMap, Secrets, Service)
- `services/order-service`, `services/inventory-service`, `services/payment-service` (Maven, Java 21), `services/traffic-generator` (Python)
- `indexer/` (chunking, embeddings, writer)
- `investigator/` (`sources/`, `triggers/`, `store/`, `detector/`, `triage/{snapshot.py, classify.py}`, `agent/{llm.py, loop.py, tools/}`, `report/`, `api.py`, `cli.py`, `config.py`)
- `docs/` (`results.md`, `faults.md`)

## Phases

0. Environment: WSL 2; Docker Desktop with a 2-node Kubernetes cluster (or 2-node kind inside WSL 2 Ubuntu); kubectl; Java 21 + Maven; Python 3.12; fork monorepo to GitHub; read-only GitHub token; LLM API key. Allocate enough Docker Desktop memory for both nodes and the application workloads.
1. Target system: monorepo skeleton, deploy scripts, `services.yaml`, PostgreSQL (schemas + roles, `created_at`/`updated_at` on all business tables), 3 services (ECS JSON logs, tracing, Actuator, `GIT_COMMIT` env + pod annotation), `/admin/faults` API implementing the app-level fault catalog, `chaos/faults.yaml` + generated OpenAPI/Postman/docs, traffic generator.
2. Investigator core in-cluster + index + chaos: config loader (hot reload), `KubernetesLogSource` collector + query, store, detector, `LogAnomalyTrigger`, webhook stub, Stage 0 snapshot collectors (k8s, metrics, PostgreSQL health), REST API (incidents, log search); `code-indexer`, indexer Job, Jenkinsfile; `chaos-runner` with `chaos_events`.
3. Agent: Stage 1 classification and class-specific budgets; `LLMClient` (Anthropic + OpenAI-compatible for OpenAI/Ollama); tools (`search_logs`, `get_log_context`, `get_pod_events`, `get_deployment_metadata`, `search_code`, `read_file` pinned to SHA, `run_readonly_sql`, `describe_table`, `find_rows_by_ids`, `db_health`, `record_finding`); evidence, token accounting, Markdown report + `GET /incidents/{id}/report`; RAG on/off comparison; scoring against `chaos_events`.
4. Fix proposal: `propose_fix` branches from `prod_commit`, applies patch under `repo_path`, opens draft PR via GitHub API with report link; never auto-merge.
5. Vendor adapters: `DatadogLogSource`, `LokiLogSource`, `SplunkLogSource`; Datadog webhook payload mapper -> `IncidentEvent`.

## Verification

### Incident generators (do the triggers work?)

- Postman collection imports cleanly; every fault in `faults.yaml` can be enabled from it, produces its documented symptom in the target service (e.g., `oom-leak` ends in an OOMKilled event; `health-down` on inventory makes order-service return 503 with connection-refused logs), and reverts on `disable` or `duration`.
- Chaos runner with a fixed seed applies the same fault sequence twice, never overlaps faults, reverts each, honours the kill switch, and records every application in `chaos_events`.

### Platform and investigator

1. Phase 1: from a clean cluster, `up` ends with all pods Ready; `kubectl logs` shows ECS JSON with `service.name` and `trace.id`; enabling `status-enum` yields errors in all three services sharing one `trace.id`; `investigator_ro` can SELECT `orders.*` but cannot INSERT or read `investigator.*`.
2. Phase 2: 500 repeated failures -> one incident per fingerprint; deleting a service pod does not stop ingestion; `list_services()` returns all three; webhook POST creates an incident; after `index-code`, `code_index` has chunks for all three services at `prod_commit`; re-indexing after a one-file change writes only changed chunks; `start-investigator` before `index-code` warns instead of crashing; Stage 0 snapshot for an `oom-leak` incident contains the OOMKilled termination reason and restart count; pods are distributed across both nodes and a controlled node drain/loss is classified with node names and affected services.
3. Phase 3 (investigator reports): for code faults the report names the correct file/line at `prod_commit` and cites relevant DB rows with `updated_at`; for infra faults (`pod-delete`, `scale-to-zero`, `postgres-down`, `limit-squeeze`) the report classifies correctly, contains the snapshot, and makes no `search_code`/`read_file` calls; for data faults the report includes table definitions, referenced rows, and lock/connection state; run each fault 5x per LLM backend, RAG on and off; compare the investigator's category and root cause with `chaos_events` and record per-category precision, correct / wrong / insufficient-evidence counts, and token reduction in `docs/results.md`; evidence table contains every tool call.
4. Phase 4: draft PR opened on the fork, base = `prod_commit`, report linked, no merge.

## Decisions

- Query-oriented `LogSource` defined in Phase 2; pod adapter is collector + query; vendor adapters are query-only.
- Trigger abstraction and webhook endpoint exist from Phase 2; vendor payload mapping deferred to Phase 5.
- Scenarios mutate state through admin endpoints so failures occur in real business paths.
- Single PostgreSQL for business data, investigator store, and code index; role separation enforces boundaries.
- RAG index built before the investigator starts; manual in demo, CI stage in production.
- Faults are real state/config changes in the services or k8s actions by the chaos runner; `faults.yaml` is the single source for endpoints, API docs, chaos, and scoring.
- Investigation is triage-first: deterministic snapshot and classification before any LLM call; code reading only when the class warrants it.
- Chaos runner RBAC is limited to the demo namespace; kill switch and one-fault-at-a-time are defaults.
- Security is a release gate: no public Service, no arbitrary pod egress, no host mounts/sockets, non-root runtime, least-privilege RBAC, generated secrets, and dependency/container/secret scans must pass before the demo is considered complete.
- Two nodes are the minimum local demo topology; one node is insufficient for the cross-node failure scenarios in this plan.
- Out of scope: historical DB reconstruction, replay/reproduction environments, auto-merge, multi-cluster, auth UI.

## Open questions

## Implementation Checkpoint (2026-10-03)

- Implemented: catalog-driven Postman/manual/seeded chaos workflows with cleanup; AST chunking, original-source line reads, atomic ready indexes, pgvector retrieval with lexical fallback; reference Jenkinsfile; separated database roles; bounded native LLM tool protocols and evidence; Datadog/Loki/Splunk adapters and payload mapping; opt-in reviewer-confirmed draft PR API; automated regression and security CI checks.
- Verified locally: role-separated target rollout without data reset; manual index Job (19/23/20 chunks); indexed investigator startup; CONFIRMED order; schema-drift incident categorized data and analyzed; retry-duplicate and status-enum workflow failure/cleanup cycles; 106 tests plus 75 subtests passed. Two disposable database integration tests require a test database and were skipped.
- Outbound Internet remains allowed. The local security gate intentionally refuses isolation approval: Docker Desktop uses kindnet and independent NetworkPolicy connectivity proof is missing. No privileged network installer or cluster reset was performed to bypass this blocker.
- External-provider acceptance requires locally supplied credentials. Live LLM/embedding/vendor/GitHub requests and token savings were not validated. Synthetic `local-demo` revisions are not accepted for real GitHub PR provenance.
- Remaining extended scope: policy-capable CNI/probe acceptance, infrastructure/node/JPA fault families beyond current HTTP/data workflows, authenticated vendor ingress relay, full live-provider evaluation. No vulnerability-free or absolute external-security guarantee is claimed.

- Demo repo visibility: public fork recommended (simpler token, portfolio-friendly).
- Ollama for investigation only after the hosted model passes acceptance runs; local tool-calling quality varies.
- Docker Desktop on Windows 10 Home is not explicitly listed in Docker's supported editions; confirm before Phase 1, else scripts target kind-in-WSL 2.
