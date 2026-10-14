"""Offline trusted application and disabled-default recovery boundary tests."""
import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bom_processor.author_recovery_api import RecoveryApi


def headers(app="trusted-app"):
    principal = {"claims": [{"typ": "appid", "val": app}, {"typ": "oid", "val": "service-object"}]}
    return {"X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(principal).encode()).decode()}


def adapter(enabled=True):
    config = SimpleNamespace(auth_mode="entra", allowed_app_ids=frozenset({"trusted-app"}), allowed_object_ids=frozenset({"service-object"}))
    service = Mock()
    service.recover.return_value = {"status": "started", "slot": 3, "execution": "test-execution"}
    factory = Mock(return_value=service)
    return RecoveryApi(config, factory, enabled=enabled), factory, service


def test_unauthenticated_or_human_only_requests_never_construct_service():
    api, factory, _ = adapter()
    assert api.handle({}, b"{")[0] == 401
    assert api.handle(headers("untrusted-app"), b"{")[0] == 403
    factory.assert_not_called()


def test_disabled_recovery_never_parses_or_dispatches():
    api, factory, _ = adapter(False)
    assert api.handle(headers(), b"{") == (403, {"error": {"code": "recovery_disabled"}})
    factory.assert_not_called()


def test_authenticated_request_binds_caller_and_operator_separately():
    api, _, service = adapter()
    payload = {"operator_id": "verified-admin", "recovery": {"request_id": "request-one"}}
    status, result = api.handle(headers(), json.dumps(payload).encode())
    assert status == 200 and result["slot"] == 3
    service.recover.assert_called_once_with(payload["recovery"], actor_id="verified-admin", caller_application="trusted-app")


def test_oversized_and_spoofed_envelopes_are_rejected():
    api, factory, _ = adapter()
    assert api.handle(headers(), b"x" * 8193)[0] == 413
    assert api.handle(headers(), b'{"operator_id":"admin","recovery":{},"caller_application":"fake"}')[0] == 409
    factory.assert_not_called()


def test_lookup_authentication_disabled_default_and_exact_envelope():
    api, factory, service = adapter()
    assert api.lookup({}, b"{")[0] == 401
    assert api.lookup(headers("untrusted-app"), b"{")[0] == 403
    assert api.lookup(headers(), b"x" * 8193)[0] == 413
    assert api.lookup(headers(), b"{")[0] == 400
    assert api.lookup(headers(), b'{"operator_id":"admin","recovery":{},"extra":true}')[0] == 409
    factory.assert_not_called()
    api.enabled = False
    assert api.lookup(headers(), b"{") == (403, {"error": {"code": "recovery_disabled"}})
    factory.assert_not_called()
    api.enabled = True
    payload = {"operator_id": "verified-admin", "recovery": {"request_id": "request-one"}}
    service.lookup.return_value = {"status": "hold", "reason": "budget_recovery_consumed"}
    assert api.lookup(headers(), json.dumps(payload).encode()) == (200, service.lookup.return_value)
    service.lookup.assert_called_once_with(payload["recovery"], actor_id="verified-admin", caller_application="trusted-app")
    service.recover.assert_not_called()


def test_lookup_missing_authority_maps_to_unavailable():
    from bom_processor.author_budget import BudgetError
    api, _, service = adapter()
    service.lookup.side_effect = BudgetError()
    assert api.lookup(headers(), b'{"operator_id":"admin","recovery":{}}') == (409, {"error": {"code": "recovery_unavailable"}})


def test_lookup_constructor_default_is_disabled():
    api, factory, _ = adapter()
    disabled = RecoveryApi(api.config, factory)
    assert disabled.lookup(headers(), b"{") == (403, {"error": {"code": "recovery_disabled"}})
    factory.assert_not_called()


@pytest.mark.parametrize("path", ["/api/v1/parser-author-recoveries", "/api/v1/parser-author-recoveries/lookup"])
def test_http_recovery_auth_before_body_and_bounded_reads(monkeypatch, path):
    from io import BytesIO
    from deterministic_api import make_handler
    api, factory, service = adapter()
    handler = object.__new__(make_handler(Mock(), recovery_api=api))
    handler.path = path
    handler._write = Mock()
    handler.connection = Mock()
    handler.rfile = Mock()
    handler.headers = {"Content-Length": "99999"}
    handler.do_POST()
    assert handler._write.call_args.args[0] == 401
    handler.rfile.read.assert_not_called()
    handler.headers = {**headers(), "Content-Length": "8193"}
    handler.do_POST()
    assert handler._write.call_args.args[0] == 413
    handler.rfile.read.assert_not_called()
    factory.assert_not_called()
    payload = b'{"operator_id":"admin","recovery":{}}'
    handler.headers = {**headers(), "Content-Length": str(len(payload))}
    handler.rfile = BytesIO(payload)
    service.lookup.return_value = {"status": "hold"}
    handler.do_POST()
    assert handler._write.call_args.args[0] == 200
    action = service.lookup if path.endswith("/lookup") else service.recover
    action.assert_called_once_with({}, actor_id="admin", caller_application="trusted-app")
    def timeout(*args):
        raise ValueError("short body")
    monkeypatch.setattr("bom_processor.package_evidence_api.read_evidence_body", timeout)
    handler.do_POST()
    assert handler._write.call_args.args[0] == 408 and handler.close_connection is True