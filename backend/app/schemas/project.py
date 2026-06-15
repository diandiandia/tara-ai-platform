from pydantic import BaseModel, Field, ConfigDict
from datetime import datetime
from typing import Optional, List

class ProjectCreate(BaseModel):
    name: str = Field(..., max_length=50, description="项目名称，最大50字符")
    description: Optional[str] = Field(None, max_length=200, description="项目描述，最大200字符")

class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=50)
    description: Optional[str] = Field(None, max_length=200)

class ProjectOut(BaseModel):
    id: int
    name: str
    description: Optional[str]
    status: str
    is_archived: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
