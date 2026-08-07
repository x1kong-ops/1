# Guolaoxing Research Knowledge Base

这是 U.S. Stock 项目的 **果老星外部、版本化研究记忆库**。后续相关分析应先通过已连接的 GitHub 数据源读取本仓库，再结合实时市场数据和一手资料进行判断。

> 本仓库不是模型参数中的永久记忆。它通过检索式读取提供长期、可审计、可更新的项目上下文。

## 当前核心资料

首次自动化运行会从经过 SHA-256 校验的引导包中物化：

- `articles.jsonl`：77篇市场相关文章的结构化摘要；
- `market_claims.jsonl`：133条日期窗口、方向、目标与历史判断；
- `figures.jsonl`：79条图表语境与视觉复核状态；
- `site_index.jsonl`：122条第一至第三阶段种子索引；
- `phase3_profiles.jsonl`：20位投资相关公开人物的隔离式专题记录；
- `phase3_queue.jsonl`：45条专题候选与排除队列；
- `PHASE2_STATUS.json`、`PHASE3_STATUS.json`、`FINAL_VALIDATION.json`：覆盖范围和验证状态。

## 自动更新

`.github/workflows/refresh.yml` 在多伦多时间每天 08:17 运行：

- 首次运行和每周日执行完整公开元数据扫描；
- 其他日期执行最近72小时的增量更新；
- 仅保存公开文章的标题、日期、分类、文章ID和网址；
- 读取并遵守 `robots.txt`；失败时停止抓取并写入 `refresh_status.json`；
- 全站索引拆分至 `memory/site_index_shards/`，便于 GitHub 检索；
- 自动提交更新，保留完整 Git 历史。

自动化不保存文章正文、原始网页 HTML、图片文件、评论者信息、账户信息或订单信息。

## 使用入口

- `PROJECT_MEMORY.md`：检索顺序、证据边界与后续分析流程；
- `PROJECT_MEMORY_STATUS.md` / `.json`：最近一次物化与刷新状态；
- `AGENTS.md`：研究助手使用本仓库时的约束；
- `memory/site_index_recent.jsonl`：最近发布或修改的文章；
- `memory/site_index_catalog.json`：全量分片清单和计数。

## 研究边界

网站观点是来源观点，不是已验证事实或交易指令。命理、占星和周期材料仅作非科学的来源语境，不能单独作为买卖依据。价格、财报、公司指引、监管文件和宏观数据必须另行读取最新一手来源。
