"""Vendor-agnostic log access. Kubernetes adapter = collector + query over the store.
Datadog/Loki/Splunk adapters implement the same Protocol without a collector."""
import hashlib
import json
import re
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Protocol

from kubernetes import client

from . import config, store


class LogSource(Protocol):
    def search(self, service: str | None, text: str | None, limit: int, time_window: int | None = None) -> list[dict]: ...
    def get_context(self, trace_id: str, limit: int, time_window: int | None = None) -> list[dict]: ...
    def list_services(self) -> list[str]: ...


def query_bounds(limit: int, time_window: int | None = None):
    limit = max(1, min(int(limit), 200))
    seconds = max(1, min(int(time_window if time_window is not None else config.LOG_QUERY_WINDOW_SECONDS), 86400))
    end = datetime.now(timezone.utc)
    return limit, end - timedelta(seconds=seconds), end


def make_log_source() -> LogSource:
    if config.LOG_PROVIDER == "kubernetes":
        return KubernetesLogSource()
    from .vendors import DatadogLogSource, LokiLogSource, SplunkLogSource

    providers = {"datadog": DatadogLogSource, "loki": LokiLogSource, "splunk": SplunkLogSource}
    if config.LOG_PROVIDER not in providers:
        raise ValueError("LOG_PROVIDER must be kubernetes, datadog, loki, or splunk")
    return providers[config.LOG_PROVIDER]()


def parse(line: str) -> dict:
    try:
        value = json.loads(line)
        return value if isinstance(value, dict) else {"message": line}
    except json.JSONDecodeError:
        return {"message": line}


VOLATILE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|\b[0-9a-f]{12,}\b|\d+", re.IGNORECASE)
EXCEPTION = re.compile(r"\b(?:[A-Za-z_][\w$]*\.)*[A-Z][\w$]*(?:Exception|Error)\b")
CORRELATION_FIELDS = {
    "trace_id": ("trace.id", "trace_id", "traceId", "dd.trace_id", "traceid"),
    "span_id": ("span.id", "span_id", "spanId", "dd.span_id", "spanid"),
    "correlation_id": ("correlation.id", "correlation_id", "correlationId", "x-correlation-id"),
    "request_id": ("request.id", "request_id", "requestId", "http.request.id", "x-request-id"),
}
TAG_ALIASES = {"dd.trace_id": "trace_id", "trace.id": "trace_id", "dd.span_id": "span_id", "span.id": "span_id",
               "correlation.id": "correlation_id", "x-correlation-id": "correlation_id",
               "request.id": "request_id", "x-request-id": "request_id"}


def lookup(record: dict, *names):
    for name in names:
        if isinstance(record, dict) and record.get(name) is not None:
            return record[name]
        value = record
        for part in name.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value is not None and not isinstance(value, dict):
            return value
    return None


def correlation_ids(record: dict | None, tags=()) -> dict:
    """trace/span/correlation/request ids from a log record and vendor tags (key:value)."""
    ids = {}
    for tag in tags or ():
        key, separator, value = str(tag).partition(":")
        key = TAG_ALIASES.get(key.strip().lower(), key.strip().lower())
        if separator and key in CORRELATION_FIELDS and value.strip():
            ids[key] = value.strip()[:128]
    for key, names in CORRELATION_FIELDS.items():
        value = lookup(record or {}, *names)
        if value is not None and str(value).strip():
            ids[key] = str(value).strip()[:128]
    return ids


def issue_fingerprint(service: str, message: str, error_type: str | None = None) -> str:
    """Same service + error type + message (ids and numbers removed) = same issue, whatever the source."""
    message = str(message or "")
    found = EXCEPTION.search(message)
    error_type = str(error_type or (found.group(0) if found else ""))
    head = VOLATILE.sub("#", message.split(" with root cause")[0]).strip().lower()[:200]
    return hashlib.sha256(f"{service}|{error_type}|{head}".encode()).hexdigest()[:32]


def fingerprint(service: str, record: dict) -> str:
    return issue_fingerprint(service, str(record.get("message", "")), record.get("error.type"))


def is_error(record: dict, message: str) -> bool:
    level = str(record.get("log.level", record.get("level", ""))).upper()
    if level in {"ERROR", "FATAL"}:
        return True
    lowered = message.lower()
    return "exception" in lowered or "oomkilled" in lowered


class KubernetesLogSource:
    def __init__(self):
        self.seen = OrderedDict()

    def collect_once(self, on_new_incident):
        store.prune_logs()
        api = client.CoreV1Api()
        for svc in config.services():
            pods = api.list_namespaced_pod(config.NAMESPACE, label_selector=svc["pod_selector"]).items
            for pod in pods:
                try:
                    output = api.read_namespaced_pod_log(pod.metadata.name, config.NAMESPACE, tail_lines=2000,
                                                         timestamps=True, since_seconds=config.LOG_RETENTION_SECONDS)
                except Exception as error:  # pod may be terminating
                    print(f"log read failed {pod.metadata.name}: {error}", flush=True)
                    continue
                occurrences = {}
                for line in output.splitlines():
                    timestamp, separator, body = line.partition(" ")
                    if separator and store.event_time(timestamp):
                        line = body
                    else:
                        timestamp = None
                    record = parse(line)
                    identity = (timestamp, line)
                    occurrences[identity] = occurrences.get(identity, 0) + 1
                    record.update({"_k8s_timestamp": timestamp, "_pod_uid": pod.metadata.uid,
                                   "_record_occurrence": occurrences[identity]})
                    message = str(record.get("message", line))
                    fp = fingerprint(svc["name"], record)
                    level = str(record.get("log.level", record.get("level", ""))).upper()
                    ids = correlation_ids(record)
                    trace_id = ids.get("trace_id")
                    if not store.insert_log(fp, svc["name"], pod.metadata.name, level, trace_id, message, record, self.seen):
                        continue
                    if not is_error(record, message):
                        continue
                    incident_id, created = store.upsert_incident(fp, svc["name"], f"{svc['name']}: {message[:180]}")
                    store.add_occurrence(incident_id, "kubernetes", service=svc["name"], pod=pod.metadata.name,
                                         event_at=record.get("@timestamp") or timestamp, ids=ids, message=message,
                                         details={"error.type": record.get("error.type"), "log.level": level})
                    if created:
                        on_new_incident(incident_id, svc["name"], fp, trace_id)

    def search(self, service, text, limit=50, time_window=None):
        limit, start, end = query_bounds(limit, time_window)
        sql = "select service, pod, event_at, observed_at, level, trace_id, message, raw from investigator.log_lines where coalesce(event_at, observed_at) between %s and %s"
        params: list = [start, end]
        if service:
            sql += " and service=%s"; params.append(service)
        if text:
            sql += " and message ilike %s"; params.append(f"%{text}%")
        sql += " order by coalesce(event_at, observed_at) desc, id desc limit %s"; params.append(limit)
        with store.db() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_context(self, trace_id, limit=100, time_window=None):
        limit, start, end = query_bounds(limit, time_window)
        with store.db() as conn:
            rows = conn.execute(
                "select service, pod, event_at, observed_at, level, trace_id, message, raw from investigator.log_lines where trace_id=%s and coalesce(event_at, observed_at) between %s and %s order by coalesce(event_at, observed_at), id limit %s",
                (trace_id, start, end, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_services(self):
        return [s["name"] for s in config.services()]
