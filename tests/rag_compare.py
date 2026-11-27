"""Offline lexical RAG versus the previous literal-substring retrieval baseline."""
import argparse
import json
import math
import time
from pathlib import Path

from indexer.index_code import chunk_java, iter_source_files
from investigator.retrieval import lexical_rank

CASES = [
    ("legacy-null", "inventory-service", "reserve null reserved quantity", "ReserveController.reserve"),
    ("retry", "order-service", "charge retry idempotency timeout", "PlaceOrderController.chargeWithRetry"),
    ("status-contract", "order-service", "PaymentStatus AUTHORIZED enum", "PlaceOrderController.PaymentStatus"),
    ("duplicate-payment", "payment-service", "charge insert payments order", "ChargeController.charge"),
]


def estimate_tokens(text):
    return math.ceil(len(text) / 4)


def compare(root, limit):
    rows = []
    for name, service, query, expected in CASES:
        chunks = []
        service_root = root / "services" / service
        full_source = []
        for file in iter_source_files(service_root):
            if file.suffix != ".java":
                continue
            text = file.read_bytes().decode("utf-8")
            full_source.append(text)
            for start, end, symbol, kind, content in chunk_java(file, text):
                chunks.append(dict(path=file.relative_to(service_root).as_posix(), start_line=start,
                                   end_line=end, symbol=symbol, kind=kind, content=content))
        metrics = {}
        for mode in ("literal_baseline", "lexical_rag"):
            started = time.perf_counter()
            if mode == "lexical_rag":
                found = lexical_rank(chunks, query, limit)
            else:
                found = sorted((chunk for chunk in chunks if any(query.lower() in chunk[key].lower()
                               for key in ("path", "symbol", "content"))), key=lambda chunk: len(chunk["content"]))[:limit]
            rank = next((position for position, chunk in enumerate(found, 1) if chunk["symbol"] == expected), None)
            context = "".join(chunk["content"] for chunk in found)
            metrics[mode] = dict(hit_at_k=rank is not None, reciprocal_rank=1 / rank if rank else 0,
                                 returned_chunks=len(found), context_utf8_bytes=len(context.encode("utf-8")),
                                 context_estimated_tokens=estimate_tokens(context),
                                 elapsed_ms=round((time.perf_counter() - started) * 1000, 3))
        full_text = "".join(full_source)
        rows.append(dict(case=name, service=service, query=query, expected_symbol=expected,
                         full_java_source_utf8_bytes=len(full_text.encode("utf-8")),
                         full_java_source_estimated_tokens=estimate_tokens(full_text), metrics=metrics))
    summary = {mode: dict(hit_rate=sum(row["metrics"][mode]["hit_at_k"] for row in rows) / len(rows),
                          mean_reciprocal_rank=sum(row["metrics"][mode]["reciprocal_rank"] for row in rows) / len(rows))
               for mode in ("literal_baseline", "lexical_rag")}
    return dict(scope="offline lexical retrieval only; not LLM/RCA or embedding quality", top_k=limit,
                token_usage="estimated source content only; ceil(Unicode characters / 4), not a model tokenizer",
                limitations="No provider calls or measured model savings. Excludes prompts, metadata, tool JSON, and output. "
                            "Overlapping chunks count each time returned; full-source reference is not a measured no-RAG run. "
                            "Empty baseline results mean retrieval misses, not token savings.",
                summary=summary, cases=rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()
    print(json.dumps(compare(args.root, max(1, min(args.top_k, 10))), indent=2))


if __name__ == "__main__":
    main()