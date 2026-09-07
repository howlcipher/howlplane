#!/usr/bin/env python3
import os
import re
import json
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

DOCS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "docs"))

class SimpleHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.in_title = False
        self.meta = {}
        self.links = []
        self.canonical = None
        self.h1s = []
        self.in_h1 = False
        self.current_h1 = []
        self.json_ld = []
        self.in_script = False
        self.script_type = ""
        self.current_script = []

    def handle_starttag(self, tag, attrs):
        attrs_d = {k.lower(): v for k, v in attrs}
        if tag == "title":
            self.in_title = True
        elif tag == "meta":
            name = attrs_d.get("name") or attrs_d.get("property")
            if name:
                self.meta[name.lower()] = attrs_d.get("content", "")
        elif tag == "link":
            if attrs_d.get("rel") == "canonical":
                self.canonical = attrs_d.get("href")
        elif tag == "a":
            href = attrs_d.get("href")
            if href:
                self.links.append(href)
        elif tag == "h1":
            self.in_h1 = True
            self.current_h1 = []
        elif tag == "script":
            stype = attrs_d.get("type", "").lower()
            if stype == "application/ld+json":
                self.in_script = True
                self.script_type = stype
                self.current_script = []

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag == "h1":
            self.in_h1 = False
            self.h1s.append(" ".join(self.current_h1).strip())
            self.current_h1 = []
        elif tag == "script" and self.in_script:
            self.in_script = False
            raw = "".join(self.current_script).strip()
            if raw:
                try:
                    self.json_ld.append(json.loads(raw))
                except Exception as e:
                    raise AssertionError(f"Invalid JSON-LD syntax: {e}")
            self.current_script = []

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif self.in_h1:
            self.current_h1.append(data.strip())
        elif self.in_script:
            self.current_script.append(data)

def test_seo():
    index_path = os.path.join(DOCS_DIR, "index.html")
    assert os.path.exists(index_path), "docs/index.html missing"
    html = open(index_path, "r", encoding="utf-8").read()

    parser = SimpleHTMLParser()
    parser.feed(html)

    # 1. Title
    assert parser.title.strip(), "HTML <title> is empty"
    assert "HowlPlane" in parser.title, "HTML <title> must include HowlPlane"
    print("  [PASS] HTML <title> exists:", parser.title.strip())

    # 2. Meta description
    assert "description" in parser.meta, "meta description is missing"
    desc = parser.meta["description"]
    assert len(desc) > 20, "meta description too short"
    assert len(desc) <= 170, f"meta description too long: {len(desc)}"
    print("  [PASS] meta description exists:", desc[:60] + "...")

    # 3. Canonical
    expected_canonical = "https://howlcipher.github.io/howlplane/"
    assert parser.canonical == expected_canonical, f"Canonical mismatch: {parser.canonical}"
    print("  [PASS] Canonical URL matches:", parser.canonical)

    # 4. Single primary H1
    assert len(parser.h1s) == 1, f"Expected exactly 1 H1, found {len(parser.h1s)}: {parser.h1s}"
    print("  [PASS] Exactly 1 H1 present:", parser.h1s[0])

    # 5. Open Graph & Twitter
    for prop in ["og:title", "og:description", "og:url", "og:image", "twitter:card", "twitter:title", "twitter:description"]:
        assert prop in parser.meta, f"Missing social tag: {prop}"
    print("  [PASS] Open Graph and Twitter card tags verified")

    # 6. JSON-LD structured data
    assert len(parser.json_ld) >= 1, "Missing application/ld+json structured data"
    ld = parser.json_ld[0]
    assert ld.get("@type") == "SoftwareApplication", f"Expected SoftwareApplication, got {ld.get("@type")}"
    assert ld.get("author", {}).get("name") == "William Elias", "Author name must be William Elias"
    assert ld.get("author", {}).get("url") == "https://howlcipher.github.io/william_elias/", "Author URL must point to william_elias portfolio"
    print("  [PASS] JSON-LD valid and correctly attributes William Elias")

    # 7. Author and entity linking
    assert "https://howlcipher.github.io/william_elias/" in parser.links, "Missing link to William Elias portfolio"
    assert "https://howlcipher.github.io/howl/" in parser.links, "Missing link to Howl ecosystem hub"
    print("  [PASS] Internal entity links to William Elias and Howl Hub verified")

    # 8. robots.txt
    robots_path = os.path.join(DOCS_DIR, "robots.txt")
    assert os.path.exists(robots_path), "docs/robots.txt missing"
    robots = open(robots_path, "r", encoding="utf-8").read()
    assert "User-agent: *" in robots, "robots.txt missing User-agent: *"
    assert "Allow: /" in robots, "robots.txt missing Allow: /"
    assert "Sitemap: https://howlcipher.github.io/howlplane/sitemap.xml" in robots, "robots.txt missing Sitemap reference"
    print("  [PASS] docs/robots.txt valid and references sitemap")

    # 9. sitemap.xml
    sitemap_path = os.path.join(DOCS_DIR, "sitemap.xml")
    assert os.path.exists(sitemap_path), "docs/sitemap.xml missing"
    tree = ET.parse(sitemap_path)
    root = tree.getroot()
    urls = [loc.text.strip() for loc in root.findall(".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
    assert expected_canonical in urls, f"Sitemap does not contain canonical URL {expected_canonical}"
    print("  [PASS] docs/sitemap.xml valid and contains canonical URL")

    print("\nAll SEO validations PASSED successfully!")

if __name__ == "__main__":
    test_seo()
