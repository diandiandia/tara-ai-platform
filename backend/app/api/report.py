from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from datetime import datetime
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

def collect_report_data(steps: List[TaraStep], assets: List[Asset], desensitize: bool) -> List[dict]:
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
    
    # 对资产按 ID 排序后依次展开子树
    for asset in sorted(assets, key=lambda x: x.id):
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
                    rt_map = {"avoid": "Avoid", "reduce": "Reduce", "share": "Share", "retain": "Retain"}
                    rt_disp = rt_map.get(str(rt).lower(), rt)
                    
                    cybersecurity_control_val = "N/A"
                    allocated_to_device_val = "No"
                    cybersecurity_requirement_val = "N/A"
                    
                    if rt_disp == "Reduce":
                        controls = []
                        allocs = []
                        reqs = []
                        for r_idx, req in enumerate(matching_reqs):
                            c_val = req.get("cybersecurity_control") or "N/A"
                            a_val = "Yes" if str(req.get("allocated_to_device", "No")).lower() in ["yes", "true"] else "No"
                            rq_val = req.get("cybersecurity_requirement") or "N/A"
                            if len(matching_reqs) > 1:
                                controls.append(f"({r_idx+1}) {c_val}")
                                allocs.append(f"({r_idx+1}) {a_val}")
                                reqs.append(f"({r_idx+1}) {rq_val}")
                            else:
                                controls.append(c_val)
                                allocs.append(a_val)
                                reqs.append(rq_val)
                        cybersecurity_control_val = "\n".join(controls)
                        allocated_to_device_val = "\n".join(allocs)
                        cybersecurity_requirement_val = "\n".join(reqs)
                    elif rt_disp == "Avoid":
                        cybersecurity_requirement_val = rd.get("item_change") or "N/A"
                        
                    for ap in attack_paths:
                        row_data = {}
                        row_data["number"] = f"T_{row_num:04d}"
                        row_data["asset_sn"] = get_asset_sn(asset)
                        row_data["asset_name"] = asset.name
                        
                        for c in attr_cols:
                            row_data[c] = "Yes" if c == attr else None
                            
                        row_data["attribute_result"] = f"{chinese_suffix_map.get(attr, '')} / {attr}"
                        row_data["damage_scenario_sn"] = ds.get("damage_scenario_sn", "")
                        row_data["damage_scenario"] = ds.get("damage_scenario", "")
                        
                        impact_rat = ds.get("impact_rating", {})
                        row_data["safety"] = impact_rat.get("safety", "Negligible")
                        row_data["financial"] = impact_rat.get("financial", "Negligible")
                        row_data["operational"] = impact_rat.get("operational", "Negligible")
                        row_data["privacy"] = impact_rat.get("privacy", "Negligible")
                        
                        overall_imp_val = ds.get("overall_impact", "Negligible")
                        if isinstance(overall_imp_val, int):
                            overall_imp_val = ["Negligible", "Moderate", "Major", "Severe"][min(max(overall_imp_val, 0), 3)]
                        row_data["overall_impact"] = overall_imp_val
                        
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
                        row_data["caf_level"] = feas_disp
                        
                        row_data["risk_value"] = rd.get("risk_value", "")
                        row_data["risk_treatment"] = rt_disp
                        row_data["cybersecurity_claim_id"] = rd.get("cybersecurity_claim_id", "N/A") if rt_disp in ["Share", "Retain"] else "N/A"
                        row_data["cybersecurity_claim"] = rd.get("cybersecurity_claim", "N/A") if rt_disp in ["Share", "Retain"] else "N/A"
                        row_data["cybersecurity_goal"] = rd.get("cybersecurity_goal", "N/A") if rt_disp == "Reduce" else "N/A"
                        
                        row_data["cybersecurity_control"] = cybersecurity_control_val
                        row_data["allocated_to_device"] = allocated_to_device_val
                        row_data["cybersecurity_requirement"] = cybersecurity_requirement_val
                        
                        rows.append(row_data)
                        row_num += 1
                        
    return rows

def create_excel_report(domain: Domain, steps: List[TaraStep], assets: List[Asset], desensitize: bool) -> str:
    """
    生成高度美观的 ISO 21434 TARA XLSX 格式报表，支持脱敏过滤，布局完全对齐 J6P_TARA_Analysis.xlsx
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "TARA"
    
    ws.views.sheetView[0].showGridLines = True
    
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
    
    # 1. 标题行高度
    ws.row_dimensions[1].height = 30
    ws.row_dimensions[2].height = 35
    
    # 填充所有表头单元格默认格式
    for col_idx in range(1, 30):
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
        "Cybersecurity Claims ID", "Cybersecurity Claims", "Cybersecurity Goal", "Cybersecurity Control", "Allocated to ADCU", "Cybersecurity Requirement"
    ]
    
    for col_idx, h in enumerate(headers_r2, 1):
        ws.cell(row=2, column=col_idx, value=h)
        
    rows = collect_report_data(steps, assets, desensitize)
    
    current_row = 3
    keys = [
        "number", "asset_sn", "asset_name",
        "attribute_result",
        "damage_scenario_sn", "damage_scenario", "safety", "financial", "operational", "privacy", "overall_impact",
        "threat_scenario", "attack_path",
        "time_consuming", "expertise", "knowledge_about_toe", "window_of_opportunity", "equipment", "difficulty", "af_level", "caf_level",
        "risk_value", "risk_treatment",
        "cybersecurity_claim_id", "cybersecurity_claim", "cybersecurity_goal", "cybersecurity_control", "allocated_to_device", "cybersecurity_requirement"
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
        
    file_path = os.path.join(EXPORTS_DIR, f"TARA_Report_{domain.id}_{'desensitized' if desensitize else 'full'}.xlsx")
    wb.save(file_path)
    return file_path

def create_csv_report(domain: Domain, steps: List[TaraStep], assets: List[Asset], desensitize: bool) -> str:
    """
    生成 ISO 21434 TARA CSV 格式报表，支持脱敏过滤
    使用 utf-8-sig 编码，防止中文在 Excel 中打开时显示乱码
    """
    file_path = os.path.join(EXPORTS_DIR, f"TARA_Report_{domain.id}_{'desensitized' if desensitize else 'full'}.csv")
    
    headers_r2 = [
        "Number", "Assets SN", "Assets Name",
        "Cybersecurity Attributes Result",
        "Damage Scenarios SN", "Damage Scenarios", "Safety", "Financial", "Operational", "Privacy", "Impact Level",
        "Threat Scenarios", "Attack Path",
        "Time Consuming", "Expertise", "Knowledge about TOE", "Window of opportunity", "Equipment", "Difficulty", "AF Level", "CAF Level",
        "Risk Value", "Risk Treatment Recommend",
        "Cybersecurity Claims ID", "Cybersecurity Claims", "Cybersecurity Goal", "Cybersecurity Control", "Allocated to ADCU", "Cybersecurity Requirement"
    ]
    
    keys = [
        "number", "asset_sn", "asset_name",
        "attribute_result",
        "damage_scenario_sn", "damage_scenario", "safety", "financial", "operational", "privacy", "overall_impact",
        "threat_scenario", "attack_path",
        "time_consuming", "expertise", "knowledge_about_toe", "window_of_opportunity", "equipment", "difficulty", "af_level", "caf_level",
        "risk_value", "risk_treatment",
        "cybersecurity_claim_id", "cybersecurity_claim", "cybersecurity_goal", "cybersecurity_control", "allocated_to_device", "cybersecurity_requirement"
    ]
    
    rows = collect_report_data(steps, assets, desensitize)
    
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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    导出网络安全 TARA 评估报告 (XLSX/CSV，支持数据脱敏，BR-57, BR-77)
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
    
    if format.lower() == "xlsx":
        file_path = create_excel_report(domain, steps, assets, desensitize)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = f"TARA_Report_{domain.name}_{'desensitized' if desensitize else 'full'}.xlsx"
    elif format.lower() == "csv":
        file_path = create_csv_report(domain, steps, assets, desensitize)
        media_type = "text/csv; charset=utf-8-sig"
        filename = f"TARA_Report_{domain.name}_{'desensitized' if desensitize else 'full'}.csv"
    else:
        raise HTTPException(status_code=400, detail="不支持的导出文件格式，仅支持 'xlsx' 或 'csv'。")
        
    return FileResponse(
        path=file_path,
        media_type=media_type,
        filename=filename
    )
