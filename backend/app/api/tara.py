from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from datetime import datetime
from typing import List, Optional, Dict, Any

from app.core.database import get_db
from app.api.auth import get_current_user
from app.models.user import User
from app.models.domain import Domain
from app.models.asset import Asset
from app.models.tara_run import TaraRun
from app.models.tara_step import TaraStep
from app.schemas.tara import TaraRunOut, TaraStepOut, StepUpdateReq, ManualOfflineInputReq
from app.api.project import check_domain_idle, check_project_active, recalculate_project_status
from app.core.celery_app import celery_app
from app.worker.tasks import run_tara_analysis

router = APIRouter(tags=["TARA 核心分析分析引擎"])

@router.post("/domains/{domain_id}/tara-runs", response_model=TaraRunOut)
def start_tara_analysis(
    domain_id: int,
    force: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    启动子域控 TARA 评估跑批流程 (BR-36, BR-37)
    """
    domain = db.query(Domain).filter(Domain.id == domain_id).first()
    if not domain:
        raise HTTPException(status_code=404, detail="子域控不存在")
        
    # 级联项目只读锁检查
    check_project_active(domain.project_id, db)
    
    # 校验 TARA 运行保护：如果已经在运行中，不允许重复启动
    if domain.status == "running":
        raise HTTPException(status_code=400, detail="该域控当前正在进行分析中，请勿重复触发")
        
    # 校验启动约束 (BR-36, BR-37)：必须包含至少 1 个已确认的资产
    confirmed_assets_count = db.query(Asset).filter(
        Asset.domain_id == domain_id,
        Asset.status == "confirmed"
    ).count()
    
    if confirmed_assets_count == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="当前子域控下无已确认的资产，请先核对确认至少1个资产才能开始 TARA 分析。"
        )
        
    # 创建 TARA 运行记录
    run_record = TaraRun(
        domain_id=domain_id,
        status="running",
        progress=0,
        started_at=datetime.now()
    )
    db.add(run_record)
    db.commit()
    db.refresh(run_record)
    
    # 将域控状态修改为 running 并锁定
    domain.status = "running"
    domain.progress = 0
    db.commit()
    
    # 级联更新项目状态 (BR-03)
    recalculate_project_status(domain.project_id, db)
    
    # 调用 Celery 派发异步分析任务
    try:
        task = run_tara_analysis.delay(domain_id, run_record.id, force=force)
        run_record.celery_task_id = task.id
        db.commit()
    except Exception as e:
        # 降级：如果 Celery 服务连接失败，启动本进程同步跑批（测试及纯沙盒开发支持）
        print(f"⚠️ Celery 异步任务派发失败，降级执行同步跑批: {e}")
        # 在独立的本地进程中直接同步调用任务方法
        run_tara_analysis(domain_id, run_record.id, force=force)
        db.refresh(run_record)
        
    return run_record

@router.post("/domains/{domain_id}/cancel-run")
def cancel_tara_analysis(
    domain_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    强行中止后台 TARA 分析 (BR-70, 取消运行按钮)
    """
    domain = db.query(Domain).filter(Domain.id == domain_id).first()
    if not domain:
        raise HTTPException(status_code=404, detail="子域控不存在")
        
    check_project_active(domain.project_id, db)
    
    # 查找最新的 running 分析任务
    running_run = db.query(TaraRun).filter(
        TaraRun.domain_id == domain_id,
        TaraRun.status == "running"
    ).order_by(TaraRun.id.desc()).first()
    
    if not running_run:
        raise HTTPException(status_code=400, detail="当前子域控未处于分析运行中状态")
        
    # 标记运行状态为 cancelled
    running_run.status = "cancelled"
    running_run.completed_at = datetime.now()
    
    # 重置域控状态和进度
    domain.status = "not_started"
    domain.progress = 0
    db.commit()
    
    # 级联更新项目状态 (BR-03)
    recalculate_project_status(domain.project_id, db)
    
    # 调用 Celery Revoke 终止任务
    if running_run.celery_task_id:
        try:
            celery_app.control.revoke(running_run.celery_task_id, terminate=True, signal="SIGTERM")
            print(f"Celery 任务 {running_run.celery_task_id} 已被强行中止。")
        except Exception as e:
            print(f"⚠️ 无法撤销 Celery 任务: {e}")
            
    return {"message": "TARA 分析任务已强行取消，状态及进度已重置。"}

@router.get("/domains/{domain_id}/tara-runs/progress", response_model=TaraRunOut)
def get_tara_progress(
    domain_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    查询域控最新的分析进度
    """
    run = db.query(TaraRun).filter(TaraRun.domain_id == domain_id).order_by(TaraRun.id.desc()).first()
    if not run:
        raise HTTPException(status_code=404, detail="该域控暂无 TARA 运行分析记录")
    return run

@router.get("/domains/{domain_id}/tara-results", response_model=List[TaraStepOut])
def get_tara_results(
    domain_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    获取域控最近一次成功完成分析的步骤结论详情
    """
    # 查找最新一次分析的 completed/failed 任务
    last_run = db.query(TaraRun).filter(
        TaraRun.domain_id == domain_id,
        TaraRun.status.in_(["completed", "failed", "cancelled"])
    ).order_by(TaraRun.id.desc()).first()
    
    if not last_run:
        # 如果当前在运行中，直接读取当前运行
        last_run = db.query(TaraRun).filter(TaraRun.domain_id == domain_id).order_by(TaraRun.id.desc()).first()
        
    if not last_run:
        return []
        
    return db.query(TaraStep).filter(TaraStep.run_id == last_run.id).all()

@router.put("/tara-steps/{step_id}", response_model=TaraStepOut)
def update_tara_step(
    step_id: int,
    req_data: StepUpdateReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    人工审阅修改 TARA 分析结果 (BR-51, 人工修改标记与理由)
    """
    step = db.query(TaraStep).filter(TaraStep.id == step_id).first()
    if not step:
        raise HTTPException(status_code=404, detail="分析步骤记录不存在")
        
    # 获取所属域控，检查只读锁
    run = db.query(TaraRun).filter(TaraRun.id == step.run_id).first()
    check_domain_idle(run.domain_id, db)
    
    # 更改 analysis_result 中的 final_output 和人工标记
    result_copy = dict(step.analysis_result)
    result_copy["is_human_modified"] = True
    result_copy["modification_reason"] = req_data.modification_reason
    result_copy["final_output"] = req_data.final_output
    
    # 如果修改的是 阶段④ (风险处理决策)，并且将决策修改为了 accept 或 transfer
    # 我们需要在接下来的 阶段⑤ (CSR生成) 自动应用风险免除联动逻辑 (BR-69)
    if step.stage == "stage4":
        new_decision = req_data.final_output.get("risk_decision")
        if new_decision in ["accept", "transfer"]:
            # 找到同一个 run 中，针对该 asset 的 阶段⑤ 记录并自动更改其结论
            stage5_step = db.query(TaraStep).filter(
                TaraStep.run_id == step.run_id,
                TaraStep.asset_id == step.asset_id,
                TaraStep.stage == "stage5"
            ).first()
            if stage5_step:
                s5_res = dict(stage5_step.analysis_result)
                s5_res["final_output"] = {
                    "cso": "无需制定安全目标 (已在阶段④免除)",
                    "csr": [],
                    "exempted": True,
                    "reason": f"风险处理决策为 '{new_decision}'，此威胁场景免除 CSR 生成。"
                }
                stage5_step.analysis_result = s5_res
    
    step.analysis_result = result_copy
    db.commit()
    db.refresh(step)
    return step

@router.post("/domains/{domain_id}/manual-update")
def manual_offline_update(
    domain_id: int,
    req_data: ManualOfflineInputReq,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    故障备用模式：直接手工填写表单入库 (BR-70, 降级脱网模式)
    """
    domain = check_domain_idle(domain_id, db)
    
    # 创建 TARA 运行记录，状态设为 completed
    run_record = TaraRun(
        domain_id=domain_id,
        status="completed",
        progress=100,
        started_at=datetime.now(),
        completed_at=datetime.now()
    )
    db.add(run_record)
    db.commit()
    db.refresh(run_record)
    
    # 写入手工传入的各个阶段结论
    for item in req_data.steps:
        # 手工录入的数据，分析结果标记为人工修改
        result = {
            "ai_output": {},
            "is_human_modified": True,
            "modification_reason": "手动故障备用入库",
            "final_output": item.output
        }
        
        tara_step = TaraStep(
            run_id=run_record.id,
            asset_id=item.asset_id,
            stage=item.stage,
            status="completed",
            input_hash="manual", # 标记为手工输入
            analysis_result=result
        )
        db.add(tara_step)
        
    # 更新域控状态
    domain.status = "completed"
    domain.progress = 100
    db.commit()
    
    # 级联更新项目状态 (BR-03)
    recalculate_project_status(domain.project_id, db)
    
    return {"message": "手工备用数据导入成功", "run_id": run_record.id}
