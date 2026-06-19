#!/usr/bin/env python3
"""Transcribe ONE audio file with faster-whisper and print the text to stdout.

Run with a python that has faster-whisper installed (the bridge runs under the
system python3, which does NOT, so tg_bridge.py invokes this script via subprocess
using a venv python — see WHISPER_PY in tg_bridge.py). Keeping it out-of-process
means the bridge never loads the ~150 MB model permanently.

    <venv-python> whisper_transcribe.py /path/to/voice.oga   # prints the transcript
"""
import sys

from faster_whisper import WhisperModel


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: whisper_transcribe.py <audio-path>")
    # "base" model is already cached under ~/.cache/huggingface/hub; cpu + int8
    # matches the other bot and is the right trade-off for short voice notes.
    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, _info = model.transcribe(sys.argv[1])
    text = " ".join(seg.text for seg in segments).strip()
    sys.stdout.write(text)


if __name__ == "__main__":
    main()
