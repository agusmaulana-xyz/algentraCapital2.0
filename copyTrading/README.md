# Copy Trade MT5

Sistem ini memantau posisi pada satu terminal MT5 master dan menyalin perubahan ke satu proses Python dan satu terminal MT5 terpisah untuk setiap follower. Gunakan akun demo untuk seluruh pengujian awal.

## Arsitektur

```text
Terminal MT5 master (sudah login)
           │ positions_get polling
           ▼
 master_publisher.py ── JSONL harian di folder bersama ──┬─ follower.py + terminal #1
                                                         ├─ follower.py + terminal #2
                                                         └─ follower.py + terminal #N
                                      launcher.py menjaga semua proses
```

Package Python `MetaTrader5` menyimpan koneksi terminal sebagai state global per proses, dan satu terminal hanya login ke satu akun. Karena itu setiap akun memakai satu folder terminal MT5 mode `/portable` dan satu proses Python. Launcher menolak konfigurasi yang memakai ulang path terminal, login follower, atau nama proses. Publisher hanya membaca terminal master; ia tidak membuat atau mengubah order.

## Persiapan Windows VPS

1. Pasang Python 3.10+ 64-bit dan terminal MT5.
2. Salin folder instalasi MT5 menjadi folder terpisah untuk setiap follower, misalnya `C:\MT5_F1` dan `C:\MT5_F2`.
3. Jalankan setiap `terminal64.exe /portable`, login ke akun follower yang sesuai, lalu tutup terminal dengan normal agar sesi tersimpan. Pastikan terminal master juga sudah login.
4. Pada setiap terminal follower, nyalakan tombol **Algo Trading** dan aktifkan **Allow algorithmic trading** pada pengaturan Expert Advisors terminal. Publisher hanya membaca terminal master; pengaturan trading algoritmik diperlukan follower untuk `order_send()`.
5. Buka PowerShell pada folder proyek, buat virtual environment, lalu pasang dependensi:

   ```powershell
   py -3.10 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   python -m pip install -e .
   ```

`MetaTrader5` hanya berjalan pada Windows dan membutuhkan terminal yang telah terpasang.

## Konfigurasi rahasia

Salin file contoh dan isi konfigurasi lokal. `.gitignore` mengabaikan `config/*.json`, tetapi tetap melacak file `*.example.json`.

```powershell
Copy-Item config\master.example.json config\master.json
Copy-Item config\follower.example.json config\akun1.json
Copy-Item config\launcher.example.json config\launcher.json
$env:MT5_PASS_AKUN1 = "password-akun-demo"
```

Password hanya dibaca dari environment variable yang disebut oleh `password_env`; jangan menaruh password di JSON, argumen proses, atau log. Untuk Task Scheduler, siapkan environment variable pada akun Windows yang menjalankan task.

Atur `terminal_path` dan `folder_sinyal` di master dan follower. Gunakan folder sinyal yang sama pada semua config. Pasangan emas XAUUSD dipetakan otomatis ke simbol broker lokal (suffix seperti `m`, `c`, `.c`, atau alias `GOLD` tidak perlu diatur). Gunakan `map_simbol` hanya untuk instrumen non-emas yang memang membutuhkan pemetaan khusus. Lot dapat memakai `tetap`, `rasio`, atau `saldo`; isi `saldo_master` untuk mode saldo. Config follower wajib menetapkan `max_lot_per_order`, `max_posisi_terbuka`, dan `max_lot_total`. Contoh follower mengaktifkan `dry_run: true`; sesuaikan semua batas dengan akun.

## Menjalankan

Jalankan tahapan ini pada akun demo terlebih dahulu:

1. Pastikan contoh follower tetap `dry_run: true`. Jalankan publisher dan launcher melalui `scripts\start_all.bat`, atau jalankan `python -m copytrade.launcher config\launcher.json` setelah mengaktifkan virtual environment. Pada dry run semua pembacaan, perhitungan, dan pemetaan diuji, tetapi tidak ada order yang dikirim.
2. Setelah log dan pemetaan sesuai harapan, mulai uji order pada akun demo dengan `dry_run: false`, akun follower kosong, dan volume kecil. Hapus state dry-run yang bersangkutan sebelum tes live pertama supaya mapping tiket sintetis tidak dipakai kembali. Pastikan akun follower mengizinkan trading algoritmik.
3. Pindah ke akun sebenarnya hanya setelah verifikasi demo dan pemeriksaan aturan broker selesai. Periksa batas lot dan posisi untuk setiap akun.

Jalankan test dari folder proyek dengan `pytest`.

## Managed copy dari portal client

Mode ini menyatukan pengaturan follower dengan portal client Algentra. Client memilih **Dikelola server**, menyelesaikan pembayaran, menyimpan password broker di halaman akun, mengatur nama simbol serta batas lot, lalu secara eksplisit memulai penyalinan live. Password terenkripsi di backend dan worker mengambil job aktif melalui `X-Copier-Worker-Key`. Worker harus berjalan pada Windows; MT5 tetap dipakai di sisi server, sedangkan client tidak perlu memasang atau menjalankan MT5 di perangkatnya.

Persiapan:

1. Pasang Algentra dan worker pada Windows Server yang dikelola sendiri. Atur `COPIER_WORKER_API_KEY` ke random secret minimal 32 karakter di `.env` backend dan environment akun Windows yang menjalankan worker. Jangan gunakan kunci contoh.
2. Siapkan terminal MT5 master untuk `master.json` seperti pada konfigurasi standalone, termasuk password lewat environment variable sendiri dan `folder_sinyal` yang bisa diakses worker.
3. Siapkan folder terminal follower bersih yang berisi `terminal64.exe`. Jangan gunakan folder master, folder terminal client yang sudah login, atau folder yang berisi sesi akun lain. Worker menggandakan folder ini ke `runtime/terminals/<account_id>`, mengaktifkan izin Auto Trading terminal pada konfigurasi portable, dan menjalankan satu proses per client. Saat client menyimpan kredensial di portal, worker memvalidasi login terlebih dahulu tanpa mengirim order; aktivasi copy live diblokir sampai validasi berhasil.
4. Salin config worker dan sesuaikan endpoint, terminal template, dan folder runtime:

   ```powershell
   Copy-Item config\managed-worker.example.json config\managed-worker.json
   ```

5. Pastikan `master.json` menunjuk terminal MT5 master yang memang sudah login dan terkoneksi. Dari root folder `algentraCapitalProject`, jalankan worker lokal dengan helper berikut; helper membaca `COPIER_WORKER_API_KEY` dari `.env` root dan tidak mencetak nilainya:

   ```powershell
   .venv\Scripts\python.exe copyTrading\scripts\run_managed_worker.py
   ```

Worker menjalankan publisher master, memeriksa daftar job aktif tiap beberapa detik, membuat terminal terisolasi, dan melaporkan heartbeat, saldo, posisi terbuka, serta deal MT5 ke portal. Saat job baru mulai, worker mengirim histori deal 30 hari terakhir per batch maksimal 100 deal, kemudian melanjutkan dari cursor terakhir. Untuk VPS, tetap jalankan worker sebagai Scheduled Task/Windows Service dan sediakan `COPIER_WORKER_API_KEY` pada environment akun Windows; jangan jalankan `launcher.py` bersamaan karena worker managed sudah menjalankan publisher master sendiri. File konfigurasi follower di runtime tidak memuat password; password disuntikkan ke environment proses follower saat start. Worker API key dan password akun lain difilter dari environment child process. Log worker tidak mencatat request body atau environment.

Untuk memasang managed worker otomatis saat Windows startup, jalankan PowerShell sebagai akun layanan VPS:

```powershell
.\scripts\install_managed_worker_task.ps1 -ProjectRoot "C:\Path\To\algentraCapitalProject" -PythonPath "C:\Path\To\python.exe"
```

Task **Algentra Managed Copy Worker** berjalan dengan hak tertinggi akun Windows yang memasangnya, membaca worker key dari `.env`, dan dimulai ulang oleh Task Scheduler jika proses berhenti.

Client dapat menjeda copy kapan saja dari portal. Menjeda atau menghapus profil menghentikan copier, tetapi posisi yang sudah terbuka tetap ada pada akun broker dan tidak ditutup otomatis. Menghapus akun dari portal membuat worker membersihkan folder terminal, state, dan konfigurasi follower milik akun tersebut pada sinkronisasi berikutnya. Uji end-to-end hanya dengan akun demo dahulu; worker aktif akan menggunakan `dry_run: false` setelah client menyetujuinya di portal.

## Menambah follower

Salin folder MT5 dalam mode `/portable`, salin `config/follower.example.json` ke satu JSON baru, beri `nama` unik dan password environment variable sendiri, lalu tambahkan path JSON itu ke daftar `followers` di `config/launcher.json`. Tidak perlu mengubah kode.

## Checklist follower siap menerima sinyal

- Terminal MT5 follower berjalan, login ke akun yang benar, dan path-nya cocok dengan config.
- Algo Trading aktif dan **Allow algorithmic trading** dicentang.
- Environment variable password tersedia untuk proses yang berjalan.
- `account_info()` menunjukkan mode hedging; mode netting memerlukan `izinkan_netting: true` dan pemetaan posisi dapat kurang akurat.
- Simbol hasil mapping tersedia di Market Watch dan spesifikasi lot sesuai batas broker.
- `dry_run` dan batas `max_posisi_terbuka`, `max_lot_total`, serta `max_lot_per_order` sudah ditinjau.
- Heartbeat `heartbeat_<nama>.txt` diperbarui dan log follower tidak menunjukkan error koneksi atau order.

## File sinyal, state, dan batasan

Publisher menambahkan satu baris JSON per sinyal ke `sinyal_YYYYMMDD.jsonl`. Follower menyimpan offset byte, tanggal file, mapping posisi, dan ID sinyal yang telah diproses secara atomik di `state/<nama>.json`. Entry buka yang lebih tua dari `max_umur_sinyal_buka_detik` dilewati; ubah, partial close, dan tutup tetap diproses tanpa batas umur. Sinyal yang sudah dikonsumsi ditandai selesai meskipun order broker gagal agar retry proses tidak membuat entry duplikat. Restart tepat setelah broker menerima order namun sebelum state sempat ditulis tetap memiliki jendela kecil yang tidak dapat dibuat exactly-once oleh file JSONL saja; pantau log dan terminal saat gangguan proses.

Latency polling, jaringan, dan slippage membuat transaksi follower mirip, bukan identik, dengan master. Spread, nama simbol, ukuran lot minimum, mode akun, dan aturan eksekusi berbeda antarbroker. Periksa aturan broker atau prop firm mengenai copy trading serta penggunaan banyak akun dari satu alamat IP.

## Pemecahan masalah

- **10027 / autotrading dinonaktifkan**: nyalakan Algo Trading dan aktifkan **Allow algorithmic trading** pada terminal follower.
- **10030 / filling mode tidak didukung**: periksa `symbol_info().filling_mode` dan spesifikasi simbol broker; follower mencoba filling mode yang dinyatakan didukung.
- **10014 / volume tidak valid**: periksa `volume_min`, `volume_max`, `volume_step`, `max_lot_per_order`, dan mode lot.
- **`positions_get()` bernilai `None` di master**: publisher mencatat `last_error()` dan menahan snapshot; kondisi ini tidak memicu sinyal tutup massal.
- **Sinyal buka kedaluwarsa**: publisher/follower atau koneksi terminal terlambat; entry lama sengaja dilewati. Sinyal pengelolaan posisi tetap diproses.
- **Follower tidak muncul di launcher**: pastikan file JSON ada, terdaftar di `followers`, `aktif` bukan `false`, dan nama follower unik.

## Startup Windows

Jalankan PowerShell sebagai akun Windows yang akan menjalankan VPS task, lalu:

```powershell
.\scripts\install_startup_task.ps1 -ProjectRoot (Get-Location).Path -PythonPath "C:\Path\To\Python\python.exe"
```

Script membuat scheduled task **CopyTrade MT5 Launcher** dengan trigger **At startup** dan hak tertinggi. Pastikan environment variable password tersedia untuk akun task tersebut.
