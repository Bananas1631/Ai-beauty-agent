"""Reciprocal Rank Fusion: сумма 1/(k + место), устойчивость к повторам и ничьим."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from beauty_agent.rag.fusion import reciprocal_rank_fusion


def test_item_in_both_lists_wins() -> None:
    fused = reciprocal_rank_fusion([["a", "b", "c"], ["c", "d"]])

    assert [item for item, _ in fused] == ["c", "a", "b", "d"]
    assert fused[0][1] == pytest.approx(1 / 63 + 1 / 61)


def test_limit_and_empty() -> None:
    assert reciprocal_rank_fusion([[], []]) == []
    assert [item for item, _ in reciprocal_rank_fusion([["a", "b", "c"]], limit=2)] == ["a", "b"]


def test_ties_keep_first_appearance() -> None:
    fused = reciprocal_rank_fusion([["a"], ["b"]])

    assert [item for item, _ in fused] == ["a", "b"]


def test_duplicate_inside_one_list_counts_once() -> None:
    fused = dict(reciprocal_rank_fusion([["a", "a", "b"]]))

    assert fused["a"] == pytest.approx(1 / 61)
    assert fused["b"] == pytest.approx(1 / 63)  # место считается по позиции в списке


def test_k_must_be_positive() -> None:
    with pytest.raises(ValueError, match="k"):
        reciprocal_rank_fusion([["a"]], k=0)


@given(st.lists(st.lists(st.integers(0, 20), max_size=10), max_size=4))
def test_scores_are_sorted_and_cover_all_items(rankings: list[list[int]]) -> None:
    fused = reciprocal_rank_fusion(rankings)
    scores = [score for _, score in fused]

    assert scores == sorted(scores, reverse=True)
    assert {item for item, _ in fused} == {item for ranking in rankings for item in ranking}
