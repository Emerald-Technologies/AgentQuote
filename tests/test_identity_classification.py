"""Critical read-only classifier access/output tests; no cloud calls."""
import base64
import json
from dataclasses import replace
from deterministic_api import ApiConfig
from bom_processor.identity_classification import classify_identity

CONFIG = ApiConfig('127.0.0.1', 0, 'easy_auth', frozenset({'trusted-app'}),
                   frozenset({'human'}), 100000, 1, {})


def headers(claims):
    return {'X-MS-CLIENT-PRINCIPAL': base64.b64encode(json.dumps({'claims': [
        {'typ': typ, 'val': value} for typ, value in claims]}).encode()).decode()}


def test_default_disabled_even_for_allowed_app():
    assert classify_identity(headers([('appid', 'trusted-app')]), CONFIG)[0] == 404


def test_only_booleans_no_identity_values():
    status, value = classify_identity(headers([('appid', 'trusted-app'), ('oid', 'private-id'),
                                               ('idtyp', 'app')]), CONFIG, enabled=True)
    assert status == 200 and value == {'allowedApplicationMatch': True, 'appOnlyClaim': True,
                                     'delegatedScopePresent': False, 'classificationCompatible': True}
    assert all(type(item) is bool for item in value.values())
    assert 'trusted-app' not in json.dumps(value) and 'private-id' not in json.dumps(value)


def test_delegated_or_missing_type_is_incompatible_not_permission():
    for claims in ([('appid', 'trusted-app')], [('appid', 'trusted-app'), ('idtyp', 'app'), ('scp', 'access')]):
        status, value = classify_identity(headers(claims), CONFIG, enabled=True)
        assert status == 200 and value['classificationCompatible'] is False


def test_human_only_unknown_app_missing_malformed_oversize_rejected():
    assert classify_identity(headers([('oid', 'human')]), CONFIG, enabled=True)[0] == 403
    assert classify_identity(headers([('appid', 'unknown')]), CONFIG, enabled=True)[0] == 403
    assert classify_identity({}, CONFIG, enabled=True)[0] == 401
    assert classify_identity({'X-MS-CLIENT-PRINCIPAL': 'malformed'}, CONFIG, enabled=True)[0] == 401
    assert classify_identity({'X-MS-CLIENT-PRINCIPAL': 'x' * 65537}, CONFIG, enabled=True)[0] == 401
    assert classify_identity({}, replace(CONFIG, auth_mode='local_disabled'), enabled=True)[0] == 403


def test_multiple_allowed_apps_rejected():
    config = replace(CONFIG, allowed_app_ids=frozenset({'trusted-app', 'other'}))
    assert classify_identity(headers([('appid', 'trusted-app'), ('azp', 'other')]), config, enabled=True)[0] == 403


def test_http_classifier_never_constructs_business_service(monkeypatch):
    import threading
    from http.server import ThreadingHTTPServer
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    from deterministic_api import ProcessingApi, make_handler
    def forbidden():
        raise AssertionError('business service must not be constructed')
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(ProcessingApi(CONFIG, forbidden)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f'http://127.0.0.1:{server.server_address[1]}/api/v1/identity/classification'
        principal = headers([('appid', 'trusted-app'), ('idtyp', 'app')])
        monkeypatch.delenv('BOM_API_IDENTITY_CLASSIFICATION_ENABLED', raising=False)
        try:
            urlopen(Request(url, headers=principal), timeout=2)
            raise AssertionError('disabled endpoint must reject')
        except HTTPError as error:
            assert error.code == 404
        monkeypatch.setenv('BOM_API_IDENTITY_CLASSIFICATION_ENABLED', 'true')
        with urlopen(Request(url, headers=principal), timeout=2) as response:
            assert json.load(response)['classificationCompatible'] is True
            assert response.headers['Cache-Control'] == 'no-store'
        for target in (url + '?extra=1', url):
            request = Request(target, headers=principal, **({'data': b'{}', 'method': 'POST'} if target == url else {}))
            try:
                urlopen(request, timeout=2)
                raise AssertionError('non-GET or non-exact route must reject')
            except HTTPError as error:
                assert error.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)