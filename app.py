"""
Video Downloader — versi Web (Flask) — "Clipgrab"

Mendukung: TikTok, Instagram (Reels/post publik), YouTube.
(Semuanya lewat yt-dlp, yang sudah punya extractor bawaan untuk
ketiga platform ini — tidak perlu library tambahan.)

Fitur:
  - Tempel banyak link sekaligus (boleh campur platform)
  - Pilih format: Video (MP4) atau Audio saja (MP3)
  - Opsi "tanpa watermark" (khusus TikTok, best-effort)
  - Progress bar per link (live, lewat polling AJAX)
  - Preview thumbnail, judul, durasi, platform, dan nama kreator per item
  - Link download langsung dari browser setelah selesai

Instalasi (sekali saja):
    pip install flask yt-dlp

Untuk mode Audio (MP3) dibutuhkan FFmpeg terpasang di sistem.

Jalankan:
    python app.py

Lalu buka di browser:
    http://localhost:5000

Batasan: konten privat (akun Instagram privat, video unlisted yang
butuh login, dll) tidak bisa diunduh tanpa autentikasi tambahan
(cookies) — tidak diimplementasikan di versi ini.

Hanya unduh video yang memang berhak Anda unduh. Hormati hak
cipta pembuat konten.
"""

import os
import uuid
import shutil
import threading
from flask import (
    Flask, request, jsonify, send_from_directory, render_template_string
)

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_ROOT = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_ROOT, exist_ok=True)

app = Flask(__name__)

JOBS = {}
JOBS_LOCK = threading.Lock()


# ----------------------------------------------------------------------
# LOGIKA DOWNLOAD
# ----------------------------------------------------------------------

def pilih_format_tanpa_watermark(formats):
    """Cari format tanpa watermark. Hanya relevan untuk TikTok —
    dipanggil dari jalankan_job() hanya kalau extractor-nya TikTok."""

    kandidat = []

    for f in formats or []:
        note = (f.get("format_note") or "").lower()
        fid = (f.get("format_id") or "").lower()
        gabungan = f"{note} {fid}"

        if "watermark" in gabungan:
            continue

        if "download" in gabungan or "nowm" in gabungan or "no_wm" in gabungan:
            kandidat.append(f)

    if kandidat:
        kandidat.sort(key=lambda f: f.get("height") or 0, reverse=True)
        return kandidat[0].get("format_id")

    return None


def jalankan_job(job_id, links, mode, no_watermark):

    for item in JOBS[job_id]["items"]:
        url = item["url"]

        def buat_hook(item_ref):
            def hook(d):
                if d.get("status") == "downloading":
                    total_bytes = d.get("total_bytes") or d.get("total_bytes_estimate")
                    downloaded = d.get("downloaded_bytes", 0)

                    if total_bytes:
                        with JOBS_LOCK:
                            item_ref["percent"] = round(downloaded / total_bytes * 100, 1)
                            item_ref["status"] = "downloading"

                elif d.get("status") == "finished":
                    with JOBS_LOCK:
                        item_ref["percent"] = 100
                        item_ref["status"] = "processing"

            return hook

        opsi = {
            "outtmpl": os.path.join(DOWNLOAD_ROOT, "%(title).60s [%(id)s].%(ext)s"),
            "progress_hooks": [buat_hook(item)],
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
        }

        if mode == "audio":
            opsi["format"] = "bestaudio/best"
            opsi["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        else:
            opsi["format"] = "mp4/best"

        try:
            with JOBS_LOCK:
                item["status"] = "downloading"

            info_awal = None
            try:
                with yt_dlp.YoutubeDL({"quiet": True, "simulate": True, "no_warnings": True}) as ydl_probe:
                    info_awal = ydl_probe.extract_info(url, download=False)

                with JOBS_LOCK:
                    item["thumbnail"] = info_awal.get("thumbnail")
                    item["duration"] = info_awal.get("duration")
                    item["uploader"] = info_awal.get("uploader") or info_awal.get("uploader_id")
                    item["platform"] = (info_awal.get("extractor_key") or "").replace(":", " ")
                    judul = info_awal.get("title")
                    if judul:
                        item["title"] = judul
            except Exception:
                pass

            platform_key = (info_awal.get("extractor_key") or "").lower() if info_awal else ""
            is_tiktok = "tiktok" in platform_key

            if mode == "video" and no_watermark and info_awal and is_tiktok:
                fmt_id = pilih_format_tanpa_watermark(info_awal.get("formats"))
                if fmt_id:
                    opsi["format"] = fmt_id

            with yt_dlp.YoutubeDL(opsi) as ydl:
                info = ydl.extract_info(url, download=True)
                nama_file = ydl.prepare_filename(info)

                if mode == "audio":
                    nama_file = os.path.splitext(nama_file)[0] + ".mp3"

            with JOBS_LOCK:
                item["status"] = "done"
                item["percent"] = 100
                item["filename"] = os.path.basename(nama_file)

        except Exception as e:
            with JOBS_LOCK:
                item["status"] = "error"
                item["error"] = str(e)

    with JOBS_LOCK:
        JOBS[job_id]["selesai"] = True


# ----------------------------------------------------------------------
# ROUTES
# ----------------------------------------------------------------------

@app.route("/")
def index():
    return render_template_string(HTML_PAGE)


@app.route("/start", methods=["POST"])
def start():

    if yt_dlp is None:
        return jsonify({"error": "yt-dlp belum terinstall di server. Jalankan: pip install yt-dlp"}), 500

    data = request.get_json(force=True)
    links_raw = data.get("links", "")
    mode = data.get("mode", "video")
    no_watermark = bool(data.get("no_watermark", True))

    links = [l.strip() for l in links_raw.splitlines() if l.strip()]

    if not links:
        return jsonify({"error": "Tidak ada link yang dikirim."}), 400

    if mode == "audio" and shutil.which("ffmpeg") is None:
        return jsonify({
            "error": "Mode Audio (MP3) butuh FFmpeg terpasang di server, tapi tidak terdeteksi."
        }), 400

    job_id = uuid.uuid4().hex[:12]

    with JOBS_LOCK:
        JOBS[job_id] = {
            "selesai": False,
            "items": [
                {
                    "url": u, "status": "menunggu", "percent": 0, "filename": None,
                    "error": None, "thumbnail": None, "title": None,
                    "duration": None, "uploader": None, "platform": None,
                }
                for u in links
            ],
        }

    thread = threading.Thread(
        target=jalankan_job, args=(job_id, links, mode, no_watermark), daemon=True
    )
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)

        if not job:
            return jsonify({"error": "Job tidak ditemukan."}), 404

        return jsonify(job)


@app.route("/file/<path:filename>")
def file(filename):
    return send_from_directory(DOWNLOAD_ROOT, filename, as_attachment=True)


# ----------------------------------------------------------------------
# FRONTEND
# ----------------------------------------------------------------------

HTML_PAGE = """
<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clipgrab — Video Downloader</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>⬇️</text></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,400;12..96,600;12..96,800&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>

:root{
    --bg:#08080d;
    --bg-soft:#0e0e16;
    --panel:#12121c;
    --panel-2:#161622;
    --line:rgba(255,255,255,.08);
    --line-2:rgba(255,255,255,.14);
    --cyan:#25f4ee;
    --pink:#ff2d6a;
    --ink:#f2f2f5;
    --muted:#8b8b9b;
    --dim:#54546a;
    --ok:#2fe6a6;
    --warn:#ffb84d;
}

*{ box-sizing:border-box; }

body{
    margin:0;
    background:var(--bg);
    color:var(--ink);
    font-family:'Inter',sans-serif;
    min-height:100vh;
    padding:48px 18px 80px;
    position:relative;
    overflow-x:hidden;
}

/* ambient duotone glow */
body::before, body::after{
    content:"";
    position:fixed;
    width:520px;
    height:520px;
    border-radius:50%;
    filter:blur(120px);
    opacity:.16;
    z-index:0;
    pointer-events:none;
}
body::before{ background:var(--cyan); top:-160px; left:-160px; }
body::after{ background:var(--pink); bottom:-180px; right:-160px; }

/* fine grain texture for a bit of premium tactility */
.grain{
    position:fixed;
    inset:0;
    z-index:0;
    pointer-events:none;
    opacity:.035;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='120'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E");
}

.wrap{
    max-width:640px;
    margin:0 auto;
    position:relative;
    z-index:1;
}

.eyebrow{
    display:inline-flex;
    align-items:center;
    gap:8px;
    font-family:'JetBrains Mono',monospace;
    font-size:11px;
    letter-spacing:.14em;
    text-transform:uppercase;
    color:var(--muted);
    margin-bottom:14px;
}

.eyebrow::before{
    content:"";
    width:6px;
    height:6px;
    border-radius:50%;
    background:var(--ok);
    box-shadow:0 0 0 3px rgba(47,230,166,.15);
    animation:pulse-dot 2.4s ease-in-out infinite;
}

@keyframes pulse-dot{
    0%,100%{ box-shadow:0 0 0 3px rgba(47,230,166,.15); }
    50%{ box-shadow:0 0 0 6px rgba(47,230,166,.06); }
}

h1{
    font-family:'Bricolage Grotesque',sans-serif;
    font-weight:800;
    font-size:40px;
    line-height:1.05;
    letter-spacing:-.01em;
    margin:0 0 6px;
    color:var(--ink);
    text-shadow:2px 0 var(--cyan), -2px 0 var(--pink);
}

.subtitle{
    color:var(--muted);
    font-size:14.5px;
    margin:0 0 18px;
    max-width:46ch;
}

/* quiet feature strip */
.feature-strip{
    display:flex;
    flex-wrap:wrap;
    gap:8px;
    margin-bottom:28px;
}

.feature-strip span{
    font-family:'JetBrains Mono',monospace;
    font-size:11px;
    color:var(--muted);
    border:1px solid var(--line);
    border-radius:999px;
    padding:6px 12px;
    display:inline-flex;
    align-items:center;
    gap:6px;
    background:rgba(255,255,255,.02);
}

.card{
    background:var(--panel);
    border:1px solid var(--line);
    border-radius:20px;
    padding:26px;
    margin-bottom:20px;
    box-shadow:0 20px 50px -20px rgba(0,0,0,.6);
}

.field-head{
    display:flex;
    justify-content:space-between;
    align-items:center;
    margin-bottom:10px;
}

label.field-label{
    font-family:'JetBrains Mono',monospace;
    font-size:11px;
    letter-spacing:.08em;
    text-transform:uppercase;
    color:var(--muted);
}

.mini-actions{
    display:flex;
    gap:6px;
}

.mini-btn{
    font-family:'JetBrains Mono',monospace;
    font-size:10.5px;
    color:var(--muted);
    background:transparent;
    border:1px solid var(--line);
    border-radius:8px;
    padding:5px 9px;
    cursor:pointer;
    transition:.15s;
}

.mini-btn:hover{
    color:var(--ink);
    border-color:var(--line-2);
    background:rgba(255,255,255,.03);
}

textarea{
    width:100%;
    min-height:120px;
    padding:14px 16px;
    background:var(--bg-soft);
    border:1.5px solid var(--line);
    border-radius:12px;
    color:var(--ink);
    font-family:'JetBrains Mono',monospace;
    font-size:13px;
    line-height:1.6;
    resize:vertical;
}

textarea::placeholder{ color:#54546a; }

textarea:focus{
    outline:none;
    border-color:var(--cyan);
    box-shadow:0 0 0 3px rgba(37,244,238,.12);
}

.opsi-row{
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:16px;
    margin-top:20px;
    flex-wrap:wrap;
}

.segment{
    display:inline-flex;
    background:var(--bg-soft);
    border:1px solid var(--line);
    border-radius:999px;
    padding:4px;
    gap:4px;
}

.segment input{ display:none; }

.segment label{
    display:flex;
    align-items:center;
    gap:6px;
    padding:8px 16px;
    border-radius:999px;
    font-size:13px;
    font-weight:500;
    color:var(--muted);
    cursor:pointer;
    transition:.15s;
    user-select:none;
}

.segment input:checked + label{
    background:linear-gradient(135deg, var(--cyan), var(--pink));
    color:#08080d;
    font-weight:600;
}

.switch-row{
    display:flex;
    align-items:center;
    gap:10px;
    font-size:13.5px;
    color:var(--ink);
    cursor:pointer;
    user-select:none;
}

.switch{
    width:38px;
    height:22px;
    border-radius:999px;
    background:var(--bg-soft);
    border:1px solid var(--line);
    position:relative;
    flex-shrink:0;
    transition:.15s;
}

.switch::after{
    content:"";
    position:absolute;
    top:2px; left:2px;
    width:16px; height:16px;
    border-radius:50%;
    background:var(--muted);
    transition:.15s;
}

.switch-row input{ display:none; }

.switch-row input:checked ~ .switch{
    background:rgba(37,244,238,.18);
    border-color:var(--cyan);
}

.switch-row input:checked ~ .switch::after{
    left:18px;
    background:var(--cyan);
}

button.mulai{
    width:100%;
    margin-top:20px;
    padding:15px;
    border:none;
    border-radius:14px;
    background:linear-gradient(135deg, var(--cyan), var(--pink));
    background-size:200% 200%;
    color:#08080d;
    font-weight:700;
    font-size:14.5px;
    letter-spacing:.01em;
    cursor:pointer;
    transition:transform .15s, box-shadow .15s, background-position .4s;
}

button.mulai:hover{
    transform:translateY(-2px);
    box-shadow:0 12px 28px -10px rgba(37,244,238,.35);
    background-position:100% 50%;
}

button.mulai:active{ transform:translateY(0); }

button.mulai:disabled{
    opacity:.5;
    cursor:not-allowed;
    transform:none;
    box-shadow:none;
}

.card-title{
    display:flex;
    justify-content:space-between;
    align-items:center;
    font-family:'JetBrains Mono',monospace;
    font-size:11px;
    letter-spacing:.08em;
    text-transform:uppercase;
    color:var(--muted);
    margin:0 0 16px;
}

.card-title .count{
    color:var(--ink);
    background:var(--panel-2);
    border:1px solid var(--line);
    border-radius:999px;
    padding:2px 9px;
    font-size:10.5px;
}

.item{
    display:flex;
    gap:14px;
    border:1px solid var(--line);
    border-radius:14px;
    padding:14px;
    margin-bottom:10px;
    background:var(--bg-soft);
    opacity:0;
    transform:translateY(10px);
    animation:item-in .4s ease forwards;
}

@keyframes item-in{
    to{ opacity:1; transform:translateY(0); }
}

.item .thumb-wrap{
    width:42px;
    height:62px;
    border-radius:9px;
    flex-shrink:0;
    background:linear-gradient(160deg, var(--cyan), var(--pink));
    opacity:.85;
    overflow:hidden;
    position:relative;
}

.item .thumb-wrap img{
    width:100%;
    height:100%;
    object-fit:cover;
    display:block;
}

.item .thumb-wrap .dur{
    position:absolute;
    bottom:2px;
    right:2px;
    background:rgba(0,0,0,.65);
    color:#fff;
    font-family:'JetBrains Mono',monospace;
    font-size:8.5px;
    padding:1px 4px;
    border-radius:4px;
    line-height:1.4;
}

.item.downloading .thumb-wrap{ animation:shimmer 1.6s linear infinite; }

@keyframes shimmer{
    0%{ filter:brightness(1); }
    50%{ filter:brightness(1.35); }
    100%{ filter:brightness(1); }
}

.item.error .thumb-wrap{ background:#402028; }
.item.done .thumb-wrap{ background:linear-gradient(160deg, var(--ok), var(--cyan)); }

.item .body{ flex:1; min-width:0; }

.item .judul{
    font-size:13px;
    font-weight:500;
    color:var(--ink);
    white-space:nowrap;
    overflow:hidden;
    text-overflow:ellipsis;
    margin-bottom:2px;
}

.item .meta-row{
    display:flex;
    gap:8px;
    align-items:center;
    margin-bottom:8px;
    flex-wrap:wrap;
}

.item .uploader{
    font-size:11px;
    color:var(--muted);
}

.item .platform-badge{
    font-family:'JetBrains Mono',monospace;
    font-size:9.5px;
    letter-spacing:.04em;
    text-transform:uppercase;
    color:var(--cyan);
    border:1px solid rgba(37,244,238,.25);
    border-radius:6px;
    padding:2px 6px;
}

.item .url{
    font-family:'JetBrains Mono',monospace;
    font-size:10.5px;
    color:var(--dim);
    word-break:break-all;
}

.progress-bg{
    background:rgba(255,255,255,.06);
    border-radius:20px;
    height:6px;
    overflow:hidden;
    margin-bottom:8px;
}

.progress-fill{
    background:linear-gradient(90deg, var(--cyan), var(--pink));
    height:100%;
    width:0%;
    transition:width .2s;
}

.item.error .progress-fill{ background:#ff5470; }
.item.done .progress-fill{ background:var(--ok); }

.status-line{
    display:flex;
    justify-content:space-between;
    align-items:center;
    font-size:12.5px;
    color:var(--muted);
}

.status-line .label{
    font-family:'JetBrains Mono',monospace;
    color:#5c5c72;
}

.status-line a{
    display:inline-flex;
    align-items:center;
    gap:5px;
    color:var(--cyan);
    font-weight:600;
    text-decoration:none;
    font-size:12.5px;
    transition:.15s;
}

.status-line a:hover{ text-decoration:underline; gap:8px; }

.pct{
    font-family:'JetBrains Mono',monospace;
    font-weight:500;
    color:var(--ink);
}

.err-text{
    color:#ff6b87;
    font-size:11.5px;
    margin-top:6px;
    font-family:'JetBrains Mono',monospace;
}

#hasil{ display:none; }

.empty-state{
    text-align:center;
    padding:20px 10px 6px;
    color:var(--dim);
    font-size:12.5px;
}

.foot-note{
    text-align:center;
    color:var(--dim);
    font-size:12px;
    margin-top:8px;
}

@media (prefers-reduced-motion: reduce){
    button.mulai, .switch, .switch::after, .segment label, .item, .eyebrow::before{
        transition:none; animation:none;
    }
    .item{ opacity:1; transform:none; }
}

</style>
</head>
<body>

<div class="grain"></div>

<div class="wrap">

<div class="eyebrow">Server lokal aktif</div>
<h1>Clipgrab</h1>
<p class="subtitle">Tempel link TikTok, Instagram, atau YouTube — satu per baris, pilih format, unduh langsung dari browser.</p>

<div class="feature-strip">
    <span>🎵 TikTok</span>
    <span>📸 Instagram</span>
    <span>▶️ YouTube</span>
    <span>🎧 Ekstrak audio MP3</span>
    <span>🔒 Diproses lokal</span>
</div>

<div class="card">

    <div class="field-head">
        <label class="field-label" for="links">Link TikTok</label>
        <div class="mini-actions">
            <button type="button" class="mini-btn" id="btnPaste">📋 Tempel</button>
            <button type="button" class="mini-btn" id="btnClear">✕ Bersihkan</button>
        </div>
    </div>

    <textarea id="links" placeholder="https://www.tiktok.com/@user/video/xxxxxxx
https://www.instagram.com/reel/xxxxxxx
https://www.youtube.com/watch?v=xxxxxxx"></textarea>

    <div class="opsi-row">
        <div class="segment">
            <input type="radio" name="mode" id="mode-video" value="video" checked>
            <label for="mode-video">🎬 Video (MP4)</label>
            <input type="radio" name="mode" id="mode-audio" value="audio">
            <label for="mode-audio">🎵 Audio (MP3)</label>
        </div>

        <label class="switch-row" for="nowm">
            Tanpa watermark (khusus TikTok)
            <input type="checkbox" id="nowm" checked>
            <span class="switch"></span>
        </label>
    </div>

    <button class="mulai" id="btnMulai">⬇  Mulai Unduh</button>

</div>

<div class="card" id="hasil">
    <p class="card-title">Antrian unduhan <span class="count" id="jumlahItem">0</span></p>
    <div id="daftarItem"></div>
</div>

<p class="foot-note">Opsi tanpa watermark bersifat best-effort dan hanya berlaku untuk TikTok. Konten privat/terkunci login (mis. akun Instagram privat) tidak bisa diunduh. Hanya unduh video yang memang berhak Anda unduh.</p>

</div>

<script>

const btnMulai = document.getElementById("btnMulai");
const btnPaste = document.getElementById("btnPaste");
const btnClear = document.getElementById("btnClear");
const linksBox = document.getElementById("links");
const hasilCard = document.getElementById("hasil");
const daftarItem = document.getElementById("daftarItem");
const jumlahItem = document.getElementById("jumlahItem");

let pollTimer = null;

btnPaste.addEventListener("click", async () => {
    try {
        const teks = await navigator.clipboard.readText();
        if(teks){
            linksBox.value = linksBox.value.trim()
                ? linksBox.value.trim() + "\\n" + teks.trim()
                : teks.trim();
        }
    } catch (err) {
        alert("Tidak bisa mengakses clipboard. Tempel manual saja (Ctrl+V) di kotak link.");
    }
});

btnClear.addEventListener("click", () => {
    linksBox.value = "";
    linksBox.focus();
});

btnMulai.addEventListener("click", async () => {

    const links = linksBox.value.trim();
    if(!links){
        alert("Tempel minimal satu link dulu.");
        return;
    }

    const mode = document.querySelector('input[name="mode"]:checked').value;
    const noWatermark = document.getElementById("nowm").checked;

    btnMulai.disabled = true;
    btnMulai.textContent = "Memulai...";
    daftarItem.innerHTML = "";
    hasilCard.style.display = "block";

    try {

        const res = await fetch("/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ links, mode, no_watermark: noWatermark })
        });

        const data = await res.json();

        if(!res.ok){
            alert(data.error || "Gagal memulai unduhan.");
            resetTombol();
            return;
        }

        pollStatus(data.job_id);

    } catch (err) {
        alert("Terjadi kesalahan: " + err);
        resetTombol();
    }

});

function resetTombol(){
    btnMulai.disabled = false;
    btnMulai.textContent = "⬇  Mulai Unduh";
}

function pollStatus(jobId){

    if(pollTimer) clearInterval(pollTimer);

    pollTimer = setInterval(async () => {

        const res = await fetch("/status/" + jobId);
        const job = await res.json();

        if(!res.ok){
            clearInterval(pollTimer);
            alert(job.error || "Job tidak ditemukan.");
            resetTombol();
            return;
        }

        renderItems(jobId, job.items);

        if(job.selesai){
            clearInterval(pollTimer);
            resetTombol();
        }

    }, 800);

}

function formatDurasi(detik){
    if(!detik && detik !== 0) return "";
    const d = Math.round(detik);
    const m = Math.floor(d / 60);
    const s = d % 60;
    return m + ":" + String(s).padStart(2, "0");
}

function renderItems(jobId, items){

    jumlahItem.textContent = items.length;

    daftarItem.innerHTML = items.map((item, idx) => {

        const cls = item.status === "error" ? "error" : (item.status === "done" ? "done" : (item.status === "downloading" ? "downloading" : ""));
        const persen = item.percent || 0;

        let kanan = "";

        if(item.status === "done" && item.filename){
            kanan = `<a href="/file/${encodeURIComponent(item.filename)}" target="_blank">⬇ Download</a>`;
        }else if(item.status === "error"){
            kanan = `<span class="pct">Gagal</span>`;
        }else{
            kanan = `<span class="pct">${persen}%</span>`;
        }

        const gambar = item.thumbnail
            ? `<img src="${item.thumbnail}" referrerpolicy="no-referrer" loading="lazy" onerror="this.remove()">`
            : "";

        const durasiBadge = item.duration
            ? `<span class="dur">${formatDurasi(item.duration)}</span>`
            : "";

        const judul = item.title
            ? `<div class="judul">${escapeHTML(item.title)}</div>`
            : "";

        const uploader = item.uploader
            ? `<span class="uploader">@${escapeHTML(item.uploader)}</span>`
            : "";

        const platformBadge = item.platform
            ? `<span class="platform-badge">${escapeHTML(item.platform)}</span>`
            : "";

        return `
        <div class="item ${cls}" style="animation-delay:${idx * 60}ms">
            <div class="thumb-wrap">${gambar}${durasiBadge}</div>
            <div class="body">
                ${judul}
                <div class="meta-row">
                    ${platformBadge}
                    ${uploader}
                </div>
                <div class="url">${item.url}</div>
                <div class="progress-bg">
                    <div class="progress-fill" style="width:${persen}%"></div>
                </div>
                <div class="status-line">
                    <span class="label">Link ${idx+1} · ${labelStatus(item.status)}</span>
                    <span>${kanan}</span>
                </div>
                ${item.error ? `<div class="err-text">${item.error}</div>` : ""}
            </div>
        </div>
        `;

    }).join("");

}

function escapeHTML(str){
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

function labelStatus(status){
    if(status === "menunggu") return "menunggu";
    if(status === "downloading") return "mengunduh";
    if(status === "processing") return "memproses";
    if(status === "done") return "selesai";
    if(status === "error") return "gagal";
    return status;
}

</script>

</body>
</html>
"""


if __name__ == "__main__":
    if yt_dlp is None:
        print("PERINGATAN: library 'yt-dlp' belum terinstall. Jalankan: pip install yt-dlp")

    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"

    app.run(debug=debug_mode, host="0.0.0.0", port=port)
