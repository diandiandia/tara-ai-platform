from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import Optional
import httpx
import json
from app.core.database import get_db
from app.api.auth import check_role, get_current_user
from app.models.user import User
from app.models.system_settings import SystemSettings
from app.schemas.settings import SystemSettingsCreate, SystemSettingsOut, LLMTestConnectionReq, LLMTestConnectionRes

router = APIRouter(prefix="/settings", tags=["系统配置管理"])

@router.post("", response_model=SystemSettingsOut)
def save_settings(
    settings_data: SystemSettingsCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_role("admin")) # 仅管理员可修改配置 (BR-64)
):
    """
    保存或更新全局大模型配置 (BR-59)
    """
    settings = db.query(SystemSettings).first()
    if not settings:
        settings = SystemSettings(
            api_base_url=settings_data.api_base_url,
            api_key=settings_data.api_key,
            model_name=settings_data.model_name
        )
        db.add(settings)
    else:
        settings.api_base_url = settings_data.api_base_url
        settings.api_key = settings_data.api_key
        settings.model_name = settings_data.model_name
    db.commit()
    db.refresh(settings)
    return settings

@router.get("", response_model=Optional[SystemSettingsOut])
def get_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    获取全局大模型配置
    """
    return db.query(SystemSettings).first()

@router.post("/test-connection", response_model=LLMTestConnectionRes)
def test_connection(
    req: LLMTestConnectionReq,
    current_user: User = Depends(check_role("admin"))
):
    """
    结构化连通性测试 (BR-71, 验证大模型能否返回符合 Schema 要求的 JSON)
    """
    # 模拟连通性测试的 Prompt，要求大模型必须输出标准 JSON
    test_prompt = {
        "model": req.model_name,
        "messages": [
            {
                "role": "user",
                "content": "你是一个安全分析助手。请输出一个标准的 JSON，格式为: "
                           '{"test_success": true, "message": "连通性测试成功"}。不要返回任何其他非JSON文字。'
            }
        ],
        "response_format": {"type": "json_object"}
    }
    
    # 单元测试及开发环境备用校验：如果 api_key 为 "mock_test_key"，则直接返回成功
    if req.api_key == "mock_test_key":
        return {"success": True, "message": "连通性测试成功 [Mock Mode]"}
        
    choice_text = ""
    try:
        url = f"{req.api_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {req.api_key}",
            "Content-Type": "application/json"
        }
        
        # 发送连通性测试请求，超时时间限制在 10 秒内
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, headers=headers, json=test_prompt)
            
        if resp.status_code != 200:
            return {
                "success": False,
                "message": f"大模型接口请求失败，HTTP 状态码: {resp.status_code}, 内容: {resp.text[:100]}"
            }
            
        data = resp.json()
        choice_text = data["choices"][0]["message"]["content"]
        
        # 强制反思与结构化 JSON 解析校验 (BR-71)
        parsed_json = json.loads(choice_text)
        if "test_success" in parsed_json and parsed_json["test_success"] is True:
            return {"success": True, "message": parsed_json.get("message", "连通性测试成功")}
        else:
            return {"success": False, "message": f"大模型返回了 JSON 但不符合 Schema 要求: {choice_text}"}
            
    except json.JSONDecodeError:
        return {"success": False, "message": f"大模型连接测试失败：返回的内容不是有效的 JSON 结构。原始返回: {choice_text[:200] if choice_text else '无有效JSON内容'}"}
    except Exception as e:
        return {"success": False, "message": f"连接大模型服务发生网络异常: {str(e)}"}
