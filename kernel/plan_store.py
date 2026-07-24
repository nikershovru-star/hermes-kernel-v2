"""kernel/plan_store.py — Plan + ExecutionOutcome persistence (ADR-024).

In-memory CRUD + optional SQLite, mirroring the ADR-023 ``SwarmStore`` pattern.
Tables: ``plans (plan_id TEXT PRIMARY KEY, data TEXT)`` and
``outcomes (outcome_id TEXT PRIMARY KEY, plan_id TEXT, data TEXT)``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from kernel.domain import ExecutionOutcome, Plan


class PlanStore:
    """Persistence for ``Plan`` and ``ExecutionOutcome`` (ADR-024)."""

    def __init__(self, db_path: str | None = None) -> None:
        self._mem: dict[str, Plan] = {}
        self._mem_outcomes: dict[str, ExecutionOutcome] = {}
        self._db = db_path
        if db_path is not None:
            self._init_db()
            self._load_all()

    # -- sqlite ----------------------------------------------------------- #
    def _init_db(self) -> None:
        assert self._db is not None
        conn = sqlite3.connect(self._db)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS plans (plan_id TEXT PRIMARY KEY, data TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS outcomes "
                "(outcome_id TEXT PRIMARY KEY, plan_id TEXT, data TEXT)"
            )
            conn.commit()
        finally:
            conn.close()

    def _load_all(self) -> None:
        assert self._db is not None
        conn = sqlite3.connect(self._db)
        try:
            for plan_id, data in conn.execute("SELECT plan_id, data FROM plans"):
                self._mem[plan_id] = Plan.model_validate_json(data)
            for outcome_id, _, data in conn.execute(
                "SELECT outcome_id, plan_id, data FROM outcomes"
            ):
                self._mem_outcomes[outcome_id] = ExecutionOutcome.model_validate_json(data)
        finally:
            conn.close()

    # -- plans ------------------------------------------------------------ #
    def put(self, plan: Plan) -> None:
        self._mem[plan.plan_id] = plan
        if self._db is not None:
            conn = sqlite3.connect(self._db)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO plans (plan_id, data) VALUES (?, ?)",
                    (plan.plan_id, plan.model_dump_json()),
                )
                conn.commit()
            finally:
                conn.close()

    def get(self, plan_id: str) -> Plan | None:
        if plan_id in self._mem:
            return self._mem[plan_id]
        if self._db is not None:
            conn = sqlite3.connect(self._db)
            try:
                row = conn.execute(
                    "SELECT data FROM plans WHERE plan_id = ?", (plan_id,)
                ).fetchone()
            finally:
                conn.close()
            if row is not None:
                return Plan.model_validate_json(row[0])
        return None

    def delete(self, plan_id: str) -> bool:
        existed = plan_id in self._mem
        self._mem.pop(plan_id, None)
        if self._db is not None:
            conn = sqlite3.connect(self._db)
            try:
                cur = conn.execute("DELETE FROM plans WHERE plan_id = ?", (plan_id,))
                conn.commit()
                existed = cur.rowcount > 0 or existed
            finally:
                conn.close()
        return existed

    def list_by_workflow(self, workflow_id: str) -> list[Plan]:
        return [p for p in self._mem.values() if p.workflow_id == workflow_id]

    # -- outcomes --------------------------------------------------------- #
    def put_outcome(self, outcome: ExecutionOutcome) -> None:
        self._mem_outcomes[outcome.outcome_id] = outcome
        if self._db is not None:
            conn = sqlite3.connect(self._db)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO outcomes (outcome_id, plan_id, data) VALUES (?, ?, ?)",
                    (outcome.outcome_id, outcome.plan_id, outcome.model_dump_json()),
                )
                conn.commit()
            finally:
                conn.close()

    def get_outcome(self, outcome_id: str) -> ExecutionOutcome | None:
        return self._mem_outcomes.get(outcome_id)

    def outcomes_for(self, plan_id: str) -> list[ExecutionOutcome]:
        return [o for o in self._mem_outcomes.values() if o.plan_id == plan_id]


__all__ = ["PlanStore"]
