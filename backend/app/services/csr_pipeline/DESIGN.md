# CSR Pipeline V2 — 通用 TARA 需求去重合并管道

## 概述

从 TARA stage5 输出的网络安全需求出发，经四阶段管道（预处理 → 判定 → 合并 → 输出），自动完成去重、互补合并、同构归组，输出可追溯、可审计的信息安全需求集合。

**核心设计原则**：
- LLM 只做分组判断，不做文本改写（判定层只输出 ID 分组）
- 每次 LLM 判定配反向验证自检，不一致进存疑
- 程序规则校验覆盖 LLM 典型错误（情态、否定、数字、条件）
- 错合并比漏合并严重，不确定默认不合并
- 全链路保留原始 ID，100% 可追溯
- 核心管道通用，项目特有部分配置化

---

## 一、数据模型（`models.py`）

### 1.1 PipelineRecord

管道中流转的唯一数据载体，只依赖 `RequirementSchema` 必填字段：

```python
@dataclass
class PipelineRecord:
    # —— Schema 必有字段（通用） ——
    record_id: str              # "ASSET-{asset_id}::{csr_id}"
    requirement_text: str       # cybersecurity_requirement 原文
    source_ids: list[str]       # source_requirement_ids（子条目级 ID）
    asset_name: str
    requirement_scope: str      # item / operational_environment / other_item
    requirement_type: str       # product / process / supplier
    owner: str                  # OEM / Tier1 / supplier
    allocated_to: str
    verification_method: str
    acceptance_criteria: str
    rationale: str
    dependencies: list[str]
    goal_ids: list[str]
    control_ids: list[str]
    claim_ids: list[str]

    # —— 管道动态抽取/填充字段 ——
    action_verbs: list[str] = field(default_factory=list)
    object_phrases: list[str] = field(default_factory=list)
    constraint_phrases: list[str] = field(default_factory=list)
    boundary_class: str = ""
    topic_label: str = ""
    atoms: list[Atom] = field(default_factory=list)

    # —— 合并状态 ——
    merge_stage: str = "NONE"   # NONE | EQUIVALENCE | COMPLEMENT | ISOMORPHIC
    merge_source_ids: list[str] = field(default_factory=list)
    canonical_text: str = ""
    canonical_origin: str = ""  # LLM / FALLBACK_ORIGINAL

    # —— 质量标记 ——
    is_uncertain: bool = False
    is_validation_failed: bool = False
    self_check_info: dict = field(default_factory=dict)
    audit_log: list[dict] = field(default_factory=list)
```

### 1.2 Atom

```python
@dataclass
class Atom:
    atom_id: str                # "record_id#N"
    text: str
    action_verb: str
    object_phrase: str
    constraint_phrase: str
    parent_record_id: str
```

### 1.3 FinalOutput

```python
@dataclass
class FinalOutput:
    requirements: list[dict]            # 旧格式兼容集
    requirements_extended: list[dict]   # 完整字段
    traceability_matrix: list[dict]     # 追溯表
    uncertainty_list: list[dict]        # 存疑清单
    statistics: dict                    # 统计报告
```

---

## 二、管道架构

```
输入：List[dict]（RequirementSchema/SummarizedRequirementSchema 的 dict 形式）
       │
  Phase 0: 初始化     make_record() → List[PipelineRecord]
       │
  Phase 1: 预处理     哈希去重 → 保守原子化 → 安全边界映射 → 主题粗分 → 结构化抽取
       │
  Phase 2: 判定       主题内分块等价判定+自检 → 并查集 → 全局归类+自检 → 回原文裁决
       │
  Phase 3: 合并       互补合并 → 规范化表述+程序校验 → 同构归组+程序校验
       │
  Phase 4: 输出       一致性校验 → 分类编号(可插拔) → 追溯表 → 存疑清单 → 统计报告
       │
输出：FinalOutput
```

### 阶段失败降级

任何阶段整体失败（LLM 超时、异常），该阶段跳过，条目原样传递，并在存疑清单记录。

---

## 三、各阶段详细设计

### Phase 0: 初始化

从 stage5 的 `requirements` 或 `summarized_requirements` 中提取，调用 `make_record()` 生成 `PipelineRecord` 列表。纯程序，不调用 LLM。

`source_ids` 使用子条目级 ID（`source_requirement_ids` 的并集 + `cybersecurity_requirement_id`）。

### Phase 1: 预处理

#### 1.1 哈希去重（`hash_dedup.py`）
- 文本规范化（去空格、全半角统一、英文小写、去标点差异）
- MD5 哈希分组，相同哈希的归一组，保留代表，其余 ID 合并
- 编辑距离 > 0.95 但哈希不同 → 标记 `near_duplicate = True`，不进此刻合并，留给回原文裁决
- 纯程序，可复现

#### 1.2 保守原子化（`conservative_atomize.py`）
- 默认不拆
- 同一安全目标的"动作 + 失败处理"不拆
- 同一动作的并列宾语不拆
- 不同安全目标的并列才拆
- LLM 判断 + 程序校验无损性（并集覆盖情态/否定/数字/条件）
- 一次 LLM 调用 + 一次反向自检

#### 1.3 安全边界映射（`boundary_mapper.py`）
- 规则引擎优先：从可配置映射表匹配
- 匹配不上 → LLM 批量判断
- 边界类只用于分组维度，不作为合并决策
- 映射表从 `config.py` 读取

#### 1.4 主题粗分（`topic_classifier.py`）
- 每批 30-40 条发 LLM
- 输出 `{record_id: topic_label}`
- 主题标签集从 `config.py` 读取
- 一次 LLM 调用 + 一次反向自检
- 自检不一致 → `topic_uncertain = True`，分块时不强制按主题分组

#### 1.5 结构化抽取（Phase 1 末尾）
- 抽取 `action_verbs`、`object_phrases`、`constraint_phrases`
- 规则优先（内置动作词词典），LLM 兜底
- 这三个字段只给 Phase 3 的规则校验器用，不参与 Phase 2 等价判定

### Phase 2: 判定

#### 2.1 主题内分块等价判定 + 自检
- 按 `(boundary_class, topic_label)` 二维分组
- 每组内 20-30 条一块，50% 重叠
- 第一次 LLM 调用（正向）："哪些等价？输出分组"（只输出 ID，不写文本）
- 第二次 LLM 调用（反向自检）："这些分组里有没有不该合并的？有没有该合没合的？"
- 不一致或低置信度 → 该分组标记 `is_uncertain`，不进合并
- `topic_uncertain` 的条目不按 topic_label 分块

#### 2.2 并查集归并局部结果
- 纯程序，处理传递性
- 重叠块通过共同成员 ID 归并

#### 2.3 全局小组名归类 + 自检
- 收集所有局部等价类的"小组名"，每批 100 个发 LLM
- 第一次（正向）："哪些小组名语义等价？"
- 第二次（反向自检）："这些归类有没有错误？"
- 跨批通过 source_ids 交集用并查集连接

#### 2.4 回原文最终裁决
- 对每个全局等价类，类内全部原文给 LLM
- 第一次（正向）："这些是否真的全部等价？不等价的拆出来"
- 第二次（反向自检）："拆开的是否合理？留在组内的是否真的等价？"
- 拆出的单独成类，`is_uncertain = True`
- 未确认的默认拆开（保守原则）
- 拆出的条目：不再参与等价合并，但**可以**参与互补合并和同构归组

### Phase 3: 合并

串行优先级固定：**等价合并 → 互补合并 → 同构归组**。

#### 3.1 互补合并识别
- 规则引擎扫描：同一主体下的 `(动作词, 失败处理词)` 配对
- 候选对发给 LLM 确认 + 反向自检
- 确认的互补对：`merge_stage = COMPLEMENT`
- 等价类作为整体参与（类内任意成员匹配即匹配）

#### 3.2 规范化表述 + 程序规则校验
- 等价类（成员 ≥ 2）和互补对 → LLM 生成 canonical text
- 成员为 1 的等价类 → 原样保留，不调 LLM
- 一次 LLM 调用 + 一次反向自检（"有没有弱化/丢失/错误？"）
- 程序规则校验器（纯代码）：
  - 情态检查：原文有"应/shall"，canonical 不能弱化
  - 否定检查：原文有"不得/禁止/拒绝"，canonical 不能丢失
  - 数字检查：原文的数字必须在 canonical 中
  - 条件检查：接口类型/阶段/触发条件/例外条件不能丢失
  - 互补检查：互补对的 canonical 必须包含动作和失败处理
- 校验失败 → canonical 回退类内最全原文，`is_validation_failed = True`

#### 3.3 同构归组 + 程序校验
- LLM 识别：动作相同、约束相同、宾语不同且可枚举的独立条目
- LLM 输出分组 + 枚举表述 + 反向自检
- 程序校验器：
  - 宾语并集完整性：枚举宾语集合 = 组内各条宾语并集
  - 约束一致性：不弱化不强化
  - 封闭性：不含"等""此类"
  - 例外条件保留：无损合并（双向关键词集合覆盖）
  - 抽象不混入："关闭所有调试接口"不与"关闭 JTAG"归组
- 校验失败 → 不归组，保留独立，进存疑

### Phase 4: 输出

#### 4.1 全局一致性校验
- 所有 source_ids 全覆盖 = 原始总数 - 存疑数
- 每个最终需求表述非空
- 不通过报错

#### 4.2 分类编号（可插拔）
- 调用 `config.classify_fn`（当前项目注入 `classify_and_finalize_csrs`）
- 编号前缀从 `config.numbering_prefix_map` 读取

#### 4.3 追溯表
- process_id → 最终 CSR ID 逐条可查
- 同构归组标注对应宾语
- 互补合并标注动作/失败处理部分

#### 4.4 存疑清单
- 自检不一致的条目
- 规则校验失败的条目
- 低置信度的条目
- 分类：阶段 + 原因 + 原始 id + 原文

#### 4.5 统计报告
- 原始记录数、子条目数
- 哈希去重消除数、近精确推迟数
- 等价合并率、互补合并率、同构归组率
- 存疑率（> 15% 告警）
- 合并类型分布

---

## 四、LLM 代理（`llm_proxy.py`）

- `judge()` — 单次判定调用
- `judge_with_self_check()` — 判定 + 反向验证自检（强制 check_prompt 为反向视角）
- `classify()` — 批量分类
- `generate()` — 文本生成
- 内置：重试 3 次、温度 0、JSONL 日志（含 input_texts 摘要 200 字符、prompt_hash）

---

## 五、可配置层（`config.py`）

```python
@dataclass
class PipelineConfig:
    hash_normalize_rules: dict
    fuzzy_dedup_threshold: float = 0.95
    boundary_patterns: dict[str, list[str]]
    fallback_to_llm_boundary: bool = True
    topic_labels: list[str]
    chunk_size: int = 25
    chunk_overlap: int = 12
    global_group_batch_size: int = 100
    action_verb_dict: list[str]
    complement_patterns: list[tuple]
    synonym_table: dict
    classify_fn: Callable | None = None
    numbering_prefix_map: dict | None = None
    max_uncertainty_rate: float = 0.15
```

---

## 六、与现有代码的集成

```
get_or_synthesize_global_csrs()
  ├── 有缓存 → 直接返回
  └── 无缓存 →
        hierarchical_synthesize_project_csrs()
          ├── [feature flag: use_v2_pipeline]
          │     ├── True  → run_pipeline_v2(ctx, config)
          │     └── False → run_hierarchical_dedup() (当前)
          └── classify_and_finalize_csrs() (V2 可选复用)
```

---

## 七、非功能需求

- 温度固定 0，prompt 版本固定，分块参数配置化 → 可复现
- 全链路 JSONL 日志 → 可审计
- 阶段失败降级 → 不断管道，最坏情况原样输出
- 双轨输出 → 兼容旧格式 + 新字段