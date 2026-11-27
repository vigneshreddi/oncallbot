"""Build-time code indexer. Run in CI after image build, or manually via scripts/index-code.ps1 for the demo.

Chunks Java files by class/method boundaries, hashes content, embeds via the configured provider (or skips).
Sources are copied into the indexer image at docker build time so the Job does not need hostPath.
"""
import hashlib
import base64
import os
import json
import math
import subprocess
import sys
from pathlib import Path

import httpx
from tree_sitter import Language, Parser
import tree_sitter_java

DB_DSN = os.getenv("DATABASE_URL", "postgresql://app@postgres:5432/incidents")
CONFIG_PATH = os.getenv("SERVICES_CONFIG", "/app/config/services.yaml")
SOURCE_ROOT = Path(os.getenv("SOURCE_ROOT", "/src"))
PROVIDER = os.getenv("EMBEDDING_PROVIDER", "none")
MODEL = os.getenv("EMBEDDING_MODEL", "")
BASE_URL = (os.getenv("EMBEDDING_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
API_KEY = os.getenv("EMBEDDING_API_KEY", "")

RAG_TABLES = ("rag_indexes", "rag_sources", "rag_chunks", "rag_embedding_cache", "rag_vectors")
CHUNKER_VERSION = "tree-sitter-java-0.23.5:v1"
MANIFEST_PATH = Path(os.getenv("SOURCE_MANIFEST", "/app/source-manifest.json"))


def check_storage(conn):
    missing = [table for table in RAG_TABLES
               if not conn.execute("select to_regclass(%s)", (f"code_index.{table}",)).fetchone()[0]]
    if missing:
        raise RuntimeError(f"Bootstrap must provision code_index tables before indexing: {', '.join(missing)}")
    if not conn.execute("select exists(select 1 from pg_extension where extname='vector')").fetchone()[0]:
        raise RuntimeError("Bootstrap must install pgvector before indexing")


def embed(texts: list[str]) -> list[list[float] | None]:
    if PROVIDER in {"", "none"} or not texts:
        return [None] * len(texts)
    if not MODEL:
        raise ValueError("EMBEDDING_MODEL is required when embeddings are enabled")
    r = httpx.post(
        f"{BASE_URL}/embeddings",
        json={"model": MODEL, "input": texts},
        headers={"Authorization": f"Bearer {API_KEY}"} if API_KEY else {},
        timeout=120,
    )
    r.raise_for_status()
    data = sorted(r.json()["data"], key=lambda item: item["index"])
    if [item["index"] for item in data] != list(range(len(texts))):
        raise ValueError("Embedding response has missing or duplicate indices")
    vectors = [item["embedding"] for item in data]
    dimensions = len(vectors[0])
    if not dimensions or any(len(vector) != dimensions or
                             not all(math.isfinite(value) for value in vector) or
                             not any(vector) for vector in vectors):
        raise ValueError("Embedding response has invalid vectors")
    return vectors


def chunk_java(path: Path, text: str):
    source = text.encode("utf-8")
    tree = Parser(Language(tree_sitter_java.language())).parse(source)
    if tree.root_node.has_error:
        raise ValueError(f"Java parse failed: {path}")
    lines = text.splitlines(keepends=True)
    declarations = {
        "class_declaration", "interface_declaration", "enum_declaration",
        "record_declaration", "annotation_type_declaration", "method_declaration",
        "constructor_declaration", "compact_constructor_declaration",
        "field_declaration", "static_initializer",
    }

    def visit(node, owners=()):
        name = node.child_by_field_name("name")
        symbol = source[name.start_byte:name.end_byte].decode() if name else node.type
        is_type = node.type in declarations and node.type not in {
            "method_declaration", "constructor_declaration", "compact_constructor_declaration",
            "field_declaration", "static_initializer",
        }
        if node.type in declarations:
            start = node.start_point.row + 1
            end = node.end_point.row + (1 if node.end_point.column else 0)
            yield start, end, ".".join((*owners, symbol)), node.type, "".join(lines[start - 1:end])
        for child in node.named_children:
            yield from visit(child, (*owners, symbol) if is_type else owners)

    yield from visit(tree.root_node)


def iter_source_files(root: Path):
    for file in sorted(root.rglob("*")):
        if not file.is_file():
            continue
        if any(part in {"target", ".git", "node_modules"} for part in file.parts):
            continue
        if file.suffix.lower() in {".java", ".sql", ".yml", ".yaml"}:
            if file.is_symlink() or not file.resolve().is_relative_to(root.resolve()):
                raise ValueError("Source symlinks/out-of-root files are not supported")
            yield file


def digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def source_snapshot(root: Path):
    return {file.relative_to(root).as_posix(): digest(file.read_bytes().decode("utf-8"))
            for file in iter_source_files(root)}


def verify_checkout(svc: dict, root: Path) -> tuple[str | None, str]:
    commit = svc["prod_commit"]
    if commit == "local-demo":
        if svc.get("commit_kind") != "synthetic":
            raise ValueError("local-demo must explicitly declare commit_kind: synthetic")
        return None, "synthetic-local-demo"
    if svc.get("commit_kind") != "git" or len(commit) not in {40, 64} or any(
            character not in "0123456789abcdef" for character in commit):
        raise ValueError("Production indexing requires a full Git SHA and commit_kind: git")
    baked = os.getenv("SOURCE_MANIFEST_B64", "")
    if baked or MANIFEST_PATH.is_file():
        manifest = json.loads(base64.b64decode(baked).decode("utf-8") if baked else
                              MANIFEST_PATH.read_text(encoding="utf-8"))
        expected = manifest["services"].get(svc["name"])
        if manifest.get("commit") != commit or expected != source_snapshot(root):
            raise ValueError("Baked source manifest does not match requested commit/source")
        return commit, "verified-build-manifest"

    def git(*arguments):
        return subprocess.check_output(["git", "-C", str(SOURCE_ROOT), *arguments], text=True).strip()

    if git("rev-parse", "HEAD") != commit:
        raise ValueError("Supplied commit does not match checkout HEAD")
    if git("status", "--porcelain", "--untracked-files=all", "--", svc["repo_path"]):
        raise ValueError("Service source has uncommitted changes")
    tracked = set(git("ls-files", "--", svc["repo_path"]).splitlines())
    if any((Path(svc["repo_path"]) / path).as_posix() not in tracked
           for path in source_snapshot(root)):
        raise ValueError("Source snapshot contains untracked/ignored files")
    return commit, "verified-git-checkout"


def prepare_service(svc: dict):
    root = (SOURCE_ROOT / svc["repo_path"]).resolve()
    if not root.is_relative_to(SOURCE_ROOT.resolve()) or not root.is_dir():
        raise ValueError("Service source root is missing or outside SOURCE_ROOT")
    source_commit, provenance = verify_checkout(svc, root)
    sources, chunks = [], []
    for file in iter_source_files(root):
        text = file.read_bytes().decode("utf-8")
        path = file.relative_to(root).as_posix()
        sources.append((path, text, digest(text)))
        declarations = chunk_java(file, text) if file.suffix == ".java" else [
            (1, max(1, len(text.splitlines())), file.stem, file.suffix.lstrip("."), text)
        ]
        for start, end, symbol, kind, content in declarations:
            chunks.append((path, start, end, symbol, kind, content, digest(content)))
    if not chunks:
        raise ValueError(f"No source chunks for {svc['name']}")
    if {row[0]: row[2] for row in sources} != source_snapshot(root):
        raise ValueError("Source changed while preparing the index")
    verify_checkout(svc, root)
    return source_commit, provenance, sources, chunks


def index_service(conn, svc: dict) -> int:
    source_commit, provenance, sources, chunks = prepare_service(svc)
    service, commit = svc["name"], svc["prod_commit"]
    enabled = PROVIDER not in {"", "none"}
    provider, model = (PROVIDER, MODEL) if enabled else ("none", "")
    dimensions = 0
    with conn.transaction():
        conn.execute("select pg_advisory_xact_lock(hashtext(%s))", (service + ":" + commit,))
        if enabled:
            unique = {chunk[6]: chunk[5] for chunk in chunks}
            known = conn.execute(
                "select distinct dimensions from code_index.rag_embedding_cache where provider=%s and model=%s",
                (provider, model),
            ).fetchall()
            if len(known) > 1:
                raise ValueError("Ambiguous cached dimensions; use a versioned embedding model")
            first_hash = next(iter(unique))
            first_vector = None if known else embed([unique[first_hash]])[0]
            dimensions = known[0][0] if known else len(first_vector)
            for content_hash, content in unique.items():
                cached = conn.execute(
                    "select embedding_json from code_index.rag_embedding_cache "
                    "where content_hash=%s and provider=%s and model=%s and dimensions=%s",
                    (content_hash, provider, model, dimensions),
                ).fetchone()
                vector = (json.loads(cached[0]) if cached else
                          first_vector if content_hash == first_hash and first_vector is not None else embed([content])[0])
                if len(vector) != dimensions:
                    raise ValueError("Embedding dimensions changed during indexing")
                conn.execute(
                    "insert into code_index.rag_embedding_cache values (%s,%s,%s,%s,%s) on conflict do nothing",
                    (content_hash, provider, model, dimensions, json.dumps(vector)),
                )
                conn.execute(
                    "insert into code_index.rag_vectors values (%s,%s,%s,%s,%s::vector) on conflict do nothing",
                    (content_hash, provider, model, dimensions, json.dumps(vector)),
                )
        for table in ("rag_indexes", "rag_sources", "rag_chunks"):
            conn.execute(f"delete from code_index.{table} where service=%s and commit_sha=%s", (service, commit))
        for row in sources:
            conn.execute("insert into code_index.rag_sources values (%s,%s,%s,%s,%s)", (service, commit, *row))
        for ordinal, row in enumerate(chunks):
            conn.execute("insert into code_index.rag_chunks values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                         (service, commit, ordinal, *row))
        conn.execute(
            "insert into code_index.rag_indexes "
            "(service,commit_sha,source_commit,provenance,provider,model,dimensions,chunker_version,chunk_count) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (service, commit, source_commit, provenance, provider, model, dimensions, CHUNKER_VERSION, len(chunks)),
        )
    print(f"indexed {service}@{commit}: {len(chunks)} chunks ({provenance}, ready)", flush=True)
    return len(chunks)


def main():
    import yaml

    if len(sys.argv) > 1 and sys.argv[1] == "manifest":
        services = yaml.safe_load(Path(CONFIG_PATH).read_text(encoding="utf-8"))["services"]
        snapshots = {}
        for svc in services:
            verify_checkout(svc, SOURCE_ROOT / svc["repo_path"])
            snapshots[svc["name"]] = source_snapshot(SOURCE_ROOT / svc["repo_path"])
        commit = subprocess.check_output(["git", "-C", str(SOURCE_ROOT), "rev-parse", "HEAD"], text=True).strip()
        print(json.dumps({"commit": commit, "services": snapshots}, sort_keys=True))
        return 0
    import psycopg

    services = yaml.safe_load(open(CONFIG_PATH, encoding="utf-8"))["services"]
    with psycopg.connect(DB_DSN, autocommit=True) as conn:
        check_storage(conn)
        missing = []
        for svc in services:
            count = index_service(conn, svc)
            if count == 0:
                missing.append(svc["name"])
        if missing:
            print(f"warning: no chunks for {missing}; investigator will fall back to GitHub-direct", flush=True)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
