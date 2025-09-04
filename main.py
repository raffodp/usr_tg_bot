#!/usr/bin/env python3
"""
Telegram bot/daemon che controlla la pagina USR Lombardia del MiM ogni 30 minuti
(e invia una notifica quando compare una nuova notizia nel tab-container).

Versione ottimizzata per Render.com SENZA database - usa variabili d'ambiente e storage temporaneo.

Funzionalità:
- Gestione iscrizioni multiple (/start, /stop, /help).
- Polling Telegram frequente (ogni 5s) per comandi quasi in tempo reale.
- Controllo notizie separato ogni 30 minuti (configurabile).
- Dati persistenti tramite variabili d'ambiente e storage temporaneo.
- Keep-alive server per Render.com (rimane sempre attivo).

Uso:
1) Python 3.10+
2) pip install -U requests beautifulsoup4 python-dotenv
3) Variabili ambiente: TELEGRAM_BOT_TOKEN, RENDER_API_KEY (opzionale)
4) Avvia: python telegram_mim_watcher.py
"""
from __future__ import annotations

import os
import time
import json
import logging
import tempfile
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, List
from urllib.parse import urljoin
from datetime import datetime, timedelta
from threading import Thread, Lock

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
TELEGRAM_POLL_INTERVAL = int(os.environ.get("TELEGRAM_POLL_INTERVAL", 5))  # default 5 sec
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}" if TELEGRAM_BOT_TOKEN else None
USER_AGENT = os.environ.get("USER_AGENT", "Mozilla/5.0 (compatible; MiMWatcher/1.0)")
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", 20))
KEEP_ALIVE_PORT = int(os.environ.get("PORT", 8000))  # Render usa PORT
RENDER_API_KEY = os.environ.get("RENDER_API_KEY", "")  # Opzionale per persistenza avanzata
# ==================================================

# =================== STORAGE MANAGER ===================
class InMemoryStorageManager:
    """Gestisce i dati in memoria con backup su variabili d'ambiente"""
    
    def __init__(self):
        self._subscribers = set()
        self._seen_news = []
        self._stats = None
        self._lock = Lock()
        self.temp_dir = Path(tempfile.gettempdir()) / "mimwatcher"
        self.temp_dir.mkdir(exist_ok=True)
        self._load_from_env_and_temp()
    
    def _load_from_env_and_temp(self):
        """Carica i dati dalle variabili d'ambiente e file temporanei"""
        try:
            # Carica subscribers dalle variabili d'ambiente
            subscribers_env = os.environ.get("MIMI_SUBSCRIBERS", "")
            if subscribers_env:
                self._subscribers = set(map(int, subscribers_env.split(",")))
                logging.info("📥 Caricati %d subscribers dalle env vars", len(self._subscribers))
            
            # Carica seen news dalle variabili d'ambiente (solo le più recenti)
            seen_env = os.environ.get("MIMI_SEEN_NEWS", "")
            if seen_env:
                self._seen_news = seen_env.split(",")[:50]  # Max 50 elementi
                logging.info("📥 Caricate %d notizie viste dalle env vars", len(self._seen_news))
            
            # Prova a caricare dai file temporanei come backup
            self._load_from_temp_files()
            
        except Exception as e:
            logging.warning("⚠️ Errore caricamento dati dalle env vars: %s", e)
    
    def _load_from_temp_files(self):
        """Carica i dati dai file temporanei del sistema"""
        try:
            # File subscribers
            subs_file = self.temp_dir / "subscribers.json"
            if subs_file.exists():
                with open(subs_file, 'r') as f:
                    temp_subs = set(json.load(f))
                    if len(temp_subs) > len(self._subscribers):
                        self._subscribers.update(temp_subs)
                        logging.info("📥 Aggiornati subscribers da file temp: %d totali", len(self._subscribers))
            
            # File seen news
            seen_file = self.temp_dir / "seen.json"
            if seen_file.exists():
                with open(seen_file, 'r') as f:
                    temp_seen = json.load(f)
                    if len(temp_seen) > len(self._seen_news):
                        self._seen_news = temp_seen[:50]
                        logging.info("📥 Aggiornate notizie viste da file temp: %d totali", len(self._seen_news))
        
        except Exception as e:
            logging.warning("⚠️ Errore caricamento file temporanei: %s", e)
    
    def _save_to_temp_files(self):
        """Salva i dati nei file temporanei come backup"""
        try:
            # Salva subscribers
            with open(self.temp_dir / "subscribers.json", 'w') as f:
                json.dump(list(self._subscribers), f)
            
            # Salva seen news
            with open(self.temp_dir / "seen.json", 'w') as f:
                json.dump(self._seen_news, f)
                
        except Exception as e:
            logging.warning("Errore salvataggio file temporanei: %s", e)
    
    def add_subscriber(self, chat_id: int) -> bool:
        """Aggiunge un nuovo iscritto"""
        with self._lock:
            if chat_id not in self._subscribers:
                self._subscribers.add(chat_id)
                self._save_to_temp_files()
                logging.info("✅ Nuovo iscritto aggiunto: %s", chat_id)
                return True
            return False
    
    def remove_subscriber(self, chat_id: int) -> bool:
        """Rimuove un iscritto"""
        with self._lock:
            if chat_id in self._subscribers:
                self._subscribers.remove(chat_id)
                self._save_to_temp_files()
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
                self._seen_news = self._seen_news[:50]  # Mantieni solo le più recenti
                self._save_to_temp_files()
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

# =================== KEEP ALIVE SERVER ===================
class KeepAliveHandler(BaseHTTPRequestHandler):
    """Handler per il server keep-alive che mantiene Render attivo"""
    
    def do_GET(self):
        # Incrementa il contatore keep-alive
        if hasattr(self.server, 'storage') and hasattr(self.server, 'stats'):
            self.server.stats.keep_alive_hits += 1
            self.server.storage.save_stats(self.server.stats)
        
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        
        # Pagina web con info sul bot
        uptime_seconds = int(time.time() - bot_start_time) if 'bot_start_time' in globals() else 0
        uptime_formatted = format_duration(uptime_seconds)
        
        # Ottieni statistiche dallo storage
        summary = self.server.storage.get_summary() if hasattr(self.server, 'storage') else {}
        subscriber_count = summary.get('subscribers_count', 0)
        seen_count = summary.get('seen_news_count', 0)
        
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
        .warning {{ background: #fff3cd; padding: 15px; border-radius: 5px; margin: 20px 0; color: #856404; }}
        .emoji {{ font-size: 1.2em; }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🤖 MiM Watcher Bot</h1>
        <p class="status">✅ Bot attivo su Render.com!</p>
        
        <div class="warning">
            <h3>⚠️ Modalità Storage Temporaneo</h3>
            <p>I dati sono archiviati in memoria e file temporanei. Per persistenza completa considera l'uso di un database.</p>
        </div>
        
        <div class="info">
            <h3>📊 Statistiche Live</h3>
            <p><span class="emoji">⏰</span> <strong>Uptime:</strong> {uptime_formatted}</p>
            <p><span class="emoji">👥</span> <strong>Iscritti:</strong> {subscriber_count}</p>
            <p><span class="emoji">📰</span> <strong>Notizie viste:</strong> {seen_count}</p>
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
        
        <div class="info">
            <h3>💾 Archiviazione Dati</h3>
            <p>📁 File temporanei del sistema</p>
            <p>🔄 Backup automatico ogni operazione</p>
            <p>⚠️ Dati potrebbero essere persi al restart (usa database per persistenza completa)</p>
        </div>
        
        <p><small>🚀 Hosting leggero su Render.com senza database</small></p>
    </div>
</body>
</html>
        """
        self.wfile.write(html.encode())
    
    def log_message(self, format, *args):
        # Disabilita i log del server HTTP
        pass

def start_keep_alive_server(storage_manager, stats):
    """Avvia il server keep-alive in un thread separato"""
    try:
        server = HTTPServer(('0.0.0.0', KEEP_ALIVE_PORT), KeepAliveHandler)
        server.storage = storage_manager
        server.stats = stats
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

def setup_logging() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

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

def broadcast(item: NewsItem, storage: InMemoryStorageManager, stats: BotStats) -> None:
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

def check_once(storage: InMemoryStorageManager, stats: BotStats) -> None:
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

def handle_start_command(chat_id: int, storage: InMemoryStorageManager, send_welcome_news: bool = True) -> bool:
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
                      f"💾 Dati archiviati in memoria (temporaneo)"
        
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

def handle_stop_command(chat_id: int, storage: InMemoryStorageManager) -> bool:
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

def handle_help_command(chat_id: int, storage: InMemoryStorageManager):
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

def handle_force_command(chat_id: int, storage: InMemoryStorageManager, stats: BotStats):
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

def handle_stats_command(chat_id: int, storage: InMemoryStorageManager, stats: BotStats):
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
           f"🌐 <b>Keep-alive hits:</b> {stats.keep_alive_hits}\n\n" \
           f"💾 <b>Storage:</b> Memoria + file temporanei\n" \
           f"⚠️ <b>Nota:</b> Dati temporanei (restart = reset)\n\n" \
           f"💡 Il bot sta monitorando USR Lombardia del MiM!\n" \
           f"🚀 Hosting: Render.com (leggero, senza database)"
    
    send_telegram_message(chat_id, text)

def poll_updates(offset: Optional[int], storage: InMemoryStorageManager, stats: BotStats, last_news_check: float) -> Optional[int]:
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

def cleanup_and_backup_periodically(storage: InMemoryStorageManager):
    """Esegue pulizia e backup periodici (opzionale)"""
    try:
        # Questa funzione può essere chiamata periodicamente per fare backup
        # Per ora salva solo nei file temporanei
        storage._save_to_temp_files()
        logging.info("🧹 Backup periodico completato")
    except Exception as e:
        logging.warning("⚠️ Errore backup periodico: %s", e)

def main() -> None:
    global bot_start_time
    setup_logging()

    if not TELEGRAM_BOT_TOKEN:
        logging.error("❌ Config mancante. Imposta TELEGRAM_BOT_TOKEN nelle variabili ambiente")
        return

    # Inizializza lo storage manager in memoria
    try:
        storage = InMemoryStorageManager()
        logging.info("✅ Storage manager in memoria inizializzato")
    except Exception as e:
        logging.error("❌ Impossibile inizializzare lo storage: %s", e)
        return

    # Crea le statistiche
    bot_start_time = time.time()
    stats = BotStats(start_time=bot_start_time)
    storage.save_stats(stats)

    # Avvia il keep-alive server per Render
    keep_alive_server = start_keep_alive_server(storage, stats)

    offset = None

    logging.info("🚀 MiM Watcher avviato su Render.com (modalità leggera)!")
    logging.info("⏰ Controllo notizie ogni %s secondi", NEWS_INTERVAL)
    logging.info("📱 Polling Telegram ogni %s secondi", TELEGRAM_POLL_INTERVAL)
    logging.info("👥 Utenti iscritti: %s", len(storage.get_subscribers()))
    logging.info("🌐 Keep-alive server su porta %s", KEEP_ALIVE_PORT)
    logging.info("💾 Storage: Memoria temporanea + file temporanei")
    logging.info("⚠️ Dati potrebbero essere persi al restart del servizio")
    
    if RENDER_API_KEY:
        logging.info("🔑 RENDER_API_KEY configurata (backup avanzato disponibile)")
    else:
        logging.info("💡 Considera l'aggiunta di RENDER_API_KEY per funzionalità avanzate")

    last_news_check = 0
    last_backup = time.time()
    
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

            # Backup periodico ogni 10 minuti
            if now - last_backup >= 600:  # 10 minuti
                cleanup_and_backup_periodically(storage)
                last_backup = now

            time.sleep(TELEGRAM_POLL_INTERVAL)
            
        except KeyboardInterrupt:
            logging.info("⏹️ Interrotto dall'utente. Salvataggio finale...")
            storage._save_to_temp_files()
            if keep_alive_server:
                keep_alive_server.shutdown()
            break
        except Exception as e:
            logging.exception("❌ Errore inatteso nel loop: %s", e)
            stats.last_error_time = time.time()
            storage.save_stats(stats)
            time.sleep(TELEGRAM_POLL_INTERVAL)

if __name__ == "__main__":
    main()