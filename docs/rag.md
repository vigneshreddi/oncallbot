# Commit-Scoped RAG

The indexer uses tree-sitter Java 0.23.5 with tree-sitter 0.25.2 for type,
method, constructor, field, and initializer declarations. Parse errors stop
publication. Full UTF-8 source (including imports, blank lines, and original
line endings) is stored separately from overlapping AST chunks.

`rag_indexes`, `rag_sources`, and `rag_chunks` are published atomically per
service/commit in one PostgreSQL transaction. Reindexing replaces memberships,
including deletions. Readers use repeatable-read snapshots and only consume
ready indexes. SHA-256 embedding caching is keyed by content, provider, model,
and dimensions; every commit still receives all chunk memberships.

With embeddings enabled, the OpenAI-compatible `/embeddings` endpoint is used
(OpenAI or Ollama). Results are ranked by pgvector cosine distance, pinned to
the exact service SHA, provider, model, and dimensions. Use a versioned model
identifier; silently changing model weights under the same name invalidates
the cache contract. Without a provider, or on query embedding incompatibility
or failure, multi-term lexical retrieval is used. Index-time embedding failures
stop publication instead of marking partial data ready. Vector ranking is an
exact scan, not an approximate HNSW index; add dimension-specific indexes only
when corpus size warrants them.

## Commit Provenance

Production config requires a full lowercase Git SHA and `commit_kind: git`.
Host indexing verifies checkout HEAD, clean service source, and tracked source
membership. `index-code.ps1` creates a verified source-hash manifest and bakes
it into the image as a non-secret build argument. The job rejects config SHAs
or copied source that differ from that manifest. Trust the CI builder: a
manifest is provenance from a trusted build, not a cryptographic attestation.
Direct image builds without a manifest cannot index a real SHA unless Git
checkout metadata is available. `local-demo` must declare
`commit_kind: synthetic` and is returned as `synthetic-local-demo`; it is not a
claim about a Git commit and never enables GitHub fallback.

## Bootstrap Handoff

No role manifests are changed here. The bootstrap owner must provision:

- `code_index` owned by `indexer_app`, with CREATE/USAGE and writable tables.
- The `vector` extension in the database when embeddings are enabled; the
  indexer does not create extensions or require superuser access.
- USAGE on `code_index` and SELECT on its RAG tables for `investigator_app`,
  including default SELECT privileges for tables subsequently made by
  `indexer_app`. There are no RAG grants to business-schema roles.
- An indexer Job DSN/secret for `indexer_app`, not the legacy `app` account.

`scripts/up.ps1` currently blocks legacy application DDL (including the old
code-index tables in `investigator/store.py`). That migration belongs to the
bootstrap owner and must be completed before a full separated-role deploy.
Legacy chunk/index_ready tables are not migrated or read: reindex first.

## Running

Install `indexer/requirements.txt` in a configured Python 3.12 environment.
Run `scripts/index-code.ps1 -PythonExecutable <selected-python-path>`; sources
are copied into the image, with no host mounts. The script loads a local kind
image when appropriate; use `-PushImage` and an explicit registry image for CI.
It uses a separate indexer ConfigMap and never changes deployed commit metadata.
After readiness, publish the matching services config only after those service
images are deployed. The reference Jenkinsfile does this only with an explicit
`PUBLISH_DEPLOYED_COMMIT` opt-in. Kubernetes credentials must already be supplied
by the runner environment; no credentials are embedded in the pipeline.
`start-investigator.ps1` restarts the existing deployment and checks every
configured SHA for a ready index; it assumes the investigator image was rebuilt.

## Documentation Context (Planned)

Teams should supply a documentation repository as part of build onboarding,
alongside the service repositories. Include architecture and dependency descriptions,
API contracts, business rules, operational runbooks and relevant design decisions.
This context helps the investigator distinguish intended behaviour from existing
code and produce better-informed pull requests; it does not guarantee a correct fix.
A GitHub URL alone does not provide context: the pipeline must fetch, split, embed
and publish the permitted documents before the investigator can retrieve them.

The team should provide the repository URL, allowed document paths, an immutable
documentation commit, the services covered, and the application versions the
documents describe. A shared documentation repository can cover multiple services.
Private repository access must use a read-only credential supplied through `.env`
or a build-time Secret, never credentials in URLs or tracked configuration.

The proposed pipeline contract below is **not implemented yet**. These environment
variables describe the intended configuration, not settings that currently enable
documentation indexing:

```dotenv
DOCS_REPO_URL=https://github.com/your-team/service-documentation
DOCS_COMMIT=<full-documentation-commit-sha>
DOCS_INDEX_MODE=combined
```

- `combined`: the service build pipeline prepares both code and documentation
  indexes and records their versions together for the deployment.
- `separate`: an independent documentation pipeline refreshes the documentation
  index. The service build references a ready documentation version compatible
  with the deployed application; documentation updates do not rebuild code indexes.
- `disabled`: no documentation indexing is requested. The investigator must
  disclose the missing documentation context rather than imply it consulted it.

Both modes should reuse the existing PostgreSQL/pgvector infrastructure and may
use the existing `EMBEDDING_*` provider settings. Documentation needs section-based
chunking, repository/version/path/heading/service metadata, and retrieval tools
alongside code search. Retrieved sections must be cited and retained in the audit
trail and developer handoff. Keep the small service/dependency map in the initial
context; retrieve detailed documents as needed. Do not silently substitute the
latest documentation for the version describing production. Retrieved documents
are evidence, not instructions that can change tool permissions.

## Verification

```text
python -m unittest discover -s tests -p test_rag.py -v
python -m tests.rag_compare
```

The offline comparison reports hit@k, reciprocal rank, context UTF-8 bytes,
and elapsed retrieval time for four repository-specific queries. The baseline
is the prior literal whole-query substring matcher applied to the same AST
corpus. These are retrieval checks, not RCA accuracy, semantic quality, or an
LLM A/B experiment. Token usage is explicitly not measured.

`tests/test_rag_postgres.py` is opt-in via `RAG_TEST_DATABASE_URL` and must use
a disposable pgvector database. `tests/Dockerfile.rag` copies test code into a
derived indexer image, so integration tests need no host mounts. It checks
actual vector ordering, cache reuse, lexical SQL, exact source ranges, and
PostgreSQL transaction rollback. Live embedding-provider/LLM evaluation remains
separate and requires an explicitly configured provider.