# DEV-P2 — 深度机制与扩展生态

| 项 | 内容 |
|---|---|
| 阶段目标 | 补齐深度机制（上诉 / 继承 / 幽灵 / 天灾），落地扩展包生态与战报导出，达到开源发布质量 |
| 前置条件 | DEV-P1 验收清单全部通过 |
| 上游文档 | [PRD.md](PRD.md) / [DEV-P0.md](DEV-P0.md) / [DEV-P1.md](DEV-P1.md) |

> **本阶段的主线是「把游戏的性格做出来」**：上诉塑造判例文化，继承制造戏剧钩子，幽灵留住死人，天灾收束战局，扩展包打开社区生态。引擎在 P0/P1 已定型，本阶段原则上不动核心管线，只做编排层与生态层。

---

## 1. 范围

### 范围内

- 上诉系统：冻结、开封、全员投票、判例入账
- 击杀继承：外壳选择、裁判重折算、新账户封存
- 幽灵视角（全知）与解说频道
- 天灾曲线生效（引擎 + 裁判叙述 + UI）
- 扩展包系统完整版：多包加载、房间选包、包校验与友好报错、包编写指南
- 内置包扩充至 3 个
- 战报 HTML 导出（自包含单文件）
- UI 打磨与视觉基调落地
- 开源发布准备：README、演示、仓库清理

### 范围外

- 新的数值公式或核心状态机变更（如需调整，走包参数，不改引擎）
- 观战位（PRD §13 待决 #5，本阶段不做，除非需求方拍板）
- 移动端深度适配
- 国际化（界面仅中文；包内容天然任意语言）

---

## 2. 任务分解

依赖：T1、T2、T3、T4 相互独立可并行；T5 依赖 T1（判例入账复用账本）；T6 依赖 T5；T7 依赖 T1~T6；T8、T9 收尾。

| ID | 任务 | 内容 | 交付物 |
|---|---|---|---|
| P2-T1 | 上诉系统 | 发起（30s 窗口）、回合冻结、封存记录开封、全员投票（60s）、多数决 + 平票维持原判、判例入账、额度扣减 | `server/game/appeals.py` |
| P2-T2 | 击杀继承 | 死亡时向凶手展示死者属性外壳、选择、裁判按 power_ratio 重折算、新账户封存（可改名） | `server/game/inheritance.py` |
| P2-T3 | 幽灵系统 | 死亡后视角升级全知（快照含全部隐形层）、解说频道（`chat.channel=ghost`）、与桌面频道隔离 | engine + 前端 |
| P2-T4 | 天灾 | 按包曲线结算全局 HP 流失、裁判叙述包装、UI 天灾等级表 | `server/game/disaster.py` |
| P2-T5 | 扩展包系统 | 多包注册、房间创建时选包、schema 校验 + 人话报错、热加载 | `server/packs/loader.py` 扩展 |
| P2-T6 | 内置包 ×3 | default-arena 完善 referee_prompt；新增 cyber-wasteland、xiuzhen-brawl（各自曲线与世界观） | `server/packs/builtin/` |
| P2-T7 | 战报导出 | 事件流 → 自包含 HTML（回合时间线、HP 曲线图、账本全文、判例、聊天截选） | `server/reports/` |
| P2-T8 | UI 打磨 | 视觉基调落地、动画反馈（HP/死亡/继承/天灾）、移动端基础可用 | `web/` |
| P2-T9 | 开源发布 | README（组网/启动/FAQ/包编写指南）、演示对局录像、仓库安全清理、发布前全局代码检查 | 发布就绪仓库 |

---

## 3. 关键实现细节

### 3.1 上诉系统（appeals.py）

**状态机**（嵌在 SETTLE 之后、NEXT 之前）：

```
SETTLE 完成 → 广播 adjudication
  ├─ 30s 内无人上诉 → NEXT
  └─ 有玩家 appeal → APPEAL_OPEN
        · 冻结回合计时；广播 appeal_open {appeal_id, initiator, ledger_seq}
        · 涉事玩家封存记录向全体存活玩家开封（广播 unsealed 内容，标注 60s 有效）
        · 60s 投票窗口：alive 玩家 vote {appeal_id, approve}
        · 早退：全员投票完成即结算
        · 结果：多数决；平票维持原判；广播 appeal_result {verdict, tally, precedent}
        · 无论成败，发起者 appeals_left − 1（死亡不返还）
        · 判例入账（type=precedent，content 含原判 + 投票结果）
        · 按裁决结果回滚或维持该次结算（事件溯源使回滚=追加补偿事件，禁止改历史事件）
        → NEXT
```

实现要点：

- **回滚不做「修改历史」**：事件 append-only 红线不破。推翻原判 = 追加 `compensation` 事件（HP 恢复、属性回滚、账本条目置 revoked）。
- 被推翻的裁定在 UI 账本中显示删除线 + 推翻原因。
- 投票时幽灵可看全部但无投票权（活着的人才利益相关）。
- 裁判上下文注入判例（近 5 条 precedent），实现「判例塑造裁判」。

### 3.2 击杀继承（inheritance.py）

```
player_dead {killer} 且 killer 存活
  → 广播死亡演出 → 单播 inheritance_offer 给凶手：
     victim 的全部属性外壳（name + narrative，无数值）
  → 凶手 30s 内选择一个（可放弃）
  → 裁判折算：新 initial = victim.value × power_ratio（0.4，包参数），新 cap 同比例
     叙事由裁判生成（「你只得其形，未得其神」）
  → 凶手可改名（外壳归属转移）→ 封存入凶手账户
  → 账本 type=inheritance（公开：谁继承了什么外壳；数值隐藏）
```

- 超时未选 = 放弃，不补发。
- 继承的新账户占凶手 `max_count` 名额（可因满额而不可用，UI 提示）。

### 3.3 幽灵系统

- 死亡瞬间起，`snapshot` 对该玩家注入 `omniscient: true`：包含所有存活者隐形层数值。
- 解说频道：`chat.channel=ghost`，所有在场者可见；永不进入任何 AI 上下文（裁判与玩家 prompt 均排除）。
- 幽灵 UI：全知标记、存活者属性面板解锁、解说频道高亮。
- 幽灵不可：宣言、回应、投票、上诉。

### 3.4 天灾（disaster.py）

```
SETTLE 后检查：turn_no ≥ start_turn 且 (turn_no − start_turn) % every_n_turns == 0
  → level += 1；damage = base_damage + growth × (level − 1)
  → 存活玩家统一 HP −damage（结算走公式，无裁判参与数值）
  → 裁判仅生成天灾叙述（低优先级，失败用模板）
  → 广播 disaster {level, damage, narrative}；UI 天灾表点亮
  → 死亡检查（天灾击杀 killer=null，不触发继承）
```

### 3.5 扩展包系统完整版

- 包注册表：启动扫描 builtin + 用户目录；房间创建时房主选包，进入对局后锁死。
- 校验错误模板：`包 xxx 的 hp.base 应为正整数，收到 "abc"（字段路径：hp.base）`——逐字段报错，一次全量列出。
- 安全校验沿用：`path.relative` 防穿越、YAML 大小上限 200KB、仅 UTF-8。
- 包编写指南（README 章节）：字段表 + 最小可运行示例 + 三条曲线调参建议（回合长度 ↔ cap_growth；开局死亡率 ↔ hp.regen；收束速度 ↔ disaster 参数）。

### 3.6 战报导出（reports/）

- 房主在结算页点「导出战报」→ 服务端把事件流渲染为**单文件 HTML**（内联 CSS/JS，无外链）。
- 内容结构：封面（包名、日期、玩家、时长）→ 回合时间线（宣言/回应/裁定原文）→ HP 曲线（SVG 折线）→ 判例专区 → 聊天截选（自动筛含投票与结盟关键词 ±2 条上下文）→ 排名。
- 设计目标：一局打得越离谱，战报越值得发到群里——这是项目传播的主载体。

---

## 4. 协议增量（相对 P1）

| 方向 | type | payload |
|---|---|---|
| C→S | `appeal` | `{ledger_seq}` |
| C→S | `vote` | `{appeal_id, approve}` |
| C→S | `inherit_choice` | `{attr_id \| "skip"}` |
| C→S | `rename_inherited` | `{attr_id, new_name}` |
| C→S | `export_report` | `{}`（房主，游戏结束后） |
| S→C | `appeal_open` | `{appeal_id, initiator, ledger_seq, unsealed[], deadline}` |
| S→C | `appeal_result` | `{appeal_id, verdict, tally, precedent_entry}` |
| S→C | `inheritance_offer` | `{victim, attributes[{attr_id, name, narrative}], deadline}`（单播） |
| S→C | `disaster` | `{level, damage, narrative}` |
| S→C | `report_ready` | `{url}`（下载端点） |
| 变更 | `chat` | channel 增加 `ghost` |

事件类型新增：`appeal / vote / precedent / compensation / inheritance / disaster / rename`。

---

## 5. UI 打磨规格（P2-T8）

- 视觉基调按 PRD §13 待决 #2 落地（默认暗色角斗场：深底、竞技场纹样、高对比 HP 色）。
- 必做动效：HP 变化浮动数字、死亡覆盖层、继承选择仪式感、天灾全屏氛围（色温变化）、上诉投票横幅。
- 账本条目增加状态徽章（active/contested/revoked 带删除线）、判例金框。
- 移动端基础可用（布局自适应四区 → 上下堆叠），不追求完美。

---

## 6. 测试计划

| 层 | 内容 |
|---|---|
| 单元 | 上诉状态机（含平票/超时/幽灵无票/额度扣减/回滚补偿事件）；继承折算比例与满额拒绝；天灾触发轮次与数值；包校验逐字段报错 |
| 集成 | 上诉全流程（mock 裁判 + 3 假客户端投票）；被推翻裁定的回滚正确性（HP/属性/账本一致性断言）；幽灵快照含全知数据且不进 AI 上下文断言；战报 HTML 生成与离线打开 |
| 回归 | P0/P1 全部验收项复跑（headless 自动局扩至覆盖天灾致死、上诉触发路径） |
| 实测 | 3 人真机完整对局包含：至少 1 次上诉、1 次击杀继承、天灾启动、战报导出 |

---

## 7. 验收清单（出口条件）

- [ ] 上诉全流程可用：30s 窗口发起 → 冻结 → 封存开封（仅投票期）→ 投票 → 多数决/平票维持 → 判例入账 → 额度扣减
- [ ] 被推翻的裁定正确回滚（HP 恢复、账本 revoked、历史事件未被篡改——事件日志 append-only 断言）
- [ ] 击杀继承全流程：外壳选择 → 裁判折算（比例正确）→ 改名 → 封存；满额与放弃路径正常
- [ ] 幽灵视角全知生效且数值不出现在 AI 上下文；解说频道与桌面频道隔离
- [ ] 天灾按包曲线触发、数值正确、被裁判叙述包装、可致人死亡（killer=null 不触发继承）
- [ ] 3 个内置包均可开局且曲线生效（同名参数在三包中取值不同，日志可证）；非法包给出逐字段人话报错
- [ ] 战报 HTML 自包含、离线可开、内容完整（时间线/HP 曲线/判例/聊天截选/排名）
- [ ] README 完整：Tailscale 组网、房主/玩家双视角启动、代理配置、混合内容 FAQ、包编写指南
- [ ] 仓库清理：无密钥、无调试残留、无隐私数据（`git log` 复查 + 全库扫描）
- [ ] 演示对局录像产出（一局完整「有上诉有继承有天灾」的样板局）
- [ ] 全局代码检查通过（P0/P1 同标准）：无冗余、无重复定义、无死代码、无弃用残留、无死锁与竞态
- [ ] 需求方终审确认，可发布

---

## 8. 风险与规避

| 风险 | 规避 |
|---|---|
| 上诉回滚破坏事件溯源一致性 | 回滚=补偿事件，禁改历史；专项集成测试断言 append-only |
| 投票窗口全员挂机卡死 | 投票 60s 超时未投视为弃权，按已投票数多数决（不足半数维持原判） |
| 继承数值失衡（外壳强数值跟随放大） | 折算比例走包参数；裁判折算只准降不准升 |
| 三包内容工作量失控 | 每包只做差异化必需项（曲线+referee_prompt+程度词表），其余字段共享 default |
| UI 打磨吞噬工期 | 打磨任务列明「必做/可弃」两档，可弃项可顺延到发布后迭代 |
| 战报体积过大 | 单文件上限软目标 2MB；HP 曲线 SVG 降采样；聊天截选限量 |
