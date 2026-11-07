import pytest

from bizscraper.text import domain_of, find_revenue, first_business_url, strip_html


@pytest.mark.parametrize(
    "text, monthly, currency",
    [
        ("We just hit $10k MRR after two years", 10_000, "USD"),
        ("Now at $3,500/month in revenue", 3_500, "USD"),
        ("doing about $120k ARR", 10_000, "USD"),
        ("Making €2.5k per month from it", 2_500, "EUR"),
        ("£800 a month, mostly from agencies", 800, "GBP"),
        ("revenue is 4k USD per month", 4_000, "USD"),
        ("$1.2M ARR bootstrapped", 100_000, "USD"),
        ("We do $150K / year, the parent did $30M a year", 12_500, "USD"),
        ("Started at $500 MRR, now $15k MRR", 15_000, "USD"),
    ],
)
def test_find_revenue(text, monthly, currency):
    rev = find_revenue(text)
    assert rev is not None
    assert rev.monthly == pytest.approx(monthly)
    assert rev.currency == currency


@pytest.mark.parametrize(
    "text",
    ["Plans start at $9", "We raised $2M seed", "no numbers here", "$5/mo", ""],
)
def test_find_revenue_ignores_non_revenue(text):
    assert find_revenue(text) is None


def test_domain_and_url_helpers():
    assert domain_of("https://www.Example.com/path") == "example.com"
    html = 'See <a href="https://github.com/me/x">code</a> and <a href="https://mytool.io">mytool</a>'
    assert first_business_url(html) == "https://mytool.io"
    assert strip_html("<p>Hi &amp; bye") == "Hi & bye"
