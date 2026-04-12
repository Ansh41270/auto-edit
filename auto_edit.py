import os
import uuid
import subprocess
import threading
import sqlite3
import hashlib
import secrets
import datetime
import requests
import numpy as np
from functools import wraps
from flask import Flask, request, jsonify, session, redirect, send_from_directory, send_file
from werkzeug.utils import secure_filename
from PIL import Image

app = Flask(__name__, static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), static_url_path="")
app.secret_key = "super_secret_key_change_this"
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = False

UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"
MUSIC_FOLDER = "music"
DB_FILE = "users.db"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
os.makedirs(MUSIC_FOLDER, exist_ok=True)

ANTHROPIC_API_KEY = "YOUR_API_KEY_HERE"
GOOGLE_DRIVE_FOLDER_ID = ""
GOOGLE_CREDENTIALS_PATH = ""

jobs = {}


# ════════════════════════════════════════════════════════
# ════════════════════════════════════════════════════════
#  PLAN LIMITS & COUPON CODES
# ════════════════════════════════════════════════════════

PLAN_LIMITS = {
    "free": {"video_edits_per_day": 1, "scripts_per_day": 2, "thumbnails_per_day": 2, "label": "Free"},
    "pro":  {"video_edits_per_day": 999, "scripts_per_day": 999, "thumbnails_per_day": 999, "label": "Pro"},
    "max":  {"video_edits_per_day": 999, "scripts_per_day": 999, "thumbnails_per_day": 999, "label": "Max"},
}

COUPON_CODES = {
    "ANSH50":    ("pro",  50,  "50% off Pro plan"),
    "LAUNCH25":  ("pro",  25,  "25% off — Launch offer"),
    "MAXFREE":   ("max",  100, "Free Max plan — Special"),
    "PROFREE":   ("pro",  100, "Free Pro plan — Special"),
    "SAVE20":    ("pro",  20,  "20% off Pro plan"),
    "YOUTUBE10": ("pro",  10,  "10% off for YouTubers"),
}

PLAN_PRICES = {
    "pro": {"monthly": 9,  "yearly": 90},
    "max": {"monthly": 29, "yearly": 290},
}

# ════════════════════════════════════════════════════════
#  DATABASE
# ════════════════════════════════════════════════════════

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            plan TEXT DEFAULT 'free',
            plan_expires TEXT DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS feature_usage (
            email TEXT,
            feature TEXT,
            date TEXT,
            count INTEGER,
            PRIMARY KEY (email, feature, date)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT,
            plan TEXT,
            amount REAL,
            coupon TEXT,
            billing TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()


def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_hex(16)
    pw_hash = hashlib.sha256((salt + password).encode()).hexdigest()
    return pw_hash, salt


def create_user(email, password):
    pw_hash, salt = hash_password(password)
    conn = sqlite3.connect(DB_FILE)
    try:
        c = conn.cursor()
        c.execute(
            "INSERT INTO users (email, password_hash, salt) VALUES (?, ?, ?)",
            (email.lower().strip(), pw_hash, salt)
        )
        conn.commit()
        return True, "Account created."
    except sqlite3.IntegrityError:
        return False, "Email already registered."
    finally:
        conn.close()


def verify_user(email, password):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT password_hash, salt FROM users WHERE email = ?", (email.lower().strip(),))
    row = c.fetchone()
    conn.close()
    if not row:
        return False
    stored_hash, salt = row
    pw_hash, _ = hash_password(password, salt)
    return pw_hash == stored_hash


def get_user_plan(email):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT plan, plan_expires FROM users WHERE email = ?", (email.lower().strip(),))
    row = c.fetchone()
    conn.close()
    if not row:
        return "free"
    plan, expires = row
    if plan != "free" and expires:
        try:
            exp = datetime.datetime.strptime(expires, "%Y-%m-%d").date()
            if datetime.date.today() > exp:
                conn2 = sqlite3.connect(DB_FILE)
                conn2.execute("UPDATE users SET plan='free', plan_expires=NULL WHERE email=?",
                              (email.lower().strip(),))
                conn2.commit()
                conn2.close()
                return "free"
        except:
            pass
    return plan or "free"


def check_feature_limit(email, feature):
    plan = get_user_plan(email)
    limit_key = f"{feature}s_per_day"
    limit = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"]).get(limit_key, 1)
    if limit >= 999:
        return True, 0, 999
    today = str(datetime.date.today())
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT count FROM feature_usage WHERE email=? AND feature=? AND date=?",
              (email, feature, today))
    row = c.fetchone()
    used = row[0] if row else 0
    conn.close()
    return (used < limit), used, limit


def consume_feature(email, feature):
    today = str(datetime.date.today())
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT count FROM feature_usage WHERE email=? AND feature=? AND date=?",
              (email, feature, today))
    row = c.fetchone()
    if row:
        c.execute("UPDATE feature_usage SET count=count+1 WHERE email=? AND feature=? AND date=?",
                  (email, feature, today))
    else:
        c.execute("INSERT INTO feature_usage (email,feature,date,count) VALUES (?,?,?,1)",
                  (email, feature, today))
    conn.commit()
    conn.close()


def can_edit(email):
    allowed, used, limit = check_feature_limit(email, "video_edit")
    if allowed:
        consume_feature(email, "video_edit")
    return allowed


def get_usage_summary(email):
    today = str(datetime.date.today())
    plan = get_user_plan(email)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT feature, count FROM feature_usage WHERE email=? AND date=?", (email, today))
    rows = {r[0]: r[1] for r in c.fetchall()}
    conn.close()
    limits = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])
    return {
        "plan": plan,
        "plan_label": limits.get("label", "Free"),
        "video_edit": {"used": rows.get("video_edit", 0), "limit": limits["video_edits_per_day"]},
        "script":     {"used": rows.get("script", 0),     "limit": limits["scripts_per_day"]},
        "thumbnail":  {"used": rows.get("thumbnail", 0),  "limit": limits["thumbnails_per_day"]},
    }


def apply_coupon(email, coupon_code, billing="monthly"):
    code = coupon_code.strip().upper()
    if code not in COUPON_CODES:
        return False, "Invalid coupon code.", 0
    plan, discount, description = COUPON_CODES[code]
    days = 365 if billing == "yearly" else 30
    expires = (datetime.date.today() + datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE users SET plan=?, plan_expires=? WHERE email=?",
                 (plan, expires, email.lower().strip()))
    conn.execute("INSERT INTO payments (email,plan,amount,coupon,billing) VALUES (?,?,?,?,?)",
                 (email, plan, 0, code, billing))
    conn.commit()
    conn.close()
    return True, f"Coupon applied! You now have {plan.upper()} plan until {expires}.", discount


def process_payment(email, plan, billing, coupon_code=""):
    if plan not in PLAN_PRICES:
        return False, "Invalid plan.", 0
    base = PLAN_PRICES[plan]["yearly"] if billing == "yearly" else PLAN_PRICES[plan]["monthly"]
    discount = 0
    if coupon_code:
        code = coupon_code.strip().upper()
        if code in COUPON_CODES:
            _, discount, _ = COUPON_CODES[code]
    final = round(base * (1 - discount/100), 2)
    days = 365 if billing == "yearly" else 30
    expires = (datetime.date.today() + datetime.timedelta(days=days)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE users SET plan=?, plan_expires=? WHERE email=?",
                 (plan, expires, email.lower().strip()))
    conn.execute("INSERT INTO payments (email,plan,amount,coupon,billing) VALUES (?,?,?,?,?)",
                 (email, plan, final, coupon_code, billing))
    conn.commit()
    conn.close()
    return True, f"Payment successful! {plan.upper()} plan active until {expires}.", final

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            return redirect("/login")
        return f(*args, **kwargs)
    return decorated


# ════════════════════════════════════════════════════════
#  GOOGLE DRIVE
# ════════════════════════════════════════════════════════

def upload_to_drive(file_path, filename):
    if not GOOGLE_CREDENTIALS_PATH or not os.path.exists(GOOGLE_CREDENTIALS_PATH):
        return None, "Google credentials not configured."
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(
            GOOGLE_CREDENTIALS_PATH,
            scopes=["https://www.googleapis.com/auth/drive"]
        )
        service = build("drive", "v3", credentials=creds)
        file_metadata = {"name": filename}
        if GOOGLE_DRIVE_FOLDER_ID:
            file_metadata["parents"] = [GOOGLE_DRIVE_FOLDER_ID]
        media = MediaFileUpload(file_path, resumable=True)
        uploaded = service.files().create(
            body=file_metadata, media_body=media, fields="id, webViewLink"
        ).execute()
        return uploaded.get("webViewLink"), None
    except ImportError:
        return None, "Run: pip install google-api-python-client google-auth"
    except Exception as e:
        return None, str(e)


# ════════════════════════════════════════════════════════
#  AI SCRIPT WRITER
# ════════════════════════════════════════════════════════

def generate_script(topic, style, duration_mins, audience="General", tone="Friendly", cta="Like & Subscribe", extra="", lang_instruction=""):
    if not ANTHROPIC_API_KEY or ANTHROPIC_API_KEY == "YOUR_API_KEY_HERE":
        lang_example = "Aaj hum baat karenge" if "Hinglish" in lang_instruction else "Today we will talk about"
        return f"""🎬 HOOK:
{lang_example} {topic}... aur aaj ka video bahut kaam aayega!

📌 INTRO:
Welcome back! Aaj hum cover karenge: {topic}
Target audience: {audience} | Style: {style} | Tone: {tone}

📖 MAIN CONTENT:
[Section 1] - Introduction to {topic}
  - Key point 1
  - Key point 2

[Section 2] - Why {topic} matters
  - Real world example
  - Tips and tricks

[Section 3] - Step by step guide
  - Step 1, Step 2, Step 3

💡 TIPS:
- Pro tip 1 about {topic}
- Pro tip 2 about {topic}

📣 CALL TO ACTION:
{cta}!

🔥 OUTRO:
Thanks for watching! Agar video pasand aayi toh {cta}.
""", None

    try:
        prompt = (
            f"Write a complete YouTube video script.\n\n"
            f"Topic: {topic}\n"
            f"Style: {style}\n"
            f"Target Duration: {duration_mins} minutes\n"
            f"Target Audience: {audience}\n"
            f"Tone: {tone}\n"
            f"Call To Action: {cta}\n"
            f"Extra Instructions: {extra if extra else 'None'}\n\n"
            f"{lang_instruction}\n\n"
            f"Include these sections with clear headers:\n"
            f"1. HOOK (attention-grabbing opening)\n"
            f"2. INTRO (introduce yourself and topic)\n"
            f"3. MAIN CONTENT (3-4 sections)\n"
            f"4. TIPS\n"
            f"5. CALL TO ACTION\n"
            f"6. OUTRO\n\n"
            f"Make it engaging, natural, and suitable for the {audience} audience."
        )
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 2500,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=40
        )
        data = response.json()
        if "content" in data and len(data["content"]) > 0:
            return data["content"][0]["text"], None
        return None, f"API error: {data.get('error', {}).get('message', 'Unknown error')}"
    except Exception as e:
        return None, str(e)


# ════════════════════════════════════════════════════════
#  FFMPEG HELPERS
# ════════════════════════════════════════════════════════

def run_cmd(cmd, job_id, step_msg, progress):
    jobs[job_id]["message"] = step_msg
    jobs[job_id]["progress"] = progress
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{step_msg} failed:\n{result.stderr}")
    return result.stdout


def detect_silences(input_path, noise="-35dB", duration=0.5):
    cmd = f'ffmpeg -i "{input_path}" -af silencedetect=noise={noise}:d={duration} -f null - 2>&1'
    out = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    text = out.stderr + out.stdout
    starts, ends = [], []
    for line in text.splitlines():
        if "silence_start" in line:
            try:
                starts.append(float(line.split("silence_start:")[1].strip()))
            except:
                pass
        if "silence_end" in line:
            try:
                ends.append(float(line.split("silence_end:")[1].split()[0].strip()))
            except:
                pass
    return list(zip(starts, ends))


def get_duration(path):
    cmd = f'ffprobe -v error -show_entries format=duration -of csv=p=0 "{path}"'
    out = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except:
        return None


def build_cut_filter(silences, total_dur, min_seg=0.5):
    keep = []
    pos = 0.0
    for s_start, s_end in sorted(silences):
        if s_start - pos > min_seg:
            keep.append((pos, s_start))
        pos = s_end
    if total_dur - pos > min_seg:
        keep.append((pos, total_dur))
    if not keep:
        return None, None
    v_parts = "+".join([f"between(t,{s},{e})" for s, e in keep])
    vf = f"select='{v_parts}',setpts=N/FRAME_RATE/TB"
    af = f"aselect='{v_parts}',asetpts=N/SR/TB"
    return vf, af


WARM_EQ = "eq=contrast=1.1:brightness=0.02:saturation=1.2:gamma_r=1.05:gamma_b=0.95"
COOL_EQ = "eq=contrast=1.15:brightness=-0.01:saturation=1.1:gamma_r=0.95:gamma_b=1.05"


def generate_subtitles(audio_path, out_srt):
    try:
        import whisper
        model = whisper.load_model("base")
        result = model.transcribe(audio_path)
        segments = result.get("segments", [])
        with open(out_srt, "w", encoding="utf-8") as f:
            for i, seg in enumerate(segments, 1):
                start = _fmt_time(seg["start"])
                end = _fmt_time(seg["end"])
                text = seg["text"].strip()
                f.write(f"{i}\n{start} --> {end}\n{text}\n\n")
        return True
    except ImportError:
        with open(out_srt, "w") as f:
            f.write("1\n00:00:00,000 --> 00:00:03,000\n[Install openai-whisper for subtitles]\n\n")
        return False


def _fmt_time(s):
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:06.3f}".replace(".", ",")


# ════════════════════════════════════════════════════════
#  EDIT PIPELINE
# ════════════════════════════════════════════════════════

def edit_pipeline(job_id, input_path, settings, music_path_input=None):
    try:
        jobs[job_id]["status"] = "processing"
        base = os.path.join(OUTPUT_FOLDER, job_id)
        os.makedirs(base, exist_ok=True)

        total_dur = get_duration(input_path)
        if total_dur is None:
            raise RuntimeError("Could not determine video duration. Is ffprobe installed?")

        # Step 1: Extract audio
        audio_path = os.path.join(base, "audio.wav")
        run_cmd(
            f'ffmpeg -y -i "{input_path}" -vn -acodec pcm_s16le -ar 16000 -ac 1 "{audio_path}"',
            job_id, "Extracting audio...", 5
        )

        # Step 2: Cut silences
        cut_path = os.path.join(base, "cut.mp4")
        if settings.get("cut_silences", True):
            silences = detect_silences(input_path)
            vf, af = build_cut_filter(silences, total_dur)
            if vf:
                run_cmd(
                    f'ffmpeg -y -i "{input_path}" -vf "{vf}" -af "{af}" -c:v libx264 -c:a aac -preset fast "{cut_path}"',
                    job_id, "Cutting silences...", 15
                )
            else:
                run_cmd(f'ffmpeg -y -i "{input_path}" -c copy "{cut_path}"', job_id, "No silences found...", 15)
        else:
            run_cmd(f'ffmpeg -y -i "{input_path}" -c copy "{cut_path}"', job_id, "Skipping silence cut...", 15)

        # Step 3: Color grade
        graded_path = os.path.join(base, "graded.mp4")
        grade = settings.get("color_grade", "warm")
        if grade == "warm":
            run_cmd(
                f'ffmpeg -y -i "{cut_path}" -vf "{WARM_EQ}" -c:v libx264 -c:a copy -preset fast "{graded_path}"',
                job_id, "Applying warm color grade...", 28
            )
        elif grade == "cool":
            run_cmd(
                f'ffmpeg -y -i "{cut_path}" -vf "{COOL_EQ}" -c:v libx264 -c:a copy -preset fast "{graded_path}"',
                job_id, "Applying cool color grade...", 28
            )
        else:
            run_cmd(f'ffmpeg -y -i "{cut_path}" -c copy "{graded_path}"', job_id, "Skipping color grade...", 28)

        # Step 4: Subtitles
        srt_path = os.path.join(base, "subs.srt")
        subtitled_path = os.path.join(base, "subtitled.mp4")
        if settings.get("subtitles", True):
            generate_subtitles(audio_path, srt_path)
            srt_fixed = srt_path.replace("\\", "/").replace(":", "\\:")
            run_cmd(
                f'ffmpeg -y -i "{graded_path}" -vf "subtitles={srt_fixed}" -c:v libx264 -c:a copy -preset fast "{subtitled_path}"',
                job_id, "Burning subtitles...", 42
            )
        else:
            run_cmd(f'ffmpeg -y -i "{graded_path}" -c copy "{subtitled_path}"', job_id, "Skipping subtitles...", 42)

        # Step 5: Background music
        music_mixed_path = os.path.join(base, "with_music.mp4")
        if music_path_input and os.path.exists(music_path_input):
            music_volume = settings.get("music_volume", 0.15)
            run_cmd(
                f'ffmpeg -y -i "{subtitled_path}" -i "{music_path_input}" '
                f'-filter_complex "[1:a]volume={music_volume},aloop=loop=-1:size=2e+09[music];'
                f'[0:a][music]amix=inputs=2:duration=first:dropout_transition=3[aout]" '
                f'-map 0:v -map "[aout]" -c:v copy -c:a aac -shortest "{music_mixed_path}"',
                job_id, "Mixing background music...", 55
            )
        else:
            run_cmd(f'ffmpeg -y -i "{subtitled_path}" -c copy "{music_mixed_path}"',
                    job_id, "Skipping background music...", 55)

        # Step 6: Intro & Outro
        intro_path = os.path.join(base, "with_intro.mp4")
        if settings.get("intro_outro", True):
            title_text = settings.get("title", "My Video").replace("'", "\\'")
            end_time = max(total_dur - 3, 0)
            run_cmd(
                f'ffmpeg -y -i "{music_mixed_path}" '
                f'-vf "drawtext=text=\'{title_text}\':fontcolor=white:fontsize=48:'
                f'x=(w-text_w)/2:y=(h-text_h)/2:enable=\'between(t,0,2)\':'
                f'box=1:boxcolor=black@0.5:boxborderw=10,'
                f'drawtext=text=\'Thanks for watching!\':fontcolor=white:fontsize=32:'
                f'x=(w-text_w)/2:y=(h-text_h)/2:enable=\'between(t,{end_time},{total_dur})\':'
                f'box=1:boxcolor=black@0.5:boxborderw=10" '
                f'-c:v libx264 -c:a copy -preset fast "{intro_path}"',
                job_id, "Adding intro & outro...", 68
            )
        else:
            run_cmd(f'ffmpeg -y -i "{music_mixed_path}" -c copy "{intro_path}"',
                    job_id, "Skipping intro/outro...", 68)

        # Step 7: Export
        output_files = {}
        landscape = os.path.join(base, "final_landscape_16x9.mp4")
        run_cmd(
            f'ffmpeg -y -i "{intro_path}" '
            f'-vf "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black" '
            f'-c:v libx264 -c:a copy -preset fast "{landscape}"',
            job_id, "Exporting 16:9...", 80
        )
        output_files["landscape"] = f"/download/{job_id}/final_landscape_16x9.mp4"

        if settings.get("both_formats", True):
            portrait = os.path.join(base, "final_portrait_9x16.mp4")
            run_cmd(
                f'ffmpeg -y -i "{intro_path}" '
                f'-vf "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black" '
                f'-c:v libx264 -c:a copy -preset fast "{portrait}"',
                job_id, "Exporting 9:16...", 90
            )
            output_files["portrait"] = f"/download/{job_id}/final_portrait_9x16.mp4"

        if settings.get("subtitles", True):
            output_files["srt"] = f"/download/{job_id}/subs.srt"

        # Step 8: Google Drive
        if settings.get("upload_to_drive", False):
            jobs[job_id]["message"] = "Uploading to Google Drive..."
            jobs[job_id]["progress"] = 95
            link, err = upload_to_drive(landscape, f"{settings.get('title','video')}_16x9.mp4")
            if link:
                output_files["landscape_drive"] = link
            if settings.get("both_formats", True):
                portrait_path = os.path.join(base, "final_portrait_9x16.mp4")
                if os.path.exists(portrait_path):
                    link2, _ = upload_to_drive(portrait_path, f"{settings.get('title','video')}_9x16.mp4")
                    if link2:
                        output_files["portrait_drive"] = link2

        jobs[job_id].update({
            "status": "done",
            "progress": 100,
            "message": "Editing complete!",
            "output_files": output_files
        })

    except Exception as e:
        jobs[job_id].update({
            "status": "error",
            "progress": 0,
            "message": str(e)
        })


# ════════════════════════════════════════════════════════
#  ROUTES
# ════════════════════════════════════════════════════════

@app.route("/")
@login_required
def index():
    return redirect("/dashboard")


@app.route("/dashboard")
@login_required
def dashboard():
    return send_from_directory(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), "dashboard.html")


@app.route("/auto-edit")
@login_required
def auto_edit_page():
    return send_from_directory(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), "auto_edit.html")

# -------- THUMBNAIL FEATURE --------
THUMB_FOLDER = "thumbnails"
if os.path.isfile(THUMB_FOLDER):
    os.remove(THUMB_FOLDER)
os.makedirs(THUMB_FOLDER, exist_ok=True)


@app.route("/thumbnail-page")
@login_required
def thumbnail_page():
    return send_from_directory(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), "thumbnail.html")


# ════════════════════════════════════════════════════════
#  PROFESSIONAL THUMBNAIL ENGINE v4
#  — Face swap + compositing like a real designer tool
# ════════════════════════════════════════════════════════

def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def get_best_font(size):
    from PIL import ImageFont
    paths = [
        "C:/Windows/Fonts/impact.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/Arial_Bold.ttf",
        "C:/Windows/Fonts/verdanab.ttf",
        "C:/Windows/Fonts/calibrib.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except:
                continue
    return ImageFont.load_default()


def detect_face_region(img):
    """Detect approximate face bounding box using simple skin-tone heuristic."""
    import numpy as np
    arr = np.array(img.convert("RGB"))
    h, w = arr.shape[:2]
    r, g, b = arr[:,:,0].astype(float), arr[:,:,1].astype(float), arr[:,:,2].astype(float)
    # Skin tone mask: reddish, not too dark, not too bright
    skin = (
        (r > 60) & (g > 40) & (b > 20) &
        (r > g) & (r > b) &
        (r - g > 10) &
        (np.abs(r.astype(int) - g.astype(int)) > 8) &
        (r < 250) & (g < 220) & (b < 200)
    )
    rows = np.where(skin.any(axis=1))[0]
    cols = np.where(skin.any(axis=0))[0]
    if len(rows) < 10 or len(cols) < 10:
        # fallback: assume face is in upper-center area
        return (w//4, 0, 3*w//4, h//2)
    y1, y2 = int(rows.min()), int(rows.max())
    x1, x2 = int(cols.min()), int(cols.max())
    # Add padding
    pad = 30
    x1 = max(0, x1 - pad)
    y1 = max(0, y1 - pad)
    x2 = min(w, x2 + pad)
    y2 = min(h, y2 + pad)
    return (x1, y1, x2, y2)


def extract_person_smart(user_img):
    """
    Smart person extraction:
    1. Try rembg if installed (best quality)
    2. Fall back to GrabCut-style edge detection
    3. Final fallback: simple corner-color removal
    """
    from PIL import ImageFilter
    PILImage = Image
    import numpy as np

    img_rgba = user_img.convert("RGBA")

    # ── Try rembg (best quality, pip install rembg) ──
    try:
        from rembg import remove as rembg_remove
        result = rembg_remove(img_rgba)
        return result
    except ImportError:
        pass
    except Exception:
        pass

    # ── Try OpenCV GrabCut ──
    try:
        import cv2
        img_rgb = np.array(user_img.convert("RGB"))
        h, w = img_rgb.shape[:2]
        mask = np.zeros((h, w), np.uint8)
        bgd_model = np.zeros((1, 65), np.float64)
        fgd_model = np.zeros((1, 65), np.float64)
        # Use center 70% as foreground rectangle
        margin_x = int(w * 0.10)
        margin_y = int(h * 0.05)
        rect = (margin_x, margin_y, w - 2*margin_x, h - 2*margin_y)
        cv2.grabCut(img_rgb, mask, rect, bgd_model, fgd_model, 8, cv2.GC_INIT_WITH_RECT)
        fg_mask = np.where((mask == 2) | (mask == 0), 0, 255).astype(np.uint8)
        # Smooth the mask
        fg_mask = cv2.GaussianBlur(fg_mask, (7, 7), 0)
        result = PILImage.fromarray(img_rgb)
        result = result.convert("RGBA")
        r, g, b, a = result.split()
        result = PILImage.merge("RGBA", (r, g, b, PILImage.fromarray(fg_mask)))
        return result
    except ImportError:
        pass
    except Exception:
        pass

    # ── Fallback: edge-aware background removal ──
    img_arr = np.array(img_rgba)
    h, w = img_arr.shape[:2]

    # Sample background from all 4 edges (top/bottom/left/right strips)
    edge_pixels = np.concatenate([
        img_arr[:8, :, :3].reshape(-1, 3),
        img_arr[-8:, :, :3].reshape(-1, 3),
        img_arr[:, :8, :3].reshape(-1, 3),
        img_arr[:, -8:, :3].reshape(-1, 3),
    ])
    bg_color = np.median(edge_pixels, axis=0).astype(int)

    # Build alpha mask: far from bg = opaque, close to bg = transparent
    diff = np.sqrt(np.sum((img_arr[:,:,:3].astype(int) - bg_color)**2, axis=2))
    tolerance = 55
    feather = 30
    alpha = np.clip((diff - tolerance) / feather * 255, 0, 255).astype(np.uint8)

    # Keep center area more opaque (protect the person)
    cy, cx = h//2, w//2
    Y, X = np.ogrid[:h, :w]
    center_dist = np.sqrt(((X-cx)/(w*0.35))**2 + ((Y-cy)/(h*0.45))**2)
    center_boost = np.clip((1.0 - center_dist) * 180, 0, 180).astype(np.uint8)
    alpha = np.clip(alpha.astype(int) + center_boost, 0, 255).astype(np.uint8)

    result = img_rgba.copy()
    result.putalpha(PILImage.fromarray(alpha))

    # Smooth edges
    smooth_alpha = PILImage.fromarray(alpha).filter(ImageFilter.GaussianBlur(2))
    result.putalpha(smooth_alpha)

    return result


def composite_person_onto_thumbnail(ref_img, person_img, position="right"):
    """
    Core compositing function:
    - Scales person to fill ~65% height of thumbnail
    - Places them on chosen side
    - Adds matching color grading, edge glow, and shadow
    - Returns composited RGBA image
    """
    from PIL import ImageFilter, ImageEnhance
    PILImage = Image
    import numpy as np

    W, H = ref_img.size
    ref_rgba = ref_img.convert("RGBA")

    # ── Scale person ──
    target_h = int(H * 1.05)  # slightly taller than canvas for bleed effect
    aspect = person_img.width / person_img.height
    target_w = int(target_h * aspect)

    # Minimum width check
    if target_w < int(W * 0.3):
        target_w = int(W * 0.38)
        target_h = int(target_w / aspect)

    person_scaled = person_img.resize((target_w, target_h), PILImage.LANCZOS)

    # ── Position ──
    if position == "right":
        px = W - target_w + int(target_w * 0.05)
    else:
        px = -int(target_w * 0.05)
    py = H - target_h  # bottom-aligned

    # ── Darken the reference bg on text side for better readability ──
    dark_layer = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
    from PIL import ImageDraw
    dd = ImageDraw.Draw(dark_layer)
    if position == "right":
        dd.rectangle([0, 0, W//2 + 60, H], fill=(0, 0, 0, 90))
    else:
        dd.rectangle([W//2 - 60, 0, W, H], fill=(0, 0, 0, 90))
    ref_rgba = PILImage.alpha_composite(ref_rgba, dark_layer)

    # ── Drop shadow behind person ──
    shadow_layer = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
    if person_scaled.mode == "RGBA":
        alpha_ch = person_scaled.split()[3]
        shadow_alpha = PILImage.new("L", person_scaled.size, 0)
        shadow_alpha.paste(alpha_ch)
        # Create dark version
        shadow_img = PILImage.new("RGBA", person_scaled.size, (0, 0, 0, 0))
        shadow_data = np.array(shadow_img)
        shadow_data[:,:,3] = (np.array(shadow_alpha) * 0.65).astype(np.uint8)
        shadow_pil = PILImage.fromarray(shadow_data).filter(ImageFilter.GaussianBlur(22))
        shadow_layer.paste(shadow_pil, (px + 20, py + 20), shadow_pil)
    ref_rgba = PILImage.alpha_composite(ref_rgba, shadow_layer)

    # ── Edge glow (matches reference thumbnail accent color) ──
    # Sample the dominant bright color from reference
    ref_small = ref_img.resize((50, 28)).convert("RGB")
    ref_arr = np.array(ref_small).reshape(-1, 3)
    bright_mask = ref_arr.max(axis=1) > 180
    if bright_mask.sum() > 5:
        glow_color = ref_arr[bright_mask].mean(axis=0).astype(int)
    else:
        glow_color = np.array([100, 150, 255])

    if person_scaled.mode == "RGBA":
        glow_layer = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
        glow_alpha = PILImage.new("L", (W, H), 0)
        glow_alpha.paste(person_scaled.split()[3], (px, py))
        glow_blurred = glow_alpha.filter(ImageFilter.GaussianBlur(25))
        glow_arr = np.zeros((H, W, 4), dtype=np.uint8)
        glow_arr[:,:,0] = int(glow_color[0])
        glow_arr[:,:,1] = int(glow_color[1])
        glow_arr[:,:,2] = int(glow_color[2])
        glow_arr[:,:,3] = (np.array(glow_blurred) * 0.55).astype(np.uint8)
        glow_pil = PILImage.fromarray(glow_arr)
        ref_rgba = PILImage.alpha_composite(ref_rgba, glow_pil)

    # ── Color match person to reference ──
    # Get average brightness of reference
    ref_gray = np.array(ref_img.convert("L")).mean()
    person_arr = np.array(person_scaled).astype(float)
    if person_scaled.mode == "RGBA":
        rgb = person_arr[:,:,:3]
        person_gray = rgb.mean()
        if person_gray > 10:
            ratio = min(ref_gray / person_gray, 1.3)
            rgb = np.clip(rgb * ratio, 0, 255)
        person_arr[:,:,:3] = rgb
        person_matched = PILImage.fromarray(person_arr.astype(np.uint8), "RGBA")
    else:
        person_matched = person_scaled

    # ── Paste person ──
    person_layer = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
    if person_matched.mode == "RGBA":
        person_layer.paste(person_matched, (px, py), person_matched)
    else:
        person_layer.paste(person_matched.convert("RGBA"), (px, py))
    result = PILImage.alpha_composite(ref_rgba, person_layer)

    return result


def add_text_overlay(img, headline, subtext, badge, position, niche, style):
    """
    Add professional text overlay on the opposite side from person.
    Matches style of reference thumbnail text.
    """
    from PIL import ImageDraw
    PILImage = Image
    import numpy as np

    W, H = img.size
    canvas = img.convert("RGBA")
    draw = ImageDraw.Draw(canvas)

    # Text zone
    if position == "right":   # person is right, text is left
        tx = 38
        max_w = W // 2 - 60
    else:                      # person is left, text is right
        tx = W // 2 + 30
        max_w = W // 2 - 60

    # Sample accent color from the reference image
    arr = np.array(img.convert("RGB"))
    # Look for bright saturated pixels
    hsv_approx_sat = (arr.max(axis=2).astype(int) - arr.min(axis=2).astype(int))
    bright = arr[hsv_approx_sat > 80]
    if len(bright) > 20:
        accent = tuple(bright[np.argmax(bright.max(axis=1))].tolist())
    else:
        accent = (255, 200, 0)

    # Font sizes — auto-fit
    def fit_font_lines(text, max_width):
        for size in [110, 90, 72, 58, 46]:
            f = get_best_font(size)
            words = text.split()
            if not words:
                return f, [""]
            # single line
            bb = draw.textbbox((0,0), text, font=f)
            if bb[2]-bb[0] <= max_width:
                return f, [text]
            # 2 lines
            if len(words) >= 2:
                mid = len(words)//2
                l1, l2 = " ".join(words[:mid]), " ".join(words[mid:])
                b1 = draw.textbbox((0,0), l1, font=f)
                b2 = draw.textbbox((0,0), l2, font=f)
                if max(b1[2]-b1[0], b2[2]-b2[0]) <= max_width:
                    return f, [l1, l2]
            # 3 lines
            if len(words) >= 3:
                t = len(words)//3
                ls = [" ".join(words[t*i:t*(i+1)]) for i in range(3)]
                ls = [l for l in ls if l]
                if all(draw.textbbox((0,0), l, font=f)[2] <= max_width for l in ls):
                    return f, ls
        return get_best_font(40), [text[:18]]

    y = 55
    font_sm = get_best_font(36)

    # ── Badge ──
    if badge and badge.strip():
        badge_layer = PILImage.new("RGBA", (W, H), (0,0,0,0))
        bl = ImageDraw.Draw(badge_layer)
        bb = bl.textbbox((0,0), badge.upper(), font=font_sm)
        bw = bb[2]-bb[0]+24
        bh = bb[3]-bb[1]+14
        # Solid colored badge
        bl.rounded_rectangle([tx, y, tx+bw, y+bh], radius=5,
                              fill=(accent[0], accent[1], accent[2], 230))
        bl.text((tx+12, y+7), badge.upper(), font=font_sm, fill=(0,0,0,255))
        canvas = PILImage.alpha_composite(canvas, badge_layer)
        draw = ImageDraw.Draw(canvas)
        y += bh + 12

    # ── Headline (big bold text like real YouTube thumbnails) ──
    chosen_font, lines = fit_font_lines(headline.upper(), max_w)

    # Alternate colors: white / accent (like "CUSTOM" white + "YOUTUBE" yellow)
    colors = [(255,255,255), accent, (255,255,255)]
    for i, line in enumerate(lines):
        color = colors[i % len(colors)]
        # Strong stroke (black outline) — signature YouTube thumbnail style
        stroke_w = 6
        for dx in range(-stroke_w, stroke_w+1, 2):
            for dy in range(-stroke_w, stroke_w+1, 2):
                if dx or dy:
                    draw.text((tx+dx, y+dy), line, font=chosen_font, fill=(0,0,0,255))
        draw.text((tx, y), line, font=chosen_font, fill=(*color, 255))
        bb = draw.textbbox((0,0), line, font=chosen_font)
        y += (bb[3]-bb[1]) + 5
    y += 8

    # ── Subtext ──
    if subtext and subtext.strip():
        font_sub = get_best_font(42)
        for dx, dy in [(-2,2),(2,2),(0,3)]:
            draw.text((tx+dx, y+dy), subtext, font=font_sub, fill=(0,0,0,200))
        draw.text((tx, y), subtext, font=font_sub, fill=(200,200,200,255))
        bb = draw.textbbox((0,0), subtext, font=font_sub)
        y += (bb[3]-bb[1]) + 10

    return canvas


def call_claude_for_text(ref_b64, title, niche, style, extra):
    """Ask Claude to generate the headline, badge, subtext for the thumbnail."""
    import json

    prompt = f"""You are a professional YouTube thumbnail designer. Look at this reference thumbnail.

Create text content for a NEW thumbnail with these details:
- Video title: "{title}"
- Niche: {niche}
- Style: {style}
- Extra: {extra if extra else "none"}

Return ONLY valid JSON, no markdown:
{{
  "headline": "SHORT 2-4 WORD PUNCHY TITLE ALL CAPS",
  "subtext": "one short supporting line or empty string",
  "badge": "short label like TOP 10 or SHOCKING or empty string"
}}

Rules:
- headline must be SHORT (max 4 words), punchy, eye-catching like real YouTube thumbnails
- Match the energy/style of the reference thumbnail
- Return ONLY the JSON object"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 200,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64",
                         "media_type": "image/png", "data": ref_b64}},
                        {"type": "text", "text": prompt}
                    ]
                }]
            },
            timeout=30
        )
        data = resp.json()
        if "content" in data:
            raw = data["content"][0]["text"].strip()
            if "```" in raw:
                raw = raw.split("```")[1].lstrip("json").strip()
            result = json.loads(raw)
            return result, "Claude AI analyzed reference and generated professional text"
    except Exception:
        pass

    # Smart fallback
    words = title.upper().split()
    headline = " ".join(words[:4]) if len(words) >= 4 else title.upper()
    return {
        "headline": headline,
        "subtext": niche,
        "badge": "WATCH THIS"
    }, "Fallback text used (set Anthropic API key for AI text generation)"


@app.route("/thumbnail", methods=["POST"])
@login_required
def thumbnail():
    import base64
    PILImage = Image

    if "ref" not in request.files or "user" not in request.files:
        return jsonify({"error": "Both reference and user images are required."}), 400
    allowed_t, used_t, limit_t = check_feature_limit(session["user"], "thumbnail")
    if not allowed_t:
        return jsonify({"error": f"Daily thumbnail limit reached ({used_t}/{limit_t}). Upgrade to Pro for unlimited.", "limit_reached": True}), 403
    consume_feature(session["user"], "thumbnail")

    ref_file  = request.files["ref"]
    user_file = request.files["user"]
    title  = request.form.get("title", "My Video")
    niche  = request.form.get("niche", "Entertainment")
    style  = request.form.get("style", "Bold & Bright")
    extra  = request.form.get("extra", "")

    job_id  = str(uuid.uuid4())
    job_dir = os.path.join(THUMB_FOLDER, job_id)
    os.makedirs(job_dir, exist_ok=True)

    ref_path  = os.path.join(job_dir, "ref.png")
    user_path = os.path.join(job_dir, "user.png")
    out1_path = os.path.join(job_dir, "thumb1.png")
    out2_path = os.path.join(job_dir, "thumb2.png")

    # Save uploads
    try:
        ref_pil  = PILImage.open(ref_file).convert("RGB")
        user_pil = PILImage.open(user_file).convert("RGBA")
        ref_pil.save(ref_path,  "PNG")
        user_pil.save(user_path, "PNG")
    except Exception as e:
        return jsonify({"error": f"Could not open images: {str(e)}"}), 400

    # Encode reference for Claude
    with open(ref_path, "rb") as f:
        ref_b64 = base64.b64encode(f.read()).decode("utf-8")

    # Get AI text content
    text_data, analysis = call_claude_for_text(ref_b64, title, niche, style, extra)
    headline = text_data.get("headline", title.upper())
    subtext  = text_data.get("subtext", "")
    badge    = text_data.get("badge", "")

    # Extract person (remove background)
    try:
        person_extracted = extract_person_smart(user_pil)
    except Exception as e:
        return jsonify({"error": f"Person extraction failed: {str(e)}"}), 500

    # ── VARIANT 1: Person on RIGHT ──
    try:
        comp1 = composite_person_onto_thumbnail(ref_pil, person_extracted, "right")
        final1 = add_text_overlay(comp1, headline, subtext, badge, "right", niche, style)
        final1.convert("RGB").save(out1_path, "PNG")
    except Exception as e:
        return jsonify({"error": f"Variant 1 failed: {str(e)}"}), 500

    # ── VARIANT 2: Person on LEFT ──
    try:
        comp2 = composite_person_onto_thumbnail(ref_pil, person_extracted, "left")
        final2 = add_text_overlay(comp2, headline, subtext, badge, "left", niche, style)
        final2.convert("RGB").save(out2_path, "PNG")
    except Exception as e:
        return jsonify({"error": f"Variant 2 failed: {str(e)}"}), 500

    return jsonify({
        "analysis": analysis,
        "thumbnails": [
            {"url": f"/thumb-output/{job_id}/1", "label": "Variant 1 — Person Right"},
            {"url": f"/thumb-output/{job_id}/2", "label": "Variant 2 — Person Left"},
        ]
    })


@app.route("/thumb-output/<job_id>/<int:num>")
@login_required
def thumb_output(job_id, num):
    filename = f"thumb{num}.png"
    directory = os.path.abspath(os.path.join(THUMB_FOLDER, job_id))
    return send_file(os.path.join(directory, filename), mimetype="image/png")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()
        if verify_user(email, password):
            session["user"] = email
            return redirect("/")
        return redirect("/login?error=1")
    return send_from_directory(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), "login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()
        confirm = request.form.get("confirm", "").strip()
        if not email or not password:
            return redirect("/register?error=empty")
        if password != confirm:
            return redirect("/register?error=mismatch")
        if len(password) < 6:
            return redirect("/register?error=short")
        success, msg = create_user(email, password)
        if success:
            return redirect("/register?registered=1")
        return redirect("/register?error=exists")
    return send_from_directory(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), "register.html")


@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect("/login")


@app.route("/pricing")
@login_required
def pricing_page():
    return send_from_directory(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), "pricing.html")


@app.route("/get-plan")
@login_required
def get_plan():
    return jsonify(get_usage_summary(session["user"]))


@app.route("/usage-summary")
@login_required
def usage_summary():
    return jsonify(get_usage_summary(session["user"]))


@app.route("/check-coupon", methods=["POST"])
@login_required
def check_coupon():
    data = request.get_json()
    code = data.get("coupon", "").strip().upper()
    if code not in COUPON_CODES:
        return jsonify({"valid": False, "message": "Invalid coupon code"})
    plan, discount, description = COUPON_CODES[code]
    return jsonify({"valid": True, "plan": plan, "discount": discount,
                    "description": description, "message": f"✅ {description}"})


@app.route("/apply-coupon", methods=["POST"])
@login_required
def apply_coupon_route():
    data    = request.get_json()
    code    = data.get("coupon", "")
    billing = data.get("billing", "monthly")
    success, message, discount = apply_coupon(session["user"], code, billing)
    if success:
        return jsonify({"success": True, "message": message,
                        "plan": get_user_plan(session["user"]), "discount": discount})
    return jsonify({"success": False, "message": message})


@app.route("/process-payment", methods=["POST"])
@login_required
def payment_route():
    data    = request.get_json()
    plan    = data.get("plan", "pro")
    billing = data.get("billing", "monthly")
    coupon  = data.get("coupon", "")
    upi_id  = data.get("upi_id", "")
    if not upi_id or "@" not in upi_id:
        return jsonify({"success": False, "message": "Please enter a valid UPI ID (e.g. name@upi)"})
    success, message, amount = process_payment(session["user"], plan, billing, coupon)
    if success:
        return jsonify({"success": True, "message": message, "amount": amount,
                        "plan": get_user_plan(session["user"])})
    return jsonify({"success": False, "message": message})


@app.route("/upgrade", methods=["POST"])
@login_required
def upgrade():
    conn = sqlite3.connect(DB_FILE)
    conn.execute("UPDATE users SET plan='pro' WHERE email=?", (session["user"],))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/upload", methods=["POST"])
@login_required
def upload():
    user = session["user"]
    if get_user_plan(user) == "free" and not can_edit(user):
        return jsonify({"error": "Free limit reached (2/day). Upgrade to Pro."}), 403

    if "video" not in request.files:
        return jsonify({"error": "No video file provided"}), 400
    file = request.files["video"]
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    job_id = str(uuid.uuid4())
    filename = secure_filename(file.filename)
    save_path = os.path.join(UPLOAD_FOLDER, f"{job_id}_{filename}")
    file.save(save_path)

    music_save_path = None
    if "music" in request.files and request.files["music"].filename:
        music_file = request.files["music"]
        music_filename = secure_filename(music_file.filename)
        music_save_path = os.path.join(MUSIC_FOLDER, f"{job_id}_{music_filename}")
        music_file.save(music_save_path)

    settings = {
        "cut_silences": request.form.get("cut_silences", "true") == "true",
        "subtitles": request.form.get("subtitles", "true") == "true",
        "color_grade": request.form.get("color_grade", "warm"),
        "title": request.form.get("title", "My Video"),
        "intro_outro": request.form.get("intro_outro", "true") == "true",
        "both_formats": request.form.get("both_formats", "true") == "true",
        "music_volume": float(request.form.get("music_volume", "0.15")),
        "upload_to_drive": request.form.get("upload_to_drive", "false") == "true",
    }

    jobs[job_id] = {"status": "queued", "progress": 0, "message": "Queued...", "output_files": {}}
    thread = threading.Thread(
        target=edit_pipeline,
        args=(job_id, save_path, settings, music_save_path),
        daemon=True
    )
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
@login_required
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


@app.route("/download/<job_id>/<filename>")
@login_required
def download(job_id, filename):
    directory = os.path.abspath(os.path.join(OUTPUT_FOLDER, job_id))
    file_path = os.path.join(directory, filename)
    if not os.path.exists(file_path):
        return jsonify({"error": "File not found"}), 404
    return send_file(file_path, as_attachment=True)


@app.route("/script")
@login_required
def script_page():
    return send_from_directory(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), "script.html")


@app.route("/generate-script", methods=["POST"])
@login_required
def generate_script_route():
    data = request.get_json()
    topic = data.get("topic", "").strip()
    style = data.get("style", "Educational")
    duration = data.get("duration", 5)
    audience = data.get("audience", "General Audience")
    tone = data.get("tone", "Friendly & Casual")
    cta = data.get("cta", "Like & Subscribe")
    extra = data.get("extra", "")
    lang_instruction = data.get("lang_instruction", "")
    if not topic:
        return jsonify({"error": "Topic is required"}), 400
    allowed_s, used_s, limit_s = check_feature_limit(session["user"], "script")
    if not allowed_s:
        return jsonify({"error": f"Daily script limit reached ({used_s}/{limit_s}). Upgrade to Pro for unlimited.", "limit_reached": True}), 403
    consume_feature(session["user"], "script")
    script, error = generate_script(topic, style, duration, audience, tone, cta, extra, lang_instruction)
    if error:
        return jsonify({"error": error}), 500
    return jsonify({"script": script})





if __name__ == "__main__":
    init_db()
    print("Server starting at http://127.0.0.1:5000")
    print("Register at:   http://127.0.0.1:5000/register")
    print("Script Writer: http://127.0.0.1:5000/script")

import os

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
