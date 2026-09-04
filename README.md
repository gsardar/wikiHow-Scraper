# WikiHow Scraper

A robust, cross-platform (macOS & Windows) toolkit for downloading, categorizing, and managing WikiHow articles. Features a modern Web UI, an interactive TUI dashboard, multi-profile Chrome auto-login management, continuous scraping pipelines, and integrated Tor proxy rotation.

---

## 🌟 Key Features

- **🌐 Web UI & Dashboard**: Clean web-based management server running on `http://127.0.0.1:8899` to launch browser sessions, monitor active profiles, trigger article downloads, and view continuous scraping status.
- **🖥️ Interactive TUI & CLI**: Complete terminal interface built with `rich` for headless or command-line operation.
- **👤 Chrome Profile & Account Management**: Manage multiple authenticated Chrome profiles (WikiHow, Google, Facebook login) with session persistence and auto-login watchdog.
- **🔄 Cross-Platform Browser Watchdog**: Works seamlessly across **macOS** and **Windows** without unexpected browser closures, using SeleniumBase and optimized Chrome process handles.
- **⚡ Continuous Article Downloader**: Batch download and organize articles with automatic categorization stored under `data/articles/`.
- **🛡️ Tor Proxy & Circuit Rotation**: Integrated SOCKS proxy with automatic IP rotation on HTTP 403/429 rate limits or Cloudflare challenges.

---

## 📋 Requirements & Installation

### Requirements
- Python 3.9+
- Google Chrome browser installed on the system

### Installation

1. **Clone the repository**:
   ```bash
   git clone git@github.com:gsardar/wikihow_scraper.git
   cd wikihow_scraper
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

*(Dependencies include: `seleniumbase`, `requests`, `beautifulsoup4`, `rich`, `psutil`, `stem`, `websockets`, `pillow`)*

---

## 🚀 Usage Guide

### 1. Launching the Web UI (Recommended)

Start the local Web UI dashboard server:

```bash
python webui.py
# or
python -m wikihow_scraper.webui
```

Open your browser and navigate to: **`http://127.0.0.1:8899`**

**From the Web UI, you can:**
- Create and manage Chrome profiles.
- Log into your WikiHow account (launches a visible Chrome browser that remains open).
- Check login status (`logged_in` / `not_logged_in`).
- Start continuous scraping sessions.

---

### 2. Launching the TUI Dashboard

To run the interactive terminal user interface:

```bash
python tui.py
# or
python -m wikihow_scraper
```

---

### 3. Command Line Interface (CLI)

#### **Scrape Articles**
```bash
# Scrape a single article via requests/Tor
python cli.py scrape "Tie-a-Tie"

# Scrape an article using a real Chrome browser instance
python cli.py scrape "Tie-a-Tie" --browser

# Batch scrape multiple articles in parallel
python cli.py scrape "Tie-a-Tie,Bake-a-Cake,Fix-a-Leak" --multi --workers 3
```

#### **Profile & Login Management**
```bash
# List all saved profiles
python cli.py profile list

# Create a new profile
python cli.py profile add --name explorer_1

# Open visible browser window to log into WikiHow
python cli.py profile login --name explorer_1

# Check if profile is logged in
python cli.py profile check-login --name explorer_1
```

#### **Proxy Management (Tor)**
```bash
# Connect to Tor SOCKS proxy
python cli.py proxy connect

# Check current IP address
python cli.py proxy status

# Rotate IP address (NEWNYM signal)
python cli.py proxy rotate
```

---

## 📂 Data & Directory Structure

```
wikihow_scraper/
├── README.md               # Quick start & documentation
├── SPECIFICATION.md        # Technical architecture details
├── webui.py                # Web UI application server (port 8899)
├── cli.py                  # Command-line interface entry point
├── tui.py                  # Terminal UI dashboard
├── profiles.py             # Chrome profile & login manager
├── article_pipeline.py     # Article parsing & download pipeline
├── continuous_scraper.py   # Continuous scraping engine
├── discovery.py            # Article URL discovery
├── tabs.py                 # Cross-platform browser launcher & watchdog
├── requirements.txt        # Python package dependencies
│
└── data/                   # Local storage (Git-ignored)
    ├── articles/           # Categorized JSON article files
    ├── profiles/           # Chrome user-data directories per profile
    └── logs/               # Application & watchdog log files
```

---

## 🔒 Security & Privacy

- All login credentials and Chrome session cookies remain local in `data/profiles/` and are strictly excluded from Git commits via `.gitignore`.
- Only empty/unfilled profile templates (`credentials.json`) are shared.

---

## 💻 Cross-Platform Notes

- **macOS & Windows**: Fully tested and optimized for both macOS and Windows.
- Chrome process initialization avoids zero-tab race conditions and uses graceful disconnect protocols (`detach_driver_safely`) to prevent automatic browser closures.
