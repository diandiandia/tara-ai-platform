from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import Optional

class DiagramUpdate(BaseModel):
    version_no: int
    snapshot_json: str

class DiagramOut(BaseModel):
    id: int
    domain_id: int
    title: str
    version_no: int
    snapshot_json: str
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
