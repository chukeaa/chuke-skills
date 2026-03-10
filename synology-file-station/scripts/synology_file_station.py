#!/usr/bin/env python3
"""Synology File Station API CLI.

This script wraps major File Station APIs and reads connection credentials from env.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

TRUE_VALUES = {"1", "true", "yes", "on", "y"}
FALSE_VALUES = {"0", "false", "no", "off", "n"}

COMMON_ERROR_CODES = {
    100: "Unknown error",
    101: "No parameter of API, method, or version",
    102: "Requested API does not exist",
    103: "Requested method does not exist",
    104: "Requested version does not support this function",
    105: "Session has no permission",
    106: "Session timeout",
    107: "Session interrupted by duplicate login",
    119: "SID not found",
}

FILE_ERROR_CODES = {
    400: "Invalid parameter of file operation",
    401: "Unknown error of file operation",
    402: "System is too busy",
    403: "Invalid user for this file operation",
    404: "Invalid group for this file operation",
    405: "Invalid user and group for this file operation",
    406: "Cannot get user/group info from account server",
    407: "Operation not permitted",
    408: "No such file or directory",
    409: "Unsupported file system",
    410: "Failed to connect internet-based file system",
    411: "Read-only file system",
    412: "Filename too long in non-encrypted file system",
    413: "Filename too long in encrypted file system",
    414: "File already exists",
    415: "Disk quota exceeded",
    416: "No space left on device",
    417: "Input/output error",
    418: "Illegal name or path",
    419: "Illegal file name",
    420: "Illegal file name on FAT file system",
    421: "Device or resource busy",
    599: "No such task for file operation",
}

API_ALIASES: dict[str, list[str]] = {
    "auth": ["SYNO.API.Auth", "SYNO.APPAuth", "SYNO.API.Authenticator"],
    "filestation.info": ["SYNO.FileStation.Info"],
    "filestation.list": ["SYNO.FileStation.List"],
    "filestation.search": [
        "SYNO.FileStation.Search",
        "SYNO.FileStation.search",
        "SYNO.FileStationSearch",
    ],
    "filestation.createfolder": ["SYNO.FileStation.CreateFolder"],
    "filestation.rename": ["SYNO.FileStation.Rename"],
    "filestation.copymove": ["SYNO.FileStation.CopyMove"],
    "filestation.delete": [
        "SYNO.FileStation.Delete",
        "SYNO.FileStation/Delete",
        "SYNO.FileStationDelete",
    ],
    "filestation.upload": ["SYNO.FileStation.Upload", "SYNO.FileStation.upload"],
    "filestation.download": ["SYNO.FileStation.Download", "SYNO.FileStation.download"],
    "filestation.extract": ["SYNO.FileStation.Extract"],
    "filestation.compress": ["SYNO.FileStation.Compress"],
    "filestation.backgroundtask": [
        "SYNO.FileStation.BackgroundTask",
        "SYNO.FileStation.BackGroundTask",
        "SYNO.FileStation-backgroundTask",
    ],
}

TASK_APIS = {
    "copy-move": "filestation.copymove",
    "delete": "filestation.delete",
    "extract": "filestation.extract",
    "compress": "filestation.compress",
}


class SynologyError(RuntimeError):
    """Base error for script runtime failures."""


class ConfigError(SynologyError):
    """Raised when env configuration is invalid."""


class SynologyApiError(SynologyError):
    """Raised when Synology API returns success=false."""

    def __init__(
        self,
        api_name: str,
        method: str,
        code: int | None,
        message: str,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.api_name = api_name
        self.method = method
        self.code = code
        self.details = details
        self.message = message


class FeatureUnavailableError(SynologyError):
    """Raised when a command is intentionally disabled in this skill."""

    def __init__(self, feature: str, reason: str) -> None:
        super().__init__(reason)
        self.feature = feature
        self.reason = reason


@dataclass(frozen=True)
class SynologyConfig:
    base_url: str
    username: str
    password: str
    verify_ssl: bool = True
    timeout: int = 30
    session: str = "FileStation"
    readonly: bool = False
    mutation_allow_paths: tuple[str, ...] = ()


def emit_json(payload: Mapping[str, Any], stream: Any = sys.stdout) -> None:
    stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    stream.flush()


def parse_bool_value(raw: Any, label: str) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None:
        raise ValueError(f"{label} is required")
    text = str(raw).strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    raise ValueError(f"{label} must be true/false, got {raw!r}")


def parse_int_value(raw: Any, label: str, minimum: int = 0) -> int:
    try:
        value = int(str(raw).strip())
    except Exception as exc:  # pragma: no cover - defensive
        raise ValueError(f"{label} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{label} must be >= {minimum}, got {value}")
    return value


def split_csv_items(values: Sequence[str] | None) -> list[str]:
    if not values:
        return []
    items: list[str] = []
    for raw in values:
        for token in str(raw).split(","):
            cleaned = token.strip()
            if cleaned:
                items.append(cleaned)
    return items


def encode_array(values: Sequence[Any]) -> str:
    return json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))


def parse_additional(raw: str | None) -> str | None:
    if raw is None:
        return None
    items = split_csv_items([raw])
    if not items:
        return None
    return encode_array(items)


def normalize_base_url(raw: str) -> str:
    text = raw.strip()
    if not text:
        raise ValueError("SYNOLOGY_BASE_URL is required")
    if not re.match(r"^https?://", text, re.IGNORECASE):
        text = "https://" + text
    return text.rstrip("/")


def normalize_remote_path(raw: str, label: str = "path") -> str:
    text = str(raw or "").strip()
    if not text:
        raise ValueError(f"{label} is required")
    if not text.startswith("/"):
        raise ValueError(f"{label} must start with '/': {raw!r}")
    normalized = "/" + "/".join(token for token in text.split("/") if token)
    return normalized if normalized else "/"


def parse_allow_paths(raw: str | None) -> tuple[str, ...]:
    items = split_csv_items([raw]) if raw else []
    if not items:
        return ()

    normalized: list[str] = []
    for item in items:
        path = normalize_remote_path(item, "SYNOLOGY_MUTATION_ALLOW_PATHS")
        if path not in normalized:
            normalized.append(path)
    return tuple(normalized)


def path_in_allowlist(path: str, allow_paths: Sequence[str]) -> bool:
    normalized = normalize_remote_path(path)
    for allowed in allow_paths:
        if normalized == allowed:
            return True
        if normalized.startswith(allowed.rstrip("/") + "/"):
            return True
    return False


def ensure_write_enabled(config: SynologyConfig, command: str) -> None:
    if config.readonly:
        raise SynologyError(
            f"Command {command!r} is blocked: SYNOLOGY_READONLY=true (read-only mode)"
        )


def ensure_mutation_allowed(
    config: SynologyConfig,
    command: str,
    target_paths: Sequence[str],
) -> None:
    ensure_write_enabled(config, command)

    if not config.mutation_allow_paths:
        # Backward-compatible behavior: no allowlist means unrestricted mutation paths.
        return

    blocked: list[str] = []
    for path in target_paths:
        try:
            normalized = normalize_remote_path(path)
        except ValueError as exc:
            raise SynologyError(str(exc)) from exc
        if not path_in_allowlist(normalized, config.mutation_allow_paths):
            blocked.append(normalized)

    if blocked:
        raise SynologyError(
            "Command "
            f"{command!r} is blocked: target path(s) outside SYNOLOGY_MUTATION_ALLOW_PATHS: "
            + ", ".join(blocked)
        )


def normalize_api_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def maybe_error_message(code: int | None) -> str | None:
    if code is None:
        return None
    if code in COMMON_ERROR_CODES:
        return COMMON_ERROR_CODES[code]
    if code in FILE_ERROR_CODES:
        return FILE_ERROR_CODES[code]
    return None


def extract_task_id(data: Mapping[str, Any]) -> str:
    for key in ("taskid", "task_id", "taskId", "task id"):
        if key in data and data[key] is not None:
            return str(data[key])
    raise SynologyError(f"task id not found in response: {data}")


def load_config_from_env(env: Mapping[str, str] | None = None) -> SynologyConfig:
    source = dict(os.environ if env is None else env)

    raw_url = (source.get("SYNOLOGY_BASE_URL") or source.get("SYNOLOGY_URL") or "").strip()
    if not raw_url:
        raise ConfigError("Missing SYNOLOGY_BASE_URL (or SYNOLOGY_URL)")

    username = (
        source.get("SYNOLOGY_USERNAME")
        or source.get("SYNOLOGY_USER")
        or ""
    ).strip()
    if not username:
        raise ConfigError("Missing SYNOLOGY_USERNAME")

    password = source.get("SYNOLOGY_PASSWORD") or source.get("SYNOLOGY_PASS") or ""
    if not password:
        raise ConfigError("Missing SYNOLOGY_PASSWORD")

    verify_ssl_raw = source.get("SYNOLOGY_VERIFY_SSL", "true")
    timeout_raw = source.get("SYNOLOGY_TIMEOUT", "30")
    session_name = (source.get("SYNOLOGY_SESSION") or "FileStation").strip() or "FileStation"
    readonly_raw = source.get("SYNOLOGY_READONLY", "false")
    allow_paths_raw = source.get("SYNOLOGY_MUTATION_ALLOW_PATHS", "")

    try:
        verify_ssl = parse_bool_value(verify_ssl_raw, "SYNOLOGY_VERIFY_SSL")
        timeout = parse_int_value(timeout_raw, "SYNOLOGY_TIMEOUT", minimum=1)
        base_url = normalize_base_url(raw_url)
        readonly = parse_bool_value(readonly_raw, "SYNOLOGY_READONLY")
        mutation_allow_paths = parse_allow_paths(allow_paths_raw)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    return SynologyConfig(
        base_url=base_url,
        username=username,
        password=password,
        verify_ssl=verify_ssl,
        timeout=timeout,
        session=session_name,
        readonly=readonly,
        mutation_allow_paths=mutation_allow_paths,
    )


class SynologyClient:
    """Thin API client for DSM WebAPI / File Station."""

    def __init__(self, config: SynologyConfig):
        self.config = config
        self.sid: str | None = None
        self.api_info: dict[str, dict[str, Any]] = {}
        self._ssl_context = self._build_ssl_context(config.verify_ssl)

    def __enter__(self) -> "SynologyClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.logout()
        except Exception:
            return None
        return None

    @staticmethod
    def _build_ssl_context(verify_ssl: bool) -> ssl.SSLContext:
        if verify_ssl:
            return ssl.create_default_context()
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    def _make_endpoint(self, path: str) -> str:
        clean = path.strip()
        if clean.startswith("http://") or clean.startswith("https://"):
            return clean
        if clean.startswith("/"):
            return self.config.base_url + clean
        if clean.startswith("webapi/"):
            return self.config.base_url + "/" + clean
        return self.config.base_url + "/webapi/" + clean

    def _request(
        self,
        url: str,
        method: str,
        params: Mapping[str, Any] | None = None,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        request_method = method.upper()
        encoded_params = ""
        if params:
            pairs: list[tuple[str, str]] = []
            for key, value in params.items():
                if value is None:
                    continue
                pairs.append((str(key), str(value)))
            encoded_params = urllib.parse.urlencode(pairs)

        request_url = url
        request_data = body
        if request_method == "GET":
            if encoded_params:
                sep = "&" if "?" in request_url else "?"
                request_url = f"{request_url}{sep}{encoded_params}"
        elif request_data is None:
            request_data = encoded_params.encode("utf-8")

        req = urllib.request.Request(request_url, data=request_data, method=request_method)
        if headers:
            for key, value in headers.items():
                req.add_header(key, value)

        try:
            with urllib.request.urlopen(
                req,
                timeout=self.config.timeout,
                context=self._ssl_context,
            ) as response:
                status = int(response.getcode())
                response_headers = dict(response.headers.items())
                payload = response.read()
                return status, response_headers, payload
        except urllib.error.HTTPError as exc:
            response_headers = dict(exc.headers.items()) if exc.headers else {}
            payload = exc.read()
            return int(exc.code), response_headers, payload
        except urllib.error.URLError as exc:
            raise SynologyError(f"Network error while calling {request_url}: {exc}") from exc

    def _decode_json(self, api_name: str, method: str, body: bytes) -> Mapping[str, Any]:
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            text = body.decode("utf-8", errors="replace")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            snippet = text[:500].strip()
            raise SynologyError(
                f"Non-JSON response from {api_name}.{method}: {snippet!r}"
            ) from exc
        if not isinstance(payload, Mapping):
            raise SynologyError(
                f"Unexpected JSON payload type from {api_name}.{method}: {type(payload).__name__}"
            )
        return payload

    def _parse_api_response(
        self,
        api_name: str,
        method: str,
        status: int,
        body: bytes,
    ) -> Mapping[str, Any]:
        payload = self._decode_json(api_name, method, body)
        success = payload.get("success")
        if success is not True:
            error_obj = payload.get("error") if isinstance(payload.get("error"), Mapping) else {}
            code = error_obj.get("code") if isinstance(error_obj, Mapping) else None
            if isinstance(code, str) and code.isdigit():
                code_int: int | None = int(code)
            elif isinstance(code, (int, float)):
                code_int = int(code)
            else:
                code_int = None

            message = maybe_error_message(code_int) or "Synology API request failed"
            details = error_obj.get("errors") if isinstance(error_obj, Mapping) else None
            raise SynologyApiError(
                api_name=api_name,
                method=method,
                code=code_int,
                message=message,
                details=details,
            )

        data = payload.get("data")
        if isinstance(data, Mapping):
            return data
        if data is None:
            return {}
        if status >= 400:
            raise SynologyError(f"HTTP {status} from {api_name}.{method}")
        return {"value": data}

    def _to_wire_params(
        self,
        params: Mapping[str, Any],
        include_sid: bool,
    ) -> dict[str, str]:
        wire: dict[str, str] = {}
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, bool):
                wire[str(key)] = "true" if value else "false"
            elif isinstance(value, (int, float)):
                wire[str(key)] = str(value)
            else:
                wire[str(key)] = str(value)

        if include_sid:
            if not self.sid:
                raise SynologyError("Missing session sid, login required")
            wire["_sid"] = self.sid
        return wire

    def fetch_api_info(self) -> Mapping[str, Any]:
        endpoint = self._make_endpoint("query.cgi")
        status, _, body = self._request(
            endpoint,
            method="GET",
            params={
                "api": "SYNO.API.Info",
                "version": 1,
                "method": "query",
                "query": "all",
            },
        )
        data = self._parse_api_response("SYNO.API.Info", "query", status, body)
        info: dict[str, dict[str, Any]] = {}
        for key, value in data.items():
            if isinstance(value, Mapping):
                info[str(key)] = dict(value)
        if not info:
            raise SynologyError("SYNO.API.Info returned empty API map")
        self.api_info = info
        return data

    def resolve_api_name(self, api_key: str) -> str:
        if not self.api_info:
            self.fetch_api_info()

        candidates = API_ALIASES.get(api_key, [api_key])
        for candidate in candidates:
            if candidate in self.api_info:
                return candidate

        normalized_map: dict[str, str] = {}
        for actual_name in self.api_info:
            normalized = normalize_api_name(actual_name)
            if normalized not in normalized_map:
                normalized_map[normalized] = actual_name

        for candidate in candidates:
            normalized = normalize_api_name(candidate)
            if normalized in normalized_map:
                return normalized_map[normalized]

        raise SynologyError(
            f"API {api_key!r} not found on target DSM. Available count={len(self.api_info)}"
        )

    def get_api_spec(self, api_key: str) -> tuple[str, str, int]:
        actual_name = self.resolve_api_name(api_key)
        spec = self.api_info.get(actual_name) or {}
        path = str(spec.get("path") or "").strip()
        if not path:
            raise SynologyError(f"API {actual_name} has no path in query info")

        raw_version = spec.get("maxVersion")
        if raw_version is None:
            raw_version = spec.get("version")
        if raw_version is None:
            raw_version = 1
        try:
            version = int(raw_version)
        except Exception as exc:  # pragma: no cover - defensive
            raise SynologyError(f"API {actual_name} has invalid version {raw_version!r}") from exc

        return actual_name, path, version

    def ensure_login(self) -> None:
        if self.sid:
            return
        if not self.api_info:
            self.fetch_api_info()
        self.login()

    def login(self) -> str:
        api_name, path, version = self.get_api_spec("auth")
        endpoint = self._make_endpoint(path)
        params = self._to_wire_params(
            {
                "api": api_name,
                "version": version,
                "method": "login",
                "account": self.config.username,
                "passwd": self.config.password,
                "session": self.config.session,
                "format": "sid",
            },
            include_sid=False,
        )
        status, _, body = self._request(endpoint, method="GET", params=params)
        data = self._parse_api_response(api_name, "login", status, body)
        sid = data.get("sid")
        if not sid:
            raise SynologyError("Login succeeded but sid is missing")
        self.sid = str(sid)
        return self.sid

    def logout(self) -> None:
        if not self.sid:
            return
        try:
            api_name, path, version = self.get_api_spec("auth")
        except Exception:
            self.sid = None
            return

        endpoint = self._make_endpoint(path)
        params = self._to_wire_params(
            {
                "api": api_name,
                "version": version,
                "method": "logout",
                "session": self.config.session,
            },
            include_sid=True,
        )
        self._request(endpoint, method="GET", params=params)
        self.sid = None

    def call_api(
        self,
        api_key: str,
        method: str,
        params: Mapping[str, Any] | None = None,
        http_method: str = "GET",
        include_sid: bool = True,
    ) -> Mapping[str, Any]:
        if include_sid:
            self.ensure_login()
        if not self.api_info:
            self.fetch_api_info()

        api_name, path, version = self.get_api_spec(api_key)
        endpoint = self._make_endpoint(path)

        payload = {
            "api": api_name,
            "version": version,
            "method": method,
        }
        if params:
            payload.update(params)

        wire = self._to_wire_params(payload, include_sid=include_sid)
        status, _, body = self._request(endpoint, method=http_method, params=wire)
        return self._parse_api_response(api_name, method, status, body)

    def call_with_task_id(
        self,
        api_key: str,
        method: str,
        task_id: str,
        extra_params: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        base = dict(extra_params or {})
        last_error: SynologyApiError | None = None
        tried: list[str] = []

        for key in ("taskid", "task_id"):
            if key in tried:
                continue
            tried.append(key)
            params = dict(base)
            params[key] = task_id
            try:
                return self.call_api(api_key, method, params=params)
            except SynologyApiError as exc:
                # Retry alternate task-id key only for parameter-style errors.
                if exc.code in {100, 101, 400}:
                    last_error = exc
                    continue
                raise

        if last_error:
            raise last_error
        raise SynologyError(f"Unable to call {api_key}.{method} with task id {task_id}")

    def wait_task(
        self,
        api_key: str,
        task_id: str,
        poll_interval: float,
        timeout: int,
    ) -> Mapping[str, Any]:
        start = time.monotonic()
        while True:
            status = self.call_with_task_id(api_key, "status", task_id)
            finished = bool(status.get("finished"))
            if finished:
                return status

            if timeout > 0 and (time.monotonic() - start) >= timeout:
                raise SynologyError(f"Task {task_id} timed out after {timeout} seconds")
            time.sleep(max(poll_interval, 0.2))

    def upload_file(
        self,
        dest_folder: str,
        local_file: Path,
        create_parents: bool,
        overwrite_mode: str,
    ) -> Mapping[str, Any]:
        self.ensure_login()
        api_name, path, version = self.get_api_spec("filestation.upload")
        endpoint = self._make_endpoint(path)
        sid_query = urllib.parse.urlencode({"_sid": self.sid or ""})
        endpoint_with_sid = endpoint + ("&" if "?" in endpoint else "?") + sid_query

        overwrite_value: str | bool | None = None
        if overwrite_mode != "auto":
            if version >= 3:
                overwrite_value = "overwrite" if overwrite_mode == "overwrite" else "skip"
            else:
                overwrite_value = overwrite_mode == "overwrite"

        base_fields = {
            "api": api_name,
            "version": version,
            "method": "upload",
            "path": dest_folder,
            "create_parents": create_parents,
            "overwrite": overwrite_value,
        }
        wire_fields = self._to_wire_params(base_fields, include_sid=False)

        last_error: SynologyApiError | None = None
        for file_field in ("filename", "file"):
            content_type, body = build_multipart_body(wire_fields, file_field, local_file)
            status, _, response_body = self._request(
                endpoint_with_sid,
                method="POST",
                body=body,
                headers={"Content-Type": content_type},
            )
            try:
                return self._parse_api_response(api_name, "upload", status, response_body)
            except SynologyApiError as exc:
                if file_field == "filename" and exc.code in {100, 101, 400, 1802}:
                    last_error = exc
                    continue
                raise

        if last_error:
            raise last_error
        raise SynologyError("Upload failed with unknown error")

    def download_files(self, remote_paths: Sequence[str], mode: str) -> tuple[bytes, dict[str, str]]:
        self.ensure_login()
        api_name, path, version = self.get_api_spec("filestation.download")
        endpoint = self._make_endpoint(path)

        params = self._to_wire_params(
            {
                "api": api_name,
                "version": version,
                "method": "download",
                "path": encode_array(remote_paths),
                "mode": mode,
            },
            include_sid=True,
        )
        status, headers, body = self._request(endpoint, method="GET", params=params)

        content_type = ""
        for key, value in headers.items():
            if key.lower() == "content-type":
                content_type = value.lower()
                break

        if "application/json" in content_type or body[:1] == b"{":
            # Download API may return JSON on errors.
            data = self._parse_api_response(api_name, "download", status, body)
            raise SynologyError(f"Download returned JSON instead of file payload: {data}")

        if status >= 400:
            raise SynologyError(f"Download failed with HTTP status {status}")

        return body, headers


def build_multipart_body(
    fields: Mapping[str, str],
    file_field_name: str,
    file_path: Path,
) -> tuple[str, bytes]:
    boundary = f"----synology-boundary-{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    for key, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode("utf-8"))
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")

    mime_type, _ = mimetypes.guess_type(str(file_path))
    if not mime_type:
        mime_type = "application/octet-stream"

    file_data = file_path.read_bytes()
    chunks.append(f"--{boundary}\r\n".encode("utf-8"))
    chunks.append(
        (
            f'Content-Disposition: form-data; name="{file_field_name}"; '
            f'filename="{file_path.name}"\r\n'
        ).encode("utf-8")
    )
    chunks.append(f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"))
    chunks.append(file_data)
    chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))

    body = b"".join(chunks)
    content_type = f"multipart/form-data; boundary={boundary}"
    return content_type, body


def pair_values(left: list[str], right: list[str], left_label: str, right_label: str) -> tuple[list[str], list[str]]:
    if not left:
        raise SynologyError(f"{left_label} is required")
    if not right:
        raise SynologyError(f"{right_label} is required")

    if len(left) == len(right):
        return left, right
    if len(left) == 1 and len(right) > 1:
        return [left[0]] * len(right), right
    if len(right) == 1 and len(left) > 1:
        return left, [right[0]] * len(left)

    raise SynologyError(
        f"{left_label} count ({len(left)}) must match {right_label} count ({len(right)})"
    )


def ensure_paths(values: Sequence[str], label: str) -> list[str]:
    items = split_csv_items(list(values))
    if not items:
        raise SynologyError(f"{label} is required")
    return items


def command_check_config(args: argparse.Namespace, config: SynologyConfig) -> Mapping[str, Any]:
    payload: dict[str, Any] = {
        "type": "status",
        "event": "config_loaded",
        "base_url": config.base_url,
        "username": config.username,
        "session": config.session,
        "verify_ssl": config.verify_ssl,
        "timeout": config.timeout,
        "readonly": config.readonly,
        "mutation_allow_paths": list(config.mutation_allow_paths),
    }

    if not args.probe:
        return payload

    with SynologyClient(config) as client:
        client.ensure_login()
        resolved: dict[str, Any] = {}
        for key in (
            "filestation.info",
            "filestation.list",
            "filestation.search",
            "filestation.createfolder",
            "filestation.rename",
            "filestation.copymove",
            "filestation.delete",
            "filestation.upload",
            "filestation.download",
            "filestation.compress",
            "filestation.extract",
            "filestation.backgroundtask",
        ):
            try:
                api_name, api_path, api_version = client.get_api_spec(key)
                resolved[key] = {
                    "available": True,
                    "api": api_name,
                    "path": api_path,
                    "version": api_version,
                }
            except Exception as exc:
                resolved[key] = {
                    "available": False,
                    "error": str(exc),
                }

        payload["probe"] = {
            "login": True,
            "resolved_apis": resolved,
        }

    return payload


def command_info(client: SynologyClient) -> Mapping[str, Any]:
    data = client.call_api("filestation.info", "get")
    return {"type": "status", "event": "info", "data": data}


def command_list_shares(args: argparse.Namespace, client: SynologyClient) -> Mapping[str, Any]:
    params: dict[str, Any] = {
        "offset": args.offset,
        "limit": args.limit,
        "sort_by": args.sort_by,
        "sort_direction": args.sort_direction,
        "onlywritable": args.only_writable,
        "additional": parse_additional(args.additional),
    }
    data = client.call_api("filestation.list", "list_share", params=params)
    return {"type": "status", "event": "list_shares", "data": data}


def command_list(args: argparse.Namespace, client: SynologyClient) -> Mapping[str, Any]:
    params: dict[str, Any] = {
        "folder_path": args.folder,
        "offset": args.offset,
        "limit": args.limit,
        "sort_by": args.sort_by,
        "sort_direction": args.sort_direction,
        "pattern": args.pattern,
        "filetype": args.filetype,
        "goto_path": args.goto_path,
        "additional": parse_additional(args.additional),
    }
    data = client.call_api("filestation.list", "list", params=params)
    return {"type": "status", "event": "list", "data": data}


def command_get_info(args: argparse.Namespace, client: SynologyClient) -> Mapping[str, Any]:
    paths = ensure_paths(args.path, "--path")
    params: dict[str, Any] = {
        "path": encode_array(paths),
        "additional": parse_additional(args.additional),
    }
    data = client.call_api("filestation.list", "getinfo", params=params)
    return {"type": "status", "event": "get_info", "paths": paths, "data": data}


def build_search_start_params(args: argparse.Namespace) -> dict[str, Any]:
    folders = ensure_paths(args.folder, "--folder")
    params: dict[str, Any] = {
        "folder_path": encode_array(folders),
        "recursive": args.recursive,
        "pattern": args.pattern,
        "extension": args.extension,
        "filetype": args.filetype,
        "size_from": args.size_from,
        "size_to": args.size_to,
        "mtime_from": args.mtime_from,
        "mtime_to": args.mtime_to,
        "crtime_from": args.crtime_from,
        "crtime_to": args.crtime_to,
        "atime_from": args.atime_from,
        "atime_to": args.atime_to,
        "owner": args.owner,
        "group": args.group,
    }
    return params


def command_search_start(args: argparse.Namespace, client: SynologyClient) -> Mapping[str, Any]:
    data = client.call_api("filestation.search", "start", params=build_search_start_params(args))
    task_id = extract_task_id(data)
    return {"type": "status", "event": "search_started", "task_id": task_id, "data": data}


def command_search_list(args: argparse.Namespace, client: SynologyClient) -> Mapping[str, Any]:
    params: dict[str, Any] = {
        "offset": args.offset,
        "limit": args.limit,
        "sort_by": args.sort_by,
        "sort_direction": args.sort_direction,
        "pattern": args.pattern,
        "filetype": args.filetype,
        "additional": parse_additional(args.additional),
    }
    data = client.call_with_task_id("filestation.search", "list", args.task_id, extra_params=params)
    return {
        "type": "status",
        "event": "search_list",
        "task_id": args.task_id,
        "data": data,
    }


def command_search_stop(args: argparse.Namespace, client: SynologyClient) -> Mapping[str, Any]:
    client.call_with_task_id("filestation.search", "stop", args.task_id)
    return {"type": "status", "event": "search_stopped", "task_id": args.task_id}


def command_search_clean(args: argparse.Namespace, client: SynologyClient) -> Mapping[str, Any]:
    client.call_with_task_id("filestation.search", "clean", args.task_id)
    return {"type": "status", "event": "search_cleaned", "task_id": args.task_id}


def command_mkdir(args: argparse.Namespace, client: SynologyClient) -> Mapping[str, Any]:
    parents = split_csv_items(args.parent)
    names = split_csv_items(args.name)
    parents, names = pair_values(parents, names, "--parent", "--name")
    ensure_mutation_allowed(client.config, "mkdir", parents)

    params: dict[str, Any] = {
        "folder_path": encode_array(parents),
        "name": encode_array(names),
        "force_parent": args.force_parent,
        "additional": parse_additional(args.additional),
    }
    data = client.call_api("filestation.createfolder", "create", params=params)
    return {
        "type": "status",
        "event": "mkdir",
        "created": len(names),
        "data": data,
    }


def command_rename(args: argparse.Namespace, client: SynologyClient) -> Mapping[str, Any]:
    paths = split_csv_items(args.path)
    names = split_csv_items(args.name)
    paths, names = pair_values(paths, names, "--path", "--name")
    ensure_mutation_allowed(client.config, "rename", paths)

    params: dict[str, Any] = {
        "path": encode_array(paths),
        "name": encode_array(names),
        "additional": parse_additional(args.additional),
        "search_taskid": args.search_taskid,
    }
    data = client.call_api("filestation.rename", "rename", params=params)
    return {
        "type": "status",
        "event": "rename",
        "renamed": len(paths),
        "data": data,
    }


def command_copy_or_move(args: argparse.Namespace, client: SynologyClient, remove_src: bool) -> Mapping[str, Any]:
    paths = ensure_paths(args.path, "--path")
    ensure_mutation_allowed(client.config, "move" if remove_src else "copy", [*paths, args.dest])
    params: dict[str, Any] = {
        "path": encode_array(paths),
        "dest_folder_path": args.dest,
        "remove_src": remove_src,
        "accurate_progress": args.accurate_progress,
        "search_taskid": args.search_taskid,
    }
    if args.overwrite != "auto":
        params["overwrite"] = args.overwrite == "overwrite"

    data = client.call_api("filestation.copymove", "start", params=params)
    task_id = extract_task_id(data)

    result: dict[str, Any] = {
        "type": "status",
        "event": "move_started" if remove_src else "copy_started",
        "task_id": task_id,
        "data": data,
    }

    if args.wait:
        task_status = client.wait_task(
            "filestation.copymove",
            task_id,
            poll_interval=args.poll_interval,
            timeout=args.timeout,
        )
        result["event"] = "move_finished" if remove_src else "copy_finished"
        result["status"] = task_status

    return result


def command_copy(args: argparse.Namespace, client: SynologyClient) -> Mapping[str, Any]:
    return command_copy_or_move(args, client, remove_src=False)


def command_move(args: argparse.Namespace, client: SynologyClient) -> Mapping[str, Any]:
    return command_copy_or_move(args, client, remove_src=True)


def command_delete(args: argparse.Namespace, client: SynologyClient) -> Mapping[str, Any]:
    paths = ensure_paths(args.path, "--path")
    ensure_mutation_allowed(client.config, "delete", paths)

    if args.blocking:
        params = {
            "path": encode_array(paths),
            "recursive": args.recursive,
            "search_taskid": args.search_taskid,
        }
        client.call_api("filestation.delete", "delete", params=params)
        return {
            "type": "status",
            "event": "delete_finished",
            "mode": "blocking",
            "deleted": len(paths),
            "paths": paths,
        }

    start_data = client.call_api(
        "filestation.delete",
        "start",
        params={
            "path": encode_array(paths),
            "recursive": args.recursive,
            "accurate_progress": args.accurate_progress,
            "search_taskid": args.search_taskid,
        },
    )
    task_id = extract_task_id(start_data)
    result: dict[str, Any] = {
        "type": "status",
        "event": "delete_started",
        "mode": "non-blocking",
        "task_id": task_id,
        "data": start_data,
    }

    if args.wait:
        status = client.wait_task(
            "filestation.delete",
            task_id,
            poll_interval=args.poll_interval,
            timeout=args.timeout,
        )
        result["event"] = "delete_finished"
        result["status"] = status

    return result


def command_upload(args: argparse.Namespace, client: SynologyClient) -> Mapping[str, Any]:
    local_files = ensure_paths(args.file, "--file")
    ensure_mutation_allowed(client.config, "upload", [args.dest_folder])
    uploaded: list[dict[str, Any]] = []

    for item in local_files:
        local_path = Path(item).expanduser().resolve()
        if not local_path.exists() or not local_path.is_file():
            raise SynologyError(f"Local file not found: {local_path}")

        data = client.upload_file(
            dest_folder=args.dest_folder,
            local_file=local_path,
            create_parents=args.create_parents,
            overwrite_mode=args.overwrite,
        )
        uploaded.append(
            {
                "local_file": str(local_path),
                "remote_folder": args.dest_folder,
                "response": data,
            }
        )

    return {
        "type": "status",
        "event": "upload_finished",
        "uploaded_count": len(uploaded),
        "files": uploaded,
    }


def _content_disposition_filename(headers: Mapping[str, str]) -> str | None:
    value = ""
    for key, header_value in headers.items():
        if key.lower() == "content-disposition":
            value = header_value
            break
    if not value:
        return None

    match = re.search(r"filename\*=UTF-8''([^;]+)", value, re.IGNORECASE)
    if match:
        return urllib.parse.unquote(match.group(1)).strip()

    match = re.search(r'filename="([^"]+)"', value, re.IGNORECASE)
    if match:
        return match.group(1).strip()

    match = re.search(r"filename=([^;]+)", value, re.IGNORECASE)
    if match:
        return match.group(1).strip().strip('"')

    return None


def choose_download_path(output: str | None, remote_paths: Sequence[str], headers: Mapping[str, str]) -> Path:
    hint_name = _content_disposition_filename(headers)

    if output:
        out = Path(output).expanduser()
        if out.exists() and out.is_dir():
            filename = hint_name or default_download_name(remote_paths)
            return out / filename
        if len(remote_paths) > 1 and out.suffix.lower() != ".zip":
            raise SynologyError("When downloading multiple paths, --output must be a .zip file or an existing directory")
        return out

    filename = hint_name or default_download_name(remote_paths)
    return Path(filename)


def default_download_name(remote_paths: Sequence[str]) -> str:
    if len(remote_paths) > 1:
        ts = int(time.time())
        return f"download-{ts}.zip"
    only = str(remote_paths[0]).rstrip("/")
    name = Path(only).name
    if not name:
        ts = int(time.time())
        return f"download-{ts}.bin"
    return name


def command_download(args: argparse.Namespace, client: SynologyClient) -> Mapping[str, Any]:
    remote_paths = ensure_paths(args.path, "--path")
    data, headers = client.download_files(remote_paths, mode=args.mode)

    output_path = choose_download_path(args.output, remote_paths, headers)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(data)

    return {
        "type": "status",
        "event": "download_finished",
        "paths": remote_paths,
        "output": str(output_path.resolve()),
        "bytes": len(data),
    }


def command_compress(args: argparse.Namespace, client: SynologyClient) -> Mapping[str, Any]:
    # Keep argument validation so callers get immediate feedback on required params.
    paths = ensure_paths(args.path, "--path")
    ensure_mutation_allowed(client.config, "compress", [*paths, args.dest_file])
    if not args.dest_file:
        raise SynologyError("--dest-file is required")

    raise FeatureUnavailableError(
        feature="compress",
        reason=(
            "Temporarily unavailable: DSM returns code 105 (Session has no permission) "
            "for SYNO.FileStation.Compress.start in this environment, including GUI-equivalent requests."
        ),
    )


def command_extract(args: argparse.Namespace, client: SynologyClient) -> Mapping[str, Any]:
    ensure_mutation_allowed(client.config, "extract", [args.dest_folder])
    params: dict[str, Any] = {
        "file_path": args.archive,
        "dest_folder_path": args.dest_folder,
        "overwrite": args.overwrite,
        "keep_dir": args.keep_dir,
        "create_subfolder": args.create_subfolder,
        "codepage": args.codepage,
        "password": args.password,
    }
    if args.item_id:
        item_ids = [parse_int_value(v, "item_id", minimum=0) for v in split_csv_items(args.item_id)]
        params["item_id"] = encode_array(item_ids)

    data = client.call_api("filestation.extract", "start", params=params)
    task_id = extract_task_id(data)

    result: dict[str, Any] = {
        "type": "status",
        "event": "extract_started",
        "task_id": task_id,
        "data": data,
    }
    if args.wait:
        status = client.wait_task(
            "filestation.extract",
            task_id,
            poll_interval=args.poll_interval,
            timeout=args.timeout,
        )
        result["event"] = "extract_finished"
        result["status"] = status

    return result


def command_background_list(args: argparse.Namespace, client: SynologyClient) -> Mapping[str, Any]:
    params: dict[str, Any] = {
        "offset": args.offset,
        "limit": args.limit,
        "sort_by": args.sort_by,
        "sort_direction": args.sort_direction,
    }
    filters = split_csv_items(args.api_filter)
    if filters:
        params["api_filter"] = encode_array(filters)

    data = client.call_api("filestation.backgroundtask", "list", params=params)
    return {"type": "status", "event": "background_list", "data": data}


def command_task_status(args: argparse.Namespace, client: SynologyClient) -> Mapping[str, Any]:
    api_key = TASK_APIS[args.api]
    data = client.call_with_task_id(api_key, "status", args.task_id)
    return {
        "type": "status",
        "event": "task_status",
        "api": args.api,
        "task_id": args.task_id,
        "data": data,
    }


def command_task_stop(args: argparse.Namespace, client: SynologyClient) -> Mapping[str, Any]:
    ensure_write_enabled(client.config, "task-stop")
    api_key = TASK_APIS[args.api]
    client.call_with_task_id(api_key, "stop", args.task_id)
    return {
        "type": "status",
        "event": "task_stopped",
        "api": args.api,
        "task_id": args.task_id,
    }


def add_wait_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--wait", action="store_true", help="Poll task status until finished")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Polling interval seconds")
    parser.add_argument("--timeout", type=int, default=600, help="Wait timeout seconds (0=no timeout)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synology File Station API CLI",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check-config", help="Validate env config")
    check.add_argument("--probe", action="store_true", help="Login and resolve key API info")

    subparsers.add_parser("info", help="Get File Station info")

    list_shares = subparsers.add_parser("list-shares", help="List shared folders")
    list_shares.add_argument("--offset", type=int, default=0)
    list_shares.add_argument("--limit", type=int, default=0)
    list_shares.add_argument("--sort-by", default="name")
    list_shares.add_argument("--sort-direction", choices=["asc", "desc"], default="asc")
    list_shares.add_argument("--only-writable", action="store_true")
    list_shares.add_argument("--additional", default="real_path,owner,time")

    list_files = subparsers.add_parser("list", help="List files in a folder")
    list_files.add_argument("--folder", required=True, help="Folder path starting with a shared folder")
    list_files.add_argument("--offset", type=int, default=0)
    list_files.add_argument("--limit", type=int, default=0)
    list_files.add_argument("--sort-by", default="name")
    list_files.add_argument("--sort-direction", choices=["asc", "desc"], default="asc")
    list_files.add_argument("--pattern")
    list_files.add_argument("--filetype", choices=["all", "file", "dir"], default="all")
    list_files.add_argument("--goto-path")
    list_files.add_argument("--additional", default="real_path,size,owner,time,perm,type")

    get_info = subparsers.add_parser("get-info", help="Get details for files/folders")
    get_info.add_argument("--path", action="append", required=True, help="Repeat or comma-separate")
    get_info.add_argument("--additional", default="real_path,size,owner,time,perm,type")

    search_start = subparsers.add_parser("search-start", help="Start a search task")
    search_start.add_argument("--folder", action="append", required=True, help="Repeat or comma-separate")
    search_start.add_argument("--recursive", action=argparse.BooleanOptionalAction, default=True)
    search_start.add_argument("--pattern")
    search_start.add_argument("--extension")
    search_start.add_argument("--filetype", choices=["all", "file", "dir"], default="all")
    search_start.add_argument("--size-from", type=int)
    search_start.add_argument("--size-to", type=int)
    search_start.add_argument("--mtime-from", type=int)
    search_start.add_argument("--mtime-to", type=int)
    search_start.add_argument("--crtime-from", type=int)
    search_start.add_argument("--crtime-to", type=int)
    search_start.add_argument("--atime-from", type=int)
    search_start.add_argument("--atime-to", type=int)
    search_start.add_argument("--owner")
    search_start.add_argument("--group")

    search_list = subparsers.add_parser("search-list", help="List search results by task id")
    search_list.add_argument("--task-id", required=True)
    search_list.add_argument("--offset", type=int, default=0)
    search_list.add_argument("--limit", type=int, default=200)
    search_list.add_argument("--sort-by", default="name")
    search_list.add_argument("--sort-direction", choices=["asc", "desc"], default="asc")
    search_list.add_argument("--pattern")
    search_list.add_argument("--filetype", choices=["all", "file", "dir"], default="all")
    search_list.add_argument("--additional", default="real_path,size,owner,time,perm,type")

    search_stop = subparsers.add_parser("search-stop", help="Stop a search task")
    search_stop.add_argument("--task-id", required=True)

    search_clean = subparsers.add_parser("search-clean", help="Clean search temporary DB")
    search_clean.add_argument("--task-id", required=True)

    mkdir = subparsers.add_parser("mkdir", help="Create folder(s)")
    mkdir.add_argument("--parent", action="append", required=True, help="Parent path(s)")
    mkdir.add_argument("--name", action="append", required=True, help="Folder name(s)")
    mkdir.add_argument("--force-parent", action="store_true")
    mkdir.add_argument("--additional", default="real_path,size,owner,time,perm,type")

    rename = subparsers.add_parser("rename", help="Rename file/folder")
    rename.add_argument("--path", action="append", required=True)
    rename.add_argument("--name", action="append", required=True)
    rename.add_argument("--additional", default="real_path,size,owner,time,perm,type")
    rename.add_argument("--search-taskid")

    copy_cmd = subparsers.add_parser("copy", help="Copy files/folders")
    copy_cmd.add_argument("--path", action="append", required=True)
    copy_cmd.add_argument("--dest", required=True)
    copy_cmd.add_argument("--overwrite", choices=["auto", "overwrite", "skip"], default="auto")
    copy_cmd.add_argument("--accurate-progress", action=argparse.BooleanOptionalAction, default=True)
    copy_cmd.add_argument("--search-taskid")
    add_wait_args(copy_cmd)

    move_cmd = subparsers.add_parser("move", help="Move files/folders")
    move_cmd.add_argument("--path", action="append", required=True)
    move_cmd.add_argument("--dest", required=True)
    move_cmd.add_argument("--overwrite", choices=["auto", "overwrite", "skip"], default="auto")
    move_cmd.add_argument("--accurate-progress", action=argparse.BooleanOptionalAction, default=True)
    move_cmd.add_argument("--search-taskid")
    add_wait_args(move_cmd)

    delete_cmd = subparsers.add_parser("delete", help="Delete files/folders")
    delete_cmd.add_argument("--path", action="append", required=True)
    delete_cmd.add_argument("--recursive", action=argparse.BooleanOptionalAction, default=True)
    delete_cmd.add_argument("--accurate-progress", action=argparse.BooleanOptionalAction, default=True)
    delete_cmd.add_argument("--search-taskid")
    mode = delete_cmd.add_mutually_exclusive_group()
    mode.add_argument("--blocking", action="store_true", default=True)
    mode.add_argument("--non-blocking", dest="blocking", action="store_false")
    add_wait_args(delete_cmd)

    upload = subparsers.add_parser("upload", help="Upload local file(s)")
    upload.add_argument("--dest-folder", required=True)
    upload.add_argument("--file", action="append", required=True, help="Local file path(s)")
    upload.add_argument("--create-parents", action="store_true")
    upload.add_argument("--overwrite", choices=["auto", "overwrite", "skip"], default="auto")

    download = subparsers.add_parser("download", help="Download remote file(s)/folder(s)")
    download.add_argument("--path", action="append", required=True, help="Remote path(s)")
    download.add_argument("--mode", choices=["open", "download"], default="download")
    download.add_argument("--output", help="Output file or directory path")

    compress = subparsers.add_parser(
        "compress",
        help="Compress files/folders (temporarily unavailable)",
    )
    compress.add_argument("--path", action="append", required=True)
    compress.add_argument("--dest-file", required=True)
    compress.add_argument("--level", choices=["moderate", "store", "fastest", "best"], default="moderate")
    compress.add_argument("--mode", choices=["add", "update", "refreshen", "synchronize"], default="add")
    compress.add_argument("--format", choices=["zip", "7z"], default="zip")
    compress.add_argument("--password")
    add_wait_args(compress)

    extract = subparsers.add_parser("extract", help="Extract archive")
    extract.add_argument("--archive", required=True, help="Archive file path")
    extract.add_argument("--dest-folder", required=True)
    extract.add_argument("--overwrite", action=argparse.BooleanOptionalAction, default=False)
    extract.add_argument("--keep-dir", action=argparse.BooleanOptionalAction, default=True)
    extract.add_argument("--create-subfolder", action=argparse.BooleanOptionalAction, default=False)
    extract.add_argument("--codepage")
    extract.add_argument("--password")
    extract.add_argument("--item-id", action="append", help="Optional item id(s)")
    add_wait_args(extract)

    bg = subparsers.add_parser("background-list", help="List background tasks")
    bg.add_argument("--offset", type=int, default=0)
    bg.add_argument("--limit", type=int, default=0)
    bg.add_argument("--sort-by", choices=["crtime", "finished"], default="crtime")
    bg.add_argument("--sort-direction", choices=["asc", "desc"], default="asc")
    bg.add_argument("--api-filter", action="append", help="API filter list")

    task_status = subparsers.add_parser("task-status", help="Query task status")
    task_status.add_argument("--api", choices=sorted(TASK_APIS.keys()), required=True)
    task_status.add_argument("--task-id", required=True)

    task_stop = subparsers.add_parser("task-stop", help="Stop task")
    task_stop.add_argument("--api", choices=sorted(TASK_APIS.keys()), required=True)
    task_stop.add_argument("--task-id", required=True)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        config = load_config_from_env()

        if args.command == "check-config":
            emit_json(command_check_config(args, config))
            return 0

        with SynologyClient(config) as client:
            if args.command == "info":
                emit_json(command_info(client))
            elif args.command == "list-shares":
                emit_json(command_list_shares(args, client))
            elif args.command == "list":
                emit_json(command_list(args, client))
            elif args.command == "get-info":
                emit_json(command_get_info(args, client))
            elif args.command == "search-start":
                emit_json(command_search_start(args, client))
            elif args.command == "search-list":
                emit_json(command_search_list(args, client))
            elif args.command == "search-stop":
                emit_json(command_search_stop(args, client))
            elif args.command == "search-clean":
                emit_json(command_search_clean(args, client))
            elif args.command == "mkdir":
                emit_json(command_mkdir(args, client))
            elif args.command == "rename":
                emit_json(command_rename(args, client))
            elif args.command == "copy":
                emit_json(command_copy(args, client))
            elif args.command == "move":
                emit_json(command_move(args, client))
            elif args.command == "delete":
                emit_json(command_delete(args, client))
            elif args.command == "upload":
                emit_json(command_upload(args, client))
            elif args.command == "download":
                emit_json(command_download(args, client))
            elif args.command == "compress":
                emit_json(command_compress(args, client))
            elif args.command == "extract":
                emit_json(command_extract(args, client))
            elif args.command == "background-list":
                emit_json(command_background_list(args, client))
            elif args.command == "task-status":
                emit_json(command_task_status(args, client))
            elif args.command == "task-stop":
                emit_json(command_task_stop(args, client))
            else:
                raise SynologyError(f"Unsupported command: {args.command}")

        return 0

    except ConfigError as exc:
        emit_json({"type": "error", "event": "invalid_config", "error": str(exc)}, stream=sys.stderr)
        return 2
    except FeatureUnavailableError as exc:
        emit_json(
            {
                "type": "error",
                "event": "not_available",
                "feature": exc.feature,
                "message": exc.reason,
            },
            stream=sys.stderr,
        )
        return 1
    except SynologyApiError as exc:
        emit_json(
            {
                "type": "error",
                "event": "api_error",
                "api": exc.api_name,
                "method": exc.method,
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            },
            stream=sys.stderr,
        )
        return 1
    except SynologyError as exc:
        emit_json({"type": "error", "event": "runtime_error", "error": str(exc)}, stream=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - defensive
        emit_json({"type": "error", "event": "unexpected_error", "error": str(exc)}, stream=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
