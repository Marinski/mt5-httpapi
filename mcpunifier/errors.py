"""Sentinel errors for the MCP unifier.

Typed so a tool can raise something precise and the MCP layer can turn it into
a message the caller can act on, rather than leaking an httpx exception.
"""


class UnifierError(Exception):
    """Base class for every error raised by this service."""


class UnknownTerminal(UnifierError):
    """The requested broker/account/instance is not configured.

    Carries the configured terminal list so the caller is told what it could
    have asked for instead of only being refused.
    """

    def __init__(self, terminal: str, known: list[str]) -> None:
        self.terminal = terminal
        self.known = known
        known_text = ", ".join(known) if known else "(none configured)"
        super().__init__(f"unknown terminal '{terminal}'; configured: {known_text}")


class TerminalUnreachable(UnifierError):
    """A configured terminal did not answer.

    Raised per call so one terminal being down never looks like a fault in the
    unifier and never takes the other terminals with it.
    """

    def __init__(self, terminal: str, reason: str) -> None:
        self.terminal = terminal
        self.reason = reason
        super().__init__(f"terminal '{terminal}' unreachable: {reason}")


class TerminalRejected(UnifierError):
    """A terminal answered with a non-2xx status.

    The upstream body is preserved because mt5api's own error payloads carry
    the useful part: MT5 retcodes and validation messages.
    """

    def __init__(self, terminal: str, status: int, body: str) -> None:
        self.terminal = terminal
        self.status = status
        self.body = body
        super().__init__(f"terminal '{terminal}' returned HTTP {status}: {body}")


class ToolArgumentError(UnifierError):
    """A tool was called with arguments it cannot turn into a request, such
    as malformed base64 or two mutually exclusive options. Raised before any
    terminal is contacted."""


class ChartctlDisabled(UnifierError):
    """A Chart Deployments tool was called on a terminal that does not serve
    the chartctl routes.

    The terminal answers those paths with a plain 404 when chartctl is off,
    which on its own reads like a missing deployment rather than a disabled
    feature.
    """

    def __init__(self, terminal: str, configured: bool) -> None:
        self.terminal = terminal
        self.configured = configured
        if configured:
            message = (
                f"terminal '{terminal}' does not serve the Chart Deployments "
                "routes although config.yaml enables chartctl for it; its API "
                "process probably started before that change, so restart the stack"
            )
        else:
            message = (
                f"Chart Deployments are not enabled on terminal '{terminal}': set "
                "chartctl.enabled: true in config.yaml (live-mode terminals only, "
                "and the terminal entry must not set chartctl: false), then "
                "restart the stack"
            )
        super().__init__(message)


class FilesDisabled(UnifierError):
    """A file API tool was called on a terminal that does not serve the
    /files routes, which then answer a plain 404 that reads like a missing
    file."""

    def __init__(self, terminal: str, configured: bool) -> None:
        self.terminal = terminal
        self.configured = configured
        if configured:
            message = (
                f"terminal '{terminal}' does not serve the file API although "
                "config.yaml enables it; its API process probably started "
                "before that change, so restart the stack"
            )
        else:
            message = (
                f"the file API is not enabled on terminal '{terminal}': set "
                "files.enabled: true in config.yaml (the terminal entry must "
                "not set files: false), then restart the stack"
            )
        super().__init__(message)


class UnexpectedContent(UnifierError):
    """A terminal answered 2xx with a body of the wrong media type."""

    def __init__(self, terminal: str, expected: str, actual: str) -> None:
        self.terminal = terminal
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"terminal '{terminal}' returned '{actual or 'no content type'}', "
            f"expected '{expected}'"
        )


class ConfigError(UnifierError):
    """The configuration could not be read or contains no usable terminals."""
