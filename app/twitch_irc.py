"""Anonymous read-only Twitch chat reader (IRC over TLS).

Twitch lets anyone read a public chat without a token by logging in as ``justinfanNNNNN``.
With the ``twitch.tv/tags`` capability every PRIVMSG carries IRCv3 tags, including
``custom-reward-id`` when the message is the text a viewer typed while redeeming a channel
points reward. That is enough to turn "redeem *Добавить DPI* and type 1600" into an event,
with no Twitch application, OAuth or EventSub.

The reader runs in a daemon thread, reconnects with backoff and never writes to chat.
"""
from __future__ import annotations

import random
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

HOST, PORT = "irc.chat.twitch.tv", 6697


@dataclass
class IrcMessage:
    tags: dict[str, str]
    prefix: str
    command: str
    params: list[str]
    trailing: str

    @property
    def user(self) -> str:
        return self.prefix.split("!", 1)[0] if self.prefix else ""

    @property
    def display_name(self) -> str:
        return self.tags.get("display-name") or self.user

    @property
    def reward_id(self) -> str:
        return self.tags.get("custom-reward-id", "")

    @property
    def is_mod(self) -> bool:
        return self.tags.get("mod") == "1" or "broadcaster/" in self.tags.get("badges", "")


def _unescape_tag(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append({"s": " ", ":": ";", "r": "\r", "n": "\n", "\\": "\\"}.get(nxt, nxt))
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def parse_line(line: str) -> IrcMessage | None:
    """Parse one raw IRC line (IRCv3 tags + prefix + command + params + trailing)."""
    line = line.rstrip("\r\n")
    if not line:
        return None
    tags: dict[str, str] = {}
    if line.startswith("@"):
        raw, _, line = line.partition(" ")
        for item in raw[1:].split(";"):
            k, _, v = item.partition("=")
            tags[k] = _unescape_tag(v)
    prefix = ""
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
    trailing = ""
    if " :" in line:
        line, _, trailing = line.partition(" :")
    elif line.startswith(":"):
        line, trailing = "", line[1:]
    parts = line.split()
    if not parts:
        return None
    return IrcMessage(tags=tags, prefix=prefix, command=parts[0].upper(), params=parts[1:], trailing=trailing)


class TwitchChatReader:
    """Reads #channel anonymously and calls ``on_message(IrcMessage)`` for every PRIVMSG."""

    def __init__(self, on_message: Callable[[IrcMessage], None], on_status: Callable[[], None] | None = None) -> None:
        self.on_message = on_message
        self.on_status = on_status
        self.channel: str = ""
        self.connected = False
        self.last_error: str | None = None
        self.messages = 0
        self.connected_at: float | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._sock: ssl.SSLSocket | None = None
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- control
    def status(self) -> dict:
        return {"channel": self.channel, "connected": self.connected, "last_error": self.last_error,
                "messages": self.messages, "connected_at": self.connected_at,
                "running": bool(self._thread and self._thread.is_alive())}

    def start(self, channel: str) -> None:
        channel = (channel or "").strip().lstrip("#").lower()
        with self._lock:
            if self._thread and self._thread.is_alive() and self.channel == channel:
                return
            self.stop()
            if not channel:
                return
            self.channel = channel
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="twitch-irc", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        sock = self._sock
        if sock:
            try:
                sock.close()
            except OSError:
                pass
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._thread = None
        self.connected = False
        self._notify()

    def _notify(self) -> None:
        if self.on_status:
            try:
                self.on_status()
            except Exception:
                pass

    # ------------------------------------------------------------------- loop
    def _run(self) -> None:
        backoff = 3.0
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 3.0
            except Exception as e:  # noqa: BLE001 - network errors of all kinds
                self.last_error = str(e) or e.__class__.__name__
                self.connected = False
                self._notify()
            if self._stop.is_set():
                break
            self._stop.wait(backoff)
            backoff = min(60.0, backoff * 2)

    def _session(self) -> None:
        ctx = ssl.create_default_context()
        raw = socket.create_connection((HOST, PORT), timeout=20)
        sock = ctx.wrap_socket(raw, server_hostname=HOST)
        sock.settimeout(300)  # Twitch pings every ~5 minutes; a silent socket means we are gone
        self._sock = sock
        nick = f"justinfan{random.randint(10000, 99999)}"
        sock.sendall(b"CAP REQ :twitch.tv/tags twitch.tv/commands\r\n")
        sock.sendall(f"PASS SCHMOOPIIE\r\nNICK {nick}\r\nJOIN #{self.channel}\r\n".encode())
        buf = b""
        try:
            while not self._stop.is_set():
                data = sock.recv(4096)
                if not data:
                    raise ConnectionError("соединение с чатом закрыто")
                buf += data
                while b"\r\n" in buf:
                    line, buf = buf.split(b"\r\n", 1)
                    self._handle(sock, line.decode("utf-8", "replace"))
        finally:
            self._sock = None
            try:
                sock.close()
            except OSError:
                pass
            self.connected = False
            self._notify()

    def _handle(self, sock: ssl.SSLSocket, line: str) -> None:
        msg = parse_line(line)
        if msg is None:
            return
        if msg.command == "PING":
            sock.sendall(f"PONG :{msg.trailing or 'tmi.twitch.tv'}\r\n".encode())
            return
        if msg.command == "RECONNECT":
            raise ConnectionError("Twitch попросил переподключиться")
        if msg.command == "NOTICE" and "authentication failed" in msg.trailing.lower():
            raise ConnectionError(f"Twitch: {msg.trailing}")
        if msg.command == "JOIN" and msg.user.startswith("justinfan"):
            self.connected = True
            self.connected_at = time.time()
            self.last_error = None
            self._notify()
            return
        if msg.command == "PRIVMSG":
            self.messages += 1
            try:
                self.on_message(msg)
            except Exception:
                pass
