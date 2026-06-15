import { create } from 'zustand';
import axios from 'axios';
import { useAuthStore } from './authStore';

const API_BASE = '/api';

const getHeaders = () => {
  const token = useAuthStore.getState().token;
  return token ? { Authorization: `Bearer ${token}` } : {};
};

export const useProjectStore = create((set, get) => ({
  projects: [],
  currentProject: null,
  domains: [],
  activeDomain: null,
  loading: false,
  error: null,

  fetchProjects: async (q = '') => {
    set({ loading: true, error: null });
    try {
      const url = q ? `${API_BASE}/projects?q=${encodeURIComponent(q)}` : `${API_BASE}/projects`;
      const response = await axios.get(url, { headers: getHeaders() });
      set({ projects: response.data, loading: false });
    } catch (err) {
      set({ error: err.response?.data?.detail || '获取项目列表失败', loading: false });
    }
  },

  fetchProjectDetails: async (id) => {
    set({ loading: true, error: null });
    try {
      const response = await axios.get(`${API_BASE}/projects/${id}`, { headers: getHeaders() });
      set({ currentProject: response.data, loading: false });
      return response.data;
    } catch (err) {
      set({ error: err.response?.data?.detail || '获取项目详情失败', loading: false });
      return null;
    }
  },

  createProject: async (name, description) => {
    set({ loading: true, error: null });
    try {
      const response = await axios.post(`${API_BASE}/projects`, { name, description }, { headers: getHeaders() });
      set((state) => ({ 
        projects: [response.data, ...state.projects],
        loading: false 
      }));
      return response.data;
    } catch (err) {
      set({ error: err.response?.data?.detail || '创建项目失败', loading: false });
      return null;
    }
  },

  updateProject: async (id, name, description) => {
    set({ loading: true, error: null });
    try {
      const response = await axios.put(`${API_BASE}/projects/${id}`, { name, description }, { headers: getHeaders() });
      set((state) => ({
        projects: state.projects.map((p) => (p.id === id ? response.data : p)),
        currentProject: state.currentProject?.id === id ? response.data : state.currentProject,
        loading: false
      }));
      return response.data;
    } catch (err) {
      set({ error: err.response?.data?.detail || '修改项目失败', loading: false });
      return null;
    }
  },

  deleteProject: async (id) => {
    set({ loading: true, error: null });
    try {
      await axios.delete(`${API_BASE}/projects/${id}`, { headers: getHeaders() });
      set((state) => ({
        projects: state.projects.filter((p) => p.id !== id),
        currentProject: state.currentProject?.id === id ? null : state.currentProject,
        domains: state.currentProject?.id === id ? [] : state.domains,
        activeDomain: state.currentProject?.id === id ? null : state.activeDomain,
        loading: false
      }));
      return true;
    } catch (err) {
      set({ error: err.response?.data?.detail || '删除项目失败', loading: false });
      return false;
    }
  },

  archiveProject: async (id) => {
    set({ loading: true, error: null });
    try {
      const response = await axios.post(`${API_BASE}/projects/${id}/archive`, {}, { headers: getHeaders() });
      set((state) => ({
        projects: state.projects.map((p) => (p.id === id ? response.data : p)),
        currentProject: state.currentProject?.id === id ? response.data : state.currentProject,
        loading: false
      }));
      return response.data;
    } catch (err) {
      set({ error: err.response?.data?.detail || '归档项目失败', loading: false });
      return null;
    }
  },

  unarchiveProject: async (id) => {
    set({ loading: true, error: null });
    try {
      const response = await axios.post(`${API_BASE}/projects/${id}/unarchive`, {}, { headers: getHeaders() });
      set((state) => ({
        projects: state.projects.map((p) => (p.id === id ? response.data : p)),
        currentProject: state.currentProject?.id === id ? response.data : state.currentProject,
        loading: false
      }));
      return response.data;
    } catch (err) {
      set({ error: err.response?.data?.detail || '激活项目失败', loading: false });
      return null;
    }
  },

  // Subdomains
  fetchDomains: async (projectId) => {
    set({ loading: true, error: null });
    try {
      const response = await axios.get(`${API_BASE}/projects/${projectId}/domains`, { headers: getHeaders() });
      set({ domains: response.data, loading: false });
      // Restore active domain if still valid
      const currentActive = get().activeDomain;
      if (currentActive) {
        const found = response.data.find(d => d.id === currentActive.id);
        if (found) set({ activeDomain: found });
        else set({ activeDomain: null });
      }
    } catch (err) {
      set({ error: err.response?.data?.detail || '获取子域控列表失败', loading: false });
    }
  },

  createDomain: async (projectId, name) => {
    set({ loading: true, error: null });
    try {
      const response = await axios.post(
        `${API_BASE}/projects/${projectId}/domains`, 
        { name }, 
        { headers: getHeaders() }
      );
      set((state) => ({
        domains: [...state.domains, response.data],
        loading: false
      }));
      // Re-fetch project to reflect state changes (e.g. from draft)
      get().fetchProjectDetails(projectId);
      return response.data;
    } catch (err) {
      set({ error: err.response?.data?.detail || '创建子域控失败', loading: false });
      return null;
    }
  },

  updateDomain: async (domainId, name) => {
    set({ loading: true, error: null });
    try {
      const response = await axios.put(
        `${API_BASE}/projects/domains/${domainId}`, 
        { name }, 
        { headers: getHeaders() }
      );
      set((state) => ({
        domains: state.domains.map((d) => (d.id === domainId ? response.data : d)),
        activeDomain: state.activeDomain?.id === domainId ? response.data : state.activeDomain,
        loading: false
      }));
      if (get().currentProject) {
        get().fetchProjectDetails(get().currentProject.id);
      }
      return response.data;
    } catch (err) {
      set({ error: err.response?.data?.detail || '修改子域控失败', loading: false });
      return null;
    }
  },

  deleteDomain: async (domainId) => {
    set({ loading: true, error: null });
    try {
      await axios.delete(`${API_BASE}/projects/domains/${domainId}`, { headers: getHeaders() });
      const currentProject = get().currentProject;
      set((state) => ({
        domains: state.domains.filter((d) => d.id !== domainId),
        activeDomain: state.activeDomain?.id === domainId ? null : state.activeDomain,
        loading: false
      }));
      if (currentProject) {
        get().fetchProjectDetails(currentProject.id);
      }
      return true;
    } catch (err) {
      set({ error: err.response?.data?.detail || '删除子域控失败', loading: false });
      return false;
    }
  },

  setActiveDomain: (domain) => {
    set({ activeDomain: domain });
  },

  clearError: () => set({ error: null })
}));
