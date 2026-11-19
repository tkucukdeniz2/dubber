"""Warm the PC's engines before the first sentence needs them.

After the PC enters dubbing mode its translator has no model loaded, and the first request
waits for the load: 11.5 s on 2026-11-14, long enough to run the start-up hold into its cap.
The voice's first clip is slow too. A `Warmer` makes both happen early, in the background:
when `dubber serve` starts and whenever the extension connects.

It asks the voice service first. An answer that it is ready also says that the PC is in
dubbing mode, where loading the translator is what the owner wants; without that answer
nothing is asked of the translator, which in the PC's other mode serves the owner's own
model. The translator in turn loads a model only when none is loaded or loading.
"""

import logging
import threading
import time
from collections.abc import Callable
from typing import Protocol

from dubber.engines.base import TranslatorError, VoiceError, failure_reason

log = logging.getLogger(__name__)


class _Voice(Protocol):
    def status(self) -> dict: ...

    def warm_up(self) -> None: ...


class _Translator(Protocol):
    def warm_up(self) -> str | None: ...


def _in_a_thread(call: Callable[[], None]) -> None:
    """Run `call` on a daemon thread, which never keeps the process from ending."""
    threading.Thread(target=call, name="dubber-warm-up", daemon=True).start()


_NEVER = object()


class Warmer:
    """Warms the PC voice and translator, one warm-up at a time.

    `run_in_background` starts the warm-up and returns at once; by default it is a thread.
    """

    def __init__(
        self,
        translator: _Translator,
        voice: _Voice,
        run_in_background: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._translator = translator
        self._voice = voice
        self._run = run_in_background or _in_a_thread
        self._lock = threading.Lock()
        self._running = False
        # The start of the voice service that was last warmed, told by how long its model took to load.
        self._spoke_for: object = _NEVER

    def kick(self) -> bool:
        """Start a warm-up unless one is running; return whether one was started. Never raises."""
        with self._lock:
            if self._running:
                return False
            self._running = True
        self._run(self._warm)
        return True

    def _warm(self) -> None:
        try:
            self._warm_voice()
            self._warm_translator()
        except (VoiceError, TranslatorError) as exc:
            # The usual reason: the PC is not in dubbing mode, or is still starting its voice.
            log.info("no warm-up: %s", failure_reason(exc))
        except Exception:
            log.exception("the warm-up failed")
        finally:
            with self._lock:
                self._running = False

    def _warm_voice(self) -> None:
        """Speak a word if this start of the service has not spoken for us yet; VoiceError when it is not ready."""
        status = self._voice.status()
        if not status.get("ready"):
            raise VoiceError("the PC voice is still loading its model")
        started = status.get("loaded_in_s")
        if started == self._spoke_for:
            return
        began = time.perf_counter()
        self._voice.warm_up()
        self._spoke_for = started
        log.info("warm-up: the PC voice spoke in %.1f s", time.perf_counter() - began)

    def _warm_translator(self) -> None:
        began = time.perf_counter()
        model = self._translator.warm_up()
        if model is None:
            log.debug("warm-up: the translator needs none")
        else:
            log.info("warm-up: the translator loaded %s in %.1f s", model, time.perf_counter() - began)
