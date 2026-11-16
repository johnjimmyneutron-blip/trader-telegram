# trader-telegram

day-trader-skill taramalarini GitHub Actions ile calistirir ve Telegram'a mesaj atar.
Hicbir emir vermez. Yatirim tavsiyesi degildir.

- `.github/workflows/trader.yml`: zamanlama (hafta ici, UTC)
- `telegram_notify.py`: her turda ne yapilacagina karar verir
- `trader-scanner/`, `trader-alerts/`: day-trader-skill paketinden (MIT, bkz. LICENSE-day-trader-skill)
- `trader/`: izleme listesi ve gunluk durum (is akisi kendisi gunceller)

Gerekli iki gizli deger (Settings > Secrets and variables > Actions):
`TELEGRAM_TOKEN` ve `TELEGRAM_CHAT_ID`.
