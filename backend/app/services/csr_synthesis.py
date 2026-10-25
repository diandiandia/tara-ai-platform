import os
import re
import json
import time
import hashlib
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
DOMAIN_PREFIX_MAP = {
    "硬件安全 / Hardware Security": "CSR-HW",
    "系统安全 / System Security": "CSR-SYS",
    "密码安全与密钥管理 / Cryptography & Key Management": "CSR-CRY",
    "应用安全 / Application Security": "CSR-APP",
    "数据安全与隐私保护 / Data Security & Privacy": "CSR-DAT",
    "安全监控、测试与响应 / Security Monitoring, Testing & Response": "CSR-MON",
    "通信安全 / Communication Security": "CSR-COM",
    "安全开发、供应链与运维 / Secure Development, Supply Chain & Operations": "CSR-OPS",
    "通用安全与其它 / General & Other Security": "CSR-GEN"
}

MAJOR_ORDER = {
    "硬件安全 / Hardware Security": 1,
    "系统安全 / System Security": 2,
    "密码安全与密钥管理 / Cryptography & Key Management": 3,
    "应用安全 / Application Security": 4,
    "数据安全与隐私保护 / Data Security & Privacy": 5,
    "安全监控、测试与响应 / Security Monitoring, Testing & Response": 6,
    "通信安全 / Communication Security": 7,
    "安全开发、供应链与运维 / Secure Development, Supply Chain & Operations": 8,
    "通用安全与其它 / General & Other Security": 9
}

# -------------------------------------------------------------------------
# UNECE R155 Annex 5 Regulatory Guidance Prompts for LLM Synthesis
# -------------------------------------------------------------------------
UNECE_R155_ANNEX5_GUIDELINES = {
    "硬件安全 / Hardware Security": (
        "参考 UNECE R155 Annex 5 Part A (Table A1 - Mitigations related to vehicle hardware/interfaces):\n"
        "- 物理防拆与外壳/连接器开盖检测、PCB内层布线与敏感测试点隐藏 (Tamper detection, enclosure monitoring, PCB inner-layer routing);\n"
        "- 调试接口（JTAG, SWD, UART, USB等）量产硬件不可逆熔断锁定（eFuse/OTP）与售后受控挑战-应答授权解锁机制;\n"
        "- 密码运算侧信道攻击防御（恒定时间执行、掩码平衡）与硬件电压/时钟毛刺故障注入检测及安全复位;\n"
        "- 外部非易失存储（Flash, eMMC）硬件绑定加密与物理不可克隆（PUF/Root-of-Trust）防脱机拆焊读取保护。"
    ),
    "系统安全 / System Security": (
        "参考 UNECE R155 Annex 5 Part A (Table A1 - Mitigations related to vehicle software/execution environment):\n"
        "- 基于硬件信任根的分级安全启动链（Secure Boot Chain）与不可变BootROM固件数字签名完整性校验;\n"
        "- 固件安全版本硬件单调防回滚控制（Hardware Monotonic Anti-Rollback）;\n"
        "- A/B独立双分区原子刷写与更新异常安全回滚保护机制;\n"
        "- 操作系统强制访问控制（MAC/SELinux）与最小权限隔离，系统核心只读挂载;\n"
        "- 编译期与引导期内存漏洞利用缓解机制（ASLR/KASLR, DEP/NX, Stack Canary）;\n"
        "- 运行时内存代码与核心配置完整性周期度量，异常时触发受控安全降级或安全复位;\n"
        "- 独立硬件安全看门狗（Watchdog）心跳监控与失效安全保护机制."
    ),
    "密码安全与密钥管理 / Cryptography & Key Management": (
        "参考 UNECE R155 Annex 5 Part A (Table A1 - Mitigations related to cryptographic mechanisms):\n"
        "- 硬件安全模块（HSM/SHE）非易失密钥硬件隔离存储与物理禁止导出保护;\n"
        "- 车载PKI数字证书全生命周期安全管理、证书吊销检查（CRL/OCSP）与私钥片上安全生成;\n"
        "- 跨网络与应用层端到端数据传输加密与完整性保护;\n"
        "- OTA固件包构建端端数字签名与分发传输加密;\n"
        "- 易失性内存（RAM）中的会话密钥与敏感衍生材料使用后即时安全零化（Zeroization）销毁;\n"
        "- 崩溃核心转储（Core Dump）与调试异常日志中内存切片的即时密码学加密保护."
    ),
    "应用安全 / Application Security": (
        "参考 UNECE R155 Annex 5 Part A (Table A1 - Mitigations related to vehicle applications):\n"
        "- 安全编码规范（MISRA C/C++, AUTOSAR）与输入边界深度模糊测试（Fuzzing）检查;\n"
        "- 外部通信、总线与接口输入参数全量白名单结构化校验与畸形报文过滤;\n"
        "- 服务化中间件与进程间通信（SOME/IP, IPC, RPC）对端身份双向认证与调用授权控制;\n"
        "- 本地数据库与参数配置文件细粒度访问控制权限管理（ACL/权限最小化）;\n"
        "- 应用层API对象级权限防越权校验（BOLA/IDOR）与会话鉴权;\n"
        "- 安全认证接口防暴力破解、尝试速率限制与阶梯式安全锁定;\n"
        "- 量产二进制代码符号剥离（Symbol Stripping）与抗逆向工程加固."
    ),
    "数据安全与隐私保护 / Data Security & Privacy": (
        "参考 UNECE R155 Annex 5 Part A (Table A1 - Mitigations related to data/storage/privacy):\n"
        "- 本地持久化存储中敏感参数、标定数据与网络凭证的静态透明加密（Data at Rest Encryption）;\n"
        "- 行车轨迹、高精度定位坐标及驾驶行为数据的边缘去标识化、匿名化与最小化处理;\n"
        "- 外部感知摄像头采集的车外人脸与车牌数据实时边缘模糊化或马赛克脱敏;\n"
        "- 系统运行日志、故障转储及诊断跟踪记录敏感信息全量掩码与脱敏;\n"
        "- 车载敏感数据流信道级传输加密与防窃听监听;\n"
        "- 出厂重置（Factory Reset）与报废时用户隐私数据密码学安全擦除（Crypto-Shredding）;\n"
        "- 关键标定参数与配置清单密码学完整性（HMAC/数字签名）防篡改校验;\n"
        "- 车载业务数据来源真实性与单调计数器防重放保护."
    ),
    "安全监控、测试与响应 / Security Monitoring, Testing & Response": (
        "参考 UNECE R155 Annex 5 Part A (Table A1 - Mitigations related to security monitoring):\n"
        "- 车载网络入侵检测系统（CAN/Ethernet IDS）异常流量与未知报文实时监测与告警上报;\n"
        "- 格式标准化的关键网络安全事件审计日志加密存储、防篡改（哈希链/数字签名）与防删除;\n"
        "- 调试控制台与售后特权诊断操作全过程端到端审计追踪与操作员身份绑定;\n"
        "- OTA软件升级全生命周期各关键里程碑不可抵赖审计记录与上报备案;\n"
        "- 通信接口报文流量速率整形、洪泛丢弃与总线抗拒绝服务（Anti-DoS）;\n"
        "- 安全时钟源冗余一致性交叉校验、跳变检测与抗时间欺骗防御;\n"
        "- 安全审计日志专用存储配额隔离与抗泛洪覆盖保留策略."
    ),
    "通信安全 / Communication Security": (
        "参考 UNECE R155 Annex 5 Part A (Table A1 - Mitigations related to communication channels):\n"
        "- CAN/CAN-FD总线关键控制报文AUTOSAR SecOC真实性、新鲜度值（FV）与防重放保护;\n"
        "- 车云通信与以太网传输层安全协议（TLS 1.2/1.3）双向认证（mTLS）与强密码套件;\n"
        "- 骨干以太网物理端口IEEE 802.1X网络准入与MACsec线速硬件加密完整性保护;\n"
        "- 预定义通信矩阵报文白名单过滤、默认拒绝策略与跨安全域访问控制隔离;\n"
        "- 网络协议栈畸形报文深度防御与资源耗尽型连接池防护;\n"
        "- 服务化中间件（SOME/IP）发布订阅与RPC远程方法调用的安全策略鉴权;\n"
        "- 高速传感器串行总线（SPI, UART, MIPI CSI）数据校验与硬件屏蔽防护;\n"
        "- 高精度时间同步协议（IEEE 802.1AS/PTP）报文密码学TLV签名验证与抗时钟篡改;\n"
        "- 多路感知视频流来源硬件数字指纹鉴别与防帧重放/伪造;\n"
        "- UDS 27服务与DoIP诊断会话非对称/高强度安全访问鉴权与写服务保护."
    ),
    "安全开发、供应链与运维 / Secure Development, Supply Chain & Operations": (
        "参考 UNECE R155 Annex 5 Part C (Threats/mitigations to cybersecurity lifecycle and supply chain):\n"
        "- 量产交付固件工程测试代码完全剥离与后门调试指令清零;\n"
        "- 核心半导体芯片唯一硬件标识（UID）防伪溯源与供应链防假冒核查;\n"
        "- 制造工厂专用硬件安全设备（HSM）产线端到端加密密钥注入与配给;\n"
        "- 机器可读软件物料清单（SBOM）全量跟踪与第三方组件已知漏洞（CVE）持续监控;\n"
        "- 符合ISO/SAE 21434第13章要求的网络安全应急响应流程（PSIRT）与漏洞披露协同机制."
    )
}

def clean_major_category(domain_str: str) -> str:
    s = str(domain_str or "").lower()
    if "hardware" in s or "硬件" in s:
        return "硬件安全 / Hardware Security"
    if "system" in s or "系统" in s:
        return "系统安全 / System Security"
    if "crypt" in s or "key" in s or "密码" in s or "密钥" in s:
        return "密码安全与密钥管理 / Cryptography & Key Management"
    if "app" in s or "应用" in s:
        return "应用安全 / Application Security"
    if "data" in s or "privacy" in s or "数据" in s or "隐私" in s:
        return "数据安全与隐私保护 / Data Security & Privacy"
    if "monitor" in s or "防护" in s or "监控" in s or "test" in s or "测试" in s or "respond" in s or "响应" in s:
        return "安全监控、测试与响应 / Security Monitoring, Testing & Response"
    if "comm" in s or "通信" in s or "通讯" in s or "transmission" in s:
        return "通信安全 / Communication Security"
    if "dev" in s or "ops" in s or "开发" in s or "运维" in s or "生产" in s or "supply" in s or "chain" in s or "供应链" in s:
        return "安全开发、供应链与运维 / Secure Development, Supply Chain & Operations"
    return "通用安全与其它 / General & Other Security"

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
                    for line in rq.split("\n"):
                        cleaned = re.sub(r'^\(\d+\)\s*', '', line).strip()
                        if cleaned and cleaned != "N/A":
                            req_set.add(cleaned)
                            
            summarized_csrs = out.get("summarized_csrs") or []
            for csr in summarized_csrs:
                rq = str(csr.get("cybersecurity_requirement") or "").strip()
                if rq and rq != "N/A":
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

def get_csr_traceability_maps(stage5_out: dict, req_map: dict) -> tuple:
    """
    建立 TARA 原始需求 (Tier 1) 与资产级合并汇总需求 (Tier 2) 之间的追溯关系
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
            
        for a in aliases:
            alias_to_req[str(a).lower().strip()] = req_entry

    # 2. 遍历 summarized_csrs 建立双向追溯
    for csr in summarized_csrs:
        csr_text = str(csr.get("cybersecurity_requirement") or "").strip()
        if not csr_text:
            continue
        csr_hash_id = req_map.get(csr_text, csr.get("csr_id") or "N/A")
        
        s_ids = csr.get("source_requirement_ids") or []
        mapped_any = False
        
        for sid in s_ids:
            key = str(sid).lower().strip()
            if key in alias_to_req:
                orig_text, raw_tara_id = alias_to_req[key]
                tara_to_csr.setdefault(orig_text, []).append(csr_hash_id)
                csr_to_tara.setdefault(csr_hash_id, []).append(raw_tara_id)
                mapped_any = True
                
        # 兜底：如果 source_requirement_ids 无法匹配
        if not mapped_any:
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
        toks = _extract_tokens((it.get("sub_title") or "") + " " + (it.get("title") or ""))
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
            "source_requirement_ids": all_sources
        })
        
    return consolidated

# -------------------------------------------------------------------------
# LLM-Powered UNECE R155 Annex 5 Guided Synthesis
# -------------------------------------------------------------------------
def synthesize_domain_csrs_with_llm(db: Session, domain_name: str, items: list) -> list:
    """
    使用大模型依据 UNECE R155 Annex 5 规范指引，对某一分类域下的资产原始安全需求进行
    项目级（Item-level）全局归纳、去重、资产解耦与原子化提炼。
    """
    if not items:
        return []
        
    settings = db.query(SystemSettings).first()
    if not settings or not settings.api_key or settings.api_key in ["mock_test_key", ""]:
        print(f"[TARA-AI] 未配置有效大模型 API Key，启用通用语义算法兜底聚类 ({domain_name})")
        return semantic_fallback_clustering(domain_name, items)
        
    guideline = UNECE_R155_ANNEX5_GUIDELINES.get(domain_name, "参考 UNECE R155 Annex 5 汽车网络安全威胁与缓解通用规范。")
    
    prompt = f"""你是一名资深汽车网络安全系统架构专家（精通 ISO/SAE 21434 与 UNECE R155 法规）。
当前正在执行【{domain_name}】领域的项目级网络安全需求（Item-level Cybersecurity Requirements, CSR）全局综合提炼与去重。

【法规参考依据 (UNECE R155 Annex 5 核心指引)】：
{guideline}

【任务与合并原则】：
1. 跨资产归纳合并：输入是当前被评估对象（Item）中各个底层资产提出的网络安全要求列表。若多个资产的需求涉及相同的安全防御机制和防护目标，必须将其合并为一个高度提炼、职责单一、100%可验证的项目级原子网络安全需求（CSR）；
2. 技术中立原则（ISO/SAE 21434 概念阶段规范）：聚焦安全功能能力与防御目标（What & Why），严禁过早写死具体微观算法型号或单一数值（例如：避免写死 AES-256-XTS、ECDSA P-256、15分钟超时等，应表述为“经行业认可的高强度对称加密算法”、“标准非对称数字签名”、“安全会话超时退出机制”等），为后续软硬件架构设计保留选型与国密/商密合规空间；
3. 中英文双语规范：每一项输出的 sub_category、title、cybersecurity_requirement 必须是中英文双语对照输出，格式为“中文描述 / English translation”；
4. 100% 完整双向追溯：必须在每一项的 source_requirement_ids 数组中，精确列出该项归纳合并所涵盖的所有原始 raw_id。输入中的每一个 raw_id 必须被至少一项 CSR 覆盖，绝不允许丢项！
5. 适用资产映射：在 applicable_assets 数组中列出所有适用的资产名称列表。

请严格输出如下 JSON 格式：
{{
  "consolidated_csrs": [
    {{
      "sub_category": "安全机制子分类名称 / Sub-category mechanism",
      "title": "项目级需求标题 / Project-level requirement title",
      "cybersecurity_requirement": "中文需求完整规范描述... / English requirement specification...",
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
            
        print(f"[TARA-AI] 【{domain_name}】提炼成功: {len(items)} 项原始需求成功综合归纳为 {len(cons)} 项项目级原子 CSR！")
        return cons
    except Exception as e:
        print(f"[TARA-AI] 警告: 【{domain_name}】大模型提炼失败 ({e})，平滑切换至通用语义算法兜底...")
        return semantic_fallback_clustering(domain_name, items)

def synthesize_project_csrs(db: Session, all_csrs: list, global_tier2_to_tier1: dict = None) -> list:
    """
    对跨资产收集到的所有底层安全需求按 8 大安全分类域进行并发大模型全局提炼与编号，
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
                domain_results[d] = future.result()
            except Exception as e:
                print(f"[TARA-AI] 线程执行异常 ({d}): {e}")
                domain_results[d] = semantic_fallback_clustering(d, items_by_domain[d])

    # 按照标准分类域排序，生成全局顺序编号与扁平化行数据
    sorted_domains = sorted(active_domains, key=lambda d: MAJOR_ORDER.get(d, 99))
    
    final_rows = []
    for d in sorted_domains:
        cons_list = domain_results.get(d, [])
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
            if global_tier2_to_tier1:
                for sid in source_ids:
                    t1_list = global_tier2_to_tier1.get(sid)
                    if t1_list:
                        resolved_tier1_ids.update(t1_list)
                    else:
                        resolved_tier1_ids.add(sid)
            else:
                resolved_tier1_ids.update(source_ids)
                
            sorted_tier1_ids = sorted(list(resolved_tier1_ids))
            source_ids_str = ", ".join(sorted_tier1_ids)
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
                "cybersecurity_requirement": c.get("cybersecurity_requirement") or "",
                "source_tara_requirements": source_ids_str,
                "source_count": source_cnt,
                "_raw_source_ids": sorted_tier1_ids,
                "_tier2_source_ids": source_ids
            })
            
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
                        else:
                            resolved_t1.add(sid)
                    if resolved_t1:
                        sorted_t1 = sorted(list(resolved_t1))
                        r["source_tara_requirements"] = ", ".join(sorted_t1)
                        r["source_count"] = len(sorted_t1)
                        r["_raw_source_ids"] = sorted_t1
                        r["_tier2_source_ids"] = tier2_sources
                        
                    if hash_to_global_map is not None:
                        for tid in r.get("_raw_source_ids", []):
                            hash_to_global_map[tid] = cid
                        for sid in tier2_sources:
                            hash_to_global_map[sid] = cid
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
                summarized_csrs.append({
                    "cybersecurity_requirement_id": req.get("cybersecurity_requirement_id") or f"CSR-{asset.id:03d}",
                    "csr_id": req.get("cybersecurity_requirement_id") or f"CSR-{asset.id:03d}",
                    "title": req.get("title") or f"针对 {asset.name} 的安全要求 / Security requirement for {asset.name}",
                    "sub_title": req.get("sub_title") or "安全防护 / Cybersecurity Protection",
                    "security_domain": req.get("security_domain"),
                    "cybersecurity_requirement": req_text
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
                hash_to_global_map[tid] = cid
            for sid in r.get("_tier2_source_ids", []):
                hash_to_global_map[sid] = cid

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
