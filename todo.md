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
| M3-002 | [x] | OpenClaw adapter YAML | roadmap:2026-04-21 | - | 官方路径依据**已取得**（openclaw 2026.3.2 真实安装只读实测布局）：`workspace/` 为工作根、`AGENTS.md` 为每会话指令文件、原生记忆是受管根父目录层级的 sqlite sidecar。已交付脱敏 fixture（`tests/fixtures/adapters/openclaw/`）+ 读路径回归（只收受管根 / 敏感目录 exclude 在顶层与嵌套两处生效 / 原生库与 `.env` 按实际生效层次拒绝：位置 + `include` 的 `**/*.md`（`file_pattern` 与隐藏路径跳过是其后的冗余层），`exclude` 里那两条为声明式、运行期不会被执行到 / `..` 与符号链接越界报错 / hint 注入幂等 / fixture 占位文件保持空）。**收窄口径：不含真实消费证据，`support_status` 保持 `experimental`**；收窄理由：openclaw 是 gateway 架构，沙箱内隔离启动且不复制 credentials 的可行性未实测 |
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
| HEALTH-001 | [x] | 扩展 health 运维身份字段 | design:mvp§8.7 | - | **无收窄**。七字段真实来源：`install_id`←`<MEMORY_NET_HOME>/state/install_id`（`install_leaf.sh` 生成并持久化）；`code_version`←`memory_net.__version__`；`build_id`←`local.yaml` 的 `package_ref` 尾部 40 位 commit sha，形态不匹配时回落 `code_version` 并写 `build_id_source` 使降级可审计；`host`←`platform.node()`（`memory_net/leaf/identity.py` 是 `memory_net/` 包内唯一读取点，AST 守卫覆盖别名导入、`socket`/`os.uname`、主机名环境变量等全部等价读法；`scripts/install_leaf.sh` 另有装机期读取，不在包内守卫范围）；`adapter`←adapter YAML 的 `name`；`config_source_master_sha`←`<generation>/config_source_master_sha` marker（`materialize_generation(source_master_sha=…)` 与 `install_leaf.sh` 的 `rev-parse origin/master` 写入，marker 不进内容哈希输入）；`started_at`←调用方注入的进程启动时刻（采集器不自读时钟）。`build_health()` 七个 keyword-only 可选参数，未传即不写该键（默认输出逐字节不变）；`validate_health(value, *, require_ops_identity=False)` 宽松档校验类型+非空、严格档缺一即 `SchemaError`，`HEALTH_SCHEMA_VERSION` 保持 `memory-net/health/v1` 不变；`memory-net-leaf status` 从 `cat` 升级为「读→严格校验→打印」，失败非零退出并打印缺失字段名，`--lenient` 为显式降级开关。回归：`tests/unit/test_health_contract.py`（31 例，含默认输出逐键回归与纯函数断言）、`tests/unit/test_schema_validation.py`（`test_health_schema_version_constant_is_unchanged` + 宽松/严格双档 32 例）、`tests/unit/test_leaf_identity.py`（22 例，含 `build_id` 回落、marker 缺失兼容、`test_the_hostname_is_read_in_exactly_one_module` AST 守卫 + 10 条等价读法的守卫自检）、`tests/unit/test_generation.py`（`test_config_revision_is_independent_of_the_source_master_sha` 等 4 例）、`tests/unit/test_leaf_report_subscribe.py`（report/daemon 接线 6 例，含 scanner-block 与 daemon degraded 两条降级路径带 identity、不注入时逐键回归）、`tests/unit/test_daemon.py`（CLI 与 `status` 例，含 `test_report_command_stamps_operational_identity_on_the_leaf_health_file` 命令级接线断言、`test_config_sync_records_the_snapshot_sha_as_generation_provenance` 同步后 provenance 存活、损坏/缺失 health 无 traceback） |
| INSTALL-001 | [x] | 扩展 install_leaf 生产级安全探针 | design:mvp§6.6 | - | **一处显式降级**（远端 `git+https` 安装路径不进自动化回归，见 `docs/runbook.md`；harness 用仓库本地路径 ref 离线完成，`git+*` 的 40 位 sha 强制 / 浮动 ref 拒绝 / 内嵌凭据拒绝三条以纯参数校验覆盖）。前置：`--no-cron` / `--no-daemon` 逃生阀（默认行为不变）使脚本首次可回归，hermetic harness `tests/integration/install_leaf_local.sh` 用 `mktemp -d` + 本地 bare remote + 本地路径 package ref + 中性 git 身份，CI 阻塞性运行。八项逐条对应回归：① 平台/依赖探针 → `test_preflight_rejects_an_old_python` / `test_preflight_rejects_an_old_git` / `test_preflight_rejects_a_missing_git` / `test_preflight_rejects_a_read_only_home` / `test_preflight_rejects_a_non_local_filesystem` / `test_preflight_rejects_a_full_disk` / `test_preflight_accepts_a_healthy_environment`；② `build_info` / approved build ID → `test_schema_probe_rejects_a_build_id_outside_the_remote_allowlist` / `test_schema_probe_accepts_a_build_id_inside_the_remote_allowlist` / `test_a_remote_without_an_allowlist_admits_every_build` / `test_only_a_commit_pinning_ref_yields_a_build_id` / `test_build_info_records_the_resolved_build_and_its_source` / `test_build_info_reports_a_degraded_build_id_rather_than_hiding_it` / `test_build_info_agrees_with_the_runtime_identity` + harness 的 build_info 断言（`bootstrap_state.json` **不预置** `approved_build_ids`：该键一旦存在即强制白名单，直接加会把存量安装全部 fail-closed，启用流程写在 runbook）；③ 失败阶段状态 → `test_a_failure_records_the_stage_and_the_exit_code` / `test_a_failure_record_carries_nothing_but_the_stage_and_code` / `test_an_unknown_stage_is_refused` / `test_every_stage_round_trips` / `test_the_state_is_replaced_atomically` + harness 的 `FAILED:VENV_READY` 与正则脱敏断言；④ 远端 manifest 兼容 → `test_schema_probe_rejects_an_incompatible_leaf_range` / `test_schema_probe_rejects_a_broken_manifest`；⑤ 代表性 schema probe → `test_schema_probe_accepts_a_healthy_remote` / `test_schema_probe_rejects_a_broken_bootstrap_state` / `test_schema_probe_rejects_an_incompatible_code_range` / `test_schema_probe_rejects_a_broken_product_frontmatter` / `test_cli_prints_the_stage_and_exits_non_zero`；⑥ dirty recovery 留证 → `test_a_previous_failure_is_an_inconsistency` / `test_state_claiming_absent_artifacts_is_an_inconsistency` / `test_a_dangling_config_current_is_an_inconsistency` / `test_recovery_records_hashes_and_never_contents` / `test_recovery_does_not_delete_or_move_anything` / `test_cli_recover_records_evidence_and_still_continues` + harness 的二次安装留证断言（只记清单与哈希、不复制内容、不删不移）；⑦ generation quarantine → 脚本手搓物化改为调用 `materialize_generation()`（原子物化 + 事后哈希复核 + `_corrupted/` 隔离），`test_materialize_generation_quarantines_corrupted_existing_generation` / `test_config_revision_is_independent_of_the_source_master_sha` / `test_markers_under_a_config_source_layout_are_not_hashed` / `test_markers_under_a_flat_source_layout_are_not_hashed` / `test_a_failed_marker_refresh_leaves_the_previous_sha_intact` + harness 的`config-current` 哈希自洽断言；⑧ 安装后 status 验证 → 有界轮询守护健康快照 + 严格档 `status` 通过才写 `DAEMON_STARTED`，失败即 `FAILED:POST_VERIFY` 非零退出，harness 的 daemon-enabled 安装段断言 `DAEMON_STARTED` 与严格档 status 通过 |
| DISTILL-001 | [ ] | 扩展 distiller 调度与续跑合同 | design:mvp§7.2 | - | 二次切分、singleton/noise fallback、soft/per-cluster budget、embedding/prompt cache、`unclustered.json`、`--resume`、成本调度和 metrics 一致性有回归 |
| RECORD-001 | [x] | 扩展 record/replay run-scoped 保真合同 | design:mvp§7.2 | - | `--record-dir` 产出外部 run bundle（`calls.jsonl` + `requests/<NNNN>-<hash>.json` 逐调用 JSON + `cluster_assignment.json` + `snapshot.json` 副本 + `record_metrics.json`），永不入 master 白名单；`--replay` 接受 bundle 目录；`metrics.json` 增 `replay_mode`/`replay_hits`/`replay_misses`/`replay_live_calls`（live_calls=总调用−hits）；`distiller-ci` artifact 收 `**/record_runs/**`。回归：recording per-request+bundle replay、pipeline replay 计数、orchestrator bundle+replay metrics、CLI `_replay_mode`+端到端 `run --record-dir` |
| PROV-001 | [x] | LLM 计量溯源全链路（provenance） | design:mvp§5.3.1/§9.2/§9.5 | - | `LLMResponse` 带 `usage_source`（`reported`/`estimated`/`unknown`）/`provider_backend`/`raw_meta` 三个独立字段（`usage` 仍只接受整数）；record 逐调用序列化、replay 原样回放不改写为 `replay`、缺字段的旧 bundle 回落 `unknown`；`provenance.aggregate()` 产出 run 级 `provider_backends`（去重排序，无有损标量）/`usage_source`（含 `none`/`mixed`）/`usage_source_counts`；pipeline 收 primary+fallback 两条 lane；`metrics.json` 与 bundle `record_metrics.json` 两处取值相等。回归：契约 11 例 + record/replay 9 例 + 聚合 12 例 + 双 sink 一致性 7 例 |
| PROV-002 | [x] | provenance 契约收口 | design:mvp§5.3.1/§9.5 | - | `provider_backend` 改为类型校验（非 `str` 抛 `TypeError`，空串仍 `ValueError`），杜绝非字符串标签录进 bundle 后在 replay 侧炸掉；蒸馏成功后在写产物/发布阶段失败的 run，失败 `metrics.json` 写该 run 真实 provenance 而非零调用视图，与已落盘的 `record_metrics.json` 逐字段相等。回归：非字符串 backend 构造/replay 4 例 + `prompt_hash` 口径（prompt-only 命中 vs strict 拒绝）+ 发布失败后双 sink 一致性集成用例 |
| PROV-003 | [x] | 真实 provider 的 provenance 落地 | design:mvp§5.3.1 | - | `AnthropicSDKProvider` 申报 `provider_backend="anthropic_sdk"`，仅在服务端确实返回 usage 对象时标 `reported`（补 0 占位标 `unknown`），`raw_meta` 收 `stop_reason` 与服务端回报 model；SDK client 可注入以便零调用测试。`HermesToolProvider` 与桥接脚本双向传递三字段，stdout 契约扩展 3 个可选键，旧桥接缺键/畸形值回落 `unknown`/`hermes_tool`/`{}` 且不乐观回落成 `reported`。`raw_meta` 非 mapping 抛契约自身的 `TypeError`。`_read_state_time` 要求 aware 时间，naive/损坏值按"无已知时间"处理，避免 daemon tick 抛 `TypeError`。回归：anthropic 契约 5 例 + hermes 回落 7 例 + 桥接跨进程 provenance + 时间戳 6 例 + dry-run 失败策略 |
| LINT-001 | [x] | 公开仓 CI lint 归零并锁定工具版本上界 | hardening | - | dev 工具依赖加版本上界（上游发新版不再单方面把 CI 打红）；42 条存量 ruff 告警全部等价改写清零（`capture_output`、`collections.abc`、集合推导、合并嵌套 if、去冗余 `int(round())`、原生 `fromisoformat`、闭包变量默认值绑定、静态 join 改隐式拼接）；`TRY004` 具名豁免并在 `pyproject.toml` 写明理由（其守卫的 `ValueError`/`RuntimeError` 类型被 per-leaf/per-record 隔离处理器捕获，改 `TypeError` 会改变运行时行为）；black 重排后脱敏样例的拆分字面量保持拆开并加注释锁定；`distill.yml` 的 `mypy .`（永远无法解析）对齐为 `mypy memory_net`。验收：`ruff check .` / `black --check .` / `isort --check-only .` / `mypy memory_net` 全部零告警 |
| METRICS-001 | [x] | 扩展 distiller tuning metrics | design:mvp§9.7 | - | `clusters_attempted`/`parse_failure_rate`、`citations_emitted`+`citations_resolved`+`citation_resolution_rate`、`fallback_selected`/`fallback_accepted`、`category_distribution`、`evidence_rule_rejections` 在单次蒸馏 pass 内零额度统计并落 `metrics.json`；rate 同存分子/分母避免 0/0；`metrics_aggregate.py` 跨 run 汇总并由分子/分母重算全局 rate。回归：pipeline 5 例 + orchestrator 成功/门失败两路 + aggregator 漏斗汇总 + rate 边界 |
| PROD-001 | [x] | 生产 provider 与 run 级共享预算 | design:mvp§5.3.2/§5.3.3/§5.5 | - | `HermesToolProvider` 与参考桥接显式降级为 experimental 集成入口（不进生产 profile、不作等价性基准、桥接后端保持确定性 mock），并补齐跨进程最低契约：单次调用超时（默认 300s）、超时后按进程组 `SIGTERM`→有界等待→无条件 `SIGKILL`（杜绝孙进程继续烧额度），stdout 非 JSON / 顶层非对象一律抛 `RuntimeError` 且带有界 stdout 前缀 + stderr 尾部。新增 `runctx.py`：`RunContext`（frozen，唯一注入载体）+ `RunContextBuilder`（一次装配一次冻结）+ `RunIdentity`（run 级时钟与 run_id 的唯一来源）+ `RunBudget`（run 级共享，持定价与 usd/attempts/wall-clock 三维上界，调用前按上界 `reserve()`，三终态 `settle`/`settle_unknown`/`release` 各恰好 +1 次 attempt，重复终结抛错，`assert_all_settled()` 由 orchestrator 在 `finally` 显式调用）。pipeline/orchestrator 各只加一个 `context` 参数，`None` 保持原行为；run 级时钟与 run_id 收敛到 `RunIdentity`，AST 负向测试禁止 distiller 下任何模块自读 `datetime.now`/自造 run id（`uuid4` 具名白名单）/读 `AOMT_FROZEN*` 开关。新增 `OpenAICompatProvider`（生产后端，`provider_backend="deepseek"`）：system/user 双 message 保 role 边界、`response_format={"type":"json_object"}`、`max_tokens` 直传、服务端 usage → `usage_source="reported"`（缺 usage 才估算标 `estimated`），`finish_reason=="length"` 先结算再抛错；重试消费注入预算，5xx/429/超时保守计费不回补、其它 4xx 回补，凭据只读进程环境、绝不读任何凭据文件。`cli.py` 作为唯一装配点构造并冻结 `RunContext`，primary/fallback 共用同一个 `RunBudget` 实例，`--provider`/`--fallback-provider` 增 `deepseek`；profile 未配 `llm.budget_usd` 则不构造 budget（行为与既有一致）。`openai` 进 `llm` extra，不扩硬依赖。回归：hermes 加固 3 例 + runctx 22 例 + 注入与 AST 负向 10 例 + deepseek provider 17 例（全部假 client，零真实调用） + CLI 装配 9 例。加固追加：预算闸不再因注入 budget 而关闭（`RunBudget` 只约束会 `reserve()` 的 provider，而默认 profile 是 `mock` + `budget_usd`，关掉旧闸等于让 mock/anthropic/hermes 三条 lane 无上限）；identity 存在时 run_id 以其为唯一来源，冲突抛 `ValueError`；provider 改捕获 `BaseException` 并把响应解码包进保证终结的结构，`Reservation` 增 `is_settled` 保证幂等；重试按 `退避 + timeout` 在 sleep **之前**预扣 wall-clock（只预扣 timeout 会放行最坏情况超限的调用），`monotonic` 与 `sleep` 一起注入使账本可断言；AST 负向测试去掉整模块豁免、增加导入别名解析、扫描逻辑对合成树做正反两向端到端自检

---

---

## 2026-08-11 — 生产化：profile / embedding / 解析加固（S10–S15）

| ID | 状态 | 主题 | 来源 | 负责人 | 备注 |
|---|---|---|---|---|---|
| PROD-002 | [x] | S5–S9 预算结算与 provider 逃逸路径加固 | design:mvp§5.3/§5.5 | - | 加固项：`reported` 的 usage 未完整回报时改走 `settle_unknown`（`settle` 会按估算回补预扣，而估算看不见服务端计费的 input token）；新增 `ProviderCallError` 使逃逸异常只带异常类型+HTTP 状态+尝试次数（orchestrator 会把 `str(exc)` 写进 failure health/metrics，CI 会当 artifact 发布）；新增 `BudgetedProvider` 给 `anthropic`/`hermes_tool` 补上调用前闸门（累加器只对已返回的调用累加，永远慢一次，也看不见重试）；`assert_all_settled` 改为仅在无异常传播时检查（从 `finally` 抛异常会顶替真实成因并改变异常类型）。后续加固：`reported` 校验移进 `LLMResponse.__post_init__`（两个计数须为非负整数，否则降级 `unknown`——负数会把已记账花费减回去，且 anthropic/hermes 都能伪造该声明）；SDK 客户端构造与响应解码两条逃逸路径同样改写为 `ProviderCallError`；`max_attempts`/`max_wall_clock_s` 单独配置时也必须构造 `RunBudget`。以下 4 处经权衡保留现状：`ceil(len/4)` 估算口径（方案写死、且偏差由真实 usage 结算自我修正、敞口有界为一次调用）、两道成本闸并存（有意设计，各管调用前/调用后，都朝保守方向）、`golden_eval` 与桥接脚本的额外装配点（mock-only、零成本、不写 run 产物）、builder-setter 测试非重言式（比对的是两个独立来源）。AST 绊线扩到 15 项禁用 API 并显式放行 `time.monotonic`；其静态黑名单对 `getattr`/`importlib` 动态取值原理上无解，定位为防无心之失，真正判据是 S21 的规范化比较器 |
| PROD-003 | [x] | `--config` profile 选择入口 | design:mvp§5.5.5 | - | 三级优先级 `--config` > `AOMT_DISTILL_PROFILE` > `config/distill.yaml`，默认行为不变；一律走 `git show <sha>:<path>` 从冻结快照读，仓外路径/绝对路径/`..`/空值拒绝启动。显式 profile 在快照中缺失即拒绝启动，默认 profile 缺失仍回落——不对称是有意的：点名要某份配置却静默拿到默认值，会得到一次跑在错误预算与错误模型上的成功 run |
| PROD-004 | [x] | profile schema 校验与溯源 | design:mvp§5.5.6 | - | `profile_class: production` 时九个键必须显式存在（provider/model/双向定价/budget_usd/embedding_backend/embedding_model/scanner schema_version+mode），缺一即拒绝启动；`mock`/`hermes_tool` 与 `test-cheap` 不得进生产，`mode` 非 `enforce` **拒绝而不静默纠正**；叶子侧 `audit`/`warn`/`block` 词汇在任何 profile class 下都拒绝。`ProfileProvenance` 对**原始字节**取 sha256（解析会丢注释、键序与格式）并落 `snapshot.json`；无 profile 文件时不写这三个键，产物逐字节不变 |
| PROD-005 | [x] | `EmbeddingPolicy` 单一判定 + 启动探针 | design:mvp§5.5.7 | - | 后端判定从两处独立 env 自读收敛为一个注入对象；`cluster_vectors()` 的 policy **取自产出这批向量的 embedder**（注入两次仍可能不一致，从生产者读回来在结构上不可能不一致）。`fastembed` 下 import 失败不再静默回落 3 维廉价向量（该组合与按语义距离调的 DBSCAN 参数搭配会产出无意义产物而指标正常），改为抛错。`probe()` 置于「解析出 profile 之后、任何叶子读取与 LLM 调用之前」——它不可能更早，因为后端名写在 profile 里而 profile 来自 fetch 后的冻结快照；`_preflight()` 置于 fetch 之前，只做不依赖 profile 的检查（`--provider deepseek` 的凭据与 `openai` 可 import）。`embedding_test_only` 已随 PROD-007 落进 `metrics.json` |
| PROD-006 | [x] | JSON 解析加固 | design:mvp§5.3.3 | - | `_raw_decode_object` 从"取第一个能解码的 `{}`"改为"扫描全部**同级**对象、取含 `products` 的那个"；多于一个抛 `AmbiguousProductsPayload`（两种读法的产物都能过校验，猜错完全不可见）；零个则报出**候选键名**、绝不回显值（该消息会进 `parse_error_details` → 公开 metrics）。扫描按解码长度跳过已消费区间，只比较同级对象——否则产物内部嵌套的 `products` 键会被当成第二份载荷，把一次完好的响应判成歧义 |
| PROD-007 | [x] | 生效值校验、溯源落 metrics、字节级哈希 | design:mvp§5.5.6/§5.5.7/§5.3.3 | - | 生产校验从「查 profile 字面值」改为「查生效值」：`--provider`/`--fallback-provider` 会覆盖 profile，只查 profile 的话一条 `--provider mock` 就能让 production run 发布假产物；主备两条通道分别判，未知 provider 名不再回落 mock 而是拒绝启动，空串/纯空白视同缺失。`metrics.json` 增 `profile_path`/`profile_blob_sha256`/`embedding_test_only`（缺省空串/`False`，无 profile 的 run 字面不变）——替身向量跑出的产物与真向量产物结构完全一致，事后无从分辨，所以必须写进 metrics 而不只是日志。profile 改走 `show_bytes`/`read_bytes` 读原始字节，解码只发生在喂 YAML 之前：文本通道会把 CRLF 归一成 LF，那样算出的溯源哈希对不上仓库里任何 blob。`MEMORY_NET_USE_FASTEMBED` 收敛为只在 `policy.py` 读一次、embedder 构造时固化，`dbscan.py` 完全不再读；解析扫描改 `raw_decode(text, index)` 原地推进，去掉逐花括号切后缀的 O(n²) |


