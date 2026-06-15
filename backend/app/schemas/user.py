from pydantic import BaseModel, ConfigDict
from datetime import datetime
from typing import Optional

class UserLogin(BaseModel):
    username: str
    password: str

class Token(BaseModel):
    access_token: str
    token_type: str

class UserOut(BaseModel):
    id: int
    username: str
    role: str
    must_change_password: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

class UserCreate(BaseModel):
    username: str
    password: str
    role: str = "analyst"

class PasswordChange(BaseModel):
    new_password: str

class PasswordReset(BaseModel):
    new_password: str
