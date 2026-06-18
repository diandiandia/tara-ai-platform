import { useI18n } from '../stores/i18nStore';
import React, { useEffect, useState } from 'react';
import { useTaraStore } from '../stores/taraStore';
import { 
  ArrowLeft, CheckSquare, ShieldAlert, Edit3, 
  Download, Plus, BookOpen, Layers, CheckCircle2
} from 'lucide-react';

export default function TaraResults({ setPage, domainId }) {
  const { t, language } = useI18n();
  const {
    taraResults,
    assets,
    fetchTaraResults,
    fetchAssets,
    updateTaraStep,
    submitManualOfflineResults,
    exportReport,
    loading,
    error,
    clearError
  } = useTaraStore();

  const [activeTab, setActiveTab] = useState('review'); // 'review' or 'matrix'

  // Edit Step Modal States
  const [showEditModal, setShowEditModal] = useState(false);
  const [editingStep, setEditingStep] = useState(null);
  const [editReason, setEditReason] = useState('');
  const [editFormData, setEditFormData] = useState({});

  // Export States
  const [exportFormat, setExportFormat] = useState('xlsx');
  const [exportDesensitize, setExportDesensitize] = useState(false);
  const [exporting, setExporting] = useState(false);

  // Manual Input States (Fail-Safe Offline Mode)
  const [showManualModal, setShowManualModal] = useState(false);
  const [manualAssetId, setManualAssetId] = useState('');
  const [manualStage, setManualStage] = useState('stage1');
  
  // Manual form values
  const [mS1Conf, setMS1Conf] = useState('High');
  const [mS1Int, setMS1Int] = useState('High');
  const [mS1Avail, setMS1Avail] = useState('Medium');
  const [mS1Desc, setMS1Desc] = useState('');

  const [mS2Scenario, setMS2Scenario] = useState('');
  const [mS2Saf, setMS2Saf] = useState(1);
  const [mS2Fin, setMS2Fin] = useState(1);
  const [mS2Ops, setMS2Ops] = useState(1);
  const [mS2Priv, setMS2Priv] = useState(1);

  const [mS3Threat, setMS3Threat] = useState('');
  const [mS3Feas, setMS3Feas] = useState('Medium');

  const [mS4Rating, setMS4Rating] = useState(3);
  const [mS4Decision, setMS4Decision] = useState('mitigate');
  const [mS4Justify, setMS4Justify] = useState('');

  const [mS5Cso, setMS5Cso] = useState('');
  const [mS5Csr, setMS5Csr] = useState('');

  useEffect(() => {
    if (domainId) {
      fetchTaraResults(domainId);
      fetchAssets(domainId);
    }
  }, [domainId]);

  const handleEditClick = (step) => {
    setEditingStep(step);
    setEditReason('');
    
    // Clone step's final output
    const finalOut = JSON.parse(JSON.stringify(step.analysis_result.final_output || {}));
    setEditFormData(finalOut);
    setShowEditModal(true);
  };

  const handleEditSubmit = async (e) => {
    e.preventDefault();
    if (!editReason.trim()) {
      alert('请填写修改原因为何以满足专家审计审计审计审计审计规则！');
      return;
    }

    const updated = await updateTaraStep(editingStep.id, editFormData, editReason.trim());
    if (updated) {
      setShowEditModal(false);
      setEditingStep(null);
      // Re-fetch step results to sync risk decisions to stage 5 (BR-69)
      fetchTaraResults(domainId);
    }
  };

  const handleManualSubmit = async (e) => {
    e.preventDefault();
    if (!manualAssetId) {
      alert('请选择需要录入数据的资产项');
      return;
    }

    let finalOutput = {};
    if (manualStage === 'stage1') {
      finalOutput = {
        confidentiality: mS1Conf,
        integrity: mS1Int,
        availability: mS1Avail,
        description: mS1Desc || t('手工安全属性分析。')
      };
    } else if (manualStage === 'stage2') {
      const s = parseInt(mS2Saf);
      const f = parseInt(mS2Fin);
      const o = parseInt(mS2Ops);
      const p = parseInt(mS2Priv);
      finalOutput = {
        damage_scenario: mS2Scenario || t('手工录入损害场景。'),
        impact_rating: { safety: s, financial: f, operational: o, privacy: p },
        overall_impact: Math.max(s, f, o, p)
      };
    } else if (manualStage === 'stage3') {
      finalOutput = {
        threat_scenario: mS3Threat || t('手工录入威胁场景。'),
        attack_paths: [
          { path_id: "P_MANUAL", method: t("手工定义攻击方法"), feasibility: mS3Feas }
        ],
        final_feasibility: mS3Feas
      };
    } else if (manualStage === 'stage4') {
      finalOutput = {
        risk_rating: parseInt(mS4Rating),
        risk_decision: mS4Decision,
        justification: mS4Justify || t('手工录入决策。')
      };
    } else if (manualStage === 'stage5') {
      const isExempted = mS4Decision === 'accept' || mS4Decision === 'transfer';
      finalOutput = {
        cso: isExempted ? t('无需制定安全目标') : (mS5Cso || t('手工定义安全目标。')),
        csr: isExempted ? [] : mS5Csr.split('\n').filter(line => line.trim()),
        exempted: isExempted,
        reason: isExempted ? t('人工选择免除安全控制目标') : ''
      };
    }

    const payload = {
      asset_id: parseInt(manualAssetId),
      stage: manualStage,
      output: finalOutput
    };

    const res = await submitManualOfflineResults(domainId, [payload]);
    if (res) {
      setShowManualModal(false);
      setManualAssetId('');
      // reset manual states
      setMS1Desc('');
      setMS2Scenario('');
      setMS3Threat('');
      setMS4Justify('');
      setMS5Cso('');
      setMS5Csr('');
      alert('手工故障备用数据导入成功！');
    }
  };

  const handleExportClick = async () => {
    setExporting(true);
    const success = await exportReport(domainId, exportFormat, exportDesensitize);
    setExporting(false);
    if (success) {
      alert(t('报告导出成功！文件已保存。'));
    }
  };

  const getStageLabel = (stage) => {
    switch (stage) {
      case 'stage1': return t('① 安全属性分析');
      case 'stage2': return t('② 损害评估 (SFOP)');
      case 'stage3': return t('③ 威胁与攻击可行性');
      case 'stage4': return t('④ 风险决策评估');
      case 'stage5': return t('⑤ CSR/CSO 控制目标');
      default: return stage;
    }
  };

  const getAssetLabel = (assetId) => {
    const asset = assets.find(a => a.id === assetId);
    return asset ? `${asset.name} (${asset.asset_type})` : `${t("资产")} #${assetId}`;
  };

  // Group steps by asset for Tab 2 Matrix
  const getGroupedMatrix = () => {
    const groups = {};
    taraResults.forEach(step => {
      if (!groups[step.asset_id]) {
        groups[step.asset_id] = {
          assetName: getAssetLabel(step.asset_id),
          cso: '',
          csrs: [],
          exempted: false
        };
      }
      if (step.stage === 'stage5') {
        const out = step.analysis_result.final_output || {};
        groups[step.asset_id].cso = out.cso || t('未定义');
        groups[step.asset_id].csrs = out.csr || [];
        groups[step.asset_id].exempted = out.exempted || false;
      }
    });
    return Object.values(groups);
  };

  const groupedMatrix = getGroupedMatrix();

  return (
    <div className="dashboard-container">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '32px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
          <button 
            onClick={() => setPage('workbench')} 
            className="btn btn-secondary"
            style={{ padding: '8px 12px' }}
          >
            <ArrowLeft size={14} /> {t("返回工作台")}
          </button>
          <div>
            <h1 className="section-title" style={{ margin: '0' }}>{t("TARA 评估结果审阅")}</h1>
            <p style={{ color: 'var(--text-secondary)', fontSize: '13px', marginTop: '4px' }}>
              {t("审阅大模型自动计算出的 5 阶段安全要求。支持专家人工干预修改结论与离线导入。")}
            </p>
          </div>
        </div>

        <div style={{ display: 'flex', gap: '12px' }}>
          <button
            onClick={() => setShowManualModal(true)}
            className="btn btn-secondary"
            style={{ border: '1px dashed var(--border-glow)' }}
          >
            <Plus size={16} />
            <span>{t("手工故障备用录入")}</span>
          </button>
        </div>
      </div>

      {error && (
        <div style={{
          background: 'rgba(244, 63, 94, 0.1)',
          border: '1px solid rgba(244, 63, 94, 0.3)',
          color: '#fda4af',
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
          <button onClick={clearError} style={{ background: 'none', border: 'none', color: '#fda4af', cursor: 'pointer' }}>×</button>
        </div>
      )}

      {/* Main Tabs */}
      <div style={{ display: 'flex', gap: '16px', borderBottom: '1px solid var(--border-color)', marginBottom: '24px', paddingBottom: '1px' }}>
        <button
          onClick={() => setActiveTab('review')}
          style={{
            background: 'none',
            border: 'none',
            color: activeTab === 'review' ? 'var(--primary)' : 'var(--text-secondary)',
            fontSize: '15px',
            fontWeight: '600',
            padding: '12px 20px',
            cursor: 'pointer',
            borderBottom: activeTab === 'review' ? '2px solid var(--primary)' : '2px solid transparent',
            marginBottom: '-1px',
            display: 'flex',
            alignItems: 'center',
            gap: '8px'
          }}
        >
          <CheckSquare size={16} />
          <span>{t("TARA 5 阶段审阅表")}</span>
        </button>

        <button
          onClick={() => setActiveTab('matrix')}
          style={{
            background: 'none',
            border: 'none',
            color: activeTab === 'matrix' ? 'var(--primary)' : 'var(--text-secondary)',
            fontSize: '15px',
            fontWeight: '600',
            padding: '12px 20px',
            cursor: 'pointer',
            borderBottom: activeTab === 'matrix' ? '2px solid var(--primary)' : '2px solid transparent',
            marginBottom: '-1px',
            display: 'flex',
            alignItems: 'center',
            gap: '8px'
          }}
        >
          <BookOpen size={16} />
          <span>{t("项目级安全控制矩阵")}</span>
        </button>
      </div>

      {loading && taraResults.length === 0 ? (
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', padding: '45px 0' }}>
          <div className="spinner"></div>
          <span style={{ color: 'var(--text-secondary)' }}>{t("正在加载评估数据...")}</span>
        </div>
      ) : taraResults.length === 0 ? (
        <div className="glass" style={{ padding: '60px 40px', textAlign: 'center', color: 'var(--text-secondary)' }}>
          <Layers size={48} style={{ color: 'var(--text-muted)', marginBottom: '16px' }} />
          <h3 style={{ fontSize: '18px', marginBottom: '8px', color: 'var(--text-primary)' }}>{t("没有找到分析记录")}</h3>
          <p style={{ fontSize: '14px', maxWidth: '440px', margin: '0 auto' }}>
            {t("该子系统域控尚未生成分析步骤数据。您可以在工作台点击“启动 TARA 分析”派发异步任务，或者使用右上角“手工故障备用录入”填入已有数据。")}
          </p>
        </div>
      ) : activeTab === 'review' ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '32px' }}>
          
          {/* Report Export Panel */}
          <div className="glass" style={{ padding: '20px 24px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '16px' }}>
            <div>
              <h4 style={{ fontSize: '15px', fontWeight: '600', color: 'var(--text-primary)' }}>{t("导出评估报告")}</h4>
              <p style={{ color: 'var(--text-secondary)', fontSize: '12px', marginTop: '2px' }}>{t("一键生成 XLSX 或 CSV 归档安全规范文档，保存在宿主机持久挂载卷")}</p>
            </div>
            
            <div style={{ display: 'flex', alignItems: 'center', gap: '16px', flexWrap: 'wrap' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <span style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>{t("文件格式:")}</span>
                <select
                  value={exportFormat}
                  onChange={(e) => setExportFormat(e.target.value)}
                  style={{ background: 'var(--bg-card)', border: '1px solid var(--border-color)', borderRadius: '4px', color: 'var(--text-primary)', padding: '6px 12px', outline: 'none', fontSize: '13px' }}
                >
                  <option value="xlsx">{t("Excel 工作簿 (.xlsx)")}</option>
                  <option value="csv">{t("CSV 文件 (.csv)")}</option>
                </select>
              </div>

              <label style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '13px', cursor: 'pointer', color: 'var(--text-secondary)' }}>
                <input
                  type="checkbox"
                  checked={exportDesensitize}
                  onChange={(e) => setExportDesensitize(e.target.checked)}
                  style={{ cursor: 'pointer' }}
                />
                <span>{t("导出脱敏版 (隐藏攻击路径/漏洞)")}</span>
              </label>

              <button
                onClick={handleExportClick}
                className="btn btn-primary"
                style={{ padding: '8px 16px' }}
                disabled={exporting}
              >
                {exporting ? <div className="spinner"></div> : <Download size={14} />}
                <span>{t("立即下载")}</span>
              </button>
            </div>
          </div>

          {/* Results Review Table */}
          <div className="table-container">
            <table className="custom-table">
              <thead>
                <tr>
                  <th style={{ width: '220px' }}>{t("资产项")}</th>
                  <th style={{ width: '180px' }}>{t("分析阶段")}</th>
                  <th>{t("评估结论")}</th>
                  <th style={{ width: '180px' }}>{t("安全控制要求/指标")}</th>
                  <th style={{ width: '110px' }}>{t("人工标记")}</th>
                  <th style={{ width: '100px' }}>{t("操作")}</th>
                </tr>
              </thead>
              <tbody>
                {taraResults.map((step) => {
                  const finalOut = step.analysis_result.final_output || {};
                  const isModified = step.analysis_result.is_human_modified;
                  
                  // Extract display parameters based on stage
                  let conclusion = '';
                  let metrics = '';
                  
                  if (step.stage === 'stage1') {
                    conclusion = finalOut.description || t('无 CIA 描述');
                    metrics = `C: ${finalOut.confidentiality || 'N/A'} • I: ${finalOut.integrity || 'N/A'} • A: ${finalOut.availability || 'N/A'}`;
                  } else if (step.stage === 'stage2') {
                    conclusion = finalOut.damage_scenario || t('无损害场景');
                    const ratings = finalOut.damage_ratings || finalOut.impact_rating || {};
                    metrics = `${t("整体影响")}: ${finalOut.overall_impact || 0} (S:${ratings.safety || 0} F:${ratings.financial || 0} O:${ratings.operational || 0} P:${ratings.privacy || 0})`;
                  } else if (step.stage === 'stage3') {
                    conclusion = finalOut.threat_scenario || t('无威胁场景');
                    metrics = `${t("最终可行性")}: ${finalOut.final_feasibility || 'N/A'}`;
                  } else if (step.stage === 'stage4') {
                    conclusion = finalOut.justification || t('无决策依据');
                    metrics = `${t("风险")}: ${finalOut.risk_rating || 0} ${t("决策")}: ${finalOut.risk_decision || 'N/A'}`;
                  } else if (step.stage === 'stage5') {
                    const isEx = finalOut.exempted;
                    conclusion = isEx ? `[${t("免除")}] ${finalOut.reason}` : `${t("安全目标")}: ${finalOut.cso || 'N/A'}`;
                    metrics = isEx ? t('无需CSR要求') : (language === 'zh' ? `包含 ${finalOut.csr?.length || 0} 条CSR` : `Contains ${finalOut.csr?.length || 0} CSR(s)`);
                  }

                  return (
                    <tr key={step.id}>
                      <td>
                        <span style={{ fontWeight: '500' }}>{getAssetLabel(step.asset_id)}</span>
                      </td>
                      <td>
                        <span style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>{getStageLabel(step.stage)}</span>
                      </td>
                      <td>
                        <p style={{ fontSize: '13px', color: 'var(--text-secondary)', lineHeight: '1.4', maxWidth: '400px' }}>
                          {conclusion}
                        </p>
                      </td>
                      <td>
                        <code style={{ fontSize: '11px', padding: '2px 6px', background: 'rgba(15, 23, 42, 0.03)', border: '1px solid var(--border-color)' }}>
                          {metrics}
                        </code>
                      </td>
                      <td>
                        {isModified ? (
                          <span style={{ fontSize: '11px', background: 'rgba(217, 119, 6, 0.08)', color: '#d97706', padding: '2px 8px', borderRadius: '4px', fontWeight: '600' }} title={`${t("修改原因")}: ${step.analysis_result.modification_reason}`}>
                            {t("专家人工修改")}
                          </span>
                        ) : (
                          <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>{t("AI 原始草案")}</span>
                        )}
                      </td>
                      <td>
                        <button
                          onClick={() => handleEditClick(step)}
                          className="btn btn-secondary"
                          style={{ padding: '6px 10px', fontSize: '12px' }}
                          title={t("人工修改此评估项")}
                        >
                          <Edit3 size={12} />
                          <span>{t("修改")}</span>
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      ) : (
        /* Tab 2 Matrix */
        <div style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}>
          <div className="glass" style={{ padding: '20px' }}>
            <h3 style={{ fontSize: '16px', color: 'var(--text-primary)', fontWeight: '600', marginBottom: '6px' }}>
              {t("安全控制矩阵汇总 (Consolidated CSR Matrix)")}
            </h3>
            <p style={{ color: 'var(--text-secondary)', fontSize: '13px' }}>
              {t("聚合该域控所有资产项。根据安全目标 (CSO) 对标输出的所有网络安全控制要求 (CSR) 一览。")}
            </p>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
            {groupedMatrix.map((item, idx) => (
              <div key={idx} className="glass" style={{ padding: '24px' }}>
                <h4 style={{ fontSize: '16px', fontWeight: '600', color: 'var(--primary)', marginBottom: '14px', borderBottom: '1px solid var(--border-color)', paddingBottom: '8px' }}>
                  {t("资产项")}: {item.assetName}
                </h4>

                {item.exempted ? (
                  <div style={{ color: 'var(--text-muted)', fontSize: '13px', display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <CheckCircle2 size={14} style={{ color: 'var(--success)' }} />
                    <span>{t("该威胁已被专家评估为接受风险/转移风险。安全需求已根据联动规则豁免制定。")}</span>
                  </div>
                ) : (
                  <div>
                    <div style={{ marginBottom: '14px' }}>
                      <span style={{ fontSize: '12px', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
                        {t("网络安全目标 (CSO):")}
                      </span>
                      <p style={{ color: 'var(--text-primary)', fontSize: '14px', fontWeight: '500', marginTop: '4px' }}>
                        {item.cso || t('未指定或分析未跑完')}
                      </p>
                    </div>

                    <div>
                      <span style={{ fontSize: '12px', color: 'var(--text-secondary)', textTransform: 'uppercase', letterSpacing: '0.5px' }}>
                        {t("网络安全控制要求 (CSR):")}
                      </span>
                      {item.csrs.length === 0 ? (
                        <p style={{ color: 'var(--text-muted)', fontSize: '13px', marginTop: '4px' }}>{t("无安全要求列表")}</p>
                      ) : (
                        <ul style={{ paddingLeft: '20px', marginTop: '8px', display: 'flex', flexDirection: 'column', gap: '8px' }}>
                          {item.csrs.map((csr, cIdx) => (
                            <li key={cIdx} style={{ fontSize: '13px', color: 'var(--text-secondary)', lineHeight: '1.4' }}>
                              {csr}
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Edit Conclusion Dialog (BR-51) */}
      {showEditModal && editingStep && (
        <div className="modal-overlay">
          <div className="modal-content glass" style={{ width: '560px', maxWidth: '95%' }}>
            <h3 style={{ fontSize: '18px', fontWeight: '600', color: 'var(--text-primary)', marginBottom: '20px' }}>
              {t("人工修订结论")} [{getStageLabel(editingStep.stage)}]
            </h3>

            <form onSubmit={handleEditSubmit}>
              <div style={{ maxHeight: '360px', overflowY: 'auto', paddingRight: '4px', marginBottom: '20px' }}>
                <div style={{ background: 'rgba(15, 23, 42, 0.02)', padding: '12px', borderRadius: '8px', border: '1px solid var(--border-color)', marginBottom: '16px', fontSize: '12px', color: 'var(--text-secondary)' }}>
                  <span><b>{t("修改资产:")}</b> {getAssetLabel(editingStep.asset_id)}</span>
                </div>

                {/* Render forms according to stage */}
                {editingStep.stage === 'stage1' && (
                  <div>
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '12px', marginBottom: '16px' }}>
                      <div className="input-group">
                        <span className="input-label">Confidentiality</span>
                        <select
                          className="input-field"
                          value={editFormData.confidentiality || 'Low'}
                          onChange={(e) => setEditFormData({ ...editFormData, confidentiality: e.target.value })}
                        >
                          <option value="High">High</option>
                          <option value="Medium">Medium</option>
                          <option value="Low">Low</option>
                        </select>
                      </div>
                      <div className="input-group">
                        <span className="input-label">Integrity</span>
                        <select
                          className="input-field"
                          value={editFormData.integrity || 'Low'}
                          onChange={(e) => setEditFormData({ ...editFormData, integrity: e.target.value })}
                        >
                          <option value="High">High</option>
                          <option value="Medium">Medium</option>
                          <option value="Low">Low</option>
                        </select>
                      </div>
                      <div className="input-group">
                        <span className="input-label">Availability</span>
                        <select
                          className="input-field"
                          value={editFormData.availability || 'Low'}
                          onChange={(e) => setEditFormData({ ...editFormData, availability: e.target.value })}
                        >
                          <option value="High">High</option>
                          <option value="Medium">Medium</option>
                          <option value="Low">Low</option>
                        </select>
                      </div>
                    </div>

                    <div className="input-group">
                      <span className="input-label">{t("破坏影响描述")}</span>
                      <textarea
                        className="input-field"
                        rows={3}
                        value={editFormData.description || ''}
                        onChange={(e) => setEditFormData({ ...editFormData, description: e.target.value })}
                        style={{ resize: 'none' }}
                      />
                    </div>
                  </div>
                )}

                {editingStep.stage === 'stage2' && (
                  <div>
                    <div className="input-group">
                      <span className="input-label">{t("潜在损害场景描述")}</span>
                      <textarea
                        className="input-field"
                        rows={3}
                        value={editFormData.damage_scenario || ''}
                        onChange={(e) => setEditFormData({ ...editFormData, damage_scenario: e.target.value })}
                        style={{ resize: 'none' }}
                      />
                    </div>

                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '8px', marginBottom: '16px' }}>
                      <div className="input-group">
                        <span className="input-label">Safety (S)</span>
                        <input
                          type="number"
                          className="input-field"
                          min="0" max="3"
                          value={editFormData.impact_rating?.safety || 0}
                          onChange={(e) => setEditFormData({
                            ...editFormData,
                            impact_rating: { ...editFormData.impact_rating, safety: parseInt(e.target.value) || 0 }
                          })}
                        />
                      </div>
                      <div className="input-group">
                        <span className="input-label">Financial (F)</span>
                        <input
                          type="number"
                          className="input-field"
                          min="0" max="3"
                          value={editFormData.impact_rating?.financial || 0}
                          onChange={(e) => setEditFormData({
                            ...editFormData,
                            impact_rating: { ...editFormData.impact_rating, financial: parseInt(e.target.value) || 0 }
                          })}
                        />
                      </div>
                      <div className="input-group">
                        <span className="input-label">Operational (O)</span>
                        <input
                          type="number"
                          className="input-field"
                          min="0" max="3"
                          value={editFormData.impact_rating?.operational || 0}
                          onChange={(e) => setEditFormData({
                            ...editFormData,
                            impact_rating: { ...editFormData.impact_rating, operational: parseInt(e.target.value) || 0 }
                          })}
                        />
                      </div>
                      <div className="input-group">
                        <span className="input-label">Privacy (P)</span>
                        <input
                          type="number"
                          className="input-field"
                          min="0" max="3"
                          value={editFormData.impact_rating?.privacy || 0}
                          onChange={(e) => setEditFormData({
                            ...editFormData,
                            impact_rating: { ...editFormData.impact_rating, privacy: parseInt(e.target.value) || 0 }
                          })}
                        />
                      </div>
                    </div>
                  </div>
                )}

                {editingStep.stage === 'stage3' && (
                  <div>
                    <div className="input-group">
                      <span className="input-label">{t("潜在威胁场景描述")}</span>
                      <textarea
                        className="input-field"
                        rows={3}
                        value={editFormData.threat_scenario || ''}
                        onChange={(e) => setEditFormData({ ...editFormData, threat_scenario: e.target.value })}
                        style={{ resize: 'none' }}
                      />
                    </div>

                    <div className="input-group">
                      <span className="input-label">{t("最终攻击可行性等级 (Feasibility)")}</span>
                      <select
                        className="input-field"
                        value={editFormData.final_feasibility || 'Medium'}
                        onChange={(e) => setEditFormData({ ...editFormData, final_feasibility: e.target.value })}
                      >
                        <option value="Very High">Very High</option>
                        <option value="High">High</option>
                        <option value="Medium">Medium</option>
                        <option value="Low">Low</option>
                        <option value="Very Low">Very Low</option>
                      </select>
                    </div>
                  </div>
                )}

                {editingStep.stage === 'stage4' && (
                  <div>
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: '12px', marginBottom: '16px' }}>
                      <div className="input-group">
                        <span className="input-label">{t("计算出的风险值 (Rating)")}</span>
                        <input
                          type="number"
                          className="input-field"
                          value={editFormData.risk_rating || 0}
                          onChange={(e) => setEditFormData({ ...editFormData, risk_rating: parseInt(e.target.value) || 0 })}
                        />
                      </div>
                      <div className="input-group">
                        <span className="input-label">{t("安全处理决策 (Decision)")}</span>
                        <select
                          className="input-field"
                          value={editFormData.risk_decision || 'mitigate'}
                          onChange={(e) => setEditFormData({ ...editFormData, risk_decision: e.target.value })}
                        >
                          <option value="mitigate">{t("Mitigate (缓解风险)")}</option>
                          <option value="accept">{t("Accept (接受风险 - 免除CSR)")}</option>
                          <option value="transfer">{t("Transfer (转移风险 - 免除CSR)")}</option>
                          <option value="avoid">{t("Avoid (规避风险)")}</option>
                        </select>
                      </div>
                    </div>

                    <div className="input-group">
                      <span className="input-label">{t("合理性说明 (Justification)")}</span>
                      <textarea
                        className="input-field"
                        rows={3}
                        value={editFormData.justification || ''}
                        onChange={(e) => setEditFormData({ ...editFormData, justification: e.target.value })}
                        style={{ resize: 'none' }}
                      />
                    </div>
                  </div>
                )}

                {editingStep.stage === 'stage5' && (
                  <div>
                    <div className="input-group">
                      <span className="input-label">{t("网络安全目标 (CSO)")}</span>
                      <input
                        type="text"
                        className="input-field"
                        value={editFormData.cso || ''}
                        onChange={(e) => setEditFormData({ ...editFormData, cso: e.target.value })}
                        disabled={editFormData.exempted}
                      />
                    </div>

                    <div className="input-group">
                      <span className="input-label">{t("网络安全控制要求列表 (CSR，一行写一条)")}</span>
                      <textarea
                        className="input-field"
                        rows={5}
                        placeholder={t("每行填入一条安全要求...")}
                        value={editFormData.csr?.join('\n') || ''}
                        onChange={(e) => setEditFormData({ ...editFormData, csr: e.target.value.split('\n') })}
                        style={{ resize: 'none' }}
                        disabled={editFormData.exempted}
                      />
                    </div>
                  </div>
                )}

                <div className="input-group">
                  <span className="input-label" style={{ color: '#fde047', fontWeight: '600' }}>
                    {t("修改原因说明 (Mandatory审计留痕)")} <span style={{ color: 'var(--accent)' }}>*</span>
                  </span>
                  <input
                    type="text"
                    className="input-field"
                    placeholder={t("例如: 结合实际网络物理拓扑过滤了虚警")}
                    value={editReason}
                    onChange={(e) => setEditReason(e.target.value)}
                    required
                  />
                </div>
              </div>

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '12px' }}>
                <button
                  type="button"
                  onClick={() => setShowEditModal(false)}
                  className="btn btn-secondary"
                >
                  {t("取消")}
                </button>
                <button
                  type="submit"
                  className="btn btn-primary"
                >
                  {t("确认保存修订")}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Manual Input Modal (BR-70, Fail-safe) */}
      {showManualModal && (
        <div className="modal-overlay">
          <div className="modal-content glass" style={{ width: '560px', maxWidth: '95%' }}>
            <h3 style={{ fontSize: '18px', fontWeight: '600', color: 'var(--text-primary)', marginBottom: '20px' }}>
              {t("手动录入 TARA 评估指标 (脱网备用)")}
            </h3>

            <form onSubmit={handleManualSubmit}>
              <div style={{ maxHeight: '380px', overflowY: 'auto', paddingRight: '4px', marginBottom: '20px' }}>
                <div className="input-group">
                  <span className="input-label">{t("选择目标资产项")}</span>
                  <select
                    className="input-field"
                    value={manualAssetId}
                    onChange={(e) => setManualAssetId(e.target.value)}
                    required
                  >
                    <option value="">{t("-- 请选择资产 --")}</option>
                    {assets.filter(a => a.status === 'confirmed').map(a => (
                      <option key={a.id} value={a.id}>{a.name} ({a.asset_type})</option>
                    ))}
                  </select>
                </div>

                <div className="input-group">
                  <span className="input-label">{t("选择分析阶段")}</span>
                  <select
                    className="input-field"
                    value={manualStage}
                    onChange={(e) => setManualStage(e.target.value)}
                  >
                    <option value="stage1">{t("① 安全属性分析")}</option>
                    <option value="stage2">{t("② 损害评估 (SFOP)")}</option>
                    <option value="stage3">{t("③ 威胁与攻击可行性")}</option>
                    <option value="stage4">{t("④ 风险决策评估")}</option>
                    <option value="stage5">{t("⑤ CSR/CSO 控制目标")}</option>
                  </select>
                </div>

                <div style={{ borderTop: '1px solid var(--border-color)', paddingTop: '16px', marginTop: '16px' }}>
                  {manualStage === 'stage1' && (
                    <div>
                      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '10px', marginBottom: '14px' }}>
                        <div className="input-group">
                          <span className="input-label">Confidentiality</span>
                          <select className="input-field" value={mS1Conf} onChange={(e) => setMS1Conf(e.target.value)}>
                            <option value="High">High</option><option value="Medium">Medium</option><option value="Low">Low</option>
                          </select>
                        </div>
                        <div className="input-group">
                          <span className="input-label">Integrity</span>
                          <select className="input-field" value={mS1Int} onChange={(e) => setMS1Int(e.target.value)}>
                            <option value="High">High</option><option value="Medium">Medium</option><option value="Low">Low</option>
                          </select>
                        </div>
                        <div className="input-group">
                          <span className="input-label">Availability</span>
                          <select className="input-field" value={mS1Avail} onChange={(e) => setMS1Avail(e.target.value)}>
                            <option value="High">High</option><option value="Medium">Medium</option><option value="Low">Low</option>
                          </select>
                        </div>
                      </div>
                      <div className="input-group">
                        <span className="input-label">{t("描述")}</span>
                        <input type="text" className="input-field" value={mS1Desc} onChange={(e) => setMS1Desc(e.target.value)} placeholder={t("分析CIA破坏场景原因...")} />
                      </div>
                    </div>
                  )}

                  {manualStage === 'stage2' && (
                    <div>
                      <div className="input-group">
                        <span className="input-label">{t("潜在损害场景")}</span>
                        <input type="text" className="input-field" value={mS2Scenario} onChange={(e) => setMS2Scenario(e.target.value)} placeholder={t("例如: 刹车失效, 动力异常等")} required />
                      </div>
                      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '8px' }}>
                        <div className="input-group">
                          <span className="input-label">S (Safety)</span>
                          <input type="number" className="input-field" min="0" max="3" value={mS2Saf} onChange={(e) => setMS2Saf(e.target.value)} />
                        </div>
                        <div className="input-group">
                          <span className="input-label">F (Finance)</span>
                          <input type="number" className="input-field" min="0" max="3" value={mS2Fin} onChange={(e) => setMS2Fin(e.target.value)} />
                        </div>
                        <div className="input-group">
                          <span className="input-label">O (Ops)</span>
                          <input type="number" className="input-field" min="0" max="3" value={mS2Ops} onChange={(e) => setMS2Ops(e.target.value)} />
                        </div>
                        <div className="input-group">
                          <span className="input-label">P (Privacy)</span>
                          <input type="number" className="input-field" min="0" max="3" value={mS2Priv} onChange={(e) => setMS2Priv(e.target.value)} />
                        </div>
                      </div>
                    </div>
                  )}

                  {manualStage === 'stage3' && (
                    <div>
                      <div className="input-group">
                        <span className="input-label">{t("威胁场景")}</span>
                        <input type="text" className="input-field" value={mS3Threat} onChange={(e) => setMS3Threat(e.target.value)} placeholder={t("描述可能遭受的攻击场景...")} required />
                      </div>
                      <div className="input-group">
                        <span className="input-label">{t("攻击可行性 (Feasibility)")}</span>
                        <select className="input-field" value={mS3Feas} onChange={(e) => setMS3Feas(e.target.value)}>
                          <option value="Very High">Very High</option><option value="High">High</option><option value="Medium">Medium</option><option value="Low">Low</option><option value="Very Low">Very Low</option>
                        </select>
                      </div>
                    </div>
                  )}

                  {manualStage === 'stage4' && (
                    <div>
                      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: '10px' }}>
                        <div className="input-group">
                          <span className="input-label">{t("风险值 (0~10)")}</span>
                          <input type="number" className="input-field" min="0" max="10" value={mS4Rating} onChange={(e) => setMS4Rating(e.target.value)} />
                        </div>
                        <div className="input-group">
                          <span className="input-label">{t("风险处理决策")}</span>
                          <select className="input-field" value={mS4Decision} onChange={(e) => setMS4Decision(e.target.value)}>
                            <option value="mitigate">{t("Mitigate (缓解风险)")}</option>
                            <option value="accept">{t("Accept (接受风险 - 免除CSR)")}</option>
                            <option value="transfer">{t("Transfer (转移风险 - 免除CSR)")}</option>
                            <option value="avoid">{t("Avoid (规避风险)")}</option>
                          </select>
                        </div>
                      </div>
                      <div className="input-group">
                        <span className="input-label">{t("决策依据说明")}</span>
                        <input type="text" className="input-field" value={mS4Justify} onChange={(e) => setMS4Justify(e.target.value)} />
                      </div>
                    </div>
                  )}

                  {manualStage === 'stage5' && (
                    <div>
                      <div className="input-group">
                        <span className="input-label">{t("网络安全目标 (CSO)")}</span>
                        <input type="text" className="input-field" value={mS5Cso} onChange={(e) => setMS5Cso(e.target.value)} placeholder={t("保护资产不受XXX威胁...")} />
                      </div>
                      <div className="input-group">
                        <span className="input-label">{t("安全要求列表 (CSR，一行填写一条)")}</span>
                        <textarea className="input-field" rows={4} value={mS5Csr} onChange={(e) => setMS5Csr(e.target.value)} placeholder={t("例如: 开启身份鉴权\n对报文施加CAN签名验证...")} />
                      </div>
                    </div>
                  )}
                </div>
              </div>

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '12px' }}>
                <button
                  type="button"
                  onClick={() => setShowManualModal(false)}
                  className="btn btn-secondary"
                >
                  {t("取消")}
                </button>
                <button
                  type="submit"
                  className="btn btn-primary"
                >
                  {t("录入存盘")}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  );
}
