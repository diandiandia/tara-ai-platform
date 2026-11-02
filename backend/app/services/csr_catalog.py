"""Import and query the fixed CSR catalog.

The catalog is a controlled list of target requirements (for example the
customer's ``csr_new.xlsx``).  TARA requirements are mapped to this catalog;
the catalog itself is never rewritten by the LLM.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.csr_catalog import CsrCatalogEntry, CsrCatalogVersion


def _clean(value) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _find_columns(header: List[str]) -> Dict[str, int]:
    wanted = {
        "csr_id": ("需求ID", "需求 ID", "CSR ID", "CSR编号", "编号", "id"),
        "module": ("所属模块", "模块", "分类", "module"),
        "name": ("需求名称", "名称", "标题", "name", "title"),
        "description": ("需求描述", "描述", "内容", "description", "requirement"),
    }
    mapping: Dict[str, int] = {}
    lowered = [str(h or "").strip().lower() for h in header]
    for key, aliases in wanted.items():
        for idx, value in enumerate(lowered):
            if any(alias.lower() in value for alias in aliases):
                mapping[key] = idx
                break
    return mapping


def parse_csr_catalog_xlsx(path: str, sheet_name: Optional[str] = None) -> dict:
    """Parse a CSR catalog workbook into normalized rows.

    Expected columns are: 需求ID / 所属模块 / 需求名称 / 需求描述.  If the
    header cannot be recognised, the first four columns are used positionally.
    The returned ``source_hash`` is stable for the same normalized rows.
    """
    from openpyxl import load_workbook

    if not os.path.exists(path):
        raise FileNotFoundError(path)

    wb = load_workbook(path, data_only=True, read_only=True)
    try:
        if sheet_name and sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
        else:
            ws = wb[wb.sheetnames[0]]
            sheet_name = ws.title

        raw_rows = [list(row) for row in ws.iter_rows(values_only=True)]
    finally:
        wb.close()

    if not raw_rows:
        raise ValueError("CSR catalog workbook is empty")

    header = [_clean(x) for x in raw_rows[0]]
    columns = _find_columns(header)
    if not {"csr_id", "description"}.issubset(columns):
        # Positional fallback for the known 4-column template.
        columns = {"csr_id": 0, "module": 1, "name": 2, "description": 3}
        first = " ".join(header).lower()
        looks_like_header = any(
            token in first
            for token in ("需求", "csr", "名称", "描述", "module", "description", "id")
        )
        data_rows = raw_rows[1:] if looks_like_header else raw_rows
    else:
        data_rows = raw_rows[1:]

    rows: List[dict] = []
    seen: Dict[str, int] = {}
    for line_no, raw in enumerate(data_rows, start=2):
        def cell(key: str) -> str:
            idx = columns.get(key)
            if idx is None or idx >= len(raw):
                return ""
            return _clean(raw[idx])

        csr_id = cell("csr_id")
        if not csr_id:
            continue
        if csr_id in seen:
            raise ValueError(f"duplicate CSR ID {csr_id} at rows {seen[csr_id]} and {line_no}")
        seen[csr_id] = line_no

        description = cell("description")
        name = cell("name")
        module = cell("module")
        if not description:
            raise ValueError(f"CSR {csr_id} has an empty description at row {line_no}")
        rows.append({
            "csr_id": csr_id,
            "module": module or "未分类 / Uncategorized",
            "name": name or csr_id,
            "description": description,
        })

    if not rows:
        raise ValueError("no CSR rows found in workbook")

    normalized = "\n".join(
        f"{r['csr_id']}|{r['module']}|{r['name']}|{r['description']}" for r in rows
    )
    source_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return {
        "rows": rows,
        "row_count": len(rows),
        "source_hash": source_hash,
        "sheet_name": sheet_name,
    }


def get_active_catalog_version(db: Session) -> Optional[CsrCatalogVersion]:
    return (
        db.query(CsrCatalogVersion)
        .filter(CsrCatalogVersion.is_active.is_(True))
        .order_by(CsrCatalogVersion.id.desc())
        .first()
    )


def import_csr_catalog(
    db: Session,
    path: str,
    sheet_name: Optional[str] = None,
    activate: bool = True,
) -> dict:
    """Import a catalog workbook and optionally make it the active version."""
    parsed = parse_csr_catalog_xlsx(path, sheet_name=sheet_name)
    version = f"CSR-CAT-{parsed['source_hash'][:12].upper()}"

    existing = (
        db.query(CsrCatalogVersion)
        .filter(CsrCatalogVersion.version == version)
        .first()
    )
    if existing is None:
        if activate:
            db.query(CsrCatalogVersion).update({CsrCatalogVersion.is_active: False})
        existing = CsrCatalogVersion(
            version=version,
            source_file=os.path.basename(path),
            sheet_name=parsed["sheet_name"],
            row_count=parsed["row_count"],
            is_active=activate,
        )
        db.add(existing)
        db.flush()
        for row in parsed["rows"]:
            db.add(CsrCatalogEntry(
                version_id=existing.id,
                csr_id=row["csr_id"],
                module=row["module"],
                name=row["name"],
                description=row["description"],
            ))
        db.commit()
        created = True
    else:
        if activate and not existing.is_active:
            db.query(CsrCatalogVersion).update({CsrCatalogVersion.is_active: False})
            existing.is_active = True
            db.commit()
        created = False

    return {
        "version": version,
        "row_count": parsed["row_count"],
        "source_hash": parsed["source_hash"],
        "source_file": os.path.basename(path),
        "sheet_name": parsed["sheet_name"],
        "created": created,
        "active": bool(existing.is_active),
    }


def get_active_catalog(db: Session) -> List[dict]:
    version = get_active_catalog_version(db)
    if version is None:
        return []
    entries = (
        db.query(CsrCatalogEntry)
        .filter(CsrCatalogEntry.version_id == version.id)
        .order_by(CsrCatalogEntry.id)
        .all()
    )
    return [
        {
            "csr_id": entry.csr_id,
            "module": entry.module,
            "name": entry.name,
            "description": entry.description,
        }
        for entry in entries
    ]
