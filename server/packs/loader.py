"""扩展包加载器。

安全要求（PRD §6.6）：路径必须用 path.relative 校验，禁止仅前缀匹配。
P0 只做加载与校验；房间选包留到 P2。
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

PACKS_DIR = Path(__file__).resolve().parent
BUILTIN_DIR = PACKS_DIR / "builtin"
MAX_PACK_BYTES = 200 * 1024


class PackError(Exception):
    pass


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
            raise ValueError("level_words 不能为空")
        for k, ratio in v.items():
            if not 0 < ratio <= 5:
                raise ValueError(f"程度词 {k} 的倍率必须在 (0, 5] 之间")
        return v


class AttributeCfg(BaseModel):
    max_count: int = Field(ge=1, le=10)
    max_initial: int = Field(ge=1, le=999)
    max_cap: int = Field(ge=1, le=9999)
    max_exchange_rate: float = Field(gt=0, le=10)


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


class Pack(BaseModel):
    id: str
    name: str
    description: str = ""
    hp: HpCfg
    intensity: IntensityCfg
    attributes: AttributeCfg
    timers: TimerCfg
    disaster: DisasterCfg
    appeal: AppealCfg
    inheritance: InheritanceCfg
    victory: str = "last_alive"
    tie_break: list[str] = Field(default_factory=list)
    referee_prompt: str = ""

    def public_view(self) -> dict:
        """给客户端看的包信息（不含裁判提示词，P1 才开放）。"""
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
        }


def _assert_inside(base: Path, candidate: Path) -> None:
    rel = os.path.relpath(candidate, base)
    if rel.startswith(".."):
        raise PackError(f"扩展包路径越界：{candidate}")


def load_pack_file(path: Path) -> Pack:
    _assert_inside(PACKS_DIR, path.resolve())
    if path.stat().st_size > MAX_PACK_BYTES:
        raise PackError(f"扩展包过大（>{MAX_PACK_BYTES // 1024}KB）：{path.name}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise PackError(f"扩展包必须是 UTF-8 编码：{path.name}") from exc
    except yaml.YAMLError as exc:
        raise PackError(f"扩展包 YAML 语法错误：{path.name} → {exc}") from exc
    if not isinstance(raw, dict):
        raise PackError(f"扩展包根节点必须是映射：{path.name}")
    try:
        return Pack.model_validate(raw)
    except ValidationError as exc:
        details = []
        for err in exc.errors():
            field = ".".join(str(p) for p in err["loc"])
            details.append(f"{field}: {err['msg']}")
        raise PackError(f"扩展包校验失败（{path.name}）：\n  " + "\n  ".join(details)) from exc


def load_packs() -> dict[str, Pack]:
    packs: dict[str, Pack] = {}
    if not BUILTIN_DIR.exists():
        raise PackError(f"缺少内置包目录：{BUILTIN_DIR}")
    for file in sorted(BUILTIN_DIR.glob("*.yaml")):
        pack = load_pack_file(file)
        packs[pack.id] = pack
    if not packs:
        raise PackError("未找到任何内置扩展包")
    return packs
