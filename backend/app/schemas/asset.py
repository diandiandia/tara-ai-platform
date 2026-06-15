from pydantic import BaseModel, Field, ConfigDict
from datetime import datetime
from typing import Optional, List

class AssetCreate(BaseModel):
    name: str = Field(..., max_length=100)
    asset_type: str = Field(..., max_length=50) # data, software, hardware, communication
    protocol: Optional[str] = Field(None, max_length=50)
    description: Optional[str] = Field(None)

class AssetUpdate(BaseModel):
    name: Optional[str] = Field(None, max_length=100)
    asset_type: Optional[str] = Field(None, max_length=50)
    protocol: Optional[str] = Field(None, max_length=50)
    description: Optional[str] = Field(None)
    status: Optional[str] = Field(None) # draft, confirmed, rejected

class AssetOut(BaseModel):
    id: int
    domain_id: int
    diagram_id: Optional[int]
    name: str
    asset_type: str
    protocol: Optional[str]
    description: Optional[str]
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class DeduplicateSuggestionItem(BaseModel):
    keep_asset_id: int
    remove_asset_ids: List[int]
    reason: str

class DeduplicateConfirmReq(BaseModel):
    suggestions: List[DeduplicateSuggestionItem]
