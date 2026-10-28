from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from datetime import datetime
import hashlib
import os
import re
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

from app.services.csr_synthesis import (
    build_id_maps,
    get_csr_traceability_maps
)

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
                if damage_scenarios:
                    continue
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
                
                matching_ts = [ts for ts in threat_scenarios if ts.get("damage_scenario_sn") == ds.get("damage_scenario_sn")]
                if not matching_ts:
                    matching_ts = [ts for ts in threat_scenarios if ts.get("attribute") == attr]
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
                        # 匹配不到需求时不再用顶层 csr（Tier-2 汇总文本）兜底：
                        # 否则会把 Tier-2 内容哈希混入 TARA 的 Cybersecurity Requirement ID 列，
                        # 与 CSR_Requirements 来源列的 Tier-1 哈希不在同一 ID 空间，破坏追溯。
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
            matched_global_ids = []
            part_to_global_map = {}
            for line in lines:
                import re
                cleaned = re.sub(r'^\(\d+\)\s*', '', line).strip()
                if not cleaned or cleaned == "N/A":
                    continue
                line_global_ids = set()
                # 1. 优先通过原始需求哈希 ID 映射
                raw_tid = req_map.get(cleaned) or req_map.get(line) or req_map.get(req_val)
                if raw_tid and hash_to_global_map and raw_tid in hash_to_global_map:
                    val = hash_to_global_map[raw_tid]
                    if isinstance(val, (set, list)):
                        line_global_ids.update(val)
                    else:
                        line_global_ids.add(val)
                # 2. 通过资产级汇总 CSR ID 映射
                for sm_id in tara_to_csr.get(cleaned, []):
                    if hash_to_global_map and sm_id in hash_to_global_map:
                        val = hash_to_global_map[sm_id]
                        if isinstance(val, (set, list)):
                            line_global_ids.update(val)
                        else:
                            line_global_ids.add(val)
                    else:
                        line_global_ids.add(sm_id)
                part_to_global_map[cleaned] = sorted(list(line_global_ids))
                matched_global_ids.extend(line_global_ids)

            row["part_to_global_map"] = part_to_global_map
            if matched_global_ids:
                row["merged_csr_id"] = ", ".join(sorted(list(set(matched_global_ids))))
            else:
                row["merged_csr_id"] = "N/A"
        else:
            row["merged_csr_id"] = "N/A"
            row["part_to_global_map"] = {}

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
# ISO/SAE 21434 & UNECE R155 Concept-Phase Global CSR Synthesis Integration
# -------------------------------------------------------------------------
from app.services.csr_synthesis import (
    DOMAIN_PREFIX_MAP,
    MAJOR_ORDER,
    get_or_synthesize_global_csrs
)

def _apply_security_domain_correction(rows: list) -> list:
    """按控制机制纠正安全域，并为缺少功能分类的旧缓存补规则分类。"""
    from app.core.taxonomy import correct_security_domain, classify_functional_category

    for r in rows:
        text = r.get("cybersecurity_requirement") or ""
        dom = clean_major_category(r.get("security_domain"))
        r["security_domain"] = correct_security_domain(dom, text)
        if not r.get("functional_category"):
            # 旧缓存（本功能上线前生成）没有功能分类字段，用规则兜底
            r["functional_category"] = classify_functional_category(
                f"{r.get('title', '')} {text} {r.get('subject', '')}"
            )
            r["category_source"] = "rule_fallback"
            r["category_confidence"] = "low"
            r["needs_review"] = True
    return rows


def collect_csr_report_data(steps, assets, hash_to_global_map: dict = None, db: Session = None, domain: Domain = None, force_recompute: bool = False):
    """
    收集并进行全局去重、合并与两级安全分类整理项目级网络安全需求（CSR）数据。
    基于 UNECE R155 Annex 5 规范指引与大模型全局综合提炼，支持自动持久化与语义兜底。
    """
    if db is None:
        from app.core.database import SessionLocal
        local_db = SessionLocal()
        try:
            rows = get_or_synthesize_global_csrs(local_db, domain, steps, assets, hash_to_global_map, force_recompute=force_recompute)
        finally:
            local_db.close()
        return _apply_security_domain_correction(rows)
    else:
        rows = get_or_synthesize_global_csrs(db, domain, steps, assets, hash_to_global_map, force_recompute=force_recompute)
        return _apply_security_domain_correction(rows)


def build_csr_coverage_rows(csr_rows: list) -> list:
    """Build a raw-requirement -> final-CSR coverage audit table.

    The synthesis layer records missing/extra/duplicate IDs inside
    ``_coverage_report`` and marks auto-filled rows with
    ``_coverage_status == 'fallback_original'``. This function turns those
    markers into a human-readable sheet so a reviewer can see exactly which
    input requirement was covered, by which CSR, and which IDs were missing,
    duplicated, or hallucinated.
    """
    coverage = {}
    reports_by_domain = {}

    for row in csr_rows:
        domain = str(row.get("security_domain") or "")
        csr_id = str(row.get("csr_id") or "")
        status = str(row.get("_coverage_status") or "merged")
        raw_ids = row.get("_tier2_source_ids") or row.get("_raw_source_ids") or []
        if not isinstance(raw_ids, list):
            raw_ids = [str(raw_ids)]
        if not raw_ids:
            # Older cached rows only have the rendered source field.
            raw_ids = [
                part.strip()
                for part in str(row.get("source_tara_requirements") or "").split(",")
                if part.strip()
            ]
        for raw_id in raw_ids:
            raw_id = str(raw_id).strip()
            if not raw_id:
                continue
            key = (domain, raw_id)
            entry = coverage.setdefault(
                key,
                {"csr_ids": [], "status": status},
            )
            if csr_id and csr_id not in entry["csr_ids"]:
                entry["csr_ids"].append(csr_id)
            if status == "fallback_original":
                entry["status"] = "fallback_original"

        report = row.get("_coverage_report") or {}
        if report:
            reports_by_domain.setdefault(domain, report)

    result = []
    for (domain, raw_id), entry in sorted(coverage.items()):
        report = reports_by_domain.get(domain, {})
        duplicate = raw_id in (report.get("duplicate_raw_ids") or {})
        notes = []
        if entry["status"] == "fallback_original":
            notes.append("模型未覆盖，已用原始需求兜底 / model gap, original requirement preserved")
        if duplicate:
            notes.append("被多条 CSR 引用（允许的拆分） / referenced by multiple CSRs (intentional split)")
        if report.get("verification_missing_count"):
            notes.append(f"verification_missing={report.get('verification_missing_count')}（已验证方法兜底） / verification method filled as TBD")
        if report.get("mode"):
            notes.append(f"mode={report.get('mode')}")
        result.append({
            "raw_id": raw_id,
            "security_domain": domain,
            "csr_id": ", ".join(entry["csr_ids"]),
            "coverage_status": entry["status"],
            "duplicate": "yes" if duplicate else "no",
            "note": "; ".join(notes),
        })

    # IDs the model invented are stripped before export; keep them visible in
    # the audit sheet rather than silently dropping them.
    for domain, report in reports_by_domain.items():
        for raw_id in report.get("extra_raw_ids") or []:
            result.append({
                "raw_id": raw_id,
                "security_domain": domain,
                "csr_id": "",
                "coverage_status": "extra_stripped",
                "duplicate": "no",
                "note": "模型编造，已从输出剔除 / hallucinated, stripped from output",
            })
        for raw_id in report.get("missing_raw_ids") or []:
            if (domain, raw_id) not in coverage:
                result.append({
                    "raw_id": raw_id,
                    "security_domain": domain,
                    "csr_id": "",
                    "coverage_status": "missing_no_fallback",
                    "duplicate": "no",
                    "note": "缺失且无自动兜底，需人工处理 / missing without fallback, manual action required",
                })

    return result


CUSTOMER_ONLY_NA = "N/A (TARA Threat Analysis Enhanced)"
TARA_ONLY_NA = "N/A (Customer Specific)"


def collect_merged_requirement_rows(db: Session, domain_id: int) -> List[dict]:
    """收集某个域控的最终合并需求（OEM 客户需求 + 本域控 TARA 需求），用于导出 Merged_Requirements sheet。"""
    from app.models.req_merge import MergedRequirement, MergedRequirementSource

    if not domain_id:
        return []
    rows = (
        db.query(MergedRequirement)
        .filter(MergedRequirement.domain_id == domain_id)
        .order_by(MergedRequirement.id)
        .all()
    )
    result = []
    for r in rows:
        refs = (
            db.query(MergedRequirementSource)
            .filter(MergedRequirementSource.merged_id == r.id)
            .all()
        )
        customer = [x.source_id for x in refs if x.source_type == "customer"]
        tara = [x.source_id for x in refs if x.source_type == "tara"]
        result.append({
            "final_id": r.final_id,
            "customer_ids": ", ".join(customer) if customer else CUSTOMER_ONLY_NA,
            "tara_ids": ", ".join(tara) if tara else TARA_ONLY_NA,
            "source_tara_raw_ids": r.source_tara_raw_ids or "",
            "security_domain": r.security_domain or "",
            "control_category": r.control_category or "",
            "requirement_title": r.requirement_title or "",
            "subject": r.subject or "",
            "requirement_text": r.requirement_text or "",
            "additional_info": r.additional_info or "",
            "status": r.status or "",
            "allocated_module": r.allocated_module or "",
            "responsible_org": r.responsible_org or "",
            "remarks": r.remarks or "",
        })
    return result


def create_excel_report(domain: Domain, steps: List[TaraStep], assets: List[Asset], desensitize: bool, export_type: str = "all", db: Session = None, force_recompute: bool = False) -> str:
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
    csr_rows_pre = collect_csr_report_data(steps, assets, hash_to_global_map, db=db, domain=domain, force_recompute=force_recompute)
    
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
            "Atomic CSR ID", "Security Domain", "Functional Category / 功能分类", "Control Category", "Requirement Title", "Subject / 责任主体", "Cybersecurity Requirement (CN/EN)",
            "Verification Method / 验证方法", "Requirement Type / 需求类型", "Rationale / 需求依据",
            "Applicable Assets / 适用资产列表", "Assets Count / 覆盖资产数",
            "Source Cybersecurity Requirement IDs / 来源TARA需求ID", "Source Count / 追溯条目数",
            "Needs Review / 待复核"
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
        csr_keys = ["csr_id", "security_domain", "functional_category", "sub_category", "title", "subject", "cybersecurity_requirement", "verification_method", "requirement_type", "rationale", "applicable_assets", "assets_count", "source_cybersecurity_requirement_ids", "source_count", "needs_review"]
        
        for row_data in csr_rows:
            for col_idx, key in enumerate(csr_keys, 1):
                val = row_data.get(key, "")
                if key == "source_cybersecurity_requirement_ids" and not val:
                    # Backward compatibility for cached rows created before
                    # the explicit source column was added.
                    val = row_data.get("source_tara_requirements", "")
                if key == "needs_review":
                    val = "Yes" if val else "No"
                cell = ws2.cell(row=current_row, column=col_idx, value=val)
                cell.font = cell_font
                cell.border = thin_border
                
                if key in ["title", "cybersecurity_requirement", "verification_method", "rationale", "applicable_assets"]:
                    cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
                else:
                    cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                    
            ws2.row_dimensions[current_row].height = 70
            current_row += 1
            
        # 列宽设置 (Sheet 2)
        col_w_map2 = {1: 16, 2: 22, 3: 22, 4: 26, 5: 32, 6: 50, 7: 60, 8: 28, 9: 14, 10: 40, 11: 36, 12: 14, 13: 36, 14: 12, 15: 12}
        for c_idx, width in col_w_map2.items():
            col_letter = openpyxl.utils.get_column_letter(c_idx)
            ws2.column_dimensions[col_letter].width = width

        # 导出 CSR_Coverage Sheet（raw_id -> Final CSR 覆盖审计）
        ws_cov = wb.create_sheet(title="CSR_Coverage")
        ws_cov.views.sheetView[0].showGridLines = True
        cov_headers = [
            "Raw Requirement ID / 原始需求ID", "Security Domain / 安全域",
            "Final CSR ID / 最终CSR", "Coverage Status / 覆盖状态",
            "Duplicate / 重复引用", "Note / 说明",
        ]
        for col_idx, header in enumerate(cov_headers, 1):
            cell = ws_cov.cell(row=1, column=col_idx, value=header)
            cell.fill = header_fill_r1
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = thin_border
        ws_cov.row_dimensions[1].height = 30

        status_fills = {
            "fallback_original": PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid"),
            "extra_stripped": PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid"),
            "missing_no_fallback": PatternFill(start_color="FECACA", end_color="FECACA", fill_type="solid"),
            "verification_missing": PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid"),
        }
        for row_idx, cov_row in enumerate(build_csr_coverage_rows(csr_rows_pre), 2):
            values = [
                cov_row["raw_id"], cov_row["security_domain"], cov_row["csr_id"],
                cov_row["coverage_status"], cov_row["duplicate"], cov_row["note"],
            ]
            for col_idx, value in enumerate(values, 1):
                cell = ws_cov.cell(row=row_idx, column=col_idx, value=value)
                cell.font = cell_font
                cell.border = thin_border
                cell.alignment = Alignment(
                    horizontal="left" if col_idx in (1, 3, 6) else "center",
                    vertical="center",
                    wrap_text=True,
                )
                if col_idx == 4 and cov_row["coverage_status"] in status_fills:
                    cell.fill = status_fills[cov_row["coverage_status"]]

        for c_idx, width in {1: 30, 2: 26, 3: 24, 4: 22, 5: 12, 6: 60}.items():
            ws_cov.column_dimensions[openpyxl.utils.get_column_letter(c_idx)].width = width

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
            fallback_aids = [a.strip() for a in merged_val.split(",") if a.strip() and a.strip() != "N/A"]
            if not fallback_aids:
                fallback_aids = ["未映射 / Unmapped"]
                
            part_to_global = tara_r.get("part_to_global_map", {})
                
            for cid, part in zip(cids, parts):
                part_clean = re.sub(r'^\(\d+\)\s*', '', part).strip()
                specific_aids = part_to_global.get(part_clean) or part_to_global.get(part) or []
                if not specific_aids and hash_to_global_map and cid in hash_to_global_map:
                    val = hash_to_global_map[cid]
                    specific_aids = list(val) if isinstance(val, (set, list)) else [val]
                if not specific_aids:
                    specific_aids = fallback_aids

                for aid in sorted(list(set(specific_aids))):
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

    # 导出 Merged_Requirements Sheet（客户需求 + TARA 需求合并后的最终信息安全需求）
    if export_type in ("all", "merged") and db is not None:
        merged_rows = collect_merged_requirement_rows(db, domain.id)
        if merged_rows:
            if export_type == "merged":
                ws_merged = default_ws
                ws_merged.title = "Merged_Requirements"
            else:
                ws_merged = wb.create_sheet(title="Merged_Requirements")
            ws_merged.views.sheetView[0].showGridLines = True
            merged_headers = [
                "Final CSR ID\n最终需求编号",
                "Mapped Customer Req IDs\n映射客户需求编号",
                "Mapped TARA CSR IDs\n映射TARA需求编号",
                "Source TARA Raw IDs / 追溯原始TARA需求\n(底层威胁分析条目ID)",
                "Security Domain\n安全域",
                "Control Category\n控制分类",
                "Requirement Title\n需求标题",
                "Subject / 责任主体",
                "Unified Cybersecurity Requirement (CN/EN)\n整合网络安全需求描述 (中英双语)",
                "Additional Info",
                "Status",
                "Allocated Module\n分配架构模块",
                "Responsible Org\n责任组织",
                "Remarks",
            ]
            for col_idx, h in enumerate(merged_headers, 1):
                cell = ws_merged.cell(row=1, column=col_idx, value=h)
                cell.fill = header_fill_r1
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                cell.border = thin_border
            ws_merged.row_dimensions[1].height = 30

            for row_idx, row_data in enumerate(merged_rows, 2):
                values = [
                    row_data["final_id"], row_data["customer_ids"], row_data["tara_ids"],
                    row_data["source_tara_raw_ids"], row_data["security_domain"], row_data["control_category"],
                    row_data["requirement_title"], row_data["subject"], row_data["requirement_text"],
                    row_data["additional_info"], row_data["status"],
                    row_data["allocated_module"], row_data["responsible_org"], row_data["remarks"],
                ]
                for col_idx, val in enumerate(values, 1):
                    cell = ws_merged.cell(row=row_idx, column=col_idx, value=val)
                    cell.font = cell_font
                    cell.border = thin_border
                    if col_idx in (1, 2, 3, 4, 11):
                        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                    else:
                        cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)
                ws_merged.row_dimensions[row_idx].height = 70

            col_w_merged = {1: 16, 2: 30, 3: 24, 4: 45, 5: 24, 6: 30, 7: 45, 8: 24, 9: 75, 10: 20, 11: 12, 12: 22, 13: 22, 14: 26}
            for c_idx, width in col_w_merged.items():
                ws_merged.column_dimensions[openpyxl.utils.get_column_letter(c_idx)].width = width

    filename_suffix = f"{export_type}_{'desensitized' if desensitize else 'full'}"
    file_path = os.path.join(EXPORTS_DIR, f"TARA_Report_{domain.id}_{filename_suffix}.xlsx")
    wb.save(file_path)
    return file_path

def create_csv_report(domain: Domain, steps: List[TaraStep], assets: List[Asset], desensitize: bool, export_type: str = "tara", db: Session = None) -> str:
    """
    生成 ISO 21434 TARA CSV 格式报表，根据 export_type 可选导出 TARA 威胁分析 或 CSR 需求列表
    使用 utf-8-sig 编码，防止中文在 Excel 中打开时显示乱码
    """
    filename_suffix = f"{export_type}_{'desensitized' if desensitize else 'full'}"
    file_path = os.path.join(EXPORTS_DIR, f"TARA_Report_{domain.id}_{filename_suffix}.csv")
    
    if export_type == "csr":
        headers_r2 = [
            "Atomic CSR ID", "Security Domain", "Functional Category", "Control Category", "Requirement Title", "Subject",
            "Cybersecurity Requirement", "Verification Method", "Requirement Type", "Rationale",
            "Applicable Assets", "Assets Count",
            "Source Cybersecurity Requirement IDs", "Source Count", "Needs Review"
        ]
        keys = [
            "csr_id", "security_domain", "functional_category", "sub_category", "title", "subject",
            "cybersecurity_requirement", "verification_method", "requirement_type", "rationale", "applicable_assets", "assets_count",
            "source_cybersecurity_requirement_ids", "source_count", "needs_review"
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
        collect_csr_report_data(steps, assets, hash_to_global_map, db=db, domain=domain)
        rows = collect_report_data(steps, assets, desensitize, hash_to_global_map)
    
    with open(file_path, mode="w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(headers_r2)
        for row in rows:
            values = []
            for k in keys:
                val = row.get(k, "")
                if k == "source_cybersecurity_requirement_ids" and not val:
                    val = row.get("source_tara_requirements", "")
                if k == "needs_review":
                    val = "Yes" if val else "No"
                values.append("" if val is None else val)
            writer.writerow(values)
            
    return file_path

# ----------------- 导出报告接口 -----------------

@router.get("/domains/{domain_id}/export")
def export_report(
    domain_id: int,
    format: str = "xlsx", # xlsx 或 csv
    desensitize: bool = False, # 是否脱敏导出 (BR-57, BR-77)
    export_type: str = "all", # all (导出全部Sheets，仅对xlsx有效), tara (只导出威胁分析), csr (只导出安全需求汇总)
    force_recompute: bool = False, # 是否强制重新计算全局 CSR 需求
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
        
    if export_type_lower not in ["all", "tara", "csr", "merged"]:
        raise HTTPException(status_code=400, detail="不支持的导出类型，可选值: all, tara, csr, merged。")
    
    if format.lower() == "xlsx":
        file_path = create_excel_report(domain, steps, assets, desensitize, export_type_lower, db=db, force_recompute=force_recompute)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = f"TARA_Report_{domain.name}_{export_type_lower}_{'desensitized' if desensitize else 'full'}.xlsx"
    elif format.lower() == "csv":
        file_path = create_csv_report(domain, steps, assets, desensitize, export_type_lower, db=db)
        media_type = "text/csv; charset=utf-8-sig"
        filename = f"TARA_Report_{domain.name}_{export_type_lower}_{'desensitized' if desensitize else 'full'}.csv"
    else:
        raise HTTPException(status_code=400, detail="不支持的导出文件格式，仅支持 'xlsx' 或 'csv'。")
        
    return FileResponse(
        path=file_path,
        media_type=media_type,
        filename=filename
    )
