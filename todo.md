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
| M3-002 | [>] | OpenClaw adapter YAML | roadmap:2026-04-21 | - | 需要当前 OpenClaw 官方路径依据和 fixture |
| M3-003 | [>] | OpenCode adapter YAML | roadmap:2026-04-21 | - | 需要当前 OpenCode 官方路径依据和 fixture |
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
| METRICS-001 | [x] | 扩展 distiller tuning metrics | design:mvp§9.7 | - | `clusters_attempted`/`parse_failure_rate`、`citations_emitted`+`citations_resolved`+`citation_resolution_rate`、`fallback_selected`/`fallback_accepted`、`category_distribution`、`evidence_rule_rejections` 在单次蒸馏 pass 内零额度统计并落 `metrics.json`；rate 同存分子/分母避免 0/0；`metrics_aggregate.py` 跨 run 汇总并由分子/分母重算全局 rate。回归：pipeline 5 例 + orchestrator 成功/门失败两路 + aggregator 漏斗汇总 + rate 边界 |

---
