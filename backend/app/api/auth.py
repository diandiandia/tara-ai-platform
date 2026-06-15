from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
from typing import List
from app.core.database import get_db
from app.core.security import verify_password, create_access_token, decode_access_token, get_password_hash
from app.models.user import User
from app.schemas.user import UserLogin, Token, UserOut, UserCreate, PasswordChange, PasswordReset

router = APIRouter(prefix="/auth", tags=["认证管理"])

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login-oauth2")

def get_current_user(
    request: Request,
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db)
) -> User:
    """
    依赖注入：获取当前登录用户
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="登录凭证已失效或无效",
        headers={"WWW-Authenticate": "Bearer"},
    )
    username = decode_access_token(token)
    if username is None:
        raise credentials_exception
    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise credentials_exception
        
    # 管理员权限限制：禁止访问除了认证和配置之外的所有业务 API (BR-02)
    if user.role == "admin":
        path = request.url.path
        is_allowed = (
            path.startswith("/api/auth") or 
            path.startswith("/api/settings") or 
            (path.startswith("/api/projects/") and (path.endswith("/archive") or path.endswith("/unarchive")))
        )
        if not is_allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="管理员账户没有操作项目业务数据的权限"
            )
            
    return user

def check_role(required_role: str):
    """
    依赖注入工厂：判断用户是否拥有相应角色权限
    """
    def dependency(user: User = Depends(get_current_user)):
        if user.role != required_role and user.role != "admin": # admin 具有所有权限
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"权限不足，需要角色: {required_role}"
            )
        return user
    return dependency

def require_analyst(user: User = Depends(get_current_user)):
    """
    依赖注入：仅普通分析员可进行此业务操作，管理员仅有配置及账户管理权限
    """
    if user.role == "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="管理员无权操作项目业务数据"
        )
    return user

@router.post("/login", response_model=Token)
def login(login_data: UserLogin, db: Session = Depends(get_db)):
    """
    用户登录
    """
    user = db.query(User).filter(User.username == login_data.username).first()
    if not user or not verify_password(login_data.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名或密码错误"
        )
    access_token = create_access_token(subject=user.username)
    return {"access_token": access_token, "token_type": "bearer"}

from fastapi.security import OAuth2PasswordRequestForm
@router.post("/login-oauth2", response_model=Token)
def login_oauth2(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """
    为 Swagger UI 提供的 OAuth2 兼容登录接口
    """
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名或密码错误"
        )
    access_token = create_access_token(subject=user.username)
    return {"access_token": access_token, "token_type": "bearer"}

@router.get("/me", response_model=UserOut)
def read_users_me(current_user: User = Depends(get_current_user)):
    """
    获取当前登录用户信息
    """
    return current_user


# ----------------- 用户管理 API (仅限管理员 admin) -----------------

@router.get("/users", response_model=List[UserOut])
def list_users(
    db: Session = Depends(get_db),
    current_user: User = Depends(check_role("admin"))
):
    """
    管理员：获取所有用户列表
    """
    return db.query(User).all()

@router.post("/users", response_model=UserOut)
def create_user(
    user_data: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_role("admin"))
):
    """
    管理员：创建新用户
    """
    # 检查用户名是否冲突
    existing = db.query(User).filter(User.username == user_data.username).first()
    if existing:
        raise HTTPException(status_code=400, detail="用户名已存在")
    
    hashed = get_password_hash(user_data.password)
    new_user = User(
        username=user_data.username,
        password_hash=hashed,
        role=user_data.role,
        must_change_password=1 if user_data.role == "admin" else 0  # 新管理员登录必须改密码
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user

@router.delete("/users/{user_id}")
def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_role("admin"))
):
    """
    管理员：删除用户
    """
    if current_user.id == user_id:
        raise HTTPException(status_code=400, detail="不能删除自身账号")
    
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
        
    db.delete(user)
    db.commit()
    return {"message": f"用户 {user.username} 已删除"}

@router.post("/users/{user_id}/reset-password")
def reset_user_password(
    user_id: int,
    reset_data: PasswordReset,
    db: Session = Depends(get_db),
    current_user: User = Depends(check_role("admin"))
):
    """
    管理员：重置指定用户的密码
    """
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
        
    hashed = get_password_hash(reset_data.new_password)
    user.password_hash = hashed
    # 如果被重置密码的用户是管理员，要求其重新登录后修改密码
    if user.role == "admin":
        user.must_change_password = 1
    db.commit()
    return {"message": f"用户 {user.username} 的密码重置成功"}

@router.post("/change-password")
def change_my_password(
    change_data: PasswordChange,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    所有用户：修改自己的密码（主要用于管理员首次登录强制修改密码）
    """
    hashed = get_password_hash(change_data.new_password)
    current_user.password_hash = hashed
    current_user.must_change_password = 0 # 标记为已修改密码
    db.commit()
    return {"message": "密码修改成功"}
