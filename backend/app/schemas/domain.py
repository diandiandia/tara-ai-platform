from pydantic import BaseModel, Field, ConfigDict
from datetime import datetime
from typing import Optional

class DomainCreate(BaseModel):
    name: str = Field(..., max_length=50, description="子域控名称")

class DomainUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=50)

class DomainOut(BaseModel):
    id: int
    project_id: int
    name: str
    status: str
    progress: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
