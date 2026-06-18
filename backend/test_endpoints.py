import os
import sys
import unittest
from fastapi.testclient import TestClient

# 将 backend 路径加入 sys.path
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

# 强制测试使用独立的 SQLite 文件，防止清空/破坏生产数据库 (tara.db)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(PROJECT_ROOT, 'tara_test.db')}"

from app.main import app
from app.core.database import SessionLocal, Base, engine
from app.models.user import User
from app.models.project import Project
from app.models.domain import Domain
from app.models.diagram import Diagram
from app.models.asset import Asset
from app.models.tara_run import TaraRun
from app.models.tara_step import TaraStep
from app.core.security import get_password_hash

class TestTaraAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 配置 Celery 单元测试同步执行 (task_always_eager)
        from app.core.celery_app import celery_app
        celery_app.conf.task_always_eager = True

        # 1. 确保测试前初始化数据库，清空表以保证测试用例独立运行
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        
        # 2. 准备测试数据 (创建测试用户)
        cls.db = SessionLocal()
        cls.test_username = "test_analyst"
        cls.test_password = "AnalystPassword"
        
        # 创建普通分析员
        cls.analyst = cls.db.query(User).filter(User.username == cls.test_username).first()
        if not cls.analyst:
            cls.analyst = User(
                username=cls.test_username,
                password_hash=get_password_hash(cls.test_password),
                role="analyst"
            )
            cls.db.add(cls.analyst)
            
        # 创建管理员
        cls.admin_username = "test_admin"
        cls.admin_password = "AdminPassword"
        cls.admin = cls.db.query(User).filter(User.username == cls.admin_username).first()
        if not cls.admin:
            cls.admin = User(
                username=cls.admin_username,
                password_hash=get_password_hash(cls.admin_password),
                role="admin"
            )
            cls.db.add(cls.admin)
            
        cls.db.commit()
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def get_auth_headers(self, username, password):
        """
        辅助函数：获取 JWT Token 报头
        """
        response = self.client.post("/api/auth/login", json={
            "username": username,
            "password": password
        })
        self.assertEqual(response.status_code, 200)
        token = response.json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    def test_01_authentication(self):
        """
        测试 JWT 认证与登录限制 (BR-64)
        """
        # 测试错误密码登录
        response = self.client.post("/api/auth/login", json={
            "username": self.test_username,
            "password": "wrong_password"
        })
        self.assertEqual(response.status_code, 400)
        
        # 测试正确密码登录
        response = self.client.post("/api/auth/login", json={
            "username": self.test_username,
            "password": self.test_password
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("access_token", response.json())
        
        # 测试越权访问敏感接口 (非 admin 归档项目应该返回 403)
        headers = self.get_auth_headers(self.test_username, self.test_password)
        response = self.client.post("/api/projects/1/archive", headers=headers)
        self.assertEqual(response.status_code, 403) # analyst 应该是 403 Forbidden

    def test_02_project_crud_and_validation(self):
        """
        测试项目与域控创建及表单限制
        """
        headers = self.get_auth_headers(self.test_username, self.test_password)
        
        # 1. 验证名称字数超限 (BR-页面1 限制：name上限50)
        long_name = "a" * 51
        response = self.client.post("/api/projects", headers=headers, json={
            "name": long_name,
            "description": "正常描述"
        })
        self.assertEqual(response.status_code, 422) # Pydantic 参数校验失败
        
        # 2. 正常创建项目
        response = self.client.post("/api/projects", headers=headers, json={
            "name": "车载安全控制系统项目",
            "description": "汽车子域控 TARA 安全属性评估及威胁分析项目"
        })
        self.assertEqual(response.status_code, 200)
        project = response.json()
        self.assertEqual(project["status"], "draft") # 默认草稿
        project_id = project["id"]
        
        # 3. 正常创建子域控
        response = self.client.post(f"/api/projects/{project_id}/domains", headers=headers, json={
            "name": "IVI域控"
        })
        self.assertEqual(response.status_code, 200)
        domain = response.json()
        self.assertEqual(domain["status"], "not_started")
        
        # 4. 创建第二个子域控 ADCU
        response = self.client.post(f"/api/projects/{project_id}/domains", headers=headers, json={
            "name": "ADCU域控"
        })
        self.assertEqual(response.status_code, 200)
        adcu_domain = response.json()
        
        # 5. 校验项目状态推导 (BR-03)
        # 两个子域控状态均为 not_started，项目状态应推导为 draft
        response = self.client.get(f"/api/projects/{project_id}", headers=headers)
        self.assertEqual(response.json()["status"], "draft")
        
        # 6. 模拟一个域控分析运行中，项目状态变更为 in_progress
        db_session = SessionLocal()
        domain_db = db_session.query(Domain).filter(Domain.id == adcu_domain["id"]).first()
        domain_db.status = "running"
        db_session.commit()
        db_session.close()
        
        # 再次触发项目状态推导，检查是否推导为 in_progress
        response = self.client.get(f"/api/projects/{project_id}", headers=headers)
        # 先手动调用一下接口触发状态更新
        response_update = self.client.put(f"/api/projects/{project_id}", headers=headers, json={"name": "车载安全控制系统项目"})
        self.assertEqual(response_update.json()["status"], "in_progress")

    def test_03_domain_running_lock(self):
        """
        测试子域控处于“分析中”（running）时的修改锁定逻辑 (BR-10)
        """
        headers = self.get_auth_headers(self.test_username, self.test_password)
        
        # 1. 查找刚才设置为 running 的 ADCU 域控
        db_session = SessionLocal()
        domain = db_session.query(Domain).filter(Domain.status == "running").first()
        self.assertIsNotNone(domain)
        domain_id = domain.id
        db_session.close()
        
        # 2. 尝试修改处于 running 的域控名称，应该被拦截 (返回 400)
        response = self.client.put(f"/api/projects/domains/{domain_id}", headers=headers, json={
            "name": "修改后的ADCU"
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("已被锁定", response.json()["detail"])

    def test_03_b_diagram_and_locking(self):
        """
        测试功能图的乐观锁 (BR-16) 与 Redis 心跳锁 (BR-72)
        """
        headers = self.get_auth_headers(self.test_username, self.test_password)
        import json
        
        # 1. 查找 IVI 域控
        db_session = SessionLocal()
        domain = db_session.query(Domain).filter(Domain.name == "IVI域控").first()
        self.assertIsNotNone(domain)
        domain_id = domain.id
        db_session.close()
        
        # 2. 创建功能图
        response = self.client.post(f"/api/diagrams?domain_id={domain_id}&title=远程诊断功能图", headers=headers)
        self.assertEqual(response.status_code, 200)
        diag = response.json()
        diag_id = diag["id"]
        self.assertEqual(diag["version_no"], 1)
        
        # 3. 抢占编辑锁
        lock_resp = self.client.post(f"/api/diagrams/{diag_id}/lock", headers=headers)
        self.assertEqual(lock_resp.status_code, 200)
        self.assertEqual(lock_resp.json()["locked_by"], self.test_username)
        
        # 4. 心跳包续期
        hb_resp = self.client.post(f"/api/diagrams/{diag_id}/heartbeat", headers=headers)
        self.assertEqual(hb_resp.status_code, 200)
        
        # 5. 保存画布 (正常保存，版本号自增至 2)
        save_resp = self.client.put(f"/api/diagrams/{diag_id}", headers=headers, json={
            "version_no": 1,
            "snapshot_json": '{"nodes": [], "edges": []}'
        })
        self.assertEqual(save_resp.status_code, 200)
        
        # 6. 并发覆盖测试 (使用过期版本号 1，应该返回 409 Conflict)
        conflict_resp = self.client.put(f"/api/diagrams/{diag_id}", headers=headers, json={
            "version_no": 1,
            "snapshot_json": '{"nodes": [], "edges": []}'
        })
        self.assertEqual(conflict_resp.status_code, 409)
        self.assertIn("已被其他成员更新", conflict_resp.json()["detail"])
        
        # 7. 主动释放锁
        rel_resp = self.client.post(f"/api/diagrams/{diag_id}/release", headers=headers)
        self.assertEqual(rel_resp.status_code, 200)

    def test_03_c_asset_extraction_and_deduplication(self):
        """
        测试资产提取 (BR-25, BR-29) 与 AI 资产去重建议及状态合并 (BR-33, BR-35)
        """
        headers = self.get_auth_headers(self.test_username, self.test_password)
        import json
        
        db_session = SessionLocal()
        domain = db_session.query(Domain).filter(Domain.name == "IVI域控").first()
        self.assertIsNotNone(domain)
        domain_id = domain.id
        diagram = db_session.query(Diagram).filter(Diagram.domain_id == domain_id).first()
        diag_id = diagram.id
        db_session.close()
        
        # 1. 模拟 React Flow 图形结构
        # 包含重复资产的命名 (例如: IVI_Bluetooth 与 IVI_Bluetooth_Receiver)
        snapshot_data = {
            "nodes": [
                {
                    "id": "node_1",
                    "type": "entity",
                    "data": {"name": "IVI_Bluetooth", "protocol": "Bluetooth", "description": "蓝牙网关"}
                },
                {
                    "id": "node_2",
                    "type": "entity",
                    "data": {"name": "IVI_Bluetooth_Receiver", "protocol": "Bluetooth", "description": "蓝牙接收器"}
                },
                {
                    "id": "node_3",
                    "type": "storage",
                    "data": {"name": "UserData_DB", "description": "用户本地数据"}
                }
            ],
            "edges": [
                {
                    "id": "edge_1",
                    "source": "node_1",
                    "target": "node_2",
                    "data": {"name": "BT_Stream", "protocol": "RFCOMM", "transmitted_info": "音频及配对流"}
                }
            ]
        }
        
        # 更新画布 JSON (version_no 此时应为 2)
        self.client.put(f"/api/diagrams/{diag_id}", headers=headers, json={
            "version_no": 2,
            "snapshot_json": json.dumps(snapshot_data)
        })
        
        # 2. 提取资产
        ext_resp = self.client.post(f"/api/domains/{domain_id}/extract-assets", headers=headers)
        self.assertEqual(ext_resp.status_code, 200)
        assets = ext_resp.json()
        
        # 检查是否成功提取 4 个资产 (3个节点，1个连线)
        self.assertEqual(len(assets), 4)
        
        # 获取 IVI_Bluetooth_Receiver 资产 ID 并设为 confirmed
        receiver_asset = next(a for a in assets if a["name"] == "IVI_Bluetooth_Receiver")
        db_session = SessionLocal()
        asset_db = db_session.query(Asset).filter(Asset.id == receiver_asset["id"]).first()
        asset_db.status = "confirmed"
        db_session.commit()
        db_session.close()
        
        # 3. 再次提取，模拟画布变更，清空 draft 资产，但保留刚才 confirmed 的资产
        # 我们模拟一个新的空画布，此时只有 confirmed 的资产应该被保留在数据库中
        self.client.put(f"/api/diagrams/{diag_id}", headers=headers, json={
            "version_no": 3,
            "snapshot_json": json.dumps({"nodes": [], "edges": []})
        })
        ext_again_resp = self.client.post(f"/api/domains/{domain_id}/extract-assets", headers=headers)
        assets_again = ext_again_resp.json()
        
        # 此时应该只有 confirmed 资产被保留 (IVI_Bluetooth_Receiver)
        self.assertEqual(len(assets_again), 1)
        self.assertEqual(assets_again[0]["name"], "IVI_Bluetooth_Receiver")
        
        # 还原画布数据以测试去重
        self.client.put(f"/api/diagrams/{diag_id}", headers=headers, json={
            "version_no": 4,
            "snapshot_json": json.dumps(snapshot_data)
        })
        assets_final = self.client.post(f"/api/domains/{domain_id}/extract-assets", headers=headers).json()
        self.assertEqual(len(assets_final), 4)
        
        # 4. 触发域控内 AI 去重建议 (BR-33)
        dedup_suggestions_resp = self.client.post(f"/api/domains/{domain_id}/deduplicate", headers=headers)
        self.assertEqual(dedup_suggestions_resp.status_code, 200)
        suggestions = dedup_suggestions_resp.json()
        self.assertTrue(len(suggestions) >= 1)
        
        # 5. 确认去重合并并保留轨迹 (BR-35)
        confirm_dedup_resp = self.client.post(f"/api/domains/{domain_id}/deduplicate/confirm", headers=headers, json={
            "suggestions": suggestions
        })
        self.assertEqual(confirm_dedup_resp.status_code, 200)
        
        # 检查被合并删除的资产是否变成了 rejected 并追加了备注信息
        db_session = SessionLocal()
        removed_asset_id = suggestions[0]["remove_asset_ids"][0]
        removed_asset = db_session.query(Asset).filter(Asset.id == removed_asset_id).first()
        self.assertEqual(removed_asset.status, "rejected")
        self.assertIn("AI去重合并", removed_asset.description)
        db_session.close()

        # 6. 测试锁定保护：自动提取资产为 confirmed 或 rejected 时无法通过 API 修改其它字段，也无法直接物理删除
        # 尝试更新名字，应该返回 400 Bad Request
        lock_upd_resp = self.client.post(f"/api/assets/{removed_asset_id}/confirm", headers=headers, json={
            "name": "恶意篡改已核对名称"
        })
        self.assertEqual(lock_upd_resp.status_code, 400)
        self.assertIn("只读锁定", lock_upd_resp.json()["detail"])
        
        # 尝试删除该已拒绝/已核对的自动提取资产，应该返回 400 Bad Request
        lock_del_resp = self.client.delete(f"/api/assets/{removed_asset_id}", headers=headers)
        self.assertEqual(lock_del_resp.status_code, 400)
        self.assertIn("切换为“待核对”", lock_del_resp.json()["detail"])
        
        # 允许将其状态变更回 draft
        unlock_resp = self.client.post(f"/api/assets/{removed_asset_id}/confirm", headers=headers, json={
            "status": "draft"
        })
        self.assertEqual(unlock_resp.status_code, 200)
        self.assertEqual(unlock_resp.json()["status"], "draft")
        
        # 变回 draft 后，现在可以正常修改并最终删除该自动提取资产
        mod_resp = self.client.post(f"/api/assets/{removed_asset_id}/confirm", headers=headers, json={
            "name": "修改后的解密资产名称"
        })
        self.assertEqual(mod_resp.status_code, 200)
        self.assertEqual(mod_resp.json()["name"], "修改后的解密资产名称")

    def test_03_d_tara_analysis_engine(self):
        """
        测试 TARA 异步/同步分析跑批 (BR-36/37, 40/41)、增量/继承 (BR-45, BR-51/75) 及联动免除 (BR-69)、取消运行 (BR-70)
        """
        headers = self.get_auth_headers(self.test_username, self.test_password)
        
        # 1. 获取 IVI 域控
        db_session = SessionLocal()
        domain = db_session.query(Domain).filter(Domain.name == "IVI域控").first()
        self.assertIsNotNone(domain)
        domain_id = domain.id
        
        # 确认另一个资产 IVI_Bluetooth
        bt_asset = db_session.query(Asset).filter(
            Asset.domain_id == domain_id,
            Asset.name == "IVI_Bluetooth"
        ).first()
        self.assertIsNotNone(bt_asset)
        bt_asset.status = "confirmed"
        bt_asset_id = bt_asset.id
        db_session.commit()
        db_session.close()
        
        # 2. 启动 TARA 分析 (有 confirmed 资产，应启动成功并触发同步/异步跑批)
        run_resp = self.client.post(f"/api/domains/{domain_id}/tara-runs", headers=headers)
        self.assertEqual(run_resp.status_code, 200)
        run_data = run_resp.json()
        self.assertEqual(run_data["progress"], 100) # 由于本地同步执行，进度应该为 100%
        
        # 3. 检查生成步骤详情
        steps_resp = self.client.get(f"/api/domains/{domain_id}/tara-results", headers=headers)
        self.assertEqual(steps_resp.status_code, 200)
        steps = steps_resp.json()
        
        # 1个 confirmed 资产，每个有5个阶段，共5个步骤
        self.assertEqual(len(steps), 5)
        
        # 4. 人工修改分析结论并进行标记 (BR-51)
        # 我们修改 IVI_Bluetooth 的阶段 ④ (风险处理决策)，决策改为 accept (接受风险)
        bt_stage4 = next(s for s in steps if s["asset_id"] == bt_asset_id and s["stage"] == "stage4")
        step_id = bt_stage4["id"]
        
        mod_resp = self.client.put(f"/api/tara-steps/{step_id}", headers=headers, json={
            "final_output": {"risk_rating": 2, "risk_decision": "accept", "justification": "分析员研判：仅测试"},
            "modification_reason": "人工研判：蓝牙近端风险极低且可控，选择接受风险"
        })
        self.assertEqual(mod_resp.status_code, 200)
        res_data = mod_resp.json()["analysis_result"]
        self.assertTrue(res_data["is_human_modified"])
        self.assertEqual(res_data["final_output"]["risk_decision"], "accept")
        
        # 5. 校验风险决策联动逻辑 (BR-69)
        # 因为阶段 ④ 修改为了 accept，阶段 ⑤ (CSR生成) 应该自动触发免除逻辑
        steps_after_mod_resp = self.client.get(f"/api/domains/{domain_id}/tara-results", headers=headers)
        steps_after_mod = steps_after_mod_resp.json()
        bt_stage5 = next(s for s in steps_after_mod if s["asset_id"] == bt_asset_id and s["stage"] == "stage5")
        
        self.assertTrue(bt_stage5["analysis_result"]["final_output"]["exempted"])
        self.assertEqual(len(bt_stage5["analysis_result"]["final_output"]["csr"]), 0)
        
        # 6. 重新分析并测试继承 (BR-51/75)
        # 重新跑批应该利用 incremental/inheritance 逻辑
        rerun_resp = self.client.post(f"/api/domains/{domain_id}/tara-runs", headers=headers)
        self.assertEqual(rerun_resp.status_code, 200)
        
        steps_rerun_resp = self.client.get(f"/api/domains/{domain_id}/tara-results", headers=headers)
        steps_rerun = steps_rerun_resp.json()
        
        # 校验继承：重新分析后，IVI_Bluetooth 阶段 ④ 的结论应该依然是人工修改后的 "accept" 结论，而不是重置为 AI 草案
        bt_stage4_rerun = next(s for s in steps_rerun if s["asset_id"] == bt_asset_id and s["stage"] == "stage4")
        self.assertTrue(bt_stage4_rerun["analysis_result"]["is_human_modified"])
        self.assertEqual(bt_stage4_rerun["analysis_result"]["final_output"]["risk_decision"], "accept")
        
        # 7. 测试中止运行 (Cancel Run, BR-70)
        # 模拟一个运行中的任务
        db_session = SessionLocal()
        domain_db = db_session.query(Domain).filter(Domain.id == domain_id).first()
        domain_db.status = "running"
        
        run_record = TaraRun(
            domain_id=domain_id,
            status="running",
            celery_task_id="mock-task-123"
        )
        db_session.add(run_record)
        db_session.commit()
        db_session.close()
        
        # 中止任务
        cancel_resp = self.client.post(f"/api/domains/{domain_id}/cancel-run", headers=headers)
        self.assertEqual(cancel_resp.status_code, 200)
        
        # 校验是否被重置为 not_started
        response = self.client.get(f"/api/projects/domains/{domain_id}", headers=headers) # wait, route is projects/domains/{id}? No, get domain details endpoint is GET /api/projects/{proj_id}/domains
        # We can query domain directly from DB
        db_session = SessionLocal()
        domain_chk = db_session.query(Domain).filter(Domain.id == domain_id).first()
        self.assertEqual(domain_chk.status, "not_started")
        self.assertEqual(domain_chk.progress, 0)
        db_session.close()
        
        # 8. 测试脱网离线手工备份填报 (BR-70)
        manual_resp = self.client.post(f"/api/domains/{domain_id}/manual-update", headers=headers, json={
            "steps": [
                {
                    "asset_id": bt_asset_id,
                    "stage": "stage1",
                    "output": {"confidentiality": "Low", "integrity": "Low"}
                }
            ]
        })
        self.assertEqual(manual_resp.status_code, 200)
        db_session = SessionLocal()
        domain_chk = db_session.query(Domain).filter(Domain.id == domain_id).first()
        self.assertEqual(domain_chk.status, "completed")
        db_session.close()

    def test_03_d2_manual_asset_management(self):
        """
        测试手动添加资产与删除资产 (BR-ManualAsset)
        """
        headers = self.get_auth_headers(self.test_username, self.test_password)
        
        # 1. 获取 IVI 域控 ID
        db_session = SessionLocal()
        domain = db_session.query(Domain).filter(Domain.name == "IVI域控").first()
        self.assertIsNotNone(domain)
        domain_id = domain.id
        db_session.close()
        
        # 2. 手动创建一个资产
        asset_data = {
            "name": "手动测试资产",
            "asset_type": "software",
            "protocol": "HTTPS",
            "description": "人工手动录入的安全资产"
        }
        response = self.client.post(f"/api/domains/{domain_id}/assets", headers=headers, json=asset_data)
        self.assertEqual(response.status_code, 200)
        new_asset = response.json()
        self.assertEqual(new_asset["name"], "手动测试资产")
        self.assertEqual(new_asset["asset_type"], "software")
        self.assertEqual(new_asset["protocol"], "HTTPS")
        self.assertEqual(new_asset["description"], "人工手动录入的安全资产")
        self.assertEqual(new_asset["status"], "draft") # 手动添加默认初始为待核对 (draft)
        asset_id = new_asset["id"]
        
        # 3. 获取资产列表，校验新创建的资产在列表中
        list_response = self.client.get(f"/api/domains/{domain_id}/assets", headers=headers)
        self.assertEqual(list_response.status_code, 200)
        assets_list = list_response.json()
        self.assertTrue(any(a["id"] == asset_id for a in assets_list))
        
        # 4. 删除此资产
        del_response = self.client.delete(f"/api/assets/{asset_id}", headers=headers)
        self.assertEqual(del_response.status_code, 200)
        self.assertEqual(del_response.json()["message"], "资产删除成功")
        
        # 5. 再次获取资产列表，校验该资产已被物理删除
        list_response_after = self.client.get(f"/api/domains/{domain_id}/assets", headers=headers)
        self.assertEqual(list_response_after.status_code, 200)
        assets_list_after = list_response_after.json()
        self.assertFalse(any(a["id"] == asset_id for a in assets_list_after))

    def test_03_e_settings_and_export(self):
        """
        测试 AI 配置、连通性测试 (BR-59/71) 与 XLSX/DOCX 报告脱敏导出 (BR-57/77)
        """
        admin_headers = self.get_auth_headers(self.admin_username, self.admin_password)
        analyst_headers = self.get_auth_headers(self.test_username, self.test_password)
        
        # 1. 保存 AI 大模型配置
        settings_data = {
            "api_base_url": "https://api.openai.com/v1",
            "api_key": "mock_test_key",
            "model_name": "gpt-4"
        }
        set_resp = self.client.post("/api/settings", headers=admin_headers, json=settings_data)
        self.assertEqual(set_resp.status_code, 200)
        self.assertEqual(set_resp.json()["model_name"], "gpt-4")
        
        # 普通分析员越权保存配置测试，应返回 403
        set_forbidden_resp = self.client.post("/api/settings", headers=analyst_headers, json=settings_data)
        self.assertEqual(set_forbidden_resp.status_code, 403)
        
        # 2. 大模型结构化连通性测试 (api_key 为 mock_test_key 会在 Mock 模式下返回 success)
        test_conn_resp = self.client.post("/api/settings/test-connection", headers=admin_headers, json=settings_data)
        self.assertEqual(test_conn_resp.status_code, 200)
        self.assertTrue(test_conn_resp.json()["success"])
        
        # 3. 导出 TARA 评估 Excel 报告 (XLSX, 正常版)
        db_session = SessionLocal()
        domain = db_session.query(Domain).filter(Domain.name == "IVI域控").first()
        self.assertIsNotNone(domain)
        domain_id = domain.id
        db_session.close()
        
        # 首先需要确保域控下有一个 completed 任务，手工填报已经让我们有了一个 completed 运行
        # 导出 XLSX
        xls_resp = self.client.get(f"/api/reports/domains/{domain_id}/export?format=xlsx", headers=analyst_headers)
        self.assertEqual(xls_resp.status_code, 200)
        self.assertEqual(xls_resp.headers["content-type"], "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        
        # 导出脱敏版 XLSX
        xls_desens_resp = self.client.get(f"/api/reports/domains/{domain_id}/export?format=xlsx&desensitize=true", headers=analyst_headers)
        self.assertEqual(xls_desens_resp.status_code, 200)
        
        # 导出 CSV
        doc_resp = self.client.get(f"/api/reports/domains/{domain_id}/export?format=csv", headers=analyst_headers)
        self.assertEqual(doc_resp.status_code, 200)
        self.assertTrue("text/csv" in doc_resp.headers["content-type"])
        
        # 导出脱敏版 CSV
        doc_desens_resp = self.client.get(f"/api/reports/domains/{domain_id}/export?format=csv&desensitize=true", headers=analyst_headers)
        self.assertEqual(doc_desens_resp.status_code, 200)

    def test_04_project_archiving_lock(self):
        """
        测试项目归档（completed）后的强行只读拦截逻辑 (BR-78)
        """
        # 1. 使用管理员账号归档项目
        admin_headers = self.get_auth_headers(self.admin_username, self.admin_password)
        
        db_session = SessionLocal()
        project = db_session.query(Project).first()
        self.assertIsNotNone(project)
        project_id = project.id
        db_session.close()
        
        # 归档项目
        response = self.client.post(f"/api/projects/{project_id}/archive", headers=admin_headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "completed")
        
        # 2. 模拟普通分析员尝试修改画布或项目数据，应被拦截并返回 403 只读错误
        analyst_headers = self.get_auth_headers(self.test_username, self.test_password)
        response = self.client.put(f"/api/projects/{project_id}", headers=analyst_headers, json={
            "name": "尝试恶意篡改归档项目名"
        })
        self.assertEqual(response.status_code, 403)
        self.assertIn("只读", response.json()["detail"])

if __name__ == "__main__":
    unittest.main()
