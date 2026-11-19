# tests/test_protocol.py
import json

import pytest

from dubber.protocol import (
    MESSAGE_TYPES, Captions, Control, Duck, Hello, ProtocolError, Seek, Start, State,
    Status, Stop, Subtitle, decode, encode,
)

def test_roundtrip_state():
    msg = State(video_id="XP5k0bHmhqc", current_time=61.88, rate=1.0, paused=False, live=False, volume=0.8)
    assert decode(encode(msg)) == msg
    assert json.loads(encode(msg))["type"] == "state"

def test_decode_captions_keeps_json3_dict():
    text = json.dumps({"type": "captions", "video_id": "v", "lang": "en", "kind": "asr", "json3": {"events": []}})
    assert decode(text) == Captions(video_id="v", lang="en", kind="asr", json3={"events": []})

def test_duck_restore_and_control():
    assert json.loads(encode(Duck(volume=None)))["volume"] is None
    assert decode(encode(Control(action="rate", value=1.25))) == Control(action="rate", value=1.25)

def test_unknown_type_and_missing_field_raise():
    with pytest.raises(ProtocolError):
        decode('{"type": "nope"}')
    with pytest.raises(ProtocolError):
        decode('{"type": "seek"}')

# Wire-format rules: round trips, key order, and what decode accepts or rejects.

SAMPLES = [
    Hello(client="chrome", version="1.0"),
    State(video_id="v", current_time=1.5, rate=1.0, paused=True, live=False, volume=0.5),
    Captions(video_id="v", lang="tr", kind="translated", json3={"events": []}),
    Start(mode="live"),
    Stop(),
    Seek(current_time=12.0),
    Status(mode="captions", state="playing", buffered_ahead=2.5, lag=0.4, engine="say", message=""),
    Subtitle(text="Merhaba", start=1.0, end=2.0),
    Duck(volume=0.2),
    Control(action="pause", value=None),
]

@pytest.mark.parametrize("msg", SAMPLES)
def test_every_message_round_trips(msg):
    assert decode(encode(msg)) == msg

def test_protocol_error_is_a_value_error():
    assert issubclass(ProtocolError, ValueError)

def test_encode_puts_type_first_then_fields_in_order():
    msg = State(video_id="v", current_time=1.5, rate=1.0, paused=True, live=False, volume=0.5)
    assert list(json.loads(encode(msg))) == [
        "type", "video_id", "current_time", "rate", "paused", "live", "volume",
    ]

def test_stop_encodes_as_type_only():
    assert encode(Stop()) == '{"type": "stop"}'

def test_message_types_names_every_message():
    assert MESSAGE_TYPES == {
        "hello": Hello, "state": State, "captions": Captions, "start": Start, "stop": Stop,
        "seek": Seek, "status": Status, "subtitle": Subtitle, "duck": Duck, "control": Control,
    }

@pytest.mark.parametrize("text", [
    '{"type": "stop"',  # not JSON
    "[]",               # not an object
    '"stop"',
    "null",
])
def test_decode_rejects_malformed_documents(text):
    with pytest.raises(ProtocolError):
        decode(text)

@pytest.mark.parametrize("text", ["[" * 100_000, "1" * 5_000], ids=["deep-nesting", "huge-int"])
def test_decode_rejects_json_the_parser_refuses(text):
    with pytest.raises(ProtocolError):
        decode(text)

@pytest.mark.parametrize("text", [
    '{"current_time": 1}',  # no type
    '{"type": null}',
    '{"type": ["seek"]}',   # unhashable: must not escape as a TypeError
    '{"type": "STATE"}',    # names are case-sensitive
])
def test_decode_rejects_missing_or_unknown_type(text):
    with pytest.raises(ProtocolError):
        decode(text)

def test_decode_rejects_extra_field():
    with pytest.raises(ProtocolError):
        decode('{"type": "stop", "extra": 1}')

@pytest.mark.parametrize("text", [
    '{"type": "start", "mode": "dub"}',
    '{"type": "captions", "video_id": "v", "lang": "en", "kind": "auto", "json3": {}}',
    '{"type": "captions", "video_id": "v", "lang": "en", "kind": ["asr"], "json3": {}}',
    '{"type": "control", "action": "stop", "value": null}',
])
def test_decode_rejects_values_outside_the_allowed_sets(text):
    with pytest.raises(ProtocolError):
        decode(text)

def test_start_accepts_null_mode():
    assert decode('{"type": "start", "mode": null}') == Start(mode=None)

def test_decode_passes_numbers_through_unchanged():
    msg = decode('{"type": "seek", "current_time": 61}')
    assert msg == Seek(current_time=61)
    assert type(msg.current_time) is int
