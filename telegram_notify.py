#!/usr/bin/env python3
"""telegram_notify.py: day-trader-skill ciktilarini Telegram'a gonderir.

Bu dosya Belgeler\\trader klasorunde durur (trader-scanner, trader-alerts
klasorlerinin yaninda). Ek paket gerekmez.

    python telegram_notify.py setup      # bot token + sohbet kimligi, bir kez
    python telegram_notify.py test       # deneme mesaji
    python telegram_notify.py install    # Windows Gorev Zamanlayici: hafta ici 5 dakikada bir
    python telegram_notify.py run        # tek tur (zamanlayici bunu calistirir)
    python telegram_notify.py run --dry-run   # gonderme, ekrana yaz
    python telegram_notify.py status
    python telegram_notify.py uninstall

Her turda New York saatine bakar (yaz/kis saati kendiliginden dogru olur):
  1. Acilis oncesi (varsayilan 09:00 NY) gunluk tarama, gunde bir kez.
  2. 09:50 - 15:30 NY arasi: izleme listesinin en cok islem goren hisselerinde
     acilis araligi kirilimi. Her hisse gunde bir kez bildirilir.
  3. trader/alerts.json icindeki alarmlar (stop, hedef, gun sonu).

Hicbir emir vermez. Mesajlar bilgi icindir, yatirim tavsiyesi degildir.
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join("trader", "telegram.json")
STATE = os.path.join("trader", "state", "telegram_state.json")
LOG = os.path.join("trader", "state", "telegram.log")
LOCK = os.path.join("trader", "state", "telegram.lock")
UNIVERSE_META = os.path.join("trader", "state", "universe_meta.json")
TASK = "TraderTelegram"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

DEFAULTS = {
    "scan_time": "09:00",       # New York: gunluk tarama bu saatten sonra, gunde bir kez
    "entry_from": "09:50",      # New York: ilk kirilim kontrolu
    "entry_until": "15:30",     # New York: bundan sonra yeni sinyal bildirilmez
    "max_symbols": 40,          # izleme listesinin en cok islem goren ilk N hissesi
    "symbols": [],              # doluysa izleme listesi yerine bu hisseler izlenir
    "top_hits": 15,             # gunluk tarama mesajindaki hisse sayisi
    "daily_args": [],           # scan.py daily icin ek argumanlar
    "orb_args": [],             # scan.py orb icin ek argumanlar
    "simulate": True,           # her yeni sinyal icin plan + zaman makinesi
    "sim_max": 12,              # bir turda en fazla bu kadar sinyal simule edilir
    "sim_until": "15:55",       # simulasyon bu New York saatinde biter (gun sonu)
    "tm_args": [],              # timemachine.py run icin ek argumanlar
}
SETUP_TR = {
    "breakout_20d": "20g zirve kırılımı",
    "bull_flag": "bayrak",
    "reclaim_ma20": "20g ort. geri alma",
    "oversold": "aşırı satım",
    "oversold_bounce": "aşırı satımdan dönüş",
}


class NotifyError(Exception):
    pass


# ---------- small helpers ----------

def log(msg):
    line = "%s  %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    if sys.stdout:
        try:
            print(line)
        except (OSError, UnicodeError):
            pass


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(path, doc):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def load_config():
    cfg = dict(DEFAULTS)
    cfg.update(read_json(CONFIG, {}) or {})
    # GitHub Actions (or any server): the secrets come from the environment, not from a file
    if os.environ.get("TELEGRAM_TOKEN"):
        cfg["token"] = os.environ["TELEGRAM_TOKEN"].strip()
    if os.environ.get("TELEGRAM_CHAT_ID"):
        cfg["chat_id"] = os.environ["TELEGRAM_CHAT_ID"].strip()
    return cfg


def child_python():
    """python.exe (not pythonw) so the trader scripts get a real stdout."""
    d = os.path.dirname(sys.executable)
    for name in ("python.exe", "python3.exe"):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return sys.executable


def run_script(rel, args, timeout=900):
    """Run one trader script; returns (exit code, stdout, stderr)."""
    path = os.path.join(HERE, *rel.split("/"))
    if not os.path.exists(path):
        raise NotifyError("%s bulunamadı (zip'i Belgeler\\trader klasörüne çıkardın mı?)" % rel)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.run([child_python(), path] + [str(x) for x in args], capture_output=True,
                           timeout=timeout, env=env, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise NotifyError("%s %d saniyede bitmedi" % (rel, timeout))
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def minutes(hhmm):
    h, m = str(hhmm).strip().split(":")[:2]
    return int(h) * 60 + int(m)


def clock12_minutes(text):
    """'9:50 AM' -> minutes since midnight."""
    t = datetime.datetime.strptime(text.strip().upper(), "%I:%M %p")
    return t.hour * 60 + t.minute


def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def ny_utc_offset(day):
    """Hours New York is behind UTC on this date (US rule: 2nd Sun Mar to 1st Sun Nov)."""
    return 4 if _nth_sunday(day.year, 3, 2) <= day < _nth_sunday(day.year, 11, 1) else 5


def local_hhmm(day, ny_minutes):
    """A New York wall-clock time on `day` -> this computer's local HH:MM."""
    naive = datetime.datetime.combine(day, datetime.time(0, 0)) + datetime.timedelta(minutes=ny_minutes)
    utc = (naive + datetime.timedelta(hours=ny_utc_offset(day))).replace(tzinfo=datetime.timezone.utc)
    return utc.astimezone().strftime("%H:%M")


def hm(m):
    return "%d:%02d" % (m // 60, m % 60)


def money(x):
    return "{:,.2f}".format(x).replace(",", "X").replace(".", ",").replace("X", ".")


# ---------- telegram ----------

def tg_call(token, method, params=None, timeout=30):
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        raise NotifyError("Telegram %s: HTTP %d %s" % (method, e.code, body))
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise NotifyError("Telegram'a ulaşılamadı (%s): %s" % (method, e))
    if not doc.get("ok"):
        raise NotifyError("Telegram %s: %s" % (method, doc.get("description")))
    return doc["result"]


def send(cfg, text, dry=False):
    if dry:
        print("----- mesaj -----\n%s\n-----------------" % text)
        return
    if not cfg.get("token") or not cfg.get("chat_id"):
        raise NotifyError("Telegram ayarı yok: önce `python telegram_notify.py setup`")
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > 3900:
            tg_call(cfg["token"], "sendMessage", {"chat_id": cfg["chat_id"], "text": chunk})
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        tg_call(cfg["token"], "sendMessage", {"chat_id": cfg["chat_id"], "text": chunk})


# ---------- the three jobs ----------

def market_clock(now_arg):
    code, out, err = run_script("trader-alerts/alerts.py", ["clock", "--json"] + now_arg, timeout=60)
    try:
        return json.loads(out)
    except ValueError:
        raise NotifyError("alerts.py clock: %s" % (err.strip() or out.strip())[:200])


def refresh_universe(state, clock):
    """Once a week (or when missing) try to rebuild trader/universe.txt; keep the old one on failure."""
    path = os.path.join("trader", "universe.txt")
    meta = read_json(UNIVERSE_META, {}) or {}
    try:
        age = (datetime.date.fromisoformat(clock["date"]) - datetime.date.fromisoformat(meta.get("date", ""))).days
    except ValueError:
        age = None
    if os.path.exists(path) and age is not None and age < 7:
        return
    try:
        code, out, err = run_script("trader-scanner/scan.py", ["universe"], timeout=180)
    except NotifyError as e:
        code, out, err = 1, "", str(e)
    if code == 0:
        write_json(UNIVERSE_META, {"date": clock["date"]})
        log("hisse listesi yenilendi")
    else:
        log("hisse listesi yenilenemedi: %s" % (err.strip() or out.strip())[-200:])
        if os.path.exists(path) and age is None:
            write_json(UNIVERSE_META, {"date": clock["date"]})   # a hand-made list: try again in a week


def job_daily(cfg, clock, now_arg, state):
    refresh_universe(state, clock)
    if not os.path.exists(os.path.join("trader", "universe.txt")):
        raise NotifyError("trader/universe.txt yok ve indirilemedi: bilgisayarındaki "
                          "Belgeler\\trader\\trader\\universe.txt dosyasını buraya koy")
    code, out, err = run_script("trader-scanner/scan.py", ["daily", "--json"] + now_arg + list(cfg["daily_args"]))
    try:
        doc = json.loads(out)
    except ValueError:
        raise NotifyError("günlük tarama: %s" % (err.strip() or out.strip())[-300:])
    c, hits = doc.get("counts", {}), doc.get("hits", [])
    day = datetime.date.fromisoformat(clock["date"])
    lines = ["Günlük tarama (%s)" % doc.get("as_of", ""),
             "%s grafikten %s geldi, %s tarandı, %s bir kurala uydu." % (
                 "{:,}".format(c.get("requested", 0)).replace(",", "."),
                 "{:,}".format(c.get("fetched", 0)).replace(",", "."),
                 "{:,}".format(c.get("scanned", 0)).replace(",", "."), len(hits))]
    per = []
    for key, label in SETUP_TR.items():
        n = sum(1 for h in hits if key in h.get("setups", []))
        if n:
            per.append("%s %d" % (label, n))
    if per:
        lines.append(" · ".join(per))
    if hits:
        lines.append("")
        lines.append("En çok işlem görenler:")
        for h in hits[:int(cfg["top_hits"])]:
            lines.append("%-5s %s$  RSI %.0f  %s" % (
                h["symbol"], money(h["price"]), h.get("rsi") or 0,
                ", ".join(SETUP_TR.get(s, s) for s in h.get("setups", []))))
    if clock.get("opens"):
        lines.append("")
        lines.append("Açılış %s, ilk kırılım kontrolü %s (yerel saat)." % (
            local_hhmm(day, minutes(clock["opens"])), local_hhmm(day, minutes(cfg["entry_from"]))))
    lines.append("Kurala uyan grafikler, al listesi değil.")
    return "\n".join(lines)


def watch_symbols(cfg):
    if cfg.get("symbols"):
        return [str(s).upper() for s in cfg["symbols"]]
    wl = read_json(os.path.join("trader", "watchlist.json"), {}) or {}
    return [h["symbol"] for h in wl.get("hits", []) if h.get("symbol")][:int(cfg["max_symbols"])]


def pct(x):
    return ("%.0f" % (x * 100)) if x >= 0.095 else ("%.1f" % (x * 100)).replace(".", ",")


def r_txt(x):
    return ("%+.2f" % x).replace(".", ",").replace("-", "−") + "R"


def simulate(h, clock, now_arg, cfg):
    """Exit plan + Time Machine for one ORB hit; returns message lines (never raises)."""
    tmp = tempfile.mkdtemp(prefix="tm-")
    plan_path = os.path.join(tmp, "plan.json")
    try:
        code, out, err = run_script("trader-exit/exit.py", [
            "plan", "--symbol", h["symbol"], "--entry", h["price"], "--or-high", h["or_high"],
            "--or-low", h["or_low"], "--time", h["at"], "--date", clock["date"],
            "--write", plan_path, "--json"] + now_arg, timeout=120)
        if code != 0 or not os.path.exists(plan_path):
            reason = (err.strip() or out.strip()).splitlines()
            return ["  plan yok: %s" % (reason[-1][:160] if reason else "exit.py hata verdi")]
        plan = read_json(plan_path, {})
        lines = ["  stop %s · hedef %s (2R) · risk %s$/hisse" % (
            money(plan["stop"]), money(plan["target"]), money(plan["risk_per_share"]))]
        code, out, err = run_script("trader-timemachine/timemachine.py", [
            "run", "--plan", plan_path, "--until", cfg["sim_until"], "--json"]
            + now_arg + list(cfg["tm_args"]), timeout=180)
        try:
            tm = json.loads(out)
        except ValueError:
            reason = (err.strip() or out.strip()).splitlines()
            return lines + ["  simülasyon yok: %s" % (reason[-1][:160] if reason else "hata")]
        mc, hist = tm.get("mc", {}), tm.get("history", {})
        lines.append("  simülasyon: hedef %%%s · stop %%%s · ikisi de değil %%%s · ort. %s" % (
            pct(mc.get("p_target", 0)), pct(mc.get("p_stop", 0)), pct(mc.get("p_neither", 0)),
            r_txt(mc.get("avg_R", 0))))
        if hist.get("triggered"):
            lines.append("  geçmiş %d gün: %d sinyal, %d hedef, %d stop · ort. %s" % (
                hist.get("sessions", 0), hist["triggered"], hist.get("target", 0), hist.get("stop", 0),
                r_txt(hist.get("avg_R", 0))))
        return lines
    except NotifyError as e:
        return ["  simülasyon yok: %s" % e]
    finally:
        for f in os.listdir(tmp):
            try:
                os.remove(os.path.join(tmp, f))
            except OSError:
                pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass


def job_orb(cfg, clock, now_arg, state):
    syms = watch_symbols(cfg)
    if not syms:
        return None
    code, out, err = run_script("trader-scanner/scan.py",
                                ["orb", "--symbols", ",".join(syms), "--no-write", "--json"]
                                + now_arg + list(cfg["orb_args"]), timeout=600)
    try:
        doc = json.loads(out)
    except ValueError:
        log("orb: %s" % (err.strip() or out.strip())[-200:])   # "not yet", "closed": nothing to say
        return None
    seen = set(state.get("triggered", []))
    new = [h for h in doc.get("hits", []) if h["symbol"] not in seen]
    if not new:
        return None
    day = datetime.date.fromisoformat(clock["date"])
    now_min = minutes(clock["now_new_york"].split()[1])
    new.sort(key=lambda h: clock12_minutes(h["at"]), reverse=True)
    lines = ["Açılış aralığı kırılımı (%s, New York %s)" % (local_hhmm(day, now_min), hm(now_min))]
    n_sim = int(cfg["sim_max"]) if cfg.get("simulate") else 0
    for i, h in enumerate(new):
        at = clock12_minutes(h["at"])
        age = now_min - at
        late = "  [%d dk önce, geç]" % age if age > 10 else ""
        if i:
            lines.append("")
        lines.append("%-5s %s$ · kırılım saati %s · aralık tepesi %s$ üstünde kapandı (+%%%s), aralık %s - %s%s" % (
            h["symbol"], money(h["price"]), local_hhmm(day, at), money(h["or_high"]),
            ("%.2f" % h["pct_over"]).replace(".", ","), money(h["or_low"]), money(h["or_high"]), late))
        if i < n_sim:
            lines.extend(simulate(h, clock, now_arg, cfg))
    lines.append("")
    if n_sim:
        lines.append("Simülasyon sinyal fiyatından, gün sonu %s New York'a kadar; tahmin değildir. "
                     "2R hedefte başa baş için hedefin, sonuçlananların %%33'ünde önce gelmesi gerekir."
                     % cfg["sim_until"])
    if len(new) > n_sim and cfg.get("simulate"):
        lines.append("İlk %d sinyal simüle edildi." % n_sim)
    lines.append("%d hisse izlendi. Sinyal bir emir değildir." % len(syms))
    state["triggered"] = sorted(seen | set(h["symbol"] for h in new))
    return "\n".join(lines)


def job_alerts(now_arg):
    if not os.path.exists(os.path.join("trader", "alerts.json")):
        return None
    code, out, err = run_script("trader-alerts/alerts.py", ["check", "--json"] + now_arg, timeout=120)
    try:
        doc = json.loads(out)
    except ValueError:
        return None          # no rules today
    rings = doc.get("rings", [])
    if not rings:
        return None
    return "\n".join(["ALARM"] + [r.get("message", str(r)) for r in rings]
                     + ["", "Alarm bilgidir, emir değildir."])


# ---------- run ----------

def tick(a):
    os.chdir(HERE)
    cfg = load_config()
    now_arg = ["--now", a.now] if a.now else []
    if os.path.exists(LOCK) and time.time() - os.path.getmtime(LOCK) < 15 * 60:
        log("önceki tur hâlâ çalışıyor, bu tur atlandı")
        return 0
    os.makedirs(os.path.dirname(LOCK), exist_ok=True)
    with open(LOCK, "w") as fh:
        fh.write(str(os.getpid()))
    try:
        clock = market_clock(now_arg)
        if not clock.get("opens"):
            log("borsa bugün kapalı (%s)" % clock.get("reason"))
            return 0
        now_min = minutes(clock["now_new_york"].split()[1])
        opens, closes = minutes(clock["opens"]), minutes(clock["closes"])
        state = read_json(STATE, {}) or {}
        if state.get("date") != clock["date"]:
            state = {"date": clock["date"], "triggered": [], "errors": []}

        def guarded(name, fn):
            """Run one job; report a failure to Telegram once a day, not every 5 minutes."""
            try:
                text = fn()
                if text:
                    send(cfg, text, a.dry_run)
                    log("%s: mesaj gönderildi" % name)
                return True
            except NotifyError as e:
                log("%s HATA: %s" % (name, e))
                if name not in state.get("errors", []):
                    state.setdefault("errors", []).append(name)
                    try:
                        send(cfg, "Hata (%s): %s" % (name, e), a.dry_run)
                    except NotifyError as e2:
                        log("hata mesajı gönderilemedi: %s" % e2)
                return False

        if not state.get("scan_done") and minutes(cfg["scan_time"]) <= now_min < closes:
            if guarded("günlük tarama", lambda: job_daily(cfg, clock, now_arg, state)):
                state["scan_done"] = True
            write_json(STATE, state)
        until = min(minutes(cfg["entry_until"]), closes)
        if max(minutes(cfg["entry_from"]), opens + 20) <= now_min <= until:
            guarded("kırılım", lambda: job_orb(cfg, clock, now_arg, state))
        if opens <= now_min <= closes:
            guarded("alarm", lambda: job_alerts(now_arg))
        write_json(STATE, state)
        return 0
    finally:
        try:
            os.remove(LOCK)
        except OSError:
            pass


# ---------- setup, install ----------

def cmd_setup(a):
    os.chdir(HERE)
    print("1) Telegram'da @BotFather ile konuş: /newbot yaz, bir ad ve bir kullanıcı adı ver.")
    print("   BotFather sana bir token verir (123456:ABC... biçiminde).")
    token = input("Token'ı buraya yapıştır (yalnızca bu bilgisayarda saklanır): ").strip()
    me = tg_call(token, "getMe")
    print("Bot bulundu: @%s" % me.get("username"))
    print("2) Telegram'da @%s sohbetini aç ve bota herhangi bir mesaj yaz (örn. merhaba)." % me.get("username"))
    input("Yazdıktan sonra burada Enter'a bas... ")
    chat = None
    for _ in range(6):
        for u in reversed(tg_call(token, "getUpdates", {"timeout": 5})):
            msg = u.get("message") or u.get("channel_post") or {}
            if msg.get("chat", {}).get("id") is not None:
                chat = msg["chat"]
                break
        if chat:
            break
        print("   henüz mesaj görünmüyor, tekrar deniyorum...")
    if not chat:
        raise NotifyError("mesaj bulunamadı: bota bir mesaj yazıp setup'ı yeniden çalıştır")
    cfg = read_json(CONFIG, {}) or {}
    cfg.update({"token": token, "chat_id": chat["id"]})
    for k, v in DEFAULTS.items():
        cfg.setdefault(k, v)
    write_json(CONFIG, cfg)
    send(cfg, "Bağlantı tamam. Day trader bildirimleri bu sohbete gelecek.")
    print("Kaydedildi: %s. Telegram'a deneme mesajı gönderildi." % CONFIG)
    print("Sıradaki: python telegram_notify.py install")
    return 0


def cmd_test(a):
    os.chdir(HERE)
    send(load_config(), "Deneme mesajı: bağlantı çalışıyor.", a.dry_run)
    print("Gönderildi.")
    return 0


def cmd_install(a):
    if os.name != "nt":
        raise NotifyError("install yalnızca Windows'ta çalışır")
    d = os.path.dirname(sys.executable)
    pyw = os.path.join(d, "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    today = datetime.date.today()
    start = local_hhmm(today, 7 * 60 + 30)          # 07:30 New York, an hour of slack for the clock change
    cmd = ["schtasks", "/Create", "/F", "/TN", TASK, "/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI",
           "/ST", start, "/RI", "5", "/DU", "10:00",
           "/TR", '"%s" "%s" run' % (exe, os.path.abspath(__file__))]
    p = subprocess.run(cmd, capture_output=True)
    out = (p.stdout + p.stderr).decode("mbcs" if os.name == "nt" else "utf-8", "replace").strip()
    if p.returncode != 0:
        raise NotifyError("görev oluşturulamadı: %s" % out)
    print(out)
    print("Görev kuruldu: hafta içi %s'den itibaren 10 saat boyunca 5 dakikada bir." % start)
    print("Bilgisayar açık ve uyanık olmalı. Kaldırmak için: python telegram_notify.py uninstall")
    return 0


def cmd_uninstall(a):
    p = subprocess.run(["schtasks", "/Delete", "/F", "/TN", TASK], capture_output=True)
    print((p.stdout + p.stderr).decode("mbcs" if os.name == "nt" else "utf-8", "replace").strip())
    return p.returncode


def cmd_status(a):
    os.chdir(HERE)
    cfg = load_config()
    tok = cfg.get("token") or ""
    print("Token: %s" % (tok[:6] + "..." if tok else "yok (setup çalıştır)"))
    print("Sohbet: %s" % cfg.get("chat_id", "yok"))
    print("İzlenen: %s" % (", ".join(watch_symbols(cfg)) or "izleme listesi boş"))
    print("Durum: %s" % json.dumps(read_json(STATE, {}), ensure_ascii=False))
    if os.path.exists(LOG):
        with open(LOG, encoding="utf-8") as fh:
            print("Son kayıtlar:\n" + "".join(fh.readlines()[-8:]).rstrip())
    return 0


def main():
    ap = argparse.ArgumentParser(description="day-trader-skill -> Telegram")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run")
    p.add_argument("--dry-run", action="store_true", help="gönderme, ekrana yaz")
    p.add_argument("--now", help='saat sabitle, New York: "2026-10-19 10:15"')
    p.set_defaults(fn=tick)
    p = sub.add_parser("test")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_test)
    for name, fn in (("setup", cmd_setup), ("install", cmd_install), ("uninstall", cmd_uninstall),
                     ("status", cmd_status)):
        sub.add_parser(name).set_defaults(fn=fn)
    a = ap.parse_args()
    try:
        return a.fn(a)
    except NotifyError as e:
        log("HATA: %s" % e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
