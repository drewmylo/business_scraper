"""Text helpers: strip HN HTML, pull URLs and domains, and find revenue figures in prose."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import urlparse

_TAG = re.compile(r"<[^>]+>")
_HREF = re.compile(r'href="([^"]+)"')
_BARE_URL = re.compile(r"https?://[^\s<>\"')\]]+")

# Domains that are never the business itself.
NOISE_DOMAINS = {
    "news.ycombinator.com", "github.com", "twitter.com", "x.com", "youtube.com", "youtu.be",
    "medium.com", "reddit.com", "linkedin.com", "facebook.com", "instagram.com", "imgur.com",
    "indiehackers.com", "producthunt.com", "apps.apple.com", "play.google.com", "en.wikipedia.org",
    "docs.google.com", "bit.ly", "t.co", "substack.com", "stripe.com", "web.archive.org",
}


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = text.replace("<p>", "\n\n")
    return html.unescape(_TAG.sub("", text)).strip()


def domain_of(url: str | None) -> str | None:
    if not url:
        return None
    try:
        host = urlparse(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def first_business_url(raw_html: str | None) -> str | None:
    """First link in an HN comment that is not a social or code-hosting site."""
    if not raw_html:
        return None
    candidates = [html.unescape(u) for u in _HREF.findall(raw_html)] or _BARE_URL.findall(strip_html(raw_html))
    for url in candidates:
        d = domain_of(url)
        if d and not any(d == n or d.endswith("." + n) for n in NOISE_DOMAINS):
            return url
    return None


@dataclass(frozen=True)
class Revenue:
    monthly: float
    currency: str
    text: str


_CURRENCY = {"$": "USD", "us$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP",
             "a$": "AUD", "aud": "AUD", "au$": "AUD", "c$": "CAD", "cad": "CAD"}
_MULT = {"": 1, "k": 1e3, "m": 1e6, "mm": 1e6, "thousand": 1e3, "million": 1e6}

_AMOUNT = (
    r"(?P<cur>us\$|a\$|au\$|c\$|\$|€|£)\s?(?P<num>\d[\d,]*(?:\.\d+)?)\s?(?P<mult>k|mm|m|thousand|million)?\b"
    r"|(?P<num2>\d[\d,]*(?:\.\d+)?)\s?(?P<mult2>k|mm|m|thousand|million)?\s?(?P<cur2>usd|eur|gbp|aud|cad)\b"
)
_PERIOD = (
    r"\s?(?:in\s|of\s)?(?P<period>mrr|arr|/\s?mo(?:nth)?\b|per\s+month|a\s+month|monthly|/\s?m\b|pm\b"
    r"|/\s?y(?:ea)?r\b|per\s+year|a\s+year|annually|yearly|p\.?a\.?)"
)
_REVENUE = re.compile(f"(?:{_AMOUNT}){_PERIOD}", re.IGNORECASE)

_YEARLY = re.compile(r"arr|y(ea)?r|annual|yearly|p\.?a", re.IGNORECASE)

# Ignore implausible figures for a small business (typos, funding, big-company case studies, single plan prices).
MIN_MONTHLY, MAX_MONTHLY = 50, 1_000_000


def find_revenue(text: str | None) -> Revenue | None:
    """Largest plausible recurring revenue figure in the text, normalised to per month."""
    if not text:
        return None
    best: Revenue | None = None
    for m in _REVENUE.finditer(text):
        num = m.group("num") or m.group("num2")
        mult = (m.group("mult") or m.group("mult2") or "").lower()
        cur = (m.group("cur") or m.group("cur2") or "$").lower()
        try:
            value = float(num.replace(",", "")) * _MULT[mult]
        except (ValueError, KeyError):
            continue
        if _YEARLY.search(m.group("period")):
            value /= 12
        if not MIN_MONTHLY <= value <= MAX_MONTHLY:
            continue
        rev = Revenue(round(value, 2), _CURRENCY.get(cur, "USD"), m.group(0).strip())
        if best is None or rev.monthly > best.monthly:
            best = rev
    return best
