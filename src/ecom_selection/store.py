"""任务持久化、报告多版本存储（含 Diff）及 Redis 任务去重/并发限流仓储。"""

import difflib
import hashlib
import json
import uuid
from typing import Any

import psycopg
import redis

from ecom_selection.config import Settings

INIT_SQL = """
CREATE SCHEMA IF NOT EXISTS ecom_selection;

CREATE TABLE IF NOT EXISTS ecom_selection.research_tasks (
    task_id             TEXT PRIMARY KEY,
    topic_hash          TEXT NOT NULL,
    topic               TEXT NOT NULL,
    target_pet          TEXT NOT NULL DEFAULT '猫',
    status              TEXT NOT NULL, -- running | awaiting_human_review | approved | rejected | failed
    require_human_gate  BOOLEAN NOT NULL DEFAULT FALSE,
    current_node        TEXT,
    rewrite_count       INTEGER NOT NULL DEFAULT 0,
    coverage_gaps_json  JSONB NOT NULL DEFAULT '[]'::jsonb,
    screening_json      JSONB NOT NULL DEFAULT '{}'::jsonb,
    market_evidence_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    user_evidence_json  JSONB NOT NULL DEFAULT '[]'::jsonb,
    audit_issues_json   JSONB NOT NULL DEFAULT '[]'::jsonb,
    node_timings_json   JSONB NOT NULL DEFAULT '{}'::jsonb,
    human_note          TEXT,
    total_prompt_tokens INTEGER NOT NULL DEFAULT 0,
    total_completion_tokens INTEGER NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ecom_selection.report_versions (
    id                  BIGSERIAL PRIMARY KEY,
    task_id             TEXT NOT NULL REFERENCES ecom_selection.research_tasks(task_id) ON DELETE CASCADE,
    version_no          INTEGER NOT NULL,
    stage_label         TEXT NOT NULL, -- v1_initial_draft | v2_audit_rewrite | v3_human_approved
    markdown_content    TEXT NOT NULL,
    audit_passed        BOOLEAN NOT NULL DEFAULT FALSE,
    audit_issues_json   JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (task_id, version_no)
);
"""


def compute_topic_hash(topic: str, target_pet: str) -> str:
    norm = f"{' '.join(topic.strip().lower().split())}|{target_pet.strip()}"
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:24]


def compute_markdown_diff(old_md: str, new_md: str, from_label: str = "V1", to_label: str = "V2") -> str:
    """生成可读的报告章节级 unified diff，方便运营评审对比修改前后内容。"""
    diff_lines = difflib.unified_diff(
        (old_md or "").splitlines(),
        (new_md or "").splitlines(),
        fromfile=from_label,
        tofile=to_label,
        lineterm="",
        n=2,
    )
    return "\n".join(diff_lines)


class SelectionStore:
    """管理任务生命周期、版本快照、Redis 并发限流与重复提交防重。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._redis = redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=3)

    def ensure_schema(self) -> None:
        with psycopg.connect(self._settings.postgres_dsn) as conn, conn.cursor() as cur:
            cur.execute(INIT_SQL)
            conn.commit()

    def health_check(self) -> dict[str, Any]:
        with psycopg.connect(self._settings.postgres_dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM ecom_selection.research_tasks;")
            tasks = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM ecom_selection.report_versions;")
            versions = cur.fetchone()[0]
        return {"postgres": True, "redis": bool(self._redis.ping()), "tasks": tasks, "report_versions": versions}

    def acquire_task_slot(self, topic_hash: str) -> tuple[bool, str | None, str | None]:
        """防重复提交 + 限制最大同时运行任务数（Redis 原子锁）。"""
        dedup_key = f"ecom_sel:running:{topic_hash}"
        existing = self._redis.get(dedup_key)
        if existing:
            return False, f"该调研主题已有进行中的任务 ({existing})，已阻止重复消耗检索与模型资源。", existing

        active_set = "ecom_sel:active_tasks"
        active_cnt = int(self._redis.scard(active_set))
        if active_cnt >= self._settings.max_concurrent_tasks:
            return (
                False,
                f"当前并发任务数已达上限（{active_cnt}/{self._settings.max_concurrent_tasks}），请稍后重试。",
                None,
            )
        task_id = f"SEL-{uuid.uuid4().hex[:10].upper()}"
        self._redis.setex(dedup_key, 300, task_id)
        self._redis.sadd(active_set, task_id)
        return True, None, task_id

    def release_task_slot(self, topic_hash: str, task_id: str) -> None:
        self._redis.delete(f"ecom_sel:running:{topic_hash}")
        self._redis.srem("ecom_sel:active_tasks", task_id)

    def save_task(self, payload: dict[str, Any]) -> None:
        sql = """
            INSERT INTO ecom_selection.research_tasks (
                task_id, topic_hash, topic, target_pet, status, require_human_gate,
                current_node, rewrite_count, coverage_gaps_json, screening_json,
                market_evidence_json, user_evidence_json, audit_issues_json,
                node_timings_json, human_note, total_prompt_tokens, total_completion_tokens, updated_at
            )
            VALUES (
                %(task_id)s, %(topic_hash)s, %(topic)s, %(target_pet)s, %(status)s, %(require_human_gate)s,
                %(current_node)s, %(rewrite_count)s, %(coverage_gaps_json)s::jsonb, %(screening_json)s::jsonb,
                %(market_evidence_json)s::jsonb, %(user_evidence_json)s::jsonb, %(audit_issues_json)s::jsonb,
                %(node_timings_json)s::jsonb, %(human_note)s, %(total_prompt_tokens)s, %(total_completion_tokens)s, NOW()
            )
            ON CONFLICT (task_id) DO UPDATE SET
                status = EXCLUDED.status,
                current_node = EXCLUDED.current_node,
                rewrite_count = EXCLUDED.rewrite_count,
                coverage_gaps_json = EXCLUDED.coverage_gaps_json,
                screening_json = EXCLUDED.screening_json,
                market_evidence_json = EXCLUDED.market_evidence_json,
                user_evidence_json = EXCLUDED.user_evidence_json,
                audit_issues_json = EXCLUDED.audit_issues_json,
                node_timings_json = EXCLUDED.node_timings_json,
                human_note = EXCLUDED.human_note,
                total_prompt_tokens = EXCLUDED.total_prompt_tokens,
                total_completion_tokens = EXCLUDED.total_completion_tokens,
                updated_at = NOW();
        """
        params = {
            **payload,
            "coverage_gaps_json": json.dumps(payload.get("coverage_gaps", []), ensure_ascii=False),
            "screening_json": json.dumps(payload.get("screening", {}), ensure_ascii=False),
            "market_evidence_json": json.dumps(payload.get("market_evidence", []), ensure_ascii=False),
            "user_evidence_json": json.dumps(payload.get("user_evidence", []), ensure_ascii=False),
            "audit_issues_json": json.dumps(payload.get("audit_issues", []), ensure_ascii=False),
            "node_timings_json": json.dumps(payload.get("node_timings", {}), ensure_ascii=False),
        }
        with psycopg.connect(self._settings.postgres_dsn) as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            conn.commit()

    def add_report_version(
        self,
        task_id: str,
        stage_label: str,
        markdown_content: str,
        audit_passed: bool,
        audit_issues: list[str],
    ) -> int:
        with psycopg.connect(self._settings.postgres_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 FROM ecom_selection.report_versions WHERE task_id = %s;",
                (task_id,),
            )
            ver = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO ecom_selection.report_versions (
                    task_id, version_no, stage_label, markdown_content, audit_passed, audit_issues_json
                ) VALUES (%s, %s, %s, %s, %s, %s::jsonb);
                """,
                (task_id, ver, stage_label, markdown_content, audit_passed, json.dumps(audit_issues, ensure_ascii=False)),
            )
            conn.commit()
        return ver

    def get_task_detail(self, task_id: str) -> dict[str, Any] | None:
        with psycopg.connect(self._settings.postgres_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT task_id, topic_hash, topic, target_pet, status, require_human_gate,
                       current_node, rewrite_count, coverage_gaps_json, screening_json,
                       market_evidence_json, user_evidence_json, audit_issues_json,
                       node_timings_json, human_note, total_prompt_tokens, total_completion_tokens,
                       created_at, updated_at
                FROM ecom_selection.research_tasks
                WHERE task_id = %s;
                """,
                (task_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            cur.execute(
                """
                SELECT version_no, stage_label, markdown_content, audit_passed, audit_issues_json, created_at
                FROM ecom_selection.report_versions
                WHERE task_id = %s
                ORDER BY version_no ASC;
                """,
                (task_id,),
            )
            versions = [
                {
                    "version_no": v[0],
                    "stage_label": v[1],
                    "markdown_content": v[2],
                    "audit_passed": v[3],
                    "audit_issues": v[4],
                    "created_at": v[5].isoformat(),
                }
                for v in cur.fetchall()
            ]

        diff_text = ""
        if len(versions) >= 2:
            diff_text = compute_markdown_diff(
                versions[0]["markdown_content"],
                versions[-1]["markdown_content"],
                from_label=f"V{versions[0]['version_no']} ({versions[0]['stage_label']})",
                to_label=f"V{versions[-1]['version_no']} ({versions[-1]['stage_label']})",
            )

        return {
            "task_id": row[0],
            "topic_hash": row[1],
            "topic": row[2],
            "target_pet": row[3],
            "status": row[4],
            "require_human_gate": row[5],
            "current_node": row[6],
            "rewrite_count": row[7],
            "coverage_gaps": row[8],
            "screening": row[9],
            "market_evidence": row[10],
            "user_evidence": row[11],
            "audit_issues": row[12],
            "node_timings": row[13],
            "human_note": row[14],
            "total_prompt_tokens": row[15],
            "total_completion_tokens": row[16],
            "created_at": row[17].isoformat(),
            "updated_at": row[18].isoformat(),
            "versions": versions,
            "latest_diff": diff_text,
        }

    def list_tasks(self, limit: int = 20) -> list[dict[str, Any]]:
        with psycopg.connect(self._settings.postgres_dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT task_id, topic, status, rewrite_count, current_node, created_at
                FROM ecom_selection.research_tasks
                ORDER BY created_at DESC
                LIMIT %s;
                """,
                (limit,),
            )
            return [
                {
                    "task_id": r[0],
                    "topic": r[1],
                    "status": r[2],
                    "rewrite_count": r[3],
                    "current_node": r[4],
                    "created_at": r[5].isoformat(),
                }
                for r in cur.fetchall()
            ]
