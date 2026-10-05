"""Qualification role/identity checks; no image build or network in unit tests."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

path = Path(__file__).resolve().parents[1] / "dev/qualify_recovery_images.py"
spec = importlib.util.spec_from_file_location("qualify_images", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_uses_retained_digests_and_role_specific_scripts(monkeypatch):
    calls = []
    def output(command, **kwargs):
        if command[0] == "git":
            return b"# committed synthetic fixture"
        tag = command[3]
        return next(digest for image, digest in module.IMAGES.values() if image == tag) + "\n"
    def run(command, **kwargs):
        calls.append((command, kwargs["input"]))
        return SimpleNamespace(returncode=0, stdout="PASS", stderr="")
    monkeypatch.setattr(module.subprocess, "check_output", output)
    monkeypatch.setattr(module.subprocess, "run", run)
    module.main()
    assert len(calls) == 3
    for role, (_, digest) in module.IMAGES.items():
        command, script = next(item for item in calls if "ROLE=" + repr(role) in item[1])
        assert command[-2] == digest and "--network" in command and "none" in command
        assert "if ROLE == 'api':" in script


def test_identity_drift_stops_before_run(monkeypatch):
    monkeypatch.setattr(module.subprocess, "check_output", lambda command, **kwargs: b"fixture" if command[0] == "git" else "wrong")
    monkeypatch.setattr(module.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("drifted image executed"))
    with pytest.raises(RuntimeError, match="RETAINED_IMAGE_MISMATCH"):
        module.main()