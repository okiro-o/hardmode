"""OBS: reload the widget browser sources through OBS's built-in WebSocket server (obs-websocket 5, OBS 28+).

Why: a browser source whose page failed to load (OBS started before the app, the server was not
listening yet) stays blank for good — CEF never retries. A page that did load reconnects by itself
(EventSource), so only the start-up order is a problem. When the app starts, it asks OBS to «refresh
cache of current page» for every browser source whose URL points at this app (user's report 2026-11-08).

Protocol (https://github.com/obsproject/obs-websocket/blob/master/docs/generated/protocol.md):
server ``Hello`` (op 0, optional ``authentication {challenge, salt}``) → client ``Identify`` (op 1,
``rpcVersion`` 1, ``authentication`` = b64(sha256(b64(sha256(password + salt)) + challenge))) →
``Identified`` (op 2) → ``Request`` (op 6) / ``RequestResponse`` (op 7). Close code 4009 = wrong password.
Requests used: ``GetInputList {inputKind: browser_source}``, ``GetInputSettings {inputName}`` (``url``,
``is_local_file``), ``PressInputPropertiesButton {inputName, propertyName: "refreshnocache"}``.
Blocking (websockets' sync client); called from a worker thread or an executor.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import socket
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable
from urllib.parse import urlsplit

from . import config

log = logging.getLogger("hardmode.obs")
DEFAULT_PORT = 4455


@dataclass
class ObsSettings:
    refresh_on_start: bool = False      # reload the widget sources in OBS when the app starts
    port: int = DEFAULT_PORT
    password: str = ""                  # secret: obs-websocket server password

    @classmethod
    def from_dict(cls, d: dict | None) -> "ObsSettings":
        s = cls()
        config.assign_fields(s, d, "obs")
        s.refresh_on_start = config.to_bool(s.refresh_on_start)
        try:
            s.port = int(float(s.port))
        except (TypeError, ValueError):
            raise ValueError("port: нужно число") from None
        if not 1 <= s.port <= 65535:
            raise ValueError("port: от 1 до 65535")
        s.password = s.password.strip()[:200] if isinstance(s.password, str) else ""
        return s

    def to_dict(self) -> dict:
        return asdict(self)

    def public_dict(self) -> dict:
        d = asdict(self)
        d["password_set"] = bool(d.pop("password"))
        return d


class ObsError(RuntimeError):
    def __init__(self, message: str, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind                 # "offline" (nothing listens on the port) | "auth" | "error"


def auth_string(password: str, salt: str, challenge: str) -> str:
    secret = base64.b64encode(hashlib.sha256((password + salt).encode("utf-8")).digest()).decode("ascii")
    return base64.b64encode(hashlib.sha256((secret + challenge).encode("utf-8")).digest()).decode("ascii")


def is_our_url(url: str, app_port: int) -> bool:
    """True for http(s)://127.0.0.1:<port>/… and http(s)://localhost:<port>/…"""
    try:
        u = urlsplit(str(url or "").strip())
        return u.scheme in ("http", "https") and (u.hostname or "").lower() in ("127.0.0.1", "localhost") \
            and u.port == app_port
    except ValueError:
        return False


class ObsSession:
    """One short connection: identify, run requests, close."""

    def __init__(self, port: int, password: str, timeout: float = 4.0) -> None:
        self.port, self.password, self.timeout = port, password, timeout
        self.ws = None
        self._cm = None
        self._seq = 0

    def __enter__(self) -> "ObsSession":
        from websockets.exceptions import ConnectionClosed, InvalidHandshake
        from websockets.sync.client import connect
        try:
            # entered by hand (not ``with``): websockets 17 wants the context-manager protocol, 15 accepts it too
            self._cm = connect(f"ws://127.0.0.1:{self.port}", open_timeout=self.timeout, close_timeout=1,
                               compression=None, proxy=None, ping_interval=None)
            self.ws = self._cm.__enter__()
        except (OSError, InvalidHandshake, TimeoutError) as e:
            self._cm = None
            raise ObsError("OBS не отвечает: он не запущен или в нём выключен WebSocket-сервер "
                           f"(порт {self.port})", "offline") from e
        try:
            hello = self._recv()
            if hello.get("op") != 0:
                raise ObsError("OBS ответил не по протоколу obs-websocket 5")
            ident: dict = {"rpcVersion": 1, "eventSubscriptions": 0}
            auth = (hello.get("d") or {}).get("authentication")
            if auth:
                if not self.password:
                    raise ObsError("в OBS включён пароль WebSocket-сервера: вставьте его в Hardmode", "auth")
                ident["authentication"] = auth_string(self.password, auth.get("salt", ""), auth.get("challenge", ""))
            self.ws.send(json.dumps({"op": 1, "d": ident}))
            if self._recv().get("op") != 2:
                raise ObsError("OBS не принял подключение")
        except ConnectionClosed as e:
            self.close()
            code = e.rcvd.code if e.rcvd else None
            if code == 4009:
                raise ObsError("OBS: неверный пароль WebSocket-сервера", "auth") from None
            raise ObsError(f"OBS закрыл соединение (код {code})") from None
        except ObsError:
            self.close()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self._cm is not None:
            try:
                self._cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        self._cm = self.ws = None

    def _recv(self) -> dict:
        try:
            raw = self.ws.recv(timeout=self.timeout)
        except TimeoutError:
            raise ObsError("OBS не ответил вовремя") from None
        try:
            msg = json.loads(raw)
        except ValueError:
            raise ObsError("OBS прислал не JSON") from None
        return msg if isinstance(msg, dict) else {}

    def request(self, request_type: str, data: dict | None = None) -> dict:
        from websockets.exceptions import ConnectionClosed
        self._seq += 1
        rid = f"hm-{self._seq}"
        msg = {"op": 6, "d": {"requestType": request_type, "requestId": rid}}
        if data is not None:
            msg["d"]["requestData"] = data
        try:
            self.ws.send(json.dumps(msg))
            while True:
                reply = self._recv()
                d = reply.get("d") or {}
                if reply.get("op") == 7 and d.get("requestId") == rid:
                    break
        except ConnectionClosed:
            raise ObsError("OBS закрыл соединение") from None
        status = d.get("requestStatus") or {}
        if not status.get("result"):
            raise ObsError(f"OBS: {request_type}: {status.get('comment') or status.get('code')}")
        return d.get("responseData") or {}


def refresh_widgets(port: int, password: str, app_port: int, timeout: float = 4.0) -> dict:
    """Reload every browser source showing this app. Returns {refreshed: [names], browser_sources: n}."""
    with ObsSession(port, password, timeout) as obs:
        inputs = obs.request("GetInputList", {"inputKind": "browser_source"}).get("inputs") or []
        refreshed = []
        for it in inputs:
            name = it.get("inputName")
            if not name:
                continue
            settings = obs.request("GetInputSettings", {"inputName": name}).get("inputSettings") or {}
            if settings.get("is_local_file") or not is_our_url(settings.get("url", ""), app_port):
                continue
            obs.request("PressInputPropertiesButton", {"inputName": name, "propertyName": "refreshnocache"})
            refreshed.append(name)
    return {"refreshed": refreshed, "browser_sources": len(inputs)}


def wait_for_port(port: int, timeout: float = 20.0) -> bool:
    """Block until 127.0.0.1:<port> accepts connections (our own server finished starting)."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.25)
    return False


class ObsService:
    def __init__(self, settings: ObsSettings, app_port: Callable[[], int],
                 publish_threadsafe: Callable[[str, dict], None]) -> None:
        self.settings = settings
        self._app_port = app_port
        self._publish = publish_threadsafe
        self._lock = threading.Lock()
        self.last: dict = {}                 # {t, ok, text, refreshed}

    def update_settings(self, patch: dict) -> None:
        patch = dict(patch or {})
        merged = {**self.settings.to_dict(), **patch}
        if "password" not in patch:          # absent or "password_set" keeps the stored secret
            merged["password"] = self.settings.password
        self.settings = ObsSettings.from_dict(merged)

    def status(self) -> dict:
        return {"settings": self.settings.public_dict(), "last": dict(self.last)}

    def refresh(self, reason: str = "manual") -> dict:
        """Blocking. Never raises: the result goes to ``last`` and is published as the ``obs`` event."""
        with self._lock:
            s = self.settings
            try:
                res = refresh_widgets(s.port, s.password, self._app_port())
                n = len(res["refreshed"])
                text = (f"обновлено источников: {n} ({', '.join(res['refreshed'])})" if n else
                        "в OBS нет источников-браузеров с адресом Hardmode")
                self.last = {"t": time.time(), "ok": True, "text": text, "refreshed": res["refreshed"], "reason": reason}
            except ObsError as e:
                if reason == "start" and e.kind == "offline":
                    # the app was started first: the widgets load by themselves when OBS opens
                    self.last = {"t": time.time(), "ok": True, "refreshed": [], "reason": reason,
                                 "text": "OBS ещё не был запущен: виджеты загрузятся сами, когда он откроется"}
                else:
                    self.last = {"t": time.time(), "ok": False, "text": str(e), "refreshed": [], "reason": reason}
            except Exception as e:  # noqa: BLE001 - a broken OBS must never break the app
                log.exception("obs refresh failed")
                self.last = {"t": time.time(), "ok": False, "text": f"{type(e).__name__}: {e}", "refreshed": [],
                             "reason": reason}
            log.info("obs refresh (%s): %s", reason, self.last["text"])
            out = dict(self.last)
        self._publish("obs", self.status())
        return out

    def start(self) -> None:
        """At app start: once our server listens, reload the widget sources (OBS may have been first)."""
        if not self.settings.refresh_on_start:
            return

        def run() -> None:
            if wait_for_port(self._app_port()):
                time.sleep(0.5)
                self.refresh("start")
        threading.Thread(target=run, name="obs-refresh", daemon=True).start()
