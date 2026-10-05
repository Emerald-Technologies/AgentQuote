"""Delegated approval integrity and legacy resolution regressions; offline only."""
import json
import pytest
from test_package_resolution import request_and_store, plan, approve, api_config, auth_headers
from bom_processor.package_resolution import ParentPackageService, PackageResolutionError
from deterministic_api import PackageApi


@pytest.mark.parametrize('mutation', ['change', 'remove', 'add'])
def test_caller_attribution_tamper_rejected(mutation):
    request, storage = request_and_store()
    service = ParentPackageService(storage)
    package = plan(service, request)
    review = service.review(package['plan_id'])
    record = service.approve(plan_id=package['plan_id'], review_manifest_sha256=review['manifest_sha256'],
                             approver='admin-1', **({} if mutation == 'add' else {'caller_application': 'trusted-app'}))
    path = service._approval_path(package['plan_id'], record['approval_id'])
    value = json.loads(storage.registry[path])
    if mutation == 'remove':
        del value['caller_application']
    else:
        value['caller_application'] = 'other-app'
    storage.registry[path] = json.dumps(value).encode()
    with pytest.raises(PackageResolutionError, match='PACKAGE_APPROVAL_BINDING'):
        service._bound_approval(package['plan_id'], record['approval_id'])


def test_legacy_unpublished_approval_resolves_with_original_human_authority():
    request, storage = request_and_store()
    service = ParentPackageService(storage)
    package = plan(service, request)
    _, record = approve(service, package)
    assert 'caller_application' not in record
    api = PackageApi(api_config(), lambda: service)
    status, result = api.handle_post(auth_headers(), package['plan_id'], 'resolve', b'{}', record['approval_id'])
    assert status == 200 and result['status'] == 'completed'