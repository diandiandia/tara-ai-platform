import os
from datetime import datetime, timedelta, timezone
from typing import Union, Any
from jose import jwt
from bcrypt import hashpw, gensalt, checkpw

# 安全密钥配置
SECRET_KEY = "tara-platform-secret-key-super-secure-change-in-prod"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 120 # 2小时 (与系统设计一致)

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    验证纯文本密码与加密哈希密码是否一致
    """
    try:
        return checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))
    except Exception:
        return False

def get_password_hash(password: str) -> str:
    """
    对纯文本密码进行 bcrypt 加密哈希
    """
    pwd_bytes = password.encode('utf-8')
    return hashpw(pwd_bytes, gensalt()).decode('utf-8')

def create_access_token(subject: Union[str, Any], expires_delta: timedelta = None) -> str:
    """
    生成 JWT 访问 Token
    """
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode = {"exp": expire, "sub": str(subject)}
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def decode_access_token(token: str) -> Union[str, None]:
    """
    解码并验证 JWT Token，返回主体 username
    """
    try:
        decoded_token = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return decoded_token["sub"]
    except Exception:
        return None
