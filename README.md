# Algentra Capital

Layanan copy trading berbasis AI dengan FastAPI, SQLite, parser Gemini, koneksi Telegram, REST API untuk EA MT5, dashboard publik, dan workspace operasi privat.

## Menjalankan backend

1. Gunakan Python 3.11 atau lebih baru.
2. Dari folder proyek, salin `.env.example` menjadi `.env`, lalu ganti `APP_SECRET_KEY` dengan nilai acak minimal 32 karakter dan `ADMIN_PASSWORD` dengan password minimal 12 karakter. Isi `EA_API_KEY` bila EA akan digunakan. Isi konfigurasi Gmail SMTP dengan App Password untuk mengaktifkan OTP Gmail pada reset kata sandi; pendaftaran klien diverifikasi melalui WhatsApp yang ditautkan admin. Kata sandi akun klien minimal 8 karakter. Formulir daftar dan login klien dilindungi Cloudflare Turnstile; development memakai kunci uji resmi. Untuk production, buat widget di [Cloudflare Turnstile](https://developers.cloudflare.com/turnstile/get-started/) lalu isi `TURNSTILE_SITE_KEY` dan `TURNSTILE_SECRET_KEY` di environment layanan.
3. Buat virtual environment (`python -m venv .venv` di Windows atau `python3 -m venv .venv` di Linux/macOS), lalu pasang dependensi dengan `.venv\Scripts\python -m pip install -r backend/requirements.txt -e copyTrading` di Windows atau `.venv/bin/python -m pip install -r backend/requirements.txt -e copyTrading` di Linux/macOS. Package MT5 hanya aktif pada Windows.
4. Jalankan `run.bat` untuk pengembangan. Skrip menjalankan backend FastAPI pada `127.0.0.1:8000`, WhatsApp OTP worker pada `127.0.0.1:8765`, dan managed copy worker setelah backend siap. Kedua worker dibuka di terminal terpisah; tekan Ctrl+C di terminal utama untuk menghentikan semuanya. Backend memuat ulang saat kode berubah tanpa membuat worker duplikat. Managed copy worker memerlukan `COPIER_WORKER_API_KEY`, `copyTrading/config/managed-worker.json`, `copyTrading/config/master.json`, dan terminal MT5 yang dikonfigurasi. Jika file config belum ada, helper membuat file contoh lalu berhenti; isi pengaturannya dan jalankan `run.bat` lagi. Worker WhatsApp hanya menerima koneksi loopback dan memakai kunci turunan dari `APP_SECRET_KEY`.
5. Buka alamat yang dicetak `run.bat` diikuti `/loginAdmin` untuk masuk sebagai admin. Admin dapat membuka/menutup pendaftaran dari dashboard; jika ditutup sebelum jadwal launching, pendaftaran otomatis terbuka pada 16 Januari 2027 pukul 09.00 WIB. Untuk mengaktifkan WhatsApp OTP, buka **OTP WhatsApp** di dashboard admin, pilih **Hubungkan sesi tersimpan**, lalu pindai QR asli yang muncul dari WhatsApp > Perangkat tertaut. Sesi tersimpan di `backend/data/whatsapp_otp_session.db` dan digunakan kembali saat backend berjalan. Untuk memasangkan ulang, pilih **Hapus sesi lama & buat QR baru**; sesi lokal lama dihapus setelah koneksi berhenti. Klien mendaftar dari `/register` dengan email akun dan nomor WhatsApp yang diverifikasi melalui OTP, lalu masuk melalui `/login` dengan email dan kata sandi. Reset kata sandi tersedia melalui `/forgot-password` dengan pilihan OTP Gmail atau nomor WhatsApp yang terverifikasi. Di `/account`, client memilih paket dan mengisi detail akun MT5; sesudah membuat pesanan, portal mengarahkan client ke `/transactions` untuk membuat QRIS AirPay, membayar nominal tepat beserta kode unik, dan melihat konfirmasi otomatis. Akun dibuat setelah AirPay mengonfirmasi pembayaran.
6. Isi `GEMINI_API_KEY` di `.env` untuk Gemini parser sinyal. Chat customer service **ALGENTRA** memakai key terpisah `GEMINI_CS_API_KEY`; isi dengan API key CS yang berbeda. Model dan batas waktu CS dapat diatur lewat `GEMINI_CS_MODEL` dan `GEMINI_CS_TIMEOUT_SECONDS` (default 60 detik), terpisah dari parser sinyal. Kedua fitur tetap nonaktif sampai key masing-masing diisi.
7. Untuk Telegram, isi `TELEGRAM_API_ID` dan `TELEGRAM_API_HASH` dari [my.telegram.org](https://my.telegram.org), lalu restart backend dan buka `/telegram-setup` setelah login admin.
8. Isi `AIRPAY_BASE_URL` dan `AIRPAY_API_KEY` di `.env`; `AIRPAY_MERCHANT_UID` menjadi cadangan jika respons QR dari AirPay tidak menyertakan `mid`. API key hanya digunakan oleh backend. Pembayaran baru dibuat dan dikonfirmasi langsung melalui AirPay; bot Telegram pembayaran tetap tersedia untuk menangani pesanan manual lama yang masih menunggu pemeriksaan.

### Lisensi EA TERNAK UANG

Setelah backend terbaru berjalan pada domain HTTPS, login sebagai admin dan buka bagian **API key EA TERNAK UANG** di dashboard. Tanggal kedaluwarsa wajib diisi. Jam dan menit boleh dikosongkan; jika kosong, key berlaku sampai akhir tanggal tersebut (23:59 WIB). Jika diisi, key kedaluwarsa pada jam dan menit yang dipilih (WIB). Buat key, salin saat ditampilkan, lalu isi parameter `AlgentraApikey` pada EA. Biarkan `AlgentraServerURL` berisi `https://algentracapital.my.id`, dan tambahkan domain tersebut di **Tools > Options > Expert Advisors > Allow WebRequest** pada MT5. EA menolak berjalan jika key kosong, tidak valid, kedaluwarsa, dihapus, atau server lisensi tidak dapat dijangkau; key yang dihapus menghentikan EA pada pemeriksaan berikutnya, paling lambat sekitar satu menit. Key hanya disimpan sebagai hash di database dan tidak dapat ditampilkan ulang; buat key baru jika salinannya hilang.

Kontak publik pada beranda diatur melalui variabel `CONTACT_*` di `.env.example` dan `.env`. Tautan email, Instagram, TikTok, grup/admin Telegram, dan website ditampilkan dengan ikon; alamat sosial harus berupa URL HTTPS.

## Akses publik melalui Cloudflare Tunnel

Quick Tunnel untuk pengembangan membuat URL sementara pada domain trycloudflare.com. Untuk menggunakannya:

1. Jalankan backend dengan run.bat atau ./run.sh.
2. Buka terminal kedua dari folder proyek dan jalankan **cloudflared tunnel --url http://127.0.0.1:8000**.
3. Buka URL HTTPS yang dicetak cloudflared. URL berhenti berlaku ketika proses tunnel dihentikan.

Quick Tunnel ditujukan untuk pengembangan. Untuk domain tetap, buat named tunnel di Cloudflare, atur public hostname menuju http://127.0.0.1:8000, lalu jalankan **cloudflared tunnel run --token TOKEN_DARI_DASHBOARD** dengan token dari dashboard Cloudflare. Gunakan COOKIE_SECURE=true di .env untuk akses HTTPS produksi. Backend tetap mendengarkan di localhost; tunnel meneruskan trafik tanpa membuka port 8000 ke internet. Lihat [Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/) dan [panduan tunnel bernama](https://developers.cloudflare.com/tunnel/features/locally-managed-tunnels/create-local-tunnel/).

## Produksi di Windows Server

Gunakan satu Windows Server dengan Cloudflare Tunnel. Simpan `.env`, SQLite, sesi Telegram, dan backup di folder data persisten di luar folder kode; berikan akses folder hanya ke akun layanan. Atur `APP_ENV=production`, `COOKIE_SECURE=true`, dan `DATABASE_URL` ke jalur SQLite absolut, misalnya `sqlite:///D:/algentra-data/app.db`. Jangan gunakan jalur relatif karena layanan dapat dimulai dengan direktori kerja berbeda.

Pasang aplikasi sebagai Windows Service melalui NSSM atau Task Scheduler saat boot. Jalankan `run-service.bat` sebagai perintah layanan; ia menjalankan backend tanpa reload, WhatsApp OTP worker, dan managed copy worker sebagai proses terpisah. Backend bind ke `127.0.0.1:8000`; worker WhatsApp bind hanya ke `127.0.0.1:8765`. Atur Cloudflare Tunnel ingress ke `http://127.0.0.1:8000`; jangan buka port worker ke jaringan. Aktifkan HSTS pada Cloudflare SSL/TLS Edge Certificates. Konfigurasikan rotasi log pada service manager.

Jika situs mengembalikan halaman Cloudflare **502**, uji origin langsung dari VPS dengan `Invoke-WebRequest http://127.0.0.1:8000/health` sebelum memeriksa Tunnel. Jika gagal, lihat log service dan pastikan dependency terbaru sudah terpasang dengan `.venv\Scripts\python.exe -m pip install -r backend\requirements.txt -e copyTrading`, lalu restart service. Jika health lokal berhasil tetapi domain tetap 502, periksa log `cloudflared` dan pastikan hostname diarahkan ke `http://127.0.0.1:8000`. Setelah pembaruan fitur pembayaran, pastikan `.env` tidak mengisi hanya salah satu dari `TELEGRAM_BOT_TOKEN` atau `TELEGRAM_ADMIN_CHAT_ID`; keduanya harus diisi bersama atau dibiarkan kosong.

Database SQLite dibuat otomatis pada jalur yang ditentukan. Atur `BACKUP_DIR` pada environment layanan ke folder backup di disk/lokasi terpisah. Buat task harian Windows Task Scheduler yang menjalankan `run-backup.bat`; skrip menggunakan SQLite online backup API dan mempertahankan 14 salinan terakhir. Pulihkan dengan menyalin file backup saat aplikasi berhenti, lalu mulai ulang layanan. Uji restore sebelum peluncuran. Perubahan Gemini hanya menerima nama model dari dashboard; simpan API key di environment layanan.

## Layanan managed copy untuk akun client

Mode **Dikelola server** membolehkan client mengatur follower melalui `/account`; client tidak perlu memasang MT5 atau EA di komputernya. Setelah client menyimpan detail dan password broker, worker otomatis memeriksa login MT5 tanpa mengirim order. Portal menampilkan hasilnya dan tombol live hanya aktif setelah login serta izin trading terminal terverifikasi. Worker mengaktifkan Auto Trading pada salinan terminal follower saat menyiapkannya. Akun tetap dijeda sampai client memilih metode ukuran lot, lalu menekan **Mulai copy trading (live)** dan menyetujui konfirmasi. Managed copy tidak membatasi total lot atau jumlah posisi pada level aplikasi; aturan volume simbol, margin, dan izin broker tetap menentukan order yang bisa diterima. Password broker disimpan terenkripsi menggunakan key turunan `APP_SECRET_KEY`, tidak dikembalikan ke halaman client, dan hanya diberikan ke worker yang memakai `COPIER_WORKER_API_KEY`.

Worker berada di subfolder `copyTrading/` dan membutuhkan Windows dengan terminal MT5 serta package MetaTrader5. Untuk VPS, atur `COPIER_WORKER_API_KEY` yang sama pada backend dan akun service Windows, gunakan terminal template follower yang bersih dan terpisah dari master, lalu ikuti [panduan managed worker](copyTrading/README.md#managed-copy-dari-portal-client). Tanpa worker online, permintaan tersimpan tetapi akun tidak menyalin posisi. Jika `APP_SECRET_KEY` diganti, password managed yang tersimpan tidak dapat dibuka; client perlu menyimpan ulang kredensial dari portal.

Untuk menjalankan di lokal, pastikan `copyTrading/config/managed-worker.json` mengarah ke backend lokal dan `copyTrading/config/master.json` ke terminal sumber yang sudah login, lalu jalankan `run.bat`. Supervisor membuka managed copy worker di terminal terpisah; worker membaca key dari `.env`. Buka `/account` sebagai client. Akun follower baru diproses setelah pembelian disetujui, detail broker disimpan, dan client secara sadar mengaktifkan copy live.

## Endpoint M1

- `GET /health`
- Public site: `GET /` dan `/performance`; data performa anonim di `GET /api/public/performance` hanya memakai trade yang sudah ditutup.
- `POST /api/auth/login` untuk admin, `POST /api/auth/client-login` untuk klien, `POST /api/auth/logout`, pendaftaran melalui `POST /api/auth/register`, `POST /api/auth/resend-code`, dan `POST /api/auth/verify-otp` (`/verify-email` tetap tersedia untuk kompatibilitas), serta reset kata sandi melalui `POST /api/auth/password-reset/request` dan `POST /api/auth/password-reset/confirm`. Halaman reset: `GET /forgot-password`.
- Portal klien: `/account`; pengelolaan terminal melalui `GET/POST /api/mt5/accounts`, `PUT /api/mt5/accounts/{id}`, `PUT /api/mt5/accounts/{id}/active`, dan `POST /api/mt5/accounts/{id}/rotate-token`.
- Copy posisi MT5: `POST /api/ea/master/snapshot` memakai `X-API-Key` dari EA Telegram; `GET /api/mt5/follower/positions` memakai `X-Account-Token` khusus untuk setiap terminal follower.
- Managed copy: client membuat pesanan melalui `/api/payments/orders`, mengatur `/api/mt5/accounts/{id}/managed-copy`, dan mengaktifkan atau menjeda lewat `/api/mt5/accounts/{id}/managed-copy/active`. Worker Windows memakai `GET /api/mt5/copier/jobs` dan endpoint status/laporan dengan `X-Copier-Worker-Key`.
- `GET /api/stats`
- `GET /api/signals?limit=100&offset=0`
- `GET /api/logs?limit=100&offset=0&level=INFO&search=...`
- `GET /api/settings` dan `PUT /api/settings` dengan body `{"values":{"confidence_threshold":0.75}}`
- `POST /api/parser/test` untuk menguji klasifikasi dan validasi. Halaman admin tersedia di `/parser-test`.
- Telegram: `GET /api/tg/status`, `POST /api/tg/send-code`, `POST /api/tg/verify-code`, `POST /api/tg/verify-2fa`, `POST /api/tg/logout`, `POST /api/tg/reconnect`, `GET /api/tg/groups`, dan `POST /api/tg/groups/select`.
- OTP WhatsApp (sesi admin): `GET /api/whatsapp-otp/status`, `POST /api/whatsapp-otp/connect`, `POST /api/whatsapp-otp/new-qr`, dan `POST /api/whatsapp-otp/disconnect`. `new-qr` menghapus sesi lokal lama lalu memulai pairing baru. Kode OTP berlaku 10 menit, sekali pakai, dan pengiriman per nomor dibatasi.
- EA: `GET /api/ea/pending`, `POST /api/ea/report`, `POST /api/ea/result`, `POST /api/ea/heartbeat`, `POST /api/ea/master/snapshot`, `POST /api/ea/scalper/heartbeat`, dan `POST /api/ea/scalper/trade` memakai header `X-API-Key` yang nilainya sama dengan `EA_API_KEY`.

## Website publik dan workspace operasi

Beranda publik Algentra Capital tersedia di `/`; bagian performa menampilkan P&L agregat aktual dari trade tertutup. Database saat ini belum mengaitkan trade dengan akun klien terpisah, jadi angka publik tidak diklaim sebagai hasil beberapa akun. Workspace privat tetap tersedia di `/dashboard` setelah login admin. Tabel signal terbaru dan log sistem menampilkan paling banyak lima baris.

Grafik Indeks Equity publik menampilkan grafik garis dari laporan equity akun utama MT5. Riwayat dimulai ulang pada sesi harian pukul 07.00 WIB, dan celah data ditampilkan tanpa membuat data sintetis. Tinggi grafik tetap dibatasi agar perubahan equity tidak mengubah tata letak halaman.

## Chat AI customer service ALGENTRA

Tombol chat ALGENTRA tampil pada halaman publik dan portal klien, tetapi tidak pada workspace admin. Server menentukan mode dari sesi login: pengunjung mendapat jawaban umum, sedangkan klien hanya mendapat ringkasan akun miliknya. Riwayat publik memakai cookie pengunjung bertanda tangan; percakapan publik dan klien disimpan terpisah. Riwayat tidak dapat dibaca lagi setelah 24 jam dari awal percakapan. Pesan yang kedaluwarsa dihapus saat endpoint chat dipanggil dan oleh pembersih berkala setiap 15 menit.

Chat memakai `GEMINI_CS_API_KEY` dan `GEMINI_CS_MODEL`; key parser sinyal `GEMINI_API_KEY` tidak digunakan oleh layanan CS. Pesan dan ringkasan label/status akun milik klien dikirim ke Gemini untuk menjawab pertanyaan. Token EA tidak dimasukkan ke prompt Gemini. Token baru dan token hasil rotasi disimpan terenkripsi menggunakan key yang diturunkan dari `APP_SECRET_KEY`, lalu hanya disisipkan dalam jawaban API untuk pemilik akun yang sedang login bila pelanggan meminta kode/token. Kode/token tetap tidak muncul dalam log percakapan tersimpan; pelanggan harus menghindari membagikannya. Akun yang dibuat sebelum fitur ini menyimpan token satu-arah saja; untuk menampilkannya lewat chat, pelanggan harus merotasi token sekali dari portal.

Jaga `APP_SECRET_KEY` tetap stabil selama token terenkripsi diperlukan. Jika nilainya diganti, token lama tidak bisa dibuka oleh chat dan pelanggan perlu melakukan rotasi token dari portal; token MT5 yang sudah aktif tetap dapat dicabut/dirotasi.

## Dashboard admin

Setelah login admin, buka `/dashboard` untuk melihat statistik signal dan trade, heartbeat EA, status Telegram/Gemini, signal terbaru, statistik grup Telegram, grafik performa EA Scalper, grafik profit, log, dan riwayat trade. WebSocket `/ws` mengirim pembaruan berkala; dashboard mencoba menyambung ulang otomatis. Log dan riwayat trade menandai sumber Telegram atau EA Scalper, lalu dapat difilter serta diekspor ke CSV. Halaman settings mengatur confidence, symbol mapping, deviasi harga, mode entry, update signal, kill switch, dan konfigurasi Gemini. Sinyal yang dieksekusi selalu dikirim sebagai order live; tidak ada demo mode atau dry run. **Single entry** membuka satu order; untuk zona harga, order memakai titik tengah dan TP pertama. **Partial entry** mempertahankan pembagian order per TP; zona tanpa TP dibagi menjadi dua order di batas zona. Perubahan mode berlaku untuk signal baru yang diklaim EA; signal yang sudah diklaim menyimpan mode sebelumnya agar laporan entry tetap cocok. Signal dibatasi ke XAUUSD dan kedaluwarsa setelah satu jam. Chart.js dimuat dari CDN sehingga grafik memerlukan akses jaringan browser.

Endpoint dashboard (semuanya memerlukan sesi admin): `GET /api/stats`, `GET /api/signals`, `GET /api/groups/stats`, `GET /api/scalper/stats`, `GET /api/logs`, `GET /api/logs/export.csv`, `GET /api/trades`, `GET /api/trades/export.csv`, `GET/PUT /api/settings`, `PUT /api/settings/gemini-config`, serta `WS /ws`.

Sebelum memanggil Gemini, filter lokal hanya meneruskan pesan yang memiliki arah transaksi dan harga; percakapan serta laporan hasil trade dilewati tanpa permintaan Gemini. Backend dan EA hanya menerima XAUUSD, termasuk nama broker yang memakai suffix.

## Uji login Telegram manual

1. Buat API ID dan API hash pada `my.telegram.org`, simpan di `.env`, dan restart backend.
2. Login admin di `/loginAdmin`, lalu buka `/telegram-setup`.
3. Masukkan nomor telepon dengan kode negara, minta kode, lalu masukkan kode yang diterima di aplikasi Telegram.
4. Jika akun memakai 2FA, masukkan password 2FA. Status harus berubah ke `connected`.
5. Muat daftar grup/channel, aktifkan whitelist yang diinginkan, isi alias bila perlu, lalu simpan.
6. Kirim pesan uji di salah satu grup whitelist dan periksa hasil di API `/api/signals` dan `/api/logs`.

Session Telethon berada di `backend/data/telegram_user.session`, tidak disajikan oleh FastAPI, dan folder `backend/data` masuk `.gitignore`. Logout dari halaman Telegram Setup mencabut session yang tersimpan.

## Memasang EA MT5

1. Buat `EA_API_KEY` acak minimal 24 karakter di `.env`, lalu restart backend.
2. Login sebagai admin dan unduh source EA utama dari `/downloads/TelegramSignalEA.mq5`.
3. Buka file `.mq5` dengan MetaEditor dari MT5, tekan **F7** untuk compile, lalu pasang file `.ex5` hasil compile ke `MQL5/Experts`.
4. Di MT5, buka **Tools > Options > Expert Advisors**, aktifkan **Allow WebRequest for listed URL**, lalu tambahkan `http://127.0.0.1:8000`, sama dengan default `ServerURL` EA.
5. Pasang EA ke chart akun MT5 live, isi `ApiKey` dengan nilai `EA_API_KEY`, dan tetapkan `LotMode` serta `RiskPercent` sesuai batas risiko yang diinginkan. EA langsung mengirim order live; pastikan pengaturan sinyal, lot, SL/TP, dan izin trading terminal benar sebelum mengaktifkannya.
6. Periksa tab Experts/Journal untuk heartbeat, status koneksi, dan laporan signal.

Resolusi simbol mencoba nama persis, akhiran broker yang unik, lalu pemetaan eksplisit. EA Telegram utama mendeteksi akun cent dari mata uang akun atau identitas server; untuk signal `XAUUSD`, EA otomatis memilih `XAUUSDc` bila tersedia. EA juga menyesuaikan spread, slippage, dan jarak SL/TP bawaan jika simbol cent memakai satu angka desimal tambahan. Status serta alasan menunggu/penolakan order tampil di panel EA dan tab Experts. Jika broker memakai nama lain, isi `SymbolSuffix` atau `SymbolMapCsv` dalam format `XAUUSD=XAUUSDc`. EA Copy Trading follower tetap dapat memakai pengaturan suffix/pemetaan sendiri. `MarketWatchlistCsv` mengatur simbol yang dipublikasikan EA utama untuk panel harga.

`GET /api/ea/pending` mengklaim signal selama 90 detik. Jika simbol atau quote belum tersedia, izin trading mati, atau spread melewati batas, EA mencoba signal kembali sampai kedaluwarsa satu jam. Jika signal lawan datang saat posisi EA di XAUUSD masih profit, signal baru diabaikan. Jika posisi lawan sedang floating minus, EA menutup posisi tersebut sebelum membuka arah baru. Jika jarak risiko SL melebihi TP terdekat tetapi masih tercakup oleh TP terjauh, backend menyimpan TP terjauh saja; EA lalu memvalidasi jarak SL terhadap target itu. Sinyal tetap ditolak jika TP terjauh pun tidak cukup. Untuk sinyal MARKET tanpa entry, backend memilih TP terjauh agar EA dapat memvalidasinya terhadap quote live. EA menyimpan penanda idempotensi per entry dan mengirim ulang laporan bila perlu agar polling/restart tidak membuat order ganda. Pada mode Partial, jika sinyal memiliki beberapa TP, EA membuat satu order untuk setiap TP. Mode Single memaksa satu order dan hanya menggunakan TP pertama. Semua order memakai SL dari sinyal; bila SL tidak tersedia, EA memakai `DefaultSLPoints`. Dengan `LotMode=LOT_RISK_PERCENT` dan `RiskPercent=1.0`, anggaran risiko seluruh entry dari satu sinyal adalah sekitar 1% ekuitas akun jika semua entry mencapai SL. Anggaran uang itu dibagi rata per entry, lalu lot tiap entry dihitung dari estimasi P/L MT5 antara harga entry dan SL serta dibulatkan ke bawah sesuai langkah lot broker. Khusus akun cent XAUUSD, jika anggaran risiko menghasilkan lot di bawah minimum, EA mencoba lot fallback 0.01 apabila volume tersebut valid pada broker; pemeriksaan margin lokal dilewati hanya untuk fallback ini dan broker tetap memutuskan apakah order dapat diterima. Sinyal lain tetap memakai hitungan lot sebelumnya. Jumlah posisi tidak dibatasi oleh EA.

## EA Scalper

EA Scalper terpisah dari EA Telegram dan dapat dipasang di chart MT5 lain. Versi saat ini membuka **satu order per sinyal dengan lot tetap 0,01** dan hanya mengizinkan satu posisi aktif untuk magic number EA Scalper. `InpSingleEntryTarget` memilih satu dari target TP1/TP2/TP3. SL dipasang saat entry dan tidak digeser ke breakeven/SL+. Volume dibulatkan mengikuti aturan broker; jika broker tidak menerima 0,01 lot, EA menahan order dan mencatat alasannya.

Untuk memasukkan posisi EA Scalper ke dashboard admin:

1. Pastikan backend memakai `EA_API_KEY` yang sama dengan EA Telegram, lalu restart backend bila nilainya baru diubah.
2. Di MT5, tambahkan `https://algentracapital.my.id` ke daftar **Allow WebRequest for listed URL**. Alamat itu menjadi default `InpWebsiteServerURL`; perbarui jika domain website berubah.
3. Compile `mt5/EA Scalper.mq5` di MetaEditor, salin `EA Scalper.ex5` ke `MQL5/Experts`, lalu pasang EA pada chart instrumen yang ingin diperdagangkan.
4. Di input EA, isi `InpWebsiteApiKey` dengan nilai `EA_API_KEY`, aktifkan `InpEnableWebsiteSync`, dan pastikan URL sama dengan alamat yang diizinkan pada langkah sebelumnya.

EA mengirim heartbeat, posisi terbuka, dan hasil posisi tertutup. Dashboard memiliki grafik EA Scalper dan status koneksi yang terpisah dari statistik grup Telegram. Riwayat trade dan log mencantumkan sumber, tanpa membuat signal Telegram sintetis. Laporan close menggabungkan profit, komisi, swap, dan fee dari history MT5. Jika sinkronisasi gagal sementara, EA mencoba lagi pada siklus berikutnya; hasil close yang sudah diterima tidak dihitung dua kali.

## Copy posisi dari akun utama ke akun follower

Pendaftaran memakai OTP enam digit melalui nomor WhatsApp, berlaku 10 menit dan hanya dapat digunakan sekali. Email tetap menjadi alamat akun dan login. Reset kata sandi menyediakan pilihan Gmail atau nomor WhatsApp terverifikasi; memasukkan OTP reset Gmail juga memverifikasi email akun. Bot WhatsApp harus ditautkan admin melalui QR di dashboard. Pengiriman dibatasi satu kali per menit dan maksimal tiga kali per jam per kontak; kode menerima maksimal tiga percobaan. Gmail dapat dipakai untuk reset sandi: buat akun Gmail khusus, aktifkan 2-Step Verification, lalu buat App Password dari pengaturan Google Account. Isi `.env` seperti di bawah memakai App Password, bukan password Gmail biasa, lalu restart backend. Endpoint juga dibatasi per alamat IP.

```env
EMAIL_SMTP_HOST=smtp.gmail.com
EMAIL_SMTP_PORT=587
EMAIL_SMTP_USERNAME=algentra.sender@gmail.com
EMAIL_SMTP_PASSWORD=app-password-google
EMAIL_FROM="Algentra Capital <algentra.sender@gmail.com>"
EMAIL_SMTP_STARTTLS=true
```

### Panduan klien: menyalin posisi MT5

EA Telegram yang berjalan pada terminal akun utama Algentra mengeksekusi sinyal Telegram sekaligus membagikan posisi terbuka yang dibuat EA itu. Backend menyediakan snapshot tersebut untuk semua terminal follower klien. Follower menyalin posisi, perubahan SL/TP, dan penutupan posisi. Portal tidak meminta password broker. Masuk ke `/account`, daftarkan setiap akun follower, lalu simpan token yang muncul karena token lengkap hanya ditampilkan saat dibuat atau dirotasi.

Unduh EA setelah login ke portal:

- Unduh [source EA follower](http://127.0.0.1:8000/downloads/MT5FollowerCopyEA.mq5) — compile di MetaEditor dan pasang hasil `.ex5` pada terminal MT5.

Untuk memasang EA:

1. Di MT5, pilih **File → Open Data Folder**, lalu buka `MQL5/Experts`.
2. Buka `MT5FollowerCopyEA.mq5` dengan MetaEditor, tekan **F7** untuk compile, lalu salin file `.ex5` hasil compile ke folder tersebut.
3. Di MT5, pilih **Tools → Options → Expert Advisors**. Aktifkan **Allow WebRequest for listed URL** dan tambahkan `http://127.0.0.1:8000`.
4. Dari **Navigator → Expert Advisors**, tarik EA follower ke chart. Isi `AccountToken` dengan token akun itu dan pastikan `ServerURL` berisi `http://127.0.0.1:8000`. MT5 harus berjalan di komputer yang sama dengan backend.
5. Aktifkan **Algo Trading**. EA Telegram di terminal utama Algentra harus berjalan agar posisi sumber terus diperbarui. EA follower menyalin langsung ke akun live jika terminal dan akun mengizinkan trading.

EA Telegram menerbitkan posisi dengan magic number miliknya setiap detik; posisi manual atau EA lain di terminal utama tidak dibagikan. Follower hanya menyalin posisi terbuka, perubahan SL/TP, dan penutupan, bukan pending order sebelum terisi. `VolumeMultiplier` menentukan volume salinan relatif terhadap volume sumber, bukan persentase ekuitas. Lot disesuaikan dengan maksimum dan langkah volume broker. `MaxEntryDeviationPercent=0` menonaktifkan batas keterlambatan entry; isi angka positif bila ingin membatasi salinan yang jauh dari harga entry master. Akun follower tetap harus memakai mode **hedging** karena follower mengelola tiap posisi sumber secara terpisah. Jika data sumber tidak diperbarui selama 20 detik, follower berhenti menyinkronkan. Jika EA berhenti, posisi follower yang sudah terbuka tetap berada di terminal dan perlu dikelola di MT5. Rotasi token jika hilang atau terekspos, lalu masukkan token baru ke EA.

Semua terminal follower klien menerima sumber posisi Algentra yang sama. Akun MT5 klien hanya perlu didaftarkan sebagai follower; terminal sumber tidak perlu didaftarkan lewat portal klien.

Kedua EA yang dibagikan berupa file `.ex5` hasil compile. Jika binary diperbarui, unduh file terbaru dan salin ulang ke folder `MQL5/Experts`.

### Entry berupa rentang harga

Pesan seperti `XAUUSD BUY NOW 4100:::4093`, `XAUUSD BUY 4000-4010`, atau `BUY : ENTRY ZONE : 4185–4190 | TP : 4195, 4200, 4205, 4210, 4215 | SL : 4175` disimpan sebagai zona entry. Parser menyimpan batas bawah dan atas; rentang eksplisit mengesampingkan kata BUY NOW/SELL NOW. Pada mode Partial, bila zona memiliki beberapa TP, EA membuat jumlah order sesuai jumlah TP dan membagi entry merata di dalam zona. Untuk dua TP atau lebih, BUY dimulai dari batas bawah dan SELL dari batas atas; satu TP memakai titik tengah zona. Pada mode Single, satu order memakai titik tengah zona dan TP pertama. Harga entry diselaraskan dengan tick symbol dan dijaga tetap di dalam zona. Tiap order memakai TP sesuai urutannya; jenis order (Limit atau Stop) mengikuti posisi harga pasar MT5. Bila zona tidak memiliki TP, mode Partial membuat dua order di batas zona dengan TP default, sementara mode Single membuat satu order di titik tengah.

Sinyal dengan lebih dari satu entry memerlukan akun MT5 mode hedging agar tiap order menjadi posisi terpisah; akun netting ditolak. Pending order berlaku satu jam lalu dihapus jika belum terisi. EA memakai expiration broker bila tersedia dan penghapusan berkala saat EA berjalan sebagai fallback.

SL dan setiap TP divalidasi terhadap seluruh zona: SL BUY harus di bawah batas bawah dan TP BUY di atas batas atas; aturan SELL kebalikannya. Jarak risiko dari entry ke SL juga tidak boleh lebih besar daripada jarak ke TP terdekat. EA menghormati kill switch, spread, dan persentase risiko per signal. Lot mengikuti batas dan langkah volume broker; bila hasil perhitungan risiko lebih kecil dari lot minimum broker, entry ditolak agar anggaran risiko tidak terlampaui.

## Test

Jalankan backend tests dari folder proyek: `.venv\Scripts\python -m pytest backend/tests` di Windows atau `.venv/bin/python -m pytest backend/tests` di Linux/macOS. Jalankan worker tests di Windows dengan `$env:PYTHONPATH="copyTrading/src"; .venv\Scripts\python.exe -m pytest copyTrading/tests`.

## Belum teruji

Koneksi Gemini, Telegram, dan SMTP dengan kredensial sungguhan belum diuji. EA Telegram dan EA copy MT5 perlu dikompilasi ulang dari source di MetaEditor sebelum dipasang. Keduanya belum diuji pada akun live; setelah dipasang, order dieksekusi secara live ketika Algo Trading dan izin trading MT5 aktif.
