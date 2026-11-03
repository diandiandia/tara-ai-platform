"""CSR 辅助工具与固定目录映射适配层。

本模块提供：
  - 需求文本责任主体提取与原子子句切分（extract_subject, extract_subject_and_clauses）
  - 资产与步骤 ID 映射工具（get_asset_sn, build_id_maps, get_csr_traceability_maps）
  - 大模型 JSON 交互与重试兜底（clean_and_parse_json, post_llm_request_with_retry, _call_llm_json）
  - 语义 Token 与相似度计算（semantic_tokens, _jaccard, dedup_stage5_summarized）
  - 固定 CSR 目录适配层（hierarchical_synthesize_project_csrs, get_or_synthesize_global_csrs）

历史的旧版 LLM 多级原子化汇总与动态聚类管线已由固定标准库（csr_catalog / csr_mapping / oem_csr_merge）取代。
"""

import re
import json
import time
import hashlib
from typing import List, Tuple, Set
from sqlalchemy.orm import Session
import httpx

from app.models.tara_step import TaraStep
from app.models.domain import Domain

# 兼容性重导出
from app.core.taxonomy import (
    DOMAIN_PREFIX_MAP,
    MAJOR_ORDER,
)


def _content_hash_id(prefix: str, text: str) -> str:
    """生成确定性内容哈希 ID (8位 SHA-256 十六进制大写)"""
    sha256_hash = hashlib.sha256(str(text).strip().encode('utf-8')).hexdigest()
    return f"{prefix}{sha256_hash[:8].upper()}"


def extract_subject(text: str) -> str:
    """从需求文本中提取【主体】/ [Subject] 段，作为「责任主体」的确定性兜底。"""
    if not text:
        return ""
    m = re.search(r'【主体】([^【】]*)', str(text))
    if m and m.group(1).strip():
        return m.group(1).strip().rstrip(";；").strip()
    m = re.search(r'\[Subject\]\s*([^\[]*)', str(text), re.IGNORECASE)
    if m and m.group(1).strip():
        return m.group(1).strip().rstrip(";；").strip()
    return ""


def extract_subject_and_clauses(text: str) -> List[Tuple[str, str, str]]:
    """
    Step 1 基础函数：将复合网络安全需求拆解为单一控制动作的原子项。
    保留责任主体，并按动作动词切分复合子句。
    返回列表: [(subject, clause_text, formatted_atom_text), ...]
    """
    if not text or text == "N/A":
        return []

    subj = ""
    req_body = str(text).strip()

    # 提取【主体】或 [Subject]
    subj_match = re.search(r'【主体】\s*([^【】;；]+?)(?:；|;|【要求】|$)', req_body)
    if subj_match:
        subj = subj_match.group(1).strip()

    req_match = re.search(r'【要求】\s*(.*)', req_body, re.DOTALL)
    if req_match:
        req_body = req_match.group(1).strip()
    elif not subj:
        en_subj_match = re.search(r'\[Subject\]\s*([^\[\];；]+?)(?:；|;|\[Requirement\]|$)', req_body, re.IGNORECASE)
        if en_subj_match:
            subj = en_subj_match.group(1).strip()
        en_req_match = re.search(r'\[Requirement\]\s*(.*)', req_body, re.IGNORECASE | re.DOTALL)
        if en_req_match:
            req_body = en_req_match.group(1).strip()

    # 按标点切分候选子句
    raw_clauses = re.split(r'[；;\n。]+', req_body)
    clauses = []
    action_keywords = ["应", "不得", "禁止", "严禁", "必须", "shall", "must", "should"]

    current_clause = ""
    for c in raw_clauses:
        c_clean = c.strip()
        if not c_clean:
            continue
        has_action = any(kw in c_clean.lower() for kw in action_keywords)
        if has_action and len(c_clean) > 8:
            if current_clause:
                clauses.append(current_clause)
            current_clause = c_clean
        else:
            if current_clause:
                current_clause += "；" + c_clean
            else:
                current_clause = c_clean
    if current_clause:
        clauses.append(current_clause)

    if not clauses:
        clauses = [req_body.strip() if req_body.strip() else str(text).strip()]

    formatted_atoms = []
    for cl in clauses:
        cl_clean = re.sub(r'^[（(]\d+[）)]\s*', '', cl).strip()
        if not cl_clean:
            continue
        if subj and not cl_clean.startswith("【主体】"):
            formatted = f"【主体】{subj}；【要求】{cl_clean}"
        else:
            formatted = cl_clean
        formatted_atoms.append((subj, cl_clean, formatted))

    return formatted_atoms or [(subj, req_body, str(text))]


def get_asset_sn(asset) -> str:
    prefix_map = {
        "hardware": "H",
        "software": "S",
        "data": "D",
        "communication": "C"
    }
    t = str(getattr(asset, "asset_type", "") or "").lower().strip()
    p = prefix_map.get(t, "A")
    return f"{p}-{asset.id:03d}"


def build_id_maps(steps: List[TaraStep]):
    goal_set = set()
    claim_set = set()
    control_set = set()
    req_set = set()

    for step in steps:
        out = step.analysis_result.get("final_output") if step.analysis_result else None
        if not isinstance(out, dict):
            out = {}
        if step.stage == "stage4":
            decisions = out.get("risk_decisions") or []
            if not decisions and (out.get("cybersecurity_goal") or out.get("cybersecurity_claim")):
                decisions = [{
                    "risk_treatment": out.get("risk_decision", "Retain"),
                    "cybersecurity_goal": out.get("cybersecurity_goal", ""),
                    "cybersecurity_claim": out.get("cybersecurity_claim", "")
                }]
            for rd in decisions:
                rt = str(rd.get("risk_treatment") or rd.get("risk_decision") or "Retain").lower()
                is_exempted = rt in ["accept", "transfer", "share", "retain"]
                
                if is_exempted:
                    claim = str(rd.get("cybersecurity_claim") or "").strip()
                    if claim and claim != "N/A":
                        claim_set.add(claim)
                else:
                    goal = str(rd.get("cybersecurity_goal") or "").strip()
                    if goal and goal != "N/A":
                        goal_set.add(goal)
        elif step.stage == "stage5":
            reqs = out.get("requirements") or []
            if not reqs:
                cso_val = out.get("cso") or "N/A"
                csr_list = out.get("csr") or []
                if cso_val != "N/A" or csr_list:
                    if isinstance(csr_list, list) and csr_list:
                        reqs = [{"cybersecurity_control": cso_val, "cybersecurity_requirement": text} for text in csr_list]
                    else:
                        reqs = [{"cybersecurity_control": cso_val, "cybersecurity_requirement": str(csr_list)}]
            
            for req in reqs:
                ctrl = str(req.get("cybersecurity_control") or "").strip()
                if ctrl and ctrl != "N/A":
                    control_set.add(ctrl)
                rq = str(req.get("cybersecurity_requirement") or "").strip()
                if rq and rq != "N/A":
                    req_set.add(rq)
                    for line in rq.split("\n"):
                        cleaned = re.sub(r'^\(\d+\)\s*', '', line).strip()
                        if cleaned and cleaned != "N/A":
                            req_set.add(cleaned)
                            
            summarized_csrs = out.get("summarized_csrs") or []
            for csr in summarized_csrs:
                rq = str(csr.get("cybersecurity_requirement") or "").strip()
                if rq and rq != "N/A":
                    req_set.add(rq)
                    for line in rq.split("\n"):
                        cleaned = re.sub(r'^\(\d+\)\s*', '', line).strip()
                        if cleaned and cleaned != "N/A":
                            req_set.add(cleaned)

    def _hash_prefix(prefix: str, text: str) -> str:
        sha256_hash = hashlib.sha256(text.strip().encode('utf-8')).hexdigest()
        return f"{prefix}{sha256_hash[:8].upper()}"

    goal_map = {g: _hash_prefix("CSO_", g) for g in goal_set}
    claim_map = {c: _hash_prefix("CLM_", c) for c in claim_set}
    control_map = {ct: _hash_prefix("CSC_", ct) for ct in control_set}
    req_map = {r: _hash_prefix("CSR_", r) for r in req_set}

    return goal_map, claim_map, control_map, req_map


def get_csr_traceability_maps(stage5_out: dict, req_map: dict, strict: bool = True) -> tuple:
    """
    建立 TARA 原始需求 (Tier 1) 与资产级合并汇总需求 (Tier 2) 之间的追溯关系。

    strict=True（默认）只接受精确文本/别名/ID 匹配；无法匹配时保留为未映射，
    不再用子串或词重合度猜测，避免制造“看起来有追溯、实际不成立”的假阳性。
    strict=False 保留旧版模糊兜底，仅用于兼容历史数据。
    Returns:
      tara_to_csr: dict, 原始需求文本 -> list of summarized CSR hash IDs
      csr_to_tara: dict, summarized CSR hash ID -> list of original raw requirement IDs (CSR_XXXXXXXX)
    """
    tara_to_csr = {}
    csr_to_tara = {}
    
    requirements = stage5_out.get("requirements", [])
    summarized_csrs = stage5_out.get("summarized_csrs") or stage5_out.get("summarized_requirements") or []
    
    alias_to_req = {}
    for idx, req in enumerate(requirements, 1):
        text = str(req.get("cybersecurity_requirement") or "").strip()
        if not text or text == "N/A":
            continue
        raw_tara_id = req_map.get(text, req.get("cybersecurity_requirement_id") or f"CSR_{idx:04d}")
        req_entry = (text, raw_tara_id)
        
        aliases = [
            f"CSR-{idx:03d}",
            f"CSR-{idx}",
            f"CSR_{idx:03d}",
            f"CSR_{idx}",
            str(idx),
            raw_tara_id
        ]
        local_id = str(req.get("cybersecurity_requirement_id") or "").strip()
        if local_id:
            aliases.append(local_id)
        for line in text.split("\n"):
            cleaned = re.sub(r'^\(\d+\)\s*', '', line).strip()
            line_id = req_map.get(cleaned) if cleaned else None
            if line_id:
                aliases.append(line_id)

        for a in aliases:
            alias_to_req[str(a).lower().strip()] = req_entry

    all_csr_ids = set()
    for csr in summarized_csrs:
        csr_text = str(csr.get("cybersecurity_requirement") or "").strip()
        if not csr_text:
            continue
        csr_hash_id = req_map.get(csr_text, csr.get("csr_id") or "N/A")
        all_csr_ids.add(csr_hash_id)
        
        for req in requirements:
            orig_text = str(req.get("cybersecurity_requirement") or "").strip()
            if orig_text and orig_text == csr_text:
                raw_tara_id = req_map.get(orig_text, req.get("cybersecurity_requirement_id"))
                tara_to_csr.setdefault(orig_text, []).append(csr_hash_id)
                csr_to_tara.setdefault(csr_hash_id, []).append(raw_tara_id)

        s_ids = csr.get("source_requirement_ids") or []
        mapped_any = bool(csr_to_tara.get(csr_hash_id))
        
        for sid in s_ids:
            key = str(sid).lower().strip()
            if key in alias_to_req:
                orig_text, raw_tara_id = alias_to_req[key]
                tara_to_csr.setdefault(orig_text, []).append(csr_hash_id)
                csr_to_tara.setdefault(csr_hash_id, []).append(raw_tara_id)
                mapped_any = True
                
        if not mapped_any and not strict:
            for req in requirements:
                orig_text = str(req.get("cybersecurity_requirement") or "").strip()
                if not orig_text or orig_text == "N/A":
                    continue
                raw_tara_id = req_map.get(orig_text, req.get("cybersecurity_requirement_id") or "N/A")
                orig_clean = orig_text.lower()
                csr_clean = csr_text.lower()
                if orig_clean == csr_clean or orig_clean in csr_clean or csr_clean in orig_clean:
                    tara_to_csr.setdefault(orig_text, []).append(csr_hash_id)
                    csr_to_tara.setdefault(csr_hash_id, []).append(raw_tara_id)
                    mapped_any = True
                    
            if not mapped_any:
                for req in requirements:
                    orig_text = str(req.get("cybersecurity_requirement") or "").strip()
                    if not orig_text or orig_text == "N/A":
                        continue
                    raw_tara_id = req_map.get(orig_text, req.get("cybersecurity_requirement_id") or "N/A")
                    orig_words = set(re.findall(r"[\u4e00-\u9fa5]{2,}|\w{3,}", orig_text.lower()))
                    csr_words = set(re.findall(r"[\u4e00-\u9fa5]{2,}|\w{3,}", csr_text.lower()))
                    if orig_words and csr_words:
                        sim = len(orig_words.intersection(csr_words)) / len(orig_words.union(csr_words))
                        if sim >= 0.40:
                            tara_to_csr.setdefault(orig_text, []).append(csr_hash_id)
                            csr_to_tara.setdefault(csr_hash_id, []).append(raw_tara_id)

    if strict:
        unmapped = sorted(cid for cid in all_csr_ids if cid not in csr_to_tara)
        if unmapped:
            print(f"[TARA-AI] 追溯警告: {len(unmapped)} 项 CSR 无精确来源映射（strict 模式）: {', '.join(unmapped[:8])}")

    for k in tara_to_csr:
        tara_to_csr[k] = list(set(tara_to_csr[k]))
    for k in csr_to_tara:
        csr_to_tara[k] = list(set(csr_to_tara[k]))
        
    return tara_to_csr, csr_to_tara


def clean_and_parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    
    text = re.sub(r',\s*([\]}])', r'\1', text)
    parsed = json.loads(text, strict=False)
    if not isinstance(parsed, dict):
        raise TypeError(f"大模型返回的 JSON 解析结果不是 Dict 字典类型，而是 {type(parsed).__name__}")
    return parsed


def post_llm_request_with_retry(
    url: str,
    headers: dict,
    payload: dict,
    max_retries: int = 3,
    initial_delay: float = 1.0
) -> httpx.Response:
    delay = initial_delay
    last_exc = None
    for attempt in range(max_retries):
        try:
            with httpx.Client(timeout=90.0) as client:
                resp = client.post(url, headers=headers, json=payload)
            if resp.status_code == 200:
                return resp
            if resp.status_code in [429, 500, 502, 503, 504]:
                print(f"[TARA-AI] 大模型服务响应 {resp.status_code}，将在 {delay} 秒后重试 (第 {attempt + 1}/{max_retries} 次)...")
                time.sleep(delay)
                delay *= 2
            else:
                raise RuntimeError(f"API HTTP {resp.status_code}: {resp.text[:150]}")
        except (httpx.HTTPError, httpx.NetworkError, httpx.TimeoutException) as exc:
            last_exc = exc
            print(f"[TARA-AI] 网络异常 ({exc})，将在 {delay} 秒后重试 (第 {attempt + 1}/{max_retries} 次)...")
            time.sleep(delay)
            delay *= 2
            
    if last_exc:
        raise last_exc
    raise RuntimeError("大模型接口重试失败")


# -------------------------------------------------------------------------
# 语义相似比较与去重工具
# -------------------------------------------------------------------------
_STOP_TOKENS = {
    "security", "protection", "requirements", "requirement", "system", "safe",
    "安全", "要求", "保护", "防护", "控制", "管理", "设计", "机制", "实施", "进行",
    "应", "应能", "应可", "必须", "以及", "包括", "相关", "对于",
}

PRE_DEDUP_THRESHOLD = 0.80
CROSS_CATEGORY_DEDUP_THRESHOLD = 0.90
CONTENT_COVERAGE_MIN_RATIO = 0.30


def semantic_tokens(text: str) -> Set[str]:
    """提取规范化的中/英文 Token 用于语义相似度计算。"""
    t = str(text or "").lower()
    t = re.sub(r"【[^】]*】|\[[^\]]*\]", " ", t)
    tokens = set()
    for run in re.findall(r"[\u4e00-\u9fa5]+", t):
        if len(run) == 1:
            tokens.add(run)
        else:
            for i in range(len(run) - 1):
                tokens.add(run[i:i + 2])
    for w in re.findall(r"[a-zA-Z]{3,}", t):
        tokens.add(w)
    return set(w for w in tokens if w not in _STOP_TOKENS)


def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def dedup_stage5_summarized(summarized: list, requirements: list = None, threshold: float = PRE_DEDUP_THRESHOLD) -> list:
    """同资产后去重：合并 Stage5 LLM 漏合的近似重复 summarized_requirements。"""
    if not summarized:
        return summarized

    req_by_id = {}
    for r in requirements or []:
        rid = r.get("cybersecurity_requirement_id") or r.get("csr_id")
        if rid:
            req_by_id[str(rid)] = r

    clusters = []
    for s in summarized:
        text = f"{s.get('title', '')} {s.get('cybersecurity_requirement', '')}"
        toks = semantic_tokens(text)
        best, best_sim = None, 0.0
        for c in clusters:
            sim = _jaccard(toks, c["tokens"])
            if sim > best_sim:
                best, best_sim = c, sim
        if best is not None and best_sim >= threshold:
            best["items"].append(s)
            best["tokens"] |= toks
        else:
            clusters.append({"tokens": toks, "items": [s]})

    merged = []
    for c in clusters:
        group = c["items"]
        if len(group) == 1:
            merged.append(group[0])
            continue
        rep = max(group, key=lambda x: len(str(x.get("cybersecurity_requirement") or "")))
        sources, assets = [], []
        for s in group:
            for sid in (s.get("source_requirement_ids") or []):
                if str(sid) not in sources:
                    sources.append(str(sid))
            for an in (s.get("applicable_assets") or []):
                if an not in assets:
                    assets.append(an)
        new_s = dict(rep)
        new_s["source_requirement_ids"] = sources
        new_s["applicable_assets"] = assets
        new_s["_stage5_dedup_count"] = len(group)
        merged.append(new_s)

    for s in merged:
        toks = semantic_tokens(f"{s.get('title', '')} {s.get('cybersecurity_requirement', '')}")
        gaps = []
        for sid in (s.get("source_requirement_ids") or []):
            src = req_by_id.get(str(sid).strip())
            if not src:
                continue
            src_toks = semantic_tokens(
                f"{src.get('title', '')} {src.get('cybersecurity_requirement', '')}"
            )
            if not src_toks:
                continue
            overlap = len(src_toks & toks) / len(src_toks)
            if overlap < CONTENT_COVERAGE_MIN_RATIO:
                gaps.append({"source_id": str(sid), "overlap": round(overlap, 2)})
        s["_content_gaps"] = gaps
    return merged


def _call_llm_json(settings: dict, system_prompt: str, user_obj: dict, temperature: float = None) -> dict:
    """通用 LLM JSON 调用工具，供 csr_mapping 与 oem_csr_merge 复用。"""
    if not settings or not settings.get("api_key") or settings.get("api_key") in ["mock_test_key", ""]:
        raise RuntimeError("no LLM settings")
    url = f"{settings['api_base_url'].rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings['api_key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": settings.get("model_name", "deepseek-flash"),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_obj, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
    }
    if temperature is not None:
        payload["temperature"] = temperature
    resp = post_llm_request_with_retry(url, headers, payload, max_retries=2)
    return clean_and_parse_json(resp.json()["choices"][0]["message"]["content"])


# -------------------------------------------------------------------------
# 固定 CSR 目录适配（替代旧版动态综合管线）
# -------------------------------------------------------------------------
def hierarchical_synthesize_project_csrs(db: Session, domain: Domain, steps: list, assets: list) -> list:
    """返回基于固定 CSR 目录丰富后的 CSR 列表（包含 TARA 映射溯源）。"""
    if not db or not domain or not steps:
        return []
    from app.services.csr_mapping import build_mapping_report_data

    run_id = getattr(steps[0], "run_id", None)
    if run_id is None:
        return []
    data = build_mapping_report_data(db, domain.id, run_id)
    if not data:
        return []
    return data["csr_rows"]


def get_or_synthesize_global_csrs(
    db: Session,
    domain: Domain,
    steps: list,
    assets: list,
    hash_to_global_map: dict = None,
    force_recompute: bool = False
) -> list:
    """获取固定目录 CSR 并填充溯源字典映射。"""
    rows = hierarchical_synthesize_project_csrs(db, domain, steps, assets)
    if hash_to_global_map is not None:
        for row in rows:
            csr_id = row.get("csr_id")
            for tara_id in row.get("_raw_source_ids", []):
                if tara_id not in hash_to_global_map:
                    hash_to_global_map[tara_id] = set()
                elif isinstance(hash_to_global_map[tara_id], str):
                    hash_to_global_map[tara_id] = {hash_to_global_map[tara_id]}
                hash_to_global_map[tara_id].add(csr_id)
    return rows
