import os
import json
import re
import ssl
import time
import uuid
import smtplib
import threading
import sqlite3
import secrets
import hmac
import struct
import zlib
from datetime import datetime
from zoneinfo import ZoneInfo
from email.message import EmailMessage
from html import escape
from werkzeug.security import generate_password_hash, check_password_hash

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
    if request.path in ("/favicon.svg", "/favicon.ico", "/favicon-16.png", "/apple-touch-icon.png"):
        response.headers["Cache-Control"] = "public, max-age=86400"
        response.headers.pop("Pragma", None)
    else:
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
    "/situazione-debitoria": {
        "title": "La mia situazione debitoria | FixTude",
        "description": "Descrivi la tua situazione debitoria e scegli se procedere con la registrazione o senza account. Primo orientamento, senza promesse di risultato.",
    },
    "/situazione-debitoria/avvia": {
        "title": "Avvia la richiesta sulla tua situazione debitoria | FixTude",
        "description": "Raccogli le informazioni essenziali su creditori, importi e comunicazioni ricevute per chiedere un primo contatto a FixTude.",
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
        "title": "Come gestire le entrate: la regola di un terzo | FixTude",
        "description": "Scopri come organizzare le entrate con la regola di un terzo. Esempi pratici per lavoratori dipendenti, autonomi, single e famiglie.",
    },
    "/educazione-finanziaria/guida-saldo-e-stralcio": {
        "title": "Saldo e stralcio: cos'è, come funziona e quando conviene | FixTude",
        "description": "Saldo e stralcio dei debiti: come funziona la trattativa, quali rischi controllare, cosa scrivere nell'accordo e come evitare errori prima di pagare.",
    },
    "/sic": {
        "title": "Sistemi di informazione creditizia (SIC) | FixTude",
        "description": "Informazioni orientative sui sistemi di informazione creditizia e sulla consultazione dei dati relativi alla propria posizione.",
    },
    "/professionisti": {
        "title": "Area Professionisti | Avvocati e commercialisti FixTude",
        "description": "Area professionale FixTude per avvocati e commercialisti: profilo professionale, registrazione e accesso riservato.",
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
    noindex_paths = ("/situazione-debitoria/avvia", "/chat-staff", "/shop/conferma", "/shop/scarica", "/shop/anteprima", "/assistenza-legale/conferma", "/professionisti/login", "/professionisti/registrazione", "/professionisti/area", "/professionisti/logout", "/professionisti/admin")
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
            "headline": "Come gestire le entrate: la regola di un terzo",
            "description": SEO_PAGES[path]["description"],
            "datePublished": "2026-10-17",
            "dateModified": "2026-10-17",
            "inLanguage": "it-IT",
            "mainEntityOfPage": {"@type": "WebPage", "@id": SEO_BASE_URL + path},
            "author": {"@type": "Organization", "name": "FixTude", "url": SEO_BASE_URL + "/"},
            "publisher": {"@type": "Organization", "name": "FixTude", "url": SEO_BASE_URL + "/"},
        }
    elif path == "/educazione-finanziaria/guida-saldo-e-stralcio":
        structured_data = {
            "@context": "https://schema.org",
            "@type": "BlogPosting",
            "headline": "Saldo e stralcio: cos'è, come funziona e quando conviene",
            "description": SEO_PAGES[path]["description"],
            "datePublished": "2026-10-20",
            "dateModified": "2026-10-20",
            "inLanguage": "it-IT",
            "image": SEO_BASE_URL + "/static/articolo-saldo-stralcio.png",
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
          <link rel="icon" type="image/x-icon" href="/favicon.ico?v=20261019" sizes="48x48">
          <link rel="icon" type="image/png" sizes="16x16" href="/favicon-16.png?v=20261019">
          <link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png?v=20261019">
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
              grid-template-columns:minmax(0,1.1fr) minmax(145px,1fr) minmax(110px,.8fr) minmax(125px,.9fr) auto;
              gap:10px;
              align-items:end;
              padding:12px 0;
              border-bottom:1px solid var(--line);
            }
            .entry-row .field {margin:0}
            .entry-row select {width:100%;min-width:0;box-sizing:border-box}
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
              .entry-row .debt-type-field {grid-column:1 / -1}
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
                <a href="/professionisti">Area Professionisti</a>
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
      .home-situation-title{display:block;width:100%;max-width:820px;box-sizing:border-box;text-align:center;margin:10px auto 8px;padding:0 12px;align-self:center}
      .home-situation-intro{display:block;width:100%;max-width:760px;box-sizing:border-box;text-align:center;margin:0 auto 12px;padding:0 12px;align-self:center}
      .home-situation-options{display:grid;width:100%;box-sizing:border-box;grid-template-columns:repeat(3,minmax(0,1fr));gap:24px;margin:24px auto 8px;max-width:820px;align-items:start;justify-content:center;align-self:center}
      .home-situation-option{display:flex;flex-direction:column;align-items:center;gap:15px;min-width:0}
      .home-situation-choice{position:relative;isolation:isolate;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;width:clamp(98px,11.7vw,133px);height:clamp(98px,11.7vw,133px);padding:10px;border:1px solid rgba(255,255,255,.72);border-radius:50%;cursor:pointer;font:inherit;text-align:center;color:#fff;text-decoration:none;overflow:hidden;transform:translateY(-3px);box-shadow:0 5px 0 var(--button-depth),0 12px 22px rgba(15,23,42,.17),inset 0 1px 2px rgba(255,255,255,.72),inset 0 -7px 13px rgba(0,0,0,.24);transition:transform .18s ease,box-shadow .18s ease,filter .18s ease;touch-action:manipulation;background-clip:padding-box}
      .home-situation-choice:before{content:"";position:absolute;z-index:-1;left:12%;top:7%;width:76%;height:35%;border-radius:50%;background:linear-gradient(180deg,rgba(255,255,255,.48),rgba(255,255,255,.08) 58%,transparent);filter:blur(.35px);pointer-events:none}
      .home-situation-choice:after{content:"";position:absolute;inset:4px;border:1px solid rgba(255,255,255,.18);border-radius:50%;pointer-events:none;box-shadow:inset 0 0 0 1px rgba(0,0,0,.06)}
      .home-situation-choice:hover{filter:brightness(1.035);transform:translateY(-7px);box-shadow:0 9px 0 var(--button-depth),0 19px 25px rgba(15,23,42,.2),inset 0 1px 2px rgba(255,255,255,.72),inset 0 -7px 13px rgba(0,0,0,.24)}
      .home-situation-choice:active{transform:translateY(2px) scale(.99);box-shadow:0 1px 0 var(--button-depth),0 4px 8px rgba(15,23,42,.16),inset 0 3px 8px rgba(0,0,0,.2);filter:brightness(.98)}
      .home-situation-choice:focus-visible{outline:3px solid #1675d1;outline-offset:6px}
      .home-situation-icon{font-size:24px;line-height:1.05;font-weight:800;text-shadow:0 1px 2px rgba(0,0,0,.16)}
      .home-situation-label{font-size:clamp(15px,1.6vw,19px);font-weight:850;line-height:1.05;text-shadow:0 1px 2px rgba(0,0,0,.18)}
      .home-situation-subtitle{font-size:clamp(9px,1vw,11px);font-weight:800;letter-spacing:.055em;line-height:1.2;text-shadow:0 1px 2px rgba(0,0,0,.15)}
      .home-situation-description{display:block;max-width:230px;text-align:center;font-size:13px;line-height:1.45;font-style:italic;margin:0 auto;overflow-wrap:break-word}
      .home-situation-red{--button-depth:#720e17;background:radial-gradient(ellipse at 34% 20%,#e95a57 0%,#c9232b 38%,#a91420 72%,#790b15 100%);border-color:rgba(255,255,255,.58)}
      .home-situation-yellow{--button-depth:#80500a;background:radial-gradient(ellipse at 34% 20%,#f4d878 0%,#d9a52c 38%,#b87d14 72%,#80500a 100%);border-color:rgba(255,255,255,.6)}
      .home-situation-green{--button-depth:#06462d;background:radial-gradient(ellipse at 34% 20%,#48bd88 0%,#13845b 39%,#086544 73%,#06452f 100%);border-color:rgba(255,255,255,.58)}
      @media(max-width:700px){.home-hero-layout{grid-template-columns:1fr;gap:18px}.home-hero-copy h1{font-size:clamp(27px,6vw,36px);line-height:1.12}.home-hero-copy p{font-size:16px;line-height:1.5}.home-situation-panel{width:100%;max-width:100%;padding:0 4px}.home-situation-title{font-size:clamp(21px,5vw,28px);margin-top:8px;padding:0 8px}.home-situation-intro{font-size:15px;line-height:1.4;padding:0 8px}.home-situation-options{grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:20px;padding:0 2px}.home-situation-option{gap:9px}.home-situation-choice{width:min(18.2vw,86px);height:min(18.2vw,86px);min-width:68px;min-height:68px;padding:5px;border-width:1px;transform:translateY(-2px);box-shadow:0 3px 0 var(--button-depth),0 7px 11px rgba(15,23,42,.17),inset 0 1px 2px rgba(255,255,255,.7),inset 0 -4px 7px rgba(0,0,0,.22)}.home-situation-choice:hover{transform:translateY(-4px)}.home-situation-icon{font-size:17px}.home-situation-label{font-size:clamp(10px,2.7vw,13px)}.home-situation-subtitle{font-size:clamp(7px,2vw,9px)}.home-situation-description{max-width:100%;font-size:clamp(11px,2.9vw,13px);line-height:1.35}.home-situation-title{margin-top:8px}.home-situation-intro{font-size:15px;line-height:1.4}}
      @media(prefers-reduced-motion:reduce){.home-situation-choice{transition:none}}
      .home-path-actions{display:flex;justify-content:center;align-items:stretch;gap:12px;flex-wrap:wrap;max-width:760px;margin:28px auto 4px}
      .home-path-actions .btn{min-width:230px;text-align:center;justify-content:center;white-space:normal}
      @media(max-width:600px){.home-path-actions{display:grid;grid-template-columns:1fr;gap:10px;margin-top:22px;padding:0 8px}.home-path-actions .btn{width:100%;min-width:0;box-sizing:border-box}}
    </style>
    <section class="hero">
      <div class="home-hero-layout">
        <div class="home-hero-copy">
          <h1>Fai chiarezza sulla tua<br>situazione debitoria.</h1>
          <p><strong>Semplice. Riservato. Senza registrazione.</strong></p>
        </div>
      </div>
      <div class="home-situation-panel">
      <h2 class="home-situation-title">Cominciamo.</h2>
      <p class="home-situation-intro">Scegli il pulsante che descrive la tua situazione.<br>Il calcolatore ti fornirà un primo esito di sostenibilità.</p>
      <div class="home-situation-options">
        <div class="home-situation-option">
          <a class="home-situation-choice home-situation-red" href="/misuratore?scenario=rosso#calculatorStart" aria-label="Emergenza: non riesco a pagare la rata di questo mese e ho già ricevuto solleciti">
            <span class="home-situation-icon" aria-hidden="true">!</span><span class="home-situation-subtitle">EMERGENZA</span>
          </a>
          <em class="home-situation-description">Sto indietro con le rate.</em>
        </div>
        <div class="home-situation-option">
          <a class="home-situation-choice home-situation-yellow" href="/misuratore?scenario=giallo#calculatorStart" aria-label="Allerta: pago tutto ma faccio fatica e non mi rimane quasi nulla dello stipendio">
            <span class="home-situation-icon" aria-hidden="true">!</span><span class="home-situation-subtitle">ALLERTA</span>
          </a>
          <em class="home-situation-description">Faccio fatica a pagare tutto.</em>
        </div>
        <div class="home-situation-option">
          <a class="home-situation-choice home-situation-green" href="/misuratore?scenario=verde#calculatorStart" aria-label="Controllo: voglio verificare se i miei debiti sono sostenibili">
            <span class="home-situation-icon" aria-hidden="true">✓</span><span class="home-situation-subtitle">CONTROLLO</span>
          </a>
          <em class="home-situation-description">Situazione gestibile.</em>
        </div>
      </div>
      <div class="home-path-actions" aria-label="Scegli come proseguire">
        <a class="btn" href="/situazione-debitoria/avvia?modalita=registrazione">Procedi con la registrazione</a>
        <a class="btn outline" href="/situazione-debitoria/avvia?modalita=senza-registrazione">Procedi senza registrazione</a>
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
# NUOVO PERCORSO CLIENTI: SITUAZIONE DEBITORIA
# Prototipo di raccolta iniziale: nessun documento caricato e nessun
# archivio pratica/account cliente viene creato in questa versione.
# =========================================================

@app.route("/situazione-debitoria", methods=["GET"])
def situazione_debitoria():
    modalita = (request.args.get("modalita", "") or "").strip().lower()
    if modalita not in ("registrazione", "senza-registrazione"):
        modalita = ""
    mode_label = "con richiesta di registrazione" if modalita == "registrazione" else "senza registrazione"
    content = f'''<style>
      .intake-wrap{{max-width:850px;margin:0 auto}}
      .intake-intro{{font-size:17px;max-width:760px}}
      .intake-options{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;margin:24px 0}}
      .intake-option{{display:flex;flex-direction:column;align-items:flex-start;gap:12px;padding:22px;border:1px solid #dce5ef;border-radius:16px;background:#fff;box-shadow:0 8px 22px rgba(18,58,114,.06)}}
      .intake-option h2{{margin:0;font-size:22px;line-height:1.2}}
      .intake-option p{{margin:0;color:#52647b}}
      .intake-option .btn{{margin-top:auto;white-space:normal;text-align:center}}
      .intake-icon{{width:44px;height:44px;border-radius:12px;display:grid;place-items:center;background:#eaf2ff;color:#174b91;font-size:23px;font-weight:800}}
      .intake-note{{font-size:13px;color:#64748b;background:#f8fafc;border:1px solid #e2e8f0;padding:14px;border-radius:12px}}
      .intake-form-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}
      .intake-form-grid .full{{grid-column:1/-1}}
      .intake-type-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}}
      .intake-type{{display:flex;align-items:flex-start;gap:9px;border:1px solid #dce5ef;border-radius:10px;padding:12px;background:#fff}}
      .intake-type input{{margin-top:5px;flex-shrink:0}}
      .intake-form-actions{{display:flex;gap:10px;flex-wrap:wrap;align-items:center}}
      @media(max-width:650px){{.intake-options,.intake-form-grid,.intake-type-grid{{grid-template-columns:1fr}}.intake-form-grid .full{{grid-column:auto}}.intake-option{{padding:19px}}}}
    </style>
    <div class="intake-wrap">
      <p class="muted">FIXTUDE · PRIMO ORIENTAMENTO</p>
      <h1>La mia situazione debitoria</h1>
      <p class="intake-intro">Hai ricevuto un sollecito, una cartella, una diffida o un altro documento relativo a un debito? Inizia da qui. Ti aiutiamo a mettere in ordine le informazioni essenziali e a individuare i prossimi passi da valutare.</p>
      <section class="intake-options" aria-label="Scegli come procedere">
        <article class="intake-option">
          <div class="intake-icon" aria-hidden="true">♙</div>
          <h2>Procedi con la registrazione</h2>
          <p>Per chi desidera un percorso personale e vuole prepararsi a ritrovare le proprie richieste e gli aggiornamenti in un unico spazio.</p>
          <a class="btn" href="/situazione-debitoria/avvia?modalita=registrazione">Procedi con la registrazione</a>
          <p class="muted" style="font-size:12px">L'area personale e l'archivio delle pratiche sono in fase di attivazione: questa schermata non crea ancora un account.</p>
        </article>
        <article class="intake-option">
          <div class="intake-icon" aria-hidden="true">➜</div>
          <h2>Procedi senza registrazione</h2>
          <p>Per chi vuole descrivere subito la propria situazione, senza creare un account e senza impostare una password.</p>
          <a class="btn outline" href="/situazione-debitoria/avvia?modalita=senza-registrazione">Procedi senza registrazione</a>
        </article>
      </section>
      <div class="intake-note"><strong>Prima di iniziare:</strong> non caricare documenti in questa versione e non inserire codici fiscali, coordinate bancarie, password o dati non necessari. Se hai ricevuto un atto con una scadenza, annota la data e valuta tempestivamente un confronto con un professionista abilitato. FixTude non promette cancellazioni del debito o risultati garantiti.</div>
      <p class="muted" style="margin-top:20px">Puoi continuare anche con il <a href="/misuratore">calcolatore gratuito</a> oppure <a href="/consulenza">chiedere di essere ricontattato</a>.</p>
    </div>'''
    return page("La mia situazione debitoria", content)


@app.route("/situazione-debitoria/avvia", methods=["GET"])
def situazione_debitoria_avvia():
    modalita = (request.args.get("modalita", "senza-registrazione") or "senza-registrazione").strip().lower()
    if modalita not in ("registrazione", "senza-registrazione"):
        modalita = "senza-registrazione"
    registrazione = modalita == "registrazione"
    mode_label = "con richiesta di registrazione" if registrazione else "senza registrazione"
    titolo = "Richiesta di avvio con registrazione" if registrazione else "Descrivi la tua situazione"
    sottotitolo = ("Raccogliamo la tua richiesta per il percorso con registrazione. L'account personale non viene creato automaticamente in questa versione; ti contatteremo per i passaggi successivi." if registrazione else "Compila i campi che conosci. Non serve creare un account. Per ora non inviare allegati: ti chiediamo solo le informazioni iniziali necessarie.")
    content = f'''<style>
      .intake-wrap{{max-width:820px;margin:0 auto}}
      .intake-form-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}
      .intake-form-grid .full{{grid-column:1/-1}}
      .intake-type-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:9px;align-items:stretch}}
      .intake-form-grid .field label.intake-type{{display:flex!important;flex-direction:row;align-items:flex-start;justify-content:flex-start;gap:10px;width:100%;min-width:0;min-height:0;margin:0;padding:13px 14px;border:1px solid #dce5ef;border-radius:10px;background:#fff;font-weight:400;line-height:1.4;cursor:pointer}}
      .intake-form-grid .field label.intake-type:hover{{border-color:#8eafd9;background:#f8fbff}}
      .intake-form-grid .field label.intake-type input[type=checkbox]{{display:block!important;appearance:auto;width:17px!important;min-width:17px;max-width:17px;height:17px!important;margin:3px 0 0!important;padding:0!important;flex:0 0 17px;border:initial;box-shadow:none}}
      .intake-type span{{display:block;flex:1 1 auto;min-width:0;overflow-wrap:anywhere}}
      .intake-type strong{{display:block;margin:0 0 4px;font-size:15px;color:#203047}}
      .intake-type small{{display:block;font-size:13px;line-height:1.45;color:#65758b;font-weight:400}}
      .intake-form-actions{{display:flex;gap:10px;flex-wrap:wrap;align-items:center}}
      @media(max-width:650px){{.intake-form-grid,.intake-type-grid{{grid-template-columns:1fr}}.intake-form-grid .full{{grid-column:auto}}.intake-form-grid .field label.intake-type{{padding:12px}}}}
    </style>
    <div class="intake-wrap">
      <p class="muted">LA MIA SITUAZIONE DEBITORIA · {mode_label.upper()}</p>
      <h1>{titolo}</h1>
      <p class="muted">{sottotitolo}</p>
      <section class="card">
        <form id="debtIntakeForm">
          <input type="hidden" name="modalita_percorso" value="{escape(modalita)}">
          <input type="hidden" name="subject" value="FixTude - nuova richiesta situazione debitoria">
          <input type="hidden" name="from_name" value="FixTude - percorso situazione debitoria">
          <div class="intake-form-grid">
            <div class="field"><label for="di_name">Nome *</label><input id="di_name" name="name" maxlength="100" autocomplete="name" required></div>
            <div class="field"><label for="di_email">Email *</label><input id="di_email" name="email" type="email" maxlength="254" autocomplete="email" required></div>
            <div class="field"><label for="di_phone">Telefono (facoltativo)</label><input id="di_phone" name="phone" type="tel" maxlength="40" autocomplete="tel"></div>
            <div class="field"><label for="di_count">Quante posizioni vuoi segnalare?</label><select id="di_count" name="numero_posizioni"><option value="1">Una</option><option value="2-3">Due o tre</option><option value="4 o più">Quattro o più</option><option value="Non so ancora">Non lo so ancora</option></select></div>
            <div class="field full"><label>Che tipo di debito riguarda? Seleziona tutte le opzioni pertinenti *</label><div class="intake-type-grid">
              <label class="intake-type"><input type="checkbox" name="tipi_debito" value="Utenze e servizi"><span><strong>Utenze e servizi</strong><br><small>Luce, gas, acqua, telefono, Internet, abbonamenti</small></span></label>
              <label class="intake-type"><input type="checkbox" name="tipi_debito" value="Crediti finanziari"><span><strong>Crediti finanziari</strong><br><small>Prestiti, finanziamenti, carte, mutui, banca</small></span></label>
              <label class="intake-type"><input type="checkbox" name="tipi_debito" value="Debiti fiscali e tributari"><span><strong>Debiti fiscali e tributari</strong><br><small>Cartelle, Agenzia Entrate-Riscossione, IMU, TARI, bollo</small></span></label>
              <label class="intake-type"><input type="checkbox" name="tipi_debito" value="Altri debiti"><span><strong>Altri debiti</strong><br><small>Condominio, affitto, privati o debiti commerciali</small></span></label>
            </div></div>
            <div class="field"><label for="di_creditor">Creditore principale (se noto)</label><input id="di_creditor" name="creditore_principale" maxlength="180" placeholder="Es. società, banca, ente"></div>
            <div class="field"><label for="di_amount">Importo indicativo complessivo (€)</label><input id="di_amount" name="importo_indicativo" inputmode="decimal" maxlength="30" placeholder="Facoltativo"></div>
            <div class="field full"><label for="di_act">Hai ricevuto una comunicazione o un atto?</label><select id="di_act" name="documento_ricevuto"><option value="Non so / preferisco descriverlo">Non so / preferisco descriverlo</option><option value="Sollecito di pagamento">Sollecito di pagamento</option><option value="Messa in mora o diffida">Messa in mora o diffida</option><option value="Decreto ingiuntivo">Decreto ingiuntivo</option><option value="Atto di precetto">Atto di precetto</option><option value="Pignoramento">Pignoramento</option><option value="Cartella di pagamento">Cartella di pagamento</option><option value="Avviso di accertamento o atto fiscale">Avviso di accertamento o atto fiscale</option><option value="Intimazione di pagamento">Intimazione di pagamento</option><option value="Preavviso di fermo o ipoteca">Preavviso di fermo o ipoteca</option><option value="Piano di rateizzazione">Piano di rateizzazione</option><option value="Altro">Altro</option><option value="Nessun documento ricevuto">Nessun documento ricevuto</option></select></div>
            <div class="field"><label for="di_date">Data di ricezione (se nota)</label><input id="di_date" name="data_ricezione" type="date"></div>
            <div class="field"><label for="di_deadline">Scadenza indicata (se nota)</label><input id="di_deadline" name="scadenza_indicata" type="date"></div>
            <div class="field full"><label for="di_notes">Descrizione breve (facoltativa)</label><textarea id="di_notes" name="descrizione" maxlength="1800" rows="4" placeholder="Descrivi in breve cosa è successo e cosa vorresti chiarire. Non inserire codici fiscali, coordinate bancarie o altri dati sensibili."></textarea></div>
          </div>
          <div style="position:absolute;left:-10000px" aria-hidden="true"><label for="di_website">Non compilare</label><input id="di_website" name="website" tabindex="-1" autocomplete="off"></div>
          <label class="check" style="margin-top:16px"><input type="checkbox" name="privacy" value="Letta" required><span>Dichiaro di aver letto l'<a href="/privacy" target="_blank" rel="noopener">Informativa Privacy</a> e chiedo di essere ricontattato per questa richiesta.</span></label>
          <label class="check"><input type="checkbox" name="accept_terms" value="Accettati" required><span>Accetto i <a href="/termini" target="_blank" rel="noopener">Termini e Condizioni</a>.</span></label>
          <p class="intake-note" style="font-size:13px;color:#64748b;background:#f8fafc;border:1px solid #e2e8f0;padding:13px;border-radius:10px">I dati saranno inviati al titolare via Web3Forms per gestire la richiesta; l'applicazione non salva una copia nel proprio database. In questa versione non è previsto il caricamento di documenti né un archivio pratica. Le richieste che riguardano atti con termini o scadenze potrebbero richiedere una valutazione professionale tempestiva.</p>
          <div id="debtIntakeResult" role="status" aria-live="polite"></div>
          <div class="intake-form-actions"><button class="btn" id="debtIntakeSubmit" type="submit">Invia la richiesta</button><a class="btn outline" href="/situazione-debitoria">Torna alla scelta</a></div>
        </form>
      </section>
    </div>
    <script>
    (()=>{{
      const form=document.getElementById('debtIntakeForm');
      const result=document.getElementById('debtIntakeResult');
      const button=document.getElementById('debtIntakeSubmit');
      const checks=[...form.querySelectorAll('input[name="tipi_debito"]')];
      form.addEventListener('submit',async e=>{{
        e.preventDefault(); result.className=''; result.textContent='';
        if(!form.reportValidity())return;
        if(!checks.some(x=>x.checked)){{result.className='notice error';result.textContent='Seleziona almeno una categoria di debito.';return;}}
        const values={{}};
        for(const [key,value] of new FormData(form).entries()){{
          if(key==='website'&&String(value).trim()){{result.className='notice success';result.textContent='Richiesta inviata. Grazie.';form.reset();return;}}
          if(key==='tipi_debito'){{(values[key]||(values[key]=[])).push(value);}}
          else if(key!=='website') values[key]=value;
        }}
        values.access_key='6cfd757e-6b16-49e0-96b5-e055a159f403';
        values.botcheck='';
        button.disabled=true;button.textContent='Invio in corso…';
        try{{
          const response=await fetch('https://api.web3forms.com/submit',{{method:'POST',headers:{{'Content-Type':'application/json','Accept':'application/json'}},body:JSON.stringify(values)}});
          const data=await response.json();
          if(!response.ok||data.success!==true)throw new Error(data.message||'Non è stato possibile inviare la richiesta. Riprova più tardi.');
          result.className='notice success';
          result.textContent='Richiesta inviata correttamente. Grazie. FixTude potrà ricontattarti usando i recapiti indicati.';
          form.reset();
        }}catch(err){{result.className='notice error';result.textContent=err.message||'Errore di connessione. Riprova più tardi.';}}
        finally{{button.disabled=false;button.textContent='Invia la richiesta';}}
      }});
    }})();
    </script>'''
    return page(titolo, content)


# =========================================================
# CALCOLATORE
# =========================================================

@app.route("/misuratore")
def misuratore():
    scenario = (request.args.get("scenario", "verde") or "verde").strip().lower()
    if scenario not in ("rosso", "giallo", "verde"):
        scenario = "verde"
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
      /* Calcolatore guidato: sette schermate orizzontali */
      .calc-wizard{margin:18px 0 28px;min-width:0}
      .calc-progress{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:0 0 12px;font-size:14px;color:var(--muted,#64748b)}
      .calc-progress-track{height:6px;border-radius:999px;background:#e5eaf1;overflow:hidden;margin-bottom:18px}
      .calc-progress-fill{height:100%;width:20%;border-radius:999px;background:var(--blue2,#2563eb);transition:width .25s ease}
      .calc-viewport{overflow:hidden;width:100%;height:auto;border-radius:16px;touch-action:pan-y;transition:height .2s ease}
      .calc-track{display:flex;align-items:flex-start;width:100%;min-width:100%;transition:transform .32s ease;will-change:transform;transform:translate3d(0,0,0)}
      .calc-panel{flex:0 0 100%;width:100%;min-width:100%;max-width:100%;box-sizing:border-box;padding:1px 2px 8px}
      .calc-panel>.card{height:auto;min-height:0;box-sizing:border-box;margin:0}
      .calc-nav{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:16px;padding:14px 0 4px;border-top:1px solid var(--line,#e2e8f0)}
      .calc-nav .btn{display:inline-flex!important;visibility:visible!important;opacity:1!important;cursor:pointer;min-height:44px;position:relative;z-index:5}
      .calc-nav-global{width:100%;min-height:60px}
      .calc-nav .calc-back,.calc-nav .calc-next{background:#155eef!important;color:#fff!important;border:1px solid #155eef!important;border-radius:10px;font-weight:700;box-shadow:0 3px 0 #0b3eaa;}
      .calc-nav .calc-back:hover,.calc-nav .calc-next:hover{background:#0b4ed0!important;color:#fff!important;filter:brightness(1.04)}
      .calc-nav .calc-back:active,.calc-nav .calc-next:active{transform:translateY(2px);box-shadow:0 1px 0 #0b3eaa}
      .calc-nav .calc-back[hidden]{visibility:hidden!important;display:inline-flex!important;opacity:0!important;pointer-events:none}
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
        if (cards.length !== 7) {
          console.error("FixTude: il calcolatore richiede 7 sezioni; sezioni trovate:", cards.length);
          return;
        }

        const wizard = document.createElement("div");
        wizard.className = "calc-wizard";
        wizard.setAttribute("aria-label", "Calcolatore guidato a scorrimento orizzontale in sette passaggi");
        const progress = document.createElement("div");
        progress.className = "calc-progress";
        const progressLabel = document.createElement("strong");
        progressLabel.textContent = "Passaggio 1 di 7 · Entrate mensili";
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

        // Un modulo per passaggio: nessun gruppo finale enorme o vuoto.
        const titles = ["Entrate mensili", "Spese mensili", "Debiti e rate", "Il tuo esito", "Piano di rientro", "Saldo e stralcio", "Prossimi passi"];
        const totalSteps = cards.length;
        let currentStep = 0;
        const panels = cards.map((card, index) => {
          const panel = document.createElement("section");
          panel.className = "calc-panel";
          panel.setAttribute("role", "group");
          panel.setAttribute("aria-label", "Passaggio " + (index + 1) + " di " + totalSteps + ": " + titles[index]);
          panel.appendChild(card);
          track.appendChild(panel);
          return panel;
        });

        // Navigazione unica, esterna ai pannelli: resta visibile anche su mobile.
        const nav = document.createElement("div");
        nav.className = "calc-nav calc-nav-global";
        const back = document.createElement("button");
        back.type = "button";
        back.className = "btn calc-back";
        back.textContent = "← Indietro";
        back.setAttribute("aria-label", "Torna al passaggio precedente");
        back.addEventListener("click", () => showStep(currentStep - 1));
        const caption = document.createElement("span");
        caption.className = "calc-step-caption";
        caption.setAttribute("aria-live", "polite");
        caption.textContent = "1 / " + totalSteps;
        const next = document.createElement("button");
        next.type = "button";
        next.className = "btn calc-next";
        next.textContent = "Avanti →";
        next.setAttribute("aria-label", "Vai al passaggio successivo");
        next.addEventListener("click", () => {
          if (currentStep < totalSteps - 1) showStep(currentStep + 1);
          else showStep(0);
        });
        nav.append(back, caption, next);
        wizard.appendChild(nav);

        function showStep(step) {
          currentStep = Math.max(0, Math.min(totalSteps - 1, step));
          // Usa pixel reali del viewport: evita salti di pannello dovuti alle percentuali CSS.
          track.style.transform = "translate3d(" + (-currentStep * viewport.clientWidth) + "px, 0, 0)";
          viewport.style.height = panels[currentStep].scrollHeight + "px";
          caption.textContent = (currentStep + 1) + " / " + totalSteps;
          back.disabled = currentStep === 0;
          back.style.visibility = currentStep === 0 ? "hidden" : "visible";
          next.textContent = currentStep === totalSteps - 1 ? "Torna all'inizio ↑" : "Avanti →";
          progressLabel.textContent = "Passaggio " + (currentStep + 1) + " di " + totalSteps + " · " + titles[currentStep];
          progressFill.style.width = (((currentStep + 1) / totalSteps) * 100) + "%";
          panels.forEach((panel, i) => {
            panel.setAttribute("aria-hidden", i === currentStep ? "false" : "true");
            panel.inert = i !== currentStep;
          });
          if (currentStep >= 3) {
            const lumpSum = document.getElementById("lumpSum");
            if (lumpSum) lumpSum.dispatchEvent(new Event("input", { bubbles: true }));
          }
        }
        showStep(0);
        window.addEventListener("resize", () => {
          track.style.transition = "none";
          track.style.transform = "translate3d(" + (-currentStep * viewport.clientWidth) + "px, 0, 0)";
          viewport.style.height = panels[currentStep].scrollHeight + "px";
          requestAnimationFrame(() => { track.style.transition = ""; });
        });
        // Gli elementi dinamici (righe aggiunte/rimosse) aggiornano l'altezza del pannello.
        const resizeObserver = new ResizeObserver(() => {
          const activePanel = panels[currentStep];
          if (activePanel) viewport.style.height = activePanel.scrollHeight + "px";
        });
        panels.forEach(panel => resizeObserver.observe(panel));
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
        row.appendChild(nameField);
        if (type === "debt") {
          const typeField = document.createElement("div");
          typeField.className = "field debt-type-field";
          const typeLabel = document.createElement("label");
          typeLabel.textContent = "Tipo di debito";
          typeField.appendChild(typeLabel);
          const typeSelect = document.createElement("select");
          typeSelect.setAttribute("aria-label", "Tipo di debito");
          typeSelect.innerHTML = '<option value="Utenze e servizi">Utenze e servizi</option><option value="Prestiti e finanziamenti">Prestiti e finanziamenti</option><option value="Carte e conto corrente">Carte e conto corrente</option><option value="Mutuo">Mutuo</option><option value="Affitto e condominio">Affitto e condominio</option><option value="Debiti fiscali e tributi">Debiti fiscali e tributi</option><option value="Debiti verso privati">Debiti verso privati</option><option value="Altro">Altro</option>';
          typeField.appendChild(typeSelect);
          row.appendChild(typeField);
        }
        row.appendChild(primary.field);
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
    # Il percorso resta sempre il wizard originale in sette passaggi. I campi specifici
    # rosso/giallo sono inseriti nella terza pagina, senza creare schermate aggiuntive.
    scenario_intro = {
        "rosso": "EMERGENZA · Analisi guidata in 7 passaggi. Oltre al bilancio, puoi indicare gli atti ricevuti e le spese richieste.",
        "giallo": "ALLERTA · Analisi guidata in 7 passaggi. Oltre al bilancio, puoi indicare le comunicazioni ricevute e le spese richieste.",
        "verde": "CONTROLLO · Analisi guidata in 7 passaggi per verificare il margine mensile e la sostenibilità dei debiti."
    }[scenario]
    scenario_fields = ""
    if scenario == "rosso":
        scenario_fields = """
        <div class="scenario-extra scenario-red" aria-label="Atti e procedure">
          <h3>Atti e procedure ricevuti</h3>
          <p>Seleziona gli atti presenti e inserisci l'importo richiesto, se disponibile. Puoi aggiungere più voci. Non caricare documenti.</p>
          <div id="scenarioActRows"></div>
          <button class="btn outline" type="button" id="scenarioAddAct">+ Aggiungi un altro atto</button>
          <div class="field"><label for="scenarioTotal">Importo totale attualmente richiesto (€)</label><input id="scenarioTotal" type="number" min="0" step="0.01" value="0"></div>
          <div class="field"><label for="scenarioRecoveryIncluded">Le spese di recupero sono già incluse nell'importo richiesto?</label><select id="scenarioRecoveryIncluded"><option value="unknown">Non so / non è chiaro</option><option value="yes">Sì</option><option value="no">No</option></select></div>
          <div class="field"><label for="scenarioRecoveryAmount">Importo spese di recupero, se distinto (€)</label><input id="scenarioRecoveryAmount" type="number" min="0" step="0.01" value="0"></div>
          <div class="field"><label for="scenarioLegalRequested">Sono richieste spese legali?</label><select id="scenarioLegalRequested"><option value="unknown">Non so / non è chiaro</option><option value="yes">Sì</option><option value="no">No</option></select></div>
          <div class="field"><label for="scenarioLegalAmount">Importo spese legali, se indicato (€)</label><input id="scenarioLegalAmount" type="number" min="0" step="0.01" value="0"></div>
          <p class="muted" id="scenarioExtraSummary" aria-live="polite">I dati aggiuntivi sono elaborati in questa pagina e non vengono salvati.</p>
        </div>"""
    elif scenario == "giallo":
        scenario_fields = """
        <div class="scenario-extra scenario-yellow" aria-label="Comunicazioni ricevute">
          <h3>Comunicazioni ricevute</h3>
          <p>Indica ciò che hai ricevuto, anche più di una comunicazione. Non caricare documenti.</p>
          <div class="field"><label for="scenarioSolleciti">Solleciti di pagamento</label><select id="scenarioSolleciti"><option value="no">Non ricevuti</option><option value="yes">Ricevuti</option></select></div>
          <div class="field"><label for="scenarioSollecitiCount">Numero di solleciti</label><input id="scenarioSollecitiCount" type="number" min="0" step="1" value="0"></div>
          <div class="field"><label for="scenarioSollecitiAmount">Importo richiesto nei solleciti (€)</label><input id="scenarioSollecitiAmount" type="number" min="0" step="0.01" value="0"></div>
          <div class="field"><label for="scenarioPreavviso">Preavviso di costituzione in mora</label><select id="scenarioPreavviso"><option value="no">Non ricevuto</option><option value="yes">Ricevuto</option></select></div>
          <div class="field"><label for="scenarioPreavvisoAmount">Importo richiesto (€)</label><input id="scenarioPreavvisoAmount" type="number" min="0" step="0.01" value="0"></div>
          <div class="field"><label for="scenarioMora">Costituzione in mora</label><select id="scenarioMora"><option value="no">Non ricevuta</option><option value="yes">Ricevuta</option></select></div>
          <div class="field"><label for="scenarioMoraAmount">Importo richiesto (€)</label><input id="scenarioMoraAmount" type="number" min="0" step="0.01" value="0"></div>
          <div class="field"><label for="scenarioDiffida">Diffida legale</label><select id="scenarioDiffida"><option value="no">Non ricevuta</option><option value="yes">Ricevuta</option></select></div>
          <div class="field"><label for="scenarioDiffidaAmount">Importo richiesto nella diffida (€)</label><input id="scenarioDiffidaAmount" type="number" min="0" step="0.01" value="0"></div>
          <div class="field"><label for="scenarioTotal">Importo totale attualmente richiesto (€)</label><input id="scenarioTotal" type="number" min="0" step="0.01" value="0"></div>
          <div class="field"><label for="scenarioRecoveryIncluded">Le spese di recupero sono già incluse nell'importo richiesto?</label><select id="scenarioRecoveryIncluded"><option value="unknown">Non so / non è chiaro</option><option value="yes">Sì</option><option value="no">No</option></select></div>
          <div class="field"><label for="scenarioRecoveryAmount">Importo spese di recupero, se distinto (€)</label><input id="scenarioRecoveryAmount" type="number" min="0" step="0.01" value="0"></div>
          <div class="field"><label for="scenarioLegalRequested">Sono richieste spese legali?</label><select id="scenarioLegalRequested"><option value="unknown">Non so / non è chiaro</option><option value="yes">Sì</option><option value="no">No</option></select></div>
          <div class="field"><label for="scenarioLegalAmount">Importo spese legali, se indicato (€)</label><input id="scenarioLegalAmount" type="number" min="0" step="0.01" value="0"></div>
          <p class="muted" id="scenarioExtraSummary" aria-live="polite">I dati aggiuntivi sono elaborati in questa pagina e non vengono salvati.</p>
        </div>"""
    content = content.replace('<div id="debtRows"></div>', '<div id="debtRows"></div>\n' + scenario_fields)
    # Add visual identity to the existing seven-page wizard; green is explicitly styled too.
    theme_css = """
    <style>
      html[data-calc-scenario="rosso"] { --calc-accent:#b4232f; --calc-accent-soft:#fff0f1; --calc-accent-dark:#781923; }
      html[data-calc-scenario="giallo"] { --calc-accent:#b77900; --calc-accent-soft:#fff8e6; --calc-accent-dark:#704700; }
      html[data-calc-scenario="verde"] { --calc-accent:#16834a; --calc-accent-soft:#effaf3; --calc-accent-dark:#075c31; }
      html[data-calc-scenario] .calc-progress-fill { background:var(--calc-accent)!important; }
      html[data-calc-scenario] .calc-progress-track { background:var(--calc-accent-soft)!important; }
      html[data-calc-scenario] .calc-nav .calc-back, html[data-calc-scenario] .calc-nav .calc-next { background:var(--calc-accent)!important;border-color:var(--calc-accent)!important;box-shadow:0 3px 0 var(--calc-accent-dark)!important;color:#fff!important; }
      html[data-calc-scenario] .calc-panel>.card { border-top:5px solid var(--calc-accent)!important; }
      html[data-calc-scenario] .calc-panel>.card h2 { color:var(--calc-accent-dark)!important; }
      html[data-calc-scenario] .calc-progress strong { color:var(--calc-accent-dark)!important; }
      html[data-calc-scenario] .scenario-intro { padding:14px 16px;border-left:6px solid var(--calc-accent);border-radius:10px;background:var(--calc-accent-soft);color:var(--calc-accent-dark);font-weight:750;line-height:1.5; }
      html[data-calc-scenario="verde"] .calc-panel>.card { background:linear-gradient(180deg,var(--calc-accent-soft) 0, #fff 90px)!important; }
      html[data-calc-scenario="rosso"] .calc-panel>.card { background:linear-gradient(180deg,#fff5f5 0, #fff 90px)!important; }
      html[data-calc-scenario="giallo"] .calc-panel>.card { background:linear-gradient(180deg,#fffaf0 0, #fff 90px)!important; }
      .scenario-extra { margin-top:20px;padding:18px;border:1px solid var(--calc-accent);border-left:6px solid var(--calc-accent);border-radius:14px;background:var(--calc-accent-soft); }
      .scenario-extra h3 { margin:0 0 8px;color:var(--calc-accent-dark);font-size:19px; }
      .scenario-extra>p { line-height:1.5; }
      .scenario-extra .field { margin-top:12px; }
      .scenario-extra .field input,.scenario-extra .field select { width:100%;box-sizing:border-box; }
      .scenario-extra .btn { margin-top:10px; }
    </style>
    <script>document.documentElement.dataset.calcScenario = '""" + scenario + """';</script>
    """
    scenario_title = {"rosso": "Emergenza", "giallo": "Allerta", "verde": "Controllo"}[scenario]
    content = content.replace('<h1>Calcolatore di sostenibilità del debito</h1>', '<h1>Calcolatore di sostenibilità del debito · ' + scenario_title + '</h1><p class="scenario-intro">' + scenario_intro + '</p>')
    content = content.replace("    <script>\n    (() => {\n      // Inizializza il wizard", theme_css + "\n    <script>\n    (() => {\n      // Inizializza il wizard")
    # Browser-only interactions for the extra fields; never post or persist entered values.
    extra_js = """
    <script>
    (() => {
      const root=document.querySelector('.scenario-extra'); if(!root)return;
      const euro=n=>Number(n||0).toLocaleString('it-IT',{style:'currency',currency:'EUR'});
      const money=id=>{const e=document.getElementById(id);return e?Math.max(0,Number(e.value)||0):0;};
      const update=()=>{
        const out=document.getElementById('scenarioExtraSummary'); if(!out)return;
        const total=money('scenarioTotal'),rec=money('scenarioRecoveryAmount'),leg=money('scenarioLegalAmount');
        const lines=['Totale richiesto indicato: '+euro(total), 'Spese di recupero distinte: '+euro(rec), 'Spese legali distinte: '+euro(leg)];
        if(document.getElementById('scenarioActRows')){
          const acts=[...root.querySelectorAll('.scenario-act-row')].map(r=>({type:r.querySelector('select').value,amount:Math.max(0,Number(r.querySelector('input').value)||0)})).filter(a=>a.type!=='Nessun atto');
          lines.unshift(acts.length?'Atti indicati: '+acts.map(a=>a.type+' ('+euro(a.amount)+')').join('; '):'Nessun atto selezionato.');
        } else {
          const names=[['Solleciti di pagamento','scenarioSolleciti','scenarioSollecitiAmount'],['Preavviso di costituzione in mora','scenarioPreavviso','scenarioPreavvisoAmount'],['Costituzione in mora','scenarioMora','scenarioMoraAmount'],['Diffida legale','scenarioDiffida','scenarioDiffidaAmount']];
          const selected=names.filter(a=>document.getElementById(a[1])?.value==='yes').map(a=>a[0]+' ('+euro(money(a[2]))+')');
          lines.unshift(selected.length?'Comunicazioni indicate: '+selected.join('; '):'Nessuna comunicazione selezionata.');
          if(document.getElementById('scenarioSolleciti')?.value==='yes') lines.push('Numero di solleciti: '+Math.floor(money('scenarioSollecitiCount'))+'.');
        }
        lines.push('Spese di recupero: '+({'yes':'già incluse','no':'non incluse','unknown':'non chiaramente identificate'}[document.getElementById('scenarioRecoveryIncluded')?.value||'unknown'])+'.');
        lines.push('Spese legali: '+({'yes':'indicate come richieste','no':'non indicate','unknown':'non chiaramente identificate'}[document.getElementById('scenarioLegalRequested')?.value||'unknown'])+'.');
        out.textContent=lines.join(' ');
      };
      const addAct=()=>{const host=document.getElementById('scenarioActRows'),row=document.createElement('div');row.className='scenario-act-row field';const s=document.createElement('select');['Nessun atto','Atto di messa in mora / intimazione','Decreto ingiuntivo','Atto di precetto','Pignoramento presso terzi','Pignoramento mobiliare','Pignoramento immobiliare','Altro atto giudiziario'].forEach(t=>{const o=document.createElement('option');o.value=t;o.textContent=t;s.appendChild(o)});const i=document.createElement('input');i.type='number';i.min='0';i.step='0.01';i.value='0';i.setAttribute('aria-label','Importo richiesto per questo atto');const label=document.createElement('label');label.textContent='Tipo di atto';const amountLabel=document.createElement('label');amountLabel.textContent='Importo richiesto (€)';const remove=document.createElement('button');remove.type='button';remove.className='btn outline';remove.textContent='Rimuovi';remove.onclick=()=>{row.remove();if(!host.children.length)addAct();update()};row.append(label,s,amountLabel,i,remove);host.appendChild(row);s.addEventListener('change',update);i.addEventListener('input',update);};
      document.getElementById('scenarioAddAct')?.addEventListener('click',addAct);
      if(document.getElementById('scenarioActRows'))addAct();
      root.querySelectorAll('input,select').forEach(e=>e.addEventListener('input',update));root.querySelectorAll('select').forEach(e=>e.addEventListener('change',update));
      document.getElementById('resetCalculator')?.addEventListener('click',()=>{setTimeout(()=>{root.querySelectorAll('input').forEach(e=>{if(e.type==='number')e.value='0'});root.querySelectorAll('select').forEach(e=>e.selectedIndex=0);if(document.getElementById('scenarioActRows')){document.getElementById('scenarioActRows').replaceChildren();addAct()}update()},0)});
      update();
    })();
    </script>
    """
    content += extra_js
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

# I PDF sono attualmente organizzati nella cartella static/pdf del progetto.
# ATTENZIONE: i file in static/ sono raggiungibili anche tramite URL diretto.
# Questa configurazione mantiene la struttura attuale; per proteggere i prodotti
# a pagamento dovremo poi spostare i PDF completi fuori da static/.
PDF_DIR = BASE_DIR / "static" / "pdf"
BOOK_FILE = PDF_DIR / "NIENTE_PANICO.pdf"
PREVIEW_FILE = PDF_DIR / "NIENTE_PANICO_ANTEPRIMA.pdf"
GUIDE_FILE = PDF_DIR / "FixTude_Guida_Saldo_e_Stralcio_EDIZIONE_RIVISTA_SEO.pdf"
GUIDE_PRODUCT_ID = "guida_saldo_stralcio_pdf"
GUIDE_PRICE_CENTS = 699

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
    <section class="card education-article-card" aria-labelledby="guide-stipendio">
      <a class="education-article-image-link" href="/educazione-finanziaria/come-gestire-lo-stipendio-regola-un-terzo" aria-label="Leggi la guida: Come gestire le entrate: la regola di un terzo">
        <img class="education-article-image" src="/static/articolo-entrate-budget.png" alt="Infografica per organizzare le entrate e il bilancio personale con la regola di un terzo" loading="lazy">
      </a>
      <div class="education-article-copy">
        <p class="muted">GUIDA PRATICA · 7 MINUTI DI LETTURA</p>
        <h2 id="guide-stipendio"><a href="/educazione-finanziaria/come-gestire-lo-stipendio-regola-un-terzo">Come gestire le entrate: la regola di un terzo</a></h2>
        <p>Un metodo semplice per osservare come si distribuisce il reddito tra spese essenziali, vita quotidiana e obiettivi finanziari. Con esempi numerici e indicazioni su come adattare le proporzioni alla propria situazione.</p>
        <a class="btn" href="/educazione-finanziaria/come-gestire-lo-stipendio-regola-un-terzo">Leggi la guida</a>
      </div>
    </section>
    <section class="card education-article-card" aria-labelledby="guide-saldo-stralcio">
      <a class="education-article-image-link" href="/educazione-finanziaria/guida-saldo-e-stralcio" aria-label="Leggi la guida sul saldo e stralcio">
        <img class="education-article-image" src="/static/articolo-saldo-stralcio.png" alt="Saldo e stralcio: debiti, cartelle e accordo per una possibile chiusura bonaria" loading="lazy">
      </a>
      <div class="education-article-copy">
        <p class="muted">GUIDA PRATICA · 8 MINUTI DI LETTURA</p>
        <h2 id="guide-saldo-stralcio"><a href="/educazione-finanziaria/guida-saldo-e-stralcio">Saldo e stralcio: cos'è, come funziona e quando conviene</a></h2>
        <p>Come valutare una proposta di chiusura a saldo e stralcio, quali documenti controllare, cosa pretendere nell'accordo scritto e quali errori evitare prima di effettuare il pagamento.</p>
        <a class="btn" href="/educazione-finanziaria/guida-saldo-e-stralcio">Leggi l'articolo</a>
      </div>
    </section>
    <style>
      .education-article-card{display:grid;grid-template-columns:minmax(150px,230px) minmax(0,1fr);gap:20px;align-items:center}
      .education-article-image-link{display:block;max-width:230px}
      .education-article-image{display:block;width:100%;height:auto;max-height:190px;object-fit:cover;border-radius:12px}
      .education-article-copy h2{margin-top:6px}
      @media(max-width:600px){.education-article-card{grid-template-columns:1fr;gap:12px}.education-article-image-link{width:100%;max-width:100%}.education-article-image{width:100%;max-height:210px;object-fit:contain;object-position:left center}.education-article-copy h2{font-size:1.25rem}}
    </style>
    <section class="card" id="bilancio"><h2>Organizzare il bilancio familiare</h2><p>Annota le entrate nette effettivamente disponibili e separa le spese essenziali (casa, utenze, alimentazione, salute e trasporti) dalle spese flessibili. Inserisci poi rate, arretrati e scadenze. Il margine mensile è una stima: lascia un cuscinetto per gli imprevisti e non considerare sostenibile una rata che mette a rischio le necessità di base.</p><ul><li>Usa importi mensili omogenei.</li><li>Evita di contare entrate incerte come se fossero garantite.</li><li>Aggiorna il prospetto quando cambiano reddito o spese.</li></ul><p><a href="/misuratore">Prova il calcolatore gratuito di sostenibilità del debito</a>.</p></section>
    <section class="card" id="priorita"><h2>Dare priorità ai pagamenti</h2><p>Prima di distribuire il denaro tra i debiti, proteggi le necessità essenziali e valuta le scadenze concrete. Non esiste una priorità universale valida per ogni situazione: atti giudiziari, garanzie, utenze e debiti fiscali possono avere conseguenze differenti. Se hai ricevuto un atto con termini, chiedi tempestivamente una valutazione qualificata.</p><ul><li>Apri e conserva le comunicazioni ricevute.</li><li>Segna importo, creditore, scadenza e riferimento della pratica.</li><li>Non ignorare raccomandate, PEC o atti notificati.</li></ul></section>
    <section class="card" id="creditori"><h2>Parlare con i creditori</h2><p>Una richiesta efficace è chiara, documentata e realistica. Indica il riferimento del rapporto, spiega brevemente la difficoltà, proponi un importo compatibile con il bilancio e chiedi conferma scritta delle condizioni. Non promettere pagamenti che non puoi sostenere e non considerare accettata una proposta finché non hai ricevuto una conferma verificabile.</p><p>Puoi partire dai <a href="/moduli">moduli gratuiti FixTude</a> e personalizzarli.</p></section>
    <section class="card" id="errori"><h2>Errori da evitare</h2><ul><li>Pagare intermediari che promettono cancellazioni o risultati garantiti.</li><li>Firmare accordi senza aver letto importi, scadenze e conseguenze.</li><li>Inviare documenti personali a destinatari non verificati.</li><li>Contrarre nuovo debito senza valutare il costo totale e la sostenibilità.</li><li>Rimandare la richiesta di aiuto quando arrivano atti con termini precisi.</li></ul><p>Le informazioni di questa pagina sono generali e non sostituiscono una consulenza professionale.</p></section>'''
    return page("Educazione finanziaria", content)


@app.route("/educazione-finanziaria/come-gestire-lo-stipendio-regola-un-terzo")
def articolo_gestione_stipendio():
    content = '''<article class="card article-content">
      <p class="muted">EDUCAZIONE FINANZIARIA · 7 MINUTI DI LETTURA</p>
      <h1>Come gestire le entrate: la regola di un terzo</h1>
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

@app.route("/educazione-finanziaria/guida-saldo-e-stralcio")
def articolo_saldo_e_stralcio():
    content = '''<article class="card article-content">
      <p class="muted">EDUCAZIONE FINANZIARIA · 8 MINUTI DI LETTURA</p>
      <h1>Saldo e stralcio: cos'è, come funziona e quando conviene</h1>
      <figure class="article-hero-image"><img src="/static/articolo-saldo-stralcio.png" alt="Saldo e stralcio: debiti, cartelle e accordo per una possibile chiusura bonaria" loading="eager" fetchpriority="high"><figcaption>Il saldo e stralcio può consentire una chiusura concordata, ma ogni proposta va verificata con attenzione.</figcaption></figure>
      <p class="article-subtitle">Ridurre un debito attraverso un accordo è possibile in alcuni casi, ma non esistono sconti automatici né percentuali garantite. La differenza la fanno la situazione concreta, il soggetto che vanta il credito e le condizioni scritte dell'intesa.</p>
      <p class="muted">Pubblicato il 20 ottobre 2026 · Aggiornato il 20 ottobre 2026</p>
      <p>Quando arrivano solleciti, telefonate o comunicazioni di recupero, l'idea di chiudere una posizione pagando una somma inferiore al debito richiesto può sembrare una via d'uscita. È questo, in sintesi, il principio del <strong>saldo e stralcio</strong>: il creditore accetta una somma concordata a definizione della posizione, secondo condizioni esplicite e verificabili. Ma attenzione: non basta proporre uno sconto e non è sufficiente ricevere un messaggio informale. Occorre capire chi può accettare la proposta, quale debito si sta definendo e quali effetti avrà il pagamento.</p>
      <h2>Che cos'è il saldo e stralcio</h2>
      <p>Con l'espressione saldo e stralcio si indica comunemente un accordo con cui creditore e debitore concordano il pagamento di una somma inferiore a quella complessivamente richiesta, in cambio della definizione del debito alle condizioni stabilite. Può riguardare, a seconda del caso e della disponibilità del creditore, debiti verso banche, finanziarie, fornitori o altri soggetti privati. La valutazione è individuale: il creditore può rifiutare la proposta, formulare una controproposta oppure accettarla con scadenze e modalità specifiche.</p>
      <p>Non è una procedura che il debitore possa imporre unilateralmente. Non esiste una regola generale per cui basti offrire il 30%, il 50% o un'altra percentuale perché la posizione venga chiusa. La somma eventualmente accettata dipende, tra l'altro, dalla documentazione, dalla fase di recupero, dalle risorse disponibili e dalle politiche del creditore.</p>
      <h2>Quando può essere utile valutare una trattativa</h2>
      <p>Una proposta può avere senso quando il debitore dispone di una somma realmente accessibile, magari dopo aver ricostruito il bilancio familiare o aver ricevuto un aiuto documentabile, e quando il creditore è disposto a valutare una definizione concordata. È fondamentale non promettere denaro che non si possiede e non utilizzare nuovo credito costoso senza averne valutato conseguenze e sostenibilità.</p>
      <p>Prima di avviare una trattativa, ricostruisci la situazione: importo richiesto, eventuali pagamenti già effettuati, interessi e spese indicati, riferimenti contrattuali, comunicazioni ricevute e stato della pratica. Se sono presenti atti giudiziari, termini perentori, pignoramenti o altre iniziative legali, non aspettare che una trattativa informale risolva automaticamente la questione: chiedi una valutazione qualificata in tempi utili.</p>
      <h2>Il primo controllo: chi sta chiedendo il pagamento?</h2>
      <p>La società che telefona o invia una lettera potrebbe essere il creditore originario, un incaricato del recupero oppure un soggetto che dichiara di aver acquistato il credito. Queste situazioni non sono necessariamente equivalenti. Prima di pagare, verifica l'identità del soggetto, il riferimento della posizione e il titolo in base al quale interviene. Se la proposta proviene da un incaricato, accertati che abbia i poteri necessari per formulare e confermare l'accordo.</p>
      <p>Non condividere dati bancari o documenti personali con interlocutori non verificati. Se hai dubbi, utilizza canali ufficiali e recapiti reperiti autonomamente, non soltanto quelli contenuti in un messaggio inatteso. Conserva lettere, email, PEC, ricevute e ogni documento relativo alla trattativa.</p>
      <h2>Come preparare una proposta sostenibile</h2>
      <p>La proposta deve partire dalle possibilità economiche effettive. Calcola le entrate nette, sottrai le spese necessarie, le rate già in corso e le uscite irregolari prevedibili. Mantieni un margine per gli imprevisti: impegnare tutto il denaro disponibile può creare un nuovo problema appena arriva una bolletta o una spesa sanitaria.</p>
      <p>Una proposta credibile è chiara e concreta. Può indicare la somma che si è in grado di pagare, la disponibilità entro una certa data e l'eventuale esigenza di una breve rateizzazione. Evita dichiarazioni non vere o promesse che non potrai mantenere. Se il denaro è disponibile soltanto a una certa condizione, non presentarlo come già garantito.</p>
      <h2>L'accordo scritto: cosa deve chiarire</h2>
      <p>Il punto decisivo non è soltanto l'importo scontato, ma il significato giuridico del pagamento. Prima di versare la somma, leggi l'accordo e verifica che identifichi chiaramente:</p>
      <ul>
        <li>il creditore o il soggetto legittimato ad accettare e il riferimento della posizione;</li>
        <li>l'importo complessivo oggetto dell'accordo e la somma concordata;</li>
        <li>la scadenza, il metodo di pagamento e l'eventuale calendario delle rate;</li>
        <li>le condizioni in cui il pagamento determina la definizione della posizione;</li>
        <li>il trattamento dell'eventuale residuo e gli impegni assunti dal creditore;</li>
        <li>la gestione di eventuali coobbligati o garanti, se coinvolti, e gli effetti previsti nei loro confronti.</li>
      </ul>
      <p>Le formule generiche come “pratica in lavorazione” o “importo accettato” non sostituiscono una conferma completa degli effetti dell'accordo. La quietanza del pagamento dimostra che una somma è stata versata, ma non sempre chiarisce da sola che ogni residuo sia estinto. Per questo è importante leggere l'intero documento e, quando la situazione è complessa, sottoporlo a un professionista prima di firmare o pagare.</p>
      <h2>Un esempio numerico, senza promesse di sconto</h2>
      <p>Immaginiamo che la posizione riporti un importo di 5.000 euro e che il debitore possa mettere a disposizione 2.000 euro senza compromettere le spese essenziali. Potrebbe proporre quella cifra come definizione a saldo e stralcio, ma il creditore non è obbligato ad accettarla. Potrebbe rifiutare, chiedere una cifra diversa oppure proporre un piano di rientro. Solo se viene raggiunto un accordo valido e sufficientemente preciso si potrà procedere secondo le condizioni concordate.</p>
      <p>L'esempio serve a illustrare il meccanismo, non a suggerire che una proposta del 40% sia normalmente accettata. Ogni situazione è diversa e l'esito non può essere garantito in anticipo.</p>
      <h2>Saldo e stralcio o piano di rientro?</h2>
      <p>Un saldo e stralcio richiede in genere una disponibilità iniziale, mentre un piano di rientro distribuisce il pagamento nel tempo e può comportare il pagamento di una quota più ampia del debito. La scelta dipende dalla liquidità reale, dalla stabilità delle entrate, dalle condizioni offerte e dalle conseguenze del mancato rispetto degli impegni. Una rata apparentemente contenuta può diventare insostenibile se il bilancio è già in deficit.</p>
      <p>Non confrontare le alternative soltanto in base alla cifra mensile. Valuta il totale da pagare, le scadenze, gli eventuali costi, cosa accade se salti una rata e quali obblighi restano in capo alle parti. Non esiste una soluzione migliore per tutti.</p>
      <h2>Gli errori da evitare prima di pagare</h2>
      <ul>
        <li><strong>Pagare sulla base di un accordo solo telefonico:</strong> chiedi una conferma scritta, completa e verificabile.</li>
        <li><strong>Non controllare l'interlocutore:</strong> verifica chi è il creditore e chi può impegnarlo.</li>
        <li><strong>Confondere ricevuta e liberatoria:</strong> conserva entrambe e controlla cosa attestano.</li>
        <li><strong>Impegnare tutto il risparmio:</strong> proteggi le spese essenziali e un margine per gli imprevisti.</li>
        <li><strong>Ignorare atti e scadenze:</strong> una trattativa non sospende automaticamente termini o procedure.</li>
        <li><strong>Dare per scontata la posizione di garanti e coobbligati:</strong> verifica espressamente gli effetti dell'accordo per ogni soggetto coinvolto.</li>
      </ul>
      <h2>Una checklist prima di accettare</h2>
      <div class="checklist-box"><p>Prima di effettuare il pagamento, controlla di poter rispondere sì a queste domande:</p><ul><li>Ho verificato l'identità del creditore o i poteri del suo incaricato?</li><li>Il documento identifica senza ambiguità la posizione da definire?</li><li>La somma e le scadenze sono scritte in modo preciso?</li><li>È chiaro che cosa succede al residuo dopo il pagamento concordato?</li><li>Ho verificato l'eventuale posizione di garanti o coobbligati?</li><li>Posso rispettare l'accordo senza compromettere le necessità fondamentali?</li><li>Ho valutato eventuali atti legali o termini in corso?</li></ul></div>
      <h2>In conclusione</h2>
      <p>Il saldo e stralcio può rappresentare un'opportunità di definizione concordata, ma non è uno sconto automatico e non elimina la necessità di verificare i documenti. Prima si ricostruisce la posizione, poi si valuta una proposta sostenibile; infine si controlla l'accordo scritto prima di pagare. Se ci sono procedure giudiziarie, più debitori, garanti o dubbi sull'importo richiesto, è prudente ottenere assistenza professionale.</p>
      <section class="notice"><strong>Nota informativa</strong><br>Questo articolo ha finalità generali di educazione finanziaria e non costituisce consulenza legale, fiscale o finanziaria personalizzata. Gli effetti di un accordo dipendono dal testo sottoscritto, dai soggetti coinvolti e dalle circostanze concrete. Non sono garantiti sconti, accettazioni o risultati.</section>
      <div class="actions"><a class="btn" href="/shop">Scopri la guida completa FixTude</a><a class="btn outline" href="/misuratore">Calcola il tuo margine mensile</a><a class="btn outline" href="/educazione-finanziaria">Tutte le guide di educazione finanziaria</a></div>
    </article>
    <style>
      .article-content h1{margin-top:0}.article-content h2{margin-top:30px}
      .article-hero-image{margin:18px 0 20px}.article-hero-image img{display:block;width:100%;height:auto;max-height:520px;object-fit:cover;border-radius:14px}.article-hero-image figcaption{font-size:13px;color:#5b6573;margin-top:7px}.article-subtitle{font-size:1.08rem;line-height:1.65;color:#34445b;margin:0 0 18px}
      .checklist-box{padding:16px;background:#f4f8fd;border:1px solid #dce5ef;border-radius:12px}.checklist-box li{margin:7px 0}
    </style>'''
    return page("Saldo e stralcio: guida pratica", content)


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
    "esame_documenti": {
        "name": "FixTude Starter - Orientamento e primo esame documentale",
        "short": "Starter",
        "amount": 4990,
        "display": "49,90",
    },
    "analisi_consulenza": {
        "name": "FixTude Standard - Analisi approfondita e primo colloquio",
        "short": "Standard",
        "amount": 9990,
        "display": "99,90",
    },
}


@app.route("/assistenza-legale")
def assistenza_legale():
    content = """<section class="hero"><h1>Orientamento e Primo esame documentale</h1><p>Due servizi a pagamento per aiutarti a fare ordine nella documentazione e individuare i prossimi passi. Prima dell’acquisto sono indicati prezzo e contenuto del pacchetto; eventuali incarichi legali successivi, se necessari, saranno concordati direttamente con l’avvocato e non sono inclusi nel prezzo.</p></section>
    <div class="grid legal-packages-grid">
      <section class="card legal-package"><div class="package-kicker">PACCHETTO STARTER</div><h2>Starter</h2><div class="price">€ 49,90</div><p><strong>Orientamento e primo esame documentale.</strong> Prima lettura della documentazione fornita, individuazione dei principali punti da verificare e preparazione delle domande utili per il passo successivo.</p><p class="muted">Il servizio non comprende rappresentanza legale, attività giudiziale o garanzia di un particolare risultato. Eventuali incarichi ulteriori e relativi compensi sono separati.</p><form method="post" action="/assistenza-legale/acquista"><input type="hidden" name="package" value="esame_documenti"><label class="check"><input type="checkbox" name="accept_terms" value="yes" required><span>Accetto i <a href="/termini" target="_blank" rel="noopener">Termini e Condizioni</a> e dichiaro di aver letto l’<a href="/privacy" target="_blank" rel="noopener">Informativa Privacy</a>.</span></label><button class="btn" type="submit">Acquista Starter — € 49,90</button></form></section>
      <section class="card legal-package"><div class="package-kicker">PACCHETTO STANDARD</div><h2>Standard</h2><div class="price">€ 99,90</div><p><strong>Analisi approfondita e primo colloquio orientativo.</strong> Esame più approfondito della documentazione e primo colloquio per chiarire gli aspetti da valutare e le possibili opzioni successive.</p><p class="muted">Il servizio non comprende automaticamente incarichi successivi, rappresentanza o attività giudiziale. Ogni eventuale incarico legale e relativo compenso sarà concordato direttamente con il professionista.</p><form method="post" action="/assistenza-legale/acquista"><input type="hidden" name="package" value="analisi_consulenza"><label class="check"><input type="checkbox" name="accept_terms" value="yes" required><span>Accetto i <a href="/termini" target="_blank" rel="noopener">Termini e Condizioni</a> e dichiaro di aver letto l’<a href="/privacy" target="_blank" rel="noopener">Informativa Privacy</a>.</span></label><button class="btn" type="submit">Acquista Standard — € 99,90</button></form></section>
    </div>
    <section class="card"><h2>Come funzionano i servizi a pagamento</h2><p><strong>Starter (€ 49,90)</strong> è pensato per un primo orientamento e una lettura iniziale dei documenti. <strong>Standard (€ 99,90)</strong> aggiunge un’analisi più approfondita e un primo colloquio orientativo. Il contenuto concreto, le modalità di erogazione e il soggetto responsabile del servizio devono essere confermati nelle condizioni applicabili prima dell’acquisto.</p><p>Questi prezzi non includono eventuali ulteriori incarichi professionali, spese o attività successive. Se sarà necessario un mandato legale, il cliente e l’avvocato lo concorderanno direttamente per iscritto. Nessun esito è garantito.</p><p><a href="/incarico-legale">Consulta il fac-simile di incarico professionale</a> · <a href="/professionisti">Area Professionisti</a></p></section>"""
    return page("Orientamento e Primo esame documentale", content)


@app.route("/assistenza-legale/acquista", methods=["POST"])
def assistenza_legale_acquista():
    if request.form.get("accept_terms") != "yes":
        return page("Conferma necessaria", '<h1>Conferma necessaria</h1><p>Per proseguire devi accettare i Termini e Condizioni e dichiarare di aver letto l’Informativa Privacy.</p><a class="btn" href="/assistenza-legale">Torna ai servizi</a>'), 400
    package_key = request.form.get("package", "").strip()
    pkg = ASSISTENZA_LEGALE_PACCHETTI.get(package_key)
    if not pkg:
        return page("Pacchetto non valido", '<h1>Pacchetto non valido</h1><p>Seleziona uno dei servizi disponibili.</p><a class="btn" href="/assistenza-legale">Torna ai servizi</a>'), 400
    secret_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if stripe is None or not secret_key:
        return page("Pagamento non disponibile", '<h1>Pagamento non disponibile</h1><p>Il pagamento online non è configurato al momento. Non è stato effettuato alcun addebito.</p><a class="btn" href="/assistenza-legale">Torna ai servizi</a>'), 503
    stripe.api_key = secret_key
    try:
        checkout = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price_data": {"currency": "eur", "unit_amount": pkg["amount"], "product_data": {"name": pkg["name"], "description": "Servizio di orientamento secondo le condizioni pubblicate su FixTude"}}, "quantity": 1}],
            success_url=request.host_url.rstrip("/") + "/assistenza-legale/conferma?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=request.host_url.rstrip("/") + "/assistenza-legale?annullato=1",
            metadata={"fixtude_product": "assistenza_legale", "fixtude_package": package_key, "terms_accepted": "yes", "terms_version": "2026-10-19"},
        )
        return redirect(checkout.url, code=303)
    except Exception:
        app.logger.exception("Creazione sessione Stripe assistenza legale non riuscita")
        return page("Pagamento non disponibile", '<h1>Non è stato possibile avviare il pagamento</h1><p>Nessun pagamento è stato confermato. Riprova più tardi o scrivi a info@fixtude.it.</p><a class="btn" href="/assistenza-legale">Torna ai servizi</a>'), 502


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
    <section class="card product-card" aria-labelledby="guida-saldo-title">
      <div class="shop-book-cover"><img src="/static/Guida%20al%20Saldo%20e%20Stralcio%20FixTude.png" alt="Copertina della guida FixTude Saldo e stralcio"></div>
      <div class="shop-book-description">
        <h2 id="guida-saldo-title">GUIDA AL SALDO E STRALCIO</h2>
        <p>Tutto quello che c'è da sapere sul saldo e stralcio: come funziona, quali sono i possibili vantaggi e i limiti di una pratica utilizzata per tentare la chiusura bonaria di una posizione debitoria.</p>
        <p><strong>Formato:</strong> guida digitale PDF, edizione rivista.</p>
        <p class="muted">Un contenuto informativo per comprendere meglio la procedura e prepararsi a valutare la propria situazione. L'accettazione di una proposta dipende sempre dal creditore e dalle circostanze del caso.</p>
        <p class="muted">Puoi leggere gratuitamente l'anteprima del primo capitolo prima di acquistare.</p><p><a class="btn outline" href="/shop/anteprima-saldo-stralcio" target="_blank" rel="noopener">Leggi il primo capitolo gratuitamente</a></p>
        <div class="price">€ 6,99</div>
        <form method="post" action="/shop/acquista-guida"><label class="check"><input type="checkbox" name="accept_terms" value="yes" required><span>Accetto i <a href="/termini" target="_blank" rel="noopener">Termini e Condizioni</a> e dichiaro di aver letto l'<a href="/privacy" target="_blank" rel="noopener">Informativa Privacy</a>.</span></label><button class="btn" type="submit">Acquista la guida PDF</button></form>
      </div>
    </section>
    <section class="card product-card print-coming-soon" aria-labelledby="print-book-title">
      <div class="shop-book-cover"><img src="/static/niente_panico_cover.png" alt="Copertina del libro Niente Panico, prevista anche per l'edizione cartacea"></div>
      <div class="shop-book-description">
        <div class="coming-soon-label">COMING SOON</div>
        <h2 id="print-book-title">NIENTE PANICO — Edizione cartacea</h2>
        <p>Il libro in formato cartaceo sarà disponibile prossimamente, con stampa su richiesta.</p>
        <p class="muted">Prezzo e spedizione saranno comunicati prima dell'apertura degli ordini.</p>
        <div class="quantity-selector" aria-label="Selettore quantità non ancora attivo"><span>Numero di copie</span><div class="quantity-controls"><button type="button" class="btn outline" disabled aria-label="Diminuisci numero di copie">−</button><output aria-live="polite">1</output><button type="button" class="btn outline" disabled aria-label="Aumenta numero di copie">+</button></div></div>
        <button type="button" class="btn coming-soon-button" disabled aria-disabled="true">COMING SOON</button>
      </div>
    </section>
    <section class="card product-card print-coming-soon" aria-labelledby="print-guida-saldo-title">
      <div class="shop-book-cover print-book-mockup"><img src="/static/guida_saldo_stralcio_cartaceo_mockup.png" alt="Mockup illustrativo del libro cartaceo Guida al Saldo e Stralcio FixTude, in arrivo"></div>
      <div class="shop-book-description"><div class="coming-soon-label">COMING SOON</div><h2 id="print-guida-saldo-title">GUIDA AL SALDO E STRALCIO — Edizione cartacea</h2><p>La guida sarà proposta prossimamente anche in formato cartaceo, con stampa su richiesta.</p><p class="muted">Prezzo, tempi e spedizione saranno comunicati prima dell'apertura degli ordini.</p><div class="quantity-selector" aria-label="Selettore quantità non ancora attivo"><span>Numero di copie</span><div class="quantity-controls"><button type="button" class="btn outline" disabled aria-label="Diminuisci numero di copie">−</button><output aria-live="polite">1</output><button type="button" class="btn outline" disabled aria-label="Aumenta numero di copie">+</button></div></div><button type="button" class="btn coming-soon-button" disabled aria-disabled="true">COMING SOON</button></div>
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
    # Mostra solo il primo capitolo gratuito, senza esporre il PDF completo.
    if not PREVIEW_FILE.is_file():
        return page("Anteprima non disponibile", '<h1>Anteprima temporaneamente non disponibile</h1><p>Il primo capitolo gratuito non è al momento disponibile. Riprova più tardi.</p><a class="btn" href="/shop">Torna allo Shop</a>'), 404
    response = send_file(PREVIEW_FILE, mimetype="application/pdf", as_attachment=False, download_name="FixTude_NIENTE_PANICO_Anteprima.pdf", conditional=True)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Disposition"] = 'inline; filename="FixTude_NIENTE_PANICO_Anteprima.pdf"'
    return response


@app.route("/shop/anteprima-saldo-stralcio")
def shop_anteprima_saldo_stralcio():
    # Anteprima PDF gratuita della guida al saldo e stralcio.
    preview_file = PDF_DIR / "FixTude_Guida_Saldo_e_Stralcio_ANTEPRIMA.pdf"
    if not preview_file.is_file():
        return page("Anteprima non disponibile", '<h1>Anteprima temporaneamente non disponibile</h1><p>Il primo capitolo gratuito non è al momento disponibile. Riprova più tardi.</p><a class="btn" href="/shop">Torna allo Shop</a>'), 404
    response = send_file(preview_file, mimetype="application/pdf", as_attachment=False, download_name="FixTude_Guida_Saldo_e_Stralcio_Anteprima.pdf", conditional=True)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Disposition"] = 'inline; filename="FixTude_Guida_Saldo_e_Stralcio_Anteprima.pdf"'
    return response


def _crea_checkout_pdf(product_id, product_name, description, cancel_url):
    # Crea un checkout separato per ciascun PDF digitale.
    secret_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if stripe is None or not secret_key:
        return page("Acquisto temporaneamente non disponibile", '<h1>Acquisto non ancora attivo</h1><p>Il pagamento online non è configurato al momento. Non è stato effettuato alcun addebito.</p><a class="btn" href="/shop">Torna allo Shop</a>'), 503
    stripe.api_key = secret_key
    try:
        checkout = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price_data": {"currency": "eur", "unit_amount": 699, "product_data": {"name": product_name, "description": description}}, "quantity": 1}],
            success_url=request.host_url.rstrip("/") + "/shop/conferma?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=request.host_url.rstrip("/") + cancel_url,
            metadata={"fixtude_product": product_id, "terms_accepted": "yes", "terms_version": "2026-10-19"},
        )
        return redirect(checkout.url, code=303)
    except Exception:
        app.logger.exception("Creazione sessione Stripe non riuscita per %s", product_id)
        return page("Acquisto non disponibile", '<h1>Non è stato possibile avviare il pagamento</h1><p>Nessun pagamento è stato confermato. Riprova più tardi.</p><a class="btn" href="/shop">Torna allo Shop</a>'), 502


@app.route("/shop/acquista", methods=["POST"])
def shop_acquista():
    if request.form.get("accept_terms") != "yes":
        return page("Conferma necessaria", '<h1>Prima di proseguire</h1><p>Devi accettare i Termini e Condizioni per continuare con l’acquisto.</p><a class="btn" href="/shop">Torna allo shop</a>'), 400
    return _crea_checkout_pdf("niente_panico_pdf", "NIENTE PANICO - libro digitale FixTude", "Libro digitale in formato PDF", "/shop?annullato=1")


@app.route("/shop/acquista-guida", methods=["POST"])
def shop_acquista_guida():
    if request.form.get("accept_terms") != "yes":
        return page("Conferma necessaria", '<h1>Prima di proseguire</h1><p>Devi accettare i Termini e Condizioni per continuare con l’acquisto.</p><a class="btn" href="/shop">Torna allo shop</a>'), 400
    return _crea_checkout_pdf(GUIDE_PRODUCT_ID, "Guida FixTude - Saldo e stralcio", "Guida digitale PDF, edizione rivista", "/shop?annullato=1")


@app.route("/shop/conferma")
def shop_conferma():
    session_id = request.args.get("session_id", "").strip()
    secret_key = os.environ.get("STRIPE_SECRET_KEY", "").strip()
    if not session_id or stripe is None or not secret_key:
        return page("Verifica acquisto", '<h1>Non possiamo verificare il pagamento</h1><p>Contatta info@fixtude.it per assistenza.</p>'), 400
    try:
        stripe.api_key = secret_key
        checkout_data = stripe.checkout.Session.retrieve(session_id).to_dict()
        paid = checkout_data.get("payment_status") == "paid"
        product_id = (checkout_data.get("metadata") or {}).get("fixtude_product")
        products = {
            "niente_panico_pdf": {"file": BOOK_FILE, "title": "NIENTE PANICO", "filename": "FixTude_NIENTE_PANICO.pdf"},
            GUIDE_PRODUCT_ID: {"file": GUIDE_FILE, "title": "Guida FixTude — Saldo e stralcio", "filename": "FixTude_Guida_Saldo_e_Stralcio.pdf"},
        }
        product = products.get(product_id)
        if not paid or not product:
            return page("Pagamento da verificare", '<h1>Pagamento non ancora confermato</h1><p>Il PDF sarà disponibile dopo la conferma del pagamento.</p><a class="btn" href="/shop">Torna allo Shop</a>'), 402
        if not product["file"].is_file():
            return page("PDF in preparazione", f'<h1>Pagamento confermato</h1><p>Il pagamento risulta confermato, ma il file di {escape(product["title"])} non è presente nell’area privata del server. Contatta info@fixtude.it per ricevere assistenza.</p>'), 500
        content = f'<h1>Grazie per il tuo acquisto!</h1><section class="card"><p>Il pagamento è stato confermato.</p><p><strong>{escape(product["title"])}</strong> è pronto per il download.</p><a class="btn" href="/shop/scarica?session_id={escape(session_id, quote=True)}">Scarica il PDF</a></section><p class="muted">Conserva questa pagina finché non hai scaricato il file.</p>'
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
        checkout_data = stripe.checkout.Session.retrieve(session_id).to_dict()
        paid = checkout_data.get("payment_status") == "paid"
        product_id = (checkout_data.get("metadata") or {}).get("fixtude_product")
        products = {
            "niente_panico_pdf": {"file": BOOK_FILE, "filename": "FixTude_NIENTE_PANICO.pdf"},
            GUIDE_PRODUCT_ID: {"file": GUIDE_FILE, "filename": "FixTude_Guida_Saldo_e_Stralcio.pdf"},
        }
        product = products.get(product_id)
        if not paid or not product or not product["file"].is_file():
            return "Accesso non autorizzato o file non disponibile", 403
        response = send_file(product["file"], as_attachment=True, download_name=product["filename"], mimetype="application/pdf")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "private, no-store"
        return response
    except Exception:
        app.logger.exception("Download PDF non riuscito")
        return "Non è stato possibile verificare l'acquisto", 502


# =========================================================
# DOCUMENTI LEGALI - IDENTIFICAZIONE DEL TITOLARE E INFORMATIVE
# =========================================================

LEGAL_VERSION = "2026-10-19"
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
    return Response(f"""<!doctype html><html lang="it-IT"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#123a72"><meta name="description" content="{escape(seo_description, quote=True)}"><meta name="robots" content="index, follow, max-image-preview:large"><link rel="canonical" href="{escape(canonical_url, quote=True)}"><link rel="icon" type="image/x-icon" href="/favicon.ico?v=20261019" sizes="48x48"><link rel="icon" type="image/png" sizes="16x16" href="/favicon-16.png?v=20261019"><link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png?v=20261019"><meta property="og:locale" content="it_IT"><meta property="og:type" content="website"><meta property="og:site_name" content="FixTude"><meta property="og:title" content="{escape(seo_title, quote=True)}"><meta property="og:description" content="{escape(seo_description, quote=True)}"><meta property="og:url" content="{escape(canonical_url, quote=True)}"><meta property="og:image" content="{SEO_BASE_URL}/static/fixtude-512.png"><meta property="og:image:alt" content="Logo FixTude"><meta name="twitter:card" content="summary_large_image"><meta name="twitter:title" content="{escape(seo_title, quote=True)}"><meta name="twitter:description" content="{escape(seo_description, quote=True)}"><meta name="twitter:image" content="{SEO_BASE_URL}/static/fixtude-512.png"><title>{escape(seo_title)}</title><style>body{{margin:0;background:#f5f8fc;color:#203047;font:16px/1.65 Arial,sans-serif}}main{{width:min(920px,calc(100% - 28px));margin:28px auto 50px}}.paper{{background:#fff;border:1px solid #dce5ef;border-radius:16px;padding:clamp(20px,4vw,38px)}}h1,h2{{line-height:1.25}}h1{{color:#174b91}}h2{{font-size:21px;margin-top:28px}}a{{color:#174b91}}.notice{{background:#fff7df;border:1px solid #efd58b;border-radius:10px;padding:13px 15px}}.muted{{color:#65758b;font-size:14px}}footer{{border-top:1px solid #dce5ef;margin-top:28px;padding-top:16px;font-size:13px;color:#65758b}}.top{{display:flex;justify-content:space-between;gap:15px;flex-wrap:wrap;margin-bottom:18px}}.brand{{font-size:24px;font-weight:800;color:#174b91;text-decoration:none}}</style></head><body><main><div class="top"><a class="brand" href="/">FixTude</a><a href="/">Torna al sito</a></div><article class="paper"><h1>{escape(title)}</h1><p class="muted">Versione {LEGAL_VERSION}</p><div class="notice"><strong>Denominazione del servizio:</strong> FixTude · <strong>Titolare:</strong> {escape(LEGAL_OWNER_NAME)} · <strong>P. IVA:</strong> {escape(LEGAL_VAT_NUMBER)}</div>{body}<footer><strong>Contatti:</strong> {owner_line}<br>Ultimo aggiornamento: 19 ottobre 2026<br><button type="button" id="ft-cookie-reopen-footer" style="border:0;background:transparent;color:#174b91;text-decoration:underline;cursor:pointer;font:inherit">Preferenze cookie</button></footer></article></main></body></html>""", mimetype="text/html")


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
    <h2>10. Area Professionisti</h2><p>L’area professionisti è in fase di attivazione. La registrazione non comporta automaticamente la pubblicazione del profilo o l’attivazione di un abbonamento. I profili sono pubblicati solo dopo verifica e approvazione. Le eventuali funzionalità di inoltro richieste e i pacchetti di lead non sono pubblicati come offerta commerciale, non sono acquistabili e non saranno attivati fino alla definizione delle condizioni contrattuali e alla verifica legale e deontologica.</p><h2>11. Assistenza e modifiche</h2><p>Per richieste o reclami scrivere a info@fixtude.it. I termini potranno essere aggiornati per modifiche normative o tecniche; per gli acquisti conclusi restano fermi i diritti già acquisiti.</p>"""
    return legal_page("Termini e Condizioni", body)


@app.route("/privacy")
def privacy():
    body = """<p>Questa informativa descrive i principali trattamenti connessi alla versione attuale del sito FixTude. Viene aggiornata quando cambiano in modo rilevante le funzionalità, i fornitori o le modalità di trattamento.</p>
    <h2>1. Titolare del trattamento</h2><p>Il titolare del trattamento è Giovanni Pugliese, che gestisce il servizio denominato FixTude, con domicilio professionale indicato in Via Saronno 65, 00188 Roma, Italia, Partita IVA 15990471003. Per richieste relative ai dati personali: info@fixtude.it.</p>
    <h2>2. Dati trattati e finalità</h2><ul><li><strong>Calcolatore:</strong> gli importi inseriti vengono elaborati nel browser e il codice del calcolatore non li invia agli endpoint applicativi.</li><li><strong>Modulo di ricontatto:</strong> il modulo visibile invia nome, email, telefono facoltativo e messaggio tramite Web3Forms. È presente anche l’endpoint alternativo <code>/api/contact</code>, che se invocato inoltra i dati tramite il server SMTP configurato dal titolare. L’applicazione non conserva una copia in un proprio database; i dati possono restare nei sistemi dei fornitori e nella casella email del destinatario secondo le rispettive condizioni.</li><li><strong>Chat:</strong> i messaggi sono conservati temporaneamente nella memoria del processo server; le chat inattive vengono eliminate dopo circa due ore, ma un riavvio può eliminarle prima. L’invio di un messaggio genera una notifica email tramite Web3Forms dal browser; i dati possono essere trattati da Web3Forms e dal sistema email del destinatario secondo le rispettive condizioni.</li><li><strong>Area Professionisti:</strong> per la registrazione e l'accesso vengono trattati email, password in forma di hash, nome/denominazione professionale, categoria, città, indicazione dell'Ordine/Albo e presentazione pubblica. I profili restano non pubblici fino all'approvazione manuale. I dati sono conservati nel database applicativo configurato dal titolare; prima di attivare la funzione va configurato un archivio persistente e definito un periodo di conservazione. La vetrina non mostra email, telefono o altri recapiti diretti.</li><li><strong>Libro digitale:</strong> se l'acquisto è attivo, Stripe tratta i dati necessari a elaborare e verificare il pagamento. FixTude riceve le informazioni necessarie a verificare la transazione.</li><li><strong>Dati tecnici:</strong> hosting e infrastruttura possono trattare dati tecnici, log e informazioni necessarie a sicurezza e funzionamento.</li></ul>
    <h2>3. Basi giuridiche</h2><p>Le richieste di contatto sono trattate per rispondere a una richiesta dell'interessato e adottare misure precontrattuali su sua richiesta. I dati relativi ad acquisti sono trattati per eseguire il contratto e adempiere agli obblighi di legge. Sicurezza e prevenzione abusi possono basarsi sul legittimo interesse, da valutare e documentare. Eventuali attività promozionali richiedono una valutazione e, ove necessario, un consenso separato.</p>
    <h2>4. Destinatari e fornitori</h2><p>I fornitori attualmente coinvolti o da verificare includono Render per l’hosting (se confermato in produzione), Web3Forms per il modulo visibile e le notifiche email della chat, il fornitore del servizio SMTP configurato per l’endpoint alternativo, Register per la casella email e Stripe per i pagamenti del libro, quando attivi. Devono essere controllati termini, ruoli privacy, subfornitori, tempi di conservazione e possibili trasferimenti fuori dallo Spazio Economico Europeo.</p>
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


def _png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)


def _generated_icon_png(size=48):
    size = max(16, min(int(size), 512))
    blue, dark, white = (23, 75, 145, 255), (17, 24, 39, 255), (255, 255, 255, 255)
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            nx, ny = x / size, y / size
            color = white
            if (0.20 <= nx <= 0.35 and 0.15 <= ny <= 0.84) or (0.20 <= nx <= 0.77 and 0.15 <= ny <= 0.30) or (0.20 <= nx <= 0.65 and 0.39 <= ny <= 0.54):
                color = blue
            if (0.55 <= nx <= 0.84 and 0.43 <= ny <= 0.56) or (0.66 <= nx <= 0.77 and 0.52 <= ny <= 0.84):
                color = dark
            raw.extend(color)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return sig + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _png_chunk(b"IEND", b"")


def _preferred_icon_png(filename, size):
    path = os.path.join(app.root_path, "static", filename)
    try:
        with open(path, "rb") as f:
            data = f.read()
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
            width, height = struct.unpack(">II", data[16:24])
            if width == int(size) and height == int(size):
                return data
    except OSError:
        pass
    return _generated_icon_png(size)


def _png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)


def _generated_icon_png(size=48):
    size = max(16, min(int(size), 512))
    blue, dark, white = (23, 75, 145, 255), (17, 24, 39, 255), (255, 255, 255, 255)
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            nx, ny = x / size, y / size
            color = white
            if (0.20 <= nx <= 0.35 and 0.15 <= ny <= 0.84) or (0.20 <= nx <= 0.77 and 0.15 <= ny <= 0.30) or (0.20 <= nx <= 0.65 and 0.39 <= ny <= 0.54):
                color = blue
            if (0.55 <= nx <= 0.84 and 0.43 <= ny <= 0.56) or (0.66 <= nx <= 0.77 and 0.52 <= ny <= 0.84):
                color = dark
            raw.extend(color)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return sig + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + _png_chunk(b"IEND", b"")


def _preferred_icon_png(filename, size):
    path = os.path.join(app.root_path, "static", filename)
    try:
        with open(path, "rb") as f:
            data = f.read()
        if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
            width, height = struct.unpack(">II", data[16:24])
            if width == int(size) and height == int(size):
                return data
    except OSError:
        pass
    return _generated_icon_png(size)


@app.route("/favicon.svg", methods=["GET"])
def favicon_svg():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="12" fill="#fff"/><path d="M13 10h35v9H22v9h22v9H22v17h-9z" fill="#174b91"/><path d="M35 35h19v7h-6v12h-7V42h-6z" fill="#111827"/></svg>"""
    return Response(svg, mimetype="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@app.route("/favicon-16.png", methods=["GET"])
def favicon_png_16():
    return Response(_preferred_icon_png("fixtude-16.png", 16), mimetype="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.route("/apple-touch-icon.png", methods=["GET"])
def apple_touch_icon():
    return Response(_preferred_icon_png("fixtude-180.png", 180), mimetype="image/png", headers={"Cache-Control": "public, max-age=86400"})


@app.route("/favicon.ico", methods=["GET"])
def favicon_ico():
    png = _preferred_icon_png("fixtude-48.png", 48)
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 48, 48, 0, 0, 1, 32, len(png), 22)
    return Response(header + entry + png, mimetype="image/vnd.microsoft.icon", headers={"Cache-Control": "public, max-age=86400"})


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
        "/educazione-finanziaria", "/educazione-finanziaria/come-gestire-lo-stipendio-regola-un-terzo", "/educazione-finanziaria/guida-saldo-e-stralcio", "/sic", "/assistenza-legale", "/professionisti", "/shop",
        "/disclaimer", "/termini", "/privacy", "/cookie",
    ]
    urls = "".join(
        "<url><loc>" + escape(SEO_BASE_URL + ("" if path == "/" else path)) + "</loc></url>"
        for path in paths
    )
    xml = '<?xml version="1.0" encoding="UTF-8"?>' + '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + urls + '</urlset>'
    return Response(xml, mimetype="application/xml; charset=utf-8")


# =========================================================
# AREA PROFESSIONISTI - REGISTRAZIONE, ACCESSO E PROFILI
# Database SQLite: configurare FIXTUDE_DB_PATH su disco persistente Render.
# I profili sono pubblicati solo dopo approvazione manuale.
# Nessun recapito diretto viene mostrato nella vetrina pubblica.
# =========================================================

PRO_DB_CONFIGURED = bool(os.environ.get("FIXTUDE_DB_PATH", "").strip())
PRO_DB_PATH = os.environ.get("FIXTUDE_DB_PATH", "").strip() or os.path.join(app.root_path, "fixtude_professionisti.sqlite3")
PROFESSIONISTI_ADMIN_TOKEN = os.environ.get("PROFESSIONISTI_ADMIN_TOKEN", "").strip()


def contiene_recapiti_diretti(value):
    value = str(value or "")
    patterns = (
        r"(?i)\b[\w.+%-]+@[\w.-]+\.[a-z]{2,}\b",
        r"(?i)(?:https?://|www\.|mailto:|tel:|wa\.me/|t\.me/)",
        r"(?i)\b(?:telefono|cellulare|whatsapp|telegram|e-?mail|pec|linkedin|instagram|facebook|contattami|contattare|scrivimi|chiamami)\b",
        r"(?<!\w)\+?\d(?:[\s()./-]*\d){7,}(?!\w)",
    )
    return any(re.search(pattern, value) for pattern in patterns)


def professionisti_registration_ready():
    # Non raccogliere dati professionali finché non sono configurati archivio persistente,
    # segreto di sessione e token amministrativo per la revisione manuale.
    return (
        os.environ.get("PROFESSIONISTI_REGISTRATION_ENABLED", "1").strip() == "1"
        and bool(os.environ.get("FIXTUDE_DB_PATH", "").strip())
        and bool(os.environ.get("PROFESSIONISTI_ADMIN_TOKEN", "").strip())
        and bool(os.environ.get("SECRET_KEY", "").strip())
    )


def professionisti_db():
    parent = os.path.dirname(PRO_DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    db = sqlite3.connect(PRO_DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("CREATE TABLE IF NOT EXISTS professionisti ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, "
        "password_hash TEXT NOT NULL, nome_pubblico TEXT NOT NULL, "
        "categoria TEXT NOT NULL CHECK(categoria IN ('Avvocato','Commercialista')), "
        "citta TEXT NOT NULL, albo TEXT NOT NULL, bio TEXT NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','suspended')), "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    db.commit()
    return db


def pro_csrf_token():
    token = session.get("pro_csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["pro_csrf"] = token
    return token


def pro_csrf_valid():
    supplied = str(request.form.get("csrf_token", ""))
    expected = str(session.get("pro_csrf", ""))
    return bool(supplied and expected and hmac.compare_digest(supplied, expected))


def pro_current_user():
    user_id = session.get("professionista_id")
    if not user_id:
        return None
    db = professionisti_db()
    try:
        return db.execute("SELECT * FROM professionisti WHERE id = ?", (user_id,)).fetchone()
    finally:
        db.close()


def pro_shell(title, body, robots="noindex, nofollow"):
    safe_title = escape(title)
    safe_body = body
    html = f'''<!doctype html><html lang="it-IT"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="{escape(robots)}"><meta name="theme-color" content="#123a72"><link rel="icon" type="image/x-icon" href="/favicon.ico?v=20261019" sizes="48x48"><link rel="icon" type="image/png" sizes="16x16" href="/favicon-16.png?v=20261019"><link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png?v=20261019"><title>{safe_title} | FixTude</title><style>
    *{{box-sizing:border-box}}body{{margin:0;background:#f5f8fc;color:#203047;font:16px/1.6 Arial,sans-serif}}main{{width:min(960px,calc(100% - 28px));margin:28px auto 55px}}header{{background:#fff;border-bottom:1px solid #dce5ef;padding:14px max(14px,calc((100% - 960px)/2));display:flex;justify-content:space-between;align-items:center;gap:16px}}header a{{color:#174b91;text-decoration:none;font-weight:700}}.brand{{font-size:24px}}.card{{background:#fff;border:1px solid #dce5ef;border-radius:15px;padding:20px;margin:16px 0}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px}}h1,h2,h3{{line-height:1.25}}h1{{color:#174b91}}label{{display:block;font-weight:700;margin:12px 0 5px}}input,select,textarea{{width:100%;font:inherit;padding:11px 12px;border:1px solid #bdcbdc;border-radius:8px;background:#fff}}textarea{{min-height:100px;resize:vertical}}.btn,button.btn{{display:inline-block;background:#174b91;color:#fff;border:0;border-radius:9px;padding:11px 16px;text-decoration:none;font:inherit;font-weight:700;cursor:pointer}}.btn.secondary{{background:#fff;color:#174b91;border:1px solid #174b91}}.notice{{background:#fff7df;border:1px solid #efd58b;border-radius:10px;padding:13px 15px}}.muted{{color:#65758b;font-size:14px}}.profile{{border:1px solid #dce5ef;border-radius:12px;padding:16px;background:#fff}}.profile h3{{margin:0 0 6px}}.tag{{display:inline-block;background:#eaf2fc;color:#174b91;border-radius:999px;padding:3px 9px;font-size:12px;font-weight:700}}.actions{{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}}.price{{font-size:22px;font-weight:800;color:#174b91}}.doc h2{{margin-top:26px}}.signature{{margin-top:38px;display:grid;grid-template-columns:1fr 1fr;gap:35px}}.line{{border-top:1px solid #64748b;padding-top:8px;margin-top:55px}}@media(max-width:600px){{header{{padding:12px 14px}}.card{{padding:16px}}.signature{{grid-template-columns:1fr}}}}
    @media print{{header,.no-print,.back-row{{display:none!important}}body{{background:#fff}}main{{width:100%;margin:0}}.card{{border:0;padding:0}}}}
    </style></head><body><header><a class="brand" href="/">FixTude</a><nav><a href="/professionisti">Area Professionisti</a> · <a href="/">Sito</a></nav></header><main>{safe_body}</main></body></html>'''
    return Response(html, mimetype="text/html; charset=utf-8")


@app.route("/professionisti")
def area_professionisti():
    pros = []
    if PRO_DB_CONFIGURED and os.path.isfile(PRO_DB_PATH):
        try:
            db = professionisti_db()
            try:
                pros = db.execute("SELECT nome_pubblico,categoria,citta,albo,bio FROM professionisti WHERE status='approved' ORDER BY categoria,nome_pubblico").fetchall()
            finally:
                db.close()
        except Exception:
            app.logger.exception("Lettura vetrina professionisti non riuscita")
    # Filtra anche eventuali record preesistenti, non solo i nuovi invii/modifiche.
    pros = [p for p in pros if not any(contiene_recapiti_diretti(p[field]) for field in ("nome_pubblico", "citta", "albo", "bio"))]
    if pros:
        cards = "".join(f'''<article class="profile"><span class="tag">{escape(p['categoria'])}</span><h3>{escape(p['nome_pubblico'])}</h3><p><strong>{escape(p['citta'])}</strong></p><p>{escape(p['albo'])}</p><p>{escape(p['bio'])}</p></article>''' for p in pros)
    else:
        cards = '<div class="card"><p>La vetrina è in fase di attivazione. I profili appariranno solo dopo la verifica e l’approvazione.</p></div>'
    body = f'''<section class="card"><h1>Area Professionisti FixTude</h1><p>Uno spazio dedicato ad avvocati e commercialisti che desiderano presentare le proprie competenze a persone che cercano orientamento nella gestione di situazioni debitorie.</p><div class="notice"><strong>Trasparenza:</strong> i profili pubblici non mostrano telefono, email, link di contatto, chat o pulsanti per contattare direttamente il professionista. L’eventuale inoltro di richieste e i pacchetti lead non sono attivi in questa fase, in attesa di verifica legale e deontologica.</div><div class="actions"><a class="btn" href="/professionisti/registrazione">Richiedi la registrazione</a><a class="btn secondary" href="/professionisti/login">Accedi</a></div></section><section><h2>Professionisti approvati</h2><div class="grid">{cards}</div></section><section class="card"><h2>Condizioni di adesione</h2><p>La vetrina e l’area riservata sono in fase di verifica. Al momento non è attivo alcun abbonamento e non è possibile acquistare pacchetti o lead. Le condizioni economiche saranno pubblicate soltanto dopo una verifica legale e deontologica specifica e la definizione di un contratto completo.</p><p>FixTude non riceve percentuali, commissioni o partecipazioni sui compensi delle pratiche professionali. Il rapporto professionale, l’incarico e il relativo compenso sono concordati direttamente tra cliente e professionista.</p><p class="muted">Le proposte economiche interne non costituiscono un’offerta al pubblico e non generano obblighi di pagamento.</p></section>'''
    return pro_shell("Area Professionisti", body, robots="index, follow")


@app.route("/professionisti/registrazione", methods=["GET", "POST"])
def professionisti_registrazione():
    # La pagina e il modulo devono essere visibili anche durante la preparazione
    # tecnica. Finché archivio, chiave di sessione e token admin non sono configurati,
    # l'invio resta bloccato e nessun dato viene dichiarato come registrato.
    registration_ready = professionisti_registration_ready()
    err = ""
    if request.method == "POST":
        if not registration_ready:
            token = escape(pro_csrf_token(), quote=True)
            body = f'''<section class="card"><h1>Modulo di registrazione</h1><p class="notice"><strong>Modulo visibile, invio non ancora attivo.</strong> La registrazione sarà completata quando sarà configurata l’archiviazione sicura su Render. I dati inseriti non sono stati salvati. Puoi tornare al modulo senza che venga creata una registrazione.</p><p><a class="btn" href="/professionisti/registrazione">Torna al modulo</a> <a class="btn secondary" href="/professionisti">Area Professionisti</a></p></section>'''
            return pro_shell("Registrazione non ancora attiva", body), 503
        if rate_limited("professionisti_login", 10, 300):
            return pro_shell("Troppi tentativi", '<section class="card"><h1>Troppi tentativi</h1><p>Attendi qualche minuto prima di riprovare.</p></section>'), 429
        if not pro_csrf_valid():
            return pro_shell("Richiesta non valida", '<section class="card"><h1>Richiesta non valida</h1><p>Ricarica la pagina e riprova.</p></section>'), 400
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        nome = request.form.get("nome_pubblico", "").strip()
        categoria = request.form.get("categoria", "").strip()
        citta = request.form.get("citta", "").strip()
        albo = request.form.get("albo", "").strip()
        bio = request.form.get("bio", "").strip()
        if request.form.get("privacy_ack") != "yes": err = "Per proseguire devi confermare di aver letto l’informativa privacy."
        elif not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email): err = "Inserisci un indirizzo email valido."
        elif len(password) < 12: err = "La password deve contenere almeno 12 caratteri."
        elif not nome or len(nome) > 120: err = "Inserisci il nome professionale da mostrare in vetrina."
        elif categoria not in ("Avvocato", "Commercialista"): err = "Seleziona una categoria valida."
        elif not citta or len(citta) > 100: err = "Inserisci la città professionale."
        elif not albo or len(albo) > 180: err = "Indica Ordine/Albo e provincia di iscrizione."
        elif not bio or len(bio) > 900: err = "Inserisci una breve presentazione (massimo 900 caratteri)."
        elif any(contiene_recapiti_diretti(v) for v in (nome, citta, albo, bio)): err = "I campi pubblici non possono contenere email, telefoni, link o inviti al contatto diretto."
        else:
            db = professionisti_db()
            try:
                now = datetime.now(ROME_TZ).isoformat(timespec="seconds")
                db.execute("INSERT INTO professionisti(email,password_hash,nome_pubblico,categoria,citta,albo,bio,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'pending',?,?)", (email,generate_password_hash(password),nome,categoria,citta,albo,bio,now,now))
                db.commit()
                return pro_shell("Registrazione ricevuta", '<section class="card"><h1>Richiesta ricevuta</h1><p>La registrazione è stata salvata. Il profilo non è pubblico finché FixTude non completa la verifica e l’approvazione.</p><p>Per ora non è stato attivato alcun abbonamento e non è stato richiesto alcun pagamento.</p><a class="btn" href="/professionisti/login">Vai all’accesso</a></section>')
            except sqlite3.IntegrityError:
                err = "Esiste già un account con questo indirizzo email. Prova ad accedere."
            except Exception:
                app.logger.exception("Registrazione professionista non riuscita")
                err = "Non è stato possibile completare la registrazione. Riprova più tardi."
            finally:
                db.close()
    token = escape(pro_csrf_token(), quote=True)
    readiness_notice = '' if registration_ready else '<p class="notice"><strong>Modulo in anteprima:</strong> puoi visualizzarlo e controllare i campi. L’invio effettivo sarà attivato dopo la configurazione dell’archiviazione sicura su Render. In questa fase i dati non vengono salvati.</p>'
    body = f'''<section class="card"><h1>Registrazione professionisti</h1><p>Compila la richiesta se sei un avvocato o un commercialista e desideri presentare il tuo profilo professionale su FixTude.</p>{readiness_notice}<p class="muted">La registrazione non comporta l’attivazione automatica di un abbonamento. Il profilo pubblico sarà visibile soltanto dopo verifica e approvazione. Telefono, email, link e altri recapiti diretti non saranno pubblicati nella vetrina.</p>{f'<p class="notice">{escape(err)}</p>' if err else ''}<form method="post"><input type="hidden" name="csrf_token" value="{token}"><label for="nome_pubblico">Nome e cognome / denominazione professionale</label><input id="nome_pubblico" name="nome_pubblico" maxlength="120" required><label for="categoria">Professione</label><select id="categoria" name="categoria" required><option value="">Seleziona</option><option>Avvocato</option><option>Commercialista</option></select><label for="citta">Città</label><input id="citta" name="citta" maxlength="100" required><label for="albo">Ordine/Albo e provincia di iscrizione</label><input id="albo" name="albo" maxlength="180" required><label for="bio">Presentazione pubblica (max 900 caratteri)</label><textarea id="bio" name="bio" maxlength="900" required></textarea><label for="email">Email per accesso e comunicazioni riservate</label><input id="email" name="email" type="email" autocomplete="email" maxlength="254" required><label for="password">Password (almeno 12 caratteri)</label><input id="password" name="password" type="password" autocomplete="new-password" minlength="12" required><p class="muted">Non inserire nella presentazione dati di clienti o informazioni riservate. L’account resta in attesa di verifica.</p><p><label><input type="checkbox" name="privacy_ack" value="yes" required style="width:auto"> Ho letto l’<a href="/privacy" target="_blank" rel="noopener">Informativa Privacy</a>.</label></p><button class="btn" type="submit">Invia richiesta</button></form></section>'''
    return pro_shell("Registrazione professionisti", body)


@app.route("/professionisti/login", methods=["GET", "POST"])
def professionisti_login():
    if not professionisti_registration_ready():
        return pro_shell("Accesso in preparazione", '<section class="card"><h1>Accesso in preparazione</h1><p>L’area riservata non è ancora attiva. Riprova dopo l’attivazione tecnica.</p></section>'), 503
    err = ""
    if request.method == "POST":
        if not pro_csrf_valid():
            return pro_shell("Richiesta non valida", '<section class="card"><h1>Richiesta non valida</h1><p>Ricarica la pagina e riprova.</p></section>'), 400
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        db = professionisti_db()
        try:
            user = db.execute("SELECT * FROM professionisti WHERE email=?", (email,)).fetchone()
            if user and check_password_hash(user["password_hash"], password):
                session.clear()
                session["professionista_id"] = user["id"]
                session["pro_csrf"] = secrets.token_urlsafe(32)
                return redirect("/professionisti/area", code=303)
            err = "Credenziali non riconosciute. Verifica email e password."
        finally:
            db.close()
    token = escape(pro_csrf_token(), quote=True)
    body = f'''<section class="card"><h1>Accesso professionisti</h1>{f'<p class="notice">{escape(err)}</p>' if err else ''}<form method="post"><input type="hidden" name="csrf_token" value="{token}"><label for="email">Email</label><input id="email" name="email" type="email" autocomplete="username" required><label for="password">Password</label><input id="password" name="password" type="password" autocomplete="current-password" required><p><button class="btn" type="submit">Accedi</button></p></form><p class="muted">Non hai un account? <a href="/professionisti/registrazione">Richiedi la registrazione</a>.</p></section>'''
    return pro_shell("Accesso professionisti", body)


@app.route("/professionisti/area", methods=["GET", "POST"])
def professionisti_area():
    if not professionisti_registration_ready():
        return pro_shell("Area in preparazione", '<section class="card"><h1>Area in preparazione</h1><p>L’area riservata non è ancora attiva.</p></section>'), 503
    user = pro_current_user()
    if not user:
        return redirect("/professionisti/login", code=303)
    if request.method == "POST":
        if not pro_csrf_valid():
            return pro_shell("Richiesta non valida", '<section class="card"><h1>Richiesta non valida</h1></section>'), 400
        nome = request.form.get("nome_pubblico", "").strip()
        citta = request.form.get("citta", "").strip()
        albo = request.form.get("albo", "").strip()
        bio = request.form.get("bio", "").strip()
        if (nome and citta and albo and bio and len(nome) <= 120 and len(citta) <= 100 and len(albo) <= 180 and len(bio) <= 900
                and not any(contiene_recapiti_diretti(v) for v in (nome, citta, albo, bio))):
            db = professionisti_db()
            try:
                db.execute("UPDATE professionisti SET nome_pubblico=?,citta=?,albo=?,bio=?,status=CASE WHEN status='approved' THEN 'pending' ELSE status END,updated_at=? WHERE id=?", (nome,citta,albo,bio,datetime.now(ROME_TZ).isoformat(timespec="seconds"),user["id"]))
                db.commit()
                return redirect("/professionisti/area?salvato=1", code=303)
            finally:
                db.close()
    user = pro_current_user()
    status_text = {"pending":"In attesa di verifica: il profilo non è pubblico.","approved":"Profilo approvato e visibile nella vetrina.","suspended":"Profilo sospeso: contatta l’amministrazione FixTude."}.get(user["status"], "Stato non disponibile")
    token = escape(pro_csrf_token(), quote=True)
    saved = '<p class="notice">Modifiche salvate. Il profilo è in revisione.</p>' if request.args.get("salvato") else ''
    if request.method == "POST" and not request.args.get("salvato"):
        saved = '<p class="notice">Modifiche non salvate: controlla i campi e rimuovi recapiti, link o inviti al contatto diretto.</p>'
    body = f'''<section class="card"><h1>Area riservata professionisti</h1><p><strong>Account:</strong> {escape(user['email'])}</p><p class="notice">{escape(status_text)}</p>{saved}<form method="post"><input type="hidden" name="csrf_token" value="{token}"><label for="nome_pubblico">Nome/denominazione pubblica</label><input id="nome_pubblico" name="nome_pubblico" maxlength="120" value="{escape(user['nome_pubblico'], quote=True)}" required><label>Professione</label><input value="{escape(user['categoria'], quote=True)}" disabled><label for="citta">Città</label><input id="citta" name="citta" maxlength="100" value="{escape(user['citta'], quote=True)}" required><label for="albo">Ordine/Albo e provincia</label><input id="albo" name="albo" maxlength="180" value="{escape(user['albo'], quote=True)}" required><label for="bio">Presentazione pubblica</label><textarea id="bio" name="bio" maxlength="900" required>{escape(user['bio'])}</textarea><p class="muted">Telefono, email, chat e link di contatto non vengono pubblicati nella vetrina.</p><button class="btn" type="submit">Salva modifiche</button></form></section><section class="card"><h2>Abbonamento</h2><p>Non hai un abbonamento attivo. Prezzi e pacchetti lead sono in fase di verifica legale/deontologica e non possono essere acquistati da questa area.</p><p><a href="/professionisti/contratto">Consulta la bozza di contratto professionisti</a>.</p><form method="post" action="/professionisti/logout"><input type="hidden" name="csrf_token" value="{token}"><button class="btn secondary" type="submit">Esci</button></form></section>'''
    return pro_shell("Area riservata professionisti", body)


@app.route("/professionisti/logout", methods=["POST"])
def professionisti_logout():
    if not pro_csrf_valid():
        return pro_shell("Richiesta non valida", '<section class="card"><h1>Richiesta non valida</h1></section>'), 400
    session.pop("professionista_id", None)
    session.pop("pro_csrf", None)
    return redirect("/professionisti", code=303)


@app.route("/professionisti/admin", methods=["GET", "POST"])
def professionisti_admin():
    if not PROFESSIONISTI_ADMIN_TOKEN or not PRO_DB_CONFIGURED or not os.environ.get("SECRET_KEY", "").strip():
        return pro_shell("Amministrazione non configurata", '<section class="card"><h1>Area amministrazione non configurata</h1><p>Per abilitare la revisione servono PROFESSIONISTI_ADMIN_TOKEN, SECRET_KEY e FIXTUDE_DB_PATH su un disco persistente Render.</p></section>'), 503
    if request.method == "POST" and request.form.get("admin_token"):
        if not pro_csrf_valid():
            return pro_shell("Richiesta non valida", '<section class="card"><h1>Richiesta non valida</h1><p>Ricarica la pagina e riprova.</p></section>'), 400
        if hmac.compare_digest(request.form.get("admin_token", ""), PROFESSIONISTI_ADMIN_TOKEN):
            session["professionisti_admin"] = True
    if not session.get("professionisti_admin"):
        token = escape(pro_csrf_token(), quote=True)
        return pro_shell("Accesso amministrazione", f'<section class="card"><h1>Revisione profili</h1><form method="post"><input type="hidden" name="csrf_token" value="{token}"><label for="admin_token">Token amministrativo</label><input id="admin_token" name="admin_token" type="password" autocomplete="current-password" required><button class="btn" type="submit">Accedi</button></form></section>')
    db = professionisti_db()
    try:
        if request.method == "POST" and request.form.get("action") in ("approve", "suspend"):
            if not pro_csrf_valid():
                return pro_shell("Richiesta non valida", '<section class="card"><h1>Richiesta non valida</h1><p>Ricarica la pagina e riprova.</p></section>'), 400
            try: pid = int(request.form.get("professional_id", ""))
            except ValueError: pid = 0
            status = "approved" if request.form.get("action") == "approve" else "suspended"
            db.execute("UPDATE professionisti SET status=?,updated_at=? WHERE id=?", (status,datetime.now(ROME_TZ).isoformat(timespec="seconds"),pid)); db.commit()
        rows = db.execute("SELECT id,email,nome_pubblico,categoria,citta,albo,status,created_at FROM professionisti ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END,created_at DESC").fetchall()
    finally: db.close()
    cards=[]
    for row in rows:
        tok=escape(pro_csrf_token(), quote=True)
        actions=f'''<form method="post"><input type="hidden" name="csrf_token" value="{tok}"><input type="hidden" name="professional_id" value="{row['id']}"><button class="btn" name="action" value="approve">Approva</button> <button class="btn secondary" name="action" value="suspend">Sospendi</button></form>'''
        cards.append(f'''<article class="profile"><h3>{escape(row['nome_pubblico'])}</h3><p>{escape(row['categoria'])} · {escape(row['citta'])}</p><p>{escape(row['albo'])}</p><p>{escape(row['email'])} · stato: <strong>{escape(row['status'])}</strong></p>{actions}</article>''')
    return pro_shell("Revisione profili", '<section class="card"><h1>Revisione profili professionali</h1><p>Controlla l’identità professionale e l’iscrizione all’Ordine prima di approvare un profilo. La verifica è manuale.</p><div class="grid">'+(''.join(cards) if cards else '<p>Nessuna registrazione.</p>')+'</div></section>')


@app.route("/professionisti/contratto")
def professionisti_contratto():
    if not professionisti_registration_ready():
        return pro_shell("Bozza contrattuale non ancora disponibile", '<section class="card"><h1>Bozza contrattuale in revisione</h1><p>Il contratto non è ancora disponibile per la sottoscrizione. L’area professionisti e le condizioni economiche restano sospese fino alla verifica legale e deontologica.</p></section>'), 503
    user = pro_current_user()
    if not user:
        return redirect("/professionisti/login", code=303)
    body = '''<article class="card doc"><div class="no-print"><p class="notice"><strong>Bozza da sottoporre a revisione legale prima della firma.</strong> La pubblicazione di questa bozza non costituisce attestazione di conformità deontologica. Le funzioni di invio di lead e i relativi corrispettivi restano sospesi fino all’approvazione del testo definitivo.</p><button class="btn" onclick="window.print()" type="button">Stampa / salva in PDF</button></div><h1>Contratto di adesione e servizi di presenza professionale FixTude</h1><p>Bozza del 19 ottobre 2026 — da completare con i dati del professionista e approvare prima dell’uso.</p><p><strong>Tra</strong> Giovanni Pugliese, titolare del servizio online FixTude, con domicilio professionale in Via Saronno 65, 00188 Roma, P. IVA 15990471003, email info@fixtude.it (di seguito “Gestore”);</p><p><strong>e</strong> il professionista identificato in calce (di seguito “Professionista”).</p><h2>1. Oggetto</h2><p>Il contratto disciplina l’eventuale adesione del Professionista alla vetrina FixTude, la gestione del proprio account e la pubblicazione di un profilo professionale previa verifica e approvazione. FixTude non svolge attività riservate al Professionista e non assume il mandato del cliente.</p><h2>2. Autonomia professionale</h2><p>Il Professionista opera in piena autonomia, sotto la propria responsabilità, nel rispetto delle norme di legge, delle regole dell’Ordine di appartenenza e dei doveri di indipendenza, competenza, riservatezza e correttezza. Nessun rapporto di lavoro subordinato, associazione professionale o società tra le parti è creato dal presente contratto.</p><h2>3. Profilo e verifica</h2><p>Il Professionista garantisce la veridicità e l’aggiornamento delle informazioni, dichiara di essere regolarmente iscritto all’Ordine/Albo ove richiesto e autorizza la pubblicazione dei soli dati professionali approvati: nome/denominazione, categoria, città, Ordine/Albo e presentazione. Telefono, email, chat e collegamenti di contatto diretto non saranno mostrati nella vetrina pubblica. Il Gestore può chiedere documentazione verificabile e sospendere il profilo in caso di informazioni incomplete o non corrette.</p><h2>4. Servizi, corrispettivi e attivazione</h2><p>La vetrina e l’account sono in fase di attivazione. <strong>Alla data della presente bozza non è attivo alcun piano a pagamento e non è formulata un’offerta economica vincolante.</strong> Eventuali corrispettivi, durata, rinnovo, prestazioni incluse, imposte e condizioni di cessazione dovranno essere indicati in un ordine di attivazione completo, sottoscritto da entrambe le parti dopo revisione legale.</p><p>Qualsiasi servizio che preveda quantità, inoltro, assegnazione o priorità di richieste/lead è escluso dal presente testo e non potrà essere attivato né fatturato senza uno specifico allegato approvato dopo una verifica legale e deontologica dedicata. Nessun corrispettivo è dovuto fino alla sottoscrizione dell’ordine definitivo e all’attivazione esplicita del servizio.</p><h2>5. Divieto di compensi sui mandati</h2><p>Il Gestore non percepisce percentuali, commissioni o partecipazioni sui compensi professionali, sugli accordi transattivi o sui risultati delle pratiche. Il Professionista determina autonomamente i propri compensi, concordandoli direttamente con il cliente nel mandato professionale scritto. Nessun esito è garantito da FixTude.</p><h2>6. Contatti e incarichi</h2><p>Il presente contratto non conferisce al Gestore poteri di rappresentanza o negoziazione per conto del Professionista o del cliente. Un eventuale incarico professionale è conferito direttamente dal cliente al Professionista mediante documento separato, con descrizione delle attività, compensi, spese, tempi, informativa privacy e condizioni applicabili.</p><h2>7. Dati personali e riservatezza</h2><p>Ciascuna parte tratta i dati personali nel rispetto della normativa applicabile. Il Professionista non deve inserire nella vetrina dati di clienti, informazioni coperte da segreto o documenti riservati. I ruoli privacy, le basi giuridiche, i tempi di conservazione e le eventuali responsabilità connesse all’inoltro di richieste dovranno essere descritti nell’informativa e negli eventuali accordi dedicati prima di attivare tali funzioni.</p><h2>8. Durata, rinnovo e recesso</h2><p>La durata, il rinnovo, il preavviso e le modalità di cessazione saranno indicati nell’ordine di attivazione sottoscritto. In assenza di ordine attivo non sussiste abbonamento né obbligo di pagamento. Ciascuna parte può chiedere la correzione o la rimozione di dati professionali non aggiornati; il Gestore può sospendere un profilo per ragioni documentate di sicurezza, correttezza o conformità normativa.</p><h2>9. Limitazioni e responsabilità</h2><p>Il Gestore fornisce un servizio di presenza online e strumenti informativi, senza garantire un numero di clienti, un volume di richieste, l’accettazione di incarichi o risultati professionali. Il Professionista resta responsabile delle prestazioni rese e delle verifiche relative al cliente e al conflitto d’interessi. Restano ferme le responsabilità inderogabili previste dalla legge.</p><h2>10. Reclami e legge applicabile</h2><p>Le comunicazioni relative al servizio possono essere inviate a info@fixtude.it. Il contratto è soggetto alla legge italiana, fatti salvi i fori inderogabili e le norme imperative applicabili. Ogni clausola va interpretata in modo compatibile con le norme professionali e deontologiche pertinenti.</p><h2>11. Approvazioni specifiche</h2><p>Le eventuali clausole che richiedano approvazione specifica ai sensi degli artt. 1341 e 1342 c.c. dovranno essere individuate e approvate separatamente nella versione definitiva dopo revisione legale.</p><div class="signature"><div><p><strong>Il Gestore</strong></p><div class="line">Giovanni Pugliese — FixTude</div><div class="line">Data e firma</div></div><div><p><strong>Il Professionista</strong></p><div class="line">Nome / denominazione</div><div class="line">Codice fiscale / P. IVA</div><div class="line">Ordine/Albo e provincia</div><div class="line">Data e firma</div></div></div></article>'''
    return pro_shell("Bozza contratto professionisti", body)


@app.route("/incarico-legale")
def bozza_incarico_legale():
    body = '''<article class="card doc"><div class="no-print"><p class="notice"><strong>Fac-simile informativo da completare e validare dall’avvocato incaricato.</strong> Non è un mandato già conferito e non sostituisce il preventivo, l’informativa sul trattamento dei dati o gli altri documenti richiesti nel caso concreto.</p><button class="btn" onclick="window.print()" type="button">Stampa / salva in PDF</button></div><h1>Fac-simile di conferimento dell’incarico professionale</h1><p><strong>Cliente:</strong> ______________________________________________</p><p>Codice fiscale: __________________________ Indirizzo: __________________________________________</p><p>Email/PEC per comunicazioni: _________________________________________________________________</p><p><strong>Avvocato incaricato:</strong> __________________________________________, C.F./P. IVA __________________</p><p>Ordine degli Avvocati di __________________________, studio in ____________________________________</p><h2>1. Oggetto dell’incarico</h2><p>Il Cliente conferisce all’Avvocato, che accetta, l’incarico limitato alle seguenti attività e posizioni debitorie: ______________________________________________________________________________________________.</p><p>Creditori/controparte e riferimenti dei documenti: __________________________________________________.</p><h2>2. Attività incluse e limiti</h2><p>Attività stragiudiziali incluse: ____________________________________________________________________.</p><p>Eventuale attività giudiziale, procedimento, autorità e grado inclusi: ___________________________________.</p><p>Restano escluse le attività non espressamente indicate, i nuovi procedimenti e i gradi successivi, salvo separato accordo scritto.</p><h2>3. Compenso, preventivo e spese</h2><p>Compenso concordato, criteri e scadenze: __________________________________________________________.</p><p>IVA, contributo previdenziale, spese generali, anticipazioni e spese vive: ________________________________.</p><p>Eventuali somme pagate a FixTude per prodotti o servizi distinti non costituiscono automaticamente compenso dell’Avvocato, salvo accordo espresso e documentato tra le parti interessate.</p><h2>4. Obblighi e autonomia</h2><p>L’Avvocato opera in autonomia e secondo le regole professionali applicabili, senza garantire il risultato. Il Cliente si impegna a fornire documenti veritieri e completi, comunicare tempestivamente atti e scadenze e informare l’Avvocato di ogni cambiamento rilevante.</p><h2>5. Informazioni, comunicazioni e privacy</h2><p>Canali concordati per le comunicazioni: ___________________________________________________________.</p><p>L’informativa privacy, le modalità di conservazione dei documenti, i soggetti autorizzati e le eventuali comunicazioni a terzi sono descritti nell’informativa separata consegnata al Cliente.</p><h2>6. Durata, revoca e rinuncia</h2><p>L’incarico decorre dalla sottoscrizione e termina con: __________________________________________________. Revoca e rinuncia sono disciplinate dalla legge e dalle regole professionali applicabili, con liquidazione delle attività svolte e delle spese dovute secondo l’accordo e la normativa.</p><h2>7. Documenti allegati</h2><p>☐ Preventivo dettagliato · ☐ Informativa privacy · ☐ Documenti del Cliente · ☐ Altro: _______________________</p><div class="signature"><div><div class="line">Luogo, data e firma del Cliente</div></div><div><div class="line">Luogo, data e firma dell’Avvocato</div></div></div><p class="muted">Il professionista deve adattare il fac-simile al caso concreto e verificare gli obblighi informativi e formali applicabili prima della sottoscrizione.</p></article>'''
    return pro_shell("Fac-simile incarico legale", body)


@app.route("/health")
def health():
    return jsonify(status="ok", service="FixTude")


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=False
    )


