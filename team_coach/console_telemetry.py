"""Read bot snapshots from the local Dota console log."""

import json
from os import fstat
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

from .service import MAX_BODY


_SNAPSHOT = b"[VScript] [team-coach-snapshot] "
_LINE_TIME = re.compile(rb"^\d{2}/\d{2} \d{2}:\d{2}:\d{2} ")
_CHUNK_LIMIT = 600
_MAX_CHUNKS = (MAX_BODY + _CHUNK_LIMIT - 1) // _CHUNK_LIMIT
MAILBOX_NAME = "team_coach_replies.lua"
_REQUEST_ID = re.compile(r"[0-9]+-[0-9]+")
_MAX_REPLY = 4096
_MATCH_END = (
    b"S:Gamerules: entering state 'DOTA_GAMERULES_STATE_POST_GAME'",
    b"[Server] S:Gamerules: entering state 'DOTA_GAMERULES_STATE_POST_GAME'",
    b"[Server] SV:  Server shutting down:",
)


def team_mailbox_name(team):
    return f"team_coach_replies_{team}.lua"


def _lua_string(value):
    escaped = (
        chr(byte) if 32 <= byte < 127 and byte not in (34, 92) else f"\\{byte:03d}"
        for byte in value.encode("utf-8")
    )
    return '"' + "".join(escaped) + '"'


class LuaMailbox:
    """Publish bounded replies for the most recent request from each bot."""

    def __init__(self, path):
        self.path = path
        self._replies = {}
        self._session = None
        self._dirty = True

    def clear(self):
        self._replies.clear()
        self._session = None
        self._dirty = True

    def queue(self, packet, reply):
        request_id = packet.get("request_id")
        if not isinstance(request_id, str) or len(request_id) > 32 or not _REQUEST_ID.fullmatch(request_id):
            return
        body = json.dumps(reply, ensure_ascii=False, separators=(",", ":"))
        if len(body.encode("utf-8")) > _MAX_REPLY:
            return
        if self._session != reply["session_id"]:
            self.clear()
            self._session = reply["session_id"]
        self._replies[packet["bot"]["id"]] = (_lua_string(request_id), _lua_string(body))
        self._dirty = True

    def flush(self):
        if not self._dirty:
            return
        temporary = None
        try:
            with NamedTemporaryFile(
                dir=self.path.parent, prefix="coach-replies-", suffix=".tmp",
                mode="w", encoding="ascii", delete=False,
            ) as output:
                temporary = Path(output.name)
                output.write("return {\n")
                for bot_id, (request_id, body) in sorted(self._replies.items()):
                    output.write(f"    [{bot_id}] = {{{request_id}, {body}}},\n")
                output.write("}\n")
            temporary.replace(self.path)
        except OSError:
            # A short-lived file lock in the bot VM can delay replacement on Windows.
            return
        else:
            self._dirty = False
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass


class ConsoleTelemetry:
    def __init__(self, path, mailbox=None):
        self.path = path
        self.mailbox = mailbox
        self._identity = None
        self._offset = 0
        self._parts = []
        self._count = 0
        try:
            stat = path.stat()
        except FileNotFoundError:
            pass
        else:
            self._identity = (stat.st_dev, stat.st_ino)
            self._offset = stat.st_size

    def _clear_packet(self):
        self._parts.clear()
        self._count = 0

    def _packet(self, fragment):
        header, _, chunk = fragment.partition(b" ")
        index, _, count = header.partition(b"/")
        if not index.isdigit() or not count.isdigit() or len(header) > 5:
            self._clear_packet()
            return None
        index, count = int(index), int(count)
        if index == 1:
            self._clear_packet()
            self._count = count
        if (
            not 1 <= index <= count <= _MAX_CHUNKS
            or index != len(self._parts) + 1 or count != self._count
            or not 0 < len(chunk) <= _CHUNK_LIMIT
        ):
            self._clear_packet()
            return None
        self._parts.append(chunk)
        if index != count:
            return None
        payload = b"".join(self._parts)
        self._clear_packet()
        if len(payload) > MAX_BODY:
            return None
        try:
            return json.loads(payload)
        except (ValueError, RecursionError):
            return None

    def ingest(self, service):
        mailboxes = {service.team: self.mailbox} if self.mailbox is not None else {}
        self.ingest_sides({service.team: service}, mailboxes)

    def ingest_sides(self, services, mailboxes):
        mailboxes = {side: box for side, box in mailboxes.items() if services[side].mode != "shadow"}
        try:
            source = self.path.open("rb")
        except FileNotFoundError:
            for mailbox in mailboxes.values():
                mailbox.flush()
            return
        with source:
            stat = fstat(source.fileno())
            identity = (stat.st_dev, stat.st_ino)
            if identity != self._identity or stat.st_size < self._offset:
                if self._identity is not None:
                    for service in services.values():
                        service.end_match()
                self._identity = identity
                self._offset = 0
                self._clear_packet()
                for mailbox in mailboxes.values():
                    mailbox.clear()
            source.seek(self._offset)
            while line := source.readline():
                if not line.endswith(b"\n"):
                    break
                self._offset = source.tell()
                message = _LINE_TIME.sub(b"", line, count=1)
                if message.startswith(_MATCH_END):
                    for service in services.values():
                        service.observe()
                        service.end_match()
                    self._clear_packet()
                    for mailbox in mailboxes.values():
                        mailbox.clear()
                elif message.startswith(_SNAPSHOT):
                    packet = self._packet(message[len(_SNAPSHOT):].rstrip(b"\r\n"))
                    team = packet.get("team") if isinstance(packet, dict) else None
                    service = services.get(team) if isinstance(team, str) else None
                    if service is None:
                        continue
                    try:
                        reply = service.exchange(packet)
                    except ValueError:
                        continue
                    if team in mailboxes:
                        mailboxes[team].queue(packet, reply)
        for mailbox in mailboxes.values():
            mailbox.flush()
