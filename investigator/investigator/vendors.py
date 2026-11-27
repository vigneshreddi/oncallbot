"""Read-only vendor queries; endpoints and credentials are operator configuration."""
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from . import config
from .sources import parse, query_bounds

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DATADOG_SITES = {"datadoghq.com", "us3.datadoghq.com", "us5.datadoghq.com", "datadoghq.eu",
                 "ap1.datadoghq.com", "ap2.datadoghq.com", "ddog-gov.com"}


class VendorError(RuntimeError):
    pass


def https_base(value: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment)
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Vendor base URL must be HTTPS without credentials, query, or fragment")
    return value.rstrip("/")


def field(record: dict, *names):
    for name in names:
        if record.get(name) is not None:
            return record[name]
        value = record
        for part in name.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value is not None:
            return value
    return None


def event_time(value):
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+)?", value)):
            number = float(value)
            if number > 100_000_000_000:
                number /= 1000
            return datetime.fromtimestamp(number, timezone.utc).isoformat()
        if value:
            timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            return timestamp.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        pass
    return None


def normalize(record: dict, timestamp=None) -> dict:
        return {"service": field(record, "service.name", "service"),
            "pod": field(record, "pod", "kubernetes.pod_name", "host.name", "host"),
            "event_at": event_time(timestamp if timestamp is not None else field(record, "@timestamp", "timestamp", "_time")),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "level": str(field(record, "log.level", "level", "status") or "").upper(),
            "trace_id": field(record, "trace_id", "trace.id", "traceId", "dd.trace_id"),
            "message": str(field(record, "message", "msg", "_raw") or ""), "raw": record}


def literal(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 1000 or any(ord(char) < 32 for char in value):
        raise ValueError("Log filter must be nonempty text of at most 1000 characters without control characters")
    return value


def dd_quote(value: str) -> str:
    escaped = re.sub(r'([+\-=&|><!(){}\[\]^"~*?:\\/])', r"\\\1", value)
    return '"' + escaped + '"'


class VendorLogSource:
    def __init__(self, transport=None):
        self.transport = transport
        self.headers = {}
        self.auth = None

    def list_services(self):
        return [entry["name"] for entry in config.services()]

    def search(self, service=None, text=None, limit=50, time_window=None):
        return self._query(service, literal(text), None, limit, time_window)

    def get_context(self, trace_id, limit=100, time_window=None):
        if trace_id is None:
            raise ValueError("trace_id is required")
        return self._query(None, None, literal(trace_id), limit, time_window)

    def _query(self, service, text, trace_id, limit, time_window):
        names = self.list_services()
        if service is not None:
            if service not in names:
                raise ValueError("service is not configured")
            names = [service]
        if not names:
            return []
        limit, start, end = query_bounds(limit, time_window)
        rows = self.fetch(names, text, trace_id, limit, start, end)
        rows = [row for row in rows if row["service"] in names
                and (trace_id is None or str(row["trace_id"]) == trace_id)]
        rows.sort(key=lambda row: row["event_at"] or "", reverse=trace_id is None)
        return rows[:limit]

    def fetch(self, names, text, trace_id, limit, start, end):
        raise NotImplementedError

    def request(self, method, path, **kwargs) -> bytes:
        started = time.monotonic()
        try:
            with httpx.Client(verify=True, follow_redirects=False, trust_env=False,
                              timeout=config.LOG_HTTP_TIMEOUT_SECONDS, headers=self.headers,
                              auth=self.auth, transport=self.transport) as client:
                with client.stream(method, self.base_url + path, **kwargs) as response:
                    if not 200 <= response.status_code < 300:
                        raise VendorError(f"Log provider returned HTTP {response.status_code}")
                    output = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        if len(output) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise VendorError("Log provider response exceeds byte limit")
                        if time.monotonic() - started > config.LOG_HTTP_TIMEOUT_SECONDS:
                            raise VendorError("Log provider response exceeded deadline")
                        output.extend(chunk)
                    return bytes(output)
        except httpx.HTTPError:
            raise VendorError("Log provider request failed") from None

    def json_request(self, method, path, **kwargs):
        try:
            return json.loads(self.request(method, path, **kwargs))
        except (ValueError, UnicodeError):
            raise VendorError("Log provider returned invalid JSON") from None


class DatadogLogSource(VendorLogSource):
    def __init__(self, transport=None):
        super().__init__(transport)
        if config.DATADOG_SITE not in DATADOG_SITES:
            raise ValueError("DATADOG_SITE is not a supported Datadog site")
        if not config.DATADOG_API_KEY or not config.DATADOG_APP_KEY:
            raise ValueError("DATADOG_API_KEY and DATADOG_APP_KEY are required")
        self.base_url = "https://api." + config.DATADOG_SITE
        self.headers = {"DD-API-KEY": config.DATADOG_API_KEY, "DD-APPLICATION-KEY": config.DATADOG_APP_KEY}

    def fetch(self, names, text, trace_id, limit, start, end):
        query = "(" + " OR ".join("service:" + dd_quote(name) for name in names) + ")"
        if text:
            query += " " + dd_quote(text)
        if trace_id:
            query += " (" + " OR ".join(name + ":" + dd_quote(trace_id) for name in
                                       ("@trace_id", "@trace.id", "@traceId", "@dd.trace_id")) + ")"
        data = self.json_request("POST", "/api/v2/logs/events/search", json={
            "filter": {"query": query, "from": start.isoformat(), "to": end.isoformat()},
            "sort": "timestamp" if trace_id else "-timestamp", "page": {"limit": limit}})
        try:
            rows = []
            for item in data["data"][:limit]:
                attrs = item["attributes"]
                record = dict(attrs.get("attributes") or {})
                for key in ("service", "host", "status", "message", "timestamp"):
                    if key in attrs:
                        record[key] = attrs[key]
                rows.append(normalize(record))
            return rows
        except (KeyError, TypeError, AttributeError, ValueError):
            raise VendorError("Datadog returned an invalid log response") from None


class LokiLogSource(VendorLogSource):
    def __init__(self, transport=None):
        super().__init__(transport)
        self.base_url = https_base(config.LOKI_BASE_URL)
        if bool(config.LOKI_USERNAME) != bool(config.LOKI_PASSWORD):
            raise ValueError("LOKI_USERNAME and LOKI_PASSWORD must be configured together")
        if config.LOKI_TOKEN and config.LOKI_USERNAME:
            raise ValueError("Configure Loki bearer token or basic authentication, not both")
        if config.LOKI_TOKEN:
            self.headers["Authorization"] = "Bearer " + config.LOKI_TOKEN
        elif config.LOKI_USERNAME:
            self.auth = httpx.BasicAuth(config.LOKI_USERNAME, config.LOKI_PASSWORD)
        if config.LOKI_TENANT_ID:
            self.headers["X-Scope-OrgID"] = config.LOKI_TENANT_ID

    def fetch(self, names, text, trace_id, limit, start, end):
        if len(names) == 1:
            query = "{service=" + json.dumps(names[0]) + "}"
        else:
            pattern = "^(?:" + "|".join(re.escape(name) for name in names) + ")$"
            query = "{service=~" + json.dumps(pattern) + "}"
        if text:
            query += " |= " + json.dumps(text)
        if trace_id:
            query += " | json | " + " or ".join(name + "=" + json.dumps(trace_id) for name in
                                               ("trace_id", "traceId", "dd_trace_id", "trace_id_extracted"))
        data = self.json_request("GET", "/loki/api/v1/query_range", params={
            "query": query, "start": str(int(start.timestamp() * 1_000_000_000)),
            "end": str(int(end.timestamp() * 1_000_000_000)), "limit": limit,
            "direction": "forward" if trace_id else "backward"})
        try:
            if data["status"] != "success" or data["data"]["resultType"] != "streams":
                raise VendorError("Loki returned a non-stream log response")
            rows = []
            for stream in data["data"]["result"]:
                for timestamp, line in stream["values"]:
                    record = {**stream["stream"], **parse(line)}
                    stamp = datetime.fromtimestamp(int(timestamp) / 1_000_000_000, timezone.utc).isoformat()
                    rows.append(normalize(record, stamp))
            return rows
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError, OSError):
            raise VendorError("Loki returned an invalid log response") from None


class SplunkLogSource(VendorLogSource):
    def __init__(self, transport=None):
        super().__init__(transport)
        self.base_url = https_base(config.SPLUNK_BASE_URL)
        if not config.SPLUNK_TOKEN:
            raise ValueError("SPLUNK_TOKEN is required")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", config.SPLUNK_INDEX):
            raise ValueError("SPLUNK_INDEX must be a single index name")
        self.headers = {"Authorization": "Bearer " + config.SPLUNK_TOKEN}

    def fetch(self, names, text, trace_id, limit, start, end):
        query = "search index=" + json.dumps(config.SPLUNK_INDEX)
        query += " (" + " OR ".join("service=" + json.dumps(name) for name in names) + ")"
        if text:
            query += " " + json.dumps(text)
        if trace_id:
            query += " (" + " OR ".join(name + "=" + json.dumps(trace_id) for name in
                                       ("trace_id", "trace.id", "traceId", "dd.trace_id")) + ")"
        query += " | sort 0 " + ("" if trace_id else "- ") + "_time | head " + str(limit)
        body = self.request("POST", "/services/search/jobs/export", data={
            "search": query, "output_mode": "json", "earliest_time": str(start.timestamp()),
            "latest_time": str(end.timestamp()), "exec_mode": "oneshot", "preview": "false",
            "max_count": str(limit), "max_time": str(config.LOG_HTTP_TIMEOUT_SECONDS)})
        try:
            rows = []
            for line in body.splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                if any(message.get("type") in {"ERROR", "FATAL"} for message in item.get("messages", [])):
                    raise VendorError("Splunk export failed")
                if item.get("preview"):
                    continue
                if "result" in item:
                    result = item["result"]
                    record = {**parse(result.get("_raw", "")), **result}
                    rows.append(normalize(record))
            return rows
        except (ValueError, TypeError, AttributeError):
            raise VendorError("Splunk returned an invalid export response") from None