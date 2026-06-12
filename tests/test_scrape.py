"""Tests for the scrape layer: HTML text extraction, caching behaviour,
and truncation. No network — the ladder's fetch step is stubbed."""

from scanner.scrape import Scraper, html_to_text, url_cache_key


SAMPLE_HTML = """
<html>
  <head><title>Ignored</title><style>body {color: red}</style></head>
  <body>
    <script>var ignored = true;</script>
    <h1>Acme Youth Enterprise Fund</h1>
    <p>Founded in 2015, the fund operates in 14 counties across Kenya.</p>
    <nav>Home About Contact</nav>
  </body>
</html>
"""


def test_html_to_text_keeps_visible_text_only():
    text = html_to_text(SAMPLE_HTML)
    assert "Acme Youth Enterprise Fund" in text
    assert "Founded in 2015" in text
    assert "var ignored" not in text       # script stripped
    assert "color: red" not in text        # style stripped


def test_cache_roundtrip_avoids_refetch(tmp_path, monkeypatch):
    """Second fetch of the same URL must come from cache, not the network."""
    scraper = Scraper(cache_dir=tmp_path)
    calls = {"count": 0}

    def fake_fetch(url):
        calls["count"] += 1
        return "scraped", "Some page text " * 50

    monkeypatch.setattr(scraper, "_fetch_uncached", fake_fetch)

    first = scraper.fetch("https://example.org/page")
    second = scraper.fetch("https://example.org/page")
    assert first == second
    assert calls["count"] == 1  # network hit exactly once


def test_failures_are_cached_too(tmp_path, monkeypatch):
    """An unreachable URL is not retried endlessly within a run."""
    scraper = Scraper(cache_dir=tmp_path)
    calls = {"count": 0}

    def fake_fetch(url):
        calls["count"] += 1
        return "unreachable", None

    monkeypatch.setattr(scraper, "_fetch_uncached", fake_fetch)

    assert scraper.fetch("https://dead.example.org") == ("unreachable", None)
    assert scraper.fetch("https://dead.example.org") == ("unreachable", None)
    assert calls["count"] == 1


def test_truncation_caps_page_text(tmp_path, monkeypatch):
    """Pages are truncated to max_chars before they hit LLM input."""
    scraper = Scraper(cache_dir=tmp_path, max_chars=100)

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/plain"}
        text = "x" * 10_000
        content = b""

        def raise_for_status(self):
            return None

    monkeypatch.setattr("scanner.scrape.httpx.get", lambda *a, **k: FakeResponse())
    status, text = scraper.fetch("https://example.org/long")
    assert status == "scraped"
    assert len(text) == 100


def test_tiny_pages_count_as_unreachable(tmp_path, monkeypatch):
    """A near-empty page (bot wall / JS shell) with the reader disabled is
    honestly 'unreachable', not a fake success."""
    scraper = Scraper(cache_dir=tmp_path, reader_enabled=False)

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "text/html"}
        text = "<html><body>403</body></html>"
        content = b""

        def raise_for_status(self):
            return None

    monkeypatch.setattr("scanner.scrape.httpx.get", lambda *a, **k: FakeResponse())
    status, text = scraper.fetch("https://blocked.example.org")
    assert status == "unreachable"
    assert text is None


def test_reader_fallback_recovers_js_shell_page(tmp_path, monkeypatch):
    """When the plain GET returns an empty JS shell, the reader tier renders
    the page and recovers its text — turning an 'unreachable' into a
    'scraped'."""
    scraper = Scraper(cache_dir=tmp_path, reader_enabled=True)

    class ShellResponse:
        status_code = 200
        headers = {"content-type": "text/html"}
        text = "<html><body><div id='root'></div></body></html>"  # JS shell, no content
        content = b""

        def raise_for_status(self):
            return None

    class ReaderResponse:
        status_code = 200
        headers = {"content-type": "text/plain"}
        text = "GrowthAfrica is a Nairobi-based accelerator " * 20  # rendered content
        content = b""

        def raise_for_status(self):
            return None

    def fake_get(url, *a, **k):
        # The reader URL is the target prefixed with the reader host.
        return ReaderResponse() if url.startswith("https://r.jina.ai/") else ShellResponse()

    monkeypatch.setattr("scanner.scrape.httpx.get", fake_get)
    status, text = scraper.fetch("https://growthafrica.com")
    assert status == "scraped"
    assert "GrowthAfrica" in text


def test_reader_disabled_gives_up_on_blocked_page(tmp_path, monkeypatch):
    """With the reader off, a hard 403 stays unreachable (no second attempt)."""
    import httpx

    scraper = Scraper(cache_dir=tmp_path, reader_enabled=False)

    def raising_get(*a, **k):
        raise httpx.HTTPError("403 Forbidden")

    monkeypatch.setattr("scanner.scrape.httpx.get", raising_get)
    status, text = scraper.fetch("https://blocked.example.org")
    assert status == "unreachable"
    assert text is None


def test_cache_key_is_stable_and_filename_safe():
    key1 = url_cache_key("https://example.org/page?a=1")
    key2 = url_cache_key("https://example.org/page?a=1")
    assert key1 == key2
    assert key1.isalnum()
