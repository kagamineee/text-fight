"""WebSocket 入站消息契约。

出站消息采用普通 dict（字段即协议），入站必须经 Pydantic 校验，
保证任何客户端输入都不会直接进入业务逻辑。
"""

from __future__ import annotations

import json
from typing import Literal, Optional, Union

from pydantic import BaseModel, Field, ValidationError


class ProtocolError(Exception):
    pass


class _In(BaseModel):
    type: str


class HelloIn(_In):
    type: Literal["hello"]
    name: str = Field(min_length=1, max_length=16)


class CreateRoomIn(_In):
    type: Literal["create_room"]
    password: str = Field(min_length=4, max_length=64)
    name: str = Field(default="", max_length=16)


class JoinRoomIn(_In):
    type: Literal["join_room"]
    code: str = Field(min_length=4, max_length=8)
    password: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=16)


class LeaveIn(_In):
    type: Literal["leave_room"]


class AddBotIn(_In):
    type: Literal["add_bot"]


class RemoveBotIn(_In):
    type: Literal["remove_bot"]
    bot_id: str


class KickIn(_In):
    type: Literal["kick"]
    player_id: str


class StartGameIn(_In):
    type: Literal["start_game"]


class SetReadyIn(_In):
    type: Literal["set_ready"]
    ready: bool


class ReturnLobbyIn(_In):
    type: Literal["return_lobby"]


class DeclareIn(_In):
    type: Literal["declare"]
    content: str = Field(min_length=1, max_length=800)


class ChatIn(_In):
    type: Literal["chat"]
    text: str = Field(min_length=1, max_length=500)
    channel: Literal["table", "ghost"] = "table"


class OpenAttributeIn(_In):
    type: Literal["open_attribute"]
    name: str = Field(min_length=1, max_length=16)
    narrative: str = Field(default="", max_length=200)
    attr_type: Literal["attack", "defense", "utility"]
    initial: int = Field(ge=1, le=99)
    cap: int = Field(ge=1, le=999)
    exchange_rate: float = Field(gt=0, le=10)
    consume_rule: str = Field(default="", max_length=200)


class ReconnectIn(_In):
    type: Literal["reconnect"]
    token: str


class HeartbeatIn(_In):
    type: Literal["heartbeat"]


_INBOUND: dict[str, type[BaseModel]] = {
    "hello": HelloIn,
    "create_room": CreateRoomIn,
    "join_room": JoinRoomIn,
    "leave_room": LeaveIn,
    "add_bot": AddBotIn,
    "remove_bot": RemoveBotIn,
    "kick": KickIn,
    "start_game": StartGameIn,
    "set_ready": SetReadyIn,
    "return_lobby": ReturnLobbyIn,
    "declare": DeclareIn,
    "chat": ChatIn,
    "open_attribute": OpenAttributeIn,
    "reconnect": ReconnectIn,
    "heartbeat": HeartbeatIn,
}

InboundMessage = Union[
    HelloIn,
    CreateRoomIn,
    JoinRoomIn,
    LeaveIn,
    AddBotIn,
    RemoveBotIn,
    KickIn,
    StartGameIn,
    SetReadyIn,
    ReturnLobbyIn,
    DeclareIn,
    ChatIn,
    OpenAttributeIn,
    ReconnectIn,
    HeartbeatIn,
]


def parse_inbound(raw: str) -> InboundMessage:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProtocolError("消息不是合法 JSON") from exc
    if not isinstance(data, dict) or "type" not in data:
        raise ProtocolError("消息缺少 type 字段")
    model = _INBOUND.get(str(data["type"]))
    if model is None:
        raise ProtocolError(f"未知消息类型：{data['type']}")
    try:
        return model.model_validate(data)  # type: ignore[return-value]
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(p) for p in first["loc"])
        raise ProtocolError(f"字段 {field} 不合法：{first['msg']}") from exc
