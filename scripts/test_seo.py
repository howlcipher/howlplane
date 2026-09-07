#!/usr/bin/env python3
import json
import os
import re
from html.parser import HTMLParser

DOCS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "docs")
)


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
    with open(index_path, "r", encoding="utf-8") as f:
        html = f.read()

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
    assert parser.canonical == expected_canonical, (
        f"Canonical mismatch: {parser.canonical}"
    )
    print("  [PASS] Canonical URL matches:", parser.canonical)

    # 4. Single primary H1
    assert len(parser.h1s) == 1, (
        f"Expected exactly 1 H1, found {len(parser.h1s)}: {parser.h1s}"
    )
    print("  [PASS] Exactly 1 H1 present:", parser.h1s[0])

    # 5. Open Graph & Twitter
    og_props = [
        "og:title", "og:description", "og:url", "og:image",
        "twitter:card", "twitter:title", "twitter:description"
    ]
    for prop in og_props:
        assert prop in parser.meta, f"Missing social tag: {prop}"
    print("  [PASS] Open Graph and Twitter card tags verified")

    # 6. JSON-LD structured data
    assert len(parser.json_ld) >= 1, "Missing JSON-LD structured data"
    ld = parser.json_ld[0]
    ld_type = ld.get("@type")
    assert ld_type == "SoftwareApplication", (
        f"Expected SoftwareApplication, got {ld_type}"
    )
    author = ld.get("author", {})
    assert author.get("name") == "William Elias", (
        "Author name must be William Elias"
    )
    author_url = author.get("url")
    expected_author_url = "https://howlcipher.github.io/william_elias/"
    assert author_url == expected_author_url, (
        "Author URL must point to william_elias portfolio"
    )
    print("  [PASS] JSON-LD valid and correctly attributes William Elias")

    # 7. Author and entity linking
    william_url = "https://howlcipher.github.io/william_elias/"
    howl_url = "https://howlcipher.github.io/howl/"
    assert william_url in parser.links, "Missing link to William Elias"
    assert howl_url in parser.links, "Missing link to Howl ecosystem hub"
    print("  [PASS] Internal entity links to William Elias and Howl verified")

    # 8. robots.txt
    robots_path = os.path.join(DOCS_DIR, "robots.txt")
    assert os.path.exists(robots_path), "docs/robots.txt missing"
    with open(robots_path, "r", encoding="utf-8") as f:
        robots = f.read()
    assert "User-agent: *" in robots, "robots.txt missing User-agent: *"
    assert "Allow: /" in robots, "robots.txt missing Allow: /"
    sitemap_target = (
        "Sitemap: https://howlcipher.github.io/howlplane/sitemap.xml"
    )
    assert sitemap_target in robots, "robots.txt missing Sitemap reference"
    print("  [PASS] docs/robots.txt valid and references sitemap")

    # 9. sitemap.xml
    sitemap_path = os.path.join(DOCS_DIR, "sitemap.xml")
    assert os.path.exists(sitemap_path), "docs/sitemap.xml missing"
    with open(sitemap_path, "r", encoding="utf-8") as f:
        sitemap_content = f.read()
    urls = [
        u.strip() for u in re.findall(r"<loc>(.*?)</loc>", sitemap_content)
    ]
    assert expected_canonical in urls, (
        f"Sitemap does not contain canonical URL {expected_canonical}"
    )
    print("  [PASS] docs/sitemap.xml valid and contains canonical URL")

    print("\nAll SEO validations PASSED successfully!")


if __name__ == "__main__":
    test_seo()
