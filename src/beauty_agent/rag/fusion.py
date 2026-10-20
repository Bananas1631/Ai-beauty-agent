"""Reciprocal Rank Fusion: объединить несколько ранжированных списков в один.

score(документ) = Σ 1 / (k + место в списке), места с 1. RRF не сводит несравнимые оценки
(косинус, ts_rank, оценки другого сервиса) к одной шкале — важен только порядок. k = 60 —
значение из исходной статьи (Cormack и др., 2009): большие k сглаживают вклад первых мест.

В PostgreSQL то же самое считает SQL-запрос гибридного поиска (db/repositories/knowledge.py);
здесь — для случаев, когда списки приходят из разных мест: индексатор ищет векторный и
полнотекстовый топы отдельными запросами, а агент смешивает фрагменты сайта из индексатора
с FAQ своего модуля.
"""

from collections.abc import Hashable, Sequence

RRF_K = 60


def reciprocal_rank_fusion[T: Hashable](
    rankings: Sequence[Sequence[T]], *, k: int = RRF_K, limit: int | None = None
) -> list[tuple[T, float]]:
    """Элементы по убыванию суммарной оценки; при равенстве — в порядке первого появления.

    Повтор элемента внутри одного списка не добавляет оценку: считается лучшее место.
    """
    if k < 1:
        raise ValueError("k должен быть положительным")
    scores: dict[T, float] = {}
    for ranking in rankings:
        seen: set[T] = set()
        for place, item in enumerate(ranking, start=1):
            if item in seen:
                continue
            seen.add(item)
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + place)
    ordered = sorted(scores.items(), key=lambda pair: -pair[1])  # sorted устойчива: ничьи — по появлению
    return ordered if limit is None else ordered[:limit]
