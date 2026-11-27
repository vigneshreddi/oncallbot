"""Generate Postman artifacts from the HTTP fault catalog; --check detects drift."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "chaos/runner"))
from chaos_runner import PHASES, load_catalog

BASE_VARIABLES = {
    "order-service": "orderUrl",
    "inventory-service": "inventoryUrl",
    "payment-service": "paymentUrl",
}


def load_catalogs(paths):
    faults = [fault for path in paths for fault in load_catalog(path)]
    seen = set()
    for fault in faults:
        if fault["id"] in seen:
            raise ValueError(f"Duplicate fault ID across catalogs: {fault['id']}")
        seen.add(fault["id"])
    return faults


def generate(faults):
    collection = {
        "info": {
            "name": "Incident Lab Scenario Workflows",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
            "description": "Generated from chaos/faults.yaml. Run one scenario folder at a time. "
                           "Setup failures jump to cleanup; cleanup HTTP failures retry three times. "
                           "If a run is stopped or the client crashes, execute the remaining cleanup requests manually. "
                           "Do not run concurrent generators: capture-mode is shared service policy. "
                           "Business orders and retry payments are retained as incident evidence. "
                           "health-down probes a missing SKU, not a readiness switch; schema-drift is read-only.",
        },
        "item": [],
    }
    for fault in faults:
        folder = {"name": fault["id"], "item": []}
        first_cleanup = f"{fault['id']} / cleanup / 1"
        saved_names = [name for step in fault["prepare"] for name in step.get("save", {})]
        for phase in PHASES:
            for index, step in enumerate(fault[phase], 1):
                name = f"{fault['id']} / {phase} / {index}"
                pre = []
                if phase == "prepare" and index == 1:
                    pre.extend(f"pm.collectionVariables.unset({json.dumps(saved)});" for saved in saved_names)
                    pre.append('pm.collectionVariables.set("charge_order_id", -(Date.now() * 1000 + Math.floor(Math.random() * 1000)));')
                if step.get("requires"):
                    pre.append(f"if (!pm.collectionVariables.has({json.dumps(step['requires'])})) pm.execution.skipRequest();")
                statuses = step.get("expected_status", list(range(200, 300))) if phase == "provoke" else list(range(200, 300))
                tests = [f"const ok = !!pm.response && {json.dumps(statuses)}.includes(pm.response.code);"]
                tests.append('pm.test("Expected HTTP status", () => pm.expect(ok).to.be.true);')
                if phase == "prepare":
                    tests.append(f"if (!ok) pm.execution.setNextRequest({json.dumps(first_cleanup)});")
                    if step.get("save"):
                        tests.append("if (ok) {")
                        tests.append("  try {")
                        tests.append("    const payload = pm.response.json();")
                        for saved, field in step["save"].items():
                            tests.append(f"    if (typeof payload[{json.dumps(field)}] !== 'string') throw new Error('Missing policy value');")
                            tests.append(f"    pm.collectionVariables.set({json.dumps(saved)}, encodeURIComponent(payload[{json.dumps(field)}]));")
                        tests.extend(["  } catch (error) {", f"    pm.execution.setNextRequest({json.dumps(first_cleanup)});", "    throw error;", "  }", "}"])
                if phase == "cleanup":
                    retry_key = json.dumps(name + " retries")
                    tests.extend([
                        f"const retries = Number(pm.variables.get({retry_key}) || 0);",
                        "if (!ok && retries < 2) {",
                        f"  pm.variables.set({retry_key}, retries + 1);",
                        f"  pm.execution.setNextRequest({json.dumps(name)});",
                        "} else {",
                        f"  pm.variables.unset({retry_key});",
                        "}",
                    ])
                if fault["id"] == "slow-endpoint" and phase == "provoke":
                    tests.append('pm.test("Charge latency is at least 3 seconds", () => pm.expect(pm.response.responseTime).to.be.at.least(3000));')
                request = {
                    "method": step["method"],
                    "header": [],
                    "url": "{{" + BASE_VARIABLES[step["service"]] + "}}" + step["path"],
                }
                if "json" in step:
                    raw = json.dumps(step["json"], indent=2).replace('"{{charge_order_id}}"', '{{charge_order_id}}')
                    request["header"].append({"key": "Content-Type", "value": "application/json"})
                    request["body"] = {"mode": "raw", "raw": raw, "options": {"raw": {"language": "json"}}}
                item = {"name": name, "request": request, "event": [{"listen": "test", "script": {"type": "text/javascript", "exec": tests}}]}
                if pre:
                    item["event"].insert(0, {"listen": "prerequest", "script": {"type": "text/javascript", "exec": pre}})
                folder["item"].append(item)
        collection["item"].append(folder)
    environment = {
        "id": "incident-lab-local",
        "name": "Incident Lab Local",
        "values": [
            {"key": "orderUrl", "value": "http://localhost:18080", "enabled": True},
            {"key": "inventoryUrl", "value": "http://localhost:18081", "enabled": True},
            {"key": "paymentUrl", "value": "http://localhost:18082", "enabled": True},
        ],
        "_postman_variable_scope": "environment",
    }
    return collection, environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--catalog", type=Path, default=ROOT / "chaos/faults.yaml")
    parser.add_argument("--extra-catalog", type=Path, action="append", default=[])
    parser.add_argument("--output-dir", type=Path, default=ROOT / "api/postman")
    args = parser.parse_args()
    artifacts = generate(load_catalogs([args.catalog, *args.extra_catalog]))
    if not args.check:
        args.output_dir.mkdir(parents=True, exist_ok=True)
    for filename, artifact in zip(("collection.json", "environment.json"), artifacts):
        path = args.output_dir / filename
        content = json.dumps(artifact, indent=2) + "\n"
        if args.check:
            if path.read_text(encoding="utf-8") != content:
                raise SystemExit(f"Out of date: {path}")
        else:
            path.write_text(content, encoding="utf-8")
    print("Postman artifacts match catalog" if args.check else "Postman artifacts generated")


if __name__ == "__main__":
    main()