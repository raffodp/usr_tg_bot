# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a simplified Python Telegram bot that monitors news from the USR Lombardia section of the Italian Ministry of Education website (MiM). The bot checks for new publications every 30 minutes and sends notifications to subscribed users.

## Key Architecture

### Main Components
- **main.py**: Single-file bot implementation with all functionality
- **SimpleStorageManager**: Handles user subscriptions and seen news tracking using only JSON files
- **Telegram Bot Commands**: `/start`, `/stop`, `/help`, `/last`, `/next`, `/stats`, `/force`

### Storage System
- **JSON-only approach**: Uses only JSON files for data persistence
- SimpleStorageManager class provides thread-safe operations
- JSON files: `subscribers.json`, `seen.json`, `stats.json`
- **Full persistence** across container restarts
- No environment variables needed for data storage

### Web Scraping
- Targets `https://www.mim.gov.it/web/usr-lombardia` 
- BeautifulSoup parsing focused on tab-container elements
- Filters out invalid URLs and titles using validation functions

## Development Commands

### Running the Bot
```bash
python main.py
```

### Dependencies Installation
```bash
pip install -U requests beautifulsoup4 python-dotenv
```

### Environment Configuration
Required environment variables:
- `TELEGRAM_BOT_TOKEN`: Bot token from @BotFather

Optional configuration:
- `NEWS_INTERVAL`: News check interval in seconds (default: 1800 = 30 min)

Fixed configuration (hardcoded):
- `TELEGRAM_POLL_INTERVAL`: 5 seconds (fixed)
- `REQUEST_TIMEOUT`: 20 seconds (fixed)
- `USER_AGENT`: Mozilla/5.0 compatible (fixed)

## Configuration Files

- **requirements.txt**: Python dependencies
- **.env**: Environment variables (contains sensitive tokens - do not commit)
- **.gitignore**: Excludes .env and .data/ directories
- **JSON persistence files**: `subscribers.json`, `seen.json`, `stats.json` (auto-generated)

## Key Implementation Details

### Threading Model
- Main loop handles Telegram polling every 5 seconds
- News checking runs on separate 30-minute schedule
- Simple single-threaded design with thread-safe storage operations

### Error Handling
- Web scraping failures logged but don't crash the bot
- Telegram API errors handled gracefully per user
- Automatic retry logic for transient failures

### Data Persistence Strategy
- **JSON-only approach**: Uses only JSON files for all data storage
- Thread-safe operations with locks  
- JSON files automatically created and updated: `subscribers.json`, `seen.json`, `stats.json`
- **Full persistence**: Data survives container restarts
- **No environment variables** needed for data storage

## Deployment Context

Optimized for Render.com free tier deployment without database requirements. The bot maintains state using only JSON files, providing reliable data storage without external dependencies.

### Render.com Specific Notes
- No keep-alive server needed (removed for simplicity)
- Use external services (like cron-job.org) to prevent sleep if needed
- JSON files provide full persistence across deployments
- Only two environment variables needed: `TELEGRAM_BOT_TOKEN` and optionally `NEWS_INTERVAL`
- Automatic file creation and management