"""Local documentation for the dashboard's Documentation page.

The docs are the Markdown under ``website/docs`` of a checkout, written for
Docusaurus. The dashboard used to show them by framing a website, which
cannot work: the site refuses to be framed, and a local agent should not
need the network to explain itself. This module reads the same files from
disk instead:

* :func:`build_tree` turns ``website/sidebars.ts`` into the navigation.
* :func:`render_page` turns one page into HTML.
* :func:`resolve_asset` finds an image a page refers to.

Nothing here imports the web server, so it can be tested and reused on its
own. Pages are rendered with raw HTML switched off: whatever a page
contains is shown as text, never run, and the only attributes in the output
are the ones written here.

A wheel install has no ``website/`` directory; :func:`find_site` returns
``None`` for it and the page says so.
"""

from __future__ import annotations

import html
import json
import posixpath
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import yaml

_DOC_SUFFIXES = (".md", ".mdx")

#: Image types a page may show, by file suffix.
IMAGE_TYPES: Dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".svg": "image/svg+xml",
}

# A doc id is a path of plain segments: no dots-only parts, no empty parts,
# no backslashes. Everything a request sends is checked against this before
# it touches the file system.
_SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9_.-]*"
_PATH_RE = re.compile(rf"^{_SEGMENT}(?:/{_SEGMENT})*$")

_ADMONITION_KINDS = {"note", "tip", "info", "warning", "caution", "danger", "important"}


# ---------------------------------------------------------------------------
# Where the docs are
# ---------------------------------------------------------------------------


def find_site(project_root: Path) -> Optional[Path]:
    """The ``website`` directory of a checkout, or ``None`` without one."""
    site = Path(project_root) / "website"
    return site if (site / "docs").is_dir() else None


def _inside(path: Path, parent: Path) -> Optional[Path]:
    """``path`` fully resolved, if it is a file that really lives in ``parent``."""
    try:
        real = path.resolve()
        if real.is_file() and real.is_relative_to(parent.resolve()):
            return real
    except OSError:
        pass
    return None


def resolve_doc(site: Path, doc_id: Any) -> Optional[Path]:
    """The file behind a doc id (``user-guide/cli``), or ``None``."""
    if not isinstance(doc_id, str) or not _PATH_RE.match(doc_id):
        return None
    docs = site / "docs"
    for candidate in (f"{doc_id}.md", f"{doc_id}.mdx", f"{doc_id}/index.md", f"{doc_id}/index.mdx"):
        found = _inside(docs / candidate, docs)
        if found is not None:
            return found
    return None


def _doc_id_of(site: Path, path: Path) -> str:
    """The canonical id of a page file: its path under ``docs``, no suffix."""
    rel = path.resolve().relative_to((site / "docs").resolve())
    return rel.with_suffix("").as_posix()


def resolve_asset(site: Path, rel: Any) -> Optional[Path]:
    """An image under ``website/static`` (``img/x.png``), or ``None``."""
    if not isinstance(rel, str) or not _PATH_RE.match(rel):
        return None
    if posixpath.splitext(rel)[1].lower() not in IMAGE_TYPES:
        return None
    return _inside(site / "static" / rel, site / "static")


# ---------------------------------------------------------------------------
# Frontmatter and labels
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_H1_RE = re.compile(r"^#[ \t]+(.+?)[ \t]*#*[ \t]*$", re.M)
_HEADING_ID_RE = re.compile(r"\s*\{#([A-Za-z0-9_-]+)\}\s*$")


def _split_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        meta = None
    return (meta if isinstance(meta, dict) else {}), text[match.end():]


def _plain(text: str) -> str:
    """Heading text without its ``{#id}`` or inline Markdown marks."""
    text = _HEADING_ID_RE.sub("", text)
    return re.sub(r"[`*_]", "", text).strip()


def _fallback_title(path: Path) -> str:
    return path.parent.name if path.stem == "index" else path.stem


def _label(path: Path) -> str:
    """What the navigation calls a page: its sidebar label, else its title,
    else its first heading, else its file name."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            head = handle.read(8192)
    except OSError:
        return _fallback_title(path)
    meta, body = _split_frontmatter(head)
    for key in ("sidebar_label", "title"):
        value = meta.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    heading = _H1_RE.search(body)
    if heading and _plain(heading.group(1)):
        return _plain(heading.group(1))
    return _fallback_title(path)


# ---------------------------------------------------------------------------
# Reading sidebars.ts
# ---------------------------------------------------------------------------


class _NotALiteral(ValueError):
    pass


class _JsLiteral:
    """Reads the object-literal subset of JavaScript that a sidebar is
    written in: objects, arrays, strings, numbers and booleans, with
    comments and trailing commas. Anything computed raises."""

    _BARE_RE = re.compile(r"[A-Za-z0-9_.$+-]+")
    _WORDS = {"true": True, "false": False, "null": None, "undefined": None}

    def __init__(self, text: str, start: int = 0) -> None:
        self.text = text
        self.pos = start

    def _skip(self) -> None:
        text = self.text
        while self.pos < len(text):
            if text[self.pos].isspace():
                self.pos += 1
            elif text.startswith("//", self.pos):
                end = text.find("\n", self.pos)
                self.pos = len(text) if end < 0 else end
            elif text.startswith("/*", self.pos):
                end = text.find("*/", self.pos)
                if end < 0:
                    raise _NotALiteral("unterminated comment")
                self.pos = end + 2
            else:
                return

    def _peek(self) -> str:
        self._skip()
        if self.pos >= len(self.text):
            raise _NotALiteral("unexpected end")
        return self.text[self.pos]

    def value(self) -> Any:
        char = self._peek()
        if char == "{":
            return self._object()
        if char == "[":
            return self._array()
        if char in "'\"`":
            return self._string()
        return self._bare()

    def _string(self) -> str:
        quote = self.text[self.pos]
        self.pos += 1
        out: List[str] = []
        while True:
            if self.pos >= len(self.text):
                raise _NotALiteral("unterminated string")
            char = self.text[self.pos]
            if char == "\\":
                nxt = self.text[self.pos + 1 : self.pos + 2]
                out.append({"n": "\n", "t": "\t"}.get(nxt, nxt))
                self.pos += 2
            elif char == quote:
                self.pos += 1
                return "".join(out)
            elif quote == "`" and self.text.startswith("${", self.pos):
                raise _NotALiteral("template expression")
            else:
                out.append(char)
                self.pos += 1

    def _bare(self) -> Any:
        match = self._BARE_RE.match(self.text, self.pos)
        if not match:
            raise _NotALiteral(f"unexpected {self.text[self.pos]!r}")
        word = match.group(0)
        self.pos = match.end()
        if word in self._WORDS:
            return self._WORDS[word]
        try:
            return json.loads(word)
        except ValueError:
            # A variable or a call: its value is not written here.
            raise _NotALiteral(f"not a literal: {word}") from None

    def _array(self) -> List[Any]:
        self.pos += 1
        items: List[Any] = []
        while self._peek() != "]":
            items.append(self.value())
            if self._peek() == ",":
                self.pos += 1
        self.pos += 1
        return items

    def _object(self) -> Dict[str, Any]:
        self.pos += 1
        out: Dict[str, Any] = {}
        while self._peek() != "}":
            if self.text[self.pos] in "'\"":
                key = self._string()
            else:
                match = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*").match(self.text, self.pos)
                if not match:
                    raise _NotALiteral("bad key")
                key = match.group(0)
                self.pos = match.end()
            if self._peek() != ":":
                raise _NotALiteral("expected ':'")
            self.pos += 1
            out[key] = self.value()
            if self._peek() == ",":
                self.pos += 1
        self.pos += 1
        return out


def parse_sidebar(source: str) -> Optional[List[Any]]:
    """The items of the first sidebar in a ``sidebars.ts`` file, or ``None``
    when the file is not a plain literal (then the folders are used)."""
    start = re.search(r"=\s*\{", source)
    if not start:
        return None
    try:
        sidebars = _JsLiteral(source, start.end() - 1).value()
    except (_NotALiteral, IndexError):
        return None
    for items in sidebars.values():
        if isinstance(items, list):
            return items
    return None


def sidebar_doc_ids(items: Optional[List[Any]]) -> List[str]:
    """Every doc id a parsed sidebar names, in order."""
    out: List[str] = []
    for item in items or []:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, dict):
            if item.get("type") == "doc" and isinstance(item.get("id"), str):
                out.append(item["id"])
            link = item.get("link")
            if isinstance(link, dict) and link.get("type") == "doc" and isinstance(link.get("id"), str):
                out.append(link["id"])
            if isinstance(item.get("items"), list):
                out.extend(sidebar_doc_ids(item["items"]))
    return out


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


def _doc_node(site: Path, doc_id: Any, label: Any = None) -> Optional[Dict[str, Any]]:
    path = resolve_doc(site, doc_id)
    if path is None:
        return None
    text = label.strip() if isinstance(label, str) and label.strip() else _label(path)
    return {"type": "doc", "id": _doc_id_of(site, path), "label": text}


def _nodes_from_sidebar(site: Path, items: List[Any]) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    for item in items:
        node: Optional[Dict[str, Any]] = None
        if isinstance(item, str):
            node = _doc_node(site, item)
        elif isinstance(item, dict):
            kind = item.get("type")
            if kind == "doc":
                node = _doc_node(site, item.get("id"), item.get("label"))
            elif kind == "category":
                children = _nodes_from_sidebar(site, item.get("items") or [])
                link = item.get("link")
                if isinstance(link, dict) and link.get("type") == "doc":
                    landing = _doc_node(site, link.get("id"))
                    if landing and landing["id"] not in {n.get("id") for n in children}:
                        children.insert(0, landing)
                if children:
                    node = {"type": "category", "label": str(item.get("label") or "").strip() or "…", "items": children}
            elif kind == "autogenerated" and isinstance(item.get("dirName"), str):
                folder = site / "docs" / item["dirName"]
                if _PATH_RE.match(item["dirName"]) and folder.is_dir():
                    nodes.extend(_nodes_from_folder(site, folder))
        if node is not None:
            nodes.append(node)
    return nodes


def _position(meta: Dict[str, Any], key: str) -> float:
    try:
        return float(meta.get(key))
    except (TypeError, ValueError):
        return float("inf")


def _nodes_from_folder(site: Path, folder: Path) -> List[Dict[str, Any]]:
    """The navigation a folder implies: its pages, then its sub-folders,
    each ordered by the position Docusaurus would use and then by name."""
    pages: List[Tuple[float, str, Dict[str, Any]]] = []
    folders: List[Tuple[float, str, Dict[str, Any]]] = []
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return []
    for entry in entries:
        if entry.is_dir():
            children = _nodes_from_folder(site, entry)
            if not children:
                continue
            meta: Dict[str, Any] = {}
            try:
                loaded = json.loads((entry / "_category_.json").read_text(encoding="utf-8"))
                meta = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                pass
            label = str(meta.get("label") or entry.name.replace("-", " ").capitalize())
            folders.append((_position(meta, "position"), label.lower(), {"type": "category", "label": label, "items": children}))
        elif entry.suffix in _DOC_SUFFIXES and _inside(entry, site / "docs"):
            node = _doc_node(site, _doc_id_of(site, entry))
            if node is None:
                continue
            try:
                meta, _ = _split_frontmatter(entry.read_text(encoding="utf-8", errors="replace")[:8192])
            except OSError:
                meta = {}
            pages.append((_position(meta, "sidebar_position"), node["label"].lower(), node))
    return [node for _, _, node in sorted(pages, key=lambda p: p[:2])] + [
        node for _, _, node in sorted(folders, key=lambda f: f[:2])
    ]


def build_tree(site: Path) -> List[Dict[str, Any]]:
    """The Documentation page's navigation: categories and pages, in the
    order of ``sidebars.ts``. Pages that no longer exist are left out. When
    the sidebar cannot be read as a literal, the folder layout is used."""
    items: Optional[List[Any]] = None
    try:
        items = parse_sidebar((site / "sidebars.ts").read_text(encoding="utf-8"))
    except OSError:
        pass
    tree = _nodes_from_sidebar(site, items) if items is not None else []
    return tree or _nodes_from_folder(site, site / "docs")


def iter_docs(tree: List[Dict[str, Any]]) -> Iterator[Dict[str, Any]]:
    """Every page node of a navigation tree, depth first."""
    for node in tree:
        if node.get("type") == "doc":
            yield node
        else:
            yield from iter_docs(node.get("items") or [])


# ---------------------------------------------------------------------------
# Preparing a page: the parts of MDX that are not Markdown
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_IMPORT_RE = re.compile(r"^(?:import\s.+?\sfrom\s+['\"][^'\"]+['\"]|import\s+['\"][^'\"]+['\"]|export\s+(?:const|default|function)\b.*);?[ \t]*$", re.M)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_CODE_SPAN_RE = re.compile(r"(`+[^`]*`+)")
_ATTR_RE = re.compile(r"\b(alt|src|title)\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", re.I)
_IMG_TAG_RE = re.compile(r"<img\b([^<>]*?)/?>", re.S | re.I)
_IFRAME_RE = re.compile(r"<iframe\b([^<>]*?)>(?:\s*</iframe>)?", re.S | re.I)
_SUMMARY_RE = re.compile(r"<summary\b[^<>]*>(.*?)</summary>", re.S | re.I)
_KBD_RE = re.compile(r"<kbd>(.*?)</kbd>", re.S | re.I)
_BOLD_RE = re.compile(r"<(strong|b)>(.*?)</\1>", re.S | re.I)
# Layout tags the docs use around Markdown. The tags go; what they wrap stays.
_HTML_TAG_RE = re.compile(
    r"</?(?:div|p|span|details|video|source|center|figure|figcaption|section|a|br)\b[^<>]*?>", re.S | re.I
)
# An MDX component tag on lines of its own, however many it takes.
_COMPONENT_RE = re.compile(r"^[ \t]*</?[A-Z][A-Za-z0-9.]*\b[^<>]*?>[ \t]*$", re.M | re.S)


def _attrs(text: str) -> Dict[str, str]:
    return {m.group(1).lower(): (m.group(2) if m.group(2) is not None else m.group(3)) for m in _ATTR_RE.finditer(text)}


def _link_text(text: str) -> str:
    return re.sub(r"[\[\]\r\n]", " ", text).strip()


def _img_tag_to_markdown(match: "re.Match[str]") -> str:
    attrs = _attrs(match.group(1))
    src = attrs.get("src", "").strip()
    if not src or re.search(r"[\s()]", src):
        return match.group(0)
    return f"![{_link_text(attrs.get('alt', ''))}]({src})"


def _iframe_to_markdown(match: "re.Match[str]") -> str:
    """An embedded frame (a video, mostly) cannot be shown from here. Its
    address can: the embed becomes a link that opens it."""
    attrs = _attrs(match.group(1))
    src = attrs.get("src", "").strip()
    if not re.match(r"^https?://[^\s()]+$", src):
        return ""
    return f"\n\n[{_link_text(attrs.get('title', '')) or src}]({src})\n\n"


def _html_to_markdown(text: str) -> str:
    text = _IMG_TAG_RE.sub(_img_tag_to_markdown, text)
    text = _IFRAME_RE.sub(_iframe_to_markdown, text)
    text = _SUMMARY_RE.sub(lambda m: f"\n\n**{m.group(1).strip()}**\n\n", text)
    text = _KBD_RE.sub(lambda m: f"`{m.group(1)}`", text)
    text = _BOLD_RE.sub(lambda m: f"**{m.group(2)}**", text)
    return _HTML_TAG_RE.sub("", text)


def _strip_mdx(text: str) -> str:
    text = _COMMENT_RE.sub("", text)
    text = _IMPORT_RE.sub("", text)
    # HTML named inside `inline code` is being talked about, not used.
    parts = _CODE_SPAN_RE.split(text)
    text = "".join(part if index % 2 else _html_to_markdown(part) for index, part in enumerate(parts))
    return _COMPONENT_RE.sub("", text)


def _prepare(body: str) -> str:
    """Remove what MDX adds on top of Markdown (imports, component tags,
    comments) and turn ``<img>`` tags into Markdown images, leaving code
    blocks exactly as written."""
    out: List[str] = []
    prose: List[str] = []
    fence: Optional[str] = None
    for line in body.splitlines(keepends=True):
        match = _FENCE_RE.match(line)
        if fence is None:
            if match:
                out.append(_strip_mdx("".join(prose)))
                prose = []
                fence = match.group(1)
                out.append(line)
            else:
                prose.append(line)
        else:
            out.append(line)
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence) and not line.strip().strip("`~"):
                fence = None
    out.append(_strip_mdx("".join(prose)))
    return "".join(out)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_ADMONITION_RE = re.compile(r"^(:{3,})[ \t]*([A-Za-z]+)[ \t]*(.*?)[ \t]*$")
_STRAY_CLOSER_RE = re.compile(r"^:{3,}[ \t]*$")


def _admonition_rule(state: Any, start_line: int, end_line: int, silent: bool) -> bool:
    """Docusaurus admonitions: ``:::tip Title`` … ``:::``. The body is
    ordinary Markdown, so a code block inside one keeps working."""
    if state.sCount[start_line] - state.blkIndent >= 4:
        return False
    begin = state.bMarks[start_line] + state.tShift[start_line]
    line = state.src[begin : state.eMarks[start_line]]
    if _STRAY_CLOSER_RE.match(line):
        # A closing marker with nothing open (its opening line was edited
        # away): swallow it rather than print ":::" in the page. A marker
        # that does close something never gets here, because the body of
        # an open admonition stops one line short of it.
        if not silent:
            state.line = start_line + 1
        return True
    match = _ADMONITION_RE.match(line)
    if not match or match.group(2).lower() not in _ADMONITION_KINDS:
        return False
    if silent:
        return True
    marker, kind, title = match.group(1), match.group(2).lower(), match.group(3)
    title = title.strip("[]") or kind.capitalize()

    # The closing marker is a line of the same colons, outside any code block.
    next_line = start_line
    closed = False
    fence: Optional[str] = None
    while True:
        next_line += 1
        if next_line >= end_line:
            break
        line = state.src[state.bMarks[next_line] + state.tShift[next_line] : state.eMarks[next_line]]
        fenced = _FENCE_RE.match(line)
        if fence is None and fenced:
            fence = fenced.group(1)
            continue
        if fence is not None:
            if fenced and fenced.group(1)[0] == fence[0] and len(fenced.group(1)) >= len(fence) and not line.strip().strip("`~"):
                fence = None
            continue
        if line.strip() == marker:
            closed = True
            break

    old_parent, old_line_max = state.parentType, state.lineMax
    state.parentType = "admonition"
    state.lineMax = next_line

    token = state.push("admonition_open", "div", 1)
    token.attrSet("class", f"admonition {kind}")
    token.map = [start_line, next_line]
    token = state.push("admonition_title", "p", 0)
    token.content = title
    state.md.block.tokenize(state, start_line + 1, next_line)
    state.push("admonition_close", "div", -1)

    state.parentType = old_parent
    state.lineMax = old_line_max
    state.line = next_line + (1 if closed else 0)
    return True


def _render_admonition_title(renderer: Any, tokens: Any, idx: int, options: Any, env: Any) -> str:
    return f'<p class="admonition-title">{html.escape(tokens[idx].content)}</p>\n'


def _markdown() -> Any:
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"html": False}).enable(["table", "strikethrough"])
    md.block.ruler.before(
        "fence", "admonition", _admonition_rule, {"alt": ["paragraph", "reference", "blockquote", "list"]}
    )
    md.add_render_rule("admonition_title", _render_admonition_title)
    return md


def _slug(text: str, taken: Dict[str, int]) -> str:
    """A heading's anchor, made the way the docs site makes it, so the
    ``#anchors`` already written in the docs keep landing."""
    base = re.sub(r"[^\w\- ]", "", text.strip().lower(), flags=re.UNICODE).replace(" ", "-") or "section"
    count = taken.get(base, 0)
    taken[base] = count + 1
    return base if count == 0 else f"{base}-{count}"


def _inline_text(token: Any) -> str:
    return "".join(child.content for child in (token.children or []) if child.type in ("text", "code_inline"))


def _target_of(site: Path, doc_id: str, href: str) -> Optional[Tuple[str, str]]:
    """``(doc id, anchor)`` when a link points at another page of the docs."""
    path, _, anchor = href.partition("#")
    path = path.split("?", 1)[0]
    if path.startswith("/"):
        # The site serves the docs from /docs/, and links are written both
        # with and without that prefix. The bare root is the landing page.
        path = path[len("/docs/") :] if path.startswith("/docs/") else path[1:]
        path = path.rstrip("/") or "index"
    else:
        path = posixpath.normpath(posixpath.join(posixpath.dirname(doc_id), path))
    path = path.rstrip("/")
    for suffix in _DOC_SUFFIXES:
        if path.endswith(suffix):
            path = path[: -len(suffix)]
    found = resolve_doc(site, path)
    return (_doc_id_of(site, found), anchor) if found is not None else None


def _rewrite_link(site: Path, doc_id: str, token: Any) -> None:
    href = token.attrGet("href") or ""
    if href.startswith("#"):
        return
    scheme = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*):", href)
    if scheme:
        if scheme.group(1).lower() in ("http", "https"):
            token.attrSet("target", "_blank")
            token.attrSet("rel", "noopener noreferrer")
        elif scheme.group(1).lower() != "mailto":
            token.attrs.pop("href", None)
        return
    target = _target_of(site, doc_id, href)
    if target is None:
        # A page that is not in this checkout: keep the words, drop the link.
        token.attrs.pop("href", None)
        return
    page, anchor = target
    token.attrSet("href", f"?page={page}" + (f"#{anchor}" if anchor else ""))
    token.attrSet("data-doc-id", page)
    if anchor:
        token.attrSet("data-doc-anchor", anchor)


def _rewrite_image(site: Path, token: Any) -> None:
    src = token.attrGet("src") or ""
    if re.match(r"^https?://", src, re.I):
        token.attrSet("loading", "lazy")
        token.attrSet("referrerpolicy", "no-referrer")
        return
    token.attrs.pop("src", None)
    rel = src.split("#", 1)[0].split("?", 1)[0]
    if rel.startswith("/docs/"):
        rel = rel[len("/docs/") :]
    rel = rel.lstrip("/")
    if resolve_asset(site, rel) is not None:
        # The dashboard fetches it through the API, with the session.
        token.attrSet("data-doc-asset", rel)


def render_page(site: Path, doc_id: Any) -> Optional[Dict[str, Any]]:
    """One page as ``{id, title, description, html, toc}``, or ``None`` when
    there is no such page."""
    path = resolve_doc(site, doc_id)
    if path is None:
        return None
    doc_id = _doc_id_of(site, path)
    meta, body = _split_frontmatter(path.read_text(encoding="utf-8", errors="replace"))

    md = _markdown()
    tokens = md.parse(_prepare(body))

    taken: Dict[str, int] = {}
    toc: List[Dict[str, Any]] = []
    first_heading = ""
    for index, token in enumerate(tokens):
        if token.type == "heading_open":
            inline = tokens[index + 1]
            custom = _HEADING_ID_RE.search(inline.content)
            if custom and inline.children and inline.children[-1].type == "text":
                last = inline.children[-1]
                last.content = _HEADING_ID_RE.sub("", last.content)
            text = _inline_text(inline).strip()
            anchor = _slug(custom.group(1) if custom else text, taken)
            token.attrSet("id", anchor)
            level = int(token.tag[1])
            if level == 1 and not first_heading:
                first_heading = text
            elif level in (2, 3):
                toc.append({"level": level, "id": anchor, "text": text})
        for child in token.children or []:
            if child.type == "link_open":
                _rewrite_link(site, doc_id, child)
            elif child.type == "image":
                _rewrite_image(site, child)

    title = meta.get("title")
    description = meta.get("description")
    return {
        "id": doc_id,
        "title": str(title).strip() if isinstance(title, (str, int, float)) and str(title).strip() else (first_heading or _fallback_title(path)),
        "description": str(description).strip() if isinstance(description, str) else "",
        "html": md.renderer.render(tokens, md.options, {}),
        "toc": toc,
    }
