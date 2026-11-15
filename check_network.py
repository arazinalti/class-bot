import socket
import requests

TARGETS = {
    "Groq (گفتار-به-متن)": "https://api.groq.com/openai/v1/models",
    "OpenAI (گفتار-به-متن)": "https://api.openai.com/v1/models",
    "Deepgram (گفتار-به-متن)": "https://api.deepgram.com/v1/projects",
    "Anthropic (خلاصه‌سازی)": "https://api.anthropic.com/v1/models",
    "ntfy (اعلان)": "https://ntfy.sh",
    "Hugging Face (مدل)": "https://huggingface.co",
    "PyPI": "https://pypi.org",
    "Playwright CDN (مرورگر)": "https://cdn.playwright.dev",
}


def check(name, url):
    host = url.split("/")[2]
    try:
        socket.gethostbyname(host)
    except OSError:
        print(f"[DNS ✗] {name}: آدرس پیدا نشد")
        return
    try:
        r = requests.get(url, timeout=10)
    except requests.RequestException as e:
        print(f"[اتصال ✗] {name}: {type(e).__name__}")
        return
    body = r.text[:120].replace("\n", " ")
    geo = any(k in r.text.lower() for k in ("unsupported_country", "not available in your", "region"))
    if geo:
        print(f"[مسدود به‌خاطر کشور ✗] {name}: HTTP {r.status_code} - {body}")
    else:
        print(f"[باز ✓] {name}: HTTP {r.status_code}")


for n, u in TARGETS.items():
    check(n, u)
