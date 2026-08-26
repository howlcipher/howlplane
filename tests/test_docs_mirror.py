import subprocess
from pathlib import Path
from src.control_plane.git_env import run_git_in_repo


def test_site_nav_has_no_broken_docs_html_link():
    """
    Both the landing page and the Jekyll layout used to link to a
    "/docs.html" nav item that had no corresponding source file anywhere
    in the repo, producing a live 404 on the deployed Pages site. Guards
    against reintroducing that dead link; nav items should point at real
    generated destinations like the pdoc API index instead.
    """
    repo_root = Path(__file__).resolve().parents[1]
    index_html = (repo_root / "docs" / "index.html").read_text(encoding="utf-8")
    layout_html = (
        repo_root / "docs_theme" / "_layouts" / "default.html"
    ).read_text(encoding="utf-8")

    assert 'href="docs.html"' not in index_html
    assert "'/docs.html'" not in layout_html
    assert '"/docs.html"' not in layout_html

    assert 'href="api/"' in index_html
    assert "'/api/' | relative_url" in layout_html


def test_landing_page_and_layout_share_one_stylesheet():
    """
    The landing page and every other Pages-rendered doc page used to load
    two unrelated, visually inconsistent stylesheets (docs/styles.css vs.
    docs_theme/assets/css/cyberpunk.css). They now share a single unified
    docs/styles.css so the site reads as one coherent design system.
    """
    repo_root = Path(__file__).resolve().parents[1]
    layout_html = (
        repo_root / "docs_theme" / "_layouts" / "default.html"
    ).read_text(encoding="utf-8")

    assert "'/styles.css' | relative_url" in layout_html
    assert "cyberpunk.css" not in layout_html
    assert not (repo_root / "docs_theme" / "assets").exists()


def test_only_hand_authored_frontend_tracked_under_docs():
    """
    docs/ mixes a hand-authored frontend (index.html, app.js, styles.css,
    favicon.svg, og-image.jpg — no generation step, exercised by
    tests/test_frontend.py) with content mirrored from documentation/,
    .agents/, assets/, and repo markdown files by `make docs` / CI, which
    is rebuilt and redeployed to the `gh-pages` branch on every push. Only
    the hand-authored files may be tracked; everything else is a build
    artifact and must stay gitignored. Guards against reintroducing the
    committed-mirror drift removed by improvements item 27.
    """
    repo_root = Path(__file__).resolve().parents[1]
    result = run_git_in_repo(repo_root, ["ls-files", "docs"])
    assert result.returncode == 0
    tracked_paths = set(result.stdout.strip().splitlines())
    hand_authored_frontend = {
        "docs/index.html",
        "docs/app.js",
        "docs/styles.css",
        "docs/style.css",
        "docs/script.js",
        "docs/favicon.svg",
        "docs/favicon.png",
        "docs/og-image.jpg",
        "docs/social-preview.png",
    }
    assert tracked_paths == hand_authored_frontend


def test_docs_target_builds_into_staging_before_swapping_live_dirs():
    """
    Verify that the 'docs' target builds documentation into a staging directory,
    then removes live directories, then moves built subtrees into place.
    """
    repo_root = Path(__file__).resolve().parents[1]
    makefile_path = repo_root / "Makefile"
    with makefile_path.open() as f:
        lines = f.readlines()

    docs_recipe_lines = []
    in_docs_recipe = False
    for line in lines:
        line = line.rstrip()
        if line.startswith("docs:"):
            in_docs_recipe = True
            continue
        if in_docs_recipe:
            if not line or (not line.startswith("\t") and not line.startswith("#")):
                break
            docs_recipe_lines.append(line)

    # Find indices
    rm_tmp_idx = None
    build_idx = None
    swap_rm_idx = None
    mv_indices = []
    final_rm_idx = None
    for i, line in enumerate(docs_recipe_lines):
        if "rm -rf docs/.build-tmp" in line and rm_tmp_idx is None:
            rm_tmp_idx = i
        if "pdoc" in line and "docs/.build-tmp/api" in line:
            build_idx = i
        if "rm -rf" in line and all(x in line for x in ["docs/api", "docs/documentation", "docs/assets", "docs/.agents"]):
            swap_rm_idx = i
        if line.lstrip().startswith("mv") and "docs/.build-tmp/" in line:
            mv_indices.append(i)
        if "rm -rf docs/.build-tmp" in line and i != rm_tmp_idx:
            final_rm_idx = i

    assert rm_tmp_idx is not None, "Makefile should start by removing staging dir with 'rm -rf docs/.build-tmp'"
    assert build_idx is not None, "Makefile should have a staging build step with pdoc"
    assert swap_rm_idx is not None, "Makefile should remove live dirs before moving built content"
    assert len(mv_indices) == 4, f"Makefile should have four mv lines, found {len(mv_indices)}"
    last_mv_idx = max(mv_indices)
    assert final_rm_idx is not None, "Makefile should clean up staging dir after moving"
    assert rm_tmp_idx < build_idx < swap_rm_idx < last_mv_idx < final_rm_idx, "Staging build before removal and move sequence must be correct"
