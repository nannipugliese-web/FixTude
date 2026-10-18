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


def chat_orario_stato():
    """Chat disponibile dal lunedì al venerdì, 09:30-14:30, ora di Roma."""
    adesso = datetime.now(ROME_TZ)
    ora = adesso.time()
    inizio = datetime.strptime("09:30", "%H:%M").time()
    fine = datetime.strptime("14:30", "%H:%M").time()
    giorno_lavorativo = adesso.weekday() < 5  # lunedì=0, venerdì=4
    attiva = giorno_lavorativo and inizio <= ora < fine
    if attiva:
        return {
            "open": True,
            "status": "Chat disponibile · lunedì-venerdì, 9:30-14:30",
            "notice": "La chat è attiva. Scrivi il tuo messaggio e ti risponderemo appena possibile."
        }
    return {
        "open": False,
        "status": "Fuori orario · lunedì-venerdì, 9:30-14:30",
        "notice": "La chat in tempo reale è disponibile dal lunedì al venerdì, dalle 9:30 alle 14:30. Puoi lasciare un messaggio in qualsiasi momento: ti risponderemo nel più breve tempo possibile."
    }

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


SEO_BASE_URL = "https://www.fixtude.it"
SEO_DEFAULT_DESCRIPTION = (
    "FixTude ti aiuta a fare chiarezza sulla situazione debitoria con un "
    "calcolatore gratuito di sostenibilità, materiali informativi e la "
    "possibilità di chiedere un ricontatto."
)
SEO_PAGES = {
    "/": {
        "title": "FixTude | Calcolatore gratuito di sostenibilità dei debiti",
        "description": SEO_DEFAULT_DESCRIPTION,
    },
    "/misuratore": {
        "title": "Calcolatore gratuito di sostenibilità del debito | FixTude",
        "description": "Calcola gratuitamente il margine mensile e valuta in modo orientativo il peso delle rate rispetto a entrate e spese.",
    },
    "/consulenza": {
        "title": "Richiedi un ricontatto per la tua situazione debitoria | FixTude",
        "description": "Contatta FixTude per chiedere un ricontatto e descrivere la tua situazione debitoria. La richiesta è facoltativa e non comporta obblighi.",
    },
    "/moduli": {
        "title": "Moduli gratuiti per la gestione dei debiti | FixTude",
        "description": "Consulta e scarica i moduli informativi gratuiti FixTude per organizzare le comunicazioni e affrontare le questioni debitorie.",
    },
    "/educazione-finanziaria": {
        "title": "Educazione finanziaria e gestione del bilancio | FixTude",
        "description": "Informazioni pratiche per leggere il bilancio familiare, valutare le spese e gestire gli impegni economici con maggiore consapevolezza.",
    },
    "/educazione-finanziaria/come-gestire-lo-stipendio-regola-un-terzo": {
        "title": "Come gestire le entrate: regola 1/3, 1/3, 1/3 | FixTude",
        "description": "Scopri come organizzare le entrate con la regola 1/3, 1/3, 1/3. Esempi pratici per lavoratori dipendenti, autonomi, single e famiglie.",
    },
    "/sic": {
        "title": "Sistemi di informazione creditizia (SIC) | FixTude",
        "description": "Informazioni orientative sui sistemi di informazione creditizia e sulla consultazione dei dati relativi alla propria posizione.",
    },
    "/shop": {
        "title": "Shop digitale e guide FixTude",
        "description": "Scopri i materiali digitali FixTude dedicati all'organizzazione del bilancio e alla gestione delle situazioni debitorie.",
    },
    "/assistenza-legale": {
        "title": "Assistenza legale per debiti | Pacchetti lancio FixTude",
        "description": "Scopri i pacchetti FixTude di assistenza legale per situazioni debitorie, con orientamento al professionista competente e prezzi di lancio trasparenti.",
    },
    "/disclaimer": {
        "title": "Disclaimer: limiti del calcolatore e dei contenuti | FixTude",
        "description": "Leggi finalità, limiti e criteri orientativi del calcolatore di sostenibilità del debito e dei materiali informativi FixTude.",
    },
    "/termini": {
        "title": "Termini e condizioni di utilizzo | FixTude",
        "description": "Consulta i termini che regolano l'utilizzo del sito FixTude, del calcolatore, dei materiali informativi e degli eventuali acquisti digitali.",
    },
    "/privacy": {
        "title": "Informativa Privacy | FixTude",
        "description": "Informazioni sul trattamento dei dati personali, sui contatti, sulla chat e sui diritti degli utenti di FixTude.",
    },
    "/cookie": {
        "title": "Cookie Policy e preferenze | FixTude",
        "description": "Consulta le informazioni sui cookie e sulle preferenze relative alle statistiche e agli strumenti utilizzati da FixTude.",
    },
}


def page(title, content, include_chat=True):
    """Layout comune, responsive, con metadati SEO e pulsante chat."""
    path = request.path or "/"
    seo = SEO_PAGES.get(path, {})
    seo_title = seo.get("title", f"{title} | FixTude")
    seo_description = seo.get("description", SEO_DEFAULT_DESCRIPTION)
    canonical_url = SEO_BASE_URL + ("" if path == "/" else path)
    noindex_paths = ("/chat-staff", "/shop/conferma", "/shop/scarica", "/shop/anteprima", "/assistenza-legale/conferma")
    robots = "noindex, nofollow" if path in noindex_paths or path.startswith("/api/") else "index, follow, max-image-preview:large"
    structured_data = None
    if path == "/":
        structured_data = {
            "@context": "https://schema.org",
            "@graph": [
                {
                    "@type": "WebSite",
                    "@id": SEO_BASE_URL + "/#website",
                    "url": SEO_BASE_URL + "/",
                    "name": "FixTude",
                    "inLanguage": "it-IT",
                    "description": SEO_DEFAULT_DESCRIPTION,
                },
                {
                    "@type": "Organization",
                    "@id": SEO_BASE_URL + "/#organization",
                    "name": "FixTude",
                    "url": SEO_BASE_URL + "/",
                    "email": "info@fixtude.it",
                    "vatID": "IT15990471003",
                    "logo": {
                        "@type": "ImageObject",
                        "url": SEO_BASE_URL + "/static/fixtude-512.png",
                    },
                },
            ],
        }
    elif path == "/educazione-finanziaria/come-gestire-lo-stipendio-regola-un-terzo":
        structured_data = {
            "@context": "https://schema.org",
            "@type": "BlogPosting",
            "headline": "Come gestire le entrate: la regola 1/3, 1/3, 1/3 per single e famiglie",
            "description": SEO_PAGES[path]["description"],
            "datePublished": "2026-10-17",
            "dateModified": "2026-10-17",
            "inLanguage": "it-IT",
            "mainEntityOfPage": {"@type": "WebPage", "@id": SEO_BASE_URL + path},
            "author": {"@type": "Organization", "name": "FixTude", "url": SEO_BASE_URL + "/"},
            "publisher": {"@type": "Organization", "name": "FixTude", "url": SEO_BASE_URL + "/"},
        }
    return render_template_string(
        """
        <!doctype html>
        <html lang="it-IT">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <meta name="theme-color" content="#123a72">
          <meta name="color-scheme" content="light">
          <title>{{ seo_title }}</title>
          <meta name="description" content="{{ seo_description }}">
          <meta name="robots" content="{{ robots }}">
          <link rel="canonical" href="{{ canonical_url }}">
          <link rel="icon" type="image/png" sizes="16x16" href="/static/fixtude-16.png">
          <link rel="icon" type="image/png" sizes="48x48" href="/static/fixtude-48.png">
          <link rel="apple-touch-icon" sizes="180x180" href="/static/fixtude-180.png">
          <link rel="icon" type="image/png" sizes="512x512" href="/static/fixtude-512.png">
          <meta property="og:locale" content="it_IT">
          <meta property="og:type" content="website">
          <meta property="og:site_name" content="FixTude">
          <meta property="og:title" content="{{ seo_title }}">
          <meta property="og:description" content="{{ seo_description }}">
          <meta property="og:url" content="{{ canonical_url }}">
          <meta property="og:image" content="{{ seo_base_url }}/static/fixtude-512.png">
          <meta property="og:image:alt" content="Logo FixTude">
          <meta name="twitter:card" content="summary_large_image">
          <meta name="twitter:title" content="{{ seo_title }}">
          <meta name="twitter:description" content="{{ seo_description }}">
          <meta name="twitter:image" content="{{ seo_base_url }}/static/fixtude-512.png">
          {% if structured_data %}<script type="application/ld+json">{{ structured_data|tojson }}</script>{% endif %}
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
            .menu-toggle{display:inline-flex;align-items:center;justify-content:center;width:44px;height:44px;border:1px solid var(--line);border-radius:10px;background:#fff;color:var(--ink);font-size:25px;cursor:pointer;line-height:1}
            .nav-menu{display:none;position:absolute;right:max(18px,calc((100vw - 1080px)/2));top:68px;z-index:1000;min-width:245px;max-width:calc(100vw - 32px);padding:10px;background:#fff;border:1px solid var(--line);border-radius:14px;box-shadow:0 14px 32px rgba(20,40,70,.18)}
            .nav-menu.open{display:flex;flex-direction:column;gap:0}
            .nav-menu a{display:block;padding:12px 14px;border-radius:8px;text-decoration:none;color:var(--ink);font-size:14px;font-weight:700}
            .nav-menu a:hover,.nav-menu a:focus-visible{background:#edf4fc;color:var(--blue)}
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
              transition:transform .2s ease, box-shadow .2s ease, background-color .2s ease, color .2s ease, border-color .2s ease;
              will-change:transform;
            }
            button, nav a, .chat-launcher {
              transition:transform .2s ease, box-shadow .2s ease, background-color .2s ease, color .2s ease;
            }
            .btn:hover {background:var(--blue2)}
            @media (hover:hover) and (pointer:fine) {
              .btn:hover, button:hover, .chat-launcher:hover {
                transform:translateY(-2px);
                box-shadow:0 7px 18px rgba(18,58,114,.20);
              }
              nav a:hover {transform:translateY(-1px)}
            }
            .btn:active, button:active, .chat-launcher:active {transform:scale(.98)}
            @media (prefers-reduced-motion:reduce) {
              *, *::before, *::after {scroll-behavior:auto !important;transition:none !important;animation:none !important}
            }
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
              grid-template-columns:minmax(0,1.25fr) minmax(110px,1fr) minmax(110px,1fr) auto;
              gap:10px;
              align-items:end;
              padding:12px 0;
              border-bottom:1px solid var(--line);
            }
            .entry-row .field {margin:0}
            .traffic-light {
              display:flex;
              align-items:center;
              gap:12px;
              border-radius:12px;
              border:1px solid var(--line);
              padding:14px 16px;
              margin:16px 0;
              background:#fff;
            }
            .traffic-light .signal {
              width:24px;
              height:24px;
              min-width:24px;
              border-radius:50%;
              background:#9ca3af;
              border:3px solid rgba(0,0,0,.08);
            }
            .traffic-light.red {background:#fff1f1;border-color:#f3b4b4}
            .traffic-light.red .signal {background:#dc2626}
            .traffic-light.yellow {background:#fff9e8;border-color:#f3d58b}
            .traffic-light.yellow .signal {background:#eab308}
            .traffic-light.green {background:#effaf2;border-color:#a8dfb5}
            .traffic-light.green .signal {background:#16a34a}
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
            .cookie-footer-link {border:0;background:transparent;color:var(--blue);text-decoration:underline;cursor:pointer;font:inherit;font-size:13px;padding:0}
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
            .product-card {display:grid;grid-template-columns:minmax(190px, .7fr) minmax(0,1.3fr);gap:28px;align-items:center}
            .shop-book-cover {display:flex;justify-content:center;align-items:center;padding:8px}
            .shop-book-cover img {display:block;width:min(100%,300px);height:auto;max-height:440px;object-fit:contain;border-radius:5px;box-shadow:0 14px 30px rgba(20,48,85,.18)}
            .shop-book-description {min-width:0}
            .shop-book-description form {margin-top:14px}
            .price {font-size:30px;font-weight:800;color:var(--blue);white-space:nowrap;margin:12px 0}
            .resource-list {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
            .back-row {display:flex;justify-content:flex-start;margin:0 0 16px}
            .back-button {display:inline-flex;align-items:center;gap:8px;min-height:42px;padding:9px 15px;border:1px solid var(--line);border-radius:10px;background:#fff;color:var(--blue);font:inherit;font-weight:700;cursor:pointer;transition:background .18s ease,transform .18s ease}
            .back-button:hover {background:#edf4fc;transform:translateX(-2px)}
            .back-button:focus-visible {outline:3px solid #8ab8ef;outline-offset:2px}
            .resource-link {display:block;padding:17px;border:1px solid var(--line);border-radius:12px;background:#fff;text-decoration:none;font-weight:700}
            .resource-link span {display:block;font-weight:400;color:var(--muted);font-size:14px;margin-top:5px}
            @media(max-width:650px) {
              .product-card {grid-template-columns:minmax(0,1fr);gap:12px}
              .shop-book-cover img {width:min(72%,250px);max-height:360px}
              .resource-list {grid-template-columns:1fr}
              .nav {align-items:center;flex-direction:row;gap:9px}
              .nav-menu{top:64px;right:12px}
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
              <a class="brand" href="/" aria-label="FixTude, homepage">FixTude</a>
              <button class="menu-toggle" id="siteMenuToggle" type="button" aria-label="Apri menu" aria-expanded="false" aria-controls="siteMenu">☰</button>
              <nav class="nav-menu" id="siteMenu" aria-label="Menu principale">
                <a href="/misuratore">Calcolatore</a>
                <a href="/consulenza">Contatta un esperto</a>
                <a href="/moduli">Moduli gratuiti</a>
                <a href="/educazione-finanziaria">Educazione finanziaria</a>
                <a href="/sic">Banche dati SIC</a>
                <a href="/assistenza-legale">Assistenza Legale</a>
                <a href="/shop">Shop</a>
              </nav>
            </div>
            <script>
            (()=>{const b=document.getElementById('siteMenuToggle'),m=document.getElementById('siteMenu');if(!b||!m)return;function close(){m.classList.remove('open');b.setAttribute('aria-expanded','false');b.setAttribute('aria-label','Apri menu');b.textContent='☰'}b.addEventListener('click',()=>{const open=!m.classList.contains('open');m.classList.toggle('open',open);b.setAttribute('aria-expanded',String(open));b.setAttribute('aria-label',open?'Chiudi menu':'Apri menu');b.textContent=open?'×':'☰'});document.addEventListener('click',e=>{if(!m.contains(e.target)&&!b.contains(e.target))close()});document.addEventListener('keydown',e=>{if(e.key==='Escape')close()});m.querySelectorAll('a').forEach(a=>a.addEventListener('click',close))})();
            </script>
          </header>
          <main>
            {% if request.path != '/' %}
            <div class="back-row">
              <button class="back-button" type="button" onclick="if (window.history.length > 1) { window.history.back(); } else { window.location.href='/'; }" aria-label="Torna alla pagina precedente">← Indietro</button>
            </div>
            {% endif %}
            {{ content|safe }}
          </main>
          <footer>
            FixTude · Informazione orientativa, senza impegno.
            <br>Partita IVA 15990471003 ·
            <a href="/termini">Termini e Condizioni</a> ·
            <a href="/disclaimer">Disclaimer</a> ·
            <a href="/privacy">Privacy</a> ·
            <a href="/cookie">Cookie Policy</a> ·
            <button type="button" id="ft-cookie-reopen-footer" class="cookie-footer-link">Preferenze cookie</button>
          </footer>
          <section id="ft-cookie-banner" role="dialog" aria-modal="false" aria-labelledby="ft-cookie-title" aria-describedby="ft-cookie-description" hidden>
            <h2 id="ft-cookie-title">La tua privacy conta</h2>
            <p id="ft-cookie-description">FixTude utilizza strumenti tecnici necessari al funzionamento del sito. Google Analytics viene caricato soltanto se autorizzi le statistiche. I pixel pubblicitari non sono attivi. Puoi accettare, rifiutare o personalizzare la tua scelta.</p>
            <p>Leggi la <a href="/cookie">Cookie Policy</a> e l'<a href="/privacy">Informativa Privacy</a>.</p>
            <div class="ft-cookie-actions">
              <button type="button" class="ft-cookie-btn primary" id="ft-cookie-accept">Accetta tutti</button>
              <button type="button" class="ft-cookie-btn" id="ft-cookie-reject">Rifiuta gli opzionali</button>
              <button type="button" class="ft-cookie-btn" id="ft-cookie-customize">Personalizza</button>
            </div>
            <div id="ft-cookie-preferences" hidden>
              <div class="ft-cookie-row"><div><strong>Cookie tecnici necessari</strong><small>Necessari per le funzioni essenziali; sempre attivi.</small></div><input type="checkbox" checked disabled aria-label="Cookie tecnici sempre attivi"></div>
              <div class="ft-cookie-row"><label for="ft-cookie-analytics">Statistiche / Google Analytics<small>Google Analytics viene caricato soltanto se autorizzi questa categoria; puoi modificare la scelta in qualsiasi momento.</small></label><input type="checkbox" id="ft-cookie-analytics"></div>
              <div class="ft-cookie-row"><label for="ft-cookie-marketing">Pubblicità / pixel<small>I pixel pubblicitari non sono attivi.</small></label><input type="checkbox" id="ft-cookie-marketing"></div>
              <div class="ft-cookie-actions"><button type="button" class="ft-cookie-btn primary" id="ft-cookie-save">Salva preferenze</button><button type="button" class="ft-cookie-btn" id="ft-cookie-cancel">Annulla</button></div>
            </div>
          </section>
          <style>
            #ft-cookie-banner{position:fixed;z-index:99999;left:16px;right:16px;bottom:16px;max-width:900px;margin:auto;background:#fff;color:#172033;border:1px solid #dce4f2;border-radius:16px;box-shadow:0 18px 55px rgba(14,30,70,.22);padding:20px;font:14px/1.55 Arial,Helvetica,sans-serif}
            #ft-cookie-banner[hidden],#ft-cookie-preferences[hidden]{display:none!important}
            #ft-cookie-banner h2{font-size:19px;line-height:1.25;margin:0 0 8px;color:#172033}
            #ft-cookie-banner p{margin:0 0 12px;color:#4b5565}
            #ft-cookie-banner a{color:#2457d6;text-decoration:underline}
            .ft-cookie-actions{display:flex;flex-wrap:wrap;gap:9px;margin-top:14px}
            .ft-cookie-btn{border:1px solid #cbd5e1;border-radius:9px;padding:10px 14px;font-weight:700;font-size:14px;cursor:pointer;background:#fff;color:#172033}
            .ft-cookie-btn.primary{background:#2457d6;border-color:#2457d6;color:#fff}
            .ft-cookie-btn:focus-visible{outline:3px solid #93c5fd;outline-offset:2px}
            #ft-cookie-preferences{border-top:1px solid #e5eaf2;margin-top:14px;padding-top:12px}
            .ft-cookie-row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:8px 0;border-bottom:1px solid #edf0f5}
            .ft-cookie-row label{font-weight:700;margin:0}
            .ft-cookie-row small{display:block;font-weight:400;color:#667085}
            .ft-cookie-row input{width:19px;height:19px;accent-color:#2457d6;flex-shrink:0}
            @media(max-width:600px){ #ft-cookie-banner{left:8px;right:8px;bottom:8px;padding:16px;max-height:82vh;overflow:auto;border-radius:13px}.ft-cookie-actions{display:grid;grid-template-columns:1fr}.ft-cookie-btn{width:100%;min-height:43px}}
          </style>
          <script id="fixtude-cookie-script">
          (() => {
            const banner = document.getElementById('ft-cookie-banner');
            if (!banner) return;
            const prefs = document.getElementById('ft-cookie-preferences');
            const analytics = document.getElementById('ft-cookie-analytics');
            const marketing = document.getElementById('ft-cookie-marketing');
            const footerButton = document.getElementById('ft-cookie-reopen-footer');
            const cookieName = 'fixtude_cookie_consent';
            const GA_ID = 'G-HMBBNLHC7E';

            function getChoice() {
              const prefix = cookieName + '=';
              const item = document.cookie.split('; ').find(v => v.startsWith(prefix));
              if (!item) return null;
              try { return JSON.parse(decodeURIComponent(item.slice(prefix.length))); }
              catch (_) { return null; }
            }

            function loadAnalytics() {
              window['ga-disable-' + GA_ID] = false;
              window.dataLayer = window.dataLayer || [];
              window.gtag = window.gtag || function(){ window.dataLayer.push(arguments); };
              if (!window.__fixtudeGaInitialized) {
                window.gtag('consent', 'default', {
                  analytics_storage: 'denied',
                  ad_storage: 'denied',
                  ad_user_data: 'denied',
                  ad_personalization: 'denied'
                });
                window.gtag('js', new Date());
                window.gtag('config', GA_ID, {send_page_view: false});
                const script = document.createElement('script');
                script.async = true;
                script.src = 'https://www.googletagmanager.com/gtag/js?id=' + encodeURIComponent(GA_ID);
                document.head.appendChild(script);
                window.__fixtudeGaInitialized = true;
              }
              window.gtag('consent', 'update', {
                analytics_storage: 'granted',
                ad_storage: 'denied',
                ad_user_data: 'denied',
                ad_personalization: 'denied'
              });
              window.gtag('event', 'page_view', {
                page_path: window.location.pathname + window.location.search,
                page_title: document.title
              });
            }

            function applyAnalyticsConsent(allowed) {
              window['ga-disable-' + GA_ID] = !allowed;
              if (allowed) {
                loadAnalytics();
              } else if (typeof window.gtag === 'function') {
                window.gtag('consent', 'update', {
                  analytics_storage: 'denied',
                  ad_storage: 'denied',
                  ad_user_data: 'denied',
                  ad_personalization: 'denied'
                });
              }
            }

            function saveChoice(a, m) {
              const value = {necessary:true, analytics:!!a, marketing:!!m, savedAt:new Date().toISOString(), version:2};
              const secure = location.protocol === 'https:' ? '; Secure' : '';
              document.cookie = cookieName + '=' + encodeURIComponent(JSON.stringify(value)) + '; Max-Age=15552000; Path=/; SameSite=Lax' + secure;
              applyAnalyticsConsent(!!a);
              banner.hidden = true;
            }

            function openBanner(showPrefs) {
              banner.hidden = false;
              prefs.hidden = !showPrefs;
              const old = getChoice();
              analytics.checked = !!(old && old.analytics);
              marketing.checked = !!(old && old.marketing);
            }

            const existingChoice = getChoice();
            if (!existingChoice) openBanner(false);
            else applyAnalyticsConsent(!!existingChoice.analytics);

            if (footerButton) footerButton.addEventListener('click', () => openBanner(true));
            document.getElementById('ft-cookie-accept').addEventListener('click', () => saveChoice(true,true));
            document.getElementById('ft-cookie-reject').addEventListener('click', () => saveChoice(false,false));
            document.getElementById('ft-cookie-customize').addEventListener('click', () => openBanner(true));
            document.getElementById('ft-cookie-save').addEventListener('click', () => saveChoice(analytics.checked,marketing.checked));
            document.getElementById('ft-cookie-cancel').addEventListener('click', () => { if (getChoice()) banner.hidden = true; else openBanner(false); });
          })();
          </script>
          {% if include_chat %}
          <button class="chat-launcher" id="chatLauncher"
                  type="button" aria-expanded="false">
            💬 Chat con un esperto
          </button>
          <section class="chat-panel" id="chatPanel"
                   aria-label="Chat con un esperto FixTude">
            <div class="chat-head">
              <div>
                <strong>Chat con un esperto FixTude</strong>
                <div class="chat-status" id="chatStatus">
                  Scrivi un messaggio
                </div>
              </div>
              <button id="chatClose" type="button"
                      aria-label="Chiudi chat">×</button>
            </div>
            <div class="chat-messages" id="chatMessages"
                 aria-live="polite">
              <p class="muted">Chat FixTude: disponibile dal lunedì al venerdì, dalle 9:30 alle 14:30. Fuori orario e nel fine settimana puoi lasciare un messaggio: ti risponderemo nel più breve tempo possibile.</p>
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
              status.textContent = data.status || "Disponibile dal lunedì al venerdì, dalle 9:30 alle 14:30";
              if (data.notice) {
                const note = document.createElement("p");
                note.className = "muted";
                note.textContent = data.notice;
                messages.replaceChildren(note);
                lastCount = -1;
              }
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
                  "Disponibile dalle 9:30 alle 14:30";
                if (data.notice && data.messages.length === 0) {
                  const note = document.createElement("p");
                  note.className = "muted";
                  note.textContent = data.notice;
                  messages.replaceChildren(note);
                }
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
        seo_title=seo_title,
        seo_description=seo_description,
        canonical_url=canonical_url,
        seo_base_url=SEO_BASE_URL,
        robots=robots,
        structured_data=structured_data,
        content=content,
        include_chat=include_chat
    )


# =========================================================
# HOME
# =========================================================

@app.route("/")
def home():
    content = """
    <style>
      .home-hero-layout{display:block;max-width:900px;margin:0 auto;align-items:start;text-align:center}
      .home-hero-copy{max-width:820px;margin:0 auto}
      .home-hero-copy h1{max-width:100%;margin-left:auto;margin-right:auto;text-align:center}
      .home-hero-copy p{max-width:760px;margin-left:auto;margin-right:auto;text-align:center}
      .home-hero-copy h1{font-size:clamp(30px,4vw,46px);line-height:1.1;margin:0 0 14px}
      .home-hero-copy p{font-size:18px;line-height:1.55;max-width:680px}
      .home-situation-panel{width:100%;max-width:900px;margin:0 auto;display:flex;flex-direction:column;align-items:center;text-align:center;box-sizing:border-box}
      .home-situation-title{display:block;width:100%;max-width:820px;box-sizing:border-box;text-align:center;margin:30px auto 8px;padding:0 12px;align-self:center}
      .home-situation-intro{display:block;width:100%;max-width:680px;box-sizing:border-box;text-align:center;margin:0 auto 12px;padding:0 12px;align-self:center}
      .home-situation-options{display:grid;width:100%;box-sizing:border-box;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px;margin:24px auto 8px;max-width:820px;align-items:start;justify-content:center;align-self:center}
      .home-situation-option{display:flex;flex-direction:column;align-items:center;gap:15px;min-width:0}
      .home-situation-choice{position:relative;isolation:isolate;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;width:clamp(98px,11.7vw,133px);height:clamp(98px,11.7vw,133px);padding:10px;border:3px solid rgba(255,255,255,.75);border-radius:50%;cursor:pointer;font:inherit;text-align:center;color:#fff;text-decoration:none;overflow:hidden;transform:translateY(-5px);box-shadow:0 6px 0 var(--button-depth),0 12px 17px rgba(16,24,40,.25),inset 0 3px 5px rgba(255,255,255,.75),inset 0 -8px 12px rgba(0,0,0,.2);transition:transform .13s ease,box-shadow .13s ease,filter .13s ease;touch-action:manipulation}
      .home-situation-choice:before{content:"";position:absolute;z-index:-1;left:9%;top:5%;width:82%;height:43%;border-radius:50%;background:linear-gradient(180deg,rgba(255,255,255,.78),rgba(255,255,255,.05));filter:blur(1px);pointer-events:none}
      .home-situation-choice:hover{filter:brightness(1.07);transform:translateY(-11px);box-shadow:0 13px 0 var(--button-depth),0 23px 28px rgba(16,24,40,.3),inset 0 3px 5px rgba(255,255,255,.75),inset 0 -8px 12px rgba(0,0,0,.2)}
      .home-situation-choice:active{transform:translateY(4px) scale(.985);box-shadow:0 1px 0 var(--button-depth),0 4px 8px rgba(16,24,40,.22),inset 0 4px 9px rgba(0,0,0,.22);filter:brightness(.97)}
      .home-situation-choice:focus-visible{outline:3px solid #1675d1;outline-offset:7px}
      .home-situation-icon{font-size:24px;line-height:1.05;font-weight:900}
      .home-situation-label{font-size:clamp(15px,1.6vw,19px);font-weight:900;line-height:1.05;text-shadow:0 2px 2px rgba(0,0,0,.2)}
      .home-situation-subtitle{font-size:clamp(9px,1vw,11px);font-weight:850;line-height:1.2}
      .home-situation-description{display:block;max-width:230px;text-align:center;font-size:13px;line-height:1.45;font-style:italic;margin:0 auto;overflow-wrap:break-word}
      .home-situation-red{--button-depth:#861821;background:radial-gradient(ellipse at 35% 18%,#ff9588 0%,#ff3d2f 33%,#d70d0d 69%,#9f0009 100%);border-color:#ffb6aa}
      .home-situation-yellow{--button-depth:#98600b;color:#382707;background:radial-gradient(ellipse at 35% 18%,#fff7a0 0%,#ffdb24 35%,#f5ac00 72%,#b66b00 100%);border-color:#fff0a0}
      .home-situation-green{--button-depth:#07502e;background:radial-gradient(ellipse at 35% 18%,#82f2b1 0%,#16b968 35%,#078346 70%,#00532c 100%);border-color:#a6f7c8}
      @media(max-width:700px){.home-hero-layout{grid-template-columns:1fr;gap:18px}.home-hero-copy h1{font-size:clamp(27px,6vw,36px);line-height:1.12}.home-hero-copy p{font-size:16px;line-height:1.5}.home-situation-panel{width:100%;max-width:100%;padding:0 4px}.home-situation-title{font-size:clamp(21px,5vw,28px);margin-top:24px;padding:0 8px}.home-situation-intro{font-size:15px;line-height:1.4;padding:0 8px}.home-situation-options{grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:20px;padding:0 2px}.home-situation-option{gap:9px}.home-situation-choice{width:min(18.2vw,86px);height:min(18.2vw,86px);min-width:68px;min-height:68px;padding:5px;border-width:2px;transform:translateY(-3px);box-shadow:0 4px 0 var(--button-depth),0 7px 9px rgba(16,24,40,.22),inset 0 2px 4px rgba(255,255,255,.75),inset 0 -4px 6px rgba(0,0,0,.2)}.home-situation-choice:hover{transform:translateY(-5px)}.home-situation-icon{font-size:17px}.home-situation-label{font-size:clamp(10px,2.7vw,13px)}.home-situation-subtitle{font-size:clamp(7px,2vw,9px)}.home-situation-description{max-width:100%;font-size:10px;line-height:1.35}.home-situation-title{margin-top:24px}.home-situation-intro{font-size:15px;line-height:1.4}}
      @media(prefers-reduced-motion:reduce){.home-situation-choice{transition:none}}
    </style>
    <section class="hero">
      <div class="home-hero-layout">
        <div class="home-hero-copy">
          <h1>Fai chiarezza sulla tua situazione debitoria.</h1>
          <p><strong>Semplice. Riservato. Senza registrazione.</strong></p>
        </div>
      </div>
      <div class="home-situation-panel">
      <h2 class="home-situation-title">Da dove vuoi cominciare?</h2>
      <p class="home-situation-intro">Scegli il pulsante che descrive la tua situazione. Il calcolatore ti fornirà un primo esito di sostenibilità.</p>
      <div class="home-situation-options">
        <div class="home-situation-option">
          <a class="home-situation-choice home-situation-red" href="/misuratore?scenario=rosso#calculatorStart" aria-label="Emergenza: non riesco a pagare la rata di questo mese e ho già ricevuto solleciti">
            <span class="home-situation-icon" aria-hidden="true">!</span><span class="home-situation-subtitle">EMERGENZA</span>
          </a>
          <em class="home-situation-description">Non riesco a pagare la rata di questo mese e ho già ricevuto solleciti.</em>
        </div>
        <div class="home-situation-option">
          <a class="home-situation-choice home-situation-yellow" href="/misuratore?scenario=giallo#calculatorStart" aria-label="Allerta: pago tutto ma faccio fatica e non mi rimane quasi nulla dello stipendio">
            <span class="home-situation-icon" aria-hidden="true">!</span><span class="home-situation-subtitle">ALLERTA</span>
          </a>
          <em class="home-situation-description">Pago tutto, ma faccio fatica. Non mi rimane quasi nulla dello stipendio.</em>
        </div>
        <div class="home-situation-option">
          <a class="home-situation-choice home-situation-green" href="/misuratore?scenario=verde#calculatorStart" aria-label="Controllo: voglio verificare se i miei debiti sono sostenibili">
            <span class="home-situation-icon" aria-hidden="true">✓</span><span class="home-situation-subtitle">CONTROLLO</span>
          </a>
          <em class="home-situation-description">La situazione è gestibile, ma voglio verificare se i miei debiti sono davvero sostenibili.</em>
        </div>
      </div>
      </div>
    </section>
    <section class="card start-card"><h2>Come funziona FixTude</h2><div class="grid">
      <div><strong>1. Fai il calcolo</strong><p class="muted">Usa gratuitamente il calcolatore. I dati inseriti restano nel browser e non serve registrarsi.</p></div>
      <div><strong>2. Valuta il risultato</strong><p class="muted">Ottieni una stima orientativa del margine mensile e del peso delle rate.</p></div>
      <div><strong>3. Se vuoi, chiedi di parlare con un esperto</strong><p class="muted">Lascia i tuoi recapiti e una breve descrizione della situazione per chiedere il contatto di un esperto FixTude. Il contatto è facoltativo e non comporta obblighi.</p></div>
      <div><strong>4. Hai una domanda?</strong><p class="muted">Apri la chat con un esperto FixTude in basso a destra per inviare un messaggio allo staff. La chat è disponibile senza registrazione.</p></div>
    </div></section>
    <p class="muted">Il calcolatore e i materiali gratuiti hanno finalità informative e orientative. Gli eventuali servizi di assistenza legale sono descritti nella sezione dedicata e vengono svolti da professionisti abilitati sulla base di uno specifico mandato. Nessun servizio garantisce l'accettazione di proposte da parte dei creditori o un esito giudiziario favorevole.</p>
    """
    return page("Fai chiarezza", content)


# =========================================================
# CALCOLATORE
# =========================================================

@app.route("/misuratore")
def misuratore():
    content = """
    <h1>Calcolatore di sostenibilità del debito</h1>
    <p class="muted">
      Una prima analisi orientativa del bilancio: saldo mensile, margine prudenziale,
      quota indicativa destinabile al debito e possibili scenari di rientro.
      Tutti i calcoli sono eseguiti nel browser: gli importi non vengono inviati al server.
    </p>

    <section class="card" id="calculatorStart">
      <h2>1. Entrate nette mensili</h2>
      <p class="muted">Inserisci entrate effettivamente disponibili e ragionevolmente stabili. Evita di conteggiare somme occasionali o incerte come reddito ricorrente.</p>
      <div id="calculatorWelcome" class="calculator-welcome" role="status" aria-live="polite" hidden>
        <button type="button" class="welcome-close" id="calculatorWelcomeClose" aria-label="Chiudi messaggio">×</button>
        <strong>Usa il calcolatore e analizziamo la tua situazione.</strong>
        <span>Un passo alla volta, per capire meglio il tuo margine economico.</span>
      </div>
      <div id="incomeRows"></div>
      <button class="btn outline" type="button" id="addIncome">+ Aggiungi entrata</button>
    </section>

    <section class="card">
      <h2>2. Spese mensili</h2>
      <p class="muted">Inserisci le spese reali, incluse casa, utenze, alimentari, trasporti, salute e altre necessità. Non inserire qui le rate dei debiti che registrerai nella sezione successiva, per evitare un doppio conteggio.</p>
      <div id="expenseRows"></div>
      <button class="btn outline" type="button" id="addExpense">+ Aggiungi spesa</button>
    </section>

    <section class="card">
      <h2>3. Debiti e rate già in corso</h2>
      <p class="muted">Per ogni posizione indica il debito residuo e la rata mensile attualmente pagata. Le rate in corso vengono sottratte dal saldo mensile, oltre alle spese indicate sopra.</p>
      <div id="debtRows"></div>
      <button class="btn outline" type="button" id="addDebt">+ Aggiungi debito</button>
      <div class="field" style="margin-top:18px">
        <label for="lumpSum">Somma una tantum effettivamente disponibile per un eventuale saldo e stralcio (€)</label>
        <input id="lumpSum" type="number" min="0" max="100000000" step="0.01" inputmode="decimal" value="0">
        <small class="muted">Indica soltanto risorse già disponibili, non somme ipotetiche o nuovo credito da ottenere.</small>
      </div>
    </section>

    <section class="card">
      <h2>4. Esito della valutazione preliminare</h2>
      <div class="total-grid">
        <div class="total"><span class="muted">Entrate mensili</span><strong id="totalIncome">€ 0,00</strong></div>
        <div class="total"><span class="muted">Spese dichiarate</span><strong id="totalExpenses">€ 0,00</strong></div>
        <div class="total"><span class="muted">Rate già in corso</span><strong id="totalPayments">€ 0,00</strong></div>
        <div class="total"><span class="muted">Debito complessivo inserito</span><strong id="totalDebt">€ 0,00</strong></div>
        <div class="total"><span class="muted">Saldo mensile</span><strong id="residual">€ 0,00</strong></div>
        <div class="total"><span class="muted">Margine prudenziale</span><strong id="prudentialMargin">€ 0,00</strong></div>
        <div class="total"><span class="muted">Quota mensile iniziale suggerita</span><strong id="suggestedQuota">€ 0,00</strong></div>
        <div class="total"><span class="muted">Riserva imprevisti applicata</span><strong>€ 200,00</strong></div>
      </div>
      <div class="traffic-light" id="trafficLight" role="status" aria-live="polite">
        <span class="signal" aria-hidden="true"></span>
        <div><strong id="trafficTitle">Valutazione non ancora disponibile</strong><div class="muted" id="trafficDescription">Inserisci i dati necessari per ottenere un esito orientativo.</div></div>
      </div>
      <div class="notice" id="assessment">Completa i dati per visualizzare l'analisi.</div>
      <p class="muted" id="dataQuality"></p>
    </section>

    <section class="card">
      <h2>5. Simulazioni di piano di rientro</h2>
      <p class="muted">Il confronto considera il debito complessivo dichiarato e non include eventuali interessi, spese, penali o condizioni contrattuali. La compatibilità matematica non significa che il creditore accetterà il piano.</p>
      <div class="table-wrap">
        <table class="result-table">
          <thead><tr><th>Durata</th><th>Rata mensile teorica</th><th>Confronto con la quota suggerita</th></tr></thead>
          <tbody id="planRows"><tr><td colspan="3">Inserisci un debito residuo per calcolare gli scenari.</td></tr></tbody>
        </table>
      </div>
      <div class="notice" id="planSummary">Le simulazioni appariranno dopo l'inserimento dei dati.</div>
    </section>

    <section class="card">
      <h2>6. Simulazione separata: saldo e stralcio</h2>
      <p class="muted">Esempio teorico basato su un'offerta pari al 60% del debito inserito. È soltanto uno scenario negoziale: non è una percentuale garantita né una previsione di accettazione.</p>
      <div class="total-grid">
        <div class="total"><span class="muted">Offerta teorica al 60%</span><strong id="settlementAmount">—</strong></div>
        <div class="total"><span class="muted">Somma una tantum disponibile</span><strong id="availableLumpSum">€ 0,00</strong></div>
      </div>
      <div class="notice" id="settlementSummary">Inserisci il debito e l'eventuale somma una tantum disponibile per valutare questo scenario.</div>
    </section>

    <section class="card">
      <h2>7. Cosa fare dopo</h2>
      <p id="nextStep">Il risultato è preliminare e dipende dalla completezza e accuratezza dei dati inseriti.</p>
      <a class="btn" href="/consulenza">Richiedi di essere contattato</a>
      <button class="btn outline" type="button" id="resetCalculator" style="margin-left:8px">Azzera tutti i campi</button>
      <p class="muted" style="margin-top:16px">Questo strumento fornisce una simulazione informativa, non una certificazione di solvibilità, una consulenza legale, fiscale o finanziaria personalizzata, né una garanzia di accordo con i creditori. Leggi il <a href="/disclaimer">Disclaimer FixTude</a>.</p>
    </section>

    <style>
      .situation-options{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:22px;margin:24px auto 8px;max-width:900px}
      .situation-option{display:flex;flex-direction:column;align-items:center;gap:18px;min-width:0}
      .situation-choice{position:relative;isolation:isolate;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;width:clamp(98px,11.7vw,133px);height:clamp(98px,11.7vw,133px);padding:10px;border:3px solid rgba(255,255,255,.72);border-radius:50%;cursor:pointer;font:inherit;text-align:center;color:#fff;overflow:hidden;transform:translateY(-7px);box-shadow:0 7px 0 var(--button-depth),0 15px 22px rgba(16,24,40,.25),inset 0 3px 5px rgba(255,255,255,.75),inset 0 -7px 12px rgba(0,0,0,.2);transition:transform .13s ease,box-shadow .13s ease,filter .13s ease;touch-action:manipulation}
      .situation-choice::before{content:"";position:absolute;z-index:-1;left:9%;top:5%;width:82%;height:43%;border-radius:50%;background:linear-gradient(180deg,rgba(255,255,255,.72),rgba(255,255,255,.06));filter:blur(1px);pointer-events:none}
      .situation-choice::after{content:"";position:absolute;z-index:-1;inset:5px;border:2px solid rgba(255,255,255,.27);border-radius:50%;box-shadow:inset 0 2px 4px rgba(255,255,255,.25);pointer-events:none}
      .situation-choice:hover{filter:brightness(1.07);transform:translateY(-10px);box-shadow:0 10px 0 var(--button-depth),0 20px 26px rgba(16,24,40,.28),inset 0 3px 5px rgba(255,255,255,.75),inset 0 -7px 12px rgba(0,0,0,.2)}
      .situation-choice:active,.situation-choice.is-pressed{transform:translateY(3px) scale(.985);box-shadow:0 1px 0 var(--button-depth),0 4px 8px rgba(16,24,40,.22),inset 0 4px 9px rgba(0,0,0,.22);filter:brightness(.97)}
      .situation-choice[aria-pressed="true"]{outline:3px solid var(--button-depth);outline-offset:5px}
      .situation-choice:focus-visible{outline:3px solid var(--blue2);outline-offset:7px}
      .situation-icon{display:flex;align-items:center;justify-content:center;width:47px;height:47px;margin-bottom:4px;border:4px solid currentColor;border-radius:50%;font-size:37px;font-weight:900;line-height:1;text-shadow:0 2px 2px rgba(0,0,0,.15)}
      .situation-shield{border-radius:13px 13px 18px 18px;width:43px;height:43px;font-size:29px}
      .situation-label{font-size:clamp(20px,2.2vw,28px);font-weight:900;letter-spacing:.3px;line-height:1.05;text-shadow:0 2px 2px rgba(0,0,0,.2)}
      .situation-subtitle{font-size:clamp(13px,1.3vw,17px);font-weight:850;line-height:1.2;text-shadow:0 1px 2px rgba(0,0,0,.18)}
      .situation-description{max-width:250px;text-align:center;font-size:14px;line-height:1.5;font-style:italic;color:var(--ink)}
      .situation-red{--button-depth:#861821;background:radial-gradient(ellipse at 35% 18%,#ff9588 0%,#ff3d2f 33%,#d70d0d 69%,#9f0009 100%);border-color:#ffb6aa}
      .situation-yellow{--button-depth:#98600b;color:#382707;background:radial-gradient(ellipse at 35% 18%,#fff7a0 0%,#ffdb24 35%,#f5ac00 72%,#b66b00 100%);border-color:#fff0a0}
      .situation-green{--button-depth:#07502e;background:radial-gradient(ellipse at 35% 18%,#82f2b1 0%,#16b968 35%,#078346 70%,#00532c 100%);border-color:#a6f7c8}
      .calculator-welcome{position:relative;margin:16px 0;padding:16px 42px 16px 16px;border:1px solid #b8d5f5;border-radius:12px;background:#eef5ff;color:#203047;box-shadow:0 5px 18px rgba(18,58,114,.08)}
      .calculator-welcome strong,.calculator-welcome span{display:block}.calculator-welcome span{margin-top:5px;font-size:14px;color:#52647a}
      .calculator-welcome[hidden]{display:none!important}.welcome-close{position:absolute;right:9px;top:7px;border:0;background:transparent;font-size:23px;line-height:1;cursor:pointer;color:#52647a}
      @media(max-width:700px){.situation-options{grid-template-columns:1fr;gap:24px}.situation-choice{width:176px;height:176px}.situation-description{max-width:320px}}
      @media(prefers-reduced-motion:reduce){.situation-choice{transition:none}}
      /* Calcolatore guidato: cinque schermate orizzontali */
      .calc-wizard{margin:18px 0 28px;min-width:0}
      .calc-progress{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:0 0 12px;font-size:14px;color:var(--muted,#64748b)}
      .calc-progress-track{height:6px;border-radius:999px;background:#e5eaf1;overflow:hidden;margin-bottom:18px}
      .calc-progress-fill{height:100%;width:20%;border-radius:999px;background:var(--blue2,#2563eb);transition:width .25s ease}
      .calc-viewport{overflow:hidden;width:100%;border-radius:16px;touch-action:pan-y}
      .calc-track{display:flex;align-items:stretch;width:100%;min-width:100%;transition:transform .32s ease;will-change:transform;transform:translate3d(0,0,0)}
      .calc-panel{flex:0 0 100%;width:100%;min-width:100%;max-width:100%;box-sizing:border-box;padding:1px 2px 8px}
      .calc-panel>.card{height:100%;box-sizing:border-box;margin:0}
      .calc-panel .calc-nav{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:22px;padding-top:16px;border-top:1px solid var(--line,#e2e8f0)}
      .calc-nav .btn{cursor:pointer;min-height:44px}
      .calc-nav .calc-back,.calc-nav .calc-next{background:#155eef!important;color:#fff!important;border:1px solid #155eef!important;border-radius:10px;font-weight:700;box-shadow:0 3px 0 #0b3eaa;}
      .calc-nav .calc-back:hover,.calc-nav .calc-next:hover{background:#0b4ed0!important;color:#fff!important;filter:brightness(1.04)}
      .calc-nav .calc-back:active,.calc-nav .calc-next:active{transform:translateY(2px);box-shadow:0 1px 0 #0b3eaa}
      .calc-nav .calc-back[hidden]{display:none!important}
      .calc-nav .calc-next{margin-left:auto}
      .calc-step-caption{font-size:13px;color:var(--muted,#64748b)}
      @media(max-width:640px){.calc-progress{font-size:13px}.calc-panel>.card{padding:18px 15px}.calc-nav{gap:8px!important}.calc-nav .btn{padding:11px 13px;font-size:14px}.calc-step-caption{max-width:74px;text-align:center}}
      @media(prefers-reduced-motion:reduce){.calc-track,.calc-progress-fill{transition:none}}
      .traffic-light{display:flex;gap:14px;align-items:flex-start;padding:16px;margin:16px 0;border:1px solid var(--line);border-radius:12px;background:#f7f9fc}
      .traffic-light .signal{width:18px;height:18px;min-width:18px;border-radius:50%;background:#94a3b8;margin-top:4px;box-shadow:0 0 0 5px #e2e8f0}
      .traffic-light.green{background:#effaf2;border-color:#b6e3c1}.traffic-light.green .signal{background:#188448;box-shadow:0 0 0 5px #d5f0dd}
      .traffic-light.yellow{background:#fff9e9;border-color:#f0d68b}.traffic-light.yellow .signal{background:#b77900;box-shadow:0 0 0 5px #ffedbd}
      .traffic-light.red{background:#fff1f1;border-color:#efb8b8}.traffic-light.red .signal{background:#b42318;box-shadow:0 0 0 5px #fbd8d8}
      .table-wrap{width:100%;overflow-x:auto}.result-table{width:100%;border-collapse:collapse;margin:12px 0;font-size:14px}.result-table th,.result-table td{text-align:left;padding:11px 9px;border-bottom:1px solid var(--line);vertical-align:top}.result-table th{background:#f1f5fa}
      .status-good{color:#18753a;font-weight:700}.status-caution{color:#8a5a00;font-weight:700}.status-bad{color:#b42318;font-weight:700}
      @media(max-width:640px){.result-table{font-size:13px}.result-table th,.result-table td{padding:9px 7px}.table-wrap{margin-left:-2px;width:calc(100% + 4px)}}
    </style>

    <script>
    (() => {
      // Inizializza il wizard solo dopo che tutto il modulo è presente nel DOM.
      function initCalculatorWizard() {
        const calculatorStart = document.getElementById("calculatorStart");
        if (!calculatorStart || document.querySelector(".calc-wizard")) return;
        const host = calculatorStart.parentElement;
        if (!host) return;
        const cards = Array.from(host.querySelectorAll(":scope > section.card"));
        if (cards.length < 7) {
          console.error("FixTude: impossibile inizializzare il calcolatore guidato; sezioni trovate:", cards.length);
          return;
        }

        const wizard = document.createElement("div");
        wizard.className = "calc-wizard";
        wizard.setAttribute("aria-label", "Calcolatore guidato a scorrimento orizzontale in cinque passaggi");
        const progress = document.createElement("div");
        progress.className = "calc-progress";
        const progressLabel = document.createElement("strong");
        progressLabel.textContent = "Passaggio 1 di 5 · Entrate mensili";
        const progressHint = document.createElement("span");
        progressHint.textContent = "I dati restano nel browser";
        progress.append(progressLabel, progressHint);
        const progressTrack = document.createElement("div");
        progressTrack.className = "calc-progress-track";
        progressTrack.setAttribute("aria-hidden", "true");
        const progressFill = document.createElement("div");
        progressFill.className = "calc-progress-fill";
        progressTrack.appendChild(progressFill);
        const viewport = document.createElement("div");
        viewport.className = "calc-viewport";
        viewport.setAttribute("aria-live", "polite");
        const track = document.createElement("div");
        track.className = "calc-track";
        viewport.appendChild(track);
        wizard.append(progress, progressTrack, viewport);
        host.insertBefore(wizard, cards[0]);

        // 5 passaggi: entrate, spese, debiti, esito, simulazioni e prossimi passi.
        const groups = [[0], [1], [2], [3], [4, 5, 6]];
        const titles = ["Entrate mensili", "Spese mensili", "Debiti e rate", "Il tuo esito", "Simulazioni e prossimi passi"];
        let currentStep = 0;
        const panels = groups.map((indices, index) => {
          const panel = document.createElement("section");
          panel.className = "calc-panel";
          panel.setAttribute("role", "group");
          panel.setAttribute("aria-label", "Passaggio " + (index + 1) + " di 5: " + titles[index]);
          indices.forEach(i => { if (cards[i]) panel.appendChild(cards[i]); });
          const nav = document.createElement("div");
          nav.className = "calc-nav";
          const back = document.createElement("button");
          back.type = "button";
          back.className = "btn calc-back";
          back.textContent = "← Indietro";
          back.hidden = index === 0;
          back.addEventListener("click", () => showStep(currentStep - 1));
          const caption = document.createElement("span");
          caption.className = "calc-step-caption";
          caption.textContent = (index + 1) + " / 5";
          const next = document.createElement("button");
          next.type = "button";
          next.className = "btn calc-next";
          next.textContent = index === 4 ? "Torna all'esito ↑" : "Avanti →";
          next.addEventListener("click", () => showStep(index === 4 ? 3 : currentStep + 1));
          nav.append(back, caption, next);
          panel.appendChild(nav);
          track.appendChild(panel);
          return panel;
        });

        function showStep(step) {
          currentStep = Math.max(0, Math.min(4, step));
          // Percentuale riferita alla larghezza del pannello, non alla larghezza totale del track.
          track.style.transform = "translate3d(" + (-currentStep * 100) + "%, 0, 0)";
          progressLabel.textContent = "Passaggio " + (currentStep + 1) + " di 5 · " + titles[currentStep];
          progressFill.style.width = (((currentStep + 1) / 5) * 100) + "%";
          panels.forEach((panel, i) => {
            panel.setAttribute("aria-hidden", i === currentStep ? "false" : "true");
            panel.inert = i !== currentStep;
          });
          if (currentStep >= 3) {
            const lumpSum = document.getElementById("lumpSum");
            if (lumpSum) lumpSum.dispatchEvent(new Event("input", { bubbles: true }));
          }
          const activePanel = panels[currentStep];
          if (activePanel) activePanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
        }
        showStep(0);
        if (window.location.hash === "#calculatorStart") {
          window.setTimeout(() => calculatorStart.scrollIntoView({ behavior: "smooth", block: "start" }), 150);
        }
      }
      if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initCalculatorWizard, { once: true });
      else initCalculatorWizard();
      const definitions = {
        income: [["Stipendio o pensione", 0], ["Altre entrate ricorrenti", 0]],
        expense: [["Casa e utenze", 0], ["Alimentari e necessità", 0], ["Trasporti", 0], ["Salute", 0], ["Altre spese", 0]],
        debt: [["Debito", 0, 0]]
      };
      const labels = {income:"Entrata", expense:"Spesa", debt:"Debito"};
      const RESERVE = 200;
      const QUOTA_FACTOR = 0.50;
      const money = n => Number(n || 0).toLocaleString("it-IT", {style:"currency", currency:"EUR"});
      const safeNumber = input => {
        const n = Number(input.value);
        return Number.isFinite(n) && n > 0 ? Math.min(n, 100000000) : 0;
      };

      function addRow(type, label="", amount=0, payment=0) {
        const container = document.getElementById(type === "income" ? "incomeRows" : type === "expense" ? "expenseRows" : "debtRows");
        const row = document.createElement("div");
        row.className = "entry-row";
        const nameField = document.createElement("div");
        nameField.className = "field name-field";
        const nameLabel = document.createElement("label");
        nameLabel.textContent = type === "debt" ? "Nome del debito (facoltativo)" : "Voce";
        nameField.appendChild(nameLabel);
        const nameInput = document.createElement("input");
        nameInput.type = "text";
        nameInput.maxLength = 80;
        nameInput.value = label;
        nameInput.placeholder = labels[type];
        nameInput.setAttribute("aria-label", "Descrizione");
        nameField.appendChild(nameInput);

        function makeAmountField(labelText, value, ariaLabel) {
          const field = document.createElement("div");
          field.className = "field";
          const fieldLabel = document.createElement("label");
          fieldLabel.textContent = labelText;
          field.appendChild(fieldLabel);
          const input = document.createElement("input");
          input.type = "number"; input.min = "0"; input.max = "100000000"; input.step = "0.01";
          input.inputMode = "decimal"; input.value = value; input.setAttribute("aria-label", ariaLabel);
          field.appendChild(input); input.addEventListener("input", calculate);
          return {field, input};
        }
        const primary = makeAmountField(type === "debt" ? "Debito residuo (€)" : "Importo mensile (€)", amount, type === "debt" ? "Importo residuo del debito in euro" : "Importo mensile in euro");
        row.append(nameField, primary.field);
        if (type === "debt") {
          const installment = makeAmountField("Rata mensile già in corso (€)", payment, "Rata mensile già in corso in euro");
          row.appendChild(installment.field);
        }
        const remove = document.createElement("button");
        remove.type = "button"; remove.className = "remove-button"; remove.textContent = "Rimuovi";
        remove.setAttribute("aria-label", "Rimuovi voce");
        remove.addEventListener("click", () => { row.remove(); calculate(); });
        row.appendChild(remove); container.appendChild(row);
        nameInput.addEventListener("input", calculate);
        calculate();
      }

      function sumRows(id, inputIndex=0) {
        let total = 0;
        document.querySelectorAll("#" + id + " .entry-row").forEach(row => {
          const input = row.querySelectorAll('input[type="number"]')[inputIndex];
          if (input) total += safeNumber(input);
        });
        return total;
      }
      function setTrafficLight(level, title, description) {
        const light = document.getElementById("trafficLight");
        light.classList.remove("red", "yellow", "green");
        if (level) light.classList.add(level);
        document.getElementById("trafficTitle").textContent = title;
        document.getElementById("trafficDescription").textContent = description;
      }
      function setText(id, value) { document.getElementById(id).textContent = value; }

      function calculate() {
        const income = sumRows("incomeRows");
        const expenses = sumRows("expenseRows");
        const debt = sumRows("debtRows", 0);
        const payments = sumRows("debtRows", 1);
        const lumpSum = safeNumber(document.getElementById("lumpSum"));
        const balance = income - expenses - payments;
        const margin = Math.max(0, balance - RESERVE);
        const quota = margin * QUOTA_FACTOR;
        const expenseRowsPresent = document.querySelectorAll("#expenseRows .entry-row").length > 0;
        const hasExpenseAmount = expenses > 0;
        const debtRowsPresent = document.querySelectorAll("#debtRows .entry-row").length > 0;

        setText("totalIncome", money(income));
        setText("totalExpenses", money(expenses));
        setText("totalPayments", money(payments));
        setText("totalDebt", money(debt));
        setText("residual", money(balance));
        setText("prudentialMargin", money(margin));
        setText("suggestedQuota", money(quota));
        setText("availableLumpSum", money(lumpSum));

        const assessment = document.getElementById("assessment");
        const dataQuality = document.getElementById("dataQuality");
        const nextStep = document.getElementById("nextStep");

        if (income <= 0) {
          setTrafficLight(null, "Valutazione sospesa — dati insufficienti", "Inserisci almeno un'entrata netta mensile positiva.");
          assessment.textContent = "Non è possibile stimare la sostenibilità senza un valore positivo delle entrate.";
          dataQuality.textContent = "Controllo completezza: manca un'entrata mensile positiva.";
          nextStep.textContent = "Completa le entrate e verifica che gli importi siano mensili e realistici.";
          renderPlans(debt, quota, false);
          renderSettlement(debt, lumpSum);
          return;
        }

        if (!expenseRowsPresent || !hasExpenseAmount) {
          setTrafficLight("yellow", "GIALLO — Verifica la completezza dei dati", "Non risultano spese mensili valorizzate. Prima di valutare una rata, verifica che tutte le spese essenziali siano state inserite.");
          assessment.textContent = "Il saldo provvisorio non è affidabile finché le spese non sono state inserite o confermate. Non interpretare un saldo elevato come denaro liberamente disponibile.";
          dataQuality.textContent = "Controllo completezza: inserisci le spese mensili oppure verifica che l'assenza di importi sia corretta.";
          nextStep.textContent = "Rivedi le spese essenziali, le rate già in corso e le eventuali uscite periodiche prima di considerare una proposta.";
          renderPlans(debt, quota, false);
          renderSettlement(debt, lumpSum);
          return;
        }

        dataQuality.textContent = "Controllo completezza: ricontrolla che le spese siano mensili, che le rate inserite non siano già comprese nelle spese e che il debito indicato sia aggiornato.";
        if (balance <= 0) {
          setTrafficLight("red", "ROSSO — Disavanzo mensile", "Le uscite e le rate in corso assorbono tutte le entrate o le superano. Non viene suggerita una nuova rata.");
          assessment.textContent = "Il bilancio presenta un disavanzo di " + money(Math.abs(balance)) + " al mese. La priorità è verificare le spese, le entrate e le urgenze; non è prudente ipotizzare un nuovo impegno mensile sulla base di questi dati.";
          nextStep.textContent = "È opportuno un approfondimento prima di formulare proposte ai creditori. Verifica anche eventuali spese straordinarie, entrate variabili e scadenze urgenti.";
        } else if (balance < 400 || quota < 100) {
          setTrafficLight("yellow", "GIALLO — Margine limitato", "Il saldo è positivo, ma la quota prudenziale è contenuta. Una rata va valutata con cautela.");
          assessment.textContent = "Dopo le spese e le rate già in corso restano " + money(balance) + " al mese. Applicando la riserva di " + money(RESERVE) + ", il margine prudenziale è " + money(margin) + " e la quota iniziale suggerita è " + money(quota) + ". Un importo inferiore a 100 € non viene presentato come rata automaticamente sostenibile.";
          nextStep.textContent = "Controlla la stabilità del reddito e le spese non mensili. Se il quadro resta incerto, richiedi un approfondimento prima di impegnarti.";
        } else {
          setTrafficLight("green", "VERDE — Margine potenzialmente compatibile", "I dati inseriti mostrano un margine compatibile con una possibile valutazione di rientro, senza garanzia di accettazione.");
          assessment.textContent = "Il saldo mensile è " + money(balance) + ". Sottraendo la riserva di " + money(RESERVE) + ", il margine prudenziale è " + money(margin) + "; il 50% di tale margine, pari a " + money(quota) + ", è la quota iniziale da confrontare con le rate teoriche. Non è una certificazione di solvibilità.";
          nextStep.textContent = "Confronta gli scenari di durata con la quota suggerita. Prima di inviare una proposta, verifica il saldo aggiornato del debito e tutte le condizioni del creditore.";
        }
        renderPlans(debt, quota, true);
        renderSettlement(debt, lumpSum);
      }

      function renderPlans(debt, quota, dataComplete) {
        const body = document.getElementById("planRows");
        const summary = document.getElementById("planSummary");
        if (debt <= 0) {
          body.innerHTML = '<tr><td colspan="3">Inserisci un debito residuo maggiore di zero per simulare un piano.</td></tr>';
          summary.textContent = "Senza un importo di debito non è possibile calcolare le rate teoriche.";
          return;
        }
        const durations = [6, 12, 24];
        body.replaceChildren();
        let anyFit = false;
        durations.forEach(months => {
          const installment = debt / months;
          const fit = dataComplete && quota >= 100 && installment <= quota;
          anyFit = anyFit || fit;
          const tr = document.createElement("tr");
          const tdMonths = document.createElement("td"); tdMonths.textContent = months + " mesi";
          const tdRate = document.createElement("td"); tdRate.textContent = money(installment);
          const tdStatus = document.createElement("td");
          if (!dataComplete) { tdStatus.textContent = "Completa prima i dati del bilancio"; tdStatus.className = "status-caution"; }
          else if (fit) { tdStatus.textContent = "Compatibile in via teorica"; tdStatus.className = "status-good"; }
          else if (quota < 100) { tdStatus.textContent = "Margine insufficiente per una proposta automatica"; tdStatus.className = "status-caution"; }
          else { tdStatus.textContent = "Supera la quota prudenziale"; tdStatus.className = "status-bad"; }
          tr.append(tdMonths, tdRate, tdStatus); body.appendChild(tr);
        });
        if (!dataComplete) summary.textContent = "Le rate sono soltanto matematiche: completa e verifica il bilancio prima di interpretare la compatibilità.";
        else if (anyFit) summary.textContent = "Almeno uno scenario rientra nella quota prudenziale calcolata. Restano da verificare interessi, costi, scadenze e accettazione del creditore.";
        else summary.textContent = "Nessuno degli scenari standard rientra nella quota prudenziale, oppure la quota è troppo contenuta per suggerire una rata. Non aumentare l'impegno per forzare il risultato.";
      }

      function renderSettlement(debt, lumpSum) {
        const amountEl = document.getElementById("settlementAmount");
        const summary = document.getElementById("settlementSummary");
        if (debt <= 0) {
          amountEl.textContent = "—";
          summary.textContent = "Inserisci il debito residuo complessivo per calcolare l'esempio al 60%.";
          return;
        }
        const offer = debt * 0.60;
        amountEl.textContent = money(offer);
        if (lumpSum <= 0) {
          summary.textContent = "Per un debito di " + money(debt) + ", il 60% corrisponde a " + money(offer) + ". Non è stata indicata una somma una tantum disponibile, quindi la fattibilità finanziaria non è verificata.";
        } else if (lumpSum >= offer) {
          summary.textContent = "La somma indicata copre matematicamente l'offerta ipotetica di " + money(offer) + ". Ciò non implica che il creditore accetti il 60%: occorre un accordo espresso e scritto che definisca gli effetti del pagamento.";
        } else {
          summary.textContent = "La somma indicata (" + money(lumpSum) + ") è inferiore all'offerta teorica di " + money(offer) + ". La differenza è " + money(offer - lumpSum) + ". Non è possibile concludere che il creditore accetterà una cifra inferiore.";
        }
      }

      const situationButtons = document.querySelectorAll(".situation-choice");
      const calculatorStart = document.getElementById("calculatorStart");
      const incomeRows = document.getElementById("incomeRows");
      const calculatorWelcome = document.getElementById("calculatorWelcome");
      const welcomeClose = document.getElementById("calculatorWelcomeClose");
      situationButtons.forEach(button => {
        button.addEventListener("pointerdown", () => {
          button.classList.add("is-pressed");
          window.setTimeout(() => button.classList.remove("is-pressed"), 180);
        });
        button.addEventListener("click", () => {
          situationButtons.forEach(item => {
            item.setAttribute("aria-pressed", item === button ? "true" : "false");
          });
          calculatorWelcome.hidden = false;
          calculatorStart.scrollIntoView({behavior:"smooth", block:"start"});
          window.setTimeout(() => {
            const firstInput = incomeRows.querySelector("input");
            if (firstInput) firstInput.focus({preventScroll:true});
          }, 450);
        });
      });
      if (welcomeClose) welcomeClose.addEventListener("click", () => { calculatorWelcome.hidden = true; });

      document.getElementById("addIncome").addEventListener("click", () => addRow("income", "Nuova entrata", 0));
      document.getElementById("addExpense").addEventListener("click", () => addRow("expense", "Nuova spesa", 0));
      document.getElementById("addDebt").addEventListener("click", () => addRow("debt", "Nuovo debito", 0, 0));
      document.getElementById("lumpSum").addEventListener("input", calculate);
      document.getElementById("resetCalculator").addEventListener("click", () => {
        document.getElementById("incomeRows").replaceChildren();
        document.getElementById("expenseRows").replaceChildren();
        document.getElementById("debtRows").replaceChildren();
        document.getElementById("lumpSum").value = "0";
        definitions.income.forEach(([label, value]) => addRow("income", label, value));
        definitions.expense.forEach(([label, value]) => addRow("expense", label, value));
        definitions.debt.forEach(([label, value, payment]) => addRow("debt", label, value, payment));
      });
      document.getElementById("resetCalculator").click();
    })();
    </script>
    """
    return page("Calcolatore di sostenibilità del debito", content)


# =========================================================
# RICONTATTO
# =========================================================

@app.route("/consulenza")
def consulenza():
    content = """
    <h1>Richiedi di essere contattato da un esperto FixTude</h1>
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

        <label class="check">
          <input type="checkbox" name="accept_terms" value="yes" required>
          <span>Accetto i <a href="/termini" target="_blank" rel="noopener">Termini e Condizioni</a>.</span>
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
    accept_terms = data.get("accept_terms")

    if not name or len(name) > 100:
        return jsonify(error="Inserisci un nome valido."), 400

    if not valid_email(email):
        return jsonify(error="Inserisci un indirizzo email valido."), 400

    if len(phone) > 40 or len(message) > 2000:
        return jsonify(error="Uno dei campi supera la lunghezza consentita."), 400

    if privacy not in (True, "on", "true", "1"):
        return jsonify(error="Per inviare la richiesta devi confermare di aver letto l'informativa."), 400

    if accept_terms not in (True, "yes", "on", "true", "1"):
        return jsonify(error="Per inviare la richiesta devi accettare i Termini e Condizioni."), 400

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

    stato = chat_orario_stato()
    return jsonify(ok=True, **stato)


@app.route("/api/chat/<chat_id>/messages", methods=["GET"])
def chat_messages(chat_id):
    if not re.fullmatch(r"[a-fA-F0-9-]{32,36}", chat_id):
        return jsonify(error="Sessione non valida."), 400

    with chat_lock:
        chat = chats.get(chat_id)
        if not chat:
            stato = chat_orario_stato()
            return jsonify(messages=[], **stato)

        chat["updated"] = time.time()
        messages = list(chat["messages"])

    stato = chat_orario_stato()
    return jsonify(messages=messages, **stato)


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
          <h1>Chat riservata all’esperto FixTude</h1>
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
          <p class="muted">Area riservata all’esperto FixTude.</p>
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
    <h1>Area riservata · Chat esperto FixTude</h1>
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

# I quattro moduli gratuiti sono direttamente nella cartella static/.
MODULE_DIR = BASE_DIR / "static"

# Il PDF completo a pagamento deve restare fuori da static/, che Flask rende pubblico.
# Prima del deploy, collocare il file in private/NIENTE_PANICO.pdf.
PRIVATE_DIR = BASE_DIR / "private"
BOOK_FILE = PRIVATE_DIR / "NIENTE_PANICO.pdf"

# L'anteprima gratuita del primo capitolo è separata dal libro completo.
# Il file deve essere presente in static/pdf/NIENTE_PANICO_ANTEPRIMA.pdf.
PDF_DIR = BASE_DIR / "static" / "pdf"
PREVIEW_FILE = PDF_DIR / "NIENTE_PANICO_ANTEPRIMA.pdf"

MODULE_FILES = {
    "saldo-stralcio": (
        "FixTude_Modulo_Saldo_e_Stralcio_2_pagine.pdf",
        "Proposta di saldo e stralcio",
    ),
    "dilazione": (
        "FixTude_Modulo_Dilazione_Pagamento_2_pagine.pdf",
        "Richiesta di dilazione del pagamento",
    ),
    "piano-rientro": (
        "FixTude_Modulo_Piano_Rientro_con_Titoli_2_pagine.pdf",
        "Richiesta di piano di rientro",
    ),
    "raccolta": (
        "FixTude_Libreria_Moduli_Richieste_Debitorie_6_pagine_v2.pdf",
        "Raccolta completa di moduli di richiesta",
    ),
}


@app.route("/moduli")
def moduli_gratuiti():
    cards = []

    for slug, (filename, label) in MODULE_FILES.items():
        cards.append(f'''
        <section class="card">
            <h2>{escape(label)}</h2>
            <p class="muted">
                Documento PDF gratuito, consultabile e scaricabile
                senza registrazione.
            </p>
            <div class="actions">
                <a class="btn outline"
                   href="/static/{filename}"
                   target="_blank"
                   rel="noopener">
                    Visualizza PDF
                </a>
                <a class="btn" href="/download/modulo/{slug}">
                    Scarica PDF
                </a>
            </div>
        </section>
        ''')

    content = '''
    <h1>Moduli FixTude gratuiti</h1>
    <p class="muted">
        Consulta e scarica gratuitamente i modelli per preparare
        richieste ai creditori. Adatta ogni documento alla tua
        situazione, controlla gli importi e allega soltanto
        documenti pertinenti. L'invio di una richiesta non garantisce
        che il creditore accetti la proposta.
    </p>
    <div class="grid">
    ''' + "".join(cards) + '''
    </div>
    <div class="notice">
        Tutti e quattro i documenti sono gratuiti e non richiedono
        registrazione o pagamento.
    </div>
    '''

    return page("Moduli gratuiti", content)


@app.route("/download/modulo/<slug>")
def download_modulo(slug):
    item = MODULE_FILES.get(slug)

    if not item:
        return "Modulo non trovato", 404

    filename = item[0]
    path = MODULE_DIR / filename

    if not path.is_file():
        app.logger.error("PDF modulo non trovato: %s", path)
        return page(
            "Modulo non disponibile",
            '''
            <h1>Modulo temporaneamente non disponibile</h1>
            <p>
                Il documento non è al momento disponibile.
                Riprova più tardi.
            </p>
            <a class="btn" href="/moduli">
                Torna ai moduli gratuiti
            </a>
            ''',
        ), 404

    return send_file(
        path,
        as_attachment=True,
        download_name=filename,
        mimetype="application/pdf",
    )


@app.route("/educazione-finanziaria")
def educazione_finanziaria():
    content = '''<h1>Educazione finanziaria</h1>
    <p class="muted">Guide introduttive gratuite per comprendere meglio il bilancio personale, organizzare le spese e affrontare con maggiore consapevolezza gli impegni economici.</p>
    <section class="card" aria-labelledby="guide-stipendio">
      <p class="muted">GUIDA PRATICA · 7 MINUTI DI LETTURA</p>
      <h2 id="guide-stipendio"><a href="/educazione-finanziaria/come-gestire-lo-stipendio-regola-un-terzo">Come gestire le entrate: la regola 1/3, 1/3, 1/3 per single e famiglie</a></h2>
      <p>Un metodo semplice per osservare come si distribuisce il reddito tra spese essenziali, vita quotidiana e obiettivi finanziari. Con esempi numerici e indicazioni su come adattare le proporzioni alla propria situazione.</p>
      <a class="btn" href="/educazione-finanziaria/come-gestire-lo-stipendio-regola-un-terzo">Leggi la guida</a>
    </section>
    <section class="card" id="bilancio"><h2>Organizzare il bilancio familiare</h2><p>Annota le entrate nette effettivamente disponibili e separa le spese essenziali (casa, utenze, alimentazione, salute e trasporti) dalle spese flessibili. Inserisci poi rate, arretrati e scadenze. Il margine mensile è una stima: lascia un cuscinetto per gli imprevisti e non considerare sostenibile una rata che mette a rischio le necessità di base.</p><ul><li>Usa importi mensili omogenei.</li><li>Evita di contare entrate incerte come se fossero garantite.</li><li>Aggiorna il prospetto quando cambiano reddito o spese.</li></ul><p><a href="/misuratore">Prova il calcolatore gratuito di sostenibilità del debito</a>.</p></section>
    <section class="card" id="priorita"><h2>Dare priorità ai pagamenti</h2><p>Prima di distribuire il denaro tra i debiti, proteggi le necessità essenziali e valuta le scadenze concrete. Non esiste una priorità universale valida per ogni situazione: atti giudiziari, garanzie, utenze e debiti fiscali possono avere conseguenze differenti. Se hai ricevuto un atto con termini, chiedi tempestivamente una valutazione qualificata.</p><ul><li>Apri e conserva le comunicazioni ricevute.</li><li>Segna importo, creditore, scadenza e riferimento della pratica.</li><li>Non ignorare raccomandate, PEC o atti notificati.</li></ul></section>
    <section class="card" id="creditori"><h2>Parlare con i creditori</h2><p>Una richiesta efficace è chiara, documentata e realistica. Indica il riferimento del rapporto, spiega brevemente la difficoltà, proponi un importo compatibile con il bilancio e chiedi conferma scritta delle condizioni. Non promettere pagamenti che non puoi sostenere e non considerare accettata una proposta finché non hai ricevuto una conferma verificabile.</p><p>Puoi partire dai <a href="/moduli">moduli gratuiti FixTude</a> e personalizzarli.</p></section>
    <section class="card" id="errori"><h2>Errori da evitare</h2><ul><li>Pagare intermediari che promettono cancellazioni o risultati garantiti.</li><li>Firmare accordi senza aver letto importi, scadenze e conseguenze.</li><li>Inviare documenti personali a destinatari non verificati.</li><li>Contrarre nuovo debito senza valutare il costo totale e la sostenibilità.</li><li>Rimandare la richiesta di aiuto quando arrivano atti con termini precisi.</li></ul><p>Le informazioni di questa pagina sono generali e non sostituiscono una consulenza professionale.</p></section>'''
    return page("Educazione finanziaria", content)


@app.route("/educazione-finanziaria/come-gestire-lo-stipendio-regola-un-terzo")
def articolo_gestione_stipendio():
    content = '''<article class="card article-content">
      <p class="muted">EDUCAZIONE FINANZIARIA · 7 MINUTI DI LETTURA</p>
      <h1>Come gestire le entrate: la regola 1/3, 1/3, 1/3 per single e famiglie</h1>
      <figure class="article-hero-image"><img src="/static/articolo-entrate-budget.png" alt="Organizzare le entrate e il bilancio personale con la regola dei tre terzi: necessità, vita quotidiana e risparmio" loading="eager" fetchpriority="high"><figcaption>Un metodo orientativo per distribuire le entrate tra necessità, vita quotidiana e obiettivi.</figcaption></figure>
      <p class="article-subtitle">Non conta soltanto quanto guadagni, ma come organizzi le risorse che hai a disposizione: stipendio, lavoro autonomo, pensione o entrate variabili.</p>
      <p class="muted">Pubblicato il 17 ottobre 2026 · Aggiornato il 17 ottobre 2026</p>
      <p>Arrivare a fine mese senza sapere dove siano finiti i soldi è una situazione comune. Bollette, spesa, trasporti, impegni familiari e rate possono assorbire il reddito poco alla volta. Un bilancio semplice aiuta a vedere con chiarezza quanto entra, quanto esce e quale margine rimane.</p>
      <p>La regola <strong>1/3, 1/3, 1/3</strong> è un punto di partenza per organizzare le entrate nette disponibili: divide idealmente il reddito in tre parti. Non è una legge, non è una regola valida per tutti e non garantisce che ogni bilancio possa rispettarla. Serve come riferimento da adattare alle condizioni reali della persona o della famiglia.</p>
      <h2>La regola dei tre terzi: come funziona</h2>
      <div class="budget-grid">
        <div class="budget-item"><strong>1/3 · Necessità</strong><p>Casa, utenze, alimentari, trasporti, salute e altre spese essenziali.</p></div>
        <div class="budget-item"><strong>1/3 · Vita quotidiana</strong><p>Abbigliamento, attività personali, tempo libero e spese flessibili, tenendo conto delle esigenze familiari.</p></div>
        <div class="budget-item"><strong>1/3 · Futuro e impegni</strong><p>Riserva per imprevisti, risparmio, obiettivi e gestione delle rate o degli arretrati, secondo le priorità effettive.</p></div>
      </div>
      <p>Le categorie sono indicative. Per esempio, una famiglia con un mutuo elevato potrebbe spendere ben più di un terzo per le necessità; chi ha debiti arretrati potrebbe dover destinare una parte maggiore alla gestione degli impegni, senza compromettere cibo, casa, salute e altre necessità fondamentali.</p>
      <h2>Un esempio con 1.800 euro netti al mese</h2>
      <p>Applicando la divisione in tre parti a un reddito netto mensile di 1.800 euro, si ottengono tre quote teoriche da 600 euro:</p>
      <ul><li><strong>600 euro per le necessità:</strong> contributo alle spese di casa, utenze, alimentari, trasporti e salute.</li><li><strong>600 euro per la vita quotidiana:</strong> spese personali e flessibili, da controllare in base alle esigenze reali.</li><li><strong>600 euro per futuro e impegni:</strong> risparmio, fondo imprevisti e gestione dei debiti, quando presenti.</li></ul>
      <p>È un esempio matematico, non un budget universale: gli importi reali possono rendere necessaria una distribuzione molto diversa.</p>
      <h2>Esempio pratico per una persona single con 1.500 euro</h2>
      <p>Immaginiamo una persona che riceva 1.500 euro netti e abbia queste uscite mensili:</p>
      <div class="table-wrap"><table class="result-table"><thead><tr><th>Voce</th><th>Importo mensile</th></tr></thead><tbody><tr><td>Casa e spese domestiche</td><td>700 €</td></tr><tr><td>Alimentari e trasporti</td><td>300 €</td></tr><tr><td>Spese personali e tempo libero</td><td>200 €</td></tr><tr><td>Risparmio e imprevisti</td><td>100 €</td></tr><tr><td>Rate e altri impegni</td><td>200 €</td></tr><tr><th>Totale</th><th>1.500 €</th></tr></tbody></table></div>
      <p>In questo caso, le necessità essenziali superano il primo terzo. Non significa che la persona stia gestendo male il denaro: può dipendere dal costo dell'alloggio, dalla zona in cui vive o da altre condizioni. La priorità è capire se le uscite sono sostenibili, quali voci possono essere riviste e se resta un margine realistico per gli imprevisti.</p>
      <h2>Come adattare la regola a una famiglia</h2>
      <p>In una famiglia è utile partire dal reddito netto complessivo e includere tutte le spese comuni: abitazione, bollette, spesa alimentare, trasporti, scuola, salute, cura dei figli e assistenza, quando presenti. Le spese annuali prevedibili — come assicurazioni, manutenzioni o tasse — possono essere trasformate in una quota mensile, così da non sorprendere il bilancio quando arrivano.</p>
      <p>Se le spese essenziali assorbono già gran parte delle entrate, non forzare la divisione in tre parti. Registra le uscite, distingui quelle inevitabili da quelle modificabili e verifica se esistono costi ricorrenti da rinegoziare o ridurre. Se il bilancio è in deficit, occorre affrontare il disavanzo prima di fissare obiettivi di risparmio irrealistici.</p>
      <h2>Come iniziare, passo dopo passo</h2>
      <ol><li><strong>Calcola le entrate nette:</strong> considera il denaro effettivamente disponibile e non includere entrate occasionali o incerte come se fossero garantite.</li><li><strong>Registra le spese per due o tre mesi:</strong> usa estratti conto, ricevute e bollette per evitare stime troppo ottimistiche.</li><li><strong>Separa le categorie:</strong> necessità, spese flessibili, rate e altri impegni, riserva e obiettivi.</li><li><strong>Controlla le scadenze:</strong> annota importi, date e conseguenze dei pagamenti mancati.</li><li><strong>Rivedi il budget ogni mese:</strong> aggiorna le cifre quando cambiano reddito, casa, famiglia o altre condizioni.</li></ol>
      <h2>Se ci sono debiti, quale margine resta davvero?</h2>
      <p>Per una prima fotografia, puoi calcolare il margine mensile sottraendo alle entrate nette tutte le spese effettive, comprese le rate già in corso:</p>
      <p class="formula-box"><strong>Margine mensile = entrate nette − spese complessive</strong></p>
      <p>Il risultato va letto con prudenza: spese irregolari, imprevisti e variazioni di reddito possono ridurlo. Non tutto il margine apparente dovrebbe essere impegnato in una nuova rata. È opportuno proteggere prima le necessità essenziali e mantenere una riserva compatibile con la propria situazione.</p>
      <p>Puoi fare una prima simulazione con il <a href="/misuratore">calcolatore gratuito di sostenibilità del debito FixTude</a>. I dati inseriti nel calcolatore vengono elaborati nel browser e lo strumento offre una valutazione orientativa, non una certificazione di solvibilità né una garanzia di accordo con i creditori.</p>
      <h2>Gli errori più comuni</h2>
      <ul><li>Usare percentuali rigide senza considerare il costo reale della casa e delle necessità.</li><li>Dimenticare spese annuali o irregolari.</li><li>Confondere il saldo temporaneo del conto con una disponibilità mensile stabile.</li><li>Impegnare tutto il margine in rate, senza lasciare spazio agli imprevisti.</li><li>Ricorrere a nuovo credito per coprire un deficit senza valutarne costo e sostenibilità.</li></ul>
      <h2>In sintesi</h2>
      <p>La regola 1/3, 1/3, 1/3 può aiutare a iniziare una conversazione sul proprio bilancio, ma non è un obiettivo da raggiungere a ogni costo. Il metodo più utile è quello che rispecchia le entrate, le spese e gli impegni reali, mette al sicuro le necessità fondamentali e viene aggiornato con regolarità.</p>
      <section class="notice"><strong>Nota informativa</strong><br>Questa guida ha finalità generali e informative. Non costituisce consulenza finanziaria, fiscale o legale personalizzata. Le scelte relative a debiti e pagamenti dipendono dalla situazione concreta e dagli eventuali atti o accordi applicabili.</section>
      <div class="actions"><a class="btn" href="/misuratore">Prova il calcolatore gratuito</a><a class="btn outline" href="/educazione-finanziaria">Tutte le guide di educazione finanziaria</a></div>
    </article>
    <style>
      .article-content h1{margin-top:0}.article-content h2{margin-top:30px}
      .article-hero-image{margin:18px 0 20px}.article-hero-image img{display:block;width:100%;height:auto;max-height:520px;object-fit:cover;border-radius:14px}.article-hero-image figcaption{font-size:13px;color:#5b6573;margin-top:7px}.article-subtitle{font-size:1.08rem;line-height:1.65;color:#34445b;margin:0 0 18px}
      .budget-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:20px 0}
      .budget-item{background:#f4f8fd;border:1px solid #dce5ef;border-radius:12px;padding:16px;min-width:0}
      .budget-item p{margin-bottom:0}.formula-box{padding:16px;background:#eef5ff;border-left:4px solid var(--blue);border-radius:8px}
      .result-table{width:100%;border-collapse:collapse;margin:12px 0;font-size:14px}.result-table th,.result-table td{padding:11px;text-align:left;border-bottom:1px solid var(--line)}.result-table th{background:#f4f7fb}
      .table-wrap{overflow-x:auto}
      @media(max-width:650px){.budget-grid{grid-template-columns:1fr}.article-content{padding:18px}}
    </style>'''
    return page("Come gestire le entrate", content)

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

# =========================================================
# ASSISTENZA LEGALE - PACCHETTI LANCIO E CHECKOUT STRIPE
# =========================================================

ASSISTENZA_LEGALE_PACCHETTI = {
    "debito_5000": {
        "name": "Assistenza Legale FixTude - debiti fino a 5.000 euro",
        "short": "Debiti fino a € 5.000",
        "amount": 49900,
        "display": "499",
    },
    "debito_10000": {
        "name": "Assistenza Legale FixTude - debiti da 5.000 a 10.000 euro",
        "short": "Debiti da € 5.000 a € 10.000",
        "amount": 89900,
        "display": "899",
    },
    "debito_30000": {
        "name": "Assistenza Legale FixTude - debiti da 10.000 a 30.000 euro",
        "short": "Debiti da € 10.000 a € 30.000",
        "amount": 199000,
        "display": "1.990",
    },
    "debito_60000": {
        "name": "Assistenza Legale FixTude - debiti da 30.000 a 60.000 euro",
        "short": "Debiti da € 30.000 a € 60.000",
        "amount": 399000,
        "display": "3.990",
    },
}


@app.route("/assistenza-legale")
def assistenza_legale():
    cards = []
    package_details = {
        "debito_5000": [
            "Analisi della posizione debitoria e della documentazione ricevuta.",
            "Valutazione delle opzioni di rientro, negoziazione e saldo e stralcio.",
            "Orientamento al legale del pool più competente per materia e pratica.",
            "Compenso dell’avvocato incluso per le attività stragiudiziali e giudiziali espressamente comprese nel mandato scritto, con fasi definite per la pratica.",
        ],
        "debito_10000": [
            "Tutte le attività del pacchetto precedente, calibrate sulla pratica.",
            "Esame degli atti ricevuti e individuazione delle priorità e delle scadenze.",
            "Gestione delle trattative con creditori e società di recupero tramite il professionista incaricato.",
            "Compenso dell’avvocato incluso per l’assistenza giudiziale espressamente prevista nel mandato professionale concordato.",
        ],
        "debito_30000": [
            "Analisi coordinata delle posizioni debitorie comprese nell'incarico.",
            "Piano personalizzato di intervento e valutazione delle alternative negoziali.",
            "Compenso dell’avvocato incluso per l’assistenza stragiudiziale e giudiziale espressamente prevista nel mandato scritto.",
            "Valutazione di atti monitori ed esecutivi e delle iniziative difensive pertinenti al caso.",
        ],
        "debito_60000": [
            "Analisi approfondita delle posizioni incluse nell'incarico.",
            "Strategia coordinata di gestione e negoziazione del debito.",
            "Orientamento al legale del pool con competenza specifica, anche per questioni esecutive.",
            "Compenso dell’avvocato incluso per le attività stragiudiziali e giudiziali e le fasi espressamente incluse nel mandato.",
        ],
    }
    for key, pkg in ASSISTENZA_LEGALE_PACCHETTI.items():
        items = "".join("<li>" + escape(item) + "</li>" for item in package_details[key])
        cards.append(f'''<section class="card legal-package"><div class="package-kicker">PREZZO DI LANCIO</div><h2>{escape(pkg["short"])}</h2><div class="price">€ {pkg["display"]}</div><ul>{items}</ul><form method="post" action="/assistenza-legale/acquista"><input type="hidden" name="package" value="{key}"><label class="check"><input type="checkbox" name="accept_terms" value="yes" required><span>Accetto i <a href="/termini" target="_blank" rel="noopener">Termini e Condizioni</a> e dichiaro di aver letto l'<a href="/privacy" target="_blank" rel="noopener">Informativa Privacy</a>. Ho compreso che il pagamento avvia la richiesta di attivazione e che l'incarico professionale, le attività incluse e le eventuali spese vive saranno formalizzati per iscritto.</span></label><button class="btn" type="submit">Acquista e vai al checkout</button></form></section>''')
    content = '''<section class="hero"><h1>Assistenza Legale FixTude</h1><p>Un percorso strutturato per affrontare le situazioni debitorie, con orientamento al professionista competente del nostro pool di avvocati e prezzi di lancio trasparenti.</p><p><strong>Quattro pacchetti, un percorso personalizzato e un checkout Stripe immediato.</strong></p><p class="muted">Al checkout saranno mostrati i metodi di pagamento disponibili per la transazione, comprese eventuali opzioni rateali se abilitate e compatibili.</p></section>
    <section class="card"><h2>Cosa può comprendere il servizio</h2><ul><li>Analisi della situazione debitoria e della documentazione.</li><li>Valutazione di strategie di rientro, negoziazione e saldo e stralcio.</li><li>Esame di solleciti, decreti ingiuntivi, precetti e atti esecutivi, quando pertinenti.</li><li>Orientamento al legale del pool con competenza specifica.</li><li>Assistenza stragiudiziale e giudiziale, comprese opposizioni o procedure pertinenti, soltanto secondo l'incarico professionale sottoscritto.</li><li>Definizione delle attività, delle fasi, dei compensi e delle spese incluse prima dell'avvio dell'attività legale.</li></ul></section>
    <div class="notice"><strong>Prezzi di lancio.</strong> Gli importi promozionali sono quelli indicati per ciascuna fascia di indebitamento. La fascia è un criterio iniziale di orientamento, non una valutazione automatica della complessità. Prima dell'avvio dell'attività, il professionista incaricato confermerà per iscritto il perimetro dell'incarico. Il pacchetto non può essere inteso come copertura illimitata di qualsiasi causa, di ogni grado di giudizio o di procedimenti non identificati al momento dell'incarico. Il compenso dell’avvocato per le attività e le fasi espressamente incluse nel mandato è compreso nel prezzo di lancio: non sono previste ulteriori parcelle per quelle attività. Le spese vive e gli esborsi di terzi (ad esempio contributo unificato, notifiche, consulenti tecnici o altri ausiliari) sono esclusi salvo diversa indicazione scritta. Attività ulteriori o nuovi procedimenti non compresi nel mandato richiedono un preventivo scritto e l’accettazione del cliente prima di essere avviati.</div>
    <p class="muted">Il pagamento non garantisce accordi con i creditori, riduzioni del debito o un esito giudiziario favorevole. L'attività riservata è svolta dall'avvocato incaricato, che definisce e sottoscrive il mandato professionale. Se dopo la valutazione la pratica non rientra nel perimetro del pacchetto, saranno chiarite le opzioni disponibili prima di avviare attività ulteriori.</p>
    <div class="legal-packages-grid">''' + "".join(cards) + '''</div>
    <style>.legal-packages-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;margin-top:20px}.legal-package{margin:0;display:flex;flex-direction:column}.legal-package h2{margin:5px 0}.package-kicker{font-size:12px;letter-spacing:.09em;font-weight:800;color:#16794b}.legal-package .price{font-size:32px;margin:4px 0 12px}.legal-package ul{padding-left:20px;flex:1}.legal-package li{margin:0 0 8px}.legal-package form{margin-top:12px}.legal-package .btn{width:100%}.notice{padding:16px;border:1px solid #efd58b;border-radius:12px;background:#fff7df;margin:20px 0}@media(max-width:700px){.legal-packages-grid{grid-template-columns:1fr}}</style>'''
    return page("Assistenza Legale", content)


@app.route("/assistenza-legale/acquista", methods=["POST"])
def assistenza_legale_acquista():
    package_key = request.form.get("package", "").strip()
    pkg = ASSISTENZA_LEGALE_PACCHETTI.get(package_key)
    if not pkg:
        return page("Pacchetto non valido", '<h1>Pacchetto non valido</h1><p>Torna alla pagina Assistenza Legale e seleziona un pacchetto disponibile.</p><a class="btn" href="/assistenza-legale">Torna ai pacchetti</a>'), 400
    if request.form.get("accept_terms") != "yes":
        return page("Conferma necessaria", '<h1>Prima di proseguire</h1><p>Devi accettare i Termini e Condizioni per continuare.</p><a class="btn" href="/assistenza-legale">Torna ai pacchetti</a>'), 400
    secret_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if stripe is None or not secret_key:
        return page("Checkout temporaneamente non disponibile", '<h1>Pagamento non disponibile</h1><p>Il checkout non è configurato al momento. Non è stato effettuato alcun addebito.</p><a class="btn" href="/assistenza-legale">Torna ai pacchetti</a>'), 503
    stripe.api_key = secret_key
    try:
        # Non impostare payment_method_types: Stripe propone i metodi abilitati
        # nella Dashboard e compatibili con il pagamento, inclusi Klarna/Scalapay
        # quando disponibili per quella transazione e per il cliente.
        checkout = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price_data": {"currency": "eur", "unit_amount": pkg["amount"], "product_data": {"name": pkg["name"], "description": "Pacchetto promozionale FixTude: compenso dell’avvocato incluso per le attività e le fasi espressamente comprese nel mandato scritto; spese vive di terzi escluse salvo accordo scritto."}}, "quantity": 1}],
            success_url=request.host_url.rstrip("/") + "/assistenza-legale/conferma?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=request.host_url.rstrip("/") + "/assistenza-legale?annullato=1",
            billing_address_collection="auto",
            metadata={"fixtude_product": "assistenza_legale", "fixtude_package": package_key, "terms_accepted": "yes", "terms_version": "2026-10-18"},
        )
        return redirect(checkout.url, code=303)
    except Exception:
        app.logger.exception("Creazione checkout Stripe assistenza legale non riuscita")
        return page("Checkout non disponibile", '<h1>Non è stato possibile avviare il pagamento</h1><p>Nessun pagamento è stato confermato. Riprova più tardi.</p><a class="btn" href="/assistenza-legale">Torna ai pacchetti</a>'), 502


@app.route("/assistenza-legale/conferma")
def assistenza_legale_conferma():
    session_id = request.args.get("session_id", "").strip()
    secret_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not session_id or stripe is None or not secret_key:
        return page("Verifica pagamento", '<h1>Non possiamo verificare il pagamento</h1><p>Contatta info@fixtude.it per assistenza.</p>'), 400
    try:
        stripe.api_key = secret_key
        checkout = stripe.checkout.Session.retrieve(session_id)
        data = checkout.to_dict()
        metadata = data.get("metadata") or {}
        paid = data.get("payment_status") == "paid"
        package_key = metadata.get("fixtude_package", "")
        pkg = ASSISTENZA_LEGALE_PACCHETTI.get(package_key)
        if not paid or metadata.get("fixtude_product") != "assistenza_legale" or not pkg:
            return page("Pagamento da verificare", '<h1>Pagamento non ancora confermato</h1><p>La pratica sarà presa in carico dopo la verifica del pagamento.</p><a class="btn" href="/assistenza-legale">Torna all’assistenza legale</a>'), 402
        content = f'''<h1>Pagamento confermato</h1><section class="card"><p>Grazie. Il pagamento del pacchetto <strong>{escape(pkg["short"])}</strong> risulta confermato.</p><p>Il prossimo passaggio è la verifica della documentazione e l'orientamento al professionista del pool più competente. Il mandato legale e il perimetro delle attività saranno formalizzati per iscritto.</p><p>Per avviare il contatto, scrivi a <a href="mailto:info@fixtude.it">info@fixtude.it</a> indicando il riferimento di sessione <code>{escape(session_id)}</code>. Non inviare dati bancari o documenti particolarmente sensibili via email non protetta.</p><a class="btn" href="/consulenza">Completa la richiesta di contatto</a></section>'''
        return page("Pagamento confermato", content)
    except Exception:
        app.logger.exception("Verifica checkout Stripe assistenza legale non riuscita")
        return page("Verifica non riuscita", '<h1>Non è stato possibile verificare il pagamento</h1><p>Riprova più tardi o scrivi a info@fixtude.it.</p>'), 502


@app.route("/shop")
def shop():
    content = '''<h1>Shop FixTude</h1>
    <section class="card product-card">
      <div class="shop-book-cover"><img src="/static/niente_panico_cover.png" alt="Copertina del libro Niente Panico di FixTude"></div>
      <div class="shop-book-description"><h2>NIENTE PANICO</h2><p>Una guida pratica per mettere ordine nella situazione debitoria, leggere meglio le proprie possibilità e prepararsi ad affrontare i creditori con maggiore lucidità.</p><p><strong>Formato:</strong> libro digitale PDF.</p><p class="muted">Prima di acquistare, puoi leggere gratuitamente il primo capitolo. Il libro completo sarà disponibile dopo la conferma del pagamento.</p><p><a class="btn outline" href="/shop/anteprima" target="_blank" rel="noopener">Leggi il primo capitolo gratuitamente</a></p><p class="muted">Dopo la conferma del pagamento potrai scaricare il PDF completo dalla pagina di conferma. Non è necessaria la registrazione.</p><div class="price">€ 6,99</div><form method="post" action="/shop/acquista"><label class="check"><input type="checkbox" name="accept_terms" value="yes" required><span>Accetto i <a href="/termini" target="_blank" rel="noopener">Termini e Condizioni</a> e dichiaro di aver letto l'<a href="/privacy" target="_blank" rel="noopener">Informativa Privacy</a>.</span></label><button class="btn" type="submit">Acquista il libro digitale</button></form></div>
    </section>
    <section class="card product-card print-coming-soon" aria-labelledby="print-book-title">
      <div class="shop-book-cover"><img src="/static/niente_panico_cover.png" alt="Copertina prevista per l'edizione cartacea di Niente Panico"></div>
      <div class="shop-book-description">
        <div class="coming-soon-label">COMING SOON</div>
        <h2 id="print-book-title">NIENTE PANICO — Edizione cartacea</h2>
        <p>Il libro in formato cartaceo sarà disponibile prossimamente, con stampa su richiesta.</p>
        <p class="muted">Prezzo e spedizione saranno comunicati prima dell'apertura degli ordini.</p>
        <div class="quantity-selector" aria-label="Selettore quantità non ancora attivo">
          <span>Numero di copie</span>
          <div class="quantity-controls">
            <button type="button" class="btn outline" disabled aria-label="Diminuisci numero di copie">−</button>
            <output aria-live="polite">1</output>
            <button type="button" class="btn outline" disabled aria-label="Aumenta numero di copie">+</button>
          </div>
        </div>
        <button type="button" class="btn coming-soon-button" disabled aria-disabled="true">COMING SOON</button>
      </div>
    </section>
    <p class="muted">I moduli di richiesta debitoria restano gratuiti e sono disponibili nella sezione <a href="/moduli">Moduli gratuiti</a>.</p>
    <style>
      .coming-soon-label{display:inline-block;background:#e8eef7;color:#23436c;border-radius:999px;padding:6px 12px;font-size:12px;font-weight:800;letter-spacing:.08em;margin-bottom:8px}
      .quantity-selector{margin:16px 0;display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap}
      .quantity-controls{display:flex;align-items:center;gap:12px}
      .quantity-controls output{display:inline-flex;min-width:38px;min-height:38px;align-items:center;justify-content:center;font-weight:800;border:1px solid #dce5ef;border-radius:8px}
      .quantity-controls button:disabled,.coming-soon-button:disabled{opacity:.65;cursor:not-allowed;filter:saturate(.6)}
      .coming-soon-button{width:100%;max-width:260px}
      @media(max-width:640px){.quantity-selector{align-items:flex-start;flex-direction:column}.coming-soon-button{max-width:none}}
    </style>'''
    return page("Shop", content)

@app.route("/shop/anteprima")
def shop_anteprima():
    """Mostra solo il primo capitolo gratuito, senza esporre il PDF completo."""
    if not PREVIEW_FILE.is_file():
        return page("Anteprima non disponibile", '<h1>Anteprima temporaneamente non disponibile</h1><p>Il primo capitolo gratuito non è al momento disponibile. Riprova più tardi.</p><a class="btn" href="/shop">Torna allo Shop</a>'), 404
    response = send_file(
        PREVIEW_FILE,
        mimetype="application/pdf",
        as_attachment=False,
        download_name="FixTude_NIENTE_PANICO_Anteprima.pdf",
        conditional=True,
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Disposition"] = 'inline; filename="FixTude_NIENTE_PANICO_Anteprima.pdf"'
    return response


@app.route("/shop/acquista", methods=["POST"])
def shop_acquista():
    if request.form.get("accept_terms") != "yes":
        return page("Conferma necessaria", '<h1>Prima di proseguire</h1><p>Devi accettare i Termini e Condizioni per continuare con l’acquisto.</p><a class="btn" href="/shop">Torna allo shop</a>'), 400
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
            metadata={"fixtude_product": "niente_panico_pdf", "terms_accepted": "yes", "terms_version": "2026-10-12"},
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
        # Converti l'oggetto Stripe in un dizionario Python prima di usare .get().
        checkout_data = checkout.to_dict()
        paid = checkout_data.get("payment_status") == "paid"
        product = (checkout_data.get("metadata") or {}).get("fixtude_product") == "niente_panico_pdf"
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
        # Converti l'oggetto Stripe in un dizionario Python prima di usare .get().
        checkout_data = checkout.to_dict()
        paid = checkout_data.get("payment_status") == "paid"
        product = (checkout_data.get("metadata") or {}).get("fixtude_product") == "niente_panico_pdf"
        if not paid or not product or not BOOK_FILE.is_file():
            return "Accesso non autorizzato o file non disponibile", 403
        return send_file(BOOK_FILE, as_attachment=True, download_name="FixTude_NIENTE_PANICO.pdf", mimetype="application/pdf")
    except Exception:
        app.logger.exception("Download libro non riuscito")
        return "Non è stato possibile verificare l'acquisto", 502


# =========================================================
# DOCUMENTI LEGALI - IDENTIFICAZIONE DEL TITOLARE E INFORMATIVE
# =========================================================

LEGAL_VERSION = "2026-10-16"
LEGAL_OWNER_NAME = os.environ.get("FIXTUDE_TITOLARE_NOME", "Giovanni Pugliese").strip()
LEGAL_OWNER_ADDRESS = os.environ.get("FIXTUDE_TITOLARE_INDIRIZZO", "Via Saronno 65, 00188 Roma, Italia").strip()
LEGAL_VAT_NUMBER = os.environ.get("FIXTUDE_PARTITA_IVA", "15990471003").strip()
LEGAL_EMAIL = "info@fixtude.it"


def legal_page(title, body):
    path = request.path or "/"
    seo = SEO_PAGES.get(path, {})
    seo_title = seo.get("title", f"{title} | FixTude")
    seo_description = seo.get("description", SEO_DEFAULT_DESCRIPTION)
    canonical_url = SEO_BASE_URL + ("" if path == "/" else path)
    owner_line = f"{escape(LEGAL_OWNER_NAME)} · P. IVA {escape(LEGAL_VAT_NUMBER)} · {escape(LEGAL_OWNER_ADDRESS)} · {LEGAL_EMAIL}"
    return Response(f"""<!doctype html><html lang="it-IT"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#123a72"><meta name="description" content="{escape(seo_description, quote=True)}"><meta name="robots" content="index, follow, max-image-preview:large"><link rel="canonical" href="{escape(canonical_url, quote=True)}"><link rel="icon" type="image/png" sizes="16x16" href="/static/fixtude-16.png"><link rel="icon" type="image/png" sizes="48x48" href="/static/fixtude-48.png"><link rel="apple-touch-icon" sizes="180x180" href="/static/fixtude-180.png"><link rel="icon" type="image/png" sizes="512x512" href="/static/fixtude-512.png"><meta property="og:locale" content="it_IT"><meta property="og:type" content="website"><meta property="og:site_name" content="FixTude"><meta property="og:title" content="{escape(seo_title, quote=True)}"><meta property="og:description" content="{escape(seo_description, quote=True)}"><meta property="og:url" content="{escape(canonical_url, quote=True)}"><meta property="og:image" content="{SEO_BASE_URL}/static/fixtude-512.png"><meta property="og:image:alt" content="Logo FixTude"><meta name="twitter:card" content="summary_large_image"><meta name="twitter:title" content="{escape(seo_title, quote=True)}"><meta name="twitter:description" content="{escape(seo_description, quote=True)}"><meta name="twitter:image" content="{SEO_BASE_URL}/static/fixtude-512.png"><title>{escape(seo_title)}</title><style>body{{margin:0;background:#f5f8fc;color:#203047;font:16px/1.65 Arial,sans-serif}}main{{width:min(920px,calc(100% - 28px));margin:28px auto 50px}}.paper{{background:#fff;border:1px solid #dce5ef;border-radius:16px;padding:clamp(20px,4vw,38px)}}h1,h2{{line-height:1.25}}h1{{color:#174b91}}h2{{font-size:21px;margin-top:28px}}a{{color:#174b91}}.notice{{background:#fff7df;border:1px solid #efd58b;border-radius:10px;padding:13px 15px}}.muted{{color:#65758b;font-size:14px}}footer{{border-top:1px solid #dce5ef;margin-top:28px;padding-top:16px;font-size:13px;color:#65758b}}.top{{display:flex;justify-content:space-between;gap:15px;flex-wrap:wrap;margin-bottom:18px}}.brand{{font-size:24px;font-weight:800;color:#174b91;text-decoration:none}}</style></head><body><main><div class="top"><a class="brand" href="/">FixTude</a><a href="/">Torna al sito</a></div><article class="paper"><h1>{escape(title)}</h1><p class="muted">Versione {LEGAL_VERSION}</p><div class="notice"><strong>Denominazione del servizio:</strong> FixTude · <strong>Titolare:</strong> {escape(LEGAL_OWNER_NAME)} · <strong>P. IVA:</strong> {escape(LEGAL_VAT_NUMBER)}</div>{body}<footer><strong>Contatti:</strong> {owner_line}<br>Ultimo aggiornamento: 16 ottobre 2026<br><button type="button" id="ft-cookie-reopen-footer" style="border:0;background:transparent;color:#174b91;text-decoration:underline;cursor:pointer;font:inherit">Preferenze cookie</button></footer></article></main></body></html>""", mimetype="text/html")


@app.route("/disclaimer")
def legal_disclaimer():
    body = """<p>Questo Disclaimer descrive la natura e i limiti degli strumenti e dei contenuti disponibili su FixTude. Si applica in particolare al calcolatore di sostenibilità del debito, ai materiali informativi gratuiti e alle eventuali richieste di contatto.</p>
    <h2>1. Natura del servizio</h2><p>FixTude è un servizio informativo e di orientamento che propone strumenti per organizzare i dati economici, comprendere alcune possibili opzioni e preparare domande o comunicazioni da sottoporre ai soggetti competenti. L'utilizzo del sito, del calcolatore, della chat o dei moduli gratuiti non costituisce di per sé conferimento di un incarico professionale, mandato a rappresentare l'utente o instaurazione automatica di un rapporto di consulenza.</p>
    <h2>2. Calcolatore di sostenibilità del debito</h2><p>Il calcolatore esegue simulazioni matematiche sulla base degli importi inseriti dall'utente. La riserva mensile di 200 euro, la quota iniziale pari al 50% del margine prudenziale e le soglie del semaforo sono parametri orientativi dello strumento; non rappresentano criteri ufficiali, soglie normative o garanzie di sostenibilità. Il risultato dipende dalla correttezza, completezza e attualità dei dati e può non riflettere spese occasionali, entrate variabili, interessi, costi, scadenze, garanzie o altre circostanze rilevanti.</p>
    <h2>3. Simulazioni di rientro e saldo e stralcio</h2><p>Le rate a 6, 12 e 24 mesi sono calcoli teorici basati sul debito inserito e non includono necessariamente interessi, commissioni, spese legali, penali o altri importi. Lo scenario di saldo e stralcio al 60% è un esempio matematico e negoziale, non una percentuale tipica o garantita. Soltanto il creditore può valutare e accettare una proposta; prima di effettuare pagamenti è necessario ottenere condizioni chiare e conferma scritta degli effetti dell'accordo.</p>
    <h2>4. Nessuna garanzia di risultato</h2><p>FixTude non garantisce riduzioni del debito, sospensioni di azioni di recupero, accettazione di piani, cancellazioni o rettifiche nelle banche dati creditizie, né tempi o risultati specifici. Un esito verde non certifica la solvibilità; un esito giallo o rosso non costituisce una decisione definitiva sulla situazione dell'utente.</p>
    <h2>5. Limiti e professionisti abilitati</h2><p>I contenuti generali del sito non sostituiscono una consulenza legale, fiscale, contabile o finanziaria personalizzata. FixTude non si presenta, in forza del solo uso del sito, come avvocato, commercialista, consulente finanziario abilitato, organismo di composizione della crisi o rappresentante del cliente. Quando una situazione richiede una valutazione riservata a un professionista abilitato o a un organismo previsto dalla legge, l'utente dovrebbe rivolgersi al soggetto competente. Eventuali attività ulteriori, se offerte, dovranno essere descritte e concordate separatamente prima del loro avvio.</p>
    <h2>6. Documenti e informazioni inseriti dall'utente</h2><p>L'utente è responsabile di verificare i dati inseriti, gli importi dei debiti, l'identità dei creditori, le scadenze e i documenti ricevuti. Non caricare o inviare tramite moduli o chat documenti integrali, credenziali bancarie o dati personali non necessari. In presenza di atti giudiziari, termini di opposizione, procedure esecutive o scadenze imminenti, richiedi tempestivamente assistenza qualificata.</p>
    <h2>7. Elaborazione dei dati nel calcolatore</h2><p>Gli importi inseriti nel calcolatore vengono elaborati localmente nel browser e non sono inviati dal codice del calcolatore agli endpoint applicativi né salvati in un archivio di FixTude. Questa indicazione riguarda il calcolatore e non si estende automaticamente ai dati che l'utente sceglie di comunicare tramite il modulo di contatto, la chat, la posta elettronica o servizi di terze parti, che sono disciplinati dall'<a href="/privacy">Informativa Privacy</a>.</p>
    <h2>8. Contatti e aggiornamenti</h2><p>Per chiarimenti sul servizio scrivi a info@fixtude.it. Questo Disclaimer può essere aggiornato quando cambiano le funzionalità o le condizioni del servizio. Le informazioni presenti sul sito non sostituiscono la lettura delle comunicazioni ufficiali ricevute dai creditori né la verifica della normativa applicabile al caso concreto.</p>"""
    return legal_page("Disclaimer", body)


@app.route("/termini")
def legal_terms():
    body = """<p>I presenti Termini e Condizioni disciplinano l'uso del sito FixTude, dei suoi strumenti informativi e, quando attivo, l'acquisto del libro digitale <em>NIENTE PANICO</em>.</p>
    <h2>1. Denominazione del servizio, titolare e contatti</h2><p>FixTude è la denominazione del servizio e del progetto editoriale online. Il servizio è gestito da Giovanni Pugliese, con domicilio professionale indicato in Via Saronno 65, 00188 Roma, Italia, Partita IVA 15990471003. Per comunicazioni: info@fixtude.it. FixTude è il nome del servizio e non indica, da solo, una società distinta dal titolare sopra identificato.</p>
    <h2>2. Funzionalità gratuite</h2><p>Il sito può offrire un calcolatore orientativo di sostenibilità del debito, guide, moduli scaricabili, collegamenti informativi e chat. Le funzioni e la loro disponibilità possono variare per manutenzione o ragioni tecniche. Nel calcolatore gli importi sono elaborati nel browser e il codice dello strumento non li invia agli endpoint applicativi. Le formule, la riserva e le soglie del semaforo sono criteri orientativi scelti per una prima simulazione, non standard universali né una certificazione di solvibilità. Leggi anche il <a href="/disclaimer">Disclaimer FixTude</a>.</p>
    <h2>3. Limiti delle informazioni</h2><p>I contenuti hanno finalità informative e organizzative. Non costituiscono consulenza legale, fiscale o finanziaria personalizzata, non sostituiscono un professionista abilitato e non garantiscono riduzioni del debito, cancellazioni da banche dati o accettazione di proposte da parte dei creditori. L'utente deve verificare i dati e adattare i modelli alla propria situazione. Per i dettagli sui limiti del calcolatore e delle simulazioni consulta il <a href="/disclaimer">Disclaimer FixTude</a>.</p>
    <h2>4. Chat e richieste di contatto</h2><p>L'invio di una richiesta di contatto non comporta obbligo di acquistare servizi né garantisce tempi di risposta specifici. La chat non è un servizio di emergenza. Non inserire nella chat o nel modulo dati bancari, credenziali, documenti completi o dati personali non necessari.</p>
    <h2>5. Libro digitale e pagamenti</h2><p>Per l'edizione digitale PDF di <em>NIENTE PANICO</em>, il prezzo mostrato al momento della revisione è € 6,99, salvo diversa indicazione nella pagina di acquisto. Il pagamento, quando disponibile, è elaborato da Stripe; il download viene reso disponibile dopo la verifica della transazione. Non inviare i dati della carta a FixTude via email.</p><p>L'edizione cartacea è contrassegnata come “COMING SOON”: al momento non è possibile ordinarla né pagarla dal sito. Prezzo, modalità di stampa, spedizione e condizioni saranno pubblicati prima dell'apertura degli ordini.</p>
    <h2>6. Consumatori e recesso</h2><p>Restano fermi i diritti inderogabili riconosciuti ai consumatori dalla normativa applicabile. Per contenuti digitali forniti senza supporto materiale, l'eventuale perdita del diritto di recesso richiede che ricorrano tutte le condizioni previste dalla legge, incluse le dichiarazioni espresse richieste prima dell'inizio dell'esecuzione e la conferma su supporto durevole. Il flusso di acquisto deve prevedere le informazioni e le dichiarazioni espresse richieste dalla normativa prima dell'inizio della fornitura del contenuto digitale, oltre alla conferma su supporto durevole quando prevista.</p>
    <h2>7. Uso corretto</h2><p>L'utente si impegna a usare il sito in modo lecito, a non tentare accessi non autorizzati, a non inviare spam e a non caricare dati di terzi senza una base legittima.</p>
    <h2>8. Proprietà intellettuale</h2><p>Testi, grafica, marchio e materiali originali del sito sono protetti dalle norme applicabili. I modelli scaricabili possono essere utilizzati per esigenze personali, senza rimuovere avvertenze o presentare i contenuti come pareri professionali garantiti.</p>
    <h2>9. Privacy</h2><p>Il trattamento dei dati personali è descritto nell'<a href="/privacy">Informativa Privacy</a>. L'accettazione dei presenti Termini non sostituisce l'informativa e non costituisce consenso generale a trattamenti facoltativi.</p>
    <h2>10. Assistenza e modifiche</h2><p>Per richieste o reclami scrivere a info@fixtude.it. I termini potranno essere aggiornati per modifiche normative o tecniche; per gli acquisti conclusi restano fermi i diritti già acquisiti.</p>"""
    return legal_page("Termini e Condizioni", body)


@app.route("/privacy")
def privacy():
    body = """<p>Questa informativa descrive i principali trattamenti connessi alla versione attuale del sito FixTude. Viene aggiornata quando cambiano in modo rilevante le funzionalità, i fornitori o le modalità di trattamento.</p>
    <h2>1. Titolare del trattamento</h2><p>Il titolare del trattamento è Giovanni Pugliese, che gestisce il servizio denominato FixTude, con domicilio professionale indicato in Via Saronno 65, 00188 Roma, Italia, Partita IVA 15990471003. Per richieste relative ai dati personali: info@fixtude.it.</p>
    <h2>2. Dati trattati e finalità</h2><ul><li><strong>Calcolatore:</strong> gli importi inseriti vengono elaborati nel browser e il codice del calcolatore non li invia agli endpoint applicativi.</li><li><strong>Modulo di ricontatto:</strong> nome, email, telefono facoltativo e messaggio vengono inviati tramite Web3Forms per gestire la richiesta e recapitarla alla casella configurata. L'applicazione non conserva una copia in un proprio database. I dati possono restare nella casella email del destinatario e nei sistemi del fornitore secondo le relative condizioni.</li><li><strong>Chat:</strong> i messaggi sono conservati temporaneamente nella memoria del processo server per permettere la conversazione; le chat inattive vengono eliminate dopo circa due ore, ma un riavvio può eliminarle prima. L'invio di un messaggio genera una notifica email tramite Web3Forms.</li><li><strong>Libro digitale:</strong> se l'acquisto è attivo, Stripe tratta i dati necessari a elaborare e verificare il pagamento. FixTude riceve le informazioni necessarie a verificare la transazione.</li><li><strong>Dati tecnici:</strong> hosting e infrastruttura possono trattare dati tecnici, log e informazioni necessarie a sicurezza e funzionamento.</li></ul>
    <h2>3. Basi giuridiche</h2><p>Le richieste di contatto sono trattate per rispondere a una richiesta dell'interessato e adottare misure precontrattuali su sua richiesta. I dati relativi ad acquisti sono trattati per eseguire il contratto e adempiere agli obblighi di legge. Sicurezza e prevenzione abusi possono basarsi sul legittimo interesse, da valutare e documentare. Eventuali attività promozionali richiedono una valutazione e, ove necessario, un consenso separato.</p>
    <h2>4. Destinatari e fornitori</h2><p>I fornitori attualmente coinvolti o da verificare includono Render per l'hosting (se confermato in produzione), Web3Forms per invii dal modulo e notifiche email, Register per la casella email e Stripe per i pagamenti del libro, quando attivi. Devono essere controllati termini, ruoli privacy, subfornitori, tempi di conservazione e possibili trasferimenti fuori dallo Spazio Economico Europeo.</p>
    <h2>5. Cookie e strumenti di tracciamento</h2><p>Google Analytics (ID di misurazione G-HMBBNLHC7E) viene caricato soltanto dopo la scelta esplicita della categoria statistiche nel pannello cookie. Se la categoria viene rifiutata o disattivata, il sito non avvia nuove richieste di misurazione e aggiorna lo stato di consenso di Google Analytics. I pixel pubblicitari non sono attivi. Prima del lancio definitivo occorre verificare tecnicamente i cookie effettivamente installati e aggiornare questa informativa in base ai risultati.</p>
    <h2>6. Conservazione</h2><p>I messaggi chat sono temporanei secondo quanto descritto sopra. Le richieste di contatto possono restare nei sistemi del fornitore di invio e nella casella email del destinatario. I tempi effettivi di conservazione dei fornitori, della posta e dei log devono essere verificati e definiti dal titolare prima del lancio.</p>
    <h2>7. Diritti</h2><p>Nei limiti previsti dal GDPR è possibile chiedere accesso, rettifica, cancellazione, limitazione, opposizione e, ove applicabile, portabilità, scrivendo a info@fixtude.it. È possibile proporre reclamo al Garante per la protezione dei dati personali.</p>
    <h2>8. Sicurezza e aggiornamenti</h2><p>Il titolare deve verificare periodicamente le misure di sicurezza, l'accesso ai sistemi, la configurazione dei fornitori e la corrispondenza di questa informativa con i trattamenti reali. La pagina va aggiornata prima di introdurre nuove funzionalità o servizi di terze parti.</p>"""
    return legal_page("Informativa Privacy", body)


@app.route("/cookie")
def legal_cookie():
    body = """<p>Questa pagina descrive il pannello di preferenze predisposto per FixTude. L'elenco definitivo di cookie e strumenti deve essere confermato tramite verifica tecnica del sito in produzione e dei servizi esterni richiamati.</p>
    <h2>1. Titolare e strumenti necessari</h2><p>Il servizio FixTude è gestito da Giovanni Pugliese, Via Saronno 65, 00188 Roma, Italia, P. IVA 15990471003. Per informazioni sulle preferenze cookie: info@fixtude.it.</p><p>Il sito può utilizzare strumenti tecnici necessari al funzionamento e alla sicurezza. La configurazione e i nomi esatti devono essere verificati in produzione.</p>
    <h2>2. Statistiche e pubblicità</h2><p>Google Analytics (ID di misurazione G-HMBBNLHC7E) viene caricato soltanto dopo il consenso alla categoria statistiche. Se la categoria viene rifiutata o revocata, il sito aggiorna lo stato di consenso e disabilita il tracciamento Analytics. I pixel pubblicitari non sono attivi. L’effettiva presenza e durata dei cookie deve essere verificata sul sito in produzione.</p>
    <h2>3. Memorizzazione della scelta</h2><p>Il pannello salva nel browser un cookie di prima parte denominato <code>fixtude_cookie_consent</code> per ricordare le preferenze per circa sei mesi. Le scelte possono essere modificate in qualsiasi momento tramite “Preferenze cookie” nel piè di pagina. I cookie strettamente necessari non possono essere disattivati dal pannello.</p>
    <h2>4. Verifica tecnica</h2><p>Prima della pubblicazione definitiva occorre eseguire una scansione tecnica di fixtude.it, incluse le pagine di acquisto e gli eventuali servizi incorporati, per identificare nome, fornitore, finalità e durata di tutti i cookie o strumenti di memorizzazione effettivamente presenti.</p>"""
    return legal_page("Cookie Policy", body)


@app.after_request
def inject_cookie_banner_on_legal_pages(response):
    """Aggiunge il pannello cookie anche alle pagine legali autonome."""
    try:
        content_type = response.headers.get("Content-Type", "")
        if response.status_code == 200 and "text/html" in content_type.lower() and response.direct_passthrough is False:
            html = response.get_data(as_text=True)
            if "fixtude-cookie-script" not in html and "</body>" in html.lower():
                banner = r"""
                <section id="ft-cookie-banner" role="dialog" aria-modal="false" aria-labelledby="ft-cookie-title" hidden style="position:fixed;z-index:99999;left:12px;right:12px;bottom:12px;max-width:900px;margin:auto;background:#fff;color:#172033;border:1px solid #dce4f2;border-radius:16px;box-shadow:0 18px 55px rgba(14,30,70,.22);padding:20px;font:14px/1.55 Arial,Helvetica,sans-serif">
                  <h2 id="ft-cookie-title" style="font-size:19px;margin:0 0 8px">La tua privacy conta</h2>
                  <p>Google Analytics viene caricato soltanto se autorizzi le statistiche. I pixel pubblicitari non sono attivi. Puoi accettare, rifiutare o personalizzare la tua scelta.</p>
                  <p><a href="/cookie">Cookie Policy</a> · <a href="/privacy">Informativa Privacy</a></p>
                  <div style="display:flex;flex-wrap:wrap;gap:9px"><button id="ft-cookie-accept" type="button">Accetta tutti</button><button id="ft-cookie-reject" type="button">Rifiuta gli opzionali</button><button id="ft-cookie-customize" type="button">Personalizza</button></div>
                  <div id="ft-cookie-preferences" hidden style="border-top:1px solid #ddd;margin-top:12px;padding-top:12px">
                    <p><label><input id="ft-cookie-analytics" type="checkbox"> Statistiche / Google Analytics (caricamento solo previo consenso)</label></p>
                    <p><label><input id="ft-cookie-marketing" type="checkbox"> Pubblicità / pixel (non attivi)</label></p>
                    <button id="ft-cookie-save" type="button">Salva preferenze</button> <button id="ft-cookie-cancel" type="button">Annulla</button>
                  </div>
                </section>
                <script id="fixtude-cookie-script">(()=>{const b=document.getElementById('ft-cookie-banner'),p=document.getElementById('ft-cookie-preferences'),a=document.getElementById('ft-cookie-analytics'),m=document.getElementById('ft-cookie-marketing'),f=document.getElementById('ft-cookie-reopen-footer'),n='fixtude_cookie_consent',GA_ID='G-HMBBNLHC7E';function get(){const x=document.cookie.split('; ').find(v=>v.startsWith(n+'='));if(!x)return null;try{return JSON.parse(decodeURIComponent(x.slice(n.length+1)))}catch(_){return null}}function load(){window['ga-disable-'+GA_ID]=false;window.dataLayer=window.dataLayer||[];window.gtag=window.gtag||function(){window.dataLayer.push(arguments)};if(!window.__fixtudeGaInitialized){window.gtag('consent','default',{analytics_storage:'denied',ad_storage:'denied',ad_user_data:'denied',ad_personalization:'denied'});window.gtag('js',new Date());window.gtag('config',GA_ID,{send_page_view:false});const s=document.createElement('script');s.async=true;s.src='https://www.googletagmanager.com/gtag/js?id='+encodeURIComponent(GA_ID);document.head.appendChild(s);window.__fixtudeGaInitialized=true}window.gtag('consent','update',{analytics_storage:'granted',ad_storage:'denied',ad_user_data:'denied',ad_personalization:'denied'});window.gtag('event','page_view',{page_path:location.pathname+location.search,page_title:document.title})}function apply(ok){window['ga-disable-'+GA_ID]=!ok;if(ok)load();else if(typeof window.gtag==='function')window.gtag('consent','update',{analytics_storage:'denied',ad_storage:'denied',ad_user_data:'denied',ad_personalization:'denied'})}function open(show){b.hidden=false;p.hidden=!show;const c=get();a.checked=!!(c&&c.analytics);m.checked=!!(c&&c.marketing)}function save(x,y){const v={necessary:true,analytics:!!x,marketing:!!y,savedAt:new Date().toISOString(),version:2};document.cookie=n+'='+encodeURIComponent(JSON.stringify(v))+'; Max-Age=15552000; Path=/; SameSite=Lax'+(location.protocol==='https:'?'; Secure':'');apply(!!x);b.hidden=true}const old=get();if(!old)open(false);else apply(!!old.analytics);if(f)f.addEventListener('click',()=>open(true));document.getElementById('ft-cookie-accept').addEventListener('click',()=>save(true,true));document.getElementById('ft-cookie-reject').addEventListener('click',()=>save(false,false));document.getElementById('ft-cookie-customize').addEventListener('click',()=>open(true));document.getElementById('ft-cookie-save').addEventListener('click',()=>save(a.checked,m.checked));document.getElementById('ft-cookie-cancel').addEventListener('click',()=>{if(get())b.hidden=true;else open(false)})})();</script>
                """
                pos = html.lower().rfind("</body>")
                html = html[:pos] + banner + html[pos:]
                response.set_data(html)
    except Exception:
        app.logger.exception("Impossibile aggiungere il banner cookie alla pagina legale")
    return response


@app.route("/favicon.svg", methods=["GET"])
def favicon_svg():
    """Compatibilità: rimanda alla favicon ufficiale PNG di FixTude."""
    return redirect("/static/fixtude-48.png", code=302)


@app.route("/favicon.ico", methods=["GET"])
def favicon_ico():
    """Fallback per browser che richiedono il percorso /favicon.ico."""
    return redirect("/static/fixtude-48.png", code=302)


@app.route("/robots.txt", methods=["GET"])
def robots_txt():
    """Istruzioni essenziali per i crawler dei motori di ricerca."""
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /chat-staff\n"
        "Disallow: /api/\n"
        f"Sitemap: {SEO_BASE_URL}/sitemap.xml\n"
    )
    return Response(body, mimetype="text/plain; charset=utf-8")


@app.route("/sitemap.xml", methods=["GET"])
def sitemap_xml():
    """Sitemap XML delle pagine pubbliche principali di FixTude."""
    paths = [
        "/", "/misuratore", "/consulenza", "/moduli",
        "/educazione-finanziaria", "/educazione-finanziaria/come-gestire-lo-stipendio-regola-un-terzo", "/sic", "/assistenza-legale", "/shop",
        "/disclaimer", "/termini", "/privacy", "/cookie",
    ]
    urls = "".join(
        "<url><loc>" + escape(SEO_BASE_URL + ("" if path == "/" else path)) + "</loc></url>"
        for path in paths
    )
    xml = '<?xml version="1.0" encoding="UTF-8"?>' + '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + urls + '</urlset>'
    return Response(xml, mimetype="application/xml; charset=utf-8")


@app.route("/health")
def health():
    return jsonify(status="ok", service="FixTude")


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=False
    )


