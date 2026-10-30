import os
import re
import json
import time
import hashlib
from collections import Counter, defaultdict
from typing import List, Dict, Tuple, Set
from concurrent.futures import ThreadPoolExecutor, as_completed
from sqlalchemy.orm import Session
import httpx

from app.models.tara_step import TaraStep
from app.models.asset import Asset
from app.models.domain import Domain
from app.models.diagram import Diagram
from app.models.system_settings import SystemSettings

# -------------------------------------------------------------------------
# ISO/SAE 21434 & UNECE R155 Standard Domain Taxonomy
# -------------------------------------------------------------------------
from app.core.taxonomy import (
    DOMAIN_PREFIX_MAP,
    MAJOR_ORDER,
    CANONICAL_DOMAINS,
    FUNCTIONAL_CATEGORY_NAMES,
    FUNCTIONAL_PREFIX_MAP,
    classify_functional_category,
    clean_major_category,
    correct_security_domain,
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
        return m.group(1).strip()
    m = re.search(r'\[Subject\]\s*([^\[]*)', str(text), re.IGNORECASE)
    if m and m.group(1).strip():
        return m.group(1).strip().rstrip(";").strip()
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

# R155 Annex 5 is used here as a mitigation-dimension checklist, not as a
# technology selection list. Do not add concrete protocol names, algorithm
# names, port numbers, or numeric thresholds to these strings: the synthesis
# prompt must stay capability-level so downstream design can choose the
# implementation.
UNECE_R155_ANNEX5_GUIDELINES = {
    "硬件安全 / Hardware Security": (
        "参考 UNECE R155 Annex 5 的硬件/物理接口防护维度：\n"
        "- 物理防拆、外壳/连接器开盖检测与预定义安全响应；\n"
        "- 电路板走线/测试点隐藏、封装与物理加固；\n"
        "- 调试/编程接口的量产不可逆禁用与受控授权解锁；\n"
        "- 故障注入与侧信道攻击的基础防护；\n"
        "- 外部非易失存储的硬件绑定加密与防脱机读取；\n"
        "- 硬件信任根、安全配给与供应链可追溯。"
    ),
    "系统安全 / System Security": (
        "参考 UNECE R155 Annex 5 的系统/执行环境防护维度：\n"
        "- 基于硬件信任根的分级安全启动与完整性校验；\n"
        "- 安全版本防回滚；\n"
        "- 原子更新与可回退/可恢复机制；\n"
        "- 强制访问控制、最小权限与系统分区保护；\n"
        "- 运行时完整性度量、异常检测与安全看门狗；\n"
        "- 内存漏洞利用缓解与安全降级。"
    ),
    "密码安全与密钥管理 / Cryptography & Key Management": (
        "参考 UNECE R155 Annex 5 的密码机制防护维度：\n"
        "- 密钥/证书在隔离安全硬件或受保护密码服务内存储与使用；\n"
        "- 密钥与证书的生成、注入、更新、轮换、吊销、销毁全生命周期；\n"
        "- 传输、存储与应用层数据的机密性/完整性保护；\n"
        "- 数字签名、消息认证与防重放；\n"
        "- 高质量随机数与熵源；\n"
        "- 易失性敏感材料的零化与销毁。"
    ),
    "应用安全 / Application Security": (
        "参考 UNECE R155 Annex 5 的应用/服务防护维度：\n"
        "- 安全编码、输入校验与模糊测试；\n"
        "- 服务、接口与进程间通信的身份认证与最小权限授权；\n"
        "- 会话管理、防暴力破解与权限回收；\n"
        "- 二进制抗逆向、符号剥离与后门清理；\n"
        "- 应用完整性、日志与异常处理；\n"
        "- 安全失效与降级策略。"
    ),
    "数据安全与隐私保护 / Data Security & Privacy": (
        "参考 UNECE R155 Annex 5 的数据/隐私防护维度：\n"
        "- 数据分类分级、最小化、目的限制与保留期限；\n"
        "- 静态数据加密与访问控制；\n"
        "- 传输通道加密与来源真实性；\n"
        "- 个人数据去标识化、匿名化与实时脱敏；\n"
        "- 用户数据删除、解绑与跨境传输限制；\n"
        "- 关键配置/标定数据的完整性与防篡改。"
    ),
    "安全监控、测试与响应 / Security Monitoring, Testing & Response": (
        "参考 UNECE R155 Annex 5 的监控/响应防护维度：\n"
        "- 车载网络与主机入侵检测、异常流量监控与告警；\n"
        "- 安全事件审计日志的完整性、防篡改与留存；\n"
        "- 特权操作/调试会话的审计与身份绑定；\n"
        "- 安全事件的分级响应、限流、阻断与恢复；\n"
        "- 日志/存储资源配额与抗泛洪保护；\n"
        "- 安全测试、漏洞扫描与渗透验证。"
    ),
    "通信安全 / Communication Security": (
        "参考 UNECE R155 Annex 5 的通信/网络防护维度：\n"
        "- 关键总线报文的真实性、完整性与新鲜度；\n"
        "- 车云/车载网络安全通道、双向身份认证与证书校验；\n"
        "- 网络分区、通信矩阵白名单与跨域访问控制；\n"
        "- 诊断/服务接口的安全访问与授权；\n"
        "- 资源耗尽、泛洪与拒绝服务防护；\n"
        "- 时间同步与传感器链路的数据完整性。"
    ),
    "安全开发、供应链与运维 / Secure Development, Supply Chain & Operations": (
        "参考 UNECE R155 Annex 5 的生命周期/供应链防护维度：\n"
        "- 安全编码规范、静态分析与代码审查；\n"
        "- 漏洞管理、软件物料清单与第三方组件治理；\n"
        "- 产线密钥/证书注入、配置基线与可追溯；\n"
        "- 供应链防假冒与组件来源核查；\n"
        "- 安全测试、渗透测试与发布门禁；\n"
        "- 售后维护、备份恢复与安全更新。"
    ),
    "通用安全与其它 / General & Other Security": (
        "参考 UNECE R155 Annex 5 的通用安全维度：\n"
        "- 不属于上述分类的安全能力、流程或支撑要求；\n"
        "- 应说明保护对象、控制目标与验证方法，保持技术中立。"
    ),
}

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
                    # Keep the full requirement text as a lookup key too. A
                    # multi-line requirement must not lose its full-text alias
                    # just because per-line keys are also registered.
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

    def _content_hash_id(prefix: str, text: str) -> str:
        sha256_hash = hashlib.sha256(text.strip().encode('utf-8')).hexdigest()
        return f"{prefix}{sha256_hash[:8].upper()}"

    goal_map = {g: _content_hash_id("CSO_", g) for g in goal_set}
    claim_map = {c: _content_hash_id("CLM_", c) for c in claim_set}
    control_map = {ct: _content_hash_id("CSC_", ct) for ct in control_set}
    req_map = {r: _content_hash_id("CSR_", r) for r in req_set}

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
    
    # 1. 为每个原始需求构建别名索引映射到 (text, raw_tara_id)
    # 支持 CSR-001, CSR-1, 1, 原始 requirement_id, req_map 中的哈希 ID
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
        # Older maps keyed multi-line requirements per line; keep those
        # content-hash aliases so historical source IDs still resolve.
        for line in text.split("\n"):
            cleaned = re.sub(r'^\(\d+\)\s*', '', line).strip()
            line_id = req_map.get(cleaned) if cleaned else None
            if line_id:
                aliases.append(line_id)

        for a in aliases:
            alias_to_req[str(a).lower().strip()] = req_entry

    # 2. 遍历 summarized_csrs 建立双向追溯
    all_csr_ids = set()
    for csr in summarized_csrs:
        csr_text = str(csr.get("cybersecurity_requirement") or "").strip()
        if not csr_text:
            continue
        csr_hash_id = req_map.get(csr_text, csr.get("csr_id") or "N/A")
        all_csr_ids.add(csr_hash_id)
        
        # 0. 精确文本匹配优先自动包含自身
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
                
        # 兜底：如果 source_requirement_ids 无法匹配。
        # 默认 strict 模式不做模糊猜测；只有显式 strict=False 才启用历史兜底。
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

    # 去重
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

def post_llm_request_with_retry(url: str, headers: dict, payload: dict, max_retries: int = 3, initial_delay: float = 1.0) -> httpx.Response:
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
# Dynamic Semantic Fallback Clustering (Pure Algorithm, Zero Hardcoding)
# -------------------------------------------------------------------------
def semantic_fallback_clustering(domain_name: str, items: list) -> list:
    """
    轻量自适应语义去重算法（基于中文/英文词根与 Jaccard 重合度计算）。
    当大模型 API 离线或不可达时自动启用兜底，保证导出 100% 不抛错，且不包含任何硬编码关键字列表。
    """
    def _extract_tokens(text: str) -> Set[str]:
        zh_words = re.findall(r"[\u4e00-\u9fa5]{2,}", text)
        en_words = re.findall(r"[a-zA-Z]{3,}", text.lower())
        noise = {"security", "protection", "requirements", "requirement", "system", "safe", "安全", "要求", "保护", "防护", "控制", "管理", "设计", "机制"}
        return set(w for w in zh_words + en_words if w not in noise)

    clusters = []  # list of dict: {"tokens": set, "items": list}
    for it in items:
        toks = _extract_tokens((it.get("sub_title") or "") + " " + (it.get("title") or "") + " " + (it.get("req") or ""))
        best_c = None
        best_sim = 0.0
        for c in clusters:
            union_len = len(toks.union(c["tokens"]))
            if union_len > 0:
                sim = len(toks.intersection(c["tokens"])) / union_len
                if sim > best_sim:
                    best_sim = sim
                    best_c = c
        if best_sim >= 0.30 and best_c is not None:
            best_c["items"].append(it)
            best_c["tokens"].update(toks)
        else:
            clusters.append({"tokens": set(toks), "items": [it]})

    consolidated = []
    for c in clusters:
        cluster_items = c["items"]
        # 选出文本最详尽、最丰富的项作为代表
        best_item = max(cluster_items, key=lambda x: len(str(x.get("req") or "")))
        
        all_sources = []
        all_assets = []
        for it in cluster_items:
            rid = it.get("raw_id")
            if rid and rid not in all_sources:
                all_sources.append(rid)
            aname = it.get("asset_name")
            if aname and aname not in all_assets:
                all_assets.append(aname)
                
        consolidated.append({
            "sub_category": best_item.get("sub_title") or "通用安全防护 / General Security Protection",
            "title": best_item.get("title") or "安全需求 / Security Requirement",
            "cybersecurity_requirement": best_item.get("req") or "",
            "applicable_assets": all_assets,
            "source_requirement_ids": all_sources,
            "verification_method": "检查/测试（语义兜底，需人工复核） / Inspection/Test (semantic fallback, manual review required)",
            "requirement_type": "product",
            "rationale": "语义兜底聚类，需人工复核 / Semantic fallback clustering, manual review required",
            "_degraded": True,
            "_degraded_reason": "LLM unavailable or failed; semantic fallback used"
        })
        
    return consolidated


# -------------------------------------------------------------------------
# 语义相似预去重 + 内容覆盖校验
# -------------------------------------------------------------------------
_STOP_TOKENS = {
    "security", "protection", "requirements", "requirement", "system", "safe",
    "安全", "要求", "保护", "防护", "控制", "管理", "设计", "机制", "实施", "进行",
    "应", "应能", "应可", "必须", "以及", "包括", "相关", "对于",
}

# 语义去重阈值（可调）：
# - 预去重：0.80 能把"请求实体 / 请求调试的实体"这类轻微改写的近似重复合并，
#   同时避免把同一主题但不同控制目标的条目误合；
# - 跨分类：0.90 更保守，只合并几乎同义、却被分到不同分类的条目；
# - 内容覆盖：来源关键词在合并正文中的最低重合率，低于此值标记内容缺口。
PRE_DEDUP_THRESHOLD = 0.80
CROSS_CATEGORY_DEDUP_THRESHOLD = 0.90
CONTENT_COVERAGE_MIN_RATIO = 0.30


def semantic_tokens(text: str) -> Set[str]:
    """Extract normalized Chinese/English tokens for similarity comparison.

    Chinese is split into 2-grams (not whole runs) so that near-identical
    sentences share tokens instead of becoming one giant unmatched token.
    """
    t = str(text or "").lower()
    # 去掉【主体】/【要求】/[Subject]/[Requirement] 等标签
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


def dedup_items_by_similarity(items: list, threshold: float = 0.85) -> list:
    """在交给 LLM 合并前，先做一次确定性的语义相似预去重。

    只合并相似度 >= threshold 的近似重复项（阈值默认 0.85，偏保守，避免误合）；
    真正的语义合并仍由 LLM 完成。被合并的项，其 raw_id 会汇总到
    ``source_requirement_ids``，供 ``enforce_csr_coverage`` 追溯。
    """
    if not items:
        return items

    clusters = []  # [{"tokens": set, "items": [item]}]
    for it in items:
        text = f"{it.get('sub_title', '')} {it.get('title', '')} {it.get('req', '')}"
        toks = semantic_tokens(text)
        best_c, best_sim = None, 0.0
        for c in clusters:
            sim = _jaccard(toks, c["tokens"])
            if sim > best_sim:
                best_c, best_sim = c, sim
        if best_c is not None and best_sim >= threshold:
            best_c["items"].append(it)
            best_c["tokens"] |= toks
        else:
            clusters.append({"tokens": toks, "items": [it]})

    merged = []
    for c in clusters:
        group = c["items"]
        if len(group) == 1:
            merged.append(group[0])
            continue
        # 以文本最详尽者作代表，合并来源 raw_id 与适用资产
        rep = max(group, key=lambda x: len(str(x.get("req") or "")))
        sources, assets = [], []
        for it in group:
            rid = it.get("raw_id")
            if rid and rid not in sources:
                sources.append(rid)
            an = it.get("asset_name")
            if an and an not in assets:
                assets.append(an)
        new_item = dict(rep)
        new_item["source_requirement_ids"] = sources
        new_item["applicable_assets"] = assets
        new_item["_pre_dedup_count"] = len(group)
        new_item["_pre_dedup_sources"] = sources
        merged.append(new_item)
    return merged


def _item_all_ids(it: dict) -> list:
    """Return every raw_id an input item stands for (pre-dedup may merge several)."""
    ids = []
    if it.get("raw_id"):
        ids.append(str(it["raw_id"]).strip())
    for sid in (it.get("source_requirement_ids") or []):
        sid = str(sid).strip()
        if sid:
            ids.append(sid)
    for sid in (it.get("_pre_dedup_sources") or []):
        sid = str(sid).strip()
        if sid:
            ids.append(sid)
    return [i for i in ids if i]


def verify_merged_content_coverage(items: list, cons: list, min_ratio: float = 0.30) -> list:
    """内容覆盖校验：检查每条合并 CSR 是否真的覆盖了其来源需求的关键内容。

    覆盖契约（enforce_csr_coverage）只保证 source_requirement_ids 齐全，
    但合并后的正文可能丢掉来源里的关键条件。这里用关键词重合率做二次校验：
    若某来源需求的关键词在合并后正文中的重合率低于 min_ratio，则记录到
    ``_content_gaps``，供人工复核。
    """
    item_by_id = {}
    for it in items:
        for rid in _item_all_ids(it):
            item_by_id.setdefault(rid, it)

    for c in cons:
        req_text = str(c.get("cybersecurity_requirement") or "")
        req_tokens = semantic_tokens(req_text)
        gaps = []
        for sid in c.get("source_requirement_ids") or []:
            src = item_by_id.get(str(sid).strip())
            if not src:
                continue
            src_tokens = semantic_tokens(
                f"{src.get('title', '')} {src.get('req', '')}"
            )
            if not src_tokens:
                continue
            overlap = len(src_tokens & req_tokens) / len(src_tokens)
            if overlap < min_ratio:
                gaps.append({"source_id": str(sid), "overlap": round(overlap, 2)})
        c["_content_gaps"] = gaps
        c["_content_coverage_ok"] = not gaps
    return cons


# -------------------------------------------------------------------------
# LLM-Powered UNECE R155 Annex 5 Guided Synthesis
# -------------------------------------------------------------------------
def enforce_csr_coverage(items: list, cons: list) -> tuple:
    """Ensure every input raw_id is covered by at least one consolidated CSR.

    The prompt asks the model for 100% traceability, but prompts alone are not
    a guarantee. This function is the code-level contract:
      - strips hallucinated source IDs that were not in the input;
      - appends one conservative fallback CSR for every missing raw_id;
      - reports duplicates (allowed only for intentional splits).
    """
    input_ids = []
    for it in items:
        for rid in _item_all_ids(it):
            if rid and rid not in input_ids:
                input_ids.append(rid)
    input_set = set(input_ids)

    seen = set()
    for c in cons:
        raw_ids = c.get("source_requirement_ids") or []
        if not isinstance(raw_ids, list):
            raw_ids = [x.strip() for x in str(raw_ids).split(",") if x.strip()]
        normalized = []
        for sid in raw_ids:
            sid = str(sid).strip()
            if sid and sid not in normalized:
                normalized.append(sid)
        c["source_requirement_ids"] = normalized
        seen.update(normalized)

    extra = sorted(seen - input_set)
    if extra:
        for c in cons:
            c["source_requirement_ids"] = [
                sid for sid in c["source_requirement_ids"] if sid in input_set
            ]

    missing = [rid for rid in input_ids if rid not in seen]
    for rid in missing:
        item = next((x for x in items if rid in _item_all_ids(x)), None)
        if not item:
            continue
        cons.append({
            "sub_category": item.get("sub_title") or "通用安全防护 / General Security Protection",
            "title": item.get("title") or "安全需求 / Security Requirement",
            "cybersecurity_requirement": item.get("req") or "",
            "applicable_assets": [item.get("asset_name")] if item.get("asset_name") else [],
            "source_requirement_ids": [rid],
            "verification_method": "检查/测试（覆盖兜底条目，需人工复核） / Inspection/Test (coverage fallback, manual review required)",
            "requirement_type": "product",
            "rationale": "原始需求兜底保留，需人工复核 / Original requirement preserved as coverage fallback, manual review required",
            "_coverage_status": "fallback_original",
        })

    counts = Counter(
        sid for c in cons for sid in c.get("source_requirement_ids", [])
    )
    duplicates = {sid: n for sid, n in counts.items() if n > 1}
    all_covered = all(rid in counts for rid in input_ids)

    verification_missing = []
    for c in cons:
        if not str(c.get("verification_method") or "").strip():
            verification_missing.append(
                ", ".join(c.get("source_requirement_ids", [])) or str(c.get("title") or "")
            )
            c["verification_method"] = (
                "检查/测试（验证方法待补充，需人工复核） / "
                "Inspection/Test (verification method TBD, manual review required)"
            )
            c["_coverage_status"] = c.get("_coverage_status") or "verification_missing"

    report = {
        "input_count": len(input_ids),
        "output_source_count": len(counts),
        "missing_raw_ids": missing,
        "extra_raw_ids": extra,
        "duplicate_raw_ids": duplicates,
        "fallback_count": len(missing),
        "verification_missing_count": len(verification_missing),
        "verification_complete": not verification_missing,
        # complete=True means the model itself covered every input ID.
        # all_covered=True means the final export is safe because fallback
        # entries filled the gaps.
        "complete": not missing and not extra,
        "all_covered": all_covered,
    }
    if missing or extra or verification_missing:
        print(
            f"[TARA-AI] CSR 覆盖校验: missing={len(missing)}, extra={len(extra)}, "
            f"duplicates={len(duplicates)}, verification_missing={len(verification_missing)}"
        )
    return cons, report


def synthesize_domain_csrs_with_llm(db: Session, domain_name: str, items: list) -> tuple:
    """
    使用大模型依据 UNECE R155 Annex 5 规范指引，对某一分类域下的资产原始安全需求进行
    项目级（Item-level）全局归纳、去重、资产解耦与原子化提炼。

    Returns:
        (consolidated_csrs, coverage_report)
    """
    if not items:
        return [], {"input_count": 0, "complete": True}

    # 语义相似预去重：相似度 >= PRE_DEDUP_THRESHOLD 的近似重复先合并，降低 LLM 漏合概率
    original_count = len(items)
    items = dedup_items_by_similarity(items, threshold=PRE_DEDUP_THRESHOLD)
    if len(items) != original_count:
        print(f"[TARA-AI] 【{domain_name}】语义预去重: {original_count} -> {len(items)} 项")

    settings = db.query(SystemSettings).first()
    if not settings or not settings.api_key or settings.api_key in ["mock_test_key", ""]:
        print(f"[TARA-AI] 未配置有效大模型 API Key，启用通用语义算法兜底聚类 ({domain_name})")
        cons = semantic_fallback_clustering(domain_name, items)
        cons, report = enforce_csr_coverage(items, cons)
        report["mode"] = "semantic_fallback"
        return cons, report

    guideline = UNECE_R155_ANNEX5_GUIDELINES.get(
        domain_name, "参考 UNECE R155 Annex 5 汽车网络安全威胁与缓解通用规范。"
    )

    prompt = f"""你是一名资深汽车网络安全系统架构专家（精通 ISO/SAE 21434 与 UNECE R155 法规）。
当前正在执行【{domain_name}】领域的项目级网络安全需求（Item-level Cybersecurity Requirements, CSR）全局综合提炼与去重。

【法规参考依据 (UNECE R155 Annex 5 核心指引)】：
{guideline}

【写作边界】：
- Cybersecurity Goal：对资产/接口的安全属性与期望结果的高层声明；技术中立，不写协议、算法、产品、阈值。
- Cybersecurity Claim：剩余风险可接受/已转移的正式声明；说明剩余风险、依据、适用条件、有效期与批准角色。
- Cybersecurity Control：控制类型（预防/检测/纠正；技术/操作）+ 控制目标 + 作用点；不写具体协议/算法/产品。
- Cybersecurity Requirement：责任主体（哪个组件/系统/模块负责实施）+ 保护对象（对什么）+ 应/不应行为（实施什么控制）+ 触发条件 + 可验证结果 + 验证方法；技术中立。禁止用事件、现象、数据或抽象名词作句子主语。
- 即使输入需求包含具体协议、算法、端口或数值，输出必须抽象为能力要求，不得原样保留。

【任务与合并原则】：
1. 跨资产归纳合并：输入是当前被评估对象（Item）中各个底层资产提出的网络安全要求列表。
2. 原子化边界：一条 CSR = 一个责任主体 + 一个保护对象 + 一个控制目标 + 一个可验证结果。
3. 只有“同一资产类别、同一攻击面、同一控制目标、同一验证方法”才合并；适用范围、适用资产或安全等级不同的，必须拆成多条。
4. 合并后必须保留所有来源的适用条件差异，不能用一个来源的阈值/例外代表全部来源。
5. 技术中立：聚焦安全功能能力与防御目标（What & Why），严禁写死具体算法型号、协议版本、端口号或单一数值；用“经行业认可的高强度算法”“标准安全通道”“安全会话超时”等能力表述。
6. 每条 CSR 必须给出验证方法（测试/分析/检查/演示）、需求类型（product/process/supplier）和需求依据（rationale）；没有验证方法的条目不能作为最终 CSR。
7. 中英文双语规范：sub_category、title、cybersecurity_requirement、verification_method、rationale 必须中英文对照，格式为“中文 / English”。
8. 100% 完整双向追溯：source_requirement_ids 必须精确列出该项涵盖的所有原始 raw_id；输入中的每一个 raw_id 必须被至少一项 CSR 覆盖，绝不允许丢项。
9. 适用资产映射：applicable_assets 数组列出所有适用的资产名称。
10. 责任主体明确（强制）：每条 CSR 的 cybersecurity_requirement 必须以负责实施的组件/系统/模块作主语，写清「谁（组件/系统/模块）对什么对象实施什么控制」；禁止用事件、现象、数据或抽象名词作句子主语；例如「完整性校验失败事件应记录至本地审计存储」必须写成「ADCU 审计模块应将完整性校验失败事件记录至本地审计存储」；同时在 subject 字段单列该责任主体。

请严格输出如下 JSON 格式：
{{
  "consolidated_csrs": [
    {{
      "sub_category": "安全机制子分类名称 / Sub-category mechanism",
      "title": "项目级需求标题 / Project-level requirement title",
      "subject": "责任主体（组件/系统/模块）/ Responsible subject",
      "cybersecurity_requirement": "中文需求完整规范描述... / English requirement specification...",
      "verification_method": "测试/分析/检查/演示方法 / Verification method",
      "requirement_type": "product | process | supplier",
      "rationale": "需求依据与对应威胁/攻击路径 / Requirement rationale and linked threat/attack path",
      "applicable_assets": ["资产名称1", "资产名称2"],
      "source_requirement_ids": ["raw_id_1", "raw_id_2"]
    }}
  ]
}}
"""

    url = f"{settings.api_base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": settings.model_name,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": json.dumps(items, ensure_ascii=False)}
        ],
        "response_format": {"type": "json_object"}
    }

    try:
        print(f"[TARA-AI] 正在通过大模型进行【{domain_name}】项目级全局需求提炼 ({len(items)} 项原始需求)...")
        resp = post_llm_request_with_retry(url, headers, payload, max_retries=2)
        resp_data = resp.json()
        raw_text = resp_data["choices"][0]["message"]["content"]
        parsed = clean_and_parse_json(raw_text)
        cons = parsed.get("consolidated_csrs", [])
        if not cons:
            raise ValueError("大模型返回 consolidated_csrs 列表为空")

        cons, report = enforce_csr_coverage(items, cons)

        # Coverage is a hard contract. If the first pass dropped input IDs, ask
        # the model once to re-emit the complete set, then keep whichever
        # result covers more inputs. Fallback entries still protect the export
        # if the retry also fails.
        missing_ids = report.get("missing_raw_ids") or []
        if missing_ids:
            print(
                f"[TARA-AI] 【{domain_name}】首次覆盖不完整，重试一次: "
                f"{', '.join(missing_ids[:8])}"
            )
            repair_instruction = (
                "上一次输出遗漏了以下原始 raw_id："
                f"{', '.join(missing_ids)}。\n"
                "请重新输出完整的 consolidated_csrs（不是增量），确保输入中的每一个 raw_id "
                "都至少被一项 CSR 的 source_requirement_ids 覆盖；禁止编造不存在的 raw_id。"
            )
            repair_payload = {
                "model": settings.model_name,
                "messages": payload["messages"] + [
                    {"role": "assistant", "content": raw_text},
                    {"role": "user", "content": repair_instruction},
                ],
                "response_format": payload["response_format"],
            }
            try:
                resp_repair = post_llm_request_with_retry(
                    url, headers, repair_payload, max_retries=2
                )
                raw_text_repair = resp_repair.json()["choices"][0]["message"]["content"]
                parsed_repair = clean_and_parse_json(raw_text_repair)
                cons_repair = parsed_repair.get("consolidated_csrs", [])
                if cons_repair:
                    cons_repair, report_repair = enforce_csr_coverage(items, cons_repair)
                    if len(report_repair.get("missing_raw_ids") or []) < len(missing_ids):
                        cons, report = cons_repair, report_repair
                        report["mode"] = "llm_coverage_retry"
                        print(
                            f"[TARA-AI] 【{domain_name}】覆盖重试改善: "
                            f"missing={len(report.get('missing_raw_ids') or [])}"
                        )
            except Exception as retry_err:
                print(
                    f"[TARA-AI] 警告: 【{domain_name}】覆盖重试失败 "
                    f"({retry_err})，保留首次结果并用兜底条目补齐。"
                )

        # 内容覆盖校验：合并后正文是否真的覆盖了每个来源的关键内容
        verify_merged_content_coverage(items, cons, min_ratio=CONTENT_COVERAGE_MIN_RATIO)
        gaps_count = sum(1 for c in cons if c.get("_content_gaps"))
        print(
            f"[TARA-AI] 【{domain_name}】提炼成功: {len(items)} 项原始需求 -> {len(cons)} 项 CSR；"
            f"覆盖完整={report.get('complete')}；内容缺口条目={gaps_count}"
        )
        return cons, report
    except Exception as e:
        print(f"[TARA-AI] 警告: 【{domain_name}】大模型提炼失败 ({e})，平滑切换至通用语义算法兜底...")
        cons = semantic_fallback_clustering(domain_name, items)
        cons, report = enforce_csr_coverage(items, cons)
        report["mode"] = "semantic_fallback_after_error"
        return cons, report


def dedup_stage5_summarized(summarized: list, requirements: list = None, threshold: float = PRE_DEDUP_THRESHOLD) -> list:
    """同资产后去重：合并 Stage5 LLM 漏合的近似重复 summarized_requirements。

    与跨资产阶段使用同一套语义近似判定（中文 2-gram + Jaccard）。
    合并后统一 source_requirement_ids / applicable_assets，并做内容覆盖校验，
    结果写入 ``_content_gaps``。
    """
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

    # 内容覆盖校验（来源关键词是否真的落到合并正文里）
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


def dedup_cross_category(rows: list, threshold: float = 0.90) -> list:
    """跨分类语义去重：合并不同功能分类下高度相似的 CSR。

    阈值默认 0.90（偏保守）：只合并几乎同义、但被分到不同分类的条目。
    合并后保留先出现的分类，来源 ID 取并集，并标记 ``_cross_category_merged``。
    """
    if len(rows) < 2:
        return rows

    kept = []
    used = set()
    token_cache = {}

    for i, r in enumerate(rows):
        if i in used:
            continue
        text = f"{r.get('title', '')} {r.get('cybersecurity_requirement', '')}"
        toks = semantic_tokens(text)
        token_cache[i] = toks
        sources = [str(x) for x in (r.get("source_requirement_ids") or []) if str(x).strip()]
        assets = [a for a in str(r.get("applicable_assets") or "").split("\n") if a]
        merged_from = []

        for j in range(i + 1, len(rows)):
            if j in used:
                continue
            r2 = rows[j]
            if j not in token_cache:
                token_cache[j] = semantic_tokens(
                    f"{r2.get('title', '')} {r2.get('cybersecurity_requirement', '')}"
                )
            if _jaccard(toks, token_cache[j]) >= threshold:
                used.add(j)
                merged_from.append(str(r2.get("csr_id") or ""))
                for sid in (r2.get("source_requirement_ids") or []):
                    if str(sid) not in sources:
                        sources.append(str(sid))
                for an in str(r2.get("applicable_assets") or "").split("\n"):
                    if an and an not in assets:
                        assets.append(an)

        if merged_from:
            r["_cross_category_merged"] = True
            r["_cross_category_sources"] = merged_from
            r["source_requirement_ids"] = sources
            r["source_cybersecurity_requirement_ids"] = "; ".join(sources)
            r["source_count"] = len(sources)
            r["applicable_assets"] = "\n".join(assets)
            r["assets_count"] = len(assets)
        kept.append(r)
    return kept


def classify_csr_functional_categories(db: Session, rows: list) -> list:
    """把原子化后的 CSR 逐条分类到 17 个功能分类（LLM 主分类 + 规则校验）。

    遵循「合并去重 → 原子化 → 分类」：本函数只做分类，不改变 CSR 的原子化结果。
    每条结果写入：
      functional_category / category_source / category_confidence / category_reason / needs_review
    LLM 不可用时回退到确定性规则分类。
    """
    from app.core.taxonomy import (
        FUNCTIONAL_PROMPT_LIST,
        FUNCTIONAL_KEYWORDS_PROMPT,
        FUNCTIONAL_CONFUSION_RULES,
        classify_functional_category,
        correct_functional_category,
    )

    if not rows:
        return rows

    def _rule_of(r):
        return classify_functional_category(
            f"{r.get('title', '')} {r.get('cybersecurity_requirement', '')} {r.get('subject', '')}"
        )

    # 规则初判（LLM 失败/漏项时兜底）
    for r in rows:
        r["_rule_category"] = _rule_of(r)

    settings = db.query(SystemSettings).first() if db is not None else None
    if not settings or not settings.api_key or not settings.api_base_url:
        print("[TARA-AI] 未配置大模型，功能分类回退规则分类")
        for r in rows:
            r["functional_category"] = r.get("_rule_category")
            r["category_source"] = "rule_fallback"
            r["category_confidence"] = "low"
            r["category_reason"] = ""
            r["needs_review"] = True
        return rows

    items = [
        {
            "id": r.get("csr_id"),
            "title": r.get("title"),
            "subject": r.get("subject"),
            "requirement": r.get("cybersecurity_requirement"),
        }
        for r in rows
    ]

    prompt = f"""你是一名资深汽车网络安全需求管理专家（精通 ISO/SAE 21434 与 UNECE R155）。
请把下面每一条**已原子化**的网络安全需求，归入且仅归入以下 17 个功能分类之一。

【分类枚举（只能原样输出编号+名称，输出其它值一律视为不合格）】
{FUNCTIONAL_PROMPT_LIST}

【关键字锚定（命中这些词时优先归入对应分类）】
{FUNCTIONAL_KEYWORDS_PROMPT}

【易混类别（必须分清）】
{FUNCTIONAL_CONFUSION_RULES}

【判定优先级（从上到下，命中即停）】
1) 物理/PCB/丝印/封装/防拆/探针/HSM/eFUSE/SMPU/存储绑定/不可逆禁用 → 4.1 硬件安全
2) 主题是调试（认证/会话/命令/审计/完整性） → 4.4 安全调试
3) OTA/FOTA/远程升级/远程刷写 → 4.6 OTA 刷写安全
4) 本地刷写（UDS/CAN/以太网/产线/诊断刷写） → 4.5 本地刷写安全
5) 启动/回滚 → 4.7 安全启动
6) 数据脱敏/去标识/匿名/隐私/数据分类 → 4.9 数据脱敏与隐私安全
7) 存储 → 4.8 安全存储
8) 诊断（27 服务） → 4.10 安全诊断
9) 日志/审计 → 4.11 安全日志
10) 加密/签名/密钥/证书/密码算法 → 4.12 密码、密钥与证书安全
11) 雷达/摄像头/点云/图像/传感器 → 4.13 传感器安全
12) 防火墙/入侵检测/网络隔离 → 4.14 防火墙安全
13) 车内总线/报文/以太网 → 4.15 车内通信安全
14) 远程/外网/云端 → 4.16 远程通讯安全
15) 代码扫描/漏洞/SBOM/供应链/渗透测试 → 4.17 安全开发与供应链安全
16) 认证/授权/最小权限/会话（非调试） → 4.3 访问控制
17) 兜底 → 4.2 系统加固

【反例（必须照此判定）】
- "调试接口应对请求实体执行强身份认证" → 4.4 安全调试（不是 4.1）
- "调试接口应在量产阶段通过 OTP 永久禁用" → 4.1 硬件安全
- "通过 UDS 诊断刷写应用软件" → 4.5 本地刷写安全
- "通过云端下发 OTA 包升级" → 4.6 OTA 刷写安全
- "PCB 丝印应移除调试口标识" → 4.1 硬件安全
- "用户个人数据应脱敏存储" → 4.9 数据脱敏与隐私安全

【输出前自检】
a) functional_category 是否严格是上面 17 个值之一？
b) 是否符合优先级规则（尤其"调试认证→4.4、物理禁用→4.1"）？
c) 本地刷写与 OTA 刷写是否区分正确？

【输出 JSON 格式】
{{"classifications": [{{"id": "CSR-XXX-001", "functional_category": "4.x 名称", "category_reason": "一句话理由"}}]}}

必须覆盖输入中的每一个 id，不得遗漏、不得编造。"""

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

    by_id = {}
    try:
        resp = post_llm_request_with_retry(url, headers, payload, max_retries=2)
        raw_text = resp.json()["choices"][0]["message"]["content"]
        parsed = clean_and_parse_json(raw_text)
        for c in parsed.get("classifications") or []:
            cid = str(c.get("id") or "").strip()
            if cid:
                by_id[cid] = c
        print(f"[TARA-AI] 功能分类完成: LLM 覆盖 {len(by_id)}/{len(rows)} 条")
    except Exception as exc:
        print(f"[TARA-AI] 功能分类 LLM 失败，回退规则分类: {exc}")

    for r in rows:
        c = by_id.get(str(r.get("csr_id") or "").strip()) or {}
        text = f"{r.get('title', '')} {r.get('cybersecurity_requirement', '')} {r.get('subject', '')}"
        cat, source, needs_review = correct_functional_category(
            c.get("functional_category"), text
        )
        if not c.get("functional_category"):
            # LLM 未覆盖该条 → 规则兜底
            cat = r.get("_rule_category") or classify_functional_category(text)
            source = "rule_fallback"
            needs_review = True
        r["functional_category"] = cat
        r["category_source"] = source
        r["category_confidence"] = "low" if needs_review else "high"
        r["category_reason"] = str(c.get("category_reason") or "")[:200]
        r["needs_review"] = bool(needs_review)

    # 跨分类语义去重：不同分类下高度相似的需求合并（保留首个分类，合并来源）
    rows = dedup_cross_category(rows, threshold=CROSS_CATEGORY_DEDUP_THRESHOLD)

    # 按 17 功能分类重新编号并排序（CSR-HW / CSR-DBG / CSR-FLASH / CSR-OTA ...）
    from app.core.taxonomy import FUNCTIONAL_PREFIX_MAP, FUNCTIONAL_CATEGORY_NAMES
    grouped = {}
    for r in rows:
        grouped.setdefault(r.get("functional_category") or "4.2 系统加固", []).append(r)
    for cat, lst in grouped.items():
        prefix = FUNCTIONAL_PREFIX_MAP.get(cat, "CSR-GEN")
        lst.sort(key=lambda x: str(x.get("csr_id") or ""))
        for idx, r in enumerate(lst, 1):
            new_id = f"{prefix}-{idx:03d}"
            r["old_csr_id"] = r.get("csr_id")
            r["csr_id"] = new_id
            r["number"] = new_id
    order = {c: i for i, c in enumerate(FUNCTIONAL_CATEGORY_NAMES)}
    rows.sort(key=lambda r: (order.get(r.get("functional_category"), 99), str(r.get("csr_id") or "")))
    return rows


def atomize_and_dedup_asset_requirements(asset, reqs: list, req_map: dict) -> list:
    """
    Step 1 & 2: 资产内需求原子化拆解与去重。
    1. 原子化：将复合需求拆解为单一责任主体、单一控制动作的原子项，并继承原始 TARA 哈希 ID。
    2. 资产内去重：合并同资产内控制机制完全一致或高语义相似度 (>= 0.85) 的冗余项。
    """
    raw_atoms = []
    asset_name = getattr(asset, "name", str(asset))
    asset_id = getattr(asset, "id", 0)
    diagram_id = getattr(asset, "diagram_id", None)

    for r in reqs:
        raw_text = str(r.get("cybersecurity_requirement") or "").strip()
        if not raw_text or raw_text == "N/A":
            continue
        raw_id = req_map.get(raw_text) or _content_hash_id("CSR_", raw_text)
        atoms = extract_subject_and_clauses(raw_text)
        for subj, clause, full in atoms:
            raw_atoms.append({
                "asset_id": asset_id,
                "asset_name": asset_name,
                "diagram_id": diagram_id,
                "subject": subj or extract_subject(raw_text) or asset_name,
                "clause": clause,
                "full_text": full,
                "source_tara_ids": {raw_id},
                "applicable_assets": {asset_name},
                "verification_method": r.get("verification_method") or "测试/分析 / Test/Analysis",
                "requirement_type": r.get("requirement_type") or "product",
                "rationale": r.get("rationale") or "",
                "sub_title": r.get("sub_title") or r.get("sub_category") or ""
            })

    # 资产内语义聚类去重 (Intra-asset deduplication)
    clusters = []
    for atom in raw_atoms:
        toks = semantic_tokens(atom["clause"])
        matched = False
        for c in clusters:
            sim = _jaccard(toks, c["tokens"])
            if sim >= 0.85 or atom["clause"] == c["clause"]:
                c["source_tara_ids"].update(atom["source_tara_ids"])
                matched = True
                break
        if not matched:
            clusters.append({
                "asset_id": asset_id,
                "asset_name": asset_name,
                "diagram_id": diagram_id,
                "subject": atom["subject"],
                "clause": atom["clause"],
                "tokens": toks,
                "full_text": atom["full_text"],
                "source_tara_ids": set(atom["source_tara_ids"]),
                "applicable_assets": {asset_name},
                "verification_method": atom["verification_method"],
                "requirement_type": atom["requirement_type"],
                "rationale": atom["rationale"],
                "sub_title": atom["sub_title"]
            })

    return clusters


def _normalize_id_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[;,；\n]", value)
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        parts = [value]
    result = []
    for part in parts:
        part = str(part).strip()
        if part and part.upper() != "N/A" and part not in result:
            result.append(part)
    return result


def _tara_requirement_id(r: dict, req_map: dict) -> str:
    """Prefer the explicit TARA requirement ID; fall back to the content hash."""
    rid = str(r.get("cybersecurity_requirement_id") or r.get("csr_id") or "").strip()
    if rid and rid.upper() != "N/A":
        return rid
    text = str(r.get("cybersecurity_requirement") or "").strip()
    return req_map.get(text) or _content_hash_id("CSR_", text)


def _asset_context(asset) -> dict:
    return {
        "id": getattr(asset, "id", 0),
        "name": getattr(asset, "name", ""),
        "type": getattr(asset, "asset_type", ""),
        "description": getattr(asset, "description", ""),
        "protocol": getattr(asset, "protocol", ""),
        "diagram_id": getattr(asset, "diagram_id", None),
    }


def _requirement_payload(r: dict, req_map: dict) -> dict:
    return {
        "requirement_id": _tara_requirement_id(r, req_map),
        "title": r.get("title") or "",
        "subject": r.get("subject") or "",
        "text": r.get("cybersecurity_requirement") or "",
        "verification_method": r.get("verification_method") or "",
        "acceptance_criteria": r.get("acceptance_criteria") or "",
        "requirement_scope": r.get("requirement_scope") or "",
        "owner": r.get("owner") or "",
        "allocated_to": r.get("allocated_to") or "",
        "allocation_type": r.get("allocation_type") or "",
        "requirement_type": r.get("requirement_type") or "product",
        "goal_ids": _normalize_id_list(r.get("goal_ids")),
        "control_ids": _normalize_id_list(r.get("control_ids")),
        "claim_ids": _normalize_id_list(r.get("claim_ids")),
        "control_type": r.get("control_type") or "",
        "implementation_type": r.get("implementation_type") or "",
        "control_objective": r.get("control_objective") or "",
        "control_point": r.get("control_point") or "",
        "dependencies": _normalize_id_list(r.get("dependencies")),
        "source_ids": _normalize_id_list(r.get("source_ids")),
        "rationale": r.get("rationale") or "",
        "open_issue": r.get("open_issue") or "",
    }


def _atom_from_requirement(asset, r: dict, source_ids: list, subject: str, statement: str) -> dict:
    asset_name = getattr(asset, "name", "") or ""
    source_ids = _normalize_id_list(source_ids)
    return {
        "atom_id": _content_hash_id("ATOM_", f"{getattr(asset, 'id', 0)}:{','.join(source_ids)}:{statement}"),
        "asset_id": getattr(asset, "id", 0),
        "asset_name": asset_name,
        "diagram_id": getattr(asset, "diagram_id", None),
        "subject": subject or r.get("subject") or asset_name,
        "cybersecurity_requirement": statement,
        "title": r.get("title") or statement[:80],
        "sub_title": r.get("sub_title") or r.get("sub_category") or "",
        "source_requirement_ids": source_ids,
        "source_tara_ids": set(source_ids),
        "_raw_source_ids": source_ids,
        "applicable_assets": {asset_name} if asset_name else set(),
        "verification_method": r.get("verification_method") or "测试/分析 / Test/Analysis",
        "acceptance_criteria": r.get("acceptance_criteria") or "",
        "requirement_scope": r.get("requirement_scope") or "",
        "owner": r.get("owner") or "",
        "allocated_to": r.get("allocated_to") or "",
        "allocation_type": r.get("allocation_type") or "",
        "requirement_type": r.get("requirement_type") or "product",
        "goal_ids": _normalize_id_list(r.get("goal_ids")),
        "control_ids": _normalize_id_list(r.get("control_ids")),
        "claim_ids": _normalize_id_list(r.get("claim_ids")),
        "control_type": r.get("control_type") or "",
        "implementation_type": r.get("implementation_type") or "",
        "control_objective": r.get("control_objective") or "",
        "control_point": r.get("control_point") or "",
        "dependencies": _normalize_id_list(r.get("dependencies")),
        "source_ids": _normalize_id_list(r.get("source_ids")),
        "rationale": r.get("rationale") or "",
        "open_issue": r.get("open_issue") or "",
    }


_RESPONSIBLE_SUBJECT_BLOCKERS = (
    "身份", "会话", "命令", "数据", "事件", "日志", "报文", "状态",
    "条件", "结果", "信息", "文件", "资源", "异常", "上下文",
)


def _is_responsible_subject(subject: str) -> bool:
    subject = str(subject or "").strip()
    if not subject or subject == "系统":
        return False
    return not any(word in subject for word in _RESPONSIBLE_SUBJECT_BLOCKERS)


def _rewrite_atom_subject(asset, atom: dict) -> dict:
    """Make the responsibility subject component/system/module oriented.

    The LLM sometimes puts the protected object or an event into subject
    (e.g. "调试主机身份"). For ISO 21434 traceability the subject must be the
    component/system/module responsible for implementing the requirement.
    """
    subject = str(atom.get("subject") or "").strip()
    if not _is_responsible_subject(subject):
        subject = getattr(asset, "name", "") or "系统"
    atom["subject"] = subject

    statement = str(atom.get("cybersecurity_requirement") or "").strip()
    if statement:
        if statement.startswith("系统"):
            statement = subject + statement[2:]
        elif statement.startswith("The system"):
            statement = subject + statement[len("The system"):]
        elif re.match(r"^(应|不应|不得|禁止|仅|必须|需要)", statement):
            statement = subject + statement
        atom["cybersecurity_requirement"] = statement
    return atom


def _atom_payload(atom: dict) -> dict:
    return {
        "atom_id": atom.get("atom_id") or "",
        "subject": atom.get("subject") or "",
        "statement": atom.get("cybersecurity_requirement") or "",
        "source_requirement_ids": _normalize_id_list(atom.get("source_requirement_ids")),
        "requirement_scope": atom.get("requirement_scope") or "",
        "owner": atom.get("owner") or "",
        "allocated_to": atom.get("allocated_to") or "",
        "allocation_type": atom.get("allocation_type") or "",
        "verification_method": atom.get("verification_method") or "",
        "acceptance_criteria": atom.get("acceptance_criteria") or "",
        "requirement_type": atom.get("requirement_type") or "product",
        "goal_ids": _normalize_id_list(atom.get("goal_ids")),
        "control_ids": _normalize_id_list(atom.get("control_ids")),
        "claim_ids": _normalize_id_list(atom.get("claim_ids")),
    }


def _call_llm_json(settings: dict, system_prompt: str, user_obj: dict) -> dict:
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
    resp = post_llm_request_with_retry(url, headers, payload, max_retries=2)
    return clean_and_parse_json(resp.json()["choices"][0]["message"]["content"])


def _rule_atoms_for_requirement(asset, r: dict, req_map: dict) -> list:
    rid = _tara_requirement_id(r, req_map)
    text = str(r.get("cybersecurity_requirement") or "").strip()
    if not text or text == "N/A":
        return []
    parsed = extract_subject_and_clauses(text)
    if not parsed:
        parsed = [("", text, text)]
    return [
        _rewrite_atom_subject(
            asset,
            _atom_from_requirement(asset, r, [rid], subject, statement),
        )
        for subject, _clause, statement in parsed
    ]


def atomize_asset_requirements_with_llm(asset, reqs: list, req_map: dict, settings: dict = None) -> list:
    """Step 1: per-asset LLM atomization, preserving the original TARA IDs."""
    if not reqs:
        return []

    payloads = [_requirement_payload(r, req_map) for r in reqs]
    input_ids = {p["requirement_id"] for p in payloads if p.get("requirement_id")}
    atoms = []

    system_prompt = """你是 ISO/SAE 21434 网络安全需求工程师。请对一个资产的所有原始网络安全需求做“原子化拆解”。
【规则】
1. 只拆不合并：一条原始需求可以拆成多条原子需求；不要跨原始需求合并。
2. 每条原子需求的 source_requirement_ids 只包含它来源的那个原始 requirement_id；拆分后的多条原子需求都带同一个原始 ID。
3. 一条原子需求 = 一个责任主体 + 一个保护对象 + 一个行为 + 一个可验证结果。
4. subject 必须是组件、系统、模块或角色；不得使用对象、事件、数据、条件、日志、命令、会话、身份、文件、资源作为 subject。如果原文没有明确责任主体，使用 asset.name 或 asset.name + 功能模块，不得只写“系统”。statement 必须以 subject 开头。
5. 一条原子需求只能有一个行为。出现“并/且/以及/同时”连接不同动作、对象或结果时，必须拆成多条；认证、授权、审计、日志、恢复、配置、产线/运维流程必须分开。
6. statement 必须中英文对照，格式为“中文描述 / English specification”；必须保留输入中的英文含义，不得只输出中文。
7. 不要写具体协议/算法/产品/端口/阈值；保留输入中的验证方法、验收准则、scope、owner、allocated_to、goal/control/claim IDs、rationale 等字段。
7. 不得编造 requirement_id；不得新增输入中不存在的原始需求。
【正例】
subject=ADCU调试服务；statement=ADCU调试服务应在建立调试会话前验证调试主机身份。
【反例】
subject=调试主机身份；statement=系统应验证调试主机身份并拒绝未认证会话，并记录事件。
【输出 JSON】
{"atomic_requirements":[{"atom_id":"A-001","subject":"...","statement":"...","source_requirement_ids":["TARA-REQ-001"],"verification_method":"...","acceptance_criteria":"...","requirement_scope":"item","owner":"...","allocated_to":"...","allocation_type":"primary","requirement_type":"product","goal_ids":[],"control_ids":[],"claim_ids":[],"rationale":"..."}]}"""

    if settings:
        try:
            parsed = _call_llm_json(
                settings,
                system_prompt,
                {"asset": _asset_context(asset), "requirements": payloads},
            )
            for item in parsed.get("atomic_requirements", []) or parsed.get("atoms", []) or []:
                source_ids = _normalize_id_list(
                    item.get("source_requirement_ids")
                    or item.get("source_requirement_id")
                    or item.get("source_tara_ids")
                )
                source_ids = [sid for sid in source_ids if sid in input_ids]
                statement = str(item.get("statement") or item.get("cybersecurity_requirement") or "").strip()
                if not source_ids or not statement:
                    continue
                base = next(
                    (r for r in reqs if _tara_requirement_id(r, req_map) == source_ids[0]),
                    reqs[0],
                )
                atom = _atom_from_requirement(
                    asset, base, source_ids,
                    str(item.get("subject") or ""), statement,
                )
                for field in (
                    "title", "verification_method", "acceptance_criteria",
                    "requirement_scope", "owner", "allocated_to", "allocation_type",
                    "requirement_type", "control_type", "implementation_type",
                    "control_objective", "control_point", "rationale", "open_issue",
                ):
                    if item.get(field):
                        atom[field] = item[field]
                atom["goal_ids"] = _normalize_id_list(item.get("goal_ids")) or atom["goal_ids"]
                atom["control_ids"] = _normalize_id_list(item.get("control_ids")) or atom["control_ids"]
                atom["claim_ids"] = _normalize_id_list(item.get("claim_ids")) or atom["claim_ids"]
                atoms.append(_rewrite_atom_subject(asset, atom))
        except Exception as e:
            print(f"[TARA-AI] 资产 {getattr(asset, 'name', '')} LLM 原子化失败，使用规则拆解兜底: {e}")

    if not atoms:
        for r in reqs:
            atoms.extend(_rule_atoms_for_requirement(asset, r, req_map))

    # Coverage guard: every original TARA ID must survive atomization.
    covered = {
        sid for atom in atoms
        for sid in _normalize_id_list(atom.get("source_requirement_ids"))
    }
    for r in reqs:
        rid = _tara_requirement_id(r, req_map)
        if rid not in covered:
            atoms.extend(_rule_atoms_for_requirement(asset, r, req_map))
    return atoms


def _atoms_compatible(a: dict, b: dict) -> bool:
    for field in ("requirement_scope", "owner", "allocated_to", "verification_method", "acceptance_criteria"):
        av = str(a.get(field) or "").strip()
        bv = str(b.get(field) or "").strip()
        if av and bv and av != bv:
            return False
    return True


def _rule_dedup_asset_atoms(asset, atoms: list, threshold: float = 0.85) -> list:
    clusters = []
    for atom in atoms:
        toks = semantic_tokens(atom.get("cybersecurity_requirement") or "")
        matched = None
        for cluster in clusters:
            same_source = bool(
                set(_normalize_id_list(cluster["rep"].get("source_requirement_ids")))
                & set(_normalize_id_list(atom.get("source_requirement_ids")))
            )
            if (
                not same_source
                and _jaccard(toks, cluster["tokens"]) >= threshold
                and _atoms_compatible(cluster["rep"], atom)
            ):
                matched = cluster
                break
        if matched:
            matched["source_ids"].update(_normalize_id_list(atom.get("source_requirement_ids")))
            matched["assets"].update(atom.get("applicable_assets") or set())
            matched["tokens"].update(toks)
        else:
            clusters.append({
                "rep": dict(atom),
                "tokens": toks,
                "source_ids": set(_normalize_id_list(atom.get("source_requirement_ids"))),
                "assets": set(atom.get("applicable_assets") or set()),
            })
    result = []
    for cluster in clusters:
        atom = dict(cluster["rep"])
        atom["source_requirement_ids"] = sorted(cluster["source_ids"])
        atom["source_tara_ids"] = set(cluster["source_ids"])
        atom["_raw_source_ids"] = sorted(cluster["source_ids"])
        atom["applicable_assets"] = set(cluster["assets"])
        result.append(atom)
    return result


def _dedupe_exact_atoms(items: list) -> list:
    result = []
    seen = set()
    for item in items:
        source_key = tuple(sorted(_normalize_id_list(item.get("source_requirement_ids"))))
        statement_key = re.sub(r"\s+", "", str(item.get("cybersecurity_requirement") or ""))
        key = (source_key, statement_key)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result



def dedup_asset_atoms_with_llm(asset, atoms: list, settings: dict = None) -> list:
    """Step 2: per-asset LLM deduplication; source IDs are unioned."""
    if not atoms:
        return []
    input_ids = {
        sid for atom in atoms
        for sid in _normalize_id_list(atom.get("source_requirement_ids"))
    }
    if len(atoms) <= 1:
        return atoms

    system_prompt = """你是 ISO/SAE 21434 网络安全需求工程师。请只对当前资产内的原子需求做去重合并。
【规则】
1. 只合并，不新增；不得产生输入中不存在的原始 requirement_id。
2. 不同 source_requirement_id 的原子：只有同时满足同一 requirement_scope、同一 owner、同一 allocated_to、同一责任主体、同一保护对象、同一动作/控制目标、同一 verification_method、同一 acceptance_criteria、同一适用条件，才允许合并；source IDs 取并集。
3. 同一 source_requirement_id 的原子：只有“同一动作、同一保护对象、同一控制目标，只是条件、字段或枚举值不同”时才允许合并；不同动作/控制目标的不得合并。
4. 不同动作必须分开，例如：身份认证、权限授权、审计记录、拒绝操作、告警、证据保留、资源限制、会话终止、数据遮蔽、数据排除、默认禁用与临时启用，不得互相合并。
5. 不同条件/例外/对象/组件/验证方法/验收准则的，即使文字相似，也不合并。
6. 合并时 source_requirement_ids 取并集；不得丢失任何输入原始 ID。
7. 不跨资产合并。
【可合并的正例】
- 记录主体 / 记录动作 / 记录结果 → 一条“记录主体、动作和结果”
- 校验格式 / 校验边界 → 一条“校验格式和边界”
- 会话异常 / 失效 / 超时后终止 → 一条“异常、失效或超时后终止”
- 遮蔽敏感字段 / 排除敏感字段 → 一条“遮蔽或排除”
【不得合并的反例】
- 默认禁用 vs 临时启用
- 身份认证 vs 权限授权 vs 审计记录 vs 拒绝操作
- 完整性校验 vs 记录事件
- 检测未授权修改 vs 生成告警
【输出 JSON】
{"deduplicated_requirements":[{"dedup_id":"D-001","subject":"...","statement":"...","source_requirement_ids":["TARA-REQ-001","TARA-REQ-002"],"verification_method":"...","acceptance_criteria":"...","requirement_scope":"item","owner":"...","allocated_to":"...","allocation_type":"primary","requirement_type":"product","goal_ids":[],"control_ids":[],"claim_ids":[],"merge_reason":"..."}]}"""

    deduped = []
    if settings:
        try:
            parsed = _call_llm_json(
                settings,
                system_prompt,
                {"asset": _asset_context(asset), "atoms": [_atom_payload(a) for a in atoms]},
            )
            for item in parsed.get("deduplicated_requirements", []) or parsed.get("requirements", []) or []:
                source_ids = _normalize_id_list(item.get("source_requirement_ids"))
                source_ids = [sid for sid in source_ids if sid in input_ids]
                statement = str(item.get("statement") or item.get("cybersecurity_requirement") or "").strip()
                if not source_ids or not statement:
                    continue
                base = next(
                    (a for a in atoms if set(_normalize_id_list(a.get("source_requirement_ids"))) & set(source_ids)),
                    atoms[0],
                )
                merged = dict(base)
                merged["atom_id"] = item.get("dedup_id") or _content_hash_id(
                    "ATOM_", f"{getattr(asset, 'id', 0)}:{','.join(source_ids)}:{statement}"
                )
                merged["cybersecurity_requirement"] = statement
                merged["source_requirement_ids"] = sorted(source_ids)
                merged["source_tara_ids"] = set(source_ids)
                merged["_raw_source_ids"] = sorted(source_ids)
                merged["applicable_assets"] = set(base.get("applicable_assets") or {getattr(asset, "name", "")})
                for field in (
                    "subject", "verification_method", "acceptance_criteria",
                    "requirement_scope", "owner", "allocated_to", "allocation_type",
                    "requirement_type", "control_type", "implementation_type",
                    "control_objective", "control_point", "rationale", "open_issue",
                ):
                    if item.get(field):
                        merged[field] = item[field]
                merged["goal_ids"] = _normalize_id_list(item.get("goal_ids")) or merged.get("goal_ids", [])
                merged["control_ids"] = _normalize_id_list(item.get("control_ids")) or merged.get("control_ids", [])
                merged["claim_ids"] = _normalize_id_list(item.get("claim_ids")) or merged.get("claim_ids", [])
                deduped.append(_rewrite_atom_subject(asset, merged))
        except Exception as e:
            print(f"[TARA-AI] 资产 {getattr(asset, 'name', '')} LLM 去重失败，使用语义去重兜底: {e}")

    if not deduped:
        deduped = _rule_dedup_asset_atoms(asset, atoms)

    # Same-source atoms may be merged by the LLM only when they share the same
    # action/object/objective (see the dedup prompt); do not auto-split them.

    # Coverage guard: a deduplicated item must not delete an original ID.
    covered = {
        sid for item in deduped
        for sid in _normalize_id_list(item.get("source_requirement_ids"))
    }
    for sid in sorted(input_ids - covered):
        originals = [
            dict(a) for a in atoms
            if sid in _normalize_id_list(a.get("source_requirement_ids"))
        ]
        deduped.extend(originals)
    return _dedupe_exact_atoms(deduped)


def _aggregate_subsystem_batch(diagram_title: str, batch_items: list, settings: dict = None) -> list:
    """对子系统内单批（<=25项）原子需求调用大模型或语义聚类进行综合归纳提炼。"""
    if not batch_items:
        return []
    if len(batch_items) == 1:
        it = batch_items[0]
        return [{
            "title": f"{diagram_title}安全防护要求 / Security Requirement",
            "subject": it.get("subject") or f"ADCU {diagram_title}模块",
            "cybersecurity_requirement": it.get("full_text") or it.get("clause") or "",
            "verification_method": it.get("verification_method") or "测试/分析 / Test/Analysis",
            "requirement_type": it.get("requirement_type") or "product",
            "rationale": it.get("rationale") or "",
            "source_tara_ids": set(it["source_tara_ids"]),
            "applicable_assets": set(it["applicable_assets"])
        }]

    if settings and settings.get("api_key") and settings.get("api_base_url") and settings["api_key"] not in ["mock_test_key", ""]:
        try:
            items_prompt = ""
            for idx, it in enumerate(batch_items):
                assets_str = ", ".join(sorted(list(it["applicable_assets"])))
                items_prompt += f"[{idx}] (资产: {assets_str}) 主体: {it['subject']} | 要求: {it['clause']}\n"

            prompt = f"""你是一名资深汽车网络安全总架构师。当前正在对【{diagram_title}】子系统/拓扑图内的原子安全需求进行「子系统级合并归纳与去重」。

【任务要求】：
1. 跨资产归纳合并：同一子系统内针对相同控制机制、相同防御目标的需求，合并为一条规范的子系统级安全要求（如“ADCU {diagram_title}模块应……”），消除多个资产各留一条的重复现象！
2. 责任主体规范：统一使用子系统级规范主体（如“ADCU {diagram_title}子系统”或针对该场景的规范控制器主体）。
3. 来源追溯契约（核心强制）：输出的每一项必须通过 source_indexes 列出它覆盖的输入项索引列表（如 [0, 2, 3]），必须保证所有输入的索引都被完整覆盖，严禁遗漏任何索引！
4. 技术中立与双语：需求正文输出格式为“中文描述 / English specification”，保持技术中立，不写死不可变更的具体算法型号。明确验证方法（测试/分析/演示/审查）与需求类型（product/process）。

【输入原子需求列表】：
{items_prompt}

【输出 JSON 格式】：
{{
  "subsystem_csrs": [
    {{
      "source_indexes": [0, 1],
      "title": "需求简要标题 / Requirement Title",
      "subject": "责任主体 / Responsible Subject",
      "cybersecurity_requirement": "中文需求正文... / English requirement specification...",
      "verification_method": "测试/分析 / Test/Analysis...",
      "requirement_type": "product",
      "rationale": "需求依据 / Rationale..."
    }}
  ]
}}
"""
            url = f"{settings['api_base_url'].rstrip('/')}/chat/completions"
            headers = {"Authorization": f"Bearer {settings['api_key']}", "Content-Type": "application/json"}
            payload = {
                "model": settings.get("model_name", "deepseek-flash"),
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"}
            }
            resp = post_llm_request_with_retry(url, headers, payload, max_retries=2)
            parsed = clean_and_parse_json(resp.json()["choices"][0]["message"]["content"])
            csrs = parsed.get("subsystem_csrs", [])
            if csrs:
                covered_indexes = set()
                results = []
                for csr in csrs:
                    src_idxs = csr.get("source_indexes", [])
                    raw_ids = set()
                    assets_set = set()
                    for idx in src_idxs:
                        if isinstance(idx, int) and 0 <= idx < len(batch_items):
                            covered_indexes.add(idx)
                            raw_ids.update(batch_items[idx]["source_tara_ids"])
                            assets_set.update(batch_items[idx]["applicable_assets"])
                    if raw_ids:
                        results.append({
                            "title": csr.get("title") or f"{diagram_title}安全防护要求 / Security Requirement",
                            "subject": csr.get("subject") or f"ADCU {diagram_title}子系统",
                            "cybersecurity_requirement": csr.get("cybersecurity_requirement") or "",
                            "verification_method": csr.get("verification_method") or "测试/分析 / Test/Analysis",
                            "requirement_type": csr.get("requirement_type") or "product",
                            "rationale": csr.get("rationale") or "",
                            "source_tara_ids": raw_ids,
                            "applicable_assets": assets_set
                        })

                # Fallback for any uncovered index to guarantee 100% coverage
                for idx, it in enumerate(batch_items):
                    if idx not in covered_indexes:
                        results.append({
                            "title": f"{diagram_title}安全防护要求 / Security Requirement",
                            "subject": it.get("subject") or f"ADCU {diagram_title}模块",
                            "cybersecurity_requirement": it.get("full_text") or it.get("clause") or "",
                            "verification_method": it.get("verification_method") or "测试/分析 / Test/Analysis",
                            "requirement_type": it.get("requirement_type") or "product",
                            "rationale": it.get("rationale") or "",
                            "source_tara_ids": set(it["source_tara_ids"]),
                            "applicable_assets": set(it["applicable_assets"])
                        })
                return results
        except Exception as e:
            print(f"[TARA-AI] 子系统 {diagram_title} 大模型分批聚合异常 ({e})，启用语义聚类兜底...")

    # Fallback: semantic clustering within the batch
    clusters = []
    for it in batch_items:
        toks = semantic_tokens(it.get("clause", "") or it.get("full_text", ""))
        matched = False
        for c in clusters:
            if _jaccard(toks, c["tokens"]) >= 0.75:
                c["source_tara_ids"].update(it["source_tara_ids"])
                c["applicable_assets"].update(it["applicable_assets"])
                matched = True
                break
        if not matched:
            clusters.append({
                "title": f"{diagram_title}安全防护要求 / Security Requirement",
                "subject": it.get("subject") or f"ADCU {diagram_title}模块",
                "cybersecurity_requirement": it.get("full_text") or it.get("clause") or "",
                "verification_method": it.get("verification_method") or "测试/分析 / Test/Analysis",
                "requirement_type": it.get("requirement_type") or "product",
                "rationale": it.get("rationale") or "",
                "source_tara_ids": set(it["source_tara_ids"]),
                "applicable_assets": set(it["applicable_assets"]),
                "tokens": toks
            })
    return clusters


def aggregate_subsystem_requirements(diagram_title: str, items: list, settings: dict = None) -> list:
    """
    Step 3: 场景图/子系统级聚合去重。
    跨资产归纳同一子系统内具有相同控制机制和防御目标的需求，
    将责任主体收敛为子系统规范模块，严格保持 100% 来源追溯契约。
    对超过 25 项的子系统采用分批合并策略，防止大模型调用超时或输出截断。
    """
    if not items:
        return []
    if len(items) == 1:
        return [{
            "title": f"{diagram_title}安全防护要求 / Security Requirement",
            "subject": items[0]["subject"] or f"ADCU {diagram_title}模块",
            "cybersecurity_requirement": items[0]["full_text"],
            "verification_method": items[0]["verification_method"],
            "requirement_type": items[0]["requirement_type"],
            "rationale": items[0]["rationale"],
            "source_tara_ids": set(items[0]["source_tara_ids"]),
            "applicable_assets": set(items[0]["applicable_assets"])
        }]

    BATCH_SIZE = 25
    batches = [items[i:i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]
    consolidated_results = []

    for batch in batches:
        batch_res = _aggregate_subsystem_batch(diagram_title, batch, settings)
        consolidated_results.extend(batch_res)

    return consolidated_results


def _classify_batch_with_llm(batch_items: list, settings: dict = None) -> list:
    """使用大模型对一批原子 CSR 需求进行忠实评估分类。"""
    if not batch_items:
        return []

    from app.core.taxonomy import normalize_functional_category, classify_functional_category

    categories_prompt = """
4.1 硬件安全（芯片封装、PCB防拆、物理走线/测试点隐藏、不可逆硬件熔断、侧信道与故障注入防护）
4.2 系统加固（操作系统配置基线、内核加固、只读文件系统、最小化服务/端口、资源隔离、提权缓解）
4.3 访问控制（身份认证与鉴别、最小权限原则、角色授权 RBAC、会话管理、权限校验）
4.4 安全调试（调试接口 JTAG/SWD/UART 认证与锁定、调试会话超时、调试操作限制与禁用）
4.5 本地刷写安全（UDS/产线/CAN/以太网本地刷写安全、刷写包验签、FBL安全、0x34/0x36服务）
4.6 OTA 刷写安全（云端升级包下发、OTA下载验签、版本防回滚、更新回退与恢复）
4.7 安全启动（基于只读信任根的分级安全启动、引导程序度量验签、防版本回滚）
4.8 安全存储（TEE隔离安全存储、敏感数据加密存储、安全分区保护）
4.9 数据脱敏与隐私安全（个人隐私数据保护、去标识化、匿名化、用户同意与告知、数据出境合规）
4.10 安全诊断（UDS 0x27 安全访问 种子-密钥、诊断特权服务授权）
4.11 安全日志（安全事件审计记录、日志防篡改、仅追加存储、时间源与来源绑定）
4.12 密码、密钥与证书安全（密码算法 加密/签名/哈希、密钥生成/存储/轮换全生命周期、数字证书验证）
4.13 传感器安全（雷达/摄像头等感知数据保护、传感器欺骗与注入检测、传感器物理线束防护）
4.14 防火墙安全（网络报文过滤、跨域网络隔离、入侵检测与防御 IDPS、异常流量拦截）
4.15 车内通信安全（CAN/CAN-FD/LIN/车载以太网总线通信保护、SecOC报文认证、新鲜度校验与防重放）
4.16 远程通讯安全（车联网T-Box、车云TLS安全通道、蜂窝/无线远程控制安全）
4.17 安全开发与供应链安全（安全编码规范、漏洞扫描与修复、SBOM第三方开源组件治理、渗透测试）
"""

    if settings and settings.get("api_key") and settings.get("api_base_url") and settings["api_key"] not in ["mock_test_key", ""]:
        try:
            items_str = ""
            for idx, it in enumerate(batch_items):
                title = it.get("title") or it.get("requirement_title") or ""
                subj = it.get("subject") or ""
                text = it.get("cybersecurity_requirement") or it.get("requirement_text") or it.get("full_text") or it.get("requirement") or ""
                items_str += f"[{idx}] 主体: {subj} | 标题: {title} | 需求: {text}\n"

            prompt = f"""你是一名资深汽车网络安全总架构师。请评估以下已完成原子化拆解与去重后的网络安全需求，分别属于 17 个标准功能分类中的哪一个。
【核心原则】：严禁强行生硬关联！根据每一项安全防御机制的核心本质与控制目标做出客观、忠实的判定。
- 审计日志记录、防篡改存储、仅追加 -> 4.11 安全日志（不要因为包含“防篡改”就强行归到硬件安全）
- CAN/车载总线入站报文过滤、新鲜度防重放、报文完整性 -> 4.15 车内通信安全
- 个人数据导出、隐私保护、脱敏 -> 4.9 数据脱敏与隐私安全
- JTAG/SWD 调试接口认证、调试会话 -> 4.4 安全调试
- 引导程序度量、只读信任根启动验签 -> 4.7 安全启动
- 真正硬件物理防拆、走线隐藏、探针防护、不可逆熔断 -> 4.1 硬件安全

【17 个标准分类定义】：
{categories_prompt}

【待评估需求列表】：
{items_str}

【输出格式】：
严格返回 JSON 对象：
{{
  "classifications": [
    {{"index": 0, "functional_category": "4.15 车内通信安全"}},
    ...
  ]
}}
"""
            url = f"{settings['api_base_url'].rstrip('/')}/chat/completions"
            headers = {"Authorization": f"Bearer {settings['api_key']}", "Content-Type": "application/json"}
            payload = {
                "model": settings.get("model_name", "deepseek-flash"),
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"}
            }
            resp = post_llm_request_with_retry(url, headers, payload, max_retries=2)
            parsed = clean_and_parse_json(resp.json()["choices"][0]["message"]["content"])
            classifications = parsed.get("classifications", [])
            mapping = {}
            for c in classifications:
                idx = c.get("index")
                cat = c.get("functional_category")
                if isinstance(idx, int) and cat:
                    norm = normalize_functional_category(cat)
                    if norm:
                        mapping[idx] = norm

            for idx, it in enumerate(batch_items):
                if idx in mapping:
                    it["functional_category"] = mapping[idx]
                else:
                    req_val = it.get('cybersecurity_requirement') or it.get('requirement_text') or it.get('full_text') or it.get('requirement') or ''
                    it["functional_category"] = classify_functional_category(
                        f"{it.get('title', '')} {req_val} {it.get('subject', '')}"
                    )
            return batch_items
        except Exception as e:
            print(f"[TARA-AI] 大模型批量分类异常 ({e})，切换规则兜底...")

    # Fallback to rule classifier
    for it in batch_items:
        req_val = it.get('cybersecurity_requirement') or it.get('requirement_text') or it.get('full_text') or it.get('requirement') or ''
        it["functional_category"] = classify_functional_category(
            f"{it.get('title', '')} {req_val} {it.get('subject', '')}"
        )
    return batch_items


def classify_csrs_with_llm(items: list, settings: dict = None) -> list:
    """使用大模型并发批量（每批<=25项）对原子化去重后的 CSR 进行忠实分类。"""
    if not items:
        return []
    BATCH_SIZE = 25
    batches = [items[i:i + BATCH_SIZE] for i in range(0, len(items), BATCH_SIZE)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(_classify_batch_with_llm, batch, settings) for batch in batches]
        for f in as_completed(futures):
            f.result()
    return items


def _cluster_fallback_for_category(cat_name: str, items: list) -> list:
    """无大模型或网络异常时，使用确定性语义聚类将一个分类下的需求收敛至 2~4 条。"""
    if len(items) <= 2:
        return items

    clusters = []
    for it in items:
        toks = semantic_tokens(it.get("cybersecurity_requirement", "") or it.get("clause", ""))
        matched = False
        for c in clusters:
            if _jaccard(toks, c["tokens"]) >= 0.50:
                c["source_tara_ids"].update(it.get("source_tara_ids") or it.get("_raw_source_ids") or [])
                applicable = it.get("applicable_assets", [])
                if isinstance(applicable, str):
                    c["applicable_assets"].update([a.strip() for a in applicable.split("\n") if a.strip()])
                elif isinstance(applicable, (set, list)):
                    c["applicable_assets"].update(applicable)
                matched = True
                break
        if not matched:
            it_copy = dict(it)
            it_copy["tokens"] = toks
            it_copy["source_tara_ids"] = set(it.get("source_tara_ids") or it.get("_raw_source_ids") or [])
            applicable = it.get("applicable_assets", [])
            if isinstance(applicable, str):
                it_copy["applicable_assets"] = set([a.strip() for a in applicable.split("\n") if a.strip()])
            elif isinstance(applicable, (set, list)):
                it_copy["applicable_assets"] = set(applicable)
            else:
                it_copy["applicable_assets"] = set()
            clusters.append(it_copy)

    # 贪心收敛至 <= 4 条
    while len(clusters) > 4:
        best_pair = (0, 1)
        best_sim = -1.0
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                sim = _jaccard(clusters[i]["tokens"], clusters[j]["tokens"])
                if sim > best_sim:
                    best_sim = sim
                    best_pair = (i, j)
        i, j = best_pair
        clusters[i]["source_tara_ids"].update(clusters[j]["source_tara_ids"])
        clusters[i]["applicable_assets"].update(clusters[j]["applicable_assets"])
        clusters[i]["tokens"].update(clusters[j]["tokens"])
        clusters.pop(j)

    for c in clusters:
        c["functional_category"] = cat_name

    return clusters


def _synthesize_single_category(cat_name: str, items: list, settings: dict = None) -> list:
    """对单个功能分类内的微观/子系统需求执行项目级（系统级）最终综合提炼，目标收敛为 2~4 条。"""
    if not items:
        return []
    if len(items) <= 2:
        if len(items) == 2:
            t1 = semantic_tokens(items[0].get("cybersecurity_requirement", ""))
            t2 = semantic_tokens(items[1].get("cybersecurity_requirement", ""))
            if _jaccard(t1, t2) >= 0.75:
                items[0]["source_tara_ids"].update(items[1].get("source_tara_ids") or items[1].get("_raw_source_ids") or [])
                app1 = items[0].get("applicable_assets", [])
                app2 = items[1].get("applicable_assets", [])
                set1 = set([a.strip() for a in app1.split("\n") if a.strip()]) if isinstance(app1, str) else set(app1)
                set2 = set([a.strip() for a in app2.split("\n") if a.strip()]) if isinstance(app2, str) else set(app2)
                items[0]["applicable_assets"] = set1.union(set2)
                return [items[0]]
        return items

    target_count = "2~3" if len(items) <= 15 else "3~4"

    if settings and settings.get("api_key") and settings.get("api_base_url") and settings["api_key"] not in ["mock_test_key", ""]:
        try:
            items_prompt = ""
            for idx, it in enumerate(items):
                applicable = it.get("applicable_assets", [])
                assets_list = [a.strip() for a in applicable.split("\n") if a.strip()] if isinstance(applicable, str) else list(applicable)
                assets_str = ", ".join(assets_list[:3])
                req = it.get("cybersecurity_requirement") or it.get("clause") or ""
                items_prompt += f"[{idx}] (资产: {assets_str}) 主体: {it.get('subject')} | 标题: {it.get('title')} | 需求: {req[:90]}\n"

            prompt = f"""你是一名资深汽车网络安全总架构师。当前正在对【{cat_name}】分类下的 {len(items)} 项安全需求进行「项目级（系统级）最终综合归纳提炼与合并去重」。

【任务目标】：
消除由于按资产或子系统划分导致的碎片化冗余，将输入的 {len(items)} 项具体场景/接口的微观需求，提炼收敛为 {target_count} 条高内聚、权威规范的【项目级网络安全需求（CSR）】。

【核心规则】：
1. 收敛目标：严格输出 {target_count} 条规范需求，全面覆盖该分类下各个关键防御维度（如机制生成、安全存储、可信验证、监控告警等）。
2. 100% 来源覆盖契约（绝对强制）：输出的每一项 CSR 必须包含 source_indexes 字段，列出其所涵盖的输入项索引列表（例如 [0, 1, 2, 5, 8]）。所有输入的索引（0 到 {len(items)-1}）必须被完整覆盖，严禁遗漏任何索引！
3. 规范主体与表述：责任主体统一使用系统级规范主体（如“ADCU 系统”、“ADCU 通信子系统”、“ADCU 访问控制服务”等）。正文采用中英文双语对照（中文描述 / English specification）。保持技术中立，不写死固定加密算法型号。
4. 明确验证方法（测试/分析/演示/审查）与需求类型（product/process）。

【待合并需求列表】：
{items_prompt}

【输出严格 JSON 格式】：
{{
  "category_csrs": [
    {{
      "source_indexes": [0, 1, 2],
      "title": "中文标题 / English Title",
      "subject": "责任主体 / Responsible Subject",
      "cybersecurity_requirement": "中文需求正文... / English requirement specification...",
      "verification_method": "测试/分析 / Test/Analysis",
      "requirement_type": "product",
      "rationale": "需求依据 / Rationale"
    }}
  ]
}}
"""
            url = f"{settings['api_base_url'].rstrip('/')}/chat/completions"
            headers = {"Authorization": f"Bearer {settings['api_key']}", "Content-Type": "application/json"}
            payload = {
                "model": settings.get("model_name", "deepseek-flash"),
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"}
            }
            resp = post_llm_request_with_retry(url, headers, payload, max_retries=2)
            parsed = clean_and_parse_json(resp.json()["choices"][0]["message"]["content"])
            cat_csrs = parsed.get("category_csrs", [])
            if cat_csrs:
                covered = set()
                results = []
                for c in cat_csrs:
                    src_idxs = c.get("source_indexes", [])
                    raw_ids = set()
                    assets_set = set()
                    for idx in src_idxs:
                        if isinstance(idx, int) and 0 <= idx < len(items):
                            covered.add(idx)
                            raw_ids.update(items[idx].get("source_tara_ids") or items[idx].get("_raw_source_ids") or [])
                            applicable = items[idx].get("applicable_assets", [])
                            if isinstance(applicable, str):
                                assets_set.update([a.strip() for a in applicable.split("\n") if a.strip()])
                            elif isinstance(applicable, (set, list)):
                                assets_set.update(applicable)
                    if raw_ids:
                        results.append({
                            "functional_category": cat_name,
                            "title": c.get("title") or f"{cat_name}安全要求",
                            "subject": c.get("subject") or "ADCU 系统",
                            "cybersecurity_requirement": c.get("cybersecurity_requirement") or "",
                            "verification_method": c.get("verification_method") or "测试/分析 / Test/Analysis",
                            "requirement_type": c.get("requirement_type") or "product",
                            "rationale": c.get("rationale") or "",
                            "source_tara_ids": raw_ids,
                            "applicable_assets": assets_set
                        })

                # 兜底：若 LLM 遗漏了部分 index，将其合并到第一项以保证 100% 来源追溯
                if results:
                    for idx in range(len(items)):
                        if idx not in covered:
                            missing_item = items[idx]
                            m_raw = missing_item.get("source_tara_ids") or missing_item.get("_raw_source_ids") or []
                            m_assets = missing_item.get("applicable_assets", [])
                            results[0]["source_tara_ids"].update(m_raw)
                            if isinstance(m_assets, str):
                                results[0]["applicable_assets"].update([a.strip() for a in m_assets.split("\n") if a.strip()])
                            elif isinstance(m_assets, (set, list)):
                                results[0]["applicable_assets"].update(m_assets)
                    return results
        except Exception as e:
            print(f"[TARA-AI] 分类 [{cat_name}] 大模型项目级提炼异常 ({e})，启用聚类兜底...")

    return _cluster_fallback_for_category(cat_name, items)


def aggregate_global_requirements(subsystem_items: list, settings: dict = None) -> list:
    """
    Step 4: 全资产通用与分类项目级归纳提炼。
    将跨子系统的同类安全控制按 17 个标准功能分类进行系统级全局归纳，
    消除按资产和子系统分段导致的碎片化冗余，将需求总数收敛至 50 条左右的高质量项目级 CSR，
    同时严格继承并合并所有原始 TARA 需求 ID 与适用资产，确保 100% 来源双向追溯。
    """
    if not subsystem_items:
        return []
    if len(subsystem_items) <= 1:
        return subsystem_items

    from app.core.taxonomy import classify_functional_category

    # 1. 确保所有条目具备客观的 17 功能分类
    for it in subsystem_items:
        if not it.get("functional_category"):
            req_val = it.get('cybersecurity_requirement') or it.get('clause') or it.get('title') or ''
            it["functional_category"] = classify_functional_category(f"{it.get('title', '')} {req_val} {it.get('subject', '')}")

    # 如果具备大模型配置，批量快速优化一次分类
    if settings and settings.get("api_key") and settings["api_key"] not in ["mock_test_key", ""]:
        try:
            classify_csrs_with_llm(subsystem_items, settings=settings)
        except Exception as e:
            print(f"[TARA-AI] 全局归纳前大模型分类批量微调异常 ({e})，保留预分类...")

    # 2. 按 17 功能分类分组
    by_cat = defaultdict(list)
    for it in subsystem_items:
        cat = it.get("functional_category") or "4.2 系统加固"
        by_cat[cat].append(it)

    # 3. 对各个分类并发执行项目级综合提炼（收敛至 2~4 条核心 CSR）
    consolidated_results = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        future_to_cat = {
            executor.submit(_synthesize_single_category, cat, items, settings): cat
            for cat, items in by_cat.items()
        }
        for fut in as_completed(future_to_cat):
            cat = future_to_cat[fut]
            try:
                res = fut.result()
                consolidated_results.extend(res)
            except Exception as e:
                print(f"[TARA-AI] 分类 [{cat}] 全局提炼异常 ({e})，启用聚类兜底...")
                fallback_res = _cluster_fallback_for_category(cat, by_cat[cat])
                consolidated_results.extend(fallback_res)

    return consolidated_results


def classify_and_finalize_csrs(global_items: list, all_input_raw_ids: Set[str] = None, settings: dict = None) -> list:
    """
    Step 5: 17 分类、规范编号与双向追溯构建。
    1. 由大模型对原子化去重后的最终需求进行语义分类（结合 17 类定义与上下文，忠实评估，拒绝强行联系）。
    2. 按标准顺序排序并分配唯一规范编号 (CSR-HW-001, CSR-SENSOR-001...)。
    3. 校验 100% 原始需求双向追溯覆盖。
    """
    from app.core.taxonomy import (
        FUNCTIONAL_CATEGORY_NAMES,
        FUNCTIONAL_PREFIX_MAP,
        clean_major_category
    )

    # 1. 大模型批量忠实分类 (多线程分批，每批 <= 25 项)
    classify_csrs_with_llm(global_items, settings=settings)

    for it in global_items:
        cat = it.get("functional_category") or "4.2 系统加固"
        it["security_domain"] = clean_major_category(cat)
        it["sub_category"] = it.get("title") or "通用安全防护 / General Security Protection"

    order = {c: i for i, c in enumerate(FUNCTIONAL_CATEGORY_NAMES)}
    global_items.sort(key=lambda r: (order.get(r.get("functional_category"), 99), str(r.get("title") or "")))

    pfx_counts = defaultdict(int)
    final_rows = []

    for r in global_items:
        cat = r["functional_category"]
        pfx = FUNCTIONAL_PREFIX_MAP.get(cat, "CSR-GEN")
        pfx_counts[pfx] += 1
        dyn_id = f"{pfx}-{pfx_counts[pfx]:03d}"

        sorted_raw_ids = sorted(list(r["source_tara_ids"]))
        source_ids_str = "; ".join(sorted_raw_ids)
        sorted_assets = sorted(list(r["applicable_assets"]))
        applicable_assets_str = "\n".join(sorted_assets)

        final_rows.append({
            "number": dyn_id,
            "csr_id": dyn_id,
            "functional_category": cat,
            "security_domain": r["security_domain"],
            "control_category": r.get("sub_category") or "通用安全防护 / General Security Protection",
            "sub_category": r.get("sub_category") or "通用安全防护 / General Security Protection",
            "requirement_title": r.get("title") or "网络安全要求 / Cybersecurity Requirement",
            "title": r.get("title") or "网络安全要求 / Cybersecurity Requirement",
            "subject": r.get("subject") or extract_subject(r.get("cybersecurity_requirement", "")),
            "cybersecurity_requirement": r.get("cybersecurity_requirement") or r.get("full_text") or "",
            "verification_method": r.get("verification_method") or "测试/分析 / Test/Analysis",
            "requirement_type": r.get("requirement_type") or "product",
            "rationale": r.get("rationale") or "",
            "requirement_scope": r.get("requirement_scope") or "",
            "owner": r.get("owner") or "",
            "allocated_to": r.get("allocated_to") or "",
            "allocation_type": r.get("allocation_type") or "",
            "acceptance_criteria": r.get("acceptance_criteria") or "",
            "goal_ids": _normalize_id_list(r.get("goal_ids")),
            "control_ids": _normalize_id_list(r.get("control_ids")),
            "claim_ids": _normalize_id_list(r.get("claim_ids")),
            "control_type": r.get("control_type") or "",
            "implementation_type": r.get("implementation_type") or "",
            "control_objective": r.get("control_objective") or "",
            "control_point": r.get("control_point") or "",
            "source_ids": _normalize_id_list(r.get("source_ids")),
            "dependencies": _normalize_id_list(r.get("dependencies")),
            "open_issue": r.get("open_issue") or "",
            "applicable_assets": applicable_assets_str,
            "assets_count": len(sorted_assets),
            "source_cybersecurity_requirement_ids": source_ids_str,
            "source_tara_requirements": source_ids_str,
            "source_count": len(sorted_raw_ids),
            "source_requirement_ids": sorted_raw_ids,
            "_raw_source_ids": sorted_raw_ids,
            "_tier2_source_ids": sorted_raw_ids,
            "needs_review": "No"
        })

    if all_input_raw_ids:
        observed_ids = set().union(*[set(r["_raw_source_ids"]) for r in final_rows])
        missing = all_input_raw_ids - observed_ids
        if missing:
            raise ValueError(f"100% 追溯断裂: 有 {len(missing)} 项原始 TARA 需求未被收录: {list(missing)[:10]}")

    return final_rows


def hierarchical_synthesize_project_csrs(db: Session, domain: Domain, steps: list, assets: list) -> list:
    """Per-asset dedup -> hierarchical cross-asset dedup -> atomization -> atomic dedup.

    The whole-requirement hierarchy follows the requested structure:
      1. each asset deduplicates its own TARA requirements (LLM judged);
      2. groups are merged pairwise to 16 -> 8 -> 4, then K1 absorbs
         K2/K3/K4 in order, with an LLM duplicate decision per merge;
      3. the final group is atomized, preserving every original TARA ID;
      4. atoms are deduplicated again and traced back to their source IDs.
    """
    settings_dict = None
    if db:
        try:
            s = db.query(SystemSettings).first()
            if s and s.api_key:
                settings_dict = {
                    "api_base_url": s.api_base_url,
                    "model_name": s.model_name or "deepseek-flash",
                    "api_key": s.api_key,
                }
        except Exception as e:
            print(f"[TARA-AI] 读取系统配置异常: {e}")

    steps_by_asset = {}
    for step in steps:
        if step.asset_id not in steps_by_asset:
            steps_by_asset[step.asset_id] = {}
        steps_by_asset[step.asset_id][step.stage] = step

    _, _, _, req_map = build_id_maps(steps)
    all_raw_ids = set()
    assets_data = []

    for asset in assets:
        if getattr(asset, "status", "confirmed") == "rejected":
            continue
        asset_steps = steps_by_asset.get(asset.id, {})
        step5 = asset_steps.get("stage5")
        if not step5 or not step5.analysis_result:
            continue
        fo = step5.analysis_result.get("final_output", {})
        reqs = fo.get("requirements", [])
        if not reqs:
            reqs = fo.get("summarized_csrs") or []
        if not reqs:
            continue

        for r in reqs:
            rid = _tara_requirement_id(r, req_map)
            ids = _normalize_id_list(
                r.get("source_requirement_ids") or r.get("source_tara_ids")
            )
            if rid and rid not in ids:
                ids.append(rid)
            if not ids and rid:
                ids = [rid]
            all_raw_ids.update(ids)
        assets_data.append({"asset": asset, "requirements": reqs})

    from app.services.csr_hierarchical_dedup import run_pipeline as run_hierarchical_dedup

    final_items, coverage = run_hierarchical_dedup(assets_data, settings_dict, req_map)
    final_rows = classify_and_finalize_csrs(
        final_items,
        all_input_raw_ids=all_raw_ids,
        settings=settings_dict,
    )
    print(
        f"[TARA-AI] 层级去重/原子化完成：{coverage['input_count']} 项原始 TARA 需求 -> "
        f"{len(final_rows)} 项最终 CSR，覆盖 {coverage['covered_count']} 项"
    )
    return final_rows

def synthesize_project_csrs(db: Session, all_csrs: list, global_tier2_to_tier1: dict = None) -> list:
    """兼容旧接口签名的全局综合提炼入口。"""
    raw_atoms = []
    all_raw_ids = set()
    for it in all_csrs:
        asset = it.get("asset")
        csr = it.get("csr") or {}
        raw_text = str(csr.get("cybersecurity_requirement") or "").strip()
        if not raw_text or raw_text == "N/A":
            continue
        raw_id = csr.get("csr_id") or csr.get("cybersecurity_requirement_id") or _content_hash_id("CSR_", raw_text)
        if global_tier2_to_tier1 and raw_id in global_tier2_to_tier1:
            raw_ids = set(global_tier2_to_tier1[raw_id])
        else:
            raw_ids = {raw_id}
        all_raw_ids.update(raw_ids)
        atoms = extract_subject_and_clauses(raw_text)
        for subj, clause, full in atoms:
            raw_atoms.append({
                "subject": subj or extract_subject(raw_text) or getattr(asset, "name", "ADCU"),
                "clause": clause,
                "full_text": full,
                "source_tara_ids": set(raw_ids),
                "applicable_assets": {getattr(asset, "name", "ADCU")},
                "verification_method": csr.get("verification_method") or "测试/分析 / Test/Analysis",
                "requirement_type": csr.get("requirement_type") or "product",
                "rationale": csr.get("rationale") or "",
                "tokens": semantic_tokens(clause)
            })

    # Deduplicate and finalize
    clusters = []
    for atom in raw_atoms:
        matched = False
        for c in clusters:
            if _jaccard(atom["tokens"], c["tokens"]) >= 0.85:
                c["source_tara_ids"].update(atom["source_tara_ids"])
                c["applicable_assets"].update(atom["applicable_assets"])
                matched = True
                break
        if not matched:
            clusters.append(atom)

    settings_dict = None
    if db:
        try:
            s = db.query(SystemSettings).first()
            if s and s.api_key:
                settings_dict = {
                    "api_base_url": s.api_base_url,
                    "model_name": s.model_name or "deepseek-flash",
                    "api_key": s.api_key
                }
        except Exception:
            pass

    final_rows = classify_and_finalize_csrs(clusters, all_input_raw_ids=all_raw_ids, settings=settings_dict)
    return final_rows


def get_or_synthesize_global_csrs(
    db: Session,
    domain: Domain,
    steps: list,
    assets: list,
    hash_to_global_map: dict = None,
    force_recompute: bool = False
) -> list:
    """
    获取或执行项目级全局网络安全需求归纳提炼。
    支持 force_recompute 强制刷新缓存。若命中已有的 TaraStep(stage='global_csr') 且非 force 则直接返回。
    """
    # 1. 尝试从已有 steps 中读取 global_csr 缓存 (除非 force_recompute 或历史旧缓存条目过多 > 65)
    if not force_recompute:
        for s in steps:
            if s.stage == "global_csr" and s.analysis_result:
                fo = s.analysis_result.get("final_output", {})
                cached_rows = fo.get("consolidated_csrs", [])
                if cached_rows and len(cached_rows) <= 65:
                    if hash_to_global_map is not None:
                        for r in cached_rows:
                            cid = r.get("csr_id")
                            for tid in r.get("_raw_source_ids", []):
                                if tid not in hash_to_global_map:
                                    hash_to_global_map[tid] = set()
                                elif isinstance(hash_to_global_map[tid], str):
                                    hash_to_global_map[tid] = {hash_to_global_map[tid]}
                                hash_to_global_map[tid].add(cid)
                    return cached_rows

    # 2. 运行五步分层架构提炼
    rows = hierarchical_synthesize_project_csrs(db, domain, steps, assets)

    # 3. 填充 hash_to_global_map 双向追溯映射
    if hash_to_global_map is not None:
        for r in rows:
            cid = r.get("csr_id")
            for tid in r.get("_raw_source_ids", []):
                if tid not in hash_to_global_map:
                    hash_to_global_map[tid] = set()
                elif isinstance(hash_to_global_map[tid], str):
                    hash_to_global_map[tid] = {hash_to_global_map[tid]}
                hash_to_global_map[tid].add(cid)

    # 4. 持久化到数据库 TaraStep 作为缓存
    try:
        last_step = steps[0] if steps else None
        if last_step and assets and db:
            run_id = last_step.run_id
            first_asset_id = assets[0].id

            existing = db.query(TaraStep).filter(
                TaraStep.run_id == run_id,
                TaraStep.stage == "global_csr"
            ).first()

            if existing:
                existing.analysis_result = {"final_output": {"consolidated_csrs": rows}}
            else:
                new_step = TaraStep(
                    run_id=run_id,
                    asset_id=first_asset_id,
                    stage="global_csr",
                    status="completed",
                    input_hash="global_csr_cache",
                    analysis_result={"final_output": {"consolidated_csrs": rows}}
                )
                db.add(new_step)
            db.commit()
            print(f"[TARA-AI] 成功将 {len(rows)} 项全局项目级 CSR 缓存持久化到数据库 (Run ID: {run_id})")
    except Exception as e:
        print(f"[TARA-AI] 持久化全局 CSR 缓存异常: {e}")
        if db:
            db.rollback()

    return rows
