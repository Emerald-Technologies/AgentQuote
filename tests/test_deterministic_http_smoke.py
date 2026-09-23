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
