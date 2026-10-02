"""Synthetic system BOM authority cases; no network, customer data or model."""
import pytest
import base64
import json
from email.message import Message

from bom_processor.package_system_acceptance import SystemPackageService
from bom_processor.package_resolution import PackageResolutionError, _hash
from bom_processor.package_system_acceptance import system_acceptance_identity
from test_package_resolution import request_and_store, package_workbook, plan
from deterministic_api import ApiConfig, PackageApi


def setup():
    output = package_workbook(assembly=("Board", "A"))
    request, store = request_and_store(outputs=[output, output])
    service = SystemPackageService(store)
    package = plan(service, request)
    review = service.review(package["plan_id"])
    return service, store, package, review


def accept(service, package, review, **changes):
    values = {"plan_id": package["plan_id"], "review_manifest_sha256": review["manifest_sha256"],
              "assembly_identities": [{"name": "Board", "revision": "A"}],
              "caller_application": "synthetic-quickquote"}
    values.update(changes)
    return service.accept_system(**values)


def test_system_authority_resolves_equivalent_sources_without_human_attribution():
    service, _, package, review = setup()
    accepted = accept(service, package, review)
    assert accepted["version"] == "package-system-acceptance.v1"
    assert "approver" not in accepted
    assert accept(service, package, review) == accepted
    resolved = service.resolve(plan_id=package["plan_id"], approval_id=accepted["approval_id"])
    assert resolved["status"] == "completed"
    assert len(resolved["results"]) == 1


@pytest.mark.parametrize("changes", [
    {"review_manifest_sha256": "f" * 64},
    {"assembly_identities": [{"name": "Other", "revision": "A"}]},
    {"assembly_identities": [{"name": "Board", "revision": "B"}]},
    {"assembly_identities": [{"name": "Board", "revision": "A"}] * 2},
    {"caller_application": ""},
])
def test_system_acceptance_rejects_stale_or_ambiguous_binding(changes):
    service, _, package, review = setup()
    with pytest.raises(PackageResolutionError):
        accept(service, package, review, **changes)


def test_material_conflict_cannot_be_selected_by_system():
    request, store = request_and_store(outputs=[package_workbook(quantity=2, assembly=("Board", "A")),
                                               package_workbook(quantity=3, assembly=("Board", "A"))])
    service = SystemPackageService(store)
    package = plan(service, request)
    with pytest.raises(PackageResolutionError):
        service.accept_system(plan_id=package["plan_id"], review_manifest_sha256="a" * 64,
                              assembly_identities=[{"name": "Board", "revision": "A"}],
                              caller_application="synthetic-quickquote")


def test_system_status_works_before_and_after_resolution():
    service, _, package, review = setup()
    record = accept(service, package, review)
    assert service.resolution_status(package["plan_id"], record["approval_id"])["status"] == "approved"
    service.resolve(plan_id=package["plan_id"], approval_id=record["approval_id"])
    assert service.resolution_status(package["plan_id"], record["approval_id"])["status"] == "resolved"


@pytest.mark.parametrize("timestamp", [None, "bad", "2026-10-01T12:00:00"])
def test_timestamp_corruption_rejected_on_replay_and_binding(timestamp):
    service, store, package, review = setup()
    record = accept(service, package, review)
    record["approved_time"] = timestamp
    store.registry[service._approval_path(package["plan_id"], record["approval_id"])] = json.dumps(record).encode()
    with pytest.raises(PackageResolutionError, match="SYSTEM_ACCEPTANCE_TIME_INVALID"):
        accept(service, package, review)
    with pytest.raises(PackageResolutionError, match="SYSTEM_ACCEPTANCE_TIME_INVALID"):
        service.resolve(plan_id=package["plan_id"], approval_id=record["approval_id"])


def test_child_source_substitution_after_plan_rejected():
    service, store, package, review = setup()
    path = "candidates/requests/request-1.json"
    child = json.loads(store.registry[path])
    child["source_evidence"][0]["sha256"] = "f" * 64
    store.registry[path] = json.dumps(child).encode()
    with pytest.raises(PackageResolutionError, match="SYSTEM_CHILD_SOURCE_BINDING_INVALID"):
        accept(service, package, review)


def test_hash_consistent_system_record_cannot_inherit_human_choices():
    service, store, package, review = setup()
    record = accept(service, package, review)
    record["review"]["assembly_mappings"] = {"forged": {"name": "board", "revision": "a"}}
    record["approval_id"] = _hash(system_acceptance_identity(record))
    store.registry[service._approval_path(package["plan_id"], record["approval_id"])] = json.dumps(record).encode()
    with pytest.raises(PackageResolutionError):
        service.resolve(plan_id=package["plan_id"], approval_id=record["approval_id"])


def test_status_does_not_attempt_publication():
    service, store, package, review = setup()
    record = accept(service, package, review)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("read attempted publication")
    store.upload_registry_immutable = forbidden
    assert service.resolution_status(package["plan_id"], record["approval_id"])["status"] == "approved"


def api_context():
    config = ApiConfig(host="127.0.0.1", port=8088, auth_mode="easy_auth",
                       allowed_app_ids=frozenset({"quickquote-app"}),
                       allowed_object_ids=frozenset({"human-object"}),
                       max_body_bytes=65536, max_concurrent_jobs=1, processor_options={})
    service, store, package, review = setup()
    headers = Message()
    headers["X-MS-CLIENT-PRINCIPAL"] = base64.b64encode(json.dumps({"claims": [
        {"typ": "appid", "val": "quickquote-app"},
    ]}).encode()).decode()
    body = json.dumps({"review_manifest_sha256": review["manifest_sha256"],
                       "assembly_identities": [{"name": "Board", "revision": "A"}]}).encode()
    return PackageApi(config, lambda: service), headers, body, package, store


def test_api_requires_explicit_enablement_and_application_not_human(monkeypatch):
    api, headers, body, package, _ = api_context()
    monkeypatch.delenv("PACKAGE_SYSTEM_ACCEPTANCE_ENABLED", raising=False)
    assert api.handle_post(headers, package["plan_id"], "accept-system", body)[0] == 403
    monkeypatch.setenv("PACKAGE_SYSTEM_ACCEPTANCE_ENABLED", "true")
    headers.replace_header("X-MS-CLIENT-PRINCIPAL", base64.b64encode(json.dumps({"claims": [
        {"typ": "oid", "val": "human-object"},
    ]}).encode()).decode())
    assert api.handle_post(headers, package["plan_id"], "accept-system", body)[0] == 403


def test_api_binds_system_caller_and_resolves_separate_record(monkeypatch):
    monkeypatch.setenv("PACKAGE_SYSTEM_ACCEPTANCE_ENABLED", "true")
    api, headers, body, package, _ = api_context()
    status, record = api.handle_post(headers, package["plan_id"], "accept-system", body)
    assert status == 200
    assert record["caller_application"] == "quickquote-app"
    assert "approver" not in record
    # Existing resolve requires object identity; system caller route cannot
    # fabricate one. Resolution is tested through the system service separately.
    assert api.handle_post(headers, package["plan_id"], "resolve", b"{}", record["approval_id"])[0] == 403
    status, resolved = api.handle_post(headers, package["plan_id"], "resolve-system", b"{}", record["approval_id"])
    assert status == 200 and resolved["status"] == "completed"


def test_api_rejects_caller_supplied_authority_fields(monkeypatch):
    monkeypatch.setenv("PACKAGE_SYSTEM_ACCEPTANCE_ENABLED", "true")
    api, headers, body, package, _ = api_context()
    value = json.loads(body)
    value["caller_application"] = "forged"
    assert api.handle_post(headers, package["plan_id"], "accept-system", json.dumps(value).encode())[0] == 422


@pytest.mark.parametrize("enabled", ["true", "false"])
def test_legacy_resolve_cannot_bypass_system_application_binding(monkeypatch, enabled):
    monkeypatch.setenv("PACKAGE_SYSTEM_ACCEPTANCE_ENABLED", "true")
    api, headers, body, package, _ = api_context()
    status, record = api.handle_post(headers, package["plan_id"], "accept-system", body)
    assert status == 200
    headers.replace_header("X-MS-CLIENT-PRINCIPAL", base64.b64encode(json.dumps({"claims": [
        {"typ": "oid", "val": "human-object"}, {"typ": "appid", "val": "different-app"},
    ]}).encode()).decode())
    monkeypatch.setenv("PACKAGE_SYSTEM_ACCEPTANCE_ENABLED", enabled)
    assert api.handle_post(headers, package["plan_id"], "resolve", b"{}", record["approval_id"])[0] == 403
    assert api.handle_post(headers, package["plan_id"], "resolve-system", b"{}", record["approval_id"])[0] == 403