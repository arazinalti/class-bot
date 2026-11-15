# Class Lecture Bot

A configurable Windows bot for joining online classes, capturing system audio, transcribing Persian speech with Groq Whisper, sending optional ntfy alerts, and creating English Markdown notes with Anthropic.

## Privacy

Personal names, course IDs, schedules, LMS URLs, browser sessions, transcripts, and API keys are kept outside the public source. Copy `config.example.json` to `config.json` and edit it locally. Copy `.env.example` to `.env` or set the variables in your shell. Never commit `config.json`, `.env`, `browser_profile`, `discover`, or `lectures`.

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install
Copy-Item config.example.json config.json
Copy-Item .env.example .env
```

Set the API keys in the environment. The program reads `GROQ_API_KEY` for transcription. `ANTHROPIC_API_KEY` and `ANTHROPIC_MODEL` are optional and enable note generation.

## Configure

Edit `config.json`: set the LMS URL, your course list, weekly schedule, notification topic, and the names/keywords that should trigger alerts. Set `browser.channel` to `chrome` or `msedge` when that browser is installed. Set `timing.even_week_saturday` to an ISO date such as `2026-09-19` when parity matters; leave it `null` to treat parity classes as weekly.

## First run

```powershell
python -X utf8 lecture_bot.py --login
python -X utf8 lecture_bot.py --discover
python -X utf8 lecture_bot.py --plan
```

`--login` opens the configured LMS and stores the browser session locally. `--discover` helps find online-class activities. `--plan` prints the next eight days.

## Run

```powershell
python -X utf8 lecture_bot.py --course 101
python -X utf8 lecture_bot.py --auto
python -X utf8 lecture_bot.py --url "https://your-lms.example.edu/mod/adobeconnect/view.php?id=123"
python -X utf8 lecture_bot.py --summarize lectures/example_transcript.txt
```

The bot is designed for Windows because it uses loopback recording through SoundCard and the Windows alarm API. You must have permission to record and transcribe the class audio.
