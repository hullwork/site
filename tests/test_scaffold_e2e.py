"""Tests for the bounded scaffold E2E evaluator."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "evaluate-scaffolds.py"


def _load_evaluator():
    spec = importlib.util.spec_from_file_location("scaffold_evaluator", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load scaffold evaluator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ScaffoldE2ETests(unittest.TestCase):
    def test_default_lane_is_local_and_does_not_claim_agent_rate(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=ROOT,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        evidence = json.loads(completed.stdout)
        self.assertEqual(
            {item["id"] for item in evidence["scaffolds"]},
            {"static-html", "vite-static", "fastapi-postgresql", "express-postgresql"},
        )
        self.assertFalse(evidence["options"]["allowContainerBuilds"])
        self.assertIsNone(evidence["methodology"]["agentEndToEndSuccessRate"])
        self.assertIsNone(evidence["summary"]["scaffoldContainerSuccessRate"])
        self.assertEqual(evidence["summary"]["failed"], 0)
        self.assertGreater(evidence["summary"]["not-run"], 0)

        stages = [
            stage
            for profile in evidence["scaffolds"]
            for stage in profile["stages"]
        ]
        self.assertTrue(all(stage["status"] in {"passed", "failed", "not-run", "blocked"} for stage in stages))
        self.assertTrue(all(isinstance(stage["durationMs"], (int, float)) for stage in stages))
        self.assertTrue(all(stage["durationMs"] >= 0 for stage in stages))
        self.assertIn(
            "passed",
            {
                stage["status"]
                for stage in next(
                    profile for profile in evidence["scaffolds"] if profile["id"] == "static-html"
                )["stages"]
                if stage["stage"] == "localhost-runtime-smoke"
            },
        )

    def test_opted_in_container_lane_is_blocked_without_docker(self) -> None:
        evaluator = _load_evaluator()
        with mock.patch.object(evaluator.shutil, "which", return_value=None):
            evidence = evaluator.evaluate(allow_container_builds=True)
        self.assertGreater(evidence["summary"]["blocked"], 0)
        self.assertEqual(evidence["summary"]["failed"], 0)
        self.assertEqual(evidence["summary"]["scaffoldContainerSuccessRate"], 0.0)
        self.assertIsNone(evidence["methodology"]["agentEndToEndSuccessRate"])

    def test_output_file_contains_same_evidence_shape(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "evidence.json"
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "--output", str(output)],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=ROOT,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, "")
            evidence = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(evidence["summary"]["failed"], 0)


if __name__ == "__main__":
    unittest.main()
