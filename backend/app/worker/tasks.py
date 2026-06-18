import time
import hashlib
import json
import httpx
from celery import Celery
from sqlalchemy.orm import Session
from datetime import datetime

from app.core.celery_app import celery_app
from app.core.database import SessionLocal
from app.models.domain import Domain
from app.models.project import Project
from app.models.asset import Asset
from app.models.tara_run import TaraRun
from app.models.tara_step import TaraStep
from app.models.system_settings import SystemSettings

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
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)

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
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(url, headers=headers, json=payload)
        if resp.status_code != 200:
            raise RuntimeError(f"API HTTP {resp.status_code}: {resp.text[:150]}")
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
            "cybersecurity_requirement": req_text
        })
    return {"asset_cybersecurity_requirement_list": summarized}

def mock_tara_ai_call(stage: str, asset: Asset, prev_stages: dict) -> dict:
    """
    Rules-based 降级算法：模拟 TARA 大模型分析输出
    根据资产名称与类型输出强结构化的分析草案 JSON，确保在无网络或未配置大模型时分析依然可用。
    """
    time.sleep(0.1) # 模拟微弱延迟
    
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
            if score > 2:
                selected.append(attr)
                
        def get_rating(score):
            if score >= 4: return "High"
            if score >= 2: return "Medium"
            if score >= 1: return "Low"
            return "None"
            
        return {
            "attributes": scores,
            "selected_attributes": selected,
            "confidentiality": get_rating(c),
            "integrity": get_rating(i),
            "availability": get_rating(a),
            "description": f"已选定高相关安全属性: {', '.join(selected)} 进行后续分析。 / Selected highly relevant security attributes: {', '.join(selected)} for subsequent analysis." if selected else "无高相关(分数>2)安全属性，未选择属性。 / No highly relevant (score > 2) security attributes, none selected."
        }
        
    elif stage == "stage2":
        selected_attrs = prev_stages.get("stage1", {}).get("selected_attributes", ["Confidentiality", "Integrity"])
        damage_scenarios = []
        
        # Mapping impact level strings to numerical values
        impact_map = {"negligible": 0, "moderate": 1, "major": 2, "severe": 3}
        
        for attr in selected_attrs:
            # Generate 2 damage scenarios per selected attribute
            ds1_text = f"攻击者破坏了 {asset.name} 的 {attr} 属性，导致车载系统的相关控制功能受损，从而影响驾驶安全或造成财产损失。 / Attacker compromises the {attr} attribute of {asset.name}, impairing control functions of the vehicle system, thereby impacting driving safety or causing property damage."
            ds2_text = f"恶意用户篡改 {asset.name} 的 {attr} 信息，导致系统做出错误决策，存在引发交通事故的潜在风险。 / Malicious user tampers with the {attr} information of {asset.name}, causing the system to make incorrect decisions, presenting potential risks of traffic accidents."
            
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
                "damage_scenario_sn": f"DS_{attr}_1",
                "damage_scenario": ds1_text,
                "impact_rating": {"safety": s, "financial": f, "operational": o, "privacy": p},
                "overall_impact": overall_impact
            })
            damage_scenarios.append({
                "attribute": attr,
                "damage_scenario_sn": f"DS_{attr}_2",
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
            "damage_scenario": "; ".join(all_ds_texts[:2]) if all_ds_texts else "由于安全属性泄露或篡改，导致车辆对应子域故障。 / Cybersecurity attribute breach or tampering leads to subdomain failure of the vehicle.",
            "impact_rating": {"safety": max_s, "financial": max_f, "operational": max_o, "privacy": max_p},
            "overall_impact": overall_impact
        }
        
    elif stage == "stage3":
        damage_scenarios = prev_stages.get("stage2", {}).get("damage_scenarios", [])
        if not damage_scenarios:
            # Fallback if stage2 didn't have damage_scenarios list
            damage_scenarios = [{
                "attribute": "Integrity",
                "damage_scenario_sn": "DS_Integrity_1",
                "damage_scenario": prev_stages.get("stage2", {}).get("damage_scenario", "车载子系统故障 / Vehicle subsystem failure"),
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
                "cybersecurity_goal": goal,
                "cybersecurity_claim": claim
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
                req_id = f"CSR_{dec['attribute']}_1"
                req_text = f"系统应对针对 {asset.name} 的 {dec['attribute']} 关键消息启用强制签名及抗重放序号校验。 / The system shall enable mandatory signature and anti-replay sequence number checks for critical messages targeting the {dec['attribute']} of {asset.name}."
                
                requirements.append({
                    "threat_id": dec["threat_id"],
                    "cybersecurity_control_id": f"CSO_{dec['attribute']}_1",
                    "cybersecurity_control": f"通过部署安全通信算法（如SecOC或TLS）保护 {asset.name} 的数据传输通道。 / Protect the data transmission channel of {asset.name} by deploying secure communication algorithms such as SecOC or TLS.",
                    "allocated_to_device": "yes",
                    "cybersecurity_requirement_id": req_id,
                    "cybersecurity_requirement": req_text
                })
                
                device_requirements.append({
                    "asset_id": f"ID{asset.id}",
                    "asset_name": asset.name,
                    "cybersecurity_requirement_id": req_id,
                    "csr_id": req_id,
                    "title": f"针对 {asset.name} {dec['attribute']} 的报文防伪要求 / Packet anti-counterfeiting requirements targeting {asset.name} {dec['attribute']}",
                    "sub_title": "双向通信安全签名 / Bidirectional secure communication signature",
                    "cybersecurity_requirement": req_text
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

def tara_ai_analysis_call(db: Session, stage: str, asset: Asset, prev_stages: dict) -> dict:
    """
    TARA 阶段大模型分析核心实现：集成 pyTara_V 标准业务逻辑、专家提示词与风险计算矩阵
    """
    settings = db.query(SystemSettings).first()
    if not settings or not settings.api_key or settings.api_key == "mock_test_key":
        print(f"[TARA-AI] 未配置大模型或为 Mock 模式，执行 Rules-based 降级算法 (Stage: {stage})")
        return mock_tara_ai_call(stage, asset, prev_stages)

    # 核心系统专家提示词 (基于 pyTara_V 设定)
    system_prompt = (
        "你现在是拥有丰富经验的 ISO 21434 汽车网络安全专家。当前 Item 是自动驾驶子系统域控制器（ADCU），具备 L2.9 级自动驾驶功能。\n"
        "1. 所有描述性内容、场景描述、路径、理由、安全目标和安全控制/需求必须使用中英文双语对照输出，格式为“中文描述 / English translation” (例如：“由于系统受损，功能失效。 / Impaired functions due to system compromise.”)，保证翻译的专业性与准确性；\n"
        "2. 绝对不使用 Markdown 代码块；\n"
        "3. 绝对不包含任何思考过程、推理步骤或解释说明；\n"
        "4. 只返回最终的 JSON 格式结果；\n"
        "5. 必须严格按照提供的'输出JSON结果示例'格式返回；\n"
        "6. 确保JSON格式正确，包含所有必需的字段；\n"
        "7. 不添加任何额外的键或值；请直接返回符合要求的JSON结果，不要返回思考过程。"
    )

    url = f"{settings.api_base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json"
    }

    def call_llm(msg: str, prompt: str) -> dict:
        payload = {
            "model": settings.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"输入的资产信息如下：{msg}"},
                {"role": "user", "content": prompt}
            ],
            "response_format": {"type": "json_object"}
        }
        print(f"[TARA-AI] 发起大模型服务请求 ({settings.model_name}) 执行 API 调用...")
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(url, headers=headers, json=payload)
        if resp.status_code != 200:
            raise RuntimeError(f"API HTTP {resp.status_code}: {resp.text[:150]}")
        resp_data = resp.json()
        raw_text = resp_data["choices"][0]["message"]["content"]
        return clean_and_parse_json(raw_text)

    try:
        asset_info_dict = {
            "asset_id": f"ID{asset.id}",
            "asset_name": asset.name,
            "communication_protocol": asset.protocol or "无",
            "asset_type": asset.asset_type.capitalize() if asset.asset_type else "Hardware",
            "remarks": asset.description or "无"
        }

        if stage == "stage1":
            msg_dict = {"asset_cybersecurity_attribute": {"asset_info": asset_info_dict}}
            msg = json.dumps(msg_dict, ensure_ascii=False)
            prompt = (
                "请根据以上资产asset_id, asset_name, asset_type, communication_protocol, remarks，"
                "评估资产是否应该被赋于以下安全属性：\n"
                "Authenticity, Integrity, Non-repudiation, Confidentiality, Availability, Authorization, Privacy。\n"
                "评分标准：0-不相关，1-低相关，2-中等相关，3-高相关，4-关键相关，5-必需属性。\n"
                "输出请忽略输入的资产信息内容和格式要求，请按照输出格式参考内容和要求回复。\n"
                '输出JSON结果示例:{"Authenticity": 4, "Integrity": 3, "Non-repudiation": 1, "Confidentiality": 1, "Availability": 1, "Authorization": 1, "Privacy": 5}'
            )
            ai_res = call_llm(msg, prompt)
            
            selected = []
            for attr, score in ai_res.items():
                try: s = int(score)
                except: s = 0
                if s > 2 and attr in ["Authenticity", "Integrity", "Non-repudiation", "Confidentiality", "Availability", "Authorization", "Privacy"]:
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
                "confidentiality": get_rating(ai_res.get("Confidentiality", 0)),
                "integrity": get_rating(ai_res.get("Integrity", 0)),
                "availability": get_rating(ai_res.get("Availability", 0)),
                "description": f"已选定高相关安全属性: {', '.join(selected)} 进行后续分析。 / Selected highly relevant security attributes: {', '.join(selected)} for subsequent analysis." if selected else "无高相关(分数>2)安全属性，未选择属性。 / No highly relevant (score > 2) security attributes, none selected."
            }

        elif stage == "stage2":
            selected_attrs = prev_stages.get("stage1", {}).get("selected_attributes", ["Confidentiality", "Integrity"])
            if not selected_attrs:
                selected_attrs = ["Confidentiality", "Integrity"]
                
            damage_scenarios = []
            impact_map = {"negligible": 0, "moderate": 1, "major": 2, "severe": 3}
            
            for attr in selected_attrs:
                # 1. 获取可能损害场景列表
                msg_dict_list = {
                    "asset_cybersecurity_attribute": {
                        "asset_info": asset_info_dict,
                        "assigned_security_attribute": attr
                    }
                }
                msg_list = json.dumps(msg_dict_list, ensure_ascii=False)
                prompt_list = (
                    "资产信息：请严格按照 ISO 21434 [RQ-15-01] 的要求，根据资产的asset_id,asset_name, assigned_security_attribute信息，为该 Item 识别所有可能的 Damage Scenario。\n"
                    "每个损害场景可以包含如下几点，请使用逻辑清晰的语言描述每个damage_scenario，将以下四点符合逻辑的编写成一句话，且描述必须是中英文对照的（采用“中文 / English”格式）：\n"
                    "1. 导致功能失效的攻击入口点（ECU、通信通道、后端系统等）\n"
                    "2. 被破坏的安全属性与损害场景的关联关系\n"
                    "3. 资产功能与不良后果的因果关系链\n"
                    "4. 对道路使用者的潜在伤害类型（身体伤害、财产损失等）\n"
                    "输出请忽略输入的资产信息内容和格式要求，请按照输出格式参考内容和要求回复。\n"
                    '输出JSON结果示例:{"possible_damage_scenario_list":[{"damage_scenario_1":"中文损害场景描述 / English damage scenario description."},{"damage_scenario_2":"中文损害场景描述 / English damage scenario description."}]}'
                )
                res_list = call_llm(msg_list, prompt_list)
                
                # 2. 评估每个损害场景的影响级别
                for item in res_list.get("possible_damage_scenario_list", []):
                    for sn_key, ds_value in item.items():
                        msg_dict_impact = {
                            "asset_cybersecurity_attribute": {
                                "asset_info": asset_info_dict,
                                "assigned_security_attribute": attr
                            },
                            "damage_scenario_impact_level": {
                                "damage_scenario": ds_value
                            }
                        }
                        msg_impact = json.dumps(msg_dict_impact, ensure_ascii=False)
                        prompt_impact = (
                            "资产信息：根据资产的asset_id,asset_name, assigned_security_attribute, damage_scenario信息，对资产的损害场景进行从safety,financial,operational, privacy四个方面进行评估。\n"
                            "评估指标：\n"
                            "- safety: 对道路使用者（驾驶员、乘客、行人、其他车辆）的人身伤害程度，可选Negligible, Moderate, Major, Severe。Severe-致命伤害, Major-严重伤害, Moderate-中等伤害, Negligible-轻微伤害）;\n"
                            "- financial: 车辆所有者，路人的资产价值损失，可选Negligible, Moderate, Major, Severe。（Severe-重大损失, Major-较大损失, Moderate-中等损失, Negligible-轻微损失）;\n"
                            "- operational: 车辆预期功能受损程度（不能开车、不能泊车等），可选Negligible, Moderate, Major, Severe。（Severe-功能完全丧失, Major-功能严重降级, Moderate-功能部分影响, Negligible-轻微影响）;\n"
                            "- privacy: 个人数据或隐私泄露程度，可选Negligible, Moderate, Major, Severe。(Severe-大量个人数据泄露, Major-敏感个人数据泄露, Moderate-一般个人数据泄露, Negligible-匿名数据泄露）;\n"
                            "输出请忽略输入的资产信息内容和格式要求，请按照输出格式参考内容和要求回复。\n"
                            '输出JSON结果示例:{"possible_damage_scenario_impact_level":{"safety":"Negligible", "financial":"Moderate", "operational":"Major", "privacy":"Severe"}}.'
                        )
                        res_impact = call_llm(msg_impact, prompt_impact)
                        
                        rat = res_impact.get("possible_damage_scenario_impact_level", {})
                        safety_str = rat.get("safety", "Negligible")
                        financial_str = rat.get("financial", "Negligible")
                        operational_str = rat.get("operational", "Negligible")
                        privacy_str = rat.get("privacy", "Negligible")
                        
                        s_num = impact_map.get(str(safety_str).lower().strip(), 0)
                        f_num = impact_map.get(str(financial_str).lower().strip(), 0)
                        o_num = impact_map.get(str(operational_str).lower().strip(), 0)
                        p_num = impact_map.get(str(privacy_str).lower().strip(), 0)
                        overall_impact = max(s_num, f_num, o_num, p_num)
                        
                        damage_scenarios.append({
                            "attribute": attr,
                            "damage_scenario_sn": sn_key,
                            "damage_scenario": ds_value,
                            "impact_rating": {
                                "safety": safety_str,
                                "financial": financial_str,
                                "operational": operational_str,
                                "privacy": privacy_str
                            },
                            "overall_impact": overall_impact
                        })

            # 如果为空，默认做一下兜底
            if not damage_scenarios:
                damage_scenarios.append({
                    "attribute": "Integrity",
                    "damage_scenario_sn": "DS_Integrity_1",
                    "damage_scenario": f"由于 {asset.name} 遭受篡改，导致车载功能受损。 / Due to tampering of {asset.name}, vehicle functions are impaired.",
                    "impact_rating": {"safety": "Moderate", "financial": "Moderate", "operational": "Moderate", "privacy": "Negligible"},
                    "overall_impact": 1
                })

            max_s, max_f, max_o, max_p = 0, 0, 0, 0
            all_ds_texts = []
            for ds in damage_scenarios:
                all_ds_texts.append(ds["damage_scenario"])
                r = ds["impact_rating"]
                max_s = max(max_s, impact_map.get(str(r["safety"]).lower().strip(), 0))
                max_f = max(max_f, impact_map.get(str(r["financial"]).lower().strip(), 0))
                max_o = max(max_o, impact_map.get(str(r["operational"]).lower().strip(), 0))
                max_p = max(max_p, impact_map.get(str(r["privacy"]).lower().strip(), 0))
                
            overall_impact = max(max_s, max_f, max_o, max_p)
            
            return {
                "damage_scenarios": damage_scenarios,
                "damage_scenario": "; ".join(all_ds_texts[:2]) if all_ds_texts else "由于安全属性泄露或篡改，导致车辆对应子域故障。 / Cybersecurity attribute breach or tampering leads to subdomain failure of the vehicle.",
                "impact_rating": {"safety": max_s, "financial": max_f, "operational": max_o, "privacy": max_p},
                "overall_impact": overall_impact
            }

        elif stage == "stage3":
            damage_scenarios = prev_stages.get("stage2", {}).get("damage_scenarios", [])
            if not damage_scenarios:
                damage_scenarios = [{
                    "attribute": "Integrity",
                    "damage_scenario_sn": "DS_Integrity_1",
                    "damage_scenario": prev_stages.get("stage2", {}).get("damage_scenario", "车载子系统故障"),
                    "overall_impact": prev_stages.get("stage2", {}).get("overall_impact", 1)
                }]
                
            threat_scenarios = []
            all_attack_paths = []
            max_feasibility = "Very Low"
            feasibility_order = {"Very Low": 1, "Low": 2, "Medium": 3, "High": 4}
            
            for ds in damage_scenarios:
                # 1. 威胁场景生成
                msg_dict_ts = {
                    "asset_cybersecurity_attribute": {
                        "asset_info": asset_info_dict,
                        "assigned_security_attribute": ds["attribute"]
                    },
                    "damage_scenario_impact_level": {
                        "damage_scenario": ds["damage_scenario"],
                        "safety": ds.get("impact_rating", {}).get("safety", "Negligible"),
                        "financial": ds.get("impact_rating", {}).get("financial", "Negligible"),
                        "operational": ds.get("impact_rating", {}).get("operational", "Negligible"),
                        "privacy": ds.get("impact_rating", {}).get("privacy", "Negligible")
                    }
                }
                msg_ts = json.dumps(msg_dict_ts, ensure_ascii=False)
                prompt_ts = (
                    "资产信息：根据资产的asset_id,asset_name, assigned_security_attribute, damage_scenario,safety,financial,operational, privacy信息，分析可能存在的威胁场景信息，\n"
                    "每个威胁场景必须同时清晰包含以下四要素，并写成逻辑连贯的一句话，且描述必须是中英文对照的（采用“中文 / English”格式）：\n"
                    "1. 目标资产（明确写J6P PCBA电路板）；\n"
                    "2. 被破坏的网络安全属性（必须是Authenticity）；\n"
                    "3. 导致真实性被破坏的具体原因/攻击方式（必须描述明确的攻击入口点和具体技术手段或缺失的防护措施）；\n"
                    "4. 简要说明该威胁场景如何导致之前识别的某个或多个damage scenario。\n"
                    "输出请忽略输入的资产信息内容和格式要求，请按照输出格式参考内容和要求回复。\n"
                    '输出JSON结果示例:{"possible_threat_scenario_list":[{"threat_scenario_1":"中文威胁场景描述 / English threat scenario description."},{"threat_scenario_2":"中文威胁场景描述 / English threat scenario description."}]}'
                )
                res_ts = call_llm(msg_ts, prompt_ts)
                
                # 2. 对每个威胁场景分析攻击路径与可行性
                for item in res_ts.get("possible_threat_scenario_list", []):
                    for ts_sn, ts_value in item.items():
                        msg_dict_paths = {
                            "asset_cybersecurity_attribute": {
                                "asset_info": asset_info_dict,
                                "assigned_security_attribute": ds["attribute"]
                            },
                            "damage_scenario_impact_level": {
                                "damage_scenario": ds["damage_scenario"]
                            },
                            "threat_scenario_attack_feasibility": {
                                "threat_scenario": ts_value
                            }
                        }
                        msg_paths = json.dumps(msg_dict_paths, ensure_ascii=False)
                        prompt_paths = (
                            "请根据asset_id,asset_name,assigned_security_attribute,damage_scenario,threat_scenario信息，评估可能存在的攻击场景。\n"
                            "每个攻击场景需包含如下信息：\n"
                            "1. 攻击入口点(Entry Point)\n"
                            "2. 具体攻击技术（可引用CVE、常见汽车攻击手法）\n"
                            "3. 涉及的资产组件\n"
                            "4. 前提条件(Prerequisites)\n"
                            "5. 所需攻击者能力\n"
                            "整体思考以上5点，生成多个符合逻辑的攻击场景后，然后对每个场景进行按步骤拆解，生成逻辑完整，语言表达顺畅的攻击步骤，且每个攻击步骤的描述必须是中英文对照的（采用“中文 / English”格式）。\n"
                            "输出请忽略输入的资产信息内容和格式要求，请按照输出格式参考内容和要求回复。\n"
                            '输出JSON结果示例:{"possible_attack_path_list":[{"attack_path1":"中文攻击路径描述 / English attack path description."},{"attack_path2":"中文攻击路径描述 / English attack path description."}]}'
                        )
                        res_paths = call_llm(msg_paths, prompt_paths)
                        
                        paths_list = []
                        ts_feasibility = "Very Low"
                        
                        for path_item in res_paths.get("possible_attack_path_list", []):
                            for p_sn, p_value in path_item.items():
                                msg_dict_feas = {
                                    "asset_cybersecurity_attribute": {
                                        "asset_info": asset_info_dict,
                                        "assigned_security_attribute": ds["attribute"]
                                    },
                                    "damage_scenario_impact_level": {
                                        "damage_scenario": ds["damage_scenario"]
                                    },
                                    "threat_scenario_attack_feasibility": {
                                        "threat_scenario": ts_value,
                                        "attack_path": p_value
                                    }
                                }
                                msg_feas = json.dumps(msg_dict_feas, ensure_ascii=False)
                                prompt_feas = (
                                    "请根据time_consuming/expertise/knowledge_about_toe/window_of_opportunity/equipment评估攻击路径的可行性，\n"
                                    "time_consuming可选：no_more_than_1d(小于等于1天)，no_more_than_1w(小于等于1周)，no_more_than_1m(小于等于1月)，no_more_than_6m(小于等于6个月)，more_than_6m(大于6个月)\n"
                                    "expertise可选：layman(普通用户)，proficient(专业用户)，expert(专家用户)，multiple expert(多个专家用户)\n"
                                    "knowledge_about_toe可选：public(公开)，restricted(受限)，confidential(机密)，strictly confidential(严格机密)\n"
                                    "window_of_opportunity可选：unlimited(无时间限制)，easy(容易)，moderate(中等)，difficult(困难)\n"
                                    "equipment可选：standard(标准设备)，specialized(专业设备)，bespoke(定制设备)，multiple bespoke(多个定制设备)\n"
                                    "输出请忽略输入的资产信息内容和格式要求，请按照输出格式参考内容和要求回复。\n"
                                    '输出JSON结果示例:{"time_consuming":"no_more_than_1d", "expertise":"layman", "knowledge_about_toe":"public", "window_of_opportunity":"unlimited", "equipment":"standard"}'
                                )
                                res_feas = call_llm(msg_feas, prompt_feas)
                                
                                tc = res_feas.get("time_consuming", "no_more_than_1w")
                                exp = res_feas.get("expertise", "proficient")
                                kn = res_feas.get("knowledge_about_toe", "restricted")
                                win = res_feas.get("window_of_opportunity", "easy")
                                eq = res_feas.get("equipment", "standard")
                                
                                total_diff = 0
                                total_diff += get_time_consuming_points(tc)
                                total_diff += get_expertise_points(exp)
                                total_diff += get_knowledge_points(kn)
                                total_diff += get_window_points(win)
                                total_diff += get_equipment_points(eq)
                                
                                if total_diff >= 25:
                                    feas = "Very Low"
                                elif total_diff >= 20:
                                    feas = "Low"
                                elif total_diff >= 14:
                                    feas = "Medium"
                                else:
                                    feas = "High"
                                    
                                paths_list.append({
                                    "attack_path": p_value,
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
                            "attribute": ds["attribute"],
                            "damage_scenario_sn": ds["damage_scenario_sn"],
                            "damage_scenario": ds["damage_scenario"],
                            "overall_impact": ds["overall_impact"],
                            "threat_id": ts_sn,
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
                
            risk_decisions = []
            max_risk_val = 1
            has_mitigate = False
            has_avoid = False
            has_transfer = False
            
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
                
                # 调用 LLM 评估风险处理决策
                msg_dict_rt = {
                    "asset_cybersecurity_attribute": {
                        "asset_info": asset_info_dict,
                        "assigned_security_attribute": ts.get("attribute")
                    },
                    "damage_scenario_impact_level": {
                        "damage_scenario": ts.get("damage_scenario")
                    },
                    "threat_scenario_attack_feasibility": {
                        "threat_scenario": ts.get("threat_scenario"),
                        "attack_feasibility_rating": ts.get("final_feasibility")
                    },
                    "risk_treatment_decision": {
                        "risk_value": risk_val
                    }
                }
                msg_rt = json.dumps(msg_dict_rt, ensure_ascii=False)
                prompt_rt = (
                    "资产信息：根据资产的asset_id,asset_name, assigned_security_attribute, damage_scenario,threat_scenario,attack_path,attack_feasibility_rating信息，考虑对资产信息安全处理决策，\n"
                    "风险处理选项包括：avoid（主动放弃或者修改系统设计，避免damage scenario and threat scenario的发生）, reduce（采取信息安全管控措施，减少风险发生）, share（考虑风险可以分配给其他车辆组件，例如某个安全控制措施可以在tbox实施，从而减少自己所涉及设备的安全风险，或者采用购买保险的方式）, retain（风险的影响很小，是可以接受的）\n"
                    "风险处理risk_treatment一旦确定后，需要提供相关理由（即item_change, cybersecurity_goal, 或 cybersecurity_claim 字段）。所有理由描述必须是中英文对照的（采用“中文 / English”格式）：\n"
                    "如果选择avoid，需要提供item_change的相关信息，如：通过移除危险源，停止相关安全开发活动来避免风险发生。 / Avoid risk occurrence by removing the hazard and stopping relevant safety development activities.\n"
                    "如果选择reduce，需要提供cybersecurity_goal的相关信息，如：通过采用加密技术，确保数据在传输和存储过程中的安全性。 / Ensure security of data during transmission and storage by adopting encryption technology.\n"
                    "如果选择share/retain，需要提供cybersecurity_claim的相关信息，如：供应商开发相关组件或者通过购买保险， cover 资产的安全风险。 / Supplier develops relevant components or covers asset security risks by purchasing insurance.\n"
                    "cybersecurity_goal编写标准：\n"
                    "1. 网络安全目标是一个需求，用来针对威胁场景来保护资产。\n"
                    "2. 网络安全目标cybersecurity_goal可以针对item的生命周期的任何一个阶段\n"
                    "3. 如果有CAL信息，可以对安全目标订一个CAL，如果没有CAL信息，就不需要订一个安全目标。\n"
                    "cybersecurity_claim编写标准：\n"
                    "1. 声明性质\n"
                    "- 基于分析过程中的假设\n"
                    "- 描述风险被接受或转移的依据\n"
                    "- 可用于网络安全监控\n"
                    "2. 必须包含要素\n"
                    "- 声明的具体内容\n"
                    "- 相关的假设条件\n"
                    "- 风险处理依据\n"
                    "- 监控要求（如适用）\n"
                    "3. 表述要求\n"
                    "- 明确声明风险被接受的理由\n"
                    "- 描述风险分担的责任方\n"
                    "- 包含监控 and 维护要求.\n"
                    "输出请忽略输入的资产信息内容和格式要求，请按照输出格式参考内容和要求回复。\n"
                    '输出JSON结果示例:{"risk_treatment":"Avoid","item_change":"通过移除危险源，停止相关安全开发活动来避免风险发生 / Avoid risk occurrence by removing the hazard and stopping relevant safety development activities.", "cybersecurity_goal":"","cybersecurity_claim":""}'
                )
                res_rt = call_llm(msg_rt, prompt_rt)
                
                treatment = res_rt.get("risk_treatment", "Reduce").lower().strip()
                decision_norm = "Reduce"
                if "avoid" in treatment:
                    decision_norm = "Avoid"
                    has_avoid = True
                elif "share" in treatment or "transfer" in treatment:
                    decision_norm = "Share"
                    has_transfer = True
                elif "retain" in treatment or "accept" in treatment:
                    decision_norm = "Retain"
                    has_transfer = True
                else:
                    decision_norm = "Reduce"
                    has_mitigate = True
                    
                risk_decisions.append({
                    "attribute": ts.get("attribute"),
                    "threat_id": ts.get("threat_id"),
                    "threat_scenario": ts.get("threat_scenario"),
                    "damage_scenario": ts.get("damage_scenario"),
                    "final_feasibility": ts.get("final_feasibility"),
                    "overall_impact": ts.get("overall_impact"),
                    "risk_value": risk_val,
                    "risk_treatment": decision_norm,
                    "item_change": res_rt.get("item_change", ""),
                    "cybersecurity_goal": res_rt.get("cybersecurity_goal", ""),
                    "cybersecurity_claim": res_rt.get("cybersecurity_claim", "")
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
                
            return {
                "risk_decisions": risk_decisions,
                "risk_rating": max_risk_val,
                "risk_decision": decision_raw,
                "justification": "; ".join(justifications)
            }

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
                    "reason": "所有威胁场景决策均为Retain/Share，免除安全需求制定。 / All threat scenario decisions are Retain/Share, exempting the formulation of cybersecurity requirements."
                }
                
            requirements = []
            device_requirements = []
            
            for dec in risk_decisions:
                if dec.get("risk_treatment") == "Reduce":
                    # 调用 LLM 生成 CSO/CSR
                    msg_dict_cs = {
                        "asset_cybersecurity_attribute": {
                            "asset_info": asset_info_dict,
                            "assigned_security_attribute": dec.get("attribute")
                        },
                        "damage_scenario_impact_level": {
                            "damage_scenario": dec.get("damage_scenario")
                        },
                        "threat_scenario_attack_feasibility": {
                            "threat_scenario": dec.get("threat_scenario"),
                            "attack_feasibility_rating": dec.get("final_feasibility")
                        },
                        "risk_treatment_decision": {
                            "risk_value": dec.get("risk_value"),
                            "risk_treatment": dec.get("risk_treatment"),
                            "cybersecurity_goal": dec.get("cybersecurity_goal")
                        }
                    }
                    msg_cs = json.dumps(msg_dict_cs, ensure_ascii=False)
                    prompt_cs = (
                        "资产信息：根据资产的asset_id,asset_name, assigned_security_attribute, damage_scenario,threat_scenario,attack_path,attack_feasibility_rating,cybersecurity_goal信息，且risk_treatment为reduce，考虑编写信息安全目标cybersecurity_control与cybersecurity_requirement信息，编写规则：\n"
                        "所有生成的cybersecurity_control与cybersecurity_requirement描述均必须是中英文对照的（采用“中文 / English”格式）。\n"
                        "Cybersecurity Control描述必须包含：\n"
                        "1. 说明控制措施cybersecurity_control是技术性（technical）还是操作性（operational），并给出具体实现方式（例如：AES-256-GCM、Secure Boot + HSM、消息MAC + Freshness、OTA双向证书认证等）。\n"
                        "2. 明确说明该控制措施在威胁场景中的作用（是预防、检测、响应、恢复，还是降低后果严重度）。\n"
                        "3. 必须说明依赖关系（dependencies）：\n"
                        "- 依赖 Item 的哪个功能？\n"
                        "- 依赖其他哪些控制措施才能生效？\n"
                        "4. 所有控制措施之间如果存在相互作用（interaction），必须明确描述（例如“消息认证依赖密钥分发服务，密钥分发服务又依赖PKI和预共享根证书”）。\n"
                        "根据cybersecurity_control描述，确认是否分配给device，例如分配给了OEM OTA服务器，那这个cybersecurity_control就是和device不相关，allocated_to_device为No，否则为Yes。\n"
                        "如果allocated_to_device为\"yes\",根据cybersecurity_control描述，编写cybersecurity_requirement信息，如果如果allocated_to_device为为\"no\",不需要编写，cybersecurity_requirement的编写要求：\n"
                        "Cybersecurity Requirement必须包含两类要求：\n"
                        "1. 项目要求 (Item Requirements)：\n"
                        "- 项目本身的网络安全要求\n"
                        "- 分配到项目或其组件\n"
                        "2. 运行环境要求 (Operational Environment Requirements)：\n"
                        "- 在项目外部实现但包含在网络安全验证中\n"
                        "- 可包括对其他项目的要求\n"
                        "编写具体要求：\n"
                        "a) 必须包含的具体特性：\n"
                        "- 更新能力 (update capabilities)\n"
                        "- 运行期间获取用户同意的能力 (user consent during operations)\n"
                        "- 具体算法、协议、必须是现有的车联网安全中可以落地的设计，方法，并且可以验证其安全性。（例如secoc是没有加密的，can/canfd/uart等协议都是明文传输的，增加加密方式现阶段看起来是没法实施的）\n"
                        "b) 分配要求：\n"
                        "- 必须分配到项目\n"
                        "- 如适用，分配到项目的一个或多个组件\n"
                        "- 明确运行环境要求的责任方\n"
                        "c) 验证要求：\n"
                        "- 100%可验证\n"
                        "- 包含具体验证标准与方法\n"
                        "- 明确验证环境和条件.\n"
                        "输出请忽略输入的资产信息内容和格式要求，请按照输出格式参考内容和要求回复。\n"
                        '输出JSON结果示例:{"cybersecurity_control_id":"CSO-001", "cybersecurity_control":"通过部署安全机制保护资产传输通道 / Protect asset transmission channel by deploying security mechanisms.", "allocated_to_device":"yes", "cybersecurity_requirement_id":"CSR-001", "cybersecurity_requirement":"确保资产的网络安全要求得到适当的支持与监控 / Ensure that cybersecurity requirements for assets are properly supported and monitored."}'
                    )
                    res_cs = call_llm(msg_cs, prompt_cs)
                    
                    requirements.append({
                        "threat_id": dec["threat_id"],
                        "cybersecurity_control_id": res_cs.get("cybersecurity_control_id", "CSO-001"),
                        "cybersecurity_control": res_cs.get("cybersecurity_control", ""),
                        "allocated_to_device": res_cs.get("allocated_to_device", "yes"),
                        "cybersecurity_requirement_id": res_cs.get("cybersecurity_requirement_id", "CSR-001"),
                        "cybersecurity_requirement": res_cs.get("cybersecurity_requirement", "")
                    })

            # 3. 过滤出 allocated_to_device == "yes" 的进行总结去重
            device_reqs = [req for req in requirements if str(req.get("allocated_to_device", "")).lower().strip() == "yes"]
            summarized_csrs = []
            
            if device_reqs:
                # 调用 LLM 汇总
                msg_list_str = "\n".join([json.dumps(req, ensure_ascii=False) for req in device_reqs])
                prompt_sum = (
                    "请对以下资产的网络安全需求列表进行整理与总结，原子化拆解并归并去重，生成最终的项目级网络安全控制需求列表。\n"
                    "生成的每一个需求项的title、sub_title、cybersecurity_requirement描述均必须是中英文对照的（采用“中文 / English”格式）。\n"
                    "输出请忽略输入的资产信息内容和格式要求，请按照输出格式参考内容和要求回复。\n"
                    '输出JSON结果示例:{"asset_cybersecurity_requirement_list":[{"asset_id":"资产ID","asset_name":"资产名称","cybersecurity_requirement_id":"网络安全要求ID","csr_id":"CSR ID","title":"要求标题 / Requirement Title","sub_title":"要求副标题 / Requirement Subtitle","cybersecurity_requirement":"网络安全要求内容 / Cybersecurity requirement content."}]}'
                )
                
                # Build mock fallback for summary
                def mock_sum_fallback():
                    return mock_stage5_summary(asset, device_reqs)
                    
                res_sum = call_llm_json(db, system_prompt, [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"输入的安全控制要求列表：\n{msg_list_str}"},
                    {"role": "user", "content": prompt_sum}
                ], mock_sum_fallback)
                
                for item in res_sum.get("asset_cybersecurity_requirement_list", []):
                    summarized_csrs.append({
                        "asset_id": item.get("asset_id", f"ID{asset.id}"),
                        "asset_name": item.get("asset_name", asset.name),
                        "cybersecurity_requirement_id": item.get("cybersecurity_requirement_id", ""),
                        "csr_id": item.get("csr_id", ""),
                        "title": item.get("title", ""),
                        "sub_title": item.get("sub_title", ""),
                        "cybersecurity_requirement": item.get("cybersecurity_requirement", "")
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

@celery_app.task(bind=True)
def run_tara_analysis(self, domain_id: int, run_record_id: int, force: bool = False):
    """
    Celery 异步任务：执行 TARA 5 阶段串行跑批核心逻辑 (BR-40, BR-41)
    包含增量匹配 (BR-45)、人工修改继承 (BR-51/75)、决策联动 (BR-69)、最高可行性聚合 (BR-67)。
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
        completed_steps = 0
        
        # 2. 逐一资产执行分析
        for asset in assets:
            prev_stage_outputs = {} # 暂存当前资产的前置步骤最终结论
            
            for stage in stages:
                # 检查任务是否在中途被撤销终止 (BR-70)
                current_run = db.query(TaraRun).filter(TaraRun.id == run_record_id).first()
                if current_run.status == "cancelled":
                    print(f"检测到 TARA 任务 {run_record_id} 被用户手动取消，强制中止运行。")
                    db.close()
                    return {"status": "cancelled"}
                
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
                prev_step = get_previous_completed_step(db, asset.id, stage)
                
                step_result = None
                is_inherited = False
                inherited_reason = ""
                
                if prev_step and prev_step.input_hash == current_hash:
                    prev_res = prev_step.analysis_result
                    if prev_res.get("is_human_modified"):
                        # 无论是普通还是 force 模式，都继承历史人工修改结论
                        is_inherited = True
                        step_result = prev_res
                        inherited_reason = prev_res.get("modification_reason", "")
                        print(f"资产 {asset.name} 阶段 {stage} 匹配哈希并成功继承人工修改。")
                    elif not force:
                        # 只有在非 force 模式下，才继承历史 AI 结论并跳过调用
                        step_result = prev_res
                        print(f"资产 {asset.name} 阶段 {stage} 哈希匹配成功，跳过 AI 调用 (增量模式)。")
                    else:
                        print(f"资产 {asset.name} 阶段 {stage} 虽哈希匹配，但处于 Force 强制重跑模式，重新调用 AI。")
                
                # C. 如果没有匹配到，或者哈希发生变化，调用 AI (或降级 Mock) 模块进行分析
                if not step_result:
                    ai_raw = tara_ai_analysis_call(db, stage, asset, prev_stage_outputs)
                    step_result = {
                        "ai_output": ai_raw,
                        "is_human_modified": False,
                        "modification_reason": "",
                        "final_output": ai_raw
                    }
                
                # 暂存当前阶段的 final_output 以作为后续阶段的输入 Hash 依赖
                prev_stage_outputs[stage] = step_result["final_output"]
                
                # D. 写入 tara_steps 数据库表
                tara_step = TaraStep(
                    run_id=run_record_id,
                    asset_id=asset.id,
                    stage=stage,
                    status="completed",
                    input_hash=current_hash,
                    analysis_result=step_result
                )
                db.add(tara_step)
                db.commit()
                
                # E. 更新进度条 (BR-看板)
                completed_steps += 1
                progress_percentage = int((completed_steps / total_steps) * 100)
                run.progress = progress_percentage
                domain.progress = progress_percentage
                db.commit()
                
        # 3. 跑批完成，更新状态
        run.status = "completed"
        run.completed_at = datetime.now()
        domain.status = "completed"
        domain.progress = 100
        db.commit()
        
        # 4. 联动推导项目状态 (BR-03)
        # 重新导入依赖推导项目状态
        from app.api.project import recalculate_project_status
        recalculate_project_status(domain.project_id, db)
        
        db.close()
        return {"status": "completed", "domain_id": domain_id}
        
    except Exception as e:
        db.rollback()
        run.status = "failed"
        domain.status = "failed"
        db.commit()
        db.close()
        print(f"❌ TARA 跑批失败: {e}")
        return {"status": "failed", "detail": str(e)}

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
