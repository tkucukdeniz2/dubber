import httpx, json, pytest
from dubber.engines.translate_llamacpp import LlamaCppTranslator, strip_think
from dubber.engines.base import TranslatorError
import http.server, threading
import numpy as np
from dubber.engines.translate_llamacpp import SYSTEM_PROMPT, ModelInfo, translator_from_config
from dubber.engines.base import Clip, IdentityTranslator

def test_strip_think_removes_empty_and_filled_blocks():
    assert strip_think("<think>  </think>  Merhaba dünya.") == "Merhaba dünya."
    assert strip_think("<think>uzun\ndüşünce</think>\nSelam.") == "Selam."
    assert strip_think("Selam.") == "Selam."

def make(handler, model=None):
    return LlamaCppTranslator("http://pc:8080", "k", model=model, transport=httpx.MockTransport(handler))

def test_pick_model_prefers_loaded_when_unconfigured():
    def handler(req):
        assert req.headers["authorization"] == "Bearer k"
        return httpx.Response(200, json={"data": [{"id": "gemma4-12b", "status": {"value": "unloaded"}}, {"id": "qwen3.8-27b", "status": {"value": "loaded"}}]})
    assert make(handler).pick_model() == "qwen3.8-27b"

def test_translate_sends_contract_and_strips_think():
    seen = {}
    def handler(req):
        if req.url.path.endswith("/v1/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3.8-27b", "status": {"value": "loaded"}}]})
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "<think>  </think>  Bunu nasıl açıklarsınız?"}}]})
    out = make(handler).translate("How do you explain this?", context=["Previous one."], glossary={"Apple": "Apple"})
    assert out == "Bunu nasıl açıklarsınız?"
    body = seen["body"]
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["messages"][0]["role"] == "system" and "Turkish" in body["messages"][0]["content"]
    assert body["messages"][-1] == {"role": "user", "content": "How do you explain this?"}
    assert any("Previous sentences" in m["content"] for m in body["messages"])

def test_translate_raises_on_error_status_and_empty_output():
    def bad(req):
        return httpx.Response(503, text="overloaded")
    with pytest.raises(TranslatorError) as e:
        make(bad, model="qwen3.8-27b").translate("x")
    assert "503" in str(e.value)
    def empty(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": "<think></think>"}}]})
    with pytest.raises(TranslatorError):
        make(empty, model="qwen3.8-27b").translate("x")

# What the tests above leave open: the request body, message order, model choice,
# error mapping and config reading.

def chat_reply(content="Merhaba."):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

def models_reply(*models):
    return httpx.Response(200, json={"data": [{"id": i, "status": {"value": s}} for i, s in models]})

def recorder():
    """A handler that records every request and answers it with a chat reply."""
    seen = []
    def handler(req):
        seen.append(req)
        return chat_reply()
    return handler, seen

def test_translate_posts_the_request_contract():
    handler, seen = recorder()
    make(handler, model="qwen3.8-27b").translate("Hello.")
    (req,) = seen  # a configured model needs no /v1/models lookup
    assert req.method == "POST" and req.url.path == "/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer k"
    body = json.loads(req.content)
    assert body["model"] == "qwen3.8-27b"
    assert body["temperature"] == 0.2 and body["max_tokens"] == 200
    assert body["reasoning_format"] == "none"
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["think"] is False  # Ollama's OpenAI-compatible endpoint reads this one

@pytest.mark.parametrize("context, glossary, lines", [
    (["One.", "Two."], {"Apple": "Apple", "GPU": "ekran kartı"}, "\nPrevious sentences (context only): One. Two.\nGlossary: Apple=Apple; GPU=ekran kartı"),
    (["One."], None, "\nPrevious sentences (context only): One."),
    ((), {"Apple": "Apple"}, "\nGlossary: Apple=Apple"),
], ids=["context-and-glossary", "context-only", "glossary-only"])
def test_translate_puts_context_and_glossary_lines_in_the_system_message(context, glossary, lines):
    handler, seen = recorder()
    make(handler, model="m").translate("Hello.", context=context, glossary=glossary)
    assert json.loads(seen[0].content)["messages"] == [
        {"role": "system", "content": SYSTEM_PROMPT + lines},
        {"role": "user", "content": "Hello."},
    ]

def test_translate_without_context_or_glossary_sends_the_bare_system_prompt():
    handler, seen = recorder()
    make(handler, model="m").translate("Hello.", context=(), glossary={})
    assert json.loads(seen[0].content)["messages"] == [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "Hello."},
    ]

def test_list_models_reads_ids_and_loaded_flag_and_tolerates_a_missing_status():
    def handler(req):
        return httpx.Response(200, json={"data": [
            {"id": "a", "status": {"value": "loaded"}},
            {"id": "b", "status": {"value": "unloaded"}},
            {"id": "c"},  # Ollama lists no status
        ]})
    assert make(handler).list_models() == [ModelInfo("a", True), ModelInfo("b", False), ModelInfo("c", False)]

def make_following(handler, model=None):
    """Like make, but following whatever the server has loaded (prefer_loaded)."""
    return LlamaCppTranslator("http://pc:8080", "k", model=model, prefer_loaded=True, transport=httpx.MockTransport(handler))

def pick(models, model=None, following=False):
    return (make_following if following else make)(lambda req: models_reply(*models), model=model).pick_model()

def test_pick_model_prefers_the_configured_model_when_listed():
    assert pick([("a", "loaded"), ("b", "unloaded")], model="b") == "b"

def test_pick_model_uses_the_loaded_model_when_the_configured_one_is_not_listed():
    assert pick([("a", "unloaded"), ("b", "loaded")], model="gone") == "b"

def test_pick_model_falls_back_to_the_first_listed_model_when_none_is_loaded():
    assert pick([("a", "unloaded"), ("b", "unloaded")]) == "a"

def test_pick_model_raises_when_no_models_are_listed():
    with pytest.raises(TranslatorError, match="no models listed"):
        pick([])

def test_prefer_loaded_follows_the_loaded_model_but_a_pinned_one_keeps_the_configured_model():
    models = [("gemma4-12b", "loaded"), ("qwen3.8-27b", "unloaded")]
    assert pick(models, model="qwen3.8-27b", following=True) == "gemma4-12b"
    assert pick(models, model="qwen3.8-27b") == "qwen3.8-27b"

def test_prefer_loaded_falls_back_to_the_configured_model_then_the_first_listed():
    nothing_loaded = [("a", "unloaded"), ("b", "unloaded")]
    assert pick(nothing_loaded, model="b", following=True) == "b"
    assert pick(nothing_loaded, model="gone", following=True) == "a"

def test_translate_with_prefer_loaded_sends_the_loaded_model_although_one_is_configured():
    seen = []
    def handler(req):
        seen.append(req)
        if req.url.path == "/v1/models":
            return models_reply(("gemma4-12b", "loaded"), ("qwen3.8-27b", "unloaded"))
        return chat_reply()
    make_following(handler, model="qwen3.8-27b").translate("Hello.")
    assert [r.url.path for r in seen] == ["/v1/models", "/v1/chat/completions"]
    assert json.loads(seen[1].content)["model"] == "gemma4-12b"

def test_translate_picks_the_model_once_and_sends_it():
    seen = []
    def handler(req):
        seen.append(req)
        if req.url.path == "/v1/models":
            return models_reply(("gemma4-12b", "unloaded"), ("qwen3.8-27b", "loaded"))
        return chat_reply()
    t = make(handler)
    t.translate("One.")
    t.translate("Two.")
    assert [r.url.path for r in seen] == ["/v1/models", "/v1/chat/completions", "/v1/chat/completions"]
    assert {json.loads(r.content)["model"] for r in seen[1:]} == {"qwen3.8-27b"}

# Following the loaded model (ruling R51): a translator with prefer_loaded asks the server which
# model is loaded before every request, because the owner can load another one at any moment, and a
# swap forced on the PC would take the model away from whoever is using it. Without prefer_loaded
# the model is picked on the first request and kept, as the bake-off's engines need.

def two_models(loaded):
    """A server with 'model-a' and 'model-b'. `loaded` is a one-item list naming the loaded one
    ('' for neither), so that a test can change it between two requests. Returns handler and requests."""
    seen = []
    def handler(req):
        seen.append(req)
        if req.url.path == "/v1/models":
            return models_reply(*[(m, "loaded" if m == loaded[0] else "unloaded") for m in ("model-a", "model-b")])
        return chat_reply()
    return handler, seen

def paths(seen):
    return [r.url.path for r in seen]

def models_asked_for(seen):
    return [json.loads(r.content)["model"] for r in seen if r.url.path == "/v1/chat/completions"]

def test_prefer_loaded_asks_which_model_is_loaded_before_every_request():
    loaded = ["model-a"]
    handler, seen = two_models(loaded)
    t = make_following(handler, model="model-a")
    t.translate("One.")
    loaded[0] = "model-b"  # the owner loads the other model while the translator runs
    t.translate("Two.")
    assert paths(seen) == ["/v1/models", "/v1/chat/completions"] * 2
    assert models_asked_for(seen) == ["model-a", "model-b"]  # the second request does not swap model-b out

def test_prefer_loaded_falls_back_to_the_configured_model_whenever_none_is_loaded():
    loaded = ["model-b"]
    handler, seen = two_models(loaded)
    t = make_following(handler, model="model-a")
    t.translate("One.")
    loaded[0] = ""  # the owner unloads it: nothing is loaded now
    t.translate("Two.")
    loaded[0] = "model-b"
    t.translate("Three.")
    assert models_asked_for(seen) == ["model-b", "model-a", "model-b"]

def test_prefer_loaded_falls_back_to_the_first_listed_model_when_the_configured_one_is_not_listed():
    loaded = ["model-b"]
    handler, seen = two_models(loaded)
    t = make_following(handler, model="not-listed")
    t.translate("One.")
    loaded[0] = ""
    t.translate("Two.")
    assert models_asked_for(seen) == ["model-b", "model-a"]

def test_prefer_loaded_does_not_reuse_an_earlier_model_when_the_list_cannot_be_read():
    seen, pc_is_off = [], [False]
    def handler(req):
        seen.append(req)
        if pc_is_off[0]:
            raise httpx.ConnectError("pc is off")
        return models_reply(("model-a", "loaded")) if req.url.path == "/v1/models" else chat_reply()
    t = make_following(handler, model="model-a")
    t.translate("One.")
    pc_is_off[0] = True
    with pytest.raises(TranslatorError, match="pc is off"):
        t.translate("Two.")
    # The failed request is the list. No chat request goes out for the model it remembers.
    assert paths(seen) == ["/v1/models", "/v1/chat/completions", "/v1/models"]

def test_without_prefer_loaded_the_model_is_picked_once_and_kept_although_another_is_loaded_later():
    loaded = ["model-a"]
    handler, seen = two_models(loaded)
    t = make(handler)  # no configured model and not following: it asks the server once
    t.translate("One.")
    loaded[0] = "model-b"
    t.translate("Two.")
    assert paths(seen) == ["/v1/models", "/v1/chat/completions", "/v1/chat/completions"]
    assert models_asked_for(seen) == ["model-a", "model-a"]

@pytest.mark.parametrize("following, listings", [(False, 1), (True, 2)])
def test_pick_model_asks_the_server_again_only_when_following_the_loaded_model(following, listings):
    handler, seen = two_models(["model-a"])
    t = (make_following if following else make)(handler)
    t.pick_model()
    t.pick_model()
    assert paths(seen) == ["/v1/models"] * listings

def test_a_pinned_model_is_sent_as_it_is_and_the_server_is_never_asked_which_is_loaded():
    handler, seen = two_models(["model-b"])
    t = make(handler, model="model-a")  # pinned: a model, and not following
    t.translate("One.")
    t.translate("Two.")
    assert paths(seen) == ["/v1/chat/completions"] * 2
    assert models_asked_for(seen) == ["model-a", "model-a"]

# The PC translator built from the config waits 3 s to connect, so that a PC that is off or a
# Tailscale that is down fails a sentence in seconds. Everything else is allowed 120 s, which a
# long translation can need. A translator built directly keeps its single timeout.

def test_the_pc_translator_from_the_config_connects_for_3_seconds_and_reads_for_120(tmp_path):
    (tmp_path / "api-key").write_text("secret-key\n")
    cfg = {"pc": {"base_url": "http://pc:8080", "api_key_file": str(tmp_path / "api-key")}}
    timeout = translator_from_config(cfg)._client.timeout
    assert timeout == httpx.Timeout(120.0, connect=3.0)
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (3.0, 120.0, 120.0, 120.0)

def test_a_translator_built_directly_keeps_one_timeout_for_everything():
    assert LlamaCppTranslator("http://pc:8080", None)._client.timeout == httpx.Timeout(120.0)
    assert LlamaCppTranslator("http://pc:8080", None, timeout=5.0)._client.timeout == httpx.Timeout(5.0)
    own = httpx.Timeout(9.0, connect=1.0)
    assert LlamaCppTranslator("http://pc:8080", None, timeout=own)._client.timeout == own

def test_connection_errors_become_translator_errors():
    def handler(req):
        raise httpx.ConnectError("pc is off")
    with pytest.raises(TranslatorError, match="pc is off"):
        make(handler, model="m").translate("x")

def test_models_error_status_becomes_a_translator_error():
    with pytest.raises(TranslatorError, match="401: bad key"):
        make(lambda req: httpx.Response(401, text="bad key")).pick_model()

def test_error_body_is_cut_to_200_characters():
    with pytest.raises(TranslatorError) as e:
        make(lambda req: httpx.Response(500, text="x" * 500), model="m").translate("x")
    assert str(e.value) == "500: " + "x" * 200

@pytest.mark.parametrize("body", [
    '{"choices": []}',                                   # IndexError
    "{}",                                                # KeyError
    "not json",                                          # ValueError
    '{"choices": [{"message": {"content": null}}]}',     # TypeError
    json.dumps({"choices": [], "padding": "x" * 500}),   # the body is cut to 200 characters
], ids=["empty-choices", "missing-choices", "not-json", "null-content", "long-body-is-cut"])
def test_a_malformed_200_reply_becomes_a_translator_error(body):
    with pytest.raises(TranslatorError) as e:
        make(lambda req: httpx.Response(200, text=body), model="m").translate("x")
    assert str(e.value) == "malformed reply: " + body[:200]

def test_a_malformed_models_reply_becomes_a_translator_error():
    with pytest.raises(TranslatorError, match="malformed reply"):
        make(lambda req: httpx.Response(200, json={"models": []})).pick_model()

@pytest.mark.parametrize("base", ["http://mac:11434", "http://mac:11434/", "http://mac:11434/v1", "http://mac:11434/v1/"])
def test_base_url_may_end_in_v1(base):
    handler, seen = recorder()
    LlamaCppTranslator(base, None, model="m", transport=httpx.MockTransport(handler)).translate("x")
    assert [str(r.url) for r in seen] == ["http://mac:11434/v1/chat/completions"]

def test_no_api_key_sends_no_authorization_header():
    handler, seen = recorder()
    LlamaCppTranslator("http://mac:11434", None, model="m", transport=httpx.MockTransport(handler)).translate("x")
    assert "authorization" not in seen[0].headers

@pytest.fixture
def local_pc():
    """A throwaway local server: lists two unloaded models, answers every chat, notes who asked."""
    seen = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def send_json(self, payload):
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            seen.append((self.path, self.headers["Authorization"], None))
            self.send_json({"data": [{"id": "gemma4-12b", "status": {"value": "unloaded"}}, {"id": "qwen3.8-27b", "status": {"value": "unloaded"}}]})
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, self.headers["Authorization"], body["model"]))
            self.send_json({"choices": [{"message": {"content": "Merhaba."}}]})
        def log_message(self, *args):
            pass
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", seen
    server.shutdown()
    thread.join()
    server.server_close()

def test_translator_from_config_wires_the_key_the_preferred_model_and_prefer_loaded(tmp_path, monkeypatch, local_pc):
    base_url, seen = local_pc
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "api-key").write_text("secret-key\n")
    cfg = {"pc": {"base_url": base_url, "api_key_file": "~/api-key", "preferred_model": "qwen3.8-27b"}}
    assert translator_from_config(cfg).translate("Hello.") == "Merhaba."
    # "~" expanded and the key stripped, on both requests. The models are listed although
    # one is configured (prefer_loaded); none is loaded, so the preferred one wins over the first listed.
    assert seen == [
        ("/v1/models", "Bearer secret-key", None),
        ("/v1/chat/completions", "Bearer secret-key", "qwen3.8-27b"),
    ]

def test_translator_from_config_treats_the_preferred_model_as_optional(tmp_path):
    (tmp_path / "api-key").write_text("secret-key\n")
    cfg = {"pc": {"base_url": "http://pc:8080", "api_key_file": str(tmp_path / "api-key")}}
    assert translator_from_config(cfg).model is None

def test_clip_seconds_is_samples_over_sample_rate():
    assert Clip(np.zeros(11025, dtype=np.float32), 22050).seconds == 0.5

def test_identity_translator_returns_the_text_unchanged():
    t = IdentityTranslator()
    assert t.name == "identity"
    assert t.translate("Hello.", context=["Before."], glossary={"a": "b"}) == "Hello."

@pytest.mark.live
def test_live_pc_translates_one_sentence():
    import tomllib, pathlib
    cfg = tomllib.loads(pathlib.Path("config.example.toml").read_text())
    from dubber.engines.translate_llamacpp import translator_from_config
    out = translator_from_config(cfg).translate("How do you explain when things don't go as we assume?")
    assert "<think>" not in out and len(out) > 10


# Gemma 4 on llama.cpp opens its answer with an empty thought channel. Seen on the PC in
# video mode: "<|channel>thought\n<channel|>ama benim için en çok öne çıkan görüntü bu."
def test_strip_think_removes_gemma_thought_channel():
    from dubber.engines.translate_llamacpp import strip_think

    assert strip_think("<|channel>thought\n<channel|>ama benim için en çok öne çıkan görüntü bu.") == (
        "ama benim için en çok öne çıkan görüntü bu."
    )
    assert strip_think("<|channel>thought\nThe user wants Turkish.\n<channel|>\nGemi devasa.") == "Gemi devasa."
    assert strip_think("<think>\n</think>\n<|channel>thought<channel|>İki biçim.") == "İki biçim."
    assert strip_think("Kanal kelimesi geçen sıradan bir cümle.") == "Kanal kelimesi geçen sıradan bir cümle."


# TranslateGemma takes no system prompt and no chat turns: its own template wraps the text in a
# fixed English-to-Turkish instruction. llama.cpp's chat endpoint cannot fill that template, so
# the translator sends the instruction as a plain completion when the model is TranslateGemma.
TRANSLATEGEMMA_PROMPT = (
    "<start_of_turn>user\nYou are a professional English (en) to Turkish (tr) translator. Your goal is to "
    "accurately convey the meaning and nuances of the original English text while adhering to Turkish grammar, "
    "vocabulary, and cultural sensitivities.\nProduce only the Turkish translation, without any additional "
    "explanations or commentary. Please translate the following English text into Turkish:\n\n\n"
    "The ship is called a Heighliner.<end_of_turn>\n<start_of_turn>model\n"
)


def translategemma_server(seen, reply="Geminin adı Heighliner.", finish="stop"):
    import httpx as _httpx

    def handler(request):
        seen.append((request.url.path, json.loads(request.content) if request.content else None))
        if request.url.path == "/v1/models":
            return _httpx.Response(200, json={"data": [
                {"id": "translategemma-12b", "status": {"value": "loaded"}},
                {"id": "gemma4-12b", "status": {"value": "unloaded"}},
            ]})
        return _httpx.Response(200, json={"choices": [{"text": reply, "finish_reason": finish}]})

    return _httpx.MockTransport(handler)


def test_translategemma_gets_its_own_instruction_as_a_completion():
    seen = []
    t = LlamaCppTranslator("http://pc.test:8080", "k", prefer_loaded=True, transport=translategemma_server(seen))
    assert t.translate("  The ship is called a Heighliner. ", context=["ignored"], glossary={"x": "y"}) == "Geminin adı Heighliner."
    path, body = seen[-1]
    assert path == "/v1/completions"
    assert body["model"] == "translategemma-12b" and body["prompt"] == TRANSLATEGEMMA_PROMPT
    assert body["stop"] == ["<end_of_turn>"] and body["temperature"] == 0 and body["max_tokens"] == 200


def test_translategemma_reply_is_trimmed_and_an_empty_one_is_an_error():
    seen = []
    t = LlamaCppTranslator("http://pc.test:8080", "k", prefer_loaded=True,
                           transport=translategemma_server(seen, reply="\n  Gemi devasa. \n"))
    assert t.translate("The ship is huge.") == "Gemi devasa."
    empty = LlamaCppTranslator("http://pc.test:8080", "k", prefer_loaded=True,
                               transport=translategemma_server([], reply="  "))
    with pytest.raises(TranslatorError, match="empty translation"):
        empty.translate("The ship is huge.")


def test_other_models_keep_the_chat_request():
    import httpx as _httpx
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/v1/models":
            return _httpx.Response(200, json={"data": [{"id": "gemma4-12b", "status": {"value": "loaded"}}]})
        return _httpx.Response(200, json={"choices": [{"message": {"content": "Gemi."}}]})

    t = LlamaCppTranslator("http://pc.test:8080", "k", prefer_loaded=True, transport=_httpx.MockTransport(handler))
    assert t.translate("The ship.") == "Gemi." and seen[-1] == "/v1/chat/completions"


def test_strip_think_drops_a_thinking_block_cut_off_by_the_token_limit():
    assert strip_think("<|channel>thought\nThe user wants a translation of") == ""
    assert strip_think("<think>uzun bir düşünce") == ""
    assert strip_think("<think>kısa</think>Gemi. <|channel>thought yarım") == "Gemi."


def test_the_translategemma_prompt_is_what_its_own_template_renders():
    import pathlib
    import jinja2.sandbox

    template = (pathlib.Path(__file__).parent / "fixtures" / "translategemma_chat_template.jinja").read_text(encoding="utf-8")

    def raise_exception(message):
        raise jinja2.exceptions.TemplateError(message)

    env = jinja2.sandbox.ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True)
    env.globals["raise_exception"] = raise_exception
    text = "The ship is called a Heighliner."
    rendered = env.from_string(template).render(
        bos_token="",  # llama.cpp adds BOS to a completion prompt itself
        add_generation_prompt=True,
        messages=[{"role": "user", "content": [
            {"type": "text", "source_lang_code": "en", "target_lang_code": "tr", "text": text},
        ]}],
    )
    from dubber.engines.translate_llamacpp import TRANSLATEGEMMA_PROMPT

    assert rendered == TRANSLATEGEMMA_PROMPT.format(text=text)


def test_shorten_sends_the_first_rendering_and_asks_for_at_most_n_words():
    from dubber.engines.translate_llamacpp import SHORTEN_REQUEST

    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gemma4-12b", "status": {"value": "loaded"}}]})
        return chat_reply("Gemi kısa.")

    t = LlamaCppTranslator("http://pc.test:8080", "k", prefer_loaded=True, transport=httpx.MockTransport(handler))
    shorter = t.shorten("The ship is very big.", "Gemi gerçekten çok büyük.", 3, context=["Before."], glossary={"ship": "gemi"})
    assert shorter == "Gemi kısa."
    body = json.loads(seen[-1].content)
    assert seen[-1].url.path == "/v1/chat/completions" and body["model"] == "gemma4-12b"
    assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant", "user"]
    assert [m["content"] for m in body["messages"][1:]] == [
        "The ship is very big.", "Gemi gerçekten çok büyük.", SHORTEN_REQUEST.format(max_words=3),
    ]
    assert "at most 3 words" in body["messages"][3]["content"]
    system = body["messages"][0]["content"]
    assert "Previous sentences (context only): Before." in system and "Glossary: ship=gemi" in system


def test_translategemma_cannot_shorten_and_sends_nothing():
    seen = []
    t = LlamaCppTranslator("http://pc.test:8080", "k", prefer_loaded=True, transport=translategemma_server(seen))
    assert t.shorten("The ship.", "Gemi gerçekten çok büyük.", 3) == "Gemi gerçekten çok büyük."
    assert [path for path, _ in seen] == ["/v1/models"]


def test_the_identity_stand_in_cannot_shorten():
    assert IdentityTranslator().shorten("The ship.", "The ship.", 1) == "The ship."


def chat_server(seen, reply="Gemi.", finish="stop"):
    """A server with Gemma loaded that answers every chat with `reply`, ended for `finish`."""
    def handler(request):
        seen.append((request.url.path, json.loads(request.content) if request.content else None))
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gemma4-12b", "status": {"value": "loaded"}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}, "finish_reason": finish}]})

    return httpx.MockTransport(handler)


FORTY_WORDS = " ".join(["word"] * 40)


def test_max_tokens_grows_with_the_sentence_and_never_goes_under_200():
    seen = []
    t = LlamaCppTranslator("http://pc.test:8080", "k", prefer_loaded=True, transport=chat_server(seen))
    t.translate(FORTY_WORDS)
    assert seen[-1][1]["max_tokens"] == 384
    t.shorten(FORTY_WORDS, "Gemi çok büyük.", 2)
    assert seen[-1][1]["max_tokens"] == 384
    t.translate("The ship.")
    assert seen[-1][1]["max_tokens"] == 200


def test_max_tokens_grows_for_translategemma_too():
    seen = []
    t = LlamaCppTranslator("http://pc.test:8080", "k", prefer_loaded=True, transport=translategemma_server(seen))
    t.translate(FORTY_WORDS)
    assert seen[-1][1]["max_tokens"] == 384


def test_a_cut_off_translation_is_an_error():
    t = LlamaCppTranslator("http://pc.test:8080", "k", prefer_loaded=True,
                           transport=chat_server([], reply="Gemi çok", finish="length"))
    with pytest.raises(TranslatorError, match="cut off at 200 tokens"):
        t.translate("The ship is huge.")
    with pytest.raises(TranslatorError, match="cut off at 200 tokens"):
        t.shorten("The ship is huge.", "Gemi çok büyük.", 2)


def test_a_cut_off_translategemma_reply_is_an_error():
    t = LlamaCppTranslator("http://pc.test:8080", "k", prefer_loaded=True,
                           transport=translategemma_server([], reply="Gemi çok", finish="length"))
    with pytest.raises(TranslatorError, match="cut off at 200 tokens"):
        t.translate("The ship is huge.")


# Warm-up (design of 2026-11-14, 4.5): make the server load the model the first sentence would
# use, so that the first sentence does not wait 11 s for it. A model that is loaded, or being
# loaded, is never touched.
DUBBING_MODELS = (("gemma4-12b", "unloaded"), ("translategemma-12b", "unloaded"))

def warming(models, model=None, answer=None):
    seen = []
    def handler(req):
        seen.append(req)
        if req.url.path == "/v1/models":
            return models_reply(*models)
        return answer or httpx.Response(200, json={"choices": [{"message": {"content": "M"}, "text": "M", "finish_reason": "length"}]})
    return make_following(handler, model=model), seen

def test_warm_up_asks_one_token_of_the_first_listed_model_when_none_is_loaded():
    t, seen = warming(DUBBING_MODELS, model="qwen3.8-27b")   # the preferred model is not in the dubbing preset
    assert t.warm_up() == "gemma4-12b"
    assert [(r.method, r.url.path) for r in seen] == [("GET", "/v1/models"), ("POST", "/v1/chat/completions")]
    body = json.loads(seen[1].content)
    assert body["model"] == "gemma4-12b" and body["max_tokens"] == 1 and len(body["messages"]) == 1

def test_warm_up_asks_the_configured_model_when_the_server_lists_it():
    t, seen = warming((("a", "unloaded"), ("b", "unloaded")), model="b")
    assert t.warm_up() == "b" and json.loads(seen[1].content)["model"] == "b"

def test_warm_up_of_translategemma_is_a_completion_of_one_token():
    t, seen = warming(DUBBING_MODELS, model="translategemma-12b")
    assert t.warm_up() == "translategemma-12b"
    assert seen[1].url.path == "/v1/completions"
    body = json.loads(seen[1].content)
    assert body["model"] == "translategemma-12b" and body["max_tokens"] == 1 and "Turkish" in body["prompt"]

@pytest.mark.parametrize("models", [
    (("qwen3.8-27b", "loaded"), ("gemma4-12b", "unloaded")),   # the owner's model: never touched
    (("gemma4-12b", "loading"), ("translategemma-12b", "unloaded")),   # one is on its way
    (("a", None),),                                                     # a server that does not say
])
def test_warm_up_does_nothing_unless_every_model_is_unloaded(models):
    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"data": [{"id": i, **({"status": {"value": s}} if s else {})} for i, s in models]})
    seen = []
    assert make_following(handler, model="qwen3.8-27b").warm_up() is None
    assert [(r.method, r.url.path) for r in seen] == [("GET", "/v1/models")]

def test_warm_up_says_when_it_cannot_be_done():
    with pytest.raises(TranslatorError, match="no models listed"):
        warming(())[0].warm_up()
    with pytest.raises(TranslatorError, match="500"):
        warming(DUBBING_MODELS, answer=httpx.Response(500, text="out of memory"))[0].warm_up()


def test_connections_to_the_server_stay_open_for_a_minute():
    """From far away a new connection costs round trips, and sentences come every few seconds."""
    assert LlamaCppTranslator("http://pc:8080", "k")._client._transport._pool._keepalive_expiry == 60.0


# The same model restores the punctuation of a caption track that has none.

from dubber.punctuation import PROMPT as PUNCTUATION_PROMPT


def punctuating(seen, reply="So it goes. Fine.", finish="stop", model="gemma4-12b"):
    def handler(req):
        if req.url.path.endswith("/v1/models"):
            return httpx.Response(200, json={"data": [{"id": model, "status": {"value": "loaded"}}]})
        seen.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}, "finish_reason": finish}]})
    return handler


def test_punctuate_asks_the_loaded_model_for_a_copy_with_marks():
    seen = []
    assert make(punctuating(seen)).punctuate("so it goes fine") == "So it goes. Fine."
    path, body = seen[0]
    assert path.endswith("/v1/chat/completions") and body["model"] == "gemma4-12b"
    assert body["messages"] == [
        {"role": "system", "content": PUNCTUATION_PROMPT},
        {"role": "user", "content": "so it goes fine"},
    ]
    assert body["temperature"] == 0 and body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["max_tokens"] == 3 * 4 + 64


def test_punctuate_strips_a_thought_block():
    assert make(punctuating([], reply="<|channel>thought\n<channel|>So it goes.")).punctuate("so it goes") == "So it goes."


def test_punctuate_is_not_asked_of_translategemma():
    seen = []
    with pytest.raises(TranslatorError, match="punctuation"):
        make(punctuating(seen, model="translategemma-12b")).punctuate("so it goes")
    assert seen == []


def test_punctuate_raises_when_the_reply_is_cut_off_or_empty():
    with pytest.raises(TranslatorError, match="cut off"):
        make(punctuating([], finish="length")).punctuate("so it goes")
    with pytest.raises(TranslatorError, match="empty"):
        make(punctuating([], reply="  ")).punctuate("so it goes")


def test_translate_keeps_its_own_temperature():
    seen = []
    make(punctuating(seen, reply="Öyle gider.")).translate("So it goes.")
    assert seen[0][1]["temperature"] == 0.2


# The same model looks at pictures, when the PC has loaded it with its vision add-on.

import base64

JPEG_1, JPEG_2 = b"\xff\xd8first picture\xff\xd9", b"\xff\xd8second picture\xff\xd9"


def test_look_sends_the_question_and_then_each_picture_as_a_jpeg_in_the_request():
    seen = []
    assert make(punctuating(seen, reply="woman")).look("Who presents it?", [JPEG_1, JPEG_2]) == "woman"
    path, body = seen[0]
    assert path.endswith("/v1/chat/completions") and body["model"] == "gemma4-12b"
    (message,) = body["messages"]
    assert message["role"] == "user"
    assert message["content"] == [
        {"type": "text", "text": "Who presents it?"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(JPEG_1).decode()}},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(JPEG_2).decode()}},
    ]


def test_look_asks_for_a_few_tokens_at_temperature_zero_without_thinking():
    seen = []
    make(punctuating(seen, reply="man")).look("Who presents it?", [JPEG_1])
    body = seen[0][1]
    assert body["temperature"] == 0 and body["max_tokens"] == 12
    assert body["chat_template_kwargs"] == {"enable_thinking": False} and body["reasoning_format"] == "none"


def test_look_gives_the_answer_without_the_thought_channel():
    assert make(punctuating([], reply="<|channel>thought\n<channel|>unclear")).look("Who?", [JPEG_1]) == "unclear"


def test_look_is_not_asked_of_translategemma():
    seen = []
    with pytest.raises(TranslatorError, match="look at pictures"):
        make(punctuating(seen, model="translategemma-12b")).look("Who?", [JPEG_1])
    assert seen == []


def test_look_says_when_the_model_cannot_see():
    def blind(req):
        if req.url.path.endswith("/v1/models"):
            return httpx.Response(200, json={"data": [{"id": "gemma4-12b", "status": {"value": "loaded"}}]})
        return httpx.Response(500, text="image input is not supported - hint: you may need to provide the mmproj")

    with pytest.raises(TranslatorError, match="500: image input is not supported"):
        make(blind).look("Who?", [JPEG_1])
