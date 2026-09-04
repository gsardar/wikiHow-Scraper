import os
import time
import json
import shutil
import psutil
from bs4 import BeautifulSoup
from seleniumbase import Driver
from selenium.common.exceptions import UnexpectedAlertPresentException, NoAlertPresentException
from wikihow_scraper import PROFILES_DIR
from wikihow_scraper.tabs import attach_driver, detach_driver_safely

class ProfileManager:
    @staticmethod
    def _get_running_watchdog_port(profile_name):
        """
        If a BrowserWatchdog is already running for this profile, return its CDP debug port.
        Used to ATTACH to the live instance instead of spawning a second Chrome against the
        same user_data_dir (which deadlocks - Chrome profile directories are single-writer).
        """
        tracker_file = os.path.join(PROFILES_DIR, f"{profile_name}_tracker.json")
        if not os.path.exists(tracker_file):
            return None
        try:
            with open(tracker_file, "r") as f:
                info = json.load(f)
            pid = info.get("chrome_pid")
            if pid and psutil.pid_exists(pid):
                return info.get("port")
        except Exception:
            pass
        return None

    @staticmethod
    def _dismiss_alert_if_any(driver):
        """WikiHow's Google-login flow can throw a blocking JS alert; swallow it so page reads don't crash."""
        try:
            alert = driver.switch_to.alert
            text = alert.text
            alert.accept()
            return text
        except NoAlertPresentException:
            return None

    # Confirmed by direct DOM inspection of real logged-in AND logged-out sessions.
    # History of selector bugs here, both causing false readings:
    #   - #header_user_profile, .logged-in, #user_about, and the "action=logout" text
    #     check NEVER matched WikiHow's actual markup at all (false "NOT logged in").
    #   - #nav_profile and .icon-profile are NOT reliable by presence alone - they exist
    #     in BOTH states (#nav_profile just changes its text between "LOG IN" and
    #     "MY PROFILE"; a mere .select_one() presence check can't tell them apart and
    #     caused false "logged in" reports).
    # The only unambiguous signal found: the real logout link, which simply does not
    # exist in the DOM at all when logged out.
    _LOGGED_IN_SELECTOR = "a[href='/Special:UserLogout'], a[href='/Special:Userlogout']"

    @staticmethod
    def _find_logged_in_indicator(soup):
        return soup.select_one(ProfileManager._LOGGED_IN_SELECTOR)

    @staticmethod
    def get_profile_path(profile_name):
        return os.path.join(PROFILES_DIR, profile_name)

    @staticmethod
    def get_account_file(profile_name):
        return os.path.join(ProfileManager.get_profile_path(profile_name), "account_info.json")

    @staticmethod
    def save_account_info(profile_name, provider="Unknown", account_name="Unspecified", email="N/A"):
        path = ProfileManager.get_profile_path(profile_name)
        os.makedirs(path, exist_ok=True)
        acc_file = ProfileManager.get_account_file(profile_name)
        info = {
            "profile_name": profile_name,
            "provider": provider,
            "account_name": account_name,
            "email": email,
            "last_updated": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        with open(acc_file, "w") as f:
            json.dump(info, f, indent=2)
        return info

    @staticmethod
    def get_credentials_file(profile_name):
        return os.path.join(ProfileManager.get_profile_path(profile_name), "credentials.json")

    @staticmethod
    def save_credentials(profile_name, username, password, mode="direct"):
        """
        Stores login credentials for a profile so `login` can authenticate automatically
        instead of waiting on a human. Stored in plaintext JSON inside
        profiles_data/<profile_name>/ - this folder is already excluded from git by
        the project's allow-list .gitignore, but treat it as sensitive local state.

        mode:
          "direct"   - WikiHow's own username/password form (most reliable to automate)
          "google"   - Google OAuth popup (username = Google email, password = Google password;
                       Google actively fingerprints automation and may block or demand 2FA -
                       least reliable to automate, falls back to manual if it can't complete)
          "facebook" - Facebook OAuth popup (username = FB email/phone, password = FB password)
        """
        if mode not in ("direct", "google", "facebook"):
            raise ValueError(f"mode must be 'direct', 'google', or 'facebook', got {mode!r}")
        path = ProfileManager.get_profile_path(profile_name)
        os.makedirs(path, exist_ok=True)
        cred_file = ProfileManager.get_credentials_file(profile_name)
        with open(cred_file, "w") as f:
            json.dump({"mode": mode, "username": username, "password": password}, f, indent=2)
        try:
            os.chmod(cred_file, 0o600)  # best-effort; no-op on Windows ACLs but harmless
        except Exception:
            pass
        return cred_file

    @staticmethod
    def load_credentials(profile_name):
        cred_file = ProfileManager.get_credentials_file(profile_name)
        if not os.path.exists(cred_file):
            return None
        try:
            with open(cred_file, "r") as f:
                data = json.load(f)
            username = data.get("username")
            password = data.get("password")
            if not username or not password:
                return None
            if username in ProfileManager._PLACEHOLDER_MARKERS or password in ProfileManager._PLACEHOLDER_MARKERS:
                return None  # still an unfilled template - don't attempt login with it
            data.setdefault("mode", "direct")
            return data
        except Exception:
            pass
        return None

    @staticmethod
    def get_account_info(profile_name):
        acc_file = ProfileManager.get_account_file(profile_name)
        if os.path.exists(acc_file):
            try:
                with open(acc_file, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {
            "profile_name": profile_name,
            "provider": "Not Configured",
            "account_name": "Anonymous",
            "email": "N/A"
        }

    @staticmethod
    def add_profile(profile_name, provider="Unknown", account_name="Unspecified", email="N/A"):
        path = ProfileManager.get_profile_path(profile_name)
        if os.path.exists(path):
            return False, f"Profile '{profile_name}' already exists."
        os.makedirs(path, exist_ok=True)
        ProfileManager.save_account_info(profile_name, provider, account_name, email)
        ProfileManager.write_credentials_placeholder(profile_name)
        return True, (
            f"Profile '{profile_name}' created successfully at {path}.\n"
            f"  Edit {ProfileManager.get_credentials_file(profile_name)} to enable auto-login "
            f"(or leave it as-is and use manual login)."
        )

    _PLACEHOLDER_MARKERS = {
        "your_facebook_email_or_phone_here", "your_facebook_password_here",
        "your_google_email_here", "your_google_password_here",
        "your_wikihow_username_or_email_here", "your_wikihow_password_here",
    }

    @staticmethod
    def write_credentials_placeholder(profile_name):
        """
        Writes a fill-in-the-blank credentials.json for a freshly created profile so the
        user can just open it in their editor rather than needing set-credentials for the
        first pass. Never overwrites an existing (possibly already-filled-in) file.
        """
        cred_file = ProfileManager.get_credentials_file(profile_name)
        if os.path.exists(cred_file):
            return cred_file
        placeholder = {
            "mode": "direct",
            "_comment": "mode: 'direct' (WikiHow username/password), 'google', or 'facebook'. "
                        "Fill in username/password below, then run: "
                        "python -m wikihow_scraper.cli profile login --name " + profile_name,
            "username": "your_wikihow_username_or_email_here",
            "password": "your_wikihow_password_here"
        }
        with open(cred_file, "w") as f:
            json.dump(placeholder, f, indent=2)
        try:
            os.chmod(cred_file, 0o600)
        except Exception:
            pass
        return cred_file

    @staticmethod
    def list_profiles():
        if not os.path.exists(PROFILES_DIR):
            return []
        profiles = []
        for d in os.listdir(PROFILES_DIR):
            path = os.path.join(PROFILES_DIR, d)
            if os.path.isdir(path):
                info = ProfileManager.get_account_info(d)
                profiles.append(info)
        return profiles

    @staticmethod
    def delete_profile(profile_name, stop_if_running=True):
        """
        Deletes a profile's whole directory. If a watchdog browser is currently
        running for it, that instance holds Chrome's profile files open - deleting
        while it's live would either fail outright (Windows locks open files) or,
        worse, race with the watchdog's own file writes. With stop_if_running=True
        (the default), any live watchdog for this profile is stopped first, so
        "delete" from the UI just works instead of requiring a separate manual stop.
        """
        path = ProfileManager.get_profile_path(profile_name)
        if not os.path.exists(path):
            return False, f"Profile '{profile_name}' does not exist."

        port = ProfileManager._get_running_watchdog_port(profile_name)
        if port:
            if not stop_if_running:
                return False, (
                    f"Profile '{profile_name}' has a live browser running (port {port}) - "
                    f"stop it first, or call delete with stop_if_running=True."
                )
            from wikihow_scraper.pid_tracker.pid_manager import BrowserWatchdog
            BrowserWatchdog(profile_name, port=port).stop()
            # Give Windows a moment to actually release the file handles after the
            # process is killed - an immediate rmtree can still hit "file in use".
            for _ in range(10):
                if not ProfileManager._get_running_watchdog_port(profile_name):
                    break
                time.sleep(0.5)
            time.sleep(1)

        try:
            shutil.rmtree(path)
            return True, f"Profile '{profile_name}' deleted successfully." + (
                " (stopped its running browser first)" if port else ""
            )
        except Exception as e:
            return False, f"Failed to delete profile: {e}"

    @staticmethod
    def rename_profile(old_name, new_name):
        """
        Renames a profile: moves its whole directory (credentials, account info, the
        actual Chrome user-data-dir with its login session) to the new name. Refuses
        if a live watchdog is currently running for this profile - the running Chrome
        process still has the old directory open, so moving it out from under would
        corrupt the profile; caller must stop the watchdog first.
        """
        if ProfileManager._get_running_watchdog_port(old_name):
            return False, f"Profile '{old_name}' has a live browser running - stop it before renaming."

        old_path = ProfileManager.get_profile_path(old_name)
        new_path = ProfileManager.get_profile_path(new_name)
        if not os.path.exists(old_path):
            return False, f"Profile '{old_name}' does not exist."
        if os.path.exists(new_path):
            return False, f"A profile named '{new_name}' already exists."

        try:
            shutil.move(old_path, new_path)
        except Exception as e:
            return False, f"Failed to rename profile: {e}"

        acc_file = ProfileManager.get_account_file(new_name)
        if os.path.exists(acc_file):
            try:
                with open(acc_file, "r") as f:
                    info = json.load(f)
                info["profile_name"] = new_name
                with open(acc_file, "w") as f:
                    json.dump(info, f, indent=2)
            except Exception:
                pass  # rename itself already succeeded; account_info's own name field is cosmetic

        return True, f"Renamed profile '{old_name}' -> '{new_name}'."

    @staticmethod
    def logout(profile_name):
        """
        Logs a profile out of WikiHow by navigating to Special:UserLogout. Attaches to
        a live watchdog via CDP if one is running (same pattern as check_login_status),
        otherwise launches a temporary headless browser for just this action.
        """
        path = ProfileManager.get_profile_path(profile_name)
        if not os.path.exists(path):
            return False, f"Profile '{profile_name}' does not exist."

        port = ProfileManager._get_running_watchdog_port(profile_name)
        own_driver = None
        try:
            if port:
                driver = attach_driver(port)
                driver.execute_script("window.open('about:blank', '_blank');")
                driver.switch_to.window(driver.window_handles[-1])
            else:
                driver = Driver(uc=True, headless=False, user_data_dir=path)
                own_driver = driver

            driver.get("https://www.wikihow.com/Special:UserLogout")
            time.sleep(2)
            soup = BeautifulSoup(driver.page_source, "html.parser")
            still_logged_in = bool(ProfileManager._find_logged_in_indicator(soup))

            if port:
                if len(driver.window_handles) > 1:
                    try:
                        driver.close()
                    except Exception:
                        pass
                detach_driver_safely(driver)

            if still_logged_in:
                return False, f"Logout may not have completed for '{profile_name}' - still shows a logged-in indicator."
            return True, f"Profile '{profile_name}' logged out."
        except Exception as e:
            return False, f"Logout failed: {e}"
        finally:
            if own_driver:
                try:
                    own_driver.quit()
                except Exception:
                    pass

    @staticmethod
    def check_login_status(profile_name, auto_prompt_login=True):
        """
        Checks if a Chrome profile is logged into WikiHow.
        If a BrowserWatchdog is already running for this profile, ATTACHES to it via CDP
        instead of launching a second Chrome against the same user_data_dir (which deadlocks -
        two Chrome processes cannot share one profile directory).
        """
        path = ProfileManager.get_profile_path(profile_name)
        if not os.path.exists(path):
            return False, f"Profile '{profile_name}' does not exist."

        print(f"[ProfileManager] Checking login status for profile '{profile_name}'...")

        port = ProfileManager._get_running_watchdog_port(profile_name)
        if port:
            print(f"[ProfileManager] Watchdog is live on port {port} - attaching instead of spawning a new Chrome.")
            return ProfileManager._check_login_attached(profile_name, port, auto_prompt_login)

        driver = Driver(uc=True, headless=False, user_data_dir=path)
        is_logged_in = False
        user_text = ""
        try:
            driver.get("https://www.wikihow.com/Main-Page")
            time.sleep(2)

            soup = BeautifulSoup(driver.page_source, 'html.parser')
            page_html = str(soup).lower()

            # Detect logged in elements
            user_el = ProfileManager._find_logged_in_indicator(soup)
            if user_el:
                is_logged_in = True
                user_text = user_el.get_text().strip() if user_el else "Logged In User"

                # Try detecting Google or Facebook session trace
                provider = "WikiHow Account"
                if "google" in page_html or "gmail" in page_html:
                    provider = "Google (Gmail)"
                elif "facebook" in page_html:
                    provider = "Facebook"

                ProfileManager.save_account_info(profile_name, provider=provider, account_name=user_text)
        except Exception as e:
            print(f"[ProfileManager] Check error: {e}")
        finally:
            try:
                driver.quit()
            except Exception:
                pass

        if is_logged_in:
            return True, f"Profile '{profile_name}' is LOGGED IN to WikiHow ({user_text})."
        else:
            print(f"[ProfileManager] Profile '{profile_name}' is NOT logged in.")
            if auto_prompt_login:
                print(f"[ProfileManager] Switching to HEADED mode and launching WikiHow login screen...")
                ProfileManager.interactive_login(profile_name)
                return True, f"Interactive login complete for profile '{profile_name}'."
            return False, f"Profile '{profile_name}' is NOT logged in to WikiHow."

    @staticmethod
    def _check_login_attached(profile_name, port, auto_prompt_login=True):
        """Login check performed by attaching to an already-running watchdog Chrome via CDP."""
        driver = attach_driver(port)

        try:
            original_tab = driver.current_window_handle
            driver.execute_script("window.open('https://www.wikihow.com/Main-Page', '_blank');")
            driver.switch_to.window(driver.window_handles[-1])
            time.sleep(2)
            ProfileManager._dismiss_alert_if_any(driver)

            soup = BeautifulSoup(driver.page_source, 'html.parser')
            page_html = str(soup).lower()
            user_el = ProfileManager._find_logged_in_indicator(soup)
            is_logged_in = bool(user_el)
            user_text = user_el.get_text().strip() if user_el else "Logged In User"

            if is_logged_in:
                provider = "WikiHow Account"
                if "google" in page_html or "gmail" in page_html:
                    provider = "Google (Gmail)"
                elif "facebook" in page_html:
                    provider = "Facebook"
                ProfileManager.save_account_info(profile_name, provider=provider, account_name=user_text)
                if len(driver.window_handles) > 1:
                    try:
                        driver.close()
                        driver.switch_to.window(driver.window_handles[0])
                    except Exception:
                        pass
                detach_driver_safely(driver)
                return True, f"Profile '{profile_name}' is LOGGED IN to WikiHow ({user_text})."

            print(f"[ProfileManager] Profile '{profile_name}' is NOT logged in.")
            if auto_prompt_login:
                # Hands the live driver off to continue the interactive login in this
                # SAME tab - do not close()/quit() it here, that's now the callee's job.
                driver.get("https://www.wikihow.com/Special:Userlogin")
                return ProfileManager._interactive_login_attached(profile_name, driver)

            if len(driver.window_handles) > 1:
                try:
                    driver.close()
                    driver.switch_to.window(original_tab)
                except Exception:
                    pass
            detach_driver_safely(driver)
            return False, f"Profile '{profile_name}' is NOT logged in to WikiHow."
        except Exception as e:
            detach_driver_safely(driver)
            return False, f"[ProfileManager] Attached check failed: {e}"

    @staticmethod
    def _fill_login_form(driver, username, password):
        """Fills WikiHow's MediaWiki login form (#wpName1 / #wpPassword1 / #wpLoginAttempt) and submits."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        try:
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "wpName1")))
        except Exception:
            return False, "Login form did not load in time (field #wpName1 not found)."

        try:
            user_field = driver.find_element(By.ID, "wpName1")
            pass_field = driver.find_element(By.ID, "wpPassword1")
            user_field.clear()
            user_field.send_keys(username)
            pass_field.clear()
            pass_field.send_keys(password)
            driver.find_element(By.ID, "wpLoginAttempt").click()
            return True, "Credentials submitted."
        except Exception as e:
            return False, f"Failed to fill/submit login form: {e}"

    @staticmethod
    def _wait_for_logged_in_or_error(driver, timeout_seconds, poll_interval, profile_name=None, provider="wikihow"):
        """
        Shared polling loop: watches the tab for a logged-in header, an error box, or a
        2FA/checkpoint/CAPTCHA interstitial (which gets detected + printed to the CLI
        with a screenshot rather than silently ticking down to a timeout).
        """
        checkpoint_reported = False
        elapsed = 0
        while elapsed < timeout_seconds:
            time.sleep(poll_interval)
            elapsed += poll_interval
            if ProfileManager._dismiss_alert_if_any(driver):
                continue
            try:
                soup = BeautifulSoup(driver.page_source, 'html.parser')
            except UnexpectedAlertPresentException:
                continue
            page_html = str(soup).lower()
            user_el = ProfileManager._find_logged_in_indicator(soup)
            if user_el:
                user_text = user_el.get_text().strip() if user_el else "Authenticated User"
                return "success", user_text
            error_el = soup.select_one(".errorbox, #userloginForm .error")
            if error_el:
                return "error", error_el.get_text(strip=True)

            if profile_name and provider != "wikihow" and not checkpoint_reported:
                detected, cp_message = ProfileManager._detect_and_capture_verification_prompt(
                    driver, profile_name, provider
                )
                if detected:
                    checkpoint_reported = True  # print once, then keep polling in case user resolves it
        return "timeout", None

    @staticmethod
    def _auto_login_direct(driver, creds, timeout_seconds, poll_interval):
        driver.get("https://www.wikihow.com/Special:Userlogin")
        ProfileManager._dismiss_alert_if_any(driver)
        ok, msg = ProfileManager._fill_login_form(driver, creds["username"], creds["password"])
        if not ok:
            return "error", msg
        return ProfileManager._wait_for_logged_in_or_error(driver, timeout_seconds, poll_interval)

    _CHECKPOINT_MARKERS = [
        "checkpoint", "two-factor", "two factor", "verification code", "security code",
        "enter the code", "enter this code", "confirm your identity", "approve from another device",
        "check your notifications", "choose a way to", "select the account you", "verify it's you",
        "signin/v2/challenge", "captcha", "unusual activity", "confirm it's you",
    ]

    @staticmethod
    def _detect_and_capture_verification_prompt(driver, profile_name, provider):
        """
        Detects a 2FA/checkpoint/CAPTCHA interstitial by URL + keyword heuristics.
        If found: saves a screenshot (so it can be viewed without touching the browser)
        and prints the visible instruction text directly to the CLI.
        Returns (detected: bool, message: str).
        """
        try:
            url = driver.current_url.lower()
            soup = BeautifulSoup(driver.page_source, 'html.parser')
            for tag in soup(["script", "style"]):
                tag.decompose()
            body_text = soup.get_text(" ", strip=True)
            body_lower = body_text.lower()
        except Exception:
            return False, ""

        hit = any(marker in url or marker in body_lower for marker in ProfileManager._CHECKPOINT_MARKERS)
        if not hit:
            return False, ""

        shot_dir = ProfileManager.get_profile_path(profile_name)
        shot_path = os.path.join(shot_dir, "last_verification_prompt.png")
        try:
            driver.save_screenshot(shot_path)
        except Exception:
            shot_path = None

        # Pull out a short, human-readable snippet around whichever marker matched,
        # plus any standalone 1-3 digit number (common for "tap this number on your phone" flows).
        import re
        number_hint = re.findall(r"\b\d{1,3}\b", body_text[:1500])
        snippet = body_text[:400]

        message_lines = [
            f"\n[!] {provider.title()} is asking for extra verification (2FA / checkpoint / CAPTCHA).",
            f"    URL: {driver.current_url}",
        ]
        if shot_path:
            message_lines.append(f"    Screenshot saved: {shot_path}")
        if number_hint:
            message_lines.append(f"    Numbers visible on page (possible device-confirmation code): {number_hint[:5]}")
        message_lines.append(f"    Page text: {snippet}")
        message_lines.append("    This step needs a human (phone/authenticator app) - complete it in the open browser window.\n")

        message = "\n".join(message_lines)
        print(message)
        return True, message

    @staticmethod
    def _auto_login_oauth(driver, creds, provider, timeout_seconds, poll_interval, profile_name=None):
        """
        Common flow for Google/Facebook: click WikiHow's OAuth button (usually opens a popup),
        fill the provider's own email+password steps, switch back once the popup closes.
        This is inherently fragile - both Google and Facebook actively detect automation and
        may demand a CAPTCHA, device confirmation, or 2FA that no script can complete. If that
        happens this returns "manual_needed" so the caller can hand off to a human.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        oauth_domain = "facebook.com" if provider == "facebook" else "accounts.google.com"
        main_tab = driver.current_window_handle

        # Don't open a duplicate popup if a login for this provider is already in progress
        # (e.g. from a previous call, or a human mid-flow) - reuse that tab instead.
        existing_oauth_tab = None
        for handle in driver.window_handles:
            try:
                driver.switch_to.window(handle)
                if oauth_domain in driver.current_url.lower():
                    existing_oauth_tab = handle
                    break
            except Exception:
                continue

        if existing_oauth_tab:
            print(f"[ProfileManager] Found an in-progress {provider} login tab already open - reusing it instead of opening a new one.")
            oauth_tab = existing_oauth_tab
            driver.switch_to.window(oauth_tab)
            # main_tab may no longer be valid/relevant if we didn't just open this ourselves;
            # fall back to whatever WikiHow tab exists, or leave as-is if none do.
            for handle in driver.window_handles:
                driver.switch_to.window(handle)
                if "wikihow.com" in driver.current_url.lower():
                    main_tab = handle
                    break
            driver.switch_to.window(oauth_tab)
        else:
            driver.switch_to.window(main_tab)
            driver.get("https://www.wikihow.com/Special:Userlogin")
            ProfileManager._dismiss_alert_if_any(driver)

            button_id = "gplus_login" if provider == "google" else "fb_login"
            tabs_before = set(driver.window_handles)

            try:
                WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.ID, button_id)))
                driver.find_element(By.ID, button_id).click()
            except Exception as e:
                return "error", f"Could not click the {provider} login button (#{button_id}): {e}"

            time.sleep(2)
            ProfileManager._dismiss_alert_if_any(driver)

            # OAuth may open a popup, or navigate the same tab - handle both.
            # NOTE: WikiHow's own login button has been observed firing window.open()
            # TWICE for a single click (two popups within ~100ms of each other, distinct
            # logger_id/cbt params) - this is a site-side quirk, not something our click
            # logic causes. Deduplicate defensively: if multiple new tabs landed on the
            # same OAuth domain, keep only the first and close the rest.
            new_tabs = list(set(driver.window_handles) - tabs_before)
            if len(new_tabs) > 1:
                same_domain_tabs = []
                for h in new_tabs:
                    driver.switch_to.window(h)
                    if oauth_domain in driver.current_url.lower():
                        same_domain_tabs.append(h)
                if len(same_domain_tabs) > 1:
                    print(f"[ProfileManager] {provider.title()}'s login button opened {len(same_domain_tabs)} duplicate popups from one click - closing the extras.")
                    for h in same_domain_tabs[1:]:
                        driver.switch_to.window(h)
                        try:
                            driver.close()
                        except Exception:
                            pass
                    new_tabs = [t for t in new_tabs if t not in same_domain_tabs[1:]]

            oauth_tab = new_tabs[0] if new_tabs else main_tab
            driver.switch_to.window(oauth_tab)

        time.sleep(2)

        try:
            if provider == "google":
                id_field = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "identifierId")))
                if not id_field.get_attribute("value"):
                    id_field.send_keys(creds["username"])
                    driver.find_element(By.ID, "identifierNext").click()
                    time.sleep(2)
                    ProfileManager._dismiss_alert_if_any(driver)
                pw_field = WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.NAME, "Passwd"))
                )
                if not pw_field.get_attribute("value"):
                    pw_field.send_keys(creds["password"])
                    driver.find_element(By.ID, "passwordNext").click()
            else:  # facebook
                # Facebook renders this form with dynamically-generated `id`s (React-style,
                # e.g. "_R_1h6kqsqppb6amH1_") that change per page load / A-B test variant.
                # `name="email"` / `name="pass"` are the stable field selectors - use those,
                # not id. The actual clickable Log In control is a div[role='button'], NOT
                # a <button> or input[type=submit] (those exist in the DOM but are hidden
                # decoys/no-ops) - confirmed by direct inspection.
                email_field = WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.NAME, "email")))
                pass_field = driver.find_element(By.NAME, "pass")
                if not email_field.get_attribute("value"):
                    email_field.send_keys(creds["username"])
                if not pass_field.get_attribute("value"):
                    pass_field.send_keys(creds["password"])
                login_btn = driver.find_element(By.CSS_SELECTOR, "div[role='button'][aria-label='Log In']")
                driver.execute_script("arguments[0].click();", login_btn)
        except Exception as e:
            return "manual_needed", (
                f"{provider.title()}'s login form didn't match the expected fields ({e}). "
                f"This usually means a CAPTCHA, 2FA prompt, or 'confirm it's you' step appeared - "
                f"these can't be automated. Switch to the open window and finish manually."
            )

        time.sleep(3)
        ProfileManager._dismiss_alert_if_any(driver)

        # If the popup is still open, a checkpoint/2FA step is probably in progress there -
        # keep watching THAT tab. Only fall back to the main tab once the popup has closed
        # (which is what a clean successful OAuth handoff looks like).
        if oauth_tab != main_tab and oauth_tab in driver.window_handles:
            driver.switch_to.window(oauth_tab)
            outcome, detail = ProfileManager._wait_for_logged_in_or_error(
                driver, timeout_seconds, poll_interval, profile_name=profile_name, provider=provider
            )
            if outcome == "timeout" and oauth_tab not in driver.window_handles and main_tab in driver.window_handles:
                # Popup closed while we were polling it - re-check the main tab once more.
                driver.switch_to.window(main_tab)
                return ProfileManager._wait_for_logged_in_or_error(
                    driver, timeout_seconds, poll_interval, profile_name=profile_name, provider=provider
                )
            return outcome, detail

        if main_tab in driver.window_handles:
            driver.switch_to.window(main_tab)
        return ProfileManager._wait_for_logged_in_or_error(
            driver, timeout_seconds, poll_interval, profile_name=profile_name, provider=provider
        )

    @staticmethod
    def auto_login(profile_name, timeout_seconds=60, poll_interval=2):
        """
        Logs in automatically using credentials.json saved via save_credentials().
        Dispatches to direct / google / facebook flow based on the saved "mode".
        Attaches to a live watchdog if one is running for this profile, otherwise
        launches a fresh headed browser (and leaves it open on failure so a human
        can take over manually).
        Returns (success: bool, message: str).
        """
        creds = ProfileManager.load_credentials(profile_name)
        if not creds:
            return False, (
                f"No saved credentials for profile '{profile_name}'. "
                f"Use `profile set-credentials` first, or call interactive_login() for manual sign-in."
            )

        mode = creds.get("mode", "direct")
        port = ProfileManager._get_running_watchdog_port(profile_name)
        own_driver = False
        if port:
            driver = attach_driver(port)
            driver.execute_script("window.open('about:blank', '_blank');")
            driver.switch_to.window(driver.window_handles[-1])
        else:
            from wikihow_scraper.pid_tracker.pid_manager import BrowserWatchdog
            watchdog = BrowserWatchdog(profile_name, port=9099)
            watchdog.should_cleanup = False
            driver = watchdog.launch_browser()
            watchdog._write_tracker("healthy", watchdog._get_tabs() or [])
            port = 9099

        if mode == "direct":
            outcome, detail = ProfileManager._auto_login_direct(driver, creds, timeout_seconds, poll_interval)
        elif mode in ("google", "facebook"):
            outcome, detail = ProfileManager._auto_login_oauth(
                driver, creds, mode, timeout_seconds, poll_interval, profile_name=profile_name
            )
        else:
            return False, f"Unknown login mode '{mode}'."

        # In the attached (port) case, quitting `driver` only ends THIS local
        # chromedriver session/process - it does not touch the shared watchdog browser
        # or close its tabs (confirmed: see tabs.py's _attach docstring). Skipping this
        # leaked one chromedriver process per auto_login() call.
        #
        # In the own_driver (standalone, no watchdog) case, a successful login used to
        # leave that Chrome window open indefinitely with no quit(). Confirmed bug: this
        # holds the profile directory's Chrome SingletonLock forever, so the VERY NEXT
        # check_login_status() call (e.g. the webui's account-status refresh, which
        # launches its own headless Chrome against the same user_data_dir when no
        # watchdog is tracked) fails to open that locked directory and reports
        # logged_out/error - showing red in the webui even though login truly succeeded.
        # quit() here does a clean shutdown (flushes cookies to disk) and frees the lock,
        # so status checks and later scraping can open the profile normally.
        if outcome == "success":
            provider_label = {"direct": "WikiHow Account", "google": "Google (Gmail)", "facebook": "Facebook"}[mode]
            ProfileManager.save_account_info(profile_name, provider=provider_label, account_name=detail)
            print(f"[SUCCESS] Auto-login verified for profile '{profile_name}' ({detail}).")
            if port:
                if len(driver.window_handles) > 1:
                    try:
                        driver.close()
                        driver.switch_to.window(driver.window_handles[0])
                    except Exception:
                        pass
                detach_driver_safely(driver)
            return True, f"Profile '{profile_name}' is now LOGGED IN via {provider_label} ({detail})."
        elif outcome == "error":
            if port:
                if len(driver.window_handles) > 1:
                    try:
                        driver.close()
                    except Exception:
                        pass
                detach_driver_safely(driver)
            else:
                try:
                    driver.quit()
                except Exception:
                    pass
            return False, f"Login rejected: {detail}"
        elif outcome == "manual_needed":
            print(f"\n[!] {detail}\n[!] Leaving the browser open on profile '{profile_name}' for manual completion.\n")
            if port:
                detach_driver_safely(driver)
            return False, detail
        else:
            if port:
                if len(driver.window_handles) > 1:
                    try:
                        driver.close()
                    except Exception:
                        pass
                detach_driver_safely(driver)
            else:
                try:
                    driver.quit()
                except Exception:
                    pass
            return False, f"Timed out after {timeout_seconds}s waiting for {mode} auto-login on '{profile_name}'."

    @staticmethod
    def login(profile_name, prefer_manual=False):
        """
        Single entry point for logging in to a profile:
          - If credentials.json exists and prefer_manual=False -> auto_login()
          - Otherwise -> interactive_login() (opens the login page for the human to complete)
        """
        if not prefer_manual and ProfileManager.load_credentials(profile_name):
            print(f"[ProfileManager] Found saved credentials for '{profile_name}' - attempting auto-login...")
            return ProfileManager.auto_login(profile_name)
        print(f"[ProfileManager] No saved credentials (or manual mode requested) - opening login page for '{profile_name}'...")
        return ProfileManager.interactive_login(profile_name)

    @staticmethod
    def warn_if_not_logged_in(profile_name, operation_name="this operation"):
        """
        Lightweight pre-flight check for scraping code: prints a warning (and returns False)
        if the given profile is not currently logged in. Does NOT trigger any login flow -
        callers decide whether to proceed anyway, abort, or call login()/auto_login() themselves.
        """
        status, msg = ProfileManager.check_login_status(profile_name, auto_prompt_login=False)
        if not status:
            print(
                f"\n[!] WARNING: '{operation_name}' typically requires being logged in to WikiHow, "
                f"but profile '{profile_name}' is NOT logged in.\n"
                f"    Run: python -m wikihow_scraper.cli profile login --name {profile_name}\n"
            )
        return status

    @staticmethod
    def _interactive_login_attached(profile_name, driver, timeout_seconds=600, poll_interval=5):
        """
        Polls a login tab in an ALREADY-ATTACHED driver until WikiHow shows a logged-in
        session, or timeout. Tolerates the JS alert WikiHow's Google-OAuth flow can throw.
        Never closes the tab itself (the user is actively logging in there).
        """
        print("\n" + "=" * 60)
        print(f" LOGIN TAB OPENED IN THE LIVE BROWSER FOR PROFILE: {profile_name}")
        print(" Log in there now (email/password is more reliable than Google OAuth).")
        print(f" Polling every {poll_interval}s for up to {timeout_seconds // 60} minutes...")
        print("=" * 60 + "\n")

        try:
            elapsed = 0
            while elapsed < timeout_seconds:
                time.sleep(poll_interval)
                elapsed += poll_interval

                dismissed = ProfileManager._dismiss_alert_if_any(driver)
                if dismissed:
                    print(f"  [!] Dismissed a JS alert: {dismissed!r}")
                    continue

                try:
                    soup = BeautifulSoup(driver.page_source, 'html.parser')
                except UnexpectedAlertPresentException:
                    continue

                page_html = str(soup).lower()
                user_el = ProfileManager._find_logged_in_indicator(soup)
                if user_el:
                    user_text = user_el.get_text().strip() if user_el else "Authenticated User"
                    provider = "WikiHow Account"
                    if "google" in page_html or "accounts.google" in page_html:
                        provider = "Google (Gmail)"
                    elif "facebook" in page_html:
                        provider = "Facebook"
                    ProfileManager.save_account_info(profile_name, provider=provider, account_name=user_text)
                    print(f"\n[SUCCESS] Login verified for profile '{profile_name}' ({provider})! Session saved.")
                    return True, f"Profile '{profile_name}' is now LOGGED IN ({user_text})."

                print(f"  [{elapsed}s] Still waiting for login...")

            return False, f"Timed out after {timeout_seconds}s waiting for login on profile '{profile_name}'."
        finally:
            detach_driver_safely(driver)

    @staticmethod
    def interactive_login(profile_name):
        """
        Interactive WikiHow login for a profile.
        If a BrowserWatchdog is already running for this profile, attaches to that SAME
        visible window (so a worker's login lands in the instance it will actually scrape
        with) instead of launching a second, conflicting Chrome process.
        """
        port = ProfileManager._get_running_watchdog_port(profile_name)
        if port:
            print(f"[ProfileManager] Watchdog is live on port {port} - attaching for login instead of a fresh launch.")
            driver = attach_driver(port)
            driver.execute_script("window.open('https://www.wikihow.com/Special:Userlogin', '_blank');")
            driver.switch_to.window(driver.window_handles[-1])
            return ProfileManager._interactive_login_attached(profile_name, driver)

        path = ProfileManager.get_profile_path(profile_name)
        if not os.path.exists(path):
            os.makedirs(path, exist_ok=True)

        print("\n" + "="*60)
        print(f" LAUNCHING HEADED INTERACTIVE LOGIN FOR PROFILE: {profile_name}")
        print("="*60)
        print(" A visible Chrome browser window will now open at the login screen.")
        print(" You can sign in using Google, Facebook, or WikiHow credentials.")
        print(" Once logged in, press Enter in this terminal to save the session.")
        print("="*60 + "\n")

        from wikihow_scraper.pid_tracker.pid_manager import BrowserWatchdog
        watchdog = BrowserWatchdog(profile_name, port=9099)
        watchdog.should_cleanup = False
        driver = watchdog.launch_browser()
        watchdog._write_tracker("healthy", watchdog._get_tabs() or [])
        driver.get("https://www.wikihow.com/Special:Userlogin")
        input("\n>>> Press ENTER after completing login in the browser window... <<<\n")

        soup = BeautifulSoup(driver.page_source, 'html.parser')
        page_html = str(soup).lower()

        user_el = ProfileManager._find_logged_in_indicator(soup)
        user_text = user_el.get_text().strip() if user_el else "Authenticated User"

        provider = "WikiHow Account"
        if "google" in page_html or "accounts.google" in page_html:
            provider = "Google (Gmail)"
        elif "facebook" in page_html:
            provider = "Facebook"

        ProfileManager.save_account_info(profile_name, provider=provider, account_name=user_text)
        print(f"\n[SUCCESS] Login verified for profile '{profile_name}' ({provider})! Session saved.")
        return True, f"Profile '{profile_name}' is now LOGGED IN ({user_text})."

    @staticmethod
    def interactive_menu():
        """Interactive Terminal TUI for managing profiles."""
        while True:
            print("\n" + "="*50)
            print(" WIKIHOW SCRAPER - PROFILE & ACCOUNT MANAGER")
            print("="*50)
            print("1. List existing profiles & connected accounts")
            print("2. Add a new profile")
            print("3. Check login status of a profile")
            print("4. Log in to WikiHow account (Google / Facebook / Email)")
            print("5. Delete a profile")
            print("6. Exit")
            print("="*50)
            
            choice = input("Enter choice (1-6): ").strip()
            
            if choice == "1":
                profiles = ProfileManager.list_profiles()
                print("\nExisting Profiles & Connected Accounts:")
                if not profiles:
                    print(" - No profiles found.")
                else:
                    for p in profiles:
                        print(f" - Profile: {p['profile_name']} | Provider: {p['provider']} | User: {p['account_name']}")
            
            elif choice == "2":
                name = input("\nEnter name for the new profile: ").strip()
                if not name:
                    print("Error: Name cannot be empty.")
                    continue
                provider = input("Enter account provider (e.g. Google, Facebook, Email) [default: Unknown]: ").strip() or "Unknown"
                user_email = input("Enter account email/username [optional]: ").strip() or "N/A"
                success, msg = ProfileManager.add_profile(name, provider=provider, account_name=user_email, email=user_email)
                print(msg)

            elif choice == "3":
                profiles = ProfileManager.list_profiles()
                if not profiles:
                    print("\nNo profiles exist.")
                    continue
                print("\nSelect profile to check login status:")
                for idx, p in enumerate(profiles, 1):
                    print(f"{idx}. {p['profile_name']} ({p['provider']})")
                try:
                    num = int(input(f"Enter number (1-{len(profiles)}): ").strip())
                    if 1 <= num <= len(profiles):
                        target = profiles[num-1]['profile_name']
                        status, msg = ProfileManager.check_login_status(target, auto_prompt_login=True)
                        print(f"\nResult: {msg}")
                except ValueError:
                    print("Invalid input.")

            elif choice == "4":
                profiles = ProfileManager.list_profiles()
                print("\nSelect or type profile name to log in:")
                if profiles:
                    for idx, p in enumerate(profiles, 1):
                        print(f"{idx}. {p['profile_name']} ({p['provider']})")
                    print(f"{len(profiles)+1}. Create new profile name")
                    try:
                        num = int(input(f"Enter choice (1-{len(profiles)+1}): ").strip())
                        if 1 <= num <= len(profiles):
                            target_name = profiles[num-1]['profile_name']
                        else:
                            target_name = input("Enter new profile name: ").strip()
                    except ValueError:
                        target_name = input("Enter profile name: ").strip()
                else:
                    target_name = input("Enter profile name: ").strip()
                    
                if target_name:
                    ProfileManager.interactive_login(target_name)

            elif choice == "5":
                profiles = ProfileManager.list_profiles()
                if not profiles:
                    print("\nNo profiles exist to delete.")
                    continue
                print("\nSelect a profile to delete:")
                for idx, p in enumerate(profiles, 1):
                    print(f"{idx}. {p['profile_name']}")
                try:
                    num = int(input(f"Enter number (1-{len(profiles)}): ").strip())
                    if 1 <= num <= len(profiles):
                        target_name = profiles[num-1]['profile_name']
                        confirm = input(f"Are you sure you want to delete profile '{target_name}'? (y/n): ").strip().lower()
                        if confirm == 'y':
                            success, msg = ProfileManager.delete_profile(target_name)
                            print(msg)
                        else:
                            print("Cancelled.")
                    else:
                        print("Invalid selection.")
                except ValueError:
                    print("Invalid input.")
                    
            elif choice == "6":
                print("\nExiting Profile Manager TUI.")
                break
            else:
                print("Invalid selection. Please try again.")
            
            input("\nPress Enter to continue...")
