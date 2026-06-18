import { create } from 'zustand';
import axios from 'axios';
import { useAuthStore } from './authStore';

const API_BASE = '/api';

const getHeaders = () => {
  const token = useAuthStore.getState().token;
  return token ? { Authorization: `Bearer ${token}` } : {};
};

export const useTaraStore = create((set, get) => ({
  assets: [],
  taraRun: null,
  taraResults: [],
  settings: null,
  loading: false,
  error: null,

  fetchAssets: async (domainId) => {
    set({ loading: true, error: null });
    try {
      const response = await axios.get(`${API_BASE}/domains/${domainId}/assets`, { headers: getHeaders() });
      set({ assets: response.data, loading: false });
    } catch (err) {
      set({ error: err.response?.data?.detail || '获取资产列表失败', loading: false });
    }
  },

  confirmAsset: async (assetId, fields, bypassLock = false) => {
    set({ loading: true, error: null });
    try {
      const response = await axios.post(
        `${API_BASE}/assets/${assetId}/confirm${bypassLock ? '?bypass_lock=true' : ''}`,
        fields,
        { headers: getHeaders() }
      );
      set((state) => ({
        assets: state.assets.map((a) => (a.id === assetId ? response.data : a)),
        loading: false
      }));
      return response.data;
    } catch (err) {
      set({ error: err.response?.data?.detail || '更新资产状态失败', loading: false });
      return null;
    }
  },

  createManualAsset: async (domainId, name, assetType, protocol, description) => {
    set({ loading: true, error: null });
    try {
      const response = await axios.post(
        `${API_BASE}/domains/${domainId}/assets`,
        { name, asset_type: assetType, protocol, description },
        { headers: getHeaders() }
      );
      set((state) => ({
        assets: [...state.assets, response.data],
        loading: false
      }));
      return response.data;
    } catch (err) {
      set({ error: err.response?.data?.detail || '添加资产失败', loading: false });
      return null;
    }
  },

  deleteAsset: async (assetId) => {
    set({ loading: true, error: null });
    try {
      await axios.delete(`${API_BASE}/assets/${assetId}`, { headers: getHeaders() });
      set((state) => ({
        assets: state.assets.filter((a) => a.id !== assetId),
        loading: false
      }));
      return true;
    } catch (err) {
      set({ error: err.response?.data?.detail || '删除资产失败', loading: false });
      return false;
    }
  },

  clearAssets: async (domainId) => {
    set({ loading: true, error: null });
    try {
      await axios.delete(`${API_BASE}/domains/${domainId}/assets`, { headers: getHeaders() });
      set({ assets: [], loading: false });
      return true;
    } catch (err) {
      set({ error: err.response?.data?.detail || '清空资产失败', loading: false });
      return false;
    }
  },

  extractAssets: async (domainId) => {
    set({ loading: true, error: null });
    try {
      const response = await axios.post(`${API_BASE}/domains/${domainId}/extract-assets`, {}, { headers: getHeaders() });
      set({ assets: response.data, loading: false });
      return response.data;
    } catch (err) {
      set({ error: err.response?.data?.detail || '自动提取资产失败', loading: false });
      return null;
    }
  },

  fetchDeduplicateSuggestions: async (domainId) => {
    set({ loading: true, error: null });
    try {
      const response = await axios.post(`${API_BASE}/domains/${domainId}/deduplicate`, {}, { headers: getHeaders() });
      set({ loading: false });
      return response.data; // List of DeduplicateSuggestionItem
    } catch (err) {
      set({ error: err.response?.data?.detail || 'AI 去重计算失败', loading: false });
      return [];
    }
  },

  confirmDeduplicate: async (domainId, suggestions) => {
    set({ loading: true, error: null });
    try {
      const response = await axios.post(
        `${API_BASE}/domains/${domainId}/deduplicate/confirm`,
        { suggestions },
        { headers: getHeaders() }
      );
      set({ loading: false });
      // Re-fetch assets
      get().fetchAssets(domainId);
      return response.data;
    } catch (err) {
      set({ error: err.response?.data?.detail || '确认 AI 合并资产失败', loading: false });
      return null;
    }
  },

  // TARA Engine Run Controls
  startTaraAnalysis: async (domainId, force = false) => {
    set({ loading: true, error: null });
    try {
      const response = await axios.post(`${API_BASE}/domains/${domainId}/tara-runs?force=${force}`, {}, { headers: getHeaders() });
      set({ taraRun: response.data, loading: false });
      return response.data;
    } catch (err) {
      set({ error: err.response?.data?.detail || '启动 TARA 评估分析失败', loading: false });
      return null;
    }
  },

  cancelTaraAnalysis: async (domainId) => {
    set({ loading: true, error: null });
    try {
      const response = await axios.post(`${API_BASE}/domains/${domainId}/cancel-run`, {}, { headers: getHeaders() });
      set({ taraRun: null, loading: false });
      return response.data;
    } catch (err) {
      set({ error: err.response?.data?.detail || '强行取消 TARA 运行失败', loading: false });
      return null;
    }
  },

  fetchTaraProgress: async (domainId) => {
    try {
      const response = await axios.get(`${API_BASE}/domains/${domainId}/tara-runs/progress`, { headers: getHeaders() });
      set({ taraRun: response.data });
      return response.data;
    } catch (err) {
      // It is normal if there's no run history yet
      console.warn("Could not fetch TARA run progress", err);
      return null;
    }
  },

  fetchTaraResults: async (domainId) => {
    set({ loading: true, error: null });
    try {
      const response = await axios.get(`${API_BASE}/domains/${domainId}/tara-results`, { headers: getHeaders() });
      set({ taraResults: response.data, loading: false });
      return response.data;
    } catch (err) {
      set({ error: err.response?.data?.detail || '获取 TARA 分析结果失败', loading: false });
      return [];
    }
  },

  updateTaraStep: async (stepId, finalOutput, reason) => {
    set({ loading: true, error: null });
    try {
      const response = await axios.put(
        `${API_BASE}/tara-steps/${stepId}`,
        {
          final_output: finalOutput,
          modification_reason: reason
        },
        { headers: getHeaders() }
      );
      set((state) => ({
        taraResults: state.taraResults.map((s) => (s.id === stepId ? response.data : s)),
        loading: false
      }));
      return response.data;
    } catch (err) {
      set({ error: err.response?.data?.detail || '人工修改结果失败', loading: false });
      return null;
    }
  },

  submitManualOfflineResults: async (domainId, steps) => {
    set({ loading: true, error: null });
    try {
      const response = await axios.post(
        `${API_BASE}/domains/${domainId}/manual-update`,
        { steps },
        { headers: getHeaders() }
      );
      set({ loading: false });
      get().fetchTaraResults(domainId);
      return response.data;
    } catch (err) {
      set({ error: err.response?.data?.detail || '手动录入结果入库失败', loading: false });
      return null;
    }
  },

  // LLM Settings Controls
  fetchSettings: async () => {
    set({ loading: true, error: null });
    try {
      const response = await axios.get(`${API_BASE}/settings`, { headers: getHeaders() });
      set({ settings: response.data, loading: false });
      return response.data;
    } catch (err) {
      set({ error: err.response?.data?.detail || '获取大模型配置失败', loading: false });
      return null;
    }
  },

  saveSettings: async (apiBaseUrl, apiKey, modelName) => {
    set({ loading: true, error: null });
    try {
      const response = await axios.post(
        `${API_BASE}/settings`,
        {
          api_base_url: apiBaseUrl,
          api_key: apiKey,
          model_name: modelName
        },
        { headers: getHeaders() }
      );
      set({ settings: response.data, loading: false });
      return response.data;
    } catch (err) {
      set({ error: err.response?.data?.detail || '保存大模型配置失败', loading: false });
      return null;
    }
  },

  testConnection: async (apiBaseUrl, apiKey, modelName) => {
    set({ loading: true, error: null });
    try {
      const response = await axios.post(
        `${API_BASE}/settings/test-connection`,
        {
          api_base_url: apiBaseUrl,
          api_key: apiKey,
          model_name: modelName
        },
        { headers: getHeaders() }
      );
      set({ loading: false });
      return response.data; // { success: boolean, message: string }
    } catch (err) {
      set({ error: err.response?.data?.detail || '大模型连通测试发生网络错误', loading: false });
      return { success: false, message: err.message };
    }
  },

  exportReport: async (domainId, format, desensitize) => {
    try {
      const token = useAuthStore.getState().token;
      const url = `${API_BASE}/reports/domains/${domainId}/export?format=${format}&desensitize=${desensitize}`;
      
      const response = await fetch(url, {
        method: 'GET',
        headers: token ? { Authorization: `Bearer ${token}` } : {}
      });
      
      if (!response.ok) {
        throw new Error('导出报告请求失败');
      }

      const blob = await response.blob();
      const downloadUrl = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = downloadUrl;
      
      const filename = `TARA_Report_${domainId}_${desensitize ? 'desensitized' : 'full'}.${format}`;
      link.setAttribute('download', filename);
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(downloadUrl);
      return true;
    } catch (err) {
      console.error(err);
      set({ error: err.message || '导出报告发生错误' });
      return false;
    }
  },

  clearError: () => set({ error: null })
}));
