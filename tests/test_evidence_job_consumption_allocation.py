"""Guard the actual evidence-starter template against invalid Consumption pairs.

Inputs: parsed API source; output: regression assertions, no services/models.
Run python -m pytest tests/test_evidence_job_consumption_allocation.py.
"""
import ast
from pathlib import Path


def test_actual_evidence_starter_uses_valid_4gib_consumption_allocation():
    module = ast.parse((Path(__file__).resolve().parents[1] / "deterministic_api.py").read_text(encoding="utf-8"))
    service = next(node for node in ast.walk(module)
                   if isinstance(node, ast.FunctionDef) and node.name == "evidence_service")
    starters = [node for node in ast.walk(service) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "AzureCandidateJobStarter"]
    assert len(starters) == 1
    keywords = {item.arg: item.value for item in starters[0].keywords}
    resources = ast.literal_eval(keywords["resources"])
    assert resources == {"cpu": 2, "memory": "4Gi"}
    assert float(resources["memory"].removesuffix("Gi")) == resources["cpu"] * 2