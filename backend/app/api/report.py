from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from datetime import datetime
import hashlib
import os
from typing import List
import openpyxl
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
import csv

from app.core.database import get_db
from app.api.auth import get_current_user
from app.models.user import User
from app.models.domain import Domain
from app.models.project import Project
from app.models.asset import Asset
from app.models.tara_run import TaraRun
from app.models.tara_step import TaraStep

router = APIRouter(prefix="/reports", tags=["报告导出与脱敏服务"])

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
EXPORTS_DIR = os.path.join(PROJECT_ROOT, "exports_local")

# 建立导出文件夹
if not os.path.exists(EXPORTS_DIR):
    os.makedirs(EXPORTS_DIR)

def get_asset_sort_key(asset) -> tuple:
    type_order = {
        "hardware": 1,
        "software": 2,
        "data": 3,
        "communication": 4
    }
    t = str(asset.asset_type or "").lower().strip()
    return (type_order.get(t, 99), asset.id)

def get_asset_sn(asset) -> str:
    prefix_map = {
        "hardware": "H",
        "software": "S",
        "data": "D",
        "communication": "C"
    }
    t = str(asset.asset_type).lower().strip()
    prefix = prefix_map.get(t, "A")
    return f"{prefix}-001_{asset.id:03d}"

def get_csr_traceability_maps(stage5_out: dict, req_map: dict) -> tuple:
    """
    建立 TARA 原始需求与合并汇总需求之间的追溯关系
    Returns:
      tara_to_csr: dict, 原始需求文本 -> list of summarized CSR hash IDs
      csr_to_tara: dict, summarized CSR hash ID -> list of original requirement IDs (like CSR_XXXX)
    """
    tara_to_csr = {}
    csr_to_tara = {}
    
    requirements = stage5_out.get("requirements", [])
    summarized_csrs = stage5_out.get("summarized_csrs") or stage5_out.get("summarized_requirements") or []
    
    # 1. 建立 local ID -> 原始文本 的映射
    local_id_to_text = {}
    for req in requirements:
        text = str(req.get("cybersecurity_requirement") or "").strip()
        local_id = str(req.get("cybersecurity_requirement_id") or "").strip()
        if text and local_id:
            local_id_to_text[local_id] = text
            
    # 2. 通过 source_requirement_ids 或相似度匹配映射
    for csr in summarized_csrs:
        csr_text = str(csr.get("cybersecurity_requirement") or "").strip()
        if not csr_text:
            continue
        csr_hash_id = req_map.get(csr_text, csr.get("csr_id") or "N/A")
        
        s_ids = csr.get("source_requirement_ids") or []
        mapped_any = False
        
        for sid in s_ids:
            orig_text = local_id_to_text.get(sid)
            if orig_text:
                tara_to_csr.setdefault(orig_text, []).append(csr_hash_id)
                csr_to_tara.setdefault(csr_hash_id, []).append(sid)
                mapped_any = True
                
        # 若大模型未显式输出映射（如历史数据），通过文本包含与相似度兜底
        if not mapped_any:
            for orig_req in requirements:
                orig_text = str(orig_req.get("cybersecurity_requirement") or "").strip()
                if not orig_text or orig_text == "N/A":
                    continue
                orig_clean = orig_text.lower()
                csr_clean = csr_text.lower()
                if orig_clean == csr_clean or orig_clean in csr_clean or csr_clean in orig_clean:
                    tara_to_csr.setdefault(orig_text, []).append(csr_hash_id)
                    orig_id = orig_req.get("cybersecurity_requirement_id") or req_map.get(orig_text)
                    if orig_id:
                        csr_to_tara.setdefault(csr_hash_id, []).append(orig_id)
                        mapped_any = True
                        
            if not mapped_any:
                import re
                for orig_req in requirements:
                    orig_text = str(orig_req.get("cybersecurity_requirement") or "").strip()
                    if not orig_text or orig_text == "N/A":
                        continue
                    orig_words = set(re.findall(r'[\u4e00-\u9fa5]|\w+', orig_text.lower()))
                    csr_words = set(re.findall(r'[\u4e00-\u9fa5]|\w+', csr_text.lower()))
                    if orig_words and csr_words:
                        intersection = orig_words.intersection(csr_words)
                        union = orig_words.union(csr_words)
                        jaccard = len(intersection) / len(union)
                        if jaccard > 0.15:
                            tara_to_csr.setdefault(orig_text, []).append(csr_hash_id)
                            orig_id = orig_req.get("cybersecurity_requirement_id") or req_map.get(orig_text)
                            if orig_id:
                                csr_to_tara.setdefault(csr_hash_id, []).append(orig_id)
                                
    # 去重
    for k in tara_to_csr:
        tara_to_csr[k] = list(set(tara_to_csr[k]))
    for k in csr_to_tara:
        csr_to_tara[k] = list(set(csr_to_tara[k]))
        
    return tara_to_csr, csr_to_tara

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
                        import re
                        cleaned = re.sub(r'^\(\d+\)\s*', '', line).strip()
                        if cleaned and cleaned != "N/A":
                            req_set.add(cleaned)
                            
            summarized_csrs = out.get("summarized_csrs") or []
            for csr in summarized_csrs:
                rq = str(csr.get("cybersecurity_requirement") or "").strip()
                if rq and rq != "N/A":
                    for line in rq.split("\n"):
                        import re
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

def collect_report_data(steps: List[TaraStep], assets: List[Asset], desensitize: bool, hash_to_global_map: dict = None) -> List[dict]:
    """
    收集并平铺 TARA 各阶段分析结果数据，返回扁平化字典列表
    """
    steps_by_asset = {}
    for step in steps:
        if step.asset_id not in steps_by_asset:
            steps_by_asset[step.asset_id] = {}
        steps_by_asset[step.asset_id][step.stage] = step
        
    attr_cols = ["Authenticity", "Integrity", "Non-repudiation", "Confidentiality", "Availability", "Authorization", "Privacy"]
    chinese_suffix_map = {
        "Authenticity": "真实性",
        "Integrity": "完整性",
        "Non-repudiation": "抗抵赖性",
        "Confidentiality": "机密性",
        "Availability": "可用性",
        "Authorization": "授权性",
        "Privacy": "隐私性"
    }
    
    row_num = 1
    rows = []
    
    # 对资产按类型（硬件、软件、数据、通信）排序后依次展开子树
    for asset in sorted(assets, key=get_asset_sort_key):
        asset_steps = steps_by_asset.get(asset.id, {})
        if not asset_steps:
            continue
            
        stage1_out = asset_steps.get("stage1").analysis_result.get("final_output", {}) if asset_steps.get("stage1") else {}
        stage2_out = asset_steps.get("stage2").analysis_result.get("final_output", {}) if asset_steps.get("stage2") else {}
        stage3_out = asset_steps.get("stage3").analysis_result.get("final_output", {}) if asset_steps.get("stage3") else {}
        stage4_out = asset_steps.get("stage4").analysis_result.get("final_output", {}) if asset_steps.get("stage4") else {}
        stage5_out = asset_steps.get("stage5").analysis_result.get("final_output", {}) if asset_steps.get("stage5") else {}
        
        # 提取高相关属性
        selected_attrs = stage1_out.get("selected_attributes", [])
        if not selected_attrs:
            attrs_dict = stage1_out.get("attributes", {})
            if isinstance(attrs_dict, dict):
                selected_attrs = [k for k, v in attrs_dict.items() if int(v or 0) > 2]
        if not selected_attrs:
            selected_attrs = []
            for attr in attr_cols:
                if stage1_out.get(attr.lower()) not in [None, "None", "Low"]:
                    selected_attrs.append(attr)
        if not selected_attrs:
            selected_attrs = ["Confidentiality", "Integrity", "Availability"]
            
        for attr in selected_attrs:
            # 损害场景
            damage_scenarios = stage2_out.get("damage_scenarios", [])
            if not damage_scenarios:
                if stage2_out.get("damage_scenario"):
                    damage_scenarios = [{
                        "attribute": attr,
                        "damage_scenario_sn": "DS_00001",
                        "damage_scenario": stage2_out.get("damage_scenario"),
                        "impact_rating": stage2_out.get("impact_rating", {}),
                        "overall_impact": stage2_out.get("overall_impact", "Medium")
                    }]
            
            matching_ds = [ds for ds in damage_scenarios if ds.get("attribute") == attr]
            if not matching_ds:
                matching_ds = [{
                    "attribute": attr,
                    "damage_scenario_sn": "DS_N/A",
                    "damage_scenario": "N/A",
                    "impact_rating": {"safety": "Negligible", "financial": "Negligible", "operational": "Negligible", "privacy": "Negligible"},
                    "overall_impact": "Negligible"
                }]
                
            for ds in matching_ds:
                # 威胁场景
                threat_scenarios = stage3_out.get("threat_scenarios", [])
                if not threat_scenarios:
                    if stage3_out.get("threat_scenario"):
                        threat_scenarios = [{
                            "attribute": attr,
                            "damage_scenario_sn": ds.get("damage_scenario_sn"),
                            "threat_id": "TS_00001",
                            "threat_scenario": stage3_out.get("threat_scenario"),
                            "attack_paths": stage3_out.get("attack_paths", []),
                            "final_feasibility": stage3_out.get("final_feasibility", "Medium")
                        }]
                
                matching_ts = [ts for ts in threat_scenarios if ts.get("attribute") == attr and ts.get("damage_scenario_sn") == ds.get("damage_scenario_sn")]
                if not matching_ts:
                    matching_ts = [{
                        "attribute": attr,
                        "damage_scenario_sn": ds.get("damage_scenario_sn"),
                        "threat_id": "TS_N/A",
                        "threat_scenario": "N/A",
                        "attack_paths": [],
                        "final_feasibility": "Medium"
                    }]
                    
                for ts in matching_ts:
                    # 风险处理决策
                    risk_decisions = stage4_out.get("risk_decisions", [])
                    rd = None
                    for r_dec in risk_decisions:
                        if r_dec.get("threat_id") == ts.get("threat_id"):
                            rd = r_dec
                            break
                    if not rd:
                        for r_dec in risk_decisions:
                            if r_dec.get("attribute") == attr:
                                rd = r_dec
                                break
                    if not rd:
                        rd = {
                            "risk_value": stage4_out.get("risk_rating", 1),
                            "risk_treatment": stage4_out.get("risk_decision", "Retain"),
                            "item_change": "",
                            "cybersecurity_goal": "",
                            "cybersecurity_claim": ""
                        }
                        
                    # 网络安全控制与要求
                    requirements = stage5_out.get("requirements", [])
                    matching_reqs = [r for r in requirements if r.get("threat_id") == ts.get("threat_id")]
                    if not matching_reqs:
                        # 兜底：如果为空，但顶级存在 cso 或是 csr，代表这可能是手工录入/人工修改的结构
                        cso_val = stage5_out.get("cso") or "N/A"
                        csr_list = stage5_out.get("csr") or []
                        if cso_val != "N/A" or csr_list:
                            if isinstance(csr_list, list) and csr_list:
                                matching_reqs = []
                                for idx, csr_text in enumerate(csr_list):
                                    matching_reqs.append({
                                        "cybersecurity_control": cso_val if idx == 0 else "N/A",
                                        "allocated_to_device": "yes",
                                        "cybersecurity_requirement": csr_text
                                    })
                            else:
                                csr_val = str(csr_list) if csr_list else "N/A"
                                matching_reqs = [{
                                    "cybersecurity_control": cso_val,
                                    "allocated_to_device": "yes",
                                    "cybersecurity_requirement": csr_val
                                }]
                        else:
                            matching_reqs = [{
                                "cybersecurity_control_id": "N/A",
                                "cybersecurity_control": "N/A",
                                "allocated_to_device": "No",
                                "cybersecurity_requirement_id": "N/A",
                                "cybersecurity_requirement": "N/A"
                            }]
                        
                    # 攻击路径
                    attack_paths = ts.get("attack_paths", [])
                    if not attack_paths:
                        if stage3_out.get("attack_paths"):
                            attack_paths = stage3_out.get("attack_paths")
                        else:
                            attack_paths = [{
                                "attack_path": "N/A",
                                "time_consuming": "no_more_than_1w",
                                "expertise": "proficient",
                                "knowledge_about_toe": "restricted",
                                "window_of_opportunity": "easy",
                                "equipment": "standard",
                                "difficulty": 9,
                                "feasibility": ts.get("final_feasibility", "Medium")
                            }]
                            
                    # Consolidated Requirements (Stage 5) for the columns
                    rt = rd.get("risk_treatment", "Retain")
                    rt_map = {
                        "avoid": "Avoid",
                        "reduce": "Reduce",
                        "mitigate": "Reduce",
                        "share": "Share",
                        "transfer": "Share",
                        "retain": "Retain",
                        "accept": "Retain"
                    }
                    rt_disp = rt_map.get(str(rt).lower(), rt)
                    
                    cybersecurity_control_id_val = "N/A"
                    cybersecurity_control_val = "N/A"
                    allocated_to_device_val = "No"
                    cybersecurity_requirement_id_val = "N/A"
                    cybersecurity_requirement_val = "N/A"
                    
                    if rt_disp == "Reduce":
                        control_ids = []
                        controls = []
                        allocs = []
                        req_ids = []
                        reqs = []
                        for r_idx, req in enumerate(matching_reqs):
                            c_id_val = req.get("cybersecurity_control_id") or "N/A"
                            c_val = req.get("cybersecurity_control") or "N/A"
                            a_val = "Yes" if str(req.get("allocated_to_device", "No")).lower() in ["yes", "true"] else "No"
                            rq_id_val = req.get("cybersecurity_requirement_id") or "N/A"
                            rq_val = req.get("cybersecurity_requirement") or "N/A"
                            if len(matching_reqs) > 1:
                                control_ids.append(f"({r_idx+1}) {c_id_val}")
                                controls.append(f"({r_idx+1}) {c_val}")
                                allocs.append(f"({r_idx+1}) {a_val}")
                                req_ids.append(f"({r_idx+1}) {rq_id_val}")
                                reqs.append(f"({r_idx+1}) {rq_val}")
                            else:
                                control_ids.append(c_id_val)
                                controls.append(c_val)
                                allocs.append(a_val)
                                req_ids.append(rq_id_val)
                                reqs.append(rq_val)
                        cybersecurity_control_id_val = "\n".join(control_ids)
                        cybersecurity_control_val = "\n".join(controls)
                        allocated_to_device_val = "\n".join(allocs)
                        cybersecurity_requirement_id_val = "\n".join(req_ids)
                        cybersecurity_requirement_val = "\n".join(reqs)
                    elif rt_disp == "Avoid":
                        cybersecurity_requirement_val = rd.get("item_change") or "N/A"
                        
                    for ap in attack_paths:
                        row_data = {}
                        row_data["number"] = f"T_{row_num:04d}"
                        row_data["asset_sn"] = get_asset_sn(asset)
                        row_data["asset_name"] = asset.name
                        row_data["asset_id"] = asset.id
                        
                        for c in attr_cols:
                            row_data[c] = "Yes" if c == attr else None
                            
                        row_data["attribute_result"] = f"{chinese_suffix_map.get(attr, '')} / {attr}"
                        row_data["damage_scenario_sn"] = ds.get("damage_scenario_sn", "")
                        row_data["damage_scenario"] = ds.get("damage_scenario", "")
                        
                        impact_rat = ds.get("impact_rating", {})
                        def to_str_rating(val):
                            if isinstance(val, int):
                                return ["Negligible", "Moderate", "Major", "Severe"][min(max(val, 0), 3)]
                            if isinstance(val, str) and val.isdigit():
                                return ["Negligible", "Moderate", "Major", "Severe"][min(max(int(val), 0), 3)]
                            if isinstance(val, str) and val.strip().lower() in ["negligible", "moderate", "major", "severe"]:
                                return val.strip().capitalize()
                            return str(val) if val else "Negligible"

                        row_data["safety"] = to_str_rating(impact_rat.get("safety"))
                        row_data["financial"] = to_str_rating(impact_rat.get("financial"))
                        row_data["operational"] = to_str_rating(impact_rat.get("operational"))
                        row_data["privacy"] = to_str_rating(impact_rat.get("privacy"))
                        row_data["overall_impact"] = to_str_rating(ds.get("overall_impact"))
                        
                        row_data["threat_scenario"] = ts.get("threat_scenario", "")
                        
                        # Attack Path & Feasibility Columns
                        if desensitize:
                            row_data["attack_path"] = "攻击路径分析细节已脱敏过滤。 / Attack path details have been desensitized and filtered."
                            row_data["time_consuming"] = "N/A"
                            row_data["expertise"] = "N/A"
                            row_data["knowledge_about_toe"] = "N/A"
                            row_data["window_of_opportunity"] = "N/A"
                            row_data["equipment"] = "N/A"
                            row_data["difficulty"] = "N/A"
                        else:
                            raw_ap = ap.get("attack_path", "")
                            if isinstance(raw_ap, dict):
                                ap_lines = []
                                if raw_ap.get("entry_point"):
                                    ap_lines.append(f"入口点 / Entry Point: {raw_ap.get('entry_point')}")
                                if raw_ap.get("attack_technique"):
                                    ap_lines.append(f"技术手法 / Attack Technique: {raw_ap.get('attack_technique')}")
                                if raw_ap.get("attack_steps"):
                                    steps_list = raw_ap.get("attack_steps")
                                    if isinstance(steps_list, list):
                                        ap_lines.append("步骤 / Steps:\n" + "\n".join(steps_list))
                                    else:
                                        ap_lines.append(f"步骤 / Steps: {steps_list}")
                                row_data["attack_path"] = "\n".join(ap_lines)
                            elif isinstance(ap, dict) and "entry_point" in ap:
                                ap_lines = []
                                if ap.get("entry_point"):
                                    ap_lines.append(f"入口点 / Entry Point: {ap.get('entry_point')}")
                                if ap.get("attack_technique"):
                                    ap_lines.append(f"技术手法 / Attack Technique: {ap.get('attack_technique')}")
                                if ap.get("attack_steps"):
                                    steps_list = ap.get("attack_steps")
                                    if isinstance(steps_list, list):
                                        ap_lines.append("步骤 / Steps:\n" + "\n".join(steps_list))
                                    else:
                                        ap_lines.append(f"步骤 / Steps: {steps_list}")
                                row_data["attack_path"] = "\n".join(ap_lines)
                            else:
                                row_data["attack_path"] = str(raw_ap) if raw_ap else (ap.get("method", "") or str(ap))
                                
                            tc = ap.get("time_consuming", "")
                            exp = ap.get("expertise", "")
                            toe = ap.get("knowledge_about_toe", "")
                            win = ap.get("window_of_opportunity", "")
                            eq = ap.get("equipment", "")
                            
                            tc_map = {
                                "no_more_than_1d": "≤ 1 d", "no_more_than_1w": "≤ 1 w", "no_more_than_1m": "≤ 1 m",
                                "no_more_than_6m": "≤ 6 m", "more_than_6m": "> 6 m"
                            }
                            exp_map = {
                                "layman": "Layman", "proficient": "Proficient", "expert": "Expert", "multiple_expert": "Multiple Expert"
                            }
                            toe_map = {
                                "public": "Public", "restricted": "Restricted", "confidential": "Confidential",
                                "strictly_confidential": "Strictly Confidential"
                            }
                            win_map = {
                                "unlimited": "Unlimited", "easy": "Easy", "moderate": "Moderate", "difficult": "Difficult"
                            }
                            eq_map = {
                                "standard": "Standard", "specialized": "Specialized", "bespoke": "Bespoke",
                                "multiple_bespoke": "Multiple Bespoke"
                            }
                            row_data["time_consuming"] = tc_map.get(tc, tc)
                            row_data["expertise"] = exp_map.get(exp, exp)
                            row_data["knowledge_about_toe"] = toe_map.get(toe, toe)
                            row_data["window_of_opportunity"] = win_map.get(win, win)
                            row_data["equipment"] = eq_map.get(eq, eq)
                            
                            diff = ap.get("difficulty")
                            if diff is None:
                                t_pts = {"no_more_than_1d": 0, "no_more_than_1w": 1, "no_more_than_1m": 4, "no_more_than_6m": 17, "more_than_6m": 19}.get(tc, 1)
                                ex_pts = {"layman": 0, "proficient": 3, "expert": 6, "multiple_expert": 8}.get(exp, 3)
                                to_pts = {"public": 0, "restricted": 3, "confidential": 7, "strictly_confidential": 11}.get(toe, 3)
                                w_pts = {"unlimited": 0, "easy": 1, "moderate": 4, "difficult": 10}.get(win, 1)
                                eq_pts = {"standard": 0, "specialized": 4, "bespoke": 7, "multiple_bespoke": 9}.get(eq, 0)
                                diff = t_pts + ex_pts + to_pts + w_pts + eq_pts
                            row_data["difficulty"] = diff
                            
                        feas = ap.get("feasibility", "")
                        feas_map = {"verylow": "Very Low", "low": "Low", "medium": "Medium", "high": "High"}
                        feas_disp = feas_map.get(str(feas).lower().replace(" ", ""), feas)
                        row_data["af_level"] = feas_disp
                        
                        caf = rd.get("caf_level") or feas_disp
                        caf_disp = feas_map.get(str(caf).lower().replace(" ", ""), caf)
                        row_data["caf_level"] = caf_disp
                        
                        row_data["risk_value"] = rd.get("risk_value", "")
                        row_data["risk_treatment"] = rt_disp
                        row_data["cybersecurity_claim_id"] = rd.get("cybersecurity_claim_id", "N/A") if rt_disp in ["Share", "Retain"] else "N/A"
                        row_data["cybersecurity_claim"] = rd.get("cybersecurity_claim", "N/A") if rt_disp in ["Share", "Retain"] else "N/A"
                        
                        goal_id_val = rd.get("cybersecurity_goal_id") or rd.get("cso_id")
                        if not goal_id_val or goal_id_val == "N/A":
                            if matching_reqs and matching_reqs[0].get("cybersecurity_control_id"):
                                goal_id_val = matching_reqs[0].get("cybersecurity_control_id")
                            else:
                                goal_id_val = f"CSO_{attr}_{ts.get('threat_id')}"
                        row_data["cybersecurity_goal_id"] = goal_id_val if rt_disp == "Reduce" else "N/A"
                        row_data["cybersecurity_goal"] = rd.get("cybersecurity_goal", "N/A") if rt_disp == "Reduce" else "N/A"
                        
                        row_data["cybersecurity_control_id"] = cybersecurity_control_id_val
                        row_data["cybersecurity_control"] = cybersecurity_control_val
                        row_data["allocated_to_device"] = allocated_to_device_val
                        row_data["cybersecurity_requirement_id"] = cybersecurity_requirement_id_val
                        row_data["cybersecurity_requirement"] = cybersecurity_requirement_val
                        
                        rows.append(row_data)
                        row_num += 1
                        
    goal_map, claim_map, control_map, req_map = build_id_maps(steps)
    
    # Pre-build traceability maps for all assets
    asset_trace_maps = {}
    for asset in assets:
        asset_steps = steps_by_asset.get(asset.id, {})
        stage5_out = asset_steps.get("stage5").analysis_result.get("final_output", {}) if asset_steps.get("stage5") else {}
        asset_trace_maps[asset.id] = get_csr_traceability_maps(stage5_out, req_map)

    for row in rows:
        rt_disp = row.get("risk_treatment")
        is_exempted = rt_disp in ["Share", "Retain"]
        is_mitigated = rt_disp == "Reduce"

        # 1. Claims
        claim_val = str(row.get("cybersecurity_claim") or "").strip()
        if is_exempted and claim_val and claim_val != "N/A":
            row["cybersecurity_claim_id"] = claim_map.get(claim_val, "N/A")
        else:
            row["cybersecurity_claim_id"] = "N/A"
            row["cybersecurity_claim"] = "N/A" if (is_mitigated or rt_disp == "Avoid") else row.get("cybersecurity_claim", "N/A")

        # 2. Goals (CSO)
        goal_val = str(row.get("cybersecurity_goal") or "").strip()
        if is_mitigated and goal_val and goal_val != "N/A":
            row["cybersecurity_goal_id"] = goal_map.get(goal_val, "N/A")
        else:
            row["cybersecurity_goal_id"] = "N/A"
            row["cybersecurity_goal"] = "N/A" if (is_exempted or rt_disp == "Avoid") else row.get("cybersecurity_goal", "N/A")

        # 3. Controls
        control_val = str(row.get("cybersecurity_control") or "").strip()
        if is_mitigated and control_val and control_val != "N/A":
            lines = [line.strip() for line in control_val.split("\n") if line.strip()]
            new_ids = []
            for line in lines:
                import re
                cleaned = re.sub(r'^\(\d+\)\s*', '', line).strip()
                if cleaned and cleaned != "N/A":
                    new_ids.append(control_map.get(cleaned, "CSC-0000"))
            if len(new_ids) > 1:
                row["cybersecurity_control_id"] = "\n".join(f"({i+1}) {cid}" for i, cid in enumerate(new_ids))
            else:
                row["cybersecurity_control_id"] = new_ids[0] if new_ids else "N/A"
        else:
            row["cybersecurity_control_id"] = "N/A"

        # 4. Requirements (CSR)
        req_val = str(row.get("cybersecurity_requirement") or "").strip()
        if is_mitigated and req_val and req_val != "N/A":
            lines = [line.strip() for line in req_val.split("\n") if line.strip()]
            new_ids = []
            for line in lines:
                import re
                cleaned = re.sub(r'^\(\d+\)\s*', '', line).strip()
                if cleaned and cleaned != "N/A":
                    new_ids.append(req_map.get(cleaned, "CSR-0000"))
            if len(new_ids) > 1:
                row["cybersecurity_requirement_id"] = "\n".join(f"({i+1}) {rid}" for i, rid in enumerate(new_ids))
            else:
                row["cybersecurity_requirement_id"] = new_ids[0] if new_ids else "N/A"
        else:
            row["cybersecurity_requirement_id"] = "N/A"

        # 5. Merged CSR Traceability
        asset_id = row.get("asset_id")
        if asset_id and req_val and req_val != "N/A" and is_mitigated:
            tara_to_csr, _ = asset_trace_maps.get(asset_id, ({}, {}))
            lines = [line.strip() for line in req_val.split("\n") if line.strip()]
            matched_csr_ids = []
            for line in lines:
                import re
                cleaned = re.sub(r'^\(\d+\)\s*', '', line).strip()
                if cleaned and cleaned != "N/A":
                    matched_csr_ids.extend(tara_to_csr.get(cleaned, []))
            if matched_csr_ids:
                if hash_to_global_map:
                    mapped_ids = [hash_to_global_map.get(cid, cid) for cid in matched_csr_ids]
                else:
                    mapped_ids = matched_csr_ids
                row["merged_csr_id"] = ", ".join(sorted(list(set(mapped_ids))))
            else:
                row["merged_csr_id"] = "N/A"
        else:
            row["merged_csr_id"] = "N/A"

    return rows

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


# -------------------------------------------------------------------------
# ISO/SAE 21434 Canonical Atomic Cybersecurity Requirements Catalog (58 Items)
# -------------------------------------------------------------------------
CANONICAL_ATOMIC_CATALOG = {
    # 1. 硬件安全 / Hardware Security
    "CSR-HW-001": {
        "domain": "硬件安全 / Hardware Security",
        "category": "物理与硬件调试管控 / Physical & Hardware Debug Control",
        "title": "物理调试接口量产熔断锁定与受控授权解锁 / Physical Debug Interface Production Fuse-Locking and Authenticated Unlock",
        "req": "量产阶段应通过硬件一次性可编程熔丝（eFuse）永久禁用JTAG、SWD等物理调试接口，禁用状态应支持通过只读寄存器进行硬件校验；若售后维护必须保留调试能力，接口访问前必须完成基于非对称密码学的双向挑战应答强身份认证，仅授予最小必要特权并在断开或超时后恢复锁定。 / In production stage, physical debug interfaces such as JTAG and SWD shall be permanently disabled via hardware one-time programmable eFuses, and the disabled state shall be verifiable via read-only registers; if maintenance debug capability must be retained for after-sales, interface access must require mutual challenge-response strong authentication based on asymmetric cryptography, granting only least-privilege access and reverting to locked state upon disconnect or timeout."
    },
    "CSR-HW-002": {
        "domain": "硬件安全 / Hardware Security",
        "category": "物理防拆与硬件异常监测 / Physical Tamper & Hardware Anomaly Monitoring",
        "title": "物理防拆、传感器监测与PCB/线束完整性防护 / Physical Tamper Resistance, Sensor Monitoring and PCB/Harness Integrity Protection",
        "req": "ADCU外壳与关键连接器应具备物理防拆开关或拆解检测传感器，关键通信走线与密钥信号线应布设在PCB内层并实施布线混淆，敏感器件采用BGA/CSP封装并隐藏测试点；硬件应集成电压、时钟与温度监控传感器，检测到外壳拆解、物理探针侵入或供电/时钟异常时，应在规定时间内触发告警并进入预定义安全状态。 / The ADCU enclosure and critical connectors shall incorporate physical tamper switches or enclosure opening detection sensors; critical communication traces and key signal lines shall be routed on inner PCB layers with obfuscation, sensitive devices shall use BGA/CSP packaging with hidden test points; hardware shall integrate voltage, clock, and temperature monitoring sensors, triggering an alarm and transitioning into a predefined safe state within defined time upon detection of enclosure opening, physical probe intrusion, or power/clock glitches."
    },
    "CSR-HW-003": {
        "domain": "硬件安全 / Hardware Security",
        "category": "侧信道与故障注入防护 / Side-Channel and Fault Injection Defense",
        "title": "密码算法侧信道与硬件故障注入综合防护 / Cryptographic Side-Channel and Hardware Fault Injection Comprehensive Defense",
        "req": "密码学算法实现与密钥处理应采用恒定时间执行、掩码（Masking）与功耗平衡设计，防止时序分析与差分功耗分析（DPA）；芯片应具备电磁屏蔽与噪声注入防护；当硬件传感器检测到电压毛刺、时钟毛刺或温度越界等故障注入尝试时，必须在规定时间内触发安全复位、主动置零易失性密钥并记录安全事件。 / Cryptographic algorithm implementations and key handling shall adopt constant-time execution, masking, and power balancing design to prevent timing analysis and Differential Power Analysis (DPA); chips shall incorporate electromagnetic shielding and noise injection; when hardware sensors detect voltage glitches, clock glitches, or out-of-range temperature fault injection attempts, the system must trigger a secure reset, actively zeroize volatile keys, and log a security event within specified response time."
    },
    "CSR-HW-004": {
        "domain": "硬件安全 / Hardware Security",
        "category": "存储硬件加密与密钥绑定 / Storage Hardware Encryption & Key Binding",
        "title": "外部存储芯片硬件绑定加密与完整性保护 / External Storage Hardware-Bound Encryption and Integrity Protection",
        "req": "外置Flash/eMMC中存储的敏感固件、标定数据与私密信息必须使用经行业认可的高强度对称加密算法进行实时硬件在线加密与完整性保护；解密密钥必须绑定至SoC物理不可克隆功能（PUF）或芯片内部唯一硬件根密钥，脱机拆焊读取存储介质不得获取明文数据。 / Sensitive firmware, calibration data, and confidential information stored in external Flash/eMMC must be protected with real-time on-the-fly hardware encryption and integrity verification using an approved high-strength symmetric encryption algorithm; decryption keys must be bound to the SoC PUF or internal unique hardware root key, ensuring offline chip desoldering and dumping cannot obtain plaintext data."
    },

    # 2. 系统安全 / System Security
    "CSR-SYS-001": {
        "domain": "系统安全 / System Security",
        "category": "安全启动与固件签名 / Secure Boot & Firmware Signatures",
        "title": "分级安全启动链与固件签名完整性校验 / Hierarchical Secure Boot Chain and Firmware Signature Verification",
        "req": "系统必须建立从不可变BootROM、微控制器引导加载程序至应用系统的多级信任链；在执行每一级软件与固件前，必须使用基于硬件信任根的标准非对称数字签名校验其真实性与完整性，签名校验失败时系统必须拒绝加载并进入安全降级模式。 / The system must establish a hierarchical chain of trust originating from immutable BootROM and MCU bootloader to the operating application; before executing each software/firmware stage, its authenticity and integrity shall be verified using standard asymmetric digital signatures anchored in hardware root of trust, and the system shall refuse loading and transition to a safe degraded mode upon verification failure."
    },
    "CSR-SYS-002": {
        "domain": "系统安全 / System Security",
        "category": "固件防回滚保护 / Firmware Anti-Rollback Protection",
        "title": "固件版本硬件单调防回滚控制 / Hardware Monotonic Firmware Anti-Rollback Control",
        "req": "系统应利用硬件单调计数器（Monotonic Counter）或eFuse安全记录当前已激活的固件安全版本号；在执行任何固件更新前，必须核验新固件安全版本号不低于当前硬件记录值，拒绝加载包含已知已知漏洞的历史旧版本固件。 / The system shall utilize hardware monotonic counters or eFuses to securely record the currently active firmware security version; prior to executing any firmware update, the security version of the new firmware shall be verified to be greater than or equal to the hardware recorded value, strictly rejecting obsolete firmware versions with known vulnerabilities."
    },
    "CSR-SYS-003": {
        "domain": "系统安全 / System Security",
        "category": "A/B更新与容错恢复 / A/B Update & Fault-Tolerant Recovery",
        "title": "A/B双分区原子刷写与更新异常安全回滚 / A/B Dual-Partition Atomic Flashing and Update Failure Safe Rollback",
        "req": "ADCU系统固件刷写应采用A/B独立双分区或原子更新架构，在备用分区完成全量下载、完整性验签与自检通过后方可切换启动指针；若刷写中断、验签失败或启动超时，必须自动回滚至上一有效稳定系统版本，确保更新失效不导致车辆控制器死锁或变砖。 / ADCU system firmware flashing shall adopt an A/B dual-partition or atomic update architecture, switching boot pointers only after full download, signature integrity verification, and self-test pass on the secondary partition; if flashing is interrupted, verification fails, or boot times out, the system shall automatically roll back to the previous known valid stable version, ensuring update failures do not brick the controller."
    },
    "CSR-SYS-004": {
        "domain": "系统安全 / System Security",
        "category": "访问控制与系统加固 / Access Control & System Hardening",
        "title": "操作系统强制访问控制(MAC/SELinux)与最小权限管理 / OS Mandatory Access Control (MAC/SELinux) and Least Privilege Management",
        "req": "操作系统应启用强制访问控制（如SELinux Enforcing模式），为系统进程与业务应用实施最小权限配置；系统核心与只读关键业务分区必须挂载为只读（Read-Only），禁止应用程序以root特权常驻运行，禁止未授权跨安全域访问。 / The operating system shall enable Mandatory Access Control (such as SELinux in Enforcing mode) to enforce least-privilege policies on system processes and services; core system and critical partitions shall be mounted as Read-Only, preventing applications from running with unconstrained root privileges and disallowing unauthorized cross-domain access."
    },
    "CSR-SYS-005": {
        "domain": "系统安全 / System Security",
        "category": "编译加固与内存防护 / Compiler Hardening & Memory Protection",
        "title": "编译与引导加载期内存防护与漏洞利用缓解机制 / Compile-Time and Boot-Time Memory Exploit Mitigations",
        "req": "操作系统及可执行程序在编译与镜像构建阶段应启用标准内存漏洞利用缓解机制，包括地址空间布局随机化（ASLR/KASLR）、数据执行保护（DEP/NX）及栈溢出防护金丝雀（Stack Canary），防止攻击者利用缓冲区溢出执行任意代码。 / The operating system and executable binaries shall enable standard memory exploit mitigations during compilation and image generation, including Address Space Layout Randomization (ASLR/KASLR), Data Execution Prevention (DEP/NX), and Stack Canaries, preventing arbitrary code execution via buffer overflows."
    },
    "CSR-SYS-006": {
        "domain": "系统安全 / System Security",
        "category": "存储资源配额与可用性 / Storage Resource Quota & Availability",
        "title": "本地存储资源配额隔离、日志轮转与抗耗尽保护 / Local Storage Resource Quota Isolation, Log Rotation and Anti-Exhaustion Protection",
        "req": "系统应对日志记录、核心转储（Core Dump）与临时缓存划分独立的存储挂载分区并设定写入配额与速率限制；实施日志轮转（Log Rotation）与自动修剪机制，当存储空间达到预警阈值时触发告警并按优先级清理，防止磁盘耗尽导致核心功能异常。 / The system shall allocate isolated storage partitions with write quotas and rate limits for logs, core dumps, and temporary caches; automated log rotation and pruning policies shall be implemented, triggering an alert and clearing low-priority data when storage capacity reaches defined thresholds, preventing disk exhaustion from impairing core functions."
    },
    "CSR-SYS-007": {
        "domain": "系统安全 / System Security",
        "category": "特权与调试访问授权 / Privilege & Debug Access Authorization",
        "title": "调试与诊断特权命令基于角色的授权与会话控制 / Role-Based Authorization and Session Control for Debug and Diagnostic Privileged Commands",
        "req": "对调试控制台、系统诊断及维护接口实施基于角色的访问控制（RBAC），严格遵循默认拒绝策略；所有特权操作必须生成单次有效且具备安全时效（配置合理的无操作安全超时）的加密令牌，严禁在量产环境使用默认或通用密码凭据。 / Access to debug consoles, system diagnostics, and maintenance interfaces shall enforce Role-Based Access Control (RBAC) with a default-deny policy; all privileged commands must generate single-use, time-limited cryptographic session tokens, strictly prohibiting default or hardcoded credentials in production environments."
    },
    "CSR-SYS-008": {
        "domain": "系统安全 / System Security",
        "category": "服务健壮性与看门狗监控 / Service Robustness & Watchdog Monitoring",
        "title": "核心系统服务独立看门狗监控、请求限速与异常恢复 / Core System Service Independent Watchdog Monitoring, Rate Limiting and Fault Recovery",
        "req": "ADCU各核心系统服务应配置独立硬件与软件看门狗（Watchdog）进行健康状态监测；服务接口应配置输入命令速率限制与畸形报文过滤，当服务无响应或异常崩溃时应具备独立重启恢复能力，且不影响整车安全关键控制逻辑。 / Core system services on the ADCU shall be monitored by independent hardware and software watchdogs; service interfaces shall implement command rate limiting and malformed packet filtering, recovering via independent restart upon unresponsive failure without disrupting vehicle safety-critical control loops."
    },
    "CSR-SYS-009": {
        "domain": "系统安全 / System Security",
        "category": "运行时完整性与防御 / Runtime Integrity & Defense",
        "title": "运行时代码配置完整性周期度量与安全状态保护 / Periodic Runtime Code and Configuration Integrity Measurement and Safe State Protection",
        "req": "系统在运行阶段应对关键执行代码与核心配置实施周期性内存完整性度量；当检测到未授权代码执行、配置被非法篡改或内存破坏时，应触发预定义的安全降级状态或受控安全复位。 / The system shall periodically perform runtime memory integrity measurements on critical execution code and core configurations; upon detecting unauthorized code execution, illegal configuration tampering, or memory corruption, it shall trigger a predefined safe degraded state or controlled secure reset."
    },

    # 3. 密码安全与密钥管理 / Cryptography & Key Management
    "CSR-CRY-001": {
        "domain": "密码安全与密钥管理 / Cryptography & Key Management",
        "category": "硬件安全模块与密钥隔离 / HSM & Key Isolation",
        "title": "硬件安全模块(HSM/SHE)密钥隔离存储与安全运算 / HSM/SHE Hardware-Isolated Key Storage and Cryptographic Execution",
        "req": "系统主根密钥、SecOC密钥、诊断认证密钥及TLS私钥等必须存储在硬件安全模块（HSM/eHSM）的隔离受保护非易失存储区；所有敏感密码运算必须在安全硬件隔离环境内完成，密钥在任何生命周期阶段均严禁以明文形式导出至非安全环境。 / Root keys, SecOC keys, diagnostic authentication keys, and TLS private keys must be stored in isolated, protected non-volatile memory of a Hardware Security Module (HSM/eHSM); all cryptographic operations shall execute within the secure hardware enclave, and keys shall never be exported in plaintext to the non-secure world at any lifecycle stage."
    },
    "CSR-CRY-002": {
        "domain": "密码安全与密钥管理 / Cryptography & Key Management",
        "category": "PKI与证书生命周期管理 / PKI & Certificate Lifecycle Management",
        "title": "PKI数字证书链管理、私钥保护与证书动态轮换 / PKI Digital Certificate Chain Management, Private Key Protection and Dynamic Rotation",
        "req": "系统应建立车端公钥基础设施（PKI）证书信任管理体系，支持对车云通信证书、OTA服务器证书及诊断凭据实施完整分级证书链校验；设备私钥应在本地硬件生成并不向外泄露，系统应支持基于可信协议对临期或失效证书实施安全在线轮换。 / The system shall establish an in-vehicle PKI certificate trust management hierarchy, supporting complete hierarchical certificate chain validation for cloud communication, OTA servers, and diagnostic credentials; device private keys shall be generated locally within secure hardware, and the system shall support dynamic online certificate renewal/revocation via trusted protocols."
    },
    "CSR-CRY-003": {
        "domain": "密码安全与密钥管理 / Cryptography & Key Management",
        "category": "敏感载荷加密保护 / Sensitive Payload Encryption",
        "title": "敏感总线与感知数据端到端加密及动态密钥派生 / End-to-End Encryption and Dynamic Key Derivation for Sensitive Bus and Perception Payloads",
        "req": "在CAN/CANFD及车载以太网上传输的车辆控制敏感指令、高精度环境感知数据及隐私敏感信息应采用经行业认可的高强度对称加密算法实施应用层端到端加密与防重放保护；加密会话密钥应使用经合规KDF派生的动态密钥，并定期或按会话刷新。 / Sensitive vehicle control commands, high-precision perception payloads, and privacy data transmitted over CAN/CANFD and Ethernet shall be protected with application-layer end-to-end encryption using compliant high-strength symmetric encryption algorithms; session keys shall be dynamically derived using compliant KDFs and refreshed periodically or per-session."
    },
    "CSR-CRY-004": {
        "domain": "密码安全与密钥管理 / Cryptography & Key Management",
        "category": "固件密码学校验 / Firmware Cryptographic Verification",
        "title": "固件升级包非对称数字签名验签与机密性保护 / Firmware Update Package Asymmetric Digital Signature Verification and Confidentiality Protection",
        "req": "OTA及本地刷写使用的固件镜像必须采用标准非对称数字签名与密码安全杂凑算法进行数字签名；在传输与存储过程中，升级包应使用强对称加密算法保护机密性，仅在HSM完成验签且确认来源真实合法后，方可由解密模块在安全内存中解密并刷写。 / Firmware images for OTA and local flashing must be signed using standard asymmetric digital signatures and cryptographic hash functions; during transfer and storage, packages shall be encrypted with strong symmetric ciphers, and decryption in secure memory shall proceed only after the HSM successfully validates the signature and authentic origin."
    },
    "CSR-CRY-005": {
        "domain": "密码安全与密钥管理 / Cryptography & Key Management",
        "category": "日志加密与防泄密 / Log Encryption & Leak Prevention",
        "title": "调试日志与系统核心转储数据静态加密保护 / Encryption-at-Rest for Debug Logs and System Core Dump Data",
        "req": "包含系统内部状态、内存快照与通信报文的调试日志及核心转储（Core Dump）文件在落盘存储前必须使用高强度对称加密算法实施落盘加密保护；加密密钥由HSM安全派生，未经授权的读写请求一律拒绝，防止敏感数据通过物理脱机提取泄露。 / Debug logs and core dump files containing system internal state, memory snapshots, or bus traffic shall be encrypted at rest using high-strength symmetric encryption algorithms prior to disk storage; encryption keys shall be securely derived by the HSM, strictly rejecting unauthorized access to prevent sensitive data leakage via offline extraction."
    },
    "CSR-CRY-006": {
        "domain": "密码安全与密钥管理 / Cryptography & Key Management",
        "category": "密码硬件防护与安全零化 / Cryptographic Hardware Defense & Zeroization",
        "title": "密码模块抗侧信道加固与易失性密钥主动零化 / Cryptographic Module Side-Channel Hardening and Active Zeroization of Volatile Keys",
        "req": "密码运算引擎必须集成防DPA/SPA侧信道加固技术（掩码、功耗随机化与内部噪声源）；密码硬件应连接物理完整性与环境探测电路，当发生物理开壳、探针侵入或工作电压/时钟突变时，必须在1毫秒内主动对内部易失性工作密钥实施安全物理置零（Zeroization）。 / The cryptographic engine must incorporate DPA/SPA countermeasures (masking, power randomization, noise injection); the crypto hardware shall interface with tamper detection circuits, actively zeroizing volatile operational keys within 1 ms upon detecting enclosure opening, probe contact, or electrical/clock glitch anomalies."
    },

    # 4. 应用安全 / Application Security
    "CSR-APP-001": {
        "domain": "应用安全 / Application Security",
        "category": "诊断访问控制与防火墙 / Diagnostic Access Control & Firewall",
        "title": "UDS/DoIP诊断安全访问挑战应答鉴权与诊断防火墙过滤 / UDS/DoIP Diagnostic Security Access Challenge-Response Authentication and Firewall Filtering",
        "req": "ADCU诊断服务（UDS 0x27/0x29及DoIP）在执行任何刷写、标定或特权读写前，必须强制完成基于密码学挑战应答的安全访问认证；系统必须集成诊断防火墙，根据车辆当前运行状态（如车速>0时禁止刷写）过滤高危诊断服务；连续5次鉴权失败必须锁定诊断会话至少10分钟并记录安全日志。 / ADCU diagnostic services (UDS 0x27/0x29 and DoIP) shall enforce cryptographic challenge-response security access authentication before allowing flashing, calibration, or privileged read/write; a diagnostic firewall shall filter high-risk services based on vehicle operational states (e.g., inhibit flashing when speed > 0); 5 consecutive authentication failures shall lock the session for at least 10 minutes and log an event."
    },
    "CSR-APP-002": {
        "domain": "应用安全 / Application Security",
        "category": "输入验证与内存安全 / Input Validation & Memory Safety",
        "title": "外部输入严格校验、路径遍历防御与模糊测试验证 / Strict External Input Validation, Path Traversal Defense and Fuzzing Verification",
        "req": "应用程序对所有外部输入（网络报文、RPC参数、文件上传、传感器数据）必须实施严格的数据类型、长度、数值边界与编码格式校验，对文件路径执行规范化解析以防范目录遍历（Path Traversal）；在量产前必须对外部接口执行不少于72小时的高强度模糊测试（Fuzzing），确保畸形输入不会引发崩溃或内存破坏。 / Applications must perform strict type, length, boundary, and encoding validation on all external inputs (network packets, RPC params, file uploads, sensor data), resolving file paths canonicalized to prevent path traversal; interfaces shall undergo at least 72 hours of rigorous fuzz testing before SOP, ensuring malformed inputs cannot induce crash or memory corruption."
    },
    "CSR-APP-003": {
        "domain": "应用安全 / Application Security",
        "category": "服务鉴权与中间件安全 / Service Authentication & Middleware Security",
        "title": "车载中间件(SOME/IP/DDS)服务发布订阅鉴权与IPC凭据校验 / Automotive Middleware (SOME/IP/DDS) Publish-Subscribe Authentication and IPC Credential Verification",
        "req": "车载面向服务通信（SOME/IP、DDS）及进程间通信（IPC）必须实施严格的访问控制列表（ACL）；客户端订阅点云、图像帧及感知结果前必须通过服务注册中心完成双向身份认证与授权校验，跨进程消息传递必须校验发送方UID/GID对端凭据，拒绝非法进程冒充。 / Automotive service-oriented middleware (SOME/IP, DDS) and IPC mechanisms must enforce Access Control Lists (ACL); clients subscribing to point clouds, image frames, or perception results must complete mutual authentication and authorization via the service registry, and IPC messages must verify peer UID/GID credentials to prevent spoofing."
    },
    "CSR-APP-004": {
        "domain": "应用安全 / Application Security",
        "category": "本地数据接口访问控制 / Local Data Interface Access Control",
        "title": "本地日志、转储与缓存文件接口基于角色的最小权限访问控制 / Role-Based Least-Privilege Access Control for Local Logs, Dumps and Cache File Interfaces",
        "req": "对本地存储的OTA安装日志、调试日志、核心转储与应用缓存文件的读取、修改或导出接口，必须实施基于角色的访问控制（RBAC）与默认拒绝策略；所有调用方必须通过唯一身份认证，非特权进程严禁读取其它应用的安全凭据与受保护文件。 / Access to local OTA installation logs, debug logs, core dumps, and application cache files for read, modification, or export shall enforce Role-Based Access Control (RBAC) with a default-deny policy; all callers must verify unique identity, strictly preventing non-privileged processes from accessing credentials or protected data of other applications."
    },
    "CSR-APP-005": {
        "domain": "应用安全 / Application Security",
        "category": "车云API与OTA接口授权 / Vehicle-Cloud API & OTA Interface Auth",
        "title": "车云通信与OTA升级接口对象级授权及防越权保护 / Object-Level Authorization and Insecure Direct Object Reference (IDOR) Defense for Vehicle-Cloud and OTA APIs",
        "req": "车云通信接口、地图更新及OTA下载请求必须实施基于强令牌（Token）的双向授权控制；服务端与车端必须校验请求者对特定车辆VIN、目标固件版本及数据资源的合法所有权与访问范围，严格防范不安全对象引用（IDOR）与越权下载更新。 / Vehicle-cloud communication, map updates, and OTA download requests must enforce token-based mutual authorization; both cloud and vehicle shall verify requestor ownership and access boundaries for specific VINs, target firmware versions, and data assets, strictly preventing Insecure Direct Object Reference (IDOR) and unauthorized updates."
    },
    "CSR-APP-006": {
        "domain": "应用安全 / Application Security",
        "category": "防暴力破解与会话保护 / Anti-Brute-Force & Session Protection",
        "title": "认证连续失败会话锁定与防暴力破解保护机制 / Authentication Continuous Failure Session Lockout and Anti-Brute-Force Protection",
        "req": "所有提供身份认证的外部服务接口（包括调试端口、控制台、远程会话与诊断接口）应具备防暴力破解机制；当连续鉴权失败达到预定阈值（如5次）时，必须自动锁定会话接口不少于10分钟，实施渐进式延迟惩罚并记录不可篡改的安全告警日志。 / All external interfaces offering authentication (debug ports, console, remote sessions, diagnostics) shall implement anti-brute-force controls; upon reaching consecutive failed attempt thresholds (e.g., 5 times), the interface shall automatically lock for at least 10 minutes, enforce progressive exponential delays, and log security alarms."
    },
    "CSR-APP-007": {
        "domain": "应用安全 / Application Security",
        "category": "二进制加固与知识产权 / Binary Hardening & IP Protection",
        "title": "应用程序二进制符号剥离、代码混淆加固与算法知识产权保护 / Application Binary Symbol Stripping, Code Obfuscation and Intellectual Property Protection",
        "req": "量产交付的ADCU应用程序与算法模块二进制文件必须剔除所有调试符号（Debug Symbols）与函数名元数据；核心感知算法与融合处理逻辑必须采用代码混淆或控制流扁平化加固技术，防止逆向分析与知识产权窃取。 / Production ADCU applications and algorithm binaries must strip all debug symbols and function name metadata; core perception algorithms and fusion processing logic shall undergo code obfuscation or control-flow flattening to protect against reverse engineering and intellectual property theft."
    },

    # 5. 数据安全与隐私保护 / Data Security & Privacy
    "CSR-DAT-001": {
        "domain": "数据安全与隐私保护 / Data Security & Privacy",
        "category": "静态敏感数据存储加密 / At-Rest Data Encryption",
        "title": "静态敏感文件、数据库与存储缓存硬件绑定落盘加密存储 / Static Sensitive File, Database and Storage Cache Hardware-Bound Encryption at Rest",
        "req": "存储在ADCU非易失介质中的敏感配置文件、标定数据、模型文件及数据库，必须使用经行业认可的高强度对称加密算法实施静态落盘加密与完整性保护；加密密钥应由HSM或芯片内部硬件根密钥保护，确保外部脱机读取介质无法恢复明文。 / Sensitive configuration files, calibration data, model files, and databases stored in ADCU non-volatile media must be encrypted at rest using an approved high-strength symmetric encryption algorithm with integrity protection; encryption keys shall be managed by HSM or chip internal root keys, ensuring offline desoldering cannot recover plaintext."
    },
    "CSR-DAT-002": {
        "domain": "数据安全与隐私保护 / Data Security & Privacy",
        "category": "个人数据隐私保护与假名化 / Personal Data Privacy & Pseudonymization",
        "title": "车辆行驶轨迹、位置坐标及VIN等个人敏感信息假名化与最小化处理 / Vehicle Trajectory, Position Coordinates and VIN Pseudonymization and Data Minimization",
        "req": "系统采集、处理与存储的车辆GNSS经纬度、连续行驶轨迹、车架号（VIN）及驾驶行为等个人敏感数据，应严格遵循数据最小化原则；在本地存储与外传前必须实施假名化（Pseudonymization）或哈希脱敏，明确数据法定留存周期并在到期后自动删除。 / Personal sensitive data including GNSS coordinates, continuous trajectories, VIN, and driving behaviors collected and processed by the system shall strictly follow data minimization; data must undergo pseudonymization or hashing prior to storage and transmission, with defined statutory retention periods and automatic deletion upon expiry."
    },
    "CSR-DAT-003": {
        "domain": "数据安全与隐私保护 / Data Security & Privacy",
        "category": "车载视频图像脱敏 / Video/Image Stream Anonymization",
        "title": "车载摄像头视频流与图像数据人脸、车牌本地自动脱敏与模糊化 / In-Vehicle Camera Video Stream and Image Face/License-Plate Local Automatic Anonymization and Blurring",
        "req": "车载环视与前视摄像头捕获的图像帧与视频流，在上传至云端或导出至外部调试设备前，必须在ADCU本地通过硬件加速模块对外部人脸特征、周边车辆号牌及敏感地理标识实施自动检测与马赛克/模糊化脱敏处理，满足隐私保护合规要求。 / Video streams and image frames captured by surround and forward cameras must undergo automated local hardware-accelerated face and license-plate detection and pixelation/blurring on the ADCU before uploading to cloud or exporting to debug tools, satisfying privacy compliance requirements."
    },
    "CSR-DAT-004": {
        "domain": "数据安全与隐私保护 / Data Security & Privacy",
        "category": "日志脱敏与敏感字段过滤 / Log Sanitization & Sensitive Field Filtering",
        "title": "调试与诊断日志中敏感凭据、口令及个人隐私字段实时脱敏与过滤 / Real-Time Sanitization and Filtering of Sensitive Credentials, Passwords and Privacy Fields in Logs",
        "req": "系统调试日志、诊断记录及控制台输出流必须建立统一的敏感信息过滤机制；在写入存储或输出前，必须自动屏蔽或抹除包括密码凭证、对称密钥、证书私钥、完整用户手机号及精确家庭住址等敏感数据，严禁以明文形式打印在任何日志中。 / Debug logs, diagnostic records, and console output streams must establish a unified sensitive information filter; before writing to storage or outputting, credentials, symmetric keys, private keys, phone numbers, and residential coordinates must be automatically masked or removed, strictly forbidding plaintext logging."
    },
    "CSR-DAT-005": {
        "domain": "数据安全与隐私保护 / Data Security & Privacy",
        "category": "传输机密性端到端保护 / End-to-End Transmission Confidentiality",
        "title": "敏感业务数据、固件与调试数据传输外发端到端强加密保护 / End-to-End Strong Encryption for Sensitive Business Data, Firmware and Debug Transmission",
        "req": "在车云通信、OTA升级下载、传感器融合外发及以太网调试过程中，承载敏感固件、标定参数及私密业务数据的通信链路，必须通过标准安全加密传输通道（如mTLS或应用层端到端对称加密）保护，确保网络窃听者无法截获并解密通信明文。 / Communication links carrying sensitive firmware, calibration data, and private operational information during vehicle-cloud, OTA download, sensor fusion export, and Ethernet debug must be protected via standard secure encrypted transmission channels or application-layer end-to-end encryption, preventing network eavesdroppers from intercepting plaintext."
    },
    "CSR-DAT-006": {
        "domain": "数据安全与隐私保护 / Data Security & Privacy",
        "category": "敏感数据安全擦除 / Secure Data Erasure",
        "title": "临时缓存、会话密钥与敏感数据使用完毕安全覆写与置零擦除 / Secure Overwriting and Zeroization of Temporary Caches, Session Keys and Sensitive Data Post-Use",
        "req": "固件刷写过程中的临时固件包、解密缓冲区、中间认证凭据及会话密钥，在处理完毕、升级成功或发生异常中断时，必须立即使用零值或伪随机数据进行不可恢复的多次内存覆写与置零（Zeroization），防止通过内存扫描转储获取残留敏感信息。 / Temporary firmware packages, decryption buffers, intermediate credentials, and session keys used during flashing must be immediately and irrecoverably overwritten with zeroes or pseudorandom data upon completion or abnormal termination, preventing residual data recovery via memory dump analysis."
    },
    "CSR-DAT-007": {
        "domain": "数据安全与隐私保护 / Data Security & Privacy",
        "category": "存储数据完整性与真实性 / Stored Data Integrity & Authenticity",
        "title": "关键配置、时钟校准数据与本地存储数据密码学完整性(MAC/签名)校验 / Cryptographic Integrity (MAC/Signature) Verification for Critical Configuration, Calibration and Stored Data",
        "req": "本地存储的系统关键配置参数、IMU/时钟校准数据、高精地图缓存及离线固件，必须附加基于HMAC-SHA256或数字签名的完整性校验标签；在系统加载和使用数据前必须执行校验，检测到数据被篡改、损坏或脱机替换时必须拒绝使用并记录错误。 / Critical configuration parameters, IMU/clock calibrations, HD map caches, and offline firmware stored locally must incorporate HMAC-SHA256 or digital signature integrity tags; validation shall execute before data usage, rejecting corrupted or tampered data and logging error events."
    },
    "CSR-DAT-008": {
        "domain": "数据安全与隐私保护 / Data Security & Privacy",
        "category": "审计日志防篡改与抗抵赖 / Audit Log Tamper Resistance & Non-Repudiation",
        "title": "安全日志与审计记录防篡改哈希链、数字签名及可信时间戳保护 / Tamper-Resistant Hash Chain, Digital Signature and Trusted Timestamp Protection for Security Logs",
        "req": "系统安全日志与关键操作审计记录应采用密码学哈希链（Hash Chain）技术进行链式防篡改保护，每个日志块包含前一区块哈希与可信安全时间戳；定期使用车端私钥对日志清单进行数字签名，防止日志在本地被未授权篡改、伪造、覆写或截断删除。 / System security logs and audit records shall utilize cryptographic hash chains where each block includes the hash of the preceding block and a trusted timestamp; log manifests shall be periodically signed using the device private key, preventing unauthorized local tampering, forgery, overwriting, or deletion."
    },
    "CSR-DAT-009": {
        "domain": "数据安全与隐私保护 / Data Security & Privacy",
        "category": "数据导出受控授权 / Controlled Data Export Authorization",
        "title": "本地数据与日志向外导出受控授权、操作审计与防泄露机制 / Controlled Authorization, Operational Audit and Leak Prevention for Local Data and Log Export",
        "req": "通过USB调试口、以太网或诊断接口导出ADCU内部存储的CAN报文、轨迹日志或传感器数据时，必须获得经OEM签名授权的导出许可令牌并核验操作员身份；所有数据导出操作必须记录详细的审计追踪信息，严禁导出未授权的内部系统数据。 / Exporting stored CAN logs, trajectory records, or sensor data via USB, Ethernet, or diagnostics must require OEM-signed export tokens and authenticated operator credentials; all export transactions shall be comprehensively audited, prohibiting unauthorized export of internal system data."
    },
    "CSR-DAT-010": {
        "domain": "数据安全与隐私保护 / Data Security & Privacy",
        "category": "OTA包端到端完整性保护 / OTA Package End-to-End Integrity",
        "title": "OTA安装包与元数据端到端哈希完整性校验及版本新鲜度保障 / End-to-End Hash Integrity Verification and Version Freshness for OTA Update Packages and Metadata",
        "req": "OTA固件升级包及其描述清单（Manifest）在从云端下载至车辆并刷入闪存的全流程中，必须使用密码安全杂凑算法进行全量端到端哈希完整性校验；在写入前必须校验Manifest内各镜像校验和与版本防回滚新鲜度信息，保证分发交付过程中的完整性与来源可信。 / OTA update packages and manifests must be validated with cryptographic hash functions for end-to-end integrity checks throughout download and flashing; manifests must verify individual image checksums and anti-rollback freshness prior to installation, guaranteeing authenticity and integrity across distribution."
    },

    # 6. 安全监控、测试与响应 / Security Monitoring, Testing & Response
    "CSR-MON-001": {
        "domain": "安全监控、测试与响应 / Security Monitoring, Testing & Response",
        "category": "车载网络入侵检测 / In-Vehicle Network IDS/IDPS",
        "title": "车载网络(CAN/CANFD/以太网)入侵检测与防御系统(IDS/IDPS)实时监控 / Real-Time In-Vehicle Network (CAN/CANFD/Ethernet) Intrusion Detection and Prevention (IDS/IDPS)",
        "req": "ADCU应部署车载入侵检测系统（IDS/IDPS），对CAN/CANFD总线及车载以太网实施连续实时监控；IDS应具备基于规则与基线的检测能力，识别总线泛洪攻击、高优先级ID滥用、端口扫描、畸形报文及非法IP/MAC通信，并在100ms内触发告警、阻断攻击报文或进入安全隔离状态。 / The ADCU shall deploy an in-vehicle IDS/IDPS to monitor CAN/CANFD and Ethernet traffic continuously in real time; the IDS shall support rule-based and baseline anomaly detection for bus flooding, high-priority ID abuse, port scanning, malformed frames, and illegal IP/MAC addresses, triggering alerts, blocking attacks, or entering safe isolation within 100 ms."
    },
    "CSR-MON-002": {
        "domain": "安全监控、测试与响应 / Security Monitoring, Testing & Response",
        "category": "安全事件审计与记录 / Security Event Auditing & Logging",
        "title": "关键安全事件、认证失败与特权操作结构化审计日志记录与时间同步 / Structured Audit Logging and Time Synchronization for Security Events, Failed Authentications and Privileged Actions",
        "req": "系统必须对所有安全关键事件进行结构化审计记录，包括安全启动失败、身份鉴权失败、诊断安全访问越权、固件验签失败、物理防拆告警及IDS检测事件；日志条目应包含统一的微秒级安全同步时间戳、事件严重级别、调用源标识及上下文信息。 / The system must generate structured audit logs for all security-critical events, including secure boot failures, authentication errors, diagnostic access violations, signature failures, tamper alerts, and IDS detections; log entries must include synchronized microsecond timestamps, severity levels, source identifiers, and context."
    },
    "CSR-MON-003": {
        "domain": "安全监控、测试与响应 / Security Monitoring, Testing & Response",
        "category": "调试与诊断审计不可抵赖 / Debug & Diagnostic Non-Repudiation",
        "title": "物理与网络调试操作(JTAG/USB/UART/DoIP)全量审计记录与身份绑定抗抵赖 / Comprehensive Audit Logging and Identity Binding for Physical and Network Debug Operations (JTAG/USB/UART/DoIP)",
        "req": "通过JTAG、USB、UART或DoIP调试接口执行的所有调试会话建立、内存读写、固件刷写及特权模式切换操作，必须无遗漏记录至安全审计日志；日志条目必须强行绑定已认证的操作人员或主机身份凭据，调试会话期间严禁清除或篡改审计记录。 / All debug sessions, memory read/write, flashing, and privilege escalation operations via JTAG, USB, UART, or DoIP must be comprehensively logged; log records must strictly bind authenticated operator/host credentials, strictly prohibiting deletion or tampering of audit records during debug sessions."
    },
    "CSR-MON-004": {
        "domain": "安全监控、测试与响应 / Security Monitoring, Testing & Response",
        "category": "OTA事务不可抵赖审计 / OTA Non-Repudiation Auditing",
        "title": "OTA升级全流程事务防篡改审计与设备数字签名回执保护 / Tamper-Resistant Audit Logging and Device Digital Signature Receipts for OTA Update Lifecycle",
        "req": "系统应完整记录OTA升级的完整生命周期事务，包括下载请求、验签结果、分区写入状态及版本激活事件；系统在固件更新完成后必须生成由车端私钥数字签名的不可抵赖更新执行回执，并安全回传至车云安全管理平台。 / The system shall comprehensively log the complete OTA lifecycle, including download requests, signature verification outcomes, partition write states, and activation events; upon update completion, the device must generate a non-repudiation execution receipt signed by the vehicle private key and transmit it to the cloud VSOC."
    },
    "CSR-MON-005": {
        "domain": "安全监控、测试与响应 / Security Monitoring, Testing & Response",
        "category": "通信流量限速与抗DoS / Traffic Rate Limiting & Anti-DoS",
        "title": "外部通信接口与总线流量速率限制、突发洪泛过滤与抗拒绝服务防护 / External Communication Interface and Bus Traffic Rate Limiting, Burst Flood Filtering and Anti-DoS Protection",
        "req": "ADCU外部通信接口（以太网端口、CAN/CANFD、USB调试口及控制台）应配置硬件或协议栈入口速率限制与突发流量整形；当检测到单ID报文频率超过预定阈值（如正常速率的150%）或网络洪泛攻击时，应在10ms内自动执行限流或丢弃，确保正常通信不受阻塞。 / ADCU external communication interfaces (Ethernet, CAN/CANFD, USB debug, console) shall implement ingress rate limiting and traffic shaping; when message rates exceed defined thresholds (e.g., 150% of nominal) or flood attacks occur, traffic shall be throttled or dropped within 10 ms, ensuring critical communications remain unaffected."
    },
    "CSR-MON-006": {
        "domain": "安全监控、测试与响应 / Security Monitoring, Testing & Response",
        "category": "传感器异常欺骗与失效安全 / Sensor Anomaly Spoofing & Fail-Safe",
        "title": "传感器与时间源异常欺骗检测、系统资源过载监控及安全失效降级保护 / Sensor and Time Source Anomaly Spoofing Detection, Resource Overload Monitoring and Fail-Safe Degradation",
        "req": "系统应实时监控GNSS定位数据、时间同步源及感知传感器输入，检测突变跳跃、时钟漂移及欺骗攻击；监控ADCU CPU与内存资源利用率，当检测到持续异常输入或系统计算资源耗尽时，应安全进入预定义感知降级状态（Fail-Safe），并上报整车故障诊断码（DTC）。 / The system shall monitor GNSS data, time sources, and perception sensor streams in real time for abrupt jumps, clock drifts, and spoofing attacks; CPU and memory utilization shall be tracked, transitioning safely into a defined perception degraded state (Fail-Safe) and raising vehicle DTCs upon detecting sustained anomalies or resource exhaustion.",
    },
    "CSR-MON-007": {
        "domain": "安全监控、测试与响应 / Security Monitoring, Testing & Response",
        "category": "安全日志存储可用性 / Security Log Storage Availability",
        "title": "安全日志专用存储配额隔离、写入限速与防存储泛洪耗尽保护 / Dedicated Storage Quota Isolation, Ingress Rate Limiting and Flood Prevention for Security Logs",
        "req": "安全日志子系统必须使用独立的存储配额，与业务运行数据和应用缓存严格物理/逻辑隔离；日志写入接口应实施限速（如最大50条/秒）与突发防抖，防止攻击者通过故意构造高频安全事件泛洪占满存储空间导致系统瘫痪。 / The security logging subsystem must reside on a dedicated storage quota logically/physically isolated from runtime operational and cache partitions; write interfaces shall implement rate limiting (e.g., 50 entries/s) and debouncing, preventing attackers from flooding log storage to impair system functionality."
    },

    # 7. 通信安全 / Communication Security
    "CSR-COM-001": {
        "domain": "通信安全 / Communication Security",
        "category": "总线消息认证与SecOC / Bus Message Authentication & SecOC",
        "title": "CAN/CANFD总线SecOC消息认证与单调新鲜度防重放保护 / CAN/CANFD Bus SecOC Message Authentication and Monotonic Freshness Anti-Replay",
        "req": "ADCU在CAN/CANFD总线上发送与接收的安全关键车辆控制报文、底盘状态及执行器控制指令，必须启用符合AUTOSAR标准的SecOC消息认证；使用标准消息认证码（MAC）算法并集成单调递增的新鲜度计数器（Freshness Value），拒绝MAC不匹配或计数器重复的报文。 / Safety-critical vehicle control messages, chassis states, and actuator commands on CAN/CANFD must enforce AUTOSAR-compliant SecOC message authentication; Message Authentication Codes (MAC) generated with compliant MAC algorithms shall be verified alongside monotonic Freshness Values, strictly rejecting mismatched MACs or replayed counter values."
    },
    "CSR-COM-002": {
        "domain": "通信安全 / Communication Security",
        "category": "车云通信与TLS保护 / Vehicle-Cloud TLS & mTLS",
        "title": "车云通信TLS 1.2/1.3传输加密、mTLS双向认证与证书固定保护 / Vehicle-to-Cloud TLS 1.2/1.3 Transmission Encryption, mTLS Mutual Authentication and Certificate Pinning",
        "req": "ADCU与云端平台（OTA服务器、高精地图服务器、数据回传网关）的所有网络通信必须使用TLS 1.2或TLS 1.3协议；必须启用基于设备证书与服务器证书的双向身份认证（mTLS），并实施证书固定（Certificate Pinning）及证书吊销状态校验（OCSP/CRL），严禁明文传输。 / All network sessions between ADCU and cloud endpoints (OTA, HD maps, data ingestion) must enforce TLS 1.2 or TLS 1.3; mutual authentication (mTLS) using device and server certificates shall be enabled alongside Certificate Pinning and revocation checking (OCSP/CRL), strictly disallowing plaintext transmission."
    },
    "CSR-COM-003": {
        "domain": "通信安全 / Communication Security",
        "category": "以太网接入认证与MACsec / Ethernet 802.1X & MACsec",
        "title": "车载以太网IEEE 802.1X端口设备认证与IEEE 802.1AE MACsec链路层加密 / Automotive Ethernet IEEE 802.1X Port Device Authentication and IEEE 802.1AE MACsec Link Encryption",
        "req": "车载以太网交换机与物理端口必须启用IEEE 802.1X EAP-TLS端口级设备双向身份认证，未通过认证的传感器或设备禁止接入以太网；关键以太网链路（如SGMII感知数据链路）应启用IEEE 802.1AE MACsec链路加密，提供逐跳线速数据机密性、完整性与防重放保护。 / Automotive Ethernet switches and physical ports shall enforce IEEE 802.1X EAP-TLS port-level mutual device authentication, denying network access to unauthenticated nodes; critical links (such as SGMII perception trunks) shall enable IEEE 802.1AE MACsec line-rate link encryption for hop-by-hop confidentiality, integrity, and anti-replay protection."
    },
    "CSR-COM-004": {
        "domain": "通信安全 / Communication Security",
        "category": "通信白名单与网包过滤 / Communication Whitelist & Packet Filter",
        "title": "车载通信矩阵白名单过滤、路由访问控制与默认拒绝丢弃策略 / Communication Matrix Whitelist Filtering, Routing Access Control and Default-Deny Policy",
        "req": "网关与网络交换模块必须严格依据车载通信矩阵部署白名单过滤规则；对进入ADCU的所有CAN ID、以太网源/目的IP地址、MAC地址、VLAN ID、传输层端口号及SOME/IP服务ID实施硬件级/协议栈级强校验，凡不在白名单内的未知或非法报文一律默认丢弃并触发告警。 / Gateways and network switches must strictly enforce whitelist filtering based on the approved communication matrix; all incoming CAN IDs, IP addresses, MAC addresses, VLAN tags, transport ports, and SOME/IP service IDs shall undergo rigorous validation, dropping any unauthorized or unexpected packets under a default-deny policy and raising alerts."
    },
    "CSR-COM-005": {
        "domain": "通信安全 / Communication Security",
        "category": "入口流量控制与抗泛洪 / Ingress Traffic Policing & Anti-Flooding",
        "title": "外部通信入口流量整形、报文频率限制与总线过载可用性保障 / External Communication Ingress Traffic Shaping, Message Rate Throttling and Bus Overload Protection",
        "req": "ADCU在所有外部物理与逻辑通信接口应部署入口流量整形（Traffic Policing）与令牌桶限速机制；对周期性报文实施接收时间窗口校验，对偶发事件报文设置突发突发流量上限，防止总线泛洪攻击导致微控制器总线死锁或安全感知通信饥饿。 / The ADCU shall deploy ingress traffic policing and token-bucket rate limiting across all physical and logical interfaces; periodic frames shall undergo reception timing window checks, and event-triggered frames shall enforce burst caps, preventing bus flooding attacks from locking microcontrollers or starving critical safety communications.",
    },
    "CSR-COM-006": {
        "domain": "通信安全 / Communication Security",
        "category": "中间件通信鉴权与RPC安全 / Middleware Auth & Secure RPC",
        "title": "车载面向服务中间件(SOME/IP/DDS)双向认证与远程过程调用安全鉴权 / Automotive Service-Oriented Middleware (SOME/IP/DDS) Mutual Authentication and Secure RPC Authorization",
        "req": "基于SOME/IP及DDS协议的服务发现（Service Discovery）、事件订阅（Event Subscription）及远程过程调用（RPC），必须强制实施双向服务身份验证与接口权限判定；拒绝未授权客户端对感知数据流与控制接口的调用请求，确保服务间通信受控合规。 / Service Discovery, Event Subscription, and RPC calls over SOME/IP and DDS must enforce mutual service authentication and method-level access authorization; unauthorized client calls to perception data streams and control interfaces must be rejected, ensuring controlled communication between software components."
    },
    "CSR-COM-007": {
        "domain": "通信安全 / Communication Security",
        "category": "串行传感器链路通信防护 / Serial Sensor Stream Protection",
        "title": "串行传感器通信链路(UART/SPI)报文消息认证、校验和与新鲜度防重放 / Serial Sensor Links (UART/SPI) Message Authentication, Checksums and Freshness Anti-Replay",
        "req": "ADCU与IMU、超声波雷达及GNSS接收机之间的串行通信接口（UART/SPI），必须在协议层附加数据帧校验和（CRC32）或密码学MAC认证码；报文应包含单调递增帧序列号或微秒级时间戳，丢弃校验错误、乱序或重复重放的传感器帧并进入感知降级。 / Serial communication links (UART/SPI) connecting IMU, ultrasonic radar, and GNSS receivers must incorporate CRC32 checksums or cryptographic MACs at the protocol layer; frames shall embed monotonic sequence numbers or microsecond timestamps, rejecting corrupt, out-of-order, or replayed frames and entering degraded perception mode."
    },
    "CSR-COM-008": {
        "domain": "通信安全 / Communication Security",
        "category": "时间同步协议安全 / Time Synchronization Security",
        "title": "gPTP(IEEE 802.1AS)时间同步报文密码学认证与主时钟源接入授权 / Cryptographic Authentication and Master Clock Authorization for gPTP (IEEE 802.1AS) Time Synchronization",
        "req": "车载以太网精准时间同步（gPTP / IEEE 802.1AS）协议报文必须启用密码学真实性校验与TLV消息认证码；主时钟源（Grandmaster Clock）必须通过预置证书或白名单实施设备接入鉴权，拒绝来自未知主机的非法Announce报文与虚假时钟跳变。 / In-vehicle Ethernet precision time synchronization (gPTP / IEEE 802.1AS) messages must enable cryptographic authenticity checks and TLV message authentication codes; the Grandmaster Clock must be authenticated via certificates or whitelists, rejecting unauthorized Announce frames and rogue clock jump manipulations from untrusted nodes."
    },
    "CSR-COM-009": {
        "domain": "通信安全 / Communication Security",
        "category": "车载视频流传输安全 / Video Stream Transmission Security",
        "title": "车载摄像头视频流传输(RTP/SRTP/CSI-2)源设备身份认证与数据完整性校验 / Source Device Authentication and Data Integrity Verification for In-Vehicle Camera Streams (RTP/SRTP/CSI-2)",
        "req": "车载摄像头模组与ADCU之间的视频传输链路（MIPI CSI-2/以太网RTP/SRTP）应实施视频源模组硬件身份绑定；网络视频流必须启用SRTP加密与HMAC-SHA1校验，在数据流异常中断或检测到帧被篡改插入时，系统必须在50ms内切断故障流并报告视频安全事件。 / Video transmission links (MIPI CSI-2 / RTP / SRTP) between camera modules and the ADCU shall enforce hardware identity binding of source modules; network streams shall enable SRTP encryption and HMAC-SHA1 authentication, terminating compromised streams within 50 ms and reporting video security events upon detecting frame corruption or tampering."
    },
    "CSR-COM-010": {
        "domain": "通信安全 / Communication Security",
        "category": "调试诊断通道安全保护 / Debug & Diagnostic Channel Security",
        "title": "调试与诊断通信通道(USB/DoIP/Console)双向身份认证、传输加密与防重放 / Mutual Authentication, Encrypted Channel and Anti-Replay for Debug and Diagnostic Communications (USB/DoIP/Console)",
        "req": "所有通过USB、车载以太网DoIP或串口控制台发起的调试与诊断通信，必须建立在强双向身份认证基础之上；通信会话必须采用安全加密通信协议或会话级对称加密进行全流量加密，对交互命令实施时间戳与单调序号防重放校验。 / All debug and diagnostic sessions initiated via USB, automotive Ethernet DoIP, or serial console must establish mutual strong authentication; session communications must enforce full encryption using secure encrypted communication protocols or session-level symmetric encryption, validating timestamps and monotonic counters to prevent command replay."
    },
    "CSR-COM-011": {
        "domain": "通信安全 / Communication Security",
        "category": "数据传输签名与新鲜度校验 / Transmission Signature & Freshness",
        "title": "OTA升级包与地图回传数据传输端到端数字签名、完整性校验与防重放 / End-to-End Digital Signatures, Integrity Verification and Anti-Replay for OTA Packages and Map Telemetry",
        "req": "在车云通信中传输的OTA固件包、高精地图增量数据及感知回传数据，在应用层必须附带发送方的非对称数字签名与唯一单次请求令牌（Nonce）；接收端必须完成全量验签与新鲜度校验后方可转交下游处理，严禁接收未签名或过期报文。 / OTA firmware bundles, HD map updates, and telemetry data transmitted between vehicle and cloud must embed asymmetric digital signatures and unique single-use request tokens (Nonces) at the application layer; receiving nodes must verify signatures and freshness before downstream processing, strictly rejecting unsigned or expired payloads."
    },

    # 8. 安全开发、供应链与运维 / Secure Development, Supply Chain & Operations
    "CSR-OPS-001": {
        "domain": "安全开发、供应链与运维 / Secure Development, Supply Chain & Operations",
        "category": "量产软件清理与代码加固 / Production Software Hardening & Cleanup",
        "title": "SOP量产软件调试驱动、测试代码与后门彻底移除及二进制加固 / Complete Removal of Debug Drivers, Test Code and Backdoors in Production Software and Binary Hardening",
        "req": "量产交付（SOP）的ADCU固件与应用软件必须在编译阶段完全移除所有调试驱动、测试Hook代码、未授权后门端口及测试私钥；交付的二进制文件必须剔除符号表并开启编译期安全加固选项（-fstack-protector-strong、FORTIFY_SOURCE等），防止攻击者利用残留调试代码破坏系统安全。 / Production-release (SOP) ADCU firmware and applications must completely remove all debug drivers, testing hooks, backdoor ports, and test keys during compilation; binary artifacts must strip symbol tables and enable compiler security mitigations (-fstack-protector-strong, FORTIFY_SOURCE), preventing attackers from exploiting residual test code."
    },
    "CSR-OPS-002": {
        "domain": "安全开发、供应链与运维 / Secure Development, Supply Chain & Operations",
        "category": "硬件唯一身份与防伪溯源 / Unique Hardware Identity & Anti-Counterfeiting",
        "title": "供应链芯片不可篡改硬件唯一标识(UID)与产线来料防伪体验证 / Chip Immutable Unique Identifier (UID) and Incoming Supply Chain Anti-Counterfeiting Verification",
        "req": "ADCU板载主芯片（SoC、MCU、HSM）必须具备在制造过程中熔断的只读硬件唯一序列号（UID）及由芯片原厂数字签名的电子溯源证书；OEM/Tier 1在产线来料检验阶段必须通过专用安全设备验证证书真实性并登记入库，杜绝假冒伪劣芯片流入量产链。 / Master chips (SoC, MCU, HSM) on the ADCU must incorporate immutable, factory-fused read-only unique serial numbers (UID) and manufacturer digitally signed provenance certificates; OEM/Tier 1 incoming inspection must verify certificate signatures using secure tools, preventing counterfeit silicon from entering production."
    },
    "CSR-OPS-003": {
        "domain": "安全开发、供应链与运维 / Secure Development, Supply Chain & Operations",
        "category": "产线安全配给与密钥注入 / Production Provisioning & Key Injection",
        "title": "制造产线专用安全配给环境与每设备唯一密钥安全注入流程 / Secure Manufacturing Provisioning Environment and Per-Device Unique Key Injection Pipeline",
        "req": "ADCU生产制造阶段的根密钥注入与证书配置，必须在符合ISO/SAE 21434要求的专用产线安全配给环境（Provisioning Enclave）中由硬件安全模块（产线HSM）自动完成；密钥注入全过程严禁明文呈现或人工导出，注入完成后自动生成可追溯的加密配置审计凭证。 / Root key injection and certificate provisioning during ADCU manufacturing must execute in an ISO/SAE 21434 compliant dedicated manufacturing provisioning enclave controlled by production HSMs; key injection shall remain completely automated without human exposure or plaintext export, generating traceable encrypted audit certificates upon completion."
    },
    "CSR-OPS-004": {
        "domain": "安全开发、供应链与运维 / Secure Development, Supply Chain & Operations",
        "category": "软件供应链与漏洞生命周期 / Software Supply Chain & Vulnerability Lifecycle",
        "title": "软件物料清单(SBOM)管理、开源组件漏洞监测与全生命周期安全响应 / Software Bill of Materials (SBOM) Management, Vulnerability Monitoring and Lifecycle Incident Response",
        "req": "项目必须建立完整的软件物料清单（SBOM），精确追踪操作系统、第三方开源组件及闭源中间件的版本与依赖关系；定期对接国家信息安全漏洞库与通用漏洞披露（CVE），在全生命周期内建立漏洞评估、紧急补丁制作及安全升级机制，SOP前确保已知高危漏洞清零。 / The project must maintain a complete Software Bill of Materials (SBOM), accurately tracking versions and dependencies of operating systems, open-source libraries, and proprietary packages; regular scanning against CVE databases and lifecycle vulnerability triage, patch release, and emergency update procedures shall ensure zero known critical vulnerabilities at SOP."
    },
    "CSR-OPS-005": {
        "domain": "安全开发、供应链与运维 / Secure Development, Supply Chain & Operations",
        "category": "OTA发布安全管理与审计 / OTA Release Management & Auditing",
        "title": "OTA更新发布全流程多方代码签名、发布审批与不可抵赖追踪 / Multi-Party Code Signing, Release Approval and Non-Repudiation Audit for OTA Update Pipelines",
        "req": "OTA升级固件在发布前必须经过离线受控的多方联合数字签名（Multi-Signature）流程，签名私钥必须保存在银行级硬件安全模块中并要求物理多重授权；发布系统必须记录完整的发布审批流、操作人员电子签名及镜像全量哈希清单，确保证书与软件包发布的防篡改与法律抗抵赖性。 / OTA update firmware must undergo an offline controlled multi-party digital signature workflow prior to release, with private keys protected in EAL5+ HSMs requiring M-of-N authorization; release systems must log immutable approval records, digital sign-offs, and package hashes, guaranteeing non-repudiation and distribution integrity."
    }
}

def classify_canonical_csr(major_cat: str, sub_cat: str, title: str, req_text: str) -> str:
    """
    智能将大模型提炼的需求语义归纳到 ISO/SAE 21434 标准 58 项原子需求中
    """
    t = f"{major_cat} {sub_cat} {title} {req_text}".lower()
    d = str(major_cat).lower()

    if "hardware" in d or "硬件" in d:
        if any(k in t for k in ["侧信道", "故障注入", "功耗", "掩码", "毛刺", "恒定时间"]):
            return "CSR-HW-003"
        if any(k in t for k in ["外部存储", "flash", "emmc", "puf"]):
            return "CSR-HW-004"
        if any(k in t for k in ["防拆", "外壳", "线束", "走线", "探针", "物理防护", "物理防拆", "开盖"]):
            return "CSR-HW-002"
        return "CSR-HW-001"

    if "system" in d or "系统" in d:
        if any(k in t for k in ["启动链", "安全启动", "bootrom", "启动完整性", "固件签名校验", "固件签名验证", "签名验证与防回滚"]):
            if any(k in t for k in ["防回滚", "单调计数器", "版本计数器", "抗回滚", "版本号低于"]) and not any(k in t for k in ["分级", "bootrom到应用层", "启动阶段"]):
                return "CSR-SYS-002"
            return "CSR-SYS-001"
        if any(k in t for k in ["单调计数器", "抗回滚", "防回滚", "版本计数器"]):
            return "CSR-SYS-002"
        if any(k in t for k in ["a/b", "双分区", "刷写回滚", "原子更新", "分区恢复"]):
            return "CSR-SYS-003"
        if any(k in t for k in ["selinux", "强制访问控制", "rbac与默认凭证", "rbac与默认拒绝", "进程隔离", "只读分区", "root"]):
            return "CSR-SYS-004"
        if any(k in t for k in ["内存防护", "aslr", "kaslr", "栈溢出", "dep", "canary", "编译加固", "栈保护"]):
            return "CSR-SYS-005"
        if any(k in t for k in ["度量", "运行时完整性", "未授权代码执行", "内存破坏", "安全降级", "安全复位", "运行时系统强化"]):
            return "CSR-SYS-009"
        if any(k in t for k in ["配额", "日志轮转", "日志轮换", "存储资源", "文件服务资源可用性", "防泛洪"]):
            return "CSR-SYS-006"
        if any(k in t for k in ["特权", "会话身份绑定", "命令滥用", "诊断访问与配置授权", "诊断与调试接口安全", "调试数据与固件完整性", "调试高权限", "调试授权", "会话令牌"]):
            return "CSR-SYS-007"
        return "CSR-SYS-008"

    if "crypt" in d or "key" in d or "密码" in d or "密钥" in d:
        if any(k in t for k in ["证书私钥", "证书轮换", "pki", "tls通信加密及密钥"]):
            return "CSR-CRY-002"
        if any(k in t for k in ["载荷应用层加密", "端到端加密", "aes-gcm", "sm4", "感知数据加密", "图像流机密性"]):
            return "CSR-CRY-003"
        if any(k in t for k in ["固件包签名", "更新包加密", "安装后完整性复核", "激活前校验"]):
            return "CSR-CRY-004"
        if any(k in t for k in ["调试日志与核心转储", "核心转储机密性"]):
            return "CSR-CRY-005"
        if any(k in t for k in ["密码模块侧信道", "零化", "主动置零"]):
            return "CSR-CRY-006"
        return "CSR-CRY-001"

    if "app" in d or "应用" in d:
        if any(k in t for k in ["路径规范化", "路径遍历", "输入校验", "内存防护", "模糊测试", "符号链接", "fuzzing"]):
            return "CSR-APP-002"
        if any(k in t for k in ["some/ip", "服务订阅", "ipc", "中间件授权", "服务发布", "服务注册"]):
            return "CSR-APP-003"
        if any(k in t for k in ["ota安装日志接口", "调试日志与核心转储写入流", "文件/数据库访问控制"]):
            return "CSR-APP-004"
        if any(k in t for k in ["地图与ota授权", "ota下载授权", "外部接口鉴权与最小权限", "idor"]):
            return "CSR-APP-005"
        if any(k in t for k in ["未授权访问审计与账户锁定", "锁定", "防暴力破解"]):
            return "CSR-APP-006"
        if any(k in t for k in ["符号剥离", "二进制与知识产权", "代码混淆"]):
            return "CSR-APP-007"
        return "CSR-APP-001"

    if "data" in d or "privacy" in d or "数据" in d or "隐私" in d:
        if any(k in t for k in ["视频日志脱敏", "图像帧缓冲隐私", "视频元数据隐私", "人脸", "车牌"]):
            return "CSR-DAT-003"
        if any(k in t for k in ["轨迹", "行驶状态数据隐私", "感知数据隐私最小化", "个人数据隐私保护与最小化", "去标识化", "匿名化"]):
            return "CSR-DAT-002"
        if any(k in t for k in ["日志脱敏", "诊断与gnss日志访问控制及隐私保护", "敏感通信数据加密与日志脱敏", "点云与诊断日志隐私", "个人数据隐私保护与合规处理", "调试数据机密性", "调试日志隐私", "个人数据脱敏与受控导出", "调试与诊断数据隐私保护"]):
            return "CSR-DAT-004"
        if any(k in t for k in ["静态加密", "静态文件", "外置存储加密", "受保护存储", "加密防泄露", "敏感文件访问控制与静态加密", "片上存储与密钥机密性", "隐私数据加密与最小化要求"]):
            return "CSR-DAT-001"
        if any(k in t for k in ["安全擦除", "法定保留边界", "临时敏感数据安全擦除"]):
            return "CSR-DAT-006"
        if any(k in t for k in ["防篡改及不可否认", "真实性与防篡改审计", "密码学完整性与防抵赖", "上传数据不可否认"]):
            return "CSR-DAT-008"
        if any(k in t for k in ["来源认证与防重放", "校准数据与配置文件完整性", "地图缓存完整性", "缓存与审计记录真实性"]):
            return "CSR-DAT-007"
        if any(k in t for k in ["数据导出", "导出管控"]):
            return "CSR-DAT-009"
        if any(k in t for k in ["ota固件包完整性与防回滚", "ota与tls元数据隐私"]):
            return "CSR-DAT-010"
        return "CSR-DAT-005"

    if "monitor" in d or "监控" in d or "test" in d or "测试" in d:
        if any(k in t for k in ["调试审计日志", "调试操作审计"]):
            return "CSR-MON-003"
        if any(k in t for k in ["ota下载非抵赖", "地图与ota事务不可否认"]):
            return "CSR-MON-004"
        if any(k in t for k in ["配额", "写入流的存储配额"]):
            return "CSR-MON-007"
        if any(k in t for k in ["时间源欺骗", "资源监控与失效安全"]):
            return "CSR-MON-006"
        if any(k in t for k in ["总线洪泛", "接口可用性防护", "通信可用性", "总线监控与流量控制", "可用性防护"]):
            return "CSR-MON-005"
        if any(k in t for k in ["不可否认性要求", "安全日志防篡改", "安全审计日志防篡改", "安全失效保护与安全事件审计", "时间同步安全事件审计", "审计日志不可否认", "防抵赖审计日志", "安全日志与不可抵赖审计", "fota地址操作安全审计"]):
            return "CSR-MON-002"
        return "CSR-MON-001"

    if "comm" in d or "通信" in d or "通讯" in d:
        if any(k in t for k in ["secoc"]):
            return "CSR-COM-001"
        if any(k in t for k in ["802.1x", "macsec"]):
            return "CSR-COM-003"
        if any(k in t for k in ["tls", "mtls", "车云通信传输安全", "车云双向认证", "安全通道", "车云身份认证"]):
            return "CSR-COM-002"
        if any(k in t for k in ["ptp", "时间同步"]):
            return "CSR-COM-008"
        if any(k in t for k in ["视频流", "rtp", "rtsp", "摄像头源身份认证", "图像帧真实性"]):
            return "CSR-COM-009"
        if any(k in t for k in ["spi", "超声波uart", "gnss uart", "uart汇聚流", "gnss数据流", "串行传感器"]):
            return "CSR-COM-007"
        if any(k in t for k in ["白名单", "默认拒绝", "通信矩阵白名单", "域隔离", "报文合法性校验", "外部接口行驶状态数据加密"]):
            return "CSR-COM-004"
        if any(k in t for k in ["流量控制", "限速与流量控制", "通信抗拒绝服务", "抗拒绝服务与报文合法性", "通信可用性与抗拒绝服务"]):
            return "CSR-COM-005"
        if any(k in t for k in ["some/ip安全通信", "感知服务发布订阅", "中间件服务双向身份认证"]):
            return "CSR-COM-006"
        if any(k in t for k in ["调试命令", "控制台", "调试与刷写", "诊断与调试会话", "诊断写服务", "调试与ota双向身份认证", "usb调试接口身份认证", "安全jtag调试"]):
            return "CSR-COM-010"
        return "CSR-COM-011"

    if "dev" in d or "ops" in d or "运维" in d or "开发" in d or "生产" in d or "supply" in d or "供应链" in d:
        if any(k in t for k in ["清理", "二进制保护", "调试认证", "量产调试接口清理"]):
            return "CSR-OPS-001"
        if any(k in t for k in ["来料检验", "溯源", "uid"]):
            return "CSR-OPS-002"
        if any(k in t for k in ["安全配给", "密钥注入"]):
            return "CSR-OPS-003"
        if any(k in t for k in ["漏洞管理", "安全编码", "sbom"]):
            return "CSR-OPS-004"
        return "CSR-OPS-005"

    return "CSR-GEN-001"

def collect_csr_report_data(steps, assets, hash_to_global_map: dict = None):
    """
    收集并进行全局去重、合并与两级安全分类（大分类稳定，子分类由 AI 动态生成）整理安全需求（CSR）数据
    """
    steps_by_asset = {}
    for step in steps:
        if step.asset_id not in steps_by_asset:
            steps_by_asset[step.asset_id] = {}
        steps_by_asset[step.asset_id][step.stage] = step

    # 1. 建立全局原始需求映射 (ID maps)
    _, _, _, req_map = build_id_maps(steps)

    # Pre-build traceability maps for all assets
    asset_trace_maps = {}
    for asset in assets:
        asset_steps = steps_by_asset.get(asset.id, {})
        stage5_out = asset_steps.get("stage5").analysis_result.get("final_output", {}) if asset_steps.get("stage5") else {}
        asset_trace_maps[asset.id] = get_csr_traceability_maps(stage5_out, req_map)

    # 2. 收集所有底层的安全需求并进行归档
    all_csrs = []
    for asset in assets:
        asset_steps = steps_by_asset.get(asset.id, {})
        if not asset_steps:
            continue
            
        stage5_out = asset_steps.get("stage5").analysis_result.get("final_output", {}) if asset_steps.get("stage5") else {}
        summarized_csrs = stage5_out.get("summarized_csrs", [])
        
        # 兜底：如果 summarized_csrs 为空，从 requirements 中提取
        if not summarized_csrs:
            requirements = stage5_out.get("requirements", [])
            # 考虑全部产品安全要求
            device_reqs = requirements
            
            seen_req_texts = set()
            summarized_csrs = []
            for req in device_reqs:
                req_text = req.get("cybersecurity_requirement") or ""
                if not req_text or req_text in seen_req_texts:
                    continue
                seen_req_texts.add(req_text)
                
                req_id = req.get("cybersecurity_requirement_id") or f"CSR-{asset.id:03d}-{len(seen_req_texts):02d}"
                
                summarized_csrs.append({
                    "cybersecurity_requirement_id": req_id,
                    "csr_id": req_id,
                    "title": req.get("title") or f"针对 {asset.name} 的安全要求 / Security requirement for {asset.name}",
                    "sub_title": req.get("sub_title") or f"安全防护 / Cybersecurity Protection",
                    "security_domain": req.get("security_domain"),
                    "cybersecurity_requirement": req_text
                })
                
        for csr in summarized_csrs:
            all_csrs.append({
                "asset": asset,
                "csr": csr
            })

    # 3. 对所有需求进行标准化原子聚类与全局资产解耦去重
    merged_map = {}
    
    for item in all_csrs:
        asset = item["asset"]
        csr = item["csr"]
        req_text = str(csr.get("cybersecurity_requirement") or "").strip()
        if not req_text or req_text == "N/A":
            continue
            
        raw_domain = str(csr.get("security_domain") or "").strip()
        if "->" in raw_domain:
            parts = raw_domain.split("->")
            major_cat = clean_major_category(parts[0])
            sub_cat = parts[1].strip()
        else:
            major_cat = clean_major_category(raw_domain)
            sub_cat = str(csr.get("sub_title") or csr.get("sub_category") or "通用防护 / General Protection").strip()
            
        # 标准化原子需求分类
        atomic_id = classify_canonical_csr(major_cat, sub_cat, csr.get("title", ""), req_text)
        meta = CANONICAL_ATOMIC_CATALOG.get(atomic_id)
        if meta:
            atomic_domain = meta["domain"]
            atomic_cat = meta["category"]
            atomic_title = meta["title"]
            atomic_req = meta["req"]
        else:
            atomic_domain = major_cat
            atomic_cat = sub_cat
            atomic_title = csr.get("title") or "安全要求 / Security Requirement"
            atomic_req = req_text
            
        key = atomic_id
        if key not in merged_map:
            merged_map[key] = {
                "atomic_id": atomic_id,
                "major_category": atomic_domain,
                "sub_category": atomic_cat,
                "requirement_text": atomic_req,
                "title": atomic_title,
                "assets": [],  # 保存 (asset_sn, asset_name)
                "orig_csr_ids": set(),
                "source_tara_req_ids": set()
            }
            
        entry = merged_map[key]
        asset_sn = get_asset_sn(asset)
        if (asset_sn, asset.name) not in entry["assets"]:
            entry["assets"].append((asset_sn, asset.name))
            
        raw_csr_id = req_map.get(req_text, csr.get("csr_id") or csr.get("cybersecurity_requirement_id") or "N/A")
        if raw_csr_id != "N/A":
            entry["orig_csr_ids"].add(raw_csr_id)
            _, csr_to_tara = asset_trace_maps.get(asset.id, ({}, {}))
            orig_ids = csr_to_tara.get(raw_csr_id, [])
            for oid in orig_ids:
                entry["source_tara_req_ids"].add(oid)

    # 4. 生成按标准大类排序、跨资产聚合的项目级原子 CSR 列表
    major_order = {
        "硬件安全 / Hardware Security": 1,
        "系统安全 / System Security": 2,
        "密码安全与密钥管理 / Cryptography & Key Management": 3,
        "应用安全 / Application Security": 4,
        "数据安全与隐私保护 / Data Security & Privacy": 5,
        "安全监控、测试与响应 / Security Monitoring, Testing & Response": 6,
        "通信安全 / Communication Security": 7,
        "安全开发、供应链与运维 / Secure Development, Supply Chain & Operations": 8
    }

    sorted_keys = sorted(
        merged_map.keys(),
        key=lambda k: (major_order.get(merged_map[k]["major_category"], 99), k)
    )

    rows = []
    for k in sorted_keys:
        entry = merged_map[k]
        
        assets_sorted = sorted(entry["assets"], key=lambda x: x[0])
        asset_sn_str = ", ".join(x[0] for x in assets_sorted)
        asset_name_str = ", ".join(x[1] for x in assets_sorted)
        applicable_assets_str = "\n".join(f"{sn} ({name})" if name else sn for sn, name in assets_sorted)
        
        source_tara_ids_str = ", ".join(sorted(list(entry["source_tara_req_ids"])))
        if not source_tara_ids_str:
            source_tara_ids_str = ", ".join(sorted(list(entry["orig_csr_ids"])))
            
        atomic_id = entry["atomic_id"]
        if hash_to_global_map is not None:
            for rid in entry.get("orig_csr_ids", []):
                hash_to_global_map[rid] = atomic_id
            for tid in entry.get("source_tara_req_ids", []):
                hash_to_global_map[tid] = atomic_id
        
        source_cnt = len(entry["source_tara_req_ids"]) if entry["source_tara_req_ids"] else len(entry["orig_csr_ids"])
        
        rows.append({
            "number": atomic_id,
            "csr_id": atomic_id,
            "security_domain": entry["major_category"],
            "sub_category": entry["sub_category"],
            "asset_sn": asset_sn_str,
            "asset_name": asset_name_str,
            "applicable_assets": applicable_assets_str,
            "assets_count": len(assets_sorted),
            "title": entry["title"],
            "cybersecurity_requirement": entry["requirement_text"],
            "source_tara_requirements": source_tara_ids_str,
            "source_count": source_cnt
        })

    return rows

def create_excel_report(domain: Domain, steps: List[TaraStep], assets: List[Asset], desensitize: bool, export_type: str = "all") -> str:
    """
    生成高度美观的 ISO 21434 TARA XLSX 格式报表，支持脱敏过滤，并支持导出安全需求 (CSR_Requirements) 的混合模型 sheet 页
    """
    wb = openpyxl.Workbook()
    default_ws = wb.active
    
    # 颜色与样式设定 (Dark Gray Header Theme)
    header_fill_r1 = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
    header_fill_r2 = PatternFill(start_color="374151", end_color="374151", fill_type="solid")
    header_font = Font(name="Microsoft YaHei", size=10, bold=True, color="FFFFFF")
    cell_font = Font(name="Microsoft YaHei", size=9.5)
    
    thin_border = Border(
        left=Side(style='thin', color='D1D5DB'),
        right=Side(style='thin', color='D1D5DB'),
        top=Side(style='thin', color='D1D5DB'),
        bottom=Side(style='thin', color='D1D5DB')
    )
    
    has_tara = export_type in ["all", "tara"]
    has_csr = export_type in ["all", "csr"]
    has_assets = export_type in ["all", "assets"]
    
    # 提前收集合并后安全需求的映射字典
    hash_to_global_map = {}
    csr_rows_pre = collect_csr_report_data(steps, assets, hash_to_global_map)
    
    # 导出 TARA Sheet
    if has_tara:
        ws = default_ws
        ws.title = "TARA"
        ws.views.sheetView[0].showGridLines = True
        
        # 1. 标题行高度
        ws.row_dimensions[1].height = 30
        ws.row_dimensions[2].height = 35
        
        # 填充所有表头单元格默认格式
        for col_idx in range(1, 34):
            cell_r1 = ws.cell(row=1, column=col_idx)
            cell_r1.fill = header_fill_r1
            cell_r1.font = header_font
            cell_r1.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell_r1.border = thin_border
            
            cell_r2 = ws.cell(row=2, column=col_idx)
            cell_r2.fill = header_fill_r2
            cell_r2.font = header_font
            cell_r2.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell_r2.border = thin_border
            
        # 第一层合并表头 (Row 1)
        ws.merge_cells('B1:C1')
        ws['B1'] = 'Assets'
        ws['D1'] = 'Cybersecurity Attributes Result'
        ws.merge_cells('E1:K1')
        ws['E1'] = 'Damage Scenarios and Impact Category'
        ws.merge_cells('L1:U1')
        ws['L1'] = 'Threat Scenarios and Attack Feasibility Assessment'
        
        # 第二层标题名称 (Row 2)
        headers_r2 = [
            "Number", "Assets SN", "Assets Name",
            "Cybersecurity Attributes Result",
            "Damage Scenarios SN", "Damage Scenarios", "Safety", "Financial", "Operational", "Privacy", "Impact Level",
            "Threat Scenarios", "Attack Path",
            "Time Consuming", "Expertise", "Knowledge about TOE", "Window of opportunity", "Equipment", "Difficulty", "AF Level", "CAF Level",
            "Risk Value", "Risk Treatment Recommend",
            "Cybersecurity Claims ID", "Cybersecurity Claims", "Cybersecurity Goal ID", "Cybersecurity Goal", "Cybersecurity Control ID", "Cybersecurity Control", "Allocated to Device", "Cybersecurity Requirement ID", "Cybersecurity Requirement", "Merged CSR ID / 合并后安全需求ID"
        ]
        
        for col_idx, h in enumerate(headers_r2, 1):
            ws.cell(row=2, column=col_idx, value=h)
            
        rows = collect_report_data(steps, assets, desensitize, hash_to_global_map)
        
        current_row = 3
        keys = [
            "number", "asset_sn", "asset_name",
            "attribute_result",
            "damage_scenario_sn", "damage_scenario", "safety", "financial", "operational", "privacy", "overall_impact",
            "threat_scenario", "attack_path",
            "time_consuming", "expertise", "knowledge_about_toe", "window_of_opportunity", "equipment", "difficulty", "af_level", "caf_level",
            "risk_value", "risk_treatment",
            "cybersecurity_claim_id", "cybersecurity_claim", "cybersecurity_goal_id", "cybersecurity_goal", "cybersecurity_control_id", "cybersecurity_control", "allocated_to_device", "cybersecurity_requirement_id", "cybersecurity_requirement", "merged_csr_id"
        ]
        
        for row_data in rows:
            for col_idx, key in enumerate(keys, 1):
                val = row_data.get(key, "")
                cell = ws.cell(row=current_row, column=col_idx, value=val)
                cell.font = cell_font
                cell.border = thin_border
                
                # 描述文本左对齐，其它代号及评价指标等均居中对齐
                if key in ["damage_scenario", "threat_scenario", "attack_path", "cybersecurity_requirement", "cybersecurity_control", "cybersecurity_claim", "cybersecurity_goal"]:
                    cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
                else:
                    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                    
            ws.row_dimensions[current_row].height = 24
            current_row += 1
                
        # 自适应列宽设定
        for col in ws.columns:
            max_len = 0
            for cell in col:
                if cell.row in [1, 2]:
                    continue
                val_str = str(cell.value or "")
                if len(val_str) > max_len:
                    max_len = len(val_str)
            col_letter = openpyxl.utils.get_column_letter(col[0].column)
            ws.column_dimensions[col_letter].width = min(max(max_len + 3, 11), 35)

    # 导出 CSR_Requirements Sheet (项目级原子安全需求库)
    if has_csr:
        sheet2_title = "CSR_Requirements"
        if has_tara:
            ws2 = wb.create_sheet(title=sheet2_title)
        else:
            ws2 = default_ws
            ws2.title = sheet2_title
            
        ws2.views.sheetView[0].showGridLines = True
        
        headers2 = [
            "Atomic CSR ID", "Security Domain", "Control Category", "Requirement Title", "Cybersecurity Requirement (CN/EN)",
            "Applicable Assets / 适用资产列表", "Assets Count / 覆盖资产数",
            "Source TARA CSR IDs / 追溯原始TARA需求", "Source Count / 追溯条目数"
        ]
        for col_idx, h in enumerate(headers2, 1):
            cell = ws2.cell(row=1, column=col_idx, value=h)
            cell.fill = header_fill_r1
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = thin_border

        ws2.row_dimensions[1].height = 30
        csr_rows = csr_rows_pre
        current_row = 2
        csr_keys = ["csr_id", "security_domain", "sub_category", "title", "cybersecurity_requirement", "applicable_assets", "assets_count", "source_tara_requirements", "source_count"]
        
        for row_data in csr_rows:
            for col_idx, key in enumerate(csr_keys, 1):
                val = row_data.get(key, "")
                cell = ws2.cell(row=current_row, column=col_idx, value=val)
                cell.font = cell_font
                cell.border = thin_border
                
                if key in ["title", "cybersecurity_requirement", "applicable_assets"]:
                    cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
                else:
                    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                    
            ws2.row_dimensions[current_row].height = 70
            current_row += 1
            
        # 列宽设置 (Sheet 2)
        col_w_map2 = {1: 16, 2: 24, 3: 28, 4: 35, 5: 65, 6: 32, 7: 14, 8: 40, 9: 14}
        for c_idx, width in col_w_map2.items():
            col_letter = openpyxl.utils.get_column_letter(c_idx)
            ws2.column_dimensions[col_letter].width = width

    # 导出 Traceability_Matrix Sheet (全景双向追溯矩阵)
    if has_tara and has_csr:
        ws_matrix = wb.create_sheet(title="Traceability_Matrix")
        ws_matrix.views.sheetView[0].showGridLines = True
        
        matrix_headers = [
            "Raw TARA CSR ID", "TARA Row #", "Threat Scenario ID", "Asset SN", "Asset Name",
            "Threat Scenario / 威胁场景", "Raw Requirement Text / 原始TARA需求文本",
            "Mapped Atomic CSR ID / 映射原子需求ID", "Mapped Atomic CSR Title / 映射原子需求标题", "Security Domain / 所属安全域"
        ]
        
        for col_idx in range(1, len(matrix_headers) + 1):
            cell = ws_matrix.cell(row=1, column=col_idx, value=matrix_headers[col_idx - 1])
            cell.fill = header_fill_r1
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = thin_border
            
        ws_matrix.row_dimensions[1].height = 28
        
        # 建立原子需求元数据字典
        atomic_dict = {
            r["csr_id"]: (r["title"], r["security_domain"]) for r in csr_rows_pre
        }
        for aid, meta in CANONICAL_ATOMIC_CATALOG.items():
            if aid not in atomic_dict:
                atomic_dict[aid] = (meta["title"], meta["domain"])
                
        matrix_cur_row = 2
        for t_idx, tara_r in enumerate(rows, start=3):
            cid_str = str(tara_r.get("cybersecurity_requirement_id") or "").strip()
            ctext = str(tara_r.get("cybersecurity_requirement") or "").strip()
            if not cid_str or cid_str == "N/A":
                continue
                
            import re
            cids = re.findall(r'CSR_[0-9A-Fa-f]{8}', cid_str)
            if not cids:
                cids = [c.strip() for c in cid_str.split("\n") if c.strip()]
                
            parts = [ctext] if len(cids) == 1 else [p.strip() for p in re.split(r'\(\d+\)\s*', ctext) if p.strip()]
            if len(parts) != len(cids):
                parts = [ctext] * len(cids)
                
            merged_val = str(tara_r.get("merged_csr_id") or "").strip()
            mapped_aids = [a.strip() for a in merged_val.split(",") if a.strip() and a.strip() != "N/A"]
            if not mapped_aids:
                mapped_aids = ["CSR-GEN-001"]
                
            for cid, part in zip(cids, parts):
                for aid in mapped_aids:
                    a_title, a_domain = atomic_dict.get(aid, ("安全防护要求 / Security Requirement", "通用安全 / General Security"))
                    m_row = [
                        cid,
                        t_idx,
                        tara_r.get("number", ""),
                        tara_r.get("asset_sn", ""),
                        tara_r.get("asset_name", ""),
                        tara_r.get("threat_scenario", ""),
                        part,
                        aid,
                        a_title,
                        a_domain
                    ]
                    for col_idx, m_val in enumerate(m_row, 1):
                        c_cell = ws_matrix.cell(row=matrix_cur_row, column=col_idx, value=m_val)
                        c_cell.font = cell_font
                        c_cell.border = thin_border
                        if col_idx in [1, 2, 3, 4, 8]:
                            c_cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                        else:
                            c_cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
                    ws_matrix.row_dimensions[matrix_cur_row].height = 32
                    matrix_cur_row += 1
                    
        col_w_matrix = {1: 18, 2: 12, 3: 16, 4: 14, 5: 20, 6: 35, 7: 45, 8: 18, 9: 35, 10: 24}
        for c_idx, width in col_w_matrix.items():
            col_letter = openpyxl.utils.get_column_letter(c_idx)
            ws_matrix.column_dimensions[col_letter].width = width
            
    # 导出 Assets Sheet (资产列表)
    if has_assets:
        if has_tara or has_csr:
            ws3 = wb.create_sheet(title="Assets")
        else:
            ws3 = default_ws
            ws3.title = "Assets"
            
        ws3.views.sheetView[0].showGridLines = True
        
        # 填充所有表头单元格默认格式
        for col_idx in range(1, 8):
            cell_r1 = ws3.cell(row=1, column=col_idx)
            cell_r1.fill = header_fill_r1
            cell_r1.font = header_font
            cell_r1.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell_r1.border = thin_border
            
            cell_r2 = ws3.cell(row=2, column=col_idx)
            cell_r2.fill = header_fill_r2
            cell_r2.font = header_font
            cell_r2.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell_r2.border = thin_border

        ws3.row_dimensions[1].height = 30
        ws3.row_dimensions[2].height = 35
        
        # 第一层合并表头 (Row 1)
        ws3.merge_cells('A1:G1')
        ws3['A1'] = 'Assets List / 资产列表'
        
        # 第二层标题名称 (Row 2)
        headers3_r2 = [
            "Number / 序号", "Asset SN / 资产 SN", "Asset Name / 资产名称",
            "Asset Type / 资产类型", "Protocol / 通信协议", "Description / 备注说明",
            "Status / 核对状态"
        ]
        for col_idx, h in enumerate(headers3_r2, 1):
            ws3.cell(row=2, column=col_idx, value=h)
            
        type_map = {
            "data": "数据资产 / Data",
            "software": "软件资产 / Software",
            "hardware": "硬件资产 / Hardware",
            "communication": "通信资产 / Communication"
        }
        
        status_map = {
            "draft": "待核对 / Draft",
            "confirmed": "已确认 / Confirmed",
            "rejected": "已拒绝 / Rejected"
        }
        
        current_row = 3
        for idx, asset in enumerate(sorted(assets, key=get_asset_sort_key), 1):
            row_data = [
                idx,
                get_asset_sn(asset),
                asset.name or "",
                type_map.get(str(asset.asset_type).lower().strip(), asset.asset_type or ""),
                asset.protocol or "N/A",
                asset.description or "无备注 / No description",
                status_map.get(str(asset.status).lower().strip(), asset.status or "")
            ]
            for col_idx, val in enumerate(row_data, 1):
                cell = ws3.cell(row=current_row, column=col_idx, value=val)
                cell.font = cell_font
                cell.border = thin_border
                
                # 描述文本/名称左对齐，其它居中对齐
                if col_idx in [3, 6]:
                    cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
                else:
                    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                    
            ws3.row_dimensions[current_row].height = 24
            current_row += 1
            
        # 自适应列宽设定
        for col in ws3.columns:
            max_len = 0
            for cell in col:
                if cell.row in [1, 2]:
                    continue
                val_str = str(cell.value or "")
                if len(val_str) > max_len:
                    max_len = len(val_str)
            col_letter = openpyxl.utils.get_column_letter(col[0].column)
            ws3.column_dimensions[col_letter].width = min(max(max_len + 3, 12), 40)

    filename_suffix = f"{export_type}_{'desensitized' if desensitize else 'full'}"
    file_path = os.path.join(EXPORTS_DIR, f"TARA_Report_{domain.id}_{filename_suffix}.xlsx")
    wb.save(file_path)
    return file_path

def create_csv_report(domain: Domain, steps: List[TaraStep], assets: List[Asset], desensitize: bool, export_type: str = "tara") -> str:
    """
    生成 ISO 21434 TARA CSV 格式报表，根据 export_type 可选导出 TARA 威胁分析 或 CSR 需求列表
    使用 utf-8-sig 编码，防止中文在 Excel 中打开时显示乱码
    """
    filename_suffix = f"{export_type}_{'desensitized' if desensitize else 'full'}"
    file_path = os.path.join(EXPORTS_DIR, f"TARA_Report_{domain.id}_{filename_suffix}.csv")
    
    if export_type == "csr":
        headers_r2 = [
            "Atomic CSR ID", "Security Domain", "Control Category", "Requirement Title",
            "Cybersecurity Requirement", "Applicable Assets", "Assets Count",
            "Source TARA CSR IDs", "Source Count"
        ]
        keys = [
            "csr_id", "security_domain", "sub_category", "title",
            "cybersecurity_requirement", "applicable_assets", "assets_count",
            "source_tara_requirements", "source_count"
        ]
        rows = collect_csr_report_data(steps, assets)
    else:
        headers_r2 = [
            "Number", "Assets SN", "Assets Name",
            "Cybersecurity Attributes Result",
            "Damage Scenarios SN", "Damage Scenarios", "Safety", "Financial", "Operational", "Privacy", "Impact Level",
            "Threat Scenarios", "Attack Path",
            "Time Consuming", "Expertise", "Knowledge about TOE", "Window of opportunity", "Equipment", "Difficulty", "AF Level", "CAF Level",
            "Risk Value", "Risk Treatment Recommend",
            "Cybersecurity Claims ID", "Cybersecurity Claims", "Cybersecurity Goal ID", "Cybersecurity Goal", "Cybersecurity Control ID", "Cybersecurity Control", "Allocated to Device", "Cybersecurity Requirement ID", "Cybersecurity Requirement"
        ]
        keys = [
            "number", "asset_sn", "asset_name",
            "attribute_result",
            "damage_scenario_sn", "damage_scenario", "safety", "financial", "operational", "privacy", "overall_impact",
            "threat_scenario", "attack_path",
            "time_consuming", "expertise", "knowledge_about_toe", "window_of_opportunity", "equipment", "difficulty", "af_level", "caf_level",
            "risk_value", "risk_treatment",
            "cybersecurity_claim_id", "cybersecurity_claim", "cybersecurity_goal_id", "cybersecurity_goal", "cybersecurity_control_id", "cybersecurity_control", "allocated_to_device", "cybersecurity_requirement_id", "cybersecurity_requirement"
        ]
        hash_to_global_map = {}
        collect_csr_report_data(steps, assets, hash_to_global_map)
        rows = collect_report_data(steps, assets, desensitize, hash_to_global_map)
    
    with open(file_path, mode="w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers_r2)
        for row in rows:
            writer.writerow([row.get(k, "") if row.get(k) is not None else "" for k in keys])
            
    return file_path

# ----------------- 导出报告接口 -----------------

@router.get("/domains/{domain_id}/export")
def export_report(
    domain_id: int,
    format: str = "xlsx", # xlsx 或 csv
    desensitize: bool = False, # 是否脱敏导出 (BR-57, BR-77)
    export_type: str = "all", # all (导出全部Sheets，仅对xlsx有效), tara (只导出威胁分析), csr (只导出安全需求汇总)
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    导出网络安全 TARA 评估报告与 CSR 安全需求汇总 (XLSX/CSV，支持数据脱敏与混合模型导出，BR-57, BR-77)
    """
    domain = db.query(Domain).filter(Domain.id == domain_id).first()
    if not domain:
        raise HTTPException(status_code=404, detail="子域控不存在")
        
    # 获取最新的已完成运行记录和步骤结果
    last_run = db.query(TaraRun).filter(
        TaraRun.domain_id == domain_id,
        TaraRun.status == "completed"
    ).order_by(TaraRun.id.desc()).first()
    
    if not last_run:
        raise HTTPException(status_code=400, detail="该域控当前没有已成功完成 of TARA 报告记录，无法执行导出。")
        
    steps = db.query(TaraStep).filter(TaraStep.run_id == last_run.id).all()
    assets = db.query(Asset).filter(Asset.domain_id == domain_id).all()
    
    export_type_lower = export_type.lower().strip()
    if format.lower() == "csv" and export_type_lower == "all":
        # CSV不支持多sheet，默认设置为 tara 导出
        export_type_lower = "tara"
        
    if export_type_lower not in ["all", "tara", "csr"]:
        raise HTTPException(status_code=400, detail="不支持的导出类型，可选值: all, tara, csr。")
    
    if format.lower() == "xlsx":
        file_path = create_excel_report(domain, steps, assets, desensitize, export_type_lower)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = f"TARA_Report_{domain.name}_{export_type_lower}_{'desensitized' if desensitize else 'full'}.xlsx"
    elif format.lower() == "csv":
        file_path = create_csv_report(domain, steps, assets, desensitize, export_type_lower)
        media_type = "text/csv; charset=utf-8-sig"
        filename = f"TARA_Report_{domain.name}_{export_type_lower}_{'desensitized' if desensitize else 'full'}.csv"
    else:
        raise HTTPException(status_code=400, detail="不支持的导出文件格式，仅支持 'xlsx' 或 'csv'。")
        
    return FileResponse(
        path=file_path,
        media_type=media_type,
        filename=filename
    )
