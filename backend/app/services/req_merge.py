"""客户（OEM）需求 + TARA 需求合并服务。

把「客户整车信息安全需求」与「我方 TARA 分析产出的原子需求」做语义合并，
产出统一的最终信息安全需求（FCSR），并保证双来源（客户 / TARA）追溯。

设计要点（与 csr_synthesis.py 的既有模式保持一致）：
  - LLM 负责语义判断（合并 / 拆分 / 冲突识别）；
  - 代码负责硬约束（覆盖率：每条输入都必须被至少一条输出覆盖）。
"""

from __future__ import annotations

import json
import re
from typing import List, Dict, Tuple

import openpyxl

from app.core.taxonomy import (
    DOMAIN_PREFIX_MAP,
    MAJOR_ORDER,
    CANONICAL_DOMAINS,
    clean_major_category,
)
from app.services.csr_synthesis import clean_and_parse_json, post_llm_request_with_retry, extract_subject

# ---------------------------------------------------------------------------
# Excel 解析
# ---------------------------------------------------------------------------

def parse_customer_requirements_excel(file_path: str, sheet_name: str = "OEM CSR") -> List[dict]:
    """解析 OEM CSR sheet。

    表头：序号 | 所属章节 | 需求分类 | 需求编号 | 整车网络安全需求描述 | Mapped Final CSR IDs
    """
    wb = openpyxl.load_workbook(file_path, data_only=True)
    if sheet_name not in wb.sheetnames:
        # 容忍没有 OEM CSR sheet 的文件：寻找包含「需求编号」的 sheet
        for name in wb.sheetnames:
            if "需求编号" in str([c.value for c in wb[name][1]]):
                sheet_name = name
                break
        else:
            raise ValueError(f"未找到客户需求 sheet（期望 {sheet_name}）")

    ws = wb[sheet_name]
    headers = [str(c.value or "").strip() for c in ws[1]]
    idx = _header_index(headers, ["需求编号", "Requirement ID", "外部编号"])
    text_idx = _header_index(headers, ["整车网络安全需求描述", "网络安全与测试需求描述", "需求描述", "Requirement"])
    section_idx = _header_index(headers, ["所属章节", "章节", "Section"])
    category_idx = _header_index(headers, ["分类领域", "需求分类", "分类", "Category"])

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


def parse_tara_csr_excel(file_path: str, sheet_name: str = "TARA CSR") -> List[dict]:
    """解析 TARA CSR sheet。

    表头：Atomic CSR ID | Security Domain | Control Category | Requirement Title |
          Cybersecurity Requirement (CN/EN) | Source TARA CSR IDs | Mapped Final CSR IDs
    """
    wb = openpyxl.load_workbook(file_path, data_only=True)
    if sheet_name not in wb.sheetnames:
        raise ValueError(f"未找到 TARA CSR sheet（期望 {sheet_name}）")

    ws = wb[sheet_name]
    headers = [str(c.value or "").strip() for c in ws[1]]
    idx = _header_index(headers, ["Atomic CSR ID", "CSR ID"])
    domain_idx = _header_index(headers, ["Security Domain", "安全域"])
    cat_idx = _header_index(headers, ["Control Category", "控制分类"])
    title_idx = _header_index(headers, ["Requirement Title", "需求标题"])
    text_idx = _header_index(headers, ["Cybersecurity Requirement", "需求描述"])
    src_idx = _header_index(headers, ["Source TARA CSR IDs", "Source TARA", "追溯"])

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
# 合并
# ---------------------------------------------------------------------------

def _build_merge_items(customer_reqs: List[dict], tara_csrs: List[dict]) -> List[dict]:
    items = []
    for cr in customer_reqs:
        items.append({
            "source_type": "customer",
            "source_id": cr["external_id"],
            "text": cr["text"],
            "category": cr.get("category") or cr.get("section") or "",
        })
    for tc in tara_csrs:
        items.append({
            "source_type": "tara",
            "source_id": tc["csr_id"],
            "text": tc["requirement"],
            "category": tc.get("security_domain") or "",
        })
    return items


def enforce_merge_coverage(items: List[dict], merged: List[dict]) -> Tuple[List[dict], dict]:
    """代码级覆盖契约：每条输入（客户 / TARA）都必须被至少一条最终需求覆盖。

    - 剥掉幻觉来源（不在输入集合中的 source_refs）；
    - 对未被覆盖的输入补一条兜底条目（原始需求保留）；
    - 返回缺失/幻觉清单，供审计。
    """
    input_keys = {(it["source_type"], it["source_id"]) for it in items}
    seen = set()

    for m in merged:
        refs = m.get("source_refs") or []
        normalized = []
        for ref in refs:
            st = str(ref.get("source_type") or "").strip()
            sid = str(ref.get("source_id") or "").strip()
            if st and sid and (st, sid) not in normalized:
                normalized.append((st, sid))
        m["source_refs"] = [{"source_type": st, "source_id": sid} for st, sid in normalized]
        seen.update(normalized)

    extra = sorted(seen - input_keys)
    if extra:
        for m in merged:
            m["source_refs"] = [
                r for r in m["source_refs"]
                if (r["source_type"], r["source_id"]) in input_keys
            ]

    missing = [it for it in items if (it["source_type"], it["source_id"]) not in seen]
    for it in missing:
        merged.append({
            "security_domain": "通用安全与其它 / General & Other Security",
            "control_category": it.get("category") or "",
            "requirement_title": "需求兜底保留 / Requirement Preserved",
            "requirement_text": it["text"],
            "conflict": False,
            "conflict_note": "",
            "resolution": "fallback",
            "source_refs": [{"source_type": it["source_type"], "source_id": it["source_id"]}],
            "_coverage_status": "fallback_original",
        })

    report = {
        "input_count": len(items),
        "missing_source_ids": [f"{it['source_type']}:{it['source_id']}" for it in missing],
        "extra_source_ids": [f"{st}:{sid}" for st, sid in extra],
        "complete": not missing and not extra,
        "all_covered": True,
    }
    return merged, report


def _assign_final_ids(merged: List[dict]) -> List[dict]:
    """按安全域排序并分配 FCSR-{DOMAIN}-{NNN} 编号。"""
    # 规范化安全域
    for m in merged:
        m["security_domain"] = clean_major_category(m.get("security_domain"))
    merged.sort(key=lambda m: (
        MAJOR_ORDER.get(m["security_domain"], 99),
        str(m.get("control_category") or ""),
    ))
    counters = {}
    for m in merged:
        domain = m["security_domain"]
        prefix = DOMAIN_PREFIX_MAP.get(domain, "CSR-GEN").replace("CSR-", "FCSR-")
        counters[prefix] = counters.get(prefix, 0) + 1
        m["final_id"] = f"{prefix}-{counters[prefix]:03d}"
    return merged


MERGE_PROMPT = """你是一名资深汽车网络安全需求管理专家（精通 ISO/SAE 21434 与 UNECE R155）。
请把「客户（OEM）整车信息安全需求」与「我方 TARA 分析产出的原子安全需求」进行语义合并，产出统一的最终信息安全需求（FCSR）。

【输入】
customer_requirements: [{customer_id, text, category}]
tara_csrs: [{csr_id, text, security_domain, control_category, title}]

【合并规则】
1. 语义等价（同一保护对象 + 同一控制目标 + 同一可验证结果）→ 合并为一条，source_refs 同时列出 customer_id 与 csr_id。
2. 互补（不同但都成立）→ 保留为不同条目，禁止强行合并。
3. 冲突（互相矛盾）→ 合并为一条但 conflict=true，conflict_note 说明冲突点，resolution 默认 customer_wins（客户合规优先）。
4. 客户独有（我方无对应）→ 保留，source_refs 只含 customer_id。
5. 我方独有（客户无对应）→ 保留，source_refs 只含 csr_id。
6. 每一条最终需求必须给出：security_domain（必须严格取下列标准域之一）、control_category、requirement_title、requirement_text（中英双语，格式“中文 / English”）、verification_method、subject（责任主体）。
7. 覆盖率契约：输入中的每一个 customer_id 和 csr_id 都必须出现在至少一条最终需求的 source_refs 中；禁止遗漏、禁止编造输入中不存在的 ID。
8. 责任主体明确（强制）：requirement_text 必须以负责实施的组件/系统/模块作主语，写清「谁（组件/系统/模块）对什么对象实施什么控制」；禁止用事件、现象、数据或抽象名词作句子主语；例如「完整性校验失败事件应记录至本地审计存储」必须写成「ADCU 审计模块应将完整性校验失败事件记录至本地审计存储」；subject 字段单列该责任主体。

【标准安全域】
{domains}

【输出 JSON 格式】
{{
  "merged_requirements": [
    {{
      "security_domain": "硬件安全 / Hardware Security",
      "control_category": "控制分类",
      "requirement_title": "需求标题",
      "subject": "责任主体（组件/系统/模块）/ Responsible subject",
      "requirement_text": "中文需求 / English requirement",
      "verification_method": "测试 / Test",
      "conflict": false,
      "conflict_note": "",
      "resolution": "merge",
      "source_refs": [
        {{"source_type": "customer", "source_id": "SC-CS-HW-Req1"}},
        {{"source_type": "tara", "source_id": "CSR-HW-003"}}
      ]
    }}
  ]
}}
"""


def merge_requirements_with_llm(
    db,
    project_id: int,
    customer_reqs: List[dict],
    tara_csrs: List[dict],
) -> Tuple[List[dict], dict]:
    """调用 LLM 做语义合并，并用代码校验覆盖率。返回 (merged_rows, report)。"""
    from app.models.system_settings import SystemSettings

    items = _build_merge_items(customer_reqs, tara_csrs)
    settings = db.query(SystemSettings).first()
    if not settings or not settings.api_key or settings.api_key in ["mock_test_key", ""]:
        raise RuntimeError("未配置大模型 API Key，无法执行需求合并")

    domains = "\n".join(f"     * {d}" for d in CANONICAL_DOMAINS)
    prompt = MERGE_PROMPT.format(domains=domains)

    url = f"{settings.api_base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.model_name,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(items, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
    }

    resp = post_llm_request_with_retry(url, headers, payload, max_retries=2)
    raw_text = resp.json()["choices"][0]["message"]["content"]
    parsed = clean_and_parse_json(raw_text)
    merged = parsed.get("merged_requirements") or []
    if not merged:
        raise ValueError("大模型未返回 merged_requirements")

    merged, report = enforce_merge_coverage(items, merged)
    merged = _assign_final_ids(merged)
    report["mode"] = "llm"
    return merged, report


def persist_merged_requirements(db, project_id: int, domain_id: int, merged_rows: List[dict], tara_raw_map: dict = None) -> List[dict]:
    """把某个域控的合并结果落库（先清空该域控旧结果，再写入）。

    tara_raw_map: {csr_id: "CSR_xxx, CSR_yyy"}，用于填充「Source TARA Raw IDs」列。
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
        refs = m.get("source_refs") or []
        customer_ids = [r["source_id"] for r in refs if r["source_type"] == "customer"]
        tara_ids = [r["source_id"] for r in refs if r["source_type"] == "tara"]

        # 汇总该最终需求涉及的所有 TARA 原始需求 ID（Source TARA Raw IDs）
        raw_id_list: List[str] = []
        for sid in tara_ids:
            raw = tara_raw_map.get(sid)
            if not raw:
                continue
            for part in str(raw).split(","):
                p = part.strip()
                if p and p not in raw_id_list:
                    raw_id_list.append(p)
        tara_raw_ids = ", ".join(raw_id_list)

        row = MergedRequirement(
            project_id=project_id,
            domain_id=domain_id,
            final_id=m["final_id"],
            security_domain=m.get("security_domain") or "",
            control_category=m.get("control_category") or "",
            requirement_title=m.get("requirement_title") or "",
            subject=m.get("subject") or extract_subject(m.get("requirement_text") or ""),
            requirement_text=m.get("requirement_text") or "",
            source_tara_raw_ids=tara_raw_ids,
            status="Draft",
            conflict_flag=bool(m.get("conflict")),
            conflict_note=m.get("conflict_note") or "",
            resolution=m.get("resolution") or "",
        )
        db.add(row)
        db.flush()
        for ref in refs:
            db.add(MergedRequirementSource(
                merged_id=row.id,
                source_type=ref["source_type"],
                source_id=ref["source_id"],
            ))
        persisted.append({
            **m,
            "customer_ids": customer_ids,
            "tara_ids": tara_ids,
            "source_tara_raw_ids": tara_raw_ids,
        })
    db.commit()
    return persisted
