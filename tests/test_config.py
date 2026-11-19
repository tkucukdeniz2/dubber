import pathlib, pytest
from dubber.config import load_config, Config

def test_defaults_and_override(tmp_path):
    assert load_config(None).server.port == 8765 and load_config(None).duck.volume == 0.2
    p = tmp_path / "c.toml"; p.write_text('[duck]\nvolume = 0.25\n[voice]\nvoice = "Yelda"\n')
    c = load_config(p)
    assert c.duck.volume == 0.25 and c.voice.voice == "Yelda"

def test_duck_out_of_range_rejected(tmp_path):
    p = tmp_path / "c.toml"; p.write_text('[duck]\nvolume = 0.5\n')
    with pytest.raises(ValueError):
        load_config(p)

# What the two tests above leave open: every default, the other tables, unknown names,
# value types, the repository's example file and the shape the PC translator reads.
import dataclasses

from dubber.engines import translate_llamacpp
from dubber.scheduler import SchedulerConfig

REPO = pathlib.Path(__file__).resolve().parent.parent

def write(tmp_path, text):
    p = tmp_path / "c.toml"
    p.write_text(text, encoding="utf-8")
    return p

def test_every_default():
    c = load_config(None)
    assert (c.server.host, c.server.port) == ("127.0.0.1", 8765)
    assert c.duck.volume == 0.2
    assert (c.voice.engine, c.voice.voice) == ("say", "Samantha")
    assert c.translator.engine == "identity"
    assert dataclasses.asdict(c.pc) == {
        "base_url": "http://my-pc.example.ts.net:8080",
        "api_key_file": "~/.qwen-pc/api-key",
        "preferred_model": "qwen3.8-27b",
        "tts_url": "http://my-pc.example.ts.net:8191",
    }
    assert dataclasses.asdict(c.mac_llm) == {"base_url": "http://127.0.0.1:11434/v1", "model": "qwen3.8:27b-mlx"}
    assert c.scheduler == SchedulerConfig()
    assert c.metrics_path == "metrics.jsonl"

def test_configs_share_no_mutable_section():
    a, b = Config(), Config()
    a.scheduler.startup_ready = 1.0; a.server.port = 1; a.duck.volume = 0.15
    assert (b.scheduler.startup_ready, b.server.port, b.duck.volume) == (12.0, 8765, 0.2)

def test_every_table_and_the_root_key_override_only_what_they_name(tmp_path):
    c = load_config(write(tmp_path, (
        'metrics_path = "/var/tmp/dub.jsonl"\n'
        '[server]\nport = 8800\n'
        '[translator]\nengine = "pc"\n'
        '[pc]\npreferred_model = "gemma4-12b"\n'
        '[mac_llm]\nmodel = "small"\n'
        '[scheduler]\nstartup_ready = 10\nhold_cap = 15.5\n'
    )))
    assert c.metrics_path == "/var/tmp/dub.jsonl"
    assert (c.server.host, c.server.port) == ("127.0.0.1", 8800)
    assert c.translator.engine == "pc"
    assert (c.pc.preferred_model, c.pc.base_url) == ("gemma4-12b", "http://my-pc.example.ts.net:8080")
    assert (c.mac_llm.model, c.mac_llm.base_url) == ("small", "http://127.0.0.1:11434/v1")
    assert c.scheduler.startup_ready == 10.0 and type(c.scheduler.startup_ready) is float   # a TOML integer
    assert c.scheduler.hold_cap == 15.5 and c.scheduler.lead_max == 120.0

@pytest.mark.parametrize("text, named", [
    ('[speaker]\nvolume = 1\n', "speaker"),
    ('[duck]\nlevel = 0.2\n', "duck.level"),
    ('colour = "red"\n', "colour"),
    ('[scheduler]\nlead = 3\n', "scheduler.lead"),
])
def test_unknown_tables_and_keys_are_rejected_by_name(tmp_path, text, named):
    with pytest.raises(ValueError, match=named):
        load_config(write(tmp_path, text))

def test_every_unknown_name_is_reported_at_once(tmp_path):
    with pytest.raises(ValueError) as caught:
        load_config(write(tmp_path, '[speaker]\nx = 1\n[duck]\nlevel = 0.2\n'))
    assert "speaker" in str(caught.value) and "duck.level" in str(caught.value)

@pytest.mark.parametrize("text", [
    '[server]\nport = "8765"\n',
    '[duck]\nvolume = "0.2"\n',
    '[duck]\nvolume = true\n',
    '[voice]\nvoice = 3\n',
    'duck = 0.2\n',
    '[metrics_path]\npath = "x"\n',
])
def test_values_of_the_wrong_type_are_rejected(tmp_path, text):
    with pytest.raises(ValueError):
        load_config(write(tmp_path, text))

@pytest.mark.parametrize("volume, ok", [(0.15, True), (0.25, True), (0.149, False), (0.251, False), (0, False)])
def test_duck_range_is_inclusive(tmp_path, volume, ok):
    p = write(tmp_path, f"[duck]\nvolume = {volume}\n")
    if ok:
        assert load_config(p).duck.volume == volume
    else:
        with pytest.raises(ValueError, match="duck.volume"):
            load_config(p)

def test_repository_example_loads_and_is_the_stand_in_setup():
    c = load_config(REPO / "config.example.toml")
    assert (c.translator.engine, c.voice.engine, c.voice.voice) == ("identity", "say", "Samantha")
    assert (c.server.host, c.server.port) == ("127.0.0.1", 8765)
    assert c.duck.volume == 0.2 and c.metrics_path == "metrics.jsonl"
    assert c.pc.api_key_file == "~/.qwen-pc/api-key"
    assert c.mac_llm.model == "qwen3.8:27b-mlx"

def test_repository_example_changes_nothing_that_the_defaults_do_not():
    assert load_config(REPO / "config.example.toml") == Config()

def test_repository_example_first_turkish_preset_loads_when_its_two_values_are_changed(tmp_path):
    text = (REPO / "config.example.toml").read_text(encoding="utf-8")
    lines = text.splitlines()
    # The preset is a comment that names two values to change in the tables above. It holds no
    # copy of a table to uncomment, commented or not: a second [translator] or [voice] is a config error.
    for table in ("[translator]", "[voice]"):
        assert [line.lstrip("# ") for line in lines].count(table) == 1
    start = next(i for i, line in enumerate(lines) if "First Turkish" in line)
    preset = []
    for line in lines[start:]:
        if not line.startswith("#"):
            break
        preset.append(line.lstrip("# "))
    said = " ".join(preset)
    assert 'engine = "pc" under [translator]' in said and 'voice = "Yelda" under [voice]' in said
    changed = text.replace('engine = "identity"', 'engine = "pc"').replace('voice = "Samantha"', 'voice = "Yelda"')
    c = load_config(write(tmp_path, changed))
    assert c.translator.engine == "pc"
    assert (c.voice.engine, c.voice.voice) == ("say", "Yelda")

def test_pc_section_is_what_the_pc_translator_reads(tmp_path):
    key = tmp_path / "key"; key.write_text("secret\n")
    c = Config(); c.pc.api_key_file = str(key)
    pc = dataclasses.asdict(c.pc)
    # The translator reads the first three; tts_url is the voice service's, which it ignores.
    assert set(pc) == {"base_url", "api_key_file", "preferred_model", "tts_url"}
    t = translate_llamacpp.translator_from_config({"pc": pc})
    assert t.model == "qwen3.8-27b" and t.prefer_loaded


def test_the_segmenter_table_sets_the_cuts_and_whole_sentences_are_the_default(tmp_path):
    c = load_config(write(tmp_path, '[segmenter]\npause = 0.8\nlong_pause = 2\nmin_words = 4\nmax_words = 30\nmax_seconds = 15\n'))
    assert dataclasses.asdict(c.segmenter) == {"pause": 0.8, "long_pause": 2.0, "min_words": 4, "max_words": 30, "max_seconds": 15.0}
    assert dataclasses.asdict(load_config(None).segmenter) == {
        "pause": 0.6, "long_pause": 1.5, "min_words": 6, "max_words": 60, "max_seconds": 18.0,
    }


def test_the_context_count_and_the_glossary_come_from_the_file(tmp_path):
    c = load_config(write(tmp_path, (
        '[translator]\ncontext_sentences = 2\n'
        '[glossary]\nspice = "baharat"\n"Paul Atreides" = "Paul Atreides"\n'
    )))
    assert c.translator.context_sentences == 2
    assert c.glossary == {"spice": "baharat", "Paul Atreides": "Paul Atreides"}
    d = load_config(None)
    assert (d.translator.context_sentences, d.glossary) == (3, {})


@pytest.mark.parametrize("text, message", [
    ('[glossary]\nspice = 3\n', "glossary.spice must be a string"),
    ('glossary = "spice"\n', "glossary must be a dict"),
    ('[translator]\ncontext_sentences = -1\n', "translator.context_sentences must be 0 or more"),
])
def test_a_bad_glossary_or_context_count_is_rejected(tmp_path, text, message):
    with pytest.raises(ValueError, match=message):
        load_config(write(tmp_path, text))


# `dubber voice`: rewriting the two values of the [voice] table in a file the owner has commented.
from dubber.config import VOICE_CHOICES, set_voice

def test_the_voice_choices_are_auto_the_two_designed_voices_the_owners_voice_and_yelda():
    assert VOICE_CHOICES == {
        "auto": ("pc", "auto"), "ipek": ("pc", "ipek"), "kalin": ("pc", "kalin"),
        "owner": ("pc", "owner"), "yelda": ("say", "Yelda"),
    }

def test_a_man_is_read_in_kalin_and_a_woman_in_ipek_unless_the_file_names_other_voices(tmp_path):
    assert (Config().voice.male, Config().voice.female) == ("kalin", "ipek")
    c = load_config(write(tmp_path, '[voice]\nengine = "pc"\nvoice = "auto"\nmale = "owner"\nfemale = "anne"\n'))
    assert (c.voice.engine, c.voice.voice, c.voice.male, c.voice.female) == ("pc", "auto", "owner", "anne")

def test_the_example_file_shows_the_two_voices_of_auto():
    text = (REPO / "config.example.toml").read_text(encoding="utf-8")
    assert 'male = "kalin"' in text and 'female = "ipek"' in text and '"auto"' in text

def test_set_voice_changes_only_the_two_values_of_the_voice_table(tmp_path):
    text = (REPO / "config.example.toml").read_text(encoding="utf-8")
    p = write(tmp_path, text)
    set_voice(p, "pc", "owner")
    changed = [(a, b) for a, b in zip(text.splitlines(), p.read_text(encoding="utf-8").splitlines()) if a != b]
    assert len(p.read_text(encoding="utf-8").splitlines()) == len(text.splitlines())
    assert [a.split("#")[0].strip() for a, _ in changed] == ['engine = "say"', 'voice = "Samantha"']
    assert [b.split("#")[0].strip() for _, b in changed] == ['engine = "pc"', 'voice = "owner"']
    # The comment at the end of each line stays.
    assert [a.split("#", 1)[1] for a, _ in changed] == [b.split("#", 1)[1] for _, b in changed]
    c = load_config(p)
    assert (c.voice.engine, c.voice.voice, c.translator.engine) == ("pc", "owner", "identity")

def test_set_voice_twice_gives_back_the_same_file(tmp_path):
    p = write(tmp_path, '[voice]\nengine = "pc"  # where\nvoice = "owner"  # who\n')
    before = p.read_bytes()
    set_voice(p, "say", "Yelda")
    assert p.read_bytes() != before
    set_voice(p, "pc", "owner")
    assert p.read_bytes() == before

def test_set_voice_adds_a_missing_key_to_the_table(tmp_path):
    p = write(tmp_path, '[voice]\nvoice = "Samantha"\n\n[duck]\nvolume = 0.2\n')
    set_voice(p, "say", "Yelda")
    c = load_config(p)
    assert (c.voice.engine, c.voice.voice, c.duck.volume) == ("say", "Yelda", 0.2)

def test_set_voice_adds_a_missing_table(tmp_path):
    p = write(tmp_path, '[translator]\nengine = "pc"')
    set_voice(p, "say", "Yelda")
    c = load_config(p)
    assert (c.voice.engine, c.voice.voice, c.translator.engine) == ("say", "Yelda", "pc")

@pytest.mark.parametrize("text", [
    '[voice\nengine = "pc"\n',                      # not TOML
    '[voice]\n"engine" = "pc"\nvoice = "owner"\n',  # a spelling the rewrite does not follow
])
def test_set_voice_leaves_a_file_it_cannot_rewrite_untouched(tmp_path, text):
    p = write(tmp_path, text)
    with pytest.raises(ValueError):
        set_voice(p, "say", "Yelda")
    assert p.read_text(encoding="utf-8") == text


def test_log_path_is_dubber_log_unless_the_file_says_otherwise(tmp_path):
    assert load_config(None).log_path == "dubber.log"
    assert load_config(write(tmp_path, 'log_path = "run/d.log"\n')).log_path == "run/d.log"
    assert load_config(write(tmp_path, 'log_path = ""\n')).log_path == ""   # no log file
    with pytest.raises(ValueError, match="log_path"):
        load_config(write(tmp_path, "log_path = 3\n"))


def test_restore_punctuation_is_on_unless_the_file_says_no(tmp_path):
    assert Config().translator.restore_punctuation is True
    path = tmp_path / "c.toml"
    path.write_text("[translator]\nrestore_punctuation = false\n", encoding="utf-8")
    assert load_config(path).translator.restore_punctuation is False
    path.write_text('[translator]\nrestore_punctuation = "no"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="restore_punctuation"):
        load_config(path)
