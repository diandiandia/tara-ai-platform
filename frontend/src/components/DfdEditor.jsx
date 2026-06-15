import React, { useState, useEffect, useCallback, useRef } from 'react';
import ReactFlow, { 
  MiniMap, 
  Controls, 
  Background, 
  useNodesState, 
  useEdgesState, 
  addEdge,
  MarkerType
} from 'reactflow';
import 'reactflow/dist/style.css';

import { useCanvasStore } from '../stores/canvasStore';
import { useAuthStore } from '../stores/authStore';
import { useProjectStore } from '../stores/projectStore';
import { 
  ArrowLeft, Save, Sparkles, HelpCircle, AlertTriangle, 
  Info, Cpu, ShieldAlert, WifiOff, RefreshCw, Layers
} from 'lucide-react';

import CustomDfdNode from './CustomDfdNode';
import { User, Database, Shield } from 'lucide-react';

const nodeTypes = {
  entity: CustomDfdNode,
  process: CustomDfdNode,
  storage: CustomDfdNode,
  boundary: CustomDfdNode
};

export default function DfdEditor({ setPage, diagramId }) {
  const { user } = useAuthStore();
  const { activeDomain } = useProjectStore();
  
  const {
    currentDiagram,
    nodes: storeNodes,
    edges: storeEdges,
    versionNo,
    lockedBy,
    isReadOnly,
    loading,
    error,
    isOffline,
    openDiagram,
    closeDiagram,
    setNodes,
    setEdges,
    saveDiagram,
    triggerAIGenerate,
    setOfflineStatus,
    activeUsers
  } = useCanvasStore();

  const [nodes, setNodesState, onNodesChange] = useNodesState([]);
  const [edges, setEdgesState, onEdgesChange] = useEdgesState([]);
  
  const [selectedNode, setSelectedNode] = useState(null);
  const [selectedEdge, setSelectedEdge] = useState(null);

  // AI Assistant Prompt State
  const [aiPrompt, setAiPrompt] = useState('');
  const [aiLoading, setAiLoading] = useState(false);

  // Sync ref to debounce store updates during dragging
  const syncTimeoutRef = useRef(null);

  // Sync state between zustand store and reactflow local state
  useEffect(() => {
    if (diagramId && user) {
      const foundDiag = useCanvasStore.getState().diagrams.find(d => d.id === diagramId);
      openDiagram(foundDiag || currentDiagram || { id: diagramId, domain_id: activeDomain?.id, version_no: 1, snapshot_json: '{}' }, user.username);
    }
    return () => {
      closeDiagram();
    };
  }, [diagramId]);

  useEffect(() => {
    const sanitizedNodes = storeNodes.map(node => {
      if (!node.style || !node.style.width || !node.style.height) {
        let width = 150;
        let height = 80;
        if (node.type === 'process') {
          width = 100;
          height = 100;
        } else if (node.type === 'boundary') {
          width = 280;
          height = 200;
        }
        return {
          ...node,
          style: {
            ...node.style,
            width,
            height
          }
        };
      }
      return node;
    });

    const sanitizedEdges = storeEdges.map(edge => {
      if (!edge.label && edge.data && edge.data.name) {
        return {
          ...edge,
          label: edge.data.name
        };
      }
      return edge;
    });

    setNodesState(sanitizedNodes);
    setEdgesState(sanitizedEdges);
  }, [storeNodes, storeEdges]);

  // Handle Online/Offline Status (BR-17)
  useEffect(() => {
    const handleOnline = () => setOfflineStatus(false);
    const handleOffline = () => setOfflineStatus(true);
    
    window.addEventListener('online', handleOnline);
    window.addEventListener('offline', handleOffline);

    return () => {
      window.removeEventListener('online', handleOnline);
      window.removeEventListener('offline', handleOffline);
    };
  }, []);

  // Update store when local nodes/edges change (debounced in store)
  const syncLocalChanges = (newNodes, newEdges) => {
    if (isReadOnly) return;
    setNodes(newNodes);
    setEdges(newEdges);
  };

  // Debounced store sync to eliminate drag latency / resistance (feedback loop)
  const debouncedSync = useCallback((newNodes, newEdges) => {
    if (isReadOnly) return;
    if (syncTimeoutRef.current) {
      clearTimeout(syncTimeoutRef.current);
    }
    syncTimeoutRef.current = setTimeout(() => {
      syncLocalChanges(newNodes, newEdges);
    }, 200); // Wait 200ms after user stops dragging to sync to Zustand store
  }, [isReadOnly]);

  const onConnect = useCallback((params) => {
    if (isReadOnly) return;
    const newEdges = addEdge({
      ...params,
      id: `e-${Date.now()}`,
      label: '数据流',
      data: { name: '数据流', transmitted_info: '', protocol: '' },
      markerEnd: { type: MarkerType.ArrowClosed }
    }, edges);
    setEdgesState(newEdges);
    syncLocalChanges(nodes, newEdges);
  }, [edges, nodes, isReadOnly]);

  const onNodesChangeHandler = useCallback((changes) => {
    if (isReadOnly) return;
    // Apply local changes immediately for smooth native dragging (60/120fps)
    onNodesChange(changes);
    
    const hasRemove = changes.some(c => c.type === 'remove');
    if (hasRemove) {
      setSelectedNode(null);
    }
    
    // Sync to store with a debounce so we don't trigger state-reload feedback loops during drag
    setNodesState((currentNodes) => {
      setEdgesState((currentEdges) => {
        debouncedSync(currentNodes, currentEdges);
        return currentEdges;
      });
      return currentNodes;
    });
  }, [isReadOnly, debouncedSync]);

  const onEdgesChangeHandler = useCallback((changes) => {
    if (isReadOnly) return;
    onEdgesChange(changes);
    const hasRemove = changes.some(c => c.type === 'remove');
    if (hasRemove) {
      setSelectedEdge(null);
    }
    
    setNodesState((currentNodes) => {
      setEdgesState((currentEdges) => {
        debouncedSync(currentNodes, currentEdges);
        return currentEdges;
      });
      return currentNodes;
    });
  }, [isReadOnly, debouncedSync]);

  // Click Event on Node
  const onNodeClick = useCallback((event, node) => {
    setSelectedNode(node);
    setSelectedEdge(null);
  }, []);

  // Click Event on Edge
  const onEdgeClick = useCallback((event, edge) => {
    setSelectedEdge(edge);
    setSelectedNode(null);
  }, []);

  // Click on Canvas background clears selection
  const onPaneClick = useCallback(() => {
    setSelectedNode(null);
    setSelectedEdge(null);
  }, []);

  // Helper to add nodes locally
  const handleAddNode = (type) => {
    if (isReadOnly) return;
    
    let typeName = '外部实体';
    if (type === 'process') typeName = '处理过程';
    if (type === 'storage') typeName = '数据存储';
    if (type === 'boundary') typeName = '物理边界';

    let width = 150;
    let height = 80;
    if (type === 'process') {
      width = 100;
      height = 100;
    } else if (type === 'boundary') {
      width = 280;
      height = 200;
    }

    const newNode = {
      id: `n-${Date.now()}`,
      type,
      position: { x: 250, y: 200 },
      style: { width, height },
      data: { 
        name: `新${typeName}`, 
        description: '', 
        protocol: type === 'boundary' ? '' : 'CAN', 
        remarks: '' 
      }
    };
    const newNodes = [...nodes, newNode];
    setNodesState(newNodes);
    syncLocalChanges(newNodes, edges);
    setSelectedNode(newNode);
    setSelectedEdge(null);
  };

  const handleDeleteSelectedNode = () => {
    if (isReadOnly || !selectedNode) return;
    if (window.confirm(`确认要删除节点 "${selectedNode.data.name}" 吗？所有与其连接的线也将删除。`)) {
      const newNodes = nodes.filter(n => n.id !== selectedNode.id);
      const newEdges = edges.filter(e => e.source !== selectedNode.id && e.target !== selectedNode.id);
      setNodesState(newNodes);
      setEdgesState(newEdges);
      syncLocalChanges(newNodes, newEdges);
      setSelectedNode(null);
    }
  };

  const handleDeleteSelectedEdge = () => {
    if (isReadOnly || !selectedEdge) return;
    if (window.confirm(`确认要删除连线吗？`)) {
      const newEdges = edges.filter(e => e.id !== selectedEdge.id);
      setEdgesState(newEdges);
      syncLocalChanges(nodes, newEdges);
      setSelectedEdge(null);
    }
  };

  // Details updates from right form panel
  const handleNodeDataChange = (field, value) => {
    if (isReadOnly || !selectedNode) return;
    const updatedNodes = nodes.map(n => {
      if (n.id === selectedNode.id) {
        const updatedNode = {
          ...n,
          data: { ...n.data, [field]: value }
        };
        setSelectedNode(updatedNode); // update active form panel selection
        return updatedNode;
      }
      return n;
    });
    setNodesState(updatedNodes);
    syncLocalChanges(updatedNodes, edges);
  };

  const handleEdgeDataChange = (field, value) => {
    if (isReadOnly || !selectedEdge) return;
    const updatedEdges = edges.map(e => {
      if (e.id === selectedEdge.id) {
        const updatedEdge = {
          ...e,
          data: { ...e.data, [field]: value }
        };
        if (field === 'name') {
          updatedEdge.label = value;
        }
        setSelectedEdge(updatedEdge);
        return updatedEdge;
      }
      return e;
    });
    setEdgesState(updatedEdges);
    syncLocalChanges(nodes, updatedEdges);
  };

  const handleAIGenerateClick = async () => {
    if (isReadOnly || !aiPrompt.trim()) return;
    if (window.confirm('一键 AI 生成画图会先清空您当前的画布并重置！确定继续吗？')) {
      setAiLoading(true);
      const res = await triggerAIGenerate(aiPrompt.trim());
      setAiLoading(false);
      if (res) {
        setAiPrompt('');
        alert('AI 画图拓扑生成成功！');
      }
    }
  };

  const handleManualSave = () => {
    saveDiagram();
  };

  const isLockOwner = lockedBy === user?.username;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: 'calc(100vh - 64px)', overflow: 'hidden' }}>
      {/* Editor Header Status Bar */}
      <div className="glass" style={{
        padding: '12px 24px',
        borderRadius: '0',
        borderBottom: '1px solid var(--border-color)',
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        background: 'var(--drawer-bg)'
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
          <button 
            onClick={() => setPage('workbench')} 
            className="btn btn-secondary"
            style={{ padding: '8px 14px' }}
          >
            <ArrowLeft size={14} /> 返回工作台
          </button>

          <div>
            <h3 style={{ fontSize: '15px', fontWeight: '600', color: 'var(--text-primary)' }}>
              {currentDiagram?.title || '功能数据流图'}
            </h3>
            <span style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>
              版本: v{versionNo} • 子域控: {activeDomain?.name}
            </span>
          </div>
        </div>

        {/* Sync / Lock Indicators */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '14px' }}>
          {isOffline && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', color: 'var(--warning)', fontSize: '12px', background: 'rgba(217, 119, 6, 0.08)', padding: '6px 12px', borderRadius: '6px', border: '1px solid rgba(217, 119, 6, 0.2)' }}>
              <WifiOff size={14} />
              <span>脱网离线模式 (数据将暂存在浏览器)</span>
            </div>
          )}

          {lockedBy ? (
            <div style={{
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
              fontSize: '12px',
              background: isReadOnly ? 'rgba(225, 29, 72, 0.08)' : 'rgba(5, 150, 105, 0.08)',
              color: isReadOnly ? '#e11d48' : '#059669',
              border: '1px solid',
              borderColor: isReadOnly ? 'rgba(225, 29, 72, 0.2)' : 'rgba(5, 150, 105, 0.2)',
              padding: '6px 12px',
              borderRadius: '6px'
            }}>
              <Cpu size={14} />
              <span>
                {isReadOnly 
                  ? `👁️ 只读查看中 (当前正在由 [${lockedBy}] 编辑，共 ${activeUsers?.length || 1} 人在场)` 
                  : (activeUsers?.length <= 1 
                      ? '✅ 独占编辑中 (当前仅您一人在画布中)' 
                      : `✍️ 协同编辑中 (您是当前编辑人，其他 ${activeUsers.length - 1} 人只读)`)}
              </span>
            </div>
          ) : (
            <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>正在连接协同锁定服务...</span>
          )}

          <button
            onClick={handleManualSave}
            className="btn btn-secondary"
            style={{ padding: '8px 12px' }}
            disabled={isReadOnly || loading}
          >
            {loading ? <div className="spinner"></div> : <Save size={14} />}
            <span>保存</span>
          </button>
        </div>
      </div>

      {/* Main Workspace split */}
      <div style={{ display: 'flex', flexGrow: 1, overflow: 'hidden', position: 'relative' }}>
        
        {/* Left Elements Box Panel */}
        <div className="glass" style={{
          width: '220px',
          borderRadius: '0',
          borderRight: '1px solid var(--border-color)',
          padding: '20px 14px',
          display: 'flex',
          flexDirection: 'column',
          gap: '16px',
          background: 'var(--sidebar-bg)',
          zIndex: 10,
          overflowY: 'auto'
        }}>
          <h4 style={{ fontSize: '12px', textTransform: 'uppercase', color: 'var(--text-muted)', letterSpacing: '1px', marginBottom: '4px' }}>
            STRIDE 绘图组件箱
          </h4>

          <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
            {/* Entity Card */}
            <div
              onClick={() => !isReadOnly && handleAddNode('entity')}
              style={{
                padding: '12px',
                background: 'rgba(99, 102, 241, 0.05)',
                border: '1.5px solid rgba(99, 102, 241, 0.25)',
                borderRadius: '8px',
                cursor: isReadOnly ? 'not-allowed' : 'pointer',
                display: 'flex',
                flexDirection: 'column',
                gap: '8px',
                transition: 'all 0.2s ease',
                opacity: isReadOnly ? 0.5 : 1,
              }}
              className="toolbox-card entity-card"
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <User size={15} style={{ color: '#818cf8' }} />
                <span style={{ fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)' }}>外部实体 (Entity)</span>
              </div>
              <div style={{
                height: '32px',
                border: '2px solid #6366f1',
                background: 'rgba(99, 102, 241, 0.1)',
                borderRadius: '4px',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: '10px',
                color: '#818cf8',
              }}>
                [ Entity ]
              </div>
            </div>

            {/* Process Card */}
            <div
              onClick={() => !isReadOnly && handleAddNode('process')}
              style={{
                padding: '12px',
                background: 'rgba(217, 70, 239, 0.05)',
                border: '1.5px solid rgba(217, 70, 239, 0.25)',
                borderRadius: '8px',
                cursor: isReadOnly ? 'not-allowed' : 'pointer',
                display: 'flex',
                flexDirection: 'column',
                gap: '8px',
                transition: 'all 0.2s ease',
                opacity: isReadOnly ? 0.5 : 1,
              }}
              className="toolbox-card process-card"
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <Cpu size={15} style={{ color: '#f472b6' }} />
                <span style={{ fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)' }}>处理过程 (Process)</span>
              </div>
              <div style={{
                height: '36px',
                width: '36px',
                margin: '0 auto',
                border: '2.5px solid #d946ef',
                background: 'rgba(217, 70, 239, 0.1)',
                borderRadius: '50%',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: '10px',
                color: '#f472b6',
              }}>
                P
              </div>
            </div>

            {/* Storage Card */}
            <div
              onClick={() => !isReadOnly && handleAddNode('storage')}
              style={{
                padding: '12px',
                background: 'rgba(16, 185, 129, 0.05)',
                border: '1.5px solid rgba(16, 185, 129, 0.25)',
                borderRadius: '8px',
                cursor: isReadOnly ? 'not-allowed' : 'pointer',
                display: 'flex',
                flexDirection: 'column',
                gap: '8px',
                transition: 'all 0.2s ease',
                opacity: isReadOnly ? 0.5 : 1,
              }}
              className="toolbox-card storage-card"
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <Database size={15} style={{ color: '#34d399' }} />
                <span style={{ fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)' }}>数据存储 (Storage)</span>
              </div>
              <div style={{
                height: '32px',
                borderTop: '2.5px solid #10b981',
                borderBottom: '2.5px solid #10b981',
                borderLeft: 'none',
                borderRight: 'none',
                background: 'rgba(16, 185, 129, 0.1)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: '10px',
                color: '#34d399',
              }}>
                ==========
              </div>
            </div>

            {/* Boundary Card */}
            <div
              onClick={() => !isReadOnly && handleAddNode('boundary')}
              style={{
                padding: '12px',
                background: 'rgba(244, 63, 94, 0.05)',
                border: '1.5px solid rgba(244, 63, 94, 0.25)',
                borderRadius: '8px',
                cursor: isReadOnly ? 'not-allowed' : 'pointer',
                display: 'flex',
                flexDirection: 'column',
                gap: '8px',
                transition: 'all 0.2s ease',
                opacity: isReadOnly ? 0.5 : 1,
              }}
              className="toolbox-card boundary-card"
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <Shield size={15} style={{ color: '#fb7185' }} />
                <span style={{ fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)' }}>物理边界 (Boundary)</span>
              </div>
              <div style={{
                height: '32px',
                border: '2px dashed #f43f5e',
                background: 'rgba(244, 63, 94, 0.05)',
                borderRadius: '4px',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontSize: '10px',
                color: '#fb7185',
              }}>
                - - - - - - - -
              </div>
            </div>
          </div>

          <div style={{ marginTop: 'auto', fontSize: '12px', color: 'var(--text-secondary)', lineHeight: '1.4' }}>
            <Info size={14} style={{ color: 'var(--primary)', marginBottom: '4px' }} />
            <p>提示：点击组件添加到画布，选中组件可拖动、在边缘进行缩放、连线或修改属性。</p>
          </div>
        </div>

        {/* Center Flow Canvas */}
        <div style={{ flexGrow: 1, height: '100%', background: '#f8fafc' }}>
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChangeHandler}
            onEdgesChange={onEdgesChangeHandler}
            onConnect={onConnect}
            onNodeClick={onNodeClick}
            onEdgeClick={onEdgeClick}
            onPaneClick={onPaneClick}
            nodeTypes={nodeTypes}
            fitView
            deleteKeyCode={["Delete", "Backspace"]}
          >
            <Background color="#cbd5e1" gap={16} size={1} />
            <Controls style={{ background: '#fff', border: '1px solid #cbd5e1', color: '#1e293b' }} />
            <MiniMap 
              nodeStrokeColor={(n) => {
                if (n.type === 'entity') return '#6366f1';
                if (n.type === 'process') return '#d946ef';
                if (n.type === 'storage') return '#10b981';
                if (n.type === 'boundary') return '#f43f5e';
                return '#ccc';
              }}
              nodeColor={(n) => {
                if (n.type === 'entity') return 'rgba(99, 102, 241, 0.2)';
                if (n.type === 'process') return 'rgba(217, 70, 239, 0.2)';
                if (n.type === 'storage') return 'rgba(16, 185, 129, 0.2)';
                if (n.type === 'boundary') return 'rgba(244, 63, 150, 0.2)';
                return '#222';
              }}
              style={{ background: 'var(--bg-dark)', border: '1px solid var(--border-color)' }}
            />
          </ReactFlow>
        </div>

        {/* Right Details Panel Drawer */}
        {selectedNode && (
          <div className="glass" style={{
            width: '300px',
            borderRadius: '0',
            borderLeft: '1px solid var(--border-color)',
            padding: '20px 18px',
            background: 'var(--drawer-bg)',
            zIndex: 10,
            overflowY: 'auto'
          }}>
            <h3 style={{ fontSize: '16px', fontWeight: '600', marginBottom: '20px', color: 'var(--text-primary)' }}>
              节点属性设置
            </h3>

            <div className="input-group">
              <span className="input-label">资产节点名称 <span style={{ color: 'var(--accent)' }}>*</span></span>
              <input
                type="text"
                className="input-field"
                value={selectedNode.data.name || ''}
                onChange={(e) => handleNodeDataChange('name', e.target.value)}
                disabled={isReadOnly}
              />
            </div>

            <div className="input-group">
              <span className="input-label">字体大小 (Font Size): {selectedNode.data.fontSize || 11}px</span>
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                <input
                  type="range"
                  min="6"
                  max="24"
                  step="1"
                  style={{ flexGrow: 1, accentColor: 'var(--primary)', cursor: 'pointer' }}
                  value={selectedNode.data.fontSize || 11}
                  onChange={(e) => handleNodeDataChange('fontSize', parseInt(e.target.value))}
                  disabled={isReadOnly}
                />
                <input
                  type="number"
                  min="6"
                  max="24"
                  className="input-field"
                  style={{ width: '65px', padding: '6px', textAlign: 'center', margin: 0 }}
                  value={selectedNode.data.fontSize || 11}
                  onChange={(e) => {
                    const val = parseInt(e.target.value);
                    if (!isNaN(val)) {
                      handleNodeDataChange('fontSize', val);
                    }
                  }}
                  disabled={isReadOnly}
                />
              </div>
            </div>

            <div className="input-group">
              <span className="input-label">通信协议 (Protocol)</span>
              <input
                type="text"
                className="input-field"
                placeholder="例如: CAN, LIN, Ethernet, HTTP"
                value={selectedNode.data.protocol || ''}
                onChange={(e) => handleNodeDataChange('protocol', e.target.value)}
                disabled={isReadOnly}
              />
            </div>

            <div className="input-group">
              <span className="input-label">功能描述</span>
              <textarea
                className="input-field"
                rows={3}
                placeholder="描述此安全分析项的具体工作..."
                value={selectedNode.data.description || ''}
                onChange={(e) => handleNodeDataChange('description', e.target.value)}
                style={{ resize: 'none' }}
                disabled={isReadOnly}
              />
            </div>

            <div className="input-group" style={{ marginBottom: '24px' }}>
              <span className="input-label">分析备注</span>
              <input
                type="text"
                className="input-field"
                value={selectedNode.data.remarks || ''}
                onChange={(e) => handleNodeDataChange('remarks', e.target.value)}
                disabled={isReadOnly}
              />
            </div>

            {!isReadOnly && (
              <button
                onClick={handleDeleteSelectedNode}
                className="btn btn-danger"
                style={{ width: '100%' }}
              >
                删除当前节点
              </button>
            )}
          </div>
        )}

        {selectedEdge && (
          <div className="glass" style={{
            width: '300px',
            borderRadius: '0',
            borderLeft: '1px solid var(--border-color)',
            padding: '20px 18px',
            background: 'var(--drawer-bg)',
            zIndex: 10,
            overflowY: 'auto'
          }}>
            <h3 style={{ fontSize: '16px', fontWeight: '600', marginBottom: '20px', color: 'var(--text-primary)' }}>
              连线属性设置
            </h3>

            <div className="input-group">
              <span className="input-label">数据流名称</span>
              <input
                type="text"
                className="input-field"
                value={selectedEdge.data?.name || ''}
                onChange={(e) => handleEdgeDataChange('name', e.target.value)}
                disabled={isReadOnly}
              />
            </div>

            <div className="input-group">
              <span className="input-label">通信协议 (Protocol)</span>
              <input
                type="text"
                className="input-field"
                placeholder="例如: HTTPS, MQTT, SOME/IP, CAN"
                value={selectedEdge.data?.protocol || ''}
                onChange={(e) => handleEdgeDataChange('protocol', e.target.value)}
                disabled={isReadOnly}
              />
            </div>

            <div className="input-group" style={{ marginBottom: '24px' }}>
              <span className="input-label">传输的数据信息 (Data Info)</span>
              <input
                type="text"
                className="input-field"
                placeholder="例如: 诊断帧, OTA包, 控制报文"
                value={selectedEdge.data?.transmitted_info || ''}
                onChange={(e) => handleEdgeDataChange('transmitted_info', e.target.value)}
                disabled={isReadOnly}
              />
            </div>

            {!isReadOnly && (
              <button
                onClick={handleDeleteSelectedEdge}
                className="btn btn-danger"
                style={{ width: '100%' }}
              >
                删除当前连线
              </button>
            )}
          </div>
        )}

        {/* AI Drawing Drawer sidebar (If no node or edge selected) */}
        {!selectedNode && !selectedEdge && (
          <div className="glass" style={{
            width: '300px',
            borderRadius: '0',
            borderLeft: '1px solid var(--border-color)',
            padding: '20px 18px',
            background: 'var(--drawer-bg)',
            zIndex: 10,
            display: 'flex',
            flexDirection: 'column',
            gap: '16px'
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <Sparkles size={16} style={{ color: 'var(--primary)' }} />
              <h3 style={{ fontSize: '16px', fontWeight: '600', color: 'var(--text-primary)' }}>AI 一键自动画图</h3>
            </div>
            
            <p style={{ color: 'var(--text-secondary)', fontSize: '12px', lineHeight: '1.4' }}>
              输入您想要绘制的车载安全场景（例如：“OTA固件升级拓扑”或“UDS诊断功能数据流”），AI 会自动布局生成完整的节点与连线关系。
            </p>

            <div className="input-group">
              <textarea
                className="input-field"
                rows={5}
                placeholder="例如：绘制一个车载娱乐系统IVI与中央网关、OBD接口诊断的拓扑图..."
                value={aiPrompt}
                onChange={(e) => setAiPrompt(e.target.value)}
                style={{ resize: 'none' }}
                disabled={isReadOnly || aiLoading}
              />
            </div>

            <button
              onClick={handleAIGenerateClick}
              className="btn btn-primary"
              style={{ width: '100%' }}
              disabled={isReadOnly || aiLoading || !aiPrompt.trim()}
            >
              {aiLoading ? (
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                  <div className="spinner"></div> 正在生成中...
                </div>
              ) : (
                <>
                  <Sparkles size={14} />
                  <span>一键生成拓扑图</span>
                </>
              )}
            </button>
          </div>
        )}
      </div>

      {/* Save Conflict Alert Modal (BR-16) */}
      {error && error.includes('保存冲突') && (
        <div className="modal-overlay">
          <div className="modal-content glass" style={{ width: '450px', textAlign: 'center' }}>
            <div style={{
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              width: '50px',
              height: '50px',
              borderRadius: '50%',
              background: 'rgba(225, 29, 72, 0.08)',
              color: 'var(--accent)',
              marginBottom: '16px'
            }}>
              <ShieldAlert size={28} />
            </div>

            <h3 style={{ fontSize: '18px', fontWeight: '600', color: 'var(--text-primary)', marginBottom: '8px' }}>
              乐观锁冲突！
            </h3>
            <p style={{ color: 'var(--text-secondary)', fontSize: '13px', lineHeight: '1.5', marginBottom: '24px' }}>
              检测到此画布已有其他成员进行了保存提交。
              为避免覆盖他人的工作，系统已自动将您的修改备份至浏览器的 LocalStorage 中。请立即刷新页面以合并。
            </p>

            <button 
              onClick={() => window.location.reload()} 
              className="btn btn-primary"
              style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '6px' }}
            >
              <RefreshCw size={14} /> 刷新重新加载
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
