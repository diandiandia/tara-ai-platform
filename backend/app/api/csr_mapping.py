from __future__ import annotations

import os
import tempfile
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from app.api.auth import get_current_user
from app.api.project import check_project_active
from app.core.database import get_db
from app.models.csr_catalog import CsrCatalogVersion
from app.models.domain import Domain
from app.models.user import User
from app.services.csr_catalog import (
    get_active_catalog,
    get_active_catalog_version,
    import_csr_catalog,
)
from app.services.csr_mapping import (
    _settings_from_db,
    build_mapping_report_data,
    collect_tara_requirements,
    map_tara_requirements,
    persist_mappings,
)

router = APIRouter(tags=["固定 CSR 目录与 TARA 映射"])


def _get_domain(domain_id: int, db: Session) -> Domain:
    domain = db.query(Domain).filter(Domain.id == domain_id).first()
    if not domain:
        raise HTTPException(status_code=404, detail="子域控不存在")
    return domain


@router.post("/csr-catalog/import")
def import_catalog(
    file: UploadFile = File(...),
    sheet_name: Optional[str] = Form(None),
    activate: bool = Form(True),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """导入固定 CSR 目录 xlsx；相同内容的重复导入会复用已有版本。"""
    suffix = os.path.splitext(file.filename or "csr_catalog.xlsx")[1] or ".xlsx"
    fd, path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(file.file.read())
        try:
            result = import_csr_catalog(
                db,
                path,
                sheet_name=sheet_name,
                activate=activate,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"CSR 目录导入失败: {exc}")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return result


@router.get("/csr-catalog")
def list_catalog(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    version = get_active_catalog_version(db)
    if version is None:
        return {"version": None, "row_count": 0, "rows": []}
    rows = get_active_catalog(db)
    return {
        "version": version.version,
        "source_file": version.source_file,
        "sheet_name": version.sheet_name,
        "row_count": version.row_count,
        "rows": rows,
    }


@router.get("/csr-catalog/versions")
def list_catalog_versions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    versions = (
        db.query(CsrCatalogVersion)
        .order_by(CsrCatalogVersion.id.desc())
        .all()
    )
    return [
        {
            "id": row.id,
            "version": row.version,
            "source_file": row.source_file,
            "sheet_name": row.sheet_name,
            "row_count": row.row_count,
            "is_active": row.is_active,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in versions
    ]


@router.post("/csr-catalog/versions/{version_id}/activate")
def activate_catalog_version(
    version_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    version = db.query(CsrCatalogVersion).filter(CsrCatalogVersion.id == version_id).first()
    if not version:
        raise HTTPException(status_code=404, detail="CSR 目录版本不存在")
    db.query(CsrCatalogVersion).update({CsrCatalogVersion.is_active: False})
    version.is_active = True
    db.commit()
    return {
        "version": version.version,
        "source_file": version.source_file,
        "row_count": version.row_count,
        "is_active": True,
    }


@router.post("/domains/{domain_id}/csr-mapping/run")
def run_csr_mapping(
    domain_id: int,
    run_id: Optional[int] = Query(None, description="默认使用该域控最新完成的 TARA run"),
    batch_size: int = Query(25, ge=1, le=100),
    max_workers: int = Query(4, ge=1, le=8, description="并行映射批次数"),
    limit: Optional[int] = Query(None, ge=1, description="只映射前 N 条，用于试点"),
    persist: bool = Query(True),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """把该域控 Stage5 的 TARA 需求映射到固定 CSR 目录。"""
    domain = _get_domain(domain_id, db)
    check_project_active(domain.project_id, db)

    version = get_active_catalog_version(db)
    catalog = get_active_catalog(db)
    if version is None or not catalog:
        raise HTTPException(status_code=400, detail="请先导入固定 CSR 目录")

    settings = _settings_from_db(db)
    if not settings:
        raise HTTPException(status_code=400, detail="系统未配置可用的 LLM")

    try:
        collected = collect_tara_requirements(
            db, domain_id, run_id=run_id, limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    requirements = collected["requirements"]
    if not requirements:
        raise HTTPException(status_code=400, detail="该 run 没有可映射的 Stage5 TARA 需求")

    try:
        mapped = map_tara_requirements(
            requirements,
            catalog,
            settings,
            batch_size=batch_size,
            max_workers=max_workers,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"CSR 映射失败: {exc}")

    saved = 0
    if persist:
        saved = persist_mappings(
            db,
            domain,
            collected["run"],
            mapped["rows"],
            catalog_version=version.version,
            replace=True,
        )

    return {
        "domain_id": domain_id,
        "run_id": collected["run"].id,
        "catalog_version": version.version,
        "batch_size": batch_size,
        "max_workers": max_workers,
        "limit": limit,
        "persisted_rows": saved,
        "summary": mapped["summary"],
        "rows": mapped["rows"] if not persist else [],
    }


@router.get("/domains/{domain_id}/csr-mapping")
def get_csr_mapping(
    domain_id: int,
    run_id: Optional[int] = Query(None),
    include_details: bool = Query(True),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """查看某个域控/run 已保存的 CSR 映射结果。"""
    _get_domain(domain_id, db)
    if run_id is None:
        from app.models.tara_run import TaraRun

        run = (
            db.query(TaraRun)
            .filter(TaraRun.domain_id == domain_id, TaraRun.status == "completed")
            .order_by(TaraRun.id.desc())
            .first()
        )
        if not run:
            raise HTTPException(status_code=404, detail="该域控没有已完成的 TARA run")
        run_id = run.id

    data = build_mapping_report_data(db, domain_id, run_id)
    if data is None:
        raise HTTPException(status_code=400, detail="当前没有激活的 CSR 目录")
    response = {
        "domain_id": domain_id,
        "run_id": run_id,
        "summary": data["summary"],
        "catalog_version": data["summary"].get("catalog_version"),
    }
    if include_details:
        response["mapping_details"] = data["mapping_details"]
        response["unmapped"] = data["unmapped"]
        response["csr_rows"] = data["csr_rows"]
    return response
