import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


@unittest.skipUnless(shutil.which("powershell"), "Windows PowerShell not available")
class StartupTests(unittest.TestCase):
    def run_start(self, ready, storage="t"):
        root = Path(__file__).resolve().parents[1]
        config = json.dumps({"data": {"services.yaml":
            "services:\n- name: inventory-service\n  prod_commit: local-demo\n"}})
        ready_json = json.dumps(ready)
        command = f"""
    $ErrorActionPreference = 'Stop'
    function global:kubectl {{
    $global:LASTEXITCODE = 0
    $arguments = $args -join ' '
    if ($arguments -like 'get configmap*') {{ return '{config}' }}
    if ($arguments -like '*count(*)=5*') {{ return '{storage}' }}
    if ($arguments -like '*json_agg*') {{ return '{ready_json}' }}
    if ($arguments -like 'apply*') {{ Write-Output 'DEPLOY_CALLED'; return }}
    if ($arguments -like 'rollout*') {{ return }}
    if ($arguments -like 'exec*deployment/investigator*') {{ return 'INDEX_STATUS_CALLED' }}
    throw "Unexpected mocked kubectl call: $arguments"
}}
try {{
    & '{root / 'scripts' / 'start-investigator.ps1'}' -PythonExecutable '{sys.executable}'
}} catch {{
    Write-Output $_.Exception.Message
    exit 1
}}
"""
        return subprocess.run([shutil.which("powershell"), "-NoProfile", "-NonInteractive",
                               "-Command", command], capture_output=True, text=True, timeout=30)

    def test_missing_index_blocks_before_deployment(self):
        result = self.run_start([])
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("No ready RAG index", result.stdout)
        self.assertNotIn("DEPLOY_CALLED", result.stdout)

    def test_wrong_commit_blocks_before_deployment(self):
        result = self.run_start([{"service": "inventory-service", "commit_sha": "old-sha"}])
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("No ready RAG index", result.stdout)
        self.assertNotIn("DEPLOY_CALLED", result.stdout)

    def test_missing_storage_blocks_before_deployment(self):
        result = self.run_start([], storage="f")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("bootstrap is not ready", result.stdout)
        self.assertNotIn("DEPLOY_CALLED", result.stdout)

    def test_matching_commit_deploys_then_checks_reader(self):
        result = self.run_start([{"service": "inventory-service", "commit_sha": "local-demo"}])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("DEPLOY_CALLED", result.stdout)
        self.assertIn("INDEX_STATUS_CALLED", result.stdout)

    def test_up_does_not_deploy_or_restart_investigator(self):
        script = (Path(__file__).resolve().parents[1] / "scripts/up.ps1").read_text(encoding="utf-8")
        self.assertNotRegex(script, r"(?m)^\s*Run kubectl apply(?![^\r\n]*--dry-run)[^\r\n]*investigator")
        self.assertNotRegex(script, r"(?m)^\s*Run kubectl rollout[^\r\n]*investigator")
        self.assertNotRegex(script, r"(?m)^\s*foreach[^\r\n]*investigator")


if __name__ == "__main__":
    unittest.main()