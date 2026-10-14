"""The model files the listener needs, where they come from, and their checksums.

    models/openwakeword/   melspectrogram.onnx, embedding_model.onnx, hey_jarvis_v0.1.onnx,
                           silero_vad.onnx                          (openWakeWord v0.5.1 release)
    models/kws/            encoder/decoder/joiner (int8), tokens.txt, bpe.model
                           (sherpa-onnx gigaspeech keyword-spotting model, 3.3M parameters)

All files come from the projects' GitHub releases and are checked against the
SHA-256 values below, so a corrupted or tampered download is refused.
openWakeWord's pre-trained models are licensed CC BY-NC-SA 4.0 (personal use is
fine); the sherpa-onnx model is Apache 2.0.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

log = logging.getLogger("x100.listener.models")

OWW_RELEASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
OWW_FILES: Dict[str, str] = {
    "melspectrogram.onnx": "ba2b0e0f8b7b875369a2c89cb13360ff53bac436f2895cced9f479fa65eb176f",
    "embedding_model.onnx": "70d164290c1d095d1d4ee149bc5e00543250a7316b59f31d056cff7bd3075c1f",
    "hey_jarvis_v0.1.onnx": "94a13cfe60075b132f6a472e7e462e8123ee70861bc3fb58434a73712ee0d2cb",
    "silero_vad.onnx": "a35ebf52fd3ce5f1469b2a36158dba761bc47b973ea3382b3186ca15b1f5af28",
}

KWS_NAME = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
KWS_URL = f"https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/{KWS_NAME}.tar.bz2"
KWS_SHA256 = "f170013b4716e41b62b9bfd809687c207cef798ef9bc6534d524e17af9b6561a"
KWS_STEM = "epoch-12-avg-2-chunk-16-left-64.int8.onnx"
KWS_FILES: Dict[str, str] = {
    f"encoder-{KWS_STEM}": "1e721676515bcd42a186979733981213c66c80db680e1cc582dfedf3be76e678",
    f"decoder-{KWS_STEM}": "e40ff43297abe815e8898494c17e71bba2152d9d40fa3eb803f75d0f7533329a",
    f"joiner-{KWS_STEM}": "eae9da0c7e1e6c6a3f4cc42d167899c388f6c6701b94cb96320e4f55df79624c",
    "tokens.txt": "fd2ded4050a55d2b1578870ba8697d02371980217806b7558bd0a5cc60f3ba53",
    "bpe.model": "c8a2a0129c4ab8e463164c142f82d25649661b122c8cd0b7aab5c9e80b90ad24",
}


class ModelError(RuntimeError):
    pass


@dataclass
class ModelPaths:
    root: str

    @property
    def oww_dir(self) -> str:
        return os.path.join(self.root, "openwakeword")

    @property
    def kws_dir(self) -> str:
        return os.path.join(self.root, "kws")

    def oww(self, name: str) -> str:
        return os.path.join(self.oww_dir, name)

    def wake_model(self, name: str) -> str:
        """``hey_jarvis_v0.1`` (a file in models/openwakeword/) or a path to your own .onnx model."""
        if name.endswith(".onnx") and os.sep in name:
            return name
        return self.oww(name if name.endswith(".onnx") else name + ".onnx")

    def kws(self, name: str) -> str:
        return os.path.join(self.kws_dir, name)

    def missing(self, wake_model: str = "hey_jarvis_v0.1", vad: bool = False) -> List[str]:
        needed = [self.oww("melspectrogram.onnx"), self.oww("embedding_model.onnx"), self.wake_model(wake_model)]
        if vad:
            needed.append(self.oww("silero_vad.onnx"))
        needed += [self.kws(f) for f in KWS_FILES]
        return [p for p in needed if not os.path.isfile(p)]


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "x100-listener"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def verify(paths: ModelPaths) -> List[str]:
    """Checksum problems with the model files that are present (an empty list means all fine)."""
    bad = []
    for path, digest in [(paths.oww(n), d) for n, d in OWW_FILES.items()] + \
                        [(paths.kws(n), d) for n, d in KWS_FILES.items()]:
        if os.path.isfile(path) and sha256_file(path) != digest:
            bad.append(f"{path} doesn't match its published checksum; run --download-models --force")
    return bad


def download(paths: ModelPaths, fetch: Callable[[str], bytes] = _fetch, force: bool = False,
             report: Optional[Callable[[str], None]] = print) -> None:
    say = report or (lambda _msg: None)
    os.makedirs(paths.oww_dir, exist_ok=True)
    os.makedirs(paths.kws_dir, exist_ok=True)
    for name, digest in OWW_FILES.items():
        dest = paths.oww(name)
        if not force and os.path.isfile(dest) and sha256_file(dest) == digest:
            say(f"  have {name}")
            continue
        say(f"  downloading {name}")
        data = fetch(OWW_RELEASE + name)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ModelError(f"{name}: checksum mismatch, refusing to install it")
        _write_atomic(dest, data)

    if not force and all(os.path.isfile(paths.kws(f)) for f in KWS_FILES):
        say(f"  have the keyword-spotting model ({KWS_NAME})")
        return
    say(f"  downloading {KWS_NAME} (18 MB)")
    data = fetch(KWS_URL)
    if hashlib.sha256(data).hexdigest() != KWS_SHA256:
        raise ModelError(f"{KWS_NAME}: checksum mismatch, refusing to install it")
    wanted = {f"{KWS_NAME}/{f}": f for f in KWS_FILES}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:bz2") as tar:
        found = 0
        for member in tar.getmembers():
            target = wanted.get(member.name)
            if target is None or not member.isfile():
                continue  # only the files we name; nothing else from the archive touches disk
            fh = tar.extractfile(member)
            assert fh is not None
            content = fh.read()
            if hashlib.sha256(content).hexdigest() != KWS_FILES[target]:
                raise ModelError(f"{KWS_NAME}/{target}: checksum mismatch, refusing to install it")
            _write_atomic(paths.kws(target), content)
            found += 1
    if found != len(KWS_FILES):
        raise ModelError(f"{KWS_NAME}: archive is missing files ({found} of {len(KWS_FILES)} found)")


def _write_atomic(dest: str, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix=".part-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o644)
        shutil.move(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
