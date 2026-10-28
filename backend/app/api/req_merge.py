from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Query, Form
from sqlalchemy.orm import Session
from typing import List, Optional
import os
import tempfile

from app.core.database import get_db
from app.api.auth import get_current_user
from app.api.project import check_project_active
from app.models.user import User
from app.models.domain import Domain
from app.models.asset import Asset
from app.models.tara_run import TaraRun
from app.models.tara_step import TaraStep
from app.models.req_merge import CustomerRequirement, MergedRequirement, MergedRequirementSource
from app.services.req_merge import (
    parse_customer_requirements_excel,
    preview_customer_excel,
    parse_tara_csr_excel,
    merge_requirements_with_llm,
    persist_merged_requirements,
)

router = APIRouter(tags=["客户需求与 TARA 需求合并"])


def _save_upload(upload: UploadFile) -> str:
    suffix = os.path.splitext(upload.filename or "upload.xlsx")[1] or ".xlsx"
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(upload.file.read())
    return path


def _get_domain(domain_id: int, db: Session) -> Domain:
    domain = db.query(Domain).filter(Domain.id == domain_id).first()
    if not domain:
        raise HTTPException(status_code=404, detail="子域控不存在")
    return domain


def _collect_tara_csrs_from_domain(db: Session, domain: Domain) -> List[dict]:
    """从该域控已完成的 TARA 分析中汇总其 CSR（TARA 侧来源）。"""
    from app.api.report import collect_csr_report_data

    last_run = (
        db.query(TaraRun)
        .filter(TaraRun.domain_id == domain.id, TaraRun.status == "completed")
        .order_by(TaraRun.id.desc())
        .first()
    )
    if not last_run:
        return []
    steps = db.query(TaraStep).filter(TaraStep.run_id == last_run.id).all()
    assets = db.query(Asset).filter(Asset.domain_id == domain.id).all()
    rows: List[dict] = []
    for r in collect_csr_report_data(steps, assets, db=db, domain=domain):
        rows.append({
            "csr_id": r.get("csr_id"),
            "security_domain": r.get("security_domain"),
            "control_category": r.get("sub_category"),
            "title": r.get("title"),
            "requirement": r.get("cybersecurity_requirement"),
            "source_raw_ids": r.get("source_cybersecurity_requirement_ids")
            or r.get("source_tara_requirements") or "",
        })
    return rows


# ---------------------------------------------------------------------------
# 客户（OEM）需求：项目级（整车需求，一份，供各域控复用）
# ---------------------------------------------------------------------------

@router.post("/projects/{project_id}/customer-requirements/preview")
def preview_customer_requirements(
    project_id: int,
    file: UploadFile = File(...),
    sheet_name: str = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """预览上传的 OEM Excel（sheet 列表 / 表头 / 前几行），供前端做列映射。"""
    check_project_active(project_id, db)
    path = _save_upload(file)
    try:
        data = preview_customer_excel(path, sheet_name=sheet_name)
    finally:
        os.unlink(path)
    return data


@router.post("/projects/{project_id}/customer-requirements/import")
def import_customer_requirements(
    project_id: int,
    file: UploadFile = File(...),
    sheet_name: str = Form("OEM CSR"),
    replace: bool = Form(True),
    id_column: int = Form(None),
    text_column: int = Form(None),
    section_column: int = Form(None),
    category_column: int = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """导入客户（OEM）整车信息安全需求；支持显式指定列映射，不依赖 Excel 固定格式。"""
    check_project_active(project_id, db)
    path = _save_upload(file)
    try:
        parsed = parse_customer_requirements_excel(
            path,
            sheet_name=sheet_name,
            id_col=id_column,
            text_col=text_column,
            section_col=section_column,
            category_col=category_column,
        )
    finally:
        os.unlink(path)

    if not parsed:
        raise HTTPException(status_code=400, detail="未从上传文件中解析到客户需求行")

    if replace:
        db.query(CustomerRequirement).filter(CustomerRequirement.project_id == project_id).delete(
            synchronize_session=False
        )

    for row in parsed:
        db.add(CustomerRequirement(
            project_id=project_id,
            external_id=row["external_id"],
            section=row.get("section") or "",
            category=row.get("category") or "",
            text=row["text"],
            source=file.filename or "OEM",
        ))
    db.commit()

    return {"imported": len(parsed), "project_id": project_id}


@router.get("/projects/{project_id}/customer-requirements")
def list_customer_requirements(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        db.query(CustomerRequirement)
        .filter(CustomerRequirement.project_id == project_id)
        .order_by(CustomerRequirement.id)
        .all()
    )
    return [
        {
            "id": r.id,
            "external_id": r.external_id,
            "section": r.section,
            "category": r.category,
            "text": r.text,
            "source": r.source,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# 最终需求合并：域控级（每个域控用自己的 TARA 需求 + OEM 需求各自合并）
# ---------------------------------------------------------------------------

@router.post("/domains/{domain_id}/merged-requirements/synthesize")
def synthesize_merged_requirements(
    domain_id: int,
    tara_csr_file: Optional[UploadFile] = File(None),
    tara_csr_sheet: str = Query("TARA CSR"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """把 OEM 客户需求与「本域控」的 TARA 需求合并为最终信息安全需求（FCSR）。

    TARA 侧来源优先使用上传的 TARA CSR sheet；未上传时从本域控已完成的 TARA 分析中汇总。
    """
    domain = _get_domain(domain_id, db)
    check_project_active(domain.project_id, db)

    customer_rows = (
        db.query(CustomerRequirement)
        .filter(CustomerRequirement.project_id == domain.project_id)
        .order_by(CustomerRequirement.id)
        .all()
    )
    if not customer_rows:
        raise HTTPException(status_code=400, detail="请先导入 OEM 客户需求")
    customer_reqs = [
        {"external_id": r.external_id, "section": r.section, "category": r.category, "text": r.text}
        for r in customer_rows
    ]

    if tara_csr_file is not None:
        path = _save_upload(tara_csr_file)
        try:
            tara_csrs = parse_tara_csr_excel(path, sheet_name=tara_csr_sheet)
        finally:
            os.unlink(path)
    else:
        tara_csrs = _collect_tara_csrs_from_domain(db, domain)

    if not tara_csrs:
        raise HTTPException(status_code=400, detail="没有可用的本域控 TARA CSR 来源")

    try:
        merged, report = merge_requirements_with_llm(db, domain.project_id, customer_reqs, tara_csrs)
        tara_raw_map = {tc["csr_id"]: (tc.get("source_raw_ids") or "") for tc in tara_csrs}
        persisted = persist_merged_requirements(
            db, domain.project_id, domain_id, merged, tara_raw_map=tara_raw_map
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"需求合并失败: {exc}")

    return {
        "domain_id": domain_id,
        "project_id": domain.project_id,
        "merged_count": len(persisted),
        "coverage": report,
    }


@router.get("/domains/{domain_id}/merged-requirements")
def list_merged_requirements(
    domain_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        db.query(MergedRequirement)
        .filter(MergedRequirement.domain_id == domain_id)
        .order_by(MergedRequirement.id)
        .all()
    )
    result = []
    for r in rows:
        refs = (
            db.query(MergedRequirementSource)
            .filter(MergedRequirementSource.merged_id == r.id)
            .all()
        )
        result.append({
            "id": r.id,
            "final_id": r.final_id,
            "customer_ids": [x.source_id for x in refs if x.source_type == "customer"],
            "tara_ids": [x.source_id for x in refs if x.source_type == "tara"],
            "security_domain": r.security_domain,
            "control_category": r.control_category,
            "requirement_title": r.requirement_title,
            "requirement_text": r.requirement_text,
            "status": r.status,
            "conflict_flag": r.conflict_flag,
            "conflict_note": r.conflict_note,
            "resolution": r.resolution,
        })
    return result
