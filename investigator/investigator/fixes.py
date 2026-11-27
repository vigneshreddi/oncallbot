"""Draft PRs from reviewer-submitted or AI-proposed patches; the model can only propose, never write to GitHub."""
import base64
import os
import re
import uuid
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import config


SHA = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"https://github\.com/([A-Za-z0-9_-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?")
SECRET = re.compile(
    r"-----BEGIN .*PRIVATE KEY-----|(?:gh[pousr]_|github_pat_|sk-)[A-Za-z0-9_]{16,}|"
    r"://[^\s/:]+:[^\s/@]+@|(?:password|api[_-]?key|token|secret)\s*[:=]\s*[\"']?[^\s\"'${}]{4,}",
    re.IGNORECASE,
)


class FixError(Exception):
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail
        super().__init__(detail)


class PatchFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=100_000)
    expected_blob_sha: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")


class FixRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviewer_confirmed: Literal[True]
    analysis_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    target_branch: str = Field(min_length=1, max_length=200)
    files: list[PatchFile] = Field(min_length=1, max_length=20)

    @field_validator("reviewer_confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit reviewer confirmation is required")
        return value


def valid_path(path: str) -> bool:
    return isinstance(path, str) and bool(re.fullmatch(r"[A-Za-z0-9_./-]+", path)) and all(
        part and not part.startswith(".") and part not in {"target", "node_modules"}
        for part in path.split("/")
    )


def valid_branch(branch: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9_/-]+(?:\.[A-Za-z0-9_-]+)*", branch)) and all(
        part and not part.startswith(".") and not part.endswith(".lock")
        for part in branch.split("/")
    ) and ".." not in branch


def require_sha(value) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise FixError(409, "GitHub returned an invalid commit or object SHA")
    return value


def ensure_enabled() -> str:
    if os.getenv("FIX_PR_ENABLED", "").lower() != "true":
        raise FixError(403, "Draft PR generation is disabled")
    token = os.getenv("GITHUB_WRITE_TOKEN", "")
    if not token or token == config.GITHUB_TOKEN:
        raise FixError(403, "A separate GitHub write token is required")
    return token


def propose_fix(incident_id: int, service: str, request: FixRequest, origin: str = "reviewer") -> dict:
    token = ensure_enabled()
    entry = config.service_by_name(service)
    if not entry:
        raise FixError(422, "Incident service is not configured")
    repo_url = entry.get("repo_url")
    repository = REPOSITORY.fullmatch(repo_url) if isinstance(repo_url, str) else None
    if not repository or repository.group(2) in {".", ".."}:
        raise FixError(422, "Service must allowlist an exact HTTPS GitHub repository URL")
    deployed = entry.get("prod_commit", "")
    if not isinstance(deployed, str) or not SHA.fullmatch(deployed) or entry.get("commit_kind") == "synthetic":
        raise FixError(409, "A real deployed commit SHA is required; local-demo is not supported")
    if request.analysis_sha != deployed:
        raise FixError(409, "Analysis SHA does not match the configured deployed SHA")
    if not valid_branch(request.target_branch) or request.target_branch != entry.get("pr_target_branch"):
        raise FixError(422, "Target branch must match the configured maintained pr_target_branch")
    root = entry.get("repo_path", "")
    if not valid_path(root):
        raise FixError(422, "Service repository path is invalid")
    paths = set()
    for replacement in request.files:
        if not valid_path(replacement.path) or not replacement.path.startswith(root + "/"):
            raise FixError(422, "Patch paths must stay inside the configured service path")
        if replacement.path in paths:
            raise FixError(422, "Duplicate patch path")
        paths.add(replacement.path)
        if SECRET.search(replacement.content):
            raise FixError(422, "Patch contains possible credentials")
    if sum(len(replacement.content.encode("utf-8")) for replacement in request.files) > 200_000:
        raise FixError(422, "Patch exceeds the byte limit")
    evidence_base = os.getenv("FIX_EVIDENCE_BASE_URL", "").rstrip("/")
    if not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?(?:/[A-Za-z0-9_/-]+)?", evidence_base):
        raise FixError(422, "A sanitized HTTPS evidence base URL is required")
    evidence = urlsplit(evidence_base)
    if (evidence.scheme != "https" or not evidence.hostname or evidence.username or evidence.password
            or evidence.query or evidence.fragment):
        raise FixError(422, "A sanitized HTTPS evidence base URL is required")
    owner, repo = repository.groups()
    repo_api = f"https://api.github.com/repos/{owner}/{repo}"
    branch = f"incident-fix/{incident_id}-{uuid.uuid4().hex}"

    def call(client, method, path, payload=None):
        response = client.request(method, repo_api + path, json=payload)
        if not response.is_success:
            raise FixError(502, "GitHub request failed; inspect the repository for partial draft artifacts before retrying")
        result = response.json()
        if not isinstance(result, dict):
            raise FixError(502, "Unexpected GitHub response")
        return result

    try:
        with httpx.Client(headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2022-11-28"}, timeout=30, follow_redirects=False,
                          trust_env=False) as client:
            target_ref = "/git/ref/heads/" + quote(request.target_branch, safe="")
            reference = call(client, "GET", target_ref)
            base_sha = require_sha(reference["object"]["sha"])
            if reference.get("ref") != "refs/heads/" + request.target_branch:
                raise FixError(409, "Target must be an exact branch ref")
            actual_deployed = require_sha(call(client, "GET", f"/git/commits/{deployed}")["sha"])
            comparison = call(client, "GET", f"/compare/{deployed}...{base_sha}")
            if (actual_deployed != deployed or comparison.get("status") not in {"ahead", "identical"}
                    or (comparison.get("status") == "identical") != (base_sha == deployed)
                    or comparison.get("merge_base_commit", {}).get("sha") != deployed):
                raise FixError(409, "Deployed SHA must be an ancestor of the maintained branch")
            base_commit = call(client, "GET", f"/git/commits/{base_sha}")
            if require_sha(base_commit["sha"]) != base_sha:
                raise FixError(409, "Maintained branch commit mismatch")
            base_tree = require_sha(base_commit["tree"]["sha"])
            tree = call(client, "GET", f"/git/trees/{base_tree}?recursive=1")
            if tree.get("truncated") is not False:
                raise FixError(409, "Cannot safely validate a truncated repository tree")
            entries = {item["path"]: item for item in tree["tree"]}
            for replacement in request.files:
                existing = entries.get(replacement.path)
                for index in range(1, len(replacement.path.split("/"))):
                    ancestor = entries.get("/".join(replacement.path.split("/")[:index]))
                    if ancestor and ancestor.get("type") != "tree":
                        raise FixError(422, "Patch traverses a symlink or non-directory")
                if existing and (existing.get("type") != "blob" or existing.get("mode") not in {"100644", "100755"}):
                    raise FixError(422, "Only regular files can be replaced")
                if (existing or {}).get("sha") != replacement.expected_blob_sha:
                    raise FixError(409, "Expected file blob SHA does not match the maintained branch")
            if require_sha(call(client, "GET", target_ref)["object"]["sha"]) != base_sha:
                raise FixError(409, "Maintained branch moved; review the patch again")
            call(client, "POST", "/git/refs", {"ref": "refs/heads/" + branch, "sha": base_sha})
            changes = []
            for replacement in request.files:
                blob_sha = require_sha(call(client, "POST", "/git/blobs", {
                    "content": replacement.content, "encoding": "utf-8"})["sha"])
                changes.append({"path": replacement.path, "mode": entries.get(replacement.path, {}).get("mode", "100644"),
                                "type": "blob", "sha": blob_sha})
            tree_sha = require_sha(call(client, "POST", "/git/trees", {"base_tree": base_tree, "tree": changes})["sha"])
            ai = origin == "ai"
            commit_sha = require_sha(call(client, "POST", "/git/commits", {
                "message": f"{'AI-proposed' if ai else 'Propose reviewed'} fix for incident {incident_id}",
                "tree": tree_sha, "parents": [base_sha]})["sha"])
            call(client, "PATCH", "/git/refs/heads/" + quote(branch, safe=""), {"sha": commit_sha, "force": False})
            intro = (f"Patch written by the AI investigator for incident {incident_id} and opened automatically. "
                     "No person has reviewed it yet." if ai else f"Reviewer-confirmed patch for incident {incident_id}.")
            body = (f"{intro}\n\n"
                    f"Analysis deployed SHA: `{deployed}`\nPR parent SHA: `{base_sha}`\n\n"
                    f"[Incident evidence]({evidence_base}/incidents/{incident_id}/report)\n\n"
                    "Draft only. No automated merge, deployment, or production execution. "
                    "Patch contents require human review and normal CI; evidence values are not copied here.")
            pull = call(client, "POST", "/pulls", {"title": f"{'AI-proposed' if ai else 'Proposed'} fix for incident {incident_id} ({service})",
                        "head": branch, "base": request.target_branch, "body": body, "draft": True})
            number = pull["number"]
            if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                raise FixError(502, "Unexpected GitHub pull request number")
            return {"number": number, "url": f"https://github.com/{owner}/{repo}/pull/{number}",
                    "branch": branch, "draft": True, "analysis_sha": deployed, "parent_sha": base_sha}
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as from_error:
        raise FixError(502, "GitHub operation failed; partial draft artifacts may exist") from from_error


def pr_mode() -> str:
    return "approve" if os.getenv("FIX_PR_MODE", "auto").strip().lower() == "approve" else "auto"


def open_ai_pull_request(incident_id: int, service: str, proposals: list[dict]) -> dict:
    """Apply stored AI edits for one service to the current target-branch files and open one draft PR."""
    token = ensure_enabled()
    entry = config.service_by_name(service)
    if not entry:
        raise FixError(422, "Proposal service is not configured")
    repository = REPOSITORY.fullmatch(entry.get("repo_url") or "")
    target = entry.get("pr_target_branch")
    root = (entry.get("repo_path") or "").strip("/")
    if not repository or not target or not valid_branch(target) or not valid_path(root):
        raise FixError(422, "Service repository, path and pr_target_branch must be configured")
    if not SHA.fullmatch(entry.get("prod_commit") or "") or entry.get("commit_kind") == "synthetic":
        raise FixError(409, "A real deployed commit SHA is required; local-demo is not supported")
    owner, repo = repository.groups()
    files = []
    try:
        with httpx.Client(headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2022-11-28"}, timeout=30, follow_redirects=False,
                          trust_env=False) as client:
            for proposal in proposals:
                path = f"{root}/{proposal['path']}"
                if not valid_path(path):
                    raise FixError(422, "Proposal path is invalid")
                response = client.get(f"https://api.github.com/repos/{owner}/{repo}/contents/{quote(path, safe='/')}",
                                      params={"ref": target})
                if not response.is_success:
                    raise FixError(502, "Cannot read the file from the target branch")
                data = response.json()
                if data.get("type") != "file" or data.get("encoding") != "base64":
                    raise FixError(422, "Only regular files can be patched")
                text = base64.b64decode(data["content"]).decode("utf-8")
                crlf = "\r\n" in text
                for edit in proposal["edits"]:
                    old, new = edit["old_text"], edit["new_text"]
                    if crlf:
                        old, new = old.replace("\n", "\r\n"), new.replace("\n", "\r\n")
                    if text.count(old) != 1:
                        raise FixError(409, f"AI edit no longer matches {path} on {target}; investigate again")
                    text = text.replace(old, new)
                files.append(PatchFile(path=path, content=text, expected_blob_sha=require_sha(data.get("sha"))))
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as from_error:
        raise FixError(502, "Reading the target branch failed") from from_error
    request = FixRequest(reviewer_confirmed=True, analysis_sha=entry.get("prod_commit", ""), target_branch=target, files=files)
    return propose_fix(incident_id, service, request, origin="ai")