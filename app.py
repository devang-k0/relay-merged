"""
Relay STT — Local, Free, Offline Speech-to-Text Web Tool
=========================================================

A self-contained Flask application that runs OpenAI's Whisper model
ENTIRELY ON YOUR OWN MACHINE (no internet, no API key, no cost).

Upload an audio file in the browser -> Whisper transcribes it locally ->
a .txt file is automatically downloaded to your computer.

--------------------------------------------------------------------------
HOW TO RUN
--------------------------------------------------------------------------
1. pip install -r requirements.txt
2. python app.py
3. Open http://127.0.0.1:5000 in your browser

See the accompanying README / instructions for full setup details
(including installing ffmpeg, which Whisper requires).

--------------------------------------------------------------------------
CHOOSING A WHISPER MODEL
--------------------------------------------------------------------------
This app loads the model ONCE at startup (see MODEL_NAME below) so that
repeated transcriptions don't pay the model-loading cost every time.

Available model sizes (speed vs. accuracy tradeoff), from fastest/least
accurate to slowest/most accurate:

    "tiny"   -> ~39 MB,  fastest, lowest accuracy. Good for quick drafts.
    "base"   -> ~74 MB,  fast, decent accuracy.    DEFAULT — good balance.
    "small"  -> ~244 MB, slower, better accuracy.
    "medium" -> ~769 MB, much slower, high accuracy. Needs a decent GPU/CPU.
    "large"  -> ~1.5 GB, slowest, best accuracy.     Needs a strong GPU.

To switch models, just change MODEL_NAME below, e.g.:
    MODEL_NAME = "tiny"     # fastest, for quick testing
    MODEL_NAME = "small"    # better accuracy, still CPU-friendly
    MODEL_NAME = "medium"   # best practical accuracy if you have a GPU

The first time you use a given model size, Whisper will download its
weights automatically from OpenAI's CDN and cache them locally
(~/.cache/whisper on macOS/Linux, or %USERPROFILE%\\.cache\\whisper on
Windows). After that first download, everything runs fully offline.
"""

import os
import time
import uuid
import traceback
from pathlib import Path
from flask_cors import CORS

from flask import (
    Flask,
    request,
    render_template_string,
    jsonify,
    send_file,
    after_this_request,
)
from werkzeug.utils import secure_filename

import whisper  # openai-whisper (local), NOT the OpenAI API
# ----- BULLETPROOF FFMPEG FIX -----
try:
    import imageio_ffmpeg
    ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
    ffmpeg_dir = os.path.dirname(ffmpeg_path)
    os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ["PATH"]
    print(f"[startup] FFmpeg successfully located at: {ffmpeg_path}")
except ImportError:
    print("[startup] imageio-ffmpeg not installed. Install it with: pip install imageio-ffmpeg")
# ------------------------------------



# =========================================================================
# CONFIGURATION
# =========================================================================

# --- Whisper model size. See module docstring above for tradeoffs. ---
MODEL_NAME = "base"  # <- change to "tiny" / "small" / "medium" / "large" as needed

# --- Folders ---
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / "uploads"
OUTPUT_FOLDER = BASE_DIR / "outputs"

UPLOAD_FOLDER.mkdir(exist_ok=True)
OUTPUT_FOLDER.mkdir(exist_ok=True)

# --- Accepted audio extensions ---
ALLOWED_EXTENSIONS = {"webm", "mp3", "wav", "m4a", "flac"}

# --- Max upload size: 500 MB (adjust as needed for long recordings) ---
MAX_CONTENT_LENGTH = 500 * 1024 * 1024


# =========================================================================
# FLASK APP SETUP
# =========================================================================

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)

# ===== CHANGE 1: Allow Chrome extension to talk to this server =====
CORS(app)  # This removes the "CORS block" error in the browser


# =========================================================================
# LOAD WHISPER MODEL ONCE, AT STARTUP (not per-request!)
# =========================================================================
# Loading the model is the slow/expensive part. We do it a single time
# when the Flask process starts, and reuse the same in-memory model
# object for every transcription request. This is critical for
# performance — re-loading "base" on every request would add several
# seconds (or more, for bigger models) to every single transcription.

print(f"[startup] Loading Whisper model '{MODEL_NAME}' into memory...")
_load_start = time.time()
whisper_model = whisper.load_model(MODEL_NAME)
print(f"[startup] Model '{MODEL_NAME}' loaded in {time.time() - _load_start:.1f}s. Ready for requests.")


# =========================================================================
# HELPERS
# =========================================================================

def allowed_file(filename: str) -> bool:
    """Check that the uploaded file has one of the accepted audio extensions."""
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


def cleanup_file(path: Path) -> None:
    """Best-effort delete of a temp file; never raise if it fails."""
    try:
        if path.exists():
            path.unlink()
    except OSError as e:
        print(f"[cleanup] Warning: could not delete {path}: {e}")


# =========================================================================
# ROUTES
# =========================================================================

@app.route("/")
def index():
    """Serve the single-page UI."""
    return render_template_string(INDEX_HTML, model_name=MODEL_NAME)


@app.route("/transcribe", methods=["POST"])
def transcribe():
    """
    Accept an uploaded audio file, run it through the local Whisper model,
    save the transcript as a .txt file, and return JSON pointing the
    frontend at a download URL.

    The actual file download happens via a separate GET /download/<id>
    request, triggered automatically by the frontend's JavaScript.
    """

    # ---- 1. Validate that a file was actually sent ----
    if "audio_file" not in request.files:
        return jsonify({"error": "No file was uploaded. Please choose an audio file."}), 400

    file = request.files["audio_file"]

    if file.filename == "":
        return jsonify({"error": "No file selected. Please choose an audio file."}), 400

    if not allowed_file(file.filename):
        ext_list = ", ".join(sorted(ALLOWED_EXTENSIONS))
        return jsonify({
            "error": f"Unsupported file type. Please upload one of: {ext_list}"
        }), 400

    # ---- 2. Save the upload to disk with a unique, safe filename ----
    # We use a UUID prefix so concurrent uploads never collide, and
    # secure_filename() strips any path traversal / unsafe characters.
    job_id = uuid.uuid4().hex
    original_name = secure_filename(file.filename)
    safe_ext = original_name.rsplit(".", 1)[1].lower()
    audio_path = UPLOAD_FOLDER / f"{job_id}.{safe_ext}"

    try:
        file.save(str(audio_path))
    except Exception as e:
        return jsonify({"error": f"Failed to save uploaded file: {e}"}), 500

    # ---- 3. Run Whisper transcription on the saved file ----
    try:
        start_time = time.time()

        # fp16=False keeps this safe on CPU-only machines (avoids a
        # "FP16 is not supported on CPU" warning/slowdown). If you have
        # a CUDA GPU and torch with CUDA support installed, Whisper will
        # automatically use it; you can set fp16=True in that case for
        # a speed boost.
        result = whisper_model.transcribe(str(audio_path), fp16=False)

        elapsed = time.time() - start_time
        transcript_text = result.get("text", "").strip()

        if not transcript_text:
            raise ValueError("Whisper returned an empty transcript. The audio may be silent or unreadable.")

    except Exception as e:
        # Make sure we don't leave the uploaded audio file lying around
        # even if transcription fails.
        cleanup_file(audio_path)
        traceback.print_exc()
        return jsonify({"error": f"Transcription failed: {e}"}), 500

    # ---- 4. Delete the temporary audio file (we don't need it anymore) ----
    cleanup_file(audio_path)

    # ---- 5. Write the transcript to a .txt file the user can download ----
    txt_filename = f"{job_id}.txt"
    txt_path = OUTPUT_FOLDER / txt_filename

    try:
        txt_path.write_text(transcript_text, encoding="utf-8")
    except Exception as e:
        return jsonify({"error": f"Failed to write transcript file: {e}"}), 500

    # ---- 6. Respond with metadata the frontend needs ----
    # Build a friendlier download filename based on the original upload,
    # e.g. "interview.mp3" -> "interview_transcript.txt"
    base_name = original_name.rsplit(".", 1)[0]
    download_name = f"{base_name}_transcript.txt"

    return jsonify({
        "success": True,
        "job_id": job_id,
        "download_url": f"/download/{job_id}",
        "download_name": download_name,
        "elapsed_seconds": round(elapsed, 1),
        "preview": transcript_text[:500],  # short preview for the UI
        "char_count": len(transcript_text),
    })


@app.route("/download/<job_id>")
def download(job_id):
    """
    Serve the generated .txt file as a forced download, then delete it
    from the server's outputs/ folder once it has been sent.
    """
    # Sanitize job_id to prevent path traversal — only allow the exact
    # hex format we generate ourselves.
    if not job_id.isalnum():
        return jsonify({"error": "Invalid file reference."}), 400

    txt_path = OUTPUT_FOLDER / f"{job_id}.txt"

    if not txt_path.exists():
        return jsonify({"error": "Transcript not found. It may have already been downloaded or expired."}), 404

    # Get the friendly filename from the query string if provided,
    # otherwise fall back to a generic name.
    download_name = request.args.get("name", "transcript.txt")

    @after_this_request
    def remove_file(response):
        # Clean up the server-side copy after it's been sent to the
        # browser. The user already has their own local copy at this point.
        cleanup_file(txt_path)
        return response

    return send_file(
        str(txt_path),
        mimetype="text/plain",
        as_attachment=True,
        download_name=download_name,
    )


# ===== CHANGE 2: NEW ROUTE FOR CHROME EXTENSION =====
@app.route("/upload", methods=["POST"])
def upload_from_extension():
    """
    This route is specifically for the Relay Chrome extension.
    It expects a file field named 'audio' (not 'audio_file').
    Returns the transcript as plain JSON so the extension can use it directly.
    """
    # Check if the 'audio' file is in the request
    if "audio" not in request.files:
        return jsonify({"error": "No audio file sent. Make sure the field name is 'audio'."}), 400

    file = request.files["audio"]

    if file.filename == "":
        return jsonify({"error": "Empty filename."}), 400

    # Save the uploaded file temporarily with a unique name
    job_id = uuid.uuid4().hex
    # Get the original extension (webm, mp3, etc.)
    original_name = secure_filename(file.filename)
    ext = original_name.rsplit(".", 1)[1].lower() if "." in original_name else "webm"
    audio_path = UPLOAD_FOLDER / f"{job_id}.{ext}"

    try:
        file.save(str(audio_path))
    except Exception as e:
        return jsonify({"error": f"Failed to save file: {e}"}), 500

    # Run Whisper transcription
    try:
        result = whisper_model.transcribe(str(audio_path), fp16=False)
        transcript_text = result.get("text", "").strip()

        if not transcript_text:
            raise ValueError("Whisper returned empty text. The audio might be silent.")

    except Exception as e:
        # Clean up the temp audio file before returning the error
        cleanup_file(audio_path)
        traceback.print_exc()
        return jsonify({"error": f"Transcription failed: {str(e)}"}), 500

    # Delete the temporary audio file (we don't need it anymore)
    cleanup_file(audio_path)

    # Return just the transcript text so the extension can use it immediately
    return jsonify({"transcript": transcript_text})


@app.errorhandler(413)
def file_too_large(e):
    return jsonify({"error": "File is too large. Please upload a smaller audio file."}), 413


# =========================================================================
# EMBEDDED HTML / CSS / JS (single-file app — no separate templates dir)
# =========================================================================

INDEX_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Relay STT — Local Speech-to-Text</title>
<style>

    * {
        margin: 0;
        padding: 0;
        box-sizing: border-box;
        border-radius: 0 !important;
        box-shadow: none !important;
        text-shadow: none !important;
        -webkit-font-smoothing: none;
        font-smooth: never;
    }

    html { min-width: 1024px; }

    :root {
        --bg: #FFFFFF;
        --fg: #000000;
        --fg-dim: #555555;
        --fg-dim2: #2a2a2a;
        --fg-dim3: #333333;
        --invert-bg: #000000;
        --invert-fg: #FFFFFF;
        --red: #FF0000;
        --border: #000000;
    }
    html[data-theme="dark"] {
        --bg: #000000;
        --fg: #FFFFFF;
        --fg-dim: #aaaaaa;
        --fg-dim2: #4a4a4a;
        --fg-dim3: #cccccc;
        --invert-bg: #000000;
        --invert-fg: #FFFFFF;
        --red: #FF0000;
        --border: #FFFFFF;
    }

    body {
        background: var(--bg);
        color: var(--fg);
        font-family: "Courier New", Courier, monospace;
        font-size: 15px;
        line-height: 1.5;
    }

    h1, h2, h3, h4 {
        font-family: "Arial Black", Impact, sans-serif;
        font-weight: 900;
        text-transform: uppercase;
        letter-spacing: -0.01em;
        color: var(--fg);
    }

    a { color: inherit; text-decoration: none; }

    /* ---- theme toggle: fixed box, bottom-right ---- */
    #theme-toggle {
        position: fixed;
        bottom: 24px;
        right: 24px;
        width: 48px;
        height: 48px;
        border: 2px solid var(--red);
        background: var(--bg);
        color: var(--red);
        font-size: 20px;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 999;
    }
    #theme-toggle:hover { background: var(--red); color: var(--bg); }

    /* ============================================================
       NAV — full width black bar, red dividers, hard invert
    ============================================================ */
    nav.topbar {
        background: #000000;
        display: grid;
        grid-template-columns: 2fr 1fr 3fr;
        align-items: stretch;
        border-bottom: 2px solid var(--border);
    }

    .nav-mark {
        padding: 22px 28px;
        font-family: "Arial Black", Impact, sans-serif;
        font-weight: 900;
        font-size: 20px;
        color: #FFFFFF;
        letter-spacing: -0.02em;
        border-right: 2px solid var(--red);
    }
    .nav-mark span { color: var(--red); }

    .nav-meta {
        padding: 22px 28px;
        color: #FFFFFF;
        font-size: 12px;
        display: flex;
        align-items: center;
        border-right: 2px solid var(--red);
        letter-spacing: 0.05em;
    }

    .nav-links { display: flex; justify-content: flex-end; }

    .nav-links a {
        padding: 22px 26px;
        color: #FFFFFF;
        font-size: 13px;
        letter-spacing: 0.08em;
        border-left: 2px solid #4a4a4a;
    }
    .nav-links a:hover { background: var(--red); color: #000000; }

    /* ============================================================
       HERO — asymmetric grid: upload column / model info column
    ============================================================ */
    .hero {
        display: grid;
        grid-template-columns: 3fr 2fr;
        border-bottom: 2px solid var(--border);
        min-height: 600px;
    }

    .hero-left {
        padding: 60px 50px 50px 50px;
        border-right: 2px solid var(--border);
    }

    .hero-left h1 { font-size: 52px; line-height: 0.98; margin-bottom: 0; }

    .hero-rule {
        width: 100%;
        height: 2px;
        background: var(--red);
        margin: 24px 0 26px 0;
    }

    .hero-left p.lede {
        font-size: 13.5px;
        max-width: 480px;
        margin-bottom: 30px;
    }

    /* ---- drop zone, brutalist-flat ---- */
    #drop-zone {
        border: 2px solid var(--border);
        padding: 50px 20px;
        text-align: center;
        cursor: pointer;
        margin-bottom: 0;
    }
    #drop-zone:hover,
    #drop-zone.dragover {
        background: var(--fg);
        color: var(--bg);
    }
    #drop-zone:hover .sub-text,
    #drop-zone.dragover .sub-text {
        color: var(--fg-dim3);
    }

    #drop-zone .icon {
        font-family: "Arial Black", Impact, sans-serif;
        font-size: 13px;
        letter-spacing: 0.1em;
        color: var(--red);
        margin-bottom: 14px;
    }

    #drop-zone .main-text {
        font-family: "Arial Black", Impact, sans-serif;
        font-size: 17px;
        text-transform: uppercase;
        margin-bottom: 8px;
    }

    #drop-zone .sub-text {
        font-size: 12px;
        letter-spacing: 0.04em;
        color: var(--fg-dim);
    }

    #file-input { display: none; }

    #file-name {
        font-size: 12.5px;
        color: var(--fg);
        border: 2px solid var(--border);
        border-top: none;
        padding: 12px 16px;
        min-height: 18px;
        word-break: break-all;
        display: none;
    }
    #file-name.show { display: block; }

    button#transcribe-btn {
        width: 100%;
        border: 2px solid var(--red);
        background: #000000;
        color: var(--red);
        font-family: "Courier New", Courier, monospace;
        font-size: 14px;
        letter-spacing: 0.1em;
        padding: 16px;
        cursor: pointer;
        margin-top: 18px;
    }
    button#transcribe-btn:hover:not(:disabled) {
        background: var(--red);
        color: #000000;
    }
    button#transcribe-btn:disabled {
        background: var(--bg);
        color: var(--fg-dim);
        border-color: var(--fg-dim);
        cursor: not-allowed;
    }

    /* ---- right column: model card + status log ---- */
    .hero-right {
        display: flex;
        flex-direction: column;
    }

    .model-card {
        padding: 24px 28px;
        border-bottom: 2px solid var(--border);
        background: #000000;
        color: #FFFFFF;
    }
    .model-card .label-row {
        font-size: 11px;
        letter-spacing: 0.1em;
        margin-bottom: 14px;
        color: #FFFFFF;
    }
    .model-row {
        display: grid;
        grid-template-columns: 1fr auto;
        padding: 8px 0;
        border-bottom: 1px solid var(--fg-dim2);
        font-size: 12.5px;
    }
    .model-row:last-child { border-bottom: none; }
    .model-row .val { color: var(--red); text-align: right; }

    .status-log {
        flex: 1;
        padding: 24px 28px;
        display: flex;
        flex-direction: column;
    }
    .status-log .label-row {
        font-size: 11px;
        letter-spacing: 0.1em;
        margin-bottom: 16px;
        border-bottom: 2px solid var(--border);
        padding-bottom: 12px;
    }

    #status-box {
        font-size: 13px;
        line-height: 1.6;
        display: none;
    }
    #status-box.show { display: block; }

    #status-box .state-line {
        display: flex;
        align-items: baseline;
        gap: 10px;
        margin-bottom: 10px;
    }
    #status-box .marker { color: var(--red); }
    #status-box.error .marker { color: var(--red); }
    #status-box.success .marker { color: var(--fg); }

    .blink {
        display: inline-block;
        animation: blink 1s steps(1) infinite;
    }
    @keyframes blink { 50% { opacity: 0; } }

    .preview {
        margin-top: 14px;
        padding-top: 14px;
        border-top: 2px solid var(--border);
        color: var(--fg-dim3);
        font-size: 12px;
        white-space: pre-wrap;
        max-height: 160px;
        overflow-y: auto;
    }

    .idle-msg {
        font-size: 12.5px;
        color: var(--fg-dim);
    }

    /* ============================================================
       FOOTER — minimal, left-aligned, red top border
    ============================================================ */
    footer {
        border-top: 2px solid var(--red);
        padding: 20px 50px;
        font-size: 12px;
        color: var(--fg);
    }

</style>
</head>
<body>

    <!-- ============================================================
         NAV
    ============================================================ -->
    <nav class="topbar">
        <div class="nav-mark">RE<span>/</span>AY  STT</div>
        <div class="nav-meta">LOCAL &middot; OFFLINE &middot; NO API</div>
        <div class="nav-links">
            <a href="#" onclick="return false;">DOCS</a>
            <a href="#" onclick="return false;">ABOUT</a>
        </div>
    </nav>

    <!-- ============================================================
         HERO — upload / transcribe / status
    ============================================================ -->
    <section class="hero">
        <div class="hero-left">
            <h1>SPEECH<br>TO TEXT<span style="color:#FF0000;">.</span></h1>
            <div class="hero-rule"></div>
            <p class="lede">Whisper runs on this machine. Audio never leaves it. Drop a file, get a transcript, no server-side trace left behind.</p>

            <div id="drop-zone">
                <div class="icon">[ AUDIO INPUT ]</div>
                <div class="main-text">CLICK OR DROP FILE</div>
                <div class="sub-text">.mp3 &middot; .wav &middot; .m4a &middot; .flac &middot; .webm</div>
            </div>
            <input type="file" id="file-input" accept=".webm,.mp3,.wav,.m4a,.flac">
            <div id="file-name"></div>

            <button id="transcribe-btn" disabled>TRANSCRIBE &rarr;</button>
        </div>

        <div class="hero-right">
            <div class="model-card">
                <div class="label-row">ENGINE</div>
                <div class="model-row"><div>WHISPER MODEL</div><div class="val">{{ model_name }}</div></div>
                <div class="model-row"><div>EXECUTION</div><div class="val">LOCAL</div></div>
                <div class="model-row"><div>NETWORK</div><div class="val">NONE REQUIRED</div></div>
                <div class="model-row"><div>OUTPUT</div><div class="val">.TXT FILE</div></div>
            </div>
            <div class="status-log">
                <div class="label-row">STATUS LOG</div>
                <div class="idle-msg" id="idle-msg">[ AWAITING FILE — NOTHING RUNNING ]</div>
                <div id="status-box"></div>
            </div>
        </div>
    </section>

    <!-- ============================================================
         FOOTER
    ============================================================ -->
    <footer>
        Relay STT &middot; runs entirely on your computer &middot; no internet required after model download
    </footer>

    <button id="theme-toggle" type="button">&#9789;</button>

<script>
    const themeToggle = document.getElementById('theme-toggle');
    const htmlEl = document.documentElement;

    function applyTheme(theme) {
        htmlEl.setAttribute('data-theme', theme);
        themeToggle.innerHTML = theme === 'dark' ? '&#9728;' : '&#9789;';
        localStorage.setItem('theme', theme);
    }

    applyTheme(localStorage.getItem('theme') || 'light');

    themeToggle.addEventListener('click', () => {
        const next = htmlEl.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
        applyTheme(next);
    });

    const dropZone = document.getElementById('drop-zone');
    const fileInput = document.getElementById('file-input');
    const fileNameEl = document.getElementById('file-name');
    const transcribeBtn = document.getElementById('transcribe-btn');
    const statusBox = document.getElementById('status-box');
    const idleMsg = document.getElementById('idle-msg');

    let selectedFile = null;

    // ---- Click-to-upload ----
    dropZone.addEventListener('click', () => fileInput.click());

    fileInput.addEventListener('change', () => {
        if (fileInput.files.length > 0) {
            handleFileSelect(fileInput.files[0]);
        }
    });

    // ---- Drag-and-drop ----
    ['dragenter', 'dragover'].forEach(evt => {
        dropZone.addEventListener(evt, (e) => {
            e.preventDefault();
            dropZone.classList.add('dragover');
        });
    });

    ['dragleave', 'drop'].forEach(evt => {
        dropZone.addEventListener(evt, (e) => {
            e.preventDefault();
            dropZone.classList.remove('dragover');
        });
    });

    dropZone.addEventListener('drop', (e) => {
        const files = e.dataTransfer.files;
        if (files.length > 0) {
            handleFileSelect(files[0]);
        }
    });

    function handleFileSelect(file) {
        const allowedExt = ['webm', 'mp3', 'wav', 'm4a', 'flac'];
        const ext = file.name.split('.').pop().toLowerCase();

        if (!allowedExt.includes(ext)) {
            showStatus('error', `NOT SUPPORTED — use: ${allowedExt.join(', ').toUpperCase()}`);
            transcribeBtn.disabled = true;
            selectedFile = null;
            fileNameEl.classList.remove('show');
            return;
        }

        selectedFile = file;
        fileNameEl.textContent = `SELECTED: ${file.name.toUpperCase()} (${(file.size / 1024 / 1024).toFixed(1)} MB)`;
        fileNameEl.classList.add('show');
        transcribeBtn.disabled = false;
        hideStatus();
    }

    function showStatus(type, html) {
        idleMsg.style.display = 'none';
        statusBox.className = `show ${type}`;
        statusBox.innerHTML = html;
    }

    function hideStatus() {
        statusBox.className = '';
        statusBox.innerHTML = '';
        idleMsg.style.display = 'block';
    }

    transcribeBtn.addEventListener('click', async () => {
        if (!selectedFile) return;

        transcribeBtn.disabled = true;
        dropZone.style.pointerEvents = 'none';

        showStatus('info', '<div class="state-line"><span class="marker">&gt;</span> UPLOADING FILE<span class="blink">_</span></div>');

        const formData = new FormData();
        formData.append('audio_file', selectedFile);

        try {
            // Switch message after a brief moment, since upload is usually
            // near-instant for local use but transcription can take a while.
            setTimeout(() => {
                if (statusBox.classList.contains('info') && statusBox.innerHTML.includes('UPLOADING')) {
                    showStatus('info', '<div class="state-line"><span class="marker">&gt;</span> PROCESSING AUDIO — MAY TAKE SEVERAL MINUTES<span class="blink">_</span></div>');
                }
            }, 800);

            const response = await fetch('/transcribe', {
                method: 'POST',
                body: formData
            });

            const data = await response.json();

            if (!response.ok || data.error) {
                throw new Error(data.error || 'TRANSCRIPTION FAILED FOR AN UNKNOWN REASON.');
            }

            showStatus('success',
                `<div class="state-line"><span class="marker">[OK]</span> DONE — ${data.elapsed_seconds}S &middot; ${data.char_count} CHARS</div>` +
                `<div class="preview">${escapeHtml(data.preview)}${data.char_count > 500 ? '…' : ''}</div>`
            );

            // Trigger the forced .txt download automatically.
            triggerDownload(data.download_url, data.download_name);

        } catch (err) {
            showStatus('error', `<div class="state-line"><span class="marker">[ERR]</span> ${escapeHtml(err.message.toUpperCase())}</div>`);
        } finally {
            transcribeBtn.disabled = false;
            dropZone.style.pointerEvents = 'auto';
        }
    });

    function triggerDownload(url, filename) {
        const a = document.createElement('a');
        a.href = `${url}?name=${encodeURIComponent(filename)}`;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
    }

    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }
</script>

</body>
</html>
"""


# =========================================================================
# ENTRY POINT
# =========================================================================

if __name__ == "__main__":
    # debug=False is intentional: Flask's debug reloader would otherwise
    # spawn a second process and load the (large) Whisper model TWICE.
    # If you need to actively develop the HTML/JS and want auto-reload,
    # temporarily set use_reloader=True but be aware of double model loads.
    app.run(host="127.0.0.1", port=5000, debug=False)