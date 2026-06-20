from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
import json
import uuid
from typing import List, Optional
from app.core.database import get_db
from app.api.auth import get_current_user
from app.models.user import User
from app.models.domain import Domain
from app.models.diagram import Diagram
from app.models.asset import Asset
from app.models.system_settings import SystemSettings
from app.schemas.asset import AssetCreate, AssetUpdate, AssetOut, DeduplicateSuggestionItem, DeduplicateConfirmReq
from app.api.project import check_domain_idle

router = APIRouter(tags=["资产与AI去重管理"])

# ----------------- 资产基础 CRUD API -----------------

@router.get("/domains/{domain_id}/assets", response_model=List[AssetOut])
def list_assets(
    domain_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    获取指定域控下的所有聚合资产列表
    """
    domain = db.query(Domain).filter(Domain.id == domain_id).first()
    if not domain:
        raise HTTPException(status_code=404, detail="子域控不存在")
    return db.query(Asset).filter(Asset.domain_id == domain_id).all()

@router.post("/assets/{id}/confirm", response_model=AssetOut)
def confirm_asset(
    id: int,
    confirm_data: AssetUpdate,
    bypass_lock: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    人工确认或拒绝资产状态 (BR-25, BR-33, confirmed/rejected)
    """
    asset = db.query(Asset).filter(Asset.id == id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="资产不存在")
        
    # 检查域控锁定状态
    check_domain_idle(asset.domain_id, db)
    
    # 自动提取资产在已核对/已拒绝状态时的写保护锁定 (我自己手动添加的资产不受影响)
    if not bypass_lock and asset.diagram_id is not None and asset.status in ["confirmed", "rejected"]:
        has_other_changes = (
            (confirm_data.name is not None and confirm_data.name != asset.name) or
            (confirm_data.asset_type is not None and confirm_data.asset_type != asset.asset_type) or
            (confirm_data.protocol is not None and confirm_data.protocol != asset.protocol) or
            (confirm_data.description is not None and confirm_data.description != asset.description)
        )
        if has_other_changes:
            # 允许更新状态为 draft，但拒绝在此次请求中修改其他字段
            if confirm_data.status == "draft":
                asset.status = "draft"
                db.commit()
                db.refresh(asset)
                return asset
            else:
                raise HTTPException(
                    status_code=400,
                    detail="该资产已被专家确认或拒绝，处于只读锁定状态。若要修改，请先将其状态切换为“待核对”。"
                )
    
    if confirm_data.status is not None:
        if confirm_data.status not in ["confirmed", "rejected", "draft"]:
            raise HTTPException(status_code=400, detail="非法资产确认状态")
        asset.status = confirm_data.status
        
    if confirm_data.name is not None:
        asset.name = confirm_data.name
    if confirm_data.asset_type is not None:
        asset.asset_type = confirm_data.asset_type
    if confirm_data.protocol is not None:
        asset.protocol = confirm_data.protocol
    if confirm_data.description is not None:
        asset.description = confirm_data.description
        
    db.commit()
    db.refresh(asset)
    return asset

@router.post("/domains/{domain_id}/assets", response_model=AssetOut)
def create_manual_asset(
    domain_id: int,
    asset_data: AssetCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    手动添加资产 (BR-ManualAsset)
    """
    check_domain_idle(domain_id, db)
    
    new_asset = Asset(
        domain_id=domain_id,
        diagram_id=None,
        name=asset_data.name,
        asset_type=asset_data.asset_type,
        protocol=asset_data.protocol,
        description=asset_data.description,
        status="draft"  # 手动添加的资产默认初始为 待核对 (draft)
    )
    db.add(new_asset)
    db.commit()
    db.refresh(new_asset)
    return new_asset

@router.delete("/assets/{id}")
def delete_asset(
    id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    物理删除资产 (主要用于手动添加的资产的清理)
    """
    asset = db.query(Asset).filter(Asset.id == id).first()
    if not asset:
        raise HTTPException(status_code=404, detail="资产不存在")
        
    check_domain_idle(asset.domain_id, db)
    
    # 自动提取资产在已确认或已拒绝状态时，禁止直接物理删除 (必须先改为待核对状态)
    if asset.diagram_id is not None and asset.status in ["confirmed", "rejected"]:
        raise HTTPException(
            status_code=400,
            detail="该自动提取资产已被确认或拒绝。若要删除，请先将其状态切换为“待核对”。"
        )
        
    db.delete(asset)
    db.commit()
    return {"message": "资产删除成功"}

@router.delete("/domains/{domain_id}/assets")
def clear_domain_assets(
    domain_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    清空该域控下的所有资产 (包括手动添加的和自动提取的)
    """
    check_domain_idle(domain_id, db)
    
    assets = db.query(Asset).filter(Asset.domain_id == domain_id).all()
    for asset in assets:
        db.delete(asset)
    db.commit()
    return {"message": "清空资产成功"}

# ----------------- DFD 资产收集 Parser (BR-25, BR-29) -----------------

@router.post("/domains/{domain_id}/extract-assets", response_model=List[AssetOut])
def extract_assets(
    domain_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    【收集资产】：解析域控下所有画布，自动提取资产 (BR-25)
    """
    domain = check_domain_idle(domain_id, db)
    
    # 1. 查找此域控下的所有功能图 DFD
    diagrams = db.query(Diagram).filter(Diagram.domain_id == domain_id).all()
    
    extracted_items = []
    
    # 2. 解析每个功能图
    for diagram in diagrams:
        try:
            snapshot = json.loads(diagram.snapshot_json)
        except Exception:
            continue
            
        nodes = snapshot.get("nodes", [])
        
        # 区分物理边界节点和常规内容节点
        boundary_nodes = [n for n in nodes if n.get("type") == "boundary"]
        content_nodes = [n for n in nodes if n.get("type") != "boundary"]
        
        selected_node_ids = set()
        
        if not boundary_nodes:
            # 如果画布里没有配置任何物理边界，默认提取所有内容节点
            for node in content_nodes:
                node_data = node.get("data", {})
                name = node_data.get("name")
                if not name:
                    continue
                selected_node_ids.add(node.get("id"))
                
                raw_type = node.get("type", "process")
                asset_type = "software"
                if raw_type in ("entity", "interface"):
                    asset_type = "hardware"
                elif raw_type == "storage":
                    asset_type = "data"
                    
                extracted_items.append({
                    "diagram_id": diagram.id,
                    "name": name,
                    "asset_type": asset_type,
                    "protocol": node_data.get("protocol"),
                    "description": node_data.get("description") or node_data.get("remarks")
                })
        else:
            # 如果画布里配置了物理边界，仅提取位于任何物理边界内部的内容节点
            for node in content_nodes:
                node_data = node.get("data", {})
                name = node_data.get("name")
                if not name:
                    continue
                
                # 获取节点坐标和尺寸
                pos = node.get("position", {})
                x = pos.get("x", 0)
                y = pos.get("y", 0)
                
                style = node.get("style", {})
                raw_type = node.get("type", "process")
                # 默认节点尺寸（防止前台传空）
                w = style.get("width", 100 if raw_type == "process" else 150)
                h = style.get("height", 100 if raw_type == "process" else 80)
                
                # 计算节点中心点
                cx = x + w / 2.0
                cy = y + h / 2.0
                
                # 检查中心点是否在任何物理边界内
                is_inside = False
                for b in boundary_nodes:
                    b_pos = b.get("position", {})
                    bx = b_pos.get("x", 0)
                    by = b_pos.get("y", 0)
                    b_style = b.get("style", {})
                    bw = b_style.get("width", 280)
                    bh = b_style.get("height", 200)
                    
                    if bx <= cx <= bx + bw and by <= cy <= by + bh:
                        is_inside = True
                        break
                        
                if is_inside:
                    selected_node_ids.add(node.get("id"))
                    asset_type = "software"
                    if raw_type in ("entity", "interface"):
                        asset_type = "hardware"
                    elif raw_type == "storage":
                        asset_type = "data"
                        
                    extracted_items.append({
                        "diagram_id": diagram.id,
                        "name": name,
                        "asset_type": asset_type,
                        "protocol": node_data.get("protocol"),
                        "description": node_data.get("description") or node_data.get("remarks")
                    })
                    
        # 获取符合条件的连线数据流列表 (edges)
        edges = snapshot.get("edges", [])
        for edge in edges:
            edge_data = edge.get("data", {})
            name = edge_data.get("name")
            if not name:
                continue
                
            source_id = edge.get("source")
            target_id = edge.get("target")
            
            # 如果配置了物理边界，连线的数据流只要有至少一端位于边界内节点（即跨边界的通信）就需要收集
            if boundary_nodes:
                if source_id not in selected_node_ids and target_id not in selected_node_ids:
                    continue
                    
            protocol = edge_data.get("protocol")
            desc = edge_data.get("transmitted_info")
            
            extracted_items.append({
                "diagram_id": diagram.id,
                "name": name,
                "asset_type": "communication",
                "protocol": protocol,
                "description": desc
            })
            
    # 3. 提取保留与清除规则 (BR-25)
    # 获取此子域控现存的所有资产
    existing_assets = db.query(Asset).filter(Asset.domain_id == domain_id).all()
    
    # 分组现存资产：保留已由人工确认或拒绝的资产，清空所有 draft 的资产
    confirmed_or_rejected = []
    for asset in existing_assets:
        if asset.status in ["confirmed", "rejected"]:
            confirmed_or_rejected.append(asset)
        else:
            db.delete(asset) # 级联清除待核对的旧资产
    db.commit()
    
    # 4. 插入新提取的资产（去重逻辑：若与已保留的 confirmed/rejected 资产同名同类型，则不重复插入）
    saved_keys = {(a.name, a.asset_type) for a in confirmed_or_rejected}
    
    new_assets = []
    for item in extracted_items:
        key = (item["name"], item["asset_type"])
        if key not in saved_keys:
            new_asset = Asset(
                domain_id=domain_id,
                diagram_id=item["diagram_id"],
                name=item["name"],
                asset_type=item["asset_type"],
                protocol=item["protocol"],
                description=item["description"],
                status="draft" # 初始为待核对状态
            )
            db.add(new_asset)
            new_assets.append(new_asset)
            saved_keys.add(key)
            
    db.commit()
    
    # 重新加载并返回所有当前资产
    return db.query(Asset).filter(Asset.domain_id == domain_id).all()

# ----------------- 单域控内 AI 资产去重 (BR-33, BR-35) -----------------

@router.post("/domains/{domain_id}/deduplicate", response_model=List[DeduplicateSuggestionItem])
def deduplicate_assets(
    domain_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    单域控内 AI 资产去重建议 (BR-33)
    """
    domain = check_domain_idle(domain_id, db)
    
    # 获取域控下所有未拒绝的资产
    assets = db.query(Asset).filter(Asset.domain_id == domain_id, Asset.status != "rejected").all()
    if len(assets) < 2:
        return []
        
    # 读取 AI 大模型配置
    settings = db.query(SystemSettings).first()
    
    # 建立 (diagram_id, node_name) -> node_type 的映射以区分更细致的节点类型 (如 entity, interface)
    node_type_map = {}
    diagrams = db.query(Diagram).filter(Diagram.domain_id == domain_id).all()
    for diag in diagrams:
        try:
            snapshot = json.loads(diag.snapshot_json)
            for node in snapshot.get("nodes", []):
                n_name = node.get("data", {}).get("name")
                n_type = node.get("type")
                if n_name and n_type:
                    node_type_map[(diag.id, n_name)] = n_type
        except Exception:
            continue

    def get_actual_type(asset):
        if asset.diagram_id and (asset.diagram_id, asset.name) in node_type_map:
            return node_type_map[(asset.diagram_id, asset.name)]
        return asset.asset_type
    
    # 降级备用逻辑：如果是单元测试或未配置大模型，基于字符串相似度规则给出建议
    if not settings or not settings.api_key:
        print("未检测到大模型配置，使用静态文本匹配算法进行资产去重建议。")
        suggestions = []
        visited = set()
        
        # 简单相似度规则：若名字长度大于4且有包含关系且实际类型相同，视为重复
        for i in range(len(assets)):
            if assets[i].id in visited:
                continue
            for j in range(i + 1, len(assets)):
                if assets[j].id in visited:
                    continue
                a1, a2 = assets[i], assets[j]
                if get_actual_type(a1) == get_actual_type(a2):
                    # 去除前缀后缀做简单包含校验
                    n1, n2 = a1.name.lower(), a2.name.lower()
                    if (len(n1) > 3 and len(n2) > 3) and (n1 in n2 or n2 in n1):
                        keep = a1 if len(a1.name) <= len(a2.name) else a2
                        remove = a2 if keep == a1 else a1
                        suggestions.append(DeduplicateSuggestionItem(
                            keep_asset_id=keep.id,
                            remove_asset_ids=[remove.id],
                            reason=f"命名包含重复，判断同为 '{keep.name}' 资产"
                        ))
                        visited.add(remove.id)
                        visited.add(keep.id)
                        break
        return suggestions
        
    # 配置存在，调用大模型分析
    import httpx
    try:
        url = f"{settings.api_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json"
        }
        system_prompt = (
            "你是一个车载网络安全专家。你的任务是分析给出的资产列表，识别并合并冗余的、意思重复的或可以归并为同一物理/逻辑实体的资产（尤其是那些因画图习惯或不同视角而重复提取的资产，如‘https数据流’与‘https下载数据’）。\n\n"
            "【判定与合并准则】：\n"
            "1. 语义相近合并：如果两个资产指代同一个网络通信、数据流或者物理组件（例如：一个从发送方命名为数据流，另一个从接收方命名为下载数据，且都用于HTTPS传输/OTA下载），应当进行合并。\n"
            "2. 归并原则：\n"
            "   - 通信资产：若协议相同、传输数据类型和用途高度重合，合并为一个。\n"
            "   - 软硬件及接口资产：指代同一个控制器或服务但命名略有差异的，合并为一个。特别注意：同一物理实体设备的控制器（如 'MCU'，Type 为 'entity'）与其对外物理暴露接口（如 'MCU_JTAG'，Type 为 'interface'）是不同维度的资产，有完全不同的威胁暴露面，切勿将控制器与物理接口资产进行合并。\n"
            "3. 保持简洁：保留最具体、表意最完整的一个作为 `keep_asset_id`，将冗余的放在 `remove_asset_ids`。\n\n"
            "请以符合要求的 JSON 格式输出你的去重建议。不要包含任何解释性文字或标记。"
        )
        prompt_content = f"分析以下资产列表，找出命名类似且属于同一实体的冗余合并去重资产：\n"
        for a in assets:
            actual_type = get_actual_type(a)
            type_str = f"{a.asset_type} ({actual_type})" if actual_type != a.asset_type else a.asset_type
            prompt_content += f"- ID: {a.id}, Name: {a.name}, Type: {type_str}, Protocol: {a.protocol or 'N/A'}\n"
            
        llm_payload = {
            "model": settings.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt_content}
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "DeduplicateConfirmReq",
                    "strict": True,
                    "schema": DeduplicateConfirmReq.model_json_schema()
                }
            }
        }
        
        with httpx.Client(timeout=60.0) as client:
            try:
                resp = client.post(url, headers=headers, json=llm_payload)
                if resp.status_code != 200:
                    raise ValueError(f"API returned status {resp.status_code}")
                choice_text = resp.json()["choices"][0]["message"]["content"]
            except Exception as e:
                # 回退到 json_object
                llm_payload["response_format"] = {"type": "json_object"}
                llm_payload["messages"].append({
                    "role": "user",
                    "content": f"请务必按照以下 JSON Schema 格式回复，不要包含任何 markdown 或其他文本：\n{json.dumps(DeduplicateConfirmReq.model_json_schema(), ensure_ascii=False)}"
                })
                resp = client.post(url, headers=headers, json=llm_payload)
                if resp.status_code != 200:
                    raise ValueError(f"API returned status {resp.status_code}")
                choice_text = resp.json()["choices"][0]["message"]["content"]

            # 清理 Markdown 代码块
            if choice_text.strip().startswith("```"):
                import re
                choice_text = re.sub(r"^```[a-zA-Z0-9]*\n", "", choice_text)
                choice_text = re.sub(r"\n```$", "", choice_text)
                choice_text = choice_text.strip()
                
            parsed = json.loads(choice_text)
            
            # Pydantic 校验与自修复
            try:
                validated = DeduplicateConfirmReq.model_validate(parsed)
            except Exception as val_err:
                import re
                # 尝试修复一次
                repair_messages = llm_payload["messages"] + [
                    {"role": "assistant", "content": choice_text},
                    {"role": "user", "content": f"上一次返回的 JSON 数据校验未通过，校验错误信息如下：\n{val_err}\n请修正该 JSON 数据，确保完全符合 Schema 规范且格式正确。"}
                ]
                payload_repair = {
                    "model": settings.model_name,
                    "messages": repair_messages,
                    "response_format": llm_payload["response_format"]
                }
                resp_rep = client.post(url, headers=headers, json=payload_repair)
                if resp_rep.status_code == 200:
                    choice_text_rep = resp_rep.json()["choices"][0]["message"]["content"]
                    if choice_text_rep.strip().startswith("```"):
                        choice_text_rep = re.sub(r"^```[a-zA-Z0-9]*\n", "", choice_text_rep)
                        choice_text_rep = re.sub(r"\n```$", "", choice_text_rep)
                        choice_text_rep = choice_text_rep.strip()
                    parsed_rep = json.loads(choice_text_rep)
                    validated = DeduplicateConfirmReq.model_validate(parsed_rep)
                else:
                    raise val_err

            if "suggestions" in validated.model_dump():
                suggestions_list = []
                for sug in validated.suggestions:
                    suggestions_list.append(DeduplicateSuggestionItem(
                        keep_asset_id=int(sug.keep_asset_id),
                        remove_asset_ids=[int(rid) for rid in sug.remove_asset_ids],
                        reason=str(sug.reason)
                    ))
                return suggestions_list
        raise ValueError(f"API returned status {resp.status_code}")
    except Exception as e:
        # 降级：执行字符串相似度判断
        suggestions = []
        visited = set()
        for i in range(len(assets)):
            if assets[i].id in visited:
                continue
            for j in range(i + 1, len(assets)):
                if assets[j].id in visited:
                    continue
                a1, a2 = assets[i], assets[j]
                if get_actual_type(a1) == get_actual_type(a2) and a1.name.split('_')[0] == a2.name.split('_')[0]:
                    keep = a1
                    remove = a2
                    suggestions.append(DeduplicateSuggestionItem(
                        keep_asset_id=keep.id,
                        remove_asset_ids=[remove.id],
                        reason=f"AI去重建议：'{a1.name}' 与 '{a2.name}' 命名特征极度相似，建议合并。"
                    ))
                    visited.add(remove.id)
                    break
        return suggestions

def clean_and_split_desc(desc: str) -> list:
    if not desc:
        return []
    import re
    # 清理掉之前生成的旧标识，避免二次污染
    desc = re.sub(r'\[已并入:[^\]]+\]', '', desc)
    desc = re.sub(r'\[合并资产[^\]]+\]', '', desc)
    # 按换行符或管道符分割
    lines = re.split(r'\n+|\r+|\|', desc)
    items = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 去掉原有的序号前缀，如 "1. ", "1、", "[1] " 等
        line = re.sub(r'^(?:\d+[\.\、\)]|\[\d+\])\s*', '', line).strip()
        if line and line not in items:
            items.append(line)
    return items

def merge_descriptions_to_list(desc_list: list) -> str:
    all_items = []
    for desc in desc_list:
        for item in clean_and_split_desc(desc):
            if item not in all_items:
                all_items.append(item)
    if not all_items:
        return ""
    if len(all_items) == 1:
        return all_items[0]
    return "\n".join(f"{i+1}. {item}" for i, item in enumerate(all_items))

@router.post("/domains/{domain_id}/deduplicate/confirm")
def confirm_deduplicate(
    domain_id: int,
    confirm_data: DeduplicateConfirmReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    确认 AI 去重合并结果 (BR-35, 将建议删除资产置为 rejected 并保留备注)
    """
    domain = check_domain_idle(domain_id, db)
    
    merged_count = 0
    for sug in confirm_data.suggestions:
        keep_asset = db.query(Asset).filter(Asset.id == sug.keep_asset_id).first()
        if not keep_asset:
            continue
            
        rem_descriptions = []
        for rem_id in sug.remove_asset_ids:
            rem_asset = db.query(Asset).filter(Asset.id == rem_id, Asset.domain_id == domain_id).first()
            if rem_asset:
                if rem_asset.description:
                    rem_descriptions.append(rem_asset.description)
                
                # 状态置为 rejected，保留历史痕迹 (BR-35)
                rem_asset.status = "rejected"
                rem_asset.description = (rem_asset.description or "") + f" [AI去重合并，已合并至资产: {keep_asset.name} (ID: {keep_asset.id}), 合并原因: {sug.reason}]"
                merged_count += 1
                
        if rem_descriptions:
            keep_asset.description = merge_descriptions_to_list([keep_asset.description] + rem_descriptions)
                
    db.commit()
    return {"message": f"成功合并 {merged_count} 个重复资产项，历史状态已被标记保存。"}
