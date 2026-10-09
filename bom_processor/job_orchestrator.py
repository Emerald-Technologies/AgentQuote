"""Local service-side SAS creation and Container Apps Job start orchestration.

This module contains no network calls unless an Azure adapter is explicitly used.
Secrets and SAS URLs are intentionally absent from public results and exceptions.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX = 1024 * 1024 * 1024


def _path(value: str, prefix: str | None, name: str) -> str:
    if not isinstance(value, str) or not value or value.startswith("/") or "\\" in value:
        raise ValueError(f"invalid {name}")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"invalid {name}")
    if prefix and not value.startswith(prefix.rstrip("/") + "/"):
        raise ValueError(f"invalid {name}")
    return value


@dataclass(frozen=True)
class WorkItem:
    job_id: str
    source_container: str
    parser_container: str
    output_container: str
    source_blob: str
    parser_blob: str
    input_sha256: str
    input_size: int
    parser_sha256: str
    parser_size: int
    output_prefix: str
    input_name: str = "input.csv"
    output_name: str = "output.json"
    result_name: str = "result.json"
    interpretation_name: str | None = None
    # Plan 8.8 step 4a: opt-in parser row map, transported by the worker and verified by validation.
    row_map_name: str | None = None

    def __post_init__(self) -> None:
        if self.interpretation_name not in (None, "interpretation.json"):
            raise ValueError("invalid interpretation_name")
        if self.interpretation_name and self.interpretation_name in (self.input_name, self.output_name, self.result_name):
            raise ValueError("interpretation destination collision")
        if self.row_map_name not in (None, "row-map.json"):
            raise ValueError("invalid row_map_name")
        if self.row_map_name and self.interpretation_name:
            raise ValueError("row map and interpretation are exclusive")
        if self.row_map_name and self.row_map_name in (self.input_name, self.output_name, self.result_name):
            raise ValueError("row map destination collision")
        for name, value in (("job_id", self.job_id), ("source_container", self.source_container),
                            ("parser_container", self.parser_container), ("output_container", self.output_container)):
            if not isinstance(value, str) or not _ID.fullmatch(value):
                raise ValueError(f"invalid {name}")
        if not isinstance(self.output_prefix, str) or not self.output_prefix or self.output_prefix.startswith("/"):
            raise ValueError("invalid output_prefix")
        _path(self.source_blob, "", "source_blob")
        _path(self.parser_blob, "", "parser_blob")
        _path(self.output_prefix + "/placeholder", "", "output_prefix")
        if not self.output_prefix.rstrip("/").endswith("/" + self.job_id):
            raise ValueError("invalid output_prefix")
        for name, value in (("input_sha256", self.input_sha256), ("parser_sha256", self.parser_sha256)):
            if not isinstance(value, str) or not _SHA.fullmatch(value):
                raise ValueError(f"invalid {name}")
        for name, value in (("input_name", self.input_name), ("output_name", self.output_name), ("result_name", self.result_name)):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
                raise ValueError(f"invalid {name}")
        for name, value in (("input_size", self.input_size), ("parser_size", self.parser_size)):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX:
                raise ValueError(f"invalid {name}")

    @property
    def output_blob(self) -> str:
        return f"{self.output_prefix.rstrip('/')}/{self.output_name}"

    @property
    def result_blob(self) -> str:
        return f"{self.output_prefix.rstrip('/')}/{self.result_name}"

    @property
    def interpretation_blob(self) -> str | None:
        return f"{self.output_prefix.rstrip('/')}/{self.interpretation_name}" if self.interpretation_name else None

    @property
    def row_map_blob(self) -> str | None:
        return f"{self.output_prefix.rstrip('/')}/{self.row_map_name}" if self.row_map_name else None


@dataclass(frozen=True)
class SasUrls:
    input_url: str
    parser_url: str
    output_url: str
    result_url: str
    expires_on: datetime
    interpretation_url: str | None = None
    row_map_url: str | None = None

    def __repr__(self) -> str:
        return f"SasUrls(expires_on={self.expires_on!r})"


class SasFactory(Protocol):
    def create(self, item: WorkItem, *, now: datetime | None = None) -> SasUrls: ...


class StorageReader(Protocol):
    def read(self, container: str, blob: str) -> bytes: ...


class JobStarter(Protocol):
    def start(self, item: WorkItem, urls: SasUrls) -> str: ...


class AzureSasFactory:
    def __init__(self, account_url: str, credential: Any, *, start_skew: timedelta = timedelta(minutes=1), lifetime: timedelta = timedelta(minutes=15), blob_service_client: Any = None):
        self.account_url = account_url.rstrip("/")
        self.credential = credential
        self.start_skew, self.lifetime = start_skew, lifetime
        self.client = blob_service_client

    def create(self, item: WorkItem, *, now: datetime | None = None) -> SasUrls:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas
        client = self.client
        if client is None:
            from azure.storage.blob import BlobServiceClient
            client = BlobServiceClient(account_url=self.account_url, credential=self.credential)
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        start = now - self.start_skew
        expiry = now + self.lifetime
        delegation = client.get_user_delegation_key(key_start_time=start, key_expiry_time=expiry)
        def make(container: str, blob: str, *, write: bool) -> str:
            permissions = BlobSasPermissions(read=not write, create=write, write=write)
            token = generate_blob_sas(account_name=client.account_name, container_name=container, blob_name=blob, user_delegation_key=delegation, permission=permissions, start=start, expiry=expiry, protocol="https")
            return f"{self.account_url}/{container}/{blob}?{token}"
        return SasUrls(
            make(item.source_container, item.source_blob, write=False),
            make(item.parser_container, item.parser_blob, write=False),
            make(item.output_container, item.output_blob, write=True),
            make(item.output_container, item.result_blob, write=True),
            expiry,
            make(item.output_container, item.interpretation_blob, write=True) if item.interpretation_blob else None,
            make(item.output_container, item.row_map_blob, write=True) if item.row_map_blob else None,
        )


class AzureJobStarter:
    def __init__(self, *, subscription_id: str, resource_group: str, job_name: str, credential: Any, container_name: str = "worker", api_version: str = "2024-03-01", template: Mapping[str, Any] | None = None, opener: Any = urllib.request.urlopen):
        self.subscription_id, self.resource_group, self.job_name = subscription_id, resource_group, job_name
        self.credential, self.container_name, self.api_version, self.template, self.opener = credential, container_name, api_version, template, opener

    def start(self, item: WorkItem, urls: SasUrls) -> str:
        token = self.credential.get_token("https://management.usgovcloudapi.net/.default").token
        endpoint = f"https://management.usgovcloudapi.net/subscriptions/{self.subscription_id}/resourceGroups/{self.resource_group}/providers/Microsoft.App/jobs/{self.job_name}/start?api-version={self.api_version}"
        env = [{"name": n, "value": v} for n, v in (
            ("WORKER_JOB_ID", item.job_id), ("INPUT_URL", urls.input_url),
            ("PARSER_URL", urls.parser_url), ("OUTPUT_URL", urls.output_url),
            ("RESULT_URL", urls.result_url), ("INPUT_SHA256", item.input_sha256),
            ("INPUT_SIZE", str(item.input_size)), ("PARSER_SHA256", item.parser_sha256),
            ("PARSER_SIZE", str(item.parser_size)), ("WORKER_INPUT_NAME", item.input_name),
            ("WORKER_OUTPUT_NAME", item.output_name),
            ("WORKER_OUTPUT_CONTENT_TYPE", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        )]
        if item.interpretation_name:
            if not urls.interpretation_url:
                raise ValueError("interpretation URL missing")
            env.extend([{"name": "WORKER_INTERPRETATION_NAME", "value": item.interpretation_name},
                        {"name": "INTERPRETATION_URL", "value": urls.interpretation_url}])
        if item.row_map_name:
            if not urls.row_map_url:
                raise ValueError("row map URL missing")
            env.extend([{"name": "WORKER_ROW_MAP_NAME", "value": item.row_map_name},
                        {"name": "ROW_MAP_URL", "value": urls.row_map_url}])
        container: dict[str, Any] = dict(self.template or {})
        container["name"] = self.container_name
        container["env"] = env
        # The Jobs /start endpoint accepts StartJobExecutionTemplate directly,
        # not a resource-style {properties:{template:...}} wrapper.
        body = {"containers": [container]}
        request = urllib.request.Request(endpoint, data=json.dumps(body, separators=(",", ":")).encode(), headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, method="POST")
        try:
            with self.opener(request) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.HTTPError, ValueError) as exc:
            raise RuntimeError("job start failed") from exc
        execution = payload.get("name") or payload.get("properties", {}).get("executionName")
        if not isinstance(execution, str) or not execution:
            raise RuntimeError("job start returned no execution name")
        return execution.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class StartResult:
    job_id: str
    execution_name: str
    expires_on: datetime

    def __repr__(self) -> str:
        return f"StartResult(job_id={self.job_id!r}, execution_name={self.execution_name!r}, expires_on={self.expires_on!r})"


class JobOrchestrator:
    def __init__(self, reader: StorageReader, sas_factory: SasFactory, starter: JobStarter):
        self.reader, self.sas_factory, self.starter = reader, sas_factory, starter

    def start(self, item: WorkItem) -> StartResult:
        for container, blob, expected_hash, expected_size in (
            (item.source_container, item.source_blob, item.input_sha256, item.input_size),
            (item.parser_container, item.parser_blob, item.parser_sha256, item.parser_size),
        ):
            data = self.reader.read(container, blob)
            if len(data) != expected_size or hashlib.sha256(data).hexdigest().lower() != expected_hash.lower():
                raise ValueError("source evidence mismatch")
        urls = self.sas_factory.create(item)
        execution = self.starter.start(item, urls)
        return StartResult(item.job_id, execution, urls.expires_on)


def redact_url(url: str) -> str:
    if not isinstance(url, str):
        return "<redacted>"
    return url.split("?", 1)[0] + "?<redacted>" if "?" in url else url
