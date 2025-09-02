#!/usr/bin/env python3
"""
Telegram bot/daemon che controlla la pagina USR Lombardia del MiM ogni 30 minuti
(e invia una notifica quando compare una nuova notizia nel tab-container).

Versione ottimizzata per Replit.com con keep-alive integrato.

Funzionalità:
- Gestione iscrizioni multiple (/start, /stop, /help).
- Polling Telegram frequente (ogni 5s) per comandi quasi in tempo reale.
- Controllo notizie separato ogni 30 minuti (configurabile).
- /last per mostrare manualmente l'ultima notizia disponibile.
- /force per forzare il controllo come nel ciclo automatico (solo se nuova).
- /next per sapere quando arriverà il prossimo controllo automatico.
- /stats per vedere statistiche del bot.
- Keep-alive server per Replit.com (rimane sempre attivo).
- Messaggi con emoji e formattazione migliorata.
- Conferme immediate per tutti i comandi.

Uso:
1) Python 3.10+
2) pip install -U requests beautifulsoup4 python-dotenv
3) .env con TELEGRAM_BOT_TOKEN=...
4) Avvia: python telegram_mim_watcher.py
"""
from __future__ import annotations

import os
import time
import json
import logging
from dataclasses import dataclass
from typing import Optional, List
from urllib.parse import urljoin
from datetime import datetime, timedelta
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler

import requests
from bs4 import BeautifulSoup

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# ===================== Config =====================
MIM_URL = "https://www.mim.gov.it/web/usr-lombardia"
STATE_FILE = os.environ.get("STATE_FILE", "seen.json")
SUBSCRIBERS_FILE = os.environ.get("SUBSCRIBERS_FILE", "subscribers.json")
STATS_FILE = os.environ.get("STATS_FILE", "stats.json")
NEWS_INTERVAL = int(os.environ.get("NEWS_INTERVAL", 1800))  # default 30 min
TELEGRAM_POLL_INTERVAL = int(os.environ.get("TELEGRAM_POLL_INTERVAL", 5))  # default 5 sec
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else None
USER_AGENT = os.environ.get("USER_AGENT", "Mozilla/5.0 (compatible; MiMWatcher/1.0)")
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", 20))
KEEP_ALIVE_PORT = int(os.environ.get("PORT", 8000))  # Replit usa PORT
# ==================================================

# =================== KEEP ALIVE SERVER ===================
class KeepAliveHandler(BaseHTTPRequestHandler):
    """Handler per il server keep-alive che mantiene Replit attivo"""
    
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        
        # Pagina web con info sul bot
        uptime_seconds = int(time.time() - bot_start_time) if 'bot_start_time' in globals() else 0
        uptime_formatted = format_duration(uptime_seconds)
        
        html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>MiM Watcher Bot - Status</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body {{ font-family: Arial, sans-serif; margin: 40px; background: #f5f5f5; }}
        .container {{ max-width: 600px; margin: 0 auto; background: white; padding: 30px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        .status {{ color: #28a745; font-weight: bold; }}
        .info {{ background: #e7f3ff; padding: 15px; border-radius: 5px; margin: 20px 0; }}
        .emoji {{ font-size: 1.2em; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🤖 MiM Watcher Bot</h1>
        <p class="status">✅ Bot attivo e funzionante!</p>
        
        <div class="info">
            <h3>📊 Statistiche</h3>
            <p><span class="emoji">⏰</span> <strong>Uptime:</strong> {uptime_formatted}</p>
            <p><span class="emoji">🔄</span> <strong>Controllo ogni:</strong> {NEWS_INTERVAL//60} minuti</p>
            <p><span class="emoji">📱</span> <strong>Polling Telegram:</strong> ogni {TELEGRAM_POLL_INTERVAL} secondi</p>
            <p><span class="emoji">🌐</span> <strong>Monitoraggio:</strong> <a href="{MIM_URL}" target="_blank">USR Lombardia</a></p>
        </div>
        
        <div class="info">
            <h3>🚀 Come usare il bot</h3>
            <p>1. Cerca il bot su Telegram</p>
            <p>2. Scrivi <code>/start</code> per iscriverti</p>
            <p>3. Ricevi notifiche automatiche delle nuove notizie!</p>
        </div>
        
        <p><small>🔧 Keep-alive server attivo per Replit.com</small></p>
    </div>
</body>
</html>
        """
        self.wfile.write(html.encode())
    
    def log_message(self, format, *args):
        # Disabilita i log del server HTTP per non intasare i log
        pass

def start_keep_alive_server():
    """Avvia il server keep-alive in un thread separato"""
    try:
        server = HTTPServer(('0.0.0.0', KEEP_ALIVE_PORT), KeepAliveHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logging.info("🌐 Keep-alive server avviato su porta %s", KEEP_ALIVE_PORT)
        return server
    except Exception as e:
        logging.error("❌ Errore avvio keep-alive server: %s", e)
        return None

# ============================================================

@dataclass
class NewsItem:
    title: str
    url: str
    raw_href: str

    @property
    def key(self) -> str:
        return self.raw_href.strip()

@dataclass
class BotStats:
    start_time: float
    total_news_sent: int = 0
    total_commands_processed: int = 0
    last_news_time: Optional[float] = None
    last_error_time: Optional[float] = None
    keep_alive_hits: int = 0
    
    def to_dict(self):
        return {
            'start_time': self.start_time,
            'total_news_sent': self.total_news_sent,
            'total_commands_processed': self.total_commands_processed,
            'last_news_time': self.last_news_time,
            'last_error_time': self.last_error_time,
            'keep_alive_hits': self.keep_alive_hits
        }
    
    @classmethod
    def from_dict(cls, data):
        # Gestisce chiavi mancanti per retrocompatibilità
        data.setdefault('keep_alive_hits', 0)
        return cls(**data)

def setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

def load_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path: str, data) -> None:
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error("Errore salvataggio %s: %s", path, e)

def format_time_remaining(seconds: int) -> str:
    """Formatta i secondi rimanenti in un formato leggibile"""
    if seconds <= 0:
        return "⏰ Prossimo controllo imminente!"
    
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    
    if hours > 0:
        return f"⏱️ Prossimo controllo tra: {hours}h {minutes}m {secs}s"
    elif minutes > 0:
        return f"⏱️ Prossimo controllo tra: {minutes}m {secs}s"
    else:
        return f"⏱️ Prossimo controllo tra: {secs}s"

def format_duration(seconds: int) -> str:
    """Formatta una durata in secondi"""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        return f"{seconds//60}m {seconds%60}s"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        return f"{hours}h {minutes}m"

def send_telegram_message(chat_id: int, text: str, parse_mode: str = "HTML", disable_preview: bool = False) -> bool:
    """Invia un messaggio Telegram con gestione errori migliorata"""
    if not TELEGRAM_API:
        return False
    
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
    }
    
    try:
        r = requests.post(f"{TELEGRAM_API}/sendMessage", data=payload, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        return True
    except Exception as e:
        logging.error("Errore invio a %s: %s", chat_id, e)
        return False

def fetch_page(url: str) -> str:
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.text

def parse_latest_item(html: str) -> Optional[NewsItem]:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find(id=lambda x: isinstance(x, str) and x.strip().startswith("tab-container"))
    if not container:
        container = soup

    items = container.find_all("li", class_=lambda c: c and ("asset-tab-home" in c.split() or "bg_today" in c.split()))
    if not items:
        items = container.find_all("li")

    for li in items:
        a = li.find("a", href=True)
        if not a:
            continue
        title = a.get_text(strip=True)
        href = a["href"].strip()
        abs_url = urljoin(MIM_URL, href)
        if title and href:
            return NewsItem(title=title, url=abs_url, raw_href=href)

    a = container.find("a", href=True)
    if a:
        title = a.get_text(strip=True) or "(senza titolo)"
        href = a["href"].strip()
        abs_url = urljoin(MIM_URL, href)
        return NewsItem(title=title, url=abs_url, raw_href=href)
    return None

def escape_html(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

def send_news_notification(item: NewsItem, chat_id: int) -> bool:
    """Invia una notifica news con formattazione migliorata"""
    text = f"🔔 <b>Nuova notizia USR Lombardia!</b>\n\n" \
           f"📰 <b>{escape_html(item.title)}</b>\n\n" \
           f"🔗 {escape_html(item.url)}\n\n" \
           f"⏰ <i>{datetime.now().strftime('%d/%m/%Y alle %H:%M')}</i>"
    
    return send_telegram_message(chat_id, text, disable_preview=False)

def broadcast(item: NewsItem, subscribers: List[int], stats: BotStats) -> None:
    """Invia la notifica a tutti gli iscritti"""
    successful_sends = 0
    
    for chat_id in subscribers:
        if send_news_notification(item, chat_id):
            successful_sends += 1
            logging.info("Notifica inviata a %s: %s", chat_id, item.title)
        else:
            logging.warning("Fallito invio a %s", chat_id)
    
    stats.total_news_sent += successful_sends
    stats.last_news_time = time.time()
    save_json(STATS_FILE, stats.to_dict())

def check_once(seen: List[str], subscribers: List[int], stats: BotStats) -> List[str]:
    try:
        html = fetch_page(MIM_URL)
    except Exception as e:
        logging.error("Errore fetch pagina: %s", e)
        stats.last_error_time = time.time()
        return seen

    item = parse_latest_item(html)
    if not item:
        logging.warning("Nessun elemento trovato nel tab-container.")
        return seen

    if item.key in seen:
        logging.info("Nessuna novità. Ultimo già visto: %s", item.title)
        return seen

    broadcast(item, subscribers, stats)
    seen = [item.key] + [k for k in seen if k != item.key]
    seen = seen[:50]
    save_json(STATE_FILE, seen)
    return seen

def handle_start_command(chat_id: int, subscribers: List[int], send_welcome_news: bool = True) -> List[int]:
    """Gestisce il comando /start"""
    if chat_id not in subscribers:
        subscribers.append(chat_id)
        save_json(SUBSCRIBERS_FILE, subscribers)
        logging.info("Nuovo iscritto: %s", chat_id)
        
        welcome_text = f"🎉 <b>Benvenuto nel MiM Watcher!</b>\n\n" \
                      f"📢 Riceverai notifiche automatiche ogni volta che viene pubblicata una nuova notizia su USR Lombardia.\n\n" \
                      f"⚙️ <b>Comandi disponibili:</b>\n" \
                      f"• /help - Mostra tutti i comandi\n" \
                      f"• /last - Mostra l'ultima notizia\n" \
                      f"• /next - Quando sarà il prossimo controllo\n" \
                      f"• /stats - Statistiche del bot\n" \
                      f"• /stop - Cancella iscrizione\n\n" \
                      f"🔄 Controllo notizie ogni {NEWS_INTERVAL//60} minuti"
        
        send_telegram_message(chat_id, welcome_text)
        
        # Invia l'ultima notizia disponibile come benvenuto
        if send_welcome_news:
            send_telegram_message(chat_id, "🔍 Ti mostro subito l'ultima notizia disponibile...")
            time.sleep(1)  # Piccola pausa per migliore UX
            
            try:
                html = fetch_page(MIM_URL)
                item = parse_latest_item(html)
                if item:
                    welcome_news_text = f"📰 <b>Ultima notizia USR Lombardia:</b>\n\n" \
                                       f"📄 <b>{escape_html(item.title)}</b>\n\n" \
                                       f"🔗 {escape_html(item.url)}\n\n" \
                                       f"💡 <i>Da ora riceverai automaticamente le nuove notizie!</i>"
                    send_telegram_message(chat_id, welcome_news_text, disable_preview=False)
                    logging.info("Notizia di benvenuto inviata a %s: %s", chat_id, item.title)
                else:
                    send_telegram_message(chat_id, "ℹ️ Al momento non ci sono notizie disponibili, ma ti avviserò non appena ne arriveranno!")
            except Exception as e:
                logging.error("Errore invio notizia benvenuto a %s: %s", chat_id, e)
                send_telegram_message(chat_id, "⚠️ Non riesco a recuperare l'ultima notizia al momento, ma il monitoraggio è attivo!")
        
    else:
        already_text = f"✅ Sei già iscritto alle notifiche!\n\n" \
                      f"🔄 Controllo automatico ogni {NEWS_INTERVAL//60} minuti\n" \
                      f"📝 Usa /help per vedere tutti i comandi"
        send_telegram_message(chat_id, already_text)
    
    return subscribers

def handle_stop_command(chat_id: int, subscribers: List[int]) -> List[int]:
    """Gestisce il comando /stop"""
    if chat_id in subscribers:
        subscribers.remove(chat_id)
        save_json(SUBSCRIBERS_FILE, subscribers)
        logging.info("Utente %s disiscritto.", chat_id)
        
        bye_text = f"👋 <b>Iscrizione cancellata!</b>\n\n" \
                  f"❌ Non riceverai più notifiche dalle news USR Lombardia.\n\n" \
                  f"🔄 Puoi sempre riscriverti con /start"
        
        send_telegram_message(chat_id, bye_text)
    else:
        not_subscribed_text = f"ℹ️ Non risulti iscritto alle notifiche.\n\n" \
                             f"📝 Usa /start per iscriverti"
        send_telegram_message(chat_id, not_subscribed_text)
    
    return subscribers

def handle_help_command(chat_id: int):
    """Gestisce il comando /help"""
    help_text = f"📖 <b>Comandi disponibili:</b>\n\n" \
               f"🔔 <b>/start</b> - Iscriviti alle notifiche automatiche\n" \
               f"❌ <b>/stop</b> - Cancella l'iscrizione\n" \
               f"📰 <b>/last</b> - Mostra l'ultima notizia disponibile\n" \
               f"⏰ <b>/next</b> - Quando sarà il prossimo controllo automatico\n" \
               f"🚀 <b>/force</b> - Forza il controllo notizie ora\n" \
               f"📊 <b>/stats</b> - Statistiche del bot\n" \
               f"❓ <b>/help</b> - Mostra questo messaggio\n\n" \
               f"🤖 <b>Come funziona:</b>\n" \
               f"Il bot controlla automaticamente ogni {NEWS_INTERVAL//60} minuti se ci sono nuove notizie su USR Lombardia del MiM e ti avvisa immediatamente!\n\n" \
               f"💡 <b>Suggerimento:</b> Usa /next per sapere quando sarà il prossimo controllo"
    
    send_telegram_message(chat_id, help_text)

def handle_last_command(chat_id: int):
    """Gestisce il comando /last"""
    send_telegram_message(chat_id, "🔍 Cerco l'ultima notizia disponibile...")
    
    try:
        html = fetch_page(MIM_URL)
        item = parse_latest_item(html)
        if item:
            text = f"📰 <b>Ultima notizia disponibile:</b>\n\n" \
                   f"📄 <b>{escape_html(item.title)}</b>\n\n" \
                   f"🔗 {escape_html(item.url)}"
            send_telegram_message(chat_id, text, disable_preview=False)
        else:
            send_telegram_message(chat_id, "❌ Nessuna notizia trovata al momento.")
    except Exception as e:
        logging.error("Errore /last: %s", e)
        send_telegram_message(chat_id, "⚠️ Errore nel recuperare l'ultima notizia. Riprova più tardi.")

def handle_next_command(chat_id: int, last_news_check: float):
    """Gestisce il comando /next"""
    current_time = time.time()
    time_since_last_check = current_time - last_news_check
    time_remaining = max(0, NEWS_INTERVAL - time_since_last_check)
    
    if time_remaining <= 0:
        text = "⏰ <b>Prossimo controllo imminente!</b>\n\n" \
               "🔄 Il controllo automatico dovrebbe iniziare a momenti."
    else:
        remaining_formatted = format_time_remaining(int(time_remaining))
        last_check_time = datetime.fromtimestamp(last_news_check).strftime('%H:%M:%S')
        
        text = f"⏱️ <b>Prossimo controllo automatico:</b>\n\n" \
               f"{remaining_formatted}\n\n" \
               f"📅 Ultimo controllo: {last_check_time}\n" \
               f"🔄 Intervallo: ogni {NEWS_INTERVAL//60} minuti\n\n" \
               f"💡 Usa /force per controllare subito"
    
    send_telegram_message(chat_id, text)

def handle_force_command(chat_id: int, seen: List[str], subscribers: List[int], stats: BotStats) -> List[str]:
    """Gestisce il comando /force"""
    send_telegram_message(chat_id, "🚀 Controllo forzato in corso...")
    
    try:
        seen_before = seen.copy()
        seen = check_once(seen, subscribers, stats)
        
        if seen == seen_before:
            text = "✅ <b>Controllo completato!</b>\n\n" \
                   "📰 Nessuna nuova notizia trovata.\n" \
                   "🔄 Il bot continua a monitorare automaticamente."
        else:
            text = "🎉 <b>Controllo completato!</b>\n\n" \
                   "📢 Nuova notizia trovata e inviata a tutti gli iscritti!"
        
        send_telegram_message(chat_id, text)
    except Exception as e:
        logging.error("Errore /force: %s", e)
        send_telegram_message(chat_id, "⚠️ Errore durante il controllo forzato. Riprova più tardi.")
    
    return seen

def handle_stats_command(chat_id: int, subscribers: List[int], stats: BotStats):
    """Gestisce il comando /stats con info extra per Replit"""
    current_time = time.time()
    uptime_seconds = int(current_time - stats.start_time)
    uptime_formatted = format_duration(uptime_seconds)
    
    start_date = datetime.fromtimestamp(stats.start_time).strftime('%d/%m/%Y %H:%M')
    
    last_news_text = "Nessuna news inviata ancora"
    if stats.last_news_time:
        last_news_date = datetime.fromtimestamp(stats.last_news_time).strftime('%d/%m/%Y %H:%M')
        last_news_text = f"{last_news_date}"
    
    text = f"📊 <b>Statistiche Bot MiM Watcher</b>\n\n" \
           f"🚀 <b>Avviato:</b> {start_date}\n" \
           f"⏰ <b>Uptime:</b> {uptime_formatted}\n" \
           f"👥 <b>Utenti iscritti:</b> {len(subscribers)}\n" \
           f"📰 <b>News inviate:</b> {stats.total_news_sent}\n" \
           f"⌨️ <b>Comandi processati:</b> {stats.total_commands_processed}\n" \
           f"🕐 <b>Ultima news:</b> {last_news_text}\n" \
           f"🔄 <b>Intervallo controlli:</b> {NEWS_INTERVAL//60} minuti\n" \
           f"🌐 <b>Keep-alive hits:</b> {stats.keep_alive_hits}\n\n" \
           f"💡 Il bot sta monitorando USR Lombardia del MiM!\n" \
           f"🔧 Hosting: Replit.com (sempre attivo)"
    
    send_telegram_message(chat_id, text)

def poll_updates(offset: Optional[int], subscribers: List[int], seen: List[str], stats: BotStats, last_news_check: float) -> tuple[Optional[int], List[int], List[str]]:
    if not TELEGRAM_API:
        return offset, subscribers, seen
    
    try:
        r = requests.get(f"{TELEGRAM_API}/getUpdates", params={"offset": offset or 0, "timeout": 10}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            return offset, subscribers, seen

        for update in data.get("result", []):
            offset = update["update_id"] + 1
            msg = update.get("message")
            if not msg:
                continue
            
            chat_id = msg["chat"]["id"]
            text = msg.get("text", "").strip().lower()
            
            # Incrementa il contatore comandi
            stats.total_commands_processed += 1

            if text == "/start":
                subscribers = handle_start_command(chat_id, subscribers)
            
            elif text == "/stop":
                subscribers = handle_stop_command(chat_id, subscribers)
            
            elif text == "/help":
                handle_help_command(chat_id)
            
            elif text == "/last":
                handle_last_command(chat_id)
            
            elif text == "/next":
                handle_next_command(chat_id, last_news_check)
            
            elif text == "/force":
                seen = handle_force_command(chat_id, seen, subscribers, stats)
            
            elif text == "/stats":
                handle_stats_command(chat_id, subscribers, stats)
            
            else:
                # Comando non riconosciuto
                unknown_text = f"❓ Comando non riconosciuto: <code>{escape_html(text)}</code>\n\n" \
                              f"📝 Usa /help per vedere tutti i comandi disponibili"
                send_telegram_message(chat_id, unknown_text)

        return offset, subscribers, seen
        
    except Exception as e:
        logging.error("Errore poll_updates: %s", e)
        return offset, subscribers, seen

def main() -> None:
    global bot_start_time
    setup_logging()

    if not TELEGRAM_BOT_TOKEN:
        logging.error("Config mancante. Imposta TELEGRAM_BOT_TOKEN nelle variabili ambiente o file .env")
        return

    # Avvia il keep-alive server per Replit
    bot_start_time = time.time()
    keep_alive_server = start_keep_alive_server()

    seen = load_json(STATE_FILE, [])
    subscribers = load_json(SUBSCRIBERS_FILE, [])
    
    # Carica o crea le statistiche
    stats_data = load_json(STATS_FILE, None)
    if stats_data:
        stats = BotStats.from_dict(stats_data)
        # Aggiorna start_time se è un nuovo avvio
        stats.start_time = bot_start_time
    else:
        stats = BotStats(start_time=bot_start_time)
    
    save_json(STATS_FILE, stats.to_dict())
    offset = None

    logging.info("🚀 MiM Watcher avviato su Replit!")
    logging.info("⏰ Controllo notizie ogni %s secondi", NEWS_INTERVAL)
    logging.info("📱 Polling Telegram ogni %s secondi", TELEGRAM_POLL_INTERVAL)
    logging.info("👥 Utenti iscritti: %s", len(subscribers))
    logging.info("🌐 Keep-alive server su porta %s", KEEP_ALIVE_PORT)
    logging.info("📍 URL keep-alive: https://{repl_name}.{username}.repl.co")

    last_news_check = 0
    while True:
        try:
            # Poll Telegram frequentemente
            offset, subscribers, seen = poll_updates(offset, subscribers, seen, stats, last_news_check)

            # Controllo pagina ogni NEWS_INTERVAL
            now = time.time()
            if now - last_news_check >= NEWS_INTERVAL:
                logging.info("🔄 Inizio controllo automatico notizie...")
                seen = check_once(seen, subscribers, stats)
                last_news_check = now

            time.sleep(TELEGRAM_POLL_INTERVAL)
            
        except KeyboardInterrupt:
            logging.info("⏹️ Interrotto dall'utente. Uscita...")
            if keep_alive_server:
                keep_alive_server.shutdown()
            break
        except Exception as e:
            logging.exception("❌ Errore inatteso nel loop: %s", e)
            stats.last_error_time = time.time()
            time.sleep(TELEGRAM_POLL_INTERVAL)

if __name__ == "__main__":
    main()