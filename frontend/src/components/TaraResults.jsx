import { useI18n } from '../stores/i18nStore';
import { useEffect, useState } from 'react';
import { useTaraStore } from '../stores/taraStore';
import { 
  ArrowLeft, CheckSquare, ShieldAlert, Edit3, 
  Download, Plus, BookOpen, Layers, Save, Trash2, X, ChevronLeft, ChevronRight
} from 'lucide-react';

// --- MAPPING AND CALCULATION HELPER FUNCTIONS (ISO 21434) ---

const calculateDifficultyAndFeasibility = (tc, exp, kn, win, eq) => {
  let score = 0;
  
  // 1. Time Consuming
  if (tc === 'no_more_than_1d') score += 0;
  else if (tc === 'no_more_than_1w') score += 1;
  else if (tc === 'no_more_than_1m') score += 4;
  else if (tc === 'no_more_than_6m') score += 17;
  else if (tc === 'more_than_6m') score += 19;
  else score += 1; // default fallback
  
  // 2. Expertise
  if (exp === 'layman') score += 0;
  else if (exp === 'proficient') score += 3;
  else if (exp === 'expert') score += 6;
  else if (exp === 'expert_multiple') score += 8;
  else score += 3;
  
  // 3. Knowledge about TOE
  if (kn === 'public') score += 0;
  else if (kn === 'restricted') score += 3;
  else if (kn === 'confidential') score += 7;
  else if (kn === 'strictly_confidential') score += 11;
  else score += 3;
  
  // 4. Window of Opportunity
  if (win === 'unlimited') score += 0;
  else if (win === 'easy') score += 1;
  else if (win === 'moderate') score += 4;
  else if (win === 'difficult') score += 10;
  else score += 1;
  
  // 5. Equipment
  if (eq === 'standard') score += 0;
  else if (eq === 'special' || eq === 'specialized') score += 4;
  else if (eq === 'bespoke') score += 7;
  else if (eq === 'bespoke_multiple') score += 9;
  else score += 0;
  
  // Determine feasibility Level
  let feas;
  if (score >= 25) feas = 'Very Low';
  else if (score >= 20) feas = 'Low';
  else if (score >= 14) feas = 'Medium';
  else feas = 'High';
  
  return { difficulty: score, feasibility: feas };
};

// Auto-calculate Risk Value based on ISO 21434 Risk Matrix
const calculateRiskValue = (overallImpact, feasibility) => {
  const feasKey = String(feasibility).toLowerCase().replace(/\s+/g, '');
  const riskMatrix = {
    'verylow': [1, 1, 1, 2],
    'low': [1, 2, 2, 3],
    'medium': [1, 2, 3, 4],
    'high': [1, 3, 4, 5]
  };
  const list = riskMatrix[feasKey] || [1, 2, 3, 4];
  const idx = Math.min(Math.max(parseInt(overallImpact) || 0, 0), 3);
  return list[idx];
};

const tcLabels = {
  'no_more_than_1d': '< 1d',
  'no_more_than_1w': '< 1w',
  'no_more_than_1m': '< 1m',
  'no_more_than_6m': '< 6m',
  'more_than_6m': '> 6m'
};

const getExpLabel = (key, t) => {
  const mapping = {
    'layman': t('无专业知识'),
    'proficient': t('熟悉'),
    'expert': t('专家'),
    'expert_multiple': t('多个专家')
  };
  return mapping[key] || key;
};

const getKnLabel = (key, t) => {
  const mapping = {
    'public': t('公开'),
    'restricted': t('受限'),
    'confidential': t('机密'),
    'strictly_confidential': t('严格机密')
  };
  return mapping[key] || key;
};

const getWinLabel = (key, t) => {
  const mapping = {
    'unlimited': t('无限制'),
    'easy': t('易'),
    'moderate': t('中等'),
    'difficult': t('难')
  };
  return mapping[key] || key;
};

const getEqLabel = (key, t) => {
  const mapping = {
    'standard': t('标准'),
    'special': t('专用'),
    'specialized': t('专用'),
    'bespoke': t('定制'),
    'bespoke_multiple': t('多个定制')
  };
  return mapping[key] || key;
};

// 归一化风险处置为 ISO 21434 标准值 (Avoid/Reduce/Share/Retain)，兼容历史/大小写输入
const normalizeTreatment = (val) => {
  const key = String(val || '').toLowerCase().replace(/\s+/g, '');
  const map = {
    reduce: 'Reduce', mitigate: 'Reduce',          // 缓解
    retain: 'Retain', accept: 'Retain',            // 接受
    share: 'Share', transfer: 'Share',             // 转移
    avoid: 'Avoid',                                // 规避
  };
  return map[key] || 'Reduce';
};

// 是否免除 CSR：与后端「无 Reduce 即免除」逻辑等价（Avoid/Share/Retain 均免除）
const isTreatmentExempted = (val) => normalizeTreatment(val) !== 'Reduce';

const getRiskTreatmentLabel = (value, t) => {
  const mapping = {
    'Reduce': t('缓解风险'),
    'Retain': t('接受风险'),
    'Share': t('转移风险'),
    'Avoid': t('规避风险')
  };
  return mapping[normalizeTreatment(value)] || value;
};

const getImpactLabel = (value, t) => {
  const mapping = {
    'Negligible': t('轻微'),
    'Moderate': t('中等'),
    'Major': t('重要'),
    'Severe': t('严重')
  };
  return mapping[value] || value;
};

const normalizeFeas = (val) => {
  const mapping = {
    'veryhigh': 'Very High',
    'high': 'High',
    'medium': 'Medium',
    'low': 'Low',
    'verylow': 'Very Low'
  };
  const key = String(val || '').toLowerCase().replace(/\s+/g, '');
  return mapping[key] || val || 'Medium';
};

const getAssetSn = (asset) => {
  const prefixMap = {
    "hardware": "H",
    "software": "S",
    "data": "D",
    "communication": "C"
  };
  const t = String(asset.asset_type || '').toLowerCase().trim();
  const prefix = prefixMap[t] || "A";
  const idStr = String(asset.id).padStart(3, '0');
  return `${prefix}-001_${idStr}`;
};

const inferSecurityDomain = (reqText) => {
  const reqLower = String(reqText || '').toLowerCase();
  if (reqLower.includes("ota") || reqLower.includes("update") || reqLower.includes("升级")) {
    return "安全升级 / Secure Update";
  }
  if (reqLower.includes("secoc") || reqLower.includes("transmission") || reqLower.includes("通信") || reqLower.includes("message") || reqLower.includes("bus")) {
    return "安全通信 / Secure Transmission";
  }
  if (reqLower.includes("crypt") || reqLower.includes("encrypt") || reqLower.includes("key") || reqLower.includes("signature") || reqLower.includes("sign") || reqLower.includes("verify") || reqLower.includes("hash") || reqLower.includes("算法") || reqLower.includes("密码") || reqLower.includes("秘钥") || reqLower.includes("密钥")) {
    return "密码学与存储安全 / Cryptography & Storage Security";
  }
  if (reqLower.includes("diag") || reqLower.includes("diagnose") || reqLower.includes("诊断")) {
    return "诊断安全 / Diagnostics Security";
  }
  if (reqLower.includes("access") || reqLower.includes("auth") || reqLower.includes("login") || reqLower.includes("permission") || reqLower.includes("访问") || reqLower.includes("授权") || reqLower.includes("身份")) {
    return "访问控制 / Access Control";
  }
  return "通用安全 / General Security";
};

const collectCsrReportData = (steps, assetsList) => {
  const stepsByAsset = {};
  steps.forEach(step => {
    if (!stepsByAsset[step.asset_id]) {
      stepsByAsset[step.asset_id] = {};
    }
    stepsByAsset[step.asset_id][step.stage] = step;
  });

  const rows = [];
  let csrNum = 1;
  const sortedAssets = [...assetsList].sort((a, b) => a.id - b.id);

  // Generate reqMap from all steps first
  const reqSet = new Set();
  sortedAssets.forEach(asset => {
    const assetSteps = stepsByAsset[asset.id];
    if (!assetSteps) return;

    const stage5Out = assetSteps['stage5']?.analysis_result?.final_output || {};
    let summarizedCsrs = stage5Out.summarized_csrs || [];

    if (summarizedCsrs.length === 0) {
      const requirements = stage5Out.requirements || [];
      requirements.forEach(req => {
        const rq = String(req.cybersecurity_requirement || '').trim();
        if (rq && rq !== 'N/A') reqSet.add(rq);
      });
    } else {
      summarizedCsrs.forEach(csr => {
        const rq = String(csr.cybersecurity_requirement || '').trim();
        if (rq && rq !== 'N/A') reqSet.add(rq);
      });
    }
  });

  const sortedReqs = Array.from(reqSet).sort();
  const reqMap = new Map(sortedReqs.map((r, i) => [r, `CSR-${String(i + 1).padStart(4, '0')}`]));

  sortedAssets.forEach(asset => {
    const assetSteps = stepsByAsset[asset.id];
    if (!assetSteps) return;

    const stage5Out = assetSteps['stage5']?.analysis_result?.final_output || {};
    let summarizedCsrs = stage5Out.summarized_csrs || [];

    if (summarizedCsrs.length === 0) {
      const requirements = stage5Out.requirements || [];
      const deviceReqs = requirements.filter(req => {
        const alloc = String(req.allocated_to_device || '').toLowerCase().trim();
        return alloc === 'yes' || alloc === 'true';
      });

      const seenReqTexts = new Set();
      summarizedCsrs = [];
      deviceReqs.forEach(req => {
        const reqText = req.cybersecurity_requirement || '';
        if (!reqText || seenReqTexts.has(reqText)) return;
        seenReqTexts.add(reqText);

        const domainVal = req.security_domain || inferSecurityDomain(reqText);
        const mappedId = reqMap.get(reqText.trim()) || `CSR-0000`;

        summarizedCsrs.push({
          asset_id: `ID${asset.id}`,
          asset_name: asset.name,
          cybersecurity_requirement_id: mappedId,
          csr_id: mappedId,
          title: `针对 ${asset.name} 的安全要求 / Security requirement for ${asset.name}`,
          sub_title: `网络安全防护 / Cybersecurity protection for ${asset.name}`,
          security_domain: domainVal,
          cybersecurity_requirement: reqText
        });
      });
    }

    summarizedCsrs.forEach(csr => {
      const padNum = String(csrNum).padStart(4, '0');
      const reqText = String(csr.cybersecurity_requirement || '').trim();
      const cId = reqMap.get(reqText) || csr.csr_id || csr.cybersecurity_requirement_id || `CSR-${padNum}`;
      rows.push({
        number: `CSR_${padNum}`,
        csr_id: cId,
        security_domain: csr.security_domain || "通用安全 / General Security",
        asset_sn: getAssetSn(asset),
        asset_name: asset.name,
        title: csr.title || "N/A",
        sub_title: csr.sub_title || "N/A",
        cybersecurity_requirement: csr.cybersecurity_requirement || "N/A"
      });
      csrNum++;
    });
  });

  return rows;
};

const computeContentHashId = (prefix, text) => {
  // Simple content-based hash using djb2 + fnv1a for collision resistance
  const str = (text || '').trim();
  if (!str || str === 'N/A') return 'N/A';
  let h1 = 5381;
  let h2 = 0x811c9dc5;
  for (let i = 0; i < str.length; i++) {
    const c = str.charCodeAt(i);
    h1 = ((h1 << 5) + h1 + c) >>> 0;
    h2 = (h2 ^ c) * 0x01000193 >>> 0;
  }
  const combined = ((h1 ^ h2) >>> 0).toString(16).padStart(8, '0').toUpperCase();
  return `${prefix}${combined}`;
};

const computeSequentialIds = (rows) => {
  return rows.map(row => {
    const newRow = { ...row };
    const isExempted = isTreatmentExempted(row.risk_treatment);

    if (isExempted) {
      const claim = String(row.cybersecurity_claim || '').trim();
      newRow.cybersecurity_claim_id = (claim && claim !== 'N/A') ? computeContentHashId('CLM_', claim) : 'N/A';
      newRow.cso_id = 'N/A';
      newRow.cybersecurity_control_id = 'N/A';
      newRow.cybersecurity_requirement_id = 'N/A';
    } else {
      newRow.cybersecurity_claim_id = 'N/A';

      const goal = String(row.cso || '').trim();
      newRow.cso_id = (goal && goal !== 'N/A') ? computeContentHashId('CSO_', goal) : 'N/A';

      const control = String(row.cybersecurity_control || '').trim();
      newRow.cybersecurity_control_id = (control && control !== 'N/A') ? computeContentHashId('CSC_', control) : 'N/A';

      const req = String(row.csr || '').trim();
      if (req && req !== 'N/A') {
        const lines = req.split('\n').map(line => line.trim()).filter(Boolean);
        if (lines.length > 1) {
          const mappedLines = lines.map((line, idx) => {
            const cleaned = line.replace(/^\(\d+\)\s*/, '').trim();
            const cid = computeContentHashId('CSR_', cleaned);
            return `(${idx + 1}) ${cid}`;
          });
          newRow.cybersecurity_requirement_id = mappedLines.join('\n');
        } else {
          const cleaned = req.replace(/^\(\d+\)\s*/, '').trim();
          newRow.cybersecurity_requirement_id = computeContentHashId('CSR_', cleaned);
        }
      } else {
        newRow.cybersecurity_requirement_id = 'N/A';
      }
    }

    return newRow;
  });
};

// --- HELPERS FOR SERIALIZATION & DESERIALIZATION ---

const buildExcelRowsFromSteps = (steps, assetsList) => {
  const rows = [];
  
  // Group steps by asset
  const stepsByAsset = {};
  steps.forEach(step => {
    if (!stepsByAsset[step.asset_id]) {
      stepsByAsset[step.asset_id] = {};
    }
    stepsByAsset[step.asset_id][step.stage] = step;
  });
  
  // For each asset, build the rows
  assetsList.forEach(asset => {
    const assetSteps = stepsByAsset[asset.id];
    if (!assetSteps) return;
    
    const s1 = assetSteps['stage1']?.analysis_result?.final_output || {};
    const s2 = assetSteps['stage2']?.analysis_result?.final_output || {};
    const s3 = assetSteps['stage3']?.analysis_result?.final_output || {};
    const s4 = assetSteps['stage4']?.analysis_result?.final_output || {};
    const s5 = assetSteps['stage5']?.analysis_result?.final_output || {};
    
    // Get selected attributes
    const selectedAttrs = s1.selected_attributes || [];
    if (selectedAttrs.length === 0) return;
    
    selectedAttrs.forEach(attr => {
      // Find matching damage scenarios
      const dscs = s2.damage_scenarios || [];
      let matchingDs = dscs.filter(d => d.attribute === attr);
      if (matchingDs.length === 0) {
        matchingDs = [{
          attribute: attr,
          damage_scenario_sn: `DS_${attr}_1`,
          damage_scenario: s2.damage_scenario || '',
          impact_rating: s2.impact_rating || { safety: 'Negligible', financial: 'Negligible', operational: 'Negligible', privacy: 'Negligible' },
          overall_impact: s2.overall_impact || 0
        }];
      }
      
      matchingDs.forEach((ds, dsIdx) => {
        // Find matching threat scenarios
        const tscs = s3.threat_scenarios || [];
        let matchingTs = tscs.filter(t => t.attribute === attr && t.damage_scenario_sn === ds.damage_scenario_sn);
        if (matchingTs.length === 0) {
          matchingTs = [{
            attribute: attr,
            damage_scenario_sn: ds.damage_scenario_sn,
            threat_id: `TS_${attr}_${dsIdx + 1}`,
            threat_scenario: s3.threat_scenario || '',
            attack_paths: s3.attack_paths || [],
            final_feasibility: normalizeFeas(s3.final_feasibility || 'Medium')
          }];
        }
        
        matchingTs.forEach((ts) => {
          // Find matching risk decision
          const rdec = s4.risk_decisions || [];
          let rd = rdec.find(r => r.threat_id === ts.threat_id);
          if (!rd) {
            rd = {
              risk_value: s4.risk_rating || 1,
              risk_treatment: normalizeTreatment(s4.risk_decision),
              justification: s4.justification || '',
              caf_level: normalizeFeas(ts.final_feasibility || 'Medium'),
              cybersecurity_claim: s4.cybersecurity_claim || '',
              cybersecurity_goal: s4.cybersecurity_goal || ''
            };
          }
          
          // Find matching requirements
          const reqs = s5.requirements || [];
          let matchingReqs = reqs.filter(r => r.threat_id === ts.threat_id);
          if (matchingReqs.length === 0) {
            const csrList = s5.csr || [];
            if (csrList.length > 0) {
              matchingReqs = csrList.map(c => ({
                cybersecurity_control: s5.cso || '',
                cybersecurity_requirement: c
              }));
            } else {
              matchingReqs = [{
                cybersecurity_control: s5.cso || '',
                cybersecurity_requirement: ''
              }];
            }
          }
          
          // Merge requirements into single multiline text fields (with index prefix if multiple)
          const csrIdVal = matchingReqs.map((r, rIdx) => {
            const val = r.cybersecurity_requirement_id || 'N/A';
            return matchingReqs.length > 1 ? `(${rIdx + 1}) ${val}` : val;
          }).join('\n');
          const csrTextVal = matchingReqs.map((r, rIdx) => {
            const val = r.cybersecurity_requirement || '';
            return matchingReqs.length > 1 && val ? `(${rIdx + 1}) ${val}` : val;
          }).join('\n');
          const controlIdVal = matchingReqs.map((r, rIdx) => {
            const val = r.cybersecurity_control_id || 'N/A';
            return matchingReqs.length > 1 ? `(${rIdx + 1}) ${val}` : val;
          }).join('\n');
          const controlTextVal = matchingReqs.map((r, rIdx) => {
            const val = r.cybersecurity_control || '';
            return matchingReqs.length > 1 && val ? `(${rIdx + 1}) ${val}` : val;
          }).join('\n');
          
          const anyAllocated = matchingReqs.some(r => {
            const alloc = String(r.allocated_to_device || '').toLowerCase().trim();
            return alloc === 'yes' || alloc === 'true';
          });
          const allocatedToDeviceVal = anyAllocated ? 'Yes' : 'No';

          // Get attack paths
          let attackPaths = ts.attack_paths || [];
          if (attackPaths.length === 0) {
            attackPaths = [{
              attack_path: '',
              time_consuming: 'no_more_than_1w',
              expertise: 'proficient',
              knowledge_about_toe: 'restricted',
              window_of_opportunity: 'easy',
              equipment: 'standard',
              difficulty: 1
            }];
          }

          const afVal = normalizeFeas(ts.final_feasibility || 'Medium');
          const cafVal = normalizeFeas(rd.caf_level || afVal);
          const calculatedRisk = calculateRiskValue(ds.overall_impact || 0, cafVal);
          
          attackPaths.forEach((apDetail, apIdx) => {
            const attackPathVal = apDetail.attack_path || '';
            const tc = apDetail.time_consuming || 'no_more_than_1w';
            const exp = apDetail.expertise || 'proficient';
            const kn = apDetail.knowledge_about_toe || 'restricted';
            const win = apDetail.window_of_opportunity || 'easy';
            const eq = apDetail.equipment || 'standard';
            const diff = apDetail.difficulty !== undefined ? apDetail.difficulty : 1;
            
            const rowFeas = normalizeFeas(apDetail.feasibility || apDetail.final_feasibility || afVal);
            
            rows.push({
              key: `${asset.id}-${attr}-${ds.damage_scenario_sn}-${ts.threat_id}-${apIdx}`,
              asset_id: asset.id,
              asset_name: asset.name,
              attribute: attr,
              damage_scenario: ds.damage_scenario || '',
              safety: typeof ds.impact_rating?.safety === 'number' ? ['Negligible', 'Moderate', 'Major', 'Severe'][ds.impact_rating?.safety] || 'Negligible' : ds.impact_rating?.safety || 'Negligible',
              financial: typeof ds.impact_rating?.financial === 'number' ? ['Negligible', 'Moderate', 'Major', 'Severe'][ds.impact_rating?.financial] || 'Negligible' : ds.impact_rating?.financial || 'Negligible',
              operational: typeof ds.impact_rating?.operational === 'number' ? ['Negligible', 'Moderate', 'Major', 'Severe'][ds.impact_rating?.operational] || 'Negligible' : ds.impact_rating?.operational || 'Negligible',
              privacy: typeof ds.impact_rating?.privacy === 'number' ? ['Negligible', 'Moderate', 'Major', 'Severe'][ds.impact_rating?.privacy] || 'Negligible' : ds.impact_rating?.privacy || 'Negligible',
              overall_impact: ds.overall_impact || 0,
              threat_id: ts.threat_id,
              threat_scenario: ts.threat_scenario || '',
              attack_path: attackPathVal || '',
              time_consuming: tc,
              expertise: exp,
              knowledge_about_toe: kn,
              window_of_opportunity: win,
              equipment: eq,
              difficulty: diff,
              final_feasibility: rowFeas,
              caf_level: cafVal,
              cafOverridden: cafVal !== afVal,
              risk_value: calculatedRisk,
              risk_treatment: normalizeTreatment(rd.risk_treatment || rd.risk_decision),

              // CSO, Claims, Control, Device, CSR
              cybersecurity_claim_id: rd.cybersecurity_claim_id || (isTreatmentExempted(rd.risk_treatment) ? `CLM_${attr}_${ts.threat_id}` : 'N/A'),
              cybersecurity_claim: rd.cybersecurity_claim || '',
              cso_id: rd.cybersecurity_goal_id || (normalizeTreatment(rd.risk_treatment) === 'Reduce' ? `CSO_${attr}_${ts.threat_id}` : 'N/A'),
              cso: rd.cybersecurity_goal || '',
              cybersecurity_control_id: controlIdVal || (controlTextVal && controlTextVal !== 'N/A' ? `CSO_${attr}_${ts.threat_id}` : 'N/A'),
              cybersecurity_control: controlTextVal || '',
              allocated_to_device: allocatedToDeviceVal,
              cybersecurity_requirement_id: csrIdVal || (csrTextVal && csrTextVal !== 'N/A' ? `CSR_${attr}_${ts.threat_id}_1` : 'N/A'),
              csr: csrTextVal,
            });
          });
        });
      });
    });
  });
  
  return computeSequentialIds(rows);
};

const compile5StagesForAsset = (assetId, assetRows) => {
  const selectedAttrs = Array.from(new Set(assetRows.map(r => r.attribute)));
  
  // 1. Stage 1
  const s1Output = {
    confidentiality: selectedAttrs.includes('Confidentiality') ? 'High' : 'None',
    integrity: selectedAttrs.includes('Integrity') ? 'High' : 'None',
    availability: selectedAttrs.includes('Availability') ? 'High' : 'None',
    authenticity: selectedAttrs.includes('Authenticity') ? 'High' : 'None',
    'non-repudiation': selectedAttrs.includes('Non-repudiation') ? 'High' : 'None',
    authorization: selectedAttrs.includes('Authorization') ? 'High' : 'None',
    privacy: selectedAttrs.includes('Privacy') ? 'High' : 'None',
    selected_attributes: selectedAttrs,
    description: '手动故障备用属性分析'
  };
  
  // 2. Stage 2
  const order = { 'Negligible': 0, 'Moderate': 1, 'Major': 2, 'Severe': 3 };
  const dscs = [];
  const processedAttrs = new Set();
  
  assetRows.forEach((row) => {
    const attrKey = `${row.attribute}-${row.damage_scenario}`;
    if (!processedAttrs.has(attrKey)) {
      processedAttrs.add(attrKey);
      dscs.push({
        attribute: row.attribute,
        damage_scenario_sn: row.damage_scenario_sn || `DS_${row.attribute}_${dscs.length + 1}`,
        damage_scenario: row.damage_scenario || '手工定义损害场景',
        impact_rating: {
          safety: row.safety,
          financial: row.financial,
          operational: row.operational,
          privacy: row.privacy
        },
        overall_impact: row.overall_impact
      });
    }
  });
  
  let max_s = 0, max_f = 0, max_o = 0, max_p = 0;
  dscs.forEach(d => {
    max_s = Math.max(max_s, order[d.impact_rating.safety] || 0);
    max_f = Math.max(max_f, order[d.impact_rating.financial] || 0);
    max_o = Math.max(max_o, order[d.impact_rating.operational] || 0);
    max_p = Math.max(max_p, order[d.impact_rating.privacy] || 0);
  });
  const max_overall = Math.max(max_s, max_f, max_o, max_p);
  
  const s2Output = {
    damage_scenarios: dscs,
    damage_scenario: dscs.map(d => d.damage_scenario).join('; '),
    impact_rating: {
      safety: max_s,
      financial: max_f,
      operational: max_o,
      privacy: max_p
    },
    overall_impact: max_overall
  };
  
  // 3. Stage 3
  const tscs = [];
  const threatMap = new Map();
  assetRows.forEach((row) => {
    const threatKey = `${row.attribute}-${row.threat_scenario}`;
    if (!threatMap.has(threatKey)) {
      threatMap.set(threatKey, []);
    }
    threatMap.get(threatKey).push(row);
  });
  
  let tIndex = 1;
  for (const [threatKey, rowsForThreat] of threatMap.entries()) {
    const firstRow = rowsForThreat[0];
    const dsObj = dscs.find(d => d.attribute === firstRow.attribute && d.damage_scenario === firstRow.damage_scenario) || dscs.find(d => d.attribute === firstRow.attribute) || {};
    const dsSn = dsObj.damage_scenario_sn || `DS_${firstRow.attribute}_1`;
    
    const existingId = firstRow.threat_id;
    const threatId = (existingId && existingId.startsWith('TS_')) ? existingId : `TS_${firstRow.attribute}_${tIndex}`;
    tIndex++;
    
    const paths = rowsForThreat.map((row, apIdx) => ({
      path_id: `P_${row.attribute}_${threatId}_${apIdx + 1}`,
      attack_path: row.attack_path || '手工定义攻击路径',
      time_consuming: row.time_consuming || 'no_more_than_1w',
      expertise: row.expertise || 'proficient',
      knowledge_about_toe: row.knowledge_about_toe || 'restricted',
      window_of_opportunity: row.window_of_opportunity || 'easy',
      equipment: row.equipment || 'standard',
      difficulty: parseInt(row.difficulty) || 1,
      feasibility: row.final_feasibility
    }));
    
    const feasibilityOrder = { 'Very Low': 1, 'Low': 2, 'Medium': 3, 'High': 4, 'Very High': 5 };
    let maxFeas = 'Very Low';
    paths.forEach(p => {
      const curF = normalizeFeas(p.feasibility || 'Medium');
      if ((feasibilityOrder[curF] || 1) > (feasibilityOrder[maxFeas] || 1)) {
        maxFeas = curF;
      }
    });
    
    tscs.push({
      attribute: firstRow.attribute,
      damage_scenario_sn: dsSn,
      threat_id: threatId,
      threat_scenario: firstRow.threat_scenario || '手工定义威胁场景',
      attack_paths: paths,
      final_feasibility: maxFeas
    });
  }
  
  const s3Output = {
    threat_scenarios: tscs,
    threat_scenario: tscs.map(t => t.threat_scenario).join('; '),
    attack_paths: tscs.flatMap(t => t.attack_paths),
    final_feasibility: tscs[0]?.final_feasibility || 'Medium'
  };
  
  // 4. Stage 4
  const rdecs = [];
  tscs.forEach((ts) => {
    const row = assetRows.find(r => r.attribute === ts.attribute && r.threat_scenario === ts.threat_scenario) || assetRows[0];
    const isExempted = isTreatmentExempted(row.risk_treatment);
    rdecs.push({
      threat_id: ts.threat_id,
      attribute: ts.attribute,
      risk_value: parseInt(row.risk_value) || 1,
      risk_treatment: normalizeTreatment(row.risk_treatment),
      caf_level: row.caf_level || ts.final_feasibility || 'Medium',
      justification: '手工录入决策',
      cybersecurity_claim_id: isExempted ? (row.cybersecurity_claim_id || 'N/A') : 'N/A',
      cybersecurity_claim: isExempted ? (row.cybersecurity_claim || '接受/转移网络安全风险') : '',
      cybersecurity_goal_id: isExempted ? 'N/A' : (row.cso_id || 'N/A'),
      cybersecurity_goal: isExempted ? '' : (row.cso || '保护资产不受威胁')
    });
  });
  
  const max_risk = rdecs.length > 0 ? Math.max(...rdecs.map(r => r.risk_value)) : 1;
  const s4Output = {
    risk_decisions: rdecs,
    risk_rating: max_risk,
    risk_decision: rdecs[0]?.risk_treatment || 'Reduce',
    justification: '手工录入风险决策'
  };
  
  // 5. Stage 5
  const reqs = [];
  tscs.forEach((ts) => {
    const row = assetRows.find(r => r.attribute === ts.attribute && r.threat_scenario === ts.threat_scenario) || assetRows[0];
    const isExempted = isTreatmentExempted(row.risk_treatment);
    
    // We clean prefix bullet marks like (1), (2) from requirements and split by line
    const csrList = row.csr ? row.csr.split('\n').map(line => line.replace(/^\(\d+\)\s*/, '').trim()).filter(Boolean) : [];
    const ctrlIdLines = row.cybersecurity_control_id ? row.cybersecurity_control_id.split('\n').map(line => line.replace(/^\(\d+\)\s*/, '').trim()).filter(Boolean) : [];
    const ctrlLines = row.cybersecurity_control ? row.cybersecurity_control.split('\n').map(line => line.replace(/^\(\d+\)\s*/, '').trim()).filter(Boolean) : [];
    const reqIdLines = row.cybersecurity_requirement_id ? row.cybersecurity_requirement_id.split('\n').map(line => line.replace(/^\(\d+\)\s*/, '').trim()).filter(Boolean) : [];
    
    if (isExempted) {
      reqs.push({
        threat_id: ts.threat_id,
        cybersecurity_control_id: 'N/A',
        cybersecurity_control: 'N/A',
        allocated_to_device: 'No',
        cybersecurity_requirement_id: 'N/A',
        cybersecurity_requirement: 'N/A'
      });
    } else {
      if (csrList.length > 0) {
        csrList.forEach((csrText, cIdx) => {
          const reqIdVal = reqIdLines[cIdx] || reqIdLines[0] || 'N/A';
          const ctrlIdVal = ctrlIdLines[cIdx] || ctrlIdLines[0] || 'N/A';
          const ctrlVal = ctrlLines[cIdx] || ctrlLines[0] || '实施安全控制手段';
          
          reqs.push({
            threat_id: ts.threat_id,
            cybersecurity_control_id: ctrlIdVal,
            cybersecurity_control: ctrlVal,
            allocated_to_device: row.allocated_to_device || 'Yes',
            cybersecurity_requirement_id: reqIdVal,
            cybersecurity_requirement: csrText
          });
        });
      } else {
        reqs.push({
          threat_id: ts.threat_id,
          cybersecurity_control_id: row.cybersecurity_control_id || 'N/A',
          cybersecurity_control: row.cybersecurity_control || '实施安全控制手段',
          allocated_to_device: row.allocated_to_device || 'Yes',
          cybersecurity_requirement_id: row.cybersecurity_requirement_id || 'N/A',
          cybersecurity_requirement: '手工安全要求'
        });
      }
    }
  });
  
  const cso_val = rdecs.find(r => r.cybersecurity_goal)?.cybersecurity_goal || '手工定义安全目标';
  const csr_list_val = reqs.map(r => r.cybersecurity_requirement).filter(c => c && c !== 'N/A');
  const is_exempt = rdecs.every(r => isTreatmentExempted(r.risk_treatment));
  
  const s5Output = {
    requirements: reqs,
    cso: cso_val,
    csr: csr_list_val,
    exempted: is_exempt,
    reason: is_exempt ? '手工分析免除安全控制目标' : ''
  };
  
  return [
    { asset_id: assetId, stage: 'stage1', output: s1Output },
    { asset_id: assetId, stage: 'stage2', output: s2Output },
    { asset_id: assetId, stage: 'stage3', output: s3Output },
    { asset_id: assetId, stage: 'stage4', output: s4Output },
    { asset_id: assetId, stage: 'stage5', output: s5Output }
  ];
};

export default function TaraResults({ setPage, domainId }) {
  const { t } = useI18n();
  const {
    taraResults,
    assets,
    fetchTaraResults,
    fetchAssets,
    submitManualOfflineResults,
    exportReport,
    loading,
    error,
    clearError
  } = useTaraStore();

  const [activeTab, setActiveTab] = useState('review'); // 'review' or 'matrix'

  // Excel layout flat rows
  const [taraRows, setTaraRows] = useState([]);
  const updateTaraRows = (updater) => {
    setTaraRows(prev => {
      const nextRows = typeof updater === 'function' ? updater(prev) : updater;
      return computeSequentialIds(nextRows);
    });
  };
  const [editingRowKey, setEditingRowKey] = useState(null);
  const [originalRowBackup, setOriginalRowBackup] = useState(null);

  // Pagination states
  const [currentPage, setCurrentPage] = useState(1);
  const [matrixCurrentPage, setMatrixCurrentPage] = useState(1);
  const itemsPerPage = 10;

  useEffect(() => {
    setTimeout(() => {
      setMatrixCurrentPage(1);
    }, 0);
  }, [activeTab, domainId]);

  // Export States
  const [exportFormat, setExportFormat] = useState('xlsx');
  const [exportDesensitize, setExportDesensitize] = useState(false);
  const [exporting, setExporting] = useState(false);

  useEffect(() => {
    if (domainId) {
      fetchTaraResults(domainId);
      fetchAssets(domainId);
    }
  }, [domainId, fetchAssets, fetchTaraResults]);

  useEffect(() => {
    if (taraResults.length > 0 && assets.length > 0) {
      const rows = buildExcelRowsFromSteps(taraResults, assets);
      setTimeout(() => updateTaraRows(rows), 0);
    } else {
      setTimeout(() => updateTaraRows([]), 0);
    }
  }, [taraResults, assets]);

  // Handle adding an empty row inline
  const handleAddRow = () => {
    const confirmedAssets = assets.filter(a => a.status === 'confirmed');
    if (confirmedAssets.length === 0) {
      alert(t('暂无确认状态的资产，请先在画布确认资产后添加行。'));
      return;
    }
    
    const newRow = {
      key: `new-${Date.now()}`,
      isNew: true,
      asset_id: confirmedAssets[0].id,
      asset_name: confirmedAssets[0].name,
      attribute: 'Integrity',
      damage_scenario: '',
      safety: 'Negligible',
      financial: 'Negligible',
      operational: 'Negligible',
      privacy: 'Negligible',
      overall_impact: 0,
      threat_scenario: '',
      attack_path: '',
      time_consuming: 'no_more_than_1w',
      expertise: 'proficient',
      knowledge_about_toe: 'restricted',
      window_of_opportunity: 'easy',
      equipment: 'standard',
      difficulty: 8,
      final_feasibility: 'Medium',
      caf_level: 'Medium',
      cafOverridden: false,
      risk_value: 1, // calculated
      risk_treatment: 'Reduce',
      cybersecurity_claim_id: 'N/A',
      cybersecurity_claim: '',
      cso_id: `CSO_Integrity_${Date.now()}`,
      cso: '',
      cybersecurity_control_id: `CSO_Integrity_${Date.now()}`,
      cybersecurity_control: '',
      allocated_to_device: 'Yes',
      cybersecurity_requirement_id: `CSR_Integrity_${Date.now()}_1`,
      csr: ''
    };
    
    updateTaraRows(prev => [...prev, newRow]);
    setOriginalRowBackup(null);
    setEditingRowKey(newRow.key);
    
    // Jump to the last page where the new row will be placed
    const totalPages = Math.ceil((taraRows.length + 1) / itemsPerPage);
    setCurrentPage(totalPages);
  };

  // Handle in-line changes
  const handleRowChange = (key, field, value) => {
    updateTaraRows(prev => prev.map(row => {
      if (row.key === key) {
        let updatedRow = { ...row, [field]: value };
        
        // If S/F/O/P changed, automatically recalculate overall impact
        if (['safety', 'financial', 'operational', 'privacy'].includes(field)) {
          const order = { 'Negligible': 0, 'Moderate': 1, 'Major': 2, 'Severe': 3 };
          const s = order[updatedRow.safety] || 0;
          const f = order[updatedRow.financial] || 0;
          const o = order[updatedRow.operational] || 0;
          const p = order[updatedRow.privacy] || 0;
          updatedRow.overall_impact = Math.max(s, f, o, p);
        }
        
        // If Feasibility Factors changed, automatically recalculate Difficulty points and AF Level
        if (['time_consuming', 'expertise', 'knowledge_about_toe', 'window_of_opportunity', 'equipment'].includes(field)) {
          const tc = field === 'time_consuming' ? value : updatedRow.time_consuming;
          const exp = field === 'expertise' ? value : updatedRow.expertise;
          const kn = field === 'knowledge_about_toe' ? value : updatedRow.knowledge_about_toe;
          const win = field === 'window_of_opportunity' ? value : updatedRow.window_of_opportunity;
          const eq = field === 'equipment' ? value : updatedRow.equipment;
          
          const calc = calculateDifficultyAndFeasibility(tc, exp, kn, win, eq);
          updatedRow.difficulty = calc.difficulty;
          updatedRow.final_feasibility = calc.feasibility;
          
          // Auto sync CAF level with calculated AF level if not manually calibrated
          if (!updatedRow.cafOverridden) {
            updatedRow.caf_level = calc.feasibility;
          }
        }
        
        // Track manual calibration of CAF Level
        if (field === 'caf_level') {
          updatedRow.cafOverridden = true;
        }
        
        // Automatically calculate Risk Value based on overall_impact and caf_level
        updatedRow.risk_value = calculateRiskValue(updatedRow.overall_impact, updatedRow.caf_level);
        
        return updatedRow;
      }
      return row;
    }));
  };

  // Handle in-line editing trigger
  const handleEditRowClick = (row) => {
    setEditingRowKey(row.key);
    setOriginalRowBackup({ ...row });
  };

  // Cancel edit
  const handleCancelEdit = (rowKey) => {
    if (rowKey.startsWith('new-')) {
      // Remove temp row
      updateTaraRows(prev => prev.filter(r => r.key !== rowKey));
    } else if (originalRowBackup) {
      // Restore previous values
      updateTaraRows(prev => prev.map(r => r.key === rowKey ? originalRowBackup : r));
    }
    setEditingRowKey(null);
    setOriginalRowBackup(null);
  };

  // Save changes to database
  const handleSaveRow = async (rowKey) => {
    const row = taraRows.find(r => r.key === rowKey);
    if (!row) return;

    if (!row.damage_scenario?.trim()) {
      alert(t('损害场景不能为空！'));
      return;
    }
    if (!row.threat_scenario?.trim()) {
      alert(t('威胁场景不能为空！'));
      return;
    }

    // Group local state rows by asset_id
    const rowsByAsset = {};
    taraRows.forEach(r => {
      if (!rowsByAsset[r.asset_id]) {
        rowsByAsset[r.asset_id] = [];
      }
      rowsByAsset[r.asset_id].push(r);
    });

    // Compile steps for all present assets
    const allStepsPayload = [];
    Object.keys(rowsByAsset).forEach(assetIdStr => {
      const aId = parseInt(assetIdStr);
      const assetRows = rowsByAsset[assetIdStr];
      const steps = compile5StagesForAsset(aId, assetRows);
      allStepsPayload.push(...steps);
    });

    // Merge any existing assets from the DB that are not in the current table rows
    const presentAssetIds = Object.keys(rowsByAsset).map(id => parseInt(id));
    
    // Deduplicate existing steps from taraResults by run
    const stepsByAssetDB = {};
    taraResults.forEach(step => {
      if (!stepsByAssetDB[step.asset_id]) {
        stepsByAssetDB[step.asset_id] = [];
      }
      stepsByAssetDB[step.asset_id].push(step);
    });

    Object.keys(stepsByAssetDB).forEach(assetIdStr => {
      const aId = parseInt(assetIdStr);
      if (!presentAssetIds.includes(aId)) {
        stepsByAssetDB[assetIdStr].forEach(step => {
          allStepsPayload.push({
            asset_id: step.asset_id,
            stage: step.stage,
            output: step.analysis_result.final_output
          });
        });
      }
    });

    const res = await submitManualOfflineResults(domainId, allStepsPayload);
    if (res) {
      setEditingRowKey(null);
      setOriginalRowBackup(null);
      alert(t('手动修订数据保存成功！'));
      fetchTaraResults(domainId);
    }
  };

  // Delete row
  const handleDeleteRow = async (rowKey) => {
    if (window.confirm(t('您确定要彻底删除这一行分析场景吗？保存后对应的数据将被清除。'))) {
      const originalAssetIds = Array.from(new Set(taraRows.map(r => r.asset_id)));
      const remainingRows = taraRows.filter(r => r.key !== rowKey);
      updateTaraRows(remainingRows);
      
      // If it was a temp unsaved row, just hide it
      if (rowKey.startsWith('new-')) {
        setEditingRowKey(null);
        return;
      }

      // Compile remaining rows to save
      const rowsByAsset = {};
      remainingRows.forEach(r => {
        if (!rowsByAsset[r.asset_id]) {
          rowsByAsset[r.asset_id] = [];
        }
        rowsByAsset[r.asset_id].push(r);
      });

      const allStepsPayload = [];
      Object.keys(rowsByAsset).forEach(assetIdStr => {
        const aId = parseInt(assetIdStr);
        const assetRows = rowsByAsset[assetIdStr];
        const steps = compile5StagesForAsset(aId, assetRows);
        allStepsPayload.push(...steps);
      });

      // Maintain other untouched asset steps
      const stepsByAssetDB = {};
      taraResults.forEach(step => {
        if (!stepsByAssetDB[step.asset_id]) {
          stepsByAssetDB[step.asset_id] = [];
        }
        stepsByAssetDB[step.asset_id].push(step);
      });

      Object.keys(stepsByAssetDB).forEach(assetIdStr => {
        const aId = parseInt(assetIdStr);
        if (!originalAssetIds.includes(aId)) {
          stepsByAssetDB[assetIdStr].forEach(step => {
            allStepsPayload.push({
              asset_id: step.asset_id,
              stage: step.stage,
              output: step.analysis_result.final_output
            });
          });
        }
      });

      // Submit manual update
      const res = await submitManualOfflineResults(domainId, allStepsPayload);
      if (res) {
        setEditingRowKey(null);
        setOriginalRowBackup(null);
        alert(t('场景行删除成功并已同步到评估库！'));
        fetchTaraResults(domainId);
      }
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

  const getAssetLabel = (assetId) => {
    const asset = assets.find(a => a.id === assetId);
    return asset ? `${asset.name} (${asset.asset_type})` : `${t("资产")} #${assetId}`;
  };



  // Pagination helper parameters
  const totalPages = Math.ceil(taraRows.length / itemsPerPage);
  const indexOfLastItem = currentPage * itemsPerPage;
  const indexOfFirstItem = indexOfLastItem - itemsPerPage;
  const currentRows = taraRows.slice(indexOfFirstItem, indexOfLastItem);

  const matrixRows = collectCsrReportData(taraResults, assets);
  const matrixTotalPages = Math.ceil(matrixRows.length / itemsPerPage);
  const matrixPaginatedRows = matrixRows.slice(
    (matrixCurrentPage - 1) * itemsPerPage,
    matrixCurrentPage * itemsPerPage
  );

  return (
    <div className="dashboard-container" style={{ maxWidth: 'none', width: '100%' }}>
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
              {t("审阅并直接修订子系统的安全评估结果。支持表格内嵌修改与即时导出。")}
            </p>
          </div>
        </div>

        {activeTab === 'review' && (
          <div style={{ display: 'flex', gap: '12px' }}>
            <button
              onClick={handleAddRow}
              className="btn btn-secondary"
              style={{ border: '1px dashed var(--border-glow)' }}
            >
              <Plus size={16} />
              <span>{t("手动添加评估行")}</span>
            </button>
          </div>
        )}
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
          <span>{t("TARA 评估详情表")}</span>
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
          <span>{t("项目级安全控制要求矩阵")}</span>
        </button>
      </div>

      {loading && taraRows.length === 0 ? (
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', padding: '45px 0' }}>
          <div className="spinner"></div>
          <span style={{ color: 'var(--text-secondary)' }}>{t("正在加载评估数据...")}</span>
        </div>
      ) : taraRows.length === 0 ? (
        <div className="glass" style={{ padding: '60px 40px', textAlign: 'center', color: 'var(--text-secondary)' }}>
          <Layers size={48} style={{ color: 'var(--text-muted)', marginBottom: '16px' }} />
          <h3 style={{ fontSize: '18px', marginBottom: '8px', color: 'var(--text-primary)' }}>{t("没有找到分析记录")}</h3>
          <p style={{ fontSize: '14px', maxWidth: '440px', margin: '0 auto' }}>
            {t("该子系统域控尚未生成分析步骤数据。您可以在工作台点击“启动 TARA 分析”派发异步任务，或者使用右上角“手动添加评估行”直接在下方填写。")}
          </p>
        </div>
      ) : activeTab === 'review' ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '24px' }}>
          
          {/* Report Export Panel */}
          <div className="glass" style={{ padding: '20px 24px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '16px' }}>
            <div>
              <h4 style={{ fontSize: '15px', fontWeight: '600', color: 'var(--text-primary)' }}>{t("导出评估报告")}</h4>
              <p style={{ color: 'var(--text-secondary)', fontSize: '12px', marginTop: '2px' }}>{t("一键生成与 Excel 完全对齐的 XLSX 工作簿或 CSV 归档规范文档")}</p>
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
          <div className="table-container" style={{ overflowX: 'auto', border: '1px solid var(--border-color)', borderRadius: '8px', background: 'var(--bg-card)' }}>
            <table className="custom-table" style={{ width: '100%', borderCollapse: 'collapse', fontSize: '11px' }}>
              <thead>
                <tr style={{ background: 'rgba(15, 23, 42, 0.05)', borderBottom: '1px solid var(--border-color)' }}>
                  <th style={{ minWidth: '100px', padding: '10px' }}>{t("资产名称")}</th>
                  <th style={{ minWidth: '95px', padding: '10px' }}>{t("安全属性")}</th>
                  <th style={{ minWidth: '160px', padding: '10px' }}>{t("危害/损害场景")}</th>
                  <th style={{ minWidth: '85px', padding: '10px' }}>{t("S (安全)")}</th>
                  <th style={{ minWidth: '85px', padding: '10px' }}>{t("F (财务)")}</th>
                  <th style={{ minWidth: '85px', padding: '10px' }}>{t("O (运营)")}</th>
                  <th style={{ minWidth: '85px', padding: '10px' }}>{t("P (隐私)")}</th>
                  <th style={{ width: '80px', padding: '10px' }}>{t("影响等级")}</th>
                  <th style={{ minWidth: '160px', padding: '10px' }}>{t("威胁场景")}</th>
                  <th style={{ minWidth: '140px', padding: '10px' }}>{t("攻击路径")}</th>
                  
                  {/* Excel Attack Feasibility Factors */}
                  <th style={{ minWidth: '85px', padding: '10px' }}>{t("时间开销")}</th>
                  <th style={{ minWidth: '85px', padding: '10px' }}>{t("专业知识")}</th>
                  <th style={{ minWidth: '85px', padding: '10px' }}>{t("TOE知识")}</th>
                  <th style={{ minWidth: '85px', padding: '10px' }}>{t("机会窗口")}</th>
                  <th style={{ minWidth: '85px', padding: '10px' }}>{t("所需设备")}</th>
                  <th style={{ width: '60px', padding: '10px' }}>{t("折算分值")}</th>
                  <th style={{ width: '80px', padding: '10px' }}>{t("AF Level")}</th>
                  <th style={{ width: '80px', padding: '10px' }}>{t("CAF Level")}</th>
                  
                  <th style={{ width: '60px', padding: '10px' }}>{t("风险值")}</th>
                  <th style={{ minWidth: '95px', padding: '10px' }}>{t("处理决策")}</th>
                  
                  {/* Cybersecurity Goals and Requirements */}
                  <th style={{ minWidth: '140px', padding: '10px' }}>{t("安全声称 (Claims)")}</th>
                  <th style={{ minWidth: '140px', padding: '10px' }}>{t("安全目标 (CSO)")}</th>
                  <th style={{ minWidth: '140px', padding: '10px' }}>{t("安全控制")}</th>
                  <th style={{ width: '80px', padding: '10px' }}>{t("分配至设备")}</th>
                  <th style={{ minWidth: '160px', padding: '10px' }}>{t("安全要求 (CSR)")}</th>
                  <th style={{ minWidth: '90px', padding: '10px', textAlign: 'center', position: 'sticky', right: 0, background: 'var(--bg-card)', zIndex: 10 }}>{t("操作")}</th>
                </tr>
              </thead>
              <tbody>
                {currentRows.map((row) => {
                  const isEditing = editingRowKey === row.key;
                  
                  return (
                    <tr key={row.key} style={{ borderBottom: '1px solid var(--border-color)', height: '48px' }}>
                      
                      {/* Asset Name */}
                      <td style={{ padding: '8px' }}>
                        {isEditing ? (
                          <select
                            value={row.asset_id}
                            onChange={(e) => handleRowChange(row.key, 'asset_id', parseInt(e.target.value))}
                            className="input-field"
                            style={{ width: '100%', fontSize: '11px', padding: '4px' }}
                          >
                            {assets.filter(a => a.status === 'confirmed').map(a => (
                              <option key={a.id} value={a.id}>{a.name}</option>
                            ))}
                          </select>
                        ) : (
                          <span style={{ fontWeight: '500' }}>{getAssetLabel(row.asset_id)}</span>
                        )}
                      </td>

                      {/* Security Attribute */}
                      <td style={{ padding: '8px' }}>
                        {isEditing ? (
                          <select
                            value={row.attribute}
                            onChange={(e) => handleRowChange(row.key, 'attribute', e.target.value)}
                            className="input-field"
                            style={{ width: '100%', fontSize: '11px', padding: '4px' }}
                          >
                            <option value="Confidentiality">Confidentiality</option>
                            <option value="Integrity">Integrity</option>
                            <option value="Availability">Availability</option>
                            <option value="Authenticity">Authenticity</option>
                            <option value="Non-repudiation">Non-repudiation</option>
                            <option value="Authorization">Authorization</option>
                            <option value="Privacy">Privacy</option>
                          </select>
                        ) : (
                          <span>{row.attribute}</span>
                        )}
                      </td>

                      {/* Damage Scenario */}
                      <td style={{ padding: '8px' }}>
                        {isEditing ? (
                          <textarea
                            value={row.damage_scenario}
                            onChange={(e) => handleRowChange(row.key, 'damage_scenario', e.target.value)}
                            className="input-field"
                            rows={2}
                            style={{ width: '100%', fontSize: '11px', padding: '4px', resize: 'vertical' }}
                          />
                        ) : (
                          <p style={{ margin: 0, maxWidth: '200px', whiteSpace: 'normal', wordBreak: 'break-all' }}>{row.damage_scenario}</p>
                        )}
                      </td>

                      {/* Safety (S) */}
                      <td style={{ padding: '8px' }}>
                        {isEditing ? (
                          <select value={row.safety} onChange={(e) => handleRowChange(row.key, 'safety', e.target.value)} className="input-field" style={{ width: '100%', fontSize: '10px', padding: '2px' }}>
                            <option value="Negligible">{t('轻微')}</option>
                            <option value="Moderate">{t('中等')}</option>
                            <option value="Major">{t('重要')}</option>
                            <option value="Severe">{t('严重')}</option>
                          </select>
                        ) : (
                          <span>{getImpactLabel(row.safety, t)}</span>
                        )}
                      </td>

                      {/* Financial (F) */}
                      <td style={{ padding: '8px' }}>
                        {isEditing ? (
                          <select value={row.financial} onChange={(e) => handleRowChange(row.key, 'financial', e.target.value)} className="input-field" style={{ width: '100%', fontSize: '10px', padding: '2px' }}>
                            <option value="Negligible">{t('轻微')}</option>
                            <option value="Moderate">{t('中等')}</option>
                            <option value="Major">{t('重要')}</option>
                            <option value="Severe">{t('严重')}</option>
                          </select>
                        ) : (
                          <span>{getImpactLabel(row.financial, t)}</span>
                        )}
                      </td>

                      {/* Operational (O) */}
                      <td style={{ padding: '8px' }}>
                        {isEditing ? (
                          <select value={row.operational} onChange={(e) => handleRowChange(row.key, 'operational', e.target.value)} className="input-field" style={{ width: '100%', fontSize: '10px', padding: '2px' }}>
                            <option value="Negligible">{t('轻微')}</option>
                            <option value="Moderate">{t('中等')}</option>
                            <option value="Major">{t('重要')}</option>
                            <option value="Severe">{t('严重')}</option>
                          </select>
                        ) : (
                          <span>{getImpactLabel(row.operational, t)}</span>
                        )}
                      </td>

                      {/* Privacy (P) */}
                      <td style={{ padding: '8px' }}>
                        {isEditing ? (
                          <select value={row.privacy} onChange={(e) => handleRowChange(row.key, 'privacy', e.target.value)} className="input-field" style={{ width: '100%', fontSize: '10px', padding: '2px' }}>
                            <option value="Negligible">{t('轻微')}</option>
                            <option value="Moderate">{t('中等')}</option>
                            <option value="Major">{t('重要')}</option>
                            <option value="Severe">{t('严重')}</option>
                          </select>
                        ) : (
                          <span>{getImpactLabel(row.privacy, t)}</span>
                        )}
                      </td>

                      {/* Overall Impact Level */}
                      <td style={{ padding: '8px', textAlign: 'center' }}>
                        <span style={{ 
                          fontWeight: '600', 
                          padding: '2px 6px', 
                          borderRadius: '4px',
                          background: row.overall_impact >= 3 ? 'rgba(244, 63, 94, 0.1)' : row.overall_impact === 2 ? 'rgba(245, 158, 11, 0.1)' : 'rgba(59, 130, 246, 0.1)',
                          color: row.overall_impact >= 3 ? '#fda4af' : row.overall_impact === 2 ? '#fcd34d' : '#93c5fd'
                        }}>
                          {t(['轻微', '中等', '重要', '严重'][row.overall_impact] || '轻微')}
                        </span>
                      </td>

                      {/* Threat Scenario */}
                      <td style={{ padding: '8px' }}>
                        {isEditing ? (
                          <textarea
                            value={row.threat_scenario}
                            onChange={(e) => handleRowChange(row.key, 'threat_scenario', e.target.value)}
                            className="input-field"
                            rows={2}
                            style={{ width: '100%', fontSize: '11px', padding: '4px', resize: 'vertical' }}
                          />
                        ) : (
                          <p style={{ margin: 0, maxWidth: '200px', whiteSpace: 'normal', wordBreak: 'break-all' }}>{row.threat_scenario}</p>
                        )}
                      </td>

                      {/* Attack Path */}
                      <td style={{ padding: '8px' }}>
                        {isEditing ? (
                          <textarea
                            value={row.attack_path}
                            onChange={(e) => handleRowChange(row.key, 'attack_path', e.target.value)}
                            className="input-field"
                            rows={2}
                            style={{ width: '100%', fontSize: '11px', padding: '4px', resize: 'vertical' }}
                          />
                        ) : (
                          <p style={{ margin: 0, maxWidth: '160px', whiteSpace: 'normal', wordBreak: 'break-all', fontSize: '11px', color: 'var(--text-secondary)' }}>{row.attack_path || '-'}</p>
                        )}
                      </td>

                      {/* --- EXCEL FEASIBILITY FACTORS (INLINE EDITING) --- */}

                      {/* Time Consuming */}
                      <td style={{ padding: '8px' }}>
                        {isEditing ? (
                          <select
                            value={row.time_consuming}
                            onChange={(e) => handleRowChange(row.key, 'time_consuming', e.target.value)}
                            className="input-field"
                            style={{ width: '100%', fontSize: '10px', padding: '2px' }}
                          >
                            <option value="no_more_than_1d">&lt; 1d</option>
                            <option value="no_more_than_1w">&lt; 1w</option>
                            <option value="no_more_than_1m">&lt; 1m</option>
                            <option value="no_more_than_6m">&lt; 6m</option>
                            <option value="more_than_6m">&gt; 6m</option>
                          </select>
                        ) : (
                          <span>{tcLabels[row.time_consuming] || row.time_consuming}</span>
                        )}
                      </td>

                      {/* Expertise */}
                      <td style={{ padding: '8px' }}>
                        {isEditing ? (
                          <select
                            value={row.expertise}
                            onChange={(e) => handleRowChange(row.key, 'expertise', e.target.value)}
                            className="input-field"
                            style={{ width: '100%', fontSize: '10px', padding: '2px' }}
                          >
                            <option value="layman">{t('无专业知识')}</option>
                            <option value="proficient">{t('熟悉')}</option>
                            <option value="expert">{t('专家')}</option>
                            <option value="expert_multiple">{t('多个专家')}</option>
                          </select>
                        ) : (
                          <span>{getExpLabel(row.expertise, t)}</span>
                        )}
                      </td>

                      {/* Knowledge about TOE */}
                      <td style={{ padding: '8px' }}>
                        {isEditing ? (
                          <select
                            value={row.knowledge_about_toe}
                            onChange={(e) => handleRowChange(row.key, 'knowledge_about_toe', e.target.value)}
                            className="input-field"
                            style={{ width: '100%', fontSize: '10px', padding: '2px' }}
                          >
                            <option value="public">{t('公开')}</option>
                            <option value="restricted">{t('受限')}</option>
                            <option value="confidential">{t('机密')}</option>
                            <option value="strictly_confidential">{t('严格机密')}</option>
                          </select>
                        ) : (
                          <span>{getKnLabel(row.knowledge_about_toe, t)}</span>
                        )}
                      </td>

                      {/* Window of opportunity */}
                      <td style={{ padding: '8px' }}>
                        {isEditing ? (
                          <select
                            value={row.window_of_opportunity}
                            onChange={(e) => handleRowChange(row.key, 'window_of_opportunity', e.target.value)}
                            className="input-field"
                            style={{ width: '100%', fontSize: '10px', padding: '2px' }}
                          >
                            <option value="unlimited">{t('无限制')}</option>
                            <option value="easy">{t('易')}</option>
                            <option value="moderate">{t('中等')}</option>
                            <option value="difficult">{t('难')}</option>
                          </select>
                        ) : (
                          <span>{getWinLabel(row.window_of_opportunity, t)}</span>
                        )}
                      </td>

                      {/* Equipment */}
                      <td style={{ padding: '8px' }}>
                        {isEditing ? (
                          <select
                            value={row.equipment}
                            onChange={(e) => handleRowChange(row.key, 'equipment', e.target.value)}
                            className="input-field"
                            style={{ width: '100%', fontSize: '10px', padding: '2px' }}
                          >
                            <option value="standard">{t('标准')}</option>
                            <option value="special">{t('专用')}</option>
                            <option value="bespoke">{t('定制')}</option>
                            <option value="bespoke_multiple">{t('多个定制')}</option>
                          </select>
                        ) : (
                          <span>{getEqLabel(row.equipment, t)}</span>
                        )}
                      </td>

                      {/* Difficulty points (calculated) */}
                      <td style={{ padding: '8px', textAlign: 'center', fontWeight: 'bold', color: 'var(--primary)' }}>
                        {row.difficulty}
                      </td>

                      {/* AF Level (calculated feasibility) */}
                      <td style={{ padding: '8px' }}>
                        <span style={{ 
                          fontWeight: '600',
                          color: row.final_feasibility === 'High' ? '#f43f5e' : row.final_feasibility === 'Medium' ? '#f59e0b' : '#10b981'
                        }}>
                          {row.final_feasibility}
                        </span>
                      </td>

                      {/* CAF Level (calibrated feasibility) */}
                      <td style={{ padding: '8px' }}>
                        {isEditing ? (
                          <select
                            value={row.caf_level}
                            onChange={(e) => handleRowChange(row.key, 'caf_level', e.target.value)}
                            className="input-field"
                            style={{ width: '100%', fontSize: '10px', padding: '2px' }}
                          >
                            <option value="Very High">Very High</option>
                            <option value="High">High</option>
                            <option value="Medium">Medium</option>
                            <option value="Low">Low</option>
                            <option value="Very Low">Very Low</option>
                          </select>
                        ) : (
                          <span style={{ 
                            fontWeight: '600',
                            color: row.caf_level === 'High' ? '#f43f5e' : row.caf_level === 'Medium' ? '#f59e0b' : '#10b981'
                          }}>
                            {row.caf_level}
                          </span>
                        )}
                      </td>

                      {/* --- END OF EXCEL FEASIBILITY FACTORS --- */}

                      {/* Risk Value (Readonly, calculated) */}
                      <td style={{ padding: '8px', textAlign: 'center' }}>
                        <span style={{ 
                          fontWeight: '700', 
                          color: row.risk_value >= 4 ? '#f43f5e' : row.risk_value === 3 ? '#f59e0b' : '#10b981',
                          fontSize: '13px'
                        }}>
                          {row.risk_value}
                        </span>
                      </td>

                      {/* Risk Treatment */}
                      <td style={{ padding: '8px' }}>
                        {isEditing ? (
                          <select
                            value={row.risk_treatment}
                            onChange={(e) => handleRowChange(row.key, 'risk_treatment', e.target.value)}
                            className="input-field"
                            style={{ width: '100%', fontSize: '11px', padding: '4px' }}
                          >
                            <option value="Reduce">{t('缓解风险')}</option>
                            <option value="Retain">{t('接受风险')}</option>
                            <option value="Share">{t('转移风险')}</option>
                            <option value="Avoid">{t('规避风险')}</option>
                          </select>
                        ) : (
                          <span>{getRiskTreatmentLabel(row.risk_treatment, t)}</span>
                        )}
                      </td>



                      {/* Cybersecurity Claims */}
                      <td style={{ padding: '8px' }}>
                        {isEditing ? (
                          <textarea
                            value={row.cybersecurity_claim}
                            onChange={(e) => handleRowChange(row.key, 'cybersecurity_claim', e.target.value)}
                            className="input-field"
                            rows={2}
                            style={{ width: '100%', fontSize: '11px', padding: '4px', resize: 'vertical' }}
                          />
                        ) : (
                          <p style={{ margin: 0, maxWidth: '160px', whiteSpace: 'normal', wordBreak: 'break-all' }}>{row.cybersecurity_claim || '-'}</p>
                        )}
                      </td>



                      {/* CSO (Cybersecurity Goal) */}
                      <td style={{ padding: '8px' }}>
                        {isEditing ? (
                          <textarea
                            value={row.cso}
                            onChange={(e) => handleRowChange(row.key, 'cso', e.target.value)}
                            className="input-field"
                            rows={2}
                            style={{ width: '100%', fontSize: '11px', padding: '4px', resize: 'vertical' }}
                          />
                        ) : (
                          <p style={{ margin: 0, maxWidth: '160px', whiteSpace: 'normal', wordBreak: 'break-all' }}>{row.cso || '-'}</p>
                        )}
                      </td>



                      {/* Cybersecurity Control */}
                      <td style={{ padding: '8px' }}>
                        {isEditing ? (
                          <textarea
                            value={row.cybersecurity_control}
                            onChange={(e) => handleRowChange(row.key, 'cybersecurity_control', e.target.value)}
                            className="input-field"
                            rows={2}
                            style={{ width: '100%', fontSize: '11px', padding: '4px', resize: 'vertical' }}
                          />
                        ) : (
                          <p style={{ margin: 0, maxWidth: '160px', whiteSpace: 'normal', wordBreak: 'break-all' }}>{row.cybersecurity_control || '-'}</p>
                        )}
                      </td>

                      {/* Allocated to device */}
                      <td style={{ padding: '8px', textAlign: 'center' }}>
                        {isEditing ? (
                          <select
                            value={row.allocated_to_device}
                            onChange={(e) => handleRowChange(row.key, 'allocated_to_device', e.target.value)}
                            className="input-field"
                            style={{ width: '100%', fontSize: '11px', padding: '4px' }}
                          >
                            <option value="Yes">{t('是')}</option>
                            <option value="No">{t('否')}</option>
                          </select>
                        ) : (
                          <span style={{ fontWeight: '500', color: row.allocated_to_device === 'Yes' ? '#10b981' : 'var(--text-secondary)' }}>
                            {t(row.allocated_to_device === 'Yes' ? '是' : '否')}
                          </span>
                        )}
                      </td>



                      {/* CSR (Cybersecurity Requirement) */}
                      <td style={{ padding: '8px' }}>
                        {isEditing ? (
                          <textarea
                            value={row.csr}
                            onChange={(e) => handleRowChange(row.key, 'csr', e.target.value)}
                            className="input-field"
                            rows={2}
                            placeholder={t("每行写一条CSR要求...")}
                            style={{ width: '100%', fontSize: '11px', padding: '4px', resize: 'vertical' }}
                          />
                        ) : (
                          <p style={{ margin: 0, maxWidth: '200px', whiteSpace: 'pre-line', wordBreak: 'break-all', fontSize: '11px', color: 'var(--text-secondary)' }}>{row.csr || '-'}</p>
                        )}
                      </td>

                      {/* Action buttons */}
                      <td style={{ padding: '8px', textAlign: 'center', position: 'sticky', right: 0, background: 'var(--bg-card)', zIndex: 10, borderLeft: '1px solid var(--border-color)' }}>
                        <div style={{ display: 'flex', gap: '6px', justifyContent: 'center' }}>
                          {isEditing ? (
                            <>
                              <button
                                onClick={() => handleSaveRow(row.key)}
                                className="btn btn-primary"
                                style={{ padding: '4px 6px', background: '#10b981' }}
                                title={t("保存行修订")}
                              >
                                <Save size={12} />
                              </button>
                              <button
                                onClick={() => handleCancelEdit(row.key)}
                                className="btn btn-secondary"
                                style={{ padding: '4px 6px' }}
                                title={t("取消修改")}
                              >
                                <X size={12} />
                              </button>
                            </>
                          ) : (
                            <>
                              <button
                                onClick={() => handleEditRowClick(row)}
                                className="btn btn-secondary"
                                style={{ padding: '4px 6px' }}
                                disabled={editingRowKey !== null}
                                title={t("编辑场景行")}
                              >
                                <Edit3 size={12} />
                              </button>
                              <button
                                onClick={() => handleDeleteRow(row.key)}
                                className="btn btn-secondary"
                                style={{ padding: '4px 6px', color: '#f43f5e' }}
                                disabled={editingRowKey !== null}
                                title={t("删除场景行")}
                              >
                                <Trash2 size={12} />
                              </button>
                            </>
                          )}
                        </div>
                      </td>

                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {/* Pagination Controls */}
          {totalPages > 1 && (
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', padding: '12px 16px', background: 'var(--bg-card)', border: '1px solid var(--border-color)', borderRadius: '8px' }}>
              <span style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
                {t("显示")} {indexOfFirstItem + 1} - {Math.min(indexOfLastItem, taraRows.length)} {t("条，共")} {taraRows.length} {t("条分析记录")}
              </span>

              <div style={{ display: 'flex', gap: '8px' }}>
                <button
                  onClick={() => setCurrentPage(prev => Math.max(prev - 1, 1))}
                  disabled={currentPage === 1 || editingRowKey !== null}
                  className="btn btn-secondary"
                  style={{ padding: '6px 12px', display: 'flex', alignItems: 'center', gap: '4px' }}
                >
                  <ChevronLeft size={14} />
                  <span>{t("上一页")}</span>
                </button>

                <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                  {Array.from({ length: totalPages }, (_, i) => i + 1).map(page => (
                    <button
                      key={page}
                      onClick={() => setCurrentPage(page)}
                      disabled={editingRowKey !== null}
                      style={{
                        padding: '6px 10px',
                        border: '1px solid var(--border-color)',
                        borderRadius: '4px',
                        background: currentPage === page ? 'var(--primary)' : 'var(--bg-card)',
                        color: currentPage === page ? '#fff' : 'var(--text-primary)',
                        cursor: editingRowKey !== null ? 'not-allowed' : 'pointer',
                        fontSize: '12px',
                        fontWeight: '600'
                      }}
                    >
                      {page}
                    </button>
                  ))}
                </div>

                <button
                  onClick={() => setCurrentPage(prev => Math.min(prev + 1, totalPages))}
                  disabled={currentPage === totalPages || editingRowKey !== null}
                  className="btn btn-secondary"
                  style={{ padding: '6px 12px', display: 'flex', alignItems: 'center', gap: '4px' }}
                >
                  <span>{t("下一页")}</span>
                  <ChevronRight size={14} />
                </button>
              </div>
            </div>
          )}

        </div>
      ) : (
        /* Project-level requirement matrix */
        <div style={{ display: 'flex', flexDirection: 'column' }}>
          <div className="table-container" style={{ overflowX: 'auto', border: '1px solid var(--border-color)', borderRadius: '8px', background: 'var(--bg-card)' }}>
            <table className="custom-table" style={{ width: '100%', borderCollapse: 'collapse', fontSize: '11px' }}>
              <thead>
                <tr style={{ background: 'rgba(15, 23, 42, 0.05)', borderBottom: '1px solid var(--border-color)' }}>
                  <th style={{ minWidth: '130px', padding: '10px' }}>{t("Security Domain")}</th>
                  <th style={{ minWidth: '80px', padding: '10px' }}>{t("Asset SN")}</th>
                  <th style={{ minWidth: '100px', padding: '10px' }}>{t("Asset Name")}</th>
                  <th style={{ minWidth: '150px', padding: '10px' }}>{t("Requirement Title")}</th>
                  <th style={{ minWidth: '150px', padding: '10px' }}>{t("Requirement Subtitle")}</th>
                  <th style={{ minWidth: '220px', padding: '10px' }}>{t("Cybersecurity Requirement")}</th>
                </tr>
              </thead>
              <tbody>
                {matrixPaginatedRows.map((row, idx) => (
                  <tr key={idx} style={{ borderBottom: '1px solid var(--border-color)', height: '40px' }}>
                    <td style={{ padding: '8px' }}>{row.security_domain}</td>
                    <td style={{ padding: '8px', textAlign: 'center' }}>{row.asset_sn}</td>
                    <td style={{ padding: '8px', fontWeight: '500' }}>{row.asset_name}</td>
                    <td style={{ padding: '8px' }}>{row.title}</td>
                    <td style={{ padding: '8px' }}>{row.sub_title}</td>
                    <td style={{ padding: '8px', whiteSpace: 'pre-line', wordBreak: 'break-all' }}>{row.cybersecurity_requirement}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {/* Matrix Pagination Controls */}
          {matrixTotalPages > 1 && (
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: '16px' }}>
              <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
                {t("共")} {matrixRows.length} {t("条记录，当前第")} {matrixCurrentPage} / {matrixTotalPages} {t("页")}
              </div>
              <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
                <button
                  onClick={() => setMatrixCurrentPage(prev => Math.max(prev - 1, 1))}
                  disabled={matrixCurrentPage === 1}
                  className="btn btn-secondary"
                  style={{ padding: '6px 12px', display: 'flex', alignItems: 'center', gap: '4px' }}
                >
                  <ChevronLeft size={14} />
                  <span>{t("上一页")}</span>
                </button>

                <div style={{ display: 'flex', gap: '4px' }}>
                  {Array.from({ length: matrixTotalPages }, (_, i) => i + 1).map(page => (
                    <button
                      key={page}
                      onClick={() => setMatrixCurrentPage(page)}
                      style={{
                        padding: '6px 10px',
                        border: '1px solid var(--border-color)',
                        borderRadius: '4px',
                        background: matrixCurrentPage === page ? 'var(--primary)' : 'var(--bg-card)',
                        color: matrixCurrentPage === page ? '#fff' : 'var(--text-primary)',
                        cursor: 'pointer',
                        fontSize: '12px',
                        fontWeight: '600'
                      }}
                    >
                      {page}
                    </button>
                  ))}
                </div>

                <button
                  onClick={() => setMatrixCurrentPage(prev => Math.min(prev + 1, matrixTotalPages))}
                  disabled={matrixCurrentPage === matrixTotalPages}
                  className="btn btn-secondary"
                  style={{ padding: '6px 12px', display: 'flex', alignItems: 'center', gap: '4px' }}
                >
                  <span>{t("下一页")}</span>
                  <ChevronRight size={14} />
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
