import os
from urllib.parse import quote_plus

import yaml

NAMESPACE = os.getenv("TARGET_NAMESPACE", "incident-lab")
DB_DSN = os.getenv("DATABASE_URL", "postgresql://app@postgres:5432/incidents")
RO_PASSWORD = os.getenv("RO_PASSWORD", "")
if RO_PASSWORD:
    RO_DB_DSN = f"postgresql://investigator_ro:{quote_plus(RO_PASSWORD)}@postgres:5432/incidents"
else:
    RO_DB_DSN = ""
CONFIG_PATH = os.getenv("SERVICES_CONFIG", "/app/config/services.yaml")

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "none")  # anthropic | openai | ollama | none
LLM_MODEL = os.getenv("LLM_MODEL", "")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
LLM_MAX_STEPS = max(1, min(int(os.getenv("LLM_MAX_STEPS", "8")), 32))
LLM_RETRIES = max(0, min(int(os.getenv("LLM_RETRIES", "2")), 5))
LLM_TIMEOUT_SECONDS = max(1, min(int(os.getenv("LLM_TIMEOUT_SECONDS", "60")), 120))
LLM_MAX_TOOL_CALLS = max(1, min(int(os.getenv("LLM_MAX_TOOL_CALLS", "32")), 100))
INVESTIGATION_WORKERS = max(1, min(int(os.getenv("INVESTIGATION_WORKERS", "2")), 64))
INVESTIGATION_QUEUE_SIZE = max(0, min(int(os.getenv("INVESTIGATION_QUEUE_SIZE", "8")), 1024))
LOG_RETENTION_SECONDS = max(60, int(os.getenv("LOG_RETENTION_SECONDS", "86400")))
LOG_CACHE_SIZE = max(1, int(os.getenv("LOG_CACHE_SIZE", "10000")))
INCIDENT_WINDOW_SECONDS = max(1, int(os.getenv("INCIDENT_WINDOW_SECONDS", "300")))

LOG_PROVIDER = os.getenv("LOG_PROVIDER", "kubernetes").strip().lower()
LOG_QUERY_WINDOW_SECONDS = max(1, min(int(os.getenv("LOG_QUERY_WINDOW_SECONDS", "900")), 86400))
LOG_HTTP_TIMEOUT_SECONDS = max(1, min(int(os.getenv("LOG_HTTP_TIMEOUT_SECONDS", "30")), 60))
DATADOG_SITE = os.getenv("DATADOG_SITE", "datadoghq.com")
DATADOG_API_KEY = os.getenv("DATADOG_API_KEY", "")
DATADOG_APP_KEY = os.getenv("DATADOG_APP_KEY", "")
LOKI_BASE_URL = os.getenv("LOKI_BASE_URL", "")
LOKI_USERNAME = os.getenv("LOKI_USERNAME", "")
LOKI_PASSWORD = os.getenv("LOKI_PASSWORD", "")
LOKI_TOKEN = os.getenv("LOKI_TOKEN", "")
LOKI_TENANT_ID = os.getenv("LOKI_TENANT_ID", "")
SPLUNK_BASE_URL = os.getenv("SPLUNK_BASE_URL", "")
SPLUNK_TOKEN = os.getenv("SPLUNK_TOKEN", "")
SPLUNK_INDEX = os.getenv("SPLUNK_INDEX", "main")

EMBEDDING_PROVIDER = os.getenv("EMBEDDING_PROVIDER", "none")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "")
EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "")
EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", "")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")


def services() -> list[dict]:
    with open(CONFIG_PATH, encoding="utf-8") as stream:
        return yaml.safe_load(stream).get("services", [])


def service_by_name(name: str) -> dict | None:
    return next((s for s in services() if s["name"] == name), None)
