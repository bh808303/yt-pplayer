"""Minimal asyncio client for a headless mpv controlled over its JSON IPC socket."""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import tempfile
from typing import Any, Callable


class Mpv:
    def __init__(self, on_event: Callable[[dict], None]) -> None:
        self._on_event = on_event
        self._proc: asyncio.subprocess.Process | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._sock = os.path.join(tempfile.gettempdir(), f"yt-pplayer-{os.getpid()}.sock")

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            "mpv", "--idle=yes", "--no-video", "--no-terminal", "--force-window=no",
            "--ytdl=no", "--cache=yes", "--demuxer-max-bytes=64MiB",
            f"--input-ipc-server={self._sock}",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        for _ in range(100):
            try:
                reader, self._writer = await asyncio.open_unix_connection(self._sock)
                break
            except OSError:
                await asyncio.sleep(0.05)
        else:
            raise RuntimeError("mpv did not open its IPC socket")
        asyncio.create_task(self._read(reader))
        for i, prop in enumerate(("time-pos", "duration", "pause", "volume", "core-idle"), 1):
            await self.command("observe_property", i, prop)

    async def _read(self, reader: asyncio.StreamReader) -> None:
        while line := await reader.readline():
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if "request_id" in msg and msg["request_id"] in self._pending:
                self._pending.pop(msg["request_id"]).set_result(msg)
            elif "event" in msg:
                self._on_event(msg)

    async def command(self, *args: Any) -> Any:
        if self._writer is None:
            return None
        rid = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        self._writer.write(json.dumps({"command": list(args), "request_id": rid}).encode() + b"\n")
        await self._writer.drain()
        msg = await asyncio.wait_for(fut, 5)
        return msg.get("data")

    async def play(self, url: str, headers: dict[str, str], title: str) -> None:
        # Shown by MPRIS (media keys, bar widget) instead of the raw stream URL.
        await self.command("set_property", "force-media-title", title)
        if ua := headers.get("User-Agent"):
            await self.command("set_property", "user-agent", ua)
        extra = [f"{k}: {v}" for k, v in headers.items() if k != "User-Agent"]
        await self.command("set_property", "http-header-fields", extra)
        await self.command("loadfile", url, "replace")
        await self.command("set_property", "pause", False)

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            try:
                await asyncio.wait_for(self.command("quit"), 1)
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), 2)
            except asyncio.TimeoutError:
                self._proc.kill()
        try:
            os.unlink(self._sock)
        except OSError:
            pass
