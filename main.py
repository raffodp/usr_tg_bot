#!/usr/bin/env python3
"""
Telegram bot che controlla la pagina USR Lombardia del MiM ogni 30 minuti
e invia notifiche quando compare una nuova notizia.

Versione semplificata per Render.com - usa solo variabili d'ambiente.

Funzionalità:
- Gestione iscrizioni multiple (/start, /stop, /help)
- Polling Telegram ogni 5 secondi
- Controllo notizie ogni 30 minuti
- Storage solo in memoria con variabili d'ambiente

Uso:
1) Python 3.10+
2) pip install -U requests beautifulsoup4 python-dotenv
3) Variabile ambiente: TELEGRAM_BOT_TOKEN
4) Avvia: python main.py
"""
from __future__ import annotations

import os
import time
import json
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, List
from urllib.parse import urljoin
from datetime import datetime
from threading import Lock, Thread
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
NEWS_INTERVAL = int(os.environ.get("NEWS_INTERVAL", 1800))  # default 30 min
TELEGRAM_POLL_INTERVAL = 5  # 5 secondi fisso
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else None
USER_AGENT = "Mozilla/5.0 (compatible; MiMWatcher/1.0)"
REQUEST_TIMEOUT = 20  # 20 secondi fisso
HTTP_PORT = int(os.environ.get("PORT", 8000))  # Porta per Render
# ==================================================

# =================== STORAGE MANAGER ===================
class SimpleStorageManager:
    """Gestisce i dati utilizzando solo file JSON"""
    
    def __init__(self):
        self._subscribers = set()
        self._seen_news = []
        self._stats = None
        self._lock = Lock()
        self._load_data()
    
    def _load_data(self):
        """Carica i dati dai file JSON"""
        try:
            self._load_from_files()
        except Exception as e:
            logging.warning("⚠️ Errore caricamento dati: %s", e)
    
    def _load_from_files(self):
        """Carica i dati dai file JSON"""
        try:
            # Carica subscribers
            if Path('subscribers.json').exists():
                with open('subscribers.json', 'r') as f:
                    self._subscribers = set(json.load(f))
                    logging.info("📥 Caricati %d subscribers da JSON", len(self._subscribers))
            
            # Carica seen news
            if Path('seen.json').exists():
                with open('seen.json', 'r') as f:
                    self._seen_news = json.load(f)[:50]
                    logging.info("📥 Caricate %d notizie viste da JSON", len(self._seen_news))
                    
            # Carica stats
            if Path('stats.json').exists():
                with open('stats.json', 'r') as f:
                    stats_data = json.load(f)
                    self._stats = BotStats(**stats_data)
                    
        except Exception as e:
            logging.warning("⚠️ Errore caricamento file JSON: %s", e)
    
    
    def _save_to_files(self):
        """Salva i dati nei file JSON"""
        try:
            # Salva subscribers
            with open('subscribers.json', 'w') as f:
                json.dump(list(self._subscribers), f)
            
            # Salva seen news
            with open('seen.json', 'w') as f:
                json.dump(self._seen_news, f)
            
            # Salva stats se esistono
            if self._stats:
                with open('stats.json', 'w') as f:
                    json.dump({
                        'start_time': self._stats.start_time,
                        'total_news_sent': self._stats.total_news_sent,
                        'total_commands_processed': self._stats.total_commands_processed,
                        'last_news_time': self._stats.last_news_time,
                        'last_error_time': self._stats.last_error_time
                    }, f)
                    
        except Exception as e:
            logging.warning("⚠️ Errore salvataggio file JSON: %s", e)
    
    def add_subscriber(self, chat_id: int) -> bool:
        """Aggiunge un nuovo iscritto"""
        with self._lock:
            if chat_id not in self._subscribers:
                self._subscribers.add(chat_id)
                self._save_to_files()
                logging.info("✅ Nuovo iscritto aggiunto: %s", chat_id)
                return True
            return False
    
    def remove_subscriber(self, chat_id: int) -> bool:
        """Rimuove un iscritto"""
        with self._lock:
            if chat_id in self._subscribers:
                self._subscribers.remove(chat_id)
                self._save_to_files()
                logging.info("✅ Iscritto rimosso: %s", chat_id)
                return True
            return False
    
    def get_subscribers(self) -> List[int]:
        """Ottiene tutti gli iscritti"""
        with self._lock:
            return list(self._subscribers)
    
    def is_subscriber(self, chat_id: int) -> bool:
        """Verifica se un utente è iscritto"""
        with self._lock:
            return chat_id in self._subscribers
    
    def add_seen_news(self, news_key: str) -> bool:
        """Aggiunge una notizia alla lista delle viste"""
        with self._lock:
            if news_key not in self._seen_news:
                self._seen_news.insert(0, news_key)
                self._seen_news = self._seen_news[:50]
                self._save_to_files()
                return True
            return False
    
    def is_news_seen(self, news_key: str) -> bool:
        """Verifica se una notizia è già stata vista"""
        with self._lock:
            return news_key in self._seen_news
    
    def get_stats(self) -> Optional['BotStats']:
        """Recupera le statistiche (solo in memoria per questa versione)"""
        return self._stats
    
    def save_stats(self, stats: 'BotStats'):
        """Salva le statistiche (solo in memoria)"""
        with self._lock:
            self._stats = stats
    
    def get_summary(self) -> dict:
        """Ritorna un riassunto dello stato attuale"""
        with self._lock:
            return {
                "subscribers_count": len(self._subscribers),
                "seen_news_count": len(self._seen_news),
                "last_news": self._seen_news[0] if self._seen_news else None
            }

# =========================================================


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

class HealthHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler for Render health checks"""
    def do_GET(self):
        if self.path == '/' or self.path == '/health':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            response = {
                'status': 'ok',
                'service': 'telegram-bot',
                'timestamp': datetime.now().isoformat()
            }
            self.wfile.write(json.dumps(response).encode())
        else:
            self.send_response(404)
            self.end_headers()
    
    def log_message(self, format, *args):
        # Disable HTTP server logs to reduce noise
        pass

def setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

def start_http_server():
    """Avvia il server HTTP per soddisfare i requisiti di Render"""
    try:
        server = HTTPServer(('0.0.0.0', HTTP_PORT), HealthHandler)
        logging.info("🌐 Server HTTP avviato sulla porta %d per Render", HTTP_PORT)
        server.serve_forever()
    except Exception as e:
        logging.error("❌ Errore server HTTP: %s", e)

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

def is_valid_news_url(href: str) -> bool:
    """Verifica se un URL è una notizia valida"""
    if not href or not href.strip():
        return False
    
    href = href.strip()
    
    if href.startswith('#'):
        return False
    
    invalid_patterns = [
        'javascript:',
        'mailto:',
        '#content',
        '#tab',
        'cookie-policy',
        'privacy-policy',
        'accessibilita',
        '/cerca',
        '/search'
    ]
    
    href_lower = href.lower()
    for pattern in invalid_patterns:
        if pattern in href_lower:
            return False
    
    return True

def is_valid_news_title(title: str) -> bool:
    """Verifica se un titolo è valido per una notizia"""
    if not title or not title.strip():
        return False
    
    title = title.strip()
    
    if len(title) < 10:
        return False
    
    invalid_titles = [
        'home',
        'cerca',
        'contatti',
        'privacy',
        'cookie',
        'accessibilità',
        'vai al contenuto',
        'menu principale',
        'ultime comunicazioni'
    ]
    
    title_lower = title.lower()
    for invalid in invalid_titles:
        if title_lower == invalid or invalid in title_lower:
            return False
    
    return True

def parse_latest_item(html: str) -> Optional[NewsItem]:
    """Parser migliorato che filtra meglio le notizie valide"""
    soup = BeautifulSoup(html, "html.parser")
    
    container = soup.find(id=lambda x: isinstance(x, str) and x.strip().startswith("tab-container"))
    if not container:
        container = soup
    
    candidate_selectors = [
        "li.asset-tab-home a[href]",
        "li.bg_today a[href]", 
        ".news-item a[href]",
        ".comunicazione a[href]",
        "article a[href]",
        "li a[href]",
        "a[href]"
    ]
    
    for selector in candidate_selectors:
        links = container.select(selector)
        
        for a in links:
            title = a.get_text(strip=True)
            href = a.get("href", "").strip()
            
            if not is_valid_news_url(href):
                continue
                
            if not is_valid_news_title(title):
                continue
            
            abs_url = urljoin(MIM_URL, href)
            
            logging.info("✅ Notizia valida trovata: %s -> %s", title[:50], href[:50])
            return NewsItem(title=title, url=abs_url, raw_href=href)
    
    logging.warning("❌ Nessuna notizia valida trovata nel parser")
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

def broadcast(item: NewsItem, storage: SimpleStorageManager, stats: BotStats) -> None:
    """Invia la notifica a tutti gli iscritti"""
    subscribers = storage.get_subscribers()
    successful_sends = 0
    
    for chat_id in subscribers:
        if send_news_notification(item, chat_id):
            successful_sends += 1
            logging.info("Notifica inviata a %s: %s", chat_id, item.title)
        else:
            logging.warning("Fallito invio a %s", chat_id)
    
    stats.total_news_sent += successful_sends
    stats.last_news_time = time.time()
    storage.save_stats(stats)

def check_once(storage: SimpleStorageManager, stats: BotStats) -> None:
    """Controlla una volta le notizie"""
    try:
        html = fetch_page(MIM_URL)
    except Exception as e:
        logging.error("Errore fetch pagina: %s", e)
        stats.last_error_time = time.time()
        storage.save_stats(stats)
        return

    item = parse_latest_item(html)
    if not item:
        logging.warning("Nessun elemento valido trovato nel tab-container.")
        return

    if storage.is_news_seen(item.key):
        logging.info("Nessuna novità. Ultimo già visto: %s", item.title)
        return

    logging.info("🆕 Nuova notizia trovata: %s", item.title)
    
    # Salva la notizia come vista
    storage.add_seen_news(item.key)
    
    # Invia a tutti gli iscritti
    broadcast(item, storage, stats)

# =================== COMMAND HANDLERS ===================

def handle_start_command(chat_id: int, storage: SimpleStorageManager, send_welcome_news: bool = True) -> bool:
    """Gestisce il comando /start"""
    if storage.add_subscriber(chat_id):
        logging.info("Nuovo iscritto: %s", chat_id)
        
        subscriber_count = len(storage.get_subscribers())
        welcome_text = f"🎉 <b>Benvenuto nel MiM Watcher!</b>\n\n" \
                      f"📢 Riceverai notifiche automatiche ogni volta che viene pubblicata una nuova notizia su USR Lombardia.\n\n" \
                      f"👥 <b>Iscritti totali:</b> {subscriber_count}\n\n" \
                      f"⚙️ <b>Comandi disponibili:</b>\n" \
                      f"• /help - Mostra tutti i comandi\n" \
                      f"• /last - Mostra l'ultima notizia\n" \
                      f"• /next - Quando sarà il prossimo controllo\n" \
                      f"• /stats - Statistiche del bot\n" \
                      f"• /stop - Cancella iscrizione\n\n" \
                      f"🔄 Controllo notizie ogni {NEWS_INTERVAL//60} minuti\n" \
                      f"💾 Dati salvati in JSON (persistenti)"
        
        send_telegram_message(chat_id, welcome_text)
        
        # Invia l'ultima notizia disponibile come benvenuto
        if send_welcome_news:
            send_telegram_message(chat_id, "🔍 Ti mostro subito l'ultima notizia disponibile...")
            time.sleep(1)
            
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
        
        return True
    else:
        subscriber_count = len(storage.get_subscribers())
        already_text = f"✅ Sei già iscritto alle notifiche!\n\n" \
                      f"👥 Iscritti totali: {subscriber_count}\n" \
                      f"🔄 Controllo automatico ogni {NEWS_INTERVAL//60} minuti\n" \
                      f"📝 Usa /help per vedere tutti i comandi"
        send_telegram_message(chat_id, already_text)
        return False

def handle_stop_command(chat_id: int, storage: SimpleStorageManager) -> bool:
    """Gestisce il comando /stop"""
    if storage.remove_subscriber(chat_id):
        logging.info("Utente %s disiscritto.", chat_id)
        
        remaining_count = len(storage.get_subscribers())
        bye_text = f"👋 <b>Iscrizione cancellata!</b>\n\n" \
                  f"❌ Non riceverai più notifiche dalle news USR Lombardia.\n\n" \
                  f"👥 Iscritti rimasti: {remaining_count}\n\n" \
                  f"🔄 Puoi sempre riscriverti con /start"
        
        send_telegram_message(chat_id, bye_text)
        return True
    else:
        not_subscribed_text = f"ℹ️ Non risulti iscritto alle notifiche.\n\n" \
                             f"📝 Usa /start per iscriverti"
        send_telegram_message(chat_id, not_subscribed_text)
        return False

def handle_help_command(chat_id: int, storage: SimpleStorageManager):
    """Gestisce il comando /help"""
    subscriber_count = len(storage.get_subscribers())
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
               f"👥 <b>Community:</b> {subscriber_count} iscritti attivi\n" \
               f"💾 <b>Storage:</b> Dati temporanei in memoria\n" \
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

def handle_force_command(chat_id: int, storage: SimpleStorageManager, stats: BotStats):
    """Gestisce il comando /force"""
    send_telegram_message(chat_id, "🚀 Controllo forzato in corso...")
    
    try:
        check_once(storage, stats)
        
        text = "✅ <b>Controllo forzato completato!</b>\n\n" \
               "📰 Se sono state trovate nuove notizie, sono state inviate a tutti gli iscritti.\n" \
               "🔄 Il bot continua a monitorare automaticamente."
        
        send_telegram_message(chat_id, text)
    except Exception as e:
        logging.error("Errore /force: %s", e)
        send_telegram_message(chat_id, "⚠️ Errore durante il controllo forzato. Riprova più tardi.")

def handle_stats_command(chat_id: int, storage: SimpleStorageManager, stats: BotStats):
    """Gestisce il comando /stats con informazioni dal storage temporaneo"""
    current_time = time.time()
    uptime_seconds = int(current_time - stats.start_time)
    uptime_formatted = format_duration(uptime_seconds)
    
    start_date = datetime.fromtimestamp(stats.start_time).strftime('%d/%m/%Y %H:%M')
    
    last_news_text = "Nessuna news inviata ancora"
    if stats.last_news_time:
        last_news_date = datetime.fromtimestamp(stats.last_news_time).strftime('%d/%m/%Y %H:%M')
        last_news_text = f"{last_news_date}"
    
    summary = storage.get_summary()
    subscriber_count = summary['subscribers_count']
    seen_count = summary['seen_news_count']
    
    text = f"📊 <b>Statistiche Bot MiM Watcher</b>\n\n" \
           f"🚀 <b>Avviato:</b> {start_date}\n" \
           f"⏰ <b>Uptime:</b> {uptime_formatted}\n" \
           f"👥 <b>Utenti iscritti:</b> {subscriber_count}\n" \
           f"📰 <b>News inviate:</b> {stats.total_news_sent}\n" \
           f"⌨️ <b>Comandi processati:</b> {stats.total_commands_processed}\n" \
           f"🕐 <b>Ultima news:</b> {last_news_text}\n" \
           f"📄 <b>Notizie memorizzate:</b> {seen_count}\n" \
           f"🔄 <b>Intervallo controlli:</b> {NEWS_INTERVAL//60} minuti\n" \
           f"💾 <b>Storage:</b> Solo file JSON\n" \
           f"✅ <b>Nota:</b> Dati persistenti tra restart\n\n" \
           f"💡 Il bot sta monitorando USR Lombardia del MiM!\n" \
           f"🚀 Hosting: Render.com con persistenza JSON"
    
    send_telegram_message(chat_id, text)

def poll_updates(offset: Optional[int], storage: SimpleStorageManager, stats: BotStats, last_news_check: float) -> Optional[int]:
    """Polling aggiornato per usare lo storage temporaneo"""
    if not TELEGRAM_API:
        return offset
    
    try:
        r = requests.get(f"{TELEGRAM_API}/getUpdates", params={"offset": offset or 0, "timeout": 10}, timeout=REQUEST_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            return offset

        for update in data.get("result", []):
            offset = update["update_id"] + 1
            msg = update.get("message")
            if not msg:
                continue
            
            chat_id = msg["chat"]["id"]
            text = msg.get("text", "").strip().lower()
            
            # Incrementa il contatore comandi
            stats.total_commands_processed += 1
            storage.save_stats(stats)

            if text == "/start":
                handle_start_command(chat_id, storage)
            
            elif text == "/stop":
                handle_stop_command(chat_id, storage)
            
            elif text == "/help":
                handle_help_command(chat_id, storage)
            
            elif text == "/last":
                handle_last_command(chat_id)
            
            elif text == "/next":
                handle_next_command(chat_id, last_news_check)
            
            elif text == "/force":
                handle_force_command(chat_id, storage, stats)
            
            elif text == "/stats":
                handle_stats_command(chat_id, storage, stats)
            
            else:
                # Comando non riconosciuto
                unknown_text = f"❓ Comando non riconosciuto: <code>{escape_html(text)}</code>\n\n" \
                              f"📝 Usa /help per vedere tutti i comandi disponibili"
                send_telegram_message(chat_id, unknown_text)

        return offset
        
    except Exception as e:
        logging.error("Errore poll_updates: %s", e)
        return offset


def main() -> None:
    global bot_start_time
    setup_logging()

    if not TELEGRAM_BOT_TOKEN:
        logging.error("❌ Config mancante. Imposta TELEGRAM_BOT_TOKEN nelle variabili ambiente")
        return

    # Inizializza lo storage manager semplificato
    try:
        storage = SimpleStorageManager()
        logging.info("✅ Storage manager semplificato inizializzato")
    except Exception as e:
        logging.error("❌ Impossibile inizializzare lo storage: %s", e)
        return

    # Crea le statistiche
    bot_start_time = time.time()
    stats = BotStats(start_time=bot_start_time)
    storage.save_stats(stats)


    # Avvia il server HTTP in un thread separato per Render
    http_thread = Thread(target=start_http_server, daemon=True)
    http_thread.start()

    offset = None

    logging.info("🚀 MiM Watcher avviato su Render.com!")
    logging.info("🌐 Server HTTP sulla porta %d", HTTP_PORT)
    logging.info("⏰ Controllo notizie ogni %s secondi", NEWS_INTERVAL)
    logging.info("📱 Polling Telegram ogni %s secondi", TELEGRAM_POLL_INTERVAL)
    logging.info("👥 Utenti iscritti: %s", len(storage.get_subscribers()))
    logging.info("💾 Storage: Solo file JSON")
    logging.info("✅ Dati persistenti tra restart")

    last_news_check = 0
    
    while True:
        try:
            # Poll Telegram frequentemente
            offset = poll_updates(offset, storage, stats, last_news_check)

            # Controllo pagina ogni NEWS_INTERVAL
            now = time.time()
            if now - last_news_check >= NEWS_INTERVAL:
                logging.info("🔄 Inizio controllo automatico notizie...")
                check_once(storage, stats)
                last_news_check = now


            time.sleep(TELEGRAM_POLL_INTERVAL)
            
        except KeyboardInterrupt:
            logging.info("⏹️ Interrotto dall'utente.")
            break
        except Exception as e:
            logging.exception("❌ Errore inatteso nel loop: %s", e)
            stats.last_error_time = time.time()
            storage.save_stats(stats)
            time.sleep(TELEGRAM_POLL_INTERVAL)

if __name__ == "__main__":
    main()