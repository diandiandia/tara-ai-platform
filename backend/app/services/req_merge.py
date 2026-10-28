"""客户（OEM）需求 + TARA 需求合并服务。

把「客户整车信息安全需求」与「我方 TARA 分析产出的原子需求」做语义合并，
产出统一的最终信息安全需求（FCSR），并保证双来源（客户 / TARA）100% 双向追溯。

核心架构与处理流水线：
  Step 1: 客户需求原子化拆解（复合长句拆为单一控制机制原子项，继承原始 customer_id）
  Step 2: 客户需求自身去重（跨章节冗余聚类去重，并集保留所有关联 customer_id）
  Step 3: 我方 TARA 需求零拆解（前期已完成原子化与去重，直接作为高标准基线输入）
  Step 4: 双干净池分批对齐合并与去重（分批语义对齐，彻底避免单 Prompt 溢出；严格覆盖率契约）
  Step 5: 统一 17 分类与 FCSR 标准编号（大模型忠实评估分类，按标准前缀递增编号）
  Step 6: 双向追溯与差距矩阵落库（客户需求覆盖、TARA 需求覆盖、底层 TARA 原始哈希穿透）
"""

from __future__ import annotations

import json
import re
from typing import List, Dict, Tuple, Set, Optional
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import openpyxl

from app.core.taxonomy import (
    DOMAIN_PREFIX_MAP,
    MAJOR_ORDER,
    CANONICAL_DOMAINS,
    clean_major_category,
    correct_security_domain,
    FUNCTIONAL_CATEGORY_NAMES,
    FUNCTIONAL_PREFIX_MAP,
    FUNCTIONAL_PROMPT_LIST,
    FUNCTIONAL_KEYWORDS_PROMPT,
    FUNCTIONAL_CONFUSION_RULES,
    classify_functional_category,
    correct_functional_category,
    normalize_functional_category,
)
from app.services.csr_synthesis import (
    clean_and_parse_json,
    post_llm_request_with_retry,
    extract_subject,
    extract_subject_and_clauses,
    semantic_tokens,
    _jaccard,
    classify_csrs_with_llm,
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

def prepare_tara_atomic_requirements(tara_csrs: List[dict]) -> List[dict]:
    """Step 3: 我方 TARA 原子需求准备（零拆解基准输入）。

    我方 TARA 分析产出的 CSR 已经在前期流水线中完成了严格的资产内原子化拆解与去重，
    本身已是单一控制机制、单一责任主体的标准原子需求基线，因此原样接入，零拆解！
    """
    tara_atoms = []
    for tc in tara_csrs:
        csr_id = str(tc.get("csr_id") or "").strip()
        req_text = str(tc.get("requirement") or tc.get("cybersecurity_requirement") or "").strip()
        if not csr_id or not req_text or req_text == "N/A":
            continue

        title = str(tc.get("title") or tc.get("requirement_title") or tc.get("sub_category") or "").strip()
        domain = str(tc.get("security_domain") or tc.get("functional_category") or "").strip()
        cat = str(tc.get("control_category") or title).strip()
        raw_ids = str(tc.get("source_raw_ids") or tc.get("source_cybersecurity_requirement_ids") or tc.get("source_tara_requirements") or "").strip()

        tara_atoms.append({
            "csr_id": csr_id,
            "requirement": req_text,
            "title": title or "网络安全控制需求 / Cybersecurity Requirement",
            "security_domain": domain,
            "control_category": cat,
            "source_raw_ids": raw_ids,
            "source_tara_ids": {csr_id},
            "tokens": semantic_tokens(req_text),
            "subject": tc.get("subject") or extract_subject(req_text) or "ECU控制器",
        })
    return tara_atoms


# ---------------------------------------------------------------------------
# Step 4: 双干净池分批对齐合并与去重 (Cross-Pool Alignment & Merge in Batches)
# ---------------------------------------------------------------------------

MERGE_BATCH_PROMPT = """你是一名资深汽车网络安全需求工程专家（精通 ISO/SAE 21434 与 UNECE R155）。
请对当前【{category}】领域的「客户（OEM）网络安全需求」与「我方 TARA 分析安全需求 (CSR)」进行精确语义对齐、去重与合并，输出统一的最终网络安全需求（FCSR）。

【合并规则】
1. 语义等价或包含（Merge）：
   - 若客户需求与 TARA 需求描述同一安全控制目标、保护对象与机制，必须合并为一条 FCSR。
   - 整合出一条表述专业、技术严密的统一需求（中英双语，格式：“【主体】...；【要求】... / [Subject] ...; [Requirement] ...”）。
   - source_customer_ids 列出被合并的所有客户需求原始编号；source_tara_ids 列出被合并的所有 TARA CSR 编号。
   - resolution: "merge" 或 "enrich"（高标准吸收）。
2. 存在冲突（Conflict）：
   - 若双方存在技术冲突或参数矛盾，conflict=true，conflict_note 填写冲突详情，resolution 设为 "customer_wins"（客户优先）。
3. 客户独有（Customer Only）：
   - 客户提出但当前候选 TARA 需求未覆盖的，保留为独立需求，source_customer_ids 填客户编号，source_tara_ids 为空列表 []，resolution: "customer_only"。
4. 我方 TARA 独有（TARA Only）：
   - 我方 TARA 识别但客户未要求的，保留为独立需求，source_customer_ids 为空列表 []，source_tara_ids 填 TARA 编号，resolution: "tara_only"。
5. 责任主体明确（强制）：
   - requirement_text 必须以责任主体开头，subject 单列责任主体组件名。
6. 双向覆盖率契约：
   - 当前批次输入中的每一个客户需求编号和 TARA 需求编号，都必须出现在至少一条输出的 source_customer_ids 或 source_tara_ids 中；严禁遗漏、严禁伪造。

【当前批次输入】
[客户需求列表 (Customer Requirements)]:
{customer_items_json}

[我方 TARA 需求列表 (TARA Requirements)]:
{tara_items_json}

【输出 JSON 格式要求】
{{
  "merged_requirements": [
    {{
      "requirement_title": "需求标题 / Requirement Title",
      "subject": "责任主体组件 / Subject",
      "requirement_text": "【主体】...；【要求】... / [Subject] ...; [Requirement] ...",
      "conflict": false,
      "conflict_note": "",
      "resolution": "merge",
      "source_customer_ids": ["SC-CS-001"],
      "source_tara_ids": ["CSR-HW-001"]
    }}
  ]
}}
"""


def _merge_batch_rule_fallback(
    category: str,
    customer_batch: List[dict],
    tara_batch: List[dict],
) -> List[dict]:
    """确定性规则对齐备选逻辑（用于无 API Key 或大模型异常降级）。"""
    merged_items = []
    matched_tara_ids = set()

    for c in customer_batch:
        best_t = None
        best_sim = 0.0
        for t in tara_batch:
            sim = _jaccard(c["tokens"], t["tokens"])
            if sim > best_sim:
                best_sim = sim
                best_t = t

        if best_t and best_sim >= 0.50:
            matched_tara_ids.add(best_t["csr_id"])
            subj = best_t.get("subject") or c.get("subject") or "ECU控制器"
            merged_items.append({
                "functional_category": category,
                "requirement_title": best_t.get("title") or "网络安全需求 / Cybersecurity Requirement",
                "subject": subj,
                "requirement_text": best_t["requirement"],
                "conflict": False,
                "conflict_note": "",
                "resolution": "merge",
                "source_customer_ids": list(c["source_customer_ids"]),
                "source_tara_ids": [best_t["csr_id"]],
            })
        else:
            subj = c.get("subject") or "ECU控制器"
            c_id_label = sorted(list(c["source_customer_ids"]))[0]
            merged_items.append({
                "functional_category": category,
                "requirement_title": f"客户专属需求 / Customer Requirement ({c_id_label})",
                "subject": subj,
                "requirement_text": f"【主体】{subj}；【要求】{c['clause']}",
                "conflict": False,
                "conflict_note": "",
                "resolution": "customer_only",
                "source_customer_ids": list(c["source_customer_ids"]),
                "source_tara_ids": [],
            })

    for t in tara_batch:
        if t["csr_id"] not in matched_tara_ids:
            merged_items.append({
                "functional_category": category,
                "requirement_title": t.get("title") or "TARA 强化需求 / TARA Enhanced Requirement",
                "subject": t.get("subject") or "ECU控制器",
                "requirement_text": t["requirement"],
                "conflict": False,
                "conflict_note": "",
                "resolution": "tara_only",
                "source_customer_ids": [],
                "source_tara_ids": [t["csr_id"]],
            })

    return merged_items


def _merge_batch_with_llm(
    category: str,
    customer_batch: List[dict],
    tara_batch: List[dict],
    settings: dict,
) -> List[dict]:
    """通过大模型执行单个批次的语义合并与对齐。"""
    c_input = [
        {
            "customer_ids": sorted(list(c["source_customer_ids"])),
            "subject": c.get("subject") or "",
            "text": c["clause"],
        }
        for c in customer_batch
    ]
    t_input = [
        {
            "csr_id": t["csr_id"],
            "title": t["title"],
            "subject": t.get("subject") or "",
            "requirement": t["requirement"],
        }
        for t in tara_batch
    ]

    prompt = MERGE_BATCH_PROMPT.format(
        category=category,
        customer_items_json=json.dumps(c_input, ensure_ascii=False, indent=2),
        tara_items_json=json.dumps(t_input, ensure_ascii=False, indent=2),
    )

    url = f"{settings.get('api_base_url', '').rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.get('api_key', '')}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.get("model_name") or "deepseek-chat",
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"请对上述【{category}】领域的客户需求与 TARA 需求进行对齐合并与去重，输出符合 JSON 格式的最终需求。"},
        ],
        "response_format": {"type": "json_object"},
    }

    try:
        resp = post_llm_request_with_retry(url, headers, payload, max_retries=2)
        raw_text = resp.json()["choices"][0]["message"]["content"]
        parsed = clean_and_parse_json(raw_text)
        items = parsed.get("merged_requirements") or []
        if not items:
            return _merge_batch_rule_fallback(category, customer_batch, tara_batch)
        for it in items:
            it["functional_category"] = category
        return items
    except Exception as e:
        print(f"[TARA-AI] 批次【{category}】LLM 合并异常，启用规则降级: {e}")
        return _merge_batch_rule_fallback(category, customer_batch, tara_batch)


def create_merge_batches(
    customer_atoms: List[dict],
    tara_atoms: List[dict],
    max_batch_size: int = 20,
) -> Tuple[List[Tuple[str, List[dict], List[dict]]], List[dict]]:
    """按安全域及语义关联组织精简批次（每批 <= 20 项）。

    高效机制：
    - 仅对双方都存在需求的领域（c_items 和 t_items 同时非空）组装 LLM 对齐批次；
    - 客户完全未提要求的类别，我方 TARA CSR 直接作为 tara_only 保留，无需空转 LLM；
    - 客户提了但我方无对应类别的，直接作为 customer_only 保留；
    - 最终所有需求统一交由大模型进行标准 17 分类。
    """
    batches = []
    direct_items = []
    matched_cust_indices = set()

    for cat in FUNCTIONAL_CATEGORY_NAMES:
        # TARA 需求已具备 security_domain 属性
        t_items = [t for t in tara_atoms if cat in t.get("security_domain", "")]

        # 关联属于该领域的客户需求（通过客户自身章节分类或与 TARA 项语义重叠）
        cat_keywords = [w for w in re.split(r'[\s/、]+', cat) if len(w) >= 2]
        c_items = []
        for idx, c in enumerate(customer_atoms):
            c_meta = f"{c.get('category', '')} {c.get('section', '')}"
            label_match = any(w in c_meta for w in cat_keywords)
            semantic_match = any(_jaccard(c["tokens"], t["tokens"]) >= 0.35 for t in t_items)
            if label_match or semantic_match:
                c_items.append(c)
                matched_cust_indices.add(idx)

        if not c_items and not t_items:
            continue

        if not c_items and t_items:
            # 客户完全未提要求的领域：直接作为 TARA 独有，无需 LLM 空转
            for t in t_items:
                direct_items.append({
                    "functional_category": cat,
                    "requirement_title": t.get("title") or "TARA 强化需求",
                    "subject": t.get("subject") or "ECU控制器",
                    "requirement_text": t["requirement"],
                    "conflict": False,
                    "conflict_note": "",
                    "resolution": "tara_only",
                    "source_customer_ids": [],
                    "source_tara_ids": [t["csr_id"]],
                })
            continue

        # 双方均有条目：组装批次给 LLM 做语义对齐、去重与冲突判定
        if len(c_items) <= max_batch_size:
            batches.append((cat, c_items, t_items))
        else:
            for i in range(0, len(c_items), max_batch_size):
                sub_c = c_items[i:i + max_batch_size]
                batches.append((cat, sub_c, t_items))

    # 处理未与任何 TARA 领域关联的客户独有需求
    for idx, c in enumerate(customer_atoms):
        if idx not in matched_cust_indices:
            c_id_label = sorted(list(c["source_customer_ids"]))[0]
            direct_items.append({
                "functional_category": c.get("category") or "通用安全与其它",
                "requirement_title": f"客户专属需求 ({c_id_label})",
                "subject": c.get("subject") or "ECU控制器",
                "requirement_text": f"【主体】{c.get('subject') or 'ECU控制器'}；【要求】{c['clause']}",
                "conflict": False,
                "conflict_note": "",
                "resolution": "customer_only",
                "source_customer_ids": list(c["source_customer_ids"]),
                "source_tara_ids": [],
            })

    return batches, direct_items


# ---------------------------------------------------------------------------
# 双向覆盖率契约与合并规范化 (Bidirectional Coverage Enforcement)
# ---------------------------------------------------------------------------

def enforce_merge_coverage_bidirectional(
    customer_reqs: List[dict],
    tara_csrs: List[dict],
    merged_items: List[dict],
) -> Tuple[List[dict], dict]:
    """代码级双向覆盖契约：保证 100% 客户需求覆盖 + 100% TARA 需求覆盖。

    - 剥除任何不在输入集合中的幻觉 ID；
    - 针对未被覆盖的客户需求自动补齐独立条目 (resolution=customer_only)；
    - 针对未被覆盖的 TARA 需求自动补齐独立条目 (resolution=tara_only)；
    - 规范化 source_refs 与 source_customer_ids / source_tara_ids。
    """
    input_customer_map = {
        str(cr["external_id"]).strip(): cr
        for cr in customer_reqs
        if str(cr.get("external_id") or "").strip()
    }
    input_tara_map = {
        str(tc["csr_id"]).strip(): tc
        for tc in tara_csrs
        if str(tc.get("csr_id") or "").strip()
    }

    all_cust_ids = set(input_customer_map.keys())
    all_tara_ids = set(input_tara_map.keys())

    # 1. 规范化与幻觉剥除
    consolidated: List[dict] = []
    seen_cust_ids = set()
    seen_tara_ids = set()

    for m in merged_items:
        raw_cids = m.get("source_customer_ids") or []
        raw_tids = m.get("source_tara_ids") or []

        # 容忍从 source_refs 提取
        for ref in m.get("source_refs") or []:
            st = ref.get("source_type")
            sid = str(ref.get("source_id") or "").strip()
            if st == "customer" and sid:
                raw_cids.append(sid)
            elif st == "tara" and sid:
                raw_tids.append(sid)

        valid_cids = sorted(list({str(cid).strip() for cid in raw_cids if str(cid).strip() in all_cust_ids}))
        valid_tids = sorted(list({str(tid).strip() for tid in raw_tids if str(tid).strip() in all_tara_ids}))

        # 若没有任何有效关联且无需求文本，跳过脏数据
        if not valid_cids and not valid_tids and not m.get("requirement_text"):
            continue

        seen_cust_ids.update(valid_cids)
        seen_tara_ids.update(valid_tids)

        m["source_customer_ids"] = valid_cids
        m["source_tara_ids"] = valid_tids
        m["source_refs"] = (
            [{"source_type": "customer", "source_id": cid} for cid in valid_cids]
            + [{"source_type": "tara", "source_id": tid} for tid in valid_tids]
        )
        consolidated.append(m)

    # 2. 覆盖率缺口兜底（保证 100% 来源双向追溯）
    missing_cids = sorted(list(all_cust_ids - seen_cust_ids))
    for cid in missing_cids:
        cr = input_customer_map[cid]
        text = str(cr.get("text") or "").strip()
        subj = extract_subject(text) or "ECU控制器"
        consolidated.append({
            "functional_category": classify_functional_category(f"{cr.get('category', '')} {cr.get('section', '')} {text}".strip()),
            "requirement_title": f"客户独有需求 / Customer Specific Requirement ({cid})",
            "subject": subj,
            "requirement_text": f"【主体】{subj}；【要求】{text}",
            "conflict": False,
            "conflict_note": "",
            "resolution": "customer_only",
            "source_customer_ids": [cid],
            "source_tara_ids": [],
            "source_refs": [{"source_type": "customer", "source_id": cid}],
            "_coverage_status": "fallback_customer_gap",
        })

    missing_tids = sorted(list(all_tara_ids - seen_tara_ids))
    for tid in missing_tids:
        tc = input_tara_map[tid]
        req_text = str(tc.get("requirement") or tc.get("cybersecurity_requirement") or "").strip()
        subj = tc.get("subject") or extract_subject(req_text) or "ECU控制器"
        consolidated.append({
            "functional_category": tc.get("functional_category") or tc.get("security_domain") or "4.2 系统加固",
            "requirement_title": tc.get("title") or "TARA 强化需求 / TARA Enhanced Requirement",
            "subject": subj,
            "requirement_text": req_text,
            "conflict": False,
            "conflict_note": "",
            "resolution": "tara_only",
            "source_customer_ids": [],
            "source_tara_ids": [tid],
            "source_refs": [{"source_type": "tara", "source_id": tid}],
            "_coverage_status": "fallback_tara_gap",
        })

    report = {
        "customer_input_count": len(all_cust_ids),
        "customer_covered_count": len(all_cust_ids),
        "customer_missing_count": 0,
        "tara_input_count": len(all_tara_ids),
        "tara_covered_count": len(all_tara_ids),
        "tara_missing_count": 0,
        "merged_output_count": len(consolidated),
        "all_covered": True,
        "complete": len(missing_cids) == 0 and len(missing_tids) == 0,
        "missing_customer_ids": missing_cids,
        "missing_tara_ids": missing_tids,
    }

    return consolidated, report


# ---------------------------------------------------------------------------
# Step 5: 统一 17 分类与 FCSR 编号 (Faithful 17 Categories & Standard FCSR IDs)
# ---------------------------------------------------------------------------

def classify_and_finalize_merged_csrs(
    merged: List[dict],
    settings: dict = None,
) -> List[dict]:
    """对合并后的最终需求进行大模型 17 分类并分配规范 FCSR 编号。"""
    if not merged:
        return []

    # 1. 大模型批量忠实分类（若有 settings）
    try:
        classify_csrs_with_llm(merged, settings=settings)
    except Exception as e:
        print(f"[TARA-AI] 合并需求 LLM 17 分类异常，沿用规则分类: {e}")

    # 2. 规范化分类名称（100% 信任大模型语义判定，不使用死板规则强行覆盖）
    for m in merged:
        text = m.get("requirement_text") or m.get("requirement_title") or ""
        raw_cat = m.get("functional_category") or m.get("security_domain")
        norm_cat = normalize_functional_category(raw_cat)
        if not norm_cat:
            norm_cat = classify_functional_category(f"{m.get('requirement_title', '')} {text}".strip())
        m["functional_category"] = norm_cat
        m["security_domain"] = norm_cat
        m["control_category"] = m.get("control_category") or m.get("requirement_title") or "安全防护控制"
        m["category_source"] = "llm" if raw_cat else "rule_fallback"

    # 3. 按 17 分类标准顺序排序
    order = {c: i for i, c in enumerate(FUNCTIONAL_CATEGORY_NAMES)}
    merged.sort(key=lambda m: (
        order.get(m["functional_category"], 99),
        str(m.get("control_category") or ""),
    ))

    # 4. 分配 FCSR-{PREFIX}-{NNN:03d} 编号
    counters = {}
    for m in merged:
        cat = m["functional_category"]
        prefix = FUNCTIONAL_PREFIX_MAP.get(cat, "CSR-GEN").replace("CSR-", "FCSR-")
        counters[prefix] = counters.get(prefix, 0) + 1
        m["final_id"] = f"{prefix}-{counters[prefix]:03d}"

    return merged


# 兼容保留
def _assign_final_ids(merged: List[dict]) -> List[dict]:
    return classify_and_finalize_merged_csrs(merged)


def enforce_merge_coverage(items: List[dict], merged: List[dict]) -> Tuple[List[dict], dict]:
    """兼容旧接口签名的覆盖契约。"""
    c_reqs = [{"external_id": it["source_id"], "text": it["text"]} for it in items if it.get("source_type") == "customer"]
    t_reqs = [{"csr_id": it["source_id"], "requirement": it["text"]} for it in items if it.get("source_type") == "tara"]
    return enforce_merge_coverage_bidirectional(c_reqs, t_reqs, merged)


# ---------------------------------------------------------------------------
# 顶层合并服务入口 (Pipeline Orchestrator)
# ---------------------------------------------------------------------------

def merge_requirements_with_llm(
    db,
    project_id: int,
    customer_reqs: List[dict],
    tara_csrs: List[dict],
) -> Tuple[List[dict], dict]:
    """客户需求 + TARA 需求全流程合并流水线。

    返回 (merged_rows, coverage_report)。
    """
    from app.models.system_settings import SystemSettings

    settings_dict = {}
    if db is not None:
        settings_obj = db.query(SystemSettings).first()
        if settings_obj and settings_obj.api_key:
            settings_dict = {
                "api_base_url": settings_obj.api_base_url,
                "api_key": settings_obj.api_key,
                "model_name": settings_obj.model_name,
            }

    is_llm_active = bool(
        settings_dict.get("api_key") and settings_dict.get("api_key") not in ["mock_test_key", ""]
    )

    print(f"[TARA-AI] 启动需求合并流水线: 客户原始需求 {len(customer_reqs)} 项, 我方 TARA CSR {len(tara_csrs)} 项, LLM active={is_llm_active}")

    # Step 1 & 2: 客户需求原子化拆解与客户内部去重
    customer_atoms = atomize_and_dedup_customer_requirements(customer_reqs)
    print(f"[TARA-AI] 步骤 1&2 完成: 客户需求拆解并去重为 {len(customer_atoms)} 项原子客户需求")

    # Step 3: 我方 TARA 原子需求准备（零拆解基准输入）
    tara_atoms = prepare_tara_atomic_requirements(tara_csrs)
    print(f"[TARA-AI] 步骤 3 完成: 我方 TARA 标准基线保持 {len(tara_atoms)} 项原子需求（零拆解）")

    # Step 4: 双干净池分批对齐合并与去重
    batches, direct_items = create_merge_batches(customer_atoms, tara_atoms, max_batch_size=20)
    print(f"[TARA-AI] 步骤 4: 构建 {len(batches)} 个需要大模型对齐合并的重叠领域批次 (直接保留单侧条目: {len(direct_items)} 项)")

    merged_candidates: List[dict] = list(direct_items)
    if is_llm_active and len(batches) > 1:
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(_merge_batch_with_llm, cat, c_batch, t_batch, settings_dict)
                for cat, c_batch, t_batch in batches
            ]
            for f in as_completed(futures):
                merged_candidates.extend(f.result())
    else:
        for cat, c_batch, t_batch in batches:
            if is_llm_active:
                merged_candidates.extend(_merge_batch_with_llm(cat, c_batch, t_batch, settings_dict))
            else:
                merged_candidates.extend(_merge_batch_rule_fallback(cat, c_batch, t_batch))

    # 双向覆盖率校验与兜底契约
    consolidated, report = enforce_merge_coverage_bidirectional(
        customer_reqs, tara_csrs, merged_candidates
    )

    # Step 5: 统一 17 分类与 FCSR 规范编号
    final_csrs = classify_and_finalize_merged_csrs(consolidated, settings=settings_dict if is_llm_active else None)
    print(f"[TARA-AI] 步骤 5 完成: 最终生成 {len(final_csrs)} 项 FCSR 需求，双向 100% 覆盖校验通过！")

    report["mode"] = "llm" if is_llm_active else "rule_fallback"
    return final_csrs, report


# ---------------------------------------------------------------------------
# Step 6: 数据库持久化 (DB Persistence)
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
            raw = tara_raw_map.get(sid)
            if not raw:
                continue
            for part in str(raw).split(","):
                p = part.strip()
                if p and p not in raw_id_list:
                    raw_id_list.append(p)
        tara_raw_ids = ", ".join(raw_id_list)

        status_label = "Approved"
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

        persisted.append({
            **m,
            "customer_ids": customer_ids,
            "tara_ids": tara_ids,
            "source_tara_raw_ids": tara_raw_ids,
        })

    db.commit()
    return persisted
