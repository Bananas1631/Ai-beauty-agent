"""Нарезка страницы на фрагменты по 300–500 токенов с перекрытием и метаданными.

Небольшой фрагмент точнее находится поиском и дешевле обходится в промпте. Разрез идёт по
заголовкам и абзацам, а не посреди предложения; конец предыдущего фрагмента повторяется в начале
следующего (перекрытие), чтобы мысль на границе не потерялась.
"""

import re
from dataclasses import dataclass

from beauty_agent.agent.tokens import estimate_tokens
from beauty_agent.rag.extract import Block, Page

MIN_TOKENS = 300
MAX_TOKENS = 500
OVERLAP_TOKENS = 60
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


@dataclass(frozen=True, slots=True)
class Chunk:
    position: int
    url: str
    page_title: str
    section: str | None
    text: str

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)

    def for_embedding(self) -> str:
        """Заголовки страницы и раздела — часть смысла фрагмента: «Цены» без них не найти."""
        head = " — ".join(part for part in (self.page_title, self.section) if part)
        return f"{head}\n{self.text}" if head else self.text


def _split_long(text: str, limit: int) -> list[str]:
    """Абзац длиннее лимита режется по предложениям, а предложение-гигант — по словам."""
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(text):
        candidate = f"{current} {sentence}".strip()
        if estimate_tokens(candidate) <= limit:
            current = candidate
            continue
        if current:
            pieces.append(current)
        if estimate_tokens(sentence) <= limit:
            current = sentence
            continue
        words, current = sentence.split(), ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if estimate_tokens(candidate) > limit and current:
                pieces.append(current)
                current = word
            else:
                current = candidate
    if current:
        pieces.append(current)
    return pieces


def _tail(paragraphs: list[str], budget: int) -> list[str]:
    """Перекрытие со следующим фрагментом: последние абзацы общим объёмом не больше budget.

    Если даже последний абзац длиннее бюджета — переносятся его последние слова: без этого
    на страницах из длинных абзацев перекрытия не было бы совсем.
    """
    tail: list[str] = []
    for paragraph in reversed(paragraphs):
        if estimate_tokens(" ".join([paragraph, *tail])) > budget:
            break
        tail.insert(0, paragraph)
    if tail or not paragraphs:
        return tail
    words: list[str] = []
    for word in reversed(paragraphs[-1].split()):
        if estimate_tokens(" ".join([word, *words])) > budget:
            break
        words.insert(0, word)
    return [" ".join(words)] if words else []


def chunk_page(
    page: Page,
    *,
    min_tokens: int = MIN_TOKENS,
    max_tokens: int = MAX_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    section: str | None = None
    chunk_section: str | None = None
    current: list[str] = []
    fresh = 0  # абзацы текущего фрагмента, ещё не попавшие ни в один фрагмент

    def emit() -> None:
        nonlocal current, fresh
        if current and fresh:
            chunks.append(Chunk(len(chunks), page.url, page.title, chunk_section, "\n".join(current)))
        current = _tail(current, overlap_tokens) if current else []
        fresh = 0

    def size(paragraphs: list[str]) -> int:
        return estimate_tokens("\n".join(paragraphs))

    for block in page.blocks:
        if block.heading_level:
            if size(current) >= min_tokens:
                emit()
                current = []  # новый раздел начинается без хвоста прошлого
            section = block.text
            continue
        for paragraph in _split_long(block.text, max_tokens - overlap_tokens):
            if current and size([*current, paragraph]) > max_tokens:
                emit()
            if not fresh:
                chunk_section = section
            current.append(paragraph)
            fresh += 1
    emit()
    return chunks


def chunk_blocks(url: str, title: str, text: str) -> list[Chunk]:
    """Фрагменты обычного текста (карточка филиала, FAQ) — тем же алгоритмом, что и страницы."""
    blocks = tuple(Block(line.strip()) for line in text.splitlines() if line.strip())
    return chunk_page(Page(url, title, blocks))
