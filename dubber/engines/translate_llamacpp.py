"""Translate English into Turkish on the PC's llama.cpp server."""

import base64
import pathlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from dubber.engines.base import Translator, TranslatorError
from dubber.punctuation import PROMPT as PUNCTUATION_PROMPT

SYSTEM_PROMPT = (
    "You are a professional interpreter dubbing an English YouTube video into Turkish. "
    "Translate the user's message into natural, spoken Turkish. "
    "Output only the Turkish sentence: no explanations, notes or quotation marks. "
    "Write numbers, dates, units, currencies and acronyms as Turkish words, "
    "the way a speaker would say them. "
    "Keep English names as they are or transliterate them, following the glossary. "
    "Be concise. "
    "When asked for a shorter rendering, rephrase it but keep every content word, "
    "number and name. "
    'Lines after these instructions may give "Previous sentences (context only):", '
    'which are never translated, and a "Glossary:" of English=Turkish pairs to follow.'
)

# Sent after the first rendering, as the next user turn, when that rendering is too long for its slot.
SHORTEN_REQUEST = (
    "Too long to fit. Say it again in at most {max_words} words, keeping every content word, "
    "number and name. Output only the Turkish."
)

# Qwen's <think>...</think>, and Gemma 4's thought channel <|channel>thought ... <channel|>,
# which llama.cpp passes on in the reply even when it is empty. A block cut off by the token
# limit has no end and runs to the end of the reply, so nothing of it is ever spoken.
_THINK_BLOCK = re.compile(r"<think>.*?(?:</think>|\Z)|<\|channel>.*?(?:<channel\|>|\Z)", re.DOTALL)


# What `look` allows the answer: a word about the pictures needs no more.
LOOK_TOKENS = 12


def strip_think(text: str) -> str:
    """Remove every thinking block (Qwen's or Gemma's), then strip the surrounding space."""
    return _THINK_BLOCK.sub("", text).strip()


@dataclass
class ModelInfo:
    """A model the server lists. `state` is the status it reports ("loaded", "loading",
    "unloaded"), or "" when it reports none; two infos are equal by id and `loaded` alone."""

    id: str
    loaded: bool
    state: str = field(default="", compare=False)


def _messages(
    text: str, context: Sequence[str], glossary: Mapping[str, str] | None
) -> list[dict[str, str]]:
    """The two chat turns: a system message, then the bare sentence.

    The system message is the prompt followed by one line each for the context and
    the glossary, when given.
    """
    system = [SYSTEM_PROMPT]
    if context:
        system.append("Previous sentences (context only): " + " ".join(context))
    if glossary:
        pairs = "; ".join(f"{k}={v}" for k, v in glossary.items())
        system.append("Glossary: " + pairs)
    return [
        {"role": "system", "content": "\n".join(system)},
        {"role": "user", "content": text},
    ]


def _parse_models(body: dict) -> list[ModelInfo]:
    states = [((m.get("status") or {}).get("value") or "") for m in body["data"]]
    return [ModelInfo(m["id"], state == "loaded", str(state)) for m, state in zip(body["data"], states)]


def _max_tokens(text: str) -> int:
    """Room for the Turkish of `text`: 200 tokens, more past 17 words (a 40-word sentence gets 384)."""
    return max(200, 64 + 8 * len(text.split()))


def _whole_choice(body: dict, max_tokens: int) -> dict:
    """The reply's first choice; one cut off by the token limit is an error, so half a sentence is never spoken."""
    choice = body["choices"][0]
    if choice.get("finish_reason") == "length":
        raise TranslatorError(f"the translation was cut off at {max_tokens} tokens")
    return choice


def _parse_translation(body: dict, max_tokens: int) -> str:
    return strip_think(_whole_choice(body, max_tokens)["message"]["content"])


def _parse_completion(body: dict, max_tokens: int) -> str:
    return strip_think(_whole_choice(body, max_tokens)["text"])


# TranslateGemma (Google's translation-tuned Gemma 3) is trained on one fixed instruction, which
# its own chat template builds from a source and a target language code. llama.cpp's chat
# endpoint cannot pass those codes to the template, so the instruction is sent as a plain
# completion, written out exactly as the template renders it for English to Turkish.
TRANSLATEGEMMA_PROMPT = (
    "<start_of_turn>user\nYou are a professional English (en) to Turkish (tr) translator. Your goal is to "
    "accurately convey the meaning and nuances of the original English text while adhering to Turkish grammar, "
    "vocabulary, and cultural sensitivities.\nProduce only the Turkish translation, without any additional "
    "explanations or commentary. Please translate the following English text into Turkish:\n\n\n"
    "{text}<end_of_turn>\n<start_of_turn>model\n"
)


def is_translategemma(model: str) -> bool:
    return "translategemma" in model.lower()


class LlamaCppTranslator(Translator):
    """Translate through the OpenAI-compatible API of a llama.cpp llama-server.

    base_url is the server root; a trailing /v1 is accepted too, so one class
    serves the PC and an Ollama endpoint. api_key, when given, is sent as a Bearer
    token. timeout is one number of seconds for everything, or an httpx.Timeout to
    give connecting a limit of its own.

    A configured model is requested as it is. Without one, the first translate asks
    the server which model to use (see pick_model) and keeps the answer. With
    prefer_loaded every translate asks again, with one GET /v1/models, and requests
    the model that is loaded at that moment. So a model the owner loads later is
    followed, and no request makes the server swap a loaded model out. When none is
    loaded the configured model is requested if the server lists it, else the first
    listed one.

    A TranslateGemma model gets its own fixed instruction as a completion instead of the
    chat request (see TRANSLATEGEMMA_PROMPT); the context and glossary are not sent to it,
    and it cannot be asked to shorten.
    """

    name = "llamacpp"

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str | None = None,
        timeout: float | httpx.Timeout = 120.0,
        transport: httpx.BaseTransport | None = None,
        prefer_loaded: bool = False,
    ):
        self.model = model
        self.prefer_loaded = prefer_loaded
        self._picked: str | None = None
        self._client = httpx.Client(
            base_url=base_url.rstrip("/").removesuffix("/v1"),
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            transport=transport,
            timeout=timeout,
            # Sentences come every few seconds: keep the connection, which httpx would drop after 5 s.
            limits=httpx.Limits(keepalive_expiry=60.0),
        )

    def _request(
        self, method: str, path: str, parse: Callable[[Any], Any], **kwargs
    ) -> Any:
        """Send a request and return parse(its JSON body).

        A transport error, a status other than 200 and a body that parse cannot read
        all become a TranslatorError.
        """
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise TranslatorError(str(exc)) from exc
        if response.status_code != 200:
            raise TranslatorError(f"{response.status_code}: {response.text[:200]}")
        try:
            return parse(response.json())
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            raise TranslatorError(f"malformed reply: {response.text[:200]}") from exc

    def list_models(self) -> list[ModelInfo]:
        """The models the server lists, each flagged when it is currently loaded."""
        return self._request("GET", "/v1/models", _parse_models)

    def pick_model(self) -> str:
        """Choose the model to request.

        The configured model when the server lists it, else the loaded model, else
        the first listed one. With prefer_loaded the loaded model comes first, so a
        model the owner has loaded is used and not swapped out; the configured model
        is the fallback for when none is loaded. With prefer_loaded the server is asked
        on every call, because the owner can load another model at any moment. Without
        it the server is asked on the first call and the choice is kept.
        """
        if self._picked is not None and not self.prefer_loaded:
            return self._picked
        models = self.list_models()
        if not models:
            raise TranslatorError("no models listed")
        ids = [m.id for m in models]
        loaded = [m.id for m in models if m.loaded]
        configured = [self.model] if self.model in ids else []
        if self.prefer_loaded:
            groups = (loaded, configured, ids)
        else:
            groups = (configured, loaded, ids)
        self._picked = next(group[0] for group in groups if group)
        return self._picked

    def warm_up(self) -> str | None:
        """Make the server load the model the next sentence would use; return its id.

        Nothing is done, and None returned, unless every listed model says it is
        unloaded: a model that is loaded or loading is the owner's or on its way, and a
        server that reports no status is left alone. Otherwise one token is asked of the
        configured model if the server lists it, else of the first listed one, which
        makes the server load it. Raises TranslatorError when the server cannot be asked.
        """
        models = self.list_models()
        if not models:
            raise TranslatorError("no models listed")
        if any(m.state != "unloaded" for m in models):
            return None
        ids = [m.id for m in models]
        model = self.model if self.model in ids else ids[0]
        if is_translategemma(model):
            path, body = "/v1/completions", {"prompt": TRANSLATEGEMMA_PROMPT.format(text="Hi.")}
        else:
            path, body = "/v1/chat/completions", {"messages": [{"role": "user", "content": "Hi."}]}
        # The reply is cut off at its first token and thrown away: only the load matters.
        self._request("POST", path, lambda reply: None, json={"model": model, "max_tokens": 1, "temperature": 0, **body})
        return model

    def translate(
        self,
        text: str,
        context: Sequence[str] = (),
        glossary: Mapping[str, str] | None = None,
    ) -> str:
        model = self._model()
        if is_translategemma(model):
            return self._translate_translategemma(model, text)
        return self._chat(model, _messages(text, context, glossary), _max_tokens(text))

    def shorten(
        self,
        text: str,
        turkish: str,
        max_words: int,
        context: Sequence[str] = (),
        glossary: Mapping[str, str] | None = None,
    ) -> str:
        """A shorter rendering of `text` than `turkish`, in at most `max_words` words.

        The chat goes on from the first rendering: `turkish` is sent back as the model's own
        turn, then SHORTEN_REQUEST. TranslateGemma's fixed instruction has no room for such a
        request, so with it `turkish` comes back unchanged and nothing is sent.
        """
        model = self._model()
        if is_translategemma(model):
            return turkish
        messages = [
            *_messages(text, context, glossary),
            {"role": "assistant", "content": turkish},
            {"role": "user", "content": SHORTEN_REQUEST.format(max_words=max_words)},
        ]
        return self._chat(model, messages, _max_tokens(text))

    def _model(self) -> str:
        if self.model and not self.prefer_loaded:
            return self.model  # pinned: sent as it is, without asking the server
        return self.pick_model()

    def punctuate(self, text: str) -> str:
        """The model's copy of `text`, the words of a caption track without punctuation, with punctuation and capitals.

        The caller lays the copy over its own words and takes only the marks (see
        dubber.punctuation), so nothing here checks it. The model is the one `translate`
        would use. TranslateGemma only translates: asking it is an error, raised before
        any request.
        """
        model = self._model()
        if is_translategemma(model):
            raise TranslatorError("TranslateGemma only translates: it cannot restore punctuation")
        messages = [{"role": "system", "content": PUNCTUATION_PROMPT}, {"role": "user", "content": text}]
        # A copy has a token or two for each word, and its marks.
        return self._chat(model, messages, 3 * len(text.split()) + 64, temperature=0)

    def look(self, question: str, pictures: Sequence[bytes]) -> str:
        """The model's answer to `question` about `pictures`, each of them a JPEG file.

        The question is sent first and the pictures after it, in order. The model is the one
        `translate` would use. It sees pictures only when the PC loaded it with its vision
        add-on (the mmproj line of the preset); a model without one is refused by the server,
        and that refusal is the TranslatorError. TranslateGemma only translates: asking it is
        an error, raised before any request.
        """
        model = self._model()
        if is_translategemma(model):
            raise TranslatorError("TranslateGemma only translates: it cannot look at pictures")
        parts: list[dict[str, Any]] = [{"type": "text", "text": question}]
        for picture in pictures:
            url = "data:image/jpeg;base64," + base64.b64encode(picture).decode("ascii")
            parts.append({"type": "image_url", "image_url": {"url": url}})
        return self._chat(model, [{"role": "user", "content": parts}], LOOK_TOKENS, temperature=0)

    def _chat(self, model: str, messages: list[dict[str, Any]], max_tokens: int, temperature: float = 0.2) -> str:
        translation = self._request(
            "POST",
            "/v1/chat/completions",
            lambda body: _parse_translation(body, max_tokens),
            json={
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "messages": messages,
                "chat_template_kwargs": {"enable_thinking": False},
                "reasoning_format": "none",
                # Ollama's OpenAI-compatible endpoint reads "think"; llama-server ignores unknown fields.
                "think": False,
            },
        )
        if not translation:
            raise TranslatorError("empty translation")
        return translation

    def _translate_translategemma(self, model: str, text: str) -> str:
        """TranslateGemma's own instruction as a completion; it has no room for context or a glossary."""
        max_tokens = _max_tokens(text)
        translation = self._request(
            "POST",
            "/v1/completions",
            lambda body: _parse_completion(body, max_tokens),
            json={
                "model": model,
                "prompt": TRANSLATEGEMMA_PROMPT.format(text=text.strip()),
                "temperature": 0,
                "max_tokens": max_tokens,
                "stop": ["<end_of_turn>"],
            },
        )
        if not translation:
            raise TranslatorError("empty translation")
        return translation


# The PC translator waits this long to connect and this long for an answer. A PC that is off, or
# a Tailscale that is down, then fails a sentence in seconds, where one limit of 120 s for
# everything would hold it for minutes. A long translation is still allowed its 120 s.
PC_TIMEOUT = httpx.Timeout(120.0, connect=3.0)


def translator_from_config(cfg: dict) -> LlamaCppTranslator:
    """Build the PC translator from the [pc] table of the config.

    It asks the server which model is loaded before every translation and requests
    that one, and falls back to pc.preferred_model when none is, so dubbing never swaps
    out a model the owner is using (spec 7.8). Connecting is given PC_TIMEOUT's 3 s.
    Nothing is sent here: the key file is read and the client is made.
    """
    pc = cfg["pc"]
    key_file = pathlib.Path(pc["api_key_file"]).expanduser()
    api_key = key_file.read_text(encoding="utf-8").strip()
    return LlamaCppTranslator(
        pc["base_url"],
        api_key,
        model=pc.get("preferred_model"),
        timeout=PC_TIMEOUT,
        prefer_loaded=True,
    )
