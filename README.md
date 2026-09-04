# WikiHow Scraper

A robust, cross-platform (macOS & Windows) toolkit for downloading, categorizing, and managing WikiHow articles. Features a modern Web UI, an interactive TUI dashboard, multi-profile Chrome auto-login management, continuous scraping pipelines, and integrated Tor proxy rotation.

---

## 🌟 Key Features

- **🌐 Web UI & Dashboard**: Clean web-based management server running on `http://127.0.0.1:8899` to launch browser sessions, monitor active profiles, trigger article downloads, and view continuous scraping status.
- **🖥️ Interactive TUI & CLI**: Complete terminal interface built with `rich` for headless or command-line operation.
- **👤 Chrome Profile & Account Management**: Manage multiple authenticated Chrome profiles (WikiHow, Google, Facebook login) with session persistence and auto-login watchdog.
- **🔄 Cross-Platform Browser Watchdog**: Works seamlessly across **macOS** and **Windows** without unexpected browser closures, using SeleniumBase and optimized Chrome process handles.
- **♾️ Unlimited Continuous Article Downloader**: Batch download and organize articles with automatic category grouping saved under `data/articles/`. Features auto-replenish discovery for infinite continuous scraping.
- **🛡️ Tor Proxy & Circuit Rotation**: Integrated SOCKS proxy with automatic IP rotation on HTTP 403/429 rate limits or Cloudflare challenges.

---

## 📖 Step-by-Step Quick Start Guide

### Step 1: Start the Web Server

Run the Web UI server from your terminal:

```bash
python webui.py
# or
python -m wikihow_scraper.webui
```

Once started, open your web browser and navigate to: **`http://127.0.0.1:8899`**

---

### Step 2: Logging into a Chrome Profile

To scrape logged-in pages or preserve user session cookies:

1. In the Web UI sidebar under **Profile**, select an existing profile (e.g. `explorer_1`) or enter a name and click **Add Profile**.
2. Click **Login (Interactive)**.
3. A visible Google Chrome window will open on your desktop navigated to WikiHow.
4. Log into your WikiHow account (via WikiHow direct login, Google, or Facebook).
5. Once logged in, you can close or keep the browser window open.
6. Click **Check Status** in the Web UI — the status will show **`LOGGED IN`**.

---

### Step 3: Starting Unlimited Continuous Scraping

To launch continuous article downloading:

1. In the left sidebar under **Mode**, click **Continuous**.
2. Choose a **Sequencing Strategy**:
   - **Sorted (site-wide)**: Scrapes articles ordered by *Newest created*, *Newest edits*, *Most edited*, etc.
   - **Random articles**: Picks random articles site-wide or within a selected category.
   - **Manual list**: Type exact article titles separated by commas.
3. Under **Count**, leave the box **blank or enter 0** for **unlimited continuous scraping**.
4. Click the green **Start** button.
5. The **Run State** will change to **`RUNNING`**. The scraper will continuously download articles, categorize them, and auto-discover new articles whenever the queue empties.
6. To stop scraping at any time, click the red **Stop** button.

---

## ❓ Frequently Asked Questions

### Is there a limit on continuous scraping?
**No, continuous scraping is 100% unlimited.** 
- Previously, runs were capped at 20 articles per batch.
- We updated the engine with **automatic auto-discovery replenishment**. Now, when the initial queue finishes, the continuous scraper automatically discovers and queues new articles, continuing indefinitely until you click **Stop**.

---

## 📋 Requirements & Installation

### Requirements
- Python 3.9+
- Google Chrome browser installed on the system

### Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/gsardar/wikihow_scraper.git
   cd wikihow_scraper
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

---

## 💻 Command Line (CLI) & TUI Usage

### Interactive Terminal Dashboard (TUI)
```bash
python tui.py
# or
python -m wikihow_scraper
```

### Command Line Interface (CLI)

```bash
# Scrape a single article
python cli.py scrape "Tie-a-Tie"

# Manage profiles via CLI
python cli.py profile list
python cli.py profile login --name profile_1

# Tor Proxy management
python cli.py proxy connect
python cli.py proxy rotate
```

---

## 📂 Data & Directory Structure

```
wikihow_scraper/
├── README.md               # Documentation & quick start guide
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
