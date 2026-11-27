"""Tools the model may call. Every limit is enforced here, not by the model."""
import re

import httpx

from . import config, store
from .sources import make_log_source

READONLY_SQL = re.compile(r"^\s*select\b", re.IGNORECASE)
IDENT = re.compile(r"^[a-z_][a-z0-9_]*$", re.IGNORECASE)
ALLOWED_SCHEMAS = {"orders", "inventory", "payments"}

logs = make_log_source()


def search_logs(service: str = None, text: str = None, limit: int = 30, time_window: int | None = None):
    return logs.search(service, text, max(1, min(int(limit), 100)), time_window=time_window)


def get_log_context(trace_id: str, limit: int = 100, time_window: int | None = None):
    return logs.get_context(trace_id, max(1, min(int(limit), 200)), time_window=time_window)


def describe_table(schema: str, table: str):
    if schema not in ALLOWED_SCHEMAS or not IDENT.match(table):
        return {"error": "schema or table not allowed"}
    with store.db(config.RO_DB_DSN) as conn:
        conn.execute("set statement_timeout = 3000")
        rows = conn.execute(
            "select column_name, data_type, is_nullable from information_schema.columns where table_schema=%s and table_name=%s order by ordinal_position",
            (schema, table),
        ).fetchall()
    return {"schema": schema, "table": table, "columns": [dict(r) for r in rows], "observed_at": store.now().isoformat()}


def find_rows_by_ids(schema: str, table: str, id_column: str, ids: list):
    if schema not in ALLOWED_SCHEMAS or not IDENT.match(table) or not IDENT.match(id_column):
        return {"error": "identifier not allowed"}
    values = [str(v) for v in ids][:20]
    if not values:
        return {"error": "ids required"}
    sql = f"select * from {schema}.{table} where {id_column}::text = any(%s)"
    with store.db(config.RO_DB_DSN) as conn:
        conn.execute("set statement_timeout = 3000")
        rows = conn.execute(sql, (values,)).fetchmany(50)
    return {"rows": [dict(r) for r in rows], "observed_at": store.now().isoformat(), "note": "current state, not incident-time state"}


def db_health():
    with store.db(config.RO_DB_DSN) as conn:
        conn.execute("set statement_timeout = 3000")
        activity = dict(conn.execute(
            "select count(*) as connections, count(*) filter (where wait_event is not null) as waiting, "
            "count(*) filter (where state='active' and now()-xact_start > interval '30 seconds') as long_tx "
            "from pg_stat_activity"
        ).fetchone())
        locks = conn.execute(
            "select relation::regclass::text as relation, mode, granted, count(*) from pg_locks "
            "where relation is not null group by 1,2,3 order by 4 desc limit 20"
        ).fetchall()
    return {"activity": activity, "locks": [dict(r) for r in locks], "observed_at": store.now().isoformat()}


def run_readonly_sql(sql: str):
    if not READONLY_SQL.match(sql) or ";" in sql.strip().rstrip(";"):
        return {"error": "only a single SELECT statement is allowed"}
    with store.db(config.RO_DB_DSN) as conn:
        conn.execute("set statement_timeout = 3000")
        conn.execute("set search_path to orders, inventory, payments")
        rows = conn.execute(sql).fetchmany(50)
    return {"rows": [dict(r) for r in rows], "observed_at": store.now().isoformat(), "note": "current state, not incident-time state"}


def index_status():
    from . import retrieval

    return retrieval.index_status()


def search_code(service: str, query: str, limit: int = 5):
    from . import retrieval

    return retrieval.search_code(service, query, limit)


def read_file(service: str, path: str, start_line: int = 1, end_line: int = 120):
    from . import retrieval

    return retrieval.read_file(service, path, start_line, end_line)


MAX_EDITS = 10
MAX_EDIT_CHARS = 5000


def propose_patch(service: str, path: str, edits: list, summary: str = ""):
    """Validate an AI-written fix against the indexed deployed code. Stores nothing and never contacts GitHub."""
    from . import fixes, retrieval

    svc = config.service_by_name(service)
    if not svc:
        return {"error": "unknown service"}
    root = svc.get("repo_path", "").strip("/")
    path = path[len(root) + 1:] if isinstance(path, str) and path.startswith(root + "/") else path
    if not isinstance(path, str) or not fixes.valid_path(path):
        return {"error": "path must be a safe file path relative to the service, as used by read_file"}
    if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_EDITS:
        return {"error": f"edits must be a list of 1 to {MAX_EDITS} items"}
    source = retrieval.source_text(service, path)
    if source is None:
        return {"error": "file not found in the deployed code index; only existing files can be patched"}
    text = source.replace("\r\n", "\n")
    normalized = []
    for number, edit in enumerate(edits, 1):
        old = edit.get("old_text") if isinstance(edit, dict) else None
        new = edit.get("new_text") if isinstance(edit, dict) else None
        if not isinstance(old, str) or not isinstance(new, str) or not old.strip():
            return {"error": f"edit {number}: old_text and new_text must be strings and old_text must not be empty"}
        if len(old) > MAX_EDIT_CHARS or len(new) > MAX_EDIT_CHARS:
            return {"error": f"edit {number}: each text is limited to {MAX_EDIT_CHARS} characters"}
        old, new = old.replace("\r\n", "\n"), new.replace("\r\n", "\n")
        if fixes.SECRET.search(new):
            return {"error": f"edit {number}: new_text looks like it contains a credential"}
        count = text.count(old)
        if count != 1:
            return {"error": f"edit {number}: old_text must appear exactly once in {path} (found {count}); copy it exactly from read_file"}
        text = text.replace(old, new)
        normalized.append({"old_text": old, "new_text": new})
    if text == source.replace("\r\n", "\n"):
        return {"error": "edits do not change the file"}
    return {"accepted": True, "service": service, "path": path, "repo_path": f"{root}/{path}",
            "commit": svc.get("prod_commit"), "edits": normalized, "summary": str(summary)[:500],
            "note": "Proposal stored. The investigator opens a draft pull request after the investigation; do not claim it is merged."}


TOOLS = {
    "search_logs": search_logs,
    "get_log_context": get_log_context,
    "describe_table": describe_table,
    "find_rows_by_ids": find_rows_by_ids,
    "db_health": db_health,
    "run_readonly_sql": run_readonly_sql,
    "search_code": search_code,
    "read_file": read_file,
    "propose_patch": propose_patch,
}

TOOL_SCHEMAS = [
    {"name": "search_logs", "description": "Search recent error logs by service and text.",
        "parameters": {"type": "object", "properties": {"service": {"type": "string"}, "text": {"type": "string", "maxLength": 1000}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}, "time_window": {"type": "integer", "minimum": 1, "maximum": 86400}}, "additionalProperties": False}},
    {"name": "get_log_context", "description": "All log lines across services sharing a trace id.",
        "parameters": {"type": "object", "properties": {"trace_id": {"type": "string", "minLength": 1, "maxLength": 128}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}, "time_window": {"type": "integer", "minimum": 1, "maximum": 86400}}, "required": ["trace_id"], "additionalProperties": False}},
    {"name": "describe_table", "description": "Columns and types for a business table.",
     "parameters": {"type": "object", "properties": {"schema": {"type": "string"}, "table": {"type": "string"}}, "required": ["schema", "table"]}},
    {"name": "find_rows_by_ids", "description": "Fetch current rows by id values found in logs.",
     "parameters": {"type": "object", "properties": {
         "schema": {"type": "string"}, "table": {"type": "string"}, "id_column": {"type": "string"},
         "ids": {"type": "array", "items": {"type": "string"}}}, "required": ["schema", "table", "id_column", "ids"]}},
    {"name": "db_health", "description": "PostgreSQL connection, wait, and lock snapshot (observed now).",
     "parameters": {"type": "object", "properties": {}}},
    {"name": "run_readonly_sql", "description": "Run one SELECT against business schemas (read-only, 50 rows max).",
     "parameters": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}},
    {"name": "search_code", "description": "Find code chunks of a service at its deployed commit. Cite path and line numbers.",
     "parameters": {"type": "object", "properties": {"service": {"type": "string"}, "query": {"type": "string"}}, "required": ["service", "query"]}},
    {"name": "read_file", "description": "Read a file range of a service at its deployed commit from the code index (GitHub if missing).",
     "parameters": {"type": "object", "properties": {"service": {"type": "string"}, "path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, "required": ["service", "path"]}},
    {"name": "propose_patch", "description": "Propose a minimal fix for one existing file of a service. Each edit replaces old_text (copied exactly from read_file, appearing exactly once) with new_text. Call once per file. The investigator opens a draft pull request after review checks; you cannot merge or deploy.",
     "parameters": {"type": "object", "properties": {
         "service": {"type": "string"}, "path": {"type": "string", "description": "File path as used by read_file"},
         "summary": {"type": "string", "maxLength": 500},
         "edits": {"type": "array", "minItems": 1, "maxItems": MAX_EDITS, "items": {"type": "object", "properties": {
             "old_text": {"type": "string", "maxLength": MAX_EDIT_CHARS}, "new_text": {"type": "string", "maxLength": MAX_EDIT_CHARS}},
             "required": ["old_text", "new_text"], "additionalProperties": False}}},
         "required": ["service", "path", "edits"], "additionalProperties": False}},
]

TOOLS_BY_CATEGORY = {
    "infra": ["search_logs", "db_health"],
    "dependency": ["search_logs", "get_log_context"],
    "data": ["search_logs", "get_log_context", "describe_table", "find_rows_by_ids", "db_health", "run_readonly_sql"],
    "code": ["search_logs", "get_log_context", "describe_table", "find_rows_by_ids", "run_readonly_sql", "search_code", "read_file", "propose_patch"],
    "unknown": ["search_logs", "get_log_context", "search_code"],
}
