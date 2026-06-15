import React, { useState } from 'react';
import { useAuthStore } from '../stores/authStore';
import { Key, Lock, AlertTriangle, ShieldCheck, CheckCircle2 } from 'lucide-react';

export default function ForceChangePassword() {
  const { user, changePassword, logout, loading, error, clearError } = useAuthStore();
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  
  const [success, setSuccess] = useState(false);
  const [validationError, setValidationError] = useState('');

  const handleSubmit = async (e) => {
    e.preventDefault();
    setValidationError('');
    clearError();

    if (!newPassword.trim()) {
      setValidationError('密码不能为空！');
      return;
    }

    if (newPassword !== confirmPassword) {
      setValidationError('两次输入的新密码不一致，请检查！');
      return;
    }

    if (newPassword.length < 6) {
      setValidationError('为了账户安全，新密码长度不能少于 6 位。');
      return;
    }

    const res = await changePassword(newPassword.trim());
    if (res) {
      setSuccess(true);
    }
  };

  if (success) {
    return (
      <div className="dashboard-container" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100vh', width: '100vw', background: 'var(--bg-dark)' }}>
        <div className="glass" style={{ width: '400px', padding: '32px', textAlign: 'center', display: 'flex', flexDirection: 'column', gap: '20px' }}>
          <div style={{ margin: '0 auto', background: 'rgba(52, 211, 153, 0.1)', color: 'var(--success)', borderRadius: '50%', width: '60px', height: '60px', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <CheckCircle2 size={32} />
          </div>
          <h2 style={{ fontSize: '20px', fontWeight: '700', color: 'var(--text-primary)', margin: 0 }}>密码修改成功！</h2>
          <p style={{ color: 'var(--text-secondary)', fontSize: '14px', lineHeight: '1.5' }}>
            系统已保存您的安全密码。为了使新密码生效，请重新登录系统。
          </p>
          <button 
            onClick={logout}
            className="btn btn-primary"
            style={{ width: '100%', justifyContent: 'center', marginTop: '10px' }}
          >
            <span>重新登录</span>
          </button>
        </div>
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100vh', width: '100vw', background: 'var(--bg-dark)' }}>
      <div className="glass" style={{ width: '440px', padding: '40px', display: 'flex', flexDirection: 'column', gap: '24px' }}>
        
        {/* Header */}
        <div style={{ textAlign: 'center', display: 'flex', flexDirection: 'column', gap: '8px' }}>
          <div style={{ margin: '0 auto', display: 'flex', alignItems: 'center', gap: '8px', fontWeight: '800', fontSize: '22px', color: 'var(--text-primary)' }}>
            <ShieldCheck size={28} style={{ color: 'var(--primary)' }} />
            <span>TARA AI Platform</span>
          </div>
          <span style={{ fontSize: '11px', textTransform: 'uppercase', color: 'var(--primary)', letterSpacing: '2px', fontWeight: '600' }}>
            账户首次登录安全保护 (BR-01)
          </span>
        </div>

        {/* Warning card */}
        <div style={{
          background: 'rgba(217, 119, 6, 0.08)',
          border: '1px solid rgba(217, 119, 6, 0.25)',
          color: 'var(--warning)',
          padding: '16px',
          borderRadius: '8px',
          fontSize: '13px',
          lineHeight: '1.5',
          display: 'flex',
          gap: '10px',
          alignItems: 'flex-start'
        }}>
          <AlertTriangle size={18} style={{ flexShrink: 0, marginTop: '2px' }} />
          <div>
            <b>需要修改初始密码</b>
            <p style={{ marginTop: '2px' }}>
              检测到当前账户 [<b>{user?.username}</b>] 拥有管理员或系统核心权限，但仍在使用初始密码。系统强制要求必须修改初始密码后才能进入工作台。
            </p>
          </div>
        </div>

        {/* Form errors */}
        {(validationError || error) && (
          <div style={{
            background: 'rgba(244, 63, 94, 0.08)',
            border: '1px solid rgba(244, 63, 94, 0.25)',
            color: '#e11d48',
            padding: '12px 14px',
            borderRadius: '8px',
            fontSize: '13px',
            display: 'flex',
            alignItems: 'center',
            gap: '8px'
          }}>
            <AlertTriangle size={15} />
            <span>{validationError || error}</span>
          </div>
        )}

        {/* Form */}
        <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
          <div className="input-group">
            <span className="input-label" style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
              <Key size={14} /> 设置新密码
            </span>
            <input
              type="password"
              className="input-field"
              placeholder="请输入新安全密码"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              required
            />
          </div>

          <div className="input-group">
            <span className="input-label" style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
              <Lock size={14} /> 确认新密码
            </span>
            <input
              type="password"
              className="input-field"
              placeholder="请再次输入新密码"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              required
            />
          </div>

          <div style={{ display: 'flex', gap: '12px', marginTop: '10px' }}>
            <button
              type="button"
              onClick={logout}
              className="btn btn-secondary"
              style={{ flex: 1, justifyContent: 'center' }}
            >
              <span>取消登录</span>
            </button>
            
            <button
              type="submit"
              className="btn btn-primary"
              style={{ flex: 2, justifyContent: 'center' }}
              disabled={loading}
            >
              {loading ? <div className="spinner"></div> : '提交修改'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
