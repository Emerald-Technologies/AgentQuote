"""Operator-requested retry of a FAILED evidence request (no proposal to build on).

Input: server-built retry binding {retryOfRequestId, attempt}. Output: detached value.
A retry carries no feedback and changes nothing the model sees; it only gives the
same package inputs a new request identity, because a failed request is immutable.
The job service additionally proves the referenced request exists, failed, and has
identical package inputs. Run: python -m pytest tests/test_package_evidence_retry.py.
"""
from __future__ import annotations

from typing import Any

from .package_evidence_contracts import hash_value, integer, strict

# Shares the attempt ceiling with qualification-driven successors (QuickQuote enforces
# one attempt number per approval across both kinds).
MAX_ATTEMPT = 5
V4 = "package-evidence-request.v4"
# Fields that must be identical between a failed request and its retry. Policy may
# differ (a retry under a fixed adapter is the point); context/sources may not.
BOUND_FIELDS = ("context", "roots", "workbooks", "retainedBomSources")


def validate_retry(value: Any) -> dict:
    strict(value, {"retryOfRequestId", "attempt"}, "INVALID_RETRY")
    hash_value(value["retryOfRequestId"])
    integer(value["attempt"], 2, MAX_ATTEMPT)
    return {"retryOfRequestId": value["retryOfRequestId"], "attempt": value["attempt"]}


def request_attempt(request: dict) -> int:
    """Attempt number recorded by a request: retry, successor, or initial (1)."""
    if "retry" in request:
        return request["retry"]["attempt"]
    if "successor" in request:
        return request["successor"]["attempt"]
    return 1
