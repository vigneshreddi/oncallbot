# Inventory JPA Demo Faults

Disabled by default. Both `SPRING_PROFILES_ACTIVE=demo` and
`INVENTORY_DEMO_FAULTS_ENABLED=true` must be set on inventory-service.
Neither setting alone exposes the endpoints or initializes fixture tables.
The inventory deployment manifest explicitly sets the opt-in to `false` and does
not activate the demo profile. Keep both gates disabled outside bounded demos.

The service uses its existing datasource and inventory database role. Hibernate
DDL generation and Open Session in View are disabled. Only the enabled demo
initializer creates `inventory.jpa_fault_batch` and
`inventory.jpa_fault_reservation`; it does not alter JDBC `stock` or its null
legacy-import behavior. Credentials continue to come from the existing database
environment variables/Secrets.

Run one workflow at a time against one inventory replica, with a connection pool
of at least two connections. The fixture has fixed ID 1, and the scenario methods
serialize requests within the process. There are no worker threads, timers,
host access, caller-supplied SQL, or persistent fault flags.

| Phase | Method | Path | Expected |
| --- | --- | --- | --- |
| Prepare | POST | `/inventory/admin/jpa/prepare` | 200; replace only fixture ID 1 |
| Lazy provoke | POST | `/inventory/admin/jpa/lazy/provoke` | 500; Hibernate `LazyInitializationException` |
| Optimistic provoke | POST | `/inventory/admin/jpa/optimistic-lock/provoke` | 500; stale version update |
| Cleanup | POST | `/inventory/admin/jpa/cleanup` | 200; idempotently delete fixture ID 1 |

Lazy loading accesses a real `@OneToMany` collection after the read transaction
has closed. Optimistic locking keeps the first transaction's `@Version` entity
stale while a `REQUIRES_NEW` transaction on another connection commits a competing
update. Flushing the stale entity fails and rolls back the first transaction;
the winner's quantity and version advance by one. Repeated provoke requests
repeat this interleaving without sleeps or scheduler-dependent races. Calling a
provoke endpoint before prepare returns 409 rather than a synthetic JPA failure.
Each provoke path emits an ERROR diagnostic with the request's trace context
immediately before the operation that throws; the real exception still reaches
Spring's HTTP error handling.

Cleanup removes only the fixture rows, not the dedicated tables. If a client
stops after prepare/provoke, execute cleanup explicitly. The fixture is constant
size and never modifies ordinary stock. Disabling the property or removing the
demo profile requires a restart and removes the endpoints.

## Catalog Integration

`chaos/inventory-faults.yaml` is a standalone version-2 catalog accepted by the
existing chaos runner using `--catalog chaos/inventory-faults.yaml --fault
jpa-lazy` (or `jpa-optimistic-lock`). Both entries use zero duration and are
excluded from random chaos selection. The runner still performs cleanup after
failures. The shared catalog and existing `api/postman/collection.json` and
`api/postman/environment.json` are left unchanged. Separate generated artifacts
are `api/postman/inventory-jpa.collection.json` and
`api/postman/inventory-jpa.environment.json`.

Regenerate the JPA-only Postman files through a separate output directory:

```powershell
python scripts/generate-postman.py --catalog chaos/inventory-faults.yaml --output-dir tmp/inventory-jpa-postman
python scripts/generate-postman.py --catalog chaos/inventory-faults.yaml --output-dir tmp/inventory-jpa-postman --check
Move-Item -Force tmp/inventory-jpa-postman/collection.json api/postman/inventory-jpa.collection.json
Move-Item -Force tmp/inventory-jpa-postman/environment.json api/postman/inventory-jpa.environment.json
python -m unittest chaos.tests.test_inventory_postman
```

Use `--extra-catalog chaos/inventory-faults.yaml` for a merged collection, always
with a separate output directory to preserve existing Postman edits. Duplicate
IDs across catalogs are rejected. Default generator invocation continues to use
only the shared catalog. After explicitly enabling both service gates and opening
an inventory-only loopback port-forward, either workflow can also be run with:

```powershell
kubectl port-forward --address 127.0.0.1 -n incident-lab svc/inventory-service 18081:8080
# In another terminal with the configured Python interpreter:
.\scripts\trigger.ps1 -Fault jpa-lazy -Catalog chaos/inventory-faults.yaml -DurationSeconds 0
.\scripts\trigger.ps1 -Fault jpa-optimistic-lock -Catalog chaos/inventory-faults.yaml -DurationSeconds 0
```

Do not combine this catalog with background chaos traffic. Run cleanup before
restoring the original deployment environment, then wait for the disabled rollout.

## Focused Validation

```powershell
docker build -f services/inventory-service/Dockerfile.jpa-tests -t incidentlab-inventory-jpa-tests:local services/inventory-service
python -m unittest chaos.tests.test_inventory_postman chaos.tests.test_postman chaos.tests.test_workflow
```

The Maven tests use real Hibernate transactions with H2 PostgreSQL compatibility
mode, not mocked repositories or manufactured exceptions. They verify exception
types, repeated optimistic conflicts and committed versions, HTTP statuses,
idempotent cleanup, isolation from other rows, and both activation gates.

## Local PostgreSQL Acceptance (2026-10-03)

The production image `incidentlab-inventory-jpa:local` was built and deployed by
patching only inventory-service's image/pull policy. Requests ran through bounded
`kubectl exec` subprocesses in the existing investigator runtime; no public
Service, external port-forward, database reset or other service rollout was used.

| Check | Observed |
| --- | --- |
| Disabled, before activation | All four endpoints 404; no JPA fixture tables |
| Enabled, without prepare | Both provoke endpoints 409 |
| Lazy workflow | Prepare 200, provoke 500 (`LazyInitializationException`), cleanup 200 |
| Optimistic workflow | Prepare 200, provoke 500 (`ObjectOptimisticLockingFailureException`), cleanup 200 |
| Competing transaction | Version 1 and reserved quantity 1 committed; stale transaction rolled back |
| Diagnostics | Each ERROR log carried the supplied trace ID before its exception |
| Repeated cleanup | 200; both fixture tables empty |
| Ownership | Inventory schema, stock and both JPA tables owned by `inventory_svc` |
| Restored state | Original environment restored exactly; all four endpoints 404; inventory 1/1 Ready |
| Data preservation | Stock fingerprint unchanged; fixture tables retained empty; other service pods unchanged |

The first post-restore check was interrupted by a Kubernetes exec failure after
the environment had already been restored. A separate final read-only check
confirmed all disabled statuses and preservation assertions successfully.
Five Docker JPA tests passed with zero failures/errors/skips. NetworkPolicy
enforcement remains a separate existing security limitation, not certified here.