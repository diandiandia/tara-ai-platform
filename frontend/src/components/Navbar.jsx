import React from 'react';
import { useAuthStore } from '../stores/authStore';
import { useProjectStore } from '../stores/projectStore';
import { useCanvasStore } from '../stores/canvasStore';
import { LogOut, Settings, Layout, Folder, ShieldCheck, Users, User as UserIcon } from 'lucide-react';

export default function Navbar({ setPage }) {
  const { user, logout } = useAuthStore();
  const { currentProject } = useProjectStore();
  const { closeDiagram } = useCanvasStore();

  const handleLogout = () => {
    closeDiagram();
    logout();
  };

  const handleHomeClick = () => {
    closeDiagram();
    setPage(user?.role === 'admin' ? 'users' : 'projects');
  };

  const handleSettingsClick = () => {
    closeDiagram();
    setPage('settings');
  };

  return (
    <nav className="header-nav">
      <div style={{ display: 'flex', alignItems: 'center', gap: '20px' }}>
        <div 
          onClick={handleHomeClick}
          style={{ 
            display: 'flex', 
            alignItems: 'center', 
            gap: '8px', 
            cursor: 'pointer',
            fontWeight: '700',
            fontSize: '18px',
            color: 'var(--text-primary)'
          }}
        >
          <ShieldCheck size={22} style={{ color: 'var(--primary)' }} />
          <span>TARA AI Platform</span>
        </div>

        {currentProject && (
          <div style={{ 
            display: 'flex', 
            alignItems: 'center', 
            gap: '6px', 
            fontSize: '13px', 
            color: 'var(--text-secondary)',
            background: 'var(--bg-card)',
            padding: '4px 12px',
            borderRadius: '6px',
            border: '1px solid var(--border-color)'
          }}>
            <Folder size={14} style={{ color: 'var(--secondary)' }} />
            <span>当前项目: {currentProject.name}</span>
          </div>
        )}
      </div>

      <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
        {user && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '13px' }}>
            <div style={{
              background: 'var(--bg-card)',
              padding: '6px 12px',
              borderRadius: '6px',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
              border: '1px solid var(--border-color)'
            }}>
              <UserIcon size={12} style={{ color: 'var(--primary)' }} />
              <span>{user.username}</span>
              <span style={{ 
                fontSize: '10px', 
                background: user.role === 'admin' ? 'rgba(225, 29, 72, 0.08)' : 'rgba(2, 132, 199, 0.08)',
                color: user.role === 'admin' ? '#e11d48' : '#0284c7',
                border: user.role === 'admin' ? '1px solid rgba(225, 29, 72, 0.15)' : '1px solid rgba(2, 132, 199, 0.15)',
                padding: '2px 6px',
                borderRadius: '4px',
                fontWeight: '600'
              }}>
                {user.role === 'admin' ? '管理员' : '分析员'}
              </span>
            </div>
          </div>
        )}

        {user?.role === 'admin' && (
          <button 
            onClick={() => setPage('users')}
            className="btn btn-secondary"
            style={{ padding: '8px 12px', fontSize: '13px', display: 'flex', alignItems: 'center', gap: '4px' }}
            title="用户管理"
          >
            <Users size={15} />
            <span>用户管理</span>
          </button>
        )}

        <button 
          onClick={handleSettingsClick}
          className="btn btn-secondary"
          style={{ padding: '8px 12px', fontSize: '13px' }}
          title="系统配置"
        >
          <Settings size={15} />
          <span>系统配置</span>
        </button>

        <button 
          onClick={handleLogout}
          className="btn btn-danger"
          style={{ padding: '8px 12px', fontSize: '13px' }}
          title="退出登录"
        >
          <LogOut size={15} />
          <span>退出</span>
        </button>
      </div>
    </nav>
  );
}
