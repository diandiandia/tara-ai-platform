import { useI18n } from '../stores/i18nStore';
import { useEffect, useState, useRef, useCallback } from 'react';
import { useProjectStore } from '../stores/projectStore';
import { useCanvasStore } from '../stores/canvasStore';
import { useTaraStore } from '../stores/taraStore';
import { 
  FolderPlus, GitFork, Table, Play, XCircle, 
  Trash2, AlertTriangle, ArrowLeft,
  Sparkles, CheckCircle2, Eye, Download, Upload, Merge, FileDown
} from 'lucide-react';
import {
  importCustomerRequirements,
  previewCustomerRequirements,
  synthesizeMergedRequirements,
  exportMergedRequirements,
  fetchCustomerRequirements,
  fetchMergedRequirements,
} from '../stores/reqMergeStore';

export default function Workbench({ setPage, setDomainId, setDiagramId, projectId }) {
  const { t } = useI18n();
  const { 
    currentProject, 
    domains, 
    activeDomain, 
    fetchProjectDetails, 
    fetchDomains, 
    createDomain, 
    deleteDomain,
    setActiveDomain 
  } = useProjectStore();

  const { 
    diagrams, 
    fetchDiagrams, 
    createDiagram, 
    deleteDiagram 
  } = useCanvasStore();

  const { 
    assets, 
    fetchAssets, 
    confirmAsset, 
    extractAssets, 
    clearAssets,
    fetchDeduplicateSuggestions,
    startTaraAnalysis,
    cancelTaraAnalysis,
    fetchTaraProgress,
    createManualAsset,
    deleteAsset,
    error: taraError,
    clearError: clearTaraError
  } = useTaraStore();

  const [showDomainModal, setShowDomainModal] = useState(false);
  const [newDomainName, setNewDomainName] = useState('');
  const [domainModalError, setDomainModalError] = useState('');
  const [showOnboardingModal, setShowOnboardingModal] = useState(false);
  const [onboardCreatedDomain, setOnboardCreatedDomain] = useState(null);

  const [showDfdModal, setShowDfdModal] = useState(false);
  const [newDfdTitle, setNewDfdTitle] = useState('');
  const [dfdModalError, setDfdModalError] = useState('');

  // Manual Asset Modal State
  const [showAddAssetModal, setShowAddAssetModal] = useState(false);
  const [newAssetName, setNewAssetName] = useState('');
  const [newAssetType, setNewAssetType] = useState('data');
  const [newAssetProtocol, setNewAssetProtocol] = useState('');
  const [newAssetDesc, setNewAssetDesc] = useState('');
  const [addAssetModalError, setAddAssetModalError] = useState('');

  // AI Deduplication Modal
  const [showDeduplicateModal, setShowDeduplicateModal] = useState(false);
  const [suggestionStatus, setSuggestionStatus] = useState('');
  const [originalAssets, setOriginalAssets] = useState([]);
  const [optimizedAssets, setOptimizedAssets] = useState([]);
  const [leftSearch, setLeftSearch] = useState('');
  const [rightSearch, setRightSearch] = useState('');
  const [compactMode, setCompactMode] = useState(true);
  const [expandedAssetIds, setExpandedAssetIds] = useState(new Set());

  // OEM 客户需求导入 & 最终需求合并
  const [oemImporting, setOemImporting] = useState(false);
  const [merging, setMerging] = useState(false);
  const [mergeMsg, setMergeMsg] = useState(null); // { type: 'success' | 'error', text: string }
  const [customerReqCount, setCustomerReqCount] = useState(0);
  const [mergedCount, setMergedCount] = useState(0);
  const [oemPreviewFile, setOemPreviewFile] = useState(null);
  const [oemPreview, setOemPreview] = useState(null); // { sheets, sheet, headers, preview, row_count }
  const [oemMapping, setOemMapping] = useState({ id_column: null, text_column: null, section_column: null, category_column: null });
  const oemFileRef = useRef(null);

  const progressIntervalRef = useRef(null);

  const stopProgressPolling = () => {
    if (progressIntervalRef.current) {
      clearInterval(progressIntervalRef.current);
      progressIntervalRef.current = null;
    }
  };

  const refreshCustomerReqCount = useCallback(async () => {
    if (!projectId) return;
    try {
      const rows = await fetchCustomerRequirements(projectId);
      setCustomerReqCount(Array.isArray(rows) ? rows.length : 0);
    } catch {
      setCustomerReqCount(0);
    }
  }, [projectId]);

  const refreshMergedCount = useCallback(async (domainId) => {
    if (!domainId) {
      setMergedCount(0);
      return;
    }
    try {
      const merged = await fetchMergedRequirements(domainId);
      setMergedCount(Array.isArray(merged) ? merged.length : 0);
    } catch {
      setMergedCount(0);
    }
  }, []);

  const guessColumn = (headers, keywords) => {
    const idx = headers.findIndex((h) =>
      keywords.some((k) => String(h).toLowerCase().includes(String(k).toLowerCase()))
    );
    return idx >= 0 ? idx + 1 : null;
  };

  const handleOemFileChange = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    setMergeMsg(null);
    setOemPreviewFile(file);
    try {
      const data = await previewCustomerRequirements(projectId, file);
      setOemPreview(data);
      setOemMapping({
        id_column: guessColumn(data.headers, ['编号', 'ID', 'requirement id']),
        text_column: guessColumn(data.headers, ['描述', '要求', 'requirement', 'description']),
        section_column: guessColumn(data.headers, ['章节', 'section']),
        category_column: guessColumn(data.headers, ['分类', '领域', 'category']),
      });
    } catch (err) {
      setOemPreviewFile(null);
      setMergeMsg({ type: 'error', text: err?.response?.data?.detail || t("客户需求预览失败") });
    }
  };

  const handleOemSheetChange = async (sheetName) => {
    if (!oemPreviewFile) return;
    try {
      const data = await previewCustomerRequirements(projectId, oemPreviewFile, sheetName);
      setOemPreview(data);
      setOemMapping({
        id_column: guessColumn(data.headers, ['编号', 'ID', 'requirement id']),
        text_column: guessColumn(data.headers, ['描述', '要求', 'requirement', 'description']),
        section_column: guessColumn(data.headers, ['章节', 'section']),
        category_column: guessColumn(data.headers, ['分类', '领域', 'category']),
      });
    } catch (err) {
      setMergeMsg({ type: 'error', text: err?.response?.data?.detail || t("客户需求预览失败") });
    }
  };

  const handleConfirmOemImport = async () => {
    if (!oemPreviewFile) return;
    if (!oemMapping.id_column || !oemMapping.text_column) {
      setMergeMsg({ type: 'error', text: t("请指定「需求编号列」和「需求描述列」") });
      return;
    }
    setOemImporting(true);
    setMergeMsg(null);
    try {
      const res = await importCustomerRequirements(projectId, oemPreviewFile, {
        sheet_name: oemPreview?.sheet,
        ...oemMapping,
      });
      await refreshCustomerReqCount();
      setMergeMsg({ type: 'success', text: `${t("已导入")} ${res.imported} ${t("条客户需求")}` });
      setOemPreview(null);
      setOemPreviewFile(null);
    } catch (err) {
      setMergeMsg({ type: 'error', text: err?.response?.data?.detail || t("客户需求导入失败") });
    } finally {
      setOemImporting(false);
    }
  };

  const handleMergeRequirements = async () => {
    if (!activeDomain) return;
    setMerging(true);
    setMergeMsg(null);
    try {
      const res = await synthesizeMergedRequirements(activeDomain.id);
      setMergedCount(res.merged_count || 0);
      setMergeMsg({
        type: 'success',
        text: `${t("合并完成")}：${res.merged_count || 0} ${t("条最终需求")}`,
      });
    } catch (err) {
      setMergeMsg({ type: 'error', text: err?.response?.data?.detail || t("需求合并失败") });
    } finally {
      setMerging(false);
    }
  };

  const handleDownloadMerged = async () => {
    if (!activeDomain) return;
    try {
      const blob = await exportMergedRequirements(activeDomain.id);
      const url = window.URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `Merged_Requirements_${activeDomain.name}.xlsx`;
      a.click();
      window.URL.revokeObjectURL(url);
    } catch (err) {
      setMergeMsg({ type: 'error', text: err?.response?.data?.detail || t("导出最终需求失败") });
    }
  };

  // Start polling TARA run progress
  const startProgressPolling = useCallback((domainId) => {
    stopProgressPolling();
    progressIntervalRef.current = setInterval(async () => {
      const run = await fetchTaraProgress(domainId);
      if (run) {
        if (run.status !== 'running' && run.status !== 'pending') {
          stopProgressPolling();
          // Re-fetch project details to update status & tree
          fetchProjectDetails(projectId);
          fetchDomains(projectId);
        }
      }
    }, 3000);
  }, [projectId, fetchDomains, fetchProjectDetails, fetchTaraProgress]);

  // Initial Fetch
  useEffect(() => {
    if (projectId) {
      fetchProjectDetails(projectId);
      fetchDomains(projectId);
      refreshCustomerReqCount();
    }
    return () => {
      stopProgressPolling();
    };
  }, [projectId, fetchDomains, fetchProjectDetails, refreshCustomerReqCount]);

  // Fetch Domain Details when Active Domain changes
  useEffect(() => {
    if (activeDomain) {
      fetchDiagrams(activeDomain.id);
      fetchAssets(activeDomain.id);
      fetchTaraProgress(activeDomain.id);
      refreshMergedCount(activeDomain.id);
      
      // If domain is running, start polling progress
      if (activeDomain.status === 'running') {
        startProgressPolling(activeDomain.id);
      } else {
        stopProgressPolling();
      }
    } else {
      setMergedCount(0);
      stopProgressPolling();
    }
  }, [activeDomain, fetchAssets, fetchDiagrams, fetchTaraProgress, startProgressPolling, refreshMergedCount]);

  const handleCreateDomainSubmit = async (e) => {
    e.preventDefault();
    setDomainModalError('');

    if (!newDomainName.trim()) {
      setDomainModalError(t('子域控名称不能为空'));
      return;
    }
    if (newDomainName.length > 50) {
      setDomainModalError(t('子域控名称不能超过 50 个字符'));
      return;
    }

    const domain = await createDomain(projectId, newDomainName.trim());
    if (domain) {
      setShowDomainModal(false);
      setNewDomainName('');
      setActiveDomain(domain);
      
      // Onboarding step (BR-4.2.2)
      setOnboardCreatedDomain(domain);
      setShowOnboardingModal(true);
    }
  };

  const handleCreateDfdSubmit = async (e) => {
    e.preventDefault();
    setDfdModalError('');

    if (!newDfdTitle.trim()) {
      setDfdModalError(t('功能图标题不能为空'));
      return;
    }
    if (newDfdTitle.length > 100) {
      setDfdModalError(t('功能图标题不能超过 100 个字符'));
      return;
    }

    const diagram = await createDiagram(activeDomain.id, newDfdTitle.trim());
    if (diagram) {
      setShowDfdModal(false);
      setNewDfdTitle('');
      fetchDiagrams(activeDomain.id);
    }
  };

  const handleDeleteDomainClick = async (e, d) => {
    e.stopPropagation();
    if (d.status === 'running') return;
    if (window.confirm(t('确定要删除子域控 "') + d.name + t('" 吗？这将会级联清除其关联的功能图、提取的资产和 TARA 评估报告。'))) {
      await deleteDomain(d.id);
    }
  };

  const handleDeleteDfdClick = async (e, diagId, title) => {
    e.stopPropagation();
    if (activeDomain.status === 'running') return;
    if (window.confirm(t('确定要删除功能图 "') + title + t('" 吗？该画布提取出的资产也将级联清除。'))) {
      await deleteDiagram(diagId);
      fetchDiagrams(activeDomain.id);
      fetchAssets(activeDomain.id);
    }
  };

  const handleAssetStatusChange = async (assetId, newStatus) => {
    await confirmAsset(assetId, { status: newStatus });
  };

  const handleAssetFieldChange = (assetId, field, value) => {
    useTaraStore.setState((state) => ({
      assets: state.assets.map((a) => (a.id === assetId ? { ...a, [field]: value } : a))
    }));
  };

  const handleAssetFieldBlur = async (assetId, field, value) => {
    await confirmAsset(assetId, { [field]: value });
  };

  const handleDeleteAsset = async (asset) => {
    if (window.confirm(t('确定要彻底删除资产 "') + asset.name + t('" 吗？'))) {
      await deleteAsset(asset.id);
    }
  };

  const handleCreateManualAssetSubmit = async (e) => {
    e.preventDefault();
    setAddAssetModalError('');
    if (!newAssetName.trim()) {
      setAddAssetModalError(t('资产名称不能为空！'));
      return;
    }
    const res = await createManualAsset(
      activeDomain.id,
      newAssetName.trim(),
      newAssetType,
      newAssetProtocol.trim() || null,
      newAssetDesc.trim() || null
    );
    if (res) {
      setShowAddAssetModal(false);
      setNewAssetName('');
      setNewAssetType('data');
      setNewAssetProtocol('');
      setNewAssetDesc('');
    }
  };

  const handleExtractAssets = async () => {
    if (!activeDomain) return;
    const res = await extractAssets(activeDomain.id);
    if (res) {
      alert(t("资产自动提取成功！已提取/同步 ") + res.length + t(" 个资产项目。"));
    }
  };

  const handleClearAssets = async () => {
    if (!activeDomain) return;
    if (window.confirm(t("确定要清空当前域控下的所有资产吗？此操作无法撤销。"))) {
      await clearAssets(activeDomain.id);
    }
  };

  const handleExportAssetsCSV = () => {
    if (!assets || assets.length === 0) {
      alert(t('无可导出的资产数据！'));
      return;
    }
    
    // 带 UTF-8 BOM，防止 Excel 打开中文乱码
    let csvContent = '\uFEFF';
    csvContent += t('序号,资产名称,资产类型,通信协议,备注说明,专家核对状态\n');
    
    const typeMap = {
      data: t('数据资产'),
      software: t('软件资产'),
      hardware: t('硬件资产'),
      communication: t('通信资产')
    };
    
    const statusMap = {
      draft: t('待核对'),
      confirmed: t('已确认'),
      rejected: t('已拒绝')
    };
    
    const typeOrder = {
      hardware: 1,
      software: 2,
      data: 3,
      communication: 4
    };
    const sortedAssets = [...assets].sort((a, b) => {
      const tA = String(a.asset_type || '').toLowerCase().trim();
      const tB = String(b.asset_type || '').toLowerCase().trim();
      const orderA = typeOrder[tA] || 99;
      const orderB = typeOrder[tB] || 99;
      if (orderA !== orderB) {
        return orderA - orderB;
      }
      return a.id - b.id;
    });
    
    sortedAssets.forEach((asset, index) => {
      const row = [
        index + 1,
        asset.name || '',
        typeMap[asset.asset_type] || asset.asset_type || '',
        asset.protocol || 'N/A',
        asset.description || t('无备注'),
        statusMap[asset.status] || asset.status || ''
      ];
      
      const escapedRow = row.map(val => {
        let str = String(val);
        if (str.includes(',') || str.includes('"') || str.includes('\n')) {
          str = str.replace(/"/g, '""');
          return `"${str}"`;
        }
        return str;
      });
      
      csvContent += escapedRow.join(',') + '\n';
    });
    
    const blob = new Blob([csvContent], { type: 'text/csv;charset=utf-8;' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.setAttribute('href', url);
    link.setAttribute('download', `${t("资产汇总表")}_${activeDomain.name}_${new Date().toISOString().slice(0, 10)}.csv`);
    link.style.visibility = 'hidden';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  const handleTriggerDeduplicate = async () => {
    if (!activeDomain) return;
    setSuggestionStatus('loading');
    setShowDeduplicateModal(true);
    setLeftSearch('');
    setRightSearch('');
    setCompactMode(true);
    setExpandedAssetIds(new Set());

    const currentAssets = assets.filter(a => a.status !== 'rejected');
    setOriginalAssets(JSON.parse(JSON.stringify(currentAssets)));

    const cleanAndSplitDesc = (desc) => {
      if (!desc) return [];
      // 清理掉之前的自动合并后缀标签，避免二次污染
      let cleaned = desc.replace(/\[已并入:[^\]]+\]/g, '');
      cleaned = cleaned.replace(/\[合并资产[^\]]+\]/g, '');
      const lines = cleaned.split(/\n+|\r+|\|/);
      const items = [];
      lines.forEach(line => {
        let clean = line.trim();
        if (!clean) return;
        // 去掉已有的数字序号前缀，以便重新排版
        clean = clean.replace(/^(?:\d+[.、)]|\[\d+\])\s*/, '').trim();
        if (clean && !items.includes(clean)) {
          items.push(clean);
        }
      });
      return items;
    };

    const mergeDescriptionsToList = (descList) => {
      const allItems = [];
      descList.forEach(desc => {
        cleanAndSplitDesc(desc).forEach(item => {
          if (!allItems.includes(item)) {
            allItems.push(item);
          }
        });
      });
      if (allItems.length === 0) return '';
      if (allItems.length === 1) return allItems[0];
      return allItems.map((item, idx) => `${idx + 1}. ${item}`).join('\n');
    };

    const suggs = await fetchDeduplicateSuggestions(activeDomain.id);

    const removeIds = new Set();
    suggs.forEach(s => {
      s.remove_asset_ids.forEach(id => removeIds.add(id));
    });

    const optimized = currentAssets.map(a => {
      const cloned = JSON.parse(JSON.stringify(a));
      const matchedSug = suggs.find(s => s.keep_asset_id === a.id);
      if (matchedSug) {
        const remDescs = [];
        matchedSug.remove_asset_ids.forEach(rid => {
          const rAsset = currentAssets.find(ra => ra.id === rid);
          if (rAsset && rAsset.description) {
            remDescs.push(rAsset.description);
          }
        });
        cloned.description = mergeDescriptionsToList([cloned.description, ...remDescs]);
      }
      return cloned;
    }).filter(a => !removeIds.has(a.id));

    setOptimizedAssets(optimized);
    setSuggestionStatus(suggs.length === 0 ? 'empty' : 'ready');
  };

  const handleUpdateOptimized = (id, field, value) => {
    setOptimizedAssets(prev => prev.map(a => a.id === id ? { ...a, [field]: value } : a));
  };

  const handleRemoveFromOptimized = (id) => {
    setOptimizedAssets(prev => prev.filter(a => a.id !== id));
  };

  const handleAddToOptimized = (asset) => {
    if (optimizedAssets.some(a => a.id === asset.id)) return;
    setOptimizedAssets(prev => [...prev, JSON.parse(JSON.stringify(asset))]);
  };

  const toggleAssetExpand = (id) => {
    setExpandedAssetIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  };

  const handleSaveDeduplicated = async () => {
    setSuggestionStatus('loading');
    try {
      const optimizedIds = new Set(optimizedAssets.map(a => a.id));
      const promises = [];

      for (const orig of originalAssets) {
        if (optimizedIds.has(orig.id)) {
          const opt = optimizedAssets.find(a => a.id === orig.id);
          const isModified = opt.name !== orig.name || opt.protocol !== orig.protocol || opt.description !== orig.description || opt.asset_type !== orig.asset_type;
          if (isModified || opt.status !== 'confirmed') {
            promises.push(confirmAsset(opt.id, {
              status: 'confirmed',
              name: opt.name,
              asset_type: opt.asset_type,
              protocol: opt.protocol,
              description: opt.description
            }, true));
          }
        } else {
          if (orig.status !== 'rejected') {
            promises.push(confirmAsset(orig.id, {
              status: 'rejected'
            }, true));
          }
        }
      }

      await Promise.all(promises);
      setShowDeduplicateModal(false);
      alert(t('去重合并及资产编辑保存成功！所有保留资产已自动标记为 “已确认” 状态。'));
      fetchAssets(activeDomain.id);
    } catch (err) {
      alert(t('保存合并结果失败：') + err.message);
    } finally {
      setSuggestionStatus('ready');
    }
  };

  const handleStartTara = async () => {
    if (!activeDomain) return;
    
    // Check if at least 1 confirmed asset exists
    const confirmedCount = assets.filter(a => a.status === 'confirmed').length;
    if (confirmedCount === 0) {
      alert('启动错误：子域控内没有已确认(confirmed)的资产。请先核对确认至少一个资产后再启动 TARA。');
      return;
    }

    const hasExistingRun = activeDomain.status === 'completed' || activeDomain.status === 'failed';
    let force = false;

    if (hasExistingRun) {
      if (window.confirm(t('确定要重新启动子域控 "') + activeDomain.name + t('" 的 TARA 评估分析吗？'))) {
        force = window.confirm(
          t('是否要【强制全新分析】以覆盖现有的 AI 评估结论？') + '\n\n' +
          t('- 点击 [确定]：强制全新分析（重新调用 AI 生成所有阶段结果，但您手动修改的内容将被保留）') + '\n' +
          t('- 点击 [取消]：增量继承分析（仅分析有变动的资产以节省 Token，其余资产直接继承上次结论）')
        );
      } else {
        return; // User cancelled starting the analysis entirely
      }
    } else {
      if (!window.confirm(t('确定要启动子域控 "') + activeDomain.name + t('" 的 TARA 评估分析吗？这需要花费一些时间。'))) {
        return;
      }
    }

    const run = await startTaraAnalysis(activeDomain.id, force);
    if (run) {
      // Refresh domain details and start progress polling
      fetchDomains(projectId);
      startProgressPolling(activeDomain.id);
    }
  };

  const handleCancelTara = async (domainId) => {
    if (window.confirm(t('确定要强制终止后台的 TARA 评估分析任务吗？'))) {
      await cancelTaraAnalysis(domainId);
      // Re-fetch progress
      fetchDomains(projectId);
      stopProgressPolling();
    }
  };

  const getDomainStatusBadge = (status) => {
    switch (status) {
      case 'completed': return <span className="badge badge-completed">{t("已完成")}</span>;
      case 'running': return <span className="badge badge-running">{t("分析中")}</span>;
      case 'failed': return <span className="badge badge-failed">{t("分析失败")}</span>;
      case 'not_started':
      default:
        return <span className="badge badge-draft">{t("未开始")}</span>;
    }
  };

  const getAssetTypeLabel = (type) => {
    switch(type) {
      case 'data': return t('数据资产');
      case 'software': return t('软件资产');
      case 'hardware': return t('硬件资产');
      case 'communication': return t('通信资产');
      default: return type;
    }
  };

  const isProjectCompleted = currentProject?.is_archived === 1;

  return (
    <div style={{ display: 'flex', flexGrow: 1, height: 'calc(100vh - 64px)', overflow: 'hidden' }}>
      {/* Left Sidebar */}
      <div className="glass" style={{
        width: '320px',
        borderRight: '1px solid var(--border-color)',
        borderRadius: '0',
        padding: '24px 16px',
        display: 'flex',
        flexDirection: 'column',
        gap: '20px',
        background: 'var(--sidebar-bg)'
      }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
          <button 
            onClick={() => setPage('projects')}
            style={{ 
              display: 'flex', 
              alignItems: 'center', 
              gap: '6px', 
              background: 'none', 
              border: 'none', 
              color: 'var(--text-secondary)',
              cursor: 'pointer',
              fontSize: '13px'
            }}
          >
            <ArrowLeft size={14} /> {t("返回列表")}
          </button>
        </div>

        <div>
          <button
            onClick={() => setShowDomainModal(true)}
            className="btn btn-primary"
            style={{ width: '100%', padding: '12px' }}
            disabled={isProjectCompleted}
          >
            <FolderPlus size={16} />
            <span>{t("新建子域控")}</span>
          </button>
        </div>

        {/* Domain Tree / List */}
        <div style={{ flexGrow: 1, overflowY: 'auto' }}>
          <h4 style={{ fontSize: '12px', textTransform: 'uppercase', color: 'var(--text-muted)', marginBottom: '12px', letterSpacing: '1px' }}>
            {t("子系统域控列表")} ({domains.length})
          </h4>

          {domains.length === 0 ? (
            <div style={{ textAlign: 'center', padding: '24px 0', color: 'var(--text-muted)', fontSize: '13px' }}>
              {t("暂无子域控，请创建。")}
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
              {domains.map((d) => {
                const isActive = activeDomain?.id === d.id;
                return (
                  <div
                    key={d.id}
                    onClick={() => setActiveDomain(d)}
                    style={{
                      padding: '10px 14px',
                      borderRadius: '8px',
                      background: isActive ? 'rgba(99, 102, 241, 0.08)' : 'transparent',
                      border: '1px solid',
                      borderColor: isActive ? 'var(--primary)' : 'transparent',
                      cursor: 'pointer',
                      display: 'flex',
                      flexDirection: 'column',
                      gap: '6px',
                      transition: 'all 0.2s ease'
                    }}
                  >
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span style={{ fontSize: '14px', fontWeight: isActive ? '600' : '400', color: isActive ? 'var(--primary)' : 'var(--text-primary)' }}>
                        {d.name}
                      </span>
                      {!isProjectCompleted && d.status !== 'running' && (
                        <Trash2 
                          size={13} 
                          className="trash-icon"
                          onClick={(e) => handleDeleteDomainClick(e, d)} 
                          style={{ color: 'var(--text-muted)', cursor: 'pointer' }}
                        />
                      )}
                    </div>

                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      {getDomainStatusBadge(d.status)}
                      
                      {d.status === 'running' && (
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                          {!isProjectCompleted && (
                            <button
                              onClick={(e) => { e.stopPropagation(); handleCancelTara(d.id); }}
                              style={{ background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer', display: 'flex' }}
                              title={t("取消分析")}
                            >
                              <XCircle size={14} />
                            </button>
                          )}
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {/* Right Work Area */}
      <div style={{ flexGrow: 1, padding: '32px 32px 16px 32px', display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden' }}>
        {taraError && (
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
            alignItems: 'center',
            flexShrink: 0
          }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <AlertTriangle size={16} />
              <span>{t(taraError)}</span>
            </div>
            <button onClick={clearTaraError} style={{ background: 'none', border: 'none', color: '#e11d48', cursor: 'pointer' }}>×</button>
          </div>
        )}

        {!activeDomain ? (
          <div style={{
            height: '100%',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            textAlign: 'center',
            color: 'var(--text-secondary)'
          }}>
            <GitFork size={64} style={{ color: 'var(--text-muted)', marginBottom: '16px' }} />
            <h3 style={{ fontSize: '20px', marginBottom: '8px', color: 'var(--text-primary)' }}>{t("请选择子域控")}</h3>
            <p style={{ fontSize: '14px', maxWidth: '400px' }}>
              {t("请在左侧侧边栏中选择一个已有的子系统域控进行威胁评估建模，或者创建一个新的子域控。")}
            </p>
          </div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', flexGrow: 1, minHeight: 0 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '20px', flexShrink: 0 }}>
              <div>
                <h2 style={{ fontSize: '24px', color: 'var(--text-primary)', fontWeight: '600', display: 'flex', alignItems: 'center', gap: '8px' }}>
                  {activeDomain.name} {t("工作台")}
                </h2>
                <p style={{ color: 'var(--text-secondary)', fontSize: '13px', marginTop: '4px' }}>
                  {t("在此管理该域控的数据流拓扑画布图，以及核对自动提取的安全资产")}
                </p>
              </div>
            </div>

            {/* DFD Diagrams Grid */}
            <div style={{ flex: '1 1 35%', display: 'flex', flexDirection: 'column', minHeight: 0, marginBottom: '24px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px', flexShrink: 0 }}>
                <h3 style={{ fontSize: '16px', fontWeight: '600', color: 'var(--text-primary)' }}>{t("功能图 (DFD)")}</h3>
                <button 
                  onClick={() => setShowDfdModal(true)} 
                  className="btn btn-secondary"
                  style={{ padding: '6px 12px', fontSize: '12px' }}
                  disabled={isProjectCompleted || activeDomain.status === 'running'}
                >
                  {t("+ 新建 DFD 画布")}
                </button>
              </div>

              <div style={{ overflowY: 'auto', flexGrow: 1, minHeight: 0, paddingRight: '4px' }}>
                {diagrams.length === 0 ? (
                  <div className="glass" style={{ padding: '36px', textAlign: 'center', borderStyle: 'dashed', color: 'var(--text-secondary)' }}>
                    {t("暂无功能数据流图，请点击右上角“新建 DFD 画布”进行绘制。")}
                  </div>
                ) : (
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))', gap: '16px' }}>
                    {diagrams.map((diag) => (
                      <div
                        key={diag.id}
                        className="glass-interactive"
                        onClick={() => {
                          setDomainId(activeDomain.id);
                          setDiagramId(diag.id);
                          setPage('dfd-editor');
                        }}
                        style={{
                          padding: '16px',
                          cursor: 'pointer',
                          display: 'flex',
                          flexDirection: 'column',
                          minHeight: '120px',
                          justifyContent: 'space-between'
                        }}
                      >
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                          <span style={{ fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)' }}>
                            {diag.title}
                          </span>
                          {!isProjectCompleted && activeDomain.status !== 'running' && (
                            <Trash2
                              size={14}
                              onClick={(e) => handleDeleteDfdClick(e, diag.id, diag.title)}
                              style={{ color: 'var(--text-muted)', cursor: 'pointer' }}
                            />
                          )}
                        </div>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: '12px', color: 'var(--text-secondary)' }}>
                          <span>{t("版本")}: v{diag.version_no}</span>
                          <span>{t("去画图 DFD →")}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>

            {/* Assets Table */}
            <div style={{ flex: '1 1 65%', display: 'flex', flexDirection: 'column', minHeight: 0, paddingBottom: '8px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px', flexShrink: 0 }}>
                <h3 style={{ fontSize: '16px', fontWeight: '600', color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <Table size={16} /> {t("提取资产汇总表")}
                </h3>
                <div style={{ display: 'flex', gap: '10px' }}>
                  <button
                    onClick={() => setShowAddAssetModal(true)}
                    className="btn btn-secondary"
                    style={{ padding: '6px 12px', fontSize: '12px' }}
                    disabled={isProjectCompleted || activeDomain.status === 'running'}
                  >
                    <span>{t("+ 手动添加资产")}</span>
                  </button>
                  <button
                    onClick={handleExtractAssets}
                    className="btn btn-primary"
                    style={{ padding: '6px 12px', fontSize: '12px' }}
                    disabled={isProjectCompleted || activeDomain.status === 'running' || diagrams.length === 0}
                  >
                    <Sparkles size={13} />
                    <span>{t("提取资产")}</span>
                  </button>
                  <button
                    onClick={handleExportAssetsCSV}
                    className="btn btn-secondary"
                    style={{ padding: '6px 12px', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '4px' }}
                    disabled={assets.length === 0}
                  >
                    <Download size={13} />
                    <span>{t("导出资产 CSV")}</span>
                  </button>
                  <button
                    onClick={handleClearAssets}
                    className="btn btn-danger"
                    style={{ padding: '6px 12px', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '4px' }}
                    disabled={isProjectCompleted || activeDomain.status === 'running' || assets.length === 0}
                  >
                    <Trash2 size={13} />
                    <span>{t("清空资产")}</span>
                  </button>
                </div>
              </div>

              <div style={{ overflowY: 'auto', flexGrow: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
                {assets.length === 0 ? (
                  <div className="glass" style={{ padding: '36px', textAlign: 'center', borderStyle: 'dashed', color: 'var(--text-secondary)', flexGrow: 1, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                    {t("暂无提取出的资产，请在绘制画布后点击上方“提取资产”按钮，或点击“+ 手动添加资产”手动录入。")}
                  </div>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', flexGrow: 1, minHeight: 0 }}>
                    <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '10px', padding: '6px 12px', background: 'rgba(99, 102, 241, 0.05)', borderRadius: '6px', border: '1px solid rgba(99, 102, 241, 0.15)', alignSelf: 'flex-start', flexShrink: 0 }}>
                      💡 <b>{t("提示：")}</b>{t("直接点击下表中的“资产名称”、“资产类型”、“通信协议”、“备注说明”输入框即可直接修改，失焦（点击别处）即可自动保存同步。")}
                    </div>
                    <div className="table-container" style={{ flexGrow: 1, overflowY: 'auto', border: '1px solid var(--border-color)', borderRadius: '6px', minHeight: 0 }}>
                      <table className="custom-table">
                        <thead>
                          <tr>
                            <th style={{ width: '5%' }}>{t("序号")}</th>
                            <th style={{ width: '25%' }}>{t("资产名称")}</th>
                            <th style={{ width: '15%' }}>{t("资产类型")}</th>
                            <th style={{ width: '15%' }}>{t("通信协议")}</th>
                            <th style={{ width: '25%' }}>{t("备注说明")}</th>
                            <th style={{ width: '12%' }}>{t("专家核对状态")}</th>
                            <th style={{ width: '3%', textAlign: 'center' }}>{t("操作")}</th>
                          </tr>
                        </thead>
                        <tbody>
                          {assets.map((asset, index) => {
                            const isAutoAsset = asset.diagram_id !== null && asset.diagram_id !== undefined;
                            const isConfirmedOrRejected = asset.status === 'confirmed' || asset.status === 'rejected';
                            const isFieldDisabled = isProjectCompleted || activeDomain.status === 'running' || (isAutoAsset && isConfirmedOrRejected);
                            
                            return (
                              <tr key={asset.id} style={{ opacity: asset.status === 'rejected' ? 0.5 : 1 }}>
                                <td>{index + 1}</td>
                                <td>
                                  <input
                                    type="text"
                                    className="editable-cell-input"
                                    style={{ fontWeight: '600' }}
                                    value={asset.name}
                                    onChange={(e) => handleAssetFieldChange(asset.id, 'name', e.target.value)}
                                    onBlur={(e) => handleAssetFieldBlur(asset.id, 'name', e.target.value)}
                                    disabled={isFieldDisabled}
                                  />
                                </td>
                                <td>
                                  <select
                                    className="editable-cell-select"
                                    value={asset.asset_type}
                                    onChange={(e) => {
                                      handleAssetFieldChange(asset.id, 'asset_type', e.target.value);
                                      handleAssetFieldBlur(asset.id, 'asset_type', e.target.value);
                                    }}
                                    disabled={isFieldDisabled}
                                    style={{ paddingRight: '12px' }}
                                  >
                                    <option value="data">{t("数据资产")}</option>
                                    <option value="software">{t("软件资产")}</option>
                                    <option value="hardware">{t("硬件资产")}</option>
                                    <option value="communication">{t("通信资产")}</option>
                                  </select>
                                </td>
                                <td>
                                  <input
                                    type="text"
                                    className="editable-cell-input"
                                    style={{ fontFamily: 'monospace' }}
                                    value={asset.protocol || ''}
                                    onChange={(e) => handleAssetFieldChange(asset.id, 'protocol', e.target.value)}
                                    onBlur={(e) => handleAssetFieldBlur(asset.id, 'protocol', e.target.value)}
                                    placeholder="N/A"
                                    disabled={isFieldDisabled}
                                  />
                                </td>
                                <td>
                                  <input
                                    type="text"
                                    className="editable-cell-input"
                                    value={asset.description || ''}
                                    onChange={(e) => handleAssetFieldChange(asset.id, 'description', e.target.value)}
                                    onBlur={(e) => handleAssetFieldBlur(asset.id, 'description', e.target.value)}
                                    placeholder={t("无备注")}
                                    disabled={isFieldDisabled}
                                  />
                                </td>
                                <td>
                                  <select
                                    value={asset.status}
                                    onChange={(e) => handleAssetStatusChange(asset.id, e.target.value)}
                                    disabled={isProjectCompleted || activeDomain.status === 'running'}
                                    style={{
                                      background: 'var(--bg-card)',
                                      border: '1px solid var(--border-color)',
                                      borderRadius: '4px',
                                      color: asset.status === 'confirmed' ? 'var(--success)' : asset.status === 'rejected' ? 'var(--accent)' : 'var(--text-primary)',
                                      padding: '4px 8px',
                                      outline: 'none',
                                      fontSize: '13px',
                                      cursor: 'pointer',
                                      width: '100%'
                                    }}
                                  >
                                    <option value="draft" style={{ color: 'var(--text-primary)', background: 'var(--bg-dark)' }}>{t("待核对 (Draft)")}</option>
                                    <option value="confirmed" style={{ color: 'var(--success)', background: 'var(--bg-dark)' }}>{t("已确认 (Confirmed)")}</option>
                                    <option value="rejected" style={{ color: 'var(--accent)', background: 'var(--bg-dark)' }}>{t("已拒绝 (Rejected)")}</option>
                                  </select>
                                </td>
                                <td style={{ textAlign: 'center' }}>
                                  <button
                                    onClick={() => handleDeleteAsset(asset)}
                                    className="btn-icon"
                                    style={{ color: 'var(--accent)', cursor: 'pointer', background: 'none', border: 'none', padding: '4px' }}
                                    title={t("彻底删除资产")}
                                    disabled={isFieldDisabled}
                                  >
                                    <Trash2 size={13} />
                                  </button>
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
              </div>

              {/* Action Buttons Section under the Assets Table */}
              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '16px', marginTop: '16px', flexShrink: 0 }}>
                <button
                  onClick={handleTriggerDeduplicate}
                  className="btn btn-secondary"
                  disabled={isProjectCompleted || activeDomain.status === 'running' || assets.length < 2}
                  style={{ padding: '10px 20px' }}
                >
                  <Sparkles size={16} style={{ color: 'var(--primary)' }} />
                  <span>{t("AI 资产去重")}</span>
                </button>

                <button
                  onClick={handleStartTara}
                  className="btn btn-primary"
                  disabled={isProjectCompleted || activeDomain.status === 'running' || assets.length === 0}
                  style={{ padding: '10px 20px' }}
                >
                  <Play size={16} />
                  <span>{t("启动 TARA 分析")}</span>
                </button>

                {(activeDomain.status === 'completed' || activeDomain.status === 'failed') && (
                  <button
                    onClick={() => { setDomainId(activeDomain.id); setPage('tara-results'); }}
                    className="btn btn-secondary"
                    style={{ border: '1px solid var(--primary)', color: 'var(--primary)', padding: '10px 20px' }}
                  >
                    <Eye size={16} />
                    <span>{t("查看 TARA 结果")}</span>
                  </button>
                )}
              </div>

              {/* 最终需求合并：上传 OEM 需求 → 合并客户需求与 TARA 需求 */}
              <div style={{ marginTop: '14px', paddingTop: '14px', borderTop: '1px solid var(--border-color)', flexShrink: 0 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '16px', flexWrap: 'wrap' }}>
                  <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
                    {t("客户需求")}: <b>{customerReqCount}</b>
                    {mergedCount > 0 && <> · {t("最终需求")}: <b>{mergedCount}</b></>}
                  </div>
                  <div style={{ display: 'flex', gap: '12px', alignItems: 'center' }}>
                    <input
                      ref={oemFileRef}
                      type="file"
                      accept=".xlsx,.xls,.csv"
                      style={{ display: 'none' }}
                      onChange={handleOemFileChange}
                    />
                    <button
                      onClick={() => oemFileRef.current?.click()}
                      className="btn btn-secondary"
                      disabled={oemImporting || isProjectCompleted || activeDomain.status === 'running'}
                      style={{ padding: '10px 20px' }}
                    >
                      <Upload size={16} />
                      <span>{oemImporting ? t("导入中...") : t("上传 OEM 需求")}</span>
                    </button>
                    <button
                      onClick={handleMergeRequirements}
                      className="btn btn-primary"
                      disabled={merging || isProjectCompleted || activeDomain.status === 'running' || customerReqCount === 0}
                      style={{ padding: '10px 20px' }}
                    >
                      <Merge size={16} />
                      <span>{merging ? t("合并中...") : t("合并需求")}</span>
                    </button>
                    <button
                      onClick={handleDownloadMerged}
                      className="btn btn-secondary"
                      disabled={mergedCount === 0}
                      style={{ padding: '10px 20px' }}
                    >
                      <FileDown size={16} />
                      <span>{t("导出最终需求")}</span>
                    </button>
                  </div>
                </div>
                {mergeMsg && (
                  <div style={{
                    marginTop: '8px',
                    fontSize: '13px',
                    color: mergeMsg.type === 'error' ? '#e11d48' : 'var(--success)',
                    textAlign: 'right',
                  }}>
                    {mergeMsg.text}
                  </div>
                )}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* OEM 需求列映射 Modal */}
      {oemPreview && (
        <div className="modal-overlay" onClick={() => { setOemPreview(null); setOemPreviewFile(null); }}>
          <div className="modal-content glass" style={{ maxWidth: '920px', width: '90%' }} onClick={(e) => e.stopPropagation()}>
            <h3 style={{ fontSize: '18px', fontWeight: '600', marginBottom: '12px', color: 'var(--text-primary)' }}>
              {t("导入 OEM 需求：指定列映射")}
            </h3>
            <p style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '16px' }}>
              {t("请选择 sheet 并指定「需求编号列」和「需求描述列」，不要求 Excel 固定表头格式。")}
            </p>

            <div style={{ display: 'flex', gap: '16px', marginBottom: '16px', flexWrap: 'wrap' }}>
              <label style={{ fontSize: '13px', display: 'flex', flexDirection: 'column', gap: '6px', minWidth: '200px' }}>
                {t("Sheet")}
                <select
                  className="input-field"
                  value={oemPreview.sheet}
                  onChange={(e) => handleOemSheetChange(e.target.value)}
                >
                  {oemPreview.sheets.map((s) => <option key={s} value={s}>{s}</option>)}
                </select>
              </label>
              <label style={{ fontSize: '13px', display: 'flex', flexDirection: 'column', gap: '6px', minWidth: '200px' }}>
                {t("需求编号列")}
                <select
                  className="input-field"
                  value={oemMapping.id_column || ''}
                  onChange={(e) => setOemMapping({ ...oemMapping, id_column: Number(e.target.value) || null })}
                >
                  <option value="">{t("请选择")}</option>
                  {oemPreview.headers.map((h, i) => <option key={i} value={i + 1}>{i + 1}. {h || `列${i + 1}`}</option>)}
                </select>
              </label>
              <label style={{ fontSize: '13px', display: 'flex', flexDirection: 'column', gap: '6px', minWidth: '200px' }}>
                {t("需求描述列")}
                <select
                  className="input-field"
                  value={oemMapping.text_column || ''}
                  onChange={(e) => setOemMapping({ ...oemMapping, text_column: Number(e.target.value) || null })}
                >
                  <option value="">{t("请选择")}</option>
                  {oemPreview.headers.map((h, i) => <option key={i} value={i + 1}>{i + 1}. {h || `列${i + 1}`}</option>)}
                </select>
              </label>
            </div>

            <div style={{ maxHeight: '300px', overflow: 'auto', border: '1px solid var(--border-color)', borderRadius: '6px' }}>
              <table className="custom-table" style={{ fontSize: '12px' }}>
                <thead>
                  <tr>
                    {oemPreview.headers.map((h, i) => <th key={i}>{i + 1}. {h || ''}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {oemPreview.preview.map((row, ri) => (
                    <tr key={ri}>
                      {row.map((cellText, ci) => (
                        <td key={ci} style={{ maxWidth: '220px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{cellText}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '12px', marginTop: '20px' }}>
              <button
                className="btn btn-secondary"
                onClick={() => { setOemPreview(null); setOemPreviewFile(null); }}
              >
                {t("取消")}
              </button>
              <button
                className="btn btn-primary"
                onClick={handleConfirmOemImport}
                disabled={oemImporting || !oemMapping.id_column || !oemMapping.text_column}
              >
                {oemImporting ? t("导入中...") : `${t("确认导入")}（${oemPreview.row_count} ${t("行")}）`}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Create Domain Modal (BR-4.2.2) */}
      {showDomainModal && (
        <div className="modal-overlay">
          <div className="modal-content glass">
            <h3 style={{ fontSize: '18px', fontWeight: '600', marginBottom: '20px', color: 'var(--text-primary)' }}>
              {t("创建子系统域控")}
            </h3>

            {domainModalError && (
              <div style={{
                background: 'rgba(244, 63, 94, 0.1)',
                border: '1px solid rgba(244, 63, 94, 0.3)',
                color: '#e11d48',
                padding: '10px 12px',
                borderRadius: '6px',
                fontSize: '13px',
                marginBottom: '16px'
              }}>
                {t(domainModalError)}
              </div>
            )}

            <form onSubmit={handleCreateDomainSubmit}>
              <div className="input-group" style={{ marginBottom: '24px' }}>
                <span className="input-label">{t("子域控名称")} <span style={{ color: 'var(--accent)' }}>*</span> ({t("最多50字")})</span>
                <input
                  type="text"
                  className="input-field"
                  placeholder={t("例如: IVI智能娱乐系统")}
                  value={newDomainName}
                  onChange={(e) => setNewDomainName(e.target.value)}
                  maxLength={50}
                  required
                />
              </div>

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '12px' }}>
                <button
                  type="button"
                  onClick={() => setShowDomainModal(false)}
                  className="btn btn-secondary"
                >
                  {t("取消")}
                </button>
                <button
                  type="submit"
                  className="btn btn-primary"
                >
                  {t("确认创建")}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Onboarding Modal (BR-4.2.2) */}
      {showOnboardingModal && onboardCreatedDomain && (
        <div className="modal-overlay">
          <div className="modal-content glass" style={{ width: '450px', textAlign: 'center', padding: '32px 24px' }}>
            <div style={{
              display: 'inline-flex',
              alignItems: 'center',
              justifyContent: 'center',
              width: '56px',
              height: '56px',
              borderRadius: '50%',
              background: 'rgba(52, 211, 153, 0.1)',
              color: 'var(--success)',
              marginBottom: '20px'
            }}>
              <CheckCircle2 size={32} />
            </div>

            <h3 style={{ fontSize: '20px', fontWeight: '600', marginBottom: '12px', color: 'var(--text-primary)' }}>
              {t("子域控创建成功！")}
            </h3>
            <p style={{ color: 'var(--text-secondary)', fontSize: '14px', lineHeight: '1.5', marginBottom: '28px' }}>
              {t("您已成功添加了")} <b>{onboardCreatedDomain.name}</b> {t("子系统域控。请选择您的下一步操作：")}
            </p>

            <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
              <button
                onClick={() => {
                  setShowOnboardingModal(false);
                  setShowDfdModal(true); // Open DFD creation directly
                }}
                className="btn btn-primary"
                style={{ width: '100%', padding: '12px' }}
              >
                {t("开始 DFD 绘图分析")}
              </button>
              
              <button
                onClick={() => {
                  setShowOnboardingModal(false);
                  setShowDomainModal(true);
                }}
                className="btn btn-secondary"
                style={{ width: '100%', padding: '12px' }}
              >
                {t("继续创建子域控")}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Create Manual Asset Modal */}
      {showAddAssetModal && (
        <div className="modal-overlay">
          <div className="modal-content glass">
            <h3 style={{ fontSize: '18px', fontWeight: '600', marginBottom: '20px', color: 'var(--text-primary)' }}>
              {t("手动添加资产")}
            </h3>

            {addAssetModalError && (
              <div style={{
                background: 'rgba(244, 63, 94, 0.1)',
                border: '1px solid rgba(244, 63, 94, 0.3)',
                color: '#e11d48',
                padding: '10px 12px',
                borderRadius: '6px',
                fontSize: '13px',
                marginBottom: '16px'
              }}>
                {t(addAssetModalError)}
              </div>
            )}

            <form onSubmit={handleCreateManualAssetSubmit}>
              <div className="input-group">
                <span className="input-label">{t("资产名称")} <span style={{ color: 'var(--accent)' }}>*</span></span>
                <input
                  type="text"
                  className="input-field"
                  placeholder={t("例如: 车载诊断管理服务")}
                  value={newAssetName}
                  onChange={(e) => setNewAssetName(e.target.value)}
                  maxLength={100}
                  required
                />
              </div>

              <div className="input-group">
                <span className="input-label">{t("资产类型")} <span style={{ color: 'var(--accent)' }}>*</span></span>
                <select
                  className="input-field"
                  value={newAssetType}
                  onChange={(e) => setNewAssetType(e.target.value)}
                  style={{ cursor: 'pointer' }}
                >
                  <option value="data">{t("数据资产 (Data)")}</option>
                  <option value="software">{t("软件资产 (Software)")}</option>
                  <option value="hardware">{t("硬件资产 (Hardware)")}</option>
                  <option value="communication">{t("通信资产 (Communication)")}</option>
                </select>
              </div>

              <div className="input-group">
                <span className="input-label">{t("通信协议")}</span>
                <input
                  type="text"
                  className="input-field"
                  placeholder={t("例如: UDS, CAN, HTTPS (选填)")}
                  value={newAssetProtocol}
                  onChange={(e) => setNewAssetProtocol(e.target.value)}
                  maxLength={50}
                />
              </div>

              <div className="input-group" style={{ marginBottom: '24px' }}>
                <span className="input-label">{t("备注说明")}</span>
                <textarea
                  className="input-field"
                  placeholder={t("请输入资产的备注描述或功能说明 (选填)")}
                  value={newAssetDesc}
                  onChange={(e) => setNewAssetDesc(e.target.value)}
                  rows={3}
                  style={{ resize: 'none' }}
                />
              </div>

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '12px' }}>
                <button
                  type="button"
                  onClick={() => setShowAddAssetModal(false)}
                  className="btn btn-secondary"
                >
                  {t("取消")}
                </button>
                <button
                  type="submit"
                  className="btn btn-primary"
                >
                  {t("确认添加")}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Create DFD Modal */}
      {showDfdModal && (
        <div className="modal-overlay">
          <div className="modal-content glass">
            <h3 style={{ fontSize: '18px', fontWeight: '600', marginBottom: '20px', color: 'var(--text-primary)' }}>
              {t("创建数据流图 (DFD)")}
            </h3>

            {dfdModalError && (
              <div style={{
                background: 'rgba(244, 63, 94, 0.1)',
                border: '1px solid rgba(244, 63, 94, 0.3)',
                color: '#e11d48',
                padding: '10px 12px',
                borderRadius: '6px',
                fontSize: '13px',
                marginBottom: '16px'
              }}>
                {t(dfdModalError)}
              </div>
            )}

            <form onSubmit={handleCreateDfdSubmit}>
              <div className="input-group" style={{ marginBottom: '24px' }}>
                <span className="input-label">{t("功能图标题")} <span style={{ color: 'var(--accent)' }}>*</span> ({t("最多100字")})</span>
                <input
                  type="text"
                  className="input-field"
                  placeholder={t("例如: 远程诊断服务数据流图")}
                  value={newDfdTitle}
                  onChange={(e) => setNewDfdTitle(e.target.value)}
                  maxLength={100}
                  required
                />
              </div>

              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '12px' }}>
                <button
                  type="button"
                  onClick={() => setShowDfdModal(false)}
                  className="btn btn-secondary"
                >
                  {t("取消")}
                </button>
                <button
                  type="submit"
                  className="btn btn-primary"
                >
                  {t("确认创建")}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* AI Deduplication Modal */}
      {showDeduplicateModal && (
        <div className="modal-overlay">
          <div className="modal-content glass" style={{ width: '96vw', height: '96vh', maxWidth: '1600px', maxHeight: '1000px', display: 'flex', flexDirection: 'column', overflow: 'hidden', padding: '24px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '16px', flexShrink: 0 }}>
              <Sparkles size={20} style={{ color: 'var(--primary)' }} />
              <h3 style={{ fontSize: '18px', fontWeight: '600', color: 'var(--text-primary)' }}>{t("AI 资产合并与去重对比看板")}</h3>
            </div>

            {suggestionStatus === 'loading' ? (
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', flexGrow: 1, gap: '12px' }}>
                <div className="spinner"></div>
                <span style={{ color: 'var(--text-secondary)' }}>{t("AI 正在分析资产相似性，请稍候...")}</span>
              </div>
            ) : suggestionStatus === 'empty' ? (
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', flexGrow: 1, textAlign: 'center', padding: '30px 0' }}>
                <CheckCircle2 size={36} style={{ color: 'var(--success)', marginBottom: '12px' }} />
                <h4 style={{ color: 'var(--text-primary)', marginBottom: '6px' }}>{t("未发现冗余重复资产")}</h4>
                <p style={{ color: 'var(--text-secondary)', fontSize: '13px' }}>{t("该子系统域控内的所有提取资产命名和属性均特征明确，没有发现重复项。")}</p>
                <button onClick={() => setShowDeduplicateModal(false)} className="btn btn-secondary" style={{ marginTop: '20px' }}>{t("关闭")}</button>
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', flexGrow: 1, overflow: 'hidden' }}>
                <p style={{ color: 'var(--text-secondary)', fontSize: '13px', marginBottom: '16px', lineHeight: '1.5', flexShrink: 0 }}>
                  {t("AI 算法已识别出可能重复的资产合并项。请通过下方的")}<b>{t("对比看板")}</b>{t("核对。左侧原始数据可通过“恢复加入”添加至右侧，右侧资产可直接修改名称和协议。")}
                </p>

                {/* Toolbar */}
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px', gap: '20px', flexShrink: 0 }}>
                  {/* Left Search */}
                  <div style={{ flex: '1 1 42%', display: 'flex', gap: '10px', alignItems: 'center' }}>
                    <input 
                      type="text" 
                      className="input-field" 
                      placeholder={t("🔍 搜索原始资产名称或协议...")} 
                      style={{ padding: '8px 12px', fontSize: '13px', margin: 0 }}
                      value={leftSearch}
                      onChange={(e) => setLeftSearch(e.target.value)}
                    />
                  </div>

                  {/* Middle Controller (Toggles) */}
                  <div style={{ display: 'flex', gap: '10px', alignItems: 'center', flexShrink: 0 }}>
                    <button
                      onClick={() => setCompactMode(!compactMode)}
                      className="btn btn-secondary"
                      style={{ padding: '8px 14px', fontSize: '12px', borderColor: compactMode ? 'var(--primary)' : 'var(--border-color)', color: compactMode ? 'var(--primary)' : 'var(--text-primary)', height: '36px', display: 'flex', alignItems: 'center', gap: '6px' }}
                      type="button"
                    >
                      <span>{compactMode ? t('🔘 紧凑视图') : t('⚪ 详细视图')}</span>
                    </button>
                  </div>

                  {/* Right Search */}
                  <div style={{ flex: '1 1 58%', display: 'flex', gap: '10px', alignItems: 'center' }}>
                    <input 
                      type="text" 
                      className="input-field" 
                      placeholder={t("🔍 搜索优化后资产名称或协议...")} 
                      style={{ padding: '8px 12px', fontSize: '13px', margin: 0 }}
                      value={rightSearch}
                      onChange={(e) => setRightSearch(e.target.value)}
                    />
                  </div>
                </div>

                {/* Columns Container */}
                <div style={{ display: 'flex', gap: '20px', flexGrow: 1, overflow: 'hidden', minHeight: '0', marginBottom: '16px' }}>
                  
                  {/* Left Column: Original */}
                  <div style={{ flex: '1 1 42%', display: 'flex', flexDirection: 'column', border: '1px solid var(--border-color)', borderRadius: '8px', padding: '16px', background: 'var(--bg-card)', overflow: 'hidden' }}>
                    <h4 style={{ fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)', marginBottom: '12px', borderBottom: '1px solid var(--border-color)', paddingBottom: '8px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexShrink: 0 }}>
                      <span>{t("原始提取资产")} ({leftSearch ? originalAssets.filter(a => a.name.toLowerCase().includes(leftSearch.toLowerCase()) || (a.protocol && a.protocol.toLowerCase().includes(leftSearch.toLowerCase()))).length : originalAssets.length})</span>
                      <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>{t("DFD 提取")}</span>
                    </h4>
                    <div style={{ overflowY: 'auto', flexGrow: 1, display: 'flex', flexDirection: 'column', gap: '8px', paddingRight: '4px' }}>
                      {originalAssets
                        .filter(a => !leftSearch || a.name.toLowerCase().includes(leftSearch.toLowerCase()) || (a.protocol && a.protocol.toLowerCase().includes(leftSearch.toLowerCase())))
                        .map((asset) => {
                          const isKept = optimizedAssets.some(o => o.id === asset.id);
                          const assetIndex = originalAssets.findIndex(o => o.id === asset.id);
                          return (
                            <div 
                              key={asset.id} 
                              style={{ 
                                padding: compactMode ? '6px 10px' : '10px 12px', 
                                borderRadius: '6px', 
                                border: '1px solid', 
                                borderColor: isKept ? 'rgba(5, 150, 105, 0.2)' : 'rgba(225, 29, 72, 0.2)',
                                background: isKept ? 'rgba(5, 150, 105, 0.04)' : 'rgba(225, 29, 72, 0.04)',
                                opacity: isKept ? 1 : 0.75,
                                transition: 'all 0.15s ease'
                              }}
                            >
                              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                                <span style={{ fontSize: '13px', fontWeight: '500', color: 'var(--text-primary)' }}>
                                  #{assetIndex + 1} {asset.name}
                                </span>
                                {isKept ? (
                                  <span style={{ fontSize: '11px', color: 'var(--success)' }}>{t("已保留")}</span>
                                ) : (
                                  <button 
                                    onClick={() => handleAddToOptimized(asset)}
                                    className="btn btn-secondary"
                                    style={{ padding: '2px 8px', fontSize: '11px', border: '1px solid var(--primary)', color: 'var(--primary)', height: '22px', margin: 0 }}
                                    type="button"
                                  >
                                    {t("恢复加入")}
                                  </button>
                                )}
                              </div>
                              <div style={{ fontSize: '11px', color: 'var(--text-secondary)', marginTop: '2px' }}>
                                {t("协议:")} <code>{asset.protocol || 'N/A'}</code> | {t("类型")}: {getAssetTypeLabel(asset.asset_type)}
                              </div>
                              {!compactMode && asset.description && (
                                <div style={{ fontSize: '11px', color: 'var(--text-muted)', marginTop: '4px', borderTop: '1px dashed var(--border-color)', paddingTop: '4px', textOverflow: 'ellipsis', overflow: 'hidden', whiteSpace: 'nowrap' }} title={asset.description}>
                                  {asset.description}
                                </div>
                              )}
                            </div>
                          );
                        })}
                    </div>
                  </div>

                  {/* Right Column: AI-Optimized & Editable */}
                  <div style={{ flex: '1 1 58%', display: 'flex', flexDirection: 'column', border: '1px solid var(--border-color)', borderRadius: '8px', padding: '16px', background: 'var(--bg-card)', overflow: 'hidden' }}>
                    <h4 style={{ fontSize: '14px', fontWeight: '600', color: 'var(--text-primary)', marginBottom: '12px', borderBottom: '1px solid var(--border-color)', paddingBottom: '8px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexShrink: 0 }}>
                      <span>{t("AI 优化后资产")} ({rightSearch ? optimizedAssets.filter(a => a.name.toLowerCase().includes(rightSearch.toLowerCase()) || (a.protocol && a.protocol.toLowerCase().includes(rightSearch.toLowerCase()))).length : optimizedAssets.length})</span>
                      <span style={{ fontSize: '11px', color: 'var(--primary)', fontWeight: '600' }}>{t("双击字段可编辑")}</span>
                    </h4>
                    <div style={{ overflowY: 'auto', flexGrow: 1, display: 'flex', flexDirection: 'column', gap: '8px', paddingRight: '4px' }}>
                      {optimizedAssets
                        .filter(a => !rightSearch || a.name.toLowerCase().includes(rightSearch.toLowerCase()) || (a.protocol && a.protocol.toLowerCase().includes(rightSearch.toLowerCase())))
                        .map((asset) => {
                          const assetIndex = optimizedAssets.findIndex(o => o.id === asset.id);
                          const isExpanded = !compactMode || expandedAssetIds.has(asset.id);
                          return (
                            <div 
                              key={asset.id} 
                              style={{ 
                                padding: '10px', 
                                borderRadius: '6px', 
                                border: '1px solid var(--border-color)',
                                background: 'rgba(255,255,255,0.01)',
                                display: 'flex',
                                flexDirection: 'column',
                                gap: '6px'
                              }}
                            >
                              {/* Card Header */}
                              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexShrink: 0 }}>
                                <span style={{ fontSize: '12px', fontWeight: '600', color: 'var(--primary)' }}>
                                  {t("序号")}: #{assetIndex + 1}
                                </span>
                                <div style={{ display: 'flex', gap: '6px', alignItems: 'center' }}>
                                  {compactMode && (
                                    <button
                                      onClick={() => toggleAssetExpand(asset.id)}
                                      className="btn btn-secondary"
                                      style={{ padding: '2px 6px', fontSize: '11px', height: '22px', border: 'none', background: 'transparent' }}
                                      type="button"
                                    >
                                      {isExpanded ? t('收起 ▴') : t('展开 ▾')}
                                    </button>
                                  )}
                                  <button
                                    onClick={() => handleRemoveFromOptimized(asset.id)}
                                    className="btn btn-secondary"
                                    style={{ padding: '2px 8px', fontSize: '11px', color: 'var(--accent)', borderColor: 'rgba(225,29,72,0.25)', height: '22px' }}
                                    type="button"
                                  >
                                    {t("排除")}
                                  </button>
                                </div>
                              </div>

                              {/* Form Inputs (Name, Type & Protocol inline) */}
                              <div style={{ display: 'flex', gap: '8px' }}>
                                <input 
                                  type="text"
                                  className="input-field"
                                  style={{ padding: '5px 8px', fontSize: '12px', margin: 0, flex: 2 }}
                                  value={asset.name}
                                  onChange={(e) => handleUpdateOptimized(asset.id, 'name', e.target.value)}
                                  placeholder={t("名称")}
                                />
                                <select
                                  className="input-field"
                                  style={{ padding: '5px 8px', fontSize: '12px', margin: 0, flex: 1.5, height: '32px', cursor: 'pointer', background: 'var(--bg-card)', color: 'var(--text-primary)' }}
                                  value={asset.asset_type}
                                  onChange={(e) => handleUpdateOptimized(asset.id, 'asset_type', e.target.value)}
                                >
                                  <option value="data">{t("数据资产")}</option>
                                  <option value="software">{t("软件资产")}</option>
                                  <option value="hardware">{t("硬件资产")}</option>
                                  <option value="communication">{t("通信资产")}</option>
                                </select>
                                <input 
                                  type="text"
                                  className="input-field"
                                  style={{ padding: '5px 8px', fontSize: '12px', margin: 0, flex: 1 }}
                                  value={asset.protocol || ''}
                                  onChange={(e) => handleUpdateOptimized(asset.id, 'protocol', e.target.value)}
                                  placeholder={t("协议")}
                                />
                              </div>

                              {/* Expanded description area */}
                              {isExpanded && (
                                <textarea 
                                  className="input-field"
                                  style={{ padding: '6px 8px', fontSize: '11px', resize: 'none', margin: 0 }}
                                  rows={compactMode ? 2 : 3}
                                  value={asset.description || ''}
                                  onChange={(e) => handleUpdateOptimized(asset.id, 'description', e.target.value)}
                                  placeholder={t("资产备注描述与合并历史说明")}
                                />
                              )}
                            </div>
                          );
                        })}
                    </div>
                  </div>

                </div>

                {/* Footer Controls */}
                <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '12px', borderTop: '1px solid var(--border-color)', paddingTop: '16px', flexShrink: 0 }}>
                  <button
                    onClick={() => setShowDeduplicateModal(false)}
                    className="btn btn-secondary"
                    type="button"
                  >
                    {t("取消")}
                  </button>
                  <button
                    onClick={handleSaveDeduplicated}
                    className="btn btn-primary"
                    type="button"
                  >
                    {t("确认应用去重并保存资产")}
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
