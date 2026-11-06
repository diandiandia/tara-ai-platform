import { create } from 'zustand';
import axios from 'axios';
import { useAuthStore } from './authStore';

const API_BASE = '/api';
const WS_BASE = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}`;

const getHeaders = () => {
  const token = useAuthStore.getState().token;
  return token ? { Authorization: `Bearer ${token}` } : {};
};

// Browser-side mirror of every diagram, so the canvas can be painted instantly
// on open instead of waiting for a network round-trip. It also acts as a safety
// net when a save cannot reach the server.
const CACHE_PREFIX = 'tara_canvas_cache_';

const readCache = (diagramId) => {
  try {
    const raw = localStorage.getItem(`${CACHE_PREFIX}${diagramId}`);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
};

// Returns false when the browser refused the write (quota exceeded / storage
// disabled). Callers that mirror data speculatively use this to stop early
// instead of downloading more payloads that cannot be stored anyway.
const writeCache = (diagramId, payload) => {
  try {
    localStorage.setItem(`${CACHE_PREFIX}${diagramId}`, JSON.stringify({ ...payload, updatedAt: Date.now() }));
    return true;
  } catch {
    return false;
  }
};

const removeCache = (diagramId) => {
  try {
    localStorage.removeItem(`${CACHE_PREFIX}${diagramId}`);
  } catch {
    // Ignore
  }
};

// The diagrams list only needs metadata; keeping snapshot_json out of it stops
// the workbench from holding every canvas snapshot in memory.
const toMeta = (diagram) => ({
  id: diagram.id,
  domain_id: diagram.domain_id,
  title: diagram.title,
  version_no: diagram.version_no,
  updated_at: diagram.updated_at
});

// xyflow attaches runtime properties (measured, selected, dragging, etc.) to
// nodes and edges after they are rendered. Those values differ across sessions
// and would cause a byte-for-byte comparison to report a conflict when the
// business content is identical. Stripping them before comparison gives the
// real answer.
const STRIP_NODE_KEYS = new Set([
  'measured', 'selected', 'dragging', 'resizing',
  'width', 'height', 'selectable', 'deletable',
  'draggable', 'connectable', 'focusable', 'parentId'
]);
const STRIP_EDGE_KEYS = new Set(['selected', 'dragging']);

const normalizeNode = (n) => {
  const out = {};
  for (const k of Object.keys(n)) {
    if (!STRIP_NODE_KEYS.has(k)) out[k] = n[k];
  }
  return out;
};

const normalizeEdge = (e) => {
  const out = {};
  for (const k of Object.keys(e)) {
    if (!STRIP_EDGE_KEYS.has(k)) out[k] = e[k];
  }
  return out;
};

// Compares two snapshots by business content: they match when the same set of
// nodes and edges are present, regardless of xyflow-internal runtime fields or
// JSON formatting differences between the Python server and the JS client.
const snapshotMatches = (a, b) => {
  if (a === b) return true;
  if (!a || !b) return false;
  try {
    const left = JSON.parse(a);
    const right = JSON.parse(b);
    const leftNodes = (left.nodes || []).map(normalizeNode);
    const rightNodes = (right.nodes || []).map(normalizeNode);
    const leftEdges = (left.edges || []).map(normalizeEdge);
    const rightEdges = (right.edges || []).map(normalizeEdge);
    return JSON.stringify(leftNodes) === JSON.stringify(rightNodes)
      && JSON.stringify(leftEdges) === JSON.stringify(rightEdges);
  } catch {
    return false;
  }
};

export const useCanvasStore = create((set, get) => {
  let saveTimeout = null;
  // Incremented whenever a diagram session is closed, so in-flight saves
  // cannot restore currentDiagram after the editor has been left.
  let sessionSeq = 0;
  // Only one prefetch sweep runs at a time; bumping the token abandons the
  // previous sweep when the user switches domain mid-flight.
  let prefetchToken = 0;

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
    deletingDiagramId: null,
    isOffline: !navigator.onLine,
    isSaving: false,
    savePending: false,
    activeUsers: [],
    sessionId: 0,
    // True while the in-memory canvas differs from the last persisted snapshot
    dirty: false,
    // Last snapshot known to be persisted on the server (used to tell a real
    // conflict apart from a pure version-number drift)
    serverSnapshot: null,

    setOfflineStatus: (status) => {
      set({ isOffline: status });
      if (!status) {
        // Online: Sync offline changes if any
        get().syncOfflineData();
      }
    },

    fetchDiagrams: async (domainId, opts = {}) => {
      const { skipPrefetch = false } = opts;
      set({ loading: true, error: null });
      try {
        const response = await axios.get(`${API_BASE}/diagrams/domain/${domainId}`, { headers: getHeaders() });
        set({ diagrams: response.data, loading: false });
        // Warm the browser cache in the background so opening any canvas from
        // the list is instant instead of waiting on a cold network round-trip.
        if (!skipPrefetch) {
          get().prefetchDiagrams(response.data);
        }
      } catch (err) {
        set({ error: err.response?.data?.detail || '获取功能图列表失败', loading: false });
      }
    },

    // Mirrors every diagram snapshot of the active domain into localStorage.
    // Runs sequentially and yields between items so the workbench stays
    // responsive, and stops as soon as the browser refuses a write (quota).
    prefetchDiagrams: async (list) => {
      const items = Array.isArray(list) ? list : get().diagrams;
      if (!items.length) return;

      const token = ++prefetchToken;

      for (const meta of items) {
        if (token !== prefetchToken) return;
        if (!meta || !meta.id) continue;

        // Never touch the canvas the user is editing right now.
        if (get().currentDiagram?.id === meta.id) continue;

        const cached = readCache(meta.id);
        // Unsaved local edits must win over any server copy.
        if (cached?.dirty) continue;
        // Already mirrored at this version: nothing to download.
        if (cached?.snapshot && cached.versionNo === meta.version_no) continue;

        try {
          const response = await axios.get(`${API_BASE}/diagrams/${meta.id}`, { headers: getHeaders() });
          if (token !== prefetchToken) return;
          if (get().currentDiagram?.id === meta.id) continue;

          const fresh = response.data;
          const stored = writeCache(fresh.id, {
            versionNo: fresh.version_no,
            snapshot: fresh.snapshot_json,
            serverSnapshot: fresh.snapshot_json,
            dirty: false,
            title: fresh.title,
            domainId: fresh.domain_id
          });
          // Out of storage: further downloads would be wasted.
          if (!stored) return;
        } catch {
          // Best effort. A failed prefetch only means the next open is a cold
          // start, which the loader already handles.
        }

        await new Promise((resolve) => setTimeout(resolve, 0));
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
          diagrams: [...state.diagrams, toMeta(response.data)],
          loading: false
        }));
        return response.data;
      } catch (err) {
        set({ error: err.response?.data?.detail || '创建功能图失败', loading: false });
        return null;
      }
    },

    deleteDiagram: async (diagramId) => {
      // Guard against duplicate calls (e.g. from a component that rapidly
      // re-mounts while the confirm dialog was open, or from click-event
      // propagation quirks). The first caller wins; concurrent callers skip.
      if (get().deletingDiagramId === diagramId) return false;
      set({ loading: true, error: null, deletingDiagramId: diagramId });
      try {
        await axios.delete(`${API_BASE}/diagrams/${diagramId}`, { headers: getHeaders() });
        removeCache(diagramId);
        set((state) => ({
          diagrams: state.diagrams.filter((d) => d.id !== diagramId),
          currentDiagram: state.currentDiagram?.id === diagramId ? null : state.currentDiagram,
          loading: false,
          deletingDiagramId: null
        }));
        return true;
      } catch (err) {
        set({ error: err.response?.data?.detail || '删除功能图失败', loading: false, deletingDiagramId: null });
        return false;
      }
    },

    openDiagram: async (diagram, username) => {
      get().closeDiagram();
      sessionSeq += 1;

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
        serverSnapshot: diagram.snapshot_json || '{}',
        dirty: false,
        lockedBy: null,
        isReadOnly: false,
        error: null,
        activeUsers: [username],
        sessionId: sessionSeq
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

    // Writes the current canvas into the browser cache. Called synchronously
    // before any navigation so the last edits survive even if the network save
    // never completes.
    persistLocalCache: () => {
      const diag = get().currentDiagram;
      if (!diag || !diag.id) return;
      writeCache(diag.id, {
        versionNo: get().versionNo,
        snapshot: JSON.stringify({ nodes: get().nodes, edges: get().edges }),
        serverSnapshot: get().serverSnapshot,
        dirty: get().dirty,
        title: diag.title,
        domainId: diag.domain_id
      });
    },

    openDiagramById: async (diagramId, username) => {
      get().closeDiagram();

      const cached = readCache(diagramId);

      // Cache hit: paint the canvas immediately, then only ask the server for a
      // tiny version token to decide whether the full snapshot is needed.
      if (cached && cached.snapshot) {
        const listed = get().diagrams.find((d) => d.id === diagramId);
        set({ loading: false, error: null });
        await get().openDiagram(
          {
            id: diagramId,
            version_no: cached.versionNo,
            snapshot_json: cached.snapshot,
            title: cached.title,
            domain_id: cached.domainId || listed?.domain_id
          },
          username
        );
        set({
          serverSnapshot: cached.serverSnapshot || null,
          dirty: !!cached.dirty
        });

        let meta;
        try {
          const metaResponse = await axios.get(`${API_BASE}/diagrams/${diagramId}/meta`, { headers: getHeaders() });
          meta = metaResponse.data;
        } catch (err) {
          if (err.response?.status === 404) {
            // The diagram was deleted elsewhere; drop the stale cache
            removeCache(diagramId);
            set({ error: err.response?.data?.detail || '功能图不存在' });
            return;
          }
          // Offline or transient failure: the cached copy stays usable
          return;
        }

        if (get().currentDiagram?.id !== diagramId) return;

        set((state) => ({
          diagrams: state.diagrams.some((d) => d.id === meta.id)
            ? state.diagrams.map((d) => (d.id === meta.id ? { ...d, ...meta } : d))
            : [...state.diagrams, meta]
        }));

        if (meta.version_no === cached.versionNo) {
          // Server has not moved since the cache was written: nothing to fetch.
          // Only unsaved local edits still need to reach the server.
          if (get().dirty) get().flushSave();
          return;
        }

        if (get().dirty) {
          // Local edits exist and the server moved on. Let the save path decide
          // between "version drift" (safe to realign) and a real conflict.
          get().flushSave();
          return;
        }

        // Nothing unsaved locally and the server has newer content: pull it.
        try {
          const response = await axios.get(`${API_BASE}/diagrams/${diagramId}`, { headers: getHeaders() });
          if (get().currentDiagram?.id !== diagramId) return;
          await get().openDiagram(response.data, username);
          get().persistLocalCache();
        } catch (err) {
          set({ error: err.response?.data?.detail || '加载画布失败' });
        }
        return;
      }

      // Cold start: no cache yet, the full snapshot is unavoidable.
      set({ loading: true, error: null });
      const startSessionId = get().sessionId;
      try {
        const response = await axios.get(`${API_BASE}/diagrams/${diagramId}`, { headers: getHeaders() });
        // The user may have left the editor while this request was in flight.
        // Reopening now would resurrect a closed session and overwrite the edits
        // made on the canvas with the server copy.
        if (get().sessionId !== startSessionId) {
          set({ loading: false });
          return;
        }
        const fresh = response.data;
        await get().openDiagram(fresh, username);
        set((state) => ({
          loading: false,
          diagrams: state.diagrams.some((d) => d.id === fresh.id)
            ? state.diagrams.map((d) => (d.id === fresh.id ? { ...d, ...toMeta(fresh) } : d))
            : [...state.diagrams, toMeta(fresh)]
        }));
        get().persistLocalCache();
      } catch (err) {
        set({
          loading: false,
          error: err.response?.data?.detail || '加载画布失败，请返回工作台重试'
        });
      }
    },

    closeDiagram: () => {
      sessionSeq += 1;
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
        activeUsers: [],
        sessionId: sessionSeq,
        serverSnapshot: null,
        dirty: false,
        error: null
      });
    },

    setNodes: (nodes) => {
      set({ nodes, dirty: true });
      get().triggerAutoSave();
    },

    setEdges: (edges) => {
      set({ edges, dirty: true });
      get().triggerAutoSave();
    },

    triggerAutoSave: () => {
      if (get().isReadOnly) return;
      // Mirror the edit into the browser cache right away so a crash, refresh or
      // failed network save cannot lose it.
      get().persistLocalCache();
      if (saveTimeout) clearTimeout(saveTimeout);
      saveTimeout = setTimeout(() => {
        get().saveDiagram();
      }, 2000); // 2 second debounce
    },

    flushSave: async () => {
      if (saveTimeout) {
        clearTimeout(saveTimeout);
        saveTimeout = null;
      }
      if (!get().currentDiagram || get().isReadOnly) return;

      // An in-flight save may already be holding a stale snapshot, and a queued
      // save is fired without awaiting. Keep polling until the store is idle so
      // the latest edits are definitely persisted before the session closes.
      const deadline = Date.now() + 15000;
      while ((get().isSaving || get().savePending) && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 100));
      }

      if (!get().currentDiagram || get().isReadOnly) return;
      if (get().isSaving || get().savePending) return;
      if (!get().dirty) return;
      await get().saveDiagram();
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

      // Mirror the outgoing payload into the browser cache so the canvas can be
      // restored instantly (and offline) even if this request never lands.
      get().persistLocalCache();

      // If offline, store in LocalStorage (BR-17)
      if (get().isOffline || !navigator.onLine) {
        localStorage.setItem(`tara_offline_diagram_${diag.id}`, JSON.stringify({
          version_no: get().versionNo,
          snapshot_json,
          timestamp: Date.now()
        }));
        console.log("Offline state, saved diagram snapshot to LocalStorage");
        set({ isSaving: false, savePending: false });
        return;
      }

      const MAX_ATTEMPTS = 3;
      let lastError = null;
      let versionResynced = false;

      for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt += 1) {
        try {
          const response = await axios.put(
            `${API_BASE}/diagrams/${diag.id}`,
            {
              version_no: get().versionNo,
              snapshot_json
            },
            { headers: getHeaders() }
          );
          // Save successful, update local version_no and keep the diagrams list in sync.
          // The version number is a server-side fact that must be recorded even
          // when the session changed while the request was in flight (leaving
          // and re-entering the editor): dropping it would leave the local
          // counter behind and make every later save fail with 409. It is only
          // skipped when another diagram has been opened meanwhile, otherwise
          // this diagram's version would leak into that one.
          const sameDiagram = get().currentDiagram?.id === diag.id;
          const stillDirty = sameDiagram
            && JSON.stringify({ nodes: get().nodes, edges: get().edges }) !== snapshot_json;
          set((state) => ({
            versionNo: sameDiagram ? response.data.version_no : state.versionNo,
            currentDiagram: sameDiagram ? response.data : state.currentDiagram,
            serverSnapshot: sameDiagram ? response.data.snapshot_json : state.serverSnapshot,
            diagrams: state.diagrams.map((d) => (d.id === response.data.id ? { ...d, ...toMeta(response.data) } : d)),
            error: sameDiagram ? null : state.error,
            dirty: sameDiagram ? stillDirty : state.dirty,
            isSaving: false
          }));
          get().persistLocalCache();
          // Remove local backup if it matches
          localStorage.removeItem(`tara_offline_diagram_${diag.id}`);

          // If another save was queued during flight, run it now
          if (get().savePending) {
            get().saveDiagram();
          }
          return;
        } catch (err) {
          lastError = err;
          const status = err.response?.status;

          if (status === 409 && !versionResynced) {
            // A 409 does not necessarily mean someone else edited the canvas:
            // it also happens when the local version counter drifted (e.g. an
            // earlier save succeeded but its response was lost, or the session
            // was reopened with a stale version). Compare the server content
            // against what is actually on the canvas to tell the two apart.
            versionResynced = true;
            try {
              const detail = await axios.get(`${API_BASE}/diagrams/${diag.id}`, { headers: getHeaders() });
              const serverSnapshot = detail.data.snapshot_json;
              const localSnapshot = JSON.stringify({ nodes: get().nodes, edges: get().edges });
              const sameDiagram = get().currentDiagram?.id === diag.id;

              if (snapshotMatches(serverSnapshot, localSnapshot)) {
                // The edits already reached the server (a previous attempt
                // committed but its response was lost). Realigning the version
                // is enough; sending the payload again would just be a no-op.
                set({
                  versionNo: detail.data.version_no,
                  serverSnapshot: sameDiagram ? serverSnapshot : get().serverSnapshot,
                  currentDiagram: sameDiagram ? detail.data : get().currentDiagram,
                  dirty: sameDiagram ? false : get().dirty,
                  isSaving: false,
                  savePending: false,
                  error: sameDiagram ? null : get().error
                });
                if (sameDiagram) get().persistLocalCache();
                lastError = null;
                return;
              }

              if (snapshotMatches(serverSnapshot, get().serverSnapshot)) {
                // The server still holds the copy we last knew about, so nobody
                // else touched it: only our version counter drifted. Realign it
                // and retry the write immediately.
                set({ versionNo: detail.data.version_no, serverSnapshot });
                attempt -= 1;
                continue;
              }
            } catch (detailErr) {
              lastError = detailErr;
            }
            break;
          }

          // Retry only transient failures (network error / server error / SQLite lock)
          const retryable = !status || status >= 500;
          if (!retryable || attempt === MAX_ATTEMPTS) {
            break;
          }
          await new Promise((resolve) => setTimeout(resolve, attempt * 800));
        }
      }

      const err = lastError;
      // Clear the queued flag as well: the queued save cannot run once we bail
      // out here, and leaving it set would make flushSave spin until timeout.
      set({ isSaving: false, savePending: false });
      if (err?.response?.status === 409) {
        // Version conflict (BR-16)
        // Backup local data to localStorage to avoid overwrite
        localStorage.setItem(`tara_backup_conflict_${diag.id}`, JSON.stringify({
          nodes: get().nodes,
          edges: get().edges,
          version_no: get().versionNo,
          timestamp: Date.now()
        }));
        set({
          error: '保存冲突 (409)：服务器上的画布内容已发生变化（可能来自另一个浏览器标签页或更早的会话），本次修改未写入。您的最新修改已备份至浏览器 LocalStorage，请刷新页面加载最新画布后再合并。'
        });
      } else {
        set({
          error: err?.response?.data?.detail
            || `自动保存失败：${err?.message || '服务器无响应'}（修改尚未保存到服务器，请检查网络后重试）`
        });
      }
    },

    syncOfflineData: async () => {
      const diag = get().currentDiagram;
      if (!diag) return;
      const offlineData = localStorage.getItem(`tara_offline_diagram_${diag.id}`);
      if (!offlineData) return;

      try {
        const parsed = JSON.parse(offlineData);
        // Always resolve the current version from the server first: the version
        // stored offline is stale by definition and would trigger a false 409.
        const detail = await axios.get(`${API_BASE}/diagrams/${diag.id}`, { headers: getHeaders() });
        const response = await axios.put(
          `${API_BASE}/diagrams/${diag.id}`,
          {
            version_no: detail.data.version_no,
            snapshot_json: parsed.snapshot_json
          },
          { headers: getHeaders() }
        );
        set({ 
          versionNo: response.data.version_no, 
          currentDiagram: response.data,
          serverSnapshot: response.data.snapshot_json,
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
          serverSnapshot: response.data.snapshot_json,
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
          serverSnapshot: response.data.snapshot_json,
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