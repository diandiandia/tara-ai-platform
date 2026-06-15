import { create } from 'zustand';
import axios from 'axios';

const API_BASE = '/api';

export const useAuthStore = create((set, get) => ({
  token: localStorage.getItem('tara_token') || null,
  user: JSON.parse(localStorage.getItem('tara_user')) || null,
  loading: false,
  error: null,

  login: async (username, password) => {
    set({ loading: true, error: null });
    try {
      const response = await axios.post(`${API_BASE}/auth/login`, { username, password });
      const { access_token } = response.data;
      
      // Get user info
      const userRes = await axios.get(`${API_BASE}/auth/me`, {
        headers: { Authorization: `Bearer ${access_token}` }
      });
      
      localStorage.setItem('tara_token', access_token);
      localStorage.setItem('tara_user', JSON.stringify(userRes.data));
      
      set({ token: access_token, user: userRes.data, loading: false });
      return true;
    } catch (err) {
      console.error(err);
      set({ 
        error: err.response?.data?.detail || '登录失败，请检查用户名和密码', 
        loading: false 
      });
      return false;
    }
  },

  logout: () => {
    localStorage.removeItem('tara_token');
    localStorage.removeItem('tara_user');
    set({ token: null, user: null, error: null });
  },

  clearError: () => set({ error: null }),

  isAdmin: () => get().user?.role === 'admin',
  isAuthenticated: () => !!get().token,

  users: [],
  
  changePassword: async (newPassword) => {
    set({ loading: true, error: null });
    try {
      const headers = { Authorization: `Bearer ${get().token}` };
      await axios.post(`${API_BASE}/auth/change-password`, { new_password: newPassword }, { headers });
      const updatedUser = { ...get().user, must_change_password: false };
      localStorage.setItem('tara_user', JSON.stringify(updatedUser));
      set({ user: updatedUser, loading: false });
      return true;
    } catch (err) {
      set({ error: err.response?.data?.detail || '修改密码失败', loading: false });
      return false;
    }
  },

  fetchUsers: async () => {
    set({ loading: true, error: null });
    try {
      const headers = { Authorization: `Bearer ${get().token}` };
      const res = await axios.get(`${API_BASE}/auth/users`, { headers });
      set({ users: res.data, loading: false });
      return res.data;
    } catch (err) {
      set({ error: err.response?.data?.detail || '获取用户列表失败', loading: false });
      return null;
    }
  },

  createUser: async (username, password, role) => {
    set({ loading: true, error: null });
    try {
      const headers = { Authorization: `Bearer ${get().token}` };
      const res = await axios.post(`${API_BASE}/auth/users`, { username, password, role }, { headers });
      set((state) => ({ users: [...state.users, res.data], loading: false }));
      return res.data;
    } catch (err) {
      set({ error: err.response?.data?.detail || '创建用户失败', loading: false });
      return null;
    }
  },

  deleteUser: async (userId) => {
    set({ loading: true, error: null });
    try {
      const headers = { Authorization: `Bearer ${get().token}` };
      await axios.delete(`${API_BASE}/auth/users/${userId}`, { headers });
      set((state) => ({ users: state.users.filter(u => u.id !== userId), loading: false }));
      return true;
    } catch (err) {
      set({ error: err.response?.data?.detail || '删除用户失败', loading: false });
      return false;
    }
  },

  resetUserPassword: async (userId, newPassword) => {
    set({ loading: true, error: null });
    try {
      const headers = { Authorization: `Bearer ${get().token}` };
      await axios.post(`${API_BASE}/auth/users/${userId}/reset-password`, { new_password: newPassword }, { headers });
      set({ loading: false });
      return true;
    } catch (err) {
      set({ error: err.response?.data?.detail || '重置密码失败', loading: false });
      return false;
    }
  }
}));
