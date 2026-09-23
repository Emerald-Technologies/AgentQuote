import hashlib
import json
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from bom_processor.job_orchestrator import JobOrchestrator, SasUrls, StartResult, WorkItem


def item():
    source, parser = b"source", b"parser"
    return WorkItem(
        "job-1", "boms", "parsers", "boms",
        "customer/rfq-id/package-1/original-boms/source.xlsx", "Customer/parse.py",
        hashlib.sha256(source).hexdigest(), len(source), hashlib.sha256(parser).hexdigest(), len(parser),
        "customer/rfq-id/package-1/processed/job-1",
    )


def test_validation_and_deterministic_paths():
    a = item()
    assert a.output_blob == "customer/rfq-id/package-1/processed/job-1/output.json"
    with pytest.raises(ValueError):
        WorkItem("../bad", "boms", "parsers", "boms", "../source", "Customer/parse.py", "0" * 64, 1, "0" * 64, 1, "processed/job-1")


def test_orchestrator_checks_evidence_before_sas_and_returns_no_urls():
    reader = Mock()
    reader.read.side_effect = [b"source", b"parser"]
    expiry = datetime.now(timezone.utc)
    sas = Mock(create=Mock(return_value=SasUrls("https://x/a?sig=secret", "https://x/b?sig=secret", "https://x/c?sig=secret", "https://x/d?sig=secret", expiry)))
    starter = Mock(start=Mock(return_value="exec-1"))
    result = JobOrchestrator(reader, sas, starter).start(item())
    assert result == StartResult("job-1", "exec-1", expiry)
    assert "secret" not in repr(result)
    sas.create.assert_called_once()
    starter.start.assert_called_once()


def test_bad_evidence_never_creates_sas():
    reader = Mock(); reader.read.return_value = b"wrong"
    sas = Mock()
    with pytest.raises(ValueError):
        JobOrchestrator(reader, sas, Mock()).start(item())
    sas.create.assert_not_called()
