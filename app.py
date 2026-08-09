"""
Remote WhatsApp Web bridge for GitHub Codespaces.

Runs a small Flask app that drives a headless Chromium browser (via
Playwright) loaded to web.whatsapp.com. You interact with WhatsApp
through the Codespaces-forwarded port in your OWN browser — the
automation and the WhatsApp Web session both live inside the
Codespace container, not on your local device.

SECURITY NOTE: whoever can reach this port can act as your WhatsApp
account. Keep the Codespaces port set to "Private" and set WA_APP_SECRET
below to something only you know. See README.md for full setup steps.
"""

import base64
import os
import queue
import threading
import urllib.parse

from flask import Flask, jsonify, render_template_string, request
from playwright.sync_api import sync_playwright

APP_SECRET = os.environ.get("WA_APP_SECRET", "")
SESSION_DIR = os.path.join(os.getcwd(), "wa_session")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Playwright must be driven from a single dedicated thread. Flask routes
# submit small jobs to this worker via a queue and block for the result.
# ---------------------------------------------------------------------------
class BrowserWorker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.jobs = queue.Queue()
        self.ready = threading.Event()
        self.page = None
        self.last_load_error = None

    def run(self):
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                SESSION_DIR,
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
                viewport={"width": 1280, "height": 900},
                user_agent=USER_AGENT,
            )
            self.page = context.pages[0] if context.pages else context.new_page()
            # Hide the most common automation fingerprint before any page loads.
            self.page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            self._load_whatsapp()
            # IMPORTANT: always reach the job loop, even if the initial load
            # above failed - otherwise every request hangs forever with no
            # way to retry or inspect what went wrong.
            self.ready.set()

            while True:
                func, args, kwargs, result, done = self.jobs.get()
                try:
                    result["value"] = func(self.page, *args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    result["error"] = str(exc)
                finally:
                    done.set()

    def _load_whatsapp(self):
        try:
            self.page.goto(
                "https://web.whatsapp.com",
                wait_until="domcontentloaded",
                timeout=60000,
            )
            self.last_load_error = None
        except Exception as exc:  # noqa: BLE001
            self.last_load_error = str(exc)

    def call(self, func, *args, timeout=30, **kwargs):
        result = {}
        done = threading.Event()
        self.jobs.put((func, args, kwargs, result, done))
        if not done.wait(timeout=timeout):
            raise TimeoutError("Browser worker timed out")
        if "error" in result:
            raise RuntimeError(result["error"])
        return result.get("value")


worker = BrowserWorker()
worker.start()
worker.ready.wait(timeout=70)


# ---------------------------------------------------------------------------
# Auth: every route except "/" requires the shared secret, since this
# port becomes reachable from the internet once forwarded.
# ---------------------------------------------------------------------------
@app.before_request
def check_secret():
    if request.path == "/":
        return
    if not APP_SECRET:
        return  # no secret set - fine only for quick throwaway local testing
    supplied = request.headers.get("X-App-Key") or request.args.get("key")
    if supplied != APP_SECRET:
        return jsonify({"error": "unauthorized"}), 401


# ---------------------------------------------------------------------------
# Browser actions (each runs on the worker thread via worker.call)
# NOTE: WhatsApp Web's DOM changes over time, and it sometimes shows an
# anti-automation warning instead of the QR to headless browsers. Use
# /debug below if the QR never appears - it shows exactly what the
# browser is rendering.
# ---------------------------------------------------------------------------
def _get_qr(page):
    try:
        el = page.wait_for_selector("canvas", timeout=5000)
    except Exception:  # noqa: BLE001
        return None
    if el is None:
        return None
    png = el.screenshot()
    if not png or len(png) < 200:  # blank/corrupt capture, e.g. mid-refresh
        return None
    return base64.b64encode(png).decode()


def _is_logged_in(page):
    return page.query_selector("#pane-side") is not None


def _send_message(page, phone, text):
    url = f"https://web.whatsapp.com/send?phone={phone}&text={urllib.parse.quote(text)}"
    page.goto(url)
    page.wait_for_selector('[data-icon="send"], [data-testid="send"]', timeout=20000)
    page.click('[data-icon="send"], [data-testid="send"]')
    return True


def _debug_info(page):
    return {
        "title": page.title(),
        "url": page.url,
        "load_error": worker.last_load_error,
        "screenshot": base64.b64encode(page.screenshot()).decode(),
    }


def _reload(page):
    worker._load_whatsapp()
    return True


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
PAGE = """
<!doctype html>
<html>
<head>
  <title>Remote WhatsApp Bridge</title>
  <style>
    body { font-family: system-ui, sans-serif; max-width: 480px; margin: 40px auto; padding: 0 16px; }
    img { max-width: 280px; display: block; margin: 16px 0; border: 1px solid #ddd; }
    input, textarea { width: 100%; padding: 8px; margin: 6px 0; box-sizing: border-box; }
    button { padding: 8px 16px; cursor: pointer; margin-right: 8px; }
    #status { font-weight: bold; }
    #debug-box { font-size: 12px; color: #444; white-space: pre-wrap; word-break: break-all; }
  </style>
</head>
<body>
  <h2>Remote WhatsApp Bridge</h2>
  <p id="status">Checking status…</p>
  <div id="qr-box"></div>
  <button onclick="reload_()">Reload WhatsApp page</button>
  <button onclick="showDebug()">View debug screenshot</button>
  <div id="debug-box"></div>

  <div id="send-box" style="display:none">
    <h3>Send a message</h3>
    <input id="phone" placeholder="Phone number with country code, digits only (e.g. 61412345678)">
    <textarea id="text" placeholder="Message text"></textarea>
    <button onclick="send()">Send</button>
    <p id="send-result"></p>
  </div>

  <script>
    const key = new URLSearchParams(location.search).get("key") || "";
    const withKey = (url) => url + (url.includes("?") ? "&" : "?") + "key=" + encodeURIComponent(key);
    let misses = 0;

    async function poll() {
      const res = await fetch(withKey("/status"));
      const data = await res.json();
      if (data.logged_in) {
        misses = 0;
        document.getElementById("status").textContent = "Connected ✅";
        document.getElementById("qr-box").innerHTML = "";
        document.getElementById("send-box").style.display = "block";
      } else {
        document.getElementById("send-box").style.display = "none";
        const qrRes = await fetch(withKey("/qr"));
        const qrData = await qrRes.json();
        if (qrData.qr) {
          misses = 0;
          document.getElementById("status").textContent = "Scan this QR code: WhatsApp → Linked Devices";
          document.getElementById("qr-box").innerHTML =
            '<img src="data:image/png;base64,' + qrData.qr + '">';
        } else {
          misses++;
          document.getElementById("status").textContent =
            misses > 3
              ? "Still no QR after several tries — click 'View debug screenshot' below"
              : "Loading WhatsApp Web…";
        }
      }
      setTimeout(poll, 3000);
    }

    async function reload_() {
      document.getElementById("status").textContent = "Reloading…";
      await fetch(withKey("/reload"), { method: "POST" });
      misses = 0;
    }

    async function showDebug() {
      const res = await fetch(withKey("/debug"));
      const data = await res.json();
      document.getElementById("debug-box").innerHTML =
        "<b>title:</b> " + data.title + "<br><b>url:</b> " + data.url +
        (data.load_error ? "<br><b>load_error:</b> " + data.load_error : "") +
        '<br><img src="data:image/png;base64,' + data.screenshot + '">';
    }

    async function send() {
      const phone = document.getElementById("phone").value;
      const text = document.getElementById("text").value;
      const res = await fetch(withKey("/send"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ phone, text }),
      });
      const data = await res.json();
      document.getElementById("send-result").textContent =
        data.sent ? "Sent ✅" : "Error: " + (data.error || "unknown");
    }

    poll();
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/status")
def status():
    return jsonify({"logged_in": worker.call(_is_logged_in)})


@app.route("/qr")
def qr():
    return jsonify({"qr": worker.call(_get_qr)})


@app.route("/debug")
def debug():
    return jsonify(worker.call(_debug_info, timeout=15))


@app.route("/reload", methods=["POST"])
def reload_route():
    worker.call(_reload, timeout=65)
    return jsonify({"ok": True})


@app.route("/send", methods=["POST"])
def send():
    data = request.get_json(force=True)
    phone = data.get("phone", "")
    text = data.get("text", "")
    if not phone or not text:
        return jsonify({"error": "phone and text are required"}), 400
    try:
        ok = worker.call(_send_message, phone, text, timeout=30)
        return jsonify({"sent": ok})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"sent": False, "error": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)