"""The dashboard's Documentation page reads the docs that ship with the checkout.

``janus_cli.dashboard_docs`` turns ``website/sidebars.ts`` into the page's
navigation and each Markdown page into HTML the dashboard can show without
reaching the network. These tests use small throwaway docs trees for
behaviour, and the real ``website/`` only for relationships that must hold
for every page (the sidebar resolves, pages render).
"""

from pathlib import Path

import pytest

from janus_cli import dashboard_docs as dd

_REPO = Path(__file__).resolve().parents[2]
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

SIDEBAR = """
import type {SidebarsConfig} from '@docusaurus/plugin-content-docs';

const sidebars: SidebarsConfig = {
  docs: [
    {
      type: 'category',
      label: 'Getting Started',
      collapsed: true,
      items: [
        'start/quickstart',
        // a page that was deleted but is still listed
        'start/gone',
        {
          type: 'category',
          label: "Deeper",
          items: ['start/deep/index'],
        },
      ],
    },
    'guide/cli',
    {type: 'doc', id: 'guide/tui', label: 'The TUI'},
    {type: 'link', label: 'Elsewhere', href: 'https://example.com'},
    {type: 'category', label: 'Empty', items: ['nope/missing']},
  ],
};

export default sidebars;
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def site(tmp_path):
    """A small ``website/`` directory: sidebar, four pages, one image."""
    root = tmp_path / "website"
    _write(root / "sidebars.ts", SIDEBAR)
    _write(
        root / "docs/start/quickstart.md",
        '---\ntitle: "Quickstart"\nsidebar_label: "Quick start"\ndescription: "Go."\n---\n\n# Quickstart\n\nHello.\n',
    )
    _write(root / "docs/start/deep/index.md", "# Going deeper\n\nBody.\n")
    _write(root / "docs/guide/cli.md", "---\ntitle: CLI\n---\n\nText.\n")
    _write(root / "docs/guide/tui.mdx", "Just text, no title anywhere.\n")
    (root / "static/img").mkdir(parents=True)
    (root / "static/img/shot.png").write_bytes(_PNG)
    return root


def _page(site, doc_id, body):
    _write(site / "docs" / f"{doc_id}.md", body)
    page = dd.render_page(site, doc_id)
    assert page is not None
    return page


# ── Where the docs are ─────────────────────────────────────────────────────


def test_finds_the_website_directory_of_a_checkout(site):
    assert dd.find_site(site.parent) == site


def test_reports_no_docs_for_an_install_without_them(tmp_path):
    # A wheel install has no website/ directory at all.
    assert dd.find_site(tmp_path) is None


# ── Navigation ─────────────────────────────────────────────────────────────


def test_tree_follows_the_sidebar_order_and_nesting(site):
    tree = dd.build_tree(site)

    assert [node["label"] for node in tree] == ["Getting Started", "CLI", "The TUI"]
    started = tree[0]
    assert started["type"] == "category"
    assert [node["label"] for node in started["items"]] == ["Quick start", "Deeper"]
    assert started["items"][1]["items"] == [
        {"type": "doc", "id": "start/deep/index", "label": "Going deeper"}
    ]


def test_tree_labels_come_from_the_page_itself(site):
    labels = {node["id"]: node["label"] for node in dd.iter_docs(dd.build_tree(site))}

    assert labels["start/quickstart"] == "Quick start"  # sidebar_label wins
    assert labels["guide/cli"] == "CLI"  # then the title
    assert labels["start/deep/index"] == "Going deeper"  # then the first heading
    assert labels["guide/tui"] == "The TUI"  # a label set in the sidebar wins over all


def test_tree_leaves_out_pages_that_do_not_exist(site):
    ids = [node["id"] for node in dd.iter_docs(dd.build_tree(site))]

    assert "start/gone" not in ids
    assert "nope/missing" not in ids


def test_tree_drops_a_category_left_with_nothing_in_it(site):
    assert "Empty" not in [node["label"] for node in dd.build_tree(site)]


def test_tree_falls_back_to_folders_when_the_sidebar_cannot_be_read(site):
    (site / "sidebars.ts").write_text("export default buildSidebar(require('./x'))", encoding="utf-8")

    tree = dd.build_tree(site)
    ids = sorted(node["id"] for node in dd.iter_docs(tree))

    assert ids == ["guide/cli", "guide/tui", "start/deep/index", "start/quickstart"]
    assert all(node["type"] == "category" for node in tree)


def test_tree_falls_back_to_folders_without_a_sidebar_file(site):
    (site / "sidebars.ts").unlink()

    assert len(list(dd.iter_docs(dd.build_tree(site)))) == 4


# ── Finding a page ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "doc_id",
    ["../sidebars", "start/../../sidebars", "/etc/passwd", "start/quickstart.md", "", "start\\quickstart", "start//quickstart"],
)
def test_page_lookup_refuses_anything_but_a_plain_doc_id(site, doc_id):
    assert dd.resolve_doc(site, doc_id) is None


def test_page_lookup_does_not_follow_a_link_out_of_the_docs(site, tmp_path):
    secret = _write(tmp_path / "secret.md", "# Secret\n")
    (site / "docs/start/leak.md").symlink_to(secret)

    assert dd.resolve_doc(site, "start/leak") is None


def test_page_lookup_does_not_follow_a_link_into_a_folder_that_only_starts_the_same(site):
    # "docs-private" begins with "docs": comparing bare prefixes would let it through.
    secret = _write(site / "docs-private/secret.md", "# Secret\n")
    (site / "docs/start/leak.md").symlink_to(secret)

    assert dd.resolve_doc(site, "start/leak") is None


def test_folder_navigation_leaves_out_a_link_out_of_the_docs(site, tmp_path):
    (site / "sidebars.ts").unlink()
    (site / "docs/start/leak.md").symlink_to(_write(tmp_path / "secret.md", "# Secret\n"))

    assert "start/leak" not in [node["id"] for node in dd.iter_docs(dd.build_tree(site))]


def test_page_lookup_skips_hidden_files(site):
    _write(site / "docs/start/.draft.md", "# Draft\n")

    assert dd.resolve_doc(site, "start/.draft") is None


def test_page_lookup_finds_md_mdx_and_folder_index_pages(site):
    assert dd.resolve_doc(site, "guide/cli").name == "cli.md"
    assert dd.resolve_doc(site, "guide/tui").name == "tui.mdx"
    assert dd.resolve_doc(site, "start/deep").name == "index.md"


def test_unknown_page_renders_as_nothing(site):
    assert dd.render_page(site, "start/nope") is None


# ── Rendering ──────────────────────────────────────────────────────────────


def test_page_carries_its_title_and_description_and_hides_the_frontmatter(site):
    page = dd.render_page(site, "start/quickstart")

    assert page["id"] == "start/quickstart"
    assert page["title"] == "Quickstart"
    assert page["description"] == "Go."
    assert "sidebar_label" not in page["html"]
    assert "<h1" in page["html"] and "Hello." in page["html"]


def test_page_title_is_the_frontmatter_title_even_without_a_heading(site):
    assert dd.render_page(site, "guide/cli")["title"] == "CLI"


def test_page_title_falls_back_to_the_heading_then_the_file_name(site):
    assert dd.render_page(site, "start/deep/index")["title"] == "Going deeper"
    assert dd.render_page(site, "guide/tui")["title"] == "tui"


def test_tables_and_fenced_code_render(site):
    html = _page(site, "t", "| a | b |\n|---|---|\n| 1 | 2 |\n\n```bash\njanus --tui\n```\n")["html"]

    assert "<table>" in html and "<td>1</td>" in html
    assert "<pre>" in html and "janus --tui" in html


def test_admonition_keeps_its_kind_title_and_a_code_block_inside(site):
    body = "Before.\n\n:::tip Fast path\nRun this:\n\n```bash\njanus update\n```\n:::\n\nAfter.\n"
    html = _page(site, "a", body)["html"]

    assert 'class="admonition tip"' in html
    assert "Fast path" in html
    assert "<pre>" in html and "janus update" in html
    assert ":::" not in html
    assert html.index("Before.") < html.index("janus update") < html.index("After.")


def test_admonition_without_a_title_is_named_after_its_kind(site):
    html = _page(site, "a", ":::warning\nCareful.\n:::\n")["html"]

    assert 'class="admonition warning"' in html
    assert "Warning" in html and "Careful." in html


def test_admonition_title_in_brackets_loses_the_brackets(site):
    html = _page(site, "a", ":::warning[Read this first]\nText.\n:::\n")["html"]

    assert "Read this first" in html
    assert "[Read this first]" not in html


def test_a_closing_marker_with_nothing_open_is_not_shown(site):
    # A page whose opening line was edited away should not print ":::".
    html = _page(site, "a", "Some text.\n:::\n\nMore text.\n")["html"]

    assert ":::" not in html
    assert "Some text." in html and "More text." in html


def test_colons_inside_a_code_block_are_not_an_admonition(site):
    html = _page(site, "a", "```md\n:::tip\nliteral\n:::\n```\n")["html"]

    assert "admonition" not in html
    assert ":::tip" in html


def test_admonition_runs_to_its_own_closing_marker_past_one_in_a_code_block(site):
    body = ":::note\nWrite it like this:\n\n```md\n:::\n```\n\nStill inside.\n:::\n\nOutside.\n"
    html = _page(site, "a", body)["html"]

    inside = html[html.index('class="admonition note"') : html.index("Outside.")]
    assert inside.index("Still inside.") < inside.index("</div>")
    assert "<p>Still inside.</p>" in html and "<p>Outside.</p>" in html


def test_only_known_admonition_kinds_open_one(site):
    html = _page(site, "a", ":::details\nPlain text here.\n")["html"]

    assert 'class="admonition' not in html
    assert "Plain text here." in html


def test_code_blocks_are_left_exactly_as_written(site):
    body = '```jsx\nimport Tabs from "@theme/Tabs";\n\n<Tabs>\n<!-- kept -->\n<img src="/img/shot.png" />\n```\n'
    html = _page(site, "c", body)["html"]

    assert "import Tabs from" in html
    assert "&lt;Tabs&gt;" in html
    assert "&lt;!-- kept --&gt;" in html
    assert "data-doc-asset" not in html


def test_html_comments_are_not_shown(site):
    html = _page(site, "c", "Shown.\n\n<!-- a note to editors -->\n\nAlso shown.\n")["html"]

    assert "note to editors" not in html
    assert "Shown." in html and "Also shown." in html


def test_raw_html_in_a_page_is_shown_as_text_not_run(site):
    html = _page(site, "x", 'Hi <script>alert(1)</script> and <b onclick="x()">bold</b>.\n\n<div>block</div>\n')["html"]

    assert "<script" not in html
    assert "<b " not in html and "<div>" not in html
    assert "&lt;script&gt;" in html


def test_mdx_imports_and_component_tags_are_dropped(site):
    body = 'import Tabs from "@theme/Tabs";\n\n<Tabs>\n\nKept text.\n\n</Tabs>\n'
    html = _page(site, "m", body)["html"]

    assert "import Tabs" not in html
    assert "Tabs" not in html
    assert "Kept text." in html


def test_an_embedded_video_becomes_a_link_to_it(site):
    body = (
        "Watch:\n\n"
        "<div style={{position: 'relative', paddingBottom: '56.25%'}}>\n"
        "  <iframe\n"
        "    style={{position: 'absolute', top: 0}}\n"
        '    src="https://www.youtube-nocookie.com/embed/R3YOGfTBcQg"\n'
        '    title="Janus Masterclass"\n'
        "    allowFullScreen\n"
        "  ></iframe>\n"
        "</div>\n\n"
        "After.\n"
    )
    html = _page(site, "v", body)["html"]

    assert 'href="https://www.youtube-nocookie.com/embed/R3YOGfTBcQg"' in html
    assert "Janus Masterclass" in html
    assert "iframe" not in html and "paddingBottom" not in html and "div" not in html
    assert "After." in html


@pytest.mark.parametrize("src", ["javascript:alert(1)", "/local/player", "data:text/html,x", ""])
def test_an_embedded_frame_that_is_not_a_web_address_is_left_out(site, src):
    html = _page(site, "v", f'Before.\n\n<iframe src="{src}" title="Player"></iframe>\n\nAfter.\n')["html"]

    assert "Player" not in html and "javascript" not in html and "iframe" not in html
    assert "Before." in html and "After." in html


def test_details_show_their_summary_and_their_body(site):
    body = "<details>\n<summary>Advanced options</summary>\n\nHidden **by default** on the site.\n\n</details>\n"
    html = _page(site, "d", body)["html"]

    assert "<strong>Advanced options</strong>" in html
    assert "<strong>by default</strong>" in html
    assert "details" not in html and "summary" not in html


def test_key_names_and_bold_written_as_html_still_read_as_such(site):
    html = _page(site, "k", "Press <kbd>Ctrl</kbd>+<kbd>K</kbd> to <strong>search</strong>.\n")["html"]

    assert "<code>Ctrl</code>+<code>K</code>" in html
    assert "<strong>search</strong>" in html
    assert "kbd" not in html


def test_html_named_inside_inline_code_is_left_alone(site):
    html = _page(site, "k", "Wrap it in a `<div>` or a `<kbd>x</kbd>`, not an `<iframe src=\"x\">`.\n")["html"]

    assert "<code>&lt;div&gt;</code>" in html
    assert "<code>&lt;kbd&gt;x&lt;/kbd&gt;</code>" in html
    assert "&lt;iframe" in html


def test_links_to_other_pages_point_at_the_dashboard_page(site):
    body = (
        "[abs](/docs/guide/cli) [rel](./quickstart.md#first) [up](../guide/tui) "
        "[dir](/docs/start/deep/) [bare](quickstart)\n"
    )
    html = _page(site, "start/links", body)["html"]

    assert 'data-doc-id="guide/cli"' in html
    assert 'data-doc-anchor="first"' in html
    assert 'data-doc-id="guide/tui"' in html
    assert 'data-doc-id="start/deep/index"' in html
    assert html.count('data-doc-id="start/quickstart"') == 2


def test_a_link_to_the_docs_root_opens_the_landing_page(site):
    _write(site / "docs/index.md", "# Welcome\n")
    html = _page(site, "start/links", "Back to the [start](/) or [here](/docs/).\n")["html"]

    assert html.count('data-doc-id="index"') == 2


def test_a_link_to_a_page_that_does_not_exist_is_left_as_plain_text(site):
    html = _page(site, "start/links", "See [the void](/docs/nowhere/at-all).\n")["html"]

    assert "the void" in html
    assert "href=" not in html
    assert "data-doc-id" not in html


def test_links_within_the_page_stay_anchors(site):
    html = _page(site, "x", "[down](#details)\n\n## Details\n")["html"]

    assert 'href="#details"' in html
    assert 'id="details"' in html


def test_outside_links_open_in_a_new_tab_without_handing_over_the_opener(site):
    html = _page(site, "x", "[site](https://example.com/a) [mail](mailto:a@example.com)\n")["html"]

    assert 'href="https://example.com/a"' in html
    assert 'target="_blank"' in html
    assert 'rel="noopener noreferrer"' in html
    assert 'href="mailto:a@example.com"' in html


@pytest.mark.parametrize("href", ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,x", "vbscript:x"])
def test_links_with_a_scheme_that_runs_code_lose_their_target(site, href):
    html = _page(site, "x", f"[click]({href})\n")["html"]

    assert "click" in html
    assert "href=" not in html


def test_site_images_are_named_for_the_dashboard_to_fetch(site):
    html = _page(site, "x", "![Shot](/img/shot.png)\n\n![Based](/docs/img/shot.png)\n")["html"]

    assert html.count('data-doc-asset="img/shot.png"') == 2
    assert 'alt="Shot"' in html
    # Nothing the browser would request on its own, without the session.
    assert "src=" not in html


def test_html_image_tags_are_understood_too(site):
    html = _page(site, "x", '<img alt="Shot" className="fig"\n  src="/img/shot.png" />\n')["html"]

    assert 'data-doc-asset="img/shot.png"' in html
    assert 'alt="Shot"' in html
    assert "className" not in html


@pytest.mark.parametrize("src", ["/img/../../sidebars.ts", "/img/missing.png", "/img/notes.txt", "../../../etc/passwd"])
def test_an_image_that_is_not_a_site_image_is_not_offered(site, src):
    _write(site / "static/img/notes.txt", "not an image")
    html = _page(site, "x", f"![alt text]({src})\n")["html"]

    assert "data-doc-asset" not in html
    assert "src=" not in html
    assert "alt text" in html


def test_images_from_the_web_are_left_alone(site):
    html = _page(site, "x", "![Remote](https://example.com/a.png)\n")["html"]

    assert 'src="https://example.com/a.png"' in html


def test_page_lists_its_sections_for_the_on_this_page_column(site):
    page = _page(site, "x", "# Title\n\n## First part\n\n### Detail\n\n#### Too fine to list\n\n## Second {#custom}\n")

    assert [(t["level"], t["id"], t["text"]) for t in page["toc"]] == [
        (2, "first-part", "First part"),
        (3, "detail", "Detail"),
        (2, "custom", "Second"),
    ]
    assert 'id="custom"' in page["html"] and "{#custom}" not in page["html"]


# ── Images ─────────────────────────────────────────────────────────────────


def test_asset_lookup_finds_an_image_of_the_site(site):
    assert dd.resolve_asset(site, "img/shot.png") == (site / "static/img/shot.png").resolve()


@pytest.mark.parametrize(
    "rel",
    ["img/../../sidebars.ts", "../sidebars.ts", "/etc/passwd", "img/notes.txt", "img/missing.png", "", "img\\shot.png", "img"],
)
def test_asset_lookup_refuses_anything_but_an_image_inside_the_site(site, rel):
    _write(site / "static/img/notes.txt", "not an image")

    assert dd.resolve_asset(site, rel) is None


def test_asset_lookup_skips_hidden_files(site):
    (site / "static/img/.hidden.png").write_bytes(_PNG)

    assert dd.resolve_asset(site, "img/.hidden.png") is None


def test_asset_lookup_does_not_follow_a_link_into_a_folder_that_only_starts_the_same(site):
    (site / "static-private").mkdir()
    (site / "static-private/secret.png").write_bytes(_PNG)
    (site / "static/img/leak.png").symlink_to(site / "static-private/secret.png")

    assert dd.resolve_asset(site, "img/leak.png") is None


def test_asset_lookup_does_not_follow_a_link_out_of_the_site(site, tmp_path):
    outside = tmp_path / "private.png"
    outside.write_bytes(_PNG)
    (site / "static/img/leak.png").symlink_to(outside)

    assert dd.resolve_asset(site, "img/leak.png") is None


# ── The docs in this repository ────────────────────────────────────────────


def test_the_real_sidebar_is_read_not_guessed():
    site = dd.find_site(_REPO)
    assert site is not None

    assert dd.parse_sidebar((site / "sidebars.ts").read_text(encoding="utf-8")) is not None


def test_every_page_in_the_real_navigation_exists():
    site = dd.find_site(_REPO)
    docs = list(dd.iter_docs(dd.build_tree(site)))

    assert docs, "the sidebar produced no pages"
    assert [d["id"] for d in docs if dd.resolve_doc(site, d["id"]) is None] == []
    assert all(d["label"].strip() for d in docs)


def test_the_real_navigation_covers_what_the_sidebar_names():
    # A parser that silently skipped entries would still pass the test above.
    site = dd.find_site(_REPO)
    named = dd.sidebar_doc_ids(dd.parse_sidebar((site / "sidebars.ts").read_text(encoding="utf-8")))
    shown = {d["id"] for d in dd.iter_docs(dd.build_tree(site))}

    assert {doc_id for doc_id in named if dd.resolve_doc(site, doc_id)} == shown


# ── The routes the dashboard calls ─────────────────────────────────────────


class TestDocsRoutes:
    @pytest.fixture(autouse=True)
    def _client(self, monkeypatch, _isolate_janus_home, site):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        from janus_cli.routers import docs as docs_routes
        from janus_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

        self.site = site
        self.routes = docs_routes
        self.header = _SESSION_HEADER_NAME
        monkeypatch.setattr(docs_routes, "_site", lambda: site)
        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    def test_tree_lists_the_navigation_and_names_a_first_page(self):
        data = self.client.get("/api/docs/tree").json()

        assert data["available"] is True
        assert data["home"] == "start/quickstart"
        assert [node["label"] for node in data["tree"]] == ["Getting Started", "CLI", "The TUI"]

    def test_tree_says_so_when_the_install_has_no_docs(self, monkeypatch):
        monkeypatch.setattr(self.routes, "_site", lambda: None)

        resp = self.client.get("/api/docs/tree")

        assert resp.status_code == 200
        assert resp.json() == {"available": False, "home": None, "tree": []}

    def test_page_returns_the_rendered_page(self):
        data = self.client.get("/api/docs/page", params={"id": "start/quickstart"}).json()

        assert data["id"] == "start/quickstart"
        assert data["title"] == "Quickstart"
        assert "Hello." in data["html"]

    @pytest.mark.parametrize("doc_id", ["start/nope", "../sidebars", "/etc/passwd", ""])
    def test_page_is_not_found_for_anything_that_is_not_a_page(self, doc_id):
        assert self.client.get("/api/docs/page", params={"id": doc_id}).status_code == 404

    def test_page_is_not_found_when_the_install_has_no_docs(self, monkeypatch):
        monkeypatch.setattr(self.routes, "_site", lambda: None)

        assert self.client.get("/api/docs/page", params={"id": "start/quickstart"}).status_code == 404

    def test_asset_returns_the_image_as_an_image(self):
        resp = self.client.get("/api/docs/asset", params={"path": "img/shot.png"})

        assert resp.status_code == 200
        assert resp.content == _PNG
        assert resp.headers["content-type"] == "image/png"
        assert resp.headers["x-content-type-options"] == "nosniff"

    def test_asset_cannot_run_scripts_if_opened_on_its_own(self):
        # An SVG is a document: opened directly it could run script with the
        # dashboard's session. The sandbox policy takes that away.
        _write(self.site / "static/img/diagram.svg", '<svg xmlns="http://www.w3.org/2000/svg"/>')

        resp = self.client.get("/api/docs/asset", params={"path": "img/diagram.svg"})

        assert resp.headers["content-type"].startswith("image/svg+xml")
        assert "sandbox" in resp.headers["content-security-policy"]

    @pytest.mark.parametrize("rel", ["img/missing.png", "../sidebars.ts", "img/../../sidebars.ts", "/etc/passwd", ""])
    def test_asset_is_not_found_for_anything_that_is_not_a_site_image(self, rel):
        assert self.client.get("/api/docs/asset", params={"path": rel}).status_code == 404

    @pytest.mark.parametrize(
        "url, params",
        [
            ("/api/docs/tree", {}),
            ("/api/docs/page", {"id": "start/quickstart"}),
            ("/api/docs/asset", {"path": "img/shot.png"}),
        ],
    )
    def test_docs_routes_need_the_session_like_every_other_api_route(self, url, params):
        resp = self.client.get(url, params=params, headers={self.header: "wrong-token"})

        assert resp.status_code == 401
