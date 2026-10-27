import time
import hashlib
import json
import httpx
import threading
from concurrent.futures import ThreadPoolExecutor
from celery import Celery
from sqlalchemy.orm import Session
from datetime import datetime

from app.core.celery_app import celery_app
from app.core.taxonomy import DOMAIN_PROMPT_LIST
from app.core.database import SessionLocal
from app.models.domain import Domain
from app.models.project import Project
from app.models.asset import Asset
from app.models.diagram import Diagram
from app.models.tara_run import TaraRun
from app.models.tara_step import TaraStep
from app.models.system_settings import SystemSettings
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import uuid

# SQLite Single-Writer Lock to prevent "database is locked" errors during concurrent commits
db_write_lock = threading.Lock()

def generate_unique_ds_sn() -> str:
    """
    生成 100% 唯一的损害场景编号 (DS SN)
    格式示例: DS_A8B9C1D2
    """
    entropy = f"{time.time_ns()}_{uuid.uuid4()}"
    sha256_hash = hashlib.sha256(entropy.encode('utf-8')).hexdigest()
    return f"DS_{sha256_hash[:8].upper()}"

def generate_content_id(prefix: str, content: str) -> str:
    """
    基于内容生成唯一编号（相同内容 → 相同ID），用于 CSO/CLM/CSC/CSR 等去重场景
    格式示例: CSO_A8B9C1D2
    """
    sha256_hash = hashlib.sha256(content.strip().encode('utf-8')).hexdigest()
    return f"{prefix}{sha256_hash[:8].upper()}"

def calculate_md5(*args) -> str:
    """
    计算字符串列表的 MD5 哈希值
    """
    hasher = hashlib.md5()
    for arg in args:
        if arg:
            hasher.update(str(arg).encode('utf-8'))
    return hasher.hexdigest()

def get_previous_completed_step(db: Session, asset_id: int, stage: str) -> TaraStep:
    """
    获取上一次运行中成功完成的步骤记录，用于增量校验
    """
    return db.query(TaraStep)\
        .join(TaraRun)\
        .filter(
            TaraStep.asset_id == asset_id,
            TaraStep.stage == stage,
            TaraStep.status == "completed"
        )\
        .order_by(TaraStep.id.desc())\
        .first()

def clean_and_parse_json(text: str) -> dict:
    """
    清除 LLM 返回的 Markdown 代码块标记并解析为 JSON
    """
    import re
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    
    # 1. 尝试清除 JSON 里的尾随逗号 (Trailing Commas)
    text = re.sub(r',\s*([\]}])', r'\1', text)
    
    # 2. 使用 strict=False 解析以容忍控制字符 (如未转义的换行、制表符等)
    parsed = json.loads(text, strict=False)
    
    # 3. 校验解析结果类型是否为字典
    if not isinstance(parsed, dict):
        raise TypeError(f"大模型返回的 JSON 解析结果不是 Dict 字典类型，而是 {type(parsed).__name__}")
        
    return parsed

def post_llm_request_with_retry(url: str, headers: dict, payload: dict, max_retries: int = 3, initial_delay: float = 1.0) -> httpx.Response:
    """
    带有自动重试机制的 HTTP POST 请求，用于弹性调用外部大模型接口
    """
    delay = initial_delay
    last_exc = None
    for attempt in range(max_retries):
        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(url, headers=headers, json=payload)
            if resp.status_code == 200:
                return resp
            
            # 如果遇到 429 或 5xx 状态码，进行指数退避重试
            if resp.status_code in [429, 500, 502, 503, 504]:
                print(f"[TARA-AI] 大模型服务响应 {resp.status_code}，将在 {delay} 秒后重试 (第 {attempt + 1}/{max_retries} 次)...")
                time.sleep(delay)
                delay *= 2
            else:
                # 400/401/403/404 等非临时性错误不进行重试，直接抛出
                raise RuntimeError(f"API HTTP {resp.status_code}: {resp.text[:150]}")
        except (httpx.HTTPError, httpx.NetworkError, httpx.TimeoutException) as exc:
            last_exc = exc
            print(f"[TARA-AI] 网络异常 ({exc})，将在 {delay} 秒后重试 (第 {attempt + 1}/{max_retries} 次)...")
            time.sleep(delay)
            delay *= 2
            
    if last_exc:
        raise last_exc
    raise RuntimeError("API 调用重试失败")

def get_time_consuming_points(val: str) -> int:
    v = str(val).lower().strip().replace(" ", "").replace("_", "").replace("-", "")
    if "1d" in v or "oneday" in v or "1day" in v:
        return 0
    if "1w" in v or "oneweek" in v or "1week" in v:
        return 1
    if "1m" in v or "onemonth" in v or "1month" in v:
        return 4
    if "6m" in v or "sixmonth" in v:
        return 17
    if "morethan" in v or "greaterthan" in v or ">" in v:
        return 19
    return 1 # default fallback

def get_expertise_points(val: str) -> int:
    v = str(val).lower().strip().replace(" ", "").replace("_", "").replace("-", "")
    if "layman" in v:
        return 0
    if "proficient" in v:
        return 3
    if "expert" in v:
        if "multiple" in v:
            return 8
        return 6
    return 3 # default fallback

def get_knowledge_points(val: str) -> int:
    v = str(val).lower().strip().replace(" ", "").replace("_", "").replace("-", "")
    if "public" in v:
        return 0
    if "restricted" in v:
        return 3
    if "confidential" in v:
        if "strictly" in v:
            return 11
        return 7
    return 3 # default fallback

def get_window_points(val: str) -> int:
    v = str(val).lower().strip().replace(" ", "").replace("_", "").replace("-", "")
    if "unlimited" in v:
        return 0
    if "easy" in v:
        return 1
    if "moderate" in v:
        return 4
    if "difficult" in v:
        return 10
    return 1 # default fallback

def get_equipment_points(val: str) -> int:
    v = str(val).lower().strip().replace(" ", "").replace("_", "").replace("-", "")
    if "standard" in v:
        return 0
    if "special" in v:  # matches specialized or specialied
        return 4
    if "bespoke" in v:
        if "multiple" in v:
            return 9
        return 7
    return 0 # default fallback

def call_llm_json(db: Session, system_prompt: str, messages: list, mock_fallback_func, *fallback_args) -> dict:
    settings = db.query(SystemSettings).first()
    if not settings or not settings.api_key or settings.api_key == "mock_test_key":
        return mock_fallback_func(*fallback_args)
    
    url = f"{settings.api_base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": settings.model_name,
        "messages": messages,
        "response_format": {"type": "json_object"}
    }
    
    try:
        print(f"[TARA-AI] 发起大模型服务请求 ({settings.model_name}) 执行 API 调用...")
        resp = post_llm_request_with_retry(url, headers, payload)
        resp_data = resp.json()
        raw_text = resp_data["choices"][0]["message"]["content"]
        return clean_and_parse_json(raw_text)
    except Exception as e:
        print(f"[TARA-AI] 警告: 大模型调用或解析失败 ({e})，执行 Mock 降级。")
        return mock_fallback_func(*fallback_args)

def mock_stage5_summary(asset: Asset, device_reqs: list) -> dict:
    summarized = []
    for req in device_reqs:
        req_id = req.get("cybersecurity_requirement_id", "CSR-001")
        req_text = req.get("cybersecurity_requirement", "")
        summarized.append({
            "asset_id": f"ID{asset.id}",
            "asset_name": asset.name,
            "cybersecurity_requirement_id": req_id,
            "csr_id": req_id,
            "title": f"针对 {asset.name} 的安全要求 / Security requirement for {asset.name}",
            "sub_title": f"网络安全防护 / Cybersecurity protection for {asset.name}",
            "security_domain": "安全通信 / Secure Transmission" if "通信" in req_text or "message" in req_text else "系统安全 / System Security",
            "cybersecurity_requirement": req_text
        })
    return {"asset_cybersecurity_requirement_list": summarized}

FEASIBILITY_MATRIX = [
    (25, "Very Low"),
    (20, "Low"),
    (14, "Medium"),
    (0, "High")
]

def calculate_feasibility(total_diff: int) -> str:
    for threshold, score in FEASIBILITY_MATRIX:
        if total_diff >= threshold:
            return score
    return "High"

def mock_tara_ai_call(stage: str, asset: Asset, prev_stages: dict) -> dict:
    """
    Rules-based 降级算法：模拟 TARA 大模型分析输出
    根据资产名称与类型输出强结构化的分析草案 JSON，确保在无网络或未配置大模型时分析依然可用。
    """
    time.sleep(0.1) # 模拟微弱延迟
    
    domain_name = asset.domain.name if asset.domain else "车载子系统"
    
    if stage == "stage1":
        # 评估属性
        auth, i, non_rep, c, a, authz, priv = 1, 1, 1, 1, 1, 1, 1
        if asset.asset_type == "data":
            c, i, priv = 4, 3, 5
        elif asset.asset_type == "communication":
            auth, i, a = 4, 4, 3
        elif asset.asset_type == "hardware":
            i, a = 3, 3
        else: # software
            auth, i, a = 4, 4, 3
            
        selected = []
        scores = {
            "Authenticity": auth,
            "Integrity": i,
            "Non-repudiation": non_rep,
            "Confidentiality": c,
            "Availability": a,
            "Authorization": authz,
            "Privacy": priv
        }
        for attr, score in scores.items():
            if score >= 2:
                selected.append(attr)
                
        def get_rating(score):
            if score >= 4: return "High"
            if score >= 2: return "Medium"
            if score >= 1: return "Low"
            return "None"
            
        return {
            "attributes": scores,
            "selected_attributes": selected,
            "authenticity": get_rating(auth),
            "integrity": get_rating(i),
            "non-repudiation": get_rating(non_rep),
            "confidentiality": get_rating(c),
            "availability": get_rating(a),
            "authorization": get_rating(authz),
            "privacy": get_rating(priv),
            "description": f"已选定高相关安全属性: {', '.join(selected)} 进行后续分析。 / Selected highly relevant security attributes: {', '.join(selected)} for subsequent analysis." if selected else "无高相关(分数>=2)安全属性，未选择属性。 / No highly relevant (score >= 2) security attributes, none selected."
        }
        
    elif stage == "stage2":
        selected_attrs = prev_stages.get("stage1", {}).get("selected_attributes", ["Confidentiality", "Integrity"])
        damage_scenarios = []
        
        # Mapping impact level strings to numerical values
        impact_map = {"negligible": 0, "moderate": 1, "major": 2, "severe": 3}
        
        for attr in selected_attrs:
            # Generate 2 damage scenarios per selected attribute
            ds1_text = f"攻击者破坏了 {asset.name} 的 {attr} 属性，导致 {domain_name} 系统的相关控制功能受损，从而影响驾驶安全或造成财产损失。 / Attacker compromises the {attr} attribute of {asset.name}, impairing control functions of the {domain_name} system, thereby impacting driving safety or causing property damage."
            ds2_text = f"恶意用户篡改 {asset.name} 的 {attr} 信息，导致 {domain_name} 系统做出错误决策，存在引发交通事故的潜在风险。 / Malicious user tampers with the {attr} information of {asset.name}, causing the {domain_name} system to make incorrect decisions, presenting potential risks of traffic accidents."
            
            # Decide impact ratings
            s, f, o, p = "Negligible", "Moderate", "Moderate", "Negligible"
            if attr == "Privacy":
                p = "Severe"
            if attr in ["Integrity", "Authenticity"]:
                s = "Major"
                o = "Severe"
                
            s_num = impact_map.get(s.lower(), 1)
            f_num = impact_map.get(f.lower(), 1)
            o_num = impact_map.get(o.lower(), 1)
            p_num = impact_map.get(p.lower(), 0)
            overall_impact = max(s_num, f_num, o_num, p_num)
            
            damage_scenarios.append({
                "attribute": attr,
                "damage_scenario_sn": generate_unique_ds_sn(),
                "damage_scenario": ds1_text,
                "impact_rating": {"safety": s, "financial": f, "operational": o, "privacy": p},
                "overall_impact": overall_impact
            })
            damage_scenarios.append({
                "attribute": attr,
                "damage_scenario_sn": generate_unique_ds_sn(),
                "damage_scenario": ds2_text,
                "impact_rating": {"safety": s, "financial": f, "operational": o, "privacy": p},
                "overall_impact": overall_impact
            })
            
        # Select overall max safety, financial, operational, privacy
        max_s, max_f, max_o, max_p = 0, 0, 0, 0
        all_ds_texts = []
        for ds in damage_scenarios:
            all_ds_texts.append(ds["damage_scenario"])
            r = ds["impact_rating"]
            # Convert string to number
            max_s = max(max_s, impact_map.get(r["safety"].lower(), 0))
            max_f = max(max_f, impact_map.get(r["financial"].lower(), 0))
            max_o = max(max_o, impact_map.get(r["operational"].lower(), 0))
            max_p = max(max_p, impact_map.get(r["privacy"].lower(), 0))
            
        overall_impact = max(max_s, max_f, max_o, max_p)
        
        return {
            "damage_scenarios": damage_scenarios,
            "damage_scenario": "; ".join(all_ds_texts[:2]) if all_ds_texts else f"由于安全属性泄露或篡改，导致 {domain_name} 子系统故障。 / Cybersecurity attribute breach or tampering leads to failure of the {domain_name} subsystem.",
            "impact_rating": {"safety": max_s, "financial": max_f, "operational": max_o, "privacy": max_p},
            "overall_impact": overall_impact
        }
        
    elif stage == "stage3":
        damage_scenarios = prev_stages.get("stage2", {}).get("damage_scenarios", [])
        if not damage_scenarios:
            # Fallback if stage2 didn't have damage_scenarios list
            damage_scenarios = [{
                "attribute": "Integrity",
                "damage_scenario_sn": generate_unique_ds_sn(),
                "damage_scenario": prev_stages.get("stage2", {}).get("damage_scenario", f"由于 {asset.name} 遭受篡改，导致 {domain_name} 子系统功能受损。 / Due to tampering of {asset.name}, {domain_name} subsystem functions are impaired."),
                "overall_impact": prev_stages.get("stage2", {}).get("overall_impact", 1)
            }]
            
        threat_scenarios = []
        all_attack_paths = []
        max_feasibility = "Very Low"
        feasibility_order = {"Very Low": 1, "Low": 2, "Medium": 3, "High": 4}
        
        for ds in damage_scenarios:
            ts_text = f"攻击者利用系统调试协议漏洞，向目标资产发送虚假指令，破坏其 {ds['attribute']} 属性，进而导致损害场景: {ds['damage_scenario']} / Attacker exploits system debugging protocol vulnerability to send spoofed commands to the target asset, compromising its {ds['attribute']} attribute, thereby leading to the damage scenario: {ds['damage_scenario']}"
            
            # Formulate 2 attack paths
            paths = [
                {
                    "attack_path": "近距离通过OBD/蓝牙接口注入恶意协议报文进行重放或伪造。 / Close-range injection of malicious protocol packets via OBD/Bluetooth interface for replay or spoofing.",
                    "time_consuming": "no_more_than_1w",
                    "expertise": "proficient",
                    "knowledge_about_toe": "restricted",
                    "window_of_opportunity": "easy",
                    "equipment": "standard",
                    "difficulty": 9,
                    "feasibility": "High"
                },
                {
                    "attack_path": "物理拆解设备并对芯片进行固件读取、逆向分析及逻辑修改。 / Physical disassembly of the device for chip firmware extraction, reverse engineering, and logic modification.",
                    "time_consuming": "no_more_than_1m",
                    "expertise": "expert",
                    "knowledge_about_toe": "confidential",
                    "window_of_opportunity": "moderate",
                    "equipment": "specialized",
                    "difficulty": 21,
                    "feasibility": "Low"
                }
            ]
            
            ts_feasibility = "High" # max of its paths
            threat_scenarios.append({
                "attribute": ds["attribute"],
                "damage_scenario_sn": ds["damage_scenario_sn"],
                "damage_scenario": ds["damage_scenario"],
                "overall_impact": ds["overall_impact"],
                "threat_id": f"TS_{ds['attribute']}_1",
                "threat_scenario": ts_text,
                "attack_paths": paths,
                "final_feasibility": ts_feasibility
            })
            
            for p in paths:
                all_attack_paths.append({
                    "path_id": f"P{len(all_attack_paths) + 1}",
                    "method": p["attack_path"],
                    "feasibility": p["feasibility"]
                })
                if feasibility_order.get(p["feasibility"], 1) > feasibility_order.get(max_feasibility, 1):
                    max_feasibility = p["feasibility"]
                    
        all_ts_texts = [ts["threat_scenario"] for ts in threat_scenarios]
        
        return {
            "threat_scenarios": threat_scenarios,
            "threat_scenario": "; ".join(all_ts_texts[:2]) if all_ts_texts else "攻击者操纵相关接口以注入恶意数据流。 / Attacker manipulates relevant interfaces to inject malicious data streams.",
            "attack_paths": all_attack_paths,
            "final_feasibility": max_feasibility
        }
        
    elif stage == "stage4":
        threat_scenarios = prev_stages.get("stage3", {}).get("threat_scenarios", [])
        if not threat_scenarios:
            # Fallback
            threat_scenarios = [{
                "attribute": "Integrity",
                "threat_id": "TS_Integrity_1",
                "threat_scenario": prev_stages.get("stage3", {}).get("threat_scenario", "威胁场景 / Threat scenario"),
                "overall_impact": prev_stages.get("stage2", {}).get("overall_impact", 1),
                "final_feasibility": prev_stages.get("stage3", {}).get("final_feasibility", "Medium")
            }]
            
        risk_decisions = []
        max_risk_val = 1
        has_mitigate = False
        has_avoid = False
        has_transfer = False
        
        # Risk matrix
        risk_matrix = {
            "verylow": [1, 1, 1, 2],
            "low": [1, 2, 2, 3],
            "medium": [1, 2, 3, 4],
            "high": [1, 3, 4, 5],
        }
        
        justifications = []
        for ts in threat_scenarios:
            impact_val = ts.get("overall_impact", 1)
            feasibility_key = str(ts.get("final_feasibility", "Medium")).lower().replace(" ", "")
            risk_val = risk_matrix.get(feasibility_key, [1, 2, 3, 4])[min(max(impact_val, 0), 3)]
            
            max_risk_val = max(max_risk_val, risk_val)
            
            decision = "Reduce"
            change = ""
            goal = f"针对资产 {asset.name} 的安全属性 {ts.get('attribute')} 威胁，制定安全防护策略，通过采用防重放、报文完整性签名校验等机制降低攻击可行性与整体风险。 / Formulate security protection strategies against threats to the security attribute {ts.get('attribute')} of asset {asset.name}, reducing attack feasibility and overall risk through replay protection and packet integrity signature verification mechanisms."
            claim = ""
            
            if risk_val <= 1:
                decision = "Retain"
                claim = f"基于风险值极小 ({risk_val})，接受该威胁针对 {asset.name} 的网络安全风险。 / Based on minimal risk value ({risk_val}), accept the cybersecurity risk of this threat targeting {asset.name}."
                goal = ""
                has_transfer = True
            else:
                has_mitigate = True
                
            risk_decisions.append({
                "attribute": ts.get("attribute"),
                "threat_id": ts.get("threat_id"),
                "threat_scenario": ts.get("threat_scenario"),
                "damage_scenario": ts.get("damage_scenario"),
                "final_feasibility": ts.get("final_feasibility"),
                "overall_impact": ts.get("overall_impact"),
                "risk_value": risk_val,
                "risk_treatment": decision,
                "item_change": change,
                "cybersecurity_goal_id": generate_content_id("CSO_", goal) if goal else "",
                "cybersecurity_goal": goal,
                "security_property": ts.get("attribute") if goal else "",
                "goal_asset": asset.name if goal else "",
                "success_criteria": "",
                "cybersecurity_claim_id": generate_content_id("CLM_", claim) if claim else "",
                "cybersecurity_claim": claim,
                "residual_risk": f"风险值 {risk_val} / Risk value {risk_val}" if claim else "",
                "claim_justification": "基于风险矩阵评估 / Based on risk matrix assessment" if claim else "",
                "claim_conditions": "",
                "claim_validity": "",
                "claim_owner": ""
            })
            
            justifications.append(f"安全风险为 {risk_val} ({ts.get('attribute')}属性)，采取 {decision} 决策。 / Cybersecurity risk is {risk_val} ({ts.get('attribute')} attribute), decision: {decision} decision.")
            
        decision_raw = "mitigate"
        if has_mitigate:
            decision_raw = "mitigate"
        elif has_avoid:
            decision_raw = "avoid"
        elif has_transfer:
            decision_raw = "transfer"
        else:
            decision_raw = "accept"
            
        return {
            "risk_decisions": risk_decisions,
            "risk_rating": max_risk_val,
            "risk_decision": decision_raw,
            "justification": "; ".join(justifications)
        }
        
    elif stage == "stage5":
        risk_decisions = prev_stages.get("stage4", {}).get("risk_decisions", [])
        if not risk_decisions:
            # Fallback
            risk_decisions = [{
                "attribute": "Integrity",
                "threat_id": "TS_Integrity_1",
                "threat_scenario": "威胁场景 / Threat scenario",
                "risk_value": prev_stages.get("stage4", {}).get("risk_rating", 3),
                "risk_treatment": "Reduce" if prev_stages.get("stage4", {}).get("risk_decision") == "mitigate" else "Retain",
                "cybersecurity_goal": "保护系统安全 / Protect system security"
            }]
            
        # Check if any reduce
        has_reduce = any(dec.get("risk_treatment") == "Reduce" for dec in risk_decisions)
        if not has_reduce:
            return {
                "cso": "无需制定安全目标 / No cybersecurity goals needed",
                "csr": [],
                "exempted": True,
                "reason": "所有威胁场景决策均为Retain/Share，免除安全需求制定。 / All threat scenario decisions are Retain/Share, exempting the formulation of cybersecurity requirements."
            }
            
        requirements = []
        device_requirements = []
        for dec in risk_decisions:
            if dec.get("risk_treatment") == "Reduce":
                req_text = f"系统应对针对 {asset.name} 的 {dec['attribute']} 关键消息启用强制签名及抗重放序号校验。 / The system shall enable mandatory signature and anti-replay sequence number checks for critical messages targeting the {dec['attribute']} of {asset.name}."
                
                ctrl_text = f"通过部署经行业认可的安全通信机制保护 {asset.name} 的数据传输通道。 / Protect the data transmission channel of {asset.name} by deploying industry-recognized secure communication mechanisms."
                requirements.append({
                    "threat_id": dec["threat_id"],
                    "cybersecurity_control_id": generate_content_id("CSC_", ctrl_text),
                    "cybersecurity_control": ctrl_text,
                    "control_type": "预防 / Preventive",
                    "implementation_type": "技术 / Technical",
                    "control_objective": ctrl_text,
                    "control_point": asset.name,
                    "allocated_to_device": "yes",
                    "cybersecurity_requirement_id": generate_content_id("CSR_", req_text),
                    "cybersecurity_requirement": req_text,
                    "verification_method": "测试/检查 / Test/Inspection",
                    "requirement_type": "product",
                    "rationale": dec.get("threat_scenario", "")
                })
                
                s_dom = "安全通信 / Secure Transmission" if dec.get('attribute') in ["Integrity", "Authenticity", "Non-repudiation"] else "系统安全 / System Security"
                device_requirements.append({
                    "asset_id": f"ID{asset.id}",
                    "asset_name": asset.name,
                    "cybersecurity_requirement_id": generate_content_id("CSR_", req_text),
                    "csr_id": generate_content_id("CSR_", req_text),
                    "title": f"针对 {asset.name} {dec['attribute']} 的报文防伪要求 / Packet anti-counterfeiting requirements targeting {asset.name} {dec['attribute']}",
                    "sub_title": "双向通信安全签名 / Bidirectional secure communication signature",
                    "security_domain": s_dom,
                    "cybersecurity_requirement": req_text,
                    "verification_method": "测试/检查 / Test/Inspection",
                    "requirement_type": "product",
                    "rationale": dec.get("threat_scenario", ""),
                    "source_requirement_ids": [generate_content_id("CSR_", req_text)]
                })
                
        # Consolidated CSR list
        all_csr_texts = [r["cybersecurity_requirement"] for r in device_requirements]
        all_cso_texts = [r["cybersecurity_control"] for r in requirements]
        
        return {
            "requirements": requirements,
            "summarized_csrs": device_requirements,
            "cso": "; ".join(all_cso_texts[:2]) if all_cso_texts else "网络安全目标制定完成 / Formulation of cybersecurity goals completed",
            "csr": all_csr_texts,
            "exempted": False,
            "reason": ""
        }
        
    return {}

class Stage1Output(BaseModel):
    Authenticity: int = Field(ge=0, le=5)
    Integrity: int = Field(ge=0, le=5)
    Non_repudiation: int = Field(ge=0, le=5, alias="Non-repudiation")
    Confidentiality: int = Field(ge=0, le=5)
    Availability: int = Field(ge=0, le=5)
    Authorization: int = Field(ge=0, le=5)
    Privacy: int = Field(ge=0, le=5)

    model_config = {
        "populate_by_name": True
    }

class AttackPathSchema(BaseModel):
    attack_path: str = Field(description="中英文对照的攻击步骤描述，格式：中文 / English")
    time_consuming: str = Field(description="时间消耗，可选值：no_more_than_1d, no_more_than_1w, no_more_than_1m, no_more_than_6m, more_than_6m")
    expertise: str = Field(description="专业知识，可选值：layman, proficient, expert, multiple_expert")
    knowledge_about_toe: str = Field(description="靶机知识，可选值：public, restricted, confidential, strictly_confidential")
    window_of_opportunity: str = Field(description="攻击窗口，可选值：unlimited, easy, moderate, difficult")
    equipment: str = Field(description="所需设备，可选值：standard, specialized, bespoke, multiple_bespoke")

class ThreatScenarioSchema(BaseModel):
    threat_scenario: str = Field(description="中英文对照的威胁场景描述，格式：中文 / English")
    attribute: str = Field(description="安全属性，如 Authenticity, Integrity, Non-repudiation, Confidentiality, Availability, Authorization, Privacy 中的一个")
    attack_paths: List[AttackPathSchema] = Field(description="针对该威胁场景的攻击路径列表")

class DamageScenarioSchema(BaseModel):
    damage_scenario_sn: str = Field(description="损害场景编号，以 DS_ 开头并结合唯一哈希字符标识，例如 DS_A8B9C1D2")
    damage_scenario: str = Field(description="中英文对照的损害场景描述，格式：中文 / English")
    attribute: str = Field(description="涉及的安全属性，如 Authenticity, Integrity, Non-repudiation, Confidentiality, Availability, Authorization, Privacy 中的一个")
    safety: str = Field(description="安全影响级别评级。可选值: Negligible, Moderate, Major, Severe")
    financial: str = Field(description="财务影响级别评级。可选值: Negligible, Moderate, Major, Severe")
    operational: str = Field(description="运行影响级别评级。可选值: Negligible, Moderate, Major, Severe")
    privacy: str = Field(description="隐私影响级别评级。可选值: Negligible, Moderate, Major, Severe")
    threat_scenarios: List[ThreatScenarioSchema] = Field(description="针对该损害场景可能存在的威胁场景列表")

class Stage23Output(BaseModel):
    damage_scenarios: List[DamageScenarioSchema]

class RequirementSchema(BaseModel):
    threat_id: Optional[str] = Field(default="", description="对应威胁场景的编号，例如 TS_Integrity_1")
    goal_ids: List[str] = Field(default_factory=list, description="该要求对应的 Goal ID 列表")
    cybersecurity_control_id: str = Field(description="安全控制措施编号，格式以 CSO-001, CSO-002 等递增顺序编号编写，不使用 CSO-XXX 字面量")
    control_ids: List[str] = Field(default_factory=list, description="该要求对应的 Control ID 列表")
    claim_ids: List[str] = Field(default_factory=list, description="该要求支撑的 Claim ID 列表，可为空")
    cybersecurity_control: str = Field(description="中英文对照的安全控制措施目标，格式：中文 / English")
    control_type: str = Field(default="", description="控制类型：预防/检测/纠正 / Preventive/Detective/Corrective")
    implementation_type: str = Field(default="", description="实现类型：技术/操作 / Technical/Operational")
    control_objective: str = Field(default="", description="控制目标（中英文对照）")
    control_point: str = Field(default="", description="控制作用点，例如资产、接口或生命周期阶段（中英文对照）")
    dependent_functions: List[str] = Field(default_factory=list, description="该控制依赖的 item 功能")
    dependent_controls: List[str] = Field(default_factory=list, description="该控制依赖的其他 Control ID")
    interaction_description: str = Field(default="", description="控制之间、控制与 item 功能之间的交互/依赖描述（中英文对照）")
    applicable_lifecycle: str = Field(default="", description="适用生命周期阶段（中英文对照）")
    allocated_to_device: str = Field(description="是否分配给目标域控/设备，可选值：yes, no；只有目标域控或其 component 实现时为 yes")
    requirement_scope: str = Field(default="item", description="需求范围：item / operational_environment / other_item")
    owner: str = Field(default="", description="需求责任方：OEM / Tier1 / supplier / plant / service / backend")
    allocated_to: str = Field(default="", description="分配到的 item/component ID；运行环境或其他 item 填 N/A")
    allocation_type: str = Field(default="primary", description="分配类型：primary / secondary")
    cybersecurity_requirement_id: str = Field(description="网络安全要求编号，格式以 CSR-001, CSR-002 等递增顺序编号编写，不使用 CSR-XXX 字面量")
    cybersecurity_requirement: str = Field(description="中英文对照的具体网络安全要求，格式：中文 / English")
    verification_method: str = Field(default="", description="验证方法（测试/分析/检查/演示），中英文对照；没有验证方法的要求不能作为最终 CSR")
    acceptance_criteria: str = Field(default="", description="可判定的通过/失败验收准则（中英文对照）")
    source_ids: List[str] = Field(default_factory=list, description="来源 OEM/TARA ID，只能引用输入中存在的 ID")
    dependencies: List[str] = Field(default_factory=list, description="依赖的其他 Requirement/Control/接口")
    requirement_type: str = Field(default="product", description="需求类型：product（产品）/ process（流程）/ supplier（供应商）")
    rationale: str = Field(default="", description="需求依据及对应的威胁/攻击路径（中英文对照）")
    open_issue: str = Field(default="", description="信息不足或待确认事项（中英文对照）；不得用编造内容替代")

class RiskDecisionSchema(BaseModel):
    threat_id: str = Field(description="对应威胁场景的编号，例如 TS_Integrity_1")
    risk_treatment: str = Field(description="风险处理选项，可选值：Avoid, Reduce, Share, Retain")
    item_change: str = Field(description="如果选择 Avoid，提供 item_change 中英文理由，否则填空字符串")
    cybersecurity_goal: str = Field(description="如果选择 Reduce，提供 cybersecurity_goal 中英文目标描述，否则填空字符串")
    security_property: str = Field(default="", description="Goal 对应的安全属性，例如 Integrity / Confidentiality")
    goal_asset: str = Field(default="", description="Goal 保护对象（中英文对照）")
    success_criteria: str = Field(default="", description="Goal 的可验证成功判据（中英文对照）")
    cybersecurity_claim: str = Field(description="如果选择 Share/Retain，提供 cybersecurity_claim 中英文声明，否则填空字符串")
    claim_type: str = Field(default="", description="Claim 类型：residual_risk_accept / risk_transfer / dependency_assumption / goal_achievement")
    residual_risk: str = Field(default="", description="Claim 的剩余风险说明（中英文对照）")
    claim_justification: str = Field(default="", description="Claim 的可接受/可转移依据（中英文对照）")
    claim_conditions: str = Field(default="", description="Claim 的适用条件与依赖（中英文对照）")
    claim_validity: str = Field(default="", description="Claim 的有效期（中英文对照）")
    claim_owner: str = Field(default="", description="Claim 的批准角色/责任人（中英文对照）")
    claim_evidence: str = Field(default="", description="Claim 的证据/参考文件（中英文对照）")
    claim_status: str = Field(default="", description="Claim 状态：draft / approved / rejected")
    open_issue: str = Field(default="", description="信息不足或待确认事项（中英文对照）；不得用编造内容替代")
    requirements: List[RequirementSchema] = Field(description="仅在 Reduce 时编写该威胁场景的安全要求列表，否则为空列表")

class SummarizedRequirementSchema(BaseModel):
    cybersecurity_requirement_id: str = Field(description="网络安全要求ID，格式以 CSR-001, CSR-002 等递增顺序编号编写，不使用 CSR-XXX 字面量")
    csr_id: str = Field(description="CSR ID，同网络安全要求ID")
    title: str = Field(description="中英文对照的要求标题，格式：中文 / English")
    sub_title: str = Field(description="中英文对照的要求副标题，格式：中文 / English")
    security_domain: str = Field(description="中英文对照的安全领域，例如：安全通信 / Secure Transmission")
    cybersecurity_requirement: str = Field(description="中英文对照的网络安全要求内容，格式：中文 / English")
    verification_method: str = Field(default="", description="验证方法（测试/分析/检查/演示），中英文对照")
    requirement_type: str = Field(default="product", description="需求类型：product / process / supplier")
    rationale: str = Field(default="", description="需求依据及对应的威胁/攻击路径（中英文对照）")
    source_requirement_ids: List[str] = Field(default=[], description="该归纳/去重后的网络安全需求所涵盖的原始网络安全要求编号列表 (即前面 requirements 中的 cybersecurity_requirement_id，例如 ['CSR-001', 'CSR-002'])，以体现严格的追溯关系")

class Stage45Output(BaseModel):
    risk_decisions: List[RiskDecisionSchema]
    summarized_requirements: List[SummarizedRequirementSchema] = Field(description="对所有 allocated_to_device == 'yes' 的安全要求进行去重与归纳整理后的项目级网络安全需求列表")

class StandaloneStage5Output(BaseModel):
    requirements: List[RequirementSchema] = Field(description="为所有 risk_treatment 为 Reduce 的威胁场景分别拟定的安全控制措施与网络安全要求。每个要求必须关联对应的 threat_id。")
    summarized_requirements: List[SummarizedRequirementSchema] = Field(description="对所有 allocated_to_device == 'yes' 的安全要求进行去重与归纳整理后的项目级网络安全需求列表")

def _extract_dfd_context(snapshot_json: str) -> tuple:
    """Extract component and interface hints from a stored React Flow DFD."""
    try:
        snapshot = json.loads(snapshot_json or "{}")
    except Exception:
        return [], []
    if not isinstance(snapshot, dict):
        return [], []

    components = []
    interfaces = []
    for node in snapshot.get("nodes", []) or []:
        if not isinstance(node, dict):
            continue
        data = node.get("data") if isinstance(node.get("data"), dict) else {}
        name = str(data.get("name") or "").strip()
        if not name:
            continue
        components.append({
            "id": str(node.get("id") or name),
            "name": name,
            "type": str(node.get("type") or data.get("type") or ""),
            "description": str(data.get("description") or data.get("remarks") or ""),
            "protocol": str(data.get("protocol") or ""),
            "source": "dfd",
        })

    for edge in snapshot.get("edges", []) or []:
        if not isinstance(edge, dict):
            continue
        data = edge.get("data") if isinstance(edge.get("data"), dict) else {}
        label = data.get("label") or data.get("name") or edge.get("label") or edge.get("id")
        if label:
            interfaces.append(str(label))

    return components, interfaces


def build_item_context(db: Session, asset: Asset) -> dict:
    """Build the target-domain-controller / item context for the LLM.

    The application models a "Domain" as a sub-domain controller. Its name is
    therefore the target domain controller for the current TARA. Component and
    interface details are only available if the user has drawn a DFD; when they
    are absent we return an empty list instead of inventing components.
    """
    domain = None
    project = None
    diagrams = []
    try:
        domain = db.query(Domain).filter(Domain.id == asset.domain_id).first()
        project = db.query(Project).filter(Project.id == domain.project_id).first() if domain else None
        diagrams = db.query(Diagram).filter(Diagram.domain_id == asset.domain_id).all()
    except Exception as context_err:
        # Context enrichment must never block the TARA run. Missing DFD tables
        # or an unavailable project record simply means less allocation context.
        print(f"[TARA-AI] 目标域控/DFD 上下文读取失败，降级为最小上下文: {context_err}")

    components = []
    interfaces = []
    for diagram in diagrams:
        dfd_components, dfd_interfaces = _extract_dfd_context(diagram.snapshot_json)
        components.extend(dfd_components)
        interfaces.extend(dfd_interfaces)

    # Deduplicate while preserving order. The current asset is always included
    # so the model has at least one concrete component to allocate to.
    unique_components = []
    seen_component_keys = set()
    seen_component_names = set()
    for component in components:
        key = (component.get("id"), component.get("name"))
        name = str(component.get("name") or "").strip()
        if key in seen_component_keys or (name and name in seen_component_names):
            continue
        seen_component_keys.add(key)
        if name:
            seen_component_names.add(name)
        unique_components.append(component)

    asset_key = (f"AST-{asset.id}", asset.name)
    if asset_key not in seen_component_keys and asset.name not in seen_component_names:
        unique_components.append({
            "id": f"AST-{asset.id}",
            "name": asset.name,
            "type": asset.asset_type or "",
            "description": asset.description or "",
            "protocol": asset.protocol or "",
            "source": "asset",
        })

    unique_interfaces = []
    seen_interfaces = set()
    for interface in interfaces:
        if interface not in seen_interfaces:
            seen_interfaces.add(interface)
            unique_interfaces.append(interface)
    if not unique_interfaces and asset.protocol and asset.protocol != "无":
        unique_interfaces.append(asset.protocol)

    domain_name = domain.name if domain else ""
    project_name = project.name if project else ""
    target_domain_controller = {
        "id": f"DOMAIN-{asset.domain_id}",
        "name": domain_name,
        "project": project_name,
        "description": project.description if project and project.description else "",
    }
    item = {
        "name": domain_name or asset.name,
        "boundary": f"当前分析的子域控/目标域控: {domain_name or asset.name}",
        "functions": [
            c["name"] for c in unique_components
            if c.get("type") == "process" and c.get("name")
        ],
        "components": unique_components,
        "interfaces": unique_interfaces,
        "lifecycle": ["development", "production", "operation", "maintenance", "decommissioning"],
    }
    return {
        "target_domain_controller": target_domain_controller,
        "item": item,
    }


def validate_concept_objects(ai_res: dict, target_domain_controller: dict, components: list) -> list:
    """Lightweight ISO 21434 content checks for the four TARA sheet objects.

    This does not change which objects are generated for a treatment; it only
    reports content that the model should fix before the TARA sheet is used.
    """
    warnings = []
    component_names = {
        str(c.get("name") or "").strip()
        for c in (components or [])
        if str(c.get("name") or "").strip()
    }
    component_ids = {
        str(c.get("id") or "").strip()
        for c in (components or [])
        if str(c.get("id") or "").strip()
    }
    component_keys = component_names | component_ids
    for idx, dec in enumerate(ai_res.get("risk_decisions", []) or []):
        treatment = str(dec.get("risk_treatment") or "").lower()
        prefix = f"risk_decisions[{idx}]"
        if "avoid" in treatment:
            if dec.get("cybersecurity_goal") or dec.get("cybersecurity_claim") or dec.get("requirements"):
                warnings.append(f"{prefix}: Avoid 应只填 item_change，其它对象应为空")
            continue

        requirements = dec.get("requirements") or []
        if "share" in treatment or "retain" in treatment or "accept" in treatment:
            for field in (
                "cybersecurity_claim", "claim_type", "residual_risk",
                "claim_justification", "claim_conditions", "claim_validity", "claim_owner",
            ):
                if not str(dec.get(field) or "").strip():
                    warnings.append(f"{prefix}: Claim 缺少 {field}")
            if requirements:
                warnings.append(f"{prefix}: Share/Retain 不应生成 item requirements")
            continue

        # Reduce
        for field in ("cybersecurity_goal", "security_property", "success_criteria"):
            if not str(dec.get(field) or "").strip():
                warnings.append(f"{prefix}: Goal 缺少 {field}")
        if not requirements:
            warnings.append(f"{prefix}: Reduce 缺少 requirements")
        for r_idx, req in enumerate(requirements):
            rprefix = f"{prefix}.requirements[{r_idx}]"
            for field in (
                "cybersecurity_requirement", "verification_method",
                "acceptance_criteria", "source_ids",
            ):
                if not req.get(field):
                    warnings.append(f"{rprefix}: Requirement 缺少 {field}")
            if str(req.get("allocated_to_device") or "").lower() == "yes":
                if not target_domain_controller.get("name"):
                    warnings.append(f"{rprefix}: allocated_to_device=yes 但缺少目标域控信息")
                allocated_to = str(req.get("allocated_to") or "").strip()
                if component_keys and allocated_to and allocated_to not in component_keys:
                    if not any(key in allocated_to or allocated_to in key for key in component_keys):
                        warnings.append(f"{rprefix}: allocated_to 未匹配 item.components: {allocated_to}")
    return warnings


def tara_ai_analysis_call(db: Session, stage: str, asset: Asset, prev_stages: dict) -> dict:
    """
    TARA 阶段大模型分析核心实现：集成 pyTara_V 标准业务逻辑、专家提示词与风险计算矩阵
    """
    settings = db.query(SystemSettings).first()
    if not settings or not settings.api_key or settings.api_key == "mock_test_key":
        print(f"[TARA-AI] 未配置大模型或为 Mock 模式，执行 Rules-based 降级算法 (Stage: {stage})")
        return mock_tara_ai_call(stage, asset, prev_stages)

    domain_name = asset.domain.name if asset.domain else "车载子系统"
    project_name = asset.domain.project.name if (asset.domain and asset.domain.project) else "车辆系统"
    project_desc = asset.domain.project.description if (asset.domain and asset.domain.project and asset.domain.project.description) else ""
    
    item_context = f"当前分析的子系统/域 (Domain) 是：{domain_name}，所属项目 (Project) 是：{project_name}"
    if project_desc:
        item_context += f"（项目描述：{project_desc}）"

    # 核心系统专家提示词 (基于 pyTara_V 设定)
    system_prompt = (
        f"你现在是拥有丰富经验的 ISO 21434 汽车网络安全专家。{item_context}。\n"
        "【术语与写作边界】\n"
        "- Damage Scenario：资产/安全属性受损后对车辆、人员、业务或隐私造成的影响；不描述攻击手段。\n"
        "- Threat Scenario：威胁源或攻击接口 + 被破坏的安全属性 + 攻击行为；不写具体实现方案。\n"
        "- Cybersecurity Goal：仅用于风险处置包含 Reduce 的场景；是保护资产免受特定 Threat Scenario 影响的顶层要求。必须关联 threat_id/damage_scenario_id/asset，写单一主安全属性、goal_statement、success_criteria、适用生命周期；技术中立，不写协议、算法、产品、阈值、具体控制手段；不复述 Threat，不写实现方案。一个 Goal 可以对应多个 Control/Requirement。\n"
        "- Cybersecurity Claim：用于 Share，或 Retain 基于 assumptions 的场景；是声明，不是 Control，不是 Requirement。必须写 claim_type、claim_statement、residual_risk、justification、conditions、validity、owner/approver、evidence；不得写“系统应……”，不得复制 Threat 或 Control；信息不足写 open_issue，不得编造。\n"
        "- Cybersecurity Control：控制类型（预防/检测/纠正）、实现类型（技术/操作）、控制目标、作用点、依赖的 item 功能/其他 Control、交互描述、关联 Goal/Claim；必须描述控制之间、控制与 item 功能之间的交互/依赖；不写具体协议/算法/产品型号。\n"
        "- Cybersecurity Requirement：保护对象 + 安全属性/控制目标 + 应/不应行为 + 适用条件 + 验证方法；必须写 requirement_scope（item / operational_environment / other_item）、owner、allocated_to、allocation_type、requirement_type（product/process/supplier）、verification_method、acceptance_criteria、source_ids；一条 Requirement 只能有一个保护对象、一个行为、一个可验证结果；多个独立“应/不应/不得”必须拆条；技术中立。\n"
        "- Allocated to Device：只有 Requirement 由 target_domain_controller 或其 item.components 中的组件实现时填 'yes'；由运行环境、其他域控/ECU、其他 item、供应商、后台或流程实现时填 'no'；信息不足填 'no' 并在 open_issue 说明；禁止默认 yes。\n"
        "1. 所有描述性内容、场景描述、路径、理由、安全目标和安全控制/需求必须使用中英文双语对照输出，格式为“中文描述 / English translation” (例如：“由于系统受损，功能失效。 / Impaired functions due to system compromise.”)，保证翻译的专业性与准确性；\n"
        "2. 绝对不使用 Markdown 代码块；\n"
        "3. 绝对不包含任何思考过程、推理步骤或解释说明；\n"
        "4. 只返回最终的 JSON 格式结果；\n"
        "5. 确保JSON格式正确，并完全符合要求；\n"
        "6. 不要返回思考过程；\n"
        "7. 如果输入需求中包含具体协议、算法、端口或数值，输出必须抽象为能力要求，不得原样保留。"
    )

    url = f"{settings.api_base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json"
    }

    def call_llm(msg: str, prompt: str, response_model: Any = None) -> dict:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"输入的资产信息如下：{msg}"},
            {"role": "user", "content": prompt}
        ]
        
        payload = {
            "model": settings.model_name,
            "messages": messages,
        }
        
        if response_model:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "strict": True,
                    "schema": response_model.model_json_schema()
                }
            }
        else:
            payload["response_format"] = {"type": "json_object"}
            
        print(f"[TARA-AI] 发起大模型服务请求 ({settings.model_name}) 执行 API 调用...")
        
        from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
        
        @retry(
            stop=stop_after_attempt(5),
            wait=wait_exponential(multiplier=1, min=2, max=10),
            retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.RequestError)),
            reraise=True
        )
        def _execute_request():
            return post_llm_request_with_retry(url, headers, payload)
            
        try:
            resp = _execute_request()
            resp_data = resp.json()
            raw_text = resp_data["choices"][0]["message"]["content"]
            parsed = clean_and_parse_json(raw_text)
            
            if response_model:
                try:
                    validated = response_model.model_validate(parsed)
                    return validated.model_dump(by_alias=True)
                except Exception as val_err:
                    print(f"[TARA-AI] Pydantic 校验失败 ({val_err})，尝试发起自动修复...")
                    repair_messages = messages + [
                        {"role": "assistant", "content": raw_text},
                        {"role": "user", "content": f"上一次返回的 JSON 数据校验未通过，校验错误信息如下：\n{val_err}\n请修正该 JSON 数据，确保完全符合 Schema 规范且格式正确。"}
                    ]
                    payload_repair = {
                        "model": settings.model_name,
                        "messages": repair_messages,
                        "response_format": payload["response_format"]
                    }
                    
                    @retry(
                        stop=stop_after_attempt(3),
                        wait=wait_exponential(multiplier=1, min=2, max=5),
                        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.RequestError)),
                        reraise=True
                    )
                    def _execute_repair():
                        return post_llm_request_with_retry(url, headers, payload_repair)
                        
                    resp_rep = _execute_repair()
                    raw_text_rep = resp_rep.json()["choices"][0]["message"]["content"]
                    parsed_rep = clean_and_parse_json(raw_text_rep)
                    validated_rep = response_model.model_validate(parsed_rep)
                    return validated_rep.model_dump(by_alias=True)
            return parsed
        except Exception as e:
            if response_model and "response_format" in payload:
                print(f"[TARA-AI] json_schema 格式请求失败 ({e})，尝试回退到 json_object 格式...")
                payload["response_format"] = {"type": "json_object"}
                messages.append({
                    "role": "user", 
                    "content": f"请务必按照以下 JSON Schema 格式回复，不要包含任何 markdown 或其他文本：\n{json.dumps(response_model.model_json_schema(), ensure_ascii=False)}"
                })
                try:
                    resp = _execute_request()
                    resp_data = resp.json()
                    raw_text = resp_data["choices"][0]["message"]["content"]
                    parsed = clean_and_parse_json(raw_text)
                    try:
                        validated = response_model.model_validate(parsed)
                        return validated.model_dump(by_alias=True)
                    except Exception as val_err:
                        print(f"[TARA-AI] fallback 模式 Pydantic 校验失败 ({val_err})，尝试发起自动修复...")
                        repair_messages = messages + [
                            {"role": "assistant", "content": raw_text},
                            {"role": "user", "content": f"上一次返回的 JSON 数据校验未通过，校验错误信息如下：\n{val_err}\n请修正该 JSON 数据，确保完全符合 Schema 规范且格式正确。"}
                        ]
                        payload_repair = {
                            "model": settings.model_name,
                            "messages": repair_messages,
                            "response_format": {"type": "json_object"}
                        }
                        
                        @retry(
                            stop=stop_after_attempt(3),
                            wait=wait_exponential(multiplier=1, min=2, max=5),
                            retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.RequestError)),
                            reraise=True
                        )
                        def _execute_repair_fallback():
                            return post_llm_request_with_retry(url, headers, payload_repair)
                            
                        resp_rep = _execute_repair_fallback()
                        raw_text_rep = resp_rep.json()["choices"][0]["message"]["content"]
                        parsed_rep = clean_and_parse_json(raw_text_rep)
                        validated_rep = response_model.model_validate(parsed_rep)
                        return validated_rep.model_dump(by_alias=True)
                except Exception as e2:
                    raise e2
            raise e

    try:
        asset_info_dict = {
            "asset_id": f"ID{asset.id}",
            "asset_name": asset.name,
            "communication_protocol": asset.protocol or "无",
            "asset_type": asset.asset_type.capitalize() if asset.asset_type else "Hardware",
            "remarks": asset.description or "无"
        }
        asset_info_dict.update(build_item_context(db, asset))

        asset_type_lower = asset.asset_type.lower() if asset.asset_type else "hardware"

        # 针对不同资产类型的威胁分析与建模指南 (Threat modeling guidelines per asset type)
        type_guideline = ""
        if asset_type_lower == "hardware":
            type_guideline = (
                "\n【硬件安全分析指南 (Hardware Security Analysis Guidelines)】:\n"
                "对于硬件资产，威胁建模时需重点识别以下物理与底层相关的威胁场景和攻击路径：\n"
                "- 物理拆解与微探测 (Physical Tampering/Probing)：例如非法打开外壳以获取内部总线信号、芯片管脚/测试点暴露导致被物理探针接触。\n"
                "- 调试接口越权 (Debug Port Misuse)：例如 JTAG/SWD 等物理调试接口未能在量产时永久熔丝/禁用，或者缺乏安全访问认证，导致攻击者直接读取/篡改固件或注入代码。\n"
                "- 侧信道与故障注入 (Side-Channel & Fault Injection)：例如利用功耗分析或电磁泄漏窃取密钥，通过电压/时钟/温度/激光注入干扰正常代码执行（如绕过密码校验）。\n"
                "- 外部存储篡改与克隆 (Storage Interception/Cloning)：例如外置 Flash/eMMC/UFS 存储中的固件被直接读取或重写，或者假冒/克隆芯片进入供应链。\n"
                "- 密钥注入与生命周期缺陷 (Provisioning & Lifecycle)：例如产线密钥注入明文泄露，固件版本防回滚失效导致被降级攻击。\n"
            )
        elif asset_type_lower == "software":
            type_guideline = (
                "\n【软件安全分析指南 (Software Security Analysis Guidelines)】:\n"
                "对于软件/系统资产，威胁建模时需重点识别软件及操作系统层面的威胁场景和攻击路径：\n"
                "- 内存破坏与执行篡改：例如缓冲区溢出、内存泄漏、控制流篡改、恶意代码注入与执行。\n"
                "- 输入校验缺陷：外部输入数据（如网络协议报文、诊断参数、文件导入、外部API参数）未进行严格校验，导致栈/堆溢出、格式字符串执行或越界读取。\n"
                "- 访问控制缺陷与权限提升：例如未授权访问敏感服务接口/API、进程间通信(IPC)越权、本地或远程特权提升。\n"
                "- 中间件安全缺陷：SOME/IP、DDS 等车载中间件明文通信且缺乏对端身份验证与服务权限控制，导致消息被伪造注入或越权调用。\n"
                "- 软件生命周期与更新漏洞：例如固件更新包未经验证、安全启动链被绕过（如绕过各级数字签名校验）、固件被降级回滚至含有旧漏洞的版本。\n"
                "- 系统配置与服务过载：例如后台诊断端口或控制台未授权开放、保留无关高危服务、访问控制（如SELinux）未配置或在生产中处于Permissive宽容模式导致攻击未被阻断。\n"
                "- 日志审计审计缺失与漏洞滞后：例如安全启动校验失败、用户登录爆破、越权操作等关键事件未记录日志，或日志可被轻易篡改；使用了已知含有老旧漏洞且长期未修复的第三方组件库。\n"
                "- 代码逆向与反汇编：交付的二进制文件未去除调试信息或进行混淆加固，导致核心算法、AI模型或控制逻辑被轻易逆向分析、破解或篡改。\n"
            )
        elif asset_type_lower == "communication":
            type_guideline = (
                "\n【通信安全分析指南 (Communication Security Analysis Guidelines)】:\n"
                "对于通信资产，威胁建模时需重点识别通信链路与协议层面的威胁场景和攻击路径：\n"
                "- 数据窃听与中间人篡改：例如未加密通信信道被窃听、中间人攻击篡改数据包、伪造CAN/Ethernet控制报文注入。\n"
                "- 重放与伪造攻击：例如未加入单调计数器/新鲜度值的协议消息被捕获并重复发送执行动作。\n"
                "- 拒绝服务 (DoS) 与总线泛洪：例如总线泛洪攻击(CAN-DoS)、连接拒绝服务导致车载网络挂起。\n"
            )
        elif asset_type_lower == "data":
            type_guideline = (
                "\n【数据安全分析指南 (Data Security Analysis Guidelines)】:\n"
                "对于数据/密钥资产，威胁建模时需重点识别敏感数据存储、流转及密码学应用中的威胁场景和攻击路径：\n"
                "- 密码算法破译与弱熵源：使用已被破解的弱算法（如MD5, SHA1, DES, 弱RSA），或由于伪随机数生成器缺陷导致密钥可被预测。\n"
                "- 密钥泄露与非安全存储：密钥、私钥、证书或机密参数在软件代码或常规Flash分区中以明文形式存储，导致被提取泄露。\n"
                "- 越权调用密钥与重放：密钥在使用时缺乏严格访问控制，低权限进程可直接越权调用HSM进行加解密/签名，或者缺乏工作密钥定期轮换机制导致密文被长期收集重放。\n"
                "- 传输与物理脱机篡改：敏感数据明文传输被窃听，或外部存储介质（如Flash）中的关键配置文件被脱机物理篡改，缺乏完整性保护。\n"
                "- 凭证强度缺陷与残留泄露：系统密码复杂度过低可被爆破；临时固件/数据包解密密钥使用后未从内存擦除，或退役/销毁时未执行物理置零。\n"
                "- 个人数据与隐私泄露：未经脱敏和加密的用户个人信息（如人脸、车牌、行车轨迹视频等）明文存储或违规向外传输，或未遵循本地化存储法规限制导致数据跨境流失。\n"
                "- 软硬件标识篡改：版本号、VIN号、RxSWIN（法规软件识别码）被恶意篡改，导致软件升级合规状态失效。\n"
                "- 传感器数据劫持：传感器上报数据在通信总线上被攻击者恶意篡改或注入虚假物理状态数据。\n"
            )

        # 针对不同资产类型的网络安全需求与控制措施制定指南 (Security requirements formulation guidelines per asset type)
        req_guideline = ""
        if asset_type_lower == "hardware":
            req_guideline = (
                "\n【硬件安全需求维度 (Hardware Security Requirement Dimensions)】:\n"
                "请结合威胁场景选择适用维度，只写“应具备什么能力/达到什么结果”，不要写具体芯片、封装、传感器型号或实现电路：\n"
                "1. 物理防拆与安全响应：应能检测外壳开启、物理接触或探测事件，并按预定义策略记录、告警、锁定或进入安全状态。\n"
                "2. 调试/编程接口保护：量产应不可逆禁用；确需保留时，应实施强身份认证、最小权限和审计。\n"
                "3. 故障注入与侧信道防护：应具备电压/时钟/温度异常监测和行业认可的侧信道防护能力。\n"
                "4. 外部存储保护：外部非易失存储中的敏感数据应使用设备唯一密钥进行硬件绑定加密，防止脱机读取/克隆。\n"
                "5. 信任根与防回滚：应基于只读信任根建立安全启动，并提供安全版本防回滚能力。\n"
                "6. 安全配给与溯源：产线密钥/证书注入应受安全硬件保护，并与部件唯一标识建立可追溯记录。\n"
                "7. 随机数与熵源：应为密码学操作提供符合项目认可标准的高质量随机数/熵源。\n"
                "写作约束：禁止写死芯片型号、封装型号、具体传感器型号、具体算法/协议/端口/数值阈值；这些留到设计阶段。\n"
            )
        elif asset_type_lower == "software":
            req_guideline = (
                "\n【软件与系统安全需求维度 (Software & System Security Requirement Dimensions)】:\n"
                "请结合威胁场景选择适用维度，只写“应具备什么能力/达到什么结果”，不要写具体协议、算法、产品版本或数值阈值：\n"
                "1. 分级安全启动：应建立从只读信任根到应用层的逐级完整性校验链，校验失败应阻止启动并记录事件。\n"
                "2. 安全更新：更新包应验证来源真实性与完整性；应支持失败恢复和临时更新数据清理。\n"
                "3. 防回滚：应使用绑定安全硬件的安全版本机制，拒绝低于当前安全基线的版本。\n"
                "4. 访问控制与认证：应实施最小权限、基于角色的授权和强身份认证；关键操作应支持会话超时、失败锁定和审计。\n"
                "5. 运行时加固与隔离：应关闭非必要服务/端口，实施进程/权限隔离和运行时完整性监控。\n"
                "6. 输入合法性：应对所有外部输入实施类型、长度、边界和合法性校验，拒绝畸形输入并记录。\n"
                "7. 中间件与服务鉴权：服务化/进程间通信应验证对端身份和调用权限。\n"
                "8. 二进制与后门防护：量产版本应移除调试能力、剥离调试符号、实施抗逆向加固，不得保留未授权后门。\n"
                "9. 日志与监控：应记录关键安全事件，日志应防篡改、可追溯并满足留存要求。\n"
                "10. 漏洞管理：应建立第三方组件/漏洞库检索和高危漏洞闭环机制。\n"
                "11. 安全失效：检测到持续攻击或安全组件故障时，应安全降级/关闭非必要接口并记录事件。\n"
                "12. 安全开发：应遵循安全编码规范，使用静态分析/代码审查，并在发布前关闭高危缺陷。\n"
                "13. 产线配给：初始密钥/证书应在受控产线环境中注入，并保留审计记录。\n"
                "写作约束：禁止写死协议名+版本、算法名+密钥长度、端口号、具体毫秒/次数阈值；这些留到设计阶段。\n"
            )
        elif asset_type_lower == "communication":
            req_guideline = (
                "\n【通信安全需求维度 (Communication Security Requirement Dimensions)】:\n"
                "请结合威胁场景选择适用维度，只写“应具备什么能力/达到什么结果”，不要写具体协议版本、算法、端口或数值阈值：\n"
                "1. 传输与应用层保护：通信应使用经行业认可的安全通道；敏感数据应具备端到端机密性和完整性保护。\n"
                "2. 报文真实性与新鲜度：关键总线/服务报文应具备来源真实性、完整性和防重放能力。\n"
                "3. 通信可用性：应具备异常流量检测、限流、丢弃/阻断和安全降级能力。\n"
                "4. 诊断与服务访问：诊断/服务接口应实施强身份认证、角色授权和审计。\n"
                "5. 网络隔离：应通过通信矩阵白名单、分区/隔离和访问控制限制非授权通信。\n"
                "6. 身份与证书：通信对端应基于证书或等效凭证完成身份认证，并校验证书有效性。\n"
                "写作约束：禁止写死协议名+版本、算法名、端口号、具体毫秒/速率阈值；这些留到设计阶段。\n"
            )
        elif asset_type_lower == "data":
            req_guideline = (
                "\n【数据与密码安全需求维度 (Data & Cryptographic Security Requirement Dimensions)】:\n"
                "请结合威胁场景选择适用维度，只写“应具备什么能力/达到什么结果”，不要写具体算法、认证标准、法规编号或数值阈值：\n"
                "1. 密码算法策略：应使用经行业认可/项目合规基线批准的高强度密码算法，禁用已知弱算法。\n"
                "2. 密钥保护：密钥应存储在隔离安全硬件或受保护密码服务中，不得以明文暴露给普通软件。\n"
                "3. 随机数：密钥/IV/挑战值应使用符合项目认可标准的随机数/熵源，并具备健康检测。\n"
                "4. 密钥生命周期：应覆盖生成、注入、使用、更新、轮换、吊销、销毁和审计。\n"
                "5. 访问控制：密钥和敏感数据应实施基于角色/授权的最小权限访问。\n"
                "6. 安全擦除：易失性敏感材料在使用完毕或检测到防拆事件时应安全零化/销毁。\n"
                "7. 数据最小化与分类分级：应遵循最小化、目的限制、分类分级和保留期限要求。\n"
                "8. 隐私脱敏：个人数据在存储、导出、上传或显示前应实施去标识化/匿名化/脱敏。\n"
                "9. 标识完整性：VIN、软件版本、RxSWIN 等标识应具备防篡改和完整性校验。\n"
                "10. 关键配置保护：影响行车安全的标定/配置应具备授权访问和密码学完整性保护。\n"
                "11. 跨境与上传：敏感数据上传/出境应符合项目适用的数据合规要求。\n"
                "写作约束：禁止写死算法名+密钥长度、认证标准编号、法规编号、具体位数/次数/天数阈值；这些留到设计阶段。\n"
            )

        if stage == "stage1":
            msg_dict = {"asset_cybersecurity_attribute": {"asset_info": asset_info_dict}}
            msg = json.dumps(msg_dict, ensure_ascii=False)
            prompt = (
                "请根据以上资产asset_id, asset_name, asset_type, communication_protocol, remarks，"
                "评估资产是否应该被赋于以下安全属性：\n"
                "Authenticity, Integrity, Non-repudiation, Confidentiality, Availability, Authorization, Privacy。\n"
                "评分标准：0-不相关，1-低相关，2-中等相关，3-高相关，4-关键相关，5-必需属性。"
            )
            ai_res = call_llm(msg, prompt, Stage1Output)
            
            selected = []
            for attr, score in ai_res.items():
                try: s = int(score)
                except: s = 0
                if s >= 2 and attr in ["Authenticity", "Integrity", "Non-repudiation", "Confidentiality", "Availability", "Authorization", "Privacy"]:
                    selected.append(attr)

            def get_rating(score):
                try: s = int(score)
                except: s = 0
                if s >= 4: return "High"
                if s >= 2: return "Medium"
                if s >= 1: return "Low"
                return "None"

            return {
                "attributes": ai_res,
                "selected_attributes": selected,
                "authenticity": get_rating(ai_res.get("Authenticity", 0)),
                "integrity": get_rating(ai_res.get("Integrity", 0)),
                "non-repudiation": get_rating(ai_res.get("Non-repudiation", 0)),
                "confidentiality": get_rating(ai_res.get("Confidentiality", 0)),
                "availability": get_rating(ai_res.get("Availability", 0)),
                "authorization": get_rating(ai_res.get("Authorization", 0)),
                "privacy": get_rating(ai_res.get("Privacy", 0)),
                "description": f"已选定高相关安全属性: {', '.join(selected)} 进行后续分析。 / Selected highly relevant security attributes: {', '.join(selected)} for subsequent analysis." if selected else "无高相关(分数>=2)安全属性，未选择属性。 / No highly relevant (score >= 2) security attributes, none selected."
            }

        elif stage == "stage2":
            selected_attrs = prev_stages.get("stage1", {}).get("selected_attributes", ["Confidentiality", "Integrity"])
            if not selected_attrs:
                selected_attrs = ["Confidentiality", "Integrity"]
                
            msg_dict = {
                "asset_info": asset_info_dict,
                "selected_attributes": selected_attrs
            }
            msg = json.dumps(msg_dict, ensure_ascii=False)
            prompt = (
                f"请根据资产信息及选定的高相关安全属性: {selected_attrs}，为该子系统/资产进行网络安全威胁建模与分析（包括损害场景、威胁场景、攻击路径及评级）。\n"
                "分析要求：\n"
                "1. 针对每个已分配的安全属性分别识别 1 到 2 个损害场景 (Damage Scenarios) 及其四个维度的影响评级 (Impact Ratings)。影响级别取值只能是: Negligible, Moderate, Major, Severe。评估维度说明：\n"
                "   - Safety (S/安全影响): 影响车辆物理控制，危及车内人员或行人生命安全；\n"
                "   - Financial (F/财务影响): 造成直接经济损失（如车辆被盗、召回、合规罚款等）；\n"
                "   - Operational (O/运营影响): 导致车辆或零部件功能降级、失效或不可用；\n"
                "   - Privacy (P/隐私影响): 泄露用户隐私、位置或敏感个人识别数据（如VIN、行车轨迹）。\n"
                "2. 针对每个损害场景，识别可能导致它的 1 到 2 个威胁场景 (Threat Scenarios)，被破坏的安全属性必须与对应损害场景的属性一致。威胁场景描述应明确指出威胁源（或攻击接口）与受损属性。\n"
                "3. 针对每个威胁场景，设计 1 到 2 条具体的攻击路径 (Attack Paths)，并对攻击路径的各个要素进行打分。打分选项只能使用系统规定的标准选项：\n"
                "   - time_consuming: no_more_than_1d, no_more_than_1w, no_more_than_1m, no_more_than_6m, more_than_6m\n"
                "   - expertise: layman, proficient, expert, multiple_expert\n"
                "   - knowledge_about_toe: public, restricted, confidential, strictly_confidential\n"
                "   - window_of_opportunity: unlimited, easy, moderate, difficult\n"
                "   - equipment: standard, specialized, bespoke, multiple_bespoke\n"
                "4. 所有损害场景、威胁场景、攻击步骤的文本必须使用中英文双语对照输出，格式为：“中文描述 / English translation.”。\n"
                f"{type_guideline}"
            )
            ai_res = call_llm(msg, prompt, Stage23Output)
            
            impact_map = {"negligible": 0, "moderate": 1, "major": 2, "severe": 3}
            processed_ds = []
            for ds in ai_res.get("damage_scenarios", []):
                attr = ds.get("attribute")
                # Overwrite/generate unique SN programmatically in backend
                unique_sn = generate_unique_ds_sn()
                text = ds.get("damage_scenario")
                safety_str = ds.get("safety", "Negligible")
                financial_str = ds.get("financial", "Negligible")
                operational_str = ds.get("operational", "Negligible")
                privacy_str = ds.get("privacy", "Negligible")
                
                s_num = impact_map.get(str(safety_str).lower().strip(), 0)
                f_num = impact_map.get(str(financial_str).lower().strip(), 0)
                o_num = impact_map.get(str(operational_str).lower().strip(), 0)
                p_num = impact_map.get(str(privacy_str).lower().strip(), 0)
                overall_impact = max(s_num, f_num, o_num, p_num)
                
                processed_ds.append({
                    "attribute": attr,
                    "damage_scenario_sn": unique_sn,
                    "damage_scenario": text,
                    "impact_rating": {
                        "safety": safety_str,
                        "financial": financial_str,
                        "operational": operational_str,
                        "privacy": privacy_str
                    },
                    "overall_impact": overall_impact,
                    "_threat_scenarios": ds.get("threat_scenarios", [])
                })
                
            if not processed_ds:
                processed_ds.append({
                    "attribute": "Integrity",
                    "damage_scenario_sn": generate_unique_ds_sn(),
                    "damage_scenario": f"由于 {asset.name} 遭受篡改，导致 {domain_name} 子系统功能受损。 / Due to tampering of {asset.name}, {domain_name} subsystem functions are impaired.",
                    "impact_rating": {"safety": "Moderate", "financial": "Moderate", "operational": "Moderate", "privacy": "Negligible"},
                    "overall_impact": 1,
                    "_threat_scenarios": []
                })

            max_s, max_f, max_o, max_p = 0, 0, 0, 0
            all_ds_texts = []
            for ds in processed_ds:
                all_ds_texts.append(ds["damage_scenario"])
                r = ds["impact_rating"]
                max_s = max(max_s, impact_map.get(str(r["safety"]).lower().strip(), 0))
                max_f = max(max_f, impact_map.get(str(r["financial"]).lower().strip(), 0))
                max_o = max(max_o, impact_map.get(str(r["operational"]).lower().strip(), 0))
                max_p = max(max_p, impact_map.get(str(r["privacy"]).lower().strip(), 0))
                
            overall_impact = max(max_s, max_f, max_o, max_p)
            
            stage2_final = {
                "damage_scenarios": [{k: v for k, v in ds.items() if k != "_threat_scenarios"} for ds in processed_ds],
                "damage_scenario": "; ".join(all_ds_texts[:2]) if all_ds_texts else f"由于安全属性泄露或篡改，导致 {domain_name} 子系统故障。 / Cybersecurity attribute breach or tampering leads to failure of the {domain_name} subsystem.",
                "impact_rating": {"safety": max_s, "financial": max_f, "operational": max_o, "privacy": max_p},
                "overall_impact": overall_impact
            }
            
            # Store the raw threats for stage3
            stage2_final["_consolidated_stage3_data"] = processed_ds
            return stage2_final

        elif stage == "stage3":
            stage2_out = prev_stages.get("stage2", {})
            consolidated_data = stage2_out.get("_consolidated_stage3_data")
            
            if consolidated_data:
                threat_scenarios = []
                all_attack_paths = []
                max_feasibility = "Very Low"
                feasibility_order = {"Very Low": 1, "Low": 2, "Medium": 3, "High": 4}
                
                ts_idx = 1
                for ds in consolidated_data:
                    raw_threats = ds.get("_threat_scenarios", [])
                    if not raw_threats:
                        raw_threats = [{
                            "threat_scenario": f"针对 {asset.name} 的 {ds['attribute']} 属性的威胁。 / Threat against {ds['attribute']} attribute of {asset.name}.",
                            "attribute": ds["attribute"],
                            "attack_paths": []
                        }]
                        
                    for raw_ts in raw_threats:
                        ts_id = f"TS_{ds['attribute']}_{ts_idx}"
                        ts_idx += 1
                        
                        ts_value = raw_ts.get("threat_scenario")
                        ts_attr = raw_ts.get("attribute", ds["attribute"])
                        
                        paths_list = []
                        ts_feasibility = "Very Low"
                        
                        raw_paths = raw_ts.get("attack_paths", [])
                        if not raw_paths:
                            raw_paths = [{
                                "attack_path": f"利用接口进行数据篡改或重放攻击。 / Exploit interface to perform data tampering or replay attack.",
                                "time_consuming": "no_more_than_1w",
                                "expertise": "proficient",
                                "knowledge_about_toe": "restricted",
                                "window_of_opportunity": "easy",
                                "equipment": "standard"
                            }]
                            
                        for raw_p in raw_paths:
                            ap_text = raw_p.get("attack_path")
                            tc = raw_p.get("time_consuming", "no_more_than_1w")
                            exp = raw_p.get("expertise", "proficient")
                            kn = raw_p.get("knowledge_about_toe", "restricted")
                            win = raw_p.get("window_of_opportunity", "easy")
                            eq = raw_p.get("equipment", "standard")
                            
                            total_diff = 0
                            total_diff += get_time_consuming_points(tc)
                            total_diff += get_expertise_points(exp)
                            total_diff += get_knowledge_points(kn)
                            total_diff += get_window_points(win)
                            total_diff += get_equipment_points(eq)
                            
                            feas = calculate_feasibility(total_diff)
                            
                            paths_list.append({
                                "attack_path": ap_text,
                                "time_consuming": tc,
                                "expertise": exp,
                                "knowledge_about_toe": kn,
                                "window_of_opportunity": win,
                                "equipment": eq,
                                "difficulty": total_diff,
                                "feasibility": feas
                            })
                            if feasibility_order.get(feas, 1) > feasibility_order.get(ts_feasibility, 1):
                                ts_feasibility = feas
                                
                        threat_scenarios.append({
                            "attribute": ts_attr,
                            "damage_scenario_sn": ds["damage_scenario_sn"],
                            "damage_scenario": ds["damage_scenario"],
                            "overall_impact": ds["overall_impact"],
                            "threat_id": ts_id,
                            "threat_scenario": ts_value,
                            "attack_paths": paths_list,
                            "final_feasibility": ts_feasibility
                        })
                        
                        for p in paths_list:
                            all_attack_paths.append({
                                "path_id": f"P{len(all_attack_paths) + 1}",
                                "method": p["attack_path"],
                                "feasibility": p["feasibility"]
                            })
                            if feasibility_order.get(p["feasibility"], 1) > feasibility_order.get(max_feasibility, 1):
                                max_feasibility = p["feasibility"]
                                
                all_ts_texts = [ts["threat_scenario"] for ts in threat_scenarios]
                return {
                    "threat_scenarios": threat_scenarios,
                    "threat_scenario": "; ".join(all_ts_texts[:2]) if all_ts_texts else "攻击者操纵相关接口以注入恶意数据流。 / Attacker manipulates relevant interfaces to inject malicious data streams.",
                    "attack_paths": all_attack_paths,
                    "final_feasibility": max_feasibility
                }
            else:
                damage_scenarios = prev_stages.get("stage2", {}).get("damage_scenarios", [])
                if not damage_scenarios:
                    damage_scenarios = [{
                        "attribute": "Integrity",
                        "damage_scenario_sn": generate_unique_ds_sn(),
                        "damage_scenario": prev_stages.get("stage2", {}).get("damage_scenario", "车载子系统故障"),
                        "overall_impact": prev_stages.get("stage2", {}).get("overall_impact", 1)
                    }]
                
                msg_dict = {
                    "asset_info": asset_info_dict,
                    "damage_scenarios": damage_scenarios
                }
                msg = json.dumps(msg_dict, ensure_ascii=False)
                prompt = (
                    "请针对以上已识别的损害场景 (damage_scenarios)，进行网络安全威胁建模与分析（包括威胁场景、攻击路径及打分评估）。\n"
                    "分析要求：\n"
                    "1. 针对每个损害场景，识别可能导致它的 1 到 2 个威胁场景 (Threat Scenarios)，被破坏的安全属性与对应的损害场景一致。\n"
                    "2. 针对每个威胁场景，设计 1 到 2 条具体的攻击路径 (Attack Paths)，并对攻击路径的各个要素进行打分。打分选项只能使用系统规定的标准选项：\n"
                    "   - time_consuming: no_more_than_1d, no_more_than_1w, no_more_than_1m, no_more_than_6m, more_than_6m\n"
                    "   - expertise: layman, proficient, expert, multiple_expert\n"
                    "   - knowledge_about_toe: public, restricted, confidential, strictly_confidential\n"
                    "   - window_of_opportunity: unlimited, easy, moderate, difficult\n"
                    "   - equipment: standard, specialized, bespoke, multiple_bespoke\n"
                    "3. 所有威胁场景、攻击步骤的描述必须使用中英文双语对照输出，格式为：“中文描述 / English translation.”。\n"
                    f"{type_guideline}"
                )
                ai_res = call_llm(msg, prompt, Stage23Output)
                
                threat_scenarios = []
                all_attack_paths = []
                max_feasibility = "Very Low"
                feasibility_order = {"Very Low": 1, "Low": 2, "Medium": 3, "High": 4}
                
                ts_idx = 1
                for ds in damage_scenarios:
                    ds_sn = ds.get("damage_scenario_sn")
                    matched_ds = None
                    for res_ds in ai_res.get("damage_scenarios", []):
                        if res_ds.get("damage_scenario_sn") == ds_sn or res_ds.get("attribute") == ds.get("attribute"):
                            matched_ds = res_ds
                            break
                            
                    raw_threats = []
                    if matched_ds:
                        raw_threats = matched_ds.get("threat_scenarios", [])
                    if not raw_threats:
                        raw_threats = [{
                            "threat_scenario": f"针对 {asset.name} 的 {ds['attribute']} 属性的威胁。 / Threat against {ds['attribute']} attribute of {asset.name}.",
                            "attribute": ds["attribute"],
                            "attack_paths": []
                        }]
                        
                    for raw_ts in raw_threats:
                        ts_id = f"TS_{ds['attribute']}_{ts_idx}"
                        ts_idx += 1
                        
                        ts_value = raw_ts.get("threat_scenario")
                        ts_attr = raw_ts.get("attribute", ds["attribute"])
                        
                        paths_list = []
                        ts_feasibility = "Very Low"
                        
                        raw_paths = raw_ts.get("attack_paths", [])
                        if not raw_paths:
                            raw_paths = [{
                                "attack_path": f"利用接口进行数据篡改或重放攻击。 / Exploit interface to perform data tampering or replay attack.",
                                "time_consuming": "no_more_than_1w",
                                "expertise": "proficient",
                                "knowledge_about_toe": "restricted",
                                "window_of_opportunity": "easy",
                                "equipment": "standard"
                            }]
                            
                        for raw_p in raw_paths:
                            ap_text = raw_p.get("attack_path")
                            tc = raw_p.get("time_consuming", "no_more_than_1w")
                            exp = raw_p.get("expertise", "proficient")
                            kn = raw_p.get("knowledge_about_toe", "restricted")
                            win = raw_p.get("window_of_opportunity", "easy")
                            eq = raw_p.get("equipment", "standard")
                            
                            total_diff = 0
                            total_diff += get_time_consuming_points(tc)
                            total_diff += get_expertise_points(exp)
                            total_diff += get_knowledge_points(kn)
                            total_diff += get_window_points(win)
                            total_diff += get_equipment_points(eq)
                            
                            feas = calculate_feasibility(total_diff)
                            
                            paths_list.append({
                                "attack_path": ap_text,
                                "time_consuming": tc,
                                "expertise": exp,
                                "knowledge_about_toe": kn,
                                "window_of_opportunity": win,
                                "equipment": eq,
                                "difficulty": total_diff,
                                "feasibility": feas
                            })
                            if feasibility_order.get(feas, 1) > feasibility_order.get(ts_feasibility, 1):
                                ts_feasibility = feas
                                
                        threat_scenarios.append({
                            "attribute": ts_attr,
                            "damage_scenario_sn": ds["damage_scenario_sn"],
                            "damage_scenario": ds["damage_scenario"],
                            "overall_impact": ds["overall_impact"],
                            "threat_id": ts_id,
                            "threat_scenario": ts_value,
                            "attack_paths": paths_list,
                            "final_feasibility": ts_feasibility
                        })
                        
                        for p in paths_list:
                            all_attack_paths.append({
                                "path_id": f"P{len(all_attack_paths) + 1}",
                                "method": p["attack_path"],
                                "feasibility": p["feasibility"]
                            })
                            if feasibility_order.get(p["feasibility"], 1) > feasibility_order.get(max_feasibility, 1):
                                max_feasibility = p["feasibility"]
                                
                all_ts_texts = [ts["threat_scenario"] for ts in threat_scenarios]
                return {
                    "threat_scenarios": threat_scenarios,
                    "threat_scenario": "; ".join(all_ts_texts[:2]) if all_ts_texts else "攻击者操纵相关接口以注入恶意数据流。 / Attacker manipulates relevant interfaces to inject malicious data streams.",
                    "attack_paths": all_attack_paths,
                    "final_feasibility": max_feasibility
                }

        elif stage == "stage4":
            threat_scenarios = prev_stages.get("stage3", {}).get("threat_scenarios", [])
            if not threat_scenarios:
                threat_scenarios = [{
                    "attribute": "Integrity",
                    "threat_id": "TS_Integrity_1",
                    "threat_scenario": prev_stages.get("stage3", {}).get("threat_scenario", "威胁场景"),
                    "overall_impact": prev_stages.get("stage2", {}).get("overall_impact", 1),
                    "final_feasibility": prev_stages.get("stage3", {}).get("final_feasibility", "Medium")
                }]
                
            risk_matrix = {
                "verylow": [1, 1, 1, 2],
                "low": [1, 2, 2, 3],
                "medium": [1, 2, 3, 4],
                "high": [1, 3, 4, 5],
            }
            
            msg_dict = {
                "asset_info": asset_info_dict,
                "target_domain_controller": asset_info_dict.get("target_domain_controller", {}),
                "item": asset_info_dict.get("item", {}),
                "assumptions": prev_stages.get("stage3", {}).get("assumptions", []),
                "existing_controls": prev_stages.get("stage3", {}).get("existing_controls", []),
                "policy": {
                    "verification_methods": ["测试", "分析", "检查", "演示"],
                    "acceptance_criteria_rule": "必须是可判定的通过/失败条件，不能写“满足安全要求”",
                    "cal_method": "如项目使用 CAL，按 Annex E 评估；否则留空",
                    "language": "CN/EN",
                },
                "threat_scenarios": []
            }
            for ts in threat_scenarios:
                impact_val = ts.get("overall_impact", 1)
                feasibility_key = str(ts.get("final_feasibility", "Medium")).lower().replace(" ", "")
                risk_val = risk_matrix.get(feasibility_key, [1, 2, 3, 4])[min(max(impact_val, 0), 3)]
                msg_dict["threat_scenarios"].append({
                    "threat_id": ts.get("threat_id"),
                    "threat_scenario": ts.get("threat_scenario"),
                    "attribute": ts.get("attribute"),
                    "damage_scenario": ts.get("damage_scenario"),
                    "damage_scenario_sn": ts.get("damage_scenario_sn", ""),
                    "final_feasibility": ts.get("final_feasibility"),
                    "overall_impact": ts.get("overall_impact"),
                    "risk_value": risk_val,
                    "source_ids": {
                        "asset": f"ID{asset.id}",
                        "domain": f"DOMAIN-{asset.domain_id}",
                        "threat": ts.get("threat_id", ""),
                        "damage": ts.get("damage_scenario_sn", ""),
                    },
                })
            
            msg = json.dumps(msg_dict, ensure_ascii=False)
            prompt = (
                    "请针对以上威胁场景进行风险处理决策 (Risk Treatment Decisions) 评估，并制定网络安全要求与控制措施。\n"
                    "决策与要求制定规则：\n"
                    "输入中的 target_domain_controller 是当前目标域控；item.components 是该目标域控可用的组件清单。"
                    "所有分配判断只能基于这些信息；不得编造目标域控或组件。\n"
                    "1. 针对每个威胁场景，做出风险处理决策 (Avoid, Reduce, Share, Retain)。根据决策提供相应理由/声明字段，所有文本必须是中英文对照（格式为“中文 / English”）：\n"
                    "   - Avoid: 额外在 item_change 中编写避免理由（移除危险源、更改设计等），其它字段为空字符串；\n"
                    "   - Reduce: 额外在 cybersecurity_goal 中编写高层安全目标（保护对象 + 安全属性 + 期望结果 + 成功判据；技术中立，不写协议/算法/产品/阈值），并分别填写 security_property（七类安全属性之一）、goal_asset（保护对象）、success_criteria（可验证的高层判据）；且必须在该威胁场景的 requirements 列表中编写控制措施与要求；\n"
                    "   - Share/Retain: 额外在 cybersecurity_claim 中编写正式风险声明（剩余风险是什么、为什么可接受/可转移、依据/适用条件/有效期/批准角色），并分别填写 residual_risk、claim_justification、claim_conditions、claim_validity、claim_owner；其它字段为空字符串；claim 不是威胁描述或控制措施。\n"
                    "   - Claim 额外字段：claim_type（residual_risk_accept / risk_transfer / dependency_assumption / goal_achievement）、claim_evidence、claim_status；Claim 不得写成“系统应……”；如果输入 assumptions 为空，必须写 open_issue，不得编造假设。\n"
                    "2. 针对 Reduce 决策，编写的网络安全要求与控制措施 (Requirements) 必须满足：\n"
                    "   - Control：包含 cybersecurity_control_id 并以 CSO-001, CSO-002 等顺序编号格式编写，与中英文对照的 cybersecurity_control；并分别填写 control_type（预防/检测/纠正）、implementation_type（技术/操作）、control_objective（控制目标）、control_point（作用点：资产/接口/生命周期阶段）、dependent_functions、dependent_controls、interaction_description、applicable_lifecycle；不要写具体协议/算法/产品型号；\n"
                    "   - Allocated to Device：只有 Requirement 由 target_domain_controller 或其 item.components 中的组件实现时填 'yes'；由运行环境、其他域控/ECU、其他 item、供应商、后台或流程实现时填 'no'；信息不足时填 'no' 并在 open_issue 说明，禁止默认 yes；\n"
                    "   - Requirement：包含 cybersecurity_requirement_id 并以 CSR-001, CSR-002 等顺序编号格式编写，与中英文对照的 cybersecurity_requirement；并填写 requirement_scope（item / operational_environment / other_item）、owner、allocated_to、allocation_type、requirement_type（product/process/supplier）、verification_method、acceptance_criteria、source_ids、goal_ids、control_ids、rationale；每条 Requirement 只能有一个保护对象、一个行为、一个可验证结果，多个独立“应/不应/不得”必须拆成多条；技术中立，即使输入包含具体协议/算法/端口/数值，输出也必须抽象为能力要求，不得原样保留；\n"
                    "   - TARA 表文本列格式：cybersecurity_claim 用【Claim 类型】【声明】【剩余风险】【依据】【适用条件】【有效期】【批准角色】【证据】【待确认】组织；cybersecurity_goal 用【保护对象】【安全属性】【目标】【成功判据】组织；cybersecurity_control 用【控制类型】【实现类型】【控制目标】【作用点】【依赖/交互】组织；cybersecurity_requirement 用【范围】【主体】【要求】【验证方法】【验收准则】【待确认】组织；结构化字段仍按上面字段填写；\n"
                    "3. 编制合并后安全需求 (summarized_requirements 归纳与合并规则)：\n"
                    "   - 针对所有 allocated_to_device 为 'yes' 的原始网络安全要求进行系统性归纳与合并去重：\n"
                    "     * 原子化边界：一条 CSR = 一个保护对象 + 一个控制目标 + 一个可验证结果；\n"
                    "     * 只有同一资产类别、同一攻击面、同一控制目标、同一验证方法才合并；适用范围/资产/安全等级不同的必须拆成多条；\n"
                    "     * 合并后必须保留所有来源的适用条件差异，不能用一个来源的阈值/例外代表全部；\n"
                    "     * 每条 CSR 必须给出 verification_method 与 requirement_type；没有验证方法的条目不能作为最终 CSR；\n"
                    "   - 抽象规范：需求内容应保持技术中立与规范化表述，定义明确的安全能力边界与防护预期，不硬编码特定芯片型号或单一算法参数；\n"
                    "   - 包含的字段包括：title, sub_title, security_domain, cybersecurity_requirement, verification_method, requirement_type, rationale；\n"
                    "   - 特别注意：在 summarized_requirements 的每一项中，其 title, sub_title, cybersecurity_requirement 字段内容都必须是中英文双语对照输出，格式为“中文描述 / English translation”。其中 sub_title 字段是具体的安全子分类（小类），由你根据需求内容动态精炼生成（例如：调试接口安全 / Debug Interface Security，物理防拆设计 / Physical Enclosure Tamper Protection，启动链安全 / Secure Boot Chain 等）；\n"
                    f"   - 必须精确对齐 security_domain 字段（大分类），其取值只能是以下标准分类域之一（中英文双语形式）：\n{DOMAIN_PROMPT_LIST}\n"
                    "   - 必须在每一项的 source_requirement_ids 数组字段中，列出该项归纳合并后涵盖的原始网络安全要求编号（例如 ['CSR-001', 'CSR-002']），建立精确且可被追溯的映射关系；禁止遗漏、禁止编造输入中不存在的 ID；遗漏或编造将由后端覆盖校验拒绝。\n"
                    f"{req_guideline}"
                )
            ai_res = call_llm(msg, prompt, Stage45Output)
            validation_warnings = validate_concept_objects(
                ai_res,
                asset_info_dict.get("target_domain_controller", {}),
                asset_info_dict.get("item", {}).get("components", []),
            )
            if validation_warnings:
                print(f"[TARA-AI] ISO 21434 对象校验警告 ({len(validation_warnings)}):")
                for warning in validation_warnings[:12]:
                    print(f"  - {warning}")

            risk_decisions = []
            max_risk_val = 1
            has_mitigate = False
            has_avoid = False
            has_transfer = False
            justifications = []
            
            for ts in threat_scenarios:
                impact_val = ts.get("overall_impact", 1)
                feasibility_key = str(ts.get("final_feasibility", "Medium")).lower().replace(" ", "")
                risk_val = risk_matrix.get(feasibility_key, [1, 2, 3, 4])[min(max(impact_val, 0), 3)]
                
                matched_dec = None
                for dec in ai_res.get("risk_decisions", []):
                    if dec.get("threat_id") == ts.get("threat_id"):
                        matched_dec = dec
                        break
                
                if matched_dec:
                    treatment = matched_dec.get("risk_treatment", "Reduce")
                    item_change = matched_dec.get("item_change", "")
                    cybersecurity_goal = matched_dec.get("cybersecurity_goal", "")
                    security_property = matched_dec.get("security_property", "")
                    goal_asset = matched_dec.get("goal_asset", "")
                    success_criteria = matched_dec.get("success_criteria", "")
                    cybersecurity_claim = matched_dec.get("cybersecurity_claim", "")
                    claim_type = matched_dec.get("claim_type", "")
                    residual_risk = matched_dec.get("residual_risk", "")
                    claim_justification = matched_dec.get("claim_justification", "")
                    claim_conditions = matched_dec.get("claim_conditions", "")
                    claim_validity = matched_dec.get("claim_validity", "")
                    claim_owner = matched_dec.get("claim_owner", "")
                    claim_evidence = matched_dec.get("claim_evidence", "")
                    claim_status = matched_dec.get("claim_status", "")
                    claim_open_issue = matched_dec.get("open_issue", "")
                    raw_reqs = matched_dec.get("requirements", [])
                else:
                    treatment = "Reduce"
                    item_change = ""
                    cybersecurity_goal = f"针对资产 {asset.name} 的安全属性 {ts.get('attribute')} 威胁，制定防护机制。 / Formulate protection mechanisms against threat to attribute {ts.get('attribute')} of {asset.name}."
                    security_property = ts.get("attribute") or ""
                    goal_asset = asset.name or ""
                    success_criteria = ""
                    cybersecurity_claim = ""
                    claim_type = ""
                    residual_risk = ""
                    claim_justification = ""
                    claim_conditions = ""
                    claim_validity = ""
                    claim_owner = ""
                    claim_evidence = ""
                    claim_status = ""
                    claim_open_issue = ""
                    raw_reqs = []

                treatment_lower = treatment.lower().strip()
                decision_norm = "Reduce"
                if "avoid" in treatment_lower:
                    decision_norm = "Avoid"
                    has_avoid = True
                elif "share" in treatment_lower or "transfer" in treatment_lower:
                    decision_norm = "Share"
                    has_transfer = True
                elif "retain" in treatment_lower or "accept" in treatment_lower:
                    decision_norm = "Retain"
                    has_transfer = True
                else:
                    decision_norm = "Reduce"
                    has_mitigate = True

                # Enforce field mutual exclusivity based on decision type
                if decision_norm == "Avoid":
                    cybersecurity_goal = ""
                    security_property = ""
                    goal_asset = ""
                    success_criteria = ""
                    cybersecurity_claim = ""
                    claim_type = ""
                    residual_risk = ""
                    claim_justification = ""
                    claim_conditions = ""
                    claim_validity = ""
                    claim_owner = ""
                    claim_evidence = ""
                    claim_status = ""
                    claim_open_issue = ""
                    raw_reqs = []
                elif decision_norm == "Reduce":
                    item_change = ""
                    cybersecurity_claim = ""
                    claim_type = ""
                    residual_risk = ""
                    claim_justification = ""
                    claim_conditions = ""
                    claim_validity = ""
                    claim_owner = ""
                    claim_evidence = ""
                    claim_status = ""
                    claim_open_issue = ""
                elif decision_norm in ("Share", "Retain"):
                    item_change = ""
                    cybersecurity_goal = ""
                    security_property = ""
                    goal_asset = ""
                    success_criteria = ""
                    raw_reqs = []
                    
                max_risk_val = max(max_risk_val, risk_val)
                
                risk_decisions.append({
                    "attribute": ts.get("attribute"),
                    "threat_id": ts.get("threat_id"),
                    "threat_scenario": ts.get("threat_scenario"),
                    "damage_scenario": ts.get("damage_scenario"),
                    "final_feasibility": ts.get("final_feasibility"),
                    "overall_impact": ts.get("overall_impact"),
                    "risk_value": risk_val,
                    "risk_treatment": decision_norm,
                    "item_change": item_change,
                    "cybersecurity_goal_id": generate_content_id("CSO_", cybersecurity_goal) if cybersecurity_goal else "",
                    "cybersecurity_goal": cybersecurity_goal,
                    "security_property": security_property,
                    "goal_asset": goal_asset,
                    "success_criteria": success_criteria,
                    "cybersecurity_claim_id": generate_content_id("CLM_", cybersecurity_claim) if cybersecurity_claim else "",
                    "cybersecurity_claim": cybersecurity_claim,
                    "claim_type": claim_type,
                    "residual_risk": residual_risk,
                    "claim_justification": claim_justification,
                    "claim_conditions": claim_conditions,
                    "claim_validity": claim_validity,
                    "claim_owner": claim_owner,
                    "claim_evidence": claim_evidence,
                    "claim_status": claim_status,
                    "open_issue": claim_open_issue,
                    "_raw_requirements": raw_reqs
                })
                justifications.append(f"安全风险为 {risk_val} ({ts.get('attribute')}属性)，采取 {decision_norm} 决策。 / Cybersecurity risk is {risk_val} ({ts.get('attribute')} attribute), decision: {decision_norm} decision.")
                
            decision_raw = "mitigate"
            if has_mitigate:
                decision_raw = "mitigate"
            elif has_avoid:
                decision_raw = "avoid"
            elif has_transfer:
                decision_raw = "transfer"
            else:
                decision_raw = "accept"
                
            stage4_final = {
                "risk_decisions": [{k: v for k, v in rd.items() if k != "_raw_requirements"} for rd in risk_decisions],
                "risk_rating": max_risk_val,
                "risk_decision": decision_raw,
                "justification": "; ".join(justifications),
                "_validation_warnings": validation_warnings
            }
            
            stage4_final["_consolidated_stage5_data"] = {
                "risk_decisions": risk_decisions,
                "summarized_requirements": ai_res.get("summarized_requirements", [])
            }
            return stage4_final

        elif stage == "stage5":
            risk_decisions = prev_stages.get("stage4", {}).get("risk_decisions", [])
            if not risk_decisions:
                risk_decisions = [{
                    "attribute": "Integrity",
                    "threat_id": "TS_Integrity_1",
                    "threat_scenario": "威胁场景",
                    "risk_value": prev_stages.get("stage4", {}).get("risk_rating", 3),
                    "risk_treatment": "Reduce" if prev_stages.get("stage4", {}).get("risk_decision") == "mitigate" else "Retain",
                    "cybersecurity_goal": "保护系统安全"
                }]
                
            has_reduce = any(dec.get("risk_treatment") == "Reduce" for dec in risk_decisions)
            if not has_reduce:
                return {
                    "cso": "无需制定安全目标 / No cybersecurity goals needed",
                    "csr": [],
                    "exempted": True,
                    "reason": "所有威胁场景决策均为Retain/Share，免除安全需求制定。 / All threat scenario decisions are Retain/Share，exempting the formulation of cybersecurity requirements."
                }
                
            stage4_out = prev_stages.get("stage4", {})
            consolidated_data = stage4_out.get("_consolidated_stage5_data")
            
            if consolidated_data:
                # Direct extraction from Stage 4
                raw_decisions = consolidated_data.get("risk_decisions", [])
                requirements = []
                for rd in raw_decisions:
                    if rd.get("risk_treatment") == "Reduce":
                        raw_reqs = rd.get("_raw_requirements", [])
                        for req in raw_reqs:
                            ctrl_text = req.get("cybersecurity_control", "")
                            req_text = req.get("cybersecurity_requirement", "")
                            requirements.append({
                                "threat_id": rd["threat_id"],
                                "goal_ids": req.get("goal_ids", []),
                                "cybersecurity_control_id": generate_content_id("CSC_", ctrl_text) if ctrl_text else "",
                                "control_ids": req.get("control_ids", []),
                                "claim_ids": req.get("claim_ids", []),
                                "cybersecurity_control": ctrl_text,
                                "control_type": req.get("control_type", ""),
                                "implementation_type": req.get("implementation_type", ""),
                                "control_objective": req.get("control_objective", ""),
                                "control_point": req.get("control_point", ""),
                                "dependent_functions": req.get("dependent_functions", []),
                                "dependent_controls": req.get("dependent_controls", []),
                                "interaction_description": req.get("interaction_description", ""),
                                "applicable_lifecycle": req.get("applicable_lifecycle", ""),
                                "allocated_to_device": req.get("allocated_to_device", "no"),
                                "requirement_scope": req.get("requirement_scope", "item"),
                                "owner": req.get("owner", ""),
                                "allocated_to": req.get("allocated_to", ""),
                                "allocation_type": req.get("allocation_type", "primary"),
                                "cybersecurity_requirement_id": generate_content_id("CSR_", req_text) if req_text else "",
                                "cybersecurity_requirement": req_text,
                                "verification_method": req.get("verification_method", ""),
                                "acceptance_criteria": req.get("acceptance_criteria", ""),
                                "source_ids": req.get("source_ids", []),
                                "dependencies": req.get("dependencies", []),
                                "requirement_type": req.get("requirement_type", "product"),
                                "rationale": req.get("rationale", ""),
                                "open_issue": req.get("open_issue", "")
                            })
                
                device_reqs = [req for req in requirements if str(req.get("allocated_to_device", "")).lower().strip() == "yes"]
                summarized_csrs = []
                for item in consolidated_data.get("summarized_requirements", []):
                    sum_req_text = item.get("cybersecurity_requirement", "")
                    sum_req_id = generate_content_id("CSR_", sum_req_text) if sum_req_text else ""
                    summarized_csrs.append({
                        "asset_id": item.get("asset_id", f"ID{asset.id}"),
                        "asset_name": item.get("asset_name", asset.name),
                        "cybersecurity_requirement_id": sum_req_id,
                        "csr_id": sum_req_id,
                        "title": item.get("title", ""),
                        "sub_title": item.get("sub_title", ""),
                        "security_domain": item.get("security_domain", "通用安全 / General Security"),
                        "cybersecurity_requirement": item.get("cybersecurity_requirement", ""),
                        "verification_method": item.get("verification_method", ""),
                        "requirement_type": item.get("requirement_type", "product"),
                        "rationale": item.get("rationale", ""),
                        "source_requirement_ids": item.get("source_requirement_ids", [])
                    })
            else:
                # Standalone Stage 5 Call
                msg_dict = {
                    "asset_info": asset_info_dict,
                    "target_domain_controller": asset_info_dict.get("target_domain_controller", {}),
                    "item": asset_info_dict.get("item", {}),
                    "assumptions": prev_stages.get("stage3", {}).get("assumptions", []),
                    "existing_controls": prev_stages.get("stage3", {}).get("existing_controls", []),
                    "policy": {
                        "verification_methods": ["测试", "分析", "检查", "演示"],
                        "acceptance_criteria_rule": "必须是可判定的通过/失败条件，不能写“满足安全要求”",
                        "cal_method": "如项目使用 CAL，按 Annex E 评估；否则留空",
                        "language": "CN/EN",
                    },
                    "risk_decisions": [
                        {k: v for k, v in rd.items() if k != "_raw_requirements"}
                        for rd in risk_decisions if rd.get("risk_treatment") == "Reduce"
                    ]
                }
                msg = json.dumps(msg_dict, ensure_ascii=False)
                prompt = (
                    "资产信息：针对上述 risk_treatment 为 Reduce 的威胁场景列表，编写相对应安全控制措施与网络安全要求。\n"
                    "输入中的 target_domain_controller 是当前目标域控；item.components 是该目标域控可用的组件清单。所有分配判断只能基于这些信息；不得编造目标域控或组件。\n"
                    "1. 针对每个威胁场景，编写的安全控制措施 (Requirements) 必须满足：\n"
                    "   - Control：包含 cybersecurity_control_id 并以 CSO-001, CSO-002 等顺序编号格式编写，与中英文对照的 cybersecurity_control；并分别填写 control_type（预防/检测/纠正）、implementation_type（技术/操作）、control_objective（控制目标）、control_point（作用点：资产/接口/生命周期阶段）、dependent_functions、dependent_controls、interaction_description、applicable_lifecycle；不要写具体协议/算法/产品型号；\n"
                    "   - Allocated to Device：只有 Requirement 由 target_domain_controller 或其 item.components 中的组件实现时填 'yes'；由运行环境、其他域控/ECU、其他 item、供应商、后台或流程实现时填 'no'；信息不足时填 'no' 并在 open_issue 说明，禁止默认 yes；\n"
                    "   - Requirement：包含 cybersecurity_requirement_id 并以 CSR-001, CSR-002 等顺序编号格式编写，与中英文对照的 cybersecurity_requirement；并填写 requirement_scope（item / operational_environment / other_item）、owner、allocated_to、allocation_type、requirement_type（product/process/supplier）、verification_method、acceptance_criteria、source_ids、goal_ids、control_ids、rationale；每条 Requirement 只能有一个保护对象、一个行为、一个可验证结果，多个独立“应/不应/不得”必须拆成多条；技术中立，即使输入包含具体协议/算法/端口/数值，输出也必须抽象为能力要求，不得原样保留；\n"
                    "2. 编制合并后安全需求 (summarized_requirements 归纳与合并规则)：\n"
                    "   - 针对所有 allocated_to_device 为 'yes' 的原始网络安全要求进行系统性归纳与合并去重：\n"
                    "     * 原子化边界：一条 CSR = 一个保护对象 + 一个控制目标 + 一个可验证结果；\n"
                    "     * 只有同一资产类别、同一攻击面、同一控制目标、同一验证方法才合并；适用范围/资产/安全等级不同的必须拆成多条；\n"
                    "     * 合并后必须保留所有来源的适用条件差异，不能用一个来源的阈值/例外代表全部；\n"
                    "     * 每条 CSR 必须给出 verification_method 与 requirement_type；没有验证方法的条目不能作为最终 CSR；\n"
                    "   - 抽象规范：需求内容应保持技术中立与规范化表述，定义明确的安全能力边界与防护预期，不硬编码特定芯片型号或单一算法参数；\n"
                    "   - 包含的字段包括：title, sub_title, security_domain, cybersecurity_requirement, verification_method, requirement_type, rationale；\n"
                    "   - 特别注意：在 summarized_requirements 的每一项中，其 title, sub_title, cybersecurity_requirement 字段内容都必须是中英文双语对照输出，格式为“中文描述 / English translation”。其中 sub_title 字段是具体的安全子分类（小类），由你根据需求内容动态精炼生成（例如：调试接口安全 / Debug Interface Security，物理防拆设计 / Physical Enclosure Tamper Protection，启动链安全 / Secure Boot Chain 等）；\n"
                    f"   - 必须精确对齐 security_domain 字段（大分类），其取值只能是以下标准分类域之一（中英文双语形式）：\n{DOMAIN_PROMPT_LIST}\n"
                    "   - 必须在每一项的 source_requirement_ids 数组字段中，列出该项归纳合并后涵盖的原始网络安全要求编号（例如 ['CSR-001', 'CSR-002']），建立精确且可被追溯的映射关系；禁止遗漏、禁止编造输入中不存在的 ID；遗漏或编造将由后端覆盖校验拒绝。\n"
                    f"{req_guideline}"
                )
                ai_res = call_llm(msg, prompt, StandaloneStage5Output)
                
                requirements = []
                for req in ai_res.get("requirements", []):
                    ctrl_text = req.get("cybersecurity_control", "")
                    req_text = req.get("cybersecurity_requirement", "")
                    requirements.append({
                        "threat_id": req.get("threat_id", "TS_Integrity_1"),
                        "goal_ids": req.get("goal_ids", []),
                        "cybersecurity_control_id": generate_content_id("CSC_", ctrl_text) if ctrl_text else "",
                        "control_ids": req.get("control_ids", []),
                        "claim_ids": req.get("claim_ids", []),
                        "cybersecurity_control": ctrl_text,
                        "control_type": req.get("control_type", ""),
                        "implementation_type": req.get("implementation_type", ""),
                        "control_objective": req.get("control_objective", ""),
                        "control_point": req.get("control_point", ""),
                        "dependent_functions": req.get("dependent_functions", []),
                        "dependent_controls": req.get("dependent_controls", []),
                        "interaction_description": req.get("interaction_description", ""),
                        "applicable_lifecycle": req.get("applicable_lifecycle", ""),
                        "allocated_to_device": req.get("allocated_to_device", "no"),
                        "requirement_scope": req.get("requirement_scope", "item"),
                        "owner": req.get("owner", ""),
                        "allocated_to": req.get("allocated_to", ""),
                        "allocation_type": req.get("allocation_type", "primary"),
                        "cybersecurity_requirement_id": generate_content_id("CSR_", req_text) if req_text else "",
                        "cybersecurity_requirement": req_text,
                        "verification_method": req.get("verification_method", ""),
                        "acceptance_criteria": req.get("acceptance_criteria", ""),
                        "source_ids": req.get("source_ids", []),
                        "dependencies": req.get("dependencies", []),
                        "requirement_type": req.get("requirement_type", "product"),
                        "rationale": req.get("rationale", ""),
                        "open_issue": req.get("open_issue", "")
                    })
                    
                summarized_csrs = []
                for item in ai_res.get("summarized_requirements", []):
                    sum_req_text = item.get("cybersecurity_requirement", "")
                    sum_req_id = generate_content_id("CSR_", sum_req_text) if sum_req_text else ""
                    summarized_csrs.append({
                        "asset_id": item.get("asset_id", f"ID{asset.id}"),
                        "asset_name": item.get("asset_name", asset.name),
                        "cybersecurity_requirement_id": sum_req_id,
                        "csr_id": sum_req_id,
                        "title": item.get("title", ""),
                        "sub_title": item.get("sub_title", ""),
                        "security_domain": item.get("security_domain", "通用安全 / General Security"),
                        "cybersecurity_requirement": item.get("cybersecurity_requirement", ""),
                        "verification_method": item.get("verification_method", ""),
                        "requirement_type": item.get("requirement_type", "product"),
                        "rationale": item.get("rationale", ""),
                        "source_requirement_ids": item.get("source_requirement_ids", [])
                    })
                    
            all_csr_texts = [r["cybersecurity_requirement"] for r in summarized_csrs]
            all_cso_texts = [r["cybersecurity_control"] for r in requirements]
            
            return {
                "requirements": requirements,
                "summarized_csrs": summarized_csrs,
                "cso": "; ".join(all_cso_texts[:2]) if all_cso_texts else "网络安全目标制定完成 / Formulation of cybersecurity goals completed",
                "csr": all_csr_texts,
                "exempted": False,
                "reason": ""
            }

    except Exception as e:
        print(f"[TARA-AI] 警告: 大模型评估发生异常 ({e})，降级执行 Rules-based 模拟流程。")
        return mock_tara_ai_call(stage, asset, prev_stages)

def process_single_asset(asset_id: int, domain_id: int, run_record_id: int, force: bool, stages: list, total_steps: int, progress_counter: list):
    """
    单资产多阶段安全分析的处理函数，运行在独立线程中 (BR-40, BR-41)
    """
    thread_db = SessionLocal()
    try:
        # 1. 查询当前线程的 Asset
        asset = thread_db.query(Asset).filter(Asset.id == asset_id).first()
        if not asset:
            return
            
        asset_info_dict = {
            "asset_id": f"ID{asset.id}",
            "asset_name": asset.name,
            "communication_protocol": asset.protocol or "无",
            "asset_type": asset.asset_type.capitalize() if asset.asset_type else "Hardware",
            "remarks": asset.description or "无"
        }
        asset_info_dict.update(build_item_context(thread_db, asset))

        prev_stage_outputs = {} # 暂存当前资产的前置步骤最终结论
        
        for stage in stages:
            # 检查任务是否在中途被撤销终止 (BR-70)
            with db_write_lock:
                current_run = thread_db.query(TaraRun).filter(TaraRun.id == run_record_id).first()
                if not current_run:
                    return
                if current_run.status == "cancelled":
                    print(f"检测到 TARA 任务 {run_record_id} 被用户手动取消，线程强制终止。")
                    return
            
            # A. 计算 input_hash (BR-45)
            if stage == "stage1":
                input_hash_material = f"{asset.name}:{asset.asset_type}:{asset.protocol}:{asset.description}"
            elif stage == "stage2":
                input_hash_material = f"{asset.name}:{prev_stage_outputs.get('stage1')}"
            elif stage == "stage3":
                input_hash_material = f"{asset.name}:{prev_stage_outputs.get('stage1')}:{prev_stage_outputs.get('stage2')}"
            elif stage == "stage4":
                input_hash_material = f"{asset.name}:{prev_stage_outputs.get('stage1')}:{prev_stage_outputs.get('stage2')}:{prev_stage_outputs.get('stage3')}"
            elif stage == "stage5":
                input_hash_material = f"{asset.name}:{prev_stage_outputs.get('stage1')}:{prev_stage_outputs.get('stage2')}:{prev_stage_outputs.get('stage3')}:{prev_stage_outputs.get('stage4')}"
            
            current_hash = calculate_md5(input_hash_material)
            
            # B. 判断是否可以进行增量匹配或继承 (BR-45, BR-51/75)
            prev_step = get_previous_completed_step(thread_db, asset.id, stage)
            
            step_result = None
            if prev_step and prev_step.input_hash == current_hash:
                prev_res = prev_step.analysis_result
                if prev_res.get("is_human_modified"):
                    # 无论是普通还是 force 模式，都继承历史人工修改结论
                    step_result = prev_res
                    print(f"资产 {asset.name} 阶段 {stage} 匹配哈希并成功继承人工修改。")
                elif not force:
                    # 只有在非 force 模式下，才继承历史 AI 结论并跳过调用
                    step_result = prev_res
                    print(f"资产 {asset.name} 阶段 {stage} 哈希匹配成功，跳过 AI 调用 (增量模式)。")
                else:
                    print(f"资产 {asset.name} 阶段 {stage} 虽哈希匹配，但处于 Force 强制重跑模式，重新调用 AI。")
            
            # C. 如果没有匹配到，或者哈希发生变化，调用 AI (或降级 Mock) 模块进行分析
            if not step_result:
                ai_raw = tara_ai_analysis_call(thread_db, stage, asset, prev_stage_outputs)
                step_result = {
                    "ai_output": ai_raw,
                    "is_human_modified": False,
                    "modification_reason": "",
                    "final_output": ai_raw
                }
            
            # 暂存当前阶段的 final_output 以作为后续阶段 of 输入 Hash 依赖
            prev_stage_outputs[stage] = step_result["final_output"]
            
            # D. 写入 tara_steps 数据库表 (需要全局排他锁保护 SQLite 写入)
            tara_step = TaraStep(
                run_id=run_record_id,
                asset_id=asset.id,
                stage=stage,
                status="completed",
                input_hash=current_hash,
                analysis_result=step_result
            )
            
            with db_write_lock:
                thread_db.add(tara_step)
                thread_db.commit()
                
                # E. 更新进度条 (BR-看板)
                progress_counter[0] += 1
                progress_percentage = int((progress_counter[0] / total_steps) * 100)
                
                # 重新查询实例以防其在其它 session 被修改导致脏读冲突
                run = thread_db.query(TaraRun).filter(TaraRun.id == run_record_id).first()
                domain = thread_db.query(Domain).filter(Domain.id == domain_id).first()
                if run:
                    run.progress = progress_percentage
                if domain:
                    domain.progress = progress_percentage
                thread_db.commit()
                
    except Exception as e:
        print(f"❌ 线程执行资产 {asset_id} 分析发生异常: {e}")
        raise e
    finally:
        thread_db.close()

@celery_app.task(bind=True)
def run_tara_analysis(self, domain_id: int, run_record_id: int, force: bool = False):
    """
    Celery 异步任务：执行 TARA 5 阶段跑批核心逻辑 (BR-40, BR-41)
    方案B优化：并发多资产多线程执行大模型调用，提升效率；
    利用全局 db_write_lock 锁确保 SQLite 的单写线程安全。
    """
    db = SessionLocal()
    run = db.query(TaraRun).filter(TaraRun.id == run_record_id).first()
    domain = db.query(Domain).filter(Domain.id == domain_id).first()
    
    if not run or not domain:
        db.close()
        return {"status": "failed", "detail": "运行记录或域控不存在"}
        
    run.status = "running"
    run.progress = 0
    db.commit()
    
    try:
        # 1. 查询所有已确认的资产
        assets = db.query(Asset).filter(Asset.domain_id == domain_id, Asset.status == "confirmed").all()
        total_assets = len(assets)
        if total_assets == 0:
            raise ValueError("没有需要分析的确认资产")
            
        stages = ["stage1", "stage2", "stage3", "stage4", "stage5"]
        total_steps = total_assets * len(stages)
        progress_counter = [0]
        
        asset_ids = [asset.id for asset in assets]
        
        # 释放当前 session，避免独占连接池
        db.close()
        
        # 2. 并发对不同资产进行多线程大模型调用 (最大并发为3，防 LLM Rate Limit)
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(
                    process_single_asset,
                    aid,
                    domain_id,
                    run_record_id,
                    force,
                    stages,
                    total_steps,
                    progress_counter
                )
                for aid in asset_ids
            ]
            # 阻塞等待所有资产评估线程完成，若有任何线程出错，则在 .result() 时抛出
            for fut in futures:
                fut.result()
                
        # 3. 跑批完成，更新状态
        db = SessionLocal()
        run = db.query(TaraRun).filter(TaraRun.id == run_record_id).first()
        domain = db.query(Domain).filter(Domain.id == domain_id).first()
        
        # 如果已被用户撤销
        if run.status == "cancelled":
            db.close()
            return {"status": "cancelled"}
            
        # 4. 执行基于 UNECE R155 Annex 5 引导的项目级全局安全需求综合提炼
        try:
            from app.services.csr_synthesis import get_or_synthesize_global_csrs
            steps = db.query(TaraStep).filter(TaraStep.run_id == run_record_id).all()
            assets = db.query(Asset).filter(Asset.domain_id == domain_id).all()
            get_or_synthesize_global_csrs(db, domain, steps, assets)
        except Exception as synth_err:
            print(f"[TARA-AI] 警告: 项目级全局需求提炼任务执行异常: {synth_err}")

        run.status = "completed"
        run.completed_at = datetime.now()
        domain.status = "completed"
        domain.progress = 100
        db.commit()
        
        # 4. 联动推导项目状态 (BR-03)
        from app.api.project import recalculate_project_status
        recalculate_project_status(domain.project_id, db)
        
        return {"status": "completed", "domain_id": domain_id}
        
    except Exception as e:
        try:
            db_err = SessionLocal()
            run_err = db_err.query(TaraRun).filter(TaraRun.id == run_record_id).first()
            domain_err = db_err.query(Domain).filter(Domain.id == domain_id).first()
            if run_err:
                run_err.status = "failed"
            if domain_err:
                domain_err.status = "failed"
            db_err.commit()
            db_err.close()
        except Exception as commit_err:
            print(f"❌ TARA 跑批失败状态重置失败: {commit_err}")
        print(f"❌ TARA 跑批失败: {e}")
        return {"status": "failed", "detail": str(e)}
    finally:
        try:
            db.close()
        except Exception:
            pass

# ----------------- Celery 任务失败全局信号监听 (防止死锁) -----------------
from celery.signals import task_failure
from app.api.project import recalculate_project_status

@task_failure.connect
def handle_task_failure(sender=None, task_id=None, exception=None, args=None, kwargs=None, **extra):
    """
    当任何 Celery 任务执行失败时（包括入参绑定失败、启动异常等），
    自动将对应的 TaraRun 和 Domain 状态重置为 failed，释放前端 UI 只读锁。
    """
    if sender and sender.name == "app.worker.tasks.run_tara_analysis":
        db = SessionLocal()
        try:
            domain_id = None
            run_record_id = None
            
            # 尝试提取入参
            if args and len(args) >= 2:
                domain_id = args[0]
                run_record_id = args[1]
            elif kwargs:
                domain_id = kwargs.get("domain_id")
                run_record_id = kwargs.get("run_record_id")
                
            print(f"[Celery Signal] 监听到任务失败事件: task_id={task_id}, exception={exception}, domain_id={domain_id}, run_record_id={run_record_id}")
            
            if run_record_id:
                run = db.query(TaraRun).filter(TaraRun.id == run_record_id).first()
                if run and run.status == "running":
                    run.status = "failed"
                    
            if domain_id:
                domain = db.query(Domain).filter(Domain.id == domain_id).first()
                if domain and domain.status == "running":
                    domain.status = "failed"
                    db.commit()
                    recalculate_project_status(domain.project_id, db)
            db.commit()
            print(f"[Celery Signal] 成功自动释放死锁并重置状态为 failed。")
        except Exception as signal_err:
            db.rollback()
            print(f"[Celery Signal] 处理失败事件时出错: {signal_err}")
        finally:
            db.close()
