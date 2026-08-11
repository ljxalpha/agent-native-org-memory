# TODO

## 文档运作规则

`todo.md` 是当前活跃任务的唯一状态来源。

规则：
- 只保留未完成、进行中、阻塞、延期的任务。
- 已完成或放弃的任务在归档时间点移出本文件。
- 同一个任务的状态只能在本文件更新。

状态：
- `[ ]` 未开始
- `[~]` 进行中
- `[!]` 阻塞
- `[>]` 延期
- `[x]` 完成
- `[-]` 放弃

字段：
- `来源`：任务产生原因，例如 `roadmap:2026-04-21`、`design:mvp§8.9`。
- `详情`：可选详情位置；简单任务填 `-`。
- `验收`：关闭任务所需的最短可验证证据。

归档时间点：
- milestone 收尾时归档已完成/放弃任务。
- 或者本文件 active 区超过约 300 行时做一次整理。

## 当前任务

| ID | 状态 | 摘要 | 来源 | 详情 | 验收 |
|---|---:|---|---|---|---|
| M2-001 | [x] | 完善脱敏规则全集 | roadmap:2026-04-21 | - | 规则覆盖目标类型并有回归样例 |
| M2-002 | [x] | external scanner audit | roadmap:2026-04-21 | - | leaf audit + health incident + distiller summary |
| M2-003 | [x] | distiller health 失败分支与 CI 兜底 | roadmap:2026-04-21 | - | 失败 health + CI `if: always()` 兜底 |
| M2-004 | [>] | 共享试点 review gate 或平台写入保护 | roadmap:2026-04-21 | - | release branch 已实现；PR 创建/平台保护验证待真实集成环境 |
| M2-005 | [!] | 3 个叶子 24h 稳定性测试 | roadmap:2026-04-21 | - | 已补 `scripts/stability_smoke.py`；仍需真实 3 叶子 24h 运行日志 + health 可读 |
| M2-006 | [x] | 脱敏误伤/漏检评估工具雏形 | roadmap:2026-04-21 | - | benchmark 语料 + 评估输出 |
| M3-001 | [>] | Hermes adapter YAML | roadmap:2026-04-21 | - | 需要当前 Hermes 官方路径依据和 fixture |
| M3-002 | [x] | OpenClaw adapter YAML | roadmap:2026-04-21 | - | 官方路径依据**已取得**（openclaw 2026.3.2 真实安装只读实测布局）：`workspace/` 为工作根、`AGENTS.md` 为每会话指令文件、原生记忆是受管根父目录层级的 sqlite sidecar。已交付脱敏 fixture（`tests/fixtures/adapters/openclaw/`）+ 4 例读路径回归（只收受管根 / 原生库与敏感兄弟目录双重拒绝 / `..` 与符号链接越界报错 / hint 注入幂等）。**收窄口径：不含真实消费证据，`support_status` 保持 `experimental`**；收窄理由：openclaw 是 gateway 架构，沙箱内隔离启动且不复制 credentials 的可行性未实测 |
| M3-003 | [x] | OpenCode adapter YAML | roadmap:2026-04-21 | - | 官方路径依据**未取得**（本机未安装 opencode，取得依据需新装 CLI，属需人工批准的外部动作）。新口径 = 显式 UNVERIFIED banner（写明路径为约定而非实测）+ 约定根/受管根边界回归 + 元测试防复发（experimental 且无 fixture/校准记录的 adapter 必须带 banner）+ 保持 `experimental`。升级为已校准 adapter 需一次真实安装与消费验证，列为 MVP 外后续项 |
| M3-004 | [>] | Codex adapter YAML | roadmap:2026-04-21 | - | 需要当前 Codex 官方路径依据和 fixture |
| M3-005 | [x] | 冲突 preserve_both 策略 | roadmap:2026-04-21 | - | conflict handler + marker 渲染 |
| M3-006 | [x] | 蒸馏产物 conflicts frontmatter | roadmap:2026-04-21 | - | `conflicts` 字段写入并验证 |
| M3-007 | [x] | Hermes + Claude Code 冲突 E2E | roadmap:2026-04-21 | - | 至少一个产物 `conflicts != []` |
| M3-008 | [x] | `scripts/product_diff.py` 基础文本 diff | roadmap:2026-04-21 | - | 两个 run_id 可读文本 diff；结构化字段 diff 属后续扩展 |
| M3-009 | [x] | Prompt A/B 基础脚本 | roadmap:2026-04-21 | - | 同 cluster 集输出基础产物 diff；结构化质量/成本比较属后续扩展 |
| M4-001 | [x] | Hermes tool LLMProvider bridge | roadmap:2026-04-21 | - | `hermes_tool` provider 可运行 |
| M4-002 | [x] | Hermes 蒸馏 skill | roadmap:2026-04-21 | - | skill 文档 + 工具声明 |
| M4-003 | [>] | CI 内启动 Hermes Agent 蒸馏 | roadmap:2026-04-21 | - | 需要有 Hermes runner 的 CI 环境真实执行 |
| M4-004 | [>] | Hermes 路径与 Anthropic/mock 路径回归等价 | roadmap:2026-04-21 | - | 需要真实 Hermes provider 路径与 mock/Anthropic 产物等价证据 |
| M5-001 | [x] | `scripts/metrics_aggregate.py` | roadmap:2026-04-21 | - | 按 run_id 聚合 tokens/cost/duration |
| M5-002 | [x] | Golden cluster 框架 | roadmap:2026-04-21 | - | fixtures + `distiller run --eval` |
| M5-003 | [x] | Prompt A/B 基础可视化 | roadmap:2026-04-21 | - | 基于 M3 基础 diff 输出可读视图；结构化 fidelity 属后续扩展 |
| M5-004 | [x] | `docs/tuning_loop.md` | roadmap:2026-04-21 | - | 录制、改 prompt、replay 对比流程 |
| M5-005 | [x] | `docs/runbook.md` | roadmap:2026-04-21 | - | 故障处置 runbook 落地 |
| M5-006 | [x] | 跨平台本机文件系统检测增强 | hardening | - | Linux/Darwin 已知非本机 FS 拒绝启动，未知 FS 策略明确并有测试 |
| HEALTH-001 | [ ] | 扩展 health 运维身份字段 | design:mvp§8.7 | - | `adapter`/`install_id`/`code_version`/`host`/`build_id`/`config_source_master_sha`/`started_at` 有真实来源并纳入校验 |
| INSTALL-001 | [ ] | 扩展 install_leaf 生产级安全探针 | design:mvp§6.6 | - | 平台/依赖探针、`build_info`/approved build ID、失败阶段状态、manifest 兼容、schema probe、dirty recovery、generation quarantine、安装后 status 验证都有回归 |
| DISTILL-001 | [ ] | 扩展 distiller 调度与续跑合同 | design:mvp§7.2 | - | 二次切分、singleton/noise fallback、soft/per-cluster budget、embedding/prompt cache、`unclustered.json`、`--resume`、成本调度和 metrics 一致性有回归 |
| RECORD-001 | [x] | 扩展 record/replay run-scoped 保真合同 | design:mvp§7.2 | - | `--record-dir` 产出外部 run bundle（`calls.jsonl` + `requests/<NNNN>-<hash>.json` 逐调用 JSON + `cluster_assignment.json` + `snapshot.json` 副本 + `record_metrics.json`），永不入 master 白名单；`--replay` 接受 bundle 目录；`metrics.json` 增 `replay_mode`/`replay_hits`/`replay_misses`/`replay_live_calls`（live_calls=总调用−hits）；`distiller-ci` artifact 收 `**/record_runs/**`。回归：recording per-request+bundle replay、pipeline replay 计数、orchestrator bundle+replay metrics、CLI `_replay_mode`+端到端 `run --record-dir` |
| PROV-001 | [x] | LLM 计量溯源全链路（provenance） | design:mvp§5.3.1/§9.2/§9.5 | - | `LLMResponse` 带 `usage_source`（`reported`/`estimated`/`unknown`）/`provider_backend`/`raw_meta` 三个独立字段（`usage` 仍只接受整数）；record 逐调用序列化、replay 原样回放不改写为 `replay`、缺字段的旧 bundle 回落 `unknown`；`provenance.aggregate()` 产出 run 级 `provider_backends`（去重排序，无有损标量）/`usage_source`（含 `none`/`mixed`）/`usage_source_counts`；pipeline 收 primary+fallback 两条 lane；`metrics.json` 与 bundle `record_metrics.json` 两处取值相等。回归：契约 11 例 + record/replay 9 例 + 聚合 12 例 + 双 sink 一致性 7 例 |
| PROV-002 | [x] | provenance 契约收口 | design:mvp§5.3.1/§9.5 | - | `provider_backend` 改为类型校验（非 `str` 抛 `TypeError`，空串仍 `ValueError`），杜绝非字符串标签录进 bundle 后在 replay 侧炸掉；蒸馏成功后在写产物/发布阶段失败的 run，失败 `metrics.json` 写该 run 真实 provenance 而非零调用视图，与已落盘的 `record_metrics.json` 逐字段相等。回归：非字符串 backend 构造/replay 4 例 + `prompt_hash` 口径（prompt-only 命中 vs strict 拒绝）+ 发布失败后双 sink 一致性集成用例 |
| PROV-003 | [x] | 真实 provider 的 provenance 落地 | design:mvp§5.3.1 | - | `AnthropicSDKProvider` 申报 `provider_backend="anthropic_sdk"`，仅在服务端确实返回 usage 对象时标 `reported`（补 0 占位标 `unknown`），`raw_meta` 收 `stop_reason` 与服务端回报 model；SDK client 可注入以便零调用测试。`HermesToolProvider` 与桥接脚本双向传递三字段，stdout 契约扩展 3 个可选键，旧桥接缺键/畸形值回落 `unknown`/`hermes_tool`/`{}` 且不乐观回落成 `reported`。`raw_meta` 非 mapping 抛契约自身的 `TypeError`。`_read_state_time` 要求 aware 时间，naive/损坏值按"无已知时间"处理，避免 daemon tick 抛 `TypeError`。回归：anthropic 契约 5 例 + hermes 回落 7 例 + 桥接跨进程 provenance + 时间戳 6 例 + dry-run 失败策略 |
| LINT-001 | [x] | 公开仓 CI lint 归零并锁定工具版本上界 | hardening | - | dev 工具依赖加版本上界（上游发新版不再单方面把 CI 打红）；42 条存量 ruff 告警全部等价改写清零（`capture_output`、`collections.abc`、集合推导、合并嵌套 if、去冗余 `int(round())`、原生 `fromisoformat`、闭包变量默认值绑定、静态 join 改隐式拼接）；`TRY004` 具名豁免并在 `pyproject.toml` 写明理由（其守卫的 `ValueError`/`RuntimeError` 类型被 per-leaf/per-record 隔离处理器捕获，改 `TypeError` 会改变运行时行为）；black 重排后脱敏样例的拆分字面量保持拆开并加注释锁定；`distill.yml` 的 `mypy .`（永远无法解析）对齐为 `mypy memory_net`。验收：`ruff check .` / `black --check .` / `isort --check-only .` / `mypy memory_net` 全部零告警 |
| METRICS-001 | [x] | 扩展 distiller tuning metrics | design:mvp§9.7 | - | `clusters_attempted`/`parse_failure_rate`、`citations_emitted`+`citations_resolved`+`citation_resolution_rate`、`fallback_selected`/`fallback_accepted`、`category_distribution`、`evidence_rule_rejections` 在单次蒸馏 pass 内零额度统计并落 `metrics.json`；rate 同存分子/分母避免 0/0；`metrics_aggregate.py` 跨 run 汇总并由分子/分母重算全局 rate。回归：pipeline 5 例 + orchestrator 成功/门失败两路 + aggregator 漏斗汇总 + rate 边界 |
| PROD-001 | [x] | 生产 provider 与 run 级共享预算 | design:mvp§5.3.2/§5.3.3/§5.5 | - | `HermesToolProvider` 与参考桥接显式降级为 experimental 集成入口（不进生产 profile、不作等价性基准、桥接后端保持确定性 mock），并补齐跨进程最低契约：单次调用超时（默认 300s）、超时后按进程组 `SIGTERM`→有界等待→无条件 `SIGKILL`（杜绝孙进程继续烧额度），stdout 非 JSON / 顶层非对象一律抛 `RuntimeError` 且带有界 stdout 前缀 + stderr 尾部。新增 `runctx.py`：`RunContext`（frozen，唯一注入载体）+ `RunContextBuilder`（一次装配一次冻结）+ `RunIdentity`（run 级时钟与 run_id 的唯一来源）+ `RunBudget`（run 级共享，持定价与 usd/attempts/wall-clock 三维上界，调用前按上界 `reserve()`，三终态 `settle`/`settle_unknown`/`release` 各恰好 +1 次 attempt，重复终结抛错，`assert_all_settled()` 由 orchestrator 在 `finally` 显式调用）。pipeline/orchestrator 各只加一个 `context` 参数，`None` 保持原行为；run 级时钟与 run_id 收敛到 `RunIdentity`，AST 负向测试禁止 distiller 下任何模块自读 `datetime.now`/自造 run id（`uuid4` 具名白名单）/读 `AOMT_FROZEN*` 开关。新增 `OpenAICompatProvider`（生产后端，`provider_backend="deepseek"`）：system/user 双 message 保 role 边界、`response_format={"type":"json_object"}`、`max_tokens` 直传、服务端 usage → `usage_source="reported"`（缺 usage 才估算标 `estimated`），`finish_reason=="length"` 先结算再抛错；重试消费注入预算，5xx/429/超时保守计费不回补、其它 4xx 回补，凭据只读进程环境、绝不读任何凭据文件。`cli.py` 作为唯一装配点构造并冻结 `RunContext`，primary/fallback 共用同一个 `RunBudget` 实例，`--provider`/`--fallback-provider` 增 `deepseek`；profile 未配 `llm.budget_usd` 则不构造 budget（行为与既有一致）。`openai` 进 `llm` extra，不扩硬依赖。回归：hermes 加固 3 例 + runctx 22 例 + 注入与 AST 负向 10 例 + deepseek provider 17 例（全部假 client，零真实调用） + CLI 装配 9 例

---
