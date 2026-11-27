"""Commit-pinned RAG retrieval; no business-schema access or schema mutation."""
import math
import re
from urllib.parse import quote, urlparse

import httpx


def query_terms(query):
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", query)
    return list(dict.fromkeys(re.findall(r"[a-z0-9]+", separated.lower())))[:32]


def lexical_score(chunk, terms):
    return sum((4 if term in chunk["symbol"].lower() else 0) +
               (2 if term in chunk["path"].lower() else 0) +
               (1 if term in chunk["content"].lower() else 0) for term in terms)


def lexical_rank(chunks, query, limit=5):
    terms = query_terms(query)
    ranked = [{**chunk, "score": lexical_score(chunk, terms)} for chunk in chunks]
    return sorted((chunk for chunk in ranked if chunk["score"] > 0),
                  key=lambda chunk: (-chunk["score"], len(chunk["content"]),
                                     chunk["path"], chunk["start_line"]))[:limit]


def source_range(text, start_line, end_line):
    lines = text.splitlines(keepends=True)
    start = max(1, int(start_line))
    end = min(len(lines), int(end_line), start + 239)
    if end < start:
        return {"error": "invalid or out-of-file line range"}
    return {"start_line": start, "end_line": end, "content": "".join(lines[start - 1:end])}


def metadata(conn, service, commit):
    if not conn.execute("select to_regclass('code_index.rag_indexes') as relation").fetchone()["relation"]:
        return None
    row = conn.execute(
        "select * from code_index.rag_indexes where service=%s and commit_sha=%s", (service, commit),
    ).fetchone()
    return dict(row) if row else None


def query_embedding(query, info, settings):
    if settings.EMBEDDING_PROVIDER != info["provider"] or settings.EMBEDDING_MODEL != info["model"]:
        raise ValueError("Configured embedding provider/model differs from the ready index")
    base = (settings.EMBEDDING_BASE_URL or "https://api.openai.com/v1").rstrip("/")
    response = httpx.post(
        base + "/embeddings", json={"model": info["model"], "input": [query]},
        headers={"Authorization": f"Bearer {settings.EMBEDDING_API_KEY}"}
        if settings.EMBEDDING_API_KEY else {}, timeout=30,
    )
    response.raise_for_status()
    data = response.json()["data"]
    if len(data) != 1 or data[0]["index"] != 0:
        raise ValueError("Invalid query embedding response")
    vector = data[0]["embedding"]
    if len(vector) != info["dimensions"] or not all(math.isfinite(value) for value in vector) or not any(vector):
        raise ValueError("Query embedding has invalid values or dimensions")
    return vector


def search_in_connection(conn, service, commit, query, settings, limit=5):
    limit = max(1, min(int(limit), 10))
    info = metadata(conn, service, commit)
    if not info:
        return {"chunks": [], "commit": commit, "mode": "missing",
                "finding": f"code index missing for commit {commit}"}
    result = {"commit": commit, "provenance": info["provenance"], "index": info}
    terms = query_terms(query)
    if not terms:
        return {**result, "chunks": [], "mode": "lexical"}
    fallback = None
    if info["dimensions"] and settings.EMBEDDING_PROVIDER not in {"", "none"}:
        try:
            vector = query_embedding(query, info, settings)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            fallback = "Query embedding unavailable or incompatible; lexical fallback"
        else:
            import json

            rows = conn.execute(
                """select c.path,c.start_line,c.end_line,c.symbol,c.kind,c.content,
                          1 - (v.embedding <=> %s::vector) as score
                   from code_index.rag_chunks c join code_index.rag_vectors v
                     on v.content_hash=c.content_hash and v.provider=%s and v.model=%s and v.dimensions=%s
                   where c.service=%s and c.commit_sha=%s
                   order by v.embedding <=> %s::vector, length(c.content),c.path,c.start_line limit %s""",
                (json.dumps(vector), info["provider"], info["model"], info["dimensions"],
                 service, commit, json.dumps(vector), limit),
            ).fetchall()
            return {**result, "chunks": [dict(row) for row in rows], "mode": "semantic"}
    rows = conn.execute(
        """select c.path,c.start_line,c.end_line,c.symbol,c.kind,c.content,rank.score
           from code_index.rag_chunks c cross join lateral (
             select sum((case when strpos(lower(c.symbol),term)>0 then 4 else 0 end) +
                        (case when strpos(lower(c.path),term)>0 then 2 else 0 end) +
                        (case when strpos(lower(c.content),term)>0 then 1 else 0 end)) as score
             from unnest(%s::text[]) as terms(term)
           ) rank where c.service=%s and c.commit_sha=%s and rank.score>0
           order by rank.score desc,length(c.content),c.path,c.start_line limit %s""",
        (terms, service, commit, limit),
    ).fetchall()
    return {**result, "chunks": [dict(row) for row in rows], "mode": "lexical",
            **({"finding": fallback} if fallback else {})}


def search_code(service, query, limit=5):
    from . import config, store

    svc = config.service_by_name(service)
    if not svc:
        return {"error": "unknown service"}
    with store.db() as conn, conn.transaction():
        conn.execute("set transaction isolation level repeatable read, read only")
        conn.execute("set local statement_timeout = 5000")
        return search_in_connection(conn, service, svc["prod_commit"], query, config, limit)


def index_status():
    from . import config, store

    result = []
    with store.db() as conn, conn.transaction():
        conn.execute("set transaction isolation level repeatable read, read only")
        for svc in config.services():
            info = metadata(conn, svc["name"], svc["prod_commit"])
            result.append({**info, "ready": True} if info else {
                "service": svc["name"], "commit_sha": svc["prod_commit"], "ready": False,
                "finding": f"code index missing for commit {svc['prod_commit']}",
            })
    return result


def source_text(service, path):
    """Full indexed file at the deployed commit, or None."""
    from . import config, store

    svc = config.service_by_name(service)
    if not svc:
        return None
    with store.db() as conn, conn.transaction():
        conn.execute("set transaction isolation level repeatable read, read only")
        if not metadata(conn, service, svc["prod_commit"]):
            return None
        row = conn.execute(
            "select content from code_index.rag_sources where service=%s and commit_sha=%s and path=%s",
            (service, svc["prod_commit"], path),
        ).fetchone()
    return row["content"] if row else None


def read_file(service, path, start_line=1, end_line=120):
    from . import config, store

    svc = config.service_by_name(service)
    if not svc:
        return {"error": "unknown service"}
    if not path or path.startswith("/") or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")):
        return {"error": "invalid repository-relative path"}
    commit = svc["prod_commit"]
    with store.db() as conn, conn.transaction():
        conn.execute("set transaction isolation level repeatable read, read only")
        info = metadata(conn, service, commit)
        row = conn.execute(
            "select content from code_index.rag_sources where service=%s and commit_sha=%s and path=%s",
            (service, commit, path),
        ).fetchone() if info else None
    if row:
        return {"path": path, "commit": commit, "source": "code_index",
                "provenance": info["provenance"], **source_range(row["content"], start_line, end_line)}
    repo = urlparse(svc.get("repo_url", ""))
    if svc.get("commit_kind") == "git" and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) and repo.hostname == "github.com":
        owner_repo = repo.path.strip("/").removesuffix(".git")
        if len(owner_repo.split("/")) == 2:
            url = f"https://raw.githubusercontent.com/{owner_repo}/{commit}/{quote(svc['repo_path'].strip('/') + '/' + path, safe='/')}"
            response = httpx.get(url, headers={"Authorization": f"Bearer {config.GITHUB_TOKEN}"}
                                if config.GITHUB_TOKEN else {}, timeout=20)
            if response.status_code == 200:
                return {"path": path, "commit": commit, "source": "github",
                        "finding": "File absent from ready code index", **source_range(response.text, start_line, end_line)}
            return {"error": f"github {response.status_code}", "commit": commit}
    return {"error": "file not in ready code index; GitHub fallback requires a real Git commit",
            "finding": f"code index missing file {path} for commit {commit}"}