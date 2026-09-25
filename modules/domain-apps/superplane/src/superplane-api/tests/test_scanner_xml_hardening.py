"""Tests for XML parser hardening in scanner feed parsers (issue #6116).

Verifies that _parse_arxiv_response and _parse_rss_feed use defusedxml to
reject hostile XML (XXE, entity expansion, DTD processing) while continuing
to parse valid Atom and RSS feeds correctly. Exercises the actual trust
boundary: raw XML text → parsed findings list.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.scanner import _parse_arxiv_response, _parse_rss_feed

# ---------------------------------------------------------------------------
# Minimal SourceConfig stand-in — only the fields the parsers read
# ---------------------------------------------------------------------------


@dataclass
class _StubSourceConfig:
    name: str = "test"
    display_name: str = "Test"
    frequency: str = "daily"
    api_url: str = "https://example.com"
    categories: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=lambda: ["machine learning"])
    score_threshold: int = 0


# ---------------------------------------------------------------------------
# Valid feed fixtures
# ---------------------------------------------------------------------------

VALID_ATOM_FEED = """\
<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>arXiv query</title>
  <entry>
    <title>Safe Paper on Machine Learning</title>
    <summary>This paper discusses machine learning approaches.</summary>
    <id>http://arxiv.org/abs/2601.00001v1</id>
    <published>2026-01-15T00:00:00Z</published>
    <author><name>Alice Researcher</name></author>
    <category term="cs.LG"/>
  </entry>
  <entry>
    <title>Second Paper on Neural Networks</title>
    <summary>Neural network training at scale.</summary>
    <id>http://arxiv.org/abs/2601.00002v1</id>
    <published>2026-01-16T00:00:00Z</published>
    <author><name>Bob Scientist</name></author>
    <category term="cs.AI"/>
  </entry>
</feed>
"""

VALID_RSS_FEED = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Blog</title>
    <item>
      <title>Machine Learning Updates</title>
      <description>Latest machine learning news and updates.</description>
      <link>https://example.com/ml-updates</link>
    </item>
    <item>
      <title>GPU Infrastructure for ML</title>
      <description>Machine learning infrastructure on GPUs.</description>
      <link>https://example.com/gpu-ml</link>
    </item>
  </channel>
</rss>
"""

# ---------------------------------------------------------------------------
# Hostile payloads — each must be REJECTED (return empty list, no raise)
# ---------------------------------------------------------------------------

# XXE: external entity referencing a local file
XXE_PAYLOAD = """\
<?xml version="1.0"?>
<!DOCTYPE foo [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>&xxe;</title>
    <summary>Injected</summary>
    <id>http://evil.example.com</id>
    <published>2026-01-01T00:00:00Z</published>
  </entry>
</feed>
"""

# XXE variant for RSS format
XXE_RSS_PAYLOAD = """\
<?xml version="1.0"?>
<!DOCTYPE foo [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<rss version="2.0">
  <channel>
    <item>
      <title>&xxe;</title>
      <description>Machine learning injected content</description>
      <link>http://evil.example.com</link>
    </item>
  </channel>
</rss>
"""

# Entity expansion bomb (billion-laughs style)
ENTITY_BOMB_PAYLOAD = """\
<?xml version="1.0"?>
<!DOCTYPE lolz [
  <!ENTITY lol "lol">
  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
  <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
]>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>&lol4;</title>
    <summary>Bomb</summary>
    <id>http://evil.example.com</id>
    <published>2026-01-01T00:00:00Z</published>
  </entry>
</feed>
"""

# SSRF via external entity with HTTP URL
SSRF_PAYLOAD = """\
<?xml version="1.0"?>
<!DOCTYPE foo [
  <!ENTITY ssrf SYSTEM "http://169.254.169.254/latest/meta-data/">
]>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>&ssrf;</title>
    <summary>SSRF attempt</summary>
    <id>http://evil.example.com</id>
    <published>2026-01-01T00:00:00Z</published>
  </entry>
</feed>
"""


# ---------------------------------------------------------------------------
# Tests: valid feeds parse correctly
# ---------------------------------------------------------------------------


class TestValidFeedParsing:
    """Valid Atom and RSS feeds must parse into the expected findings."""

    def test_arxiv_atom_feed_returns_findings(self) -> None:
        config = _StubSourceConfig(keywords=["machine learning", "neural"])
        findings = _parse_arxiv_response(VALID_ATOM_FEED, config)
        assert len(findings) == 2
        assert findings[0]["source"] == "arxiv"
        assert findings[0]["title"] == "Safe Paper on Machine Learning"
        assert findings[0]["source_url"] == "http://arxiv.org/abs/2601.00001v1"
        assert "paper" in findings[0]["tags"]

    def test_arxiv_atom_feed_extracts_authors_and_categories(self) -> None:
        config = _StubSourceConfig()
        findings = _parse_arxiv_response(VALID_ATOM_FEED, config)
        assert findings[0]["raw_content_json"]["authors"] == ["Alice Researcher"]
        assert findings[0]["raw_content_json"]["categories"] == ["cs.LG"]

    def test_rss_feed_returns_findings(self) -> None:
        config = _StubSourceConfig(keywords=["machine learning"])
        findings = _parse_rss_feed(VALID_RSS_FEED, "test_blog", config)
        assert len(findings) == 2
        assert findings[0]["source"] == "test_blog"
        assert findings[0]["title"] == "Machine Learning Updates"
        assert findings[0]["source_url"] == "https://example.com/ml-updates"

    def test_rss_atom_variant_parsed(self) -> None:
        """RSS parser also handles Atom-format feeds (fallback path)."""
        config = _StubSourceConfig(keywords=["machine learning"])
        findings = _parse_rss_feed(VALID_ATOM_FEED, "atom_source", config)
        # Atom entries found via the .//entry fallback
        assert len(findings) >= 1


# ---------------------------------------------------------------------------
# Tests: hostile payloads rejected
# ---------------------------------------------------------------------------


class TestHostileXmlRejected:
    """Hostile XML payloads must be rejected silently — empty list, no raise."""

    def test_xxe_rejected_in_arxiv_parser(self) -> None:
        config = _StubSourceConfig()
        findings = _parse_arxiv_response(XXE_PAYLOAD, config)
        assert findings == []

    def test_xxe_rejected_in_rss_parser(self) -> None:
        config = _StubSourceConfig(keywords=["machine learning"])
        findings = _parse_rss_feed(XXE_RSS_PAYLOAD, "evil", config)
        assert findings == []

    def test_entity_bomb_rejected_in_arxiv_parser(self) -> None:
        config = _StubSourceConfig()
        findings = _parse_arxiv_response(ENTITY_BOMB_PAYLOAD, config)
        assert findings == []

    def test_entity_bomb_rejected_in_rss_parser(self) -> None:
        config = _StubSourceConfig(keywords=["machine learning"])
        # Reuse the Atom-shaped bomb — the RSS parser handles both formats
        findings = _parse_rss_feed(ENTITY_BOMB_PAYLOAD, "evil", config)
        assert findings == []

    def test_ssrf_payload_rejected_in_arxiv_parser(self) -> None:
        config = _StubSourceConfig()
        findings = _parse_arxiv_response(SSRF_PAYLOAD, config)
        assert findings == []


# ---------------------------------------------------------------------------
# Tests: malformed XML (existing behavior preserved)
# ---------------------------------------------------------------------------


class TestMalformedXml:
    """Malformed XML must return empty list without raising."""

    def test_malformed_xml_arxiv(self) -> None:
        config = _StubSourceConfig()
        findings = _parse_arxiv_response("<broken xml><<<", config)
        assert findings == []

    def test_malformed_xml_rss(self) -> None:
        config = _StubSourceConfig(keywords=["test"])
        findings = _parse_rss_feed("<broken xml><<<", "bad", config)
        assert findings == []

    def test_empty_string_arxiv(self) -> None:
        config = _StubSourceConfig()
        findings = _parse_arxiv_response("", config)
        assert findings == []

    def test_empty_string_rss(self) -> None:
        config = _StubSourceConfig(keywords=["test"])
        findings = _parse_rss_feed("", "empty", config)
        assert findings == []


# ---------------------------------------------------------------------------
# Dependency integrity: scanner.py must import defusedxml, not stdlib ET
# ---------------------------------------------------------------------------


class TestDefusedxmlDependency:
    """Ensure the scanner uses defusedxml, preventing silent revert to stdlib."""

    def test_defusedxml_importable(self) -> None:
        """defusedxml must be installed in the environment."""
        import defusedxml.ElementTree  # noqa: F401

    def test_scanner_imports_defusedxml_not_stdlib(self) -> None:
        """The scanner module must not use xml.etree.ElementTree for parsing."""
        import inspect

        from app.services import scanner

        source = inspect.getsource(scanner)
        # Must not contain the stdlib import for XML parsing
        # (the import is inside the two parse functions)
        assert "import xml.etree.ElementTree" not in source, (
            "scanner.py still imports xml.etree.ElementTree — "
            "use defusedxml.ElementTree instead"
        )
        assert "import defusedxml.ElementTree" in source
