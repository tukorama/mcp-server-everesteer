"""Async HTTP client for Everesteer API v1 + credential-free presigned S3/CDN transfers."""

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

import httpx2

from mcp_server_everesteer import __version__
from mcp_server_everesteer.client.endpoints import Endpoint
from mcp_server_everesteer.client.retry import (
    RETRY_SAFE,
    RETRY_UNSAFE,
    backoff,
    clamp,
    parse_retry_after,
)
from mcp_server_everesteer.client.transfer import (
    DownloadKind,
    StagedDownload,
    body_problem,
    confine_output_path,
    media_problem,
    read_file_bytes,
)
from mcp_server_everesteer.config import Settings
from mcp_server_everesteer.errors import (
    AuthError,
    CloudflareAccessError,
    ConflictError,
    DeadlineExceeded,
    EmailVerificationRequired,
    EveresteerError,
    InvalidRequest,
    NetworkError,
    NotFound,
    NotSent,
    PermissionDenied,
    RateLimited,
    ServerError,
)

logger = logging.getLogger(__name__)

_sleep = asyncio.sleep
_monotonic = time.monotonic
_now = time.time
_CHUNK_BYTES = 1 << 20


def monotonic() -> float:
    """Return current monotonic timestamp (uses the shared client clock hook)."""
    return _monotonic()


async def sleep(seconds: float) -> None:
    """Await ``seconds`` using the shared client sleep hook."""
    await _sleep(seconds)


Params = Mapping[str, Any] | None

# Coverage recorder hook for EVERESTEER_TEST_COVERAGE=strict.
_coverage_hook: Callable[[Endpoint], None] | None = None


def set_coverage_hook(hook: Callable[[Endpoint], None] | None) -> None:
    global _coverage_hook
    _coverage_hook = hook


def parse_json(response: httpx2.Response) -> Any:
    if not response.content.strip():
        return None
    try:
        return response.json()
    except ValueError:
        return None


def encode_body(body: Any) -> bytes:
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()


def _query(params: Params) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for key, value in (params or {}).items():
        if value is None:
            continue
        values: list[Any] = (
            list(cast(list[Any], value)) if isinstance(value, list | tuple) else [value]
        )
        for item in values:
            if isinstance(item, bool):
                pairs.append((key, "true" if item else "false"))
            else:
                pairs.append((key, str(item)))
    return pairs


def _extract_error_code(body: Any) -> str | None:
    if isinstance(body, dict):
        body_dict = cast(dict[str, Any], body)
        for key in ("code", "error_code", "error"):
            val = body_dict.get(key)
            if isinstance(val, str):
                return val
    return None


def _extract_message(body: Any, text: str) -> str:
    if isinstance(body, dict):
        body_dict = cast(dict[str, Any], body)
        for key in ("detail", "message", "error", "error_msg", "msg", "note"):
            val = body_dict.get(key)
            if val:
                return str(val)
    if isinstance(body, list) and body:
        first = cast(Any, body[0])
        if isinstance(first, dict):
            return _extract_message(first, text)
    return text[:300] or "no details"


def _looks_like_cloudflare_html(text: str) -> bool:
    head = text.lstrip()[:300].lower()
    return (
        "<html" in head
        or "<!doctype" in head
        or "cloudflare" in text.lower()
        or "AccessDenied" in text
    )


def _is_cloudflare_access_challenge(status: int, text: str, headers: httpx2.Headers) -> bool:
    if status not in (301, 302, 303, 307, 308, 401, 403):
        return False
    low = text.lower()
    loc = headers.get("location", "").lower()
    return (
        "cloudflare access" in low
        or "cdn-cgi/access" in low
        or "cdn-cgi/access" in loc
        or "cf-access" in low
        or "accessdenied" in low
    )


def _reached_cloudflare_access(response: httpx2.Response) -> bool:
    """Return whether a followed redirect chain ended on a Cloudflare Access login."""
    return "cdn-cgi/access" in str(response.url).lower() or any(
        _is_cloudflare_access_challenge(hop.status_code, "", hop.headers)
        for hop in response.history
    )


def _invalid_download(label: str, problem: str, preview: str, status: int) -> EveresteerError:
    message = f"{label} returned {problem}; nothing was saved and any previous file was kept."
    low = preview.lower()
    if "cloudflare access" in low or "cdn-cgi/access" in low or "cf-access" in low:
        return CloudflareAccessError(
            message,
            status=status,
            detail=preview,
            hint="Set CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET or check EVERESTEER_BASE_URL.",
        )
    return ServerError(message, status=status, detail=preview, hint="Retry the download later.")


class EveresteerClient:
    """Async Everesteer API v1 client with safe retries, CF Access awareness, and S3 lanes."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        base_url = settings.resolved_base_url
        basic_auth = settings.basic_auth()
        self._http = httpx2.AsyncClient(
            base_url=base_url,
            follow_redirects=False,
            timeout=settings.http_timeout,
            transport=transport,
            auth=basic_auth,
            headers={
                "Accept": "application/json",
                "User-Agent": f"mcp-server-everesteer/{__version__}",
            },
        )
        # Separate credential-free client for presigned S3 / CloudFront redirects so X-API-Key
        # and CF-Access-* headers never leak to third-party storage hosts.
        self._cdn_http = httpx2.AsyncClient(
            follow_redirects=True,
            timeout=settings.upload_timeout,
            transport=transport,
            headers={"User-Agent": f"mcp-server-everesteer/{__version__}"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._cdn_http.aclose()

    def _auth_headers(self, endpoint: Endpoint) -> dict[str, str]:
        creds = self.settings.credentials()
        if creds is None:
            if endpoint.auth:
                _ = self.settings.require_credentials()
            return {}
        headers: dict[str, str] = {"X-API-Key": creds.api_key}
        if creds.cf_access_client_id and creds.cf_access_client_secret:
            headers["CF-Access-Client-Id"] = creds.cf_access_client_id
            headers["CF-Access-Client-Secret"] = creds.cf_access_client_secret
        return headers

    def _request_timeout(
        self, label: str, endpoint: Endpoint, deadline: float | None, *, maybe_sent: bool
    ) -> float:
        timeout = self.settings.upload_timeout if endpoint.upload else self.settings.http_timeout
        if deadline is None:
            return timeout
        remaining = deadline - _monotonic()
        if remaining <= 0:
            if not maybe_sent:
                raise NotSent(f"No time left to send {label}.", hint="Try again.")
            raise DeadlineExceeded(f"No time left for {label}.", hint="Try again.")
        return max(min(timeout, remaining), 0.5)

    async def _pause(
        self, label: str, deadline: float | None, seconds: float, *, maybe_sent: bool
    ) -> None:
        if deadline is not None and _monotonic() + seconds > deadline:
            if not maybe_sent:
                raise NotSent(
                    f"{label} was not sent before the tool call budget expired.",
                    hint="Nothing was sent. Try again.",
                )
            raise DeadlineExceeded(
                f"{label} did not answer before the tool call budget expired.",
                hint="Check state before retrying.",
            )
        await _sleep(seconds)

    def _raise_transport_error(
        self,
        label: str,
        endpoint: Endpoint,
        exc: Exception,
        *,
        reached: bool,
        maybe_sent: bool,
    ) -> None:
        if reached and not endpoint.safe_to_retry:
            raise NetworkError(
                f"{label} failed after it may have reached Everesteer ({type(exc).__name__}).",
                hint="Outcome is unknown. Check account state before repeating.",
            ) from exc
        err_cls = NetworkError if maybe_sent else NotSent
        raise err_cls(
            f"Could not reach Everesteer for {label} ({type(exc).__name__}).",
            hint="Verify network connectivity and EVERESTEER_BASE_URL, then retry.",
        ) from exc

    @staticmethod
    def _encode_payload(
        raw_body: bytes | None,
        body: Any,
        files: Mapping[str, Any] | None,
        data: Mapping[str, Any] | None,
    ) -> bytes | None:
        if files is not None or data is not None:
            return None
        if raw_body is not None:
            return raw_body
        return None if body is None else encode_body(body)

    async def _send(
        self,
        endpoint: Endpoint,
        path: str,
        *,
        query: list[tuple[str, str]],
        content: bytes | None,
        files: Mapping[str, Any] | None,
        data: Mapping[str, Any] | None,
        headers: dict[str, str],
        seconds: float,
        stream: bool,
    ) -> httpx2.Response:
        """Send one attempt. With ``stream``, only a 2xx body is left unread for the caller."""
        request = self._http.build_request(
            endpoint.method,
            path,
            params=cast(Any, query) or None,
            content=content,
            files=cast(Any, files),
            data=cast(Any, data),
            headers=headers,
            timeout=seconds,
        )
        response = await self._http.send(request, stream=stream)
        if stream and not response.is_success:
            try:
                await response.aread()
            finally:
                await response.aclose()
        return response

    async def request(
        self,
        endpoint: Endpoint,
        *,
        path_args: dict[str, str] | None = None,
        params: Params = None,
        body: Any = None,
        raw_body: bytes | None = None,
        files: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        deadline: float | None = None,
        expect: tuple[int, ...] = (),
        allow_redirect: bool = False,
        stream: bool = False,
    ) -> httpx2.Response:
        """Execute one API request. Never auto-retries mutating operations.

        With ``stream=True`` a 2xx body is left unread and the caller must close the response.
        """
        if _coverage_hook is not None:
            _coverage_hook(endpoint)
        path = endpoint.render(path_args)
        content = self._encode_payload(raw_body, body, files, data)
        policy = RETRY_SAFE if endpoint.safe_to_retry else RETRY_UNSAFE
        query = _query(params)
        rate_limited = server_failures = network_failures = 0
        label = f"{endpoint.method} {endpoint.path}"
        maybe_sent = False

        while True:
            request_headers = dict(headers or {}) | self._auth_headers(endpoint)
            if content is not None:
                request_headers.setdefault("Content-Type", "application/json")
            timeout = self._request_timeout(label, endpoint, deadline, maybe_sent=maybe_sent)

            try:
                response = await self._send(
                    endpoint,
                    path,
                    query=query,
                    content=content,
                    files=files,
                    data=data,
                    headers=request_headers,
                    seconds=timeout,
                    stream=stream,
                )
            except (httpx2.TransportError, httpx2.DecodingError) as exc:
                reached = not isinstance(exc, httpx2.ConnectError | httpx2.ConnectTimeout)
                maybe_sent = maybe_sent or reached
                network_failures += 1
                if network_failures >= policy.network_attempts or (
                    reached and not policy.retry_ambiguous_timeouts
                ):
                    self._raise_transport_error(
                        label, endpoint, exc, reached=reached, maybe_sent=maybe_sent
                    )
                await self._pause(label, deadline, backoff(network_failures), maybe_sent=maybe_sent)
                continue

            maybe_sent = True
            status = response.status_code
            retry_after = parse_retry_after(response)
            if status == 429:
                rate_limited += 1
                if rate_limited >= policy.rate_limit_attempts or not endpoint.safe_to_retry:
                    raise RateLimited(
                        f"Everesteer rate-limited {label}.",
                        retry_after=retry_after,
                        status=status,
                        detail=parse_json(response),
                    )
                wait_s = clamp(retry_after, 1.0, 30.0) if retry_after else backoff(rate_limited)
                await self._pause(label, deadline, wait_s, maybe_sent=maybe_sent)
                continue
            if status in (500, 502, 503, 504):
                server_failures += 1
                if server_failures >= policy.server_attempts:
                    self._raise_for_status(label, response, endpoint)
                wait_s = clamp(retry_after, 0.5, 15.0) if retry_after else backoff(server_failures)
                await self._pause(label, deadline, wait_s, maybe_sent=maybe_sent)
                continue
            if allow_redirect and self._followable_redirect(response):
                return response
            if status >= 300 and status not in expect:
                self._raise_for_status(label, response, endpoint)
            return response

    @staticmethod
    def _followable_redirect(response: httpx2.Response) -> bool:
        """Return whether a redirect may be followed, i.e. it is not a Cloudflare Access login."""
        return response.has_redirect_location and not _is_cloudflare_access_challenge(
            response.status_code, response.text, response.headers
        )

    def _raise_for_status(self, label: str, response: httpx2.Response, endpoint: Endpoint) -> None:
        status = response.status_code
        body = parse_json(response)
        text = response.text or ""
        if body is None and _is_cloudflare_access_challenge(status, text, response.headers):
            raise CloudflareAccessError(
                f"{label} received an HTML challenge instead of API JSON "
                + f"(host: {self.settings.resolved_base_url}).",
                status=status,
                detail=text[:200],
                hint="If targeting a Cloudflare Access staging host, set CF_ACCESS_CLIENT_ID and "
                + "CF_ACCESS_CLIENT_SECRET, and ensure EVERESTEER_BASE_URL is "
                + "https://api.everesteer.ai.",
            )

        message = _extract_message(body, text)
        code = _extract_error_code(body)
        if code == "email_verification_required" or "email_verification_required" in message:
            raise EmailVerificationRequired(
                f"Email verification required before {label}: {message}",
                status=status,
                detail=body,
                hint="Call `resend_email_verification` and verify your email address first.",
            )
        if status == 401:
            raise AuthError(
                f"Everesteer rejected authentication for {label}: {message}",
                status=status,
                detail=body,
                hint="Check EVERESTEER_API_KEY / `mcp-server-everesteer auth`.",
            )
        if status == 403:
            raise PermissionDenied(
                f"Everesteer denied access for {label}: {message}",
                status=status,
                detail=body,
                hint="Your API key scope (hackathon vs full) or model ownership may restrict "
                + "this route.",
            )
        if status == 404:
            raise NotFound(f"Not found for {label}: {message}", status=status, detail=body)
        if status == 409:
            raise ConflictError(f"Conflict for {label}: {message}", status=status, detail=body)
        if 400 <= status < 500:
            raise InvalidRequest(
                f"Everesteer rejected {label} ({status}): {message}", status=status, detail=body
            )
        writes = not endpoint.safe_to_retry
        raise ServerError(
            f"{label} returned {status}: {message}",
            status=status,
            detail=body,
            hint=(
                "Outcome is unknown. Check account state before repeating this write."
                if writes
                else "Retry later."
            ),
        )

    async def json(self, endpoint: Endpoint, **kwargs: Any) -> Any:
        response = await self.request(endpoint, **kwargs)
        parsed = parse_json(response)
        if parsed is None and response.content.strip():
            text = response.text or ""
            if _looks_like_cloudflare_html(text):
                raise CloudflareAccessError(
                    f"Expected JSON from {endpoint.path} but received HTML challenge.",
                    status=response.status_code,
                    detail=text[:200],
                    hint="Set CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET or check "
                    + "EVERESTEER_BASE_URL.",
                )
            raise ServerError(
                f"Expected JSON from {endpoint.path}, got non-JSON body: {text[:200]}",
                status=response.status_code,
            )
        return parsed

    def download_target(self, output_path: str, *, server_supplied: bool = False) -> Path:
        """Resolve a destination inside the EVERESTEER_DOWNLOAD_DIR directories (default: cwd)."""
        return confine_output_path(
            output_path, self.settings.download_roots, server_supplied=server_supplied
        )

    async def download_presigned(
        self,
        endpoint: Endpoint,
        output_path: str,
        *,
        kind: DownloadKind,
        path_args: dict[str, str] | None = None,
        params: Params = None,
        deadline: float | None = None,
    ) -> tuple[str, int]:
        """Download via `?presigned=true`, following a 302 with a credential-free CDN request.

        The body streams into a temporary file that replaces `output_path` only once it is
        complete and looks like `kind`, so a failed download never damages the previous file.
        """
        target = self.download_target(output_path)
        query_params = dict(params or {})
        query_params["presigned"] = "true"
        response = await self.request(
            endpoint,
            path_args=path_args,
            params=query_params,
            deadline=deadline,
            allow_redirect=True,
            stream=True,
        )
        try:
            if not response.has_redirect_location:
                label = f"{endpoint.method} {endpoint.path}"
                return await self._save(response, target, kind=kind, label=label)
            location = str(response.url.join(response.headers["location"]))
        finally:
            await response.aclose()
        return await self._download_url(location, target, kind=kind, deadline=deadline)

    async def download_url_to_file(
        self,
        url: str,
        output_path: str,
        *,
        kind: DownloadKind,
        server_supplied: bool = False,
        deadline: float | None = None,
    ) -> tuple[str, int]:
        """Download a presigned URL into a confined path without sending API credentials."""
        target = self.download_target(output_path, server_supplied=server_supplied)
        return await self._download_url(url, target, kind=kind, deadline=deadline)

    async def _download_url(
        self, url: str, target: Path, *, kind: DownloadKind, deadline: float | None
    ) -> tuple[str, int]:
        timeout = self.settings.upload_timeout
        if deadline is not None:
            timeout = max(min(timeout, deadline - _monotonic()), 0.5)
        try:
            async with self._cdn_http.stream("GET", url, timeout=timeout) as response:
                if _reached_cloudflare_access(response):
                    raise CloudflareAccessError(
                        "Storage download was redirected to a Cloudflare Access login; "
                        + "nothing was saved and any previous file was kept.",
                        status=response.status_code,
                        hint="Regenerate the presigned URL, or check EVERESTEER_BASE_URL.",
                    )
                if response.status_code >= 400:
                    await response.aread()
                    raise ServerError(
                        f"Storage download returned HTTP {response.status_code}.",
                        status=response.status_code,
                        detail=response.text[:200],
                    )
                return await self._save(response, target, kind=kind, label="Storage download")
        except httpx2.RequestError as exc:
            raise NetworkError(
                f"Failed to download artifact from storage ({type(exc).__name__}).",
                hint="Retry the download or regenerate the presigned URL if it expired.",
            ) from exc

    @staticmethod
    async def _save(
        response: httpx2.Response, target: Path, *, kind: DownloadKind, label: str
    ) -> tuple[str, int]:
        """Stream a response body into `target` atomically after checking it is a `kind` file."""
        problem = media_problem(kind, response.headers.get("content-type", ""))
        if problem is not None:
            preview = (await response.aread())[:200].decode("utf-8", "replace")
            raise _invalid_download(label, problem, preview, response.status_code)
        staged = await asyncio.to_thread(StagedDownload.open, target)
        try:
            async for chunk in response.aiter_bytes(_CHUNK_BYTES):
                await asyncio.to_thread(staged.write, chunk)
            problem = body_problem(kind, staged.head, staged.tail, staged.size)
            if problem is not None:
                raise _invalid_download(label, problem, "", response.status_code)
            written = await asyncio.to_thread(staged.publish)
        except httpx2.RequestError as exc:
            staged.discard()
            raise NetworkError(
                f"{label} was interrupted ({type(exc).__name__}); any previous file was kept.",
                hint="Retry the download.",
            ) from exc
        except BaseException:
            staged.discard()
            raise
        return str(target), written

    async def s3_upload(
        self,
        recipe: Mapping[str, Any],
        filename: str,
        data: bytes,
        *,
        deadline: float | None = None,
    ) -> None:
        """Upload raw object bytes straight to S3 using a server-minted PUT or POST recipe."""
        method = str(recipe.get("method", "POST")).upper()
        timeout = self.settings.upload_timeout
        if deadline is not None:
            timeout = max(min(timeout, deadline - _monotonic()), 0.5)
        try:
            if method == "PUT":
                headers = dict(cast(Mapping[str, str], recipe.get("headers") or {}))
                resp = await self._cdn_http.put(
                    str(recipe["url"]), content=data, headers=headers, timeout=timeout
                )
            else:
                fields = dict(cast(Mapping[str, str], recipe.get("fields") or {}))
                resp = await self._cdn_http.post(
                    str(recipe["url"]),
                    data=fields,
                    files={"file": (filename, data, "application/octet-stream")},
                    timeout=timeout,
                )
        except httpx2.TransportError as exc:
            raise NetworkError(
                f"Direct storage upload failed ({type(exc).__name__}).",
                hint="Retry the upload.",
            ) from exc
        if resp.status_code not in (200, 201, 204):
            raise ServerError(
                f"Direct storage upload failed with HTTP {resp.status_code}: {resp.text[:300]}",
                status=resp.status_code,
            )

    @staticmethod
    async def read_local_file(file_path: str) -> tuple[bytes, str, str]:
        """Read file bytes off the event loop and return ``(data, filename, sha256_hex)``."""
        data, name = await asyncio.to_thread(read_file_bytes, file_path)
        digest = hashlib.sha256(data).hexdigest()
        return data, os.path.basename(name), digest

    def resolve_download_path(self, output_path: str | None, default_name: str) -> Path:
        return (
            self.download_target(output_path)
            if output_path
            else self.download_target(default_name, server_supplied=True)
        )
