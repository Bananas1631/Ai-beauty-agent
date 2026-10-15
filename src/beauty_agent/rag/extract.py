"""Текст страницы без меню, подвала, скриптов и форм — по смысловым блокам: заголовки и абзацы.

Структура сохраняется: нарезка идёт по заголовкам, а фрагмент помнит свой раздел.
"""

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser

_NOISE = "script, style, noscript, template, nav, header, footer, aside, form, iframe, svg, button, select, dialog"
_BLOCKS = "h1, h2, h3, h4, h5, h6, p, li, dt, dd, blockquote, td, th, pre, figcaption"
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_SPACES = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Block:
    text: str
    heading_level: int = 0  # 0 — обычный абзац


@dataclass(frozen=True, slots=True)
class Page:
    url: str
    title: str
    blocks: tuple[Block, ...]

    @property
    def text(self) -> str:
        return "\n".join(block.text for block in self.blocks)


def _clean(text: str) -> str:
    return _SPACES.sub(" ", text).strip()


def extract_page(url: str, html: str) -> Page:
    tree = LexborHTMLParser(html)
    title_node = tree.css_first("title")
    for node in tree.css(_NOISE):
        node.decompose()
    root = tree.css_first("main") or tree.css_first("article") or tree.body
    blocks: list[Block] = []
    if root is not None:
        for node in root.css(_BLOCKS):
            # абзац внутри пункта списка возьмём сам по себе, без дубля от родителя.
            # css() в selectolax находит и сам узел, поэтому «есть вложенные блоки» — это больше одного
            if node.tag not in _HEADINGS and len(node.css(_BLOCKS)) > 1:
                continue
            text = _clean(node.text(separator=" "))
            if not text:
                continue
            level = int(node.tag[1]) if node.tag in _HEADINGS else 0
            blocks.append(Block(text, level))
    h1 = next((block.text for block in blocks if block.heading_level == 1), None)
    title = h1 or (_clean(title_node.text()) if title_node else "") or urlsplit(url).path or url
    return Page(url=url, title=title[:500], blocks=tuple(blocks))


def extract_links(base_url: str, html: str) -> list[str]:
    """Ссылки страницы: абсолютные, без якорей; фильтр по сайту — у обходчика."""
    tree = LexborHTMLParser(html)
    links: list[str] = []
    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue
        absolute = urljoin(base_url, href).split("#", 1)[0]
        if absolute not in links:
            links.append(absolute)
    return links
