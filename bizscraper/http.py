"""A polite HTTP session: identifies itself, retries on transient errors, throttles requests."""
from __future__ import annotations

import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = "bizscraper/0.1 (market research; +https://github.com/drewmylo/business_scraper)"


class PoliteSession(requests.Session):
    def __init__(self, min_interval: float = 0.5):
        super().__init__()
        self.headers["User-Agent"] = USER_AGENT
        retry = Retry(
            total=5,
            backoff_factor=1.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "POST"),
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.mount("https://", adapter)
        self.mount("http://", adapter)
        self.min_interval = min_interval
        self._last = 0.0

    def request(self, *args, **kwargs):
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        kwargs.setdefault("timeout", 30)
        try:
            return super().request(*args, **kwargs)
        finally:
            self._last = time.monotonic()
