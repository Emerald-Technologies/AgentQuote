import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from bom_processor import AzureBlobStorage, BlobConflictError, BlobNotFoundError, ProcessingError


class ResourceNotFoundError(Exception):
    pass


class ResourceExistsError(Exception):
    pass


class HttpResponseError(Exception):
    pass


class ContentSettings:
    def __init__(self, *, content_type):
        self.content_type = content_type


@pytest.fixture(autouse=True)
def fake_azure_modules(monkeypatch):
    azure = ModuleType("azure")
    core = ModuleType("azure.core")
    exceptions = ModuleType("azure.core.exceptions")
    exceptions.ResourceNotFoundError = ResourceNotFoundError
    exceptions.ResourceExistsError = ResourceExistsError
    exceptions.HttpResponseError = HttpResponseError
    storage = ModuleType("azure.storage")
    blob = ModuleType("azure.storage.blob")
    blob.ContentSettings = ContentSettings
    blob.BlobServiceClient = Mock(name="BlobServiceClient")
    for name, module in {
        "azure": azure, "azure.core": core, "azure.core.exceptions": exceptions,
        "azure.storage": storage, "azure.storage.blob": blob,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    return blob


def service_with_containers():
    source = Mock(name="source_container")
    parser = Mock(name="parser_container")
    service = Mock(name="blob_service")
    service.get_container_client.side_effect = lambda name: {"boms": source, "parsers": parser}[name]
    return service, source, parser


def test_connection_string_constructor_selects_expected_containers(fake_azure_modules):
    service, source, parser = service_with_containers()
    factory = fake_azure_modules.BlobServiceClient.from_connection_string
    factory.return_value = service
    storage = AzureBlobStorage("fake-connection")
    factory.assert_called_once_with("fake-connection")
    assert storage._sources is source
    assert storage._parsers is parser


def test_managed_identity_constructor_uses_account_url_and_custom_containers(fake_azure_modules):
    source, parser, service = Mock(), Mock(), Mock()
    service.get_container_client.side_effect = lambda name: {"source": source, "parser": parser}[name]
    fake_azure_modules.BlobServiceClient.return_value = service
    credential = object()
    storage = AzureBlobStorage.from_account_url(
        "https://example.blob.core.usgovcloudapi.net", credential,
        source_container="source", parser_container="parser",
    )
    fake_azure_modules.BlobServiceClient.assert_called_once_with(
        account_url="https://example.blob.core.usgovcloudapi.net", credential=credential
    )
    assert storage._sources is source
    assert storage._parsers is parser


def blob_client(data=b"abc", declared_size=None):
    client = Mock()
    client.get_blob_properties.return_value = SimpleNamespace(
        size=len(data) if declared_size is None else declared_size
    )
    client.download_blob.return_value.readall.return_value = data
    return client


def test_bounded_download_accepts_limit_and_uses_single_concurrency():
    client = blob_client(b"abc")
    assert AzureBlobStorage._download_bounded(client, 3) == b"abc"
    client.download_blob.assert_called_once_with(max_concurrency=1)
    client.download_blob.return_value.readall.assert_called_once_with()


def test_declared_oversize_is_rejected_before_download():
    client = blob_client(b"abc", declared_size=4)
    with pytest.raises(ProcessingError, match="exceeds limit"):
        AzureBlobStorage._download_bounded(client, 3)
    client.download_blob.assert_not_called()


def test_actual_oversize_is_rejected_after_download():
    client = blob_client(b"abcd", declared_size=3)
    with pytest.raises(ProcessingError, match="exceeds limit"):
        AzureBlobStorage._download_bounded(client, 3)


def test_not_found_and_service_failure_remain_distinct():
    missing = blob_client()
    missing.get_blob_properties.side_effect = ResourceNotFoundError("missing")
    with pytest.raises(BlobNotFoundError):
        AzureBlobStorage._download_bounded(missing, 10)
    unavailable = blob_client()
    unavailable.get_blob_properties.side_effect = HttpResponseError("unavailable")
    with pytest.raises(ProcessingError, match="download failed") as failure:
        AzureBlobStorage._download_bounded(unavailable, 10)
    assert not isinstance(failure.value, BlobNotFoundError)


def configured_storage(client):
    storage = AzureBlobStorage.__new__(AzureBlobStorage)
    source = Mock()
    source.get_blob_client.return_value = client
    storage._sources = source
    parser = Mock()
    parser.get_blob_client.return_value = client
    storage._parsers = parser
    return storage


def test_missing_output_returns_none_but_service_failure_does_not():
    missing = blob_client()
    missing.get_blob_properties.side_effect = ResourceNotFoundError("missing")
    assert configured_storage(missing).download_output("missing", 10) is None
    unavailable = blob_client()
    unavailable.get_blob_properties.side_effect = HttpResponseError("unavailable")
    with pytest.raises(ProcessingError, match="download failed"):
        configured_storage(unavailable).download_output("output", 10)


def test_upload_uses_content_settings_and_no_overwrite():
    client = blob_client()
    storage = configured_storage(client)
    storage.upload_output_immutable("output.xlsx", b"data", "application/test")
    assert client.upload_blob.call_args.args == (b"data",)
    kwargs = client.upload_blob.call_args.kwargs
    assert kwargs["overwrite"] is False
    assert isinstance(kwargs["content_settings"], ContentSettings)
    assert kwargs["content_settings"].content_type == "application/test"
    client.download_blob.assert_not_called()


def test_existing_identical_upload_is_idempotent():
    client = blob_client(b"data")
    client.upload_blob.side_effect = ResourceExistsError("exists")
    configured_storage(client).upload_output_immutable("output.xlsx", b"data", "application/test")


def test_existing_different_upload_is_conflict():
    client = blob_client(b"other")
    client.upload_blob.side_effect = ResourceExistsError("exists")
    with pytest.raises(BlobConflictError, match="immutable output conflict"):
        configured_storage(client).upload_output_immutable("output.xlsx", b"data", "application/test")


def test_non_conflict_upload_failure_is_processing_error_without_comparison():
    client = blob_client()
    client.upload_blob.side_effect = HttpResponseError("service unavailable")
    with pytest.raises(ProcessingError, match="output upload failed"):
        configured_storage(client).upload_output_immutable("output.xlsx", b"data", "application/test")
    client.download_blob.assert_not_called()


def test_registry_create_reuses_immutable_upload_and_returns_etag():
    client = blob_client(b"data")
    client.get_blob_properties.return_value.etag = "etag-created"
    storage = configured_storage(client)

    assert storage.upload_registry_if_match("registry/customer/master.json", b"data", "application/json", None) == "etag-created"
    assert client.upload_blob.call_args.args == (b"data",)
    kwargs = client.upload_blob.call_args.kwargs
    assert kwargs["overwrite"] is False
    assert isinstance(kwargs["content_settings"], ContentSettings)
    assert kwargs["content_settings"].content_type == "application/json"


def test_registry_create_conflict_is_preserved_from_immutable_upload():
    client = blob_client(b"other")
    client.upload_blob.side_effect = ResourceExistsError("exists")

    with pytest.raises(BlobConflictError, match="immutable registry conflict"):
        configured_storage(client).upload_registry_if_match("registry/customer/master.json", b"data", "application/json", None)
