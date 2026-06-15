from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from typing import Dict, List, Tuple
import json

router = APIRouter(tags=["WebSocket 实时并发控制"])

class DiagramConnectionManager:
    def __init__(self):
        # diagram_id -> list of tuples (username, websocket)
        self.active_users: Dict[int, List[Tuple[str, WebSocket]]] = {}

    async def connect(self, diagram_id: int, username: str, websocket: WebSocket):
        await websocket.accept()
        if diagram_id not in self.active_users:
            self.active_users[diagram_id] = []
        
        # 移除已有的相同用户连接（例如用户刷新页面导致的旧连接未完全释放）
        self.active_users[diagram_id] = [u for u in self.active_users[diagram_id] if u[0] != username]
        
        self.active_users[diagram_id].append((username, websocket))
        print(f"WebSocket 用户 [{username}] 已加入画布 [{diagram_id}]")
        await self.broadcast_status(diagram_id)

    async def disconnect(self, diagram_id: int, username: str, websocket: WebSocket):
        if diagram_id in self.active_users:
            self.active_users[diagram_id] = [
                u for u in self.active_users[diagram_id] if u[1] != websocket
            ]
            if not self.active_users[diagram_id]:
                del self.active_users[diagram_id]
        print(f"WebSocket 用户 [{username}] 已离开画布 [{diagram_id}]")
        await self.broadcast_status(diagram_id)

    async def broadcast_status(self, diagram_id: int):
        """
        向当前查看该画布的所有人广播活跃用户列表及当前的锁持有者 (最先进入该画布的人)
        """
        if diagram_id not in self.active_users or not self.active_users[diagram_id]:
            return
            
        users = [u[0] for u in self.active_users[diagram_id]]
        # 锁给最先进入画布的第一个活跃用户
        locked_by = users[0]
        
        message = {
            "type": "COLLABORATIVE_LOCK_UPDATE",
            "active_users": users,
            "locked_by": locked_by
        }
        
        dead_connections = []
        for username, ws in self.active_users[diagram_id]:
            try:
                await ws.send_json(message)
            except Exception:
                dead_connections.append((username, ws))
                
        # 清除无法发送消息的死链
        if dead_connections:
            for username, ws in dead_connections:
                self.active_users[diagram_id] = [u for u in self.active_users[diagram_id] if u[1] != ws]
            if diagram_id in self.active_users and not self.active_users[diagram_id]:
                del self.active_users[diagram_id]
            # 重新广播状态
            await self.broadcast_status(diagram_id)

manager = DiagramConnectionManager()

@router.websocket("/ws/diagrams/{diagram_id}")
async def websocket_diagram_endpoint(
    websocket: WebSocket,
    diagram_id: int,
    username: str = Query(...)
):
    """
    WebSocket 协同端点：自动维护进入此画布的用户列表。
    当且仅当存在多个用户在同一个画布内时激活编辑锁定。
    """
    await manager.connect(diagram_id, username, websocket)
    try:
        while True:
            # 持续阻塞监听客户端连接，保持链路活性
            await websocket.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(diagram_id, username, websocket)
