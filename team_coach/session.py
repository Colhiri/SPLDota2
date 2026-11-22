"""Persistent, isolated coaching conversations over a local stdio server."""

from collections import deque
import json
import os
from pathlib import Path
from queue import Empty, Queue
import shutil
import subprocess
from subprocess import Popen
from threading import Lock, Thread
from time import monotonic
import tomllib


class SessionError(RuntimeError):
    pass


def _isolated_connections(config_path=None):
    """Game snapshots are the sole input; unrelated local servers must not delay turns."""
    config_path = (Path(config_path) if config_path is not None else
                   Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "config.toml")
    if not config_path.is_file():
        return []
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise SessionError("Could not read session connection settings") from error
    names = config.get("mcp_servers", {})
    if any(not name.replace("_", "").replace("-", "").isalnum() or not name.isascii() for name in names):
        raise SessionError("Unsupported external connection name in session configuration")
    return [f"mcp_servers.{name}.enabled=false" for name in names]


class CoachSession:
    def __init__(self, directory, *, model, effort, instructions, executable=None, timeout=30):
        self.directory = Path(directory).resolve()
        self.model, self.effort = model, effort
        self.instructions, self.executable = instructions, executable
        self.timeout = timeout
        self.process = None
        self.thread_id = None
        self.match_id = None
        self.turn_id = None
        self._counter = 0
        self._inbox = Queue()
        self._notifications = deque()
        self._lock = Lock()
        self._errors = deque(maxlen=8)
        self._connection_live = False
        self._last_health = None
        self._last_error = None

    def status(self):
        age = monotonic() - self._last_health if self._last_health is not None else None
        return {
            "id": self.thread_id, "turn_id": self.turn_id, "match_id": self.match_id,
            "ready": (self.process is not None and self.process.poll() is None and self._connection_live
                      and self.thread_id is not None and age is not None and age < 40),
            "heartbeat_age_s": round(age, 2) if age is not None else None,
            "last_error": self._last_error,
            "model": self.model, "effort": self.effort,
        }

    def _read(self, stream, inbox):
        try:
            for line in stream:
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    inbox.put(message)
        finally:
            stream.close()
            if inbox is self._inbox:
                self._connection_live = False
            inbox.put({"transport_closed": True})

    def _read_errors(self, stream):
        try:
            for line in stream:
                self._errors.append(line.strip()[-400:])
        finally:
            stream.close()

    def _send(self, message):
        try:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as error:
            raise SessionError("Coaching session disconnected") from error

    def _receive(self, deadline):
        try:
            message = self._inbox.get(timeout=max(0, deadline - monotonic()))
        except Empty as error:
            raise SessionError("Coaching session timed out") from error
        if message.get("transport_closed"):
            raise SessionError("Coaching session closed: " + " ".join(self._errors)[-600:])
        if "id" in message and "method" in message:
            # Coaching uses only the supplied snapshot and a structured response.
            self._send({"id": message["id"], "error": {"code": -32601, "message": "Tools are unavailable"}})
        return message

    def _request(self, method, params, deadline):
        self._counter += 1
        request_id = self._counter
        self._send({"id": request_id, "method": method, "params": params})
        while True:
            message = self._receive(deadline)
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise SessionError(str(message["error"].get("message", "Session request failed"))[:600])
                self._last_health = monotonic()
                self._last_error = None
                return message.get("result", {})
            if "method" in message and "id" not in message:
                self._notifications.append(message)

    def _save(self):
        temporary = self.directory / "session.tmp"
        temporary.write_text(json.dumps(self.status(), ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.directory / "session.json")

    def _start(self, deadline):
        executable = shutil.which(self.executable or "codex.exe") or shutil.which(self.executable or "codex")
        if not executable:
            raise SessionError("Session executable was not found")
        self.directory.mkdir(parents=True, exist_ok=True)
        self._inbox = Queue()
        self._connection_live = True
        self._notifications.clear()
        self._errors.clear()
        command = [executable]
        for feature in ("shell_tool", "apps", "plugins", "in_app_browser", "hooks"):
            command.extend(("--disable", feature))
        for setting in ("web_search=disabled", "skills.include_instructions=false", "include_environment_context=false",
                        *_isolated_connections()):
            command.extend(("-c", setting))
        command.extend(("app-server", "--listen", "stdio://"))
        self.process = Popen(
            command, cwd=self.directory, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        Thread(target=self._read, args=(self.process.stdout, self._inbox), daemon=True).start()
        Thread(target=self._read_errors, args=(self.process.stderr,), daemon=True).start()
        self._request("initialize", {"clientInfo": {"name": "dota_team_coach", "version": "3.0"}}, deadline)
        self._send({"method": "initialized", "params": {}})
        if self.thread_id:
            self._request("thread/resume", {"threadId": self.thread_id, "excludeTurns": True}, deadline)

    def _open_thread(self, deadline):
        result = self._request("thread/start", {
            "model": self.model, "cwd": str(self.directory), "approvalPolicy": "never",
            "sandbox": "read-only", "ephemeral": False, "baseInstructions": self.instructions,
        }, deadline)
        self.thread_id = result["thread"]["id"]
        self.turn_id = None
        self._save()

    def prepare(self):
        with self._lock:
            deadline = monotonic() + self.timeout
            if self.process is None or self.process.poll() is not None:
                self._start(deadline)
            if not self.thread_id:
                self._open_thread(deadline)
            return self.status()

    def maintain(self):
        """Probe the actual session periodically, including while no match is running."""
        with self._lock:
            try:
                deadline = monotonic() + min(self.timeout, 10)
                if self.process is None or self.process.poll() is not None or not self._connection_live:
                    self.close()
                    self._start(deadline)
                if not self.thread_id:
                    self._open_thread(deadline)
                if self._last_health is None or monotonic() - self._last_health >= 10:
                    self._request("thread/read", {"threadId": self.thread_id, "includeTurns": False}, deadline)
                return self.status()
            except (OSError, SessionError) as error:
                self._last_error = str(error)[:600]
                self.close()
                raise

    def consult(self, snapshot, schema, *, match_id):
        with self._lock:
            deadline = monotonic() + self.timeout
            if self.process is None or self.process.poll() is not None:
                self._start(deadline)
            if not self.thread_id or self.match_id is not None and self.match_id != match_id:
                self._open_thread(deadline)
            self.match_id = match_id
            try:
                result = self._request("turn/start", {
                    "threadId": self.thread_id, "effort": self.effort, "outputSchema": schema,
                    "input": [{"type": "text", "text": snapshot}],
                }, deadline)
            except (OSError, SessionError):
                self.close()
                raise
            self.turn_id = result["turn"]["id"]
            self._save()
            text, usage = "", {}
            try:
                while True:
                    message = self._notifications.popleft() if self._notifications else self._receive(deadline)
                    params = message.get("params", {})
                    if params.get("threadId") != self.thread_id:
                        continue
                    method = message.get("method")
                    if method == "thread/tokenUsage/updated" and params.get("turnId") == self.turn_id:
                        last = params.get("tokenUsage", {}).get("last", {})
                        for field, key in (
                            ("inputTokens", "input_tokens"), ("cachedInputTokens", "cached_input_tokens"),
                            ("outputTokens", "output_tokens"), ("reasoningOutputTokens", "reasoning_output_tokens"),
                        ):
                            if type(last.get(field)) is int:
                                usage[key] = last[field]
                    elif method == "item/completed" and params.get("turnId") == self.turn_id:
                        item = params.get("item", {})
                        if item.get("type") == "agentMessage":
                            text = item.get("text", "")
                    elif method == "turn/completed" and params.get("turn", {}).get("id") == self.turn_id:
                        turn = params["turn"]
                        if turn.get("status") != "completed":
                            error = turn.get("error") or {}
                            raise SessionError(error.get("message") or "Coaching turn did not complete")
                        for item in turn.get("items", []):
                            if item.get("type") == "agentMessage":
                                text = item.get("text", text)
                        if not text:
                            raise SessionError("Coaching turn returned no decision")
                        return json.loads(text), usage
            except (SessionError, ValueError):
                # Retire a timed-out process so a new request cannot queue behind a stuck turn.
                self.close()
                raise

    def close(self):
        self._connection_live = False
        process, self.process = self.process, None
        if process is not None:
            if process.poll() is None:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                else:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            if process.stdin:
                process.stdin.close()
