"""Messages exchanged between the Chrome extension and the orchestrator.

Each message is a frozen dataclass whose class attribute `type` is its name on the wire.
encode() turns a message into a JSON text frame. decode() turns a frame back into a
message and raises ProtocolError for anything it does not accept.
"""

import json
from dataclasses import asdict, dataclass
from typing import ClassVar, get_args


class ProtocolError(ValueError):
    """A frame is not a valid protocol message."""


# extension -> orchestrator


@dataclass(frozen=True)
class Hello:
    type: ClassVar[str] = "hello"
    client: str
    version: str


@dataclass(frozen=True)
class State:
    type: ClassVar[str] = "state"
    video_id: str
    current_time: float
    rate: float
    paused: bool
    live: bool
    volume: float


@dataclass(frozen=True)
class Captions:
    type: ClassVar[str] = "captions"
    video_id: str
    lang: str
    kind: str
    json3: dict


@dataclass(frozen=True)
class Start:
    type: ClassVar[str] = "start"
    mode: str | None


@dataclass(frozen=True)
class Stop:
    type: ClassVar[str] = "stop"


@dataclass(frozen=True)
class Seek:
    type: ClassVar[str] = "seek"
    current_time: float


# orchestrator -> extension


@dataclass(frozen=True)
class Status:
    type: ClassVar[str] = "status"
    mode: str
    state: str
    buffered_ahead: float
    lag: float
    engine: str
    message: str


@dataclass(frozen=True)
class Subtitle:
    type: ClassVar[str] = "subtitle"
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class Duck:
    type: ClassVar[str] = "duck"
    volume: float | None  # None means restore


@dataclass(frozen=True)
class Control:
    type: ClassVar[str] = "control"
    action: str
    value: float | None


Message = (
    Hello | State | Captions | Start | Stop | Seek | Status | Subtitle | Duck | Control
)

MESSAGE_TYPES: dict[str, type[Message]] = {cls.type: cls for cls in get_args(Message)}

# The values a field may take. Fields not listed here take any value.
_CHOICES: dict[str, dict[str, tuple[object, ...]]] = {
    "start": {"mode": ("captions", "live", None)},
    "captions": {"kind": ("manual", "asr", "translated")},
    "control": {"action": ("pause", "play", "rate")},
}


def encode(msg: Message) -> str:
    """Serialize a message as a JSON object whose first key is its type."""
    return json.dumps({"type": msg.type, **asdict(msg)})


def decode(text: str) -> Message:
    """Parse one JSON text frame into the message it carries."""
    # json.loads raises ValueError for bad text (JSONDecodeError is one) and
    # RecursionError for nesting too deep to parse.
    try:
        payload = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise ProtocolError(f"not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("message is not a JSON object")
    if "type" not in payload:
        raise ProtocolError("message has no type")
    type_name = payload.pop("type")
    # Test for str first: an unhashable value such as a list cannot be a dict key.
    if not isinstance(type_name, str) or type_name not in MESSAGE_TYPES:
        raise ProtocolError(f"unknown message type {type_name!r}")
    try:
        msg = MESSAGE_TYPES[type_name](**payload)
    except TypeError as exc:
        raise ProtocolError(f"bad {type_name} message: {exc}") from exc
    for field_name, allowed in _CHOICES.get(type_name, {}).items():
        value = getattr(msg, field_name)
        if value not in allowed:
            raise ProtocolError(f"{type_name}.{field_name} {value!r} is not an allowed value")
    return msg
