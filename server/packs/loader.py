"""扩展包加载器。

扫描两个位置：
- 内置包：server/packs/builtin/*.yaml，随仓库发布
- 用户包：默认 ~/.yap-arena/packs/*.yaml，可用环境变量 YAP_PACKS_DIR 覆盖

用户包与内置包同 id 时覆盖内置包，方便本地改写；任何包解析失败都会保留其余
可用包，并把「哪个字段、错在哪」写成人话交给调用方展示。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Annotated, Iterable, Literal, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

PACKS_DIR = Path(__file__).resolve().parent
BUILTIN_DIR = PACKS_DIR / "builtin"
MAX_PACK_BYTES = 200 * 1024


class PackError(Exception):
    pass


def user_packs_dir() -> Path:
    raw = os.environ.get("YAP_PACKS_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".yap-arena" / "packs").resolve()


class HpCfg(BaseModel):
    base: int = Field(ge=10, le=999)
    regen: int = Field(ge=0, le=99)


class IntensityCfg(BaseModel):
    cap_base: int = Field(ge=1, le=999)
    cap_growth: int = Field(ge=0, le=99)
    ceiling_factor: float = Field(ge=1.0, le=5.0)
    level_words: dict[str, float]

    @field_validator("level_words")
    @classmethod
    def _check_words(cls, v: dict[str, float]) -> dict[str, float]:
        if not v:
            raise ValueError("程度词不能为空")
        for k, ratio in v.items():
            if not 0 < ratio <= 5:
                raise ValueError(f"程度词「{k}」的倍率必须在 (0, 5] 之间")
        return v


class AttributeCfg(BaseModel):
    max_count: int = Field(ge=1, le=10)
    max_initial: int = Field(ge=1, le=999)
    max_cap: int = Field(ge=1, le=9999)
    max_exchange_rate: float = Field(gt=0, le=10)

    @model_validator(mode="after")
    def _check_limits(self) -> "AttributeCfg":
        if self.max_initial > self.max_cap:
            raise ValueError("max_initial 不能大于 max_cap")
        return self


class TimerCfg(BaseModel):
    declare_s: int = Field(ge=10, le=600)
    judge_s: int = Field(ge=5, le=300)


class DisasterCfg(BaseModel):
    start_turn: int = Field(ge=5, le=999)
    every_n_turns: int = Field(ge=1, le=20)
    base_damage: int = Field(ge=0, le=999)
    growth: int = Field(ge=0, le=999)


class AppealCfg(BaseModel):
    count: int = Field(ge=0, le=10)
    window_s: int = Field(ge=5, le=120)
    vote_s: int = Field(ge=10, le=300)


class InheritanceCfg(BaseModel):
    power_ratio: float = Field(gt=0, le=1.0)


TurnNo = Annotated[int, Field(ge=1, le=999)]
EventTarget = Literal[
    "all",
    "random",
    "lowest_hp",
    "highest_hp",
    "lowest_ratio",
    "highest_ratio",
]


class EventTriggerCfg(BaseModel):
    """事件触发条件：turns 与 every 至少配置一个，其余为可选限制。"""

    turns: list[TurnNo] = Field(default_factory=list, max_length=20)
    every: int = Field(default=0, ge=0, le=50)  # 0 = 不按周期触发
    start_turn: int = Field(default=1, ge=1, le=999)
    chance: float = Field(default=1.0, gt=0, le=1.0)
    cooldown: int = Field(default=0, ge=0, le=20)
    max_times: int = Field(default=0, ge=0, le=99)  # 0 = 不限次数

    @model_validator(mode="after")
    def _needs_when(self) -> "EventTriggerCfg":
        if not self.turns and self.every <= 0:
            raise ValueError("至少要配置 turns（指定回合）或 every（触发间隔）")
        return self


class EventAttrCfg(BaseModel):
    """事件授予的属性模板。"""

    name: str = Field(min_length=1, max_length=16)
    narrative: str = Field(default="", max_length=200)
    attr_type: Literal["attack", "defense", "utility"] = "utility"
    value: int = Field(default=1, ge=1, le=999)
    cap: Optional[int] = Field(default=None, ge=1, le=9999)  # 不填则取 value 的两倍
    duration: int = Field(default=0, ge=0, le=99)
    shield: bool = False
    freeze: bool = False
    per_turn: int = Field(default=0, ge=-99, le=99)
    indestructible: bool = False
    damage_reduction: int = Field(default=0, ge=0, le=90)
    shield_charges: int = Field(default=0, ge=0, le=99)
    lethal_only: bool = False
    block_heal_pct: int = Field(default=0, ge=0, le=100)


class EventCfg(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")
    name: str = Field(min_length=1, max_length=24)
    narrative: str = Field(default="", max_length=300)
    trigger: EventTriggerCfg
    target: EventTarget = "all"
    # fixed：引擎按 hp/hp_pct/attribute 硬结算；judged：judge_prompt 交给裁判 AI 判定
    mode: Literal["fixed", "judged"] = "fixed"
    judge_prompt: str = Field(default="", max_length=800)
    hp: int = Field(default=0, ge=-999, le=999)
    hp_pct: int = Field(default=0, ge=-100, le=100)  # 按目标最大生命的百分比
    attribute: Optional[EventAttrCfg] = None

    @model_validator(mode="after")
    def _judged_needs_prompt(self) -> "EventCfg":
        if self.mode == "judged" and not self.judge_prompt.strip():
            raise ValueError("mode 为 judged 的事件必须填写 judge_prompt（给裁判的裁定指令）")
        return self


class Pack(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,40}$")
    name: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=400)
    hp: HpCfg
    intensity: IntensityCfg
    attributes: AttributeCfg
    timers: TimerCfg
    disaster: DisasterCfg
    appeal: AppealCfg = Field(default_factory=lambda: AppealCfg(count=0, window_s=15, vote_s=30),
                             description="旧包兼容字段，当前不执行上诉")
    inheritance: InheritanceCfg
    events: list[EventCfg] = Field(default_factory=list, max_length=12)
    victory: str = "last_alive"
    tie_break: list[Literal["hp_peak", "survived_turns"]] = Field(default_factory=list)
    referee_prompt: str = ""

    @model_validator(mode="after")
    def _unique_events(self) -> "Pack":
        ids = [e.id for e in self.events]
        dup = sorted({i for i in ids if ids.count(i) > 1})
        if dup:
            raise ValueError(f"事件 id 重复：{'、'.join(dup)}")
        if len(self.tie_break) != len(set(self.tie_break)):
            raise ValueError("tie_break 比较项不能重复")
        return self

    def public_view(self) -> dict:
        """给客户端看的包信息（不含裁判提示词）。"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "hp": self.hp.model_dump(),
            "intensity": {
                "cap_base": self.intensity.cap_base,
                "cap_growth": self.intensity.cap_growth,
                "level_words": self.intensity.level_words,
            },
            "attributes": self.attributes.model_dump(),
            "timers": self.timers.model_dump(),
            "events": [
                {"id": e.id, "name": e.name, "target": e.target, "mode": e.mode}
                for e in self.events
            ],
        }


FIELD_LABELS = {
    "id": "包 id",
    "name": "名称",
    "description": "描述",
    "hp": "血量配置",
    "hp.base": "初始血量",
    "hp.regen": "每回合回复",
    "intensity": "强度曲线",
    "intensity.cap_base": "初始强度上限",
    "intensity.cap_growth": "强度每回合增长",
    "intensity.ceiling_factor": "强度封顶系数",
    "intensity.level_words": "程度词倍率",
    "attributes": "属性配置",
    "attributes.max_count": "属性数量上限",
    "attributes.max_initial": "属性初始值上限",
    "attributes.max_cap": "属性上限",
    "attributes.max_exchange_rate": "属性兑换率上限",
    "timers": "时限配置",
    "timers.declare_s": "宣言时长（秒）",
    "timers.judge_s": "裁定时长（秒）",
    "disaster": "天灾配置",
    "disaster.start_turn": "天灾起始回合",
    "disaster.every_n_turns": "天灾升级间隔",
    "disaster.base_damage": "天灾基础伤害",
    "disaster.growth": "天灾每级增伤",
    "appeal": "上诉配置",
    "appeal.count": "上诉次数",
    "appeal.window_s": "上诉窗口（秒）",
    "appeal.vote_s": "上诉投票（秒）",
    "inheritance": "继承配置",
    "inheritance.power_ratio": "继承折算比例",
    "events": "特殊事件",
    "events.*.id": "事件 id",
    "events.*.name": "事件名称",
    "events.*.narrative": "事件文案",
    "events.*.trigger": "事件触发条件",
    "events.*.trigger.turns": "指定触发回合",
    "events.*.trigger.turns.*": "指定触发回合",
    "events.*.trigger.every": "触发间隔",
    "events.*.trigger.start_turn": "触发起始回合",
    "events.*.trigger.chance": "触发概率",
    "events.*.trigger.cooldown": "触发冷却",
    "events.*.trigger.max_times": "最多触发次数",
    "events.*.target": "事件目标",
    "events.*.mode": "事件模式",
    "events.*.judge_prompt": "事件裁定指令",
    "events.*.hp": "事件固定血量变化",
    "events.*.hp_pct": "事件百分比血量变化",
    "events.*.attribute": "事件授予属性",
    "events.*.attribute.name": "事件属性名",
    "events.*.attribute.narrative": "事件属性描述",
    "events.*.attribute.attr_type": "事件属性类型",
    "events.*.attribute.value": "事件属性初始值",
    "events.*.attribute.cap": "事件属性上限",
    "events.*.attribute.duration": "事件属性持续回合",
    "events.*.attribute.per_turn": "事件属性每回合变化",
    "events.*.attribute.damage_reduction": "事件属性减伤百分比",
    "events.*.attribute.shield_charges": "事件属性免伤次数",
    "events.*.attribute.block_heal_pct": "事件属性格挡回血百分比",
    "victory": "胜负条件",
    "tie_break": "平局比较项",
    "referee_prompt": "裁判附言",
}

_INDEX_RE = re.compile(r"\.\d+")


def _field_label(path: str) -> str:
    if path in FIELD_LABELS:
        return FIELD_LABELS[path]
    normalized = _INDEX_RE.sub(".*", path)
    if normalized in FIELD_LABELS:
        return FIELD_LABELS[normalized]
    if path.startswith("intensity.level_words."):
        return f"程度词「{path.rsplit('.', 1)[-1]}」的倍率"
    return path


def _error_hint(err: dict) -> str:
    kind = err.get("type", "")
    ctx = err.get("ctx") or {}
    if kind == "missing":
        return "缺少该字段"
    if kind == "int_parsing":
        return "必须是整数"
    if kind == "float_parsing":
        return "必须是数字"
    if kind == "bool_parsing":
        return "必须是 true 或 false"
    if kind in ("list_type", "dict_type"):
        return "格式不正确"
    if kind == "greater_than_equal":
        return f"不能小于 {ctx.get('ge')}"
    if kind == "less_than_equal":
        return f"不能大于 {ctx.get('le')}"
    if kind == "greater_than":
        return f"必须大于 {ctx.get('gt')}"
    if kind == "string_pattern_mismatch":
        return "只能使用小写字母、数字和短横线（1~40 位）"
    if kind == "string_too_short":
        return f"至少 {ctx.get('min_length')} 个字符"
    if kind == "string_too_long":
        return f"最多 {ctx.get('max_length')} 个字符"
    if kind == "too_long":
        return f"最多 {ctx.get('max_length')} 项"
    if kind == "literal_error":
        return f"只能填 {ctx.get('expected')}"
    if kind == "value_error":
        return str(err.get("msg", "")).removeprefix("Value error, ")
    return str(err.get("msg", "格式不正确"))


def _friendly_message(err: dict) -> str:
    path = ".".join(str(p) for p in err.get("loc", []))
    hint = _error_hint(err)
    if not path:
        return f"扩展包：{hint}"
    label = _field_label(path)
    current = err.get("input")
    if isinstance(current, (int, float, str)) and len(str(current)) <= 40:
        return f"「{label}」（{path}）：{hint}；当前值 {current!r}"
    return f"「{label}」（{path}）：{hint}"


def _allowed_roots() -> Iterable[Path]:
    return (BUILTIN_DIR.resolve(), PACKS_DIR.resolve(), user_packs_dir())


def _assert_inside(candidate: Path) -> None:
    resolved = candidate.resolve()
    for root in _allowed_roots():
        try:
            resolved.relative_to(root)
            return
        except ValueError:
            continue
    raise PackError(f"扩展包路径越界，只能放在内置目录或用户包目录：{candidate}")


def load_pack_file(path: Path) -> Pack:
    _assert_inside(path)
    if path.stat().st_size > MAX_PACK_BYTES:
        raise PackError(f"扩展包过大（上限 {MAX_PACK_BYTES // 1024}KB）：{path.name}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise PackError(f"扩展包必须是 UTF-8 编码：{path.name}") from exc
    except yaml.YAMLError as exc:
        raise PackError(f"扩展包 YAML 语法错误（{path.name}）：{exc}") from exc
    if not isinstance(raw, dict):
        raise PackError(f"扩展包根节点必须是「字段: 值」的映射：{path.name}")
    try:
        return Pack.model_validate(raw)
    except ValidationError as exc:
        details = [_friendly_message(err) for err in exc.errors()]
        raise PackError(
            f"扩展包校验失败（{path.name}）：\n  " + "\n  ".join(details)
        ) from exc


def validate_pack_text(text: str) -> tuple[Optional[Pack], list[str]]:
    """从 YAML 文本校验扩展包（开发者面板用）；返回 (pack, issues)，issues 为空即通过。"""
    if len(text.encode("utf-8")) > MAX_PACK_BYTES:
        return None, [f"扩展包文件不能超过 {MAX_PACK_BYTES // 1024} KB。"]
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return None, [f"YAML 语法错误：{exc}"]
    if not isinstance(raw, dict):
        return None, ["根节点必须是「字段: 值」的映射"]
    try:
        return Pack.model_validate(raw), []
    except ValidationError as exc:
        return None, [_friendly_message(err) for err in exc.errors()]


class PackRegistry:
    """扩展包注册表：扫描、校验、热加载。"""

    def __init__(self) -> None:
        self.packs: dict[str, Pack] = {}
        self.issues: list[dict] = []
        self.user_files: dict[str, Path] = {}
        self._signature: tuple = ()

    @staticmethod
    def _files() -> list[tuple[str, Path]]:
        files: list[tuple[str, Path]] = []
        if BUILTIN_DIR.exists():
            files += [("builtin", f) for f in sorted(BUILTIN_DIR.glob("*.yaml"))]
        user_dir = user_packs_dir()
        if user_dir.exists():
            files += [("user", f) for f in sorted(user_dir.glob("*.yaml"))]
        return files

    @classmethod
    def _scan_signature(cls) -> tuple:
        sig = []
        for source, file in cls._files():
            try:
                st = file.stat()
                sig.append((source, str(file), st.st_mtime_ns, st.st_size))
            except OSError:
                sig.append((source, str(file), 0, 0))
        return tuple(sig)

    def reload(self) -> "PackRegistry":
        packs: dict[str, Pack] = {}
        issues: list[dict] = []
        user_files: dict[str, Path] = {}
        for source, file in self._files():
            try:
                pack = load_pack_file(file)
            except PackError as exc:
                issues.append({"file": file.name, "source": source, "message": str(exc)})
                continue
            except OSError as exc:
                issues.append(
                    {"file": file.name, "source": source, "message": f"读取失败：{exc}"}
                )
                continue
            packs[pack.id] = pack
            if source == "user":
                user_files[pack.id] = file
        if not packs:
            detail = "\n".join(item["message"] for item in issues) or "目录为空"
            raise PackError(f"没有可用的扩展包：\n{detail}")
        self.packs = packs
        self.issues = issues
        self.user_files = user_files
        self._signature = self._scan_signature()
        return self

    def maybe_reload(self) -> bool:
        if self._scan_signature() != self._signature:
            self.reload()
            return True
        return False

    @property
    def default_id(self) -> str:
        if "default-arena" in self.packs:
            return "default-arena"
        return next(iter(self.packs))


def load_packs() -> dict[str, Pack]:
    return PackRegistry().reload().packs


PACK_TEMPLATE = """id: my-arena
name: 我的角斗场
description: 一句话说明这个包的玩法和气质。

hp:
  base: 100
  regen: 5

intensity:
  cap_base: 8
  cap_growth: 2
  ceiling_factor: 1.5
  level_words:
    微弱: 0.3
    寻常: 0.6
    强力: 1.0
    致命: 1.6

attributes:
  max_count: 10
  max_initial: 10
  max_cap: 30
  max_exchange_rate: 2.0

timers:
  declare_s: 60
  judge_s: 60

disaster:
  start_turn: 15
  every_n_turns: 3
  base_damage: 2
  growth: 1

appeal:
  count: 2
  window_s: 30
  vote_s: 60

inheritance:
  power_ratio: 0.4

events:
  - id: first-blood
    name: 第一滴血
    narrative: 看台的鼓点突然停了，所有人同时看向 {target}。
    trigger:
      turns: [4]
    target: random
    hp: -6
  - id: whisper
    name: 低语
    narrative: 有什么东西在 {target} 耳边说了一句话，他的脸色变了。
    trigger:
      start_turn: 8
      every: 4
      chance: 0.6
    target: lowest_hp
    mode: judged
    judge_prompt: |
      低语勾起目标最不愿回想的一件事：造成中等精神伤害（3~6 点），
      并视其当前状态决定是否附加一个持续 2 回合的「心神不宁」属性。

victory: last_alive
tie_break:
  - survived_turns
  - hp_peak

referee_prompt: |
  世界观：写清这个世界的规则、意象和禁忌。
  裁判语气：冷峻、克制，只写谁对谁造成了什么效果，不替玩家抒情。
"""


PACK_FIELD_GUIDE: list[dict] = [
    {"path": "hp", "label": "血量", "note": "base 初始血量（10~999），regen 每回合回复（0~99）"},
    {"path": "intensity", "label": "强度曲线", "note": "cap_base 初始上限、cap_growth 每回合增长、ceiling_factor 封顶系数；level_words 是程度词倍率（0~5）"},
    {"path": "attributes", "label": "属性配置", "note": "max_count 每人属性数、max_initial 新建初始值上限（不得超过 max_cap）、max_cap 属性上限；max_exchange_rate 为旧包兼容字段"},
    {"path": "timers", "label": "时限", "note": "declare_s 宣言秒数（10~600），judge_s 裁判秒数（5~300）"},
    {"path": "appeal", "label": "上诉", "note": "保留字段，当前版本不使用"},
    {"path": "disaster", "label": "天灾", "note": "start_turn 起始回合、every_n_turns 升级间隔、base_damage 基础伤害、growth 每级增伤"},
    {"path": "inheritance", "label": "继承", "note": "power_ratio 击杀继承的数值折算比例（0~1）"},
    {"path": "tie_break", "label": "平局比较", "note": "依次比较 hp_peak（血量峰值）或 survived_turns（存活回合）；全灭时唯一领先者胜出，完全相同则平局；空列表不打破平局"},
    {"path": "events[].trigger", "label": "事件触发", "note": "turns 指定回合，或 every + start_turn 周期触发；chance 概率、cooldown 冷却、max_times 次数上限"},
    {"path": "events[].target", "label": "事件目标", "note": "all / random / lowest_hp / highest_hp / lowest_ratio / highest_ratio"},
    {"path": "events[].mode", "label": "事件模式", "note": "fixed 按 hp/hp_pct/attribute 硬结算；judged 交给裁判 AI，必须写 judge_prompt"},
    {"path": "events[].hp / hp_pct", "label": "事件效果", "note": "hp 固定血量变化，hp_pct 按目标最大生命百分比（正数回复）"},
    {"path": "referee_prompt", "label": "裁判附言", "note": "世界观与口径，会拼进每次裁定的系统提示"},
]
