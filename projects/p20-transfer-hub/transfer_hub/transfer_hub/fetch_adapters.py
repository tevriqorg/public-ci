from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .aria2_rpc import Aria2RPC, Aria2RPCError
from .engines import EngineError, download

Progress = Callable[[float | None, int, int | None], None]
MetadataUpdate = Callable[[dict[str, Any]], None]


class FetchPaused(RuntimeError):
    pass


class FetchCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class FetchRequest:
    source: str
    output_dir: Path
    filename: str
    headers: dict[str, str]
    engine_options: dict[str, Any]
    cancelled: Callable[[], bool]
    progress: Progress
    message: Callable[[str], None] | None = None


class FetchAdapter(Protocol):
    name: str

    def fetch(self, request: FetchRequest, metadata: dict[str, Any], update_metadata: MetadataUpdate) -> list[Path]: ...
    def pause(self, metadata: dict[str, Any]) -> bool: ...
    def resume(self, metadata: dict[str, Any]) -> bool: ...
    def cancel(self, metadata: dict[str, Any]) -> None: ...


class SubprocessFetchAdapter:
    def __init__(self, name: str) -> None:
        self.name = name

    def fetch(self, request: FetchRequest, metadata: dict[str, Any], update_metadata: MetadataUpdate) -> list[Path]:
        result = download(
            self.name, request.source, request.output_dir, request.filename, request.headers,
            request.cancelled, request.progress, None, None, request.engine_options,
            message=request.message,
        )
        return result if isinstance(result, list) else [result]

    def pause(self, metadata: dict[str, Any]) -> bool:
        return False

    def resume(self, metadata: dict[str, Any]) -> bool:
        return False

    def cancel(self, metadata: dict[str, Any]) -> None:
        return None


class Aria2FetchAdapter:
    name = "aria2"

    def __init__(self, rpc: Aria2RPC, ensure_daemon: Callable[[], None]) -> None:
        self.rpc = rpc
        self.ensure_daemon = ensure_daemon

    @staticmethod
    def _gid(metadata: dict[str, Any]) -> str | None:
        value = metadata.get("engine_task_id")
        return str(value) if value else None

    def fetch(self, request: FetchRequest, metadata: dict[str, Any], update_metadata: MetadataUpdate) -> list[Path]:
        self.ensure_daemon()
        gid = self._gid(metadata)
        if not gid:
            request.output_dir.mkdir(parents=True, exist_ok=True)
            gid = self.rpc.add_uri(request.source, request.output_dir, request.filename, request.headers)
            update_metadata({"engine_task_id": gid})
        try:
            path = self.rpc.wait(gid, progress=request.progress, cancelled=request.cancelled)
        except Aria2RPCError as exc:
            if str(exc) == "paused":
                raise FetchPaused() from exc
            if str(exc) == "cancelled" or request.cancelled():
                raise FetchCancelled() from exc
            raise EngineError(str(exc)) from exc
        return [path]

    def pause(self, metadata: dict[str, Any]) -> bool:
        gid = self._gid(metadata)
        if not gid:
            return False
        self.ensure_daemon()
        self.rpc.pause(gid)
        return True

    def resume(self, metadata: dict[str, Any]) -> bool:
        gid = self._gid(metadata)
        if not gid:
            return False
        self.ensure_daemon()
        self.rpc.unpause(gid)
        return True

    def cancel(self, metadata: dict[str, Any]) -> None:
        gid = self._gid(metadata)
        if not gid:
            return
        self.ensure_daemon()
        self.rpc.remove(gid)


class FetchAdapterRegistry:
    def __init__(self, aria2: Aria2FetchAdapter) -> None:
        self._adapters: dict[str, FetchAdapter] = {"aria2": aria2}
        for name in ("yt-dlp", "gallery-dl", "nm3u8dl", "uav"):
            self._adapters[name] = SubprocessFetchAdapter(name)

    def get(self, name: str) -> FetchAdapter:
        try:
            return self._adapters[name]
        except KeyError as exc:
            raise EngineError(f"unsupported fetch engine: {name}") from exc
