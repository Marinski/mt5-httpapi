"""HTTP dispatch from the unifier to one terminal's mt5api process.

Calls go straight to the terminal's own port rather than back through nginx:
the port is already in the routing table, and one less hop means one less
thing that has to agree with config.yaml.
"""

import logging
from typing import Any

import httpx

from mcpunifier.config import Settings, Terminal
from mcpunifier.constants import (
    BEARER_PREFIX,
    HEADER_AUTHORIZATION,
    HEADER_CONTENT_TYPE,
    HEADER_REQUEST_ID,
)
from mcpunifier.errors import TerminalRejected, TerminalUnreachable
from mcpunifier.logging import get_scope

logger = logging.getLogger(__name__)

_MAX_ERROR_BODY_CHARS = 2000


class TerminalClient:
    """Sends REST calls to whichever terminal a tool names.

    One shared httpx client keeps connections pooled across terminals; the
    per-call target is the only thing that changes.
    """

    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._http = httpx.AsyncClient(
            timeout=settings.request_timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        """Close the pooled connections. Called from the app lifespan."""
        await self._http.aclose()

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._settings.api_token:
            headers[HEADER_AUTHORIZATION] = f"{BEARER_PREFIX}{self._settings.api_token}"

        # Carry the inbound request id to the terminal so one MCP call can be
        # followed across both services' logs.
        request_id = get_scope().get("request_id", "")
        if request_id:
            headers[HEADER_REQUEST_ID] = str(request_id)
        return headers

    async def call(
        self,
        terminal: Terminal,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        files: dict[str, tuple[str, bytes]] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Perform one REST call against ``terminal`` and return its JSON body.

        ``files`` maps a form field to (filename, bytes) and sends a
        multipart form instead of a JSON ``body``. ``timeout`` overrides the
        configured request timeout for a call known to run longer.

        Raises TerminalUnreachable when the terminal does not answer and
        TerminalRejected when it answers non-2xx, so a single dead terminal
        surfaces as one failed tool call rather than a broken service.
        """
        response = await self._send(
            terminal,
            method,
            path,
            query,
            body,
            files,
            timeout,
        )
        try:
            payload = response.json()
        except ValueError as err:
            raise TerminalRejected(
                terminal.key,
                response.status_code,
                "response body is not JSON",
            ) from err

        # mt5api returns objects everywhere, but a bare list or scalar from a
        # future route must not break the tool contract.
        if isinstance(payload, dict):
            return payload
        return {"result": payload}

    async def call_bytes(
        self,
        terminal: Terminal,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
    ) -> tuple[str, bytes]:
        """Like ``call`` for a binary response: (content type, raw body)."""
        response = await self._send(terminal, method, path, query, None, None, None)
        return response.headers.get(HEADER_CONTENT_TYPE, ""), response.content

    async def _send(
        self,
        terminal: Terminal,
        method: str,
        path: str,
        query: dict[str, Any] | None,
        body: dict[str, Any] | None,
        files: dict[str, tuple[str, bytes]] | None,
        timeout: float | None,
    ) -> httpx.Response:
        """Send one request and return its 2xx response, or raise."""
        url = f"{terminal.base_url(self._settings.mt5_host)}{path}"

        # Route only. Query, body and files can carry order parameters or EA
        # binaries.
        logger.info(
            "dispatching to terminal",
            extra={"terminal": terminal.key, "http_method": method, "path": path},
        )

        try:
            response = await self._http.request(
                method,
                url,
                params=query,
                json=body if not files else None,
                files=files,
                headers=self._headers(),
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.HTTPError as err:
            logger.warning(
                "terminal did not answer",
                extra={
                    "terminal": terminal.key,
                    "http_method": method,
                    "path": path,
                    "reason": "terminal_unreachable",
                },
                exc_info=err,
            )
            raise TerminalUnreachable(terminal.key, str(err)) from err

        if response.status_code >= httpx.codes.BAD_REQUEST:
            body_text = response.text[:_MAX_ERROR_BODY_CHARS]
            logger.warning(
                "terminal rejected the call",
                extra={
                    "terminal": terminal.key,
                    "http_method": method,
                    "path": path,
                    "status": response.status_code,
                    "reason": "terminal_rejected",
                },
            )
            raise TerminalRejected(terminal.key, response.status_code, body_text)

        logger.debug(
            "terminal answered",
            extra={
                "terminal": terminal.key,
                "http_method": method,
                "path": path,
                "status": response.status_code,
            },
        )
        return response
