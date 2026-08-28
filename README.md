# 🌐 FreeProxy-Router

[![Proxy Scanner](https://github.com/rizkikotet-dev/FreeProxy-Router/actions/workflows/proxy-scan.yml/badge.svg)](https://github.com/rizkikotet-dev/FreeProxy-Router/actions/workflows/proxy-scan.yml)

**FreeProxy-Router** adalah AI Proxy Finder & Validator otomatis yang mencari, menguji, dan memvalidasi proxy publik menggunakan endpoint AI OpenCode. Dilengkapi dengan GitHub Actions yang berjalan setiap 30 menit untuk memperbarui daftar proxy secara otomatis.

---

## ✨ Fitur Utama

- **Multi-endpoint AI Testing** – Menguji proxy di dua endpoint sekaligus (`/responses` dan `/chat/completions`). Proxy dianggap aktif jika salah satu endpoint mengembalikan output AI yang valid.
- **Schema-aware Validation** – Memvalidasi respons AI, menolak respons yang hanya echo prompt, metadata, atau status.
- **Multi-Protocol Support** – Mendukung HTTP, HTTPS, SOCKS4, dan SOCKS5.
- **Auto Fallback** – HTTP → HTTPS fallback otomatis pada port 443.
- **Two-stage Validation** – Pre-filter hidup/mati cepat via `https://api.iplocate.io/ip`; hanya proxy yang hidup yang lanjut ke validasi AI multi-endpoint.
- **Double-check Verification** – Memverifikasi proxy dua kali sebelum dinyatakan aktif.
- **Multi-source Proxy** – Menggabungkan proxy dari Proxifly, Monosans, TheSpeedX, dan IPLocate.
- **Rich Dashboard** – Tampilan real-time dengan `rich` library.
- **Export Formats** – Output dalam format `proxies.txt`, `omniroute.txt`, dan `proxies.json`.

---

## 📦 Instalasi

```bash
# Clone repository
git clone https://github.com/rizkikotet-dev/FreeProxy-Router.git
cd FreeProxy-Router

# Install dependencies
pip install --upgrade pip
pip install requests rich
pip install "requests[socks]"
```

---

## 🚀 Penggunaan

### Jalankan Sekali

```bash
python main.py --all --all-sources --api-key "YOUR_API_KEY"
```

### Parameter Penting

| Parameter | Deskripsi |
|-----------|-----------|
| `--all` | Scan semua proxy yang berhasil dimuat |
| `--all-sources` | Gunakan semua sumber proxy yang tersedia |
| `--api-key` | API key untuk OpenCode AI |
| `--output` | File output proxy (default: `proxies.txt`) |
| `--omniroute-output` | File output format Omniroute (default: `omniroute.txt`) |
| `--json-output` | File output format JSON |
| `--limit` | Batas maksimum proxy yang discan |
| `--workers` | Jumlah thread paralel (default: 10) |
| `--max-time` | Read timeout per request (default: 15 detik) |
| `--liveness-url` | URL liveness check, bisa dipisah koma (default: `https://api.iplocate.io/ip`) |
| `--liveness-timeout` | Read timeout per liveness request (default: 8 detik) |
| `--skip-liveness` | Lewati pre-filter liveness (langsung test AI) |
| `--skip-port-check` | Lewati pengecekan port TCP terpisah |
| `--probe` | Debug satu proxy tertentu |

### Contoh Lengkap

```bash
python main.py \
  --all \
  --all-sources \
  --api-key "sk-xxx" \
  --output proxies.txt \
  --omniroute-output omniroute.txt \
  --json-output proxies.json \
  --workers 20 \
  --max-time 20 \
  --skip-port-check
```

### Debug Satu Proxy

```bash
python main.py --probe http://1.2.3.4:8080 --api-key "YOUR_API_KEY"
```

---

## 🤖 GitHub Actions Auto-Scan

Repository ini dilengkapi dengan GitHub Actions yang menjalankan scanner secara otomatis **setiap 30 menit** dan melakukan commit & push hasilnya.

### Workflow File (`.github/workflows/proxy-scan.yml`)

```yaml
name: Proxy Scanner

on:
  schedule:
    - cron: '*/30 * * * *'   # setiap 30 menit
  workflow_dispatch:         # trigger manual

concurrency:
  group: proxy-scan
  cancel-in-progress: false

jobs:
  scan:
    runs-on: ubuntu-latest
    permissions:
      contents: write

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.10'

      - name: Install dependencies
        run: |
          pip install --upgrade pip
          pip install requests rich
          pip install "requests[socks]"

      - name: Run proxy scanner
        env:
          PROXY_TEST_API_KEY: ${{ secrets.PROXY_API_KEY }}
        run: |
          set +e
          python main.py \
            --all \
            --all-sources \
            --api-key "$PROXY_TEST_API_KEY" \
            --output proxies.txt \
            --omniroute-output omniroute.txt \
            --json-output proxies.json \
            --workers 20 \
            --max-time 20 \
            --liveness-url "https://api.iplocate.io/ip" \
            --liveness-timeout 8
          SCAN_EXIT=$?
          set -e
          echo "scan_exit=$SCAN_EXIT" >> "$GITHUB_ENV"
          echo "Scan finished: exit code ${SCAN_EXIT} (0 = ada proxy aktif, 1 = tidak ada proxy aktif)"

      - name: Warn if no active proxies found
        if: env.scan_exit == '1'
        run: |
          echo "::warning::Scan selesai dengan exit code 1 (tidak ada proxy aktif atau gagal memuat source). Commit/push tetap dijalankan."

      - name: Commit and push if changes
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add -A
          git diff --staged --quiet || git commit -m "Auto-update proxy lists [skip ci]"
          git push
```

### Menyiapkan Secret API Key

1. Buka repository → **Settings** → **Secrets and variables** → **Actions**
2. Klik **New repository secret**
3. Nama: `PROXY_API_KEY`
4. Nilai: isi dengan OpenCode AI API key Anda

> ⚠️ **Catatan**: Interval 30 menit adalah rekomendasi minimum untuk menghindari batasan rate limit GitHub Actions.

---

## 📁 Output Files

| File | Deskripsi |
|------|-----------|
| `proxies.txt` | Daftar proxy aktif (satu per baris) |
| `omniroute.txt` | Format Omniroute untuk import bulk |
| `proxies.json` | JSON lengkap dengan metadata, latency, dan AI response |

### Contoh `proxies.json`

```json
{
  "timestamp": "2026-01-01T00:00:00+00:00",
  "version": "6.4",
  "stats": {
    "total_loaded": 120,
    "total_checked": 120,
    "liveness_checked": 1500,
    "liveness_alive": 120,
    "success": 45
  },
  "active_proxies": [
    {
      "protocol": "http",
      "ip": "1.2.3.4",
      "port": 8080,
      "country": "US",
      "latency_ms": 320.5,
      "proxy": "http://1.2.3.4:8080",
      "ai_text": "PONG"
    }
  ]
}
```

---

## 🛠️ Opsi Lanjutan

### Environment Variables

| Variable | Deskripsi |
|----------|-----------|
| `PROXY_TEST_API_KEY` | API key OpenCode AI |
| `PROXY_TEST_URL` | Custom endpoint URL (comma separated) |
| `PROXY_TEST_MODEL` | Model AI (default: `big-pickle`) |
| `PROXY_LIVENESS_URL` | URL liveness check (comma separated, default: `https://api.iplocate.io/ip`) |

### Sumber Proxy yang Didukung

- Proxifly (HTTP, SOCKS4, SOCKS5, All)
- Monosans (HTTP, SOCKS4, SOCKS5, JSON)
- TheSpeedX (HTTP, SOCKS4, SOCKS5)
- IPLocate (HTTP, HTTPS, SOCKS4, SOCKS5)

---

## 📄 Lisensi

MIT License — silakan gunakan dan modifikasi sesuai kebutuhan. Lihat file [LICENSE](LICENSE) untuk detail lengkap.

---

## 🙏 Kredit

Dibangun dengan:
- [requests](https://github.com/psf/requests) - HTTP library
- [rich](https://github.com/Textualize/rich) - Rich terminal UI
- [PySocks](https://github.com/Anorov/PySocks) - SOCKS support

---

## 📞 Kontribusi

Pull request dan issue selalu diterima. Untuk pertanyaan lebih lanjut, silakan buka issue di repository ini.