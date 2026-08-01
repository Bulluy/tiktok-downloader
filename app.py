"""
TikTok Video Downloader — versi Web (Flask)

Fitur sama seperti versi GUI:
  - Tempel banyak link sekaligus
  - Pilih format: Video (MP4) atau Audio saja (MP3)
  - Opsi "tanpa watermark" (best-effort)
  - Progress bar per link (live, lewat polling AJAX)
  - Link download langsung dari browser setelah selesai

Instalasi (sekali saja):
    pip install flask yt-dlp

Untuk mode Audio (MP3) dibutuhkan FFmpeg terpasang di sistem
(lihat catatan instalasi di versi GUI / cari "install ffmpeg <OS Anda>").

Jalankan:
    python app.py

Lalu buka di browser:
    http://localhost:5000

Catatan: ini server LOKAL untuk dipakai sendiri (atau di jaringan
rumah/kantor Anda). Kalau mau di-deploy ke internet publik, perlu
tambahan seperti autentikasi, rate limiting, dan penyimpanan file
yang lebih matang (bukan folder lokal) — kabari saya kalau itu
yang Anda mau, akan saya bantu strukturnya.

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

# Penyimpanan status job di memori. Cukup untuk pemakaian pribadi/lokal;
# kalau butuh multi-worker/produksi, ini perlu dipindah ke Redis/DB.
JOBS = {}
JOBS_LOCK = threading.Lock()


# ----------------------------------------------------------------------
# LOGIKA DOWNLOAD (mirip versi GUI)
# ----------------------------------------------------------------------

def pilih_format_tanpa_watermark(formats):
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

    job_dir = os.path.join(DOWNLOAD_ROOT, job_id)
    os.makedirs(job_dir, exist_ok=True)

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
            "outtmpl": os.path.join(job_dir, "%(title).60s [%(id)s].%(ext)s"),
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

            if mode == "video" and no_watermark:
                with yt_dlp.YoutubeDL({"quiet": True, "simulate": True}) as ydl_probe:
                    info_awal = ydl_probe.extract_info(url, download=False)

                fmt_id = pilih_format_tanpa_watermark(info_awal.get("formats"))
                if fmt_id:
                    opsi["format"] = fmt_id

            with yt_dlp.YoutubeDL(opsi) as ydl:
                info = ydl.extract_info(url, download=True)
                nama_file = ydl.prepare_filename(info)

                # kalau audio, ekstensi berubah jadi mp3 setelah postprocessing
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
                {"url": u, "status": "menunggu", "percent": 0, "filename": None, "error": None}
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


@app.route("/file/<job_id>/<path:filename>")
def file(job_id, filename):
    job_dir = os.path.join(DOWNLOAD_ROOT, job_id)
    return send_from_directory(job_dir, filename, as_attachment=True)


# ----------------------------------------------------------------------
# FRONTEND (satu file, tanpa folder templates/ terpisah biar simpel)
# ----------------------------------------------------------------------

HTML_PAGE = """
<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<title>TikTok Video Downloader</title>
<style>

*{ box-sizing:border-box; font-family:'Segoe UI',sans-serif; }

body{
    background:#f1f5f9;
    margin:0;
    padding:30px 16px;
}

.wrap{
    max-width:640px;
    margin:0 auto;
}

h1{
    font-size:24px;
    color:#1e293b;
    margin-bottom:4px;
}

.subtitle{
    color:#64748b;
    margin-bottom:20px;
    font-size:14px;
}

.card{
    background:white;
    border-radius:16px;
    padding:24px;
    box-shadow:0 10px 25px rgba(0,0,0,.06);
    margin-bottom:20px;
}

textarea{
    width:100%;
    min-height:120px;
    padding:12px;
    border:1.5px solid #e2e8f0;
    border-radius:10px;
    font-size:14px;
    resize:vertical;
}

textarea:focus{
    outline:none;
    border-color:#2563eb;
}

.opsi-row{
    display:flex;
    align-items:center;
    gap:20px;
    margin-top:14px;
    flex-wrap:wrap;
}

.opsi-row label{
    display:flex;
    align-items:center;
    gap:6px;
    font-size:14px;
    color:#334155;
    cursor:pointer;
}

button.mulai{
    width:100%;
    margin-top:16px;
    padding:14px;
    border:none;
    border-radius:12px;
    background:linear-gradient(135deg,#2563eb,#4f46e5);
    color:white;
    font-weight:700;
    font-size:15px;
    cursor:pointer;
    transition:.2s;
}

button.mulai:hover{ transform:translateY(-2px); }
button.mulai:disabled{ opacity:.6; cursor:not-allowed; transform:none; }

.item{
    border:1px solid #e2e8f0;
    border-radius:10px;
    padding:12px 14px;
    margin-bottom:10px;
}

.item .url{
    font-size:12px;
    color:#64748b;
    word-break:break-all;
    margin-bottom:6px;
}

.progress-bg{
    background:#e2e8f0;
    border-radius:20px;
    height:8px;
    overflow:hidden;
    margin-bottom:6px;
}

.progress-fill{
    background:#2563eb;
    height:100%;
    width:0%;
    transition:width .2s;
}

.item.error .progress-fill{ background:#ef4444; }
.item.done .progress-fill{ background:#16a34a; }

.status-line{
    display:flex;
    justify-content:space-between;
    font-size:13px;
    color:#475569;
}

.status-line a{
    color:#2563eb;
    font-weight:600;
    text-decoration:none;
}

.status-line a:hover{ text-decoration:underline; }

.err-text{
    color:#dc2626;
    font-size:12px;
    margin-top:4px;
}

#hasil{ display:none; }

</style>
</head>
<body>

<div class="wrap">

<h1>📥 TikTok Video Downloader</h1>
<p class="subtitle">Tempel link TikTok, satu per baris.</p>

<div class="card">

    <textarea id="links" placeholder="https://www.tiktok.com/@user/video/xxxxxxx"></textarea>

    <div class="opsi-row">
        <label><input type="radio" name="mode" value="video" checked> 🎬 Video (MP4)</label>
        <label><input type="radio" name="mode" value="audio"> 🎵 Audio saja (MP3)</label>
        <label><input type="checkbox" id="nowm" checked> Tanpa watermark (best-effort)</label>
    </div>

    <button class="mulai" id="btnMulai">⬇  Mulai Unduh</button>

</div>

<div class="card" id="hasil">
    <div id="daftarItem"></div>
</div>

</div>

<script>

const btnMulai = document.getElementById("btnMulai");
const linksBox = document.getElementById("links");
const hasilCard = document.getElementById("hasil");
const daftarItem = document.getElementById("daftarItem");

let pollTimer = null;

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

function renderItems(jobId, items){

    daftarItem.innerHTML = items.map((item, idx) => {

        const cls = item.status === "error" ? "error" : (item.status === "done" ? "done" : "");
        const persen = item.percent || 0;

        let kanan = "";

        if(item.status === "done" && item.filename){
            kanan = `<a href="/file/${jobId}/${encodeURIComponent(item.filename)}" target="_blank">⬇ Download File</a>`;
        }else if(item.status === "error"){
            kanan = "Gagal";
        }else{
            kanan = labelStatus(item.status) + ` ${persen}%`;
        }

        return `
        <div class="item ${cls}">
            <div class="url">${item.url}</div>
            <div class="progress-bg">
                <div class="progress-fill" style="width:${persen}%"></div>
            </div>
            <div class="status-line">
                <span>Link ${idx+1}</span>
                <span>${kanan}</span>
            </div>
            ${item.error ? `<div class="err-text">${item.error}</div>` : ""}
        </div>
        `;

    }).join("");

}

function labelStatus(status){
    if(status === "menunggu") return "Menunggu";
    if(status === "downloading") return "Mengunduh";
    if(status === "processing") return "Memproses";
    return status;
}

</script>

</body>
</html>
"""


if __name__ == "__main__":
    if yt_dlp is None:
        print("PERINGATAN: library 'yt-dlp' belum terinstall. Jalankan: pip install yt-dlp")

    # PORT diisi otomatis oleh platform hosting (Render dkk); 5000 dipakai saat run lokal.
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "0") == "1"

    app.run(debug=debug_mode, host="0.0.0.0", port=port)
