import { useI18n } from '../stores/i18nStore';
import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import {
  ReactFlow,
  ReactFlowProvider,
  MiniMap,
  Controls,
  Background,
  useNodesState,
  useEdgesState,
  useReactFlow,
  addEdge,
  MarkerType,
  reconnectEdge,
  getBezierPath,
  getSmoothStepPath,
  getStraightPath,
  EdgeLabelRenderer,
  SelectionMode,
  ConnectionMode
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import { useCanvasStore } from '../stores/canvasStore';
import { useAuthStore } from '../stores/authStore';
import { useProjectStore } from '../stores/projectStore';
import { 
  ArrowLeft, Save, Sparkles, 
  Info, Cpu, ShieldAlert, WifiOff, RefreshCw, Send,
  ArrowUp, ArrowDown, ChevronsUp, ChevronsDown
} from 'lucide-react';

import CustomDfdNode from './CustomDfdNode';
import { User, Database, Shield, Link } from 'lucide-react';

// Palette swatches for the toolbox previews, kept in sync with the colours used
// by CustomDfdNode so the shape shown in the palette is what gets drawn.
const TOOLBOX_SHAPES = {
  entity: { stroke: '#6366f1', fill: 'rgba(99, 102, 241, 0.12)' },
  process: { stroke: '#d946ef', fill: 'rgba(217, 70, 239, 0.12)' },
  storage: { stroke: '#10b981', fill: 'rgba(16, 185, 129, 0.12)' },
  boundary: { stroke: '#f43f5e', fill: 'rgba(244, 63, 94, 0.05)' },
  interface: { stroke: '#d97706', fill: 'rgba(217, 119, 6, 0.12)' }
};

function ToolboxPreview({ type }) {
  const { stroke, fill } = TOOLBOX_SHAPES[type];

  return (
    <div className="toolbox-preview">
      <svg width="68" height="34" viewBox="0 0 68 34" aria-hidden="true">
        {type === 'entity' && (
          <rect x="6" y="6" width="56" height="22" rx="4" fill={fill} stroke={stroke} strokeWidth="1.6" />
        )}
        {type === 'process' && (
          <circle cx="34" cy="17" r="12.5" fill={fill} stroke={stroke} strokeWidth="1.6" />
        )}
        {type === 'storage' && (
          <g>
            <rect x="6" y="8" width="56" height="18" fill={fill} />
            <line x1="6" y1="8" x2="62" y2="8" stroke={stroke} strokeWidth="2" strokeLinecap="round" />
            <line x1="6" y1="26" x2="62" y2="26" stroke={stroke} strokeWidth="2" strokeLinecap="round" />
          </g>
        )}
        {type === 'boundary' && (
          <rect
            x="6"
            y="6"
            width="56"
            height="22"
            rx="5"
            fill={fill}
            stroke={stroke}
            strokeWidth="1.6"
            strokeDasharray="5 3"
          />
        )}
        {type === 'interface' && (
          <g>
            <rect x="6" y="6" width="56" height="22" rx="4" fill={fill} stroke={stroke} strokeWidth="1.6" />
            <line x1="14" y1="17" x2="48" y2="17" stroke={stroke} strokeWidth="1.4" strokeDasharray="4 3" opacity="0.75" />
          </g>
        )}
      </svg>
    </div>
  );
}

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
  let edgePath;
  let labelX;
  let labelY;

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
    const isHorizontal = sourcePosition === 'left' || sourcePosition === 'right';
    
    if (curvature !== 0) {
      if (isHorizontal) {
        // Horizontal connection: offset Y coordinate to bend up/down
        const dx = targetX - sourceX;
        const cX1 = sourceX + (sourcePosition === 'right' ? Math.max(30, Math.abs(dx) * 0.25) : -Math.max(30, Math.abs(dx) * 0.25));
        const cY1 = sourceY + curvature * 120;
        const cX2 = targetX + (targetPosition === 'right' ? Math.max(30, Math.abs(dx) * 0.25) : -Math.max(30, Math.abs(dx) * 0.25));
        const cY2 = targetY + curvature * 120;
        
        edgePath = `M ${sourceX} ${sourceY} C ${cX1} ${cY1}, ${cX2} ${cY2}, ${targetX} ${targetY}`;
        labelX = 0.125 * sourceX + 0.375 * cX1 + 0.375 * cX2 + 0.125 * targetX;
        labelY = 0.125 * sourceY + 0.375 * cY1 + 0.375 * cY2 + 0.125 * targetY;
      } else {
        // Vertical connection: offset X coordinate to bend left/right
        const dy = targetY - sourceY;
        const cX1 = sourceX + curvature * 120;
        const cY1 = sourceY + (sourcePosition === 'bottom' ? Math.max(30, Math.abs(dy) * 0.25) : -Math.max(30, Math.abs(dy) * 0.25));
        const cX2 = targetX + curvature * 120;
        const cY2 = targetY + (targetPosition === 'bottom' ? Math.max(30, Math.abs(dy) * 0.25) : -Math.max(30, Math.abs(dy) * 0.25));
        
        edgePath = `M ${sourceX} ${sourceY} C ${cX1} ${cY1}, ${cX2} ${cY2}, ${targetX} ${targetY}`;
        labelX = 0.125 * sourceX + 0.375 * cX1 + 0.375 * cX2 + 0.125 * targetX;
        labelY = 0.125 * sourceY + 0.375 * cY1 + 0.375 * cY2 + 0.125 * targetY;
      }
    } else {
      [edgePath, labelX, labelY] = getBezierPath({
        sourceX,
        sourceY,
        sourcePosition,
        targetX,
        targetY,
        targetPosition,
        curvature: 0
      });
    }
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

const EDGE_TYPES_MAP = {
  parallel: ParallelBezierEdge,
  default: ParallelBezierEdge
};

const normalizeHandleId = (handleId, targetType) => {
  if (!handleId) return handleId;
  const parts = String(handleId).split('-');
  if (parts.length < 2) return handleId;
  const prefix = targetType === 'source' ? 's' : 't';
  return `${prefix}-${parts.slice(1).join('-')}`;
};

const SIDE_ORDER = ['top', 'right', 'bottom', 'left'];

const handleSide = (handleId) => {
  const parts = String(handleId || '').split('-');
  return parts.length >= 2 ? parts.slice(1).join('-') : '';
};

const nextSide = (side) => {
  const index = SIDE_ORDER.indexOf(side);
  return SIDE_ORDER[(index + 1) % SIDE_ORDER.length] || 'right';
};

const preprocessEdges = (edgesList, nodesList = []) => {
  if (!edgesList) return [];
  
  // 1. Auto-routing calculation (opt-in only: user must enable it explicitly)
  const routedEdges = edgesList.map(edge => {
    const sourceNode = nodesList.find(n => n.id === edge.source);
    const targetNode = nodesList.find(n => n.id === edge.target);

    if (!sourceNode || !targetNode) {
      return edge;
    }

    // Route only when explicitly enabled, or when the edge has no handle info
    // (AI-generated / legacy edges), otherwise respect the user's chosen handles
    const autoRouteEnabled = edge.data?.autoRoute === true || !edge.sourceHandle || !edge.targetHandle;

    // Handle self-loop
    if (edge.source === edge.target) {
      if (autoRouteEnabled) {
        return {
          ...edge,
          sourceHandle: 's-top',
          targetHandle: 't-right',
          data: { ...edge.data, autoRoute: true }
        };
      }
      const sourceHandle = normalizeHandleId(edge.sourceHandle, 'source') || 's-top';
      let targetHandle = normalizeHandleId(edge.targetHandle, 'target') || 't-right';
      if (handleSide(sourceHandle) === handleSide(targetHandle)) {
        targetHandle = `t-${nextSide(handleSide(sourceHandle))}`;
      }
      return {
        ...edge,
        sourceHandle,
        targetHandle
      };
    }

    if (!autoRouteEnabled) {
      return edge;
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

    let sourceHandle;
    let targetHandle;

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
      targetHandle,
      data: { ...edge.data, autoRoute: true }
    };
  });

  // 2. Parallel offset calculation (independent from auto-routing)
  const groups = {};
  routedEdges.forEach(edge => {
    const nodesPair = [edge.source, edge.target].sort().join('-');
    const geometry = [
      `${edge.source}:${handleSide(edge.sourceHandle)}`,
      `${edge.target}:${handleSide(edge.targetHandle)}`
    ].sort().join('|');
    const key = `${nodesPair}::${geometry}`;
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

const NODE_TYPES_MAP = {
  entity: CustomDfdNode,
  process: CustomDfdNode,
  storage: CustomDfdNode,
  boundary: CustomDfdNode,
  interface: CustomDfdNode
};

// Identifies the editor instance that currently owns the canvas session. Exit →
// immediate re-entry reuses the live session, so a cleanup left over from the
// previous instance must not tear it down once its pending save settles.
let editorOwnerToken = 0;

export default function DfdEditor(props) {
  return (
    <ReactFlowProvider>
      <DfdEditorCanvas {...props} />
    </ReactFlowProvider>
  );
}

function DfdEditorCanvas({ setPage, diagramId }) {
  // 用 useMemo 提供稳定引用，消除 React Flow v11 + StrictMode 下的 #002 误报
  const nodeTypes = useMemo(() => NODE_TYPES_MAP, []);
  const edgeTypes = useMemo(() => EDGE_TYPES_MAP, []);
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
    openDiagramById,
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
  const { getViewport } = useReactFlow();

  const [selectedNode, setSelectedNode] = useState(null);
  const [selectedEdge, setSelectedEdge] = useState(null);

  const undoStackRef = useRef([]);
  const redoStackRef = useRef([]);
  const connectStartRef = useRef(null);
  const MAX_UNDO = 50;

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

  // Latest local graph + dirty flag, used to flush pending edits before leaving the editor
  const latestGraphRef = useRef({ nodes: [], edges: [] });
  const dirtyRef = useRef(false);

  useEffect(() => {
    latestGraphRef.current = { nodes, edges };
  }, [nodes, edges]);

  // Sync state between zustand store and reactflow local state
  useEffect(() => {
    if (!diagramId || !user) {
      return;
    }

    // This instance now owns the session. The cleanup of a previous instance
    // (e.g. the one that unmounted when the user clicked "返回工作台") may still
    // be flushing a save and must not close the canvas once it settles.
    editorOwnerToken += 1;
    const ownerToken = editorOwnerToken;

    const activeDiag = useCanvasStore.getState().currentDiagram;
    if (!activeDiag || activeDiag.id !== diagramId) {
      // Always load the authoritative snapshot from the server; the local
      // diagrams list may be stale or not loaded yet.
      openDiagramById(diagramId, user.username);
    }

    return () => {
      // Flush any pending debounced edit and save before tearing down the session
      if (syncTimeoutRef.current) {
        clearTimeout(syncTimeoutRef.current);
        syncTimeoutRef.current = null;
      }
      const store = useCanvasStore.getState();
      const startSessionId = store.sessionId;

      if (!dirtyRef.current && !store.dirty) {
        // Nothing pending locally and nothing unsaved in the store
        if (ownerToken === editorOwnerToken) store.closeDiagram();
        return;
      }

      if (dirtyRef.current) {
        const latest = latestGraphRef.current;
        store.setNodes(latest.nodes);
        store.setEdges(latest.edges);
        dirtyRef.current = false;
      }

      // flushSave is async and needs currentDiagram to still be set, so the
      // session is only torn down after the save settles. The sessionId guard
      // prevents a stale cleanup from closing a newly opened diagram.
      store.flushSave().finally(() => {
        // Another editor instance mounted onto this diagram in the meantime
        // (exit followed by an immediate re-entry): the session is still live
        // and belongs to it now, so closing it here would blank the canvas.
        if (ownerToken !== editorOwnerToken) return;
        const s = useCanvasStore.getState();
        if (s.sessionId === startSessionId) {
          s.closeDiagram();
        }
      });
    };
  }, [diagramId, user, openDiagramById]);

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
  }, [storeNodes, storeEdges, setNodesState, setEdgesState]);

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
  }, [setOfflineStatus]);

  // Update store when local nodes/edges change (debounced in store)
  const syncLocalChanges = useCallback((newNodes, newEdges) => {
    if (isReadOnly) return;
    setNodes(newNodes);
    setEdges(newEdges);
    dirtyRef.current = false;
  }, [isReadOnly, setNodes, setEdges]);

  // Debounced store sync to eliminate drag latency / resistance (feedback loop)
  const debouncedSync = useCallback((newNodes, newEdges) => {
    if (isReadOnly) return;
    dirtyRef.current = true;
    if (syncTimeoutRef.current) {
      clearTimeout(syncTimeoutRef.current);
    }
    syncTimeoutRef.current = setTimeout(() => {
      syncLocalChanges(newNodes, newEdges);
    }, 200); // Wait 200ms after user stops dragging to sync to Zustand store
  }, [isReadOnly, syncLocalChanges]);

  // Flush pending edits when the page is hidden / unloaded (refresh, tab close, navigation)
  useEffect(() => {
    const persistPending = () => {
      const store = useCanvasStore.getState();
      if (isReadOnly) return;
      if (!dirtyRef.current && !store.dirty) return;
      if (syncTimeoutRef.current) {
        clearTimeout(syncTimeoutRef.current);
        syncTimeoutRef.current = null;
      }
      if (dirtyRef.current) {
        const latest = latestGraphRef.current;
        syncLocalChanges(latest.nodes, latest.edges);
      }
      store.flushSave();
    };

    const handleVisibilityChange = () => {
      if (document.visibilityState === 'hidden') persistPending();
    };

    window.addEventListener('pagehide', persistPending);
    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => {
      window.removeEventListener('pagehide', persistPending);
      document.removeEventListener('visibilitychange', handleVisibilityChange);
    };
  }, [isReadOnly, syncLocalChanges]);

  // Snap-to-grid: round position to nearest 20px
  const SNAP_GRID = 20;

  const pushUndo = useCallback((nodesSnapshot, edgesSnapshot) => {
    undoStackRef.current.push({ nodes: nodesSnapshot, edges: edgesSnapshot });
    if (undoStackRef.current.length > MAX_UNDO) undoStackRef.current.shift();
    redoStackRef.current = [];
  }, []);

  const handleUndo = useCallback(() => {
    const snapshot = undoStackRef.current.pop();
    if (!snapshot) return;
    redoStackRef.current.push({ nodes: [...nodes], edges: [...edges] });
    setNodesState(snapshot.nodes);
    setEdgesState(preprocessEdges(snapshot.edges, snapshot.nodes));
    syncLocalChanges(snapshot.nodes, snapshot.edges);
  }, [nodes, edges, setNodesState, setEdgesState, syncLocalChanges]);

  const handleRedo = useCallback(() => {
    const snapshot = redoStackRef.current.pop();
    if (!snapshot) return;
    undoStackRef.current.push({ nodes: [...nodes], edges: [...edges] });
    setNodesState(snapshot.nodes);
    setEdgesState(preprocessEdges(snapshot.edges, snapshot.nodes));
    syncLocalChanges(snapshot.nodes, snapshot.edges);
  }, [nodes, edges, setNodesState, setEdgesState, syncLocalChanges]);

  const onConnectStart = useCallback((event, { nodeId }) => {
    connectStartRef.current = nodeId || null;
  }, []);

  const onConnectEnd = useCallback(() => {
    connectStartRef.current = null;
  }, []);

  const onConnect = useCallback((params) => {
    if (isReadOnly) return;
    pushUndo([...nodes], [...edges]);

    const startNodeId = connectStartRef.current;
    const isReversed = startNodeId && startNodeId === params.target && startNodeId !== params.source;

    const connection = {
      ...params,
      source: isReversed ? params.target : params.source,
      target: isReversed ? params.source : params.target,
      sourceHandle: normalizeHandleId(isReversed ? params.targetHandle : params.sourceHandle, 'source'),
      targetHandle: normalizeHandleId(isReversed ? params.sourceHandle : params.targetHandle, 'target')
    };

    const newEdgesRaw = addEdge({
      ...connection,
      id: `e-${Date.now()}`,
      label: t('数据流'),
      data: { name: t('数据流'), transmitted_info: '', protocol: '', autoRoute: false },
      markerEnd: { type: MarkerType.ArrowClosed }
    }, edges);
    const newEdges = preprocessEdges(newEdgesRaw, nodes);
    setEdgesState(newEdges);
    syncLocalChanges(nodes, newEdges);
  }, [edges, nodes, isReadOnly, setEdgesState, syncLocalChanges, t, pushUndo]);

  const onEdgeUpdate = useCallback((oldEdge, newConnection) => {
    if (isReadOnly) return;
    pushUndo([...nodes], [...edges]);
    setEdgesState((els) => {
      const updatedRaw = reconnectEdge(oldEdge, newConnection, els);
      const updated = preprocessEdges(updatedRaw, nodes);
      syncLocalChanges(nodes, updated);
      const newEdgeObj = updated.find(e => e.id === oldEdge.id);
      if (newEdgeObj) {
        setSelectedEdge(newEdgeObj);
      }
      return updated;
    });
  }, [nodes, edges, isReadOnly, setEdgesState, syncLocalChanges, pushUndo]);

  const onNodesChangeHandler = useCallback((changes) => {
    if (isReadOnly) return;
    const hasRemove = changes.some(c => c.type === 'remove');
    if (hasRemove) {
      pushUndo([...nodes], [...edges]);
      setSelectedNode(null);
    }
    onNodesChange(changes);
    
    setNodesState((currentNodes) => {
      setEdgesState((currentEdges) => {
        const processed = preprocessEdges(currentEdges, currentNodes);
        debouncedSync(currentNodes, processed);
        return processed;
      });
      return currentNodes;
    });
  }, [isReadOnly, debouncedSync, onNodesChange, setNodesState, setEdgesState, nodes, edges, pushUndo]);

  const onEdgesChangeHandler = useCallback((changes) => {
    if (isReadOnly) return;
    const hasRemove = changes.some(c => c.type === 'remove');
    if (hasRemove) {
      pushUndo([...nodes], [...edges]);
      setSelectedEdge(null);
    }
    onEdgesChange(changes);
    
    setNodesState((currentNodes) => {
      setEdgesState((currentEdges) => {
        const processed = preprocessEdges(currentEdges, currentNodes);
        debouncedSync(currentNodes, processed);
        return processed;
      });
      return currentNodes;
    });
  }, [isReadOnly, debouncedSync, onEdgesChange, setNodesState, setEdgesState, nodes, edges, pushUndo]);

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

  // Keyboard shortcuts: Ctrl+Z = undo, Ctrl+Shift+Z / Ctrl+Y = redo
  useEffect(() => {
    const handleKeyDown = (e) => {
      if (isReadOnly) return;
      if ((e.ctrlKey || e.metaKey) && e.key === 'z' && !e.shiftKey) {
        e.preventDefault();
        handleUndo();
      } else if ((e.ctrlKey || e.metaKey) && (e.key === 'y' || (e.key === 'z' && e.shiftKey))) {
        e.preventDefault();
        handleRedo();
      }
    };
    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [isReadOnly, handleUndo, handleRedo]);

  // Helper to add nodes at viewport center (with slight random offset)
  const handleAddNode = (type) => {
    if (isReadOnly) return;
    
    let typeName = t('外部实体');
    if (type === 'process') typeName = t('处理过程');
    if (type === 'storage') typeName = t('数据存储');
    if (type === 'boundary') typeName = t('物理边界');
    if (type === 'interface') typeName = t('接口');

    let width = 150;
    let height = 80;
    if (type === 'process') {
      width = 100;
      height = 100;
    } else if (type === 'boundary') {
      width = 280;
      height = 200;
    }

    // Place at viewport center + small random offset to avoid stacking
    const vp = getViewport();
    const centerX = (window.innerWidth * 0.5 - vp.x) / vp.zoom - width / 2;
    const centerY = (window.innerHeight * 0.4 - vp.y) / vp.zoom - height / 2;
    const jitterX = (Math.random() - 0.5) * 80;
    const jitterY = (Math.random() - 0.5) * 60;

    // Snap to grid
    const snapX = Math.round((centerX + jitterX) / SNAP_GRID) * SNAP_GRID;
    const snapY = Math.round((centerY + jitterY) / SNAP_GRID) * SNAP_GRID;

    const newNode = {
      id: `n-${Date.now()}`,
      type,
      position: { x: snapX, y: snapY },
      style: { width, height },
      data: { 
        name: language === 'zh' ? `新${typeName}` : `New ${typeName}`, 
        description: '', 
        protocol: type === 'boundary' ? '' : 'CAN', 
        remarks: '' 
      }
    };
    pushUndo([...nodes], [...edges]);
    const newNodes = [...nodes, newNode];
    setNodesState(newNodes);
    syncLocalChanges(newNodes, edges);
    setSelectedNode(newNode);
    setSelectedEdge(null);
  };

  const handleDeleteSelectedNode = () => {
    if (isReadOnly || !selectedNode) return;
    pushUndo([...nodes], [...edges]);
    const newNodes = nodes.filter(n => n.id !== selectedNode.id);
    const newEdgesRaw = edges.filter(e => e.source !== selectedNode.id && e.target !== selectedNode.id);
    const newEdges = preprocessEdges(newEdgesRaw, newNodes);
    setNodesState(newNodes);
    setEdgesState(newEdges);
    syncLocalChanges(newNodes, newEdges);
    setSelectedNode(null);
  };

  const handleDeleteSelectedEdge = () => {
    if (isReadOnly || !selectedEdge) return;
    pushUndo([...nodes], [...edges]);
    const newEdgesRaw = edges.filter(e => e.id !== selectedEdge.id);
    const newEdges = preprocessEdges(newEdgesRaw, nodes);
    setEdgesState(newEdges);
    syncLocalChanges(nodes, newEdges);
    setSelectedEdge(null);
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
    const confirmMessage = nodes.length > 0
      ? t('确定应用 AI 建议的拓扑结构吗？增量模式下将保留您手动调整的节点位置与未修改的拓扑。')
      : t('一键生成 DFD 图会清空您当前的画布并用 AI 生成的图画上去！确定继续吗？');
    if (window.confirm(confirmMessage)) {
      setAiLoading(true);
      const res = await applySnapshot(lastSuggestedSnapshot);
      setAiLoading(false);
      if (res) {
        alert(t('AI 画图拓扑应用成功，您可以继续在画布上修改了！'));
      }
    }
  };

  const handleManualSave = () => {
    saveDiagram();
  };

  const handleExitToWorkbench = () => {
    // Push any debounced edit into the store first so nothing is dropped
    if (syncTimeoutRef.current) {
      clearTimeout(syncTimeoutRef.current);
      syncTimeoutRef.current = null;
    }
    const latest = latestGraphRef.current;
    const current = useCanvasStore.getState();
    const latestEmpty = !latest.nodes.length && !latest.edges.length;
    const storeHasData = current.nodes.length > 0 || current.edges.length > 0;
    // Never let an empty local graph overwrite a loaded one (e.g. clicking
    // "back" before the canvas finished hydrating).
    if (!latestEmpty || !storeHasData) {
      syncLocalChanges(latest.nodes, latest.edges);
    }

    const store = useCanvasStore.getState();
    // Write the browser cache synchronously: it is instant, so leaving the page
    // never has to wait for the network.
    store.persistLocalCache();
    // Fire the server save without blocking navigation; the request keeps going
    // after unmount and the cache above guarantees the edits are not lost.
    store.saveDiagram();
    setPage('workbench');
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
            onClick={handleExitToWorkbench} 
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
              <ToolboxPreview type="entity" />
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
              <ToolboxPreview type="process" />
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
              <ToolboxPreview type="storage" />
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
              <ToolboxPreview type="boundary" />
            </div>

            {/* Interface Card */}
            <div
              onClick={() => !isReadOnly && handleAddNode('interface')}
              style={{
                padding: '12px',
                background: 'rgba(217, 119, 6, 0.05)',
                border: '1.5px solid rgba(217, 119, 6, 0.25)',
                borderRadius: '8px',
                cursor: isReadOnly ? 'not-allowed' : 'pointer',
                display: 'flex',
                flexDirection: 'column',
                gap: '8px',
                transition: 'all 0.2s ease',
                opacity: isReadOnly ? 0.5 : 1,
              }}
              className="toolbox-card interface-card"
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <Link size={15} style={{ color: '#fbbf24' }} />
                <span style={{ fontSize: '13px', fontWeight: '600', color: 'var(--text-primary)' }}>{t("接口 (Interface)")}</span>
              </div>
              <ToolboxPreview type="interface" />
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
              reconnectable: !isReadOnly && selectedEdge && selectedEdge.id === e.id
            }))}
            onNodesChange={onNodesChangeHandler}
            onEdgesChange={onEdgesChangeHandler}
            onConnect={onConnect}
            onConnectStart={onConnectStart}
            onConnectEnd={onConnectEnd}
            connectionMode={ConnectionMode.Loose}
            onReconnect={onEdgeUpdate}
            onNodeClick={onNodeClick}
            onEdgeClick={onEdgeClick}
            onPaneClick={onPaneClick}
            nodeTypes={nodeTypes}
            edgeTypes={edgeTypes}
            fitView
            snapToGrid={true}
            snapGrid={[SNAP_GRID, SNAP_GRID]}
            selectionMode={SelectionMode.Partial}
            multiSelectionKeyCode="Shift"
            deleteKeyCode={["Delete", "Backspace"]}
            panOnScroll={true}
            selectionOnDrag={true}
            selectNodesOnDrag={true}
            panOnDrag={[1, 2]}
            connectionLineStyle={{ stroke: 'var(--primary)', strokeWidth: 2 }}
            defaultEdgeOptions={{
              markerEnd: { type: MarkerType.ArrowClosed }
            }}
          >
            <Background color="#cbd5e1" gap={16} size={1} />
            <Controls style={{ background: '#fff', border: '1px solid #cbd5e1', color: '#1e293b' }} />
            <MiniMap 
              nodeStrokeColor={(n) => {
                if (n.type === 'entity') return '#6366f1';
                if (n.type === 'process') return '#d946ef';
                if (n.type === 'storage') return '#10b981';
                if (n.type === 'boundary') return '#f43f5e';
                if (n.type === 'interface') return '#d97706';
                return '#ccc';
              }}
              nodeColor={(n) => {
                if (n.type === 'entity') return 'rgba(99, 102, 241, 0.2)';
                if (n.type === 'process') return 'rgba(217, 70, 239, 0.2)';
                if (n.type === 'storage') return 'rgba(16, 185, 129, 0.2)';
                if (n.type === 'boundary') return 'rgba(244, 63, 150, 0.2)';
                if (n.type === 'interface') return 'rgba(217, 119, 6, 0.2)';
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
                checked={selectedEdge.data?.autoRoute === true}
                onChange={(e) => handleEdgeDataChange('autoRoute', e.target.checked)}
                disabled={isReadOnly}
              />
            </div>
            <p style={{ fontSize: '11px', color: 'var(--text-secondary)', marginTop: '-12px', marginBottom: '20px', lineHeight: '1.3' }}>
              {t("默认保留您手动选择的连接点；开启后按节点相对位置自动选择最近端点，拖动节点时会自动切换")}
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
                    min="-0.8"
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

      {/* Non-conflict save error notice (network / server / database lock) */}
      {error && !error.includes('保存冲突') && (
        <div
          className="glass"
          style={{
            position: 'fixed',
            bottom: '24px',
            left: '50%',
            transform: 'translateX(-50%)',
            padding: '10px 18px',
            borderRadius: '8px',
            border: '1px solid var(--border-color)',
            color: 'var(--accent)',
            fontSize: '13px',
            zIndex: 1000,
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            maxWidth: '80vw'
          }}
        >
          <Info size={14} />
          <span>{error}</span>
        </div>
      )}

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
              {t("乐观锁冲突！")}
            </h3>
            <p style={{ color: 'var(--text-secondary)', fontSize: '13px', lineHeight: '1.5', marginBottom: '24px' }}>
              {t("服务器上的画布版本与本地版本不一致，本次修改未能写入。\n              这通常发生在同一画布被多个浏览器标签页或更早的会话修改过。您的修改已自动备份至浏览器 LocalStorage，请刷新页面加载最新画布后再合并。")}
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