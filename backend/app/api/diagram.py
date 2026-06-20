from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
import redis
import json
from typing import List, Optional
from app.core.database import get_db
from app.api.auth import get_current_user
from app.models.user import User
from app.models.domain import Domain
from app.models.diagram import Diagram
from app.schemas.diagram import DiagramUpdate, DiagramOut
from app.api.project import check_domain_idle, check_project_active
from pydantic import BaseModel, Field

class ReasoningStepsSchema(BaseModel):
    elements_analysis: str = Field(description="第一步分析车载拓扑元素及类型")
    relationships_analysis: str = Field(description="第二步分析网卡与安全边界拓扑位置")
    data_flows_analysis: str = Field(description="第三步分析通信交互数据流与传输协议")
    properties_fill_analysis: str = Field(description="第四步分析各节点和连线的详细属性说明")

class PositionSchema(BaseModel):
    x: float
    y: float

class NodeStyleSchema(BaseModel):
    width: float
    height: float

class NodeDataSchema(BaseModel):
    name: str = Field(description="资产名称")
    description: str = Field(description="详细功能描述和安全作用，不能有符号干扰且不要为空")
    protocol: str = Field(description="具体的通信协议或数据协议类型（如 CAN, LIN, Ethernet, HTTPS, FTP 等，尽量用规范大写字母，不要为空）")
    remarks: str = Field(description="相关的安全备注或资产标记")

class NodeSchema(BaseModel):
    id: str = Field(description="唯一的节点ID，例如 n1, n2")
    type: str = Field(description="节点类型，可选值: process, entity, interface, storage, boundary")
    position: PositionSchema
    style: Optional[NodeStyleSchema] = None
    data: NodeDataSchema

class EdgeDataSchema(BaseModel):
    name: str = Field(description="数据流名称")
    protocol: str = Field(description="数据流传输协议")
    transmitted_info: str = Field(description="传输的具体数据内容，不要为空")

class EdgeSchema(BaseModel):
    id: str = Field(description="唯一的连线ID，例如 e1, e2")
    source: str = Field(description="源节点ID")
    target: str = Field(description="目标节点ID")
    data: EdgeDataSchema

class DiagramTemplateOutput(BaseModel):
    reasoning_steps: ReasoningStepsSchema
    nodes: List[NodeSchema]
    edges: List[EdgeSchema]

class DiagramChatOutput(BaseModel):
    reply: str = Field(description="你对用户需求的理解和设计方案的自然语言描述（请用中文回答，不要包含 markdown 格式，控制在200字以内）")
    snapshot_json: DiagramTemplateOutput = Field(description="包含 reasoning_steps、nodes 和 edges 的 DFD 拓扑数据对象")


router = APIRouter(prefix="/diagrams", tags=["功能图与编辑器管理"])

import os

# 初始化 Redis 客户端连接
try:
    redis_host = os.getenv("REDIS_HOST", "127.0.0.1")
    redis_client = redis.Redis(host=redis_host, port=6379, db=0, decode_responses=True)
except Exception:
    redis_client = None

def get_lock_key(diagram_id: int) -> str:
    return f"lock:diagram:{diagram_id}"

# ----------------- 画布 CRUD API -----------------

@router.post("", response_model=DiagramOut)
def create_diagram(
    domain_id: int,
    title: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    新建功能图 (DFD)
    """
    # 检查域控锁定状态
    check_domain_idle(domain_id, db)
    
    diagram = Diagram(
        domain_id=domain_id,
        title=title,
        version_no=1,
        snapshot_json="{}"
    )
    db.add(diagram)
    db.commit()
    db.refresh(diagram)
    return diagram

@router.get("/domain/{domain_id}", response_model=List[DiagramOut])
def list_diagrams(
    domain_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    获取子域控下的所有功能图列表
    """
    domain = db.query(Domain).filter(Domain.id == domain_id).first()
    if not domain:
        raise HTTPException(status_code=404, detail="子域控不存在")
    return db.query(Diagram).filter(Diagram.domain_id == domain_id).all()

@router.get("/{id}", response_model=DiagramOut)
def get_diagram(
    id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    获取单个功能图详情
    """
    diagram = db.query(Diagram).filter(Diagram.id == id).first()
    if not diagram:
        raise HTTPException(status_code=404, detail="功能图不存在")
    return diagram

@router.put("/{id}", response_model=DiagramOut)
def update_diagram(
    id: int,
    diagram_data: DiagramUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    保存画布 (乐观锁并发控制，BR-16)
    """
    diagram = db.query(Diagram).filter(Diagram.id == id).first()
    if not diagram:
        raise HTTPException(status_code=404, detail="功能图不存在")
        
    # 检查域控锁定状态
    check_domain_idle(diagram.domain_id, db)
    
    # 乐观锁校验: 前端上传的版本号必须等于当前数据库的版本号
    if diagram.version_no != diagram_data.version_no:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="画布已被其他成员更新，请刷新。"
        )
        
    # 执行保存并递增版本号
    diagram.snapshot_json = diagram_data.snapshot_json
    diagram.version_no += 1
    db.commit()
    db.refresh(diagram)
    return diagram

@router.delete("/{id}")
def delete_diagram(
    id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    物理删除功能图 (级联清除来源于该 DFD 的资产项，BR-04/09)
    """
    diagram = db.query(Diagram).filter(Diagram.id == id).first()
    if not diagram:
        raise HTTPException(status_code=404, detail="功能图不存在")
        
    # 检查域控锁定状态
    check_domain_idle(diagram.domain_id, db)
    
    db.delete(diagram)
    db.commit()
    return {"message": f"功能图 {id} 及其关联资产清除成功"}

# ----------------- 基于 Redis 的独占编辑锁 (BR-72) -----------------

@router.post("/{id}/lock")
def acquire_lock(
    id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    尝试获取画布的独占编辑锁 (BR-72)
    """
    diagram = db.query(Diagram).filter(Diagram.id == id).first()
    if not diagram:
        raise HTTPException(status_code=404, detail="功能图不存在")
        
    # 检查是否已锁定
    check_domain_idle(diagram.domain_id, db)
    
    lock_key = get_lock_key(id)
    
    if redis_client:
        try:
            current_lock_val = redis_client.get(lock_key)
            if current_lock_val:
                lock_info = json.loads(current_lock_val)
                # 如果是自己占有的锁，直接返回成功并续期
                if lock_info.get("username") == current_user.username:
                    redis_client.expire(lock_key, 300) # 续期 5 分钟
                    return {"success": True, "locked_by": current_user.username}
                else:
                    # 被其他人占有，返回 423 Locked
                    raise HTTPException(
                        status_code=status.HTTP_423_LOCKED,
                        detail=f"当前正在被 {lock_info.get('username')} 编辑"
                    )
            
            # 锁不存在，进行抢占
            lock_val = json.dumps({"username": current_user.username})
            redis_client.set(lock_key, lock_val, ex=300) # 设置 5 分钟过期 (300秒)
            return {"success": True, "locked_by": current_user.username}
        except redis.RedisError as e:
            # Redis 异常，降级通过
            print(f"Redis 锁故障，降级直接获取: {e}")
            return {"success": True, "locked_by": current_user.username, "note": "Redis offline, bypass lock"}
    else:
        # 无 Redis 服务，降级通过
        return {"success": True, "locked_by": current_user.username, "note": "No Redis, bypass lock"}

@router.post("/{id}/heartbeat")
def heartbeat_lock(
    id: int,
    current_user: User = Depends(get_current_user)
):
    """
    心跳包续期编辑锁 (BR-72, 30秒续期)
    """
    lock_key = get_lock_key(id)
    if redis_client:
        try:
            current_lock_val = redis_client.get(lock_key)
            if not current_lock_val:
                raise HTTPException(status_code=400, detail="编辑锁已失效或已被他人抢占，请重新加载。")
                
            lock_info = json.loads(current_lock_val)
            if lock_info.get("username") != current_user.username:
                raise HTTPException(status_code=403, detail=f"该画布当前编辑锁由 {lock_info.get('username')} 持有")
                
            redis_client.expire(lock_key, 300) # 续期 5 分钟
            return {"success": True, "message": "锁心跳续期成功"}
        except redis.RedisError:
            return {"success": True, "message": "Redis 故障，跳过心跳"}
    return {"success": True, "message": "跳过心跳续期"}

@router.post("/{id}/release")
def release_lock(
    id: int,
    current_user: User = Depends(get_current_user)
):
    """
    主动释放编辑锁 (BR-72)
    """
    lock_key = get_lock_key(id)
    if redis_client:
        try:
            current_lock_val = redis_client.get(lock_key)
            if current_lock_val:
                lock_info = json.loads(current_lock_val)
                if lock_info.get("username") == current_user.username:
                    redis_client.delete(lock_key)
                    return {"success": True, "message": "成功释放编辑锁"}
        except redis.RedisError:
            pass
    return {"success": True, "message": "锁已释放或无需释放"}

from pydantic import BaseModel
from app.models.system_settings import SystemSettings
import httpx

class AIGenerateReq(BaseModel):
    prompt: str

@router.post("/{id}/ai-generate", response_model=DiagramOut)
def ai_generate_topology(
    id: int,
    req_data: AIGenerateReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    AI 拓扑画图一键生成接口 (BR-页面3, system_design.md 5.2)
    """
    diagram = db.query(Diagram).filter(Diagram.id == id).first()
    if not diagram:
        raise HTTPException(status_code=404, detail="功能图不存在")
        
    # 检查域控锁定状态
    check_domain_idle(diagram.domain_id, db)
    
    reasoning_steps = {}
    # 规则算法生成 Mock 数据作为降级 (当大模型不可用或没有 API_KEY 时)
    p = req_data.prompt.lower()
    if "诊断" in p or "diagnostic" in p:
        nodes = [
            {
                "id": "n1",
                "type": "entity",
                "position": {"x": 100, "y": 150},
                "data": {"name": "OBD物理接口", "description": "物理OBD诊断接口，连接诊断仪", "protocol": "CAN", "remarks": "外部测试节点"}
            },
            {
                "id": "n2",
                "type": "process",
                "position": {"x": 300, "y": 150},
                "data": {"name": "诊断网关ECU", "description": "车身网关，负责报文过滤路由", "protocol": "CAN-FD", "remarks": "核心控制单元"}
            },
            {
                "id": "n3",
                "type": "process",
                "position": {"x": 500, "y": 150},
                "data": {"name": "诊断服务进程", "description": "UDS 协议栈与诊断任务分发", "protocol": "UDS", "remarks": "核心软件进程"}
            }
        ]
        edges = [
            {
                "id": "e1",
                "source": "n1",
                "target": "n2",
                "data": {"name": "UDS请求帧", "transmitted_info": "UDS服务请求"}
            },
            {
                "id": "e2",
                "source": "n2",
                "target": "n3",
                "data": {"name": "内部路由报文", "transmitted_info": "过滤校验后的UDS消息"}
            }
        ]
    elif "ota" in p or "升级" in p or "update" in p:
        nodes = [
            {
                "id": "n1",
                "type": "entity",
                "position": {"x": 100, "y": 150},
                "data": {"name": "OTA云端服务器", "description": "云端管理与包分发系统", "protocol": "HTTPS", "remarks": "云端节点"}
            },
            {
                "id": "n2",
                "type": "process",
                "position": {"x": 300, "y": 150},
                "data": {"name": "车载T-BOX", "description": "车载无线连接终端", "protocol": "Ethernet", "remarks": "通信节点"}
            },
            {
                "id": "n3",
                "type": "process",
                "position": {"x": 500, "y": 150},
                "data": {"name": "OTA管理器进程", "description": "负责固件校验和刷写管理", "protocol": "UDS", "remarks": "软件服务"}
            }
        ]
        edges = [
            {
                "id": "e1",
                "source": "n1",
                "target": "n2",
                "data": {"name": "固件下载包", "transmitted_info": "加密签名的固件镜像"}
            },
            {
                "id": "e2",
                "source": "n2",
                "target": "n3",
                "data": {"name": "固件分发流", "transmitted_info": "刷写镜像及UDS升级指令"}
            }
        ]
    else:
        nodes = [
            {
                "id": "n1",
                "type": "entity",
                "position": {"x": 100, "y": 150},
                "data": {"name": "传感器节点", "description": "数据采集物理传感器", "protocol": "LIN", "remarks": "基础传感器"}
            },
            {
                "id": "n2",
                "type": "process",
                "position": {"x": 300, "y": 150},
                "data": {"name": "主控制器ECU", "description": "中央数据处理与指令解算", "protocol": "CAN", "remarks": "控制中枢"}
            },
            {
                "id": "n3",
                "type": "entity",
                "position": {"x": 500, "y": 150},
                "data": {"name": "执行器部件", "description": "车辆物理执行机构", "protocol": "PWM", "remarks": "执行器"}
            }
        ]
        edges = [
            {
                "id": "e1",
                "source": "n1",
                "target": "n2",
                "data": {"name": "采样信号", "transmitted_info": "采集信号流"}
            },
            {
                "id": "e2",
                "source": "n2",
                "target": "n3",
                "data": {"name": "控制信号", "transmitted_info": "驱动脉宽信号"}
            }
        ]

    # 尝试调用 LLM (如果已配置)
    settings = db.query(SystemSettings).first()
    if settings and settings.api_key and settings.api_key != "mock_test_key":
        try:
            url = f"{settings.api_base_url.rstrip('/')}/chat/completions"
            headers = {
                "Authorization": f"Bearer {settings.api_key}",
                "Content-Type": "application/json"
            }
            # 请求结构化 JSON 格式
            system_prompt = (
                "你是一个车载网络安全拓扑设计助手。请根据用户的需求，输出标准的 DFD 拓扑设计 JSON。\n"
                "本系统将根据你设计的 DFD 拓扑自动提取安全资产，请仔细遵循以下映射关系来绘制并填写属性，以便自动提取完整正确的资产：\n"
                "- 节点类型 type='process' 代表【软件资产】；\n"
                "- 节点类型 type='entity' 代表【硬件资产】（如OBD接口、网关控制器、物理设备）；\n- 节点类型 type='interface' 代表【接口资产】（属于硬件资产，如串口、USB, JTAG等）；\n"
                "- 节点类型 type='storage' 代表【数据资产】（如数据库、本地配置文件、内存数据）；\n"
                "- 节点类型 type='boundary' 代表【物理安全边界】；\n"
                "- 连线 edges 代表【数据流/通信资产】。\n"
                "\n"
                "请务必在生成每个节点和连线时填好对应属性以供提取：\n"
                "- 节点的 data.name 必须是具体的资产名称，不要有符号干扰；\n"
                "- 节点的 data.description 填该节点详细的功能描述和安全作用，不要为空；\n"
                "- 节点的 data.protocol 填具体的通信协议或数据协议类型（如 CAN, LIN, Ethernet, HTTPS, FTP 等，尽量用规范大写字母，不要为空）；\n"
                "- 节点的 data.remarks 填相关的安全备注或资产标记；\n"
                "- 连线的 data.name 填具体数据流名称；\n"
                "- 连线的 data.protocol 填数据流传输采用的协议；\n"
                "- 连线的 data.transmitted_info 填传输的具体数据内容（如固件刷写包、诊断请求指令等，不要为空）。\n"
                "\n"
                "【🧠 结构化推理思维链 (Chain of Thought)】：\n"
                "为了提高输出拓扑的安全严谨性与合理性，你必须在输出中包含一个 `reasoning_steps` 字段。在输出 `nodes` 和 `edges` 之前，在 `reasoning_steps` 里严格按照以下四个步骤按顺序进行分析：\n"
                "1. elements_analysis (元素分析): 分析并识别该场景中应包含哪些节点资产；\n"
                "2. relationships_analysis (关系分析): 分析这些节点应处于什么网段与物理边界关系中（如外部网络、车身网段、安全边界等）；\n"
                "3. data_flows_analysis (数据流分析): 梳理节点之间的数据交互方向与所采用的通信协议；\n"
                "4. properties_fill_analysis (属性填充分析): 说明如何为节点和数据流的 data 字段填充真实的属性名称、详细安全功能描述与通信内容备注。\n\n"
                "请以符合要求的 JSON 格式输出拓扑。不要包含任何非 JSON 文字或 markdown 标记。"
            )
            llm_payload = {
                "model": settings.model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"请为以下功能绘制DFD画布结构：{req_data.prompt}"}
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "DiagramTemplateOutput",
                        "strict": True,
                        "schema": DiagramTemplateOutput.model_json_schema()
                    }
                }
            }
            import httpx
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
                        "content": f"请务必按照以下 JSON Schema 格式回复，不要包含任何 markdown 或其他文本：\n{json.dumps(DiagramTemplateOutput.model_json_schema(), ensure_ascii=False)}"
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
                validated = DiagramTemplateOutput.model_validate(parsed)
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
                with httpx.Client(timeout=60.0) as client:
                    resp_rep = client.post(url, headers=headers, json=payload_repair)
                if resp_rep.status_code == 200:
                    choice_text_rep = resp_rep.json()["choices"][0]["message"]["content"]
                    if choice_text_rep.strip().startswith("```"):
                        choice_text_rep = re.sub(r"^```[a-zA-Z0-9]*\n", "", choice_text_rep)
                        choice_text_rep = re.sub(r"\n```$", "", choice_text_rep)
                        choice_text_rep = choice_text_rep.strip()
                    parsed_rep = json.loads(choice_text_rep)
                    validated = DiagramTemplateOutput.model_validate(parsed_rep)
                else:
                    raise val_err

            validated_dict = validated.model_dump(by_alias=True)
            nodes = validated_dict.get("nodes", nodes)
            edges = validated_dict.get("edges", edges)
            reasoning_steps = validated_dict.get("reasoning_steps", {})
        except Exception as e:
            print(f"⚠️ LLM 拓扑生成失败，降级执行规则算法: {e}")

    # 保存并更新 version_no 自增
    snapshot = {"nodes": nodes, "edges": edges}
    if reasoning_steps:
        snapshot["reasoning_steps"] = reasoning_steps
    diagram.snapshot_json = json.dumps(snapshot, ensure_ascii=False)
    diagram.version_no += 1
    db.commit()
    db.refresh(diagram)
    
    return diagram

def verify_dfd_compliance(db: Session, nodes: list, edges: list) -> str:
    """
    独立子代理 (Sub-Agent): 进行轻量化的 DFD 拓扑安全与合规性校验，符合 Context Budget 原则
    """
    settings = db.query(SystemSettings).first()
    
    # 规则算法作为 Mock 降级和兜底校验
    warnings = []
    
    # 1. 检查是否有 entity 直接与 storage 节点通信而没有经过任何 process
    entity_ids = {n["id"] for n in nodes if n.get("type") == "entity"}
    storage_ids = {n["id"] for n in nodes if n.get("type") == "storage"}
    
    for edge in edges:
        s = edge.get("source")
        t = edge.get("target")
        if s in entity_ids and t in storage_ids:
            warnings.append("外部实体直接访问数据存储节点，存在未授权读写风险，应使用服务进程进行接口隔离控制。")
            
    # 2. 检查连线是否缺失关键属性
    for edge in edges:
        data = edge.get("data", {})
        if not data.get("protocol") or not data.get("transmitted_info"):
            warnings.append(f"连线 {data.get('name', edge.get('id'))} 传输协议或内容不完整，可能导致资产识别失真。")
            
    local_warning = " / ".join(warnings) if warnings else "拓扑合规性初步校验通过。"

    if not settings or not settings.api_key or settings.api_key == "mock_test_key":
        return f"【🛡️ 拓扑安全校验反馈 (Local Rule-Gate)】: {local_warning}"

    try:
        url = f"{settings.api_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {settings.api_key}",
            "Content-Type": "application/json"
        }
        system_prompt = (
            "你是一个车载数据流图（DFD）安全合规性校验助手（Compliance Check Sub-Agent）。\n"
            "请针对输入的数据流图进行安全设计审查。主要检查以下两条底线校验原则：\n"
            "1. 边界隔离检查：外部实体或接口（如OBD物理接口、OTA云端、T-BOX等entity类型）不能在没有网关过滤的情况下直接与内部进程或数据库（process/storage）进行无保护数据交互。\n"
            "2. 传输协议审计：跨信任边界的通信连线必须注明传输协议（如HTTPS, DoIP）及传输具体数据，禁止为空。\n"
            "\n"
            "请用极其简练、专业的中文安全评审语言直接指出问题（若有多个问题请分点，总字数控制在150字以内，不要使用 Markdown 格式，不要包含思考过程）。\n"
            "如果拓扑设计良好，未发现任何安全漏洞风险，请直接回复：【校验通过：当前拓扑未发现明显的边界安全漏洞。】"
        )
        llm_payload = {
            "model": settings.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"需要校验的数据流图拓扑为：\n{json.dumps({'nodes': nodes, 'edges': edges}, ensure_ascii=False)}"}
            ]
        }
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, headers=headers, json=llm_payload)
        if resp.status_code == 200:
            result = resp.json()["choices"][0]["message"]["content"]
            return f"【🛡️ 拓扑安全校验反馈 (Sub-Agent Check)】: {result.strip()}"
    except Exception as e:
        print(f"⚠️ Sub-Agent 校验失败，降级执行本地规则: {e}")
        
    return f"【🛡️ 拓扑安全校验反馈 (Local Rule-Gate)】: {local_warning}"

class ChatMessage(BaseModel):
    sender: str
    text: str

class AIChatReq(BaseModel):
    prompt: str
    history: Optional[List[ChatMessage]] = None

@router.post("/{id}/ai-chat")
def ai_chat_diagram(
    id: int,
    req_data: AIChatReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    AI 拓扑画图智能对话接口
    返回 AI 聊天回复及对应建议的拓扑 DFD JSON (不自动修改画布，需点击生成按钮)
    """
    diagram = db.query(Diagram).filter(Diagram.id == id).first()
    if not diagram:
        raise HTTPException(status_code=404, detail="功能图不存在")
        
    reasoning_steps = {}
    p = req_data.prompt.lower()
    
    # 默认降级规则数据
    if "诊断" in p or "diagnostic" in p:
        nodes = [
            {"id": "n1", "type": "entity", "position": {"x": 100, "y": 150}, "data": {"name": "OBD物理接口", "description": "物理OBD诊断接口，连接诊断仪", "protocol": "CAN", "remarks": "外部测试节点"}},
            {"id": "n2", "type": "process", "position": {"x": 300, "y": 150}, "data": {"name": "诊断网关ECU", "description": "车身网关，负责报文过滤路由", "protocol": "CAN-FD", "remarks": "核心控制单元"}},
            {"id": "n3", "type": "process", "position": {"x": 500, "y": 150}, "data": {"name": "诊断服务进程", "description": "UDS 协议栈与诊断任务分发", "protocol": "UDS", "remarks": "核心软件进程"}}
        ]
        edges = [
            {"id": "e1", "source": "n1", "target": "n2", "data": {"name": "UDS请求帧", "transmitted_info": "UDS服务请求"}},
            {"id": "e2", "source": "n2", "target": "n3", "data": {"name": "内部路由报文", "transmitted_info": "过滤校验后的UDS消息"}}
        ]
        reply = "我已经为您规划好了诊断拓扑。该拓扑包括三个节点：OBD物理接口（物理诊断物理接口，连接诊断仪）、诊断网关ECU（车身网关，负责报文过滤路由） and 诊断服务进程（UDS协议栈与诊断任务分发）。数据流包括外部诊断仪发来的‘UDS请求帧’以及网关路由到核心进程的‘内部路由报文’。您可以点击下方‘一键生成dfd图’按钮，将这个方案应用到您的画布上。"
    elif "ota" in p or "升级" in p or "update" in p:
        nodes = [
            {"id": "n1", "type": "entity", "position": {"x": 100, "y": 150}, "data": {"name": "OTA云端服务器", "description": "云端管理与包分发系统", "protocol": "HTTPS", "remarks": "云端节点"}},
            {"id": "n2", "type": "process", "position": {"x": 300, "y": 150}, "data": {"name": "车载T-BOX", "description": "车载无线连接终端", "protocol": "Ethernet", "remarks": "通信节点"}},
            {"id": "n3", "type": "process", "position": {"x": 500, "y": 150}, "data": {"name": "OTA管理器进程", "description": "负责固件校验和刷写管理", "protocol": "UDS", "remarks": "软件服务"}}
        ]
        edges = [
            {"id": "e1", "source": "n1", "target": "n2", "data": {"name": "固件下载包", "transmitted_info": "加密签名的固件镜像"}},
            {"id": "e2", "source": "n2", "target": "n3", "data": {"name": "固件分发流", "transmitted_info": "刷写镜像及UDS升级指令"}}
        ]
        reply = "我已经为您规划好了OTA固件升级拓扑。包含三个关键部分：OTA云端服务器（管理与分发固件包）、车载T-BOX（无线通信终端）、OTA管理器进程（执行固件校验与刷写）。数据流包括从云端到T-BOX的‘固件下载包’，以及T-BOX分发到核心处理单元的‘固件分发流’。您可以点击下方‘一键生成dfd图’应用此拓扑。"
    else:
        nodes = [
            {"id": "n1", "type": "entity", "position": {"x": 100, "y": 150}, "data": {"name": "传感器节点", "description": "数据采集物理传感器", "protocol": "LIN", "remarks": "基础传感器"}},
            {"id": "n2", "type": "process", "position": {"x": 300, "y": 150}, "data": {"name": "主控制器ECU", "description": "中央数据处理与指令解算", "protocol": "CAN", "remarks": "控制中枢"}},
            {"id": "n3", "type": "entity", "position": {"x": 500, "y": 150}, "data": {"name": "执行器部件", "description": "车辆物理执行机构", "protocol": "PWM", "remarks": "执行器"}}
        ]
        edges = [
            {"id": "e1", "source": "n1", "target": "n2", "data": {"name": "采样信号", "transmitted_info": "采集信号流"}},
            {"id": "e2", "source": "n2", "target": "n3", "data": {"name": "控制信号", "transmitted_info": "驱动脉宽信号"}}
        ]
        reply = f"我已经为您设计了一个基础的车载网络拓扑来响应“{req_data.prompt}”需求。它包含传感器节点、主控制器ECU and 执行器部件。数据流包括采集的‘采样信号’和发给执行器的‘控制信号’。您可以点击下方‘一键生成dfd图’应用到画布。"

    # 尝试加载当前已有的拓扑快照，以支持增量模式
    current_snapshot = {}
    has_existing_nodes = False
    if diagram.snapshot_json:
        try:
            current_snapshot = json.loads(diagram.snapshot_json)
            if current_snapshot.get("nodes"):
                has_existing_nodes = True
        except Exception:
            pass

    # 尝试调用 LLM (如果已配置)
    settings = db.query(SystemSettings).first()
    if settings and settings.api_key and settings.api_key != "mock_test_key":
        try:
            url = f"{settings.api_base_url.rstrip('/')}/chat/completions"
            headers = {
                "Authorization": f"Bearer {settings.api_key}",
                "Content-Type": "application/json"
            }
            system_prompt = (
                "你是一个车载网络安全拓扑设计助手。请针对用户的聊天 and 画图需求进行解答，并同时输出数据流图DFD设计。\n"
                "本系统将根据你设计的 DFD 拓扑自动提取安全资产，请仔细遵循以下映射关系来绘制并填写属性，以便自动提取完整正确的资产：\n"
                "- 节点类型 type='process' 代表【软件资产】；\n"
                "- 节点类型 type='entity' 代表【硬件资产】（如OBD接口、网关控制器、物理设备）；\n- 节点类型 type='interface' 代表【接口资产】（属于硬件资产，如串口、USB, JTAG等）；\n"
                "- 节点类型 type='storage' 代表【数据资产】（如数据库、本地配置文件、内存数据）；\n"
                "- 节点类型 type='boundary' 代表【物理安全边界】；\n"
                "- 连线 edges 代表【数据流/通信资产】。\n"
                "\n"
                "请务必在生成每个节点和连线时填好对应属性以供提取：\n"
                "- 节点的 data.name 必须是具体的资产名称，不要有符号干扰；\n"
                "- 节点的 data.description 填该节点详细的功能描述和安全作用，不要为空；\n"
                "- 节点的 data.protocol 填具体的通信协议或数据协议类型（如 CAN, LIN, Ethernet, HTTPS, FTP 等，尽量用规范大写字母，不要为空）；\n"
                "- 节点的 data.remarks 填相关的安全备注或资产标记；\n"
                "- 连线的 data.name 填具体数据流名称；\n"
                "- 连线的 data.protocol 填数据流传输采用的协议；\n"
                "- 连线的 data.transmitted_info 填传输的具体数据内容（如固件刷写包、诊断请求指令等，不要为空）。\n"
                "\n"
                "【🧠 结构化推理思维链 (Chain of Thought)】：\n"
                "为了提高输出拓扑的安全严谨性与合理性，你必须在输出的 `snapshot_json` 中包含一个 `reasoning_steps` 字段。在输出 `nodes` 和 `edges` 之前，在 `reasoning_steps` 里严格按照以下四个步骤按顺序进行分析：\n"
                "1. elements_analysis (元素分析): 分析并识别该场景中应包含哪些节点资产；\n"
                "2. relationships_analysis (关系分析): 分析这些节点应处于什么网段与物理边界关系中（如外部网络、车身网段、安全边界等）；\n"
                "3. data_flows_analysis (数据流分析): 梳理节点之间的数据交互方向与所采用的通信协议；\n"
                "4. properties_fill_analysis (属性填充分析): 说明如何为节点和数据流的 data 字段填充真实的属性名称、详细安全功能描述与通信内容备注。\n"
                "\n"
                "【⚡ 增量更新与位置保留规则 (Incremental Mode & Coordinates Preservation)】：\n"
                "如果提供了当前画布已有的 DFD 拓扑数据，请务必执行以下增量修改规则，切勿盲目推倒重建：\n"
                "1. **保留无关已有节点**：对于与用户最新需求或对话修改无关的已有节点 and 连线，必须原样保留在 JSON 中（包括其 id, type, data, style, position 属性），不可删除或重命名已有节点 ID。\n"
                "2. **保留用户摆放位置**：已有节点的 position (坐标 x, y) 必须完全保持原样，以便保留用户的画布手动设计结果。\n"
                "3. **增量放置新节点**：新生成的节点应当赋予唯一的 id（如已有 n1, n2，则新增 n3），其 position 坐标应放在已有节点附近（例如在最近的关联节点基础上 x 或 y 轴增加 150 到 200 像素以保持画布整洁）。\n"
                "4. **更新与删除**：如果用户要求修改或删除某个节点/数据流，请按需修改其属性（如修改 protocol）或从 nodes/edges 列表中移除。\n\n"
                "请以符合要求的 JSON 格式输出聊天回复及拓扑图 snapshot_json。不要包含任何非 JSON 文字或 markdown 标记。"
            )
            messages = [{"role": "system", "content": system_prompt}]
            
            # Context Budget: 只保留最近 6 条历史消息，防止 Token 膨胀和冗余干扰
            history_limit = 6
            filtered_history = []
            if req_data.history:
                for msg in req_data.history:
                    if "AI 拓扑助理" in msg.text or "一键生成 DFD 功能图" in msg.text:
                        continue
                    role = "user" if msg.sender == "user" else "assistant"
                    filtered_history.append({"role": role, "content": msg.text})
            
            if len(filtered_history) > history_limit:
                filtered_history = filtered_history[-history_limit:]
            
            messages.extend(filtered_history)
            
            user_content = f"我的画图需求是：{req_data.prompt}"
            if has_existing_nodes:
                user_content += f"\n\n当前画布已有的 DFD 拓扑结构为：\n{json.dumps(current_snapshot, ensure_ascii=False)}"
            
            messages.append({"role": "user", "content": user_content})
            
            llm_payload = {
                "model": settings.model_name,
                "messages": messages,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "DiagramChatOutput",
                        "strict": True,
                        "schema": DiagramChatOutput.model_json_schema()
                    }
                }
            }
            import httpx
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
                        "content": f"请务必按照以下 JSON Schema 格式回复，不要包含任何 markdown 或其他文本：\n{json.dumps(DiagramChatOutput.model_json_schema(), ensure_ascii=False)}"
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
                validated = DiagramChatOutput.model_validate(parsed)
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
                with httpx.Client(timeout=60.0) as client:
                    resp_rep = client.post(url, headers=headers, json=payload_repair)
                if resp_rep.status_code == 200:
                    choice_text_rep = resp_rep.json()["choices"][0]["message"]["content"]
                    if choice_text_rep.strip().startswith("```"):
                        choice_text_rep = re.sub(r"^```[a-zA-Z0-9]*\n", "", choice_text_rep)
                        choice_text_rep = re.sub(r"\n```$", "", choice_text_rep)
                        choice_text_rep = choice_text_rep.strip()
                    parsed_rep = json.loads(choice_text_rep)
                    validated = DiagramChatOutput.model_validate(parsed_rep)
                else:
                    raise val_err

            validated_dict = validated.model_dump(by_alias=True)
            if "reply" in validated_dict and "snapshot_json" in validated_dict:
                reply = validated_dict["reply"]
                snapshot_obj = validated_dict["snapshot_json"]
                nodes = snapshot_obj.get("nodes", nodes)
                edges = snapshot_obj.get("edges", edges)
                reasoning_steps = snapshot_obj.get("reasoning_steps", {})
                # 运行独立的子代理校验并附加到回复末尾 (Context Budgeting & Sub-Agents)
                compliance_feedback = verify_dfd_compliance(db, nodes, edges)
                reply += f"\n\n{compliance_feedback}"
        except Exception as e:
            print(f"⚠️ LLM 拓扑对话失败，降级执行规则算法: {e}")

    snapshot_payload = {"nodes": nodes, "edges": edges}
    if reasoning_steps:
        snapshot_payload["reasoning_steps"] = reasoning_steps
    snapshot_str = json.dumps(snapshot_payload, ensure_ascii=False)
    return {
        "reply": reply,
        "snapshot_json": snapshot_str
    }

