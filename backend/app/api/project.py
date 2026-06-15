from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List, Optional
from app.core.database import get_db
from app.api.auth import get_current_user, check_role
from app.models.user import User
from app.models.project import Project
from app.models.domain import Domain
from app.schemas.project import ProjectCreate, ProjectUpdate, ProjectOut
from app.schemas.domain import DomainCreate, DomainUpdate, DomainOut

router = APIRouter(prefix="/projects", tags=["项目与子域控管理"])

# ----------------- 帮助校验器 (Helper Validators) -----------------

def check_project_active(project_id: int, db: Session):
    """
    BR-78: 归档冷冻校验。如果是 is_archived == 1，所有写操作强行只读，抛出 403 错误。
    """
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    if project.is_archived == 1:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="该项目已完成并归档锁定，当前状态为只读。"
        )
    return project

def check_domain_idle(domain_id: int, db: Session):
    """
    BR-10: 运行期只读锁。当子域控在“分析中” (running) 时，禁止执行任何修改操作。
    """
    domain = db.query(Domain).filter(Domain.id == domain_id).first()
    if not domain:
        raise HTTPException(status_code=404, detail="子域控不存在")
    
    # 级联校验项目是否已归档
    check_project_active(domain.project_id, db)
    
    if domain.status == "running":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="当前子域控正在进行 TARA 分析，资产与画布已被锁定。"
        )
    return domain

def recalculate_project_status(project_id: int, db: Session):
    """
    BR-03: 项目状态推导。有子域控在“分析中”时项目为进行中；所有子域控分析均为已完成时项目为已完成；其余情况为草稿。
    """
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        return
        
    domains = db.query(Domain).filter(Domain.project_id == project_id).all()
    if not domains:
        # 如果没有子域控，默认为草稿状态
        project.status = "draft"
        db.commit()
        return
        
    statuses = [d.status for d in domains]
    
    # 状态推导逻辑
    if "running" in statuses:
        new_status = "in_progress"
    elif all(s == "completed" for s in statuses):
        new_status = "completed"
    else:
        new_status = "draft"
        
    if project.status != new_status:
        project.status = new_status
        db.commit()

# ----------------- 项目 CRUD API -----------------

@router.post("", response_model=ProjectOut)
def create_project(
    project_data: ProjectCreate, 
    db: Session = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    """
    创建项目
    """
    project = Project(
        name=project_data.name,
        description=project_data.description,
        status="draft"
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    return project

@router.get("", response_model=List[ProjectOut])
def list_projects(
    q: Optional[str] = None, 
    db: Session = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    """
    模糊查询及项目列表 (BR-页面 1)
    """
    query = db.query(Project)
    if q:
        query = query.filter(
            (Project.name.like(f"%{q}%")) | (Project.description.like(f"%{q}%"))
        )
    return query.all()

@router.get("/{id}", response_model=ProjectOut)
def get_project(
    id: int, 
    db: Session = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    """
    获取项目详情
    """
    recalculate_project_status(id, db)
    project = db.query(Project).filter(Project.id == id).first()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project

@router.put("/{id}", response_model=ProjectOut)
def update_project(
    id: int, 
    project_data: ProjectUpdate, 
    db: Session = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    """
    修改项目（检查归档锁）
    """
    project = check_project_active(id, db)
    if project_data.name is not None:
        project.name = project_data.name
    if project_data.description is not None:
        project.description = project_data.description
    db.commit()
    recalculate_project_status(id, db)
    db.refresh(project)
    return project

@router.delete("/{id}")
def delete_project(
    id: int, 
    db: Session = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    """
    删除项目 (级联删除下属域控、功能图等，BR-04/09，检查归档锁)
    """
    project = check_project_active(id, db)
    db.delete(project)
    db.commit()
    return {"message": f"项目 {id} 删除成功"}

@router.post("/{id}/archive", response_model=ProjectOut)
def archive_project(
    id: int, 
    db: Session = Depends(get_db), 
    current_user: User = Depends(check_role("admin")) # 仅管理员有归档/解锁权限 (BR-78)
):
    """
    管理员：手动将项目归档冷冻
    """
    project = db.query(Project).filter(Project.id == id).first()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    project.status = "completed"
    project.is_archived = 1
    db.commit()
    db.refresh(project)
    return project

@router.post("/{id}/unarchive", response_model=ProjectOut)
def unarchive_project(
    id: int, 
    db: Session = Depends(get_db), 
    current_user: User = Depends(check_role("admin")) # 仅管理员有归档/解锁权限 (BR-78)
):
    """
    管理员：手动解除项目归档冷冻，变更为进行中
    """
    project = db.query(Project).filter(Project.id == id).first()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    project.status = "in_progress"
    project.is_archived = 0
    db.commit()
    db.refresh(project)
    return project

# ----------------- 子域控 CRUD API -----------------

@router.post("/{project_id}/domains", response_model=DomainOut)
def create_domain(
    project_id: int,
    domain_data: DomainCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    新建域控（检查项目归档锁，创建后自动推导项目状态）
    """
    check_project_active(project_id, db)
    
    domain = Domain(
        project_id=project_id,
        name=domain_data.name,
        status="not_started",
        progress=0
    )
    db.add(domain)
    db.commit()
    db.refresh(domain)
    
    recalculate_project_status(project_id, db)
    return domain

@router.get("/{project_id}/domains", response_model=List[DomainOut])
def list_domains(
    project_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    获取指定项目下的所有子域控列表
    """
    # 检查项目是否存在
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    return db.query(Domain).filter(Domain.project_id == project_id).all()

@router.put("/domains/{domain_id}", response_model=DomainOut)
def update_domain(
    domain_id: int,
    domain_data: DomainUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    修改域控名称（检查域控运行锁和归档锁，修改后自动推导项目状态）
    """
    domain = check_domain_idle(domain_id, db)
    if domain_data.name is not None:
        domain.name = domain_data.name
    db.commit()
    db.refresh(domain)
    
    recalculate_project_status(domain.project_id, db)
    return domain

@router.delete("/domains/{domain_id}")
def delete_domain(
    domain_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    删除子域控（检查域控运行锁和归档锁，删除后级联删除资产、功能图，并重新推导项目状态）
    """
    domain = check_domain_idle(domain_id, db)
    project_id = domain.project_id
    db.delete(domain)
    db.commit()
    
    recalculate_project_status(project_id, db)
    return {"message": f"域控 {domain_id} 删除成功"}
