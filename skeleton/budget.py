"""confirm holdout の query-budget（DESIGN-SUPPLEMENT.md §A.1）。

fresh confirm slice を毎サイクル同じ seed で叩き続けると longitudinal な holdout 枯渇
（adaptive data analysis・fresh slice は有限/非定常）が起きる。各 holdout slice に
「採否判断に使える回数の上限」を持たせ、消費を KB に永続追跡する。pool の slice を
予算まで使ったら retire し次の fresh slice へ rotate。pool 全枯渇なら confirm 不可
（採用を止める）。

honest: この装置は情報を増やさない。holdout 枯渇を **silent な overfit から loud な停止へ
変える**だけ。本当の床は genuinely fresh な外部データ源（有限・非定常）で、Thresholdout
（DP 的再利用・`thresholdout.py` に実装。`--confirm-policy thresholdout`）もその先に
privacy-budget の天井を持つ。連続自己改善の内部測定の天井は残る。
"""
from __future__ import annotations


class ConfirmBudget:
    """pool の holdout slice を順に消費する rotation ポリシー。

    状態は全て kb 永続（このオブジェクト自体は stateless）なので、run を越えて、
    また同じ kb から作り直しても消費が継続する（= longitudinal に効く）。
    """

    def __init__(self, kb, pool: list, per_slice_budget: int) -> None:
        if per_slice_budget < 1:
            raise ValueError("per_slice_budget は 1 以上")
        if not pool:
            raise ValueError("pool が空")
        self.kb = kb
        self.pool = list(pool)
        self.per_slice_budget = per_slice_budget

    def spend(self) -> int | None:
        """予算の残る最初の slice を 1 クエリ消費して seed を返す。全枯渇なら None。"""
        for seed in self.pool:
            if self.kb.holdout_spend(seed, self.per_slice_budget):
                return seed
        return None   # pool 全 slice 枯渇 = longitudinal exhaustion（要 fresh data）

    def status(self) -> list:
        """各 slice の消費状況（seed / spent / budget / exhausted）。"""
        spent = {r["seed"]: r["spent"] for r in self.kb.holdout_rows()}
        return [
            {"seed": s, "spent": spent.get(s, 0), "budget": self.per_slice_budget,
             "exhausted": spent.get(s, 0) >= self.per_slice_budget}
            for s in self.pool
        ]
