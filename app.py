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

    def run(self):
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                SESSION_DIR,
                headless=True,
                args=["--no-sandbox"],
            )
            self.page = context.pages[0] if context.pages else context.new_page()
            self.page.goto("https://web.whatsapp.com")
            self.ready.set()

            while True:
                func, args, kwargs, result, done = self.jobs.get()
                try:
                    result["value"] = func(self.page, *args, **kwargs)
                except Exception as exc:  # noqa: BLE001
                    result["error"] = str(exc)
                finally:
                    done.set()

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
worker.ready.wait(timeout=60)


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
# NOTE: WhatsApp Web's DOM changes over time. If status detection or
# sending stops working, these selectors are the first place to check.
# ---------------------------------------------------------------------------
def _get_qr(page):
    el = page.query_selector("canvas")
    if el:
        return base64.b64encode(el.screenshot()).decode()
    return None


def _is_logged_in(page):
    return page.query_selector("#pane-side") is not None


def _send_message(page, phone, text):
    url = f"https://web.whatsapp.com/send?phone={phone}&text={urllib.parse.quote(text)}"
    page.goto(url)
    page.wait_for_selector('[data-icon="send"], [data-testid="send"]', timeout=20000)
    page.click('[data-icon="send"], [data-testid="send"]')
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
    img { max-width: 280px; display: block; margin: 16px 0; }
    input, textarea { width: 100%; padding: 8px; margin: 6px 0; box-sizing: border-box; }
    button { padding: 8px 16px; cursor: pointer; }
    #status { font-weight: bold; }
  </style>
</head>
<body>
  <h2>Remote WhatsApp Bridge</h2>
  <p id="status">Checking status…</p>
  <div id="qr-box"></div>

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

    async function poll() {
      const res = await fetch(withKey("/status"));
      const data = await res.json();
      if (data.logged_in) {
        document.getElementById("status").textContent = "Connected ✅";
        document.getElementById("qr-box").innerHTML = "";
        document.getElementById("send-box").style.display = "block";
      } else {
        document.getElementById("status").textContent = "Scan this QR code: WhatsApp → Linked Devices";
        document.getElementById("send-box").style.display = "none";
        const qrRes = await fetch(withKey("/qr"));
        const qrData = await qrRes.json();
        if (qrData.qr) {
          document.getElementById("qr-box").innerHTML =
            '<img src="data:image/png;base64,' + qrData.qr + '">';
        }
      }
      setTimeout(poll, 3000);
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
