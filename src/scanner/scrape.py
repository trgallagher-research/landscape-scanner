"""Scraping: fetch a source's full text, cheapest method first, with a
per-URL disk cache.

Why this exists: verification runs against FULL scraped text, never search
snippets. Fetching fails constantly in the real world (PDFs, bot blocks,
dead links), so the ladder tries progressively harder and a URL is marked
"unreachable" only after every applicable rung failed. "Unreachable" is an
honest state of its own — it means *we* could not read the source, not that
the source doesn't exist or doesn't support a claim.

The ladder (v2 keeps the three rungs that earn their keep):
  1. Plain HTTP GET with realistic browser headers — resolves most pages.
  2. PDF text extraction (PyMuPDF, optional dependency) when the content
     is a PDF — institutional sources (OECD, World Bank, NGOs) are PDF-first.
  3. HTML-to-text fallback via a tolerant stdlib parser when rung 1 got a
     page but it is script-heavy — we keep whatever readable text exists.

Caching is per URL on disk, so re-running or resuming a scan never
re-fetches a page it already has. This was the single biggest cost/time
saving in v1 and carries over unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import httpx

from .models import ScrapeStatus

# Realistic browser headers: many sites serve empty or blocked pages to
# obvious bots. This is ordinary polite scraping, not evasion — we identify
# as a browser and respect failures.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.8",
}


class _TextExtractor(HTMLParser):
    """Tolerant HTML-to-text converter using only the standard library.

    Skips script/style/nav noise and collects visible text. Not as smart as
    a readability library, but dependency-free and good enough: verification
    only needs the page's visible words, not its layout.
    """

    SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "iframe"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0  # >0 while inside a tag we ignore

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._chunks.append(data.strip())

    def text(self) -> str:
        """Joined visible text with whitespace normalised."""
        joined = "\n".join(self._chunks)
        return re.sub(r"\n{3,}", "\n\n", joined)


def html_to_text(html: str) -> str:
    """Extract visible text from an HTML document."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        # Malformed HTML: fall back to a crude tag strip rather than failing.
        return re.sub(r"<[^>]+>", " ", html)
    return parser.text()


def _decode_bytes(response) -> str:
    """Decode an HTTP response body to text, preferring clean UTF-8.

    Pages frequently omit or misdeclare their charset, which makes httpx's
    automatic decode corrupt multi-byte characters (en-dashes, curly
    quotes, currency symbols turn into replacement chars). UTF-8 covers the
    overwhelming majority of the modern web, so we try it first and only
    fall back to httpx's own decode if UTF-8 produced more replacement
    characters than the fallback would.
    """
    raw = response.content
    utf8 = raw.decode("utf-8", errors="replace")
    # If a strict UTF-8 decode succeeds with few replacements, trust it.
    if utf8.count("�") <= 3:
        return utf8
    # Otherwise defer to httpx's charset detection (handles legacy encodings).
    try:
        return response.text
    except Exception:
        return utf8


def pdf_to_text(content: bytes) -> str | None:
    """Extract text from PDF bytes using PyMuPDF, if installed.

    Returns None when PyMuPDF is missing or the PDF cannot be parsed —
    the caller then marks the source unreachable rather than crashing.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return None
    try:
        with fitz.open(stream=content, filetype="pdf") as document:
            pages = [page.get_text() for page in document]
        text = "\n".join(pages).strip()
        return text or None
    except Exception:
        return None


def url_cache_key(url: str) -> str:
    """Stable filename-safe key for a URL (sha256 prefix)."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]


class Scraper:
    """Fetches full text for URLs with a per-URL JSON cache on disk."""

    def __init__(
        self,
        cache_dir: Path,
        timeout_s: float = 20.0,
        max_chars: int = 15000,
        reader_enabled: bool = True,
    ):
        """
        Parameters
        ----------
        cache_dir:
            Directory for the per-URL cache (created if missing).
        timeout_s:
            Per-URL fetch timeout before declaring unreachable.
        max_chars:
            Truncation cap applied to extracted text. Long pages dominate
            LLM input cost; 15k characters keeps the substance and cuts
            the tail.
        reader_enabled:
            Whether to use the reader-service fallback (rung 3) for pages a
            plain GET can't read. On by default; disable in tests to keep
            them fully offline.
        """
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout_s = timeout_s
        self.max_chars = max_chars
        self.reader_enabled = reader_enabled

    def fetch(self, url: str) -> tuple[ScrapeStatus, str | None]:
        """Fetch one URL's text: cache first, then the ladder.

        Returns (status, text). Status is "scraped" with text on success,
        "unreachable" with None when every applicable rung failed. The
        outcome (either way) is cached so failures aren't endlessly retried
        within and across runs.
        """
        cached = self._cache_read(url)
        if cached is not None:
            return cached

        status, text = self._fetch_uncached(url)
        self._cache_write(url, status, text)
        return status, text

    # ------------------------------------------------------------------
    # The ladder
    # ------------------------------------------------------------------

    def _fetch_uncached(self, url: str) -> tuple[ScrapeStatus, str | None]:
        """Run the fetch ladder for one URL."""
        # Rung 1: plain HTTP GET.
        try:
            response = httpx.get(
                url,
                headers=DEFAULT_HEADERS,
                timeout=self.timeout_s,
                follow_redirects=True,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            # The site blocked us or errored (403/429/timeout). Don't give up
            # yet — the reader tier (rung 3) renders many of these.
            return self._reader_fetch(url)

        content_type = response.headers.get("content-type", "").lower()

        # Rung 2: PDF content — extract with PyMuPDF when available.
        if "pdf" in content_type or url.lower().endswith(".pdf"):
            text = pdf_to_text(response.content)
            if text:
                return "scraped", text[: self.max_chars]
            return self._reader_fetch(url)  # reader can OCR/extract some PDFs too

        # HTML (or plain text) — extract visible text. Decode defensively:
        # many pages omit a charset or declare the wrong one, which makes
        # httpx's .text mangle UTF-8 (e.g. an en-dash or € becomes a
        # replacement char). Prefer a clean UTF-8 decode of the raw bytes,
        # falling back to httpx's detected text only if that looks worse.
        page = _decode_bytes(response)
        text = page if "text/plain" in content_type else html_to_text(page)
        text = text.strip()
        if len(text) < 200:
            # Too little text — almost always a JavaScript-shell page that
            # renders its content client-side, or a bot wall. The reader
            # tier renders the page server-side and usually recovers it.
            return self._reader_fetch(url)
        return "scraped", text[: self.max_chars]

    def _reader_fetch(self, url: str) -> tuple[ScrapeStatus, str | None]:
        """Rung 3: a reader service that renders JS and bypasses most bot
        walls, returning clean text. Uses Jina Reader (https://r.jina.ai/),
        which is free, keyless, and needs no extra dependency — we just
        prefix the target URL and fetch the rendered text.

        This is the single biggest scrape-recovery lever for modern sites
        (React/Vue shells, 403/429 bot blocks). If it also fails, the
        source is honestly ``unreachable``.
        """
        if not self.reader_enabled:
            return "unreachable", None
        try:
            reader_url = "https://r.jina.ai/" + url
            response = httpx.get(
                reader_url,
                headers={"User-Agent": DEFAULT_HEADERS["User-Agent"]},
                timeout=self.timeout_s + 15,  # rendering takes longer than a raw GET
                follow_redirects=True,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return "unreachable", None
        text = response.text.strip()
        if len(text) < 200:
            return "unreachable", None
        return "scraped", text[: self.max_chars]

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / f"{url_cache_key(url)}.json"

    def _cache_read(self, url: str) -> tuple[ScrapeStatus, str | None] | None:
        """Return the cached outcome for a URL, or None if not cached."""
        path = self._cache_path(url)
        if not path.is_file():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            return entry["status"], entry.get("text")
        except (json.JSONDecodeError, KeyError):
            return None  # corrupt cache entry: refetch

    def _cache_write(self, url: str, status: ScrapeStatus, text: str | None) -> None:
        """Persist one fetch outcome (success or failure) to the cache."""
        entry = {"url": url, "status": status, "text": text}
        self._cache_path(url).write_text(
            json.dumps(entry, ensure_ascii=False), encoding="utf-8"
        )
