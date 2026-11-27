import importlib.util
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("inventory_postman", ROOT / "scripts/generate-postman.py")
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)


class InventoryPostmanTests(unittest.TestCase):
    def test_separate_jpa_artifacts_match_catalog(self):
        artifacts = generator.generate(generator.load_catalogs([ROOT / "chaos/inventory-faults.yaml"]))
        for filename, artifact in zip(("inventory-jpa.collection.json", "inventory-jpa.environment.json"), artifacts):
            with self.subTest(filename=filename):
                self.assertEqual(json.dumps(artifact, indent=2) + "\n",
                                 (ROOT / "api/postman" / filename).read_text(encoding="utf-8"))

    def test_fragment_merges_without_changing_base_workflows(self):
        base = generator.load_catalog(ROOT / "chaos/faults.yaml")
        merged = generator.load_catalogs([ROOT / "chaos/faults.yaml", ROOT / "chaos/inventory-faults.yaml"])
        self.assertEqual(base, merged[:len(base)])
        self.assertEqual(["jpa-lazy", "jpa-optimistic-lock"], [fault["id"] for fault in merged[len(base):]])
        collection, _ = generator.generate(merged)
        for fault, folder in zip(merged[len(base):], collection["item"][len(base):]):
            self.assertFalse(fault["chaos"])
            self.assertEqual(0, fault["duration_seconds"])
            self.assertEqual(3, len(folder["item"]))
            for phase, item in zip(generator.PHASES, folder["item"]):
                self.assertEqual("POST", item["request"]["method"])
                self.assertEqual("{{inventoryUrl}}" + fault[phase][0]["path"], item["request"]["url"])
            self.assertIn("[500]", "\n".join(folder["item"][1]["event"][-1]["script"]["exec"]))
            self.assertIn("setNextRequest", "\n".join(folder["item"][0]["event"][-1]["script"]["exec"]))

    def test_duplicate_fragment_ids_are_rejected(self):
        fragment = ROOT / "chaos/inventory-faults.yaml"
        with self.assertRaisesRegex(ValueError, "Duplicate fault ID across catalogs"):
            generator.load_catalogs([fragment, fragment])

    def test_fragment_paths_match_real_controller_routes(self):
        controller = (ROOT / "services/inventory-service/src/main/java/dev/incidentlab/inventory/JpaFaultController.java").read_text()
        base = re.search(r'@RequestMapping\("([^"]+)"\)', controller)[1]
        routes = {base + match for match in re.findall(r'@PostMapping\("([^"]+)"\)', controller)}
        for fault in generator.load_catalog(ROOT / "chaos/inventory-faults.yaml"):
            for phase in generator.PHASES:
                self.assertIn(fault[phase][0]["path"], routes)


if __name__ == "__main__":
    unittest.main()