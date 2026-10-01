import os

from tests._requirements import import_or_skip

BeautifulSoup = import_or_skip("bs4").BeautifulSoup


def test_frontend_has_systems_console_elements():
    index_path = os.path.join(os.path.dirname(__file__), "..", "docs", "index.html")
    assert os.path.exists(index_path), "Frontend index.html is missing"

    with open(index_path, "r", encoding="utf-8") as f:
        html_content = f.read()

    soup = BeautifulSoup(html_content, "html.parser")

    # Check for systems console classes and structure
    assert soup.find(class_="section-nav") is not None
    assert soup.find(class_="signal-grid") is not None
    assert soup.find(class_="status-dot") is not None

    # Check for theme toggle
    assert soup.find(id="theme-toggle") is not None

    # Check for primary sections
    assert soup.find(id="overview") is not None
    assert soup.find(id="workflow") is not None
    assert soup.find(id="capabilities") is not None
    assert soup.find(id="howlframe") is not None


def test_frontend_script():
    app_js_path = os.path.join(os.path.dirname(__file__), "..", "docs", "app.js")
    with open(app_js_path, "r", encoding="utf-8") as f:
        js_content = f.read()

    assert "theme-toggle" in js_content
    assert "light-mode" in js_content
