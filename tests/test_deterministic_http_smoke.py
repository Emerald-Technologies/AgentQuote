import json
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from deterministic_api import ApiConfig, ProcessingApi, ROUTE, make_handler


class Processor:
    def process(self, request):
        return {"status": "completed", "job_id": request.job_id}


@pytest.fixture
def server_url():
    config = ApiConfig(
        host="127.0.0.1", port=0, auth_mode="local_disabled",
        allowed_app_ids=frozenset(), allowed_object_ids=frozenset(),
        max_body_bytes=4096, max_concurrent_jobs=1, processor_options={},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(ProcessingApi(config, Processor)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def valid_body():
    return json.dumps({
        "job_id": "job1", "customer_key": "cust", "parser_key": "Astronics",
        "rfq_uuid": "12345678-1234-4234-8234-123456789012",
        "package_version": "v1", "documents": [{
            "document_id": "doc1",
            "blob_path": "cust/rfq-12345678-1234-4234-8234-123456789012/package-v1/original-boms/doc1-input.xlsx",
            "size_bytes": 1, "sha256": "0" * 64,
        }],
    }).encode()


def test_loopback_post_returns_json_and_security_headers(server_url):
    request = Request(server_url + ROUTE, data=valid_body(), method="POST",
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=2) as response:
        assert response.status == 200
        assert json.load(response) == {"status": "completed", "job_id": "job1"}
        assert response.headers.get_content_type() == "application/json"
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("path,method,status", [
    (ROUTE, "GET", 405),
    ("/unknown", "GET", 404),
    ("/unknown", "POST", 404),
])
def test_loopback_route_and_method_fail_closed(server_url, path, method, status):
    data = b"{}" if method == "POST" else None
    request = Request(server_url + path, data=data, method=method)
    with pytest.raises(HTTPError) as failure:
        urlopen(request, timeout=2)
    assert failure.value.code == status
    payload = json.load(failure.value)
    assert "request_id" in payload["error"]


def test_loopback_invalid_json_returns_400(server_url):
    request = Request(server_url + ROUTE, data=b"{", method="POST")
    with pytest.raises(HTTPError) as failure:
        urlopen(request, timeout=2)
    assert failure.value.code == 400
    assert json.load(failure.value)["error"]["code"] == "invalid_json"


def test_review_output_route_returns_bound_xlsx():
    config = ApiConfig(
        host="127.0.0.1", port=0, auth_mode="local_disabled",
        allowed_app_ids=frozenset(), allowed_object_ids=frozenset(),
        max_body_bytes=4096, max_concurrent_jobs=1, processor_options={},
    )
    class ReviewApi:
        def handle(self, _headers, request_id, candidate_id, validation_id):
            assert (request_id, candidate_id, validation_id) == ("request-1", "candidate-1", "validation-1")
            return 200, b"xlsx-bytes"
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
        ProcessingApi(config, Processor), review_output_api=ReviewApi()))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        url = (f"http://127.0.0.1:{server.server_address[1]}/api/v1/parser-candidates/requests/"
               "request-1/candidates/candidate-1/validations/validation-1/output")
        with urlopen(url, timeout=2) as response:
            assert response.read() == b"xlsx-bytes"
            assert response.headers.get_content_type() == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            assert response.headers["Content-Disposition"] == 'attachment; filename="parsed-bom.xlsx"'
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_package_approval_and_exact_output_routes_dispatch_with_binary_headers():
    config = ApiConfig(
        host="127.0.0.1", port=0, auth_mode="local_disabled",
        allowed_app_ids=frozenset(), allowed_object_ids=frozenset(),
        max_body_bytes=4096, max_concurrent_jobs=1, processor_options={},
    )
    class PackageApi:
        def handle_get(self, _headers, plan_id, action, approval_id=None):
            assert (plan_id, action, approval_id) == ("plan-1", "approval", None)
            return 200, {"approval_id": "a" * 64}
        def handle_output(self, _headers, plan_id, approval_id, group_id):
            assert (plan_id, approval_id, group_id) == ("plan-1", "a" * 64, "group-1")
            return 200, b"resolved-xlsx"
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
        ProcessingApi(config, Processor), package_api=PackageApi()))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        root = f"http://127.0.0.1:{server.server_address[1]}/api/v1/bom-packages/plan-1"
        with urlopen(root + "/approval", timeout=2) as response:
            assert json.load(response)["approval_id"] == "a" * 64
        with urlopen(root + "/approvals/" + "a" * 64 + "/groups/group-1/output", timeout=2) as response:
            assert response.read() == b"resolved-xlsx"
            assert response.headers.get_content_type() == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            assert response.headers["Content-Length"] == str(len(b"resolved-xlsx"))
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
        for malformed in (root + "/approvals/bad/group/group-1/output",
                          root + "/approvals/" + "a" * 64 + "/groups/bad/id/output"):
            with pytest.raises(HTTPError) as failure:
                urlopen(malformed, timeout=2)
            assert failure.value.code == 404
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


def test_package_review_preview_post_reaches_the_package_api():
    # A044 (2026-10-12): POST .../review returned 404 because only "approve" was routed to the package API.
    config = ApiConfig(
        host="127.0.0.1", port=0, auth_mode="local_disabled",
        allowed_app_ids=frozenset(), allowed_object_ids=frozenset(),
        max_body_bytes=4096, max_concurrent_jobs=1, processor_options={},
    )
    calls = []
    class PackageApi:
        def handle_post(self, _headers, plan_id, action, body, approval_id=None):
            calls.append((plan_id, action, json.loads(body)))
            return 200, {"approval_ready": True}
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(
        ProcessingApi(config, Processor), package_api=PackageApi()))
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        root = f"http://127.0.0.1:{server.server_address[1]}/api/v1/bom-packages/plan-1"
        body = json.dumps({"conflict_dispositions": {"g" * 64: "f" * 64}}).encode()
        request = Request(root + "/review", data=body, method="POST", headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=2) as response:
            assert json.load(response) == {"approval_ready": True}
        assert calls == [("plan-1", "review", {"conflict_dispositions": {"g" * 64: "f" * 64}})]
        with pytest.raises(HTTPError) as failure:
            urlopen(Request(root + "/status", data=body, method="POST"), timeout=2)
        assert failure.value.code == 404
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
