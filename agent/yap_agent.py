#!/usr/bin/env python3
"""yap-agent：文字角斗场的本地模型代理。

只做三件事：读配置、校验来源、把 /v1/chat/completions 透传给上游模型。
配置可在游戏界面填写（POST /config，仅接受白名单来源），密钥只落在本机文件里。

用法：
    python agent/yap_agent.py --config agent/config.toml
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import os
import sys
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

VERSION = "0.1.0"
MAX_BODY = 2 * 1024 * 1024
DEFAULT_ALLOWED = [
    "http://localhost:*",
    "http://127.0.0.1:*",
    "http://192.168.*:*",
]

logger = logging.getLogger("yap-agent")


@dataclass
class Config:
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    key_env: str = "YAP_KEY"
    key_file: Optional[str] = None
    port: int = 17666
    allowed_origins: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED))
    passcode: str = ""
    timeout_s: float = 120.0


SAVED_NAME = "config.saved.toml"


def load_config(path: Path) -> Config:
    """基础配置 + 界面保存的覆盖配置；两个文件都可以不存在。"""
    data: dict = {}
    if path.exists():
        data.update(tomllib.loads(path.read_text(encoding="utf-8")))
    saved = path.parent / SAVED_NAME
    if saved.exists():
        data.update(tomllib.loads(saved.read_text(encoding="utf-8")))

    origins = data.get("allowed_origins") or DEFAULT_ALLOWED
    if not isinstance(origins, list) or not all(isinstance(x, str) for x in origins):
        raise ValueError("allowed_origins 必须是字符串数组")
    return Config(
        base_url=str(data.get("base_url", "")).strip().rstrip("/"),
        model=str(data.get("model", "")).strip(),
        api_key=str(data.get("api_key", "")).strip(),
        key_env=str(data.get("key_env", "YAP_KEY")).strip(),
        key_file=str(data["key_file"]).strip() if data.get("key_file") else None,
        port=int(data.get("port", 17666)),
        allowed_origins=origins,
        passcode=str(data.get("passcode", "")),
        timeout_s=float(data.get("timeout_s", 120)),
    )


def resolve_key(cfg: Config, config_dir: Path) -> str:
    if cfg.api_key:
        return cfg.api_key
    if cfg.key_env:
        value = os.environ.get(cfg.key_env, "").strip()
        if value:
            return value
    if cfg.key_file:
        path = Path(cfg.key_file)
        if not path.is_absolute():
            path = config_dir / path
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    return ""


def toml_string(value: str) -> str:
    # JSON 字符串转义是 TOML basic string 的子集，够用且不引入依赖
    return json.dumps(value, ensure_ascii=False)


def origin_allowed(origin: str, allowed: list[str]) -> bool:
    """无 Origin 的请求（服务端调用）放行；浏览器来源必须在白名单内。"""
    if not origin:
        return True
    return any(fnmatch.fnmatch(origin, pattern) for pattern in allowed)


class AgentHandler(BaseHTTPRequestHandler):
    server_version = f"yap-agent/{VERSION}"
    cfg: Config = None  # type: ignore[assignment]
    key: str = ""
    config_dir: Path = Path(".")

    # ------------------------------------------------------------ 基础
    def log_message(self, fmt: str, *args) -> None:
        logger.info("%s %s", self.address_string(), fmt % args)

    def _origin(self) -> str:
        return self.headers.get("Origin", "")

    def _origin_ok(self) -> bool:
        return origin_allowed(self._origin(), self.cfg.allowed_origins)

    def _send_cors(self) -> None:
        origin = self._origin()
        if origin and origin_allowed(origin, self.cfg.allowed_origins):
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header(
                "Access-Control-Allow-Headers", "content-type, x-yap-passcode"
            )
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")

    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self._send_cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _deny_origin(self) -> None:
        self._send_json(403, {"error": {"message": "Origin 不在白名单内"}})

    # ------------------------------------------------------------ 路由
    def do_OPTIONS(self) -> None:  # noqa: N802
        if not self._origin_ok():
            self._deny_origin()
            return
        self.send_response(204)
        self._send_cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if not self._origin_ok():
            self._deny_origin()
            return
        if urlparse(self.path).path != "/health":
            self._send_json(404, {"error": {"message": "未知路径"}})
            return
        self._send_json(200, self._health_payload())

    def _health_payload(self) -> dict:
        hint = f"…{self.key[-4:]}" if self.key else ""
        return {
            "ok": True,
            "version": VERSION,
            "model": self.cfg.model,
            "has_key": bool(self.key),
            "key_hint": hint,
            "configured": bool(self.cfg.base_url),
            "upstream": self.cfg.base_url,
        }

    def _read_json_body(self) -> Optional[dict]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            self._send_json(400, {"error": {"message": "请求体为空"}})
            return None
        if length > MAX_BODY:
            self._send_json(413, {"error": {"message": "请求体过大"}})
            return None
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {"error": {"message": "请求体不是合法 JSON"}})
            return None
        if not isinstance(body, dict):
            self._send_json(400, {"error": {"message": "请求体必须是对象"}})
            return None
        return body

    def do_POST(self) -> None:  # noqa: N802
        if not self._origin_ok():
            self._deny_origin()
            return
        path = urlparse(self.path).path
        if path == "/config":
            self._handle_config()
            return
        if path == "/v1/test":
            self._handle_test()
            return
        if path != "/v1/chat/completions":
            self._send_json(404, {"error": {"message": "未知路径"}})
            return
        if self.cfg.passcode:
            if self.headers.get("X-Yap-Passcode", "") != self.cfg.passcode:
                self._send_json(401, {"error": {"message": "口令不正确"}})
                return
        if not self.cfg.base_url:
            self._send_json(
                400,
                {"error": {"message": "还没有配置模型地址，请在游戏里打开 AI 设置填写"}},
            )
            return

        body = self._read_json_body()
        if body is None:
            return

        if not body.get("model"):
            body["model"] = self.cfg.model
        stream = bool(body.get("stream"))

        req = self._upstream_request(
            body, "text/event-stream" if stream else "application/json"
        )
        if stream:
            self._proxy_stream(req)
        else:
            self._proxy_once(req)

    # ------------------------------------------------------------ 配置
    def _handle_config(self) -> None:
        # 管理接口只接受页面调用，避免被本机其他来源静默改写
        if not self._origin():
            self._send_json(403, {"error": {"message": "该接口只接受页面调用"}})
            return
        body = self._read_json_body()
        if body is None:
            return

        base_url = str(body.get("base_url", "")).strip().rstrip("/")
        model = str(body.get("model", "")).strip()
        api_key = body.get("api_key", None)
        if not base_url:
            self._send_json(400, {"error": {"message": "上游地址不能为空"}})
            return
        if api_key is not None and not isinstance(api_key, str):
            self._send_json(400, {"error": {"message": "api_key 必须是字符串"}})
            return

        self.cfg.base_url = base_url
        self.cfg.model = model
        if api_key:
            self.cfg.api_key = api_key.strip()
            # 写到类属性上，后续请求的 handler 实例才能读到
            AgentHandler.key = self.cfg.api_key

        try:
            self._write_saved_config()
        except OSError as exc:
            self._send_json(500, {"error": {"message": f"保存配置失败：{exc}"}})
            return

        logger.info("配置已更新：%s / %s", base_url, model or "未指定模型")
        self._send_json(200, self._health_payload())

    def _write_saved_config(self) -> None:
        saved = self.config_dir / SAVED_NAME
        content = (
            "# 由游戏界面保存，可删除后重新生成。\n"
            f"base_url = {toml_string(self.cfg.base_url)}\n"
            f"model = {toml_string(self.cfg.model)}\n"
            f"api_key = {toml_string(self.cfg.api_key)}\n"
        )
        tmp = saved.with_name(saved.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, saved)

    # ------------------------------------------------------------ 测试
    def _handle_test(self) -> None:
        if self.cfg.passcode:
            if self.headers.get("X-Yap-Passcode", "") != self.cfg.passcode:
                self._send_json(401, {"error": {"message": "口令不正确"}})
                return
        if not self.cfg.base_url:
            self._send_json(
                400,
                {"error": {"message": "还没有配置模型地址，请在游戏里打开 AI 设置填写"}},
            )
            return
        if self._read_json_body() is None:
            return

        body: dict = {
            "messages": [{"role": "user", "content": "只回复 OK 两个字母"}],
            "stream": False,
            "max_tokens": 16,
        }
        if self.cfg.model:
            body["model"] = self.cfg.model
        req = self._upstream_request(body, "application/json")
        try:
            with urllib.request.urlopen(req, timeout=min(self.cfg.timeout_s, 30)) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            choice = (data.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            reply = str(message.get("content") or choice.get("text") or "").strip()
            self._send_json(200, {"ok": True, "reply": reply[:80]})
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            self._send_json(502, {"ok": False, "error": f"上游返回 {exc.code}：{detail}"})
        except Exception as exc:  # noqa: BLE001
            self._send_json(502, {"ok": False, "error": f"连不上上游：{exc}"})

    def _upstream_request(self, body: dict, accept: str) -> urllib.request.Request:
        headers = {"Content-Type": "application/json", "Accept": accept}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        return urllib.request.Request(
            self.cfg.base_url + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )

    # ------------------------------------------------------------ 透传
    def _proxy_once(self, req: urllib.request.Request) -> None:
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_s) as resp:
                status = resp.status
                content_type = resp.headers.get("Content-Type", "application/json")
                data = resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            content_type = exc.headers.get("Content-Type", "application/json")
            data = exc.read()
        except Exception as exc:  # noqa: BLE001
            self._send_json(502, {"error": {"message": f"上游调用失败：{exc}"}})
            return

        self.send_response(status)
        self._send_cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _proxy_stream(self, req: urllib.request.Request) -> None:
        try:
            resp = urllib.request.urlopen(req, timeout=self.cfg.timeout_s)
        except urllib.error.HTTPError as exc:
            data = exc.read()
            self.send_response(exc.code)
            self._send_cors()
            self.send_header(
                "Content-Type", exc.headers.get("Content-Type", "application/json")
            )
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        except Exception as exc:  # noqa: BLE001
            self._send_json(502, {"error": {"message": f"上游调用失败：{exc}"}})
            return

        with resp:
            self.send_response(resp.status)
            self._send_cors()
            self.send_header(
                "Content-Type",
                resp.headers.get("Content-Type", "text/event-stream; charset=utf-8"),
            )
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                while True:
                    chunk = resp.read(2048)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    )
    parser = argparse.ArgumentParser(description="文字角斗场 · 本地模型代理")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent / "config.toml"),
        help="配置文件路径（默认 agent/config.toml）",
    )
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    if not config_path.exists():
        print("[yap-agent] 没有找到配置文件，使用默认设置启动。")
        print("            在游戏界面右上角「AI 设置」里填写模型地址和密钥即可。")

    try:
        cfg = load_config(config_path)
    except (ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"[yap-agent] 配置有误：{exc}")
        sys.exit(1)

    key = resolve_key(cfg, config_path.parent)
    if not cfg.base_url:
        print("[yap-agent] 还没有配置模型地址，请在游戏界面里填写。")
    elif not key:
        print("[yap-agent] 警告：没有读到密钥，请在游戏界面里填写，或上游可能拒绝请求。")

    AgentHandler.cfg = cfg
    AgentHandler.key = key
    AgentHandler.config_dir = config_path.parent

    try:
        server = ThreadingHTTPServer(("127.0.0.1", cfg.port), AgentHandler)
    except OSError as exc:
        print(f"[yap-agent] 端口 {cfg.port} 绑定失败：{exc}")
        print("            可能已经有一个代理在运行，或端口被占用。")
        sys.exit(1)

    server.daemon_threads = True
    print("------------------------------------------")
    print(f"  yap-agent {VERSION}")
    print(f"  监听：   http://127.0.0.1:{cfg.port}")
    print("  上游：   " + (cfg.base_url or "（待配置）"))
    print(f"  模型：   {cfg.model or '（由请求指定）'}")
    print(f"  密钥：   {'已读取' if key else '未配置（可在游戏界面里填）'}")
    print(f"  白名单： {', '.join(cfg.allowed_origins)}")
    print("------------------------------------------")
    print("  Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[yap-agent] 已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
