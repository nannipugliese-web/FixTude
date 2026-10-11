import os
import json
import re
import ssl
import time
import uuid
import smtplib
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from email.message import EmailMessage
from html import escape

from flask import (
    Flask, request, render_template_string, jsonify, Response,
    session, redirect, url_for, send_file
)

try:
    import stripe
except ImportError:
    stripe = None

try:
    from pywebpush import webpush, WebPushException
except ImportError:  # L’app resta avviabile anche prima dell’installazione della dipendenza.
    webpush = None
    WebPushException = Exception

app = Flask(__name__)

# =========================================================
# FIXTUDE - CALCOLATORE + RICONTATTO DIRETTO + CHAT LIVE
#
# Nessun database applicativo.
# Il calcolatore funziona nel browser.
# Il modulo di ricontatto invia i dati direttamente a Web3Forms via HTTPS.
# Le notifiche email della chat usano ancora SMTP.
# La chat usa esclusivamente memoria temporanea RAM.
# =========================================================

app.secret_key = os.environ.get("SECRET_KEY", "")

if not app.secret_key:
    # Per sicurezza, in produzione configurare SECRET_KEY su Render.
    app.secret_key = os.urandom(32).hex()

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = (
    os.environ.get("COOKIE_SECURE", "1" if os.environ.get("RENDER") else "0")
    == "1"
)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

# Destinatario di prova: dopo il test riuscito si può impostare
# EMAIL_DESTINATARIO=info@fixtude.it nelle variabili di Render.
EMAIL_DESTINATARIO = os.environ.get(
    "EMAIL_DESTINATARIO", "nannipugliese@gmail.com"
).strip()

ROME_TZ = ZoneInfo("Europe/Rome")


def ora_italiana():
    """Restituisce l'orario locale italiano, rispettando ora legale e solare."""
    return datetime.now(ROME_TZ).strftime("%H:%M")

SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)

CHAT_ADMIN_PASSWORD = os.environ.get("CHAT_ADMIN_PASSWORD", "")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "").strip()
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "").strip()
VAPID_CLAIMS_EMAIL = os.environ.get("VAPID_CLAIMS_EMAIL", "mailto:info@fixtude.it").strip()

CHAT_TTL_SECONDS = 2 * 60 * 60
MAX_CHAT_MESSAGES = 150
MAX_ACTIVE_CHATS = 200

chats = {}
chat_lock = threading.RLock()
# Le sottoscrizioni push sono temporanee: si azzerano al riavvio del servizio.
push_subscriptions = []
push_lock = threading.RLock()

# Contatori temporanei antispam: nessun file o database.
rate_limits = {}
rate_lock = threading.Lock()


def clean_old_data():
    """Rimuove chat inattive e vecchi contatori dalla memoria."""
    now = time.time()

    with chat_lock:
        expired = [
            chat_id for chat_id, chat in chats.items()
            if now - chat["updated"] > CHAT_TTL_SECONDS
        ]
        for chat_id in expired:
            chats.pop(chat_id, None)

    with rate_lock:
        expired_keys = [
            key for key, values in rate_limits.items()
            if not values or now - values[-1] > 3600
        ]
        for key in expired_keys:
            rate_limits.pop(key, None)


def rate_limited(action, limit, seconds):
    """Limite temporaneo per ridurre spam e invii ripetuti."""
    now = time.time()
    ip = request.remote_addr or "unknown"
    key = f"{action}:{ip}"

    with rate_lock:
        values = rate_limits.setdefault(key, [])
        values[:] = [v for v in values if now - v < seconds]

        if len(values) >= limit:
            return True

        values.append(now)
        return False


@app.before_request
def housekeeping():
    clean_old_data()


@app.after_request
def security_headers(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


def send_email(subject, body, reply_to=None):
    """Invia una email direttamente dal server, senza aprire la posta."""
    if not all([SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_FROM]):
        raise RuntimeError("Configurazione SMTP incompleta")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = EMAIL_DESTINATARIO

    if reply_to:
        msg["Reply-To"] = reply_to

    msg.set_content(body)

    context = ssl.create_default_context()

    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(
            SMTP_HOST, SMTP_PORT, context=context, timeout=15
        ) as server:
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
    else:
        with smtplib.SMTP(
            SMTP_HOST, SMTP_PORT, timeout=15
        ) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)


def send_chat_email_async(chat_id, message):
    """Invia una notifica email per ogni nuovo messaggio del visitatore."""
    def worker():
        try:
            body = (
                "È arrivato un nuovo messaggio nella chat FixTude.\n\n"
                f"Conversazione: {chat_id[:8]}\n"
                f"Orario: {ora_italiana()}\n\n"
                "Messaggio ricevuto:\n"
                f"{message}\n\n"
                "Accedi all'area riservata per rispondere:\n"
                "https://fixtude.it/chat-staff\n"
            )
            send_email(
                subject="FixTude - nuovo messaggio in chat",
                body=body
            )
            app.logger.info(
                "Email notifica chat inviata per la conversazione %s",
                chat_id[:8]
            )
        except Exception as exc:
            app.logger.error(
                "Invio email notifica chat fallito (%s): %s",
                type(exc).__name__, str(exc)
            )

    threading.Thread(target=worker, daemon=True).start()


def valid_email(value):
    return bool(
        value
        and len(value) <= 254
        and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value)
    )


def page(title, content, include_chat=True):
    """Layout comune, responsive e con pulsante chat."""
    return render_template_string(
        """
        <!doctype html>
        <html lang="it">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <meta name="theme-color" content="#123a72">
          <title>{{ title }} | FixTude</title>
          <meta name="description"
                content="FixTude: fai chiarezza sulla tua situazione debitoria.">
          <style>
            :root {
              --blue:#174b91;
              --blue2:#2468b8;
              --ink:#203047;
              --muted:#65758b;
              --bg:#f5f8fc;
              --line:#dce5ef;
              --white:#fff;
              --green:#16794b;
              --red:#a62929;
            }
            * {box-sizing:border-box}
            html {scroll-behavior:smooth}
            body {
              margin:0;
              font-family:Arial,Helvetica,sans-serif;
              color:var(--ink);
              background:var(--bg);
              line-height:1.55;
            }
            a {color:var(--blue)}
            header {
              background:var(--white);
              border-bottom:1px solid var(--line);
            }
            .nav {
              max-width:1080px;
              margin:auto;
              padding:15px 22px;
              display:flex;
              align-items:center;
              justify-content:space-between;
              gap:20px;
            }
            .brand {
              color:var(--blue);
              font-size:25px;
              font-weight:800;
              letter-spacing:-.8px;
              text-decoration:none;
            }
            nav {display:flex;gap:20px;flex-wrap:wrap}
            nav a {
              text-decoration:none;
              color:var(--ink);
              font-size:14px;
              font-weight:700;
            }
            nav a:hover {color:var(--blue2)}
            main {
              width:min(100% - 36px,960px);
              margin:36px auto 65px;
              min-height:55vh;
            }
            .hero {
              background:linear-gradient(135deg,#123a72,#2873bf);
              color:#fff;
              padding:clamp(26px,5vw,54px);
              border-radius:20px;
            }
            .hero h1 {
              font-size:clamp(30px,5vw,47px);
              line-height:1.12;
              letter-spacing:-1px;
              margin:0 0 18px;
              max-width:720px;
            }
            .hero p {font-size:18px;max-width:660px;margin:0 0 24px}
            h1,h2,h3 {line-height:1.25}
            h1 {font-size:clamp(27px,4vw,37px);margin-top:0}
            h2 {font-size:23px}
            .muted {color:var(--muted)}
            .card {
              background:#fff;
              border:1px solid var(--line);
              border-radius:16px;
              padding:24px;
              margin:20px 0;
              box-shadow:0 4px 18px rgba(20,48,85,.035);
            }
            .grid {
              display:grid;
              grid-template-columns:repeat(2,minmax(0,1fr));
              gap:18px;
            }
            .btn {
              display:inline-flex;
              justify-content:center;
              align-items:center;
              border:0;
              border-radius:10px;
              background:var(--blue);
              color:white;
              padding:13px 19px;
              min-height:46px;
              font-size:15px;
              font-weight:700;
              text-decoration:none;
              cursor:pointer;
            }
            .btn:hover {background:var(--blue2)}
            .btn.light {background:white;color:var(--blue)}
            .btn.outline {
              background:white;
              border:1px solid var(--line);
              color:var(--blue);
            }
            .actions {display:flex;gap:10px;flex-wrap:wrap}
            label {
              display:block;
              font-weight:700;
              font-size:14px;
              margin:0 0 7px;
            }
            input,select,textarea {
              width:100%;
              min-width:0;
              border:1px solid #cbd7e5;
              border-radius:9px;
              padding:12px;
              background:#fff;
              color:var(--ink);
              font:inherit;
            }
            input:focus,select:focus,textarea:focus {
              outline:2px solid #9cc7f4;
              border-color:var(--blue2);
            }
            textarea {min-height:120px;resize:vertical}
            .field {min-width:0;margin-bottom:16px}
            .check {
              display:flex;
              align-items:flex-start;
              gap:10px;
              font-size:14px;
              font-weight:400;
            }
            .check input {width:auto;margin-top:5px}
            .notice {
              border-radius:10px;
              padding:13px 15px;
              background:#eef5ff;
              border:1px solid #d4e5fb;
              margin:16px 0;
            }
            .success {color:var(--green);font-weight:700}
            .error {color:var(--red);font-weight:700}
            .entry-row {
              display:grid;
              grid-template-columns:minmax(0,1.5fr) minmax(100px,1fr) auto;
              gap:10px;
              align-items:end;
              padding:12px 0;
              border-bottom:1px solid var(--line);
            }
            .entry-row .field {margin:0}
            .remove-button {
              border:1px solid var(--line);
              background:#fff;
              border-radius:8px;
              padding:10px 12px;
              cursor:pointer;
            }
            .total-grid {
              display:grid;
              grid-template-columns:repeat(2,minmax(0,1fr));
              gap:12px;
              margin:18px 0;
            }
            .total {
              border:1px solid var(--line);
              border-radius:12px;
              padding:16px;
              background:#fff;
              overflow-wrap:anywhere;
            }
            .total strong {display:block;font-size:25px;margin-top:4px}
            footer {
              border-top:1px solid var(--line);
              background:#fff;
              padding:24px 18px;
              text-align:center;
              font-size:13px;
              color:var(--muted);
            }
            .chat-launcher {
              position:fixed;
              right:20px;
              bottom:20px;
              z-index:1000;
              border:0;
              border-radius:30px;
              background:var(--blue);
              color:#fff;
              box-shadow:0 5px 22px #0002;
              padding:14px 20px;
              font-weight:700;
              cursor:pointer;
            }
            .chat-panel {
              position:fixed;
              z-index:1001;
              right:20px;
              bottom:82px;
              width:min(365px,calc(100vw - 28px));
              height:min(500px,calc(100dvh - 115px));
              background:#fff;
              border:1px solid var(--line);
              border-radius:16px;
              box-shadow:0 12px 45px #0003;
              display:none;
              flex-direction:column;
              overflow:hidden;
            }
            .chat-panel.open {display:flex}
            .chat-head {
              background:var(--blue);
              color:#fff;
              padding:15px;
              display:flex;
              align-items:center;
              justify-content:space-between;
              gap:10px;
            }
            .chat-head button {
              color:#fff;
              background:transparent;
              border:0;
              font-size:24px;
              cursor:pointer;
            }
            .chat-status {font-size:12px;opacity:.9}
            .chat-messages {
              flex:1;
              overflow:auto;
              padding:13px;
              background:#f6f8fb;
            }
            .chat-msg {
              max-width:88%;
              padding:10px 12px;
              border-radius:12px;
              margin:8px 0;
              overflow-wrap:anywhere;
              white-space:pre-wrap;
              font-size:14px;
            }
            .chat-msg.visitor {
              background:#dceaff;
              margin-left:auto;
            }
            .chat-msg.staff {
              background:#fff;
              border:1px solid var(--line);
            }
            .chat-time {font-size:10px;color:var(--muted);margin-top:4px}
            .chat-form {
              padding:10px;
              border-top:1px solid var(--line);
              display:flex;
              gap:8px;
            }
            .chat-form input {flex:1;min-width:0;padding:10px}
            .chat-form button {padding:10px 13px}
            .chat-hint {font-size:11px;color:var(--muted);padding:0 12px 10px}
            .product-card {display:grid;grid-template-columns:minmax(0,1fr) auto;gap:18px;align-items:center}
            .price {font-size:30px;font-weight:800;color:var(--blue);white-space:nowrap}
            .resource-list {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
            .resource-link {display:block;padding:17px;border:1px solid var(--line);border-radius:12px;background:#fff;text-decoration:none;font-weight:700}
            .resource-link span {display:block;font-weight:400;color:var(--muted);font-size:14px;margin-top:5px}
            @media(max-width:650px) {
              nav {width:100%;gap:9px 13px}
              nav a {font-size:13px}
              .product-card {grid-template-columns:1fr}
              .resource-list {grid-template-columns:1fr}
              .nav {align-items:flex-start;flex-direction:column;gap:9px}
              nav {gap:10px 16px}
              main {width:calc(100% - 24px);margin:20px auto 45px}
              .grid {grid-template-columns:1fr}
              .card {padding:18px}
              .entry-row {grid-template-columns:minmax(0,1fr) minmax(0,1fr)}
              .entry-row .name-field {grid-column:1 / -1}
              .entry-row .remove-button {grid-column:2;justify-self:end}
              .hero {border-radius:14px}
              .hero p {font-size:16px}
              .chat-launcher {right:13px;bottom:13px}
              .chat-panel {right:8px;bottom:70px;width:calc(100vw - 16px)}
            }
            /* Home: spazi più equilibrati e comandi comodi su schermi piccoli */
            .hero {display:flex;flex-direction:column;gap:0;padding:clamp(24px,4vw,42px)}
            .hero h1 {margin-bottom:12px}
            .hero p {margin-bottom:20px}
            .hero .actions {margin-top:2px;gap:12px}
            .hero .actions .btn {min-height:48px}
            .start-card .grid {row-gap:10px;column-gap:24px}
            .start-card .grid > div {min-width:0;padding:4px 0}
            .start-card .grid p {margin:7px 0 0}
            @media(max-width:650px) {
              main {width:calc(100% - 24px);margin:18px auto 42px}
              .hero {padding:24px 19px;border-radius:15px}
              .hero h1 {font-size:clamp(28px,8vw,35px);line-height:1.12;letter-spacing:-.5px;margin-bottom:12px}
              .hero p {font-size:16px;line-height:1.5;margin-bottom:18px}
              .hero .actions {display:grid;grid-template-columns:1fr;gap:10px;width:100%}
              .hero .actions .btn {width:100%;padding:12px 14px;text-align:center;white-space:normal}
              .start-card {padding:18px 17px}
              .start-card h2 {margin-top:0;font-size:21px}
              .start-card .grid {grid-template-columns:1fr;gap:14px}
              .start-card .grid > div {padding:0 0 12px;border-bottom:1px solid var(--line)}
              .start-card .grid > div:last-child {padding-bottom:0;border-bottom:0}
            }
          </style>
        </head>
        <body>
          <header>
            <div class="nav">
              <a class="brand" href="/">FixTude</a>
              <nav>
                <a href="/">Home</a>
                <a href="/misuratore">Calcolatore</a>
                <a href="/consulenza">Ricontatto</a>
                <a href="/moduli">Moduli gratuiti</a>
                <a href="/educazione-finanziaria">Educazione finanziaria</a>
                <a href="/sic">Banche dati SIC</a>
                <a href="/shop">Shop</a>
                <a href="/privacy">Privacy</a>
              </nav>
            </div>
          </header>
          <main>{{ content|safe }}</main>
          <footer>
            FixTude · Informazione orientativa, senza impegno.
            <br>Partita IVA 15990471003 ·
            <a href="/privacy">Informativa privacy</a>
          </footer>
          {% if include_chat %}
          <button class="chat-launcher" id="chatLauncher"
                  type="button" aria-expanded="false">
            💬 Chat FixTude
          </button>
          <section class="chat-panel" id="chatPanel"
                   aria-label="Chat FixTude">
            <div class="chat-head">
              <div>
                <strong>Chat FixTude</strong>
                <div class="chat-status" id="chatStatus">
                  Scrivi un messaggio
                </div>
              </div>
              <button id="chatClose" type="button"
                      aria-label="Chiudi chat">×</button>
            </div>
            <div class="chat-messages" id="chatMessages"
                 aria-live="polite">
              <p class="muted">Connessione alla chat in corso…</p>
            </div>
            <form class="chat-form" id="chatForm">
              <input id="chatInput" maxlength="1000"
                     placeholder="Scrivi qui…" autocomplete="off"
                     aria-label="Messaggio" required>
              <button class="btn" type="submit">Invia</button>
            </form>
            <div class="chat-hint">
              Non inserire dati bancari o documenti personali.
              I messaggi sono temporanei e non costituiscono
              un servizio di emergenza.
            </div>
          </section>
          <script>
          (() => {
            const panel = document.getElementById("chatPanel");
            const launcher = document.getElementById("chatLauncher");
            const close = document.getElementById("chatClose");
            const messages = document.getElementById("chatMessages");
            const form = document.getElementById("chatForm");
            const input = document.getElementById("chatInput");
            const status = document.getElementById("chatStatus");

            let chatId = "";
            let polling = false;
            let lastCount = -1;

            function newId() {
              if (window.crypto && crypto.randomUUID) {
                return crypto.randomUUID().replaceAll("-", "");
              }
              return Array.from(crypto.getRandomValues(new Uint8Array(24)))
                .map(n => n.toString(16).padStart(2, "0")).join("");
            }

            async function ensureChat() {
              try {
                chatId = localStorage.getItem("fixtude_chat_id") || "";
              } catch (_) {
                chatId = "";
              }

              if (!/^[a-f0-9-]{32,36}$/i.test(chatId)) {
                chatId = newId();
                try {
                  localStorage.setItem("fixtude_chat_id", chatId);
                } catch (_) {}
              }

              const r = await fetch("/api/chat/start", {
                method:"POST",
                headers:{"Content-Type":"application/json"},
                body:JSON.stringify({chat_id:chatId})
              });

              if (!r.ok) throw new Error("Chat non disponibile");
              const data = await r.json();
              status.textContent = data.status || "Scrivi un messaggio";
            }

            function addMessage(m) {
              const wrap = document.createElement("div");
              wrap.className = "chat-msg " +
                (m.sender === "staff" ? "staff" : "visitor");

              const body = document.createElement("div");
              body.textContent = m.text;
              wrap.appendChild(body);

              const time = document.createElement("div");
              time.className = "chat-time";
              time.textContent = m.time || "";
              wrap.appendChild(time);
              messages.appendChild(wrap);
            }

            async function refreshMessages() {
              if (!chatId || polling) return;
              polling = true;

              try {
                const r = await fetch(
                  "/api/chat/" + encodeURIComponent(chatId) + "/messages",
                  {cache:"no-store"}
                );
                if (!r.ok) throw new Error("Chat temporaneamente non disponibile");
                const data = await r.json();

                if (data.messages.length !== lastCount) {
                  messages.replaceChildren();
                  if (data.messages.length === 0) {
                    const p = document.createElement("p");
                    p.className = "muted";
                    p.textContent =
                      "Ciao! Scrivi qui la tua domanda. Ti risponderemo quando saremo disponibili.";
                    messages.appendChild(p);
                  } else {
                    data.messages.forEach(addMessage);
                  }
                  lastCount = data.messages.length;
                  messages.scrollTop = messages.scrollHeight;
                }

                status.textContent = data.status ||
                  "Messaggi temporanei";
              } catch (_) {
                status.textContent = "Connessione in attesa";
              } finally {
                polling = false;
              }
            }

            launcher.addEventListener("click", async () => {
              panel.classList.toggle("open");
              launcher.setAttribute(
                "aria-expanded", panel.classList.contains("open")
              );
              if (panel.classList.contains("open")) {
                try {
                  await ensureChat();
                  await refreshMessages();
                  input.focus();
                } catch (_) {
                  status.textContent = "Chat momentaneamente non disponibile";
                }
              }
            });

            close.addEventListener("click", () => {
              panel.classList.remove("open");
              launcher.setAttribute("aria-expanded", "false");
            });

            form.addEventListener("submit", async e => {
              e.preventDefault();
              const text = input.value.trim();
              if (!text || text.length > 1000) return;

              const button = form.querySelector("button");
              button.disabled = true;

              try {
                if (!chatId) await ensureChat();

                const r = await fetch("/api/chat/send", {
                  method:"POST",
                  headers:{"Content-Type":"application/json"},
                  body:JSON.stringify({chat_id:chatId,text})
                });
                const data = await r.json();

                if (!r.ok) throw new Error(data.error || "Invio non riuscito");

                // La notifica email viene inviata dal browser via HTTPS,
                // evitando SMTP in uscita da Render.
                try {
                  const notification = await fetch("https://api.web3forms.com/submit", {
                    method: "POST",
                    headers: {
                      "Content-Type": "application/json",
                      "Accept": "application/json"
                    },
                    body: JSON.stringify({
                      access_key: "6cfd757e-6b16-49e0-96b5-e055a159f403",
                      subject: "FixTude - nuovo messaggio in chat",
                      from_name: "FixTude - chat cliente",
                      message: "È arrivato un nuovo messaggio nella chat FixTude.\\n\\n" +
                        "Conversazione: " + chatId.slice(0, 8) + "\\n" +
                        "Orario: " + new Date().toLocaleString("it-IT") + "\\n\\n" +
                        "Messaggio ricevuto:\\n" + text + "\\n\\n" +
                        "Rispondi dall'area staff: https://fixtude.it/chat-staff"
                    })
                  });
                  const notificationData = await notification.json();
                  if (!notification.ok || notificationData.success !== true) {
                    console.error("Notifica email chat non confermata:", notificationData.message || notification.status);
                    status.textContent = "Messaggio inviato, ma la notifica email non è stata confermata.";
                  }
                } catch (notificationError) {
                  console.error("Errore notifica email chat:", notificationError);
                  status.textContent = "Messaggio inviato, ma non è stato possibile confermare la notifica email.";
                }

                input.value = "";
                await refreshMessages();
              } catch (err) {
                status.textContent = err.message ||
                  "Invio non riuscito. Riprova.";
              } finally {
                button.disabled = false;
              }
            });

            setInterval(() => {
              if (panel.classList.contains("open")) refreshMessages();
            }, 2500);
          })();
          </script>
          {% endif %}
        </body>
        </html>
        """,
        title=title,
        content=content,
        include_chat=include_chat
    )


# =========================================================
# HOME
# =========================================================

@app.route("/")
def home():
    content = """
    <section class="hero">
      <h1>Fai chiarezza sulla tua situazione debitoria.</h1>
      <p>Inserisci entrate, uscite e rate per capire quanto ti rimane ogni mese e valutare in modo indicativo la sostenibilità dei tuoi impegni.</p>
      <div class="actions"><a class="btn light" href="/misuratore">Prova il calcolatore gratuito</a><a class="btn outline contact-cta" href="/consulenza">Richiedi di essere contattato</a></div>
    </section>
    <section class="card start-card"><h2>Da dove cominciare</h2><div class="grid">
      <div><strong>1. Fai il calcolo</strong><p class="muted">Usa gratuitamente il calcolatore. I dati inseriti restano nel browser e non serve registrarsi.</p></div>
      <div><strong>2. Valuta il risultato</strong><p class="muted">Ottieni una stima orientativa del margine mensile e del peso delle rate.</p></div>
      <div><strong>3. Se vuoi, chiedi di essere ricontattato</strong><p class="muted">Lascia i tuoi recapiti e una breve descrizione della situazione. Il contatto è facoltativo e non comporta obblighi.</p></div>
      <div><strong>4. Hai una domanda?</strong><p class="muted">Apri la Chat Live FixTude in basso a destra per inviare un messaggio allo staff. La chat è disponibile senza registrazione.</p></div>
    </div></section>
    <p class="muted">Il calcolatore e i materiali hanno finalità informative e orientative. Non costituiscono consulenza legale o finanziaria personalizzata e non garantiscono l'accettazione di proposte da parte dei creditori.</p>
    """
    return page("Fai chiarezza", content)


# =========================================================
# CALCOLATORE
# =========================================================

@app.route("/misuratore")
def misuratore():
    content = """
    <h1>Calcola quanto ti rimane ogni mese</h1>
    <p class="muted">
      Inserisci importi mensili netti. Puoi aggiungere o rimuovere
      voci. I calcoli avvengono nel browser e non vengono inviati
      al server.
    </p>

    <section class="card">
      <h2>Entrate mensili</h2>
      <div id="incomeRows"></div>
      <button class="btn outline" type="button" id="addIncome">
        + Aggiungi entrata
      </button>
    </section>

    <section class="card">
      <h2>Spese mensili</h2>
      <div id="expenseRows"></div>
      <button class="btn outline" type="button" id="addExpense">
        + Aggiungi spesa
      </button>
    </section>

    <section class="card">
      <h2>Debiti e rate mensili</h2>
      <p class="muted">
        Inserisci la rata mensile di ciascun debito. Il saldo residuo
        del debito non è necessario per questo calcolo.
      </p>
      <div id="debtRows"></div>
      <button class="btn outline" type="button" id="addDebt">
        + Aggiungi debito
      </button>
    </section>

    <section class="card">
      <h2>Il tuo risultato</h2>
      <div class="total-grid">
        <div class="total">
          <span class="muted">Entrate totali</span>
          <strong id="totalIncome">€ 0,00</strong>
        </div>
        <div class="total">
          <span class="muted">Spese totali</span>
          <strong id="totalExpenses">€ 0,00</strong>
        </div>
        <div class="total">
          <span class="muted">Rate totali</span>
          <strong id="totalPayments">€ 0,00</strong>
        </div>
        <div class="total">
          <span class="muted">Residuo mensile</span>
          <strong id="residual">€ 0,00</strong>
        </div>
      </div>
      <div class="notice" id="assessment">
        Inserisci i tuoi importi per ottenere una valutazione indicativa.
      </div>
      <button class="btn" type="button" id="resetCalculator">
        Azzera i campi
      </button>
    </section>

    <section class="card">
      <h2>Vuoi approfondire?</h2>
      <p>
        Se lo desideri, puoi chiedere di essere ricontattato
        senza impegno.
      </p>
      <a class="btn" href="/consulenza">Vai al modulo di ricontatto</a>
    </section>

    <script>
    (() => {
      const definitions = {
        income: [
          ["Stipendio o pensione", 0],
          ["Altre entrate", 0]
        ],
        expense: [
          ["Casa e utenze", 0],
          ["Alimentari e necessità", 0],
          ["Trasporti", 0],
          ["Altre spese", 0]
        ],
        debt: [
          ["Rata mensile", 0]
        ]
      };

      const labels = {
        income: "Entrata",
        expense: "Spesa",
        debt: "Debito"
      };

      function money(n) {
        return n.toLocaleString("it-IT", {
          style:"currency", currency:"EUR"
        });
      }

      function addRow(type, label="", amount=0) {
        const container = document.getElementById(
          type === "income" ? "incomeRows" :
          type === "expense" ? "expenseRows" : "debtRows"
        );

        const row = document.createElement("div");
        row.className = "entry-row";

        const nameField = document.createElement("div");
        nameField.className = "field name-field";

        const nameLabel = document.createElement("label");
        nameLabel.textContent = type === "debt"
          ? "Nome del debito (facoltativo)" : "Voce";
        nameField.appendChild(nameLabel);

        const nameInput = document.createElement("input");
        nameInput.type = "text";
        nameInput.maxLength = 80;
        nameInput.value = label;
        nameInput.placeholder = labels[type];
        nameInput.setAttribute("aria-label", "Descrizione");
        nameField.appendChild(nameInput);

        const amountField = document.createElement("div");
        amountField.className = "field";

        const amountLabel = document.createElement("label");
        amountLabel.textContent = type === "debt"
          ? "Rata mensile (€)" : "Importo mensile (€)";
        amountField.appendChild(amountLabel);

        const amountInput = document.createElement("input");
        amountInput.type = "number";
        amountInput.min = "0";
        amountInput.max = "100000000";
        amountInput.step = "0.01";
        amountInput.inputMode = "decimal";
        amountInput.value = amount;
        amountInput.setAttribute("aria-label", "Importo in euro");
        amountField.appendChild(amountInput);

        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "remove-button";
        remove.textContent = "Rimuovi";
        remove.setAttribute("aria-label", "Rimuovi voce");
        remove.addEventListener("click", () => {
          row.remove();
          calculate();
        });

        row.append(nameField, amountField, remove);
        container.appendChild(row);

        amountInput.addEventListener("input", calculate);
        nameInput.addEventListener("input", calculate);
        calculate();
      }

      function sumRows(id) {
        let total = 0;
        document.querySelectorAll("#" + id + " .entry-row input[type=number]")
          .forEach(input => {
            const value = Number(input.value);
            if (Number.isFinite(value) && value > 0) {
              total += Math.min(value, 100000000);
            }
          });
        return total;
      }

      function calculate() {
        const income = sumRows("incomeRows");
        const expenses = sumRows("expenseRows");
        const payments = sumRows("debtRows");
        const residual = income - expenses - payments;
        const ratio = income > 0 ? payments / income : null;

        document.getElementById("totalIncome").textContent = money(income);
        document.getElementById("totalExpenses").textContent = money(expenses);
        document.getElementById("totalPayments").textContent = money(payments);
        document.getElementById("residual").textContent = money(residual);

        const result = document.getElementById("assessment");

        if (income <= 0) {
          result.textContent =
            "Inserisci almeno un'entrata mensile positiva per ottenere una valutazione.";
          return;
        }

        if (residual < 0 || ratio >= 0.40) {
          result.textContent =
            "Situazione da approfondire: il margine mensile è negativo oppure le rate assorbono una quota elevata delle entrate.";
        } else if (residual < income * 0.10 || ratio >= 0.25) {
          result.textContent =
            "Margine ridotto: le spese e le rate lasciano una disponibilità limitata. Valuta con attenzione il bilancio.";
        } else {
          result.textContent =
            "In base ai soli importi inseriti, il margine appare meno sotto pressione. La valutazione non considera tutte le possibili esigenze o variazioni future.";
        }

        result.textContent +=
          " Si tratta di una stima orientativa, non di una certificazione della sostenibilità del debito.";
      }

      document.getElementById("addIncome").addEventListener("click", () =>
        addRow("income", "Nuova entrata", 0)
      );
      document.getElementById("addExpense").addEventListener("click", () =>
        addRow("expense", "Nuova spesa", 0)
      );
      document.getElementById("addDebt").addEventListener("click", () =>
        addRow("debt", "Nuovo debito", 0)
      );

      document.getElementById("resetCalculator").addEventListener("click", () => {
        document.getElementById("incomeRows").replaceChildren();
        document.getElementById("expenseRows").replaceChildren();
        document.getElementById("debtRows").replaceChildren();

        definitions.income.forEach(([label, value]) =>
          addRow("income", label, value)
        );
        definitions.expense.forEach(([label, value]) =>
          addRow("expense", label, value)
        );
        definitions.debt.forEach(([label, value]) =>
          addRow("debt", label, value)
        );
      });

      document.getElementById("resetCalculator").click();
    })();
    </script>
    """
    return page("Calcolatore", content)


# =========================================================
# RICONTATTO
# =========================================================

@app.route("/consulenza")
def consulenza():
    content = """
    <h1>Richiedi di essere ricontattato</h1>
    <p class="muted">
      Compila il modulo soltanto se desideri ricevere un contatto.
      La richiesta è gratuita e non ti obbliga a proseguire.
    </p>

    <section class="card">
      <form id="contactForm">
        <div class="field">
          <label for="name">Nome</label>
          <input id="name" name="name" maxlength="100"
                 autocomplete="name" required>
        </div>
        <div class="grid">
          <div class="field">
            <label for="email">Email</label>
            <input id="email" name="email" type="email"
                   maxlength="254" autocomplete="email" required>
          </div>
          <div class="field">
            <label for="phone">Telefono (facoltativo)</label>
            <input id="phone" name="phone" type="tel"
                   maxlength="40" autocomplete="tel">
          </div>
        </div>
        <div class="field">
          <label for="message">Messaggio (facoltativo)</label>
          <textarea id="message" name="message" maxlength="2000"
                    placeholder="Scrivi qui come preferisci essere ricontattato. Non inserire dati bancari o documenti."></textarea>
        </div>

        <div style="position:absolute;left:-10000px" aria-hidden="true">
          <label for="website">Non compilare questo campo</label>
          <input id="website" name="website" tabindex="-1"
                 autocomplete="off">
        </div>
        <input type="checkbox" name="botcheck" tabindex="-1"
               autocomplete="off" style="display:none" aria-hidden="true">

        <label class="check">
          <input type="checkbox" name="privacy" required>
          <span>
            Dichiaro di aver letto l'
            <a href="/privacy" target="_blank" rel="noopener">
              informativa privacy
            </a>
            e chiedo di essere ricontattato.
          </span>
        </label>

        <p class="muted" style="font-size:13px">
          I dati del modulo vengono inviati via email per gestire
          la tua richiesta. L'applicazione non conserva una copia
          in un database.
        </p>

        <div id="contactResult" role="status" aria-live="polite"></div>
        <button class="btn" id="contactSubmit" type="submit">
          Invia richiesta
        </button>
      </form>
    </section>

    <script>
    (() => {
      const form = document.getElementById("contactForm");
      const result = document.getElementById("contactResult");
      const button = document.getElementById("contactSubmit");
      const accessKey = "6cfd757e-6b16-49e0-96b5-e055a159f403";

      form.addEventListener("submit", async e => {
        e.preventDefault();
        result.className = "";
        result.textContent = "";

        if (!form.reportValidity()) return;

        const values = Object.fromEntries(new FormData(form).entries());

        // Honeypot: se compilato da un bot, mostra un esito neutro senza inviare.
        if (String(values.website || "").trim()) {
          result.className = "notice success";
          result.textContent =
            "Richiesta inviata correttamente. Grazie, ti ricontatteremo appena possibile.";
          form.reset();
          return;
        }

        values.access_key = accessKey;
        values.subject = "FixTude - nuova richiesta di ricontatto";
        values.from_name = "FixTude - modulo di ricontatto";
        values.botcheck = values.botcheck || "";
        delete values.website;

        button.disabled = true;
        button.textContent = "Invio in corso…";

        try {
          const response = await fetch("https://api.web3forms.com/submit", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Accept": "application/json"
            },
            body: JSON.stringify(values)
          });

          const data = await response.json();
          if (!response.ok || data.success !== true) {
            throw new Error(data.message || "Non è stato possibile inviare la richiesta.");
          }

          result.className = "notice success";
          result.textContent =
            "Richiesta inviata correttamente. Grazie, ti ricontatteremo appena possibile.";
          form.reset();
        } catch (err) {
          result.className = "notice error";
          result.textContent = err.message ||
            "Errore di connessione. Riprova più tardi.";
        } finally {
          button.disabled = false;
          button.textContent = "Invia richiesta";
        }
      });
    })();
    </script>
    """
    return page("Ricontatto", content)


@app.route("/api/contact", methods=["POST"])
def contact_send():
    if rate_limited("contact", 4, 600):
        return jsonify(error="Hai effettuato troppe richieste. Riprova più tardi."), 429

    data = request.get_json(silent=True) or {}

    # Campo trappola per bot.
    if str(data.get("website", "")).strip():
        return jsonify(ok=True), 200

    name = str(data.get("name", "")).strip()
    email = str(data.get("email", "")).strip()
    phone = str(data.get("phone", "")).strip()
    message = str(data.get("message", "")).strip()
    privacy = data.get("privacy")

    if not name or len(name) > 100:
        return jsonify(error="Inserisci un nome valido."), 400

    if not valid_email(email):
        return jsonify(error="Inserisci un indirizzo email valido."), 400

    if len(phone) > 40 or len(message) > 2000:
        return jsonify(error="Uno dei campi supera la lunghezza consentita."), 400

    if privacy not in (True, "on", "true", "1"):
        return jsonify(error="Per inviare la richiesta devi confermare di aver letto l'informativa."), 400

    # Evita header email controllati dall'utente.
    if "\r" in email or "\n" in email:
        return jsonify(error="Indirizzo email non valido."), 400

    body = (
        "Nuova richiesta di ricontatto dal sito FixTude\n\n"
        f"Nome: {name}\n"
        f"Email: {email}\n"
        f"Telefono: {phone or 'Non indicato'}\n\n"
        "Messaggio:\n"
        f"{message or 'Nessun messaggio aggiuntivo'}\n"
    )

    try:
        send_email(
            subject="FixTude - nuova richiesta di ricontatto",
            body=body,
            reply_to=email
        )
    except Exception as exc:
        # Registriamo solo il tipo e il messaggio tecnico dell'errore SMTP,
        # senza riportare nei log i dati personali inseriti nel modulo.
        app.logger.error(
            "Invio email di ricontatto fallito: %s: %s",
            type(exc).__name__,
            str(exc)
        )
        return jsonify(
            error="Al momento non è possibile inviare la richiesta. Riprova più tardi."
        ), 503

    # Nessun salvataggio dei campi nel database o in un file.
    return jsonify(ok=True), 200


# =========================================================
# NOTIFICHE PUSH PER L'AREA STAFF
# =========================================================

def send_push_notification(title, body, url="/chat-staff"):
    """Invia una notifica a tutte le sottoscrizioni attive in memoria."""
    if not (webpush and VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY):
        app.logger.warning("Notifiche push non inviate: configurazione o dipendenza mancante.")
        return

    payload = {"title": title, "body": body, "url": url}
    with push_lock:
        subscriptions = list(push_subscriptions)

    expired = []
    for subscription in subscriptions:
        try:
            webpush(
                subscription_info=subscription,
                data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_CLAIMS_EMAIL or "mailto:info@fixtude.it"},
                timeout=8
            )
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in (404, 410):
                expired.append(subscription)
            app.logger.warning("Invio push non riuscito (%s).", type(exc).__name__)

    if expired:
        with push_lock:
            push_subscriptions[:] = [s for s in push_subscriptions if s not in expired]


@app.route("/service-worker.js", methods=["GET"])
def service_worker():
    javascript = r'''
self.addEventListener("push", event => {
  let data = {title: "FixTude", body: "Hai un nuovo messaggio.", url: "/chat-staff"};
  try { if (event.data) data = {...data, ...event.data.json()}; } catch (_) {}
  event.waitUntil(self.registration.showNotification(data.title || "FixTude", {
    body: data.body || "Hai un nuovo messaggio.",
    icon: "/favicon.ico",
    badge: "/favicon.ico",
    data: {url: data.url || "/chat-staff"}
  }));
});
self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || "/chat-staff", self.location.origin).href;
  event.waitUntil(clients.matchAll({type: "window", includeUncontrolled: true}).then(windows => {
    for (const client of windows) {
      if (client.url.startsWith(self.location.origin) && "focus" in client) {
        client.navigate(target);
        return client.focus();
      }
    }
    return clients.openWindow(target);
  }));
});
'''
    return Response(javascript, mimetype="application/javascript", headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.route("/api/push/public-key", methods=["GET"])
def push_public_key():
    if not staff_required():
        return jsonify(error="Accesso non autorizzato."), 401
    if not VAPID_PUBLIC_KEY or not VAPID_PRIVATE_KEY:
        return jsonify(error="Chiavi VAPID mancanti nelle variabili d'ambiente."), 503
    return jsonify(publicKey=VAPID_PUBLIC_KEY)


@app.route("/api/push/subscribe", methods=["POST"])
def push_subscribe():
    if not staff_required():
        return jsonify(error="Accesso non autorizzato."), 401
    if not (VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and webpush):
        return jsonify(error="Notifiche push non configurate correttamente."), 503
    subscription = request.get_json(silent=True)
    if not isinstance(subscription, dict) or not isinstance(subscription.get("endpoint"), str) or not subscription.get("endpoint", "").startswith("https://"):
        return jsonify(error="Sottoscrizione push non valida."), 400
    keys = subscription.get("keys")
    if not isinstance(keys, dict) or not keys.get("p256dh") or not keys.get("auth"):
        return jsonify(error="Chiavi della sottoscrizione mancanti."), 400
    with push_lock:
        if not any(s.get("endpoint") == subscription["endpoint"] for s in push_subscriptions):
            if len(push_subscriptions) >= 10:
                return jsonify(error="Limite dispositivi notifiche raggiunto."), 429
            push_subscriptions.append(subscription)
        else:
            push_subscriptions[:] = [subscription if s.get("endpoint") == subscription["endpoint"] else s for s in push_subscriptions]
    return jsonify(ok=True)


# =========================================================
# CHAT LIVE - VISITATORE
# =========================================================

@app.route("/api/chat/start", methods=["POST"])
def chat_start():
    if rate_limited("chat_start", 15, 60):
        return jsonify(error="Riprova tra qualche minuto."), 429

    data = request.get_json(silent=True) or {}
    chat_id = str(data.get("chat_id", ""))

    if not re.fullmatch(r"[a-fA-F0-9-]{32,36}", chat_id):
        return jsonify(error="Sessione chat non valida."), 400

    with chat_lock:
        if chat_id not in chats:
            if len(chats) >= MAX_ACTIVE_CHATS:
                return jsonify(error="Chat momentaneamente non disponibile."), 503

            chats[chat_id] = {
                "created": time.time(),
                "updated": time.time(),
                "messages": []
            }

        chats[chat_id]["updated"] = time.time()

    return jsonify(ok=True, status="Puoi scrivere. Risponderemo quando disponibili.")


@app.route("/api/chat/<chat_id>/messages", methods=["GET"])
def chat_messages(chat_id):
    if not re.fullmatch(r"[a-fA-F0-9-]{32,36}", chat_id):
        return jsonify(error="Sessione non valida."), 400

    with chat_lock:
        chat = chats.get(chat_id)
        if not chat:
            return jsonify(messages=[], status="Chat nuova o scaduta.")

        chat["updated"] = time.time()
        messages = list(chat["messages"])

    return jsonify(
        messages=messages,
        status="Chat attiva. Risponderemo quando disponibili."
    )


@app.route("/api/chat/send", methods=["POST"])
def chat_send():
    if rate_limited("chat_send", 40, 60):
        return jsonify(error="Hai inviato troppi messaggi. Attendi un momento."), 429

    data = request.get_json(silent=True) or {}
    chat_id = str(data.get("chat_id", ""))
    message = str(data.get("text", "")).strip()

    if not re.fullmatch(r"[a-fA-F0-9-]{32,36}", chat_id):
        return jsonify(error="Sessione chat non valida."), 400

    if not message or len(message) > 1000:
        return jsonify(error="Il messaggio deve contenere da 1 a 1000 caratteri."), 400

    with chat_lock:
        chat = chats.get(chat_id)
        if not chat:
            return jsonify(error="La chat è scaduta. Riapri la chat e riprova."), 410

        if len(chat["messages"]) >= MAX_CHAT_MESSAGES:
            return jsonify(error="Questa conversazione ha raggiunto il limite di messaggi."), 429

        chat["messages"].append({
            "sender": "visitor",
            "text": message,
            "time": ora_italiana()
        })
        chat["updated"] = time.time()

    send_push_notification(
        title="Nuovo messaggio su FixTude",
        body="Un visitatore ha scritto nella chat. Apri l’Area Riservata per rispondere.",
        url="/chat-staff"
    )
    # La notifica email viene inviata dal browser via Web3Forms HTTPS.
    # Non usare SMTP da Render per questa notifica.
    return jsonify(ok=True)


# =========================================================
# CHAT LIVE - AREA RISERVATA DEL GESTORE
# =========================================================

def staff_required():
    return bool(session.get("chat_staff") is True)


@app.route("/chat-staff", methods=["GET", "POST"])
def chat_staff():
    if not CHAT_ADMIN_PASSWORD:
        content = """
        <section class="card">
          <h1>Chat riservata al gestore</h1>
          <p class="error">
            Accesso non configurato. Imposta CHAT_ADMIN_PASSWORD
            nelle variabili d'ambiente di Render.
          </p>
        </section>
        """
        return page("Chat Staff", content, include_chat=False), 503

    if request.method == "POST":
        password = request.form.get("password", "")
        if password and password == CHAT_ADMIN_PASSWORD:
            session["chat_staff"] = True
            session.permanent = False
            return redirect(url_for("chat_staff"))

        content = """
        <section class="card">
          <h1>Accesso non riuscito</h1>
          <p class="error">Password non corretta.</p>
          <a class="btn" href="/chat-staff">Riprova</a>
        </section>
        """
        return page("Chat Staff", content, include_chat=False), 401

    if not staff_required():
        content = """
        <section class="card" style="max-width:460px;margin:auto">
          <h1>Chat FixTude</h1>
          <p class="muted">Area riservata al gestore.</p>
          <form method="post">
            <div class="field">
              <label for="password">Password di accesso</label>
              <input id="password" name="password" type="password"
                     autocomplete="current-password" required>
            </div>
            <button class="btn" type="submit">Accedi alla chat</button>
          </form>
        </section>
        """
        return page("Accesso chat", content, include_chat=False)

    content = """
    <h1>Area riservata · Chat FixTude</h1>
    <p class="muted">
      Seleziona una conversazione per leggere e inviare risposte.
      Le conversazioni esistono soltanto nella memoria temporanea del server.
    </p>
    <div class="actions">
      <button class="btn" id="enablePush" type="button">
        Attiva notifiche su questo dispositivo
      </button>
      <button class="btn outline" id="refreshChats" type="button">
        Aggiorna conversazioni
      </button>
      <form method="post" action="/chat-staff/logout">
        <button class="btn outline" type="submit">Esci</button>
      </form>
    </div>
    <div class="grid" style="margin-top:20px">
      <section class="card">
        <h2>Conversazioni attive</h2>
        <div id="chatList">Caricamento…</div>
      </section>
      <section class="card">
        <h2 id="selectedTitle">Seleziona una chat</h2>
        <div id="staffMessages"
             style="height:300px;overflow:auto;background:#f5f8fc;padding:10px;border-radius:10px">
          <p class="muted">Le conversazioni appariranno qui.</p>
        </div>
        <form id="staffForm" style="margin-top:12px">
          <div class="field">
            <label for="staffReply">La tua risposta</label>
            <textarea id="staffReply" maxlength="1000"
                      style="min-height:90px" required></textarea>
          </div>
          <button class="btn" type="submit">Invia risposta</button>
          <div id="staffStatus" role="status" aria-live="polite"></div>
        </form>
      </section>
    </div>

    <script>
    (() => {
      let selected = "";
      let chats = [];

      const list = document.getElementById("chatList");
      const messages = document.getElementById("staffMessages");
      const title = document.getElementById("selectedTitle");
      const form = document.getElementById("staffForm");
      const reply = document.getElementById("staffReply");
      const status = document.getElementById("staffStatus");
      const enablePushButton = document.getElementById("enablePush");

      function urlBase64ToUint8Array(base64String) {
        const padding = "=".repeat((4 - base64String.length % 4) % 4);
        const base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
        const raw = atob(base64);
        return Uint8Array.from([...raw].map(ch => ch.charCodeAt(0)));
      }

      async function enablePush() {
        enablePushButton.disabled = true;
        status.textContent = "Configurazione notifiche in corso…";
        try {
          if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
            throw new Error("Questo browser non supporta le notifiche push.");
          }
          const keyData = await api("/api/push/public-key");
          if (!keyData.publicKey) throw new Error("Chiavi push non configurate su Render.");
          const permission = await Notification.requestPermission();
          if (permission !== "granted") throw new Error("Permesso notifiche non concesso.");
          const registration = await navigator.serviceWorker.register("/service-worker.js");
          await navigator.serviceWorker.ready;
          let subscription = await registration.pushManager.getSubscription();
          if (!subscription) {
            subscription = await registration.pushManager.subscribe({
              userVisibleOnly: true,
              applicationServerKey: urlBase64ToUint8Array(keyData.publicKey)
            });
          }
          await api("/api/push/subscribe", {method:"POST", body:JSON.stringify(subscription)});
          status.textContent = "Notifiche attivate su questo dispositivo.";
        } catch (err) {
          status.textContent = err.message || "Impossibile attivare le notifiche.";
        } finally {
          enablePushButton.disabled = false;
        }
      }
      enablePushButton.addEventListener("click", enablePush);

      async function api(url, options={}) {
        const r = await fetch(url, {
          cache:"no-store",
          ...options,
          headers:{
            "Content-Type":"application/json",
            ...(options.headers || {})
          }
        });
        const data = await r.json();
        if (!r.ok) throw new Error(data.error || "Errore di comunicazione");
        return data;
      }

      async function loadChats() {
        try {
          const data = await api("/api/staff/chats");
          chats = data.chats;
          list.replaceChildren();

          if (!chats.length) {
            list.textContent = "Nessuna conversazione attiva al momento.";
            return;
          }

          chats.forEach(chat => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = "btn outline";
            button.style.cssText =
              "width:100%;margin:5px 0;justify-content:flex-start;overflow-wrap:anywhere";
            button.textContent =
              "Chat " + chat.id.slice(0, 8) +
              " · " + chat.count + " messaggi · " + chat.last;
            button.addEventListener("click", () => {
              selected = chat.id;
              title.textContent = "Chat " + selected.slice(0, 8);
              loadMessages();
            });
            list.appendChild(button);
          });

          if (!selected && chats.length) {
            selected = chats[0].id;
            title.textContent = "Chat " + selected.slice(0, 8);
            loadMessages();
          }
        } catch (err) {
          list.textContent = err.message;
        }
      }

      async function loadMessages() {
        if (!selected) return;

        try {
          const data = await api(
            "/api/staff/" + encodeURIComponent(selected) + "/messages"
          );
          messages.replaceChildren();

          data.messages.forEach(m => {
            const wrap = document.createElement("div");
            wrap.style.cssText =
              "background:#fff;border:1px solid #dce5ef;border-radius:10px;padding:10px;margin:8px 0;overflow-wrap:anywhere";

            const who = document.createElement("strong");
            who.textContent = m.sender === "staff" ? "Tu" : "Visitatore";
            wrap.appendChild(who);

            const body = document.createElement("div");
            body.style.whiteSpace = "pre-wrap";
            body.textContent = m.text;
            wrap.appendChild(body);

            const when = document.createElement("small");
            when.className = "muted";
            when.textContent = m.time || "";
            wrap.appendChild(when);

            messages.appendChild(wrap);
          });

          messages.scrollTop = messages.scrollHeight;
        } catch (err) {
          status.textContent = err.message;
        }
      }

      form.addEventListener("submit", async e => {
        e.preventDefault();
        if (!selected || !reply.value.trim()) return;

        const button = form.querySelector("button");
        button.disabled = true;
        status.textContent = "";

        try {
          await api("/api/staff/send", {
            method:"POST",
            body:JSON.stringify({
              chat_id:selected,
              text:reply.value.trim()
            })
          });
          reply.value = "";
          status.textContent = "Risposta inviata.";
          await loadMessages();
          await loadChats();
        } catch (err) {
          status.textContent = err.message;
        } finally {
          button.disabled = false;
        }
      });

      document.getElementById("refreshChats")
        .addEventListener("click", loadChats);

      loadChats();
      setInterval(loadChats, 5000);
      setInterval(loadMessages, 2500);
    })();
    </script>
    """
    return page("Chat Staff", content, include_chat=False)


@app.route("/api/staff/chats", methods=["GET"])
def staff_list_chats():
    if not staff_required():
        return jsonify(error="Accesso non autorizzato."), 401

    with chat_lock:
        result = []
        for chat_id, chat in chats.items():
            if time.time() - chat["updated"] > CHAT_TTL_SECONDS:
                continue

            last = chat["messages"][-1]["time"] if chat["messages"] else "Nuova"
            result.append({
                "id": chat_id,
                "count": len(chat["messages"]),
                "last": last,
                "updated": chat["updated"]
            })

    result.sort(key=lambda x: x["updated"], reverse=True)
    return jsonify(chats=result)


@app.route("/api/staff/<chat_id>/messages", methods=["GET"])
def staff_get_messages(chat_id):
    if not staff_required():
        return jsonify(error="Accesso non autorizzato."), 401

    with chat_lock:
        chat = chats.get(chat_id)
        if not chat:
            return jsonify(error="Conversazione scaduta o non disponibile."), 404
        chat["updated"] = time.time()
        messages = list(chat["messages"])

    return jsonify(messages=messages)


@app.route("/api/staff/send", methods=["POST"])
def staff_send():
    if not staff_required():
        return jsonify(error="Accesso non autorizzato."), 401

    data = request.get_json(silent=True) or {}
    chat_id = str(data.get("chat_id", ""))
    message = str(data.get("text", "")).strip()

    if not message or len(message) > 1000:
        return jsonify(error="La risposta deve contenere da 1 a 1000 caratteri."), 400

    with chat_lock:
        chat = chats.get(chat_id)
        if not chat:
            return jsonify(error="Conversazione scaduta o non disponibile."), 404

        if len(chat["messages"]) >= MAX_CHAT_MESSAGES:
            return jsonify(error="Limite di messaggi raggiunto."), 429

        chat["messages"].append({
            "sender": "staff",
            "text": message,
            "time": ora_italiana()
        })
        chat["updated"] = time.time()

    return jsonify(ok=True)


@app.route("/chat-staff/logout", methods=["POST"])
def chat_staff_logout():
    session.pop("chat_staff", None)
    return redirect(url_for("chat_staff"))


# =========================================================
# MODULI PDF GRATUITI, EDUCAZIONE FINANZIARIA, SIC E SHOP
# =========================================================

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PDF_DIR = BASE_DIR / "static" / "pdf"
BOOK_FILE = PDF_DIR / "NIENTE_PANICO.pdf"
MODULE_FILES = {
    "saldo-stralcio": ("FixTude_Modulo_Saldo_e_Stralcio_2_pagine.pdf", "Proposta di saldo e stralcio"),
    "dilazione": ("FixTude_Modulo_Dilazione_Pagamento_2_pagine.pdf", "Richiesta di dilazione del pagamento"),
    "piano-rientro": ("FixTude_Modulo_Piano_Rientro_con_Titoli_2_pagine.pdf", "Richiesta di piano di rientro"),
    "raccolta": ("FixTude_Libreria_Moduli_Richieste_Debitorie_6_pagine_v2.pdf", "Raccolta di moduli di richiesta"),
}

@app.route("/moduli")
def moduli_gratuiti():
    cards = []
    for slug, (filename, label) in MODULE_FILES.items():
        cards.append(f'''<div class="card"><h2>{escape(label)}</h2><p class="muted">PDF gratuito, senza registrazione.</p><a class="btn" href="/download/modulo/{slug}">Scarica PDF</a></div>''')
    content = '''<h1>Moduli FixTude gratuiti</h1>
    <p class="muted">I modelli sono strumenti orientativi: adattali alla tua situazione, controlla gli importi e allega solo documenti pertinenti. L'invio di una richiesta non garantisce che il creditore la accetti.</p>
    <div class="grid">''' + "".join(cards) + '''</div>
    <div class="notice">Non pagare per scaricare questi moduli: in questa fase sono gratuiti.</div>'''
    return page("Moduli gratuiti", content)

@app.route("/download/modulo/<slug>")
def download_modulo(slug):
    item = MODULE_FILES.get(slug)
    if not item:
        return "Modulo non trovato", 404
    path = PDF_DIR / item[0]
    if not path.is_file():
        return page("Modulo non disponibile", '<h1>Modulo temporaneamente non disponibile</h1><p>Il file PDF non è stato ancora caricato nel progetto. Riprova più tardi.</p>'), 404
    return send_file(path, as_attachment=True, download_name=item[0], mimetype="application/pdf")

@app.route("/educazione-finanziaria")
def educazione_finanziaria():
    content = '''<h1>Educazione finanziaria</h1>
    <p class="muted">Guide introduttive gratuite per comprendere meglio il bilancio personale e affrontare le comunicazioni con i creditori.</p>
    <section class="card" id="bilancio"><h2>1. Organizzare il bilancio familiare</h2><p>Annota le entrate nette effettivamente disponibili e separa le spese essenziali (casa, utenze, alimentazione, salute e trasporti) dalle spese flessibili. Inserisci poi rate, arretrati e scadenze. Il margine mensile è una stima: lascia un cuscinetto per gli imprevisti e non considerare sostenibile una rata che mette a rischio le necessità di base.</p><ul><li>Usa importi mensili omogenei.</li><li>Evita di contare entrate incerte come se fossero garantite.</li><li>Aggiorna il prospetto quando cambiano reddito o spese.</li></ul></section>
    <section class="card" id="priorita"><h2>2. Dare priorità ai pagamenti</h2><p>Prima di distribuire il denaro tra i debiti, proteggi le necessità essenziali e valuta le scadenze concrete. Non esiste una priorità universale valida per ogni situazione: atti giudiziari, garanzie, utenze e debiti fiscali possono avere conseguenze differenti. Se hai ricevuto un atto con termini, chiedi tempestivamente una valutazione qualificata.</p><ul><li>Apri e conserva le comunicazioni ricevute.</li><li>Segna importo, creditore, scadenza e riferimento della pratica.</li><li>Non ignorare raccomandate, PEC o atti notificati.</li></ul></section>
    <section class="card" id="creditori"><h2>3. Parlare con i creditori</h2><p>Una richiesta efficace è chiara, documentata e realistica. Indica il riferimento del rapporto, spiega brevemente la difficoltà, proponi un importo compatibile con il bilancio e chiedi conferma scritta delle condizioni. Non promettere pagamenti che non puoi sostenere e non considerare accettata una proposta finché non hai ricevuto una conferma verificabile.</p><p>Puoi partire dai <a href="/moduli">moduli gratuiti FixTude</a> e personalizzarli.</p></section>
    <section class="card" id="errori"><h2>4. Errori da evitare</h2><ul><li>Pagare intermediari che promettono cancellazioni o risultati garantiti.</li><li>Firmare accordi senza aver letto importi, scadenze e conseguenze.</li><li>Inviare documenti personali a destinatari non verificati.</li><li>Contrarre nuovo debito senza valutare il costo totale e la sostenibilità.</li><li>Rimandare la richiesta di aiuto quando arrivano atti con termini precisi.</li></ul><p>Le informazioni di questa pagina sono generali e non sostituiscono una consulenza professionale.</p></section>'''
    return page("Educazione finanziaria", content)

@app.route("/sic")
def sic_banche_dati():
    content = '''<h1>Accesso gratuito alle banche dati SIC</h1>
    <p>Puoi esercitare direttamente presso i gestori il diritto di accesso ai dati personali che ti riguardano. Le richieste di accesso ai dati sono distinte da eventuali servizi a pagamento. Segui le istruzioni aggiornate del gestore e usa esclusivamente i suoi canali ufficiali.</p>
    <div class="grid">
      <div class="card"><h2>CRIF – EURISC</h2><p>Informazioni per consumatori e accesso ai dati registrati.</p><a class="btn" href="https://www.crif.it/consumatori/" target="_blank" rel="noopener">Sito ufficiale CRIF</a></div>
      <div class="card"><h2>Experian</h2><p>Informazioni sui dati personali e sui sistemi di informazione creditizia.</p><a class="btn" href="https://www.experian.it/" target="_blank" rel="noopener">Sito ufficiale Experian</a></div>
      <div class="card"><h2>CTC</h2><p>Consorzio per la tutela del credito: informazioni e procedure per i consumatori.</p><a class="btn" href="https://www.ctconline.it/" target="_blank" rel="noopener">Sito ufficiale CTC</a></div>
      <div class="card"><h2>CAI – Banca d'Italia</h2><p>Consulta le informazioni ufficiali della Banca d'Italia sulla Centrale d'allarme interbancaria.</p><a class="btn" href="https://www.bancaditalia.it/servizi-cittadino/servizi/accesso-cai/index.html" target="_blank" rel="noopener">Accesso CAI</a></div>
    </div>
    <div class="notice">FixTude non accede alle banche dati per conto dell'utente e non garantisce tempi o risultati. Verifica sempre le istruzioni, l'identificazione richiesta e gli eventuali costi di servizi diversi dall'accesso ai dati personali.</div>'''
    return page("Banche dati SIC", content)

@app.route("/shop")
def shop():
    content = '''<h1>Shop FixTude</h1>
    <section class="card product-card"><div><h2>NIENTE PANICO</h2><p>Una guida pratica per mettere ordine nella situazione debitoria, leggere meglio le proprie possibilità e prepararsi ad affrontare i creditori con maggiore lucidità.</p><p><strong>Formato:</strong> libro digitale PDF.</p><p class="muted">Dopo la conferma del pagamento potrai scaricare il PDF dalla pagina di conferma. Non è necessaria la registrazione.</p></div><div><div class="price">€ 6,99</div><form method="post" action="/shop/acquista"><button class="btn" type="submit">Acquista il libro</button></form></div></section>
    <p class="muted">I moduli di richiesta debitoria restano gratuiti e sono disponibili nella sezione <a href="/moduli">Moduli gratuiti</a>.</p>'''
    return page("Shop", content)

@app.route("/shop/acquista", methods=["POST"])
def shop_acquista():
    secret_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if stripe is None or not secret_key:
        return page("Acquisto temporaneamente non disponibile", '<h1>Acquisto non ancora attivo</h1><p>Il pagamento online non è configurato al momento. Non è stato effettuato alcun addebito.</p><a class="btn" href="/shop">Torna allo Shop</a>'), 503
    stripe.api_key = secret_key
    try:
        checkout = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price_data": {"currency": "eur", "unit_amount": 699, "product_data": {"name": "NIENTE PANICO - libro digitale FixTude", "description": "Libro digitale in formato PDF"}}, "quantity": 1}],
            success_url=request.host_url.rstrip("/") + "/shop/conferma?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=request.host_url.rstrip("/") + "/shop?annullato=1",
            metadata={"fixtude_product": "niente_panico_pdf"},
        )
        return redirect(checkout.url, code=303)
    except Exception:
        app.logger.exception("Creazione sessione Stripe non riuscita")
        return page("Acquisto non disponibile", '<h1>Non è stato possibile avviare il pagamento</h1><p>Nessun pagamento è stato confermato. Riprova più tardi.</p><a class="btn" href="/shop">Torna allo Shop</a>'), 502

@app.route("/shop/conferma")
def shop_conferma():
    session_id = request.args.get("session_id", "").strip()
    secret_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not session_id or stripe is None or not secret_key:
        return page("Verifica acquisto", '<h1>Non possiamo verificare il pagamento</h1><p>Contatta info@fixtude.it per assistenza.</p>'), 400
    try:
        stripe.api_key = secret_key
        checkout = stripe.checkout.Session.retrieve(session_id)
        paid = checkout.get("payment_status") == "paid"
        product = (checkout.get("metadata") or {}).get("fixtude_product") == "niente_panico_pdf"
        if not paid or not product:
            return page("Pagamento da verificare", '<h1>Pagamento non ancora confermato</h1><p>Il libro sarà disponibile dopo la conferma del pagamento.</p><a class="btn" href="/shop">Torna allo Shop</a>'), 402
        if not BOOK_FILE.is_file():
            return page("PDF in preparazione", '<h1>Pagamento confermato</h1><p>Il pagamento risulta confermato, ma il file del libro non è presente sul server. Contatta info@fixtude.it per ricevere assistenza.</p>'), 500
        content = '''<h1>Grazie per il tuo acquisto!</h1><section class="card"><p>Il pagamento è stato confermato.</p><p><strong>NIENTE PANICO</strong> è pronto per il download.</p><a class="btn" href="/shop/scarica?session_id=''' + escape(session_id) + '''">Scarica il libro PDF</a></section><p class="muted">Conserva questa pagina finché non hai scaricato il file.</p>'''
        return page("Acquisto confermato", content)
    except Exception:
        app.logger.exception("Verifica sessione Stripe non riuscita")
        return page("Verifica non riuscita", '<h1>Non è stato possibile verificare il pagamento</h1><p>Riprova più tardi o scrivi a info@fixtude.it.</p>'), 502

@app.route("/shop/scarica")
def shop_scarica():
    session_id = request.args.get("session_id", "").strip()
    secret_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not session_id or stripe is None or not secret_key:
        return "Accesso non autorizzato", 403
    try:
        stripe.api_key = secret_key
        checkout = stripe.checkout.Session.retrieve(session_id)
        paid = checkout.get("payment_status") == "paid"
        product = (checkout.get("metadata") or {}).get("fixtude_product") == "niente_panico_pdf"
        if not paid or not product or not BOOK_FILE.is_file():
            return "Accesso non autorizzato o file non disponibile", 403
        return send_file(BOOK_FILE, as_attachment=True, download_name="FixTude_NIENTE_PANICO.pdf", mimetype="application/pdf")
    except Exception:
        app.logger.exception("Download libro non riuscito")
        return "Non è stato possibile verificare l'acquisto", 502


# =========================================================
# PRIVACY - BOZZA DA COMPLETARE PRIMA DEL LANCIO
# =========================================================

@app.route("/privacy")
def privacy():
    content = """
    <h1>Informativa privacy</h1>

    <section class="card">
      <p>
        <strong>Questa pagina è una bozza tecnica e deve essere completata
        e verificata prima della pubblicazione del sito.</strong>
      </p>

      <h2>Calcolatore</h2>
      <p>
        Gli importi inseriti nel calcolatore sono elaborati dal browser
        dell'utente. Il codice del calcolatore non invia tali importi
        agli endpoint applicativi.
      </p>

      <h2>Modulo di ricontatto</h2>
      <p>
        Se invii il modulo, nome, indirizzo email, eventuale telefono
        e messaggio vengono trasmessi tramite connessione HTTPS al
        servizio Web3Forms, che gestisce l'invio della richiesta alla
        casella email associata alla chiave del modulo. L'applicazione
        FixTude non conserva una copia di questi dati in un proprio
        database. Web3Forms dichiara di conservare i dati delle richieste
        secondo la propria informativa e le impostazioni del servizio;
        prima del lancio occorre verificare le condizioni aggiornate,
        i tempi di conservazione e gli eventuali trasferimenti di dati.
        La richiesta può inoltre rimanere nella casella email del
        destinatario secondo le relative impostazioni.
      </p>

      <h2>Chat</h2>
      <p>
        I messaggi della chat sono conservati temporaneamente nella
        memoria del processo server per consentire lo scambio in tempo
        reale. Non vengono inseriti in un database applicativo.
        Le conversazioni inattive vengono eliminate dopo circa due ore;
        un riavvio del processo può eliminarle prima.
      </p>

      <h2>Dati tecnici e hosting</h2>
      <p>
        Il server e il fornitore di hosting possono trattare dati tecnici
        necessari al funzionamento, alla sicurezza e alla prevenzione
        degli abusi, secondo la loro configurazione e le rispettive
        condizioni.
      </p>

      <h2>Informazioni da completare</h2>
      <p>
        Prima del lancio occorre indicare in modo definitivo il titolare
        del trattamento, i recapiti per esercitare i diritti, la base
        giuridica, i tempi di conservazione, i fornitori coinvolti,
        gli eventuali trasferimenti di dati e le modalità per presentare
        reclamo all'autorità competente.
      </p>
    </section>
    """
    return page("Privacy", content)


@app.route("/health")
def health():
    return jsonify(status="ok", service="FixTude")


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=False
    )
