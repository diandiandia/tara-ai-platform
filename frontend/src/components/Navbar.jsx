import { useI18n } from '../stores/i18nStore';
import { useAuthStore } from '../stores/authStore';
import { useProjectStore } from '../stores/projectStore';
import { useCanvasStore } from '../stores/canvasStore';
import { LogOut, Settings, Folder, ShieldCheck, Users, User as UserIcon, Globe } from 'lucide-react';

export default function Navbar({ setPage }) {
  const { t, language, setLanguage } = useI18n();
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
            <span>{t("当前项目:")} {currentProject.name}</span>
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
                {user.role === 'admin' ? t('管理员') : t('分析员')}
              </span>
            </div>
          </div>
        )}

        {user?.role === 'admin' && (
          <button 
            onClick={() => setPage('users')}
            className="btn btn-secondary"
            style={{ padding: '8px 12px', fontSize: '13px', display: 'flex', alignItems: 'center', gap: '4px' }}
            title={t("用户管理")}
          >
            <Users size={15} />
            <span>{t("用户管理")}</span>
          </button>
        )}

        <button 
          onClick={() => setLanguage(language === 'zh' ? 'en' : 'zh')}
          className="btn btn-secondary"
          style={{ padding: '8px 12px', fontSize: '13px', display: 'flex', alignItems: 'center', gap: '4px' }}
          title={language === 'zh' ? 'Switch to English' : '切换至中文'}
        >
          <Globe size={15} />
          <span>{language === 'zh' ? 'English' : '中文'}</span>
        </button>

        <button 
          onClick={handleSettingsClick}
          className="btn btn-secondary"
          style={{ padding: '8px 12px', fontSize: '13px' }}
          title={t("系统配置")}
        >
          <Settings size={15} />
          <span>{t("系统配置")}</span>
        </button>

        <button 
          onClick={handleLogout}
          className="btn btn-danger"
          style={{ padding: '8px 12px', fontSize: '13px' }}
          title={t("退出登录")}
        >
          <LogOut size={15} />
          <span>{t("退出")}</span>
        </button>
      </div>
    </nav>
  );
}
