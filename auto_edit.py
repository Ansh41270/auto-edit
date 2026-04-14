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

# ════════════════════════════════════════════════════════
#  APP SETUP
# ════════════════════════════════════════════════════════

app = Flask(
    __name__,
    static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"),
    static_url_path=""
)

app.secret_key = os.environ.get("SECRET_KEY", "super_secret_key_change_this")
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# True only on Render (HTTPS), False on localhost (HTTP)
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RENDER", False))

UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"
MUSIC_FOLDER  = "music"
THUMB_FOLDER  = "thumbnails"
DB_FILE       = "users.db"

for folder in [UPLOAD_FOLDER, OUTPUT_FOLDER, MUSIC_FOLDER]:
    os.makedirs(folder, exist_ok=True)

if os.path.isfile(THUMB_FOLDER):
    os.remove(THUMB_FOLDER)
os.makedirs(THUMB_FOLDER, exist_ok=True)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
if not ANTHROPIC_API_KEY:
    print("=" * 60)
    print("WARNING: ANTHROPIC_API_KEY is not set!")
    print("AI features will use fallback mode only.")
    print("Set it in your environment or Render dashboard.")
    print("=" * 60)

GOOGLE_DRIVE_FOLDER_ID  = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
GOOGLE_CREDENTIALS_PATH = os.environ.get("GOOGLE_CREDENTIALS_PATH", "")

jobs = {}


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
    # Auto-migrate: add missing columns if they don't exist (fixes old databases)
    migrations = [
        "ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'free'",
        "ALTER TABLE users ADD COLUMN plan_expires TEXT DEFAULT NULL",
        "ALTER TABLE users ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE payments ADD COLUMN billing TEXT DEFAULT 'monthly'",
        "ALTER TABLE payments ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    ]
    for sql in migrations:
        try:
            c.execute(sql)
        except:
            pass
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
        c.execute("INSERT INTO users (email, password_hash, salt) VALUES (?, ?, ?)",
                  (email.lower().strip(), pw_hash, salt))
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
    final = round(base * (1 - discount / 100), 2)
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


# ════════════════════════════════════════════════════════
#  AUTH DECORATOR
# ════════════════════════════════════════════════════════

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            if request.is_json or request.method == "POST":
                return jsonify({"error": "Not logged in. Please refresh and log in again."}), 401
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
            GOOGLE_CREDENTIALS_PATH, scopes=["https://www.googleapis.com/auth/drive"])
        service = build("drive", "v3", credentials=creds)
        file_metadata = {"name": filename}
        if GOOGLE_DRIVE_FOLDER_ID:
            file_metadata["parents"] = [GOOGLE_DRIVE_FOLDER_ID]
        media = MediaFileUpload(file_path, resumable=True)
        uploaded = service.files().create(
            body=file_metadata, media_body=media, fields="id, webViewLink").execute()
        return uploaded.get("webViewLink"), None
    except ImportError:
        return None, "Run: pip install google-api-python-client google-auth"
    except Exception as e:
        return None, str(e)


# ════════════════════════════════════════════════════════
#  AI SCRIPT WRITER
# ════════════════════════════════════════════════════════

def generate_script(topic, style, duration_mins, audience="General", tone="Friendly",
                    cta="Like & Subscribe", extra="", lang_instruction=""):
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
            f"Write a complete YouTube video script.\n\nTopic: {topic}\nStyle: {style}\n"
            f"Target Duration: {duration_mins} minutes\nTarget Audience: {audience}\n"
            f"Tone: {tone}\nCall To Action: {cta}\n"
            f"Extra Instructions: {extra if extra else 'None'}\n\n{lang_instruction}\n\n"
            f"Include these sections with clear headers:\n"
            f"1. HOOK\n2. INTRO\n3. MAIN CONTENT (3-4 sections)\n4. TIPS\n5. CALL TO ACTION\n6. OUTRO\n\n"
            f"Make it engaging, natural, and suitable for the {audience} audience."
        )
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 2500,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=40
        )
        data = response.json()
        if "content" in data and len(data["content"]) > 0:
            return data["content"][0]["text"], None
        return None, f"API error: {data.get('error', {}).get('message', 'Unknown error')}"
    except Exception as e:
        return None, str(e)


# ════════════════════════════════════════════════════════
#  AI VIDEO ENGINE — 8-FEATURE PIPELINE
# ════════════════════════════════════════════════════════

def run_cmd(cmd, job_id, step_msg, progress):
    jobs[job_id]["message"] = step_msg
    jobs[job_id]["progress"] = progress
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{step_msg} failed:\n{result.stderr[-600:]}")
    return result.stdout


def get_duration(path):
    cmd = f'ffprobe -v error -show_entries format=duration -of csv=p=0 "{path}"'
    out = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except:
        return None


def get_video_info(path):
    cmd = f'ffprobe -v error -select_streams v:0 -show_entries stream=width,height,r_frame_rate -of csv=p=0 "{path}"'
    out = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    try:
        parts = out.stdout.strip().split(",")
        w, h = int(parts[0]), int(parts[1])
        fps_parts = parts[2].split("/")
        fps = round(int(fps_parts[0]) / int(fps_parts[1]), 2)
        return w, h, fps
    except:
        return 1920, 1080, 30


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


# ── FEATURE 1: AI Quality Enhancement ──
def ai_quality_enhance(input_path, output_path, job_id):
    w, h, fps = get_video_info(input_path)
    vf_parts = []
    if w < 1920:
        vf_parts.append("scale=1920:1080:flags=lanczos")
    vf_parts.extend([
        "deshake=x=-1:y=-1:w=-1:h=-1:rx=16:ry=16",
        "hqdn3d=4:3:6:4",
        "unsharp=5:5:1.0:5:5:0.0",
        "eq=contrast=1.05:brightness=0.02:saturation=1.1",
    ])
    vf = ",".join(vf_parts)
    af = "afftdn=nf=-25,loudnorm=I=-16:TP=-1.5:LRA=11"
    run_cmd(
        f'ffmpeg -y -i "{input_path}" -vf "{vf}" -af "{af}" -c:v libx264 -crf 18 -preset medium -c:a aac -b:a 192k "{output_path}"',
        job_id, "🔬 AI quality enhancement & stabilization...", 8
    )


# ── FEATURE 2: Auto Caption Generation (Whisper AI) ──
def generate_subtitles(audio_path, out_srt):
    try:
        import whisper
        model = whisper.load_model("base")
        result = model.transcribe(audio_path)
        segments = result.get("segments", [])
        with open(out_srt, "w", encoding="utf-8") as f:
            for i, seg in enumerate(segments, 1):
                f.write(f"{i}\n{_fmt_time(seg['start'])} --> {_fmt_time(seg['end'])}\n{seg['text'].strip()}\n\n")
        return True, len(segments)
    except ImportError:
        with open(out_srt, "w") as f:
            f.write("1\n00:00:00,000 --> 00:00:03,000\n[Install openai-whisper for captions]\n\n")
        return False, 0
    except Exception:
        with open(out_srt, "w") as f:
            f.write("1\n00:00:00,000 --> 00:00:03,000\n[Caption error]\n\n")
        return False, 0


def _fmt_time(s):
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:06.3f}".replace(".", ",")


# ── FEATURE 3: AI Color Grading ──
COLOR_GRADES = {
    "warm":      "eq=contrast=1.1:brightness=0.02:saturation=1.2:gamma_r=1.08:gamma_b=0.93",
    "cool":      "eq=contrast=1.12:brightness=-0.01:saturation=1.1:gamma_r=0.93:gamma_b=1.08",
    "cinematic": "eq=contrast=1.25:brightness=-0.04:saturation=0.85:gamma=1.1,vignette=PI/5",
    "vintage":   "eq=contrast=1.05:brightness=0.03:saturation=0.7:gamma_r=1.05:gamma_b=0.85",
    "vivid":     "eq=contrast=1.15:brightness=0.01:saturation=1.6:gamma=0.95,unsharp=3:3:0.5",
    "moody":     "eq=contrast=1.3:brightness=-0.06:saturation=0.75:gamma=1.2,vignette=PI/4",
    "none":      None,
}


# ── FEATURE 4: AI Transitions ──
def add_transitions(input_path, output_path, job_id, total_dur, transition_type="fade"):
    fade_dur = 0.5
    fade_out_start = max(total_dur - fade_dur, 0)
    vf = f"fade=t=in:st=0:d={fade_dur},fade=t=out:st={fade_out_start:.2f}:d={fade_dur}"
    af = f"afade=t=in:st=0:d={fade_dur},afade=t=out:st={fade_out_start:.2f}:d={fade_dur}"
    if transition_type == "zoom":
        vf = (f"fade=t=in:st=0:d={fade_dur},"
              f"zoompan=z='min(zoom+0.0004,1.04)':d=25:s=1920x1080,"
              f"fade=t=out:st={fade_out_start:.2f}:d={fade_dur}")
    run_cmd(
        f'ffmpeg -y -i "{input_path}" -vf "{vf}" -af "{af}" -c:v libx264 -c:a aac -preset fast "{output_path}"',
        job_id, f"🎞️ Adding {transition_type} transitions...", 58
    )


# ── FEATURES 5 & 6: AI Audio Enhancement + Music Mixing ──
def process_audio(input_path, output_path, music_path, music_volume, job_id):
    audio_enhance = (
        "afftdn=nf=-20,"
        "acompressor=threshold=-18dB:ratio=3:attack=5:release=50,"
        "equalizer=f=200:t=o:w=2:g=2,"
        "equalizer=f=3000:t=o:w=2:g=1.5,"
        "loudnorm=I=-16:TP=-1.5:LRA=11"
    )
    if music_path and os.path.exists(music_path):
        run_cmd(
            f'ffmpeg -y -i "{input_path}" -i "{music_path}" '
            f'-filter_complex "[0:a]{audio_enhance}[speech];'
            f'[1:a]volume={music_volume},aloop=loop=-1:size=2000000000[music];'
            f'[speech][music]amix=inputs=2:duration=first:dropout_transition=3[aout]" '
            f'-map 0:v -map "[aout]" -c:v copy -c:a aac -b:a 192k -shortest "{output_path}"',
            job_id, "🎵 AI audio enhancement + music mixing...", 62
        )
    else:
        run_cmd(
            f'ffmpeg -y -i "{input_path}" -af "{audio_enhance}" -c:v copy -c:a aac -b:a 192k "{output_path}"',
            job_id, "🎵 AI audio enhancement...", 62
        )


# ── FEATURE 7: Animated Intro/Outro ──
def add_intro_outro(input_path, output_path, title, total_dur, job_id):
    safe  = title.replace("'", "\\'").replace(":", "\\:")
    end_t = max(total_dur - 3.5, 0)
    vf = (
        f"drawtext=text='{safe}':fontcolor=white:fontsize=52:"
        f"x=(w-text_w)/2:y=(h-text_h)/2-30:enable='between(t,0,3)':"
        f"box=1:boxcolor=black@0.5:boxborderw=14,"
        f"drawtext=text='AI Edit Studio':fontcolor=#6c63ff:fontsize=24:"
        f"x=(w-text_w)/2:y=(h-text_h)/2+40:enable='between(t,0.3,3)',"
        f"drawtext=text='Thanks for Watching!':fontcolor=white:fontsize=42:"
        f"x=(w-text_w)/2:y=(h-text_h)/2-20:enable='between(t,{end_t},{total_dur})':"
        f"box=1:boxcolor=black@0.5:boxborderw=12,"
        f"drawtext=text='Like • Subscribe • Share':fontcolor=#ffa94d:fontsize=26:"
        f"x=(w-text_w)/2:y=(h-text_h)/2+50:enable='between(t,{end_t+0.5},{total_dur})'"
    )
    run_cmd(
        f'ffmpeg -y -i "{input_path}" -vf "{vf}" -c:v libx264 -c:a copy -preset fast "{output_path}"',
        job_id, "🎬 Adding animated intro & outro...", 72
    )


# ── FEATURE 8: Smart Multi-Format Export ──
def smart_export(input_path, base_dir, job_id, both_formats=True, export_mode="landscape"):
    output_files = {}
    SPECS = {
        "landscape": (1920, 1080, "final_youtube_1080p.mp4",  "📤 Exporting 16:9 1080p...",    82, "landscape"),
        "portrait":  (1080, 1920, "final_vertical_9x16.mp4",  "📤 Exporting 9:16 vertical...", 86, "portrait"),
        "square":    (1080, 1080, "final_square_1x1.mp4",     "📤 Exporting 1:1 square...",    86, "square"),
    }
    w, h, fname, msg, prog, key = SPECS.get(export_mode, SPECS["landscape"])
    out_path = os.path.join(base_dir, fname)
    run_cmd(
        f'ffmpeg -y -i "{input_path}" '
        f'-vf "scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black" '
        f'-c:v libx264 -crf 20 -preset fast -c:a aac -b:a 192k -movflags +faststart "{out_path}"',
        job_id, msg, prog
    )
    output_files[key] = f"/download/{job_id}/{fname}"
    if both_formats:
        secondary = [s for s in ["landscape", "portrait", "square"] if s != export_mode]
        prog2 = 90
        for mode in secondary[:1]:
            w2, h2, fname2, msg2, _, key2 = SPECS[mode]
            out2 = os.path.join(base_dir, fname2)
            run_cmd(
                f'ffmpeg -y -i "{input_path}" '
                f'-vf "scale={w2}:{h2}:force_original_aspect_ratio=decrease,pad={w2}:{h2}:(ow-iw)/2:(oh-ih)/2:color=black" '
                f'-c:v libx264 -crf 20 -preset fast -c:a aac -b:a 192k -movflags +faststart "{out2}"',
                job_id, msg2, prog2
            )
            output_files[key2] = f"/download/{job_id}/{fname2}"
            prog2 += 3
    return output_files


# ════════════════════════════════════════════════════════
#  MAIN AI PIPELINE — orchestrates all 8 features
# ════════════════════════════════════════════════════════

def edit_pipeline(job_id, input_path, settings, music_path_input=None):
    try:
        jobs[job_id]["status"] = "processing"
        base = os.path.join(OUTPUT_FOLDER, job_id)
        os.makedirs(base, exist_ok=True)

        total_dur = get_duration(input_path)
        if total_dur is None:
            raise RuntimeError("Could not read video duration. Is ffprobe installed?")

        jobs[job_id]["total_steps"] = 8
        jobs[job_id]["steps_done"]  = 0

        # STEP 1: Extract audio
        audio_path = os.path.join(base, "audio.wav")
        run_cmd(f'ffmpeg -y -i "{input_path}" -vn -acodec pcm_s16le -ar 16000 -ac 1 "{audio_path}"',
                job_id, "🎙️ Extracting audio track...", 3)
        jobs[job_id]["steps_done"] = 1

        # STEP 2: AI Quality Enhancement
        enhanced_path = os.path.join(base, "enhanced.mp4")
        if settings.get("quality_enhance", True):
            ai_quality_enhance(input_path, enhanced_path, job_id)
        else:
            run_cmd(f'ffmpeg -y -i "{input_path}" -c copy "{enhanced_path}"',
                    job_id, "Skipping quality enhancement...", 8)
        jobs[job_id]["steps_done"] = 2

        # STEP 3: Silence Cutting
        cut_path = os.path.join(base, "cut.mp4")
        silence_thresh = settings.get("silence_thresh", "-35dB")
        silence_min    = settings.get("silence_min", 0.5)
        if settings.get("cut_silences", True):
            silences = detect_silences(enhanced_path, noise=silence_thresh, duration=silence_min)
            cut_dur  = get_duration(enhanced_path) or total_dur
            vf, af   = build_cut_filter(silences, cut_dur, min_seg=silence_min)
            if vf:
                run_cmd(
                    f'ffmpeg -y -i "{enhanced_path}" -vf "{vf}" -af "{af}" -c:v libx264 -c:a aac -preset fast "{cut_path}"',
                    job_id, "✂️ Cutting silences...", 18)
            else:
                run_cmd(f'ffmpeg -y -i "{enhanced_path}" -c copy "{cut_path}"', job_id, "No silences found...", 18)
        else:
            run_cmd(f'ffmpeg -y -i "{enhanced_path}" -c copy "{cut_path}"', job_id, "Skipping silence cut...", 18)
        jobs[job_id]["steps_done"] = 3

        # STEP 4: AI Color Grading
        graded_path = os.path.join(base, "graded.mp4")
        grade     = settings.get("color_grade", "warm")
        eq_filter = COLOR_GRADES.get(grade)
        if eq_filter:
            run_cmd(f'ffmpeg -y -i "{cut_path}" -vf "{eq_filter}" -c:v libx264 -c:a copy -preset fast "{graded_path}"',
                    job_id, f"🎨 Applying {grade} color grade...", 30)
        else:
            run_cmd(f'ffmpeg -y -i "{cut_path}" -c copy "{graded_path}"', job_id, "Skipping color grade...", 30)
        jobs[job_id]["steps_done"] = 4

        # STEP 5: Auto Captions
        srt_path       = os.path.join(base, "captions.srt")
        subtitled_path = os.path.join(base, "captioned.mp4")
        if settings.get("subtitles", True):
            ok, num_segs = generate_subtitles(audio_path, srt_path)
            srt_fixed    = srt_path.replace("\\", "/").replace(":", "\\:")
            run_cmd(
                f'ffmpeg -y -i "{graded_path}" '
                f'-vf "subtitles={srt_fixed}:force_style=\'FontSize=20,PrimaryColour=&Hffffff,OutlineColour=&H000000,Outline=2,Bold=1,Alignment=2\'" '
                f'-c:v libx264 -c:a copy -preset fast "{subtitled_path}"',
                job_id, f"💬 Burning captions ({num_segs} segments)...", 44)
        else:
            run_cmd(f'ffmpeg -y -i "{graded_path}" -c copy "{subtitled_path}"', job_id, "Skipping captions...", 44)
        jobs[job_id]["steps_done"] = 5

        # STEP 6: Audio Enhancement + Music
        audio_final = os.path.join(base, "audio_enhanced.mp4")
        process_audio(subtitled_path, audio_final, music_path_input, settings.get("music_volume", 0.12), job_id)
        jobs[job_id]["steps_done"] = 6

        # STEP 7: Transitions + Intro/Outro
        trans_path = os.path.join(base, "with_transitions.mp4")
        cur_dur    = get_duration(audio_final) or total_dur
        if settings.get("transitions", True):
            add_transitions(audio_final, trans_path, job_id, cur_dur, settings.get("transition_type", "fade"))
        else:
            run_cmd(f'ffmpeg -y -i "{audio_final}" -c copy "{trans_path}"', job_id, "Skipping transitions...", 58)

        final_pre = os.path.join(base, "pre_export.mp4")
        if settings.get("intro_outro", False):
            cur_dur2 = get_duration(trans_path) or total_dur
            add_intro_outro(trans_path, final_pre, settings.get("title", "My Video"), cur_dur2, job_id)
        else:
            run_cmd(f'ffmpeg -y -i "{trans_path}" -c copy "{final_pre}"', job_id, "Skipping intro/outro...", 72)
        jobs[job_id]["steps_done"] = 7

        # STEP 8: Smart Export
        output_files = smart_export(final_pre, base, job_id,
                                    settings.get("both_formats", False),
                                    settings.get("export_mode", "landscape"))

        if settings.get("subtitles", True) and os.path.exists(srt_path):
            output_files["srt"] = f"/download/{job_id}/captions.srt"

        if settings.get("upload_to_drive", False):
            jobs[job_id]["message"]  = "☁️ Uploading to Google Drive..."
            jobs[job_id]["progress"] = 96
            lp = os.path.join(base, "final_youtube_1080p.mp4")
            link, _ = upload_to_drive(lp, f"{settings.get('title','video')}_1080p.mp4")
            if link:
                output_files["landscape_drive"] = link
        jobs[job_id]["steps_done"] = 8

        jobs[job_id].update({
            "status": "done", "progress": 100,
            "message": "✅ AI editing complete!",
            "output_files": output_files, "steps_done": 8,
        })

    except Exception as e:
        jobs[job_id].update({"status": "error", "progress": 0, "message": str(e)})


# ════════════════════════════════════════════════════════
#  THUMBNAIL ENGINE — Professional AI Generation
# ════════════════════════════════════════════════════════

def hex_to_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def get_best_font(size):
    from PIL import ImageFont
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:/Windows/Fonts/impact.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/Arial_Bold.ttf",
        "C:/Windows/Fonts/verdanab.ttf",
        "C:/Windows/Fonts/calibrib.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except:
                continue
    return ImageFont.load_default()


def detect_face_region(img):
    arr = np.array(img.convert("RGB"))
    h, w = arr.shape[:2]
    r, g, b = arr[:,:,0].astype(float), arr[:,:,1].astype(float), arr[:,:,2].astype(float)
    skin = ((r > 60) & (g > 40) & (b > 20) & (r > g) & (r > b) &
            (r - g > 10) & (np.abs(r.astype(int) - g.astype(int)) > 8) &
            (r < 250) & (g < 220) & (b < 200))
    rows = np.where(skin.any(axis=1))[0]
    cols = np.where(skin.any(axis=0))[0]
    if len(rows) < 10 or len(cols) < 10:
        return (w//4, 0, 3*w//4, h//2)
    y1, y2 = int(rows.min()), int(rows.max())
    x1, x2 = int(cols.min()), int(cols.max())
    pad = 30
    return (max(0, x1-pad), max(0, y1-pad), min(w, x2+pad), min(h, y2+pad))


def extract_person_smart(user_img):
    from PIL import ImageFilter
    PILImage = Image
    img_rgba = user_img.convert("RGBA")

    try:
        from rembg import remove as rembg_remove
        return rembg_remove(img_rgba)
    except (ImportError, Exception):
        pass

    try:
        import cv2
        img_rgb = np.array(user_img.convert("RGB"))
        h, w = img_rgb.shape[:2]
        mask = np.zeros((h, w), np.uint8)
        bgd_model = np.zeros((1, 65), np.float64)
        fgd_model = np.zeros((1, 65), np.float64)
        margin_x, margin_y = int(w * 0.10), int(h * 0.05)
        rect = (margin_x, margin_y, w - 2*margin_x, h - 2*margin_y)
        cv2.grabCut(img_rgb, mask, rect, bgd_model, fgd_model, 8, cv2.GC_INIT_WITH_RECT)
        fg_mask = np.where((mask == 2) | (mask == 0), 0, 255).astype(np.uint8)
        fg_mask = cv2.GaussianBlur(fg_mask, (7, 7), 0)
        result = PILImage.fromarray(img_rgb).convert("RGBA")
        r, g, b, a = result.split()
        return PILImage.merge("RGBA", (r, g, b, PILImage.fromarray(fg_mask)))
    except (ImportError, Exception):
        pass

    # Fallback: edge-aware background removal
    img_arr = np.array(img_rgba)
    h, w = img_arr.shape[:2]
    edge_pixels = np.concatenate([
        img_arr[:8, :, :3].reshape(-1, 3), img_arr[-8:, :, :3].reshape(-1, 3),
        img_arr[:, :8, :3].reshape(-1, 3), img_arr[:, -8:, :3].reshape(-1, 3),
    ])
    bg_color = np.median(edge_pixels, axis=0).astype(int)
    diff  = np.sqrt(np.sum((img_arr[:,:,:3].astype(int) - bg_color)**2, axis=2))
    alpha = np.clip((diff - 55) / 30 * 255, 0, 255).astype(np.uint8)
    cy, cx = h//2, w//2
    Y, X = np.ogrid[:h, :w]
    center_dist  = np.sqrt(((X-cx)/(w*0.35))**2 + ((Y-cy)/(h*0.45))**2)
    center_boost = np.clip((1.0 - center_dist) * 180, 0, 180).astype(np.uint8)
    alpha = np.clip(alpha.astype(int) + center_boost, 0, 255).astype(np.uint8)
    result = img_rgba.copy()
    result.putalpha(PILImage.fromarray(alpha).filter(ImageFilter.GaussianBlur(2)))
    return result


def composite_person_onto_thumbnail(ref_img, person_img, position="right"):
    from PIL import ImageFilter, ImageDraw
    PILImage = Image

    W, H = ref_img.size
    ref_rgba = ref_img.convert("RGBA")

    target_h = int(H * 1.05)
    aspect   = person_img.width / person_img.height
    target_w = int(target_h * aspect)
    if target_w < int(W * 0.3):
        target_w = int(W * 0.38)
        target_h = int(target_w / aspect)

    person_scaled = person_img.resize((target_w, target_h), PILImage.LANCZOS)
    px = W - target_w + int(target_w * 0.05) if position == "right" else -int(target_w * 0.05)
    py = H - target_h

    # Darken text side
    dark_layer = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
    dd = ImageDraw.Draw(dark_layer)
    if position == "right":
        dd.rectangle([0, 0, W//2 + 60, H], fill=(0, 0, 0, 90))
    else:
        dd.rectangle([W//2 - 60, 0, W, H], fill=(0, 0, 0, 90))
    ref_rgba = PILImage.alpha_composite(ref_rgba, dark_layer)

    # Drop shadow
    shadow_layer = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
    if person_scaled.mode == "RGBA":
        alpha_ch    = person_scaled.split()[3]
        shadow_alpha = PILImage.new("L", person_scaled.size, 0)
        shadow_alpha.paste(alpha_ch)
        shadow_img  = PILImage.new("RGBA", person_scaled.size, (0, 0, 0, 0))
        shadow_data = np.array(shadow_img)
        shadow_data[:,:,3] = (np.array(shadow_alpha) * 0.65).astype(np.uint8)
        shadow_pil = PILImage.fromarray(shadow_data, "RGBA").filter(ImageFilter.GaussianBlur(22))
        shadow_layer.paste(shadow_pil, (px + 20, py + 20), shadow_pil)
    ref_rgba = PILImage.alpha_composite(ref_rgba, shadow_layer)

    # Edge glow — sampled from reference
    ref_small = ref_img.resize((50, 28)).convert("RGB")
    ref_arr   = np.array(ref_small).reshape(-1, 3)
    bright_mask = ref_arr.max(axis=1) > 180
    glow_color = ref_arr[bright_mask].mean(axis=0).astype(int) if bright_mask.sum() > 5 else np.array([100, 150, 255])

    if person_scaled.mode == "RGBA":
        glow_layer  = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
        glow_alpha  = PILImage.new("L", (W, H), 0)
        glow_alpha.paste(person_scaled.split()[3], (px, py))
        glow_blurred = glow_alpha.filter(ImageFilter.GaussianBlur(25))
        glow_arr = np.zeros((H, W, 4), dtype=np.uint8)
        glow_arr[:,:,0] = int(glow_color[0])
        glow_arr[:,:,1] = int(glow_color[1])
        glow_arr[:,:,2] = int(glow_color[2])
        glow_arr[:,:,3] = (np.array(glow_blurred) * 0.55).astype(np.uint8)
        ref_rgba = PILImage.alpha_composite(ref_rgba, PILImage.fromarray(glow_arr))

    # Color match person to reference
    ref_gray   = np.array(ref_img.convert("L")).mean()
    person_arr = np.array(person_scaled).astype(float)
    if person_scaled.mode == "RGBA":
        rgb = person_arr[:,:,:3]
        person_gray = rgb.mean()
        if person_gray > 10:
            rgb = np.clip(rgb * min(ref_gray / person_gray, 1.3), 0, 255)
        person_arr[:,:,:3] = rgb
        person_matched = PILImage.fromarray(person_arr.astype(np.uint8), "RGBA")
    else:
        person_matched = person_scaled

    person_layer = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
    if person_matched.mode == "RGBA":
        person_layer.paste(person_matched, (px, py), person_matched)
    else:
        person_layer.paste(person_matched.convert("RGBA"), (px, py))
    return PILImage.alpha_composite(ref_rgba, person_layer)


def add_text_overlay(img, headline, subtext, badge, position, niche, style,
                     accent_hex=None, energy="high"):
    from PIL import ImageDraw
    PILImage = Image

    W, H = img.size
    canvas = img.convert("RGBA")
    draw   = ImageDraw.Draw(canvas)

    tx    = 38 if position == "right" else W // 2 + 30
    max_w = W // 2 - 60

    # Accent color
    if accent_hex:
        try:
            accent = hex_to_rgb(accent_hex)
        except:
            accent = (255, 200, 0)
    else:
        style_lower = style.lower()
        niche_lower = niche.lower()
        if "dark" in style_lower or "cinematic" in style_lower:   accent = (255, 50, 50)
        elif "cool" in style_lower or "tech" in niche_lower:      accent = (60, 180, 255)
        elif "green" in style_lower or "fitness" in niche_lower:  accent = (60, 220, 100)
        elif "pink" in style_lower or "beauty" in niche_lower:    accent = (255, 80, 180)
        elif "gaming" in niche_lower:                              accent = (0, 255, 120)
        elif "finance" in niche_lower:                             accent = (255, 215, 0)
        else:
            arr = np.array(img.convert("RGB"))
            hsv_sat = (arr.max(axis=2).astype(int) - arr.min(axis=2).astype(int))
            bright  = arr[hsv_sat > 80]
            accent  = tuple(bright[np.argmax(bright.max(axis=1))].tolist()) if len(bright) > 20 else (255, 200, 0)

    size_map = {
        "high":   [115, 95, 78, 62, 48],
        "medium": [100, 82, 66, 54, 42],
        "low":    [85,  70, 56, 46, 36],
    }
    font_sizes = size_map.get(energy, size_map["high"])

    def fit_font_lines(text, max_width):
        for size in font_sizes:
            f = get_best_font(size)
            words = text.split()
            if not words:
                return f, [""]
            bb = draw.textbbox((0, 0), text, font=f)
            if bb[2] - bb[0] <= max_width:
                return f, [text]
            if len(words) >= 2:
                mid = len(words) // 2
                l1, l2 = " ".join(words[:mid]), " ".join(words[mid:])
                b1 = draw.textbbox((0, 0), l1, font=f)
                b2 = draw.textbbox((0, 0), l2, font=f)
                if max(b1[2]-b1[0], b2[2]-b2[0]) <= max_width:
                    return f, [l1, l2]
            if len(words) >= 3:
                t = len(words) // 3
                ls = [" ".join(words[t*i:t*(i+1)]) for i in range(3)]
                ls = [l for l in ls if l]
                if all(draw.textbbox((0, 0), l, font=f)[2] <= max_width for l in ls):
                    return f, ls
        return get_best_font(38), [text[:20]]

    # Dark vignette on text side
    vignette = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
    from PIL import ImageDraw as _ID
    vd = _ID.Draw(vignette)
    if position == "right":
        vd.rectangle([0, 0, int(W * 0.58), H], fill=(0, 0, 0, 110))
    else:
        vd.rectangle([int(W * 0.42), 0, W, H], fill=(0, 0, 0, 110))
    canvas = PILImage.alpha_composite(canvas, vignette)
    draw   = ImageDraw.Draw(canvas)

    y       = 55
    font_sm = get_best_font(36)

    # Badge
    if badge and badge.strip():
        badge_layer = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
        bl = ImageDraw.Draw(badge_layer)
        bb = bl.textbbox((0, 0), badge.upper(), font=font_sm)
        bw, bh = bb[2]-bb[0]+24, bb[3]-bb[1]+14
        bl.rounded_rectangle([tx, y, tx+bw, y+bh], radius=5,
                              fill=(accent[0], accent[1], accent[2], 230))
        bl.text((tx+12, y+7), badge.upper(), font=font_sm, fill=(0, 0, 0, 255))
        canvas = PILImage.alpha_composite(canvas, badge_layer)
        draw   = ImageDraw.Draw(canvas)
        y += bh + 12

    # Headline
    chosen_font, lines = fit_font_lines(headline.upper(), max_w)
    line_colors = [(255, 255, 255), accent, (255, 255, 255), accent]
    stroke_w = 7 if energy == "high" else 5

    for i, line in enumerate(lines):
        color = line_colors[i % len(line_colors)]
        for dx in range(-stroke_w, stroke_w + 1, 2):
            for dy in range(-stroke_w, stroke_w + 1, 2):
                if dx or dy:
                    draw.text((tx+dx, y+dy), line, font=chosen_font, fill=(0, 0, 0, 255))
        draw.text((tx, y), line, font=chosen_font, fill=(*color, 255))
        bb = draw.textbbox((0, 0), line, font=chosen_font)
        y += (bb[3]-bb[1]) + 5
    y += 8

    # Subtext
    if subtext and subtext.strip():
        font_sub = get_best_font(42)
        for dx, dy in [(-2, 2), (2, 2), (0, 3)]:
            draw.text((tx+dx, y+dy), subtext, font=font_sub, fill=(0, 0, 0, 200))
        draw.text((tx, y), subtext, font=font_sub, fill=(200, 200, 200, 255))
        bb = draw.textbbox((0, 0), subtext, font=font_sub)
        y += (bb[3]-bb[1]) + 10

    # Accent stripe
    stripe_layer = PILImage.new("RGBA", (W, H), (0, 0, 0, 0))
    sl = ImageDraw.Draw(stripe_layer)
    stripe_w = min(max_w, int(W * 0.35))
    sl.rectangle([tx, y+4, tx+stripe_w, y+8], fill=(accent[0], accent[1], accent[2], 200))
    canvas = PILImage.alpha_composite(canvas, stripe_layer)

    return canvas


def call_claude_for_text(ref_b64, title, niche, style, extra):
    import json

    prompt = f"""You are a world-class YouTube thumbnail designer with deep knowledge of viral content.

Analyze this reference thumbnail carefully and create optimized content for a NEW thumbnail.

NEW THUMBNAIL DETAILS:
- Video title: "{title}"
- Channel niche: {niche}
- Style target: {style}
- Extra instructions: {extra if extra else "none"}

Return ONLY valid JSON (no markdown, no explanation):
{{
  "headline": "PUNCHY 2-4 WORD TITLE ALL CAPS",
  "subtext": "one short supporting line or empty string",
  "badge": "short badge label like TOP 10 or SHOCKING or MUST SEE or empty string",
  "accent_color": "#hexcode of best accent color matching niche and style",
  "bg_dominant": "#hexcode of main background color from reference",
  "text_position": "left or right",
  "energy": "low or medium or high",
  "analysis": "2 sentence summary of what makes the reference thumbnail work"
}}

RULES: headline = MAX 4 words, punchy. Return ONLY the JSON object, nothing else."""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 400,
                "messages": [{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": ref_b64}},
                    {"type": "text", "text": prompt}
                ]}]
            },
            timeout=35
        )
        data = resp.json()
        if "content" in data:
            raw = data["content"][0]["text"].strip()
            if "```" in raw:
                raw = raw.split("```")[1].lstrip("json\n").strip()
                if "```" in raw:
                    raw = raw.split("```")[0].strip()
            result = json.loads(raw)
            return result, result.get("analysis", "Claude AI analyzed your reference thumbnail.")
    except Exception as e:
        print(f"[call_claude_for_text ERROR] {e}")

    # Smart fallback
    style_accents = {
        "Bold & Bright": "#FFD700", "Dark & Cinematic": "#FF3B3B",
        "Clean & Minimal": "#4FC3F7", "Viral Clickbait": "#FF6B00",
        "Professional": "#00BCD4", "Colorful & Fun": "#FF4081",
    }
    niche_accents = {
        "Gaming": "#00FF88", "Finance": "#FFD700", "Tech": "#4FC3F7",
        "Fitness": "#FF6B35", "Education": "#7C4DFF", "Entertainment": "#FF3B3B",
    }
    accent   = style_accents.get(style, niche_accents.get(niche, "#FFD700"))
    words    = title.upper().split()
    headline = " ".join(words[:4]) if len(words) >= 4 else title.upper()
    return {
        "headline": headline, "subtext": niche, "badge": "WATCH THIS",
        "accent_color": accent, "bg_dominant": "#0a0a0a",
        "text_position": "left", "energy": "high",
    }, "AI text generated using smart fallback (add Anthropic API key for full analysis)"


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
    return send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), "dashboard.html")


# Format selector + sub-pages
@app.route("/auto-edit")
@login_required
def auto_edit_page():
    return send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), "format_selector.html")


@app.route("/auto-edit/youtube")
@login_required
def edit_youtube():
    return send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), "edit_youtube.html")


@app.route("/auto-edit/reels")
@login_required
def edit_reels():
    return send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), "edit_reels.html")


@app.route("/auto-edit/shorts")
@login_required
def edit_shorts():
    return send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), "edit_shorts.html")


@app.route("/auto-edit/podcast")
@login_required
def edit_podcast():
    return send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), "edit_podcast.html")


@app.route("/auto-edit/classic")
@login_required
def edit_classic():
    return send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), "auto_edit.html")


@app.route("/thumbnail-page")
@login_required
def thumbnail_page():
    return send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), "thumbnail.html")


@app.route("/thumbnail", methods=["POST"])
@login_required
def thumbnail():
    import base64, traceback
    PILImage = Image

    try:
        if "ref" not in request.files or "user" not in request.files:
            return jsonify({"error": "Both reference and user images are required."}), 400

        allowed_t, used_t, limit_t = check_feature_limit(session["user"], "thumbnail")
        if not allowed_t:
            return jsonify({
                "error": f"Daily thumbnail limit reached ({used_t}/{limit_t}). Upgrade to Pro for unlimited.",
                "limit_reached": True
            }), 403

        ref_file  = request.files["ref"]
        user_file = request.files["user"]
        title = request.form.get("title", "My Video")
        niche = request.form.get("niche", "Entertainment")
        style = request.form.get("style", "Bold & Bright")
        extra = request.form.get("extra", "")

        if not ref_file.filename or not user_file.filename:
            return jsonify({"error": "Invalid file upload. Please try again."}), 400

        job_id  = str(uuid.uuid4())
        job_dir = os.path.join(THUMB_FOLDER, job_id)
        os.makedirs(job_dir, exist_ok=True)

        ref_path  = os.path.join(job_dir, "ref.png")
        user_path = os.path.join(job_dir, "user.png")
        out1_path = os.path.join(job_dir, "thumb1.png")
        out2_path = os.path.join(job_dir, "thumb2.png")

        try:
            ref_pil  = PILImage.open(ref_file).convert("RGB")
            user_pil = PILImage.open(user_file).convert("RGBA")
            ref_pil.save(ref_path, "PNG")
            user_pil.save(user_path, "PNG")
        except Exception as e:
            return jsonify({"error": f"Could not open images: {str(e)}"}), 400

        with open(ref_path, "rb") as f:
            ref_b64 = base64.b64encode(f.read()).decode("utf-8")

        text_data, analysis = call_claude_for_text(ref_b64, title, niche, style, extra)
        headline   = text_data.get("headline", title.upper())
        subtext    = text_data.get("subtext", "")
        badge      = text_data.get("badge", "")
        accent_hex = text_data.get("accent_color", None)
        energy     = text_data.get("energy", "high")

        try:
            person_extracted = extract_person_smart(user_pil)
        except Exception as e:
            return jsonify({"error": f"Person extraction failed: {str(e)}"}), 500

        try:
            comp1  = composite_person_onto_thumbnail(ref_pil, person_extracted, "right")
            final1 = add_text_overlay(comp1, headline, subtext, badge, "right", niche, style,
                                      accent_hex=accent_hex, energy=energy)
            final1.convert("RGB").save(out1_path, "PNG")
        except Exception as e:
            return jsonify({"error": f"Variant 1 failed: {str(e)}"}), 500

        try:
            comp2  = composite_person_onto_thumbnail(ref_pil, person_extracted, "left")
            final2 = add_text_overlay(comp2, headline, subtext, badge, "left", niche, style,
                                      accent_hex=accent_hex, energy=energy)
            final2.convert("RGB").save(out2_path, "PNG")
        except Exception as e:
            return jsonify({"error": f"Variant 2 failed: {str(e)}"}), 500

        consume_feature(session["user"], "thumbnail")

        return jsonify({
            "analysis": analysis,
            "thumbnails": [
                {"url": f"/thumb-output/{job_id}/1", "label": "Variant 1 — Person Right"},
                {"url": f"/thumb-output/{job_id}/2", "label": "Variant 2 — Person Left"},
            ]
        })

    except Exception as e:
        print(f"[/thumbnail UNHANDLED ERROR] {e}")
        traceback.print_exc()
        return jsonify({"error": f"Server error: {str(e)}"}), 500


@app.route("/thumb-output/<job_id>/<int:num>")
@login_required
def thumb_output(job_id, num):
    filename  = f"thumb{num}.png"
    directory = os.path.abspath(os.path.join(THUMB_FOLDER, job_id))
    return send_file(os.path.join(directory, filename), mimetype="image/png")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email    = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()
        if verify_user(email, password):
            session["user"] = email
            return redirect("/")
        return redirect("/login?error=1")
    return send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), "login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email    = request.form.get("email", "").strip()
        password = request.form.get("password", "").strip()
        confirm  = request.form.get("confirm", "").strip()
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
    return send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), "register.html")


@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect("/login")


@app.route("/pricing")
@login_required
def pricing_page():
    return send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), "pricing.html")


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
    data   = request.get_json()
    plan   = data.get("plan", "pro")
    billing = data.get("billing", "monthly")
    coupon = data.get("coupon", "")
    upi_id = data.get("upi_id", "")
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
        return jsonify({"error": "Free limit reached (1/day). Upgrade to Pro."}), 403

    if "video" not in request.files:
        return jsonify({"error": "No video file provided"}), 400
    file = request.files["video"]
    if not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    job_id    = str(uuid.uuid4())
    filename  = secure_filename(file.filename)
    save_path = os.path.join(UPLOAD_FOLDER, f"{job_id}_{filename}")
    file.save(save_path)

    music_save_path = None
    if "music" in request.files and request.files["music"].filename:
        music_file      = request.files["music"]
        music_filename  = secure_filename(music_file.filename)
        music_save_path = os.path.join(MUSIC_FOLDER, f"{job_id}_{music_filename}")
        music_file.save(music_save_path)

    fmt = request.form.get("format", "youtube")
    FORMAT_PRESETS = {
        "youtube": {"export_mode": "landscape", "max_dur": None,  "silence_thresh": "-35dB", "silence_min": 0.5},
        "reels":   {"export_mode": "portrait",  "max_dur": 90,    "silence_thresh": "-30dB", "silence_min": 0.3},
        "shorts":  {"export_mode": "portrait",  "max_dur": 60,    "silence_thresh": "-28dB", "silence_min": 0.2},
        "podcast": {"export_mode": "square",    "max_dur": None,  "silence_thresh": "-38dB", "silence_min": 0.8},
    }
    preset = FORMAT_PRESETS.get(fmt, FORMAT_PRESETS["youtube"])

    settings = {
        "cut_silences":    request.form.get("cut_silences",    "true")  == "true",
        "subtitles":       request.form.get("subtitles",       "true")  == "true",
        "color_grade":     request.form.get("color_grade",     "warm"),
        "title":           request.form.get("title",           "My Video"),
        "intro_outro":     request.form.get("intro_outro",     "false") == "true",
        "both_formats":    request.form.get("both_formats",    "false") == "true",
        "music_volume":    float(request.form.get("music_volume", "0.12")),
        "upload_to_drive": request.form.get("upload_to_drive", "false") == "true",
        "quality_enhance": request.form.get("quality_enhance", "true")  == "true",
        "transitions":     request.form.get("transitions",     "true")  == "true",
        "transition_type": request.form.get("transition_type", "fade"),
        "format":          fmt,
        "export_mode":     preset["export_mode"],
        "max_dur":         preset["max_dur"],
        "silence_thresh":  preset["silence_thresh"],
        "silence_min":     preset["silence_min"],
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
    return send_from_directory(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"), "script.html")


@app.route("/generate-script", methods=["POST"])
@login_required
def generate_script_route():
    data             = request.get_json()
    topic            = data.get("topic", "").strip()
    style            = data.get("style", "Educational")
    duration         = data.get("duration", 5)
    audience         = data.get("audience", "General Audience")
    tone             = data.get("tone", "Friendly & Casual")
    cta              = data.get("cta", "Like & Subscribe")
    extra            = data.get("extra", "")
    lang_instruction = data.get("lang_instruction", "")
    if not topic:
        return jsonify({"error": "Topic is required"}), 400
    allowed_s, used_s, limit_s = check_feature_limit(session["user"], "script")
    if not allowed_s:
        return jsonify({
            "error": f"Daily script limit reached ({used_s}/{limit_s}). Upgrade to Pro for unlimited.",
            "limit_reached": True
        }), 403
    consume_feature(session["user"], "script")
    script, error = generate_script(topic, style, duration, audience, tone, cta, extra, lang_instruction)
    if error:
        return jsonify({"error": error}), 500
    return jsonify({"script": script})


# ════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    print(f"Server starting at http://127.0.0.1:{port}")
    print(f"Register:   http://127.0.0.1:{port}/register")
    print(f"Script:     http://127.0.0.1:{port}/script")
    print(f"Thumbnails: http://127.0.0.1:{port}/thumbnail-page")
    app.run(host="0.0.0.0", port=port, debug=False)

# Runs init_db at import time for gunicorn
init_db()