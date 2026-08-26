"""Runtime resolution of the shared HTTP User-Agent."""

import re
import threading
from collections.abc import Callable

import httpx

_LATEST_STABLE_VERSION_URL = (
    "https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_STABLE"
)
_MAX_VERSION_BYTES = 64
_LOOKUP_TIMEOUT_SECONDS = 2.0
_FALLBACK_MAJOR = "152"
_VERSION_PATTERN = re.compile(rb"(?P<major>[1-9][0-9]{0,3})\.[0-9]+\.[0-9]+\.[0-9]+")
_USER_AGENT_TEMPLATE = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
)


class ChromiumUserAgentResolver:  # pylint: disable=too-few-public-methods
    """Resolve and cache one current Stable Chromium UA without becoming required."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        override: str | None,
        check_stop: Callable[[], None],
        report: Callable[[str], None],
    ) -> None:
        self._client = client
        self._check_stop = check_stop
        self._report = report
        self._resolved = override
        self._lock = threading.Lock()

    def resolve(self) -> str:
        """Return the process-cached User-Agent, resolving it on first use."""

        if self._resolved is not None:
            return self._resolved
        with self._lock:
            if self._resolved is None:
                self._resolved = self._resolve_once()
            return self._resolved

    def _resolve_once(self) -> str:
        try:
            major = self._latest_stable_major()
        except (httpx.HTTPError, UnicodeError, ValueError) as error:
            fallback = _USER_AGENT_TEMPLATE.format(major=_FALLBACK_MAJOR)
            self._report(
                "Unable to resolve the current Stable Chromium User-Agent "
                f"({type(error).__name__}); using bundled Chrome/"
                f"{_FALLBACK_MAJOR}.0.0.0 fallback"
            )
            return fallback
        return _USER_AGENT_TEMPLATE.format(major=major)

    def _latest_stable_major(self) -> str:
        self._check_stop()
        timeout = httpx.Timeout(_LOOKUP_TIMEOUT_SECONDS)
        with self._client.stream(
            "GET",
            _LATEST_STABLE_VERSION_URL,
            headers={
                "Accept": "text/plain",
            },
            timeout=timeout,
        ) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                self._check_stop()
                if len(body) + len(chunk) > _MAX_VERSION_BYTES:
                    raise ValueError("Stable Chromium version response was too large")
                body.extend(chunk)

        match = _VERSION_PATTERN.fullmatch(bytes(body).strip())
        if match is None:
            raise ValueError("Stable Chromium version response was invalid")
        return match.group("major").decode("ascii")
