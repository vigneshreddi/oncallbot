import importlib.util
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("generate_postman", ROOT / "scripts/generate-postman.py")
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)


class PostmanTests(unittest.TestCase):
    def test_every_catalog_request_is_generated_in_order(self):
        faults = generator.load_catalog(ROOT / "chaos/faults.yaml")
        collection, environment = generator.generate(faults)
        self.assertEqual(6, len(collection["item"]))
        self.assertEqual({"orderUrl", "inventoryUrl", "paymentUrl"}, {entry["key"] for entry in environment["values"]})
        for fault, folder in zip(faults, collection["item"]):
            steps = [step for phase in generator.PHASES for step in fault[phase]]
            self.assertEqual(len(steps), len(folder["item"]))
            for step, item in zip(steps, folder["item"]):
                self.assertEqual(step["method"], item["request"]["method"])
                self.assertTrue(item["request"]["url"].endswith(step["path"]))
                if "json" in step:
                    body = item["request"]["body"]["raw"].replace("{{charge_order_id}}", "-123")
                    expected = dict(step["json"])
                    if "orderId" in expected:
                        expected["orderId"] = -123
                    self.assertEqual(expected, json.loads(body))

    def test_java_routes_cover_all_catalog_paths(self):
        routes = {service: [] for service in generator.BASE_VARIABLES}
        for source in (ROOT / "services").glob("*-service/src/main/java/**/*.java"):
            if "demo-service" in source.parts:
                continue
            service = source.relative_to(ROOT / "services").parts[0]
            content = source.read_text()
            base = re.search(r'@RequestMapping\("([^"]+)"\)', content)
            if not base:
                continue
            for mapping in re.finditer(r'@(Get|Post)Mapping(?:\("([^"]+)"\))?', content):
                route = base[1] + (mapping[2] or "")
                pattern = re.escape(route)
                pattern = re.sub(r'\\\{[^}]+\\\}', '[^/]+', pattern)
                routes[service].append((mapping[1].upper(), re.compile(pattern + "$")))
        for fault in generator.load_catalog(ROOT / "chaos/faults.yaml"):
            for phase in generator.PHASES:
                for step in fault[phase]:
                    path = step["path"].split("?", 1)[0]
                    self.assertTrue(any(method == step["method"] and pattern.fullmatch(path)
                                        for method, pattern in routes[step["service"]]), path)

    def test_policy_capture_guard_and_setup_failure_routing(self):
        collection, _ = generator.generate(generator.load_catalog(ROOT / "chaos/faults.yaml"))
        status_folder = collection["item"][2]
        first_script = status_folder["item"][0]["event"]
        self.assertIn('pm.collectionVariables.unset("capture_mode");', first_script[0]["script"]["exec"])
        restore = next(item for item in status_folder["item"] if item["name"].endswith("cleanup / 1"))
        self.assertIn("skipRequest", "\n".join(restore["event"][0]["script"]["exec"]))
        self.assertIn("setNextRequest", "\n".join(first_script[-1]["script"]["exec"]))


if __name__ == "__main__":
    unittest.main()