"""Validated vendor alert payloads mapped to a configured service and event."""
import hashlib
import re
from dataclasses import asdict, dataclass, field as dataclass_field
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import config
from .sources import EXCEPTION, correlation_ids, issue_fingerprint
from .vendors import event_time, field, parse


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


def timestamp(value: str | int) -> str:
    parsed = event_time(value)
    if parsed is None:
        raise ValueError("event time must be an ISO-8601 timestamp or Unix seconds/milliseconds")
    observed = datetime.fromisoformat(parsed)
    if not 2000 <= observed.year <= datetime.now(timezone.utc).year + 1:
        raise ValueError("event time is outside the supported range")
    return parsed


def configured_service(service: str | None, tags: list[str]) -> str:
    names = {tag.split(":", 1)[1].strip() for tag in tags if tag.startswith("service:")}
    if service:
        names.add(service)
    if len(names) != 1:
        raise ValueError("exactly one service must be specified by service or service tags")
    name = names.pop()
    if not config.service_by_name(name):
        raise ValueError("service is not configured")
    return name


@dataclass(frozen=True)
class IncidentEvent:
    source: str
    service: str
    title: str
    event_id: str | None
    event_at: str
    trace_id: str | None
    metadata: dict
    rule: str | None = None
    error_type: str | None = None
    message: str | None = None
    ids: dict = dataclass_field(default_factory=dict)

    def evidence(self):
        return asdict(self)

    def fingerprint(self) -> str:
        """One incident per issue: the error signature when the alert carries one, otherwise the alerting rule."""
        text = self.message or ""
        if self.error_type or EXCEPTION.search(text):
            return issue_fingerprint(self.service, text or self.title, self.error_type)
        rule = self.rule or re.sub(r"\d+", "#", self.title.lower())
        return "alert:" + hashlib.sha256(f"{self.service}\0{self.source}\0{rule}".encode()).hexdigest()[:32]

    def correlation(self) -> dict:
        return {**self.ids, **({"trace_id": self.trace_id} if self.trace_id else {})}


class DatadogWebhook(Payload):
    service: str | None = Field(default=None, min_length=1, max_length=128)
    tags: list[str] | str = Field(default_factory=list)
    date: str | int
    id: str | int | None = None
    monitor_id: str | int | None = None
    monitor_name: str | None = Field(default=None, min_length=1, max_length=500)
    alert_id: str | int | None = None
    alert_title: str | None = Field(default=None, min_length=1, max_length=500)
    alert_status: str | None = Field(default=None, max_length=64)
    alert_transition: str | None = Field(default=None, max_length=64)
    event_type: str = Field(default="monitor_alert", min_length=1, max_length=64)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    text: str | None = Field(default=None, max_length=10000)
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)
    span_id: str | None = Field(default=None, min_length=1, max_length=128)
    correlation_id: str | None = Field(default=None, min_length=1, max_length=128)
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    error_type: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("id", "monitor_id", "alert_id")
    @classmethod
    def identifier(cls, value):
        if value is not None and (not str(value).strip() or len(str(value)) > 128):
            raise ValueError("event and monitor identifiers must contain 1 to 128 characters")
        return value

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, value):
        if isinstance(value, str):
            if len(value) > 4096:
                raise ValueError("tags exceed length limit")
            value = [tag.strip() for tag in value.split(",") if tag.strip()]
        if len(value) > 64 or any(not tag or len(tag) > 256 for tag in value):
            raise ValueError("tags must contain at most 64 nonempty entries of at most 256 characters")
        return value

    @field_validator("date")
    @classmethod
    def validate_date(cls, value):
        return timestamp(value)

    @model_validator(mode="after")
    def validate_monitor(self):
        if self.monitor_id is None and self.alert_id is None:
            raise ValueError("monitor_id or alert_id is required")
        if not (self.alert_title or self.title or self.monitor_name):
            raise ValueError("a monitor name or alert title is required")
        configured_service(self.service, self.tags)
        return self

    def to_event(self) -> IncidentEvent:
        record = parse(self.text) if self.text and self.text.lstrip().startswith("{") else {}
        ids = correlation_ids(record, self.tags)
        ids.update({key: value for key, value in {"span_id": self.span_id, "correlation_id": self.correlation_id,
                                                  "request_id": self.request_id}.items() if value})
        trace_id = self.trace_id or ids.get("trace_id")
        monitor = self.monitor_id if self.monitor_id is not None else self.alert_id
        return IncidentEvent("datadog", configured_service(self.service, self.tags),
                             self.alert_title or self.title or self.monitor_name,
                             str(self.id) if self.id is not None else None, self.date, trace_id,
                             {"monitor_id": monitor,
                              "monitor_name": self.monitor_name, "alert_id": self.alert_id,
                              "alert_status": self.alert_status, "alert_transition": self.alert_transition,
                              "event_type": self.event_type, "tags": self.tags, "text": self.text},
                             rule=f"monitor:{monitor}",
                             error_type=self.error_type or field(record, "error.type", "error.kind"),
                             message=str(field(record, "message") or self.text or ""), ids=ids)


class LokiAlert(Payload):
    status: Literal["firing", "resolved"] = "firing"
    labels: dict[str, str] = Field(max_length=64)
    annotations: dict[str, str] = Field(default_factory=dict, max_length=64)
    startsAt: str = Field(max_length=128)
    endsAt: str | None = Field(default=None, max_length=128)
    fingerprint: str = Field(min_length=1, max_length=128)
    generatorURL: str | None = Field(default=None, max_length=2048)
    @field_validator("labels")
    @classmethod
    def validate_trace(cls, value):
        trace_id = value.get("trace_id")
        if trace_id is not None and not 1 <= len(trace_id) <= 128:
            raise ValueError("trace_id must contain 1 to 128 characters")
        return value

    @field_validator("labels", "annotations")
    @classmethod
    def bounded_entries(cls, value):
        if any(len(key) > 128 or len(item) > 1000 for key, item in value.items()):
            raise ValueError("alert labels and annotations exceed length limit")
        return value

    @field_validator("startsAt")
    @classmethod
    def validate_start(cls, value):
        return timestamp(value)

    @model_validator(mode="after")
    def validate_service(self):
        configured_service(self.labels.get("service"), [])
        if self.endsAt and self.endsAt != "0001-01-01T00:00:00Z":
            timestamp(self.endsAt)
        return self

    def to_event(self):
        title = self.annotations.get("summary") or self.labels.get("alertname") or "Loki alert"
        ids = correlation_ids(self.labels)
        return IncidentEvent("loki", self.labels["service"], title[:500], f"{self.fingerprint}@{self.startsAt}",
                             self.startsAt, self.labels.get("trace_id"),
                             {"status": self.status, "labels": self.labels, "annotations": self.annotations},
                             rule=f"alertname:{self.labels.get('alertname') or title}",
                             error_type=self.labels.get("error_type") or self.labels.get("exception"),
                             message=self.annotations.get("description") or self.annotations.get("message"), ids=ids)


class LokiWebhook(Payload):
    status: Literal["firing", "resolved"] = "firing"
    alerts: list[LokiAlert] = Field(min_length=1, max_length=20)
    receiver: str | None = Field(default=None, max_length=256)
    version: str | None = Field(default=None, max_length=32)
    groupKey: str | None = Field(default=None, max_length=2048)
    truncatedAlerts: int = Field(default=0, ge=0)
    groupLabels: dict[str, str] = Field(default_factory=dict, max_length=64)
    commonLabels: dict[str, str] = Field(default_factory=dict, max_length=64)
    commonAnnotations: dict[str, str] = Field(default_factory=dict, max_length=64)
    externalURL: str | None = Field(default=None, max_length=2048)

    @field_validator("groupLabels", "commonLabels", "commonAnnotations")
    @classmethod
    def bounded_entries(cls, value):
        return LokiAlert.bounded_entries(value)


class SplunkResult(Payload):
    service: str = Field(min_length=1, max_length=128)
    time: str | int = Field(alias="_time")
    raw: str | None = Field(default=None, alias="_raw", max_length=10000)
    message: str | None = Field(default=None, max_length=500)
    trace_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("time")
    @classmethod
    def validate_time(cls, value):
        return timestamp(value)

    @model_validator(mode="after")
    def validate_service(self):
        configured_service(self.service, [])
        return self


class SplunkWebhook(Payload):
    search_name: str = Field(min_length=1, max_length=500)
    sid: str = Field(min_length=1, max_length=128)
    result: SplunkResult
    results_link: str | None = Field(default=None, max_length=2048)
    owner: str | None = Field(default=None, max_length=128)
    app: str | None = Field(default=None, max_length=128)

    def to_event(self):
        record = parse(self.result.raw) if self.result.raw else {}
        trace_id = self.result.trace_id or field(record, "trace_id", "trace.id", "traceId", "dd.trace_id")
        if trace_id is not None and (not isinstance(trace_id, str) or not 1 <= len(trace_id) <= 128):
            raise ValueError("trace_id must contain 1 to 128 characters")
        return IncidentEvent("splunk", self.result.service, self.result.message or self.search_name,
                             self.sid, self.result.time, trace_id, {"search_name": self.search_name},
                             rule=f"search:{self.search_name}",
                             error_type=field(record, "error.type", "error.kind", "exception.type"),
                             message=self.result.message or field(record, "message", "msg"), ids=correlation_ids(record))