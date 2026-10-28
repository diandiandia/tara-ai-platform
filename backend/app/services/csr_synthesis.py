import os
import re
import json
import time
import hashlib
from collections import Counter
from typing import List, Dict, Tuple, Set
from concurrent.futures import ThreadPoolExecutor, as_completed
from sqlalchemy.orm import Session
import httpx

from app.models.tara_step import TaraStep
from app.models.asset import Asset
from app.models.domain import Domain
from app.models.system_settings import SystemSettings

# -------------------------------------------------------------------------
# ISO/SAE 21434 & UNECE R155 Standard Domain Taxonomy
# -------------------------------------------------------------------------
from app.core.taxonomy import (
    DOMAIN_PREFIX_MAP,
    MAJOR_ORDER,
    CANONICAL_DOMAINS,
    clean_major_category,
    correct_security_domain,
)


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
        rid = str(it.get("raw_id") or "").strip()
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
        item = next((x for x in items if str(x.get("raw_id")) == rid), None)
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

        print(
            f"[TARA-AI] 【{domain_name}】提炼成功: {len(items)} 项原始需求 -> {len(cons)} 项 CSR；"
            f"覆盖完整={report.get('complete')}"
        )
        return cons, report
    except Exception as e:
        print(f"[TARA-AI] 警告: 【{domain_name}】大模型提炼失败 ({e})，平滑切换至通用语义算法兜底...")
        cons = semantic_fallback_clustering(domain_name, items)
        cons, report = enforce_csr_coverage(items, cons)
        report["mode"] = "semantic_fallback_after_error"
        return cons, report


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


def synthesize_project_csrs(db: Session, all_csrs: list, global_tier2_to_tier1: dict = None) -> list:
    """
    对跨资产收集到的所有底层安全需求按标准安全分类域进行并发大模型全局提炼与编号，
    并将 Tier 2 资产级汇总 CSR ID 自动穿透解析为 Tier 1 TARA 原始需求 ID (CSR_XXXXXXXX)。
    """
    items_by_domain = {d: [] for d in MAJOR_ORDER.keys()}
    
    for item in all_csrs:
        asset = item["asset"]
        csr = item["csr"]
        raw_text = str(csr.get("cybersecurity_requirement") or "").strip()
        if not raw_text or raw_text == "N/A":
            continue
            
        raw_dom = str(csr.get("security_domain") or "").strip()
        major_cat = clean_major_category(raw_dom)
        # 按控制机制纠正（例如调试口身份认证应归系统安全，而不是硬件安全）
        major_cat = correct_security_domain(major_cat, raw_text)
        if major_cat not in items_by_domain:
            major_cat = "通用安全与其它 / General & Other Security"
            
        raw_id = csr.get("csr_id") or csr.get("cybersecurity_requirement_id") or f"CSR_{len(items_by_domain[major_cat])}"
        sn = get_asset_sn(asset)
        asset_display = f"{sn} ({asset.name})" if asset.name else sn
        
        items_by_domain[major_cat].append({
            "raw_id": raw_id,
            "asset_sn": sn,
            "asset_name": asset.name or sn,
            "asset_display": asset_display,
            "domain": major_cat,
            "sub_title": str(csr.get("sub_title") or csr.get("sub_category") or "通用防护 / General Protection").strip(),
            "title": str(csr.get("title") or "安全要求 / Security Requirement").strip(),
            "req": raw_text
        })

    # 多线程并发对各安全域执行大模型综合归纳提炼
    domain_results = {}
    active_domains = [d for d, lst in items_by_domain.items() if lst]
    
    with ThreadPoolExecutor(max_workers=4) as executor:
        future_to_domain = {
            executor.submit(synthesize_domain_csrs_with_llm, db, d, items_by_domain[d]): d
            for d in active_domains
        }
        for future in as_completed(future_to_domain):
            d = future_to_domain[future]
            try:
                res = future.result()
                if isinstance(res, tuple):
                    domain_results[d] = res
                else:
                    # Backward-compatible path for callers/mocks returning a list.
                    domain_results[d] = (res, {"mode": "legacy_list", "complete": True})
            except Exception as e:
                print(f"[TARA-AI] 线程执行异常 ({d}): {e}")
                cons = semantic_fallback_clustering(d, items_by_domain[d])
                cons, report = enforce_csr_coverage(items_by_domain[d], cons)
                report["mode"] = "semantic_fallback_after_exception"
                domain_results[d] = (cons, report)

    # 按照标准分类域排序，生成全局顺序编号与扁平化行数据
    sorted_domains = sorted(active_domains, key=lambda d: MAJOR_ORDER.get(d, 99))
    
    final_rows = []
    for d in sorted_domains:
        domain_result = domain_results.get(d, ([], {}))
        if isinstance(domain_result, tuple):
            cons_list, coverage_report = domain_result
        else:
            cons_list, coverage_report = domain_result, {"mode": "legacy_list", "complete": True}
        if coverage_report.get("all_covered") is False:
            raise ValueError(
                f"CSR coverage incomplete for domain {d}: "
                f"missing={coverage_report.get('missing_raw_ids')}"
            )
        pfx = DOMAIN_PREFIX_MAP.get(d, "CSR-GEN")
        
        for idx, c in enumerate(cons_list, 1):
            dyn_csr_id = f"{pfx}-{idx:03d}"
            
            # 整理适用资产信息
            applicable_assets = c.get("applicable_assets") or []
            if isinstance(applicable_assets, list):
                applicable_assets_str = "\n".join(str(a) for a in applicable_assets if a)
                asset_name_str = ", ".join(str(a) for a in applicable_assets if a)
            else:
                applicable_assets_str = str(applicable_assets)
                asset_name_str = str(applicable_assets)
                
            source_ids = c.get("source_requirement_ids") or []
            if not isinstance(source_ids, list):
                source_ids = [s.strip() for s in str(source_ids).split(",") if s.strip()]

            # 将 Tier 2 的资产级汇总 CSR ID 穿透解析为 Tier 1 的 TARA 原始需求 ID (CSR_XXXXXXXX)
            resolved_tier1_ids = set()
            unresolved_ids = []
            if global_tier2_to_tier1:
                for sid in source_ids:
                    t1_list = global_tier2_to_tier1.get(sid)
                    if t1_list:
                        resolved_tier1_ids.update(t1_list)
                    else:
                        # 解析不到 Tier-1 时不再把 Tier-2 ID 原样塞进来源列表，
                        # 避免 Tier-2 / 幻觉 ID 污染追溯来源，破坏与 TARA 表的对账。
                        unresolved_ids.append(sid)
            else:
                resolved_tier1_ids.update(source_ids)

            if unresolved_ids:
                print(f"[TARA-AI] 追溯警告: {dyn_csr_id} 有 {len(unresolved_ids)} 个来源 ID 无法解析到 Tier-1，已剔除: {', '.join(unresolved_ids[:8])}")
                
            sorted_tier1_ids = sorted(list(resolved_tier1_ids))
            source_ids_str = "; ".join(sorted_tier1_ids)
            source_cnt = len(sorted_tier1_ids)
                
            final_rows.append({
                "number": dyn_csr_id,
                "csr_id": dyn_csr_id,
                "security_domain": d,
                "sub_category": c.get("sub_category") or "通用安全防护 / General Security Protection",
                "asset_sn": "", # 由上层或映射补充
                "asset_name": asset_name_str,
                "applicable_assets": applicable_assets_str,
                "assets_count": len(applicable_assets) if isinstance(applicable_assets, list) else 1,
                "title": c.get("title") or "网络安全要求 / Cybersecurity Requirement",
                "subject": c.get("subject") or extract_subject(c.get("cybersecurity_requirement") or ""),
                "cybersecurity_requirement": c.get("cybersecurity_requirement") or "",
                "verification_method": c.get("verification_method") or "",
                "requirement_type": c.get("requirement_type") or "product",
                "rationale": c.get("rationale") or "",
                # One column for the original TARA requirements. Multiple IDs are
                # joined with "; " so both merge (many -> one) and split
                # (one -> many) are representable without a separate link table.
                "source_requirement_ids": sorted_tier1_ids,
                "source_cybersecurity_requirement_ids": source_ids_str,
                "source_tara_requirements": source_ids_str,
                "source_count": source_cnt,
                "_raw_source_ids": sorted_tier1_ids,
                "_tier2_source_ids": source_ids,
                "_coverage_report": coverage_report,
                "_coverage_status": c.get("_coverage_status") or "merged",
                "_degraded": bool(c.get("_degraded")),
                "_degraded_reason": c.get("_degraded_reason") or "",
            })

    # Final hard check: every input raw_id must appear in at least one CSR's
    # source_requirement_ids. This is the one-column traceability contract.
    expected_tier2 = {
        str(item.get("raw_id"))
        for items in items_by_domain.values()
        for item in items
        if str(item.get("raw_id") or "").strip()
    }
    observed_tier2 = {
        str(sid)
        for row in final_rows
        for sid in (row.get("_tier2_source_ids") or [])
        if str(sid).strip()
    }
    missing_tier2 = sorted(expected_tier2 - observed_tier2)
    if missing_tier2:
        raise ValueError(
            "CSR source traceability incomplete: "
            f"missing={missing_tier2[:20]}"
        )

    # 合并去重 → 原子化 → 分类：对最终原子化 CSR 统一做 17 类功能分类
    classify_csr_functional_categories(db, final_rows)

    return final_rows

def get_or_synthesize_global_csrs(db: Session, domain: Domain, steps: list, assets: list, hash_to_global_map: dict = None) -> list:
    """
    获取或执行项目级全局网络安全需求归纳提炼。
    优先命中 TaraStep(stage='global_csr') 缓存；若未命中则执行基于大模型+UNECE R155的综合归纳并写入缓存。
    """
    steps_by_asset = {}
    for step in steps:
        if step.asset_id not in steps_by_asset:
            steps_by_asset[step.asset_id] = {}
        steps_by_asset[step.asset_id][step.stage] = step

    # 构建全局 Tier 2 到 Tier 1 (CSR_XXXXXXXX) 的穿透追溯字典
    goal_map, claim_map, control_map, req_map = build_id_maps(steps)
    global_tier2_to_tier1 = {}
    for asset in assets:
        asset_steps = steps_by_asset.get(asset.id, {})
        if not asset_steps:
            continue
        stage5_out = asset_steps.get("stage5").analysis_result.get("final_output", {}) if asset_steps.get("stage5") else {}
        _, csr_to_tara = get_csr_traceability_maps(stage5_out, req_map)
        for k, v in csr_to_tara.items():
            global_tier2_to_tier1.setdefault(k, set()).update(v)

    # 1. 尝试从已有 steps 中读取 global_csr 缓存
    for s in steps:
        if s.stage == "global_csr" and s.analysis_result:
            fo = s.analysis_result.get("final_output", {})
            cached_rows = fo.get("consolidated_csrs", [])
            if cached_rows:
                # 动态保证 cached_rows 的 source_tara_requirements 解析为 Tier 1 原始 ID
                for r in cached_rows:
                    cid = r.get("csr_id")
                    current_sources = r.get("_raw_source_ids") or []
                    tier2_sources = r.get("_tier2_source_ids") or current_sources
                    resolved_t1 = set()
                    for sid in tier2_sources:
                        t1_list = global_tier2_to_tier1.get(sid)
                        if t1_list:
                            resolved_t1.update(t1_list)
                        # 解析不到 Tier-1 时不再保留 Tier-2 ID，避免污染来源列表
                    if resolved_t1:
                        sorted_t1 = sorted(list(resolved_t1))
                        r["source_tara_requirements"] = ", ".join(sorted_t1)
                        r["source_count"] = len(sorted_t1)
                        r["_raw_source_ids"] = sorted_t1
                        r["_tier2_source_ids"] = tier2_sources

                    # 责任主体兜底：老缓存没有 subject 时，从需求正文的【主体】段提取
                    if not r.get("subject"):
                        r["subject"] = extract_subject(r.get("cybersecurity_requirement") or "")
                        
                    if hash_to_global_map is not None:
                        for tid in r.get("_raw_source_ids", []):
                            if tid not in hash_to_global_map:
                                hash_to_global_map[tid] = set()
                            elif isinstance(hash_to_global_map[tid], str):
                                hash_to_global_map[tid] = {hash_to_global_map[tid]}
                            hash_to_global_map[tid].add(cid)
                        for sid in tier2_sources:
                            if sid not in hash_to_global_map:
                                hash_to_global_map[sid] = set()
                            elif isinstance(hash_to_global_map[sid], str):
                                hash_to_global_map[sid] = {hash_to_global_map[sid]}
                            hash_to_global_map[sid].add(cid)
                return cached_rows

    # 2. 收集所有资产在 Stage 5 产出的原始安全需求
    all_csrs = []
    for asset in assets:
        asset_steps = steps_by_asset.get(asset.id, {})
        if not asset_steps:
            continue
        stage5_out = asset_steps.get("stage5").analysis_result.get("final_output", {}) if asset_steps.get("stage5") else {}
        summarized_csrs = stage5_out.get("summarized_csrs", [])
        if not summarized_csrs:
            # 兜底从 requirements 读取
            for req in stage5_out.get("requirements", []):
                req_text = req.get("cybersecurity_requirement") or ""
                if not req_text:
                    continue
                req_id = req.get("cybersecurity_requirement_id") or f"CSR-{asset.id:03d}"
                summarized_csrs.append({
                    "cybersecurity_requirement_id": req_id,
                    "csr_id": req_id,
                    "title": req.get("title") or f"针对 {asset.name} 的安全要求 / Security requirement for {asset.name}",
                    "sub_title": req.get("sub_title") or "安全防护 / Cybersecurity Protection",
                    "security_domain": req.get("security_domain"),
                    "cybersecurity_requirement": req_text,
                    "verification_method": req.get("verification_method", ""),
                    "requirement_type": req.get("requirement_type", "product"),
                    "source_requirement_ids": [req_id]
                })
        for csr in summarized_csrs:
            all_csrs.append({"asset": asset, "csr": csr})

    # 3. 执行全局项目级提炼
    rows = synthesize_project_csrs(db, all_csrs, global_tier2_to_tier1)

    # 4. 填充 hash_to_global_map 追溯映射
    if hash_to_global_map is not None:
        for r in rows:
            cid = r.get("csr_id")
            for tid in r.get("_raw_source_ids", []):
                if tid not in hash_to_global_map:
                    hash_to_global_map[tid] = set()
                elif isinstance(hash_to_global_map[tid], str):
                    hash_to_global_map[tid] = {hash_to_global_map[tid]}
                hash_to_global_map[tid].add(cid)
            for sid in r.get("_tier2_source_ids", []):
                if sid not in hash_to_global_map:
                    hash_to_global_map[sid] = set()
                elif isinstance(hash_to_global_map[sid], str):
                    hash_to_global_map[sid] = {hash_to_global_map[sid]}
                hash_to_global_map[sid].add(cid)

    # 5. 持久化到数据库 TaraStep 作为缓存
    try:
        last_step = steps[0] if steps else None
        if last_step and assets:
            run_id = last_step.run_id
            first_asset_id = assets[0].id
            
            # 检查是否已存在
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
        db.rollback()

    return rows
