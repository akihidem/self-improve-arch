"""Knowledge Base（sqlite）。

出典: improvement_loop_example.py L260-306 の sqlite KB を、この gate-only
スケルトン向けに簡素化したもの（Proposal ではなく Candidate を記録）。

各サイクルの「候補名・テスト合否・主要KPIの相対変化・採否・理由・ゲート詳細」を
追記する。採否は呼び出し側（loop）が gate の戻り値から渡す（再判定しない）。

confirm holdout の query-budget も永続追跡する（holdouts 表）。これは longitudinal
（複数サイクル/複数 run をまたぐ）な holdout 枯渇を縛るためで、消費が run を越えて
残ることが肝（再実行で予算がリセットされない）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments(
  id INTEGER PRIMARY KEY, objective TEXT, candidate TEXT,
  source_sha TEXT, created REAL);
CREATE TABLE IF NOT EXISTS decisions(
  exp_id INTEGER, adopted INTEGER, tests_passed INTEGER,
  primary_rel REAL, reason TEXT, gate_detail TEXT);
CREATE TABLE IF NOT EXISTS artifacts(sha TEXT PRIMARY KEY, body TEXT);
CREATE TABLE IF NOT EXISTS holdouts(
  seed INTEGER PRIMARY KEY, query_budget INTEGER, queries_spent INTEGER);
"""


class KnowledgeBase:
    def __init__(self, path: str = ":memory:") -> None:
        self.cx = sqlite3.connect(path)
        self.cx.executescript(SCHEMA)

    @staticmethod
    def _sha(text: str) -> str:
        return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]

    def record(self, *, objective: str, candidate_name: str, candidate_source: str,
               adopted: bool, tests_passed: bool, primary_rel: float,
               reasons: list, gate_detail: dict) -> int:
        sha = self._sha(candidate_source)
        self.cx.execute("INSERT OR IGNORE INTO artifacts(sha, body) VALUES(?,?)",
                        (sha, candidate_source))
        cur = self.cx.execute(
            "INSERT INTO experiments(objective, candidate, source_sha, created) "
            "VALUES(?,?,?,?)", (objective, candidate_name, sha, time.time()),
        )
        exp_id = cur.lastrowid
        self.cx.execute(
            "INSERT INTO decisions(exp_id, adopted, tests_passed, primary_rel, "
            "reason, gate_detail) VALUES(?,?,?,?,?,?)",
            (exp_id, int(adopted), int(tests_passed), primary_rel,
             "; ".join(reasons), json.dumps(gate_detail, ensure_ascii=False)),
        )
        self.cx.commit()
        return exp_id

    def recent(self, limit: int = 10) -> list:
        cur = self.cx.execute(
            "SELECT e.candidate, d.adopted, d.tests_passed, d.primary_rel, d.reason "
            "FROM experiments e LEFT JOIN decisions d ON d.exp_id=e.id "
            "ORDER BY e.id DESC LIMIT ?", (limit,),
        )
        return [
            {"candidate": c, "adopted": bool(a) if a is not None else None,
             "tests_passed": bool(t) if t is not None else None,
             "primary_rel": r, "reason": reason}
            for c, a, t, r, reason in cur.fetchall()
        ]

    def holdout_spend(self, seed: int, budget: int) -> bool:
        """confirm holdout(seed) に 1 クエリ課金する。

        消費が budget 未満なら +1 して True（このクエリは使える）。既に budget 到達なら
        課金せず False（枯渇＝retire）。消費は永続するので run を越えて残る。
        """
        row = self.cx.execute(
            "SELECT queries_spent FROM holdouts WHERE seed=?", (seed,)
        ).fetchone()
        if row is None:
            self.cx.execute(
                "INSERT INTO holdouts(seed, query_budget, queries_spent) VALUES(?,?,0)",
                (seed, budget),
            )
            spent = 0
        else:
            spent = row[0]
        if spent >= budget:
            return False
        self.cx.execute(
            "UPDATE holdouts SET queries_spent=queries_spent+1 WHERE seed=?", (seed,)
        )
        self.cx.commit()
        return True

    def holdout_rows(self) -> list:
        """holdout の消費状況（表示・テスト用）。"""
        cur = self.cx.execute(
            "SELECT seed, query_budget, queries_spent FROM holdouts ORDER BY seed"
        )
        return [{"seed": s, "budget": b, "spent": sp} for s, b, sp in cur.fetchall()]
