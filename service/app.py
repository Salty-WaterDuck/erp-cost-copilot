"""FastAPI 只读服务：把 CLI 的排雷、归因、反证、RAG、评测包成 HTTP 接口。

这是可选依赖层：核心引擎仍然零依赖，只有跑 API / Docker 时才安装
``service/requirements.txt``。服务不写库、不改账，只做计算与解释。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from costcontrol import __version__
from costcontrol.audit import detect_anomalies
from costcontrol.challenge import challenge, pick_target
from costcontrol.config_translator import parse_config, render_offline_translation
from costcontrol.dataset import load_dataset
from costcontrol.diagnosis import diagnose_facts
from costcontrol.evaluation import run_all
from costcontrol.facts import build_variance_facts
from costcontrol.llm_explainer import LLMExplainer, load_sense
from costcontrol.rag import search
from costcontrol.run import run_cost

ROOT = Path(__file__).resolve().parent.parent

DATASETS = {
    "demo": ROOT / "data" / "demo_ningbo.json",
    "november": ROOT / "data" / "demo_ningbo_2026_11.json",
    "dirty": ROOT / "data" / "dirty_erp_export.json",
    "fixed": ROOT / "data" / "dirty_erp_export_fixed.json",
}

app = FastAPI(
    title="ERP-Cost-Copilot API",
    description="成本排雷、强归因、反证复核、RAG 检索与评测（只读）",
    version=__version__,
)


class RagRequest(BaseModel):
    query: str = Field(min_length=2, max_length=200)
    k: int = Field(default=3, ge=1, le=10)


class ChallengeRequest(BaseModel):
    dataset: str = Field(default="demo")
    text: str = Field(min_length=2, max_length=500)


class TranslateRequest(BaseModel):
    text: str = Field(min_length=10, max_length=20000)


def _load(name: str):
    if name not in DATASETS:
        raise HTTPException(status_code=404, detail=f"未知数据集：{name}")
    path = DATASETS[name]
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"数据集文件缺失：{path.name}")
    return load_dataset(path)


def _fact_payload(fact) -> dict:
    return {
        "fact_id": getattr(fact, "fact_id", ""),
        "cost_element": fact.cost_element,
        "label": fact.label,
        "amount": str(fact.amount),
        "order_no": fact.order_no,
        "product": fact.product,
        "material": fact.material,
        "cost_center": fact.cost_center,
    }


def _diagnosis_payload(diagnosis) -> dict:
    top = diagnosis.top
    return {
        "fact": _fact_payload(diagnosis.fact),
        "top": (
            None
            if top is None
            else {
                "code": top.code,
                "label": top.label,
                "score": top.score,
                "confidence": top.confidence,
                "evidence": top.evidence,
                "falsifier": top.falsifier,
            }
        ),
        "alternatives": [
            {"code": item.code, "label": item.label, "score": item.score}
            for item in diagnosis.candidates[1:]
        ],
    }


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "datasets": sorted(DATASETS),
        "mode": "read-only",
    }


@app.post("/v1/rag")
def rag(request: RagRequest) -> dict:
    hits = search(request.query, k=request.k)
    return {
        "query": request.query,
        "hits": [
            {
                "source": hit.document.source,
                "heading": hit.document.heading,
                "score": hit.score,
                "snippet": hit.snippet,
            }
            for hit in hits
        ],
    }


@app.get("/v1/checkup")
def checkup(dataset: str = Query("dirty")) -> dict:
    ds = _load(dataset)
    cost_run = run_cost(ds)
    anomalies = detect_anomalies(ds, cost_run)
    explanation = LLMExplainer(load_sense(), None).explain(cost_run)
    return {
        "dataset": dataset,
        "period": ds.period,
        "markdown": explanation.text,
        "anomaly_count": len(anomalies),
        "unallocated": str(cost_run.unallocated_overhead),
    }


@app.get("/v1/diagnose")
def diagnose(
    dataset: str = Query("demo"),
    limit: int = Query(5, ge=1, le=20),
) -> dict:
    ds = _load(dataset)
    cost_run = run_cost(ds)
    sense = load_sense()
    facts = build_variance_facts(cost_run, sense.raw.get("org", {}))
    diagnoses = diagnose_facts(facts, cost_run)
    diagnoses.sort(key=lambda item: -abs(item.fact.amount))
    return {
        "dataset": dataset,
        "period": ds.period,
        "total": len(diagnoses),
        "items": [_diagnosis_payload(item) for item in diagnoses[:limit]],
    }


@app.post("/v1/challenge")
def challenge_endpoint(request: ChallengeRequest) -> dict:
    ds = _load(request.dataset)
    cost_run = run_cost(ds)
    sense = load_sense()
    facts = build_variance_facts(cost_run, sense.raw.get("org", {}))
    diagnoses = diagnose_facts(facts, cost_run)
    if not diagnoses:
        raise HTTPException(status_code=404, detail="本期没有可复核的差异")
    target = pick_target(diagnoses, request.text)
    outcome = challenge(target, request.text)
    return {
        "dataset": request.dataset,
        "fact": _fact_payload(target.fact),
        "text": request.text,
        "matched": outcome.matched,
        "original_score": outcome.original_score,
        "adjusted_score": outcome.adjusted_score,
        "still_valid": outcome.still_valid,
        "rationale": outcome.rationale,
        "actions": outcome.actions,
    }


@app.post("/v1/translate-config")
def translate_config_endpoint(request: TranslateRequest) -> dict:
    review = parse_config(request.text)
    return {
        "markdown": render_offline_translation(review),
        "rules": len(review.rules),
        "warnings": review.warnings,
        "notes": review.notes,
    }


@app.get("/v1/eval")
def eval_summary() -> dict:
    report = run_all()
    return {
        "total": report.total,
        "passed": report.passed,
        "pass_rate": round(report.pass_rate, 4),
        "by_kind": {
            kind: {"total": total, "passed": passed}
            for kind, (total, passed) in report.by_kind().items()
        },
        "failures": [
            {"id": item.case_id, "kind": item.kind, "detail": item.detail}
            for item in report.results
            if not item.passed
        ],
    }
