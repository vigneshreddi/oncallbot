param([string]$Namespace = 'incident-lab')
$ErrorActionPreference = 'Stop'
$raw = kubectl get secret postgres-secret -n $Namespace -o json 2>$null
if ($LASTEXITCODE -ne 0) { throw 'Cannot read postgres-secret; bootstrap stopped.' }
$secret = ($raw -join "`n") | ConvertFrom-Json
$roles = [ordered]@{
  order_svc = 'ORDER_PASSWORD'; inventory_svc = 'INVENTORY_PASSWORD'
  payment_svc = 'PAYMENT_PASSWORD'; investigator_app = 'INVESTIGATOR_PASSWORD'
  indexer_app = 'INDEXER_PASSWORD'; chaos_app = 'CHAOS_PASSWORD'; investigator_ro = 'RO_PASSWORD'
  db_viewer = 'VIEWER_PASSWORD'
}
$roleSql = foreach ($role in $roles.Keys) {
  $encoded = $secret.data.($roles[$role])
  if (-not $encoded -or $encoded -notmatch '^[A-Za-z0-9+/]+=*$') { throw "Missing or invalid Secret key: $($roles[$role])" }
  @"
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$role') THEN
    CREATE ROLE $role LOGIN;
  END IF;
  EXECUTE format('ALTER ROLE $role LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT PASSWORD %L', convert_from(decode('$encoded', 'base64'), 'UTF8'));
"@
}
$sql = @'
BEGIN;
SET LOCAL log_statement = 'none';
SET LOCAL log_min_error_statement = 'panic';
SET LOCAL log_min_duration_statement = -1;
SET LOCAL log_duration = off;
CREATE EXTENSION IF NOT EXISTS vector;
DO $roles$
BEGIN
__ROLES__
END
$roles$;
DO $memberships$
DECLARE membership record;
BEGIN
  FOR membership IN
    SELECT parent.rolname AS parent, child.rolname AS child
    FROM pg_auth_members m JOIN pg_roles parent ON parent.oid=m.roleid JOIN pg_roles child ON child.oid=m.member
    WHERE child.rolname IN ('order_svc','inventory_svc','payment_svc','investigator_app','indexer_app','chaos_app','investigator_ro','db_viewer')
  LOOP
    EXECUTE format('REVOKE %I FROM %I', membership.parent, membership.child);
  END LOOP;
END
$memberships$;
REVOKE ALL ON DATABASE incidents FROM PUBLIC;
REVOKE ALL ON DATABASE incidents FROM order_svc, inventory_svc, payment_svc, investigator_app, indexer_app, chaos_app, investigator_ro, db_viewer;
GRANT CONNECT ON DATABASE incidents TO order_svc, inventory_svc, payment_svc, investigator_app, indexer_app, chaos_app, investigator_ro, db_viewer;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM order_svc, inventory_svc, payment_svc, investigator_app, indexer_app, chaos_app, investigator_ro, db_viewer;
DO $schemas$
DECLARE mapping record; object record; client text; grantee text;
BEGIN
  FOR mapping IN SELECT * FROM (VALUES
    ('orders','order_svc'), ('inventory','inventory_svc'), ('payments','payment_svc'),
    ('investigator','investigator_app'), ('code_index','indexer_app')
  ) AS owners(schema_name, owner_name)
  LOOP
    EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I AUTHORIZATION %I', mapping.schema_name, mapping.owner_name);
    IF EXISTS (SELECT FROM pg_namespace n JOIN pg_roles r ON r.oid=n.nspowner
               WHERE n.nspname=mapping.schema_name AND r.rolname NOT IN ('app', mapping.owner_name)) THEN
      RAISE EXCEPTION 'Unexpected schema owner; manual migration required';
    END IF;
    EXECUTE format('ALTER SCHEMA %I OWNER TO %I', mapping.schema_name, mapping.owner_name);
    FOR object IN SELECT c.relname, c.relkind, r.rolname FROM pg_class c
      JOIN pg_namespace n ON n.oid=c.relnamespace JOIN pg_roles r ON r.oid=c.relowner
      WHERE n.nspname=mapping.schema_name AND c.relkind IN ('r','p','S','v','m','f')
      ORDER BY CASE WHEN c.relkind='S' THEN 1 ELSE 0 END
    LOOP
      IF object.rolname NOT IN ('app', mapping.owner_name) THEN
        RAISE EXCEPTION 'Unexpected object owner; manual migration required';
      END IF;
      EXECUTE format('ALTER %s %I.%I OWNER TO %I',
        CASE object.relkind WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
          WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END,
        mapping.schema_name, object.relname, mapping.owner_name);
    END LOOP;
    FOR object IN SELECT p.oid::regprocedure AS signature, r.rolname FROM pg_proc p
      JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_roles r ON r.oid=p.proowner
      WHERE n.nspname=mapping.schema_name AND p.prokind='f'
    LOOP
      IF object.rolname NOT IN ('app', mapping.owner_name) THEN
        RAISE EXCEPTION 'Unexpected function owner; manual migration required';
      END IF;
      EXECUTE format('ALTER FUNCTION %s OWNER TO %I', object.signature, mapping.owner_name);
    END LOOP;
    FOR client IN SELECT unnest(ARRAY['PUBLIC','order_svc','inventory_svc','payment_svc','investigator_app','indexer_app','chaos_app','investigator_ro','db_viewer'])
    LOOP
      IF client <> mapping.owner_name THEN
        grantee := CASE WHEN client='PUBLIC' THEN 'PUBLIC' ELSE quote_ident(client) END;
        EXECUTE format('REVOKE ALL ON SCHEMA %I FROM %s', mapping.schema_name, grantee);
        EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM %s', mapping.schema_name, grantee);
        FOR object IN SELECT c.relname, string_agg(quote_ident(a.attname), ', ' ORDER BY a.attnum) AS columns
          FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
          JOIN pg_attribute a ON a.attrelid=c.oid
          WHERE n.nspname=mapping.schema_name AND c.relkind IN ('r','p','v','m','f')
            AND a.attnum > 0 AND NOT a.attisdropped
          GROUP BY c.relname
        LOOP
          EXECUTE format('REVOKE ALL (%s) ON TABLE %I.%I FROM %s', object.columns, mapping.schema_name, object.relname, grantee);
        END LOOP;
        EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM %s', mapping.schema_name, grantee);
        EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA %I FROM %s', mapping.schema_name, grantee);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE ALL ON TABLES FROM %s', mapping.owner_name, grantee);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE ALL ON SEQUENCES FROM %s', mapping.owner_name, grantee);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE EXECUTE ON FUNCTIONS FROM %s', mapping.owner_name, grantee);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I REVOKE ALL ON TABLES FROM %s', mapping.owner_name, mapping.schema_name, grantee);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I REVOKE ALL ON SEQUENCES FROM %s', mapping.owner_name, mapping.schema_name, grantee);
        EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I REVOKE EXECUTE ON FUNCTIONS FROM %s', mapping.owner_name, mapping.schema_name, grantee);
      END IF;
    END LOOP;
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC', mapping.owner_name);
    EXECUTE format('ALTER ROLE %I IN DATABASE incidents SET search_path = %I, pg_catalog', mapping.owner_name, mapping.schema_name);
    IF mapping.schema_name IN ('orders','inventory','payments') THEN
      EXECUTE format('GRANT USAGE ON SCHEMA %I TO investigator_ro', mapping.schema_name);
      EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO investigator_ro', mapping.schema_name);
      EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT SELECT ON TABLES TO investigator_ro', mapping.owner_name, mapping.schema_name);
    END IF;
  END LOOP;
END
$schemas$;
GRANT USAGE ON SCHEMA public TO indexer_app, investigator_app;
ALTER ROLE indexer_app IN DATABASE incidents SET search_path = code_index, pg_catalog, public;
ALTER ROLE investigator_app IN DATABASE incidents SET search_path = investigator, pg_catalog, public;
GRANT USAGE ON SCHEMA code_index TO investigator_app;
GRANT SELECT ON ALL TABLES IN SCHEMA code_index TO investigator_app;
ALTER DEFAULT PRIVILEGES FOR ROLE indexer_app IN SCHEMA code_index GRANT SELECT ON TABLES TO investigator_app;
SET LOCAL ROLE indexer_app;
CREATE TABLE IF NOT EXISTS code_index.rag_indexes (
  service text NOT NULL, commit_sha text NOT NULL, source_commit text, provenance text NOT NULL,
  provider text NOT NULL, model text NOT NULL, dimensions integer NOT NULL,
  chunker_version text NOT NULL, chunk_count integer NOT NULL,
  ready_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(service, commit_sha)
);
CREATE TABLE IF NOT EXISTS code_index.rag_sources (
  service text NOT NULL, commit_sha text NOT NULL, path text NOT NULL, content text NOT NULL,
  content_hash text NOT NULL, PRIMARY KEY(service, commit_sha, path)
);
CREATE TABLE IF NOT EXISTS code_index.rag_chunks (
  service text NOT NULL, commit_sha text NOT NULL, ordinal integer NOT NULL, path text NOT NULL,
  start_line integer NOT NULL, end_line integer NOT NULL, symbol text NOT NULL, kind text NOT NULL,
  content text NOT NULL, content_hash text NOT NULL, PRIMARY KEY(service, commit_sha, ordinal)
);
CREATE TABLE IF NOT EXISTS code_index.rag_embedding_cache (
  content_hash text NOT NULL, provider text NOT NULL, model text NOT NULL, dimensions integer NOT NULL,
  embedding_json text NOT NULL, PRIMARY KEY(content_hash, provider, model, dimensions)
);
CREATE TABLE IF NOT EXISTS code_index.rag_vectors (
  content_hash text NOT NULL, provider text NOT NULL, model text NOT NULL, dimensions integer NOT NULL,
  embedding public.vector NOT NULL, PRIMARY KEY(content_hash, provider, model, dimensions)
);
RESET ROLE;
SET LOCAL ROLE investigator_app;
CREATE TABLE IF NOT EXISTS investigator.chaos_events (
  id bigserial PRIMARY KEY, fault_id text NOT NULL, category text, service text,
  started_at timestamptz NOT NULL, reverted_at timestamptz, params jsonb
);
RESET ROLE;
GRANT USAGE ON SCHEMA investigator TO chaos_app;
GRANT INSERT (fault_id, category, service, started_at, reverted_at, params) ON investigator.chaos_events TO chaos_app;
GRANT USAGE ON SEQUENCE investigator.chaos_events_id_seq TO chaos_app;
ALTER ROLE investigator_ro IN DATABASE incidents SET default_transaction_read_only = on;
ALTER ROLE investigator_ro IN DATABASE incidents SET statement_timeout = '3s';
ALTER ROLE investigator_ro IN DATABASE incidents SET search_path = orders, inventory, payments, pg_catalog;
GRANT USAGE ON SCHEMA public TO db_viewer;
DO $viewer$
DECLARE mapping record;
BEGIN
  FOR mapping IN SELECT * FROM (VALUES
    ('orders','order_svc'), ('inventory','inventory_svc'), ('payments','payment_svc'),
    ('investigator','investigator_app'), ('code_index','indexer_app')
  ) AS owners(schema_name, owner_name)
  LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO db_viewer', mapping.schema_name);
    EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO db_viewer', mapping.schema_name);
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA %I GRANT SELECT ON TABLES TO db_viewer', mapping.owner_name, mapping.schema_name);
  END LOOP;
END
$viewer$;
ALTER ROLE db_viewer IN DATABASE incidents SET default_transaction_read_only = on;
ALTER ROLE db_viewer IN DATABASE incidents SET statement_timeout = '30s';
ALTER ROLE db_viewer IN DATABASE incidents SET search_path = orders, inventory, payments, code_index, investigator, public, pg_catalog;
COMMIT;
'@
$sql = $sql.Replace('__ROLES__', ($roleSql -join "`n"))
$command = 'PGPASSWORD=$POSTGRES_PASSWORD; export PGPASSWORD; PGOPTIONS=''-c log_statement=none -c log_min_error_statement=panic -c log_min_duration_statement=-1 -c log_duration=off''; export PGOPTIONS; exec psql -X -q -h 127.0.0.1 -U app -d incidents -v ON_ERROR_STOP=1'
try {
  $ErrorActionPreference = 'Continue'
  $sql | kubectl exec -i -n $Namespace postgres-0 -- sh -c $command 2>&1 | Out-Null
  if ($LASTEXITCODE -ne 0) { throw 'Database role bootstrap failed; transaction rolled back. No services should be deployed.' }
} finally {
  $ErrorActionPreference = 'Stop'
  $sql = $null; $roleSql = $null; $secret = $null; $raw = $null; $encoded = $null
}
Write-Output 'Database role ownership and least-privilege grants bootstrapped.'
