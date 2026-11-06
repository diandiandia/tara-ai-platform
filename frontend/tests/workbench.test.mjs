import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { SourceTextModule, SyntheticModule, createContext } from 'node:vm';
import { transformWithOxc } from 'vite';

const source = await readFile(new URL('../src/components/Workbench.jsx', import.meta.url), 'utf8');
const i18nSource = await readFile(new URL('../src/stores/i18nStore.js', import.meta.url), 'utf8');
const { code } = await transformWithOxc(source, 'Workbench.jsx', { jsx: { runtime: 'automatic' } });

async function mountWorkbench(fetchSummary) {
  const slots = [];
  let cursor = 0;
  let changed = false;
  let effects = [];
  let tree;
  const calls = [];
  const sameDeps = (a, b) => a && b && a.length === b.length && a.every((v, i) => Object.is(v, b[i]));
  const hooks = {
    useState(initial) {
      const index = cursor++;
      slots[index] ??= { value: typeof initial === 'function' ? initial() : initial };
      return [slots[index].value, value => {
        const next = typeof value === 'function' ? value(slots[index].value) : value;
        if (!Object.is(next, slots[index].value)) {
          slots[index].value = next;
          changed = true;
        }
      }];
    },
    useRef(initial) {
      const index = cursor++;
      slots[index] ??= { current: initial };
      return slots[index];
    },
    useCallback(callback, deps) {
      const index = cursor++;
      if (!sameDeps(slots[index]?.deps, deps)) slots[index] = { value: callback, deps };
      return slots[index].value;
    },
    useEffect(effect, deps) {
      const index = cursor++;
      if (!sameDeps(slots[index]?.deps, deps)) {
        slots[index]?.cleanup?.();
        slots[index] = { deps };
        effects.push(() => { slots[index].cleanup = effect(); });
      }
    },
  };
  const action = name => async (...args) => { calls.push([name, ...args]); };
  const project = {
    currentProject: { id: 1, name: 'Test' }, domains: [],
    activeDomain: { id: 1, status: 'completed', name: 'A' },
    ...Object.fromEntries([
      'fetchProjectDetails', 'fetchDomains', 'createDomain', 'deleteDomain', 'setActiveDomain',
    ].map(name => [name, action(name)])),
  };
  const useProjectStore = () => project;
  useProjectStore.getState = () => project;
  const canvas = {
    diagrams: [], deletingDiagramIds: [],
    fetchDiagrams: action('fetchDiagrams'),
  };
  const tara = {
    assets: [], fetchAssets: action('fetchAssets'),
    fetchTaraProgress: action('fetchTaraProgress'),
    fetchTaraResults: action('fetchTaraResults'),
  };
  const dependencies = {
    react: hooks,
    'react/jsx-runtime': {
      Fragment: 'Fragment',
      jsx: (type, props) => ({ type, props }),
      jsxs: (type, props) => ({ type, props }),
    },
    zustand: {
      create: initializer => {
        let state = initializer(update => { state = { ...state, ...update }; });
        return selector => selector(state);
      },
    },
    '../stores/projectStore': { useProjectStore },
    '../stores/canvasStore': { useCanvasStore: () => canvas },
    '../stores/taraStore': { useTaraStore: () => tara },
    '../stores/reqMergeStore': {
      importCustomerRequirements: action('import'),
      previewCustomerRequirements: action('preview'),
      synthesizeMergedRequirements: action('merge'),
      exportMergedRequirements: action('export'),
      fetchCustomerRequirementCount: async () => 0,
      fetchWorkbenchSummary: (id, options) => {
        calls.push(['summary', id]);
        return fetchSummary(id, options);
      },
      fetchCsrMappingStatus: async () => ({ ready: false }),
    },
    'lucide-react': Object.fromEntries([
      'FolderPlus', 'GitFork', 'Table', 'Play', 'XCircle', 'Trash2', 'AlertTriangle',
      'ArrowLeft', 'Sparkles', 'CheckCircle2', 'Eye', 'Download', 'Upload', 'Merge', 'FileDown',
    ].map(name => [name, name])),
  };
  const context = createContext({
    console, AbortController, setInterval, clearInterval,
    localStorage: { getItem: () => null },
  });
  const link = async specifier => {
    if (specifier === '../stores/i18nStore') {
      const i18n = new SourceTextModule(i18nSource, { context });
      await i18n.link(link);
      return i18n;
    }
    const values = dependencies[specifier];
    assert.ok(values, `Unexpected import: ${specifier}`);
    return new SyntheticModule(Object.keys(values), function () {
      for (const [key, value] of Object.entries(values)) this.setExport(key, value);
    }, { context });
  };
  const module = new SourceTextModule(code, { context });
  await module.link(link);
  await module.evaluate();
  const render = () => {
    cursor = 0;
    changed = false;
    effects = [];
    tree = module.namespace.default({ projectId: 1 });
    effects.forEach(effect => effect());
  };
  const settle = async () => {
    for (let i = 0; i < 20; i++) {
      await new Promise(resolve => setImmediate(resolve));
      if (!changed) return;
      render();
    }
    assert.fail('Workbench effects did not settle');
  };
  render();
  await settle();
  return {
    calls, project, render, settle, tree: () => tree,
    close: () => slots.forEach(slot => slot?.cleanup?.()),
  };
}

function findButton(tree, title) {
  if (!tree || typeof tree !== 'object') return null;
  if (tree.type === 'button' && tree.props.title === title) return tree;
  const children = tree.props?.children;
  for (const child of Array.isArray(children) ? children.flat(Infinity) : [children]) {
    const found = findButton(child, title);
    if (found) return found;
  }
  return null;
}

test('workbench loads only lightweight summary and ignores same-domain object replacements', async () => {
  const workbench = await mountWorkbench(async id => ({
    domain_id: id, has_tara_results: true, merged_count: 200,
  }));
  try {
    assert.equal(workbench.calls.filter(([name]) => name === 'summary').length, 1);
    assert.equal(workbench.calls.filter(([name]) => name === 'fetchTaraResults').length, 0);
    assert.equal(findButton(workbench.tree(), '查看 TARA 结果').props.disabled, false);
    workbench.project.activeDomain = { ...workbench.project.activeDomain };
    workbench.render();
    await workbench.settle();
    assert.equal(workbench.calls.filter(([name]) => name === 'summary').length, 1);
    assert.equal(workbench.calls.filter(([name]) => name === 'fetchAssets').length, 1);
    workbench.project.activeDomain = { ...workbench.project.activeDomain, status: 'failed' };
    workbench.render();
    await workbench.settle();
    assert.equal(workbench.calls.filter(([name]) => name === 'summary').length, 2);
    workbench.project.activeDomain = null;
    workbench.render();
    await workbench.settle();
    assert.equal(workbench.calls.filter(([name]) => name === 'summary').length, 2);
  } finally {
    workbench.close();
  }
});

test('switching domain aborts pending summary and rejects its stale result', async () => {
  let resolveOld;
  let oldSignal;
  const workbench = await mountWorkbench((id, { signal }) => {
    if (id === 1) {
      oldSignal = signal;
      return new Promise(resolve => { resolveOld = resolve; });
    }
    return Promise.resolve({ domain_id: id, has_tara_results: false, merged_count: 0 });
  });
  try {
    workbench.project.activeDomain = { id: 2, name: 'B', status: 'completed' };
    workbench.render();
    await workbench.settle();
    assert.equal(oldSignal.aborted, true);
    resolveOld({ domain_id: 1, has_tara_results: true, merged_count: 200 });
    await workbench.settle();
    assert.equal(findButton(workbench.tree(), '正在加载 TARA 分析结果，请稍候...').props.disabled, true);
    assert.equal(workbench.calls.filter(([name]) => name === 'summary').length, 2);
  } finally {
    workbench.close();
  }
});
