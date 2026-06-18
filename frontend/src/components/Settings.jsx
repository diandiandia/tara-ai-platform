import { useI18n } from '../stores/i18nStore';
import React, { useEffect, useState } from 'react';
import { useTaraStore } from '../stores/taraStore';
import { useAuthStore } from '../stores/authStore';
import { ArrowLeft, ShieldAlert, Save, Key, Globe, Layers, AlertCircle, CheckCircle2 } from 'lucide-react';

export default function Settings({ setPage }) {
  const { t } = useI18n();
  const { settings, fetchSettings, saveSettings, testConnection, loading, error, clearError } = useTaraStore();
  const { user, isAdmin } = useAuthStore();

  const [apiBaseUrl, setApiBaseUrl] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [modelName, setModelName] = useState('');
  
  const [testResult, setTestResult] = useState(null); // { success: boolean, message: string }
  const [testing, setTesting] = useState(false);
  const [saveSuccess, setSaveSuccess] = useState(false);

  useEffect(() => {
    const loadSettings = async () => {
      const data = await fetchSettings();
      if (data) {
        setApiBaseUrl(data.api_base_url || '');
        setApiKey(data.api_key || '');
        setModelName(data.model_name || '');
      }
    };
    loadSettings();
  }, []);

  const handleSave = async (e) => {
    e.preventDefault();
    setSaveSuccess(false);
    setTestResult(null);

    if (!isAdmin()) {
      alert(t('权限不足！仅系统管理员可以修改全局大模型配置。'));
      return;
    }

    const res = await saveSettings(apiBaseUrl.trim(), apiKey.trim(), modelName.trim());
    if (res) {
      setSaveSuccess(true);
      setTimeout(() => {
        setSaveSuccess(false);
        setPage('projects');
      }, 1500);
    }
  };

  const handleTestConnection = async () => {
    setTesting(true);
    setTestResult(null);
    setSaveSuccess(false);

    const res = await testConnection(apiBaseUrl.trim(), apiKey.trim(), modelName.trim());
    setTesting(false);
    setTestResult(res);
  };

  const isEditable = isAdmin();

  return (
    <div className="dashboard-container" style={{ maxWidth: '720px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '16px', marginBottom: '32px' }}>
        <button 
          onClick={() => setPage('projects')} 
          className="btn btn-secondary"
          style={{ padding: '8px 12px' }}
          type="button"
        >
          <ArrowLeft size={14} /> {t("返回项目")}
        </button>
        <div>
          <h1 className="section-title" style={{ margin: 0 }}>{t("系统配置")}</h1>
          <p style={{ color: 'var(--text-secondary)', fontSize: '14px', marginTop: '4px' }}>
            {t("全局大模型接口及参数配置，支持一键校验结构化连通性 (BR-59, BR-71)。")}
          </p>
        </div>
      </div>

      {!isEditable && (
        <div style={{
          background: 'rgba(244, 63, 94, 0.1)',
          border: '1px solid rgba(244, 63, 94, 0.3)',
          color: '#e11d48',
          padding: '12px 16px',
          borderRadius: '8px',
          fontSize: '14px',
          marginBottom: '24px',
          display: 'flex',
          alignItems: 'center',
          gap: '8px'
        }}>
          <AlertCircle size={16} />
          <span>{t("您当前的账户角色为 “分析员”，仅可查看配置。如需修改，请使用 “管理员” 账户登录。")}</span>
        </div>
      )}

      {error && (
        <div style={{
          background: 'rgba(244, 63, 94, 0.1)',
          border: '1px solid rgba(244, 63, 94, 0.3)',
          color: '#e11d48',
          padding: '12px 16px',
          borderRadius: '8px',
          fontSize: '14px',
          marginBottom: '24px',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center'
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <ShieldAlert size={16} />
            <span>{t(error)}</span>
          </div>
          <button onClick={clearError} style={{ background: 'none', border: 'none', color: '#e11d48', cursor: 'pointer' }}>×</button>
        </div>
      )}

      {saveSuccess && (
        <div style={{
          background: 'rgba(52, 211, 153, 0.1)',
          border: '1px solid rgba(52, 211, 153, 0.3)',
          color: '#059669',
          padding: '12px 16px',
          borderRadius: '8px',
          fontSize: '14px',
          marginBottom: '24px',
          display: 'flex',
          alignItems: 'center',
          gap: '8px'
        }}>
          <CheckCircle2 size={16} />
          <span>{t("大模型配置已成功保存！")}</span>
        </div>
      )}

      <form onSubmit={handleSave} className="glass" style={{ padding: '32px' }}>
        <div className="input-group">
          <span className="input-label" style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
            <Globe size={14} /> {t("大模型 API 地址 (API Base URL)")}
          </span>
          <input
            type="url"
            className="input-field"
            placeholder={t("例如: https://api.openai.com/v1")}
            value={apiBaseUrl}
            onChange={(e) => setApiBaseUrl(e.target.value)}
            disabled={!isEditable}
            required
          />
        </div>

        <div className="input-group">
          <span className="input-label" style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
            <Key size={14} /> {t("API 密钥 (API Key / Token)")}
          </span>
          <input
            type="password"
            className="input-field"
            placeholder={t("请输入大模型授权密钥 (sk-...)")}
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            disabled={!isEditable}
            required
          />
          <span style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '2px' }}>
            {t("注: 若在沙盒内测试或不需要真实大模型调用，可以设置为")} <b>mock_test_key</b>
          </span>
        </div>

        <div className="input-group" style={{ marginBottom: '32px' }}>
          <span className="input-label" style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
            <Layers size={14} /> {t("大模型名称 (Model Name)")}
          </span>
          <input
            type="text"
            className="input-field"
            placeholder={t("例如: gpt-4o, gpt-3.5-turbo, custom-model")}
            value={modelName}
            onChange={(e) => setModelName(e.target.value)}
            disabled={!isEditable}
            required
          />
        </div>

        {/* Test Result Section */}
        {testResult && (
          <div style={{
            background: testResult.success ? 'rgba(52, 211, 153, 0.08)' : 'rgba(244, 63, 94, 0.08)',
            border: '1px solid',
            borderColor: testResult.success ? 'rgba(52, 211, 153, 0.3)' : 'rgba(244, 63, 94, 0.3)',
            color: testResult.success ? '#059669' : '#e11d48',
            padding: '16px',
            borderRadius: '8px',
            fontSize: '13px',
            marginBottom: '24px',
            lineHeight: '1.4'
          }}>
            <div style={{ fontWeight: '700', marginBottom: '4px', display: 'flex', alignItems: 'center', gap: '6px' }}>
              {testResult.success ? <CheckCircle2 size={15} /> : <AlertCircle size={15} />}
              <span>{testResult.success ? t('连通性测试成功！') : t('连通性测试失败！')}</span>
            </div>
            <p>{testResult.message}</p>
          </div>
        )}

        {/* Form Controls */}
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <button
            type="button"
            onClick={handleTestConnection}
            className="btn btn-secondary"
            disabled={loading || testing || !apiBaseUrl || !apiKey || !modelName}
          >
            {testing ? (
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                <div className="spinner"></div> {t("正在连通测试...")}
              </div>
            ) : t('测试结构化连通性')}
          </button>

          {isEditable && (
            <button
              type="submit"
              className="btn btn-primary"
              disabled={loading || testing}
            >
              <Save size={16} />
              <span>{t("保存配置")}</span>
            </button>
          )}
        </div>
      </form>
    </div>
  );
}
