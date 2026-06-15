from pydantic import BaseModel, Field, ConfigDict
from typing import Optional

class SystemSettingsCreate(BaseModel):
    api_base_url: str = Field(..., max_length=255)
    api_key: str = Field(..., max_length=255)
    model_name: str = Field(..., max_length=128)

class SystemSettingsOut(BaseModel):
    id: int
    api_base_url: str
    api_key: str
    model_name: str

    model_config = ConfigDict(from_attributes=True)

class LLMTestConnectionReq(BaseModel):
    api_base_url: str
    api_key: str
    model_name: str

class LLMTestConnectionRes(BaseModel):
    success: bool
    message: str
