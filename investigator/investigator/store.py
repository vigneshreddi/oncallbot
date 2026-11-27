import json
import hashlib
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from . import config


def now():
    return datetime.now(timezone.utc)


def db(dsn: str | None = None):
    if dsn == "":
        raise RuntimeError("RO_PASSWORD is required for read-only database tools")
    return psycopg.connect(config.DB_DSN if dsn is None else dsn, autocommit=True, row_factory=dict_row)


SCHEMA = """
create table if not exists investigator.log_lines (
  id bigserial primary key,
  fingerprint text not null,
  service text not null,
  pod text not null,
  observed_at timestamptz not null,
  level text,
  trace_id text,
  message text not null,
  raw jsonb not null
);
create index if not exists log_lines_fp on investigator.log_lines(fingerprint);
create index if not exists log_lines_trace on investigator.log_lines(trace_id);
create table if not exists investigator.incidents (
  id bigserial primary key,
  fingerprint text unique not null,
  title text not null,
  service text not null,
  category text not null default 'unknown',
  first_seen timestamptz not null,
  last_seen timestamptz not null,
  occurrences integer not null default 1,
  status text not null default 'open',
  snapshot jsonb,
  report text
);
create table if not exists investigator.evidence (
  id bigserial primary key,
  incident_id bigint not null references investigator.incidents(id),
  step integer not null,
  tool text not null,
  arguments jsonb,
  result_summary text,
  observed_at timestamptz not null default now()
);
create table if not exists investigator.chaos_events (
  id bigserial primary key,
  fault_id text not null,
  category text,
  service text,
  started_at timestamptz not null,
  reverted_at timestamptz,
  params jsonb
);
create table if not exists investigator.llm_exchanges (
  id bigserial primary key,
  incident_id bigint not null references investigator.incidents(id),
  round integer not null,
  kind text not null,
  payload jsonb not null,
  input_tokens integer not null default 0,
  output_tokens integer not null default 0,
  created_at timestamptz not null default now()
);
create index if not exists llm_exchanges_incident on investigator.llm_exchanges(incident_id, id);
create table if not exists investigator.fix_proposals (
  id bigserial primary key,
  incident_id bigint not null references investigator.incidents(id),
  service text not null,
  path text not null,
  summary text,
  edits jsonb not null,
  status text not null default 'proposed',
  pr_url text,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists fix_proposals_incident on investigator.fix_proposals(incident_id, id);
create table if not exists investigator.incident_occurrences (
  id bigserial primary key,
  incident_id bigint not null references investigator.incidents(id),
  source text not null,
  external_id text,
  service text,
  pod text,
  event_at timestamptz,
  received_at timestamptz not null default now(),
  trace_id text,
  span_id text,
  correlation_id text,
  request_id text,
  message text,
  details jsonb
);
create index if not exists incident_occurrences_incident on investigator.incident_occurrences(incident_id, id);
create index if not exists incident_occurrences_trace on investigator.incident_occurrences(trace_id);
create unique index if not exists incident_occurrences_external
  on investigator.incident_occurrences(source, external_id) where external_id is not null;
"""


def ensure_schema():
    with db() as conn:
        conn.execute(SCHEMA)
        conn.execute("""
            alter table investigator.log_lines add column if not exists event_at timestamptz;
            alter table investigator.log_lines add column if not exists record_key text;
            create unique index if not exists log_lines_record_key on investigator.log_lines(record_key);
            create index if not exists log_lines_event_at on investigator.log_lines(event_at);
            alter table investigator.incidents add column if not exists usage jsonb;
            alter table investigator.incidents add column if not exists failure text;
        """)


def event_time(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def insert_log(fp, service, pod, level, trace_id, message, record, seen_cache) -> bool:
    observed = now()
    while seen_cache and (observed - next(iter(seen_cache.values()))).total_seconds() > config.LOG_RETENTION_SECONDS:
        seen_cache.popitem(last=False)
    key = hashlib.sha256(json.dumps([service, pod, record], sort_keys=True, default=str).encode()).hexdigest()
    if key in seen_cache:
        return False
    with db() as conn:
        row = conn.execute(
            """insert into investigator.log_lines
               (fingerprint, service, pod, observed_at, event_at, record_key, level, trace_id, message, raw)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               on conflict (record_key) do nothing returning id""",
            (fp, service, pod, observed, event_time(record.get("@timestamp") or record.get("timestamp") or record.get("_k8s_timestamp")),
             key, level, trace_id, message, json.dumps(record)),
        ).fetchone()
    while len(seen_cache) >= config.LOG_CACHE_SIZE:
        seen_cache.popitem(last=False)
    seen_cache[key] = observed
    return row is not None


def prune_logs():
    with db() as conn:
        conn.execute("delete from investigator.log_lines where coalesce(event_at, observed_at) < now() - (%s * interval '1 second')",
                     (config.LOG_RETENTION_SECONDS,))


def upsert_incident(fp, service, title) -> tuple[int, bool]:
    with db() as conn:
        row = conn.execute(
            """insert into investigator.incidents (fingerprint, title, service, first_seen, last_seen)
               values (%s,%s,%s,now(),now())
               on conflict (fingerprint) do update set last_seen=now(), occurrences=investigator.incidents.occurrences+1
               returning id, occurrences""",
            (fp, title, service),
        ).fetchone()
        return row["id"], row["occurrences"] == 1


def add_evidence(incident_id: int, step: int, tool: str, arguments: dict, result_summary: str):
    with db() as conn:
        conn.execute(
            "insert into investigator.evidence (incident_id, step, tool, arguments, result_summary) values (%s,%s,%s,%s,%s)",
            (incident_id, step, tool, json.dumps(arguments, default=str), result_summary),
        )


def occurrence_incident(source: str, external_id: str | None) -> int | None:
    """Incident already holding this vendor alert/event id (a re-delivered alert), if any."""
    if not external_id:
        return None
    with db() as conn:
        row = conn.execute("select incident_id from investigator.incident_occurrences where source=%s and external_id=%s",
                           (source, str(external_id))).fetchone()
    return row["incident_id"] if row else None


def add_occurrence(incident_id: int, source: str, *, external_id=None, service=None, pod=None, event_at=None,
                   ids: dict | None = None, message=None, details: dict | None = None) -> bool:
    ids = ids or {}
    with db() as conn:
        row = conn.execute(
            """insert into investigator.incident_occurrences
               (incident_id, source, external_id, service, pod, event_at, trace_id, span_id, correlation_id, request_id, message, details)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               on conflict (source, external_id) where external_id is not null do nothing returning id""",
            (incident_id, source, str(external_id) if external_id else None, service, pod, event_time(event_at),
             ids.get("trace_id"), ids.get("span_id"), ids.get("correlation_id"), ids.get("request_id"),
             (message or "")[:2000] or None, json.dumps(details or {}, default=str)),
        ).fetchone()
    return row is not None


def incident_occurrences(incident_id: int, limit: int = 200) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "select id, source, external_id, service, pod, event_at, received_at, trace_id, span_id, correlation_id, "
            "request_id, message, details from investigator.incident_occurrences where incident_id=%s "
            "order by coalesce(event_at, received_at) desc, id desc limit %s", (incident_id, max(1, min(int(limit), 1000)))).fetchall()
    return [dict(row) for row in rows]


def add_llm_exchange(incident_id: int, round_no: int, kind: str, payload: dict, input_tokens: int = 0, output_tokens: int = 0):
    with db() as conn:
        conn.execute(
            "insert into investigator.llm_exchanges (incident_id, round, kind, payload, input_tokens, output_tokens) "
            "values (%s,%s,%s,%s,%s,%s)",
            (incident_id, round_no, kind, json.dumps(payload, default=str), input_tokens, output_tokens),
        )


def _parsed(text):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def incident_audit(incident_id: int) -> list[dict] | None:
    """Everything received and sent for one incident, in time order, as JSON Lines records."""
    with db() as conn:
        incident = conn.execute(
            "select id, fingerprint, title, service, category, status, first_seen, last_seen, occurrences, usage, failure "
            "from investigator.incidents where id=%s", (incident_id,)).fetchone()
        if not incident:
            return None
        evidence = conn.execute(
            "select step, tool, arguments, result_summary, observed_at from investigator.evidence "
            "where incident_id=%s order by step, id", (incident_id,)).fetchall()
        exchanges = conn.execute(
            "select round, kind, payload, input_tokens, output_tokens, created_at from investigator.llm_exchanges "
            "where incident_id=%s order by id", (incident_id,)).fetchall()
        proposals = conn.execute(
            "select service, path, summary, edits, status, pr_url, error, created_at, updated_at "
            "from investigator.fix_proposals where incident_id=%s order by id", (incident_id,)).fetchall()
        occurrences = conn.execute(
            "select source, external_id, service, pod, event_at, received_at, trace_id, span_id, correlation_id, "
            "request_id, message, details from investigator.incident_occurrences where incident_id=%s order by id",
            (incident_id,)).fetchall()
    events = []
    for row in evidence:
        result = _parsed(row["result_summary"])
        events.append((row["observed_at"], {"type": "tool_result", "at": row["observed_at"], "step": row["step"],
                                             "tool": row["tool"], "arguments": row["arguments"], "result": result}))
        if row["tool"] == "search_code" and isinstance(result, dict):
            for rank, chunk in enumerate(result.get("chunks") or [], 1):
                events.append((row["observed_at"], {
                    "type": "code_chunk_sent", "at": row["observed_at"], "step": row["step"], "rank": rank,
                    "error_fingerprint": incident["fingerprint"], "error_title": incident["title"],
                    "query": (row["arguments"] or {}).get("query"), "mode": result.get("mode"),
                    "commit": result.get("commit"), "path": chunk.get("path"), "symbol": chunk.get("symbol"),
                    "start_line": chunk.get("start_line"), "end_line": chunk.get("end_line"),
                    "score": chunk.get("score"), "characters": len(chunk.get("content") or "")}))
        if row["tool"] == "read_file" and isinstance(result, dict) and "error" not in result:
            events.append((row["observed_at"], {
                "type": "code_range_sent", "at": row["observed_at"], "step": row["step"],
                "error_fingerprint": incident["fingerprint"], "path": result.get("path"), "commit": result.get("commit"),
                "source": result.get("source"), "start_line": result.get("start_line"), "end_line": result.get("end_line")}))
    for row in exchanges:
        events.append((row["created_at"], {"type": "llm_" + row["kind"], "at": row["created_at"], "round": row["round"],
                                           "input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"],
                                           "payload": row["payload"]}))
    for row in proposals:
        events.append((row["updated_at"], {"type": "fix_proposal", "at": row["updated_at"], **dict(row)}))
    for row in occurrences:
        events.append((row["received_at"], {"type": "occurrence", "at": row["received_at"], **dict(row)}))
    events.sort(key=lambda item: item[0])
    return [{"type": "incident", **dict(incident)}] + [event for _, event in events]


def save_fix_proposals(incident_id: int, proposals: list[dict]):
    with db() as conn, conn.transaction():
        for proposal in proposals:
            conn.execute(
                "insert into investigator.fix_proposals (incident_id, service, path, summary, edits) values (%s,%s,%s,%s,%s)",
                (incident_id, proposal["service"], proposal["path"], proposal.get("summary"), json.dumps(proposal["edits"])),
            )


def fix_proposals(incident_id: int, status: str | None = None) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "select id, service, path, summary, edits, status, pr_url, error, created_at, updated_at "
            "from investigator.fix_proposals where incident_id=%s and (%s::text is null or status=%s) order by id",
            (incident_id, status, status)).fetchall()
    return [dict(row) for row in rows]


def mark_fix_proposals(ids: list[int], status: str, pr_url: str | None = None, error: str | None = None):
    with db() as conn:
        conn.execute("update investigator.fix_proposals set status=%s, pr_url=%s, error=%s, updated_at=now() where id = any(%s)",
                     (status, pr_url, error, ids))


def reset_investigation(incident_id: int):
    """Remove a previous run's evidence, AI exchanges and unopened proposals before investigating again."""
    with db() as conn, conn.transaction():
        conn.execute("delete from investigator.evidence where incident_id=%s", (incident_id,))
        conn.execute("delete from investigator.llm_exchanges where incident_id=%s", (incident_id,))
        conn.execute("delete from investigator.fix_proposals where incident_id=%s and status <> 'pr_opened'", (incident_id,))
        conn.execute("update investigator.incidents set status='queued', report=null, failure=null where id=%s", (incident_id,))


def _clip(text, limit):
    text = text if isinstance(text, str) else json.dumps(text, default=str, indent=1)
    return text if len(text) <= limit else text[:limit] + f"\n… ({len(text) - limit} more characters in the full audit export)"


def incident_handoff(incident_id: int) -> str | None:
    """Markdown brief of a finished investigation for a developer to continue in an AI assistant."""
    records = incident_audit(incident_id)
    if records is None:
        return None
    incident = records[0]
    svc = config.service_by_name(incident["service"]) or {}
    tools_run = [r for r in records if r["type"] == "tool_result"]
    first = next((r["result"] for r in tools_run if r["tool"] == "incident_logs"), {}) or {}
    first = first if isinstance(first, dict) else {}
    raw = first.get("raw") if isinstance(first.get("raw"), dict) else {}
    final = next((r["payload"] for r in reversed(records) if r["type"] == "llm_final"), {}) or {}
    findings = final.get("findings")
    if not findings:
        with db() as conn:
            row = conn.execute("select report from investigator.incidents where id=%s", (incident_id,)).fetchone()
        report = (row or {}).get("report") or ""
        start, end = report.find("## Analysis"), report.find("## Evidence trail")
        findings = report[start + len("## Analysis"):end].strip() if start >= 0 else "_No analysis stored._"
    lines = [
        f"# Incident {incident_id} handoff \u2014 {incident['service']} \u2014 {incident['category']}",
        "",
        "> Paste this into an AI assistant (for example GitHub Copilot Chat) opened on this repository at the",
        "> commit below, then ask it to continue. It may contain private log and database values; share it only",
        "> with tools approved for that data.",
        "",
        f"- Status: **{incident['status']}**, occurrences: {incident['occurrences']}, first seen {incident['first_seen']}, last seen {incident['last_seen']}",
        f"- Repository: {svc.get('repo_url', 'n/a')} \u2014 service path `{svc.get('repo_path', 'n/a')}`",
        f"- Deployed commit analysed: `{svc.get('prod_commit', 'n/a')}` (check it out to see the same code)",
        f"- Error fingerprint: `{incident['fingerprint']}`",
    ]
    if incident.get("failure"):
        lines.append(f"- Investigation problem: {incident['failure']}")
    lines += ["", "## 1. First error", "```", _clip(first.get("message") or incident["title"], 1500), "```"]
    stack = raw.get("error.stack_trace") or raw.get("stack_trace")
    if stack:
        frames = str(stack).splitlines()
        framework = ("at java.", "at jdk.", "at org.springframework.", "at org.apache.", "at jakarta.", "at sun.")
        kept = [line for index, line in enumerate(frames) if index == 0 or not line.strip().startswith(framework)]
        hidden = len(frames) - len(kept)
        lines += ["Stack trace (application frames; " + str(hidden) + " framework frames hidden):", "```",
                  "\n".join(kept[:40]), "```"]
    occurrences = [r for r in records if r["type"] == "occurrence"]
    sources_seen = sorted({r["source"] for r in occurrences})
    lines += ["", f"### Occurrences: {len(occurrences)} recorded" + (f" from {', '.join(sources_seen)}" if sources_seen else "")]
    if occurrences:
        lines += ["| when | source | alert/event id | trace id | span id | correlation id | request id | pod |",
                  "|---|---|---|---|---|---|---|---|"]
        for item in occurrences[-20:]:
            cells = [item.get("event_at") or item.get("at"), item["source"], item.get("external_id"), item.get("trace_id"),
                     item.get("span_id"), item.get("correlation_id"), item.get("request_id"), item.get("pod")]
            lines.append("| " + " | ".join(str(cell) if cell else "\u2013" for cell in cells) + " |")
        if not any(item.get("trace_id") for item in occurrences):
            lines.append("_No trace id was available in these occurrences, so the request cannot be followed across services._")
    lines += ["", "## 2. AI analysis", findings or "_none_"]
    proposals = [r for r in records if r["type"] == "fix_proposal"]
    lines += ["", "## 3. AI-proposed fix and pull requests"]
    if not proposals:
        lines.append("_No fix was proposed._")
    for proposal in proposals:
        state = proposal.get("pr_url") or proposal.get("error") or proposal["status"]
        lines += [f"### `{proposal['service']}` `{proposal['path']}` \u2014 {proposal['status']}: {state}",
                  proposal.get("summary") or ""]
        for number, edit in enumerate(proposal.get("edits") or [], 1):
            lines += [f"Edit {number}:", "```diff",
                      *("- " + line for line in edit["old_text"].splitlines()),
                      *("+ " + line for line in edit["new_text"].splitlines()), "```"]
    code = [r for r in records if r["type"] in {"code_chunk_sent", "code_range_sent"}]
    lines += ["", "## 4. Code the AI looked at (open these files at the commit above)"]
    if not code:
        lines.append("_No code was retrieved._")
    for item in code:
        location = f"`{item.get('path')}` lines {item.get('start_line')}\u2013{item.get('end_line')}"
        if item["type"] == "code_chunk_sent":
            score = item.get("score")
            score = f", score {score:.3f}" if isinstance(score, (int, float)) else ""
            lines.append(f"- step {item['step']}: {location} `{item.get('symbol')}` ({item.get('mode')}{score}) \u2014 search: \u201c{item.get('query')}\u201d")
        else:
            lines.append(f"- step {item['step']}: {location} (read in full)")
    database = [r for r in tools_run if r["tool"] in {"run_readonly_sql", "find_rows_by_ids", "describe_table", "db_health"}]
    lines += ["", "## 5. Database evidence (observed at investigation time, not at incident time)"]
    if not database:
        lines.append("_The AI did not query the database. Consider checking the rows involved yourself._")
    for item in database:
        lines += [f"- step {item['step']} `{item['tool']}` {json.dumps(item['arguments'], default=str)}", "```json",
                  _clip(item["result"], 2000), "```"]
    snapshot = next((r["result"] for r in tools_run if r["tool"] == "triage_snapshot"), {}) or {}
    if isinstance(snapshot, dict):
        pods = [f"{p.get('pod')} ready={p.get('ready')} restarts={p.get('restarts')}" for p in snapshot.get("pods") or []]
        warnings = [e.get("message") for e in snapshot.get("events") or [] if e.get("type") == "Warning"]
        lines += ["", "## 6. System state at investigation time",
                  f"- Pods: {', '.join(pods) or 'n/a'}",
                  f"- Database health: {json.dumps(snapshot.get('db_health'), default=str)}",
                  f"- Warning events: {len(warnings)}" + (f" (latest: {_clip(warnings[-1], 200)})" if warnings else "")]
    timeline = [r for r in records if r["type"] == "llm_response"]
    lines += ["", "## 7. Investigation timeline"]
    for item in timeline:
        calls = item["payload"].get("tool_calls") or []
        def short(arguments):
            text = json.dumps(arguments, default=str)
            return text if len(text) <= 160 else text[:157] + "…"

        action = ", ".join(f"{c['name']}({short(c.get('arguments'))})" for c in calls) or "final answer"
        lines.append(f"- round {item['round']}: {action} (tokens in {item['input_tokens']}, out {item['output_tokens']})")
    if not timeline:
        lines.append("_No AI rounds recorded (rule-based investigation)._")
    lines += ["", "## 8. Suggested prompts to continue",
              "- \u201cUsing the error, stack trace and code locations above, trace where the bad value originates, across services if needed.\u201d",
              "- \u201cReview the AI-proposed fix: does it fix the root cause, and will the failing request now succeed? Suggest a better patch if not.\u201d",
              "- \u201cWhich database rows or queries would confirm this root cause? Write read-only SQL for the schemas involved.\u201d",
              "- \u201cWrite a regression test that reproduces this error before the fix and passes after it.\u201d",
              "", f"Full machine-readable trail: `GET /incidents/{incident_id}/audit` or `scripts/export-incident-logs.ps1 -IncidentId {incident_id}`."]
    return "\n".join(line for line in lines if line is not None) + "\n"


def finish_incident(incident_id: int, category: str, snapshot: dict, report: str, usage=None, failure=None):
    with db() as conn:
        conn.execute(
            "update investigator.incidents set category=%s, snapshot=%s, report=%s, usage=%s, failure=%s, status=%s where id=%s",
            (category, json.dumps(snapshot, default=str), report, json.dumps(usage or {}), failure,
             "failed" if failure else "analyzed", incident_id),
        )


def set_status(incident_id, status, failure=None):
    with db() as conn:
        conn.execute("update investigator.incidents set status=%s, failure=%s where id=%s", (status, failure, incident_id))
