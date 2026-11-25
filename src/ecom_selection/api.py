"""项目三 FastAPI 服务入口（多 Agent 选品调研、HITL 审批恢复、版本 Diff 与 Markdown 导出）。"""

from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ecom_selection.agents import SelectionWorkflowService
from ecom_selection.config import get_settings
from ecom_selection.observability import SelectionObservability
from ecom_selection.store import SelectionStore

_STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(
    title="跨境电商多 Agent 选品调研与人工审批工作台",
    version="0.2.0",
)


class CreateTaskRequest(BaseModel):
    topic: str = Field(..., min_length=2, max_length=120, description="拟调研新品方向")
    target_pet: str = Field(default="猫", max_length=20)
    require_human_gate: bool = Field(default=True, description="是否在定稿前强制挂起人工审批")
    research_mode: Literal["parallel", "serial"] = Field(default="parallel")
    force_bad_draft_once: bool = Field(default=False, description="演示用：强制首稿违规以触发审稿打回重写与 Diff")


class HumanReviewRequest(BaseModel):
    decision: Literal["approve", "revise", "reject"]
    human_note: str = Field(default="", max_length=500)


def _get_service() -> tuple[SelectionStore, SelectionWorkflowService]:
    settings = get_settings()
    store = SelectionStore(settings)
    store.ensure_schema()
    obs = SelectionObservability(settings)
    return store, SelectionWorkflowService(settings=settings, store=store, obs=obs)


@app.get("/", include_in_schema=False)
def serve_frontend() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict:
    settings = get_settings()
    store = SelectionStore(settings)
    store.ensure_schema()
    info = store.health_check()
    return {"status": "ok", **info, "langfuse_url": settings.langfuse_host}


@app.get("/api/v1/tasks")
def list_tasks() -> dict:
    store, _ = _get_service()
    return {"items": store.list_tasks(limit=20)}


@app.post("/api/v1/tasks")
def create_research_task(req: CreateTaskRequest) -> dict:
    _, svc = _get_service()
    res = svc.run_research(
        topic=req.topic,
        target_pet=req.target_pet,
        require_human_gate=req.require_human_gate,
        research_mode=req.research_mode,
        force_bad_draft_once=req.force_bad_draft_once,
        persist=True,
    )
    if not res.get("accepted"):
        raise HTTPException(status_code=409, detail=res.get("error", "任务被去重或限流拦截"))
    return res["task"]


@app.get("/api/v1/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    store, _ = _get_service()
    detail = store.get_task_detail(task_id)
    if not detail:
        raise HTTPException(status_code=404, detail=f"未找到任务 {task_id}")
    return detail


@app.post("/api/v1/tasks/{task_id}/review")
def submit_human_review(task_id: str, req: HumanReviewRequest) -> dict:
    _, svc = _get_service()
    try:
        return svc.resume_human_review(task_id=task_id, decision=req.decision, human_note=req.human_note)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/v1/tasks/{task_id}/export")
def export_markdown(task_id: str) -> PlainTextResponse:
    store, _ = _get_service()
    detail = store.get_task_detail(task_id)
    if not detail or not detail.get("versions"):
        raise HTTPException(status_code=404, detail="无可导出的选品报告")
    latest = detail["versions"][-1]
    md = (
        f"# 选品调研报告：{detail['topic']}\n\n"
        f"- 任务编号：`{detail['task_id']}` | 状态：`{detail['status']}` | 版本：`V{latest['version_no']} ({latest['stage_label']})`\n\n"
        f"{latest['markdown_content']}\n"
    )
    return PlainTextResponse(
        content=md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{task_id}_report.md"'},
    )
