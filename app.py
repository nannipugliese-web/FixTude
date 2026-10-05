from flask import (
    Flask,
    request,
    redirect,
    url_for,
    session,
    render_template_string,
    send_file,
    abort
)

import sqlite3
import os
import json
import uuid
import secrets
import smtplib
from email.message import EmailMessage
from datetime import datetime, timezone
from pathlib import Path

from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

import stripe


# ============================================================
# FIXTUDE
# ============================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "FIXTUDE_SECRET",
    "fixtude-dev-secret-change-later"
)

BASE_DIR = Path(__file__).resolve().parent

DB_PATH = BASE_DIR / "fixtude.db"
UPLOAD_DIR = BASE_DIR / "uploads"
PDF_DIR = BASE_DIR / "generated_pdfs"

UPLOAD_DIR.mkdir(exist_ok=True)
PDF_DIR.mkdir(exist_ok=True)


# ============================================================
# STRIPE
# ============================================================

STRIPE_SECRET_KEY = os.environ.get(
    "STRIPE_SECRET_KEY",
    ""
).strip()

STRIPE_WEBHOOK_SECRET = os.environ.get(
    "STRIPE_WEBHOOK_SECRET",
    ""
).strip()

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


PAYMENT_SERVICES = {
    "analysis": {
        "name": "Analisi FixTude",
        "description": "Analisi automatica della situazione economica e debitoria con elaborazione di possibili scenari.",
        "amount": 199,
        "currency": "eur"
    },
    "pdf": {
        "name": "Documento FixTude",
        "description": "Documento PDF definitivo validato dal Risolutore.",
        "amount": 999,
        "currency": "eur"
    }
}


# ============================================================
# DEMO USERS
# ============================================================

DEMO_USERS = {
    "demo@fixtude.it": {
        "password": "1234",
        "role": "debtor"
    },
    "pro@fixtude.it": {
        "password": "1234",
        "role": "resolver"
    }
}


# ============================================================
# UTILITY
# ============================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def current_user():
    return session.get("user")


def require_login(role=None):
    user = current_user()

    if not user:
        return None

    if role and user.get("role") != role:
        return None

    return user


def parse_float(value):
    if value is None:
        return 0.0

    text = str(value).strip()

    text = (
        text
        .replace("€", "")
        .replace(" ", "")
    )

    if not text:
        return 0.0

    if "," in text and "." in text:

        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "")
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")

    elif "," in text:
        text = text.replace(".", "")
        text = text.replace(",", ".")

    try:
        return float(text)

    except ValueError:
        return 0.0


def money(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


# ============================================================
# DATABASE
# ============================================================

def init_db():

    conn = db_connect()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            data TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS debts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER NOT NULL,
            creditor TEXT NOT NULL,
            debt_type TEXT,
            current_amount REAL DEFAULT 0,
            monthly_payment REAL DEFAULT 0,
            notes TEXT,
            created_at TEXT,
            FOREIGN KEY(case_id)
                REFERENCES cases(id)
                ON DELETE CASCADE
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER NOT NULL,
            analysis_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(case_id)
                REFERENCES cases(id)
                ON DELETE CASCADE
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS solution_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER NOT NULL,
            analysis_id INTEGER,
            title TEXT NOT NULL,
            solution_type TEXT NOT NULL,
            content TEXT NOT NULL,
            pdf_path TEXT,
            status TEXT NOT NULL DEFAULT 'pending_review',
            supervisor_note TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            approved_at TEXT,
            sent_at TEXT,
            FOREIGN KEY(case_id)
                REFERENCES cases(id)
                ON DELETE CASCADE,
            FOREIGN KEY(analysis_id)
                REFERENCES ai_analyses(id)
                ON DELETE SET NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS supervision (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            solution_id INTEGER NOT NULL,
            original_content TEXT NOT NULL,
            corrected_content TEXT NOT NULL,
            correction_note TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(solution_id)
                REFERENCES solution_documents(id)
                ON DELETE CASCADE
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            notification_type TEXT NOT NULL DEFAULT 'in_app',
            read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(case_id)
                REFERENCES cases(id)
                ON DELETE CASCADE
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'debtor',
            created_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            token TEXT UNIQUE NOT NULL,
            expires_at TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_email TEXT NOT NULL,
            case_id INTEGER NOT NULL,
            service TEXT NOT NULL,
            amount INTEGER NOT NULL,
            currency TEXT NOT NULL DEFAULT 'eur',
            stripe_session_id TEXT UNIQUE,
            stripe_payment_intent_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            paid_at TEXT,
            FOREIGN KEY(case_id)
                REFERENCES cases(id)
                ON DELETE CASCADE
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_payments_user_case_service
        ON payments(user_email, case_id, service)
    """)

    for email, data in DEMO_USERS.items():

        existing = conn.execute(
            "SELECT id FROM users WHERE email = ?",
            (email,)
        ).fetchone()

        if not existing:

            conn.execute(
                """
                INSERT INTO users
                (email, password_hash, role, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    email,
                    generate_password_hash(data["password"]),
                    data["role"],
                    now_iso()
                )
            )

    conn.commit()
    conn.close()


init_db()


# ============================================================
# USERS
# ============================================================

def find_user(email):

    email = (
        email or ""
    ).strip().lower()

    conn = db_connect()

    row = conn.execute(
        """
        SELECT *
        FROM users
        WHERE email = ?
        """,
        (email,)
    ).fetchone()

    conn.close()

    return row


# ============================================================
# PASSWORD RECOVERY
# ============================================================

def send_password_reset_email(email, reset_url):
    host = os.environ.get("SMTP_HOST", "").strip()
    port = int(os.environ.get("SMTP_PORT", "587") or 587)
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    sender = os.environ.get("SMTP_FROM_EMAIL", "").strip() or username
    sender_name = os.environ.get("SMTP_FROM_NAME", "FixTude").strip()

    if not host or not sender:
        return False

    msg = EmailMessage()
    msg["Subject"] = "Recupero password FixTude"
    msg["From"] = f"{sender_name} <{sender}>"
    msg["To"] = email
    msg.set_content(
        "Hai richiesto il recupero della password FixTude.\n\n"
        "Apri questo collegamento per impostare una nuova password:\n"
        f"{reset_url}\n\n"
        "Il collegamento è valido per 60 minuti. Se non hai fatto tu la richiesta, ignora questa email."
    )

    with smtplib.SMTP(host, port, timeout=15) as smtp:
        smtp.starttls()
        if username and password:
            smtp.login(username, password)
        smtp.send_message(msg)
    return True


def create_password_reset_token(user_id):
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc).timestamp() + 3600
    expires_iso = datetime.fromtimestamp(expires, timezone.utc).isoformat(timespec="seconds")
    conn = db_connect()
    conn.execute("UPDATE password_reset_tokens SET used = 1 WHERE user_id = ? AND used = 0", (user_id,))
    conn.execute(
        "INSERT INTO password_reset_tokens (user_id, token, expires_at, used, created_at) VALUES (?, ?, ?, 0, ?)",
        (user_id, token, expires_iso, now_iso())
    )
    conn.commit()
    conn.close()
    return token


def get_valid_reset_user(token):
    conn = db_connect()
    row = conn.execute(
        """
        SELECT u.id, u.email, u.role, r.id AS reset_id, r.expires_at
        FROM password_reset_tokens r
        JOIN users u ON u.id = r.user_id
        WHERE r.token = ? AND r.used = 0
        """,
        (token,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    try:
        if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
            return None
    except Exception:
        return None
    return row


# ============================================================
# CASES
# ============================================================

def get_case(case_id):

    conn = db_connect()

    row = conn.execute(
        """
        SELECT *
        FROM cases
        WHERE id = ?
        """,
        (case_id,)
    ).fetchone()

    conn.close()

    return row


def get_case_for_email(email):

    conn = db_connect()

    row = conn.execute(
        """
        SELECT *
        FROM cases
        WHERE email = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (email,)
    ).fetchone()

    conn.close()

    return row


def get_case_data(case_id):

    row = get_case(case_id)

    if not row:
        return {}

    try:
        return json.loads(
            row["data"] or "{}"
        )
    except Exception:
        return {}


def get_debts(case_id):

    conn = db_connect()

    rows = conn.execute(
        """
        SELECT *
        FROM debts
        WHERE case_id = ?
        ORDER BY id
        """,
        (case_id,)
    ).fetchall()

    conn.close()

    return rows


# ============================================================
# ANALYSIS
# ============================================================

def calculate_case(case_id):

    data = get_case_data(case_id)
    debts = get_debts(case_id)

    incomes = data.get(
        "incomes",
        []
    ) or []

    expenses = data.get(
        "expenses",
        []
    ) or []

    total_income = sum(
        parse_float(item.get("amount"))
        for item in incomes
        if isinstance(item, dict)
    )

    total_expenses = sum(
        parse_float(item.get("amount"))
        for item in expenses
        if isinstance(item, dict)
    )

    monthly_capacity = (
        total_income -
        total_expenses
    )

    total_debt = sum(
        money(row["current_amount"])
        for row in debts
    )

    total_payments = sum(
        money(row["monthly_payment"])
        for row in debts
    )

    if monthly_capacity <= 0:

        sustainability = "critica"

    elif monthly_capacity < total_payments:

        sustainability = "debole"

    elif total_payments <= 0:

        sustainability = "da valutare"

    elif total_payments <= monthly_capacity * 0.30:

        sustainability = "buona"

    else:

        sustainability = "sotto pressione"

    warnings = []

    if total_income <= 0:
        warnings.append(
            "Non risultano entrate mensili valorizzate."
        )

    if monthly_capacity <= 0:
        warnings.append(
            "Le spese indicate assorbono interamente o superano le entrate."
        )

    if total_debt <= 0:
        warnings.append(
            "Non risultano debiti valorizzati."
        )

    if total_payments > monthly_capacity > 0:
        warnings.append(
            "Le rate indicate superano la disponibilità teorica mensile."
        )

    if not data.get("employment"):
        warnings.append(
            "La situazione lavorativa non è stata indicata."
        )

    return {
        "total_income": round(total_income, 2),
        "total_expenses": round(total_expenses, 2),
        "monthly_capacity": round(monthly_capacity, 2),
        "total_debt": round(total_debt, 2),
        "total_payments": round(total_payments, 2),
        "sustainability": sustainability,
        "warnings": warnings,
        "debts": debts,
        "data": data
    }


def build_scenarios(calc):

    capacity = calc["monthly_capacity"]
    debt = calc["total_debt"]
    payments = calc["total_payments"]

    scenarios = []

    if debt <= 0:

        return [{
            "type": "raccolta_dati",
            "title": "Completamento del quadro",
            "description": (
                "Prima di formulare una proposta economica "
                "è necessario valorizzare almeno una posizione debitoria."
            ),
            "estimated_monthly": 0,
            "priority": "alta"
        }]

    if capacity > 0:

        sustainable = min(
            capacity * 0.30,
            payments if payments > 0 else capacity * 0.30
        )

        sustainable = max(
            50,
            round(sustainable, 2)
        )

        months = max(
            1,
            round(debt / sustainable)
        )

        scenarios.append({
            "type": "piano_rientro",
            "title": "Piano di rientro sostenibile",
            "description": (
                "Ipotesi di rata costruita partendo "
                "dalla disponibilità teorica mensile indicata. "
                "È una simulazione e non una proposta vincolante."
            ),
            "estimated_monthly": sustainable,
            "months": months,
            "priority": (
                "alta"
                if payments > capacity
                else "media"
            )
        })

        if debt > 5000:

            target = round(
                debt * 0.70,
                2
            )

            settlement_monthly = max(
                50,
                round(capacity * 0.25, 2)
            )

            months2 = max(
                1,
                round(
                    target /
                    settlement_monthly
                )
            )

            scenarios.append({
                "type": "saldo_stralcio",
                "title": "Ipotesi di definizione transattiva",
                "description": (
                    "Possibile scenario da approfondire "
                    "con il creditore, subordinato alla disponibilità "
                    "di una somma e all'accettazione della controparte."
                ),
                "estimated_amount": target,
                "estimated_monthly": settlement_monthly,
                "months": months2,
                "priority": "media"
            })

    scenarios.append({
        "type": "rinegoziazione",
        "title": "Richiesta di rinegoziazione",
        "description": (
            "Richiesta di riduzione della rata o di diversa "
            "articolazione dei pagamenti."
        ),
        "estimated_monthly": max(
            0,
            round(
                min(
                    payments,
                    max(capacity * 0.30, 0)
                ),
                2
            )
        ),
        "priority": "media"
    })

    if calc["sustainability"] == "critica":

        scenarios.append({
            "type": "approfondimento_professionale",
            "title": "Approfondimento con professionista qualificato",
            "description": (
                "La sostenibilità corrente risulta critica. "
                "Il caso merita una valutazione professionale."
            ),
            "estimated_monthly": 0,
            "priority": "alta"
        })

    return scenarios[:4]


def latest_analysis(case_id):

    conn = db_connect()

    row = conn.execute(
        """
        SELECT *
        FROM ai_analyses
        WHERE case_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (case_id,)
    ).fetchone()

    conn.close()

    if not row:
        return None

    try:
        return json.loads(
            row["analysis_json"]
        )
    except Exception:
        return None


# ============================================================
# SOLUTIONS
# ============================================================

def get_solutions(case_id):

    conn = db_connect()

    rows = conn.execute(
        """
        SELECT *
        FROM solution_documents
        WHERE case_id = ?
        ORDER BY id
        """,
        (case_id,)
    ).fetchall()

    conn.close()

    return rows


def build_analysis_text(calc):

    text = []

    text.append(
        "FixTude ha elaborato una prima valutazione "
        "automatica della situazione inserita."
    )

    text.append(
        f"Entrate mensili: € {calc['total_income']:.2f}."
    )

    text.append(
        f"Spese mensili: € {calc['total_expenses']:.2f}."
    )

    text.append(
        f"Disponibilità teorica: € {calc['monthly_capacity']:.2f}."
    )

    text.append(
        f"Debito complessivo: € {calc['total_debt']:.2f}."
    )

    text.append(
        f"Rate mensili: € {calc['total_payments']:.2f}."
    )

    text.append(
        "Questa elaborazione è informativa e simulativa "
        "e non costituisce parere legale né garanzia "
        "di accettazione da parte dei creditori."
    )

    return "\n\n".join(text)


# ============================================================
# PDF
# ============================================================

def generate_pdf(solution_id):

    try:

        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import (
            SimpleDocTemplate,
            Paragraph,
            Spacer
        )
        from reportlab.lib.units import mm

    except Exception as exc:

        raise RuntimeError(
            "ReportLab non installato: "
            f"{exc}"
        )

    conn = db_connect()

    solution = conn.execute(
        """
        SELECT *
        FROM solution_documents
        WHERE id = ?
        """,
        (solution_id,)
    ).fetchone()

    conn.close()

    if not solution:
        raise FileNotFoundError(
            "Documento non trovato."
        )

    path = PDF_DIR / (
        f"fixtude_soluzione_{solution_id}.pdf"
    )

    styles = getSampleStyleSheet()

    document = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm
    )

    story = []

    story.append(
        Paragraph(
            solution["title"],
            styles["Title"]
        )
    )

    story.append(
        Spacer(1, 8 * mm)
    )

    for line in solution["content"].split("\n"):

        if line.strip():

            story.append(
                Paragraph(
                    line.replace(
                        "&",
                        "&amp;"
                    ),
                    styles["BodyText"]
                )
            )

            story.append(
                Spacer(1, 3 * mm)
            )

    story.append(
        Spacer(1, 5 * mm)
    )

    story.append(
        Paragraph(
            "Documento informativo generato da FixTude. "
            "Non costituisce parere legale o finanziario.",
            styles["BodyText"]
        )
    )

    document.build(story)

    return str(path)


# ============================================================
# LOCAL AGENT
# ============================================================

def run_local_agent(case_id):

    calc = calculate_case(case_id)

    scenarios = build_scenarios(
        calc
    )

    analysis_text = build_analysis_text(
        calc
    )

    created = now_iso()

    analysis_payload = {
        "created_at": created,
        "summary": analysis_text,
        "metrics": {
            "total_income": calc["total_income"],
            "total_expenses": calc["total_expenses"],
            "monthly_capacity": calc["monthly_capacity"],
            "total_debt": calc["total_debt"],
            "total_payments": calc["total_payments"],
            "sustainability": calc["sustainability"]
        },
        "warnings": calc["warnings"],
        "scenarios": scenarios
    }

    conn = db_connect()

    cur = conn.execute(
        """
        INSERT INTO ai_analyses
        (case_id, analysis_json, created_at)
        VALUES (?, ?, ?)
        """,
        (
            case_id,
            json.dumps(
                analysis_payload,
                ensure_ascii=False
            ),
            created
        )
    )

    analysis_id = cur.lastrowid

    for scenario in scenarios:

        content = (
            f"Scenario: {scenario['title']}.\n\n"
            f"{scenario['description']}\n\n"
            f"Priorità: {scenario.get('priority', 'media')}.\n"
        )

        if scenario.get(
            "estimated_monthly"
        ):

            content += (
                f"\nRata mensile simulata: "
                f"€ {scenario['estimated_monthly']:.2f}."
            )

        if scenario.get(
            "estimated_amount"
        ):

            content += (
                f"\nImporto transattivo simulato: "
                f"€ {scenario['estimated_amount']:.2f}."
            )

        cur = conn.execute(
            """
            INSERT INTO solution_documents
            (
                case_id,
                analysis_id,
                title,
                solution_type,
                content,
                status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                case_id,
                analysis_id,
                scenario["title"],
                scenario["type"],
                content,
                "pending_review",
                created,
                created
            )
        )

        solution_id = cur.lastrowid

        conn.commit()

        try:

            pdf_path = generate_pdf(
                solution_id
            )

            conn.execute(
                """
                UPDATE solution_documents
                SET pdf_path = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    pdf_path,
                    now_iso(),
                    solution_id
                )
            )

        except Exception:

            pass

    conn.commit()
    conn.close()

    return analysis_payload


# ============================================================
# PAYMENTS
# ============================================================

def get_paid_payment(
    email,
    case_id,
    service
):

    conn = db_connect()

    row = conn.execute(
        """
        SELECT *
        FROM payments
        WHERE user_email = ?
          AND case_id = ?
          AND service = ?
          AND status = 'paid'
        ORDER BY id DESC
        LIMIT 1
        """,
        (
            email,
            case_id,
            service
        )
    ).fetchone()

    conn.close()

    return row


def get_payment(payment_id):

    conn = db_connect()

    row = conn.execute(
        """
        SELECT *
        FROM payments
        WHERE id = ?
        """,
        (payment_id,)
    ).fetchone()

    conn.close()

    return row


def mark_payment_paid(
    payment_id,
    payment_intent=None
):

    conn = db_connect()

    payment = conn.execute(
        """
        SELECT *
        FROM payments
        WHERE id = ?
        """,
        (payment_id,)
    ).fetchone()

    if not payment:

        conn.close()
        return False

    conn.execute(
        """
        UPDATE payments
        SET status = 'paid',
            stripe_payment_intent_id = ?,
            paid_at = ?
        WHERE id = ?
        """,
        (
            payment_intent,
            now_iso(),
            payment_id
        )
    )

    conn.commit()
    conn.close()

    return True


# ============================================================
# HOME
# ============================================================

HOME_HTML = """
<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport"
content="width=device-width,initial-scale=1">
<title>FixTude</title>
<style>
body{
font-family:Arial,sans-serif;
margin:0;
background:#f6f8fb;
color:#18212f
}
.wrap{
max-width:1100px;
margin:auto;
padding:25px
}
nav{
display:flex;
justify-content:space-between;
align-items:center
}
.logo{
font-size:30px;
font-weight:800
}
.logo span{
color:#4f46e5
}
.grid{
display:grid;
grid-template-columns:1fr 1fr;
gap:25px;
margin-top:45px
}
.card{
background:white;
padding:35px;
border-radius:20px;
border:1px solid #e5e7eb;
box-shadow:0 15px 40px rgba(0,0,0,.05)
}
.dark{
background:#18212f;
color:white
}
h1{
font-size:46px;
margin:15px 0
}
p{
line-height:1.6
}
.button{
display:inline-block;
padding:13px 18px;
border-radius:9px;
text-decoration:none;
font-weight:700;
margin:5px
}
.primary{
background:#4f46e5;
color:white
}
.light{
background:#eef0f4;
color:#18212f
}
.free{
display:grid;
grid-template-columns:1fr 1fr;
gap:10px
}
.free a{
padding:20px;
border-radius:12px;
background:#293241;
color:white;
text-decoration:none
}
small{
color:#697586
}
@media(max-width:800px){
.grid{
grid-template-columns:1fr
}
h1{
font-size:36px
}
}
</style>
</head>
<body>

<div class="wrap">

<nav>
<div class="logo">
Fix<span>Tude</span>
</div>
<div>
<a href="/privato/login">Accedi</a>
&nbsp;&nbsp;
<a href="/registrazione">Registrati</a>
</div>
</nav>

<div class="grid">

<div class="card">

<small>SERVIZIO FIXTUDE</small>

<h1>
Metti in ordine la tua situazione debitoria
</h1>

<p>
Inserisci dati, entrate, spese e debiti.
FixTude organizza la situazione e produce
possibili scenari da approfondire.
</p>

<p>
<strong>✓ Analisi automatica</strong><br>
<strong>✓ Possibili scenari</strong><br>
<strong>✓ Risolutore AI</strong><br>
<strong>✓ Documenti PDF</strong>
</p>

<a class="button primary"
href="/registrazione">
Registrati gratuitamente
</a>

<a class="button light"
href="/privato/login">
Accedi
</a>

</div>

<div class="card dark">

<small style="color:#bfc6d4">
SERVIZIO GRATUITO
</small>

<h2 style="white-space:nowrap">
Controlla autonomamente le tue banche dati
</h2>

<p>
Puoi richiedere direttamente agli enti
le informazioni che ti riguardano.
</p>

<div class="free">

<a target="_blank"
href="https://www.modulorichiesta.crif.com/">
<strong>CRIF</strong><br>
Modulo ufficiale
</a>

<a target="_blank"
href="https://www.experian.it/content/dam/noindex/emea/italy/Nuovo-modulo-SIC.pdf">
<strong>EXPERIAN</strong><br>
Modulo ufficiale
</a>

<a target="_blank"
href="https://consumatore.ctconline.it/sic/apri-istanza">
<strong>CTC</strong><br>
Procedura ufficiale
</a>

<a target="_blank"
href="https://www.bancaditalia.it/servizi-cittadino/servizi/accesso-cai/Modulo-di-richiesta-dei-dati-nominativi-CAI.pdf?force_download=1">
<strong>CAI</strong><br>
Modulo ufficiale
</a>

</div>

</div>

</div>

<footer style="margin-top:60px;padding:25px 0;border-top:1px solid #ddd">
FixTude · info@fixtude.it · P. IVA: DA INSERIRE
</footer>

</div>

</body>
</html>
"""


@app.route("/")
def home():

    return render_template_string(
        HOME_HTML
    )


# ============================================================
# REGISTRATION
# ============================================================

REGISTRATION_HTML = """
<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport"
content="width=device-width,initial-scale=1">
<title>Registrazione FixTude</title>
<style>
body{
font-family:Arial;
background:#f6f8fb
}
.box{
max-width:450px;
margin:70px auto;
background:white;
padding:35px;
border-radius:18px
}
input,select{
width:100%;
box-sizing:border-box;
padding:13px;
margin:8px 0 15px
}
button{
width:100%;
padding:13px;
background:#4f46e5;
color:white;
border:0;
border-radius:8px
}
.error{
background:#fee2e2;
padding:12px;
margin-bottom:15px
}
</style>
</head>
<body>

<div class="box">

<a href="/">← FixTude</a>

<h1>Crea il tuo account</h1>

{% if error %}
<div class="error">{{ error }}</div>
{% endif %}

<form method="post">

<label>Email</label>
<input type="email"
name="email"
required>

<label>Password</label>
<div class="password-wrap">
<input id="reg-password" type="password" name="password" required minlength="8">
<button type="button" class="toggle-password" onclick="togglePassword('reg-password', this)">👁</button>
</div>

<label>Conferma password</label>
<div class="password-wrap">
<input id="reg-confirm" type="password" name="confirm_password" required minlength="8">
<button type="button" class="toggle-password" onclick="togglePassword('reg-confirm', this)">👁</button>
</div>

<button>
Crea account
</button>

</form>

</div>
<script>
function togglePassword(id, button){const input=document.getElementById(id); if(input.type==='password'){input.type='text';button.textContent='🙈'}else{input.type='password';button.textContent='👁'}}
</script>
</body>
</html>
"""


@app.route(
    "/registrazione",
    methods=["GET", "POST"]
)
def registration():

    error = None

    if request.method == "POST":

        email = (
            request.form
            .get("email", "")
            .strip()
            .lower()
        )

        password = request.form.get(
            "password",
            ""
        )

        confirm = request.form.get(
            "confirm_password",
            ""
        )

        if "@" not in email:

            error = "Email non valida."

        elif len(password) < 8:

            error = (
                "La password deve avere "
                "almeno 8 caratteri."
            )

        elif password != confirm:

            error = (
                "Le password non coincidono."
            )

        elif find_user(email):

            error = (
                "Email già registrata."
            )

        else:

            conn = db_connect()

            conn.execute(
                """
                INSERT INTO users
                (email,password_hash,role,created_at)
                VALUES (?,?,'debtor',?)
                """,
                (
                    email,
                    generate_password_hash(
                        password
                    ),
                    now_iso()
                )
            )

            conn.commit()
            conn.close()

            session.clear()

            session["user"] = {
                "email": email,
                "role": "debtor"
            }

            return redirect(
                url_for("debtor_dashboard")
            )

    return render_template_string(
        REGISTRATION_HTML,
        error=error
    )


# ============================================================
# LOGIN
# ============================================================

LOGIN_HTML = """
<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Login FixTude</title>
<style>
body{font-family:Arial;background:#f6f8fb}.box{max-width:430px;margin:80px auto;background:white;padding:35px;border-radius:18px;box-sizing:border-box}input{width:100%;box-sizing:border-box;padding:13px;margin:8px 0 15px}.password-wrap{position:relative}.password-wrap input{padding-right:48px}.toggle-password{position:absolute;right:10px;top:8px;width:38px;height:38px;padding:0;background:transparent;color:#4f46e5;border:0;font-size:19px;cursor:pointer}.button{width:100%;padding:13px;background:#4f46e5;color:white;border:0;border-radius:8px;cursor:pointer}.error{background:#fee2e2;padding:12px;margin-bottom:15px;border-radius:8px}.links{margin-top:18px;text-align:center}.links a{color:#4f46e5;text-decoration:none}
</style>
</head>
<body>
<div class="box">
<a href="/">← FixTude</a>
<h1>{% if role == "debtor" %}Accesso area privata{% else %}Accesso Risolutore{% endif %}</h1>
{% if error %}<div class="error">{{ error }}</div>{% endif %}
<form method="post">
<label>Email</label>
<input type="email" name="email" required>
<label>Password</label>
<div class="password-wrap">
<input id="login-password" type="password" name="password" required>
<button class="toggle-password" type="button" onclick="togglePassword('login-password', this)" aria-label="Mostra password">👁</button>
</div>
<button class="button">Accedi</button>
</form>
<div class="links"><a href="{{ url_for('password_forgot') }}">Password dimenticata?</a></div>
</div>
<script>
function togglePassword(id, button){const input=document.getElementById(id); if(input.type==='password'){input.type='text';button.textContent='🙈';button.setAttribute('aria-label','Nascondi password')}else{input.type='password';button.textContent='👁';button.setAttribute('aria-label','Mostra password')}}
</script>
</body>
</html>
"""

@app.route("/password-dimenticata", methods=["GET", "POST"])
def password_forgot():
    message = None
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = find_user(email)
        if user:
            try:
                token = create_password_reset_token(user["id"])
                base_url = os.environ.get("APP_BASE_URL", request.url_root.rstrip("/"))
                reset_url = f"{base_url}{url_for('password_reset', token=token)}"
                sent = send_password_reset_email(email, reset_url)
                if sent:
                    message = "Se l'indirizzo è registrato, abbiamo inviato le istruzioni per recuperare la password."
                else:
                    error = "Il recupero è predisposto, ma l'invio email non è ancora configurato sul server."
            except Exception:
                error = "Non è stato possibile inviare l'email di recupero. Riprova più tardi."
        else:
            message = "Se l'indirizzo è registrato, abbiamo inviato le istruzioni per recuperare la password."
    return render_template_string("""
    <!doctype html><html lang="it"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Password dimenticata - FixTude</title>
    <style>body{font-family:Arial;background:#f6f8fb}.box{max-width:430px;margin:80px auto;background:white;padding:35px;border-radius:18px;box-sizing:border-box}input{width:100%;box-sizing:border-box;padding:13px;margin:8px 0 15px}button{width:100%;padding:13px;background:#4f46e5;color:white;border:0;border-radius:8px}.msg{background:#e8f7ed;padding:12px;border-radius:8px}.err{background:#fee2e2;padding:12px;border-radius:8px}</style></head><body><div class="box"><a href="/privato/login">← Accesso</a><h1>Recupera password</h1><p>Inserisci l'email con cui hai creato l'account.</p>{% if message %}<div class="msg">{{ message }}</div>{% endif %}{% if error %}<div class="err">{{ error }}</div>{% endif %}<form method="post"><label>Email</label><input type="email" name="email" required><button>Invia istruzioni</button></form></div></body></html>
    """, message=message, error=error)


@app.route("/password-reset/<token>", methods=["GET", "POST"])
def password_reset(token):
    reset_user = get_valid_reset_user(token)
    if not reset_user:
        return render_template_string("""<!doctype html><html lang='it'><body style='font-family:Arial;background:#f6f8fb'><div style='max-width:500px;margin:80px auto;background:white;padding:35px;border-radius:18px'><h1>Link non valido</h1><p>Il collegamento di recupero password è scaduto o non è più valido.</p><a href='{{ url_for("password_forgot") }}'>Richiedi un nuovo link</a></div></body></html>""")
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        if len(password) < 8:
            error = "La password deve avere almeno 8 caratteri."
        elif password != confirm:
            error = "Le password non coincidono."
        else:
            conn = db_connect()
            conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(password), reset_user["id"]))
            conn.execute("UPDATE password_reset_tokens SET used = 1 WHERE id = ?", (reset_user["reset_id"],))
            conn.commit()
            conn.close()
            return redirect(url_for("debtor_login" if reset_user["role"] == "debtor" else "resolver_login"))
    return render_template_string("""
    <!doctype html><html lang="it"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Nuova password - FixTude</title><style>body{font-family:Arial;background:#f6f8fb}.box{max-width:430px;margin:80px auto;background:white;padding:35px;border-radius:18px;box-sizing:border-box}input{width:100%;box-sizing:border-box;padding:13px;margin:8px 0 15px}.password-wrap{position:relative}.password-wrap input{padding-right:48px}.toggle-password{position:absolute;right:10px;top:8px;width:38px;height:38px;padding:0;background:transparent;color:#4f46e5;border:0;font-size:19px;cursor:pointer}.button{width:100%;padding:13px;background:#4f46e5;color:white;border:0;border-radius:8px}.error{background:#fee2e2;padding:12px;border-radius:8px;margin-bottom:15px}</style></head><body><div class="box"><h1>Imposta nuova password</h1>{% if error %}<div class="error">{{ error }}</div>{% endif %}<form method="post"><label>Nuova password</label><div class="password-wrap"><input id="newpw" type="password" name="password" minlength="8" required><button type="button" class="toggle-password" onclick="togglePassword('newpw',this)">👁</button></div><label>Conferma password</label><div class="password-wrap"><input id="newpw2" type="password" name="confirm_password" minlength="8" required><button type="button" class="toggle-password" onclick="togglePassword('newpw2',this)">👁</button></div><button class="button">Salva nuova password</button></form></div><script>function togglePassword(id,b){const i=document.getElementById(id);if(i.type==='password'){i.type='text';b.textContent='🙈'}else{i.type='password';b.textContent='👁'}}</script></body></html>
    """, error=error)


@app.route(
    "/privato/login",
    methods=["GET", "POST"]
)
def debtor_login():

    error = None

    if request.method == "POST":

        email = (
            request.form
            .get("email", "")
            .strip()
            .lower()
        )

        password = request.form.get(
            "password",
            ""
        )

        user = find_user(email)

        valid = bool(
            user
            and user["role"] == "debtor"
            and check_password_hash(
                user["password_hash"],
                password
            )
        )

        if valid:

            session.clear()

            session["user"] = {
                "email": email,
                "role": "debtor"
            }

            return redirect(
                url_for("debtor_dashboard")
            )

        error = (
            "Email o password non corretti."
        )

    return render_template_string(
        LOGIN_HTML,
        role="debtor",
        error=error
    )


@app.route(
    "/risolutore/login",
    methods=["GET", "POST"]
)
def resolver_login():

    error = None

    if request.method == "POST":

        email = (
            request.form
            .get("email", "")
            .strip()
            .lower()
        )

        password = request.form.get(
            "password",
            ""
        )

        user = find_user(email)

        valid = bool(
            user
            and user["role"] == "resolver"
            and check_password_hash(
                user["password_hash"],
                password
            )
        )

        if valid:

            session.clear()

            session["user"] = {
                "email": email,
                "role": "resolver"
            }

            return redirect(
                url_for("resolver_dashboard")
            )

        error = (
            "Email o password non corretti."
        )

    return render_template_string(
        LOGIN_HTML,
        role="resolver",
        error=error
    )


# ============================================================
# DEBTOR DASHBOARD
# ============================================================

@app.route("/privato")
def debtor_dashboard():

    user = require_login(
        "debtor"
    )

    if not user:
        return redirect(
            url_for("debtor_login")
        )

    case = get_case_for_email(
        user["email"]
    )

    return render_template_string(
        """
        <!doctype html>
        <html lang="it">
        <body style="font-family:Arial;background:#f6f8fb">
        <div style="max-width:900px;margin:auto;padding:35px">

        <div style="display:flex;justify-content:space-between;align-items:center;gap:20px">
        <div>
        <h1>Area privata FixTude</h1>
        <p>{{ user.email }}</p>
        </div>
        <div style="display:flex;gap:18px;align-items:center">
        <a href="/" style="text-decoration:none;color:#4f46e5">← Indietro</a>
        <a href="{{ url_for('logout') }}">Esci</a>
        </div>
        </div>

        <div style="background:white;padding:25px;border-radius:15px;margin-top:20px">

        {% if case %}

        <h2>La tua pratica</h2>

        <p>
        La tua situazione è stata inserita.
        </p>

        <p>
        <a href="{{ url_for('case_summary') }}">
        Riepilogo
        </a>
        </p>

        <p>
        <a href="{{ url_for('case_analysis') }}">
        Analizza la situazione
        </a>
        </p>

        {% else %}

        <h2>Inizia da qui</h2>

        <a href="{{ url_for('debtor_situation') }}">
        Inserisci i tuoi dati →
        </a>

        {% endif %}

        <hr>

        <p>
        <a href="{{ url_for('payments') }}">
        Area pagamenti
        </a>
        </p>

        </div>
        </div>
        </body>
        </html>
        """,
        user=user,
        case=case
    )


# ============================================================
# SITUATION
# ============================================================

@app.route(
    "/privato/situazione",
    methods=["GET", "POST"]
)
def debtor_situation():

    user = require_login(
        "debtor"
    )

    if not user:
        return redirect(
            url_for("debtor_login")
        )

    case = get_case_for_email(
        user["email"]
    )

    existing = (
        get_case_data(case["id"])
        if case else {}
    )

    existing_debts = (
        get_debts(case["id"])
        if case else []
    )

    if request.method == "POST":

        data = {
            "name": request.form.get(
                "name",
                ""
            ).strip(),

            "surname": request.form.get(
                "surname",
                ""
            ).strip(),

            "tax_code": request.form.get(
                "tax_code",
                ""
            ).strip().upper(),

            "employment": request.form.get(
                "employment",
                ""
            ).strip(),

            "phone": request.form.get(
                "phone",
                ""
            ).strip(),

            "address": request.form.get(
                "address",
                ""
            ).strip(),

            "incomes": [],

            "expenses": []
        }

        income_labels = request.form.getlist(
            "income_label"
        )

        income_amounts = request.form.getlist(
            "income_amount"
        )

        for label, amount in zip(
            income_labels,
            income_amounts
        ):

            if label.strip() or amount.strip():

                data["incomes"].append({
                    "label": label.strip()
                    or "Entrata",

                    "amount": parse_float(
                        amount
                    )
                })

        expense_labels = request.form.getlist(
            "expense_label"
        )

        expense_amounts = request.form.getlist(
            "expense_amount"
        )

        for label, amount in zip(
            expense_labels,
            expense_amounts
        ):

            if label.strip() or amount.strip():

                data["expenses"].append({
                    "label": label.strip()
                    or "Spesa",

                    "amount": parse_float(
                        amount
                    )
                })

        creditors = request.form.getlist(
            "creditor"
        )

        debt_types = request.form.getlist(
            "debt_type"
        )

        debt_amounts = request.form.getlist(
            "debt_amount"
        )

        debt_payments = request.form.getlist(
            "debt_payment"
        )

        timestamp = now_iso()

        conn = db_connect()

        if case:

            case_id = case["id"]

            conn.execute(
                """
                UPDATE cases
                SET data = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    json.dumps(
                        data,
                        ensure_ascii=False
                    ),
                    timestamp,
                    case_id
                )
            )

            conn.execute(
                """
                DELETE FROM debts
                WHERE case_id = ?
                """,
                (case_id,)
            )

        else:

            cur = conn.execute(
                """
                INSERT INTO cases
                (email,data,created_at,updated_at)
                VALUES (?,?,?,?)
                """,
                (
                    user["email"],
                    json.dumps(
                        data,
                        ensure_ascii=False
                    ),
                    timestamp,
                    timestamp
                )
            )

            case_id = cur.lastrowid

        for (
            creditor,
            debt_type,
            amount,
            payment
        ) in zip(
            creditors,
            debt_types,
            debt_amounts,
            debt_payments
        ):

            if creditor.strip() or amount.strip():

                conn.execute(
                    """
                    INSERT INTO debts
                    (
                        case_id,
                        creditor,
                        debt_type,
                        current_amount,
                        monthly_payment,
                        notes,
                        created_at
                    )
                    VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        case_id,
                        creditor.strip()
                        or "Creditore non indicato",

                        debt_type.strip(),

                        parse_float(
                            amount
                        ),

                        parse_float(
                            payment
                        ),

                        "",

                        timestamp
                    )
                )

        conn.commit()
        conn.close()

        return redirect(
            url_for("case_summary")
        )

    return render_template_string(
        """
        <!doctype html>
        <html lang="it">
        <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>FixTude - Situazione</title>
        <style>
        body{font-family:Arial;background:#f6f8fb}
        .wrap{max-width:850px;margin:auto;padding:30px}
        .card{background:white;padding:25px;border-radius:15px;margin:15px 0}
        input{width:100%;box-sizing:border-box;padding:11px;margin:5px 0 12px}
        .row{display:grid;grid-template-columns:1fr 1fr;gap:15px}
        button{padding:13px 20px;background:#4f46e5;color:white;border:0;border-radius:8px}
        @media(max-width:650px){.row{grid-template-columns:1fr}}
        </style>
        </head>
        <body>
        <div class="wrap">

        <a href="javascript:history.back()" style="text-decoration:none;color:#4f46e5">
        ← Indietro
        </a>

        <h1>La tua situazione</h1>

        <form method="post">

        <div class="card">

        <h2>Dati personali</h2>

        <div class="row">

        <div>
        <label>Nome</label>
        <input name="name"
        value="{{ data.get('name','') }}"
        required>
        </div>

        <div>
        <label>Cognome</label>
        <input name="surname"
        value="{{ data.get('surname','') }}"
        required>
        </div>

        </div>

        <label>Codice fiscale</label>
        <input name="tax_code"
        value="{{ data.get('tax_code','') }}"
        maxlength="16"
        style="text-transform:uppercase">

        <label>Posizione lavorativa</label>
        <select name="employment">
        <option value="">Seleziona</option>
        <option value="Dipendente" {% if data.get('employment') == 'Dipendente' %}selected{% endif %}>Dipendente</option>
        <option value="Autonomo" {% if data.get('employment') == 'Autonomo' %}selected{% endif %}>Autonomo</option>
        <option value="Imprenditore" {% if data.get('employment') == 'Imprenditore' %}selected{% endif %}>Imprenditore</option>
        <option value="Pensionato" {% if data.get('employment') == 'Pensionato' %}selected{% endif %}>Pensionato</option>
        <option value="Disoccupato" {% if data.get('employment') == 'Disoccupato' %}selected{% endif %}>Disoccupato</option>
        <option value="Studente" {% if data.get('employment') == 'Studente' %}selected{% endif %}>Studente</option>
        <option value="Altro" {% if data.get('employment') == 'Altro' %}selected{% endif %}>Altro</option>
        </select>

        <label>Telefono</label>
        <input name="phone"
        value="{{ data.get('phone','') }}">

        <label>Indirizzo</label>
        <input name="address"
        value="{{ data.get('address','') }}">

        </div>


        <div class="card">

        <h2>Entrate mensili</h2>

        {% for i in range(4) %}

        <div class="row">

        <div>
        <label>Tipo di entrata</label>
        <select name="income_label">
        <option value="">Seleziona</option>
        <option value="Stipendio">Stipendio</option>
        <option value="Pensione">Pensione</option>
        <option value="Reddito da lavoro autonomo">Reddito da lavoro autonomo</option>
        <option value="Reddito da impresa">Reddito da impresa</option>
        <option value="Assegno">Assegno</option>
        <option value="Affitto percepito">Affitto percepito</option>
        <option value="Altro">Altro</option>
        </select>
        </div>

        <div>
        <label>Importo</label>
        <input name="income_amount"
        type="number"
        step="0.01"
        min="0">
        </div>

        </div>

        {% endfor %}

        </div>


        <div class="card">

        <h2>Spese mensili</h2>

        {% for i in range(6) %}

        <div class="row">

        <div>
        <label>Tipo di spesa</label>
        <select name="expense_label">
        <option value="">Seleziona</option>
        <option value="Affitto">Affitto</option>
        <option value="Mutuo">Mutuo</option>
        <option value="Utenze">Utenze</option>
        <option value="Alimentari">Alimentari</option>
        <option value="Trasporti">Trasporti</option>
        <option value="Spese mediche">Spese mediche</option>
        <option value="Spese familiari">Spese familiari</option>
        <option value="Altro">Altro</option>
        </select>
        </div>

        <div>
        <label>Importo</label>
        <input name="expense_amount"
        type="number"
        step="0.01"
        min="0">
        </div>

        </div>

        {% endfor %}

        </div>


        <div class="card">

        <h2>Debiti</h2>

        {% for i in range(5) %}

        <div style="border-top:1px solid #ddd;padding-top:15px">

        <label>Creditore</label>
        <input name="creditor"
        placeholder="Banca / finanziaria">

        <label>Tipo</label>
        <select name="debt_type">
        <option value="">Seleziona</option>
        <option value="Prestito personale">Prestito personale</option>
        <option value="Finanziamento">Finanziamento</option>
        <option value="Carta di credito">Carta di credito</option>
        <option value="Mutuo">Mutuo</option>
        <option value="Fido / scoperto">Fido / scoperto</option>
        <option value="Debito fiscale">Debito fiscale</option>
        <option value="Debito previdenziale">Debito previdenziale</option>
        <option value="Utenze">Utenze</option>
        <option value="Altro">Altro</option>
        </select>

        <label>Debito residuo</label>
        <input name="debt_amount"
        type="number"
        step="0.01"
        min="0">

        <label>Rata mensile</label>
        <input name="debt_payment"
        type="number"
        step="0.01"
        min="0">

        </div>

        {% endfor %}

        </div>

        <button>
        Salva situazione →
        </button>

        </form>

        </div>
        </body>
        </html>
        """,
        data=existing,
        debts=existing_debts
    )


# ============================================================
# SUMMARY
# ============================================================

@app.route("/privato/riepilogo")
def case_summary():

    user = require_login(
        "debtor"
    )

    if not user:
        return redirect(
            url_for("debtor_login")
        )

    case = get_case_for_email(
        user["email"]
    )

    if not case:
        return redirect(
            url_for("debtor_situation")
        )

    calc = calculate_case(
        case["id"]
    )

    return render_template_string(
        """
        <!doctype html>
        <html lang="it">
        <body style="font-family:Arial;background:#f6f8fb">

        <div style="max-width:900px;margin:auto;padding:30px">

        <a href="javascript:history.back()" style="text-decoration:none;color:#4f46e5">
        ← Indietro
        </a>

        <h1>Riepilogo</h1>

        <div style="background:white;padding:25px;border-radius:15px">

        <h2>Entrate</h2>
        € {{ "%.2f"|format(total_income) }}

        <h2>Spese</h2>
        € {{ "%.2f"|format(total_expenses) }}

        <h2>Disponibilità</h2>
        € {{ "%.2f"|format(monthly_capacity) }}

        <h2>Debito complessivo</h2>
        € {{ "%.2f"|format(total_debt) }}

        <h2>Rate</h2>
        € {{ "%.2f"|format(total_payments) }}

        </div>

        <br>

        <a href="{{ url_for('case_analysis') }}">
        Analizza la situazione →
        </a>

        </div>
        </body>
        </html>
        """,
        total_income=calc["total_income"],
        total_expenses=calc["total_expenses"],
        monthly_capacity=calc["monthly_capacity"],
        total_debt=calc["total_debt"],
        total_payments=calc["total_payments"]
    )


# ============================================================
# PAYMENT AREA
# ============================================================

@app.route("/pagamenti")
def payments():

    user = require_login(
        "debtor"
    )

    if not user:
        return redirect(
            url_for("debtor_login")
        )

    case = get_case_for_email(
        user["email"]
    )

    if not case:

        return render_template_string(
            """
            <h1>Pagamenti FixTude</h1>
            <p>Prima devi creare la tua pratica.</p>
            <a href="{{ url_for('debtor_situation') }}">
            Inserisci la situazione
            </a>
            """
        )

    analysis_paid = bool(
        get_paid_payment(
            user["email"],
            case["id"],
            "analysis"
        )
    )

    pdf_paid = bool(
        get_paid_payment(
            user["email"],
            case["id"],
            "pdf"
        )
    )

    solutions = get_solutions(
        case["id"]
    )

    has_document = any(
        s["status"] == "sent"
        for s in solutions
    )

    return render_template_string(
        """
        <!doctype html>
        <html lang="it">
        <head>
        <meta charset="utf-8">
        <meta name="viewport"
        content="width=device-width,initial-scale=1">
        <title>Pagamenti FixTude</title>
        <style>
        body{font-family:Arial;background:#f6f8fb}
        .wrap{max-width:800px;margin:auto;padding:30px}
        .card{background:white;padding:25px;border-radius:15px;margin:15px 0}
        .price{font-size:30px;font-weight:bold}
        button{padding:13px 20px;background:#4f46e5;color:white;border:0;border-radius:8px}
        .paid{background:#e8f7ed;padding:12px;border-radius:8px}
        </style>
        </head>
        <body>

        <div class="wrap">

        <a href="javascript:history.back()" style="text-decoration:none;color:#4f46e5">
        ← Indietro
        </a>

        <h1>Pagamenti</h1>

        <div class="card">

        <h2>Analisi FixTude</h2>

        <p>
        Analisi automatica della tua situazione
        e individuazione di possibili scenari.
        </p>

        <div class="price">€ 1,99</div>

        {% if analysis_paid %}

        <div class="paid">
        ✓ Analisi già acquistata.
        </div>

        <br>

        <a href="{{ url_for('case_analysis') }}">
        Apri l'analisi →
        </a>

        {% else %}

        <form method="post"
        action="{{ url_for('create_checkout') }}">

        <input type="hidden"
        name="service"
        value="analysis">

        <button>
        Paga € 1,99 con Stripe
        </button>

        </form>

        {% endif %}

        </div>


        <div class="card">

        <h2>Documento PDF</h2>

        <p>
        Documento PDF definitivo dopo
        la validazione del Risolutore.
        </p>

        <div class="price">€ 9,99</div>

        {% if pdf_paid %}

        <div class="paid">
        ✓ Documento già acquistato.
        </div>

        {% elif not has_document %}

        <p>
        Il documento sarà acquistabile
        dopo la validazione del Risolutore.
        </p>

        {% else %}

        <form method="post"
        action="{{ url_for('create_checkout') }}">

        <input type="hidden"
        name="service"
        value="pdf">

        <button>
        Paga € 9,99 con Stripe
        </button>

        </form>

        {% endif %}

        </div>

        </div>

        </body>
        </html>
        """,
        analysis_paid=analysis_paid,
        pdf_paid=pdf_paid,
        has_document=has_document
    )


# ============================================================
# CREATE STRIPE CHECKOUT
# ============================================================

@app.route(
    "/pagamenti/checkout",
    methods=["POST"]
)
def create_checkout():

    user = require_login("debtor")

    if not user:
        return redirect(url_for("debtor_login"))

    service_key = request.form.get("service", "").strip()

    if service_key not in PAYMENT_SERVICES:
        return "Servizio non valido.", 400

    service = PAYMENT_SERVICES[service_key]

    if not STRIPE_SECRET_KEY:
        app.logger.error(
            "STRIPE_SECRET_KEY assente nelle variabili d'ambiente."
        )
        return (
            "Errore di configurazione Stripe. "
            "STRIPE_SECRET_KEY non è configurata su Render.",
            500
        )

    try:
        case = get_case_for_email(user["email"])
    except Exception:
        app.logger.exception(
            "Errore nel recupero della pratica per %s",
            user["email"]
        )
        return "Errore nel recupero della pratica.", 500

    if not case:
        return redirect(url_for("debtor_situation"))

    case_id = case["id"]

    try:
        if get_paid_payment(user["email"], case_id, service_key):
            return redirect(url_for("payments"))
    except Exception:
        app.logger.exception("Errore nel controllo del pagamento esistente.")
        return "Errore nel controllo del pagamento.", 500

    if service_key == "pdf":
        try:
            solutions = get_solutions(case_id)
            if not any(s["status"] == "sent" for s in solutions):
                return redirect(url_for("payments"))
        except Exception:
            app.logger.exception("Errore nel controllo del documento PDF.")
            return "Errore nel controllo del documento PDF.", 500

    payment_id = None

    try:
        conn = db_connect()
        cursor = conn.execute(
            """
            INSERT INTO payments
            (
                user_email,
                case_id,
                service,
                amount,
                currency,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
            """,
            (
                user["email"],
                case_id,
                service_key,
                service["amount"],
                service["currency"],
                now_iso()
            )
        )
        payment_id = cursor.lastrowid
        conn.commit()
        conn.close()

    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        app.logger.exception(
            "Errore SQLite durante la creazione del pagamento."
        )
        return "Errore nella registrazione del pagamento.", 500

    try:
        app.logger.info(
            "Creazione Checkout Stripe: payment_id=%s case_id=%s service=%s amount=%s",
            payment_id,
            case_id,
            service_key,
            service["amount"]
        )

        stripe.api_key = STRIPE_SECRET_KEY

        checkout = stripe.checkout.Session.create(
            mode="payment",
            customer_email=user["email"],
            line_items=[
                {
                    "price_data": {
                        "currency": service["currency"],
                        "product_data": {
                            "name": service["name"],
                            "description": service["description"]
                        },
                        "unit_amount": service["amount"]
                    },
                    "quantity": 1
                }
            ],
            metadata={
                "payment_id": str(payment_id),
                "case_id": str(case_id),
                "service": service_key,
                "user_email": user["email"]
            },
            success_url=(
                url_for("payment_success", _external=True)
                + "?session_id={CHECKOUT_SESSION_ID}"
            ),
            cancel_url=url_for(
                "payment_cancel",
                payment_id=payment_id,
                _external=True
            )
        )

        conn = db_connect()
        conn.execute(
            """
            UPDATE payments
            SET stripe_session_id = ?
            WHERE id = ?
            """,
            (checkout.id, payment_id)
        )
        conn.commit()
        conn.close()

        app.logger.info(
            "Checkout Stripe creato correttamente: %s",
            checkout.id
        )

        return redirect(checkout.url)

    except Exception as exc:
        app.logger.exception(
            "ERRORE STRIPE CHECKOUT: %s",
            exc
        )

        try:
            conn = db_connect()
            conn.execute(
                """
                UPDATE payments
                SET status = 'failed'
                WHERE id = ?
                """,
                (payment_id,)
            )
            conn.commit()
            conn.close()
        except Exception:
            app.logger.exception(
                "Impossibile aggiornare il pagamento come failed."
            )

        return (
            "Errore nella creazione del pagamento Stripe. "
            "Controllare i log di FixTude.",
            500
        )


# ============================================================
# PAYMENT SUCCESS
# ============================================================

@app.route(
    "/pagamenti/success"
)
def payment_success():

    user = require_login(
        "debtor"
    )

    if not user:
        return redirect(
            url_for("debtor_login")
        )

    session_id = request.args.get(
        "session_id"
    )

    if not session_id:

        return redirect(
            url_for("payments")
        )

    if not STRIPE_SECRET_KEY:

        return (
            "Stripe non configurato.",
            500
        )

    try:

        checkout = stripe.checkout.Session.retrieve(
            session_id
        )

    except Exception as exc:

        return (
            "Impossibile verificare il pagamento: "
            + str(exc),
            500
        )

    metadata = (
        checkout.metadata or {}
    )

    payment_id = metadata.get(
        "payment_id"
    )

    if not payment_id:

        return (
            "Pagamento non riconosciuto.",
            400
        )

    payment = get_payment(
        int(payment_id)
    )

    if not payment:

        return (
            "Pagamento non trovato.",
            404
        )

    if payment["user_email"] != user["email"]:

        abort(403)

    if checkout.payment_status == "paid":

        mark_payment_paid(
            int(payment_id),
            checkout.payment_intent
        )

        status = "paid"

    else:

        status = checkout.payment_status

    return render_template_string(
        """
        <!doctype html>
        <html lang="it">
        <head>
        <meta charset="utf-8">
        <meta name="viewport"
        content="width=device-width,initial-scale=1">
        <title>Pagamento FixTude</title>
        <style>
        body{font-family:Arial;background:#f6f8fb}
        .box{max-width:600px;margin:80px auto;background:white;padding:35px;border-radius:18px;text-align:center}
        .ok{background:#e8f7ed;padding:15px;border-radius:10px}
        a{display:inline-block;margin-top:20px}
        </style>
        </head>
        <body>
        <div class="box">

        {% if status == "paid" %}

        <h1>Pagamento completato</h1>

        <div class="ok">
        Il pagamento è stato registrato correttamente.
        </div>

        {% else %}

        <h1>Pagamento in verifica</h1>

        <p>
        Stripe ha ricevuto la richiesta.
        Il pagamento verrà confermato automaticamente.
        </p>

        {% endif %}

        <a href="{{ url_for('payments') }}">
        Torna ai pagamenti
        </a>

        </div>
        </body>
        </html>
        """,
        status=status
    )


# ============================================================
# PAYMENT CANCEL
# ============================================================

@app.route(
    "/pagamenti/cancel/<int:payment_id>"
)
def payment_cancel(payment_id):

    user = require_login(
        "debtor"
    )

    if not user:
        return redirect(
            url_for("debtor_login")
        )

    payment = get_payment(
        payment_id
    )

    if payment and payment["user_email"] == user["email"]:

        conn = db_connect()

        conn.execute(
            """
            UPDATE payments
            SET status = 'cancelled'
            WHERE id = ?
              AND status = 'pending'
            """,
            (payment_id,)
        )

        conn.commit()
        conn.close()

    return redirect(
        url_for("payments")
    )


# ============================================================
# STRIPE WEBHOOK
# ============================================================

@app.route(
    "/stripe/webhook",
    methods=["POST"]
)
def stripe_webhook():

    payload = request.get_data()

    signature = request.headers.get(
        "Stripe-Signature",
        ""
    )

    if not STRIPE_WEBHOOK_SECRET:

        return (
            "STRIPE_WEBHOOK_SECRET non configurato.",
            500
        )

    try:

        event = stripe.Webhook.construct_event(
            payload,
            signature,
            STRIPE_WEBHOOK_SECRET
        )

    except ValueError:

        return (
            "Payload non valido.",
            400
        )

    except stripe.error.SignatureVerificationError:

        return (
            "Firma Stripe non valida.",
            400
        )

    event_type = event["type"]

    if event_type in (
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded"
    ):

        checkout = event["data"]["object"]

        metadata = (
            checkout.get("metadata")
            or {}
        )

        payment_id = metadata.get(
            "payment_id"
        )

        if payment_id:

            if checkout.get(
                "payment_status"
            ) == "paid":

                mark_payment_paid(
                    int(payment_id),
                    checkout.get(
                        "payment_intent"
                    )
                )

    elif event_type == "checkout.session.expired":

        checkout = event["data"]["object"]

        metadata = (
            checkout.get("metadata")
            or {}
        )

        payment_id = metadata.get(
            "payment_id"
        )

        if payment_id:

            conn = db_connect()

            conn.execute(
                """
                UPDATE payments
                SET status = 'cancelled'
                WHERE id = ?
                  AND status = 'pending'
                """,
                (int(payment_id),)
            )

            conn.commit()
            conn.close()

    return "", 200


# ============================================================
# DEBTOR ANALYSIS
# ============================================================

@app.route("/privato/analisi")
def case_analysis():

    user = require_login(
        "debtor"
    )

    if not user:
        return redirect(
            url_for("debtor_login")
        )

    case = get_case_for_email(
        user["email"]
    )

    if not case:

        return redirect(
            url_for("debtor_situation")
        )

    # ========================================================
    # IL PAGAMENTO DA €1,99 È OBBLIGATORIO
    # ========================================================

    paid = get_paid_payment(
        user["email"],
        case["id"],
        "analysis"
    )

    if not paid:

        return redirect(
            url_for(
                "payments"
            )
        )

    analysis = latest_analysis(
        case["id"]
    )

    solutions = get_solutions(
        case["id"]
    )

    if not analysis or not solutions:

        analysis = run_local_agent(
            case["id"]
        )

        solutions = get_solutions(
            case["id"]
        )

    calc = calculate_case(
        case["id"]
    )

    return render_template_string(
        """
        <!doctype html>
        <html lang="it">
        <head>
        <meta charset="utf-8">
        <meta name="viewport"
        content="width=device-width,initial-scale=1">
        <title>Analisi FixTude</title>
        <style>
        body{font-family:Arial;background:#f6f8fb}
        .wrap{max-width:900px;margin:auto;padding:30px}
        .card{background:white;padding:25px;border-radius:15px;margin:15px 0}
        .metric{display:inline-block;padding:18px;background:#f3f4f6;border-radius:10px;margin:5px}
        .solution{border-top:1px solid #ddd;padding:20px 0}
        </style>
        </head>
        <body>
        <div class="wrap">

        <a href="javascript:history.back()" style="text-decoration:none;color:#4f46e5">
        ← Indietro
        </a>

        <h1>Analisi FixTude</h1>

        <div class="card">

        <div class="metric">
        Entrate<br>
        <strong>€ {{ "%.2f"|format(calc.total_income) }}</strong>
        </div>

        <div class="metric">
        Spese<br>
        <strong>€ {{ "%.2f"|format(calc.total_expenses) }}</strong>
        </div>

        <div class="metric">
        Disponibilità<br>
        <strong>€ {{ "%.2f"|format(calc.monthly_capacity) }}</strong>
        </div>

        <div class="metric">
        Debiti<br>
        <strong>€ {{ "%.2f"|format(calc.total_debt) }}</strong>
        </div>

        </div>

        <div class="card">

        <h2>Valutazione</h2>

        <p>
        {{ analysis.summary }}
        </p>

        {% for warning in analysis.warnings %}

        <p style="background:#fff4d6;padding:12px">
        {{ warning }}
        </p>

        {% endfor %}

        </div>


        <div class="card">

        <h2>Possibili scenari</h2>

        {% for solution in solutions %}

        <div class="solution">

        <h3>
        {{ solution.title }}
        </h3>

        <p>
        {{ solution.content }}
        </p>

        <strong>
        Stato:
        {{ solution.status }}
        </strong>

        {% if solution.status == "sent" %}

        <p>
        <a href="{{ url_for('download_solution', solution_id=solution.id) }}">
        Scarica documento
        </a>
        </p>

        {% endif %}

        </div>

        {% endfor %}

        </div>

        <p>
        <a href="{{ url_for('payments') }}">
        Area pagamenti
        </a>
        </p>

        </div>
        </body>
        </html>
        """,
        calc=calc,
        analysis=analysis,
        solutions=solutions
    )


# ============================================================
# RESOLVER DASHBOARD
# ============================================================

@app.route("/risolutore")
def resolver_dashboard():

    user = require_login(
        "resolver"
    )

    if not user:
        return redirect(
            url_for("resolver_login")
        )

    conn = db_connect()

    cases = conn.execute(
        """
        SELECT *
        FROM cases
        ORDER BY id DESC
        """
    ).fetchall()

    conn.close()

    return render_template_string(
        """
        <!doctype html>
        <html lang="it">
        <body style="font-family:Arial;background:#f6f8fb">

        <div style="max-width:900px;margin:auto;padding:30px">

        <div style="display:flex;justify-content:space-between">

        <h1>Risolutore AI</h1>

        <a href="{{ url_for('logout') }}">
        Esci
        </a>

        </div>

        {% for case in cases %}

        {% set data = get_case_data(case.id) %}

        <div style="background:white;padding:20px;border-radius:15px;margin:15px 0">

        <h2>
        {{ data.get('name','Cliente') }}
        {{ data.get('surname','') }}
        </h2>

        <p>
        {{ case.email }}
        </p>

        <a href="{{ url_for('resolver_case', case_id=case.id) }}">
        Apri pratica →
        </a>

        </div>

        {% else %}

        <div style="background:white;padding:25px;border-radius:15px">
        Nessuna pratica.
        </div>

        {% endfor %}

        </div>
        </body>
        </html>
        """,
        cases=cases,
        get_case_data=get_case_data
    )


@app.route(
    "/risolutore/pratica/<int:case_id>"
)
def resolver_case(case_id):

    user = require_login(
        "resolver"
    )

    if not user:
        return redirect(
            url_for("resolver_login")
        )

    case = get_case(case_id)

    if not case:
        abort(404)

    calc = calculate_case(
        case_id
    )

    analysis = latest_analysis(
        case_id
    )

    solutions = get_solutions(
        case_id
    )

    return render_template_string(
        """
        <!doctype html>
        <html lang="it">
        <body style="font-family:Arial;background:#f6f8fb">

        <div style="max-width:950px;margin:auto;padding:30px">

        <a href="{{ url_for('resolver_dashboard') }}">
        ← Pratiche
        </a>

        <h1>
        Pratica #{{ case.id }}
        </h1>

        <div style="background:white;padding:25px;border-radius:15px">

        <h2>Quadro economico</h2>

        <p>
        Entrate:
        € {{ "%.2f"|format(calc.total_income) }}
        </p>

        <p>
        Spese:
        € {{ "%.2f"|format(calc.total_expenses) }}
        </p>

        <p>
        Disponibilità:
        € {{ "%.2f"|format(calc.monthly_capacity) }}
        </p>

        <p>
        Debiti:
        € {{ "%.2f"|format(calc.total_debt) }}
        </p>

        <form method="post"
        action="{{ url_for('resolver_run_analysis', case_id=case.id) }}">

        <button>
        Genera / rigenera analisi
        </button>

        </form>

        </div>

        {% if analysis %}

        <div style="background:white;padding:25px;border-radius:15px;margin-top:20px">

        <h2>Analisi</h2>

        <p>
        {{ analysis.summary }}
        </p>

        </div>

        {% endif %}

        {% for solution in solutions %}

        <div style="background:white;padding:25px;border-radius:15px;margin-top:20px">

        <h2>
        {{ solution.title }}
        </h2>

        <form method="post"
        action="{{ url_for('correct_solution', solution_id=solution.id) }}">

        <textarea
        name="content"
        style="width:100%;min-height:180px"
        >{{ solution.content }}</textarea>

        <br><br>

        <input
        name="note"
        placeholder="Nota supervisore"
        style="width:100%;padding:10px"
        >

        <br><br>

        <button>
        Salva correzione
        </button>

        </form>

        <br>

        {% if solution.status != "sent" %}

        <form method="post"
        action="{{ url_for('approve_solution', solution_id=solution.id) }}">

        <button>
        Valida e invia al cliente
        </button>

        </form>

        {% else %}

        <strong>
        Documento validato e inviato.
        </strong>

        {% endif %}

        </div>

        {% endfor %}

        </div>
        </body>
        </html>
        """,
        case=case,
        calc=calc,
        analysis=analysis,
        solutions=solutions
    )


@app.route(
    "/risolutore/pratica/<int:case_id>/analizza",
    methods=["POST"]
)
def resolver_run_analysis(case_id):

    user = require_login(
        "resolver"
    )

    if not user:
        return redirect(
            url_for("resolver_login")
        )

    if not get_case(case_id):
        abort(404)

    run_local_agent(
        case_id
    )

    return redirect(
        url_for(
            "resolver_case",
            case_id=case_id
        )
    )


# ============================================================
# CORRECTION
# ============================================================

@app.route(
    "/risolutore/soluzione/<int:solution_id>/correggi",
    methods=["POST"]
)
def correct_solution(solution_id):

    user = require_login(
        "resolver"
    )

    if not user:
        return redirect(
            url_for("resolver_login")
        )

    corrected = request.form.get(
        "content",
        ""
    ).strip()

    note = request.form.get(
        "note",
        ""
    ).strip()

    conn = db_connect()

    solution = conn.execute(
        """
        SELECT *
        FROM solution_documents
        WHERE id = ?
        """,
        (solution_id,)
    ).fetchone()

    if not solution:

        conn.close()
        abort(404)

    conn.execute(
        """
        INSERT INTO supervision
        (
            solution_id,
            original_content,
            corrected_content,
            correction_note,
            created_at
        )
        VALUES (?,?,?,?,?)
        """,
        (
            solution_id,
            solution["content"],
            corrected,
            note,
            now_iso()
        )
    )

    conn.execute(
        """
        UPDATE solution_documents
        SET content = ?,
            supervisor_note = ?,
            status = 'pending_review',
            updated_at = ?,
            approved_at = NULL,
            sent_at = NULL
        WHERE id = ?
        """,
        (
            corrected,
            note,
            now_iso(),
            solution_id
        )
    )

    conn.commit()

    case_id = solution["case_id"]

    conn.close()

    try:
        generate_pdf(
            solution_id
        )
    except Exception:
        pass

    return redirect(
        url_for(
            "resolver_case",
            case_id=case_id
        )
    )


# ============================================================
# APPROVE
# ============================================================

@app.route(
    "/risolutore/soluzione/<int:solution_id>/approva",
    methods=["POST"]
)
def approve_solution(solution_id):

    user = require_login(
        "resolver"
    )

    if not user:
        return redirect(
            url_for("resolver_login")
        )

    conn = db_connect()

    solution = conn.execute(
        """
        SELECT *
        FROM solution_documents
        WHERE id = ?
        """,
        (solution_id,)
    ).fetchone()

    if not solution:

        conn.close()
        abort(404)

    pdf_path = solution["pdf_path"]

    if not pdf_path or not Path(pdf_path).exists():

        try:
            pdf_path = generate_pdf(
                solution_id
            )
        except Exception:
            pdf_path = None

    timestamp = now_iso()

    conn.execute(
        """
        UPDATE solution_documents
        SET status = 'sent',
            pdf_path = ?,
            approved_at = ?,
            sent_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            pdf_path,
            timestamp,
            timestamp,
            timestamp,
            solution_id
        )
    )

    case = conn.execute(
        """
        SELECT *
        FROM cases
        WHERE id = ?
        """,
        (solution["case_id"],)
    ).fetchone()

    if case:

        conn.execute(
            """
            INSERT INTO notifications
            (
                case_id,
                email,
                title,
                message,
                notification_type,
                read,
                created_at
            )
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                case["id"],
                case["email"],
                "Nuovo documento disponibile",
                (
                    "Il Risolutore ha validato "
                    "il documento: "
                    + solution["title"]
                ),
                "in_app",
                0,
                timestamp
            )
        )

    conn.commit()
    conn.close()

    return redirect(
        url_for(
            "resolver_case",
            case_id=solution["case_id"]
        )
    )


# ============================================================
# DOWNLOAD SOLUTION
# ============================================================

@app.route(
    "/soluzioni/<int:solution_id>/download"
)
def download_solution(solution_id):

    user = current_user()

    if not user:
        return redirect(
            url_for("home")
        )

    conn = db_connect()

    solution = conn.execute(
        """
        SELECT *
        FROM solution_documents
        WHERE id = ?
        """,
        (solution_id,)
    ).fetchone()

    if not solution:

        conn.close()
        abort(404)

    case = conn.execute(
        """
        SELECT *
        FROM cases
        WHERE id = ?
        """,
        (solution["case_id"],)
    ).fetchone()

    conn.close()

    if not case:
        abort(404)

    if (
        user["role"] == "debtor"
        and case["email"] != user["email"]
    ):

        abort(403)

    # ========================================================
    # IL PDF DEL DEBITORE È A PAGAMENTO
    # ========================================================

    if user["role"] == "debtor":

        if solution["status"] != "sent":

            abort(403)

        paid = get_paid_payment(
            user["email"],
            case["id"],
            "pdf"
        )

        if not paid:

            return redirect(
                url_for("payments")
            )

    path = solution["pdf_path"]

    if not path or not Path(path).exists():

        try:

            path = generate_pdf(
                solution_id
            )

        except Exception as exc:

            return (
                "PDF non disponibile: "
                + str(exc),
                500
            )

    return send_file(
        path,
        as_attachment=True,
        download_name=Path(path).name
    )


# ============================================================
# NOTIFICATIONS
# ============================================================

@app.route(
    "/privato/notifiche"
)
def debtor_notifications():

    user = require_login(
        "debtor"
    )

    if not user:
        return redirect(
            url_for("debtor_login")
        )

    conn = db_connect()

    rows = conn.execute(
        """
        SELECT *
        FROM notifications
        WHERE email = ?
        ORDER BY id DESC
        """,
        (user["email"],)
    ).fetchall()

    conn.execute(
        """
        UPDATE notifications
        SET read = 1
        WHERE email = ?
        """,
        (user["email"],)
    )

    conn.commit()
    conn.close()

    return render_template_string(
        """
        <!doctype html>
        <html lang="it">
        <body style="font-family:Arial;background:#f6f8fb">

        <div style="max-width:800px;margin:auto;padding:30px">

        <a href="javascript:history.back()" style="text-decoration:none;color:#4f46e5">
        ← Indietro
        </a>

        <h1>Notifiche</h1>

        {% for n in notifications %}

        <div style="background:white;padding:20px;border-radius:12px;margin:12px 0">

        <strong>
        {{ n.title }}
        </strong>

        <p>
        {{ n.message }}
        </p>

        </div>

        {% else %}

        <p>Nessuna notifica.</p>

        {% endfor %}

        </div>
        </body>
        </html>
        """,
        notifications=rows
    )


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("home")
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(
            os.environ.get(
                "PORT",
                5000
            )
        ),
        debug=False
    )
