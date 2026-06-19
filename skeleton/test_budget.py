"""query-budget: confirm holdout の消費が rotate→枯渇し、KB に永続することを検証。

in-memory KB（sandbox 非依存・決定的）で、ConfirmBudget の pool rotation と
longitudinal な消費持続（作り直しても残る）を確認する。
"""
from budget import ConfirmBudget
from kb import KnowledgeBase


def test_budget_rotates_then_exhausts():
    kb = KnowledgeBase(":memory:")
    b = ConfirmBudget(kb, pool=[111, 222], per_slice_budget=1)
    assert b.spend() == 111    # slice 1
    assert b.spend() == 222    # slice 1 枯渇 → 次の fresh slice へ rotate
    assert b.spend() is None    # 全 slice 枯渇 = longitudinal exhaustion


def test_budget_per_slice_limit():
    kb = KnowledgeBase(":memory:")
    b = ConfirmBudget(kb, pool=[111], per_slice_budget=2)
    assert b.spend() == 111
    assert b.spend() == 111
    assert b.spend() is None    # 1 slice あたり 2 回で上限


def test_budget_persists_across_instances():
    kb = KnowledgeBase(":memory:")
    ConfirmBudget(kb, [111], 2).spend()         # 1 回消費
    # 同じ kb で ConfirmBudget を作り直しても消費は継続（リセットされない）
    b2 = ConfirmBudget(kb, [111], 2)
    assert b2.spend() == 111    # 残り 1
    assert b2.spend() is None    # 使い切り（再実行で予算が戻らない＝longitudinal に効く）


def test_kb_holdout_spend_charges_and_caps():
    kb = KnowledgeBase(":memory:")
    assert kb.holdout_spend(seed=7, budget=2) is True
    assert kb.holdout_spend(seed=7, budget=2) is True
    assert kb.holdout_spend(seed=7, budget=2) is False   # 上限到達で課金せず False
    rows = {r["seed"]: r for r in kb.holdout_rows()}
    assert rows[7]["spent"] == 2 and rows[7]["budget"] == 2
