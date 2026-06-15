from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import Optional, List, Dict, Any

class TaraRunOut(BaseModel):
    id: int
    domain_id: int
    celery_task_id: Optional[str]
    status: str
    progress: int
    started_at: datetime
    completed_at: Optional[datetime]

    model_config = ConfigDict(from_attributes=True)

class TaraStepOut(BaseModel):
    id: int
    run_id: int
    asset_id: int
    stage: str
    status: str
    input_hash: str
    analysis_result: Dict[str, Any]
    fail_reason: Optional[str]
    retry_count: int
    started_at: datetime
    completed_at: datetime

    model_config = ConfigDict(from_attributes=True)

class StepUpdateReq(BaseModel):
    final_output: Dict[str, Any]
    modification_reason: str

class ManualStepInput(BaseModel):
    asset_id: int
    stage: str
    output: Dict[str, Any]

class ManualOfflineInputReq(BaseModel):
    steps: List[ManualStepInput]
