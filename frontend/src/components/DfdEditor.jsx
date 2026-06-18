import { useI18n } from '../stores/i18nStore';
import React, { useState, useEffect, useCallback, useRef } from 'react';
import ReactFlow, { 
  MiniMap, 
  Controls, 
  Background, 
  useNodesState, 
  useEdgesState, 
  addEdge,
  MarkerType,
  updateEdge,
  getBezierPath,
  getSmoothStepPath,
  getStraightPath,
  EdgeLabelRenderer
} from 'reactflow';
import 'reactflow/dist/style.css';

import { useCanvasStore } from '../stores/canvasStore';
import { useAuthStore } from '../stores/authStore';
import { useProjectStore } from '../stores/projectStore';
import { 
  ArrowLeft, Save, Sparkles, HelpCircle, AlertTriangle, 
  Info, Cpu, ShieldAlert, WifiOff, RefreshCw, Layers, Send,
  ArrowUp, ArrowDown, ChevronsUp, ChevronsDown
} from 'lucide-react';

import CustomDfdNode from './CustomDfdNode';
import { User, Database, Shield } from 'lucide-react';

function ParallelBezierEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  sourcePosition,
  targetPosition,
  style = {},
  markerEnd,
  label,
  data
}) {
  const lineStyle = data?.lineStyle || 'bezier';
  let edgePath = '';
  let labelX = 0;
  let labelY = 0;

  if (lineStyle === 'smoothstep') {
    const borderRadius = data?.borderRadius !== undefined ? data.borderRadius : 12;
    [edgePath, labelX, labelY] = getSmoothStepPath({
      sourceX,
      sourceY,
      sourcePosition,
      targetX,
      targetY,
      targetPosition,
      borderRadius
    });
  } else if (lineStyle === 'straight') {
    [edgePath, labelX, labelY] = getStraightPath({
      sourceX,
      sourceY,
      targetX,
      targetY
    });
  } else {
    const curvature = data?.curvature !== undefined ? data.curvature : 0.25;
    [edgePath, labelX, labelY] = getBezierPath({
      sourceX,
      sourceY,
      sourcePosition,
      targetX,
      targetY,
      targetPosition,
      curvature
    });
  }

  return (
    <>
      <path
        id={id}
        style={style}
        className="react-flow__edge-path"
        d={edgePath}
        markerEnd={markerEnd}
      />
      <path
        className="react-flow__edge-interaction"
        d={edgePath}
        fill="none"
        strokeOpacity={0}
        strokeWidth={20}
        style={{ cursor: 'pointer' }}
      />
      {label && (
        <EdgeLabelRenderer>
          <div
            style={{
              position: 'absolute',
              transform: `translate(-50%, -50%) translate(${labelX}px,${labelY}px)`,
              background: '#ffffff',
              padding: '2px 6px',
              borderRadius: '4px',
              fontSize: '10px',
              fontWeight: '500',
              color: '#475569',
              border: '1px solid #cbd5e1',
              pointerEvents: 'all'
            }}
            className="nodrag nopan"
          >
            {label}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

const edgeTypes = {
  parallel: ParallelBezierEdge,
  default: ParallelBezierEdge
};

const preprocessEdges = (edgesList, nodesList = []) => {
  if (!edgesList) return [];
  
  // 1. Auto-routing calculation
  const routedEdges = edgesList.map(edge => {
    if (edge.data?.autoRoute === false) {
      return edge;
    }

    const sourceNode = nodesList.find(n => n.id === edge.source);
    const targetNode = nodesList.find(n => n.id === edge.target);

    if (!sourceNode || !targetNode) {
      return edge;
    }

    // Handle self-loop
    if (edge.source === edge.target) {
      return {
        ...edge,
        sourceHandle: 's-top',
        targetHandle: 't-right'
      };
    }

    const sWidth = sourceNode.style?.width || (sourceNode.type === 'process' ? 100 : sourceNode.type === 'boundary' ? 280 : 150);
    const sHeight = sourceNode.style?.height || (sourceNode.type === 'process' ? 100 : sourceNode.type === 'boundary' ? 200 : 80);
    const tWidth = targetNode.style?.width || (targetNode.type === 'process' ? 100 : targetNode.type === 'boundary' ? 280 : 150);
    const tHeight = targetNode.style?.height || (targetNode.type === 'process' ? 100 : targetNode.type === 'boundary' ? 200 : 80);

    const sX = sourceNode.position.x + sWidth / 2;
    const sY = sourceNode.position.y + sHeight / 2;
    const tX = targetNode.position.x + tWidth / 2;
    const tY = targetNode.position.y + tHeight / 2;

    const dx = tX - sX;
    const dy = tY - sY;

    let sourceHandle = edge.sourceHandle || 's-right';
    let targetHandle = edge.targetHandle || 't-left';

    if (Math.abs(dx) > Math.abs(dy)) {
      if (dx > 0) {
        sourceHandle = 's-right';
        targetHandle = 't-left';
      } else {
        sourceHandle = 's-left';
        targetHandle = 't-right';
      }
    } else {
      if (dy > 0) {
        sourceHandle = 's-bottom';
        targetHandle = 't-top';
      } else {
        sourceHandle = 's-top';
        targetHandle = 't-bottom';
      }
    }

    return {
      ...edge,
      sourceHandle,
      targetHandle
    };
  });

  const groups = {};
  routedEdges.forEach(edge => {
    const nodesPair = [edge.source, edge.target].sort();
    const key = nodesPair.join('-');
    if (!groups[key]) groups[key] = [];
    groups[key].push(edge);
  });

  const processed = [];
  Object.keys(groups).forEach(key => {
    const groupEdges = groups[key];
    const count = groupEdges.length;

    if (count === 1) {
      const edge = groupEdges[0];
      processed.push({
        ...edge,
        type: 'parallel',
        data: {
          ...edge.data,
          curvature: edge.data?.curvature !== undefined ? edge.data.curvature : undefined
        }
      });
    } else {
      groupEdges.forEach((edge, index) => {
        if (edge.data?.curvature !== undefined) {
          processed.push({
            ...edge,
            type: 'parallel'
          });
        } else {
          const mid = (count - 1) / 2;
          const spacing = 0.25;
          let curvature = (index - mid) * spacing;
          if (curvature === 0) {
            curvature = 0.12;
          }
          processed.push({
            ...edge,
            type: 'parallel',
            data: {
              ...edge.data,
              curvature: curvature
            }
          });
        }
      });
    }
  });
  return processed;
};

const nodeTypes = {
  entity: CustomDfdNode,
  process: CustomDfdNode,
  storage: CustomDfdNode,
  boundary: CustomDfdNode
};

export default function DfdEditor({ setPage, diagramId }) {
  const { t, language } = useI18n();
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
    triggerAIChat,
    applySnapshot,
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
  const [chatHistory, setChatHistory] = useState([
    { sender: 'ai', text: t('您好！我是您的 AI 拓扑助理。请告诉我您想要绘制的场景或系统（例如：IVI网关诊断、智能前视摄像头与域控通信），我会为您规划拓扑结构并提供一键生成 DFD 功能图。') }
  ]);
  const [lastSuggestedSnapshot, setLastSuggestedSnapshot] = useState(null);
  const [isChatSending, setIsChatSending] = useState(false);

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
      const updated = {
        ...edge,
        markerEnd: edge.markerEnd || { type: MarkerType.ArrowClosed }
      };
      if (!updated.label && updated.data && updated.data.name) {
        updated.label = updated.data.name;
      }
      return updated;
    });

    const processedEdges = preprocessEdges(sanitizedEdges, sanitizedNodes);
    setNodesState(sanitizedNodes);
    setEdgesState(processedEdges);
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
    const newEdgesRaw = addEdge({
      ...params,
      id: `e-${Date.now()}`,
      label: t('数据流'),
      data: { name: t('数据流'), transmitted_info: '', protocol: '', autoRoute: true },
      markerEnd: { type: MarkerType.ArrowClosed }
    }, edges);
    const newEdges = preprocessEdges(newEdgesRaw, nodes);
    setEdgesState(newEdges);
    syncLocalChanges(nodes, newEdges);
  }, [edges, nodes, isReadOnly]);

  const onEdgeUpdate = useCallback((oldEdge, newConnection) => {
    if (isReadOnly) return;
    setEdgesState((els) => {
      const updatedRaw = updateEdge(oldEdge, newConnection, els);
      const updated = preprocessEdges(updatedRaw, nodes);
      syncLocalChanges(nodes, updated);
      const newEdgeObj = updated.find(e => e.id === oldEdge.id);
      if (newEdgeObj) {
        setSelectedEdge(newEdgeObj);
      }
      return updated;
    });
  }, [nodes, isReadOnly]);

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
        const processed = preprocessEdges(currentEdges, currentNodes);
        debouncedSync(currentNodes, processed);
        return processed;
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
        const processed = preprocessEdges(currentEdges, currentNodes);
        debouncedSync(currentNodes, processed);
        return processed;
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
    
    let typeName = t('外部实体');
    if (type === 'process') typeName = t('处理过程');
    if (type === 'storage') typeName = t('数据存储');
    if (type === 'boundary') typeName = t('物理边界');

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
        name: language === 'zh' ? `新${typeName}` : `New ${typeName}`, 
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
    if (window.confirm(t('确认要删除节点 "') + selectedNode.data.name + t('" 吗？所有与其连接的线也将删除。'))) {
      const newNodes = nodes.filter(n => n.id !== selectedNode.id);
      const newEdgesRaw = edges.filter(e => e.source !== selectedNode.id && e.target !== selectedNode.id);
      const newEdges = preprocessEdges(newEdgesRaw, newNodes);
      setNodesState(newNodes);
      setEdgesState(newEdges);
      syncLocalChanges(newNodes, newEdges);
      setSelectedNode(null);
    }
  };

  const handleDeleteSelectedEdge = () => {
    if (isReadOnly || !selectedEdge) return;
    if (window.confirm(t("确认要删除连线吗？"))) {
      const newEdgesRaw = edges.filter(e => e.id !== selectedEdge.id);
      const newEdges = preprocessEdges(newEdgesRaw, nodes);
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

  const handleNodeZIndexChange = (value) => {
    if (isReadOnly || !selectedNode) return;
    const updatedNodes = nodes.map(n => {
      if (n.id === selectedNode.id) {
        const updatedNode = {
          ...n,
          zIndex: value
        };
        setSelectedNode(updatedNode);
        return updatedNode;
      }
      return n;
    });
    setNodesState(updatedNodes);
    syncLocalChanges(updatedNodes, edges);
  };

  const handleEdgeDataChange = (field, value) => {
    if (isReadOnly || !selectedEdge) return;
    const updatedEdgesRaw = edges.map(e => {
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
    const updatedEdges = preprocessEdges(updatedEdgesRaw, nodes);
    setEdgesState(updatedEdges);
    syncLocalChanges(nodes, updatedEdges);
  };

  const handleAIChatSend = async () => {
    if (isReadOnly || !aiPrompt.trim() || isChatSending) return;
    const userMsg = aiPrompt.trim();
    setAiPrompt('');
    setChatHistory(prev => [...prev, { sender: 'user', text: userMsg }]);
    setIsChatSending(true);
    try {
      const res = await triggerAIChat(userMsg, chatHistory);
      if (res && res.reply) {
        setChatHistory(prev => [...prev, { sender: 'ai', text: res.reply }]);
        setLastSuggestedSnapshot(res.snapshot_json);
      }
    } catch {
      setChatHistory(prev => [...prev, { sender: 'ai', text: t('抱歉，与 AI 助手沟通失败，请检查网络或配置。') }]);
    } finally {
      setIsChatSending(false);
    }
  };

  const handleApplySnapshotClick = async () => {
    if (isReadOnly || !lastSuggestedSnapshot) return;
    if (window.confirm(t('一键生成 DFD 图会清空您当前的画布并用 AI 生成的图画上去！确定继续吗？'))) {
      setAiLoading(true);
      const res = await applySnapshot(lastSuggestedSnapshot);
      setAiLoading(false);
      if (res) {
        alert('AI 画图拓扑应用成功，您可以继续在画布上修改了！');
      }
    }
  };

  const handleManualSave = () => {
    saveDiagram();
  };

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
            <ArrowLeft size={14} /> {t("返回工作台")}
          </button>

          <div>
            <h3 style={{ fontSize: '15px', fontWeight: '600', color: 'var(--text-primary)' }}>
              {currentDiagram?.title || t('功能数据流图')}
            </h3>
            <span style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>
              {t("版本")}: v{versionNo} • {t("子域控")}: {activeDomain?.name}
            </span>
          </div>
        </div>

        {/* Sync / Lock Indicators */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '14px' }}>
          {isOffline && (
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', color: 'var(--warning)', fontSize: '12px', background: 'rgba(217, 119, 6, 0.08)', padding: '6px 12px', borderRadius: '6px', border: '1px solid rgba(217, 119, 6, 0.2)' }}>
              <WifiOff size={14} />
              <span>{t("脱网离线模式 (数据将暂存在浏览器)")}</span>
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
                  ? (language === 'zh'
                      ? `👁️ 只读查看中 (当前正在由 [${lockedBy}] 编辑，共 ${activeUsers?.length || 1} 人在场)` 
                      : `👁️ Read-only View ([${lockedBy}] is editing, ${activeUsers?.length || 1} user(s) present)`)
                  : (activeUsers?.length <= 1 
                      ? t('✅ 独占编辑中 (当前仅您一人在画布中)') 
                      : (language === 'zh'
                          ? `✍️ 协同编辑中 (您是当前编辑人，其他 ${activeUsers.length - 1} 人只读)`
                          : `✍️ Collaborative Edit (You are editing, other ${activeUsers.length - 1} user(s) read-only)`))}
              </span>
            </div>
          ) : (
            <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>{t("正在连接协同锁定服务...")}</span>
          )}

          <button
            onClick={handleManualSave}
            className="btn btn-secondary"
            style={{ padding: '8px 12px' }}
            disabled={isReadOnly || loading}
          >
            {loading ? <div className="spinner"></div> : <Save size={14} />}
            <span>{t("保存")}</span>
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
            {t("STRIDE 绘图组件箱")}
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
                <span style={{ fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)' }}>{t("外部实体 (Entity)")}</span>
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
                <span style={{ fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)' }}>{t("处理过程 (Process)")}</span>
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
                <span style={{ fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)' }}>{t("数据存储 (Storage)")}</span>
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
                <span style={{ fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)' }}>{t("物理边界 (Boundary)")}</span>
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
            <p>{t("提示：点击组件添加到画布，选中组件可拖动、在边缘进行缩放、连线或修改属性。")}</p>
          </div>
        </div>

        {/* Center Flow Canvas */}
        <div style={{ flexGrow: 1, height: '100%', background: '#f8fafc' }}>
          <ReactFlow
            nodes={nodes}
            edges={edges.map(e => ({
              ...e,
              updatable: !isReadOnly && selectedEdge && selectedEdge.id === e.id
            }))}
            onNodesChange={onNodesChangeHandler}
            onEdgesChange={onEdgesChangeHandler}
            onConnect={onConnect}
            onEdgeUpdate={onEdgeUpdate}
            onNodeClick={onNodeClick}
            onEdgeClick={onEdgeClick}
            onPaneClick={onPaneClick}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            fitView
            elevateEdgesOnSelect={true}
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
              {t("节点属性设置")}
            </h3>

            <div className="input-group">
              <span className="input-label">{t("资产节点名称")} <span style={{ color: 'var(--accent)' }}>*</span></span>
              <input
                type="text"
                className="input-field"
                value={selectedNode.data.name || ''}
                onChange={(e) => handleNodeDataChange('name', e.target.value)}
                disabled={isReadOnly}
              />
            </div>

            <div className="input-group">
              <span className="input-label">{t("字体大小")} (Font Size): {selectedNode.data.fontSize || 11}px</span>
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
              <span className="input-label">{t("通信协议 (Protocol)")}</span>
              <input
                type="text"
                className="input-field"
                placeholder={t("例如: CAN, LIN, Ethernet, HTTP")}
                value={selectedNode.data.protocol || ''}
                onChange={(e) => handleNodeDataChange('protocol', e.target.value)}
                disabled={isReadOnly}
              />
            </div>

            <div className="input-group">
              <span className="input-label">{t("功能描述")}</span>
              <textarea
                className="input-field"
                rows={3}
                placeholder={t("描述此安全分析项的具体工作...")}
                value={selectedNode.data.description || ''}
                onChange={(e) => handleNodeDataChange('description', e.target.value)}
                style={{ resize: 'none' }}
                disabled={isReadOnly}
              />
            </div>

            <div className="input-group" style={{ marginBottom: '24px' }}>
              <span className="input-label">{t("分析备注")}</span>
              <input
                type="text"
                className="input-field"
                value={selectedNode.data.remarks || ''}
                onChange={(e) => handleNodeDataChange('remarks', e.target.value)}
                disabled={isReadOnly}
              />
            </div>

            <div className="input-group" style={{ marginBottom: '24px' }}>
              <span className="input-label">{t("图层顺序")}</span>
              <div style={{ display: 'flex', gap: '6px', marginBottom: '8px' }}>
                <button
                  type="button"
                  title={t("置于顶层")}
                  onClick={() => {
                    const zIndexes = nodes.map(n => n.zIndex || 0);
                    const maxZ = zIndexes.length > 0 ? Math.max(...zIndexes) : 0;
                    handleNodeZIndexChange(maxZ + 1);
                  }}
                  className="btn btn-secondary"
                  style={{ flexGrow: 1, padding: '8px 4px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '4px', fontSize: '10px', minWidth: '60px' }}
                  disabled={isReadOnly}
                >
                  <ChevronsUp size={14} style={{ color: 'var(--primary)' }} />
                  <span>{t("置于顶层")}</span>
                </button>
                <button
                  type="button"
                  title={t("上移一层")}
                  onClick={() => {
                    const currentZ = selectedNode.zIndex || 0;
                    handleNodeZIndexChange(currentZ + 1);
                  }}
                  className="btn btn-secondary"
                  style={{ flexGrow: 1, padding: '8px 4px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '4px', fontSize: '10px', minWidth: '60px' }}
                  disabled={isReadOnly}
                >
                  <ArrowUp size={14} />
                  <span>{t("上移一层")}</span>
                </button>
                <button
                  type="button"
                  title={t("下移一层")}
                  onClick={() => {
                    const currentZ = selectedNode.zIndex || 0;
                    handleNodeZIndexChange(currentZ - 1);
                  }}
                  className="btn btn-secondary"
                  style={{ flexGrow: 1, padding: '8px 4px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '4px', fontSize: '10px', minWidth: '60px' }}
                  disabled={isReadOnly}
                >
                  <ArrowDown size={14} />
                  <span>{t("下移一层")}</span>
                </button>
                <button
                  type="button"
                  title={t("置于底层")}
                  onClick={() => {
                    const zIndexes = nodes.map(n => n.zIndex || 0);
                    const minZ = zIndexes.length > 0 ? Math.min(...zIndexes) : 0;
                    handleNodeZIndexChange(minZ - 1);
                  }}
                  className="btn btn-secondary"
                  style={{ flexGrow: 1, padding: '8px 4px', display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '4px', fontSize: '10px', minWidth: '60px' }}
                  disabled={isReadOnly}
                >
                  <ChevronsDown size={14} style={{ color: 'var(--accent)' }} />
                  <span>{t("置于底层")}</span>
                </button>
              </div>
              
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <span style={{ fontSize: '12px', color: 'var(--text-secondary)', whiteSpace: 'nowrap' }}>{t("数值设定")}:</span>
                <input
                  type="number"
                  className="input-field"
                  style={{ height: '32px', padding: '4px 8px', fontSize: '12px', margin: 0 }}
                  value={selectedNode.zIndex ?? ''}
                  placeholder={t("默认 (数值越大越靠前)")}
                  onChange={(e) => {
                    const val = e.target.value === '' ? undefined : parseInt(e.target.value);
                    handleNodeZIndexChange(val);
                  }}
                  disabled={isReadOnly}
                />
              </div>
            </div>

            {!isReadOnly && (
              <button
                onClick={handleDeleteSelectedNode}
                className="btn btn-danger"
                style={{ width: '100%' }}
              >
                {t("删除当前节点")}
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
              {t("连线属性设置")}
            </h3>

            <div className="input-group">
              <span className="input-label">{t("数据流名称")}</span>
              <input
                type="text"
                className="input-field"
                value={selectedEdge.data?.name || ''}
                onChange={(e) => handleEdgeDataChange('name', e.target.value)}
                disabled={isReadOnly}
              />
            </div>

            <div className="input-group">
              <span className="input-label">{t("通信协议 (Protocol)")}</span>
              <input
                type="text"
                className="input-field"
                placeholder={t("例如: HTTPS, MQTT, SOME/IP, CAN")}
                value={selectedEdge.data?.protocol || ''}
                onChange={(e) => handleEdgeDataChange('protocol', e.target.value)}
                disabled={isReadOnly}
              />
            </div>

            <div className="input-group" style={{ marginBottom: '24px' }}>
              <span className="input-label">{t("传输的数据信息 (Data Info)")}</span>
              <input
                type="text"
                className="input-field"
                placeholder={t("例如: 诊断帧, OTA包, 控制报文")}
                value={selectedEdge.data?.transmitted_info || ''}
                onChange={(e) => handleEdgeDataChange('transmitted_info', e.target.value)}
                disabled={isReadOnly}
              />
            </div>

            <div className="input-group" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px', marginBottom: '16px' }}>
              <span className="input-label" style={{ marginBottom: 0, fontSize: '13px' }}>{t("智能自适应路由")}</span>
              <input
                type="checkbox"
                style={{ width: '18px', height: '18px', cursor: 'pointer', accentColor: 'var(--primary)' }}
                checked={selectedEdge.data?.autoRoute !== false}
                onChange={(e) => handleEdgeDataChange('autoRoute', e.target.checked)}
                disabled={isReadOnly}
              />
            </div>
            <p style={{ fontSize: '11px', color: 'var(--text-secondary)', marginTop: '-12px', marginBottom: '20px', lineHeight: '1.3' }}>
              {t("自动优化并平滑连接点，拖动节点时自动选择最近端点")}
            </p>

            <div className="input-group" style={{ marginBottom: '16px' }}>
              <span className="input-label">{t("线条类型")}</span>
              <select
                className="input-field"
                style={{ cursor: 'pointer', width: '100%', height: '38px', padding: '0 12px', borderRadius: '6px', border: '1px solid var(--border-color)', background: '#fff', color: 'var(--text-primary)' }}
                value={selectedEdge.data?.lineStyle || 'bezier'}
                onChange={(e) => handleEdgeDataChange('lineStyle', e.target.value)}
                disabled={isReadOnly}
              >
                <option value="bezier">{t("贝塞尔曲线")}</option>
                <option value="smoothstep">{t("平滑折线")}</option>
                <option value="straight">{t("直线")}</option>
              </select>
            </div>

            {(selectedEdge.data?.lineStyle || 'bezier') === 'bezier' && (
              <div className="input-group" style={{ marginBottom: '24px' }}>
                <span className="input-label">{t("弯曲度 (Curvature)")}: {selectedEdge.data?.curvature !== undefined ? selectedEdge.data.curvature : 0.25}</span>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                  <input
                    type="range"
                    min="0.0"
                    max="0.8"
                    step="0.05"
                    style={{ flexGrow: 1, accentColor: 'var(--primary)', cursor: 'pointer' }}
                    value={selectedEdge.data?.curvature !== undefined ? selectedEdge.data.curvature : 0.25}
                    onChange={(e) => handleEdgeDataChange('curvature', parseFloat(e.target.value))}
                    disabled={isReadOnly}
                  />
                </div>
              </div>
            )}

            {selectedEdge.data?.lineStyle === 'smoothstep' && (
              <div className="input-group" style={{ marginBottom: '24px' }}>
                <span className="input-label">{t("圆角大小 (Border Radius)")}: {selectedEdge.data?.borderRadius !== undefined ? selectedEdge.data.borderRadius : 12}px</span>
                <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                  <input
                    type="range"
                    min="0"
                    max="30"
                    step="1"
                    style={{ flexGrow: 1, accentColor: 'var(--primary)', cursor: 'pointer' }}
                    value={selectedEdge.data?.borderRadius !== undefined ? selectedEdge.data.borderRadius : 12}
                    onChange={(e) => handleEdgeDataChange('borderRadius', parseInt(e.target.value))}
                    disabled={isReadOnly}
                  />
                </div>
              </div>
            )}

            {!isReadOnly && (
              <button
                onClick={handleDeleteSelectedEdge}
                className="btn btn-danger"
                style={{ width: '100%' }}
              >
                {t("删除当前连线")}
              </button>
            )}
          </div>
        )}

        {/* AI Drawing Drawer sidebar (If no node or edge selected) */}
        {!selectedNode && !selectedEdge && (
          <div className="glass" style={{
            width: '320px',
            borderRadius: '0',
            borderLeft: '1px solid var(--border-color)',
            padding: '20px 16px',
            background: 'var(--drawer-bg)',
            zIndex: 10,
            display: 'flex',
            flexDirection: 'column',
            height: '100%',
            boxSizing: 'border-box'
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '6px', marginBottom: '8px' }}>
              <Sparkles size={16} style={{ color: 'var(--primary)' }} />
              <h3 style={{ fontSize: '16px', fontWeight: '600', color: 'var(--text-primary)' }}>{t("AI 一键画图助手")}</h3>
            </div>
            
            {/* Chat History Container */}
            <div style={{
              flexGrow: 1,
              border: '1px solid rgba(99, 102, 241, 0.15)',
              borderRadius: '12px',
              padding: '16px 12px',
              background: 'rgba(255, 255, 255, 0.45)',
              boxShadow: 'inset 0 2px 8px rgba(99, 102, 241, 0.03)',
              overflowY: 'auto',
              display: 'flex',
              flexDirection: 'column',
              gap: '12px',
              minHeight: '200px',
              maxHeight: 'calc(100vh - 420px)',
              marginBottom: '12px'
            }}>
              {chatHistory.map((msg, index) => (
                <div 
                  key={index} 
                  style={{ 
                    display: 'flex', 
                    flexDirection: 'column',
                    alignItems: msg.sender === 'user' ? 'flex-end' : 'flex-start'
                  }}
                >
                  <span style={{ fontSize: '10px', color: 'var(--text-secondary)', marginBottom: '4px' }}>
                    {msg.sender === 'user' ? t('您') : t('AI 助理')}
                  </span>
                  <div style={{
                    alignSelf: msg.sender === 'user' ? 'flex-end' : 'flex-start',
                    background: msg.sender === 'user' 
                      ? 'linear-gradient(135deg, var(--primary) 0%, #4f46e5 100%)' 
                      : '#ffffff',
                    border: msg.sender === 'user' ? 'none' : '1px solid rgba(15, 23, 42, 0.06)',
                    color: msg.sender === 'user' ? '#fff' : 'var(--text-primary)',
                    borderRadius: msg.sender === 'user' ? '12px 12px 0 12px' : '12px 12px 12px 0',
                    padding: '8px 12px',
                    maxWidth: '90%',
                    fontSize: '12px',
                    lineHeight: '1.4',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-all',
                    boxShadow: msg.sender === 'user' 
                      ? '0 2px 8px rgba(99, 102, 241, 0.2)' 
                      : '0 2px 8px rgba(0, 0, 0, 0.04)',
                    transition: 'all 0.2s ease'
                  }}>
                    {msg.text}
                  </div>
                </div>
              ))}
              {isChatSending && (
                <div style={{ display: 'flex', gap: '6px', alignItems: 'center', color: 'var(--text-secondary)', fontSize: '12px' }}>
                  <div className="spinner" style={{ width: '12px', height: '12px' }}></div>
                  <span>{t("AI 正在思考设计中...")}</span>
                </div>
              )}
            </div>

            {/* Input and Buttons */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
              <div className="input-group" style={{ margin: 0 }}>
                <textarea
                  className="input-field"
                  rows={3}
                  placeholder={t("例如：修改上述方案，再加入一个诊断服务器...")}
                  value={aiPrompt}
                  onChange={(e) => setAiPrompt(e.target.value)}
                  style={{ resize: 'none', fontSize: '12px', padding: '10px' }}
                  disabled={isReadOnly || isChatSending || aiLoading}
                />
              </div>

              <div style={{ display: 'flex', gap: '8px' }}>
                <button
                  onClick={handleAIChatSend}
                  className="btn btn-secondary"
                  style={{ flexGrow: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '4px', height: '36px', fontSize: '13px' }}
                  disabled={isReadOnly || isChatSending || !aiPrompt.trim()}
                >
                  <Send size={13} />
                  <span>{t("发送需求")}</span>
                </button>
              </div>

              <button
                onClick={handleApplySnapshotClick}
                className="btn btn-primary"
                style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '6px', height: '38px', fontSize: '13px' }}
                disabled={isReadOnly || aiLoading || !lastSuggestedSnapshot}
              >
                {aiLoading ? (
                  <>
                    <div className="spinner" style={{ width: '14px', height: '14px' }}></div>
                    <span>{t("正在绘制中...")}</span>
                  </>
                ) : (
                  <>
                    <Sparkles size={14} />
                    <span>{t("一键生成 DFD 图")}</span>
                  </>
                )}
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Save Conflict Alert Modal (BR-16) */}
      {error && error.includes(t('保存冲突')) && (
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
              {t("乐观锁冲突！")}
            </h3>
            <p style={{ color: 'var(--text-secondary)', fontSize: '13px', lineHeight: '1.5', marginBottom: '24px' }}>
              {t("检测到此画布已有其他成员进行了保存提交。\n              为避免覆盖他人的工作，系统已自动将您的修改备份至浏览器的 LocalStorage 中。请立即刷新页面以合并。")}
            </p>

            <button 
              onClick={() => window.location.reload()} 
              className="btn btn-primary"
              style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '6px' }}
            >
              <RefreshCw size={14} /> {t("刷新重新加载")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
