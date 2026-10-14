"""Stage 2: the command, listened for in a short window after the wake word.

sherpa-onnx's keyword spotter is open-vocabulary: phrases are plain text,
turned into the model's own BPE tokens at start-up, so a new phrase is one
table entry rather than a training run. Every phrase maps to bridge ops.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

import numpy as np

from .audio import RATE
from .models import ModelError, ModelPaths, KWS_STEM

log = logging.getLogger("x100.listener.commands")

ON = [{"op": "power", "value": "ON"}]
OFF = [{"op": "power", "value": "OFF"}]

DEFAULT_PHRASES: Dict[str, List[Dict[str, Any]]] = {
    "projector on": ON,
    "turn on the projector": ON,
    "turn the projector on": ON,
    "switch on the projector": ON,
    "projector off": OFF,
    "turn off the projector": OFF,
    "turn the projector off": OFF,
    "switch off the projector": OFF,
}


@dataclass(frozen=True)
class Phrase:
    text: str                                   # upper case, as the model's tokens are
    ops: List[Dict[str, Any]] = field(compare=False, hash=False)

    def describe(self) -> str:
        return ", ".join(f"{o.get('op')}={o.get('value')}" for o in self.ops)


def normalise(text: str) -> str:
    t = " ".join(text.upper().replace("-", " ").split())
    if not re.fullmatch(r"[A-Z' ]+", t):
        raise ValueError(f"phrase {text!r}: use letters only, with numbers as words "
                         "(\"projector HDMI two\", not \"HDMI 2\")")
    return t


def build_phrases(extra: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> List[Phrase]:
    """The default power phrases plus EXTRA_PHRASES (which may also redefine a default)."""
    table: Dict[str, List[Dict[str, Any]]] = {}
    for text, ops in list(DEFAULT_PHRASES.items()) + list((extra or {}).items()):
        table[normalise(text)] = list(ops)
    return [Phrase(text, ops) for text, ops in table.items()]


class CommandSpotter(Protocol):
    phrases: List[Phrase]

    def start(self) -> None:
        """Begin a fresh command window."""
        ...

    def feed(self, frame: np.ndarray) -> Optional[Phrase]:
        """One 80 ms frame (16 kHz int16); returns a phrase once it has been heard."""
        ...

    def stop(self) -> None: ...


class SherpaKeywordSpotter:
    """Defaults were tuned on synthesised speech (tests/test_voice_e2e.py): a beam of 6 paths finds the
    fragmented "PRO J E C T OR" tokens far more often than 4, and two trailing blanks cost ~60 ms but
    make a false command less likely."""

    def __init__(self, paths: ModelPaths, phrases: List[Phrase], threshold: float = 0.25, boost: float = 2.0,
                 threads: int = 1, max_active_paths: int = 6, trailing_blanks: int = 2):
        try:
            import sentencepiece
            import sherpa_onnx
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise ModelError("sherpa-onnx / sentencepiece aren't installed: pip install 'x100-control[listener]'") \
                from exc
        files = {k: paths.kws(f"{k}-{KWS_STEM}") for k in ("encoder", "decoder", "joiner")}
        files.update(tokens=paths.kws("tokens.txt"), bpe=paths.kws("bpe.model"))
        for p in files.values():
            if not os.path.isfile(p):
                raise ModelError(f"missing model file {p}: run `x100-listener --download-models`")
        with open(files["tokens"], encoding="utf-8") as fh:
            vocab = {line.split()[0] for line in fh if line.strip()}
        sp = sentencepiece.SentencePieceProcessor(model_file=files["bpe"])
        self.phrases = phrases
        lines = []
        for i, phrase in enumerate(phrases):
            tokens = sp.encode(phrase.text, out_type=str)
            unknown = [t for t in tokens if t not in vocab]
            if unknown:
                raise ModelError(f"phrase {phrase.text!r} has tokens the model doesn't know: {unknown}")
            lines.append(" ".join(tokens) + f" :{boost:g} #{threshold:g} @{i}")
            log.debug("phrase %d %r -> %s", i, phrase.text, tokens)
        with tempfile.TemporaryDirectory() as tmp:
            kw_file = os.path.join(tmp, "keywords.txt")
            with open(kw_file, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            self.kws = sherpa_onnx.KeywordSpotter(
                tokens=files["tokens"], encoder=files["encoder"], decoder=files["decoder"],
                joiner=files["joiner"], keywords_file=kw_file, num_threads=threads,
                max_active_paths=max_active_paths, keywords_score=boost, keywords_threshold=threshold,
                num_trailing_blanks=trailing_blanks, provider="cpu")
        self._stream = None
        log.info("command phrases: %s", "; ".join(f"{p.text.lower()} -> {p.describe()}" for p in phrases))

    def start(self) -> None:
        self._stream = self.kws.create_stream()

    def feed(self, frame: np.ndarray) -> Optional[Phrase]:
        if self._stream is None:
            return None
        self._stream.accept_waveform(RATE, frame.astype(np.float32) / 32768.0)
        hit: Optional[Phrase] = None
        while self.kws.is_ready(self._stream):
            self.kws.decode_stream(self._stream)
            label = self.kws.get_result(self._stream)
            if label:
                self.kws.reset_stream(self._stream)
                if hit is None:
                    try:
                        hit = self.phrases[int(label)]
                    except (ValueError, IndexError):  # pragma: no cover - labels are ours
                        log.warning("unexpected keyword label %r", label)
        return hit

    def stop(self) -> None:
        self._stream = None
