# DEV-P0 — 可玩骨架（无真 AI）

| 项 | 内容 |
|---|---|
| 阶段目标 | 房间系统 + 回合引擎 + 确定性裁决 + 假人，在**不接入任何真实模型**的前提下跑通完整对局，验证核心循环 |
| 前置条件 | PRD 已定稿 |
| 上游文档 | [PRD.md](PRD.md) |
| 下游文档 | [DEV-P1.md](DEV-P1.md) |

> **本阶段最重要的产出不是代码，是一个答案：假人互殴好玩吗？** 如果确定性规则下的循环都无聊，接真模型只会更无聊。所以 P0 的裁决走简化公式（见 §5），叙事是化妆品。

---

## 1. 范围

### 范围内

- 房间全生命周期：创建 / 口令加入 / 大厅 / 假人补位 / 踢人 / 开局 / 结束 / TTL 清理
- 回合状态机五阶段：DECLARE → RESPOND → ADJUDICATE → SETTLE → NEXT（含时限、早退、兜底）
- 确定性裁决 stub：数值公式全实现，叙事走模板
- 账本视图 + 事件溯源 + 进程重启恢复
- 简化版属性开户（带 type 字段的表单）
- 假人 AI（脚本模板策略）
- 前端三页：首页 / 大厅 / 牌桌（MVP 视觉，不做打磨）
- `run.bat` 一键启动（含 Tailscale 地址探测）
- headless 自动局 E2E

### 范围外（禁止提前做）

- 任何真实 LLM 调用（玩家 AI / 裁判 AI 全不做）
- yap-agent 本地代理（P1）
- 对冲的语义评估（P0 只按「是否有对冲」给固定削减系数）
- 上诉 / 继承 / 幽灵 / 天灾 / 扩展包选择器 / 战报（P2）
- UI 视觉打磨、主题系统（P2）
- 移动端适配

---

## 2. 任务分解

依赖关系：T1 → T2 → (T3, T4) → T5 → T6 → T7 → T8 → T9 → T10。

| ID | 任务 | 内容 | 交付物 |
|---|---|---|---|
| P0-T1 | 项目脚手架 | 目录结构、requirements、run.bat、预构建前端占位、.gitignore、Git 初始化 | 可启动的空服务 |
| P0-T2 | 通信协议 | 消息 schema（Pydantic）、WS 连接管理、会话令牌、心跳 | `server/net/` |
| P0-T3 | 房间管理 | 创建/加入/argon2 口令/限流/踢人/假人/开局 | `server/rooms.py` |
| P0-T4 | 事件存储 | append-only 事件日志、SQLite 持久化、快照重建、房间 TTL | `server/store/` |
| P0-T5 | 回合引擎 | 五阶段状态机、时限调度、早退、超时兜底、死亡与胜利判定 | `server/game/engine.py` |
| P0-T6 | 裁决 stub + 公式 | cap/hard/floor/defense 纯函数、模板叙事、数值单测 | `server/game/judge.py` + `formulas.py` |
| P0-T7 | 账本 + 包加载器 | 账本视图、default-arena 包解析（YAML） | `server/game/ledger.py` + `server/packs/` |
| P0-T8 | 假人 AI | 模板宣言库、策略、种子化随机 | `server/game/bots.py` |
| P0-T9 | 前端 MVP | 三页 UI、WS 客户端、HP 条、账本面板、阶段化输入区、倒计时 | `web/` |
| P0-T10 | 测试与验收 | 单测、headless 自动局、双浏览器手测脚本、验收清单核对 | 测试报告 |

---

## 3. 通信协议（P0 基线）

所有消息为 JSON：`{ "type": "...", "seq": n, "server_now": "...", ...payload }`。`seq` 为房间事件序号，客户端用于断线追平。

### 3.1 客户端 → 服务端

| type | payload | 说明 |
|---|---|---|
| `hello` | `{name}` | 建立 WS 后第一条；返回会话令牌 |
| `create_room` | `{password}` | 创建并成为房主 |
| `join_room` | `{code, password}` | 加入房间 |
| `leave_room` | `{}` | 主动离开 |
| `add_bot` / `remove_bot` | `{bot_id?}` | 仅房主，大厅阶段 |
| `kick` | `{player_id}` | 仅房主 |
| `start_game` | `{}` | 仅房主，≥2 席位 |
| `declare` | `{content, targets[], intensity_hint, action_type}` | 轮到自己时；P0 须带 `action_type` |
| `respond` | `{kind: "counter"\|"endure"\|"pass", content?}` | 回应窗口 |
| `chat` | `{channel: "table", text}` | 桌面聊天 |
| `open_attribute` | `{name, narrative, attr_type, initial, cap, exchange_rate, consume_rule}` | 本人回合的 DECLARE 阶段或 SETTLE 后 10s 内 |
| `reconnect` | `{token}` | 重连 |
| `heartbeat` | `{}` | 15s 一次 |

### 3.2 服务端 → 客户端

| type | payload | 说明 |
|---|---|---|
| `session` | `{token, player_id}` | hello 回执 |
| `error` | `{code, message, fatal?}` | 统一错误 |
| `room_state` | `{room_id, code, phase, players[], host_id, pack}` | 大厅全量状态 |
| `game_started` | `{snapshot}` | 初始全量 |
| `turn_begin` | `{turn_no, current_player, deadline}` | 含 HP 回复结算 |
| `phase_change` | `{phase, deadline}` | declare/respond/adjudicate/settle |
| `declaration` | `{actor, content, targets, intensity_hint, action_type}` | 宣言公示 |
| `response` | `{actor, kind, content}` | 回应公示（pass 不广播，仅推进） |
| `adjudication` | `{ledger_seq, verdict{accepted, rejected, floor}, effects[{player_id, hp_delta}], narrative, counters_count}` | 裁定结果 |
| `hp_update` | `{player_id, hp, hp_max, delta}` | 单独的 HP 变化动画事件 |
| `attribute_opened` | `{player_id, name, narrative}` | 仅公开外壳（P0 同样不广播数值给他人） |
| `attribute_private` | `{attr_id, name, attr_type, value, cap, exchange_rate, consume_rule}` | 仅单播本人 |
| `player_dead` | `{player_id, cause, killer?}` | P0 无继承，仅演出 |
| `game_over` | `{winner, ranking[]}` | 结算 |
| `snapshot` | `{state, last_seq}` | 重连全量（公开层 + 本人隐形层） |
| `kicked` | `{reason}` | 被踢 |

### 3.3 错误码

`AUTH_FAILED / ROOM_NOT_FOUND / ROOM_FULL / LOCKED（限流）/ NOT_HOST / NOT_YOUR_TURN / WRONG_PHASE / INVALID_PAYLOAD / ALREADY_STARTED`。

---

## 4. 数据模型（SQLite）

```sql
CREATE TABLE rooms (
  id TEXT PRIMARY KEY, code TEXT UNIQUE, password_hash TEXT,
  pack_id TEXT, phase TEXT, host_player_id TEXT,
  created_at TEXT, closed_at TEXT
);
CREATE TABLE players (
  room_id TEXT, player_id TEXT, token_hash TEXT,
  name TEXT, seat INTEGER, kind TEXT,          -- kind: human | bot
  hp INTEGER, hp_max INTEGER, status TEXT,     -- alive | dead
  connected INTEGER, joined_at TEXT,
  PRIMARY KEY (room_id, player_id)
);
CREATE TABLE attributes (
  room_id TEXT, attr_id TEXT, owner_id TEXT,
  name TEXT, narrative TEXT, attr_type TEXT,   -- attack | defense | utility
  initial INTEGER, cap INTEGER, exchange_rate REAL,
  consume_rule TEXT, value INTEGER,
  opened_turn INTEGER, sealed_at TEXT, status TEXT,
  PRIMARY KEY (room_id, attr_id)
);
CREATE TABLE events (
  room_id TEXT, seq INTEGER, turn_no INTEGER,
  type TEXT, actor TEXT, payload_json TEXT, created_at TEXT,
  PRIMARY KEY (room_id, seq)
);
```

- 事件表是唯一事实源；房间/玩家/属性表为投影，可随时从事件重建（重启恢复即用此路径）。
- 事件类型枚举与 §3.2 广播一一对应，外加内部事件 `REGEN / TTL_CLEANUP`。

---

## 5. 关键实现细节

### 5.1 状态机（engine.py）

```
class Phase(str, Enum): DECLARE / RESPOND / ADJUDICATE / SETTLE

循环：
  turn_begin: turn_no += 1；存活者 hp += regen（clamp hp_max）；广播 turn_begin
  DECLARE  : 等当前玩家 declare 或 90s 超时 → 自动防御姿态（action_type="guard"）
  RESPOND  : 广播 declaration；45s 内收 respond；全员提交即早退
  ADJUDICATE: 调用 judge stub（同步、瞬时）；产出裁定对象
  SETTLE   : 原子应用（HP、属性消耗、账本入库）→ 广播 adjudication + hp_update
             → 死亡检查（player_dead / game_over）
  轮转到下一位存活玩家
```

实现要求：

- 时限全部由服务端 `asyncio` 任务驱动，到期即触发兜底；客户端倒计时仅展示。
- **早退**：RESPOND 阶段用 `asyncio.Condition` 或计数器，收齐所有人的 pass/counter/endure 立即推进。
- 提交幂等：同 `seq` 的重复提交丢弃。
- 一切状态变更必须经过 `apply(event)` 单入口，广播与入库共用，保证事件溯源一致。

### 5.2 裁决 stub（judge.py）

P0 裁决为纯函数 `adjudicate(state, declaration, responses) -> Verdict`：

```
1. base = 数字 hint ｜ 程度词比例 × cap(t) ｜ action_type 默认值
2. consistency = 1.0（P0 无 AI，不做语义评估）
3. counter_factor = 1 − 0.15 × 对冲数量（每条对冲固定削减 15%，上限 45%）
4. final = clamp(base × consistency × counter_factor, 0, hard(t))
5. targets 为空或 guard/buff → 不产生伤害效果（见 5.3 动作语义）
6. 多目标：share_i = final / n（均摊）
7. defense_i = Σ 防御类属性 value × exchange_rate（仅 declare 声明后存在生效）
8. floor_i = max(1, round(share_i − defense_i))；damage_i = floor_i
9. 叙事 = 模板渲染（见 verdict_templates.py，按 action_type × 结果档位组织文案）
```

模板裁决与 P1 的降级裁决**共用同一套代码路径**（P1 的 AI 裁判失败时回落到此函数），所以这个 stub 不是临时代码，是常驻兜底，按正式代码质量写。

### 5.3 P0 动作语义（action_type）

P0 无 LLM，动作必须带类型才能被 stub 结算。前端提供类型选择器：

| action_type | 语义 | stub 结算 |
|---|---|---|
| `attack` | 攻击 | 走伤害公式，必须带 targets |
| `guard` | 防御姿态 | 本回合受到的影响力 ×0.7（临时系数，不涉及属性） |
| `buff` | 强化属性 | 指定已有属性 value += 1（不超过 cap） |
| `open` | 开户 | 触发 open_attribute 流程，本回合不结算数值 |
| `flavor` | 纯嘴炮/场地 | 不结算，仅入账本 |

`intensity_hint`：前端提供 程度词四档（微弱/寻常/强力/致命）+ 自定义数字输入，标注「P0：裁决以强度数值为准，文本为风味」。

> UI 上要明确告知玩家这是 P0 简化裁决；P1 起文本语义将由裁判 AI 解析。这条提示写进牌桌页脚注，避免误解留存到 P1。

### 5.4 属性开户（P0 简化版）

- 表单字段：name / narrative / attr_type / initial / cap / exchange_rate / consume_rule。
- 服务端硬校验（读包）：`max_count=3`、`initial ≤ 10`、`cap ≤ 30`、`exchange_rate ≤ 2.0`。
- 开户即封存：`opened_turn` + 时间戳写入事件流，之后仅 `buff` 与 stub 防御消耗可改 `value`，其余字段不可变。
- 数值私密性在 P0 即生效：广播仅外壳，数值走 `attribute_private` 单播——隐形层的数据通路在 P0 就要打通，P1 才不用返工。

### 5.5 假人 AI（bots.py）

- 内置 40+ 条模板宣言（attack/guard/buff/flavor 各若干），带程度词与占位符（目标名、自己属性名）。
- 策略（种子化随机，默认 seed 随房间生成，可复现）：
  - 60%：攻击当前 HP 最低的存活敌人（非自己）
  - 15%：guard（自身 HP < 30% 时提升至 35%）
  - 15%：buff 既有属性 / flavor
  - 10%：若无属性则开户一个预设属性（每假人预置 1 个专属属性，如「铁布衫」defense 6×1.2）
- 假人响应窗口行为：30% 概率对冲（模板反制语）、目标必承受自述（模板）、其余 pass。
- 假人即时决策，不占真实时限（收到事件后 1~2s 内提交，模拟节奏）。

### 5.6 内置包（default-arena）

`server/packs/builtin/default.yaml`（P0 即按完整 schema 解析，P2 直接复用）：

```yaml
id: default-arena
name: 无名角斗场
hp: {base: 40, regen: 5}
intensity:
  cap_base: 8
  cap_growth: 2            # cap(t) = 8 + 2t
  ceiling_factor: 1.5
  level_words: {微弱: 0.3, 寻常: 0.6, 强力: 1.0, 致命: 1.6}
attributes: {max_count: 3, max_initial: 10, max_cap: 30, max_exchange_rate: 2.0}
timers: {declare_s: 90, respond_s: 45, judge_s: 60}
disaster: {start_turn: 15, every_n_turns: 3, base_damage: 2, growth: 1}   # P2 启用
appeal: {count: 2, window_s: 30, vote_s: 60}                              # P2 启用
inheritance: {power_ratio: 0.4}                                           # P2 启用
victory: last_alive
tie_break: [survived_turns, hp_peak]
referee_prompt: ""       # P1 填充
```

加载器：启动时扫描 `server/packs/builtin/` 与 `server/packs/`（后者用于 P2 自定义包），Pydantic 校验；路径拼接必须用 `path.relative` 防穿越。

### 5.7 run.bat（P0 版）

```
1. 检查 python ≥3.11（失败给出下载指引）
2. 项目目录创建 .venv（不装 C 盘全局）
3. pip install -r server/requirements.txt
4. 若 web/dist 不存在 → 提示运行前端构建（开发期产物随仓库走，正式路径不应触发）
5. 启动 uvicorn（0.0.0.0:8000）
6. 探测并打印：http://localhost:8000 ｜ 局域网 IP ｜ tailscale ip -4（若存在）
7. start http://localhost:8000
```

---

## 6. 前端 MVP 规格（web/）

| 页面 | 要素 |
|---|---|
| 首页 | 创建/加入两张卡；昵称输入；错误 toast；加入限流提示 |
| 大厅 | 房号大字+复制；玩家列表（房主星标、bot 标记、连接点）；房主操作（加/删假人、踢人、开局按钮——不足 2 席禁用）；包信息展示 |
| 牌桌 | 按 PRD §4.4 四区布局；HP 条 + 数值动画；阶段化输入区（action_type 选择器 + 程度选择器 + 文本框 + 目标选择 chips）；回应窗口三键；账本面板（tab：全部/宣言/裁定/系统）；回合横幅与倒计时；死亡/胜利覆盖层 |

工程要求：

- WS 客户端封装：自动重连（指数退避）+ `reconnect` 令牌 + `seq` 校验（丢消息即请求 snapshot）。
- zustand 全局 store：`roomState / gameState / phase / myPrivate / chat`。
- 所有倒计时以消息内 `server_now` 校准，本地时钟仅做差值渲染。

---

## 7. 测试计划

| 层 | 内容 |
|---|---|
| 单元 | formulas.py 全公式（cap/hard/floor/defense/均摊/guard 系数/对冲削减上限）；口令 argon2 往返；限流窗口；包 schema 校验（含非法包拒绝） |
| 集成 | WS 协议往返（fake client）；状态机全转移路径（含三个超时兜底、早退、跳过死亡玩家）；重启进程后事件重建 == 内存终态 |
| E2E（headless） | `--autopilot` 模式：房主 + 4 假人自动开局打满至胜利；断言：HP 永不为负、seq 连续、账本条目类型合法、回合轮转跳过死者、事件重建一致性 |
| 手测 | 双浏览器（或本机 + 手机）走查：建房→加入→开局→宣言→回应→结算→死亡→胜利；杀标签页重连 |

---

## 8. 验收清单（出口条件）

- [ ] `run.bat` 一键启动，打印 localhost / 局域网 / Tailscale 三个地址并自动打开浏览器
- [ ] 口令错误被拒；连续 5 次失败后来源被锁 10 分钟；错误码正确
- [ ] 1 名真人 + 3 假人可完整打完一局直至胜利结算画面
- [ ] 回合状态机五阶段全部可达；宣言/回应/裁判三处超时兜底全部实测触发
- [ ] 全员提前提交时回应窗口早退（实测等待时间 < 2s）
- [ ] 数值公式单测全覆盖且通过（含 hard 钳制、floor≥1、均摊、guard、对冲削减上限 45%）
- [ ] 属性数值对他人不可见（他人 UI 与 WS 消息中均无本人属性数值）；本人可见
- [ ] 杀掉浏览器标签页后重连，HP、回合、阶段、账本完全一致
- [ ] 重启服务进程，房间从事件日志恢复现场
- [ ] headless 自动局跑 100 局无异常（不同种子）
- [ ] 全局代码检查通过：无冗余代码、无重复定义、无死代码、无未处理竞态（对齐工程惯例）

---

## 9. 风险与规避

| 风险 | 规避 |
|---|---|
| stub 公式数值失衡导致「假人互殴不好玩」，误判为设计失败 | 数值全部来自包 YAML，可热调；P0 验收时至少调参试玩 3 轮再下结论 |
| 前端提前过度打磨拖慢节奏 | P0 UI 明确「MVP 视觉」；打磨任务全部锁在 P2 |
| 事件 schema 设计不足，P1/P2 反复迁移 | 消息字段按 PRD 全量设计（含 P2 用的 status 枚举），P0 未用的字段允许先空置 |
| Windows 环境 Python 版本参差 | run.bat 显式版本检查 + 指引；不静默降级 |
