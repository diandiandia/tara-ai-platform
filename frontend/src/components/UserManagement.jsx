import { useI18n } from '../stores/i18nStore';
import { useEffect, useState } from 'react';
import { useAuthStore } from '../stores/authStore';
import { UserPlus, Trash2, Key, Users, ShieldAlert, CheckCircle2, AlertCircle } from 'lucide-react';

export default function UserManagement() {
  const { t } = useI18n();
  const { 
    user: currentUser, 
    users, 
    fetchUsers, 
    createUser, 
    deleteUser, 
    resetUserPassword,
    loading, 
    error, 
    clearError 
  } = useAuthStore();

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [role, setRole] = useState('analyst');

  const [resettingUser, setResettingUser] = useState(null); // User object to reset
  const [newPassword, setNewPassword] = useState('');

  const [actionSuccess, setActionSuccess] = useState('');

  useEffect(() => {
    fetchUsers();
    return () => clearError();
  }, [fetchUsers, clearError]);

  const handleCreateUser = async (e) => {
    e.preventDefault();
    setActionSuccess('');
    clearError();

    if (!username.trim() || !password.trim()) {
      return;
    }

    const res = await createUser(username.trim(), password.trim(), role);
    if (res) {
      setUsername('');
      setPassword('');
      setRole('analyst');
      setActionSuccess(t('用户 "') + res.username + t('" 创建成功！'));
      setTimeout(() => setActionSuccess(''), 3000);
    }
  };

  const handleDeleteUser = async (user) => {
    setActionSuccess('');
    clearError();

    if (user.id === currentUser.id) {
      alert(t('无法删除自身账户！'));
      return;
    }

    if (window.confirm(t('确认要删除用户 "') + user.username + t('" 吗？此操作无法撤销。'))) {
      const success = await deleteUser(user.id);
      if (success) {
        setActionSuccess(t('用户 "') + user.username + t('" 已被成功删除。'));
        setTimeout(() => setActionSuccess(''), 3000);
      }
    }
  };

  const handleOpenReset = (user) => {
    setResettingUser(user);
    setNewPassword('');
    setActionSuccess('');
    clearError();
  };

  const handleResetPasswordSubmit = async (e) => {
    e.preventDefault();
    if (!newPassword.trim() || !resettingUser) return;

    const success = await resetUserPassword(resettingUser.id, newPassword.trim());
    if (success) {
      setActionSuccess(t('用户 "') + resettingUser.username + t('" 的密码已成功重置！'));
      setResettingUser(null);
      setNewPassword('');
      setTimeout(() => setActionSuccess(''), 3000);
    }
  };

  return (
    <div className="dashboard-container" style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}>
      
      {/* Page Header */}
      <div>
        <h1 className="section-title" style={{ margin: 0, display: 'flex', alignItems: 'center', gap: '10px' }}>
          <Users style={{ color: 'var(--primary)' }} />
          <span>{t("用户账户管理")}</span>
        </h1>
        <p style={{ color: 'var(--text-secondary)', fontSize: '14px', marginTop: '4px' }}>
          {t("配置与管理系统用户账户。系统管理员只能执行用户配置与系统大模型设置，不具有项目编辑权限。")}
        </p>
      </div>

      {/* Notifications */}
      {error && (
        <div className="glass" style={{
          background: 'rgba(244, 63, 94, 0.08)',
          border: '1px solid rgba(244, 63, 94, 0.25)',
          color: '#e11d48',
          padding: '12px 16px',
          borderRadius: '8px',
          fontSize: '14px',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center'
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <ShieldAlert size={16} />
            <span>{t(error)}</span>
          </div>
          <button onClick={clearError} style={{ background: 'none', border: 'none', color: '#e11d48', cursor: 'pointer', fontWeight: 'bold' }}>×</button>
        </div>
      )}

      {actionSuccess && (
        <div className="glass" style={{
          background: 'rgba(52, 211, 153, 0.08)',
          border: '1px solid rgba(52, 211, 153, 0.25)',
          color: '#059669',
          padding: '12px 16px',
          borderRadius: '8px',
          fontSize: '14px',
          display: 'flex',
          alignItems: 'center',
          gap: '8px'
        }}>
          <CheckCircle2 size={16} />
          <span>{actionSuccess}</span>
        </div>
      )}

      {/* Main Layout Grid */}
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 340px', gap: '24px', alignItems: 'start' }}>
        
        {/* Left Side: Users List Table */}
        <div className="glass" style={{ padding: '24px', display: 'flex', flexDirection: 'column', gap: '16px' }}>
          <h3 style={{ margin: 0, fontSize: '16px', fontWeight: '600', color: 'var(--text-primary)' }}>{t("全部系统账户")} ({users.length})</h3>
          
          <div style={{ overflowX: 'auto' }}>
            <table className="tara-table" style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left' }}>
              <thead>
                <tr style={{ borderBottom: '2px solid var(--border-color)', color: 'var(--text-muted)', fontSize: '12px', textTransform: 'uppercase' }}>
                  <th style={{ padding: '12px 8px' }}>{t("用户名")}</th>
                  <th style={{ padding: '12px 8px' }}>{t("角色")}</th>
                  <th style={{ padding: '12px 8px' }}>{t("必须改密")}</th>
                  <th style={{ padding: '12px 8px' }}>{t("创建时间")}</th>
                  <th style={{ padding: '12px 8px', textAlign: 'right' }}>{t("操作")}</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => (
                  <tr key={u.id} style={{ borderBottom: '1px solid var(--border-color)', fontSize: '14px', color: 'var(--text-primary)' }}>
                    <td style={{ padding: '14px 8px', fontWeight: '600' }}>
                      {u.username} {u.id === currentUser.id && <span style={{ color: 'var(--text-muted)', fontWeight: 'normal' }}>({t("当前账户")})</span>}
                    </td>
                    <td style={{ padding: '14px 8px' }}>
                      <span style={{ 
                        fontSize: '11px', 
                        background: u.role === 'admin' ? 'rgba(225, 29, 72, 0.08)' : 'rgba(2, 132, 199, 0.08)',
                        color: u.role === 'admin' ? '#e11d48' : '#0284c7',
                        border: u.role === 'admin' ? '1px solid rgba(225, 29, 72, 0.15)' : '1px solid rgba(2, 132, 199, 0.15)',
                        padding: '2px 6px',
                        borderRadius: '4px',
                        fontWeight: '600'
                      }}>
                        {u.role === 'admin' ? t('管理员') : t('分析员')}
                      </span>
                    </td>
                    <td style={{ padding: '14px 8px' }}>
                      {u.must_change_password ? (
                        <span style={{ color: 'var(--warning)', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '4px' }}>
                          <AlertCircle size={12} /> {t("是")}
                        </span>
                      ) : (
                        <span style={{ color: 'var(--text-muted)', fontSize: '12px' }}>{t("否")}</span>
                      )}
                    </td>
                    <td style={{ padding: '14px 8px', color: 'var(--text-muted)', fontSize: '13px' }}>
                      {new Date(u.created_at).toLocaleString('zh-CN')}
                    </td>
                    <td style={{ padding: '14px 8px', textAlign: 'right' }}>
                      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px' }}>
                        <button
                          onClick={() => handleOpenReset(u)}
                          className="btn btn-secondary"
                          style={{ padding: '6px 10px', fontSize: '12px' }}
                          title={t("重置密码")}
                        >
                          <Key size={12} />
                          <span>{t("改密")}</span>
                        </button>
                        {u.id !== currentUser.id && (
                          <button
                            onClick={() => handleDeleteUser(u)}
                            className="btn btn-danger"
                            style={{ padding: '6px 10px', fontSize: '12px' }}
                            title={t("删除账号")}
                          >
                            <Trash2 size={12} />
                            <span>{t("删除")}</span>
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {/* Right Side: Create User Form or Password Reset Form */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}>
          
          {/* Create User Form */}
          <div className="glass" style={{ padding: '24px' }}>
            <h3 style={{ margin: '0 0 16px 0', fontSize: '15px', fontWeight: '600', color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: '6px' }}>
              <UserPlus size={16} style={{ color: 'var(--primary)' }} />
              <span>{t("新建系统账户")}</span>
            </h3>
            
            <form onSubmit={handleCreateUser} style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
              <div className="input-group">
                <span className="input-label">{t("用户名")}</span>
                <input
                  type="text"
                  className="input-field"
                  placeholder={t("请输入用户名")}
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  required
                />
              </div>

              <div className="input-group">
                <span className="input-label">{t("初始密码")}</span>
                <input
                  type="password"
                  className="input-field"
                  placeholder={t("请输入初始密码")}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                />
              </div>

              <div className="input-group">
                <span className="input-label">{t("角色权限")}</span>
                <select
                  className="input-field"
                  value={role}
                  onChange={(e) => setRole(e.target.value)}
                  style={{ cursor: 'pointer' }}
                >
                  <option value="analyst">{t("分析员 (Analyst)")}</option>
                  <option value="admin">{t("管理员 (Admin - 强制初登改密)")}</option>
                </select>
              </div>

              <button
                type="submit"
                className="btn btn-primary"
                style={{ width: '100%', justifyContent: 'center' }}
                disabled={loading}
              >
                <span>{t("创建账户")}</span>
              </button>
            </form>
          </div>

          {/* Reset Password Form (Conditional) */}
          {resettingUser && (
            <div className="glass" style={{ padding: '24px', border: '1px solid var(--primary)' }}>
              <h3 style={{ margin: '0 0 16px 0', fontSize: '15px', fontWeight: '600', color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: '6px' }}>
                <Key size={16} style={{ color: 'var(--warning)' }} />
                <span>{t("重置用户密码")}</span>
              </h3>
              
              <p style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '16px' }}>
              {t("正在为用户 ")} <b>{resettingUser.username}</b> {t(" 重置密码。")}
              </p>

              <form onSubmit={handleResetPasswordSubmit} style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
                <div className="input-group">
                  <span className="input-label">{t("新密码")}</span>
                  <input
                    type="password"
                    className="input-field"
                    placeholder={t("密码包含字母和数字组合")}
                    value={newPassword}
                    onChange={(e) => setNewPassword(e.target.value)}
                    required
                  />
                </div>

                <div style={{ display: 'flex', gap: '10px' }}>
                  <button
                    type="button"
                    onClick={() => setResettingUser(null)}
                    className="btn btn-secondary"
                    style={{ flex: 1, justifyContent: 'center' }}
                  >
                    <span>{t("取消")}</span>
                  </button>
                  <button
                    type="submit"
                    className="btn btn-primary"
                    style={{ flex: 1, justifyContent: 'center', background: 'var(--warning)' }}
                    disabled={loading}
                  >
                    <span>{t("确认重置")}</span>
                  </button>
                </div>
              </form>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
