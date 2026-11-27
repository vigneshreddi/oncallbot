import importlib.util
import json
from pathlib import Path

import httpx
import pytest

SPEC = importlib.util.spec_from_file_location(
    "live_acceptance", Path(__file__).resolve().parents[1] / "scripts" / "live-acceptance.py")
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)

SECRET = "mock-credential-never-print"


def llm_env(provider="openai"):
    return {"LLM_PROVIDER": provider, "LLM_MODEL": "mock-model", "LLM_API_KEY": SECRET}


def test_default_is_offline_even_when_configured(monkeypatch):
    monkeypatch.setattr(acceptance, "probe", lambda *args: pytest.fail("live call"))
    report, code = acceptance.run({**llm_env(), "GITHUB_TOKEN": SECRET})
    assert code == 0
    assert report["providers"]["llm"]["status"] == "check_only"
    assert report["providers"]["github"]["configuration"] == "configured"
    assert SECRET not in json.dumps(report)


def test_missing_providers_skip_and_git_auth_is_not_app_token(monkeypatch):
    monkeypatch.setattr(acceptance, "probe", lambda *args: pytest.fail("live call"))
    report, code = acceptance.run({"GIT_ASKPASS": SECRET, "GH_TOKEN": SECRET}, acceptance.PROVIDERS)
    assert code == 0
    assert all(item["status"] == "skipped" for item in report["providers"].values())


def test_failure_redacts_exception_and_does_not_stop_other_checks(monkeypatch):
    called = []

    def fail(provider, *args):
        called.append(provider)
        raise RuntimeError("Bearer " + SECRET + " https://name:password@host/?token=" + SECRET)

    monkeypatch.setattr(acceptance, "probe", fail)
    report, code = acceptance.run({**llm_env(), "GITHUB_TOKEN": SECRET}, {"llm", "github"})
    assert code == 1
    assert called == ["llm", "github"]
    assert SECRET not in json.dumps(report)
    assert "password" not in json.dumps(report)


@pytest.mark.parametrize("env", [
    {"LLM_PROVIDER": "unknown", "LLM_MODEL": SECRET},
    {**llm_env(), "LLM_BASE_URL": "https://user:secret@example.com"},
    {"LOKI_BASE_URL": "http://example.com"},
    {"LOKI_BASE_URL": "https://example.com", "LOKI_USERNAME": SECRET},
    {"LOKI_BASE_URL": "https://example.com", "LOKI_TOKEN": SECRET,
     "LOKI_USERNAME": SECRET, "LOKI_PASSWORD": SECRET},
    {"SPLUNK_BASE_URL": "https://example.com", "SPLUNK_TOKEN": SECRET, "SPLUNK_INDEX": "*"},
    {"DATADOG_API_KEY": SECRET, "DATADOG_APP_KEY": SECRET, "DATADOG_SITE": "unknown"},
])
def test_invalid_configuration_fails_without_live_call(env, monkeypatch):
    monkeypatch.setattr(acceptance, "probe", lambda *args: pytest.fail("live call"))
    report, code = acceptance.run(env)
    assert code == 1
    assert SECRET not in json.dumps(report)


def test_dotenv_local_loading_and_environment_precedence(tmp_path):
    path = tmp_path / ".env"
    path.write_text('export LLM_API_KEY="' + SECRET + '"\nLLM_MODEL=fixture # comment\n', encoding="utf-8")
    env = acceptance.load_environment(path, {"LLM_MODEL": "override"})
    assert env["LLM_API_KEY"] == SECRET
    assert env["LLM_MODEL"] == "override"
    assert acceptance.load_environment(tmp_path / "absent", {}) == {}


@pytest.mark.parametrize("provider", ["openai", "anthropic", "ollama"])
def test_native_llm_tool_then_final_bounded_and_sequential(provider, monkeypatch):
    from investigator import config

    original_provider = config.LLM_PROVIDER
    requests = []

    def request(method, url, **kwargs):
        payload = kwargs["json"]
        requests.append(payload)
        assert method == "POST"
        assert payload["max_completion_tokens" if provider == "openai" else "max_tokens"] == 128
        usage = ({"input_tokens": 17, "output_tokens": 3} if provider == "anthropic"
                 else {"prompt_tokens": 17, "completion_tokens": 3})
        if len(requests) == 1:
            if provider == "anthropic":
                assert payload["tool_choice"]["disable_parallel_tool_use"] is True
                return {"content": [{"type": "tool_use", "id": "mock-id",
                                     "name": "lookup_fixture", "input": {}}], "usage": usage}
            assert payload["parallel_tool_calls"] is False
            return {"choices": [{"message": {"role": "assistant", "content": None,
                    "tool_calls": [{"id": "mock-id", "type": "function", "function": {
                        "name": "lookup_fixture", "arguments": "{}"}}]}}], "usage": usage}
        assert "tools" not in payload
        if provider == "anthropic":
            assert payload["messages"][-1]["content"][0]["tool_use_id"] == "mock-id"
            return {"content": [{"type": "text", "text": "mint"}], "usage": usage}
        assert payload["messages"][-1]["tool_call_id"] == "mock-id"
        return {"choices": [{"message": {"role": "assistant", "content": "mint"}}], "usage": usage}

    monkeypatch.setattr(acceptance, "request_json", request)
    env = llm_env(provider)
    if provider == "ollama":
        env["LLM_BASE_URL"] = "http://localhost:11434/v1"
    report, code = acceptance.run(env, {"llm"})
    assert code == 0
    assert len(requests) == 2
    assert report["providers"]["llm"]["usage"] == {"input_tokens": 34, "output_tokens": 6}
    assert config.LLM_PROVIDER == original_provider
    assert SECRET not in json.dumps(report)


def test_rag_runs_identical_task_with_actual_native_retrieval(monkeypatch):
    records = []

    def request(method, url, **kwargs):
        payload = kwargs["json"]
        records.append(payload)
        if payload.get("tools"):
            message = {"role": "assistant", "tool_calls": [{"id": "fixture-id",
                "function": {"name": "lookup_fixture", "arguments": "{}"}}]}
        else:
            message = {"role": "assistant", "content": "mint"}
        return {"choices": [{"message": message}],
                "usage": {"prompt_tokens": 20 if len(records) <= 2 else 12, "completion_tokens": 2}}

    monkeypatch.setattr(acceptance, "request_json", request)
    report, code = acceptance.run(llm_env(), {"rag"})
    result = report["providers"]["rag"]
    assert code == 0
    assert len(records) == 4
    assert records[0]["messages"][1] == records[2]["messages"][1]
    assert "amber fixture" in records[1]["messages"][-1]["content"]
    assert "amber fixture" not in records[3]["messages"][-1]["content"]
    assert result["rag_off"]["usage"]["input_tokens"] == 40
    assert result["rag_on"]["usage"]["input_tokens"] == 24
    assert result["rag_off"]["inference_cost"] is None
    assert result["savings_claimed"] is False
    assert result["production_rag_verified"] is False


@pytest.mark.parametrize("problem", ["no_tool", "extra_tools", "wrong_tool", "bad_args",
                                    "missing_usage", "wrong_final", "final_tool"])
def test_llm_protocol_defects_fail_without_unbounded_steps(problem, monkeypatch):
    calls = []

    def request(*args, **kwargs):
        calls.append(kwargs)
        tool = {"id": "fixture-id", "function": {"name": "lookup_fixture", "arguments": "{}"}}
        message = {"role": "assistant", "tool_calls": [tool]}
        if problem == "no_tool":
            message = {"role": "assistant", "content": "mint"}
        elif problem == "extra_tools":
            message["tool_calls"].append(tool)
        elif problem == "wrong_tool":
            tool["function"]["name"] = "create_pr"
        elif problem == "bad_args":
            tool["function"]["arguments"] = '{"secret": "' + SECRET + '"}'
        if len(calls) == 2 and problem != "final_tool":
            message = {"role": "assistant", "content": "wrong" if problem == "wrong_final" else "mint"}
        data = {"choices": [{"message": message}], "usage": {"prompt_tokens": 3, "completion_tokens": 2}}
        if problem == "missing_usage":
            data.pop("usage")
        return data

    monkeypatch.setattr(acceptance, "request_json", request)
    report, code = acceptance.run(llm_env(), {"llm"})
    assert code == 1
    assert len(calls) <= 2
    assert SECRET not in json.dumps(report)


@pytest.mark.parametrize("provider", ["openai", "ollama"])
def test_embedding_one_small_native_validated_request(provider, monkeypatch):
    calls = []

    def request(*args, **kwargs):
        calls.append(kwargs)
        assert kwargs["json"]["input"] == ["synthetic acceptance mint"]
        return {"data": [{"index": 0, "embedding": [0.1, 0.2]}], "usage": {"total_tokens": 4}}

    monkeypatch.setattr(acceptance, "request_json", request)
    env = {"EMBEDDING_PROVIDER": provider, "EMBEDDING_MODEL": "mock-embedding",
           "EMBEDDING_API_KEY": SECRET, "EMBEDDING_BASE_URL": "https://example.com/v1"}
    report, code = acceptance.run(env, {"embeddings"})
    assert code == 0
    assert len(calls) == 1
    assert report["providers"]["embeddings"]["dimensions"] == 2
    assert SECRET not in json.dumps(report)


@pytest.mark.parametrize("vector", [[], [0, 0], [float("nan"), 1], [True, 1], ["secret", 1]])
def test_invalid_embeddings_fail(vector, monkeypatch):
    monkeypatch.setattr(acceptance, "request_json", lambda *args, **kwargs: {
        "data": [{"index": 0, "embedding": vector}]})
    report, code = acceptance.run({"EMBEDDING_PROVIDER": "openai", "EMBEDDING_MODEL": "mock",
                                   "EMBEDDING_API_KEY": SECRET}, {"embeddings"})
    assert code == 1
    assert SECRET not in json.dumps(report)


@pytest.mark.parametrize("provider", ["datadog", "loki", "splunk"])
def test_vendor_native_read_only_query_bounds_and_redaction(provider, monkeypatch):
    from investigator import vendors

    calls = []

    def handler(request):
        calls.append(request)
        if provider == "datadog":
            assert request.method == "POST"
            assert request.url.path == "/api/v2/logs/events/search"
            body = json.loads(request.content)
            assert body["page"]["limit"] == 1
            assert vendors.dd_quote("live-acceptance-fixture") in body["filter"]["query"]
            return httpx.Response(200, json={"data": []})
        if provider == "loki":
            assert request.method == "GET"
            assert request.url.path == "/loki/api/v1/query_range"
            assert request.url.params["limit"] == "1"
            return httpx.Response(200, json={"status": "success", "data": {
                "resultType": "streams", "result": []}})
        assert request.method == "POST"
        assert request.url.path == "/services/search/jobs/export"
        assert b"head+1" in request.content
        return httpx.Response(200, text="")

    original_init = vendors.VendorLogSource.__init__

    def initialize(self, transport=None):
        original_init(self, httpx.MockTransport(handler))

    monkeypatch.setattr(vendors.VendorLogSource, "__init__", initialize)
    env = {"DATADOG_API_KEY": SECRET, "DATADOG_APP_KEY": SECRET,
           "LOKI_BASE_URL": "https://example.com", "LOKI_TOKEN": SECRET,
           "SPLUNK_BASE_URL": "https://example.com", "SPLUNK_TOKEN": SECRET}
    report, code = acceptance.run(env, {provider})
    assert code == 0
    assert len(calls) == 1
    assert report["providers"][provider]["status"] == "passed"
    assert SECRET not in json.dumps(report)


def test_github_only_get_user_and_optional_repo_no_response_content(monkeypatch):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url))
        return {"id": 1, "login": SECRET, "full_name": SECRET, "token": SECRET}

    monkeypatch.setattr(acceptance, "request_json", request)
    report, code = acceptance.run({"GITHUB_TOKEN": SECRET}, {"github"}, "fixture/repo")
    assert code == 0
    assert calls == [("GET", "https://api.github.com/user"),
                     ("GET", "https://api.github.com/repos/fixture/repo")]
    assert SECRET not in json.dumps(report)


def test_http_failure_sanitized_status(monkeypatch):
    monkeypatch.setattr(acceptance, "request_json", lambda *args, **kwargs: (_ for _ in ()).throw(
        acceptance.CheckFailure("http_error", 401)))
    report, code = acceptance.run({"GITHUB_TOKEN": SECRET}, {"github"})
    assert code == 1
    assert report["providers"]["github"]["http_status"] == 401


def test_unexpected_error_code_cannot_disclose_secret(monkeypatch):
    def fail(*args):
        raise acceptance.CheckFailure(SECRET)

    monkeypatch.setattr(acceptance, "probe", fail)
    report, code = acceptance.run({"GITHUB_TOKEN": SECRET}, {"github"})
    assert code == 1
    assert SECRET not in json.dumps(report)


@pytest.mark.parametrize("status", [200, 301, 401, 429, 500])
def test_transport_no_redirects_retries_or_response_disclosure(status, monkeypatch):
    original_client = httpx.Client
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"id": 1, "login": SECRET},
                              headers={"location": "https://example.com/?token=" + SECRET})

    def client(**kwargs):
        assert kwargs["verify"] is True
        assert kwargs["follow_redirects"] is False
        assert kwargs["trust_env"] is False
        assert kwargs["timeout"] == 15
        return original_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    report, code = acceptance.run({"GITHUB_TOKEN": SECRET}, {"github"})
    assert len(calls) == 1
    assert code == int(status != 200)
    if status != 200:
        assert report["providers"]["github"]["http_status"] == status
    assert SECRET not in json.dumps(report)


def test_transport_response_size_limit(monkeypatch):
    original_client = httpx.Client
    monkeypatch.setattr(acceptance, "MAX_BYTES", 16)
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text=SECRET)), **kwargs))
    report, code = acceptance.run({"GITHUB_TOKEN": SECRET}, {"github"})
    assert code == 1
    assert report["providers"]["github"]["code"] == "response_too_large"
    assert SECRET not in json.dumps(report)


def test_only_explicitly_enabled_provider_runs(monkeypatch):
    called = []

    def probe(provider, *args):
        called.append(provider)
        return {}

    monkeypatch.setattr(acceptance, "probe", probe)
    report, code = acceptance.run({**llm_env(), "GITHUB_TOKEN": SECRET}, {"github"})
    assert code == 0
    assert called == ["github"]
    assert report["providers"]["rag"]["status"] == "check_only"


def test_github_invalid_repo_never_requests(monkeypatch):
    monkeypatch.setattr(acceptance, "request_json", lambda *args, **kwargs: pytest.fail("request"))
    report, code = acceptance.run({"GITHUB_TOKEN": SECRET}, {"github"}, "../" + SECRET)
    assert code == 1
    assert SECRET not in json.dumps(report)


def test_provider_none_and_missing_model_skip(monkeypatch):
    monkeypatch.setattr(acceptance, "probe", lambda *args: pytest.fail("request"))
    report, code = acceptance.run({"LLM_PROVIDER": "openai", "LLM_API_KEY": SECRET,
                                   "EMBEDDING_PROVIDER": "none"}, {"llm", "rag", "embeddings"})
    assert code == 0
    assert all(report["providers"][name]["status"] == "skipped" for name in ("llm", "rag", "embeddings"))


def test_cli_safe_json_ignored_report_and_missing_env(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(acceptance, "ROOT", tmp_path)
    assert acceptance.main(["--check-only"], environment={}) == 0
    report = json.loads(capsys.readouterr().out)
    assert report == json.loads((tmp_path / "tmp" / "live-acceptance.json").read_text())
    assert report["mode"] == "check_only"


@pytest.mark.parametrize("args", [["--enable", SECRET], ["--report", "../" + SECRET + ".json"],
                                   ["--check-only", "--enable", "llm"], ["--unknown", SECRET]])
def test_cli_argument_errors_never_echo_user_values(args, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(acceptance, "ROOT", tmp_path)
    assert acceptance.main(args, environment={}) == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["failed"] is True
    assert SECRET not in output.out + output.err


def test_cli_failed_probe_returns_error_and_safe_report(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(acceptance, "ROOT", tmp_path)
    monkeypatch.setattr(acceptance, "probe", lambda *args: (_ for _ in ()).throw(RuntimeError(SECRET)))
    assert acceptance.main(["--enable", "github"], {"GITHUB_TOKEN": SECRET}) == 1
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert json.loads(output.out)["providers"]["github"]["status"] == "error"
    assert SECRET not in (tmp_path / "tmp" / "live-acceptance.json").read_text()


def test_malformed_dotenv_never_echoed(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(acceptance, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(SECRET, encoding="utf-8")
    assert acceptance.main([], {}) == 1
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert json.loads(output.out)["code"] == "invalid_env_file"


def test_report_paths_remain_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(acceptance, "ROOT", tmp_path)
    assert acceptance.report_path("tmp/nested/report.json") == tmp_path / "tmp/nested/report.json"
    for path in ("README.json", "tmp/../report.json", "tmp/report.txt"):
        with pytest.raises(acceptance.CheckFailure):
            acceptance.report_path(path)