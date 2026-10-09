"""Statische Prüfungen der Website architools.ch (GitHub Pages aus docs/).

Sichert ab, was KI-Agenten und Suchmaschinen auswerten: Vertrauensseiten,
Organization-Schema, llms.txt, Sitemap, 404-Seite und interne Links.
"""
import json
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parent.parent / "docs"
SITE = "https://architools.ch/"
TRUST_PAGES = ["about.html", "contact.html", "privacy.html"]


class _PageParser(HTMLParser):
    """Sammelt Links, JSON-LD-Blöcke und den sichtbaren Text in <main>."""

    def __init__(self):
        super().__init__()
        self.links = []
        self.jsonld = []
        self.main_text = []
        self._in_main = 0
        self._in_jsonld = False
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag == "main":
            self._in_main += 1
        if tag == "script":
            if attrs.get("type") == "application/ld+json":
                self._in_jsonld = True
                self.jsonld.append("")
            else:
                self._skip += 1
        if tag == "style":
            self._skip += 1

    def handle_endtag(self, tag):
        if tag == "main":
            self._in_main -= 1
        if tag == "script":
            if self._in_jsonld:
                self._in_jsonld = False
            else:
                self._skip -= 1
        if tag == "style":
            self._skip -= 1

    def handle_data(self, data):
        if self._in_jsonld:
            self.jsonld[-1] += data
        elif self._in_main and not self._skip:
            self.main_text.append(data)


def _parse(name):
    parser = _PageParser()
    parser.feed((DOCS / name).read_text(encoding="utf-8"))
    return parser


def _visible_text(parser):
    return re.sub(r"\s+", " ", "".join(parser.main_text)).strip()


def _resolve(href, page):
    """Interner Link → Datei in docs/ (oder None für externe Links/Anker)."""
    if href.startswith(("mailto:", "http://", "https://", "#", "javascript:")):
        if href.startswith(SITE):
            href = "/" + href[len(SITE):]
        else:
            return None
    path = href.split("#")[0].split("?")[0]
    if path in ("", "/"):
        return DOCS / "index.html"
    target = DOCS / path.lstrip("/") if path.startswith("/") else (DOCS / page).parent / path
    if target.is_dir():
        target = target / "index.html"
    return target


def _html_pages():
    return sorted(p.name for p in DOCS.glob("*.html"))


# ── Vertrauensseiten ────────────────────────────────────────────────────────

@pytest.mark.parametrize("page", TRUST_PAGES)
def test_trust_page_has_substantial_content(page):
    text = _visible_text(_parse(page))
    assert len(text) >= 500, f"{page}: nur {len(text)} Zeichen Inhalt"


@pytest.mark.parametrize("page", TRUST_PAGES)
def test_trust_page_has_contact_and_canonical(page):
    html = (DOCS / page).read_text(encoding="utf-8")
    assert "info@architools.ch" in html
    assert f'<link rel="canonical" href="{SITE}{page}">' in html


def test_homepage_links_docs_and_trust_pages():
    links = set(_parse("index.html").links)
    for target in ["docs.html", *TRUST_PAGES]:
        assert target in links, f"Startseite verlinkt {target} nicht"


# ── Organization-Schema ─────────────────────────────────────────────────────

def _organizations(node):
    if isinstance(node, dict):
        if node.get("@type") == "Organization":
            yield node
        for value in node.values():
            yield from _organizations(value)
    elif isinstance(node, list):
        for item in node:
            yield from _organizations(item)


def test_homepage_organization_schema_complete():
    blocks = [json.loads(b) for b in _parse("index.html").jsonld]
    orgs = [o for b in blocks for o in _organizations(b)]
    assert orgs, "kein Organization-Schema auf der Startseite"
    org = orgs[0]

    contact = org["contactPoint"]
    assert contact["@type"] == "ContactPoint"
    assert contact["contactType"]
    assert contact.get("email") or contact.get("telephone")

    address = org["address"]
    assert address["@type"] == "PostalAddress"
    assert address["addressCountry"] == "CH"
    assert address["addressLocality"]


# ── FAQ ─────────────────────────────────────────────────────────────────────

def test_homepage_faq_matches_faqpage_schema():
    html = (DOCS / "index.html").read_text(encoding="utf-8")
    visible = re.findall(r"<summary>(.*?)</summary>\s*<p>(.*?)</p>", html, re.S)
    visible = [(_unescape(q), _unescape(a)) for q, a in visible]
    assert len(visible) >= 5

    blocks = [json.loads(b) for b in _parse("index.html").jsonld]
    faq_pages = [b for b in blocks if b.get("@type") == "FAQPage"]
    assert len(faq_pages) == 1
    schema = [(q["name"], q["acceptedAnswer"]["text"]) for q in faq_pages[0]["mainEntity"]]
    assert schema == visible, "FAQ-Text und FAQPage-Schema laufen auseinander"


def test_homepage_has_faq_and_steps_anchors():
    html = (DOCS / "index.html").read_text(encoding="utf-8")
    assert 'id="faq"' in html and 'id="so-funktionierts"' in html


def test_provider_named_consistently():
    for page in ["about.html", "privacy.html", "haftungsausschluss.html"]:
        html = (DOCS / page).read_text(encoding="utf-8")
        assert "architools" in html and "Fabio Indergand" in html, page
    for path in DOCS.glob("*.html"):
        assert "Bauchat" not in path.read_text(encoding="utf-8"), f"alter Anbietername in {path.name}"


def _unescape(text):
    import html as _html
    return _html.unescape(re.sub(r"\s+", " ", text).strip())


# ── llms.txt (Format nach llmstxt.org) ──────────────────────────────────────

def test_llms_txt_follows_spec_and_has_when_to_use():
    lines = (DOCS / "llms.txt").read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("# "), "llms.txt muss mit einer H1 beginnen"
    assert any(line.startswith("> ") for line in lines[:4]), "Zusammenfassung als Blockquote fehlt"
    assert not any(line.startswith("### ") for line in lines)

    text = "\n".join(lines)
    assert "when to use" in text.lower()

    # Innerhalb der H2-Abschnitte stehen nur Link-Listen: "- [Name](URL): Notiz"
    in_section = False
    link_re = re.compile(r"^- \[[^\]]+\]\((https?://[^)\s]+)\)(: .+)?$")
    for line in lines:
        if line.startswith("## "):
            in_section = True
            continue
        if in_section and line.strip():
            assert link_re.match(line), f"keine gültige Link-Zeile: {line!r}"


def test_llms_txt_links_point_to_existing_pages():
    text = (DOCS / "llms.txt").read_text(encoding="utf-8")
    urls = re.findall(r"\]\((https://architools\.ch/[^)\s]*)\)", text)
    assert urls
    for url in urls:
        target = _resolve(url, "index.html")
        assert target.exists(), f"llms.txt verweist auf fehlende Seite {url}"
    for page in ["docs.html", *TRUST_PAGES]:
        assert SITE + page in text


# ── Sitemap, robots.txt, 404 ────────────────────────────────────────────────

def test_sitemap_valid_and_complete():
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.parse(DOCS / "sitemap.xml").getroot()
    locs = [el.text.strip() for el in root.findall("sm:url/sm:loc", ns)]
    assert len(locs) == len(set(locs)), "doppelte Einträge in sitemap.xml"
    for loc in locs:
        assert loc.startswith(SITE)
        assert _resolve(loc, "index.html").exists(), f"sitemap.xml: {loc} existiert nicht"
    for page in ["docs.html", *TRUST_PAGES]:
        assert SITE + page in locs


def test_robots_references_sitemap():
    assert f"Sitemap: {SITE}sitemap.xml" in (DOCS / "robots.txt").read_text(encoding="utf-8")


def test_404_page_uses_absolute_links_to_help_resources():
    # GitHub Pages liefert 404.html für beliebige Pfade aus → relative Links
    # würden in Unterverzeichnissen brechen.
    parser = _parse("404.html")
    internal = [h for h in parser.links if not h.startswith(("mailto:", "http"))]
    assert internal
    assert all(h.startswith("/") for h in internal), internal
    assert "/llms.txt" in internal and "/docs.html" in internal
    assert len(_visible_text(parser)) >= 20


# ── Interne Links ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("page", _html_pages())
def test_internal_links_resolve(page):
    for href in _parse(page).links:
        target = _resolve(href, page)
        if target is not None:
            assert target.exists(), f"{page}: Link {href!r} führt ins Leere"
