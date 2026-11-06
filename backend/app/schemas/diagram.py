from pydantic import BaseModel, ConfigDict
from datetime import datetime

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

class DiagramMeta(BaseModel):
    """
    轻量画布信息，不包含 snapshot_json。
    列表页与版本校验只需要这些字段，避免把每张图的完整拓扑都下发一遍。
    """
    id: int
    domain_id: int
    title: str
    version_no: int
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)