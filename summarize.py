import os
import sys
import pathlib
import anthropic
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        return False

load_dotenv(pathlib.Path(__file__).resolve().parent / ".env")
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")

SYSTEM = (
    "You are a meticulous university teaching assistant. The input is an automatic "
    "speech-recognition transcript (Persian) of a lecture; it contains recognition errors, "
    "filler words and timestamps. Infer the intended words where the error is obvious, and "
    "mark anything uncertain with (?). Do not invent content that is not in the transcript. "
    "Write everything in clear English."
)

PROMPT = """Below is an automatic transcript of a class session. Produce Markdown notes in English:

## Overview
(5-8 sentences)

## Detailed notes
(in order of appearance; keep definitions, formulas and important examples)

## Key takeaways
(short list)

## Homework, exams and announcements
(anything about deadlines, grades, exams or schedule, with approximate time [hh:mm:ss])

## Questions and unclear parts
(questions asked in class, or parts that need review)

--- TRANSCRIPT ---
{transcript}
"""


def summarize_file(path):
    path = pathlib.Path(path)
    transcript = path.read_text(encoding="utf-8")
    if not transcript.strip():
        raise ValueError("Transcript file is empty.")
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM,
        messages=[{"role": "user", "content": PROMPT.format(transcript=transcript)}],
    )
    notes = "".join(b.text for b in resp.content if b.type == "text")
    out = path.with_name(
        path.name.replace("_transcript", "_notes").replace(".txt", ".md")
    )
    out.write_text(notes, encoding="utf-8")
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python summarize.py <transcript.txt>")
    print(summarize_file(sys.argv[1]))
