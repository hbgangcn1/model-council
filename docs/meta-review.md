# Council 元评审问题清单（2026-08-25）

> 第三次元评审（run: `runs/2026-08-25_03-48-54`，standard 档，3 轮收敛，S_r=[7.5,8.5]，置信度 0.72）。
> 报告原文见该 run 目录 `report.md`。本清单已由主会话（DeepSeek）实证核验并修正报告两处推断。

## 实证核验结论（主会话 2026-08-25 凌晨）

- `guardrail-events.jsonl` 共 685 行：
  - `identity_unknown` 306 条：全部在 2026-08-24 13:25–19:14，19:14 后零新增 → v15.3「ox-alpha 接入后归零」成立，**不是计数双写 bug**。
  - `self_verify_ban` 255 条：同样全部止于 19:14（存量群发）。
  - `pool_excluded` 124 条：持续新增到 08-25 03:50（每轮选择记 1 条汇总事件，属正常机制行为）。
- 候选池实为 3 家异厂商（DeepSeek 系 / MiniMax / openrouter-ox-alpha）→ 验证者 k=clamp(3-1,1,3)=2，双验证者成立。报告 P0-2 的「双厂商退化 k=1」触发条件不成立，降级为加固建议。

## 问题清单（按优先级）

### P0
1. **状态面板口径误导**：`council_status` 报「24h 触发 681 次」，把窗口内的存量群发当持续触发。修复：面板拆「新增/存量」两列 + 7 日趋势；可选加「护栏指标 7 日无变化自检」。
2. **子任务覆盖缺失**：本次 6 个评审维度只派了 2 个子任务，「可靠性与可观测性」「自进化闭环风险」两维度无输出。这是元评审暴露的系统缺陷：子任务分解器对多维任务的覆盖无保证。需要：分解器加维度覆盖校验 / 补专项评审。

### P1
3. **能力档案陈旧**：revision 6，最后更新 2026-08-24 19:14，约 24h 未更新。考虑强制刷新周期 ≤6h 或按 run 频率刷新。
4. **成本对账 drift -10.61% 超 ±10% 线**（est 高估实际），无自动校准动作。考虑超线 24h 内自动校准估算系数。
5. **可靠性盲区**：failed_runs.log 8 条失败记录；auto_evolve 7 日 ≥3 次重复失败；无处置链路。
6. **SLA 超标与指标一致性**：fast wallP95 279s vs 目标 120s（超标 132%）；standard 1228.6s vs 1200s。需核对 120s 目标值来源——v15.4 已把墙钟预算统一 1800s，面板目标值是否已同步更新需确认。
7. **验证者异厂商硬约束**：k 公式只约束数量，未硬约束「验证者与执行者必须异厂商」。加 `minDifferentVendors≥2`，不足时拉外部裁判兜底。
8. **档案健康字段**：缺 `lastSeenOK` / `consecutiveFail` / 采样方差；失败 ≥2 轮自动降权；余额低阈值降权。
9. **被拒样本隔离**：`self_verify_ban` / `pool_excluded` 样本是否被排除出后续评审/金标/考卷池，需查证并建立污染追踪日志。

### P2
10. √n 题量下限（建议 `max(√n,10)`）；rank 分段线性映射 + p1/p99 钳制 + 固化映射版本。
11. judge 漂移按维度拆分监控（总分正常可能掩盖分维度漂移）。
12. 金标池 TTL 强制衰减；考题生命周期各环节（通过率/拒绝原因/滞留时长）监控。

## 追加发现（2026-08-25 回答 maintainer 三问时查证所得）

13. **`terminator.maxRounds: 8` 是死参数**：`council_v14.py` L654-658 调用 `terminator.decide(..., max_rounds=params["maxIter"])` 显式传档位 maxIter（3/5/6），terminator 的全局防呆 8 只在 max_rounds=None 时生效，实际永不生效。轮数上限实际 = 档位 maxIter。需决定：修文档注释 or 恢复 8 为兜底（如 `min(maxIter, 8)` 语义确认）。
14. **run_council 返回「轮数」口径偏差**：result.json 的 `rounds` = `st.round_no`（跑完 2 轮后 advance 到 3），导致本次工具显示「轮数：3」而实际 2 轮收敛。应改为 `len(s_history)` 或标注「轮数=round_no-1」。

15. **「观察期 ≥3 天」未实现**：`golden_evolve.py` docstring（L4/L11）声称晋升考卷池需观察期 ≥3 天，但 `promote_healthy()` 代码只查「有 expected 字段 + 不在考卷中」，无任何日期检查。grep 全库确认「观察期」只出现在注释里。需决定：实现日期检查 or 改文档。
16. **verifier「异厂商」约束实际是「异 baseModel 条目」**（实锤）：`availBases` 把 `deepseek-v4-pro` 与 `deepseek-v4-flash` 当两个独立 baseModel，本次元评审 run（2026-08-25_03-48-54）s1 的两个 verifier = `deepseek-v4-pro__off` + `deepseek-v4-flash__off`——同一厂商 DeepSeek 互评，交叉验证独立性打折。需加厂商分组（vendorGroup），verifier 之间也要厂商互斥。

## 追加发现（2026-08-25 下午：benchmark 测量协议研究，maintainer 拍板）

17. **benchmark 档位插值不可信（拍板废除）**：16 档位中 4 档（v4-pro__low、v4-flash__low、M3__minimal、M3__low）为线性插值（off↔high 中点、off↔medium 1/3·2/3 分点），隐含「thinking↑能力↑单调」假设，maintainer 实测不成比例。改为**全档位×全案例**。成本实测账：DeepSeek 6 档×28 题已跑 ≈¥9.94（372 个响应文件汇总）；补 4 档增量 ≈¥2-3；新模型全档 ≈¥7-8；M3 走 5h 额度（当前 65%，全量重跑需跨窗口分批）。时间：补 4 档 1-2h，全量 4-8h（断点续传已有）。
18. **max_tokens 自定义限制截断真实输出（拍板还原模型上限）**：bench 侧自定义 32768（思考档）/8192；orchestrator 侧 16384/8192。实测铁证：flash__high C1 单题 reasoning 27529 token，32768 上限随时撞墙（runner 已有 finish_reason=length 重试记录）。API 实测上限：deepseek 393216（384K）接受；M3 524288（512K）接受、1048576 拒绝；ox-alpha 131072（OpenRouter 官方 /models）。方案：模型输出上限表，bench/orchestrator 共用，删自定义值。
19. **council 档位协议与 host-bridge 主会话脱节（P0 正确性缺陷，拍板对齐）**：council 给 DeepSeek 发手动 `budget_tokens`（512/4096/16384；bench 与 orchestrator 两处还不一致），host-bridge 主会话（host-bridge-llm-deepseek 适配器）发官方 `reasoning_effort`（off/low/high/max，off→thinking disabled）——**测的档位≠用的档位，能力档案测量有效性受损**。pi-ai 系（M3/ox）走 thinkingLevelMap（settings.yaml `reasoningEfforts` 声明界面档位→wire 拼写）。maxTokens 同样：host-bridge 默认 256000（deepseek 适配器）/131072（ox，settings.yaml 已配）/catalog 值（M3）。方案：插件桥（ctx.llm.listModels/resolveModelInfo → model-tier-bridge.json）单一数据源，Python 侧删全部手填映射。**连锁：现有 12 档实测成绩作废，与全档位×全案例合并为一次新协议全量重跑（成本重估 ¥15-25）**。方案细节见 `tier-alignment-plan.md`。
20. **budget_tokens 历史数据疑点**：flash__high C1 reasoning 27529 vs 当时 budget 应 ≤4096，响应文件未记录 thinking 参数无法溯源。修法：响应文件落盘 thinking 参数（一行改动）。
21. **orchestrator max 档「想满挤没答案」坑**：DeepSeek max 档 budget=None（无限思考）+ max_tokens 16384 → 可能思考吃满上限、content 为空。随问题 19 协议对齐（reasoning_effort + 模型上限）自然消失。

22. **模型池管理功能需求（maintainer 2026-08-25 拍板：增=手动、删=自动）**：控制台加「增加模型」下拉（数据源 = host-bridge 模型目录，与主会话模型选择一致），model 粒度入池、全档位展开，分数「未测量」；**手动跑分**（入池不自动跑）；手动删除从池移除（成绩保留只读）；**host-bridge 删模型 → 自动同步删除**（启动/目录变更时比对，自动退役、不标红直接消失）。设计见 DESIGN §14-v15.5-K，实施见 roadmap Phase 0-C（依赖 0-B 插件桥）。

## 决策待办（需 maintainer 拍板）
- [ ] 是否补跑 deep 档专项评审：可靠性与可观测性 + 自进化闭环（两个缺失维度）
- [ ] 面板口径修复是否立即做
- [ ] 能力档案刷新频率 / 成本自动校准 / 验证者硬约束：先出设计文档还是直接改
