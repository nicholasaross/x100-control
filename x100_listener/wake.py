"""Stage 1: the always-on wake word ("Hey Jarvis"), via openWakeWord's ONNX models.

``listener.py`` only needs the ``WakeDetector`` interface, so another engine
(for example a sherpa-onnx "hey jarvis" keyword) can replace this one.
"""

from __future__ import annotations

import logging
import os
from collections import deque
from typing import Protocol

import numpy as np

from .models import ModelError, ModelPaths

log = logging.getLogger("x100.listener.wake")

# openWakeWord keeps 10 s of raw audio and copies all of it into a Python list on every 80 ms frame,
# though it only ever reads the last 0.11 s. Keeping 1 s gives identical scores, and took a third off
# the wake word's time per frame on a PC.
RAW_HISTORY = 16000


class WakeDetector(Protocol):
    name: str

    def score(self, frame: np.ndarray) -> float:
        """Feed one 80 ms frame (16 kHz int16); returns 0..1 for that moment."""
        ...

    def reset(self) -> None: ...


class OpenWakeWordDetector:
    def __init__(self, paths: ModelPaths, model: str = "hey_jarvis_v0.1", vad_threshold: float = 0.0,
                 speex_ns: bool = False, threads: int = 1):
        """``threads``: how many cores openWakeWord's two feature models, its main cost, may use."""
        try:
            import onnxruntime
            from openwakeword.model import Model
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise ModelError("openwakeword isn't installed: pip install 'x100-control[listener]'") from exc
        try:  # belt and braces with ORT_DISABLE_TELEMETRY (set in x100_listener/__init__.py)
            onnxruntime.disable_telemetry_events()
        except Exception:  # pragma: no cover - older builds have no telemetry to turn off
            pass
        wake_path = paths.wake_model(model)
        melspec, embedding = paths.oww("melspectrogram.onnx"), paths.oww("embedding_model.onnx")
        for p in (wake_path, melspec, embedding):
            if not os.path.isfile(p):
                raise ModelError(f"missing model file {p}: run `x100-listener --download-models`")
        if speex_ns:
            try:
                import speexdsp_ns  # noqa: F401
            except ImportError as exc:
                raise ModelError("SPEEX_NS=true needs `pip install speexdsp-ns`") from exc
        threads = max(1, int(threads))
        self.model = Model(wakeword_models=[wake_path], inference_framework="onnx",
                           melspec_model_path=melspec, embedding_model_path=embedding,
                           enable_speex_noise_suppression=speex_ns, ncpu=1)
        if threads > 1:
            self._use_threads(onnxruntime, melspec, embedding, threads)
        if vad_threshold > 0:
            # openWakeWord only looks for the VAD model inside its own package, so load ours.
            from openwakeword.vad import VAD

            vad_path = paths.oww("silero_vad.onnx")
            if not os.path.isfile(vad_path):
                raise ModelError(f"VAD_THRESHOLD is set but {vad_path} is missing: run --download-models")
            self.model.vad = VAD(model_path=vad_path)
            self.model.vad_threshold = vad_threshold
        self.name = next(iter(self.model.models))
        pre = self.model.preprocessor
        if isinstance(getattr(pre, "raw_data_buffer", None), deque):  # openwakeword 0.6.0
            pre.raw_data_buffer = deque(pre.raw_data_buffer, maxlen=RAW_HISTORY)
        self._settle()
        self._quiet = pre.feature_buffer.copy() if hasattr(pre, "feature_buffer") else None
        log.info("wake word model %s (%d thread%s)%s%s", self.name, threads, "" if threads == 1 else "s",
                 f", VAD {vad_threshold:g}" if vad_threshold else "", ", Speex noise suppression" if speex_ns else "")

    def _use_threads(self, ort, melspec: str, embedding: str, threads: int) -> None:
        """The two feature models on ``threads`` cores, with ONNX Runtime's spin-waiting off.

        openWakeWord's own ``ncpu`` leaves spinning on, and its spare threads then spin flat out
        between frames: on the Pi 3, 2 threads used 236% of a core for frames that took 28 ms of
        every 80. Without spinning they sleep until there's work; on a PC that took CPU use from
        112% to 6% of a core, and the frames were no slower."""
        pre = self.model.preprocessor
        if not (hasattr(pre, "melspec_model") and hasattr(pre, "embedding_model")):  # pragma: no cover
            return
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = threads
        opts.inter_op_num_threads = 1
        opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        # openWakeWord's predict functions look these up on every call, so swapping them is enough.
        pre.melspec_model = ort.InferenceSession(melspec, sess_options=opts, providers=["CPUExecutionProvider"])
        pre.embedding_model = ort.InferenceSession(embedding, sess_options=opts, providers=["CPUExecutionProvider"])

    def _settle(self) -> None:
        """Start from silence rather than noise.

        openWakeWord seeds its feature history with embeddings of *random* audio (on load and on
        every reset), so the first second of scores varied from run to run on identical input.
        Seeding it with silence makes ``--wav`` replays and tests repeatable, and is closer to a
        quiet room anyway. Live listening replaces this history within ~1.3 s either way.
        """
        pre = self.model.preprocessor
        if hasattr(pre, "_get_embeddings") and hasattr(pre, "feature_buffer"):  # openwakeword 0.6.0
            pre.feature_buffer = pre._get_embeddings(np.zeros(16000 * 4, dtype=np.int16))
        else:  # pragma: no cover - a future openwakeword: push silence through instead
            for _ in range(50):
                self.model.predict(np.zeros(1280, dtype=np.int16))
            self.model.prediction_buffer.clear()

    def score(self, frame: np.ndarray) -> float:
        return float(self.model.predict(frame)[self.name])

    def reset(self) -> None:
        """Back to a quiet room. openWakeWord's own reset recomputes 4 s of features from random noise,
        over a second's work on a Pi 3; this reuses the quiet features worked out once at load."""
        pre = self.model.preprocessor
        if self._quiet is None or not hasattr(pre, "raw_data_buffer"):  # pragma: no cover - other versions
            self.model.reset()
            self._settle()
            return
        for history in self.model.prediction_buffer.values():
            history.clear()
        pre.raw_data_buffer.clear()
        pre.melspectrogram_buffer = np.ones((76, 32))  # as openWakeWord 0.6.0 starts it
        pre.accumulated_samples = 0
        pre.raw_data_remainder = np.empty(0)
        pre.feature_buffer = self._quiet.copy()
