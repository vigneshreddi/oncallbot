import threading
import time
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from kubernetes import config as kube_config
from pydantic import BaseModel, Field, ConfigDict

from . import agent, config, store
from .sources import KubernetesLogSource, make_log_source
from .tools import index_status
from .vendors import VendorError
from .webhooks import DatadogWebhook, IncidentEvent, LokiWebhook, SplunkWebhook
from .fixes import FixError, FixRequest, ensure_enabled, propose_fix

app = FastAPI(title="Incident Investigator", version="0.2.0")
source = make_log_source()
workers = ThreadPoolExecutor(max_workers=config.INVESTIGATION_WORKERS, thread_name_prefix="investigator")
slots = threading.BoundedSemaphore(config.INVESTIGATION_WORKERS + config.INVESTIGATION_QUEUE_SIZE)


class IncidentWebhook(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    service: str = Field(min_length=1, max_length=128)
    title: str = Field(default="external alert", min_length=1, max_length=500)
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)


def run_investigation(incident_id, service, fingerprint, trace_id):
    try:
        agent.investigate(incident_id, service, fingerprint, trace_id)
    except Exception as error:
        store.set_status(incident_id, "failed", f"Worker failed: {type(error).__name__}")
    finally:
        slots.release()


def on_new_incident(incident_id, service, fingerprint, trace_id):
    print(f"incident detected id={incident_id} service={service}", flush=True)
    if not slots.acquire(blocking=False):
        store.set_status(incident_id, "failed", "Investigation queue capacity exhausted")
        return False
    try:
        store.set_status(incident_id, "queued")
        workers.submit(run_investigation, incident_id, service, fingerprint, trace_id)
    except Exception as error:
        slots.release()
        store.set_status(incident_id, "failed", f"Worker submission failed: {type(error).__name__}")
        return False
    return True


def collector_loop():
    while True:
        try:
            source.collect_once(on_new_incident)
        except Exception as error:
            print(f"collector error: {error}", flush=True)
        time.sleep(5)


@app.on_event("startup")
def startup():
    store.ensure_schema()
    for entry in index_status():
        if not entry.get("ready"):
            print(f"warning: {entry.get('finding')}; GitHub-direct fallback only", flush=True)
    if isinstance(source, KubernetesLogSource):
        kube_config.load_incluster_config()
        threading.Thread(target=collector_loop, daemon=True).start()


@app.get("/health")
def health():
    return {"status": "UP", "namespace": config.NAMESPACE, "services": source.list_services(),
            "llm": config.LLM_PROVIDER, "log_provider": config.LOG_PROVIDER, "code_index": index_status()}


@app.get("/incidents")
def incidents():
    with store.db() as conn:
        rows = conn.execute("select id, service, category, status, title, first_seen, last_seen, occurrences, usage, failure from investigator.incidents order by last_seen desc").fetchall()
    return [dict(r) for r in rows]


@app.get("/incidents/{incident_id}/report", response_class=PlainTextResponse)
def report(incident_id: int):
    with store.db() as conn:
        row = conn.execute("select report from investigator.incidents where id=%s", (incident_id,)).fetchone()
    if not row:
        raise HTTPException(404)
    return row["report"] or "report not ready"


@app.get("/incidents/{incident_id}/occurrences")
def occurrences(incident_id: int, limit: int = Query(default=200, ge=1, le=1000)):
    return store.incident_occurrences(incident_id, limit)


@app.get("/incidents/{incident_id}/handoff", response_class=PlainTextResponse)
def handoff(incident_id: int):
    brief = store.incident_handoff(incident_id)
    if brief is None:
        raise HTTPException(404)
    return PlainTextResponse(brief, media_type="text/markdown; charset=utf-8")


@app.get("/incidents/{incident_id}/audit", response_class=PlainTextResponse)
def audit(incident_id: int):
    records = store.incident_audit(incident_id)
    if records is None:
        raise HTTPException(404)
    lines = "\n".join(json.dumps(record, default=str, ensure_ascii=False) for record in records) + "\n"
    return PlainTextResponse(lines, media_type="application/x-ndjson")


@app.get("/logs")
def logs(service: str | None = None, text: str | None = Query(default=None, max_length=1000),
         limit: int = Query(default=100, ge=1, le=200),
         time_window: int | None = Query(default=None, ge=1, le=86400)):
    try:
        return source.search(service, text, limit, time_window=time_window)
    except VendorError as error:
        raise HTTPException(502, str(error)) from None
    except ValueError as error:
        raise HTTPException(422, str(error)) from None


@app.post("/webhooks/incident")
def webhook(payload: IncidentWebhook):
    """Generic trigger retained for integrations with their own payload mapping."""
    service = payload.service
    if not config.service_by_name(service):
        raise HTTPException(422, "service is not configured")
    title = payload.title
    fp = "webhook:" + hashlib.sha256(f"{service}\0{title}".encode()).hexdigest()
    incident_id, created = store.upsert_incident(fp, service, f"{service}: {title}")
    store.add_occurrence(incident_id, "webhook", service=service, ids={"trace_id": payload.trace_id} if payload.trace_id else {},
                         message=title)
    if created:
        if not on_new_incident(incident_id, service, fp, payload.trace_id):
            raise HTTPException(503, {"incident_id": incident_id, "error": "investigation could not be scheduled"})
    return {"incident_id": incident_id, "created": created}


def vendor_incident(event: IncidentEvent):
    existing = store.occurrence_incident(event.source, event.event_id)
    if existing:
        return {"incident_id": existing, "created": False, "duplicate": True}
    fp = event.fingerprint()
    incident_id, created = store.upsert_incident(fp, event.service, f"{event.service}: {event.title}")
    store.add_occurrence(incident_id, event.source, external_id=event.event_id, service=event.service,
                         event_at=event.event_at, ids=event.correlation(), message=event.message or event.title,
                         details={"rule": event.rule, "error_type": event.error_type, "metadata": event.metadata})
    if created:
        store.add_evidence(incident_id, 0, "webhook_event", event.evidence(), json.dumps(event.evidence()))
        if not on_new_incident(incident_id, event.service, fp, event.trace_id):
            raise HTTPException(503, {"incident_id": incident_id, "error": "investigation could not be scheduled"})
    return {"incident_id": incident_id, "created": created}


@app.post("/webhooks/datadog")
def datadog_webhook(payload: DatadogWebhook):
    return vendor_incident(payload.to_event())


@app.post("/webhooks/loki")
def loki_webhook(payload: LokiWebhook):
    return {"incidents": [vendor_incident(alert.to_event()) for alert in payload.alerts]}


@app.post("/webhooks/splunk")
def splunk_webhook(payload: SplunkWebhook):
    try:
        event = payload.to_event()
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    return vendor_incident(event)


@app.post("/incidents/{incident_id}/propose-fix", status_code=201)
def incident_propose_fix(incident_id: int, payload: FixRequest):
    try:
        ensure_enabled()
        with store.db() as conn:
            row = conn.execute("select service, status from investigator.incidents where id=%s", (incident_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Incident not found")
        if row["status"] != "analyzed":
            raise HTTPException(409, "Incident analysis must be complete before proposing a fix")
        return propose_fix(incident_id, row["service"], payload)
    except FixError as error:
        raise HTTPException(error.status, error.detail) from error


@app.get("/incidents/{incident_id}/fix")
def incident_fix(incident_id: int):
    return store.fix_proposals(incident_id)


@app.post("/incidents/{incident_id}/open-pr")
def incident_open_pr(incident_id: int):
    """Approve stored AI proposals: the investigator opens the draft pull request(s)."""
    try:
        ensure_enabled()
    except FixError as error:
        raise HTTPException(error.status, error.detail) from error
    with store.db() as conn:
        row = conn.execute("select status from investigator.incidents where id=%s", (incident_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Incident not found")
    if row["status"] != "analyzed":
        raise HTTPException(409, "Incident analysis must be complete before opening a pull request")
    pending = [p for p in store.fix_proposals(incident_id) if p["status"] in {"proposed", "pr_failed"}]
    if not pending:
        raise HTTPException(409, "No unopened AI fix proposal for this incident")
    return {"results": agent.open_stored_proposals(incident_id, pending), "proposals": store.fix_proposals(incident_id)}


@app.post("/incidents/{incident_id}/reinvestigate", status_code=202)
def incident_reinvestigate(incident_id: int):
    with store.db() as conn:
        row = conn.execute("select service, fingerprint, status from investigator.incidents where id=%s", (incident_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Incident not found")
    if row["status"] in {"queued", "running"}:
        raise HTTPException(409, "Investigation already in progress")
    store.reset_investigation(incident_id)
    if not on_new_incident(incident_id, row["service"], row["fingerprint"], None):
        raise HTTPException(503, "Investigation queue is full")
    return {"incident_id": incident_id, "status": "queued"}
