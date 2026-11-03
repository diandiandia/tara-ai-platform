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
from app.models.tara_run import TaraRun
from app.models.req_merge import CustomerRequirement, MergedRequirement, MergedRequirementSource
from app.services.req_merge import (
    parse_customer_requirements_excel,
    preview_customer_excel,
    persist_merged_requirements,
)
from app.services.oem_csr_merge import (
    collect_merge_conflict_rows,
    merge_oem_requirements_with_fixed_csrs,
    persist_merge_conflicts,
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

    # 同一需求编号可能因重复导入/多 sheet 被解析多次，按编号去重（保留最后一条），
    # 否则下游 OEM 原子化与合并会拿到成倍的重复需求。
    deduped = {}
    for row in parsed:
        deduped[row["external_id"]] = row
    parsed = list(deduped.values())

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
    """把 OEM 客户需求与「固定 CSR 目录」合并为最终信息安全需求（FCSR）。

    新逻辑：
      1. LLM 原子化 OEM 需求；
      2. OEM 原子映射到固定 CSR（full / partial / gap）；
      3. 生成最终表：same / partial / tara_only / catalog_only / customer_only / tara_gap。

    ``tara_csr_file`` 参数保留用于兼容旧前端，但固定 CSR 模式下不再使用；
    我们的 CSR 统一来自已入库的固定 CSR 目录和 TARA→固定 CSR 映射结果。
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

    last_run = (
        db.query(TaraRun)
        .filter(TaraRun.domain_id == domain_id, TaraRun.status == "completed")
        .order_by(TaraRun.id.desc())
        .first()
    )
    if not last_run:
        raise HTTPException(status_code=400, detail="该域控没有已完成的 TARA run")
    if tara_csr_file is not None:
        print("[TARA-AI] 固定 CSR 模式忽略上传的 TARA CSR 文件，使用已入库的固定 CSR 映射结果")

    try:
        merged, conflicts, report = merge_oem_requirements_with_fixed_csrs(
            db,
            domain.project_id,
            domain_id,
            customer_reqs,
            domain,
            last_run,
        )
        persisted = persist_merged_requirements(
            db, domain.project_id, domain_id, merged, tara_raw_map={}
        )
        persisted_conflicts = persist_merge_conflicts(
            db,
            domain.project_id,
            domain_id,
            last_run.id,
            conflicts,
            replace=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"需求合并失败: {exc}")

    return {
        "domain_id": domain_id,
        "project_id": domain.project_id,
        "run_id": last_run.id,
        "merged_count": len(persisted),
        "conflict_count": persisted_conflicts,
        "coverage": report,
        "source_mode": "fixed_csr_mapping",
    }

@router.get("/domains/{domain_id}/merged-requirements/conflicts")
def list_merge_conflicts(
    domain_id: int,
    run_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _get_domain(domain_id, db)
    rows = collect_merge_conflict_rows(db, domain_id, run_id)
    return {"domain_id": domain_id, "run_id": run_id, "count": len(rows), "rows": rows}


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
            "catalog_ids": [x.source_id for x in refs if x.source_type == "catalog"],
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
