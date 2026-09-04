import time
import os
import json
import subprocess
import requests
import socket
import psutil
from stem import Signal
from stem.control import Controller
from wikihow_scraper import (
    TOR_RUNTIME_DIR, TOR_EXE, DEFAULT_PROXY_PORT, DEFAULT_CONTROL_PORT,
    GEOIP_PATH, GEOIP6_PATH, TOR_PORTS_REGISTRY, find_free_port
)

class StandaloneTorManager:
    def __init__(self):
        self.proxy_port = DEFAULT_PROXY_PORT
        self.control_port = DEFAULT_CONTROL_PORT
        self.active_ip = None
        self.is_connected = False
        self._process = None

    def _is_port_in_use(self, port):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(('127.0.0.1', port)) == 0

    def get_status(self):
        # Read from active registry if present
        ports = self.get_active_ports()
        p_port = ports.get("proxy_port", self.proxy_port)
        c_port = ports.get("control_port", self.control_port)

        alive = False
        if self._is_port_in_use(p_port):
            try:
                proxies = {
                    'http': f'socks5h://127.0.0.1:{p_port}',
                    'https': f'socks5h://127.0.0.1:{p_port}'
                }
                r = requests.get("https://api.ipify.org", proxies=proxies, timeout=3)
                if r.status_code == 200:
                    self.active_ip = r.text.strip()
                    self.is_connected = True
                    alive = True
            except Exception:
                pass
        
        if not alive:
            self.is_connected = False
            self.active_ip = None

        return {
            "status": "ONLINE" if alive else "OFFLINE",
            "proxy_port": p_port,
            "control_port": c_port,
            "current_ip": self.active_ip
        }

    def get_active_ports(self):
        """API method to fetch active Tor SOCKS and Control ports."""
        if os.path.exists(TOR_PORTS_REGISTRY):
            try:
                with open(TOR_PORTS_REGISTRY, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {"proxy_port": self.proxy_port, "control_port": self.control_port}

    def get_requests_proxies(self):
        ports = self.get_active_ports()
        p_port = ports.get("proxy_port", self.proxy_port)
        return {
            'http': f'socks5h://127.0.0.1:{p_port}',
            'https': f'socks5h://127.0.0.1:{p_port}'
        }

    def connect(self, socks_port=None, control_port=None):
        """Launches Tor using dynamically allocated random free ports by default."""
        # Allocate random ports if not specified
        p_port = socks_port or find_free_port()
        c_port = control_port or find_free_port()

        # Ensure ports don't conflict
        if p_port == c_port:
            c_port = find_free_port()

        self.proxy_port = p_port
        self.control_port = c_port

        # Clean any stale tor_scraper.exe process
        for proc in psutil.process_iter(['name']):
            try:
                if proc.info['name'].lower() == 'tor_scraper.exe':
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        
        # Wait up to 5 seconds for the ports to clear
        for _ in range(5):
            if not self._is_port_in_use(p_port) and not self._is_port_in_use(c_port):
                break
            time.sleep(1)

        # Create temporary custom torrc config
        tor_data_dir = os.path.join(TOR_RUNTIME_DIR, f"standalone_{p_port}")
        os.makedirs(tor_data_dir, exist_ok=True)
        torrc_path = os.path.join(TOR_RUNTIME_DIR, f"torrc.standalone_{p_port}")

        torrc_content = f"""
SocksPort {p_port}
ControlPort {c_port}
DataDirectory {tor_data_dir}
GeoIPFile {GEOIP_PATH}
GeoIPv6File {GEOIP6_PATH}
CookieAuthentication 0
"""
        with open(torrc_path, "w") as f:
            f.write(torrc_content.strip())

        print(f"[Tor] Launching {TOR_EXE} headless on SOCKS:{p_port} | Control:{c_port}...")
        try:
            self._process = subprocess.Popen(
                [TOR_EXE, "-f", torrc_path],
                creationflags=0x00000008 | 0x08000000,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception as e:
            print(f"[Tor] Start failed: {e}")
            return False

        # Wait for bootstrap
        for _ in range(15):
            time.sleep(2)
            try:
                proxies = {
                    'http': f'socks5h://127.0.0.1:{p_port}',
                    'https': f'socks5h://127.0.0.1:{p_port}'
                }
                r = requests.get("https://api.ipify.org", proxies=proxies, timeout=5)
                if r.status_code == 200:
                    self.active_ip = r.text.strip()
                    self.is_connected = True
                    
                    # Save ports to registry API file
                    with open(TOR_PORTS_REGISTRY, "w") as f:
                        json.dump({"proxy_port": p_port, "control_port": c_port}, f)
                        
                    print(f"[Tor] Bootstrapped successfully! IP: {self.active_ip}")
                    return True
            except Exception:
                pass
            print(".", end="", flush=True)

        print("\n[Tor] Bootstrap timeout.")
        return False

    def rotate_ip(self):
        """Signal Tor to refresh circuits and assign a new IP."""
        ports = self.get_active_ports()
        p_port = ports.get("proxy_port", self.proxy_port)
        c_port = ports.get("control_port", self.control_port)

        try:
            with Controller.from_port(port=c_port) as controller:
                controller.authenticate()
                controller.signal(Signal.NEWNYM)
                time.sleep(5)
                # Verify new IP
                proxies = {
                    'http': f'socks5h://127.0.0.1:{p_port}',
                    'https': f'socks5h://127.0.0.1:{p_port}'
                }
                r = requests.get("https://api.ipify.org", proxies=proxies, timeout=5)
                self.active_ip = r.text.strip()
                return True, f"IP rotated successfully. New IP: {self.active_ip}"
        except Exception as e:
            return False, f"Failed to rotate IP: {e}"

    def shutdown(self):
        if self._process:
            self._process.kill()
            self._process.wait()
            self._process = None
        else:
            for proc in psutil.process_iter(['name']):
                try:
                    if proc.info['name'].lower() == 'tor_scraper.exe':
                        proc.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        self.is_connected = False
        
        # Clear ports registry
        if os.path.exists(TOR_PORTS_REGISTRY):
            try:
                os.remove(TOR_PORTS_REGISTRY)
            except Exception:
                pass
                
        print("[Tor] Standalone Tor process shut down.")

tor = StandaloneTorManager()
