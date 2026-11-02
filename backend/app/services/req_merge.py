"""OEM 需求解析、原子化兜底与合并结果持久化。

当前合并主逻辑在 ``oem_csr_merge.py``：
  OEM 原子化 -> 固定 CSR 映射 -> 最终 FCSR 表（same/customer_only/
  fixed_only/partial，冲突单独进 merge_conflicts）。

本文件保留：
  - OEM Excel 解析与预览
  - OEM 需求确定性原子化兜底（LLM 不可用/漏条时使用）
  - MergedRequirement / MergedRequirementSource / MergeConflict 落库
旧的 17 分类自由对齐、覆盖兜底和自由合并代码已删除。
"""

from __future__ import annotations

import re
from typing import List, Dict, Tuple, Set, Optional

import openpyxl

from app.services.csr_synthesis import (
    extract_subject,
    extract_subject_and_clauses,
    semantic_tokens,
    _jaccard,
)


# ---------------------------------------------------------------------------
# Excel 解析与预览
# ---------------------------------------------------------------------------

def parse_customer_requirements_excel(
    file_path: str,
    sheet_name: str = "OEM CSR",
    id_col: int = None,
    text_col: int = None,
    section_col: int = None,
    category_col: int = None,
) -> List[dict]:
    """解析 OEM CSR sheet。

    表头：序号 | 所属章节 | 需求分类 | 需求编号 | 整车网络安全需求描述 | Mapped Final CSR IDs
    列映射：优先使用前端显式指定的 1-based 列号；未指定时按表头关键字自动匹配。
    """
    wb = openpyxl.load_workbook(file_path, data_only=True)
    if sheet_name not in wb.sheetnames:
        for name in wb.sheetnames:
            if "需求编号" in str([c.value for c in wb[name][1]]):
                sheet_name = name
                break
        else:
            raise ValueError(f"未找到客户需求 sheet（期望 {sheet_name}）")

    ws = wb[sheet_name]
    headers = [str(c.value or "").strip() for c in ws[1]]
    idx = id_col or _header_index(headers, ["需求编号", "Requirement ID", "外部编号"])
    text_idx = text_col or _header_index(headers, ["整车网络安全需求描述", "网络安全与测试需求描述", "需求描述", "Requirement"])
    section_idx = section_col or _header_index(headers, ["所属章节", "章节", "Section"])
    category_idx = category_col or _header_index(headers, ["分类领域", "需求分类", "分类", "Category"])

    if not idx or not text_idx:
        raise ValueError("无法确定「需求编号列」或「需求描述列」，请在前端指定列映射")

    rows = []
    for r in range(2, ws.max_row + 1):
        ext_id = _cell(ws, r, idx)
        text = _cell(ws, r, text_idx)
        if not ext_id or not text:
            continue
        rows.append({
            "external_id": ext_id,
            "section": _cell(ws, r, section_idx),
            "category": _cell(ws, r, category_idx),
            "text": text,
        })
    return rows


def preview_customer_excel(file_path: str, sheet_name: str = None) -> dict:
    """读取 Excel 的 sheet 列表、当前 sheet 表头与前 8 行样例，供前端做列映射。"""
    wb = openpyxl.load_workbook(file_path, data_only=True)
    sheets = wb.sheetnames
    if not sheet_name or sheet_name not in sheets:
        sheet_name = sheets[0]
    ws = wb[sheet_name]
    headers = [str(c.value or "").strip() for c in ws[1]]
    preview = []
    for r in range(2, min(ws.max_row, 9) + 1):
        preview.append([
            str(ws.cell(r, c).value or "").strip() if ws.cell(r, c).value is not None else ""
            for c in range(1, len(headers) + 1)
        ])
    return {
        "sheets": sheets,
        "sheet": sheet_name,
        "headers": headers,
        "preview": preview,
        "row_count": max(ws.max_row - 1, 0),
    }


def parse_tara_csr_excel(file_path: str, sheet_name: str = "TARA CSR") -> List[dict]:
    """解析 TARA CSR sheet。"""
    wb = openpyxl.load_workbook(file_path, data_only=True)
    if sheet_name not in wb.sheetnames:
        raise ValueError(f"未找到 TARA CSR sheet（期望 {sheet_name}）")

    ws = wb[sheet_name]
    headers = [str(c.value or "").strip() for c in ws[1]]
    idx = _header_index(headers, ["Atomic CSR ID", "CSR ID", "需求编号"])
    domain_idx = _header_index(headers, ["Security Domain", "安全域"])
    cat_idx = _header_index(headers, ["Control Category", "控制分类", "需求分类"])
    title_idx = _header_index(headers, ["Requirement Title", "需求标题", "标题"])
    text_idx = _header_index(headers, ["Cybersecurity Requirement", "需求描述", "整合网络安全需求描述"])
    src_idx = _header_index(headers, ["Source TARA CSR IDs", "Source TARA Raw IDs", "Source TARA", "追溯"])

    rows = []
    for r in range(2, ws.max_row + 1):
        csr_id = _cell(ws, r, idx)
        text = _cell(ws, r, text_idx)
        if not csr_id or not text:
            continue
        rows.append({
            "csr_id": csr_id,
            "security_domain": _cell(ws, r, domain_idx),
            "control_category": _cell(ws, r, cat_idx),
            "title": _cell(ws, r, title_idx),
            "requirement": text,
            "source_raw_ids": _cell(ws, r, src_idx),
        })
    return rows


def _cell(ws, row: int, col: int | None):
    if col is None:
        return ""
    v = ws.cell(row=row, column=col).value
    return str(v).strip() if v is not None else ""


def _header_index(headers: List[str], candidates: List[str]) -> int | None:
    for i, h in enumerate(headers, 1):
        for c in candidates:
            if c.lower() in h.lower():
                return i
    return None


# ---------------------------------------------------------------------------
# Step 1 & 2: 客户需求原子化拆解与客户内部去重
# ---------------------------------------------------------------------------

def atomize_and_dedup_customer_requirements(customer_reqs: List[dict]) -> List[dict]:
    """Step 1 & 2: 客户需求原子化拆解与客户内部去重。

    1. 原子化拆解：
       - 将客户（OEM）复合长需求拆解为单一责任主体、单一控制动作的原子项。
       - 清洗列表序号（如 1. / (1) / 1、 等），保留责任主体，并继承外部需求编号（external_id）。
    2. 客户内部去重：
       - 基于语义 Token 与 Jaccard 相似度 (>= 0.85) 聚类去重，消除客户跨章节/跨部门的重复要求。
       - 合并项并集继承所有关联的客户原始需求编号 (source_customer_ids)，保证 100% 来源追溯。
    """
    raw_atoms = []
    for cr in customer_reqs:
        ext_id = str(cr.get("external_id") or "").strip()
        text = str(cr.get("text") or "").strip()
        if not ext_id or not text or text == "N/A":
            continue

        category = str(cr.get("category") or cr.get("section") or "").strip()
        section = str(cr.get("section") or "").strip()

        # 拆解为原子子句
        extracted = extract_subject_and_clauses(text)
        for subj, clause, full in extracted:
            # 清理类似 "1. "、"(1)"、"1、" 的列表序号
            cl_clean = re.sub(r'^(?:[（(]?\d+[)）\.\、\s]+|\d+\.\s*)', '', clause).strip()
            if not cl_clean:
                cl_clean = clause.strip()

            # 若无提取主体，从需求文本推断主体（在 "应" 前面的词或默认主体）
            if not subj:
                if "应" in cl_clean:
                    cand_subj = cl_clean.split("应")[0].strip()
                    if 1 < len(cand_subj) <= 20 and not any(p in cand_subj for p in "，。；;,."):
                        subj = cand_subj
                if not subj:
                    subj = extract_subject(text) or "ECU控制器"

            raw_atoms.append({
                "source_customer_ids": {ext_id},
                "clause": cl_clean,
                "subject": subj,
                "category": category,
                "section": section,
            })

    # 客户内部语义聚类去重 (Intra-customer deduplication)
    clusters: List[dict] = []
    for atom in raw_atoms:
        toks = semantic_tokens(atom["clause"])
        matched = False
        for c in clusters:
            sim = _jaccard(toks, c["tokens"])
            if sim >= 0.55 or atom["clause"] == c["clause"]:
                c["source_customer_ids"].update(atom["source_customer_ids"])
                # 保留更详尽的表述
                if len(atom["clause"]) > len(c["clause"]):
                    c["clause"] = atom["clause"]
                matched = True
                break
        if not matched:
            clusters.append({
                "source_customer_ids": set(atom["source_customer_ids"]),
                "clause": atom["clause"],
                "subject": atom["subject"],
                "category": atom["category"],
                "section": atom["section"],
                "tokens": toks,
            })

    return clusters


# ---------------------------------------------------------------------------
# Step 3: 我方 TARA 原子需求准备（零拆解基准输入）
# ---------------------------------------------------------------------------

def persist_merged_requirements(
    db,
    project_id: int,
    domain_id: int,
    merged_rows: List[dict],
    tara_raw_map: dict = None,
) -> List[dict]:
    """把某个域控的合并结果落库（先清空该域控旧结果，再写入）。

    tara_raw_map: {csr_id: "CSR_xxx, CSR_yyy"}，用于穿透填充「Source TARA Raw IDs」列。
    """
    from app.models.req_merge import MergedRequirement, MergedRequirementSource

    tara_raw_map = tara_raw_map or {}

    old_ids = [
        row.id for row in
        db.query(MergedRequirement.id).filter(MergedRequirement.domain_id == domain_id).all()
    ]
    if old_ids:
        db.query(MergedRequirementSource).filter(
            MergedRequirementSource.merged_id.in_(old_ids)
        ).delete(synchronize_session=False)
        db.query(MergedRequirement).filter(MergedRequirement.domain_id == domain_id).delete(
            synchronize_session=False
        )

    persisted = []
    for m in merged_rows:
        customer_ids = m.get("source_customer_ids") or []
        tara_ids = m.get("source_tara_ids") or []

        # 汇总该最终需求涉及的所有 TARA 原始需求 ID（Source TARA Raw IDs 底层穿透）
        raw_id_list: List[str] = []
        for sid in tara_ids:
            # New fixed-CSR rows already carry original TARA IDs; older rows
            # carry generated CSR IDs that are expanded through tara_raw_map.
            raw = tara_raw_map.get(sid) or sid
            for part in str(raw).split(","):
                p = part.strip()
                if p and p not in raw_id_list:
                    raw_id_list.append(p)
        tara_raw_ids = ", ".join(raw_id_list)

        status_label = m.get("status") or ("Draft" if m.get("needs_review") else "Approved")
        if m.get("conflict"):
            status_label = "Conflict"

        row = MergedRequirement(
            project_id=project_id,
            domain_id=domain_id,
            final_id=m["final_id"],
            security_domain=m.get("functional_category") or m.get("security_domain") or "",
            control_category=m.get("control_category") or m.get("requirement_title") or "",
            requirement_title=m.get("requirement_title") or "",
            subject=m.get("subject") or extract_subject(m.get("requirement_text") or "") or "ECU控制器",
            requirement_text=m.get("requirement_text") or "",
            additional_info=m.get("additional_info") or "",
            source_tara_raw_ids=tara_raw_ids,
            status=status_label,
            conflict_flag=bool(m.get("conflict")),
            conflict_note=m.get("conflict_note") or "",
            resolution=m.get("resolution") or "",
        )
        db.add(row)
        db.flush()

        for cid in customer_ids:
            db.add(MergedRequirementSource(
                merged_id=row.id,
                source_type="customer",
                source_id=cid,
            ))
        for tid in tara_ids:
            db.add(MergedRequirementSource(
                merged_id=row.id,
                source_type="tara",
                source_id=tid,
            ))
        catalog_ids = m.get("source_catalog_csr_ids") or []
        for cid in catalog_ids:
            db.add(MergedRequirementSource(
                merged_id=row.id,
                source_type="catalog",
                source_id=cid,
            ))

        persisted.append({
            **m,
            "customer_ids": customer_ids,
            "tara_ids": tara_ids,
            "catalog_ids": catalog_ids,
            "source_tara_raw_ids": tara_raw_ids,
        })

    db.commit()
    return persisted
