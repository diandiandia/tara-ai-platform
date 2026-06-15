from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
import redis
import json
from typing import List
from app.core.database import get_db
from app.api.auth import get_current_user
from app.models.user import User
from app.models.domain import Domain
from app.models.diagram import Diagram
from app.schemas.diagram import DiagramUpdate, DiagramOut
from app.api.project import check_domain_idle, check_project_active

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
            redis_client.setex(lock_key, 300, lock_val) # 设置 5 分钟过期 (300秒)
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
                "你是一个车载网络安全拓扑设计助手。请根据用户的需求，输出一个标准的 JSON，格式为：\n"
                "{\n"
                '  "nodes": [\n'
                '    {"id": "n1", "type": "entity|process|storage|boundary", "position": {"x": 100, "y": 150}, "data": {"name": "节点名", "description": "描述", "protocol": "协议", "remarks": "备注"}}\n'
                "  ],\n"
                '  "edges": [\n'
                '    {"id": "e1", "source": "n1", "target": "n2", "data": {"name": "连线名", "transmitted_info": "传输数据"}}\n'
                "  ]\n"
                "}\n"
                "不要包含任何非 JSON 文字或 markdown 标记。"
            )
            llm_payload = {
                "model": settings.model_name,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"请为以下功能绘制DFD画布结构：{req_data.prompt}"}
                ],
                "response_format": {"type": "json_object"}
            }
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(url, headers=headers, json=llm_payload)
            if resp.status_code == 200:
                choice_text = resp.json()["choices"][0]["message"]["content"]
                parsed = json.loads(choice_text)
                if "nodes" in parsed and "edges" in parsed:
                    nodes = parsed["nodes"]
                    edges = parsed["edges"]
        except Exception as e:
            print(f"⚠️ LLM 拓扑生成失败，降级执行规则算法: {e}")

    # 保存并更新 version_no 自增
    snapshot = {"nodes": nodes, "edges": edges}
    diagram.snapshot_json = json.dumps(snapshot, ensure_ascii=False)
    diagram.version_no += 1
    db.commit()
    db.refresh(diagram)
    
    return diagram

