import { create } from 'zustand';
import axios from 'axios';
import { useAuthStore } from './authStore';

const API_BASE = '/api';
const WS_BASE = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}`;

const getHeaders = () => {
  const token = useAuthStore.getState().token;
  return token ? { Authorization: `Bearer ${token}` } : {};
};

export const useCanvasStore = create((set, get) => {
  let saveTimeout = null;

  return {
    diagrams: [],
    currentDiagram: null,
    nodes: [],
    edges: [],
    versionNo: 1,
    lockedBy: null,
    isReadOnly: false,
    ws: null,
    loading: false,
    error: null,
    isOffline: !navigator.onLine,
    isSaving: false,
    savePending: false,
    activeUsers: [],

    setOfflineStatus: (status) => {
      set({ isOffline: status });
      if (!status) {
        // Online: Sync offline changes if any
        get().syncOfflineData();
      }
    },

    fetchDiagrams: async (domainId) => {
      set({ loading: true, error: null });
      try {
        const response = await axios.get(`${API_BASE}/diagrams/domain/${domainId}`, { headers: getHeaders() });
        set({ diagrams: response.data, loading: false });
      } catch (err) {
        set({ error: err.response?.data?.detail || '获取功能图列表失败', loading: false });
      }
    },

    createDiagram: async (domainId, title) => {
      set({ loading: true, error: null });
      try {
        const response = await axios.post(
          `${API_BASE}/diagrams?domain_id=${domainId}&title=${encodeURIComponent(title)}`,
          {},
          { headers: getHeaders() }
        );
        set((state) => ({
          diagrams: [...state.diagrams, response.data],
          loading: false
        }));
        return response.data;
      } catch (err) {
        set({ error: err.response?.data?.detail || '创建功能图失败', loading: false });
        return null;
      }
    },

    deleteDiagram: async (diagramId) => {
      set({ loading: true, error: null });
      try {
        await axios.delete(`${API_BASE}/diagrams/${diagramId}`, { headers: getHeaders() });
        set((state) => ({
          diagrams: state.diagrams.filter((d) => d.id !== diagramId),
          currentDiagram: state.currentDiagram?.id === diagramId ? null : state.currentDiagram,
          loading: false
        }));
        return true;
      } catch (err) {
        set({ error: err.response?.data?.detail || '删除功能图失败', loading: false });
        return false;
      }
    },

    openDiagram: async (diagram, username) => {
      get().closeDiagram();

      let snapshot = { nodes: [], edges: [] };
      try {
        snapshot = JSON.parse(diagram.snapshot_json || '{}');
      } catch (e) {
        console.error("Failed to parse diagram snapshot JSON", e);
      }

      set({
        currentDiagram: diagram,
        nodes: snapshot.nodes || [],
        edges: snapshot.edges || [],
        versionNo: diagram.version_no,
        lockedBy: null,
        isReadOnly: false,
        error: null,
        activeUsers: [username]
      });

      // Connect to Diagram-specific WebSocket (for automatic locking and membership tracking)
      const ws = new WebSocket(`${WS_BASE}/ws/diagrams/${diagram.id}?username=${username}`);
      
      ws.onopen = () => {
        console.log("Collaborative WebSocket connected");
      };

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data);
          if (data.type === 'COLLABORATIVE_LOCK_UPDATE') {
            const activeUsers = data.active_users || [];
            const lockedBy = data.locked_by || null;
            // 如果第一个进入的人不是当前用户，则当前用户设为只读
            const isReadOnly = lockedBy && lockedBy !== username;
            
            set({
              lockedBy,
              isReadOnly: !!isReadOnly,
              activeUsers
            });
          }
        } catch (e) {
          console.error(e);
        }
      };

      ws.onclose = () => {
        console.log("Collaborative WebSocket closed");
      };

      set({ ws });
    },

    closeDiagram: () => {
      // Close WebSocket
      const ws = get().ws;
      if (ws) {
        ws.close();
      }

      if (saveTimeout) {
        clearTimeout(saveTimeout);
        saveTimeout = null;
      }

      set({
        currentDiagram: null,
        nodes: [],
        edges: [],
        lockedBy: null,
        isReadOnly: false,
        ws: null,
        activeUsers: []
      });
    },

    setNodes: (nodes) => {
      set({ nodes });
      get().triggerAutoSave();
    },

    setEdges: (edges) => {
      set({ edges });
      get().triggerAutoSave();
    },

    triggerAutoSave: () => {
      if (get().isReadOnly) return;
      if (saveTimeout) clearTimeout(saveTimeout);
      saveTimeout = setTimeout(() => {
        get().saveDiagram();
      }, 2000); // 2 second debounce
    },

    saveDiagram: async () => {
      const diag = get().currentDiagram;
      if (!diag || get().isReadOnly) return;

      if (get().isSaving) {
        set({ savePending: true });
        return;
      }

      set({ isSaving: true, savePending: false });

      const snapshot_json = JSON.stringify({
        nodes: get().nodes,
        edges: get().edges
      });

      // If offline, store in LocalStorage (BR-17)
      if (get().isOffline || !navigator.onLine) {
        localStorage.setItem(`tara_offline_diagram_${diag.id}`, JSON.stringify({
          version_no: get().versionNo,
          snapshot_json,
          timestamp: Date.now()
        }));
        console.log("Offline state, saved diagram snapshot to LocalStorage");
        set({ isSaving: false });
        return;
      }

      try {
        const response = await axios.put(
          `${API_BASE}/diagrams/${diag.id}`,
          {
            version_no: get().versionNo,
            snapshot_json
          },
          { headers: getHeaders() }
        );
        // Save successful, update local version_no
        set({ 
          versionNo: response.data.version_no,
          currentDiagram: response.data,
          error: null,
          isSaving: false
        });
        // Remove local backup if it matches
        localStorage.removeItem(`tara_offline_diagram_${diag.id}`);

        // If another save was queued during flight, run it now
        if (get().savePending) {
          get().saveDiagram();
        }
      } catch (err) {
        set({ isSaving: false });
        if (err.response?.status === 409) {
          // Version conflict (BR-16)
          // Backup local data to localStorage to avoid overwrite
          localStorage.setItem(`tara_backup_conflict_${diag.id}`, JSON.stringify({
            nodes: get().nodes,
            edges: get().edges,
            version_no: get().versionNo,
            timestamp: Date.now()
          }));
          set({
            error: '保存冲突 (409)：画布已被其他成员更新。您的最新修改已备份至本地缓存，请刷新页面加载最新画布并按提示合并。'
          });
        } else {
          set({ error: err.response?.data?.detail || '自动保存失败' });
        }
      }
    },

    syncOfflineData: async () => {
      const diag = get().currentDiagram;
      if (!diag) return;
      const offlineData = localStorage.getItem(`tara_offline_diagram_${diag.id}`);
      if (!offlineData) return;

      try {
        const parsed = JSON.parse(offlineData);
        const response = await axios.put(
          `${API_BASE}/diagrams/${diag.id}`,
          {
            version_no: parsed.version_no,
            snapshot_json: parsed.snapshot_json
          },
          { headers: getHeaders() }
        );
        set({ 
          versionNo: response.data.version_no, 
          currentDiagram: response.data,
          nodes: JSON.parse(parsed.snapshot_json).nodes || [],
          edges: JSON.parse(parsed.snapshot_json).edges || []
        });
        localStorage.removeItem(`tara_offline_diagram_${diag.id}`);
        console.log("Successfully synced offline diagram data with backend server");
      } catch (err) {
        console.error("Failed to sync offline diagram data", err);
      }
    },

    triggerAIGenerate: async (prompt) => {
      const diag = get().currentDiagram;
      if (!diag || get().isReadOnly) return null;
      set({ loading: true, error: null });
      try {
        const response = await axios.post(
          `${API_BASE}/diagrams/${diag.id}/ai-generate`,
          { prompt },
          { headers: getHeaders() }
        );
        
        const snapshot = JSON.parse(response.data.snapshot_json || '{}');
        set({
          currentDiagram: response.data,
          nodes: snapshot.nodes || [],
          edges: snapshot.edges || [],
          versionNo: response.data.version_no,
          loading: false
        });
        return response.data;
      } catch (err) {
        set({ 
          error: err.response?.data?.detail || 'AI 拓扑图生成失败', 
          loading: false 
        });
        return null;
      }
    },

    triggerAIChat: async (prompt, history = []) => {
      const diag = get().currentDiagram;
      if (!diag || get().isReadOnly) return null;
      try {
        const response = await axios.post(
          `${API_BASE}/diagrams/${diag.id}/ai-chat`,
          { prompt, history },
          { headers: getHeaders() }
        );
        return response.data;
      } catch (err) {
        throw new Error(err.response?.data?.detail || 'AI 拓扑对话失败', { cause: err });
      }
    },

    applySnapshot: async (snapshotJson) => {
      const diag = get().currentDiagram;
      if (!diag || get().isReadOnly) return null;
      set({ loading: true, error: null });
      try {
        // 先获取最新的版本号以规避乐观锁版本冲突
        const detailResp = await axios.get(
          `${API_BASE}/diagrams/${diag.id}`,
          { headers: getHeaders() }
        );
        const latestVersion = detailResp.data.version_no;

        const response = await axios.put(
          `${API_BASE}/diagrams/${diag.id}`,
          { version_no: latestVersion, snapshot_json: snapshotJson },
          { headers: getHeaders() }
        );
        const snapshot = JSON.parse(response.data.snapshot_json || '{}');
        set({
          currentDiagram: response.data,
          nodes: snapshot.nodes || [],
          edges: snapshot.edges || [],
          versionNo: response.data.version_no,
          loading: false
        });
        return response.data;
      } catch (err) {
        set({
          error: err.response?.data?.detail || '应用 AI 拓扑图失败',
          loading: false
        });
        return null;
      }
    }
  };
});
