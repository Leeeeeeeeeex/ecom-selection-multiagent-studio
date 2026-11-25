"""项目三 Langfuse v2 多 Agent 链路追踪与审批评分上报模块。"""

import base64
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ecom_selection.config import Settings


class SelectionObservability:
    """上报多 Agent 选品调研 Trace、各节点耗时 Span、Token 用量与人工审批结果。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _ready(self) -> bool:
        s = self._settings
        return bool(
            s.langfuse_enabled
            and s.langfuse_public_key
            and s.langfuse_secret_key
            and "xxxx" not in s.langfuse_public_key
            and "xxxx" not in s.langfuse_secret_key
        )

    def _post(self, batch: list[dict[str, Any]]) -> bool:
        token = base64.b64encode(
            f"{self._settings.langfuse_public_key}:{self._settings.langfuse_secret_key}".encode("utf-8")
        ).decode("ascii")
        url = f"{self._settings.langfuse_host.rstrip('/')}/api/public/ingestion"
        resp = httpx.post(
            url,
            headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
            json={"batch": batch},
            timeout=5.0,
        )
        return resp.status_code in (200, 201, 207)

    def record_task_trace(
        self,
        task_id: str,
        topic: str,
        status: str,
        node_timings: dict[str, int],
        rewrite_count: int,
        coverage_gaps: list[str],
        prompt_tokens: int,
        completion_tokens: int,
        audit_passed: bool,
    ) -> None:
        if not self._ready():
            return
        try:
            total_ms = sum(v for v in node_timings.values() if isinstance(v, int))
            now = datetime.now(timezone.utc)
            t_start = now - timedelta(milliseconds=max(1, total_ms))
            iso_now = now.isoformat().replace("+00:00", "Z")
            iso_start = t_start.isoformat().replace("+00:00", "Z")

            batch: list[dict[str, Any]] = [
                {
                    "id": uuid.uuid4().hex,
                    "type": "trace-create",
                    "timestamp": iso_now,
                    "body": {
                        "id": task_id,
                        "timestamp": iso_start,
                        "name": "ecom-selection-multiagent",
                        "input": {"topic": topic},
                        "output": {"status": status, "rewrite_count": rewrite_count, "coverage_gaps": coverage_gaps},
                        "tags": ["selection-multiagent", status, f"rewrites-{rewrite_count}"],
                        "metadata": {
                            "node_timings": node_timings,
                            "coverage_gap_count": len(coverage_gaps),
                        },
                    },
                },
                {
                    "id": uuid.uuid4().hex,
                    "type": "generation-create",
                    "timestamp": iso_now,
                    "body": {
                        "id": uuid.uuid4().hex,
                        "traceId": task_id,
                        "name": "multiagent_llm_total",
                        "model": self._settings.llm_model,
                        "startTime": iso_start,
                        "endTime": iso_now,
                        "input": topic,
                        "output": status,
                        "usage": {
                            "input": prompt_tokens,
                            "output": completion_tokens,
                            "total": prompt_tokens + completion_tokens,
                            "unit": "TOKENS",
                        },
                    },
                },
                {
                    "id": uuid.uuid4().hex,
                    "type": "score-create",
                    "timestamp": iso_now,
                    "body": {
                        "id": uuid.uuid4().hex,
                        "traceId": task_id,
                        "name": "evidence_audit_passed",
                        "value": 1.0 if audit_passed else 0.0,
                    },
                },
            ]
            self._post(batch)
        except Exception:
            pass

    def record_human_decision(self, task_id: str, decision: str, note: str) -> None:
        if not self._ready():
            return
        try:
            iso_now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            self._post(
                [
                    {
                        "id": uuid.uuid4().hex,
                        "type": "score-create",
                        "timestamp": iso_now,
                        "body": {
                            "id": uuid.uuid4().hex,
                            "traceId": task_id,
                            "name": "human_approval",
                            "value": 1.0 if decision in ("approve", "revise") else 0.0,
                            "comment": f"{decision}: {note}",
                        },
                    }
                ]
            )
        except Exception:
            pass
