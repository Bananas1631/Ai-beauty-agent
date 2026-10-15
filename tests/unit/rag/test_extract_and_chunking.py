from itertools import pairwise
from pathlib import Path

from beauty_agent.agent.tokens import estimate_tokens
from beauty_agent.rag.chunking import MAX_TOKENS, MIN_TOKENS, chunk_blocks, chunk_page
from beauty_agent.rag.extract import Block, Page, extract_links, extract_page

SITES = Path(__file__).parents[3] / "evals" / "sites"


def site_page(site: str, name: str) -> Page:
    url = f"https://{site}.example/{name}"
    return extract_page(url, (SITES / site / name).read_text(encoding="utf-8"))


def test_noise_is_removed_and_structure_kept() -> None:
    page = site_page("lavanda", "prices.html")

    assert page.title == "Цены"
    assert "Политика конфиденциальности" not in page.text  # подвал
    assert "Подпишитесь" not in page.text  # боковая колонка
    assert "Перезвоните" not in page.text  # форма
    assert "window.analytics" not in page.text  # скрипт
    assert "Главная" not in page.text  # меню
    headings = [block.text for block in page.blocks if block.heading_level]
    assert headings == ["Цены", "Ногтевой сервис", "Брови и ресницы", "Подарочные сертификаты"]
    assert "Маникюр с покрытием Shellac — от 2 500 до 3 000 ₽" in page.text


def test_nested_blocks_are_not_duplicated() -> None:
    page = extract_page("https://x.example/", "<main><ul><li><p>Один абзац</p></li></ul></main>")

    assert [block.text for block in page.blocks] == ["Один абзац"]


def test_inline_tags_do_not_break_text() -> None:
    page = extract_page("https://x.example/", "<main><p>Покрытие <b>Shellac</b> держится долго</p></main>")

    assert page.blocks[0].text == "Покрытие Shellac держится долго"


def test_links() -> None:
    html = '<a href="/prices.html#top">Цены</a><a href="mailto:a@b.c">почта</a><a href="https://other.example/">x</a>'

    assert extract_links("https://x.example/index.html", html) == [
        "https://x.example/prices.html",
        "https://other.example/",
    ]


def long_page(paragraphs: int = 40) -> Page:
    blocks = [Block("Раздел", 2)]
    blocks += [
        Block(f"Абзац номер {index}. " + "Описание процедуры и ухода за ногтями. " * 6) for index in range(paragraphs)
    ]
    return Page("https://x.example/long", "Большая страница", tuple(blocks))


def test_chunks_are_within_limits_with_overlap() -> None:
    chunks = chunk_page(long_page())

    assert len(chunks) >= 3
    for chunk in chunks[:-1]:
        assert MIN_TOKENS <= chunk.tokens <= MAX_TOKENS
    assert chunks[-1].tokens <= MAX_TOKENS
    for previous, following in pairwise(chunks):
        overlap = following.text.splitlines()[0]
        assert previous.text.endswith(overlap)  # перекрытие: конец прошлого фрагмента — начало следующего
        assert 0 < estimate_tokens(overlap) <= 60
    assert [chunk.position for chunk in chunks] == list(range(len(chunks)))


def test_chunk_metadata() -> None:
    chunk = chunk_page(site_page("lavanda", "rules.html"))[0]

    assert chunk.url == "https://lavanda.example/rules.html"
    assert chunk.page_title == "Правила записи"
    assert chunk.section == "Отмена и перенос"
    assert chunk.for_embedding().startswith("Правила записи — Отмена и перенос\n")


def test_small_sections_are_merged() -> None:
    chunks = chunk_page(site_page("lavanda", "care.html"))

    assert len(chunks) == 1  # три коротких раздела — один фрагмент, а не три огрызка
    assert "без туши" in chunks[0].text
    assert "2–3 недели" in chunks[0].text


def test_huge_paragraph_is_split() -> None:
    text = "Очень длинное предложение без точек " * 400
    page = Page("https://x.example/", "Т", (Block(text),))

    chunks = chunk_page(page)

    assert len(chunks) > 1
    assert all(estimate_tokens(chunk.text) <= MAX_TOKENS for chunk in chunks)


def test_empty_page_has_no_chunks() -> None:
    assert chunk_page(Page("https://x.example/", "Пусто", ())) == []
    assert chunk_page(Page("https://x.example/", "Только заголовок", (Block("Заголовок", 1),))) == []


def test_plain_text_chunks() -> None:
    chunks = chunk_blocks("crm:company/1", "Карточка", "Адрес: ул. Примерная, 1\nЧасы: 10–20")

    assert len(chunks) == 1
    assert "ул. Примерная" in chunks[0].text
