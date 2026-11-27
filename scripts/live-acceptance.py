"""Opt-in, synthetic provider acceptance. Never serialize configuration or responses."""
import argparse
from contextlib import contextmanager
import importlib
import json
import os
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "investigator"))
PROVIDERS = ("llm", "embeddings", "datadog", "loki", "splunk", "github", "rag")
TASK = "What is the acceptance marker for the mint fixture? Reply with only the marker."
SYSTEM = "Synthetic acceptance only. Call lookup_fixture once, then answer with only the marker."
FIXTURES = [
    {"path": "mint.txt", "symbol": "mint", "start_line": 1,
     "content": "The acceptance marker for the mint fixture is mint."},
    {"path": "amber.txt", "symbol": "amber", "start_line": 1,
     "content": "The acceptance marker for the amber fixture is amber."},
    {"path": "rose.txt", "symbol": "rose", "start_line": 1,
     "content": "The acceptance marker for the rose fixture is rose."},
]
TOOL = {"name": "lookup_fixture", "description": "Read the synthetic acceptance fixtures.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}
MAX_BYTES = 2 * 1024 * 1024
SAFE_CODES = {"invalid_response", "invalid_arguments", "invalid_env_file", "http_error",
              "response_too_large", "usage_unavailable", "tool_step_failed", "final_answer_failed",
              "invalid_repository", "invalid_report_path", "conflicting_modes", "local_check_failed"}


class CheckFailure(Exception):
    def __init__(self, code="invalid_response", http_status=None):
        self.code = code
        self.http_status = http_status


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        raise CheckFailure("invalid_arguments")


def load_environment(path, environment):
    values = {}
    if path.exists():
        if path.stat().st_size > 65536:
            raise CheckFailure("invalid_env_file")
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:]
            key, separator, value = line.partition("=")
            if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
                raise CheckFailure("invalid_env_file")
            value = value.strip()
            if value.startswith(("'", '"')):
                if len(value) < 2 or value[-1] != value[0]:
                    raise CheckFailure("invalid_env_file")
                value = value[1:-1]
            else:
                value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
            values[key.strip()] = value
    values.update(environment)
    return values


def valid_base(value, local=False):
    try:
        parsed = urlsplit(value)
        parsed.port
        return bool(parsed.hostname and not parsed.username and not parsed.password
                    and not parsed.query and not parsed.fragment
                    and (parsed.scheme == "https" or
                         (local and parsed.scheme == "http" and parsed.hostname in
                          {"localhost", "127.0.0.1", "::1", "host.docker.internal"})))
    except ValueError:
        return False


def model_state(env, prefix):
    provider = env.get(prefix + "_PROVIDER", "none").strip().lower()
    allowed = {"openai", "ollama", "anthropic"} if prefix == "LLM" else {"openai", "ollama"}
    if provider in {"", "none"}:
        return "missing"
    if provider not in allowed:
        return "invalid"
    if not env.get(prefix + "_MODEL", "").strip():
        return "missing"
    if provider != "ollama" and not env.get(prefix + "_API_KEY"):
        return "missing"
    base = env.get(prefix + "_BASE_URL", "")
    if provider == "ollama" and not base:
        return "missing"
    if base and not valid_base(base, local=provider == "ollama"):
        return "invalid"
    return "configured"


def configuration(env):
    states = {"llm": model_state(env, "LLM"), "embeddings": model_state(env, "EMBEDDING")}
    states["rag"] = states["llm"]
    states["datadog"] = ("configured" if env.get("DATADOG_API_KEY") and env.get("DATADOG_APP_KEY")
                         else "missing")
    if states["datadog"] == "configured" and env.get("DATADOG_SITE", "datadoghq.com") not in {
        "datadoghq.com", "us3.datadoghq.com", "us5.datadoghq.com", "datadoghq.eu",
        "ap1.datadoghq.com", "ap2.datadoghq.com", "ddog-gov.com",
    }:
        states["datadog"] = "invalid"
    states["loki"] = "configured" if env.get("LOKI_BASE_URL") else "missing"
    if states["loki"] == "configured" and (
        not valid_base(env["LOKI_BASE_URL"])
        or bool(env.get("LOKI_USERNAME")) != bool(env.get("LOKI_PASSWORD"))
        or (env.get("LOKI_TOKEN") and env.get("LOKI_USERNAME"))
    ):
        states["loki"] = "invalid"
    states["splunk"] = ("configured" if env.get("SPLUNK_BASE_URL") and env.get("SPLUNK_TOKEN")
                        else "missing")
    if states["splunk"] == "configured" and (
        not valid_base(env["SPLUNK_BASE_URL"])
        or not re.fullmatch(r"[A-Za-z0-9_-]+", env.get("SPLUNK_INDEX", "main"))
    ):
        states["splunk"] = "invalid"
    states["github"] = "configured" if env.get("GITHUB_TOKEN") else "missing"
    return states


@contextmanager
def override(target, **values):
    previous = {key: getattr(target, key) for key in values}
    try:
        for key, value in values.items():
            setattr(target, key, value)
        yield
    finally:
        for key, value in previous.items():
            setattr(target, key, value)


def native_settings(env):
    module = importlib.import_module("investigator.config")
    keys = (
        "LLM_PROVIDER", "LLM_MODEL", "LLM_API_KEY", "LLM_BASE_URL", "DATADOG_SITE",
        "DATADOG_API_KEY", "DATADOG_APP_KEY", "LOKI_BASE_URL", "LOKI_USERNAME",
        "LOKI_PASSWORD", "LOKI_TOKEN", "LOKI_TENANT_ID", "SPLUNK_BASE_URL",
        "SPLUNK_TOKEN", "SPLUNK_INDEX",
    )
    defaults = {"LLM_PROVIDER": "none", "DATADOG_SITE": "datadoghq.com", "SPLUNK_INDEX": "main"}
    values = {key: env.get(key, defaults.get(key, "")) for key in keys}
    values["LLM_PROVIDER"] = values["LLM_PROVIDER"].strip().lower()
    values.update(LLM_RETRIES=0, LLM_TIMEOUT_SECONDS=15, LOG_HTTP_TIMEOUT_SECONDS=15,
                  services=lambda: [{"name": "live-acceptance-fixture"}])
    return override(module, **values)


def request_json(method, url, **kwargs):
    import httpx

    with httpx.Client(timeout=15, verify=True, follow_redirects=False, trust_env=False) as client:
        with client.stream(method, url, **kwargs) as response:
            if not 200 <= response.status_code < 300:
                raise CheckFailure("http_error", response.status_code)
            body = bytearray()
            for chunk in response.iter_bytes():
                if len(body) + len(chunk) > MAX_BYTES:
                    raise CheckFailure("response_too_large")
                body.extend(chunk)
            return json.loads(body)


def usage_counts(data, provider):
    usage = data.get("usage", {})
    names = ("input_tokens", "output_tokens") if provider == "anthropic" else (
        "prompt_tokens", "completion_tokens")
    counts = [usage.get(name) for name in names]
    if any(type(count) is not int or count < 0 for count in counts) or not sum(counts):
        raise CheckFailure("usage_unavailable")
    return {"input_tokens": counts[0], "output_tokens": counts[1]}


def conversation(env, rag=False):
    from investigator import llm, retrieval

    provider = env["LLM_PROVIDER"].strip().lower()
    observed = []

    def bounded_post(url, **kwargs):
        payload = dict(kwargs["json"])
        payload["max_completion_tokens" if provider == "openai" else "max_tokens"] = 128
        if payload.get("tools"):
            if provider == "anthropic":
                payload["tool_choice"] = {"type": "tool", "name": TOOL["name"],
                                          "disable_parallel_tool_use": True}
            else:
                payload["tool_choice"] = {"type": "function", "function": {"name": TOOL["name"]}}
                payload["parallel_tool_calls"] = False
        else:
            payload.pop("tools", None)
        data = request_json("POST", url, json=payload, headers=kwargs["headers"])
        observed.append(usage_counts(data, provider))
        return data

    with native_settings(env), override(llm, post=bounded_post):
        client = llm.make_client()
        messages = [{"role": "user", "content": TASK}]
        first = client.step(SYSTEM, messages, [TOOL])
        if len(first.tool_calls) != 1 or not first.assistant_message:
            raise CheckFailure("tool_step_failed")
        call = first.tool_calls[0]
        if call.error or call.name != TOOL["name"] or call.arguments or not call.id:
            raise CheckFailure("tool_step_failed")
        chunks = retrieval.lexical_rank(FIXTURES, TASK, limit=1) if rag else FIXTURES
        content = json.dumps([chunk["content"] for chunk in chunks])
        messages.append(first.assistant_message)
        messages.extend(llm.tool_result_messages(client, [(call, content, False)]))
        final = client.step(SYSTEM, messages, [])
        if final.tool_calls or not final.text or final.text.strip().lower() != "mint":
            raise CheckFailure("final_answer_failed")
    return {key: sum(item[key] for item in observed) for key in ("input_tokens", "output_tokens")}


def embedding_probe(env):
    from investigator import retrieval

    settings = SimpleNamespace(**{key: env.get(key, "") for key in (
        "EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "EMBEDDING_API_KEY", "EMBEDDING_BASE_URL")})
    settings.EMBEDDING_PROVIDER = settings.EMBEDDING_PROVIDER.strip().lower()
    observed = []
    info = {"provider": settings.EMBEDDING_PROVIDER, "model": settings.EMBEDDING_MODEL,
            "dimensions": 0}

    def bounded_post(url, **kwargs):
        data = request_json("POST", url, json=kwargs["json"], headers=kwargs["headers"])
        vectors = data.get("data", [])
        if len(vectors) != 1 or not isinstance(vectors[0].get("embedding"), list):
            raise CheckFailure()
        vector = vectors[0]["embedding"]
        if not 1 <= len(vector) <= 8192 or any(type(value) not in {int, float} for value in vector):
            raise CheckFailure()
        info["dimensions"] = len(vector)
        observed.append(data)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: data)

    with override(retrieval, httpx=SimpleNamespace(post=bounded_post)):
        vector = retrieval.query_embedding("synthetic acceptance mint", info, settings)
    count = observed[0].get("usage", {}).get("total_tokens")
    return {"dimensions": len(vector),
            "embedding_tokens": count if type(count) is int and count >= 0 else None,
            "embedding_cost": None, "currency": None}


def vendor_probe(provider, env):
    from investigator import vendors

    classes = {"datadog": vendors.DatadogLogSource, "loki": vendors.LokiLogSource,
               "splunk": vendors.SplunkLogSource}
    with native_settings(env):
        classes[provider]().search(service="live-acceptance-fixture",
                                   text="synthetic-acceptance-only", limit=1, time_window=60)
    return {"read_only_query": True}


def github_probe(env, repository):
    if repository is not None and (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or any(part in {".", ".."} for part in repository.split("/"))
    ):
        raise CheckFailure("invalid_repository")
    headers = {"Authorization": "Bearer " + env["GITHUB_TOKEN"],
               "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    user = request_json("GET", "https://api.github.com/user", headers=headers)
    if type(user.get("id")) is not int or not user.get("login"):
        raise CheckFailure()
    if repository:
        repo = request_json("GET", "https://api.github.com/repos/" + repository, headers=headers)
        if type(repo.get("id")) is not int or not repo.get("full_name"):
            raise CheckFailure()
    return {"user_checked": True, "repository_checked": bool(repository)}


def probe(provider, env, repository):
    if provider == "llm":
        return {"usage": conversation(env), "inference_cost": None, "currency": None}
    if provider == "rag":
        return {"task": "synthetic_fixture_marker", "retrieval": "native_lexical_fixture",
                "rag_off": {"usage": conversation(env, rag=False), "inference_cost": None},
                "rag_on": {"usage": conversation(env, rag=True), "inference_cost": None},
                "embedding_cost": None, "embedding_calls": 0, "currency": None,
                "savings_claimed": False, "production_rag_verified": False}
    if provider == "embeddings":
        return embedding_probe(env)
    if provider == "github":
        return github_probe(env, repository)
    return vendor_probe(provider, env)


def run(env, enabled=(), repository=None):
    states = configuration(env)
    results = {}
    for provider in PROVIDERS:
        state = states[provider]
        result = {"configuration": state, "enabled": provider in enabled}
        if state == "invalid":
            result.update(status="error", code="invalid_configuration")
        elif state == "missing":
            result.update(status="skipped", code="missing_configuration")
        elif provider not in enabled:
            result.update(status="check_only", code="not_enabled")
        else:
            try:
                result.update(probe(provider, env, repository), status="passed", code="ok")
            except Exception as exc:
                result.update(status="error", code="provider_check_failed")
                if type(exc) is CheckFailure:
                    result["code"] = exc.code if exc.code in SAFE_CODES else "provider_check_failed"
                    if type(exc.http_status) is int and 100 <= exc.http_status <= 599:
                        result["http_status"] = exc.http_status
        results[provider] = result
    failed = any(result["status"] == "error" for result in results.values())
    return {"mode": "live" if enabled else "check_only", "providers": results,
            "failed": failed, "credentials_disclosed": False}, int(failed)


def report_path(value):
    path = (ROOT / value).resolve()
    temporary = (ROOT / "tmp").resolve()
    if temporary != ROOT / "tmp" or not path.is_relative_to(temporary) or path.suffix != ".json":
        raise CheckFailure("invalid_report_path")
    return path


def main(argv=None, environment=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument("--enable", action="append", choices=PROVIDERS, default=[],
                        help="Explicitly permit this provider's small live calls (repeatable).")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--github-repo", help="Optional owner/repo for read-only metadata.")
    parser.add_argument("--report", default="tmp/live-acceptance.json")
    try:
        args = parser.parse_args(argv)
        if args.check_only and args.enable:
            raise CheckFailure("conflicting_modes")
        destination = report_path(args.report)
        env = load_environment(ROOT / ".env", os.environ if environment is None else environment)
        report, exit_code = run(env, set(args.enable), args.github_repo)
        serialized = json.dumps(report, indent=2, allow_nan=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(serialized + "\n", encoding="utf-8")
    except Exception as exc:
        code = exc.code if type(exc) is CheckFailure and exc.code in SAFE_CODES else "local_check_failed"
        serialized = json.dumps({"failed": True, "code": code})
        exit_code = 1
    print(serialized)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())