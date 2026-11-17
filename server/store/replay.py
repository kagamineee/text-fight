"""事件重放：从 append-only 日志重建房间现场。

重启恢复的唯一路径（PRD §9.3）。
"""

from __future__ import annotations

from typing import Iterable


def _new_player(payload: dict) -> dict:
    return {
        "player_id": payload["player_id"],
        "name": payload["name"],
        "seat": payload["seat"],
        "kind": payload.get("player_kind", "human"),
        "hp": payload["hp"],
        "hp_max": payload["hp_max"],
        "alive": True,
        "ready": payload.get("player_kind", "human") == "bot",
        "hp_peak": payload["hp_max"],
        "survived_turns": 0,
        "guard_active": False,
        "attributes": {},
    }


def rebuild(events: Iterable) -> dict:
    state = {
        "players": {},
        "order": [],
        "turn_no": 0,
        "over": False,
        "winner": None,
        "started": False,
        "current_actor": None,
        "disaster_level": 0,
    }
    players = state["players"]

    for ev in events:
        payload = ev.payload or {}
        kind = payload.get("kind")

        if kind == "player_joined":
            pid = payload["player_id"]
            players[pid] = _new_player(payload)
            state["order"].append(pid)

        elif kind == "player_left":
            pid = payload.get("player_id")
            if pid in players:
                del players[pid]
            state["order"] = [p for p in state["order"] if p != pid]

        elif kind == "game_started":
            state["started"] = True

        elif kind == "turn_begin":
            state["started"] = True
            state["turn_no"] = int(payload.get("turn_no", state["turn_no"]))
            state["current_actor"] = payload.get("current_player")
            for pid, hp in (payload.get("hp") or {}).items():
                if pid in players:
                    players[pid]["hp"] = hp
                    players[pid]["hp_peak"] = max(players[pid]["hp_peak"], hp)
            for pid in state["order"]:
                if players[pid]["alive"]:
                    players[pid]["survived_turns"] += 1

        elif kind == "attribute_sealed":
            owner = players.get(payload.get("owner_id"))
            if owner is not None:
                owner["attributes"][payload["attr_id"]] = {
                    "attr_id": payload["attr_id"],
                    "owner_id": payload["owner_id"],
                    "name": payload["name"],
                    "narrative": payload.get("narrative", ""),
                    "attr_type": payload["attr_type"],
                    "initial": payload["initial"],
                    "cap": payload["cap"],
                    "exchange_rate": payload["exchange_rate"],
                    "consume_rule": payload.get("consume_rule", ""),
                    "value": payload["value"],
                    "opened_turn": payload.get("opened_turn", 0),
                }

        elif kind == "adjudication":
            effects = payload.get("effects")
            if effects is not None:
                for effect in effects:
                    pid = effect.get("player_id")
                    if pid not in players:
                        continue
                    delta = int(effect.get("hp_delta", 0))
                    players[pid]["hp"] = max(
                        0, min(players[pid]["hp_max"], players[pid]["hp"] + delta)
                    )
            for pid, dmg in (payload.get("damages") or {}).items():
                if pid in players:
                    players[pid]["hp"] = max(0, players[pid]["hp"] - int(dmg))
            for change in payload.get("attr_changes") or []:
                owner = players.get(change.get("owner"))
                if owner is None:
                    continue
                attr_id = change.get("attr_id")
                if change.get("remove"):
                    owner["attributes"].pop(str(attr_id), None)
                    continue
                attr = owner["attributes"].get(attr_id)
                if attr is not None:
                    attr["value"] = int(change["value"])

        elif kind == "death":
            pid = payload.get("player_id")
            if pid in players:
                players[pid]["alive"] = False
                players[pid]["hp"] = 0

        elif kind == "game_over":
            state["over"] = True
            state["winner"] = payload.get("winner")

        elif kind == "lobby_returned":
            state["over"] = False
            state["winner"] = None
            state["started"] = False
            state["turn_no"] = 0
            state["current_actor"] = None
            for player in players.values():
                player["hp"] = player["hp_max"]
                player["alive"] = True
                player["survived_turns"] = 0
                player["attributes"].clear()
                player["ready"] = player["kind"] == "bot"

    for pid in state["order"]:
        players[pid]["seat"] = state["order"].index(pid)

    return state
