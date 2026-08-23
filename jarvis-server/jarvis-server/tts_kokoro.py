#!/usr/bin/env python3
"""
Kokoro TTS wrapper — called by the Node.js server for server-side synthesis.

Usage:
    python tts_kokoro.py --output /path/to/output.wav "Text to speak"
    python tts_kokoro.py --output /path/to/output.wav --voice bm_daniel "Text to speak"

The server writes the WAV to a temp file and reads it back.
"""
import sys
import os
import argparse
import numpy as np
import soundfile as sf
from kokoro import KPipeline

DEFAULT_VOICE = "bm_george"
DEFAULT_LANG = "b"  # British English
DEFAULT_RATE = 1.0

def main():
    parser = argparse.ArgumentParser(description="Kokoro TTS synthesis")
    parser.add_argument("text", help="Text to synthesize")
    parser.add_argument("--output", "-o", required=True, help="Output WAV file path")
    parser.add_argument("--voice", default=DEFAULT_VOICE, help=f"Voice name (default: {DEFAULT_VOICE})")
    parser.add_argument("--lang", default=DEFAULT_LANG, help=f"Language code (default: {DEFAULT_LANG})")
    parser.add_argument("--rate", type=float, default=DEFAULT_RATE, help=f"Speech speed (default: {DEFAULT_RATE})")
    args = parser.parse_args()

    if not args.text.strip():
        sys.exit(0)

    pipeline = KPipeline(lang_code=args.lang)
    all_audio = []

    for _, _, audio in pipeline(args.text, voice=args.voice, speed=args.rate):
        if audio is not None and len(audio) > 0:
            all_audio.append(audio)

    if not all_audio:
        print("[tts_kokoro] No audio generated.", file=sys.stderr)
        sys.exit(1)

    full_audio = np.concatenate(all_audio)
    sf.write(args.output, full_audio, 24000)
    print(f"[tts_kokoro] Wrote {len(full_audio)} samples to {args.output}", file=sys.stderr)

if __name__ == "__main__":
    main()
