# Council v15.5 修改路线图（2026-08-25 整合）

> 来源：①第三次元评审问题清单 `meta-review-2026-08-25.md`（16 条，含主会话实证修正）；
> ②后续六轮讨论共识（判停标准/墙钟/递归架构/selector/解析健壮性等，maintainer 逐条拍板）。
> 本文件是「接下来要修改的内容」的唯一整合清单，实施时逐批勾销。

## 一、已拍板的决策（讨论共识）

- **D1 判停标准**：θ 三档统一 **9.5**；增长枯竭 = 近 **3 轮滑动窗口极差 < δ=0.2**；**删除轮数上限**（maxIter 不再触发 forced）。θ=9.5 实为理论天花板，多数 run 将以 early_stop 收尾（预期内，报告状态语义同步调整）。
- **D2 墙钟**：方案 A 先行——动态预算（基础值 + Σ(子任务×执行者×验证者) 估时，规模自适应），为宿主 1920s 工具超时留 ≥240s 收尾余量；**方案 B 异步化单独立项**（工具立即返回 runId，后台执行，**90s 心跳 + 里程碑事件推送进度**，防误判卡死；fast 档仅完成时一报）。
- **D3（3A）**：verifier 增加 **vendorGroup 厂商分组**，verifier 之间必须厂商互斥（v4-pro/v4-flash 归同一厂商；修复本次元评审「DeepSeek 双 verifier 互评」实锤）。
- **D4（3C）**：synthesize/decompose 角色动态化（角色能力向量经 selector 选，输出结构校验 + 角色绑定日志）；**JSON 解析四层防线**（lenient 统一解析器 / 关键字段降级抠取 / 诊断重试一次 / 异厂商替补补位）+ verdict-raw 日志存全文。验证通过 = pytest 全绿 + 真实 run `parsed:false` 归零。
- **D5（3B）judge 递归架构**（单向数据流）：
  - 金标升级「评卷考场」：每条补 1-2 个劣质答案变体 + 期望分标签；judge 档案 = 标定准确度/区分度/稳定性/rubric 服从；
  - judge 任期制（每周重选）+ 锚点集（holdout 子集）+ 影子 challenger + 换任交接（锚点双打分算 offset、基线按 offset 折算接续、换任后 3 天保守期只告警不拦截）；
  - **四锚**：A1 机器判分锚（代码/数学题标准答案由解释器验证，占比保底）｜A2 不可变 holdout（递归流程只读）｜A3 **人工锚主动推送**（每周推 5 题到会话，二元判断「通过/有疑问」，不打分、有疑问自动转第三家仲裁）｜A4 时间尺度分层（能力档案最快 → 考卷日级 → 金标周级且跨任期观察）。
- **D6 出题计划区分度目标**：维度模型间方差小（拉不开差距）→ 加题；题区分度 |r|<0.2 退役（已有）；题数目标 = CI 达标 + 区分度缺口加权。
- **D7 selector 分层**：厂商出线（每厂商 ≤1-2 条目）→ 角色向量匹配 → 排序（能力 rank × 角色胜任分 − λ成本 − μ延迟 + 多样性 + UCB 不确定度加权）→ 厂商配额轮换。**新模型必先跑 benchmark 得初分**（maintainer 工作流），初分 + 置信区间 + 厂商内 challenger 挑战在位者；新旧考卷版本可比性（成绩绑 caseHash + z 对齐）。
- **D8 人工锚**：主动推送，不让 maintainer 自记（同 D5-A3）。
- **D9 异步化进度汇报**：里程碑事件（启动/每轮结束/收敛/完成）+ 90s 心跳兜底（同 D2-B）。

## 二、分批实施计划

### Phase 0：参数/口径小修 + 测量协议对齐（maintainer 2026-08-25 扩充）

**0-A 参数/口径小修（原 Phase 0，低风险）**
| # | 内容 | 对应 |
|---|---|---|
| 0.1 | `terminator.maxRounds=8` 死参数退役（判停统一走 Phase1 新逻辑） | 问题 13 |
| 0.2 | run_council「轮数」显示口径修正（实际收敛轮数，非 round_no） | 问题 14 |
| 0.3 | `council_status` 面板拆「新增/存量」两列 + 7 日趋势（护栏事件按时间窗口展示） | 问题 1 |
| 0.4 | SLA 面板目标值核对：fast 120s 目标与 v15.4「墙钟统一 1800s」决议对齐（改目标或改注释） | 问题 6 |

**0-B 测量协议对齐 + 插值废除 + max_tokens 还原（新拍板，方案细节见 `tier-alignment-plan.md`）**
| # | 内容 | 对应 |
|---|---|---|
| 0.5 | **插件桥**：index.js 用 `ctx.llm.listModels/resolveModelInfo` 生成 `model-tier-bridge.json`（档位枚举 + wire 拼写 + defaultMaxTokens/capabilityMaxTokens）；M3 wire 拼写待确定点 T1 | 问题 19 |
| 0.6 | **Python 改造**：删 `bench/config.py` 与 `config_loader.py` 全部手填 budget_tokens/max_tokens 映射，读桥文件；DeepSeek wire 改官方 `reasoning_effort`；响应文件落盘 thinking 参数 | 问题 19/20/21 |
| 0.7 | **插值废除**：`build_capabilities.py` 删 interpolate/interp_map；CANDIDATES = 桥文件全档位枚举；DESIGN §3 分档策略段改写 | 问题 17 |
| 0.8 | **旧成绩归档**：responses/scores/scores-summary → `benchmark/archive/2026-08-25-pre-alignment/`；capabilities 快照备份 | 问题 19 连锁 |
| 0.9 | **全档位×全案例新协议重跑**（后台断点续传，4-8h，成本重估 ¥15-25 + M3 跨窗口分批）→ 档案重建 + caps_guard 校验 | 问题 17/18/19 |

**验证**：面板展示与 `guardrail-events.jsonl` 实测一致；桥文件与 `ctx.llm` 元数据逐字段一致（抽查 3 模型）；pytest 全绿（含桥解析/wire 序列化单测）；实测一发 reasoning_effort 直连与主会话同档位 reasoning 量级可比；重跑后档案**无 `interpolated:true` 条目**、revision 递增。

### Phase 0-C：模型池管理（maintainer 2026-08-25 拍板：增=手动、删=自动，依赖 0-B 插件桥）
| # | 内容 | 拍板细节 |
|---|---|---|
| 0-C.1 | `model-pool.json` 池名单（model 粒度，状态：active / retired-by-user / retired-by-host-bridge-removal + 时间戳审计） | — |
| 0-C.2 | host `/api/council/model-pool`：GET（host-bridge 目录 + 池成员 + 状态）、POST 加模型、DELETE 删模型、POST 触发跑分 | 加入 = 全档位按桥文件展开入池，分数显示「未测量」 |
| 0-C.3 | 控制台「模型池管理」区：＋增加模型弹下拉（数据源 = host-bridge 目录，与主会话一致）；每成员「跑分」按钮（**手动触发**，断点续传后台跑 + 完成通知）；删除按钮 | **手动跑分**，入池不自动跑 |
| 0-C.4 | **host-bridge 删模型 → 自动同步删除**：插件启动 + 模型目录变更时比对，池成员不在 host-bridge 目录 → 自动退役（档案成绩保留只读、审计事件），控制台**直接消失不标红** | **自动删除** |
| 0-C.5 | Python 侧：`build_capabilities`/`selector` 以池名单为成员准入第一层；「未测量」成员不参与选择（跑分 + ingest 后才入选择池） | — |

**验证**：增/删/自动同步三个场景端到端演示；池成员与 host-bridge 目录比对正确；未跑分成员不出现在 selector 候选。

### Phase 1：判停标准 + 动态墙钟（maintainer 已拍板，先试跑观察）
> ⚠ **依赖 0-B 完成**：协议对齐 + 档案重建之后才做判停实验（避免在协议未对齐的档案上观察 S_r）。
| # | 内容 | 涉及文件 |
|---|---|---|
| 1.1 | θ 三档 9.5；新增「3 轮窗口极差 < 0.2」枯竭判据；删除 maxIter forced 分支 | `council-params.json`、`terminator.py`、`council_v14.py`、`terminator_test.py` |
| 1.2 | 动态墙钟：预算 = 基础 + Σ 规模估时，上限 = 宿主超时 − 240s 余量 | 同上 |
| 1.3 | 报告状态语义：early_stop 作为常态结局的文案（converged 稀有化） | `SYNTH_PROMPT` |
| 1.4 | 文档记录 v15.5 决议（DESIGN-v14.md 新章节） | `DESIGN-v14.md` |

**验证**：terminator_test 更新后 pytest 全绿；**2-3 个真实任务试跑**，观察 S_r 爬坡曲线、几轮触发枯竭、墙钟动态值是否合理，据此微调 δ/窗口。

### Phase 2：解析四层防线 + 3C 角色动态化（D4）
| # | 内容 |
|---|---|
| 2.1 | 公共 lenient JSON 解析器（fence 剥离 + 裸换行状态机 + 截断修复 + 未转义引号启发式），verdict/decompose 统一使用 |
| 2.2 | 关键字段降级抠取（overallScore/hardGateFailed/reworkList）→ 诊断重试一次 → 异厂商替补补位，全程审计事件 |
| 2.3 | verdict-raw 日志存全文（失败可复诊） |
| 2.4 | synthesize/decompose 动态化：角色能力向量 + 输出结构校验 + 角色绑定日志（decisions.jsonl 记「角色=谁、为什么」） |

**验证**：新增解析器单测（裸换行/未转义引号/fence/截断用例）pytest 全绿；真实 run `parsed:false` 归零，替补记录可见。

### Phase 3：3A 厂商分组 + 子任务覆盖修复
| # | 内容 | 对应 |
|---|---|---|
| 3.1 | vendorGroup 厂商分组：verifier 厂商互斥、exec-verifier 厂商互斥 | 问题 16/7 |
| 3.2 | 分解器子任务数与执行容量匹配（子任务数×双路 ≤ 每轮厂商配额容量，防 no_candidate 结构性轮空）+ 无输出子任务拉低 S_r/记硬门禁 | 问题 2 |
| 3.3 | 被拒样本（self_verify_ban/pool_excluded）隔离查证 + 污染追踪日志 | 问题 9 |

**验证**：pytest + 真实 run 无 no_candidate 轮空；decisions.jsonl 可见厂商互斥生效。

### Phase 4：judge 递归架构（D5/D6/D8，最大批次，先出设计文档再动工）
| # | 内容 | 对应 |
|---|---|---|
| 4.1 | 金标升级评卷考场：劣质答案变体 + 期望分标签；「观察期 ≥3 天」实现或改文档 | 问题 15 |
| 4.2 | judge 档案（评卷准确度/区分度/稳定性）+ 任期制 + 锚点集 + 影子 challenger + 换任交接（offset 折算 + 3 天保守期） | D5 |
| 4.3 | 四锚落地：机器判分锚占比保底、不可变 holdout、**人工锚每周主动推送 5 题抽检**、时间尺度分层 | D5/D8 |
| 4.4 | 出题计划区分度目标（方差缺口加题 + 退役已有） | D6、问题 10 |
| 4.5 | judge 漂移分维度监控；金标池 TTL 衰减；考题生命周期各环节监控 | 问题 11/12 |

**验证**：judge 换任演练（模拟换任：offset 记录、基线接续、保守期行为正确）；pytest 全绿。

### Phase 5：异步化（方案 B）+ 可靠性收尾
| # | 内容 | 对应 |
|---|---|---|
| 5.1 | council run 异步化：工具立即返回 runId，后台执行 + 里程碑事件 + 90s 心跳推送主会话 | D2-B/D9 |
| 5.2 | failed_runs 8 条 / auto_evolve×3 重复失败的处置链路（自动诊断分类 + 修复或降级告警） | 问题 5 |
| 5.3 | 成本 drift 超 ±10% 线 24h 内自动校准估算系数 | 问题 4 |
| 5.4 | 能力档案刷新策略（结合 A4 时间尺度分层定档）；档案健康字段（lastSeenOK/consecutiveFail/采样方差） | 问题 3/8 |

**验证**：异步 run 全程心跳可见；失败记录有处置闭环；对账 drift 回到 ±10% 内。

## 三、覆盖矩阵

元评审 16 条 → 全部映射至 Phase 0-5（见各表「对应」列；问题 4/5/8 在 Phase 5，问题 10-12 在 Phase 4）。讨论共识 D1-D9 → D1=Phase1、D2-A=Phase1、D2-B/D9=Phase5、D3=Phase3、D4=Phase2、D5/D6/D8=Phase4、D7=Phase4（selector 分层与 judge 档案同批）。

## 四、决策待办（实施前需 maintainer 确认）
- [ ] Phase 1 试跑任务的选取（哪些真实任务当试跑样本）
- [ ] Phase 4 设计文档的粒度与审阅方式
- [ ] 实施顺序是否按 Phase 0→5 线性推进（当前建议）
