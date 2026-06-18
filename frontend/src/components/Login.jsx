import { useI18n } from '../stores/i18nStore';
import React, { useState } from 'react';
import { useAuthStore } from '../stores/authStore';
import { Shield, Lock, User as UserIcon } from 'lucide-react';

export default function Login() {
  const { t } = useI18n();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const { login, loading, error, clearError } = useAuthStore();

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!username || !password) return;
    await login(username, password);
  };

  return (
    <div style={{
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      minHeight: 'calc(100vh - 64px)',
      width: '100vw',
      padding: '20px'
    }}>
      <div className="glass" style={{
        width: '420px',
        padding: '36px',
        boxShadow: '0 10px 40px rgba(0, 0, 0, 0.08)',
        border: '1px solid var(--border-color)'
      }}>
        <div style={{ textAlign: 'center', marginBottom: '32px' }}>
          <div style={{
            display: 'inline-flex',
            alignItems: 'center',
            justifyContent: 'center',
            width: '60px',
            height: '60px',
            borderRadius: '50%',
            background: 'rgba(99, 102, 241, 0.1)',
            color: 'var(--primary)',
            marginBottom: '16px'
          }}>
            <Shield size={32} />
          </div>
          <h2 style={{ fontSize: '24px', fontWeight: '700', letterSpacing: '-0.5px' }}>
            {t("TARA AI 分析平台")}
          </h2>
          <p style={{ color: 'var(--text-secondary)', fontSize: '13px', marginTop: '6px' }}>
            {t("汽车网络安全威胁建模与分析系统 (v3)")}
          </p>
        </div>

        {error && (
          <div style={{
            background: 'rgba(244, 63, 94, 0.1)',
            border: '1px solid rgba(244, 63, 94, 0.3)',
            color: '#e11d48',
            padding: '12px',
            borderRadius: '8px',
            fontSize: '13px',
            marginBottom: '20px',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center'
          }}>
            <span>{t(error)}</span>
            <button onClick={clearError} style={{
              background: 'none',
              border: 'none',
              color: '#e11d48',
              cursor: 'pointer',
              fontWeight: 'bold'
            }}>×</button>
          </div>
        )}

        <form onSubmit={handleSubmit}>
          <div className="input-group">
            <span className="input-label" style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
              <UserIcon size={14} /> {t("用户名")}
            </span>
            <input
              type="text"
              className="input-field"
              placeholder={t("请输入用户名")}
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
            />
          </div>

          <div className="input-group" style={{ marginBottom: '24px' }}>
            <span className="input-label" style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
              <Lock size={14} /> {t("密码")}
            </span>
            <input
              type="password"
              className="input-field"
              placeholder={t("请输入密码")}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>

          <button
            type="submit"
            className="btn btn-primary"
            style={{ width: '100%', padding: '12px', fontSize: '15px' }}
            disabled={loading}
          >
            {loading ? (
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', justifyContent: 'center' }}>
                <div className="spinner"></div> {t("正在登录...")}
              </div>
            ) : t('立即登录')}
          </button>
        </form>

        <div style={{ textAlign: 'center', marginTop: '24px', fontSize: '12px', color: 'var(--text-muted)' }}>
          {t("提示: 初始管理员账户为 admin / Admin123")}
        </div>
      </div>
    </div>
  );
}
