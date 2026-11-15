import io
import os
import re
import sys
import time
import wave
import queue
import winsound
import warnings
import datetime
import pathlib
import ctypes
import threading
import subprocess
from urllib.parse import urlparse

import numpy as np
import requests
import soundcard as sc
from scipy.signal import resample_poly
from rapidfuzz import fuzz

from settings import (
    BROWSER_CHANNEL,
    COURSE_URL,
    COURSES,
    END_BUFFER_MIN,
    EVEN_WEEK_SATURDAY,
    JOIN_EARLY_MIN,
    KEYWORDS,
    LMS_HOST,
    LMS_URL,
    MY_NAMES,
    NTFY_SERVER,
    NTFY_TOPIC,
    SCHEDULE,
)

BASE = pathlib.Path(__file__).resolve().parent
PROFILE_DIR = BASE / "browser_profile"
DISCOVER_DIR = BASE / "discover"
OUT_DIR = BASE / "lectures"

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

ACTIVITY_SELECTORS = ['a[href*="/mod/adobeconnect/"]', 'a[href*="adobeconnect"]']
ACTIVITY_TEXT = re.compile(
    r"adobe|connect|کلاس آنلاین|کلاس مجازی|اتاق|جلسه|آنلاین", re.I
)
JOIN_PATTERNS = [
    r"open in browser",
    r"join (the )?(meeting|room|session)",
    r"^join$",
    r"listen only",
    r"ورود به (جلسه|اتاق|کلاس)",
    r"پیوستن",
]
RENAV_SEC = 300
CLICK_RESET_SEC = 60

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_MODEL = "whisper-large-v3"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

CHUNK_SEC = 10
NATIVE_SR = 48000
SILENCE_RMS = 0.002
FUZZY_THRESHOLD = 85
ALERT_COOLDOWN = 45
MAX_QUEUE = 6

END_SILENCE_MIN = 25
MAX_WAIT_MIN = 180
MAX_RUN_MIN = 240

warnings.filterwarnings("ignore", category=sc.SoundcardRuntimeWarning)

audio_q = queue.Queue()
stop_event = threading.Event()
state = {"start": time.time(), "last_speech": None, "reason": "", "failed": False}


def normalize(t):
    t = t.replace("ي", "ی").replace("ك", "ک").replace("\u200c", " ")
    t = re.sub(r"[\u064B-\u065F\u0670]", "", t)
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def notify(title, message, priority="high", tags="bell"):
    if not NTFY_TOPIC:
        return
    try:
        requests.post(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority, "Tags": tags},
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"[!] ارسال اعلان ناموفق بود: {e}")


def local_alarm():
    def run():
        for _ in range(10):
            winsound.Beep(1200, 350)
            winsound.Beep(800, 350)

    threading.Thread(target=run, daemon=True).start()


def find_name(text):
    norm = normalize(text)
    for raw in MY_NAMES:
        n = normalize(raw)
        if not n:
            continue
        if len(n) < 4:
            if re.search(rf"(?<!\w){re.escape(n)}(?!\w)", norm):
                return raw
        elif fuzz.partial_ratio(n, norm) >= FUZZY_THRESHOLD:
            return raw
    return None


def find_keywords(text):
    norm = normalize(text)
    return [k for k in KEYWORDS if normalize(k) in norm]


def to_wav(audio16k):
    pcm = (np.clip(audio16k, -1, 1) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm.tobytes())
    buf.seek(0)
    return buf


def transcribe(audio16k):
    for attempt in range(3):
        try:
            r = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                files={"file": ("chunk.wav", to_wav(audio16k), "audio/wav")},
                data={
                    "model": GROQ_MODEL,
                    "language": "fa",
                    "response_format": "verbose_json",
                    "temperature": "0",
                },
                timeout=60,
            )
            if r.status_code == 429:
                time.sleep(float(r.headers.get("retry-after", 5)))
                continue
            if r.status_code != 200:
                print(f"[!] خطای Groq {r.status_code}: {r.text[:200]}")
                return ""
            parts = []
            for s in r.json().get("segments", []):
                if s.get("no_speech_prob", 0) > 0.6:
                    continue
                if s.get("avg_logprob", 0) < -1.2:
                    continue
                if s.get("compression_ratio", 0) > 2.4:
                    continue
                parts.append(s["text"].strip())
            return " ".join(parts).strip()
        except requests.RequestException as e:
            print(f"[!] خطای شبکه ({attempt + 1}/3): {type(e).__name__}")
            time.sleep(2)
    return ""


def capture_loop():
    speaker = sc.default_speaker()
    mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
    print(f"[*] ضبط صدای: {speaker.name}")
    with mic.recorder(samplerate=NATIVE_SR, channels=1) as rec:
        while not stop_event.is_set():
            data = rec.record(numframes=NATIVE_SR * CHUNK_SEC)[:, 0]
            audio_q.put(data.astype(np.float32))


def transcribe_loop(transcript_path):
    prev_tail = ""
    last_alert = {}

    while not (stop_event.is_set() and audio_q.empty()):
        try:
            raw = audio_q.get(timeout=1)
        except queue.Empty:
            continue

        dropped = 0
        while audio_q.qsize() > MAX_QUEUE:
            audio_q.get_nowait()
            dropped += 1
        if dropped:
            print(
                f"[!] {dropped} تکه‌ی قدیمی دور ریخته شد تا ربات به زمان واقعی برگردد."
            )

        if float(np.sqrt(np.mean(raw**2))) < SILENCE_RMS:
            continue

        audio = resample_poly(raw, 1, NATIVE_SR // 16000).astype(np.float32)
        text = transcribe(audio)
        if not text:
            continue

        first = state["last_speech"] is None
        state["last_speech"] = time.time()
        if first:
            notify(
                "Class speech detected",
                "صدای کلاس شروع شد 🎓",
                priority="default",
                tags="mortar_board",
            )

        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] {text}")
        with open(transcript_path, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {text}\n")

        window = f"{prev_tail} {text}"
        prev_tail = text[-40:]
        now = time.time()

        hit = find_name(window)
        if hit and now - last_alert.get("name", 0) > ALERT_COOLDOWN:
            last_alert["name"] = now
            local_alarm()
            notify(
                "Your name was mentioned!",
                f"🔔 {text}",
                priority="urgent",
                tags="rotating_light",
            )
            print(f"    >>> آلارم نام ({hit})")

        kws = find_keywords(text)
        if kws and now - last_alert.get("kw", 0) > ALERT_COOLDOWN:
            last_alert["kw"] = now
            notify(
                "Important keyword",
                f"📌 {'، '.join(kws)}\n{text}",
                priority="default",
                tags="memo",
            )
            print(f"    >>> کلمه‌ی کلیدی: {kws}")


def launch(p):
    return p.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        channel=BROWSER_CHANNEL,
        headless=False,
        no_viewport=True,
        args=["--autoplay-policy=no-user-gesture-required"],
    )


def first_page(ctx):
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def in_room(ctx):
    for pg in ctx.pages:
        try:
            u = pg.url
        except Exception:
            continue
        host = urlparse(u).hostname or ""
        if host and (not LMS_HOST or host != LMS_HOST) and not re.search(r"login|sso|auth|/cas", u, re.I):
            return True
    return False


def click_pattern(page, pat):
    rx = re.compile(pat, re.I)
    for loc in (
        page.get_by_role("button", name=rx),
        page.get_by_role("link", name=rx),
        page.get_by_text(rx),
    ):
        try:
            item = loc.first
            if item.is_visible(timeout=300):
                item.click(timeout=2000)
                return True
        except Exception:
            continue
    return False


def try_join(ctx, clicked):
    for page in list(ctx.pages):
        for pat in JOIN_PATTERNS:
            if pat in clicked:
                continue
            if click_pattern(page, pat):
                clicked.add(pat)
                print(f"[*] کلیک روی «{pat}»")


def find_activity(page):
    for sel in ACTIVITY_SELECTORS:
        loc = page.locator(sel)
        if loc.count():
            return loc.first.get_attribute("href")
    loc = page.locator("a.aalink").filter(has_text=ACTIVITY_TEXT)
    if loc.count():
        return loc.first.get_attribute("href")
    return None


def run_login():
    from playwright.sync_api import sync_playwright

    print("[*] وارد سایت دانشگاه شو و بعد فقط پنجره‌ی مرورگر را ببند.")
    with sync_playwright() as p:
        ctx = launch(p)
        page = first_page(ctx)
        try:
            page.goto(LMS_URL, timeout=60000)
        except Exception as e:
            print(f"[!] باز نشدن صفحه: {type(e).__name__}")
        try:
            while ctx.pages:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        try:
            ctx.close()
        except BaseException:
            pass
    print("[✓] نشست ذخیره شد.")


def run_discover():
    from playwright.sync_api import sync_playwright

    DISCOVER_DIR.mkdir(exist_ok=True)
    pattern = re.compile(r"adobeconnect|connect|bigblue|mod/url|zoom", re.I)
    with sync_playwright() as p:
        ctx = launch(p)
        page = first_page(ctx)
        for cid, name in COURSES.items():
            print(f"\n=== {name} ({cid}) ===")
            try:
                page.goto(COURSE_URL.format(cid), timeout=60000)
                if "/login/" in page.url:
                    print(
                        "[!] نشست منقضی است. اول python lecture_bot.py --login را اجرا کن."
                    )
                    break
                page.screenshot(path=str(DISCOVER_DIR / f"{cid}.png"), full_page=True)
                links = page.eval_on_selector_all(
                    "a[href]",
                    "els => els.map(e => [(e.innerText||'').trim().slice(0,60), e.href])",
                )
                seen = set()
                for text, href in links:
                    if "sesskey" in href or href in seen:
                        continue
                    if pattern.search(href) or ACTIVITY_TEXT.search(text):
                        seen.add(href)
                        print(f"  لینک: {text} -> {href}")
                act = find_activity(page)
                if act:
                    page.goto(act, timeout=60000)
                    page.screenshot(
                        path=str(DISCOVER_DIR / f"{cid}_activity.png"), full_page=True
                    )
                    btns = page.eval_on_selector_all(
                        "button, input[type=submit], input[type=button], a.btn",
                        "els => els.map(e => (e.innerText||e.value||'').trim()).filter(Boolean)",
                    )
                    print(f"  فعالیت: {act}")
                    print(f"  دکمه‌ها: {btns}")
                else:
                    print("  فعالیت Adobe Connect پیدا نشد.")
            except Exception as e:
                print(f"  [!] خطا: {type(e).__name__}: {e}")
        try:
            ctx.close()
        except BaseException:
            pass
    print(f"\n[✓] تمام شد. اسکرین‌شات‌ها در پوشه‌ی {DISCOVER_DIR}")


def run_class(course_id, direct_url):
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            ctx = launch(p)
            page = first_page(ctx)
            activity_url = direct_url
            last_nav = 0.0
            last_reset = time.time()
            clicked = set()
            announced = False
            try:
                while not stop_event.is_set():
                    if not ctx.pages:
                        state["reason"] = "مرورگر بسته شد"
                        stop_event.set()
                        break
                    now = time.time()
                    joined = in_room(ctx)

                    if not joined and now - last_nav > RENAV_SEC:
                        last_nav = now
                        try:
                            if not activity_url:
                                page.goto(COURSE_URL.format(course_id), timeout=60000)
                                if "/login/" in page.url:
                                    state["reason"] = (
                                        "نشست منقضی است؛ python lecture_bot.py --login را اجرا کن"
                                    )
                                    state["failed"] = True
                                    stop_event.set()
                                    break
                                activity_url = find_activity(page)
                                if not activity_url:
                                    state["reason"] = (
                                        "فعالیت Adobe Connect پیدا نشد؛ python lecture_bot.py --discover را اجرا کن"
                                    )
                                    state["failed"] = True
                                    stop_event.set()
                                    break
                                print(f"[*] فعالیت کلاس: {activity_url}")
                            page.goto(activity_url, timeout=60000)
                            if "/login/" in page.url:
                                state["reason"] = (
                                    "نشست منقضی است؛ python lecture_bot.py --login را اجرا کن"
                                )
                                state["failed"] = True
                                stop_event.set()
                                break
                        except Exception as e:
                            print(f"[!] باز نشدن صفحه: {type(e).__name__}")

                    if now - last_reset > CLICK_RESET_SEC:
                        clicked.clear()
                        last_reset = now

                    try_join(ctx, clicked)

                    if joined and not announced:
                        announced = True
                        print("[✓] وارد اتاق شد. منتظر شروع کلاس...")
                        notify(
                            "Joined the room",
                            "ربات وارد اتاق شد و منتظر کلاس است ✅",
                            priority="low",
                            tags="white_check_mark",
                        )

                    try:
                        ctx.pages[0].wait_for_timeout(2000)
                    except Exception:
                        time.sleep(2)
            except KeyboardInterrupt:
                stop_event.set()
            try:
                ctx.close()
            except BaseException:
                pass
    except Exception as e:
        state["reason"] = f"خطای مرورگر: {type(e).__name__}: {e}"
        state["failed"] = True
        stop_event.set()


def choose_course(args):
    items = list(COURSES.items())
    if "--course" in args:
        key = args[args.index("--course") + 1]
        for cid, name in items:
            if key == str(cid) or key in name:
                return cid
        sys.exit("[!] درسی با این مشخصات پیدا نشد.")
    for i, (cid, name) in enumerate(items, 1):
        print(f"{i}. {name} ({cid})")
    try:
        return items[int(input("شماره‌ی درس: ")) - 1][0]
    except (ValueError, IndexError):
        sys.exit("[!] شماره‌ی نامعتبر.")


def stop_reason():
    now = time.time()
    last = state["last_speech"]
    if last and now - last > END_SILENCE_MIN * 60:
        return "کلاس تمام شد (سکوت طولانی)"
    if not last and now - state["start"] > MAX_WAIT_MIN * 60:
        return "کلاسی شروع نشد"
    if now - state["start"] > MAX_RUN_MIN * 60:
        return "حداکثر زمان اجرا"
    return None


def week_is_even(day):
    if EVEN_WEEK_SATURDAY is None:
        return True
    ws = day - datetime.timedelta(days=(day.weekday() - 5) % 7)
    return ((ws - EVEN_WEEK_SATURDAY).days // 7) % 2 == 0


def build_plan(day):
    items = []
    for wd, s_, e_, cid, parity in SCHEDULE:
        if WEEKDAYS[wd] != day.weekday():
            continue
        if parity == "even" and not week_is_even(day):
            continue
        sh, sm = map(int, s_.split(":"))
        eh, em = map(int, e_.split(":"))
        items.append(
            {
                "course": cid,
                "start": datetime.datetime.combine(day, datetime.time(sh, sm)),
                "end": datetime.datetime.combine(day, datetime.time(eh, em)),
            }
        )
    items.sort(key=lambda x: x["start"])
    for it in items:
        it["join_at"] = it["start"] - datetime.timedelta(minutes=JOIN_EARLY_MIN)
        it["stop_at"] = it["end"] + datetime.timedelta(minutes=END_BUFFER_MIN)
    for i in range(1, len(items)):
        prev, cur = items[i - 1], items[i]
        if cur["join_at"] < prev["stop_at"]:
            cur["join_at"] = max(cur["join_at"], prev["end"])
            prev["stop_at"] = min(prev["stop_at"], cur["join_at"])
    return items


def show_plan():
    if EVEN_WEEK_SATURDAY is None:
        print(
            "[i] EVEN_WEEK_SATURDAY تنظیم نشده؛ کلاس‌های «هفته‌ی زوج» هر هفته در نظر گرفته می‌شوند.\n"
        )
    today = datetime.date.today()
    names = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]
    for d in range(8):
        day = today + datetime.timedelta(days=d)
        plan = build_plan(day)
        if not plan:
            continue
        print(f"{day.isoformat()} ({names[day.weekday()]})")
        for it in plan:
            print(
                f"   {it['start']:%H:%M}-{it['end']:%H:%M}  {COURSES[it['course']]}"
                f"   (ورود {it['join_at']:%H:%M}، پایان حداکثر {it['stop_at']:%H:%M})"
            )


def run_auto():
    if not GROQ_API_KEY:
        sys.exit(
            "[!] GROQ_API_KEY تنظیم نشده است (بعد از setx، ترمینال را ببند و دوباره باز کن)."
        )
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    except Exception:
        pass
    print("[*] حالت خودکار فعال شد. این پنجره را باز بگذار. برای خروج Ctrl+C.")
    show_plan()
    notify(
        "Auto mode started",
        "حالت خودکار فعال شد ✅",
        priority="low",
        tags="white_check_mark",
    )
    done = set()
    script = str(pathlib.Path(__file__).resolve())
    try:
        while True:
            now = datetime.datetime.now()
            launched = False
            for it in build_plan(now.date()):
                key = (now.date(), it["course"], it["start"])
                if key in done or not (it["join_at"] <= now < it["stop_at"]):
                    continue
                done.add(key)
                minutes = max(5, int((it["stop_at"] - now).total_seconds() // 60))
                name = COURSES[it["course"]]
                print(f"\n[*] شروع جلسه: {name} (حداکثر {minutes} دقیقه)")
                rc = subprocess.run(
                    [
                        sys.executable,
                        script,
                        "--course",
                        str(it["course"]),
                        "--max-minutes",
                        str(minutes),
                        "--bg-summary",
                    ]
                ).returncode
                if rc != 0:
                    notify(
                        "Session failed",
                        f"جلسه‌ی {name} با خطا تمام شد. ترمینال را ببین.",
                        priority="high",
                        tags="warning",
                    )
                launched = True
                break
            if not launched:
                time.sleep(30)
    except KeyboardInterrupt:
        print("\n[*] حالت خودکار متوقف شد.")


def main():
    global MAX_RUN_MIN, MAX_WAIT_MIN
    args = sys.argv[1:]
    if "--login" in args:
        run_login()
        return
    if "--discover" in args:
        run_discover()
        return
    if "--plan" in args:
        show_plan()
        return
    if "--summarize" in args:
        from summarize import summarize_file

        path = args[args.index("--summarize") + 1]
        try:
            out = summarize_file(path)
            print(f"[✓] جزوه: {out}")
            notify(
                "Lecture notes ready",
                f"جزوه آماده شد: {pathlib.Path(out).name}",
                priority="low",
                tags="books",
            )
        except Exception as e:
            print(f"[!] خلاصه‌سازی ناموفق: {e}")
            notify(
                "Summary failed",
                f"{type(e).__name__}",
                priority="default",
                tags="warning",
            )
        return
    if "--auto" in args:
        run_auto()
        return

    if not GROQ_API_KEY:
        sys.exit(
            "[!] GROQ_API_KEY تنظیم نشده است (بعد از setx، ترمینال را ببند و دوباره باز کن)."
        )

    if "--max-minutes" in args:
        MAX_RUN_MIN = MAX_WAIT_MIN = max(5, int(args[args.index("--max-minutes") + 1]))

    direct_url = None
    course_id = None
    if "--url" in args:
        direct_url = args[args.index("--url") + 1]
    else:
        course_id = choose_course(args)

    OUT_DIR.mkdir(exist_ok=True)
    session = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
    label = COURSES.get(course_id, "class").replace(" ", "_")
    transcript_path = OUT_DIR / f"{session}_{label}_transcript.txt"
    print(f"[*] متن در {transcript_path} ذخیره می‌شود. برای پایان Ctrl+C بزن.")
    notify(
        "Lecture bot started",
        f"ربات شروع شد: {COURSES.get(course_id, 'class')} ✅",
        priority="low",
        tags="white_check_mark",
    )

    state["start"] = time.time()
    threading.Thread(
        target=run_class, args=(course_id, direct_url), daemon=True
    ).start()
    time.sleep(5)
    threading.Thread(target=capture_loop, daemon=True).start()
    t_tr = threading.Thread(target=transcribe_loop, args=(transcript_path,))
    t_tr.start()

    try:
        while t_tr.is_alive() and not stop_event.is_set():
            time.sleep(1)
            reason = stop_reason()
            if reason:
                state["reason"] = reason
                break
    except KeyboardInterrupt:
        state["reason"] = "توقف دستی"
    stop_event.set()
    print(f"\n[*] پایان: {state['reason'] or 'نامشخص'}")
    t_tr.join()

    has_text = transcript_path.exists() and transcript_path.stat().st_size > 0
    if has_text and os.environ.get("ANTHROPIC_API_KEY"):
        if "--bg-summary" in args:
            subprocess.Popen(
                [
                    sys.executable,
                    str(pathlib.Path(__file__).resolve()),
                    "--summarize",
                    str(transcript_path),
                ]
            )
            print("[*] ساخت جزوه در پس‌زمینه شروع شد.")
        else:
            from summarize import summarize_file

            print("[*] در حال ساخت جزوه...")
            try:
                out = summarize_file(transcript_path)
                print(f"[✓] جزوه: {out}")
                notify(
                    "Lecture notes ready",
                    f"جزوه آماده شد: {out.name}",
                    priority="low",
                    tags="books",
                )
            except Exception as e:
                print(f"[!] خلاصه‌سازی ناموفق: {e}")
    else:
        print("[i] متنی برای خلاصه نیست یا ANTHROPIC_API_KEY تنظیم نشده.")

    if state["failed"]:
        sys.exit(2)


if __name__ == "__main__":
    main()
