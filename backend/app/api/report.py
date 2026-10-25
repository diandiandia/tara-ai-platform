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
# ISO/SAE 21434 Concept-Phase Domain Prefix Map & Security Mechanism Taxonomy
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

# ISO/SAE 21434 & UNECE R155 Standard Security Mechanism Taxonomy for Concept-Phase Deduplication
MECHANISM_KEYS = {
    "硬件安全 / Hardware Security": [
        ("HW_SIDE_CHANNEL", ["侧信道", "故障注入", "功耗", "掩码", "毛刺", "恒定时间", "side-channel", "fault injection"]),
        ("HW_STORAGE", ["外部存储", "flash", "emmc", "puf", "脱机", "拆焊", "存储绑定"]),
        ("HW_TAMPER", ["防拆", "外壳", "连接器", "线束", "布线", "探针", "物理防护", "物理防拆", "开盖", "tamper"]),
        ("HW_LINK_MONITOR", ["物理链路", "供电完整性", "短路", "过压", "鲁棒性", "探头"]),
        ("HW_BOOT_ROOT", ["信任根", "硬件根", "安全启动", "bootrom"]),
        ("HW_DEBUG", ["调试", "jtag", "swd", "uart", "测试点", "debug", "熔丝", "熔断"]),
    ],
    "系统安全 / System Security": [
        ("SYS_ROLLBACK", ["防回滚", "单调计数器", "抗回滚", "版本计数器", "anti-rollback", "monotonic"]),
        ("SYS_AB_UPDATE", ["a/b", "双分区", "刷写回滚", "原子更新", "分区恢复", "安全更新", "更新签名"]),
        ("SYS_BOOT", ["启动链", "安全启动", "bootrom", "启动完整性", "固件签名校验", "固件签名验证", "引导加载"]),
        ("SYS_RUNTIME_INTEGRITY", ["运行时", "度量", "代码完整性", "未授权代码执行", "内存破坏", "安全降级", "安全复位", "运行时系统强化", "数据处理完整性"]),
        ("SYS_COMPILER_MEM", ["内存防护", "aslr", "kaslr", "栈溢出", "dep", "canary", "编译加固", "栈保护", "金丝雀"]),
        ("SYS_ACCESS_CTRL", ["selinux", "强制访问控制", "mac", "只读分区", "root", "权限隔离", "最小权限", "文件系统细粒度"]),
        ("SYS_STORAGE_QUOTA", ["配额", "日志轮转", "日志轮换", "存储资源", "防耗尽", "文件服务资源可用性", "防泛洪", "可用性保护"]),
        ("SYS_WATCHDOG_AVAIL", ["看门狗", "可用性保障", "超时复位", "死锁检测", "时间基准", "故障隔离"]),
        ("SYS_PRIVILEGE_AUTH", ["特权", "会话身份绑定", "命令滥用", "诊断访问", "配置授权", "接口安全", "会话令牌", "调试授权", "调试接口", "访问控制", "强认证"]),
    ],
    "密码安全与密钥管理 / Cryptography & Key Management": [
        ("CRY_PKI_CERT", ["证书", "pki", "私钥", "证书轮换", "crl", "ocsp", "公钥"]),
        ("CRY_PAYLOAD_ENC", ["载荷", "端到端加密", "对称加密", "aes", "sm4", "感知数据加密", "图像流机密性", "应用层加密"]),
        ("CRY_FW_SIGN", ["固件包签名", "更新包加密", "安装后完整性复核", "激活前校验", "验签"]),
        ("CRY_LOG_SEC", ["转储", "core dump", "调试日志机密性"]),
        ("CRY_ZEROIZE", ["密码模块侧信道", "零化", "主动置零", "密钥销毁"]),
        ("CRY_HSM_KEY", ["hsm", "密钥生成", "密钥存储", "密钥管理", "根密钥", "安全存储"]),
    ],
    "应用安全 / Application Security": [
        ("APP_VULN_INPUT", ["输入校验", "路径遍历", "符号链接", "参数校验", "fuzzing", "模糊测试", "格式化字符串", "缓冲区"]),
        ("APP_MIDDLEWARE", ["some/ip", "服务发布", "服务订阅", "ipc", "中间件授权", "服务注册", "rpc"]),
        ("APP_FILE_DB", ["ota安装日志接口", "调试日志与核心转储写入流", "文件/数据库", "访问控制列表"]),
        ("APP_API_AUTH", ["地图与ota授权", "ota下载授权", "外部接口鉴权", "idor", "接口权限"]),
        ("APP_BRUTE_FORCE", ["未授权访问审计", "账户锁定", "锁定", "防暴力破解"]),
        ("APP_OBFUSCATION", ["符号剥离", "二进制加固", "知识产权", "代码混淆", "反逆向"]),
        ("APP_EXEC_ENV", ["安全执行环境", "沙箱", "隔离运行"]),
    ],
    "数据安全与隐私保护 / Data Security & Privacy": [
        ("DAT_SENSOR_PRIVACY", ["视频日志脱敏", "图像帧缓冲隐私", "视频元数据隐私", "人脸", "车牌", "摄像头"]),
        ("DAT_DRIVING_PRIVACY", ["轨迹", "行驶状态", "感知数据隐私", "个人数据隐私", "去标识化", "匿名化", "位置信息"]),
        ("DAT_LOG_PRIVACY", ["日志脱敏", "诊断与gnss日志", "点云与诊断日志", "敏感通信数据加密与日志脱敏", "调试数据脱敏", "脱敏与受控导出"]),
        ("DAT_REST_ENC", ["静态加密", "静态文件", "外置存储加密", "受保护存储", "敏感文件访问控制与静态加密", "片上存储", "敏感参数"]),
        ("DAT_ERASURE", ["安全擦除", "法定保留边界", "临时敏感数据安全擦除", "数据销毁"]),
        ("DAT_NON_REPUDIATION", ["防篡改及不可否认", "真实性与防篡改审计", "密码学完整性与防抵赖", "上传数据不可否认", "防抵赖"]),
        ("DAT_INTEGRITY", ["来源认证与防重放", "校准数据与配置文件完整性", "地图缓存完整性", "缓存与审计记录真实性", "数据完整性"]),
        ("DAT_EXPORT_CTRL", ["数据导出", "受控导出", "导出授权"]),
        ("DAT_OTA_INTEGRITY", ["ota固件包完整性与防回滚", "ota与tls元数据隐私", "ota更新包"]),
        ("DAT_GENERAL", ["数据生命周期", "敏感数据机密性"]),
    ],
    "安全监控、测试与响应 / Security Monitoring, Testing & Response": [
        ("MON_DEBUG_AUDIT", ["调试审计日志", "调试操作审计", "调试会话记录"]),
        ("MON_OTA_AUDIT", ["ota下载非抵赖", "地图与ota事务不可否认", "ota审计"]),
        ("MON_AUDIT_QUOTA", ["配额", "写入流的存储配额", "存储耗尽"]),
        ("MON_TIME_WATCHDOG", ["时间源欺骗", "资源监控与失效安全", "时钟源", "时钟同步"]),
        ("MON_IDS_BUS", ["总线洪泛", "接口可用性防护", "通信可用性", "总线监控与流量控制", "入侵检测", "异常流量", "ids"]),
        ("MON_AUDIT_INTEGRITY", ["不可否认性要求", "安全日志防篡改", "安全审计日志防篡改", "安全失效保护与安全事件审计", "时间同步安全事件审计", "日志防篡改"]),
        ("MON_CONTINUOUS", ["连续监控", "异常检测", "安全事件响应", "应急响应"]),
    ],
    "通信安全 / Communication Security": [
        ("COM_SECOC", ["secoc", "can总线", "can/canfd", "车载总线", "报文认证"]),
        ("COM_LINK_SEC", ["802.1x", "macsec"]),
        ("COM_TLS_CLOUD", ["tls", "mtls", "车云通信", "车云双向认证", "安全通道", "车云身份认证", "私有apn"]),
        ("COM_TIME_SYNC", ["ptp", "时间同步"]),
        ("COM_MEDIA_STREAM", ["视频流", "rtp", "rtsp", "摄像头源", "图像帧"]),
        ("COM_SERIAL_BUS", ["spi", "超声波uart", "gnss uart", "uart汇聚流", "gnss数据流", "串行传感器", "串口"]),
        ("COM_WHITELIST_SEG", ["白名单", "默认拒绝", "通信矩阵", "域隔离", "网络隔离", "防火墙"]),
        ("COM_RATE_LIMIT", ["流量控制", "限速", "抗拒绝服务", "抗dos", "通信可用性"]),
        ("COM_SOMEIP", ["some/ip", "服务发布订阅", "中间件服务"]),
        ("COM_OTA_COMM", ["ota传输", "ota请求", "ota包真实性", "固件下载"]),
        ("COM_DEBUG_SESSION", ["调试命令", "控制台", "调试与刷写", "诊断与调试会话", "诊断写服务", "安全jtag", "usb调试", "诊断服务"]),
        ("COM_GENERAL", ["车内通信安全", "防重放"]),
    ],
    "安全开发、供应链与运维 / Secure Development, Supply Chain & Operations": [
        ("OPS_PROD_CLEAN", ["清理", "二进制保护", "调试认证", "量产调试接口清理"]),
        ("OPS_PROVENANCE", ["来料检验", "溯源", "uid"]),
        ("OPS_KEY_PROVISION", ["安全配给", "密钥注入"]),
        ("OPS_VULN_SBOM", ["漏洞管理", "安全编码", "sbom", "开源软件"]),
        ("OPS_MAINTENANCE", ["维护模式", "备份恢复", "配置核查", "权限基线"]),
        ("OPS_OTA_MGMT", ["ota端到端", "安全更新", "ota审计"]),
        ("OPS_LIFECYCLE", ["生命周期管理", "供应链安全"]),
    ]
}

def classify_mechanism(major_cat: str, sub_cat: str, title: str, req_text: str) -> str:
    """
    基于 ISO/SAE 21434 概念阶段安全机制对需求进行动态语义聚合归类。
    若匹配标准安全机制分类，则归纳至该机制；
    若包含新型或特定资产自定义机制，则动态保留其规范化子分类，确保不丢失任何需求且不硬编码静态文本。
    """
    corpus = f"{major_cat} {sub_cat} {title} {req_text}".lower()
    patterns = MECHANISM_KEYS.get(major_cat, [])
    for mech_id, keywords in patterns:
        if any(k in corpus for k in keywords):
            return mech_id
            
    # 动态兜底机制：提取子分类核心字符作为动态机制键，保证新型/独特安全需求独立成项不被挤压
    clean_sub = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fa5]", "", sub_cat).lower()[:15]
    return f"DYN_{clean_sub}" if clean_sub else "DYN_GENERAL"


def collect_csr_report_data(steps, assets, hash_to_global_map: dict = None):
    """
    依据 ISO/SAE 21434 概念阶段规范，对各资产在分析阶段产出的网络安全需求（CSR）进行
    动态语义聚合、资产解耦、原子化去重与项目级全局编号。
    
    100% 保留大模型与系统分析动态生成的真实需求表述与中英文双语内容，杜绝静态写死文本。
    """
    steps_by_asset = {}
    for step in steps:
        if step.asset_id not in steps_by_asset:
            steps_by_asset[step.asset_id] = {}
        steps_by_asset[step.asset_id][step.stage] = step

    _, _, _, req_map = build_id_maps(steps)

    # Pre-build traceability maps for all assets
    asset_trace_maps = {}
    for asset in assets:
        asset_steps = steps_by_asset.get(asset.id, {})
        stage5_out = asset_steps.get("stage5").analysis_result.get("final_output", {}) if asset_steps.get("stage5") else {}
        asset_trace_maps[asset.id] = get_csr_traceability_maps(stage5_out, req_map)

    # 1. 收集所有底层的安全需求并进行归档
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
            seen_req_texts = set()
            summarized_csrs = []
            for req in requirements:
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

    # 2. 对所有需求进行动态语义聚类与跨资产解耦去重
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
            
        title = str(csr.get("title") or "安全要求 / Security Requirement").strip()
        
        # 动态机制归类
        mech_key = classify_mechanism(major_cat, sub_cat, title, req_text)
        cluster_key = f"{major_cat}::{mech_key}"
        
        if cluster_key not in merged_map:
            merged_map[cluster_key] = {
                "major_category": major_cat,
                "mech_key": mech_key,
                "items": [],  # 存储大模型生成的所有原始 CSR 条目，用于动态选取最全面表述
                "assets": [],  # 保存 (asset_sn, asset_name)
                "orig_csr_ids": set(),
                "source_tara_req_ids": set()
            }
            
        entry = merged_map[cluster_key]
        entry["items"].append(csr)
        
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

    # 3. 生成按 ISO 21434 标准 8 大分类域排序的项目级原子 CSR 列表
    major_order = {
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

    sorted_keys = sorted(
        merged_map.keys(),
        key=lambda k: (major_order.get(merged_map[k]["major_category"], 99), k)
    )

    domain_counters = {}
    rows = []
    
    for k in sorted_keys:
        entry = merged_map[k]
        domain = entry["major_category"]
        
        # 动态分配领域内顺序 CSR ID (如 CSR-HW-001, CSR-SYS-001 等)
        pfx = DOMAIN_PREFIX_MAP.get(domain, "CSR-GEN")
        domain_counters[domain] = domain_counters.get(domain, 0) + 1
        dyn_csr_id = f"{pfx}-{domain_counters[domain]:03d}"
        
        # 动态选取该聚类中大模型输出的最丰富、最完整的需求内容（保留 AI 原生双语表述，绝不静态替换）
        best_csr = max(entry["items"], key=lambda x: len(str(x.get("cybersecurity_requirement") or "")))
        dynamic_title = best_csr.get("title") or "安全要求 / Security Requirement"
        dynamic_sub_cat = best_csr.get("sub_title") or best_csr.get("sub_category") or "通用防护 / General Protection"
        dynamic_req_text = best_csr.get("cybersecurity_requirement") or ""
        
        assets_sorted = sorted(entry["assets"], key=lambda x: x[0])
        asset_sn_str = ", ".join(x[0] for x in assets_sorted)
        asset_name_str = ", ".join(x[1] for x in assets_sorted)
        applicable_assets_str = "\n".join(f"{sn} ({name})" if name else sn for sn, name in assets_sorted)
        
        source_tara_ids_str = ", ".join(sorted(list(entry["source_tara_req_ids"])))
        if not source_tara_ids_str:
            source_tara_ids_str = ", ".join(sorted(list(entry["orig_csr_ids"])))
            
        if hash_to_global_map is not None:
            for rid in entry.get("orig_csr_ids", []):
                hash_to_global_map[rid] = dyn_csr_id
            for tid in entry.get("source_tara_req_ids", []):
                hash_to_global_map[tid] = dyn_csr_id
        
        source_cnt = len(entry["source_tara_req_ids"]) if entry["source_tara_req_ids"] else len(entry["orig_csr_ids"])
        
        rows.append({
            "number": dyn_csr_id,
            "csr_id": dyn_csr_id,
            "security_domain": domain,
            "sub_category": dynamic_sub_cat,
            "asset_sn": asset_sn_str,
            "asset_name": asset_name_str,
            "applicable_assets": applicable_assets_str,
            "assets_count": len(assets_sorted),
            "title": dynamic_title,
            "cybersecurity_requirement": dynamic_req_text,
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
        
        # 建立动态原子需求元数据字典
        atomic_dict = {
            r["csr_id"]: (r["title"], r["security_domain"]) for r in csr_rows_pre
        }
                
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
