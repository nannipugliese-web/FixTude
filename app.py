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
import threading
import re
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

_default_db = Path("/var/data/fixtude.db") if Path("/var/data").exists() else (BASE_DIR / "fixtude.db")
DB_PATH = Path(os.environ.get("FIXTUDE_DB_PATH", str(_default_db))).expanduser()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR = BASE_DIR / "uploads"
PDF_DIR = BASE_DIR / "generated_pdfs"

UPLOAD_DIR.mkdir(exist_ok=True)
PDF_DIR.mkdir(exist_ok=True)
KIT_LIBRARY_DIR = (Path("/var/data") / "kit_library") if Path("/var/data").exists() else (BASE_DIR / "kit_library")
KIT_LIBRARY_DIR.mkdir(parents=True, exist_ok=True)


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
        "name": "Kit FixTude",
        "description": "Kit PDF FixTude personalizzato e validato dall'Esperto.",
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

    # Dati fiscali del cliente, necessari per predisporre la fattura elettronica.
    user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    for column, definition in {
        "first_name": "TEXT",
        "last_name": "TEXT",
        "fiscal_code": "TEXT",
        "billing_address": "TEXT",
        "billing_cap": "TEXT",
        "billing_city": "TEXT",
        "billing_province": "TEXT",
        "vat_number": "TEXT",
        "recipient_code": "TEXT",
        "pec": "TEXT"
    }.items():
        if column not in user_columns:
            conn.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")

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

    # Documenti generati per i pagamenti: ricevuta e riepilogo fiscale.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payment_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id INTEGER NOT NULL,
            document_type TEXT NOT NULL,
            file_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(payment_id, document_type),
            FOREIGN KEY(payment_id) REFERENCES payments(id) ON DELETE CASCADE
        )
    """)

    conn.execute("""CREATE TABLE IF NOT EXISTS kit_library (id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,category TEXT NOT NULL DEFAULT 'Generale',description TEXT,file_path TEXT NOT NULL,original_filename TEXT NOT NULL,validated INTEGER NOT NULL DEFAULT 0,active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS solution_attachments (id INTEGER PRIMARY KEY AUTOINCREMENT,solution_id INTEGER NOT NULL,library_id INTEGER NOT NULL,created_at TEXT NOT NULL,UNIQUE(solution_id,library_id),FOREIGN KEY(solution_id) REFERENCES solution_documents(id) ON DELETE CASCADE,FOREIGN KEY(library_id) REFERENCES kit_library(id) ON DELETE CASCADE)""")

    # Campi aggiunti senza rompere i database FixTude già esistenti.
    existing_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(payments)").fetchall()
    }
    for column, definition in {
        "receipt_email_sent_at": "TEXT",
        "fiscal_document_status": "TEXT DEFAULT 'da_emettere'"
    }.items():
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE payments ADD COLUMN {column} {definition}")

    solution_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(solution_documents)").fetchall()
    }
    if "final_email_sent_at" not in solution_columns:
        conn.execute("ALTER TABLE solution_documents ADD COLUMN final_email_sent_at TEXT")

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
        else:
            # Account demo sempre coerenti con le credenziali pubblicate.
            # Non modifichiamo gli account normali registrati dagli utenti.
            conn.execute(
                """
                UPDATE users
                SET password_hash = ?, role = ?
                WHERE email = ?
                """,
                (
                    generate_password_hash(data["password"]),
                    data["role"],
                    email
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


def fiscal_data_complete(email):
    row = find_user(email)
    if not row:
        return False
    required = [
        "first_name", "last_name", "fiscal_code",
        "billing_address", "billing_cap", "billing_city",
        "billing_province"
    ]
    return all(str(row[column] or "").strip() for column in required)


def get_fiscal_data(email):
    row = find_user(email)
    if not row:
        return {}
    return dict(row)


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

    already_paid = payment["status"] == "paid"
    paid_timestamp = payment["paid_at"] or now_iso()

    conn.execute(
        """
        UPDATE payments
        SET status = 'paid',
            stripe_payment_intent_id = ?,
            paid_at = ?
        WHERE id = ?
        """,
        (
            payment_intent or payment["stripe_payment_intent_id"],
            paid_timestamp,
            payment_id
        )
    )

    conn.commit()
    conn.close()

    if not already_paid:
        prepare_payment_documents(payment_id)

    return True


# ============================================================
# PAYMENT RECEIPTS / DOCUMENTS
# ============================================================

def payment_document(payment_id, document_type):
    conn = db_connect()
    row = conn.execute(
        """SELECT * FROM payment_documents
           WHERE payment_id = ? AND document_type = ?""",
        (payment_id, document_type)
    ).fetchone()
    conn.close()
    return row


def _safe_pdf_text(value):
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def generate_payment_pdf(payment_id, document_type="receipt"):
    """Genera un PDF riepilogativo del pagamento e lo archivia.

    'receipt' = ricevuta di pagamento.
    'fiscal'  = documento riepilogativo con i dati fiscali disponibili.
    Non viene presentato come fattura elettronica SdI: per quella serve
    un'integrazione con un intermediario/servizio di fatturazione elettronica.
    """
    existing = payment_document(payment_id, document_type)
    if existing and Path(existing["file_path"]).exists():
        return existing["file_path"]

    payment = get_payment(payment_id)
    if not payment:
        raise FileNotFoundError("Pagamento non trovato.")

    case = get_case(payment["case_id"])
    data = get_case_data(payment["case_id"])

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    from reportlab.lib.units import mm

    prefix = "ricevuta" if document_type == "receipt" else "documento_fiscale"
    path = PDF_DIR / f"fixtude_{prefix}_{payment_id}.pdf"
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(path), pagesize=A4,
                            rightMargin=20*mm, leftMargin=20*mm,
                            topMargin=20*mm, bottomMargin=20*mm)
    story = [
        Paragraph("FixTude", styles["Title"]),
        Spacer(1, 5*mm),
        Paragraph("Ricevuta di pagamento" if document_type == "receipt" else "Documento riepilogativo fiscale", styles["Heading2"]),
        Spacer(1, 5*mm),
        Paragraph(f"Numero pagamento: {_safe_pdf_text(payment_id)}", styles["BodyText"]),
        Paragraph(f"Data pagamento: {_safe_pdf_text(payment['paid_at'] or payment['created_at'])}", styles["BodyText"]),
        Paragraph(f"Cliente: {_safe_pdf_text(data.get('name',''))} {_safe_pdf_text(data.get('surname',''))}", styles["BodyText"]),
        Paragraph(f"Codice fiscale: {_safe_pdf_text(data.get('tax_code',''))}", styles["BodyText"]),
        Paragraph(f"Email: {_safe_pdf_text(payment['user_email'])}", styles["BodyText"]),
        Paragraph(f"Indirizzo: {_safe_pdf_text(data.get('address',''))}", styles["BodyText"]),
        Spacer(1, 5*mm),
        Paragraph(f"Servizio: {_safe_pdf_text(PAYMENT_SERVICES.get(payment['service'], {}).get('name', payment['service']))}", styles["BodyText"]),
        Paragraph(f"Importo: € {payment['amount']/100:.2f}".replace('.', ','), styles["BodyText"]),
        Paragraph(f"Valuta: {_safe_pdf_text(payment['currency'].upper())}", styles["BodyText"]),
        Spacer(1, 8*mm),
    ]
    if document_type == "fiscal":
        story.append(Paragraph(
            "Documento riepilogativo generato da FixTude. Non sostituisce la fattura elettronica trasmessa tramite Sistema di Interscambio (SdI).",
            styles["BodyText"]
        ))
    else:
        story.append(Paragraph("Pagamento registrato tramite il sistema di pagamento utilizzato da FixTude.", styles["BodyText"]))
    doc.build(story)

    conn = db_connect()
    conn.execute(
        """INSERT OR REPLACE INTO payment_documents
           (payment_id, document_type, file_path, created_at)
           VALUES (?, ?, ?, ?)""",
        (payment_id, document_type, str(path), now_iso())
    )
    if document_type == "fiscal":
        conn.execute("UPDATE payments SET fiscal_document_status = 'riepilogo_generato' WHERE id = ?", (payment_id,))
    conn.commit()
    conn.close()
    return str(path)


def send_payment_email(payment_id):
    """Invia una sola email post-pagamento con i documenti disponibili."""
    payment = get_payment(payment_id)
    if not payment or payment["status"] != "paid":
        return False
    if payment["receipt_email_sent_at"]:
        return True

    host = os.environ.get("SMTP_HOST", "").strip()
    port = int(os.environ.get("SMTP_PORT", "587") or 587)
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    sender = os.environ.get("SMTP_FROM_EMAIL", "").strip() or username
    sender_name = os.environ.get("SMTP_FROM_NAME", "FixTude").strip()
    if not host or not sender:
        return False

    receipt_path = generate_payment_pdf(payment_id, "receipt")
    fiscal_path = generate_payment_pdf(payment_id, "fiscal")
    service = PAYMENT_SERVICES.get(payment["service"], {})

    msg = EmailMessage()
    msg["Subject"] = f"FixTude - pagamento ricevuto € {payment['amount']/100:.2f}".replace('.', ',')
    msg["From"] = f"{sender_name} <{sender}>"
    msg["To"] = payment["user_email"]
    msg.set_content(
        f"Abbiamo ricevuto il pagamento di € {payment['amount']/100:.2f}.\n\n"
        f"Servizio: {service.get('name', payment['service'])}\n\n"
        "In allegato trovi la ricevuta di pagamento e il documento riepilogativo. "
        "I documenti restano disponibili anche nella tua Area Privata.\n\n"
        "Nota: il documento riepilogativo non sostituisce una fattura elettronica SdI."
    )
    for path, label in ((receipt_path, "ricevuta.pdf"), (fiscal_path, "documento_riepilogativo.pdf")):
        with open(path, "rb") as f:
            msg.add_attachment(f.read(), maintype="application", subtype="pdf", filename=label)

    with smtplib.SMTP(host, port, timeout=15) as smtp:
        smtp.starttls()
        if username and password:
            smtp.login(username, password)
        smtp.send_message(msg)

    conn = db_connect()
    conn.execute("UPDATE payments SET receipt_email_sent_at = ? WHERE id = ?", (now_iso(), payment_id))
    conn.commit()
    conn.close()
    return True


def prepare_payment_documents(payment_id):
    """Prepara i documenti senza bloccare il webhook con l'invio SMTP."""
    try:
        generate_payment_pdf(payment_id, "receipt")
        generate_payment_pdf(payment_id, "fiscal")
        # L'email viene tentata in background; se SMTP non è configurato
        # il pagamento resta comunque correttamente registrato e archiviato.
        threading.Thread(target=lambda: _safe_send_payment_email(payment_id), daemon=True).start()
    except Exception:
        app.logger.exception("Errore nella preparazione dei documenti del pagamento %s", payment_id)


def _safe_send_payment_email(payment_id):
    try:
        send_payment_email(payment_id)
    except Exception:
        app.logger.exception("Errore invio email pagamento %s", payment_id)


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
background:linear-gradient(135deg,#18212f 0%,#1e2b40 52%,#293f68 100%);
color:white;
align-self:start;
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
.access-buttons{
display:flex;
gap:10px;
margin-top:5px;
}
.access-buttons .button{
flex:1;
text-align:center;
margin:0;
white-space:nowrap;
}
@media(max-width:760px){
.access-buttons .button{
font-size:13px;
padding:12px 8px;
}
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

.education{margin-top:52px;padding:35px;background:#18212f;color:white;border-radius:20px;box-shadow:0 15px 40px rgba(0,0,0,.08)}
.education-head{display:flex;justify-content:space-between;align-items:flex-start;gap:25px;margin-bottom:25px}
.education-head small{color:#aeb7c6;font-weight:700;letter-spacing:.4px}
.education-head h2{font-size:34px;margin:8px 0 6px}
.education-head p{margin:0;color:#d4dae3;max-width:700px}
.education-badge{background:#4f46e5;padding:9px 13px;border-radius:999px;font-size:12px;font-weight:800;white-space:nowrap}
.education-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:15px}
.education-card{display:block;padding:22px;background:#293241;border:1px solid rgba(255,255,255,.08);border-radius:15px;color:white;text-decoration:none;transition:transform .15s,background .15s}
.education-card:hover{transform:translateY(-3px);background:#323c4c}
.education-card span{display:inline-block;font-size:12px;color:#aeb7c6;font-weight:800;margin-bottom:16px}
.education-card strong{display:block;font-size:18px;line-height:1.3;margin-bottom:9px}
.education-card p{font-size:14px;line-height:1.5;color:#cbd2dc;margin:0 0 18px}
.education-card b{font-size:13px;color:#fff}
.education-cta{margin-top:18px;padding:18px 20px;border-radius:13px;background:#f5f7fa;color:#18212f;display:flex;justify-content:space-between;gap:20px;align-items:center}
.education-cta strong{font-size:15px}.education-cta span{font-size:13px;color:#697586}
@media(max-width:800px){.education-head{flex-direction:column}.education-grid{grid-template-columns:1fr}.education-cta{flex-direction:column;align-items:flex-start}}

/* ============================================================
   FixTude responsive refinement — final symmetry
   ============================================================ */

/* The two main cards always have exactly the same height. */
.grid{
    align-items:stretch !important;
}
/* Grid stretch keeps both cards equal without forcing
   a content-box height that can overflow into the next section. */

/* The SIC card uses the available height intelligently. */
.dark{
    align-self:stretch !important;
    display:flex !important;
    flex-direction:column !important;
}

/* More air between description and SIC buttons,
   while the 2x2 grid fills the card proportionally. */
.free{
    display:grid !important;
    grid-template-columns:repeat(2,minmax(0,1fr));
    grid-template-rows:repeat(2,minmax(0,1fr));
    gap:18px !important;
    margin-top:30px !important;
    margin-bottom:8px !important;
    flex:1 1 auto !important;
    align-content:stretch !important;
}
.free a{
    display:flex !important;
    flex-direction:column;
    justify-content:center;
    min-width:0;
    min-height:76px;
    margin:0 !important;
    padding:17px 20px !important;
    border-radius:14px;
    text-decoration:none;
    background:rgba(255,255,255,.10) !important;
    border:1px solid rgba(255,255,255,.09);
    box-shadow:0 5px 14px rgba(0,0,0,.11);
    transition:
        transform .18s ease,
        background .18s ease,
        box-shadow .18s ease,
        border-color .18s ease;
}
.free a:hover{
    transform:translateY(-4px);
    background:rgba(255,255,255,.18) !important;
    border-color:rgba(255,255,255,.20);
    box-shadow:0 12px 25px rgba(0,0,0,.24);
}
.free a:active{
    transform:translateY(-1px);
}
.free a strong{
    display:inline-block;
    margin-bottom:3px;
}

.sic-title{
    margin-top:20px;
    margin-bottom:0;
}

.education-grid{
    display:grid !important;
    grid-template-columns:repeat(3,minmax(0,1fr));
    gap:16px !important;
}
.education-card{
    min-width:0;
}

@media(max-width:800px){
    .grid{
        grid-template-columns:1fr !important;
        align-items:stretch !important;
    }
    .dark{
        align-self:auto !important;
    }
    .sic-title{
        white-space:normal !important;
    }
}

@media(max-width:760px){
    body{
        overflow-x:hidden;
    }
    .free{
        grid-template-columns:repeat(2,minmax(0,1fr)) !important;
        grid-template-rows:repeat(2,minmax(0,1fr)) !important;
        gap:12px !important;
        margin-top:22px !important;
        margin-bottom:0 !important;
    }
    .free a{
        min-height:76px;
        padding:14px 12px !important;
        font-size:13px;
        line-height:1.35;
    }
    .education-grid{
        grid-template-columns:1fr !important;
        gap:12px !important;
    }
    .education-card{
        width:100%;
        margin:0 !important;
    }
    h1{
        font-size:32px !important;
        line-height:1.15 !important;
    }
    h2{
        font-size:24px !important;
        line-height:1.2 !important;
    }
    p{
        line-height:1.5;
    }
    .card{
        padding:22px 18px !important;
    }
    .container{
        width:100% !important;
        max-width:100% !important;
        padding-left:14px !important;
        padding-right:14px !important;
    }
}

@media(max-width:430px){
    .free{
        gap:10px !important;
        margin-top:20px !important;
    }
    .free a{
        min-height:72px;
        padding:12px 10px !important;
    }
    .free a strong{
        font-size:14px;
    }
}

.education-intro{
    white-space:nowrap;
}
@media(max-width:760px){
    .education-intro{
        white-space:normal;
    }
}

/* ============================================================
   HOME ACCESS — FixTude v25
   ============================================================ */
.home-access{margin-top:18px}
.register-home{display:block !important;width:100%;text-align:center;margin-bottom:10px}
.access-buttons{display:grid !important;grid-template-columns:repeat(2,minmax(0,1fr)) !important;gap:10px !important;margin-top:0 !important}
.access-buttons .button{width:100%;margin:0 !important;text-align:center;white-space:nowrap}
@media(max-width:430px){
  .logo{display:inline-block !important;width:max-content !important;white-space:nowrap !important;letter-spacing:-1px !important}
  .logo span{display:inline !important}
  .home-access{margin-top:14px}
  .register-home{padding:12px 14px !important;margin-bottom:15px}
  .access-buttons{grid-template-columns:repeat(2,minmax(0,1fr)) !important;gap:8px !important}
  .access-buttons .button{padding:11px 7px !important;font-size:12px !important;line-height:1.15 !important}
}

/* ============================================================
   MOBILE / VISUAL REFINEMENT — FixTude v23
   ============================================================ */
*{box-sizing:border-box}
html,body{width:100%;max-width:100%;overflow-x:hidden}
body{
  background:linear-gradient(135deg,#eef2ff 0%,#f8fafc 48%,#eef7f5 100%);
  color:#18212f;
}
.wrap{max-width:1120px;padding:20px 24px 34px}
nav{gap:18px;flex-wrap:wrap;padding:4px 0 8px}
nav>div:last-child{display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:wrap}
nav>div:last-child a{
  display:inline-flex;align-items:center;justify-content:center;
  min-height:38px;padding:9px 12px;border-radius:10px;
  text-decoration:none;font-weight:700;color:#344054;
  background:rgba(255,255,255,.72);border:1px solid rgba(79,70,229,.10);
  box-shadow:0 3px 12px rgba(30,41,59,.05);
}
nav>div:last-child a:hover{background:#fff;color:#4f46e5;transform:translateY(-1px)}
.logo{letter-spacing:-.7px}
.grid{gap:20px;margin-top:30px}
.card{padding:30px}
.card:first-child{background:rgba(255,255,255,.94)}
.home-price{margin:17px 0 18px;padding:13px 15px;border:1px solid rgba(79,70,229,.16);border-radius:12px;background:linear-gradient(110deg,rgba(238,242,255,.88),rgba(248,250,252,.92));}
.home-price strong{display:block;color:#4f46e5;font-size:17px;line-height:1.3}
.home-price span{display:block;margin-top:5px;color:#697586;font-size:12px;line-height:1.4}
@media(max-width:760px){.home-price{margin:14px 0;padding:12px 11px}.home-price strong{font-size:15px}.home-price span{font-size:11px}}
.dark{background:linear-gradient(145deg,#18212f 0%,#27375b 100%)}
.education{
  margin-top:30px;padding:27px;
  background:linear-gradient(145deg,#18212f 0%,#24375d 100%);
}
.education-head{gap:15px;margin-bottom:18px}
.education-head h2{font-size:30px}
.education-grid{gap:12px}
.education-card{padding:18px}
.education-card span{margin-bottom:10px}
.education-card strong{font-size:16px}
.education-card p{font-size:13px;line-height:1.4;margin-bottom:12px}
.education-cta{margin-top:14px;padding:15px 17px}

@media(max-width:760px){
  .wrap{padding:12px 12px 24px}
  nav{display:block;margin-bottom:6px}
  nav>.logo{font-size:27px;margin:2px 4px 11px}
  nav>div:last-child{
    display:grid;grid-template-columns:1fr 1fr;gap:7px;width:100%;
  }
  nav>div:last-child a{
    min-height:40px;padding:8px 7px;font-size:12px;line-height:1.15;
    white-space:normal;text-align:center;
  }
  nav>div:last-child a:last-child{
    grid-column:1 / -1;
  }
  .grid{display:grid;grid-template-columns:1fr;gap:12px;margin-top:14px}
  .card{padding:21px 17px;border-radius:16px}
  h1{font-size:30px!important;line-height:1.08;margin:11px 0 13px}
  h2{font-size:22px!important;line-height:1.15}
  p{font-size:14px;line-height:1.48}
  .button{padding:11px 12px;margin:4px 0;font-size:13px}
  .access-buttons{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:7px}
  .access-buttons .button{font-size:12px;min-height:43px;display:flex;align-items:center;justify-content:center;padding:8px 6px;white-space:normal;line-height:1.15}
  .free{gap:9px!important;margin-top:18px!important}
  .free a{min-height:68px!important;padding:11px 9px!important;font-size:12px;line-height:1.25}
  .sic-title{margin-top:14px}
  .education{margin-top:18px;padding:19px 13px;border-radius:16px}
  .education-head{display:flex;flex-direction:row;align-items:center;gap:9px;margin-bottom:13px}
  .education-head>div:first-child{min-width:0;flex:1}
  .education-head small{font-size:9px;letter-spacing:.25px}
  .education-head h2{font-size:23px!important;margin:4px 0}
  .education-head p{font-size:12px;line-height:1.35;margin:0}
  .education-badge{font-size:9px;padding:7px 8px;flex:0 0 auto}
  /* On mobile the three education cards stay horizontal and compact. */
  .education-grid{
    display:flex!important;flex-direction:row!important;gap:9px!important;
    overflow-x:auto;overflow-y:hidden;padding:2px 2px 7px;
    scroll-snap-type:x mandatory;-webkit-overflow-scrolling:touch;
  }
  .education-card{
    flex:0 0 78%;width:78%;min-width:78%;
    padding:15px 13px;border-radius:13px;scroll-snap-align:start;
  }
  .education-card span{font-size:10px;margin-bottom:7px}
  .education-card strong{font-size:15px;line-height:1.2;margin-bottom:7px}
  .education-card p{font-size:12px;line-height:1.35;margin-bottom:9px}
  .education-card b{font-size:11px}
  .education-cta{margin-top:9px;padding:12px 13px;display:block}
  .education-cta strong{display:block;font-size:12px;margin-bottom:4px}
  .education-cta span{font-size:11px;line-height:1.3}
  footer{margin-top:28px!important;padding:18px 0!important;font-size:11px}
}
@media(max-width:430px){
  nav>div:last-child a{font-size:11px}
  .access-buttons .button{font-size:11px}
  .education-card{flex-basis:84%;min-width:84%;width:84%}
}

/* SIC non attivi prima della registrazione/accesso cliente */
.free.sic-locked{opacity:.58;filter:saturate(.55)}
.sic-locked-message{
  grid-column:1 / -1;
  display:flex;align-items:center;justify-content:center;
  min-height:76px;padding:14px 16px;text-align:center;
  border:1px dashed rgba(255,255,255,.28);border-radius:14px;
  color:#e2e8f0;font-size:13px;line-height:1.4;
}
/* CTA registrazione nel box SIC: stessa gerarchia visiva del box bianco */
.sic-register{
  display:block !important;
  width:100%;
  text-align:center;
  margin:18px 0 0 !important;
  padding:13px 16px;
  line-height:1.25;
  flex-shrink:0;
}
@media(max-width:760px){
  .sic-register{
    margin-top:16px !important;
    padding:12px 14px;
    font-size:14px;
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

<div class="home-price">
  <strong>Analisi e documenti a partire da 1,99 €</strong>
  <span>Analisi FixTude € 1,99 · Kit personalizzato € 9,99</span>
</div>

<p>
<strong>✓ Analisi automatica</strong><br>
<strong>✓ Possibili scenari</strong><br>
<strong>✓ Esperto FixTude</strong><br>
<strong>✓ KIT PDF FixTude</strong>
</p>

<div class="home-access">
<a class="button primary register-home" href="/registrazione">
Registrati
</a>

<div class="access-buttons">
<a class="button light" href="/privato/login">
👤 Accesso cliente
</a>

<a class="button light" href="/risolutore/login">
🔐 Area Riservata FixTude
</a>
</div>
</div>

</div>

<div class="card dark">

<small style="color:#bfc6d4">
SERVIZIO GRATUITO
</small>

<h2 class="sic-title">
Controlla autonomamente le banche dati
</h2>

<p>
Puoi richiedere direttamente agli enti
le informazioni che ti riguardano.
</p>

<div class="free {% if not session.get('user') or session.get('user', {}).get('role') != 'debtor' %}sic-locked{% endif %}">

{% if session.get('user') and session.get('user', {}).get('role') == 'debtor' %}
<a target="_blank" rel="noopener noreferrer"
href="https://www.modulorichiesta.crif.com/">
<strong>CRIF</strong><br>
Modulo ufficiale
</a>

<a target="_blank" rel="noopener noreferrer"
href="https://www.experian.it/content/dam/noindex/emea/italy/Nuovo-modulo-SIC.pdf">
<strong>EXPERIAN</strong><br>
Modulo ufficiale
</a>

<a target="_blank" rel="noopener noreferrer"
href="https://consumatore.ctconline.it/sic/apri-istanza">
<strong>CTC</strong><br>
Procedura ufficiale
</a>

<a target="_blank" rel="noopener noreferrer"
href="https://www.bancaditalia.it/servizi-cittadino/servizi/accesso-cai/Modulo-di-richiesta-dei-dati-nominativi-CAI.pdf?force_download=1">
<strong>CAI</strong><br>
Modulo ufficiale
</a>
{% else %}
<div class="sic-locked-message">Registrati o accedi al tuo account per attivare i collegamenti alle banche dati.</div>
{% endif %}

</div>

{% if not session.get('user') or session.get('user', {}).get('role') != 'debtor' %}
<a class="button primary register-home sic-register" href="/registrazione">
Registrati per accedere ai SIC
</a>
{% else %}
<a class="button light sic-register" href="/privato">
Accedi alla tua area cliente
</a>
{% endif %}

</div>

</div>

<section class="education">
  <div class="education-head">
    <div>
      <small>IMPARA A CAPIRE LA TUA SITUAZIONE</small>
      <h2>Educazione finanziaria</h2>
      <p>Informazioni semplici e concrete per orientarti tra debiti, rate, segnalazioni e possibilità da approfondire.</p>
    </div>
    <div class="education-badge">GUIDE GRATUITE</div>
  </div>
  <div class="education-grid">
    <a href="{{ url_for('guide_situation') }}" class="education-card"><span>01</span><strong>Capire la propria situazione debitoria</strong><p>Da dove partire e quali dati raccogliere prima di prendere decisioni.</p><b>Leggi la guida →</b></a>
    <a href="{{ url_for('guide_sic') }}" class="education-card"><span>02</span><strong>CRIF, Experian e CTC: cosa sono?</strong><p>Come funzionano i SIC e quali informazioni possono contenere.</p><b>Leggi la guida →</b></a>
    <a href="{{ url_for('guide_rates') }}" class="education-card"><span>03</span><strong>Quando le rate diventano difficili da sostenere</strong><p>Come valutare entrate, spese, rate e disponibilità mensile.</p><b>Leggi la guida →</b></a>
  </div>
  <div class="education-cta"><strong>Prima di affrontare un problema, impara a leggerlo.</strong><span>Le guide FixTude sono gratuite e pensate per essere comprensibili a tutti.</span></div>
</section>

<footer style="margin-top:60px;padding:25px 0;border-top:1px solid #ddd">
FixTude · <a href="mailto:info@fixtude.it">info@fixtude.it</a> · P. IVA: 15990471003
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

@app.route("/educazione-finanziaria/situazione-debitoria")
def guide_situation():
    return render_template_string('<!doctype html><html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Come capire la tua situazione debitoria · FixTude</title><style>\n:root{--blue:#4f46e5;--ink:#18212f;--muted:#697586;--bg:#f6f8fb;--line:#e5e7eb}*{box-sizing:border-box}body{margin:0;font-family:Arial,sans-serif;background:var(--bg);color:var(--ink)}.guide-wrap{max-width:900px;margin:auto;padding:22px}.guide-nav{display:flex;justify-content:space-between;align-items:center;margin-bottom:35px}.guide-nav .logo{font-size:28px;font-weight:800}.guide-nav .logo span{color:var(--blue)}.guide-nav a{color:var(--blue);text-decoration:none;font-weight:700}.hero{background:var(--ink);color:white;padding:42px;border-radius:22px;margin-bottom:22px}.hero small{color:#aeb7c6;font-weight:800;letter-spacing:.5px}.hero h1{font-size:42px;line-height:1.1;margin:12px 0}.hero p{font-size:18px;line-height:1.6;color:#d4dae3;margin:0}.article{background:white;border:1px solid var(--line);border-radius:20px;padding:38px;box-shadow:0 12px 35px rgba(0,0,0,.05)}.article h2{font-size:25px;margin:30px 0 10px}.article h2:first-child{margin-top:0}.article p,.article li{font-size:16px;line-height:1.7}.article ul{padding-left:23px}.tip{background:#f1f3ff;border-left:4px solid var(--blue);padding:17px 18px;border-radius:10px;margin:22px 0}.warning{background:#fff7ed;border-left:4px solid #f59e0b;padding:17px 18px;border-radius:10px;margin:22px 0}.links{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin:18px 0}.links a{padding:14px;background:#f6f8fb;border:1px solid var(--line);border-radius:10px;color:var(--blue);text-decoration:none;font-weight:700}.cta{margin-top:28px;background:var(--ink);color:white;padding:25px;border-radius:16px}.cta h3{margin:0 0 8px}.cta p{color:#d4dae3}.cta a{display:inline-block;background:var(--blue);color:white;text-decoration:none;padding:12px 18px;border-radius:9px;font-weight:700}.back{display:inline-block;margin-bottom:18px;color:var(--blue);text-decoration:none;font-weight:700}footer{text-align:center;color:var(--muted);font-size:13px;padding:25px 0}@media(max-width:700px){.guide-wrap{padding:14px}.guide-nav{margin-bottom:20px}.guide-nav .logo{font-size:24px}.hero{padding:27px 20px;border-radius:16px}.hero h1{font-size:31px}.hero p{font-size:16px}.article{padding:22px 18px;border-radius:16px}.article h2{font-size:22px}.article p,.article li{font-size:15px}.links{grid-template-columns:1fr}.cta{padding:20px}.cta a{display:block;text-align:center}}\n</style></head><body><div class="guide-wrap"><div class="guide-nav"><div class="logo">Fix<span>Tude</span></div><a href="/privato">Area Personale</a></div><a class="back" href="/">← Torna a FixTude</a><section class="hero"><small>GUIDA GRATUITA · 01</small><h1>Come capire la tua situazione debitoria</h1><p>Prima di cercare una soluzione, metti in ordine i numeri che descrivono davvero la tua situazione.</p></section><article class="article"><h2>Da dove partire</h2><p>Quando si hanno più debiti, il primo errore è guardare soltanto il totale. Per capire davvero la situazione servono almeno quattro elementi: quanto entra ogni mese, quanto si spende, quanto si paga in rate e quanto resta disponibile.</p><div class="tip"><strong>Regola pratica:</strong> prima di cercare una soluzione, costruisci una fotografia aggiornata della situazione.</div><h2>1. Raccogli tutte le posizioni</h2><p>Prepara un elenco dei debiti e, per ciascuno, indica creditore, importo residuo, rata, scadenza, eventuali arretrati e tipo di finanziamento o posizione.</p><h2>2. Calcola le entrate reali</h2><p>Considera le entrate mensili effettivamente disponibili. Se variano, usa una media prudente invece di basarti sul mese migliore.</p><h2>3. Separa spese essenziali e spese variabili</h2><p>Affitto o mutuo, utenze, alimentazione, trasporti e altre spese necessarie vanno distinte dalle spese che possono variare. Questo aiuta a capire quale margine esiste realmente.</p><h2>4. Calcola la disponibilità mensile</h2><p>Un calcolo semplice è: <strong>entrate − spese necessarie = disponibilità prima delle rate</strong>. Da qui si può valutare il peso complessivo degli impegni finanziari.</p><h2>5. Controlla anche le informazioni esterne</h2><p>Se esistono dubbi su finanziamenti, ritardi o segnalazioni, è utile verificare i dati presenti nei sistemi di informazione creditizia e conservarne una copia.</p><div class="warning"><strong>Attenzione:</strong> una fotografia incompleta può portare a decisioni sbagliate. Non inserire un nuovo impegno finanziario solo perché la rata sembra sostenibile isolatamente.</div><h2>Una fotografia utile</h2><p>Alla fine dovresti poter rispondere con numeri aggiornati a cinque domande: quanto devo? a chi? quanto pago ogni mese? quanto mi costa vivere? quanto posso realisticamente destinare ai debiti?</p><div class="cta"><h3>Vuoi capire meglio la tua situazione?</h3><p>FixTude analizza dati, entrate, spese e debiti e costruisce possibili scenari da approfondire.</p><a href="/privato/situazione">Inizia con FixTude →</a></div></article><footer>FixTude · <a href="mailto:info@fixtude.it">info@fixtude.it</a> · P. IVA 15990471003</footer></div></body></html>')


@app.route("/educazione-finanziaria/crif-experian-ctc")
def guide_sic():
    return render_template_string('<!doctype html><html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CRIF, Experian e CTC: cosa sono? · FixTude</title><style>\n:root{--blue:#4f46e5;--ink:#18212f;--muted:#697586;--bg:#f6f8fb;--line:#e5e7eb}*{box-sizing:border-box}body{margin:0;font-family:Arial,sans-serif;background:var(--bg);color:var(--ink)}.guide-wrap{max-width:900px;margin:auto;padding:22px}.guide-nav{display:flex;justify-content:space-between;align-items:center;margin-bottom:35px}.guide-nav .logo{font-size:28px;font-weight:800}.guide-nav .logo span{color:var(--blue)}.guide-nav a{color:var(--blue);text-decoration:none;font-weight:700}.hero{background:var(--ink);color:white;padding:42px;border-radius:22px;margin-bottom:22px}.hero small{color:#aeb7c6;font-weight:800;letter-spacing:.5px}.hero h1{font-size:42px;line-height:1.1;margin:12px 0}.hero p{font-size:18px;line-height:1.6;color:#d4dae3;margin:0}.article{background:white;border:1px solid var(--line);border-radius:20px;padding:38px;box-shadow:0 12px 35px rgba(0,0,0,.05)}.article h2{font-size:25px;margin:30px 0 10px}.article h2:first-child{margin-top:0}.article p,.article li{font-size:16px;line-height:1.7}.article ul{padding-left:23px}.tip{background:#f1f3ff;border-left:4px solid var(--blue);padding:17px 18px;border-radius:10px;margin:22px 0}.warning{background:#fff7ed;border-left:4px solid #f59e0b;padding:17px 18px;border-radius:10px;margin:22px 0}.links{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin:18px 0}.links a{padding:14px;background:#f6f8fb;border:1px solid var(--line);border-radius:10px;color:var(--blue);text-decoration:none;font-weight:700}.cta{margin-top:28px;background:var(--ink);color:white;padding:25px;border-radius:16px}.cta h3{margin:0 0 8px}.cta p{color:#d4dae3}.cta a{display:inline-block;background:var(--blue);color:white;text-decoration:none;padding:12px 18px;border-radius:9px;font-weight:700}.back{display:inline-block;margin-bottom:18px;color:var(--blue);text-decoration:none;font-weight:700}footer{text-align:center;color:var(--muted);font-size:13px;padding:25px 0}@media(max-width:700px){.guide-wrap{padding:14px}.guide-nav{margin-bottom:20px}.guide-nav .logo{font-size:24px}.hero{padding:27px 20px;border-radius:16px}.hero h1{font-size:31px}.hero p{font-size:16px}.article{padding:22px 18px;border-radius:16px}.article h2{font-size:22px}.article p,.article li{font-size:15px}.links{grid-template-columns:1fr}.cta{padding:20px}.cta a{display:block;text-align:center}}\n</style></head><body><div class="guide-wrap"><div class="guide-nav"><div class="logo">Fix<span>Tude</span></div><a href="/privato">Area Personale</a></div><a class="back" href="/">← Torna a FixTude</a><section class="hero"><small>GUIDA GRATUITA · 02</small><h1>CRIF, Experian e CTC: cosa sono?</h1><p>Come controllare le principali banche dati creditizie e distinguere i SIC dalla CAI.</p></section><article class="article"><h2>Cosa sono i SIC</h2><p>I Sistemi di Informazioni Creditizie (SIC) sono banche dati gestite da soggetti privati che raccolgono e condividono, secondo le regole applicabili, informazioni sui rapporti di credito. Tra i sistemi conosciuti in Italia ci sono CRIF, Experian e CTC.</p><h2>Perché controllarli</h2><p>Prima di valutare una situazione debitoria può essere utile sapere quali rapporti risultano presenti e verificare che i dati siano corretti e aggiornati.</p><h2>CRIF</h2><p>Per chiedere l\'accesso ai propri dati è possibile utilizzare i canali ufficiali indicati da CRIF.</p><div class="links"><a target="_blank" rel="noopener" href="https://www.crif.it/consumatori/sistema-informazioni-creditizie-sic/accedi-ai-tuoi-dati-consumatori/">CRIF · Accesso ai dati</a><a target="_blank" rel="noopener" href="https://www.modulorichiesta.crif.com/">CRIF · Modulo richiesta</a></div><h2>Experian</h2><p>Experian mette a disposizione la documentazione per richiedere l\'accesso ai dati del SIC.</p><div class="links"><a target="_blank" rel="noopener" href="https://www.experian.it/content/dam/noindex/emea/italy/Nuovo-modulo-SIC.pdf">Experian · Modulo SIC</a></div><h2>CTC</h2><p>Per CTC è disponibile una procedura ufficiale dedicata al consumatore.</p><div class="links"><a target="_blank" rel="noopener" href="https://consumatore.ctconline.it/sic/apri-istanza">CTC · Accesso consumatore</a></div><h2>Non confondere SIC e CAI</h2><p>La Centrale d\'Allarme Interbancaria (CAI) è distinta dai SIC. Per informazioni e accesso ai dati CAI si può fare riferimento alla Banca d\'Italia.</p><div class="links"><a target="_blank" rel="noopener" href="https://www.bancaditalia.it/servizi-cittadino/servizi/accesso-cai/index.html">Banca d\'Italia · CAI</a><a target="_blank" rel="noopener" href="https://www.bancaditalia.it/servizi-cittadino/servizi/accesso-cai/Modulo-di-richiesta-dei-dati-nominativi-CAI.pdf?force_download=1">CAI · Modulo ufficiale</a></div><div class="warning"><strong>Importante:</strong> trovare un\'informazione in un SIC non significa automaticamente che un finanziamento verrà rifiutato o che una posizione sia irregolare. Il significato dipende dai dati presenti e dal contesto.</div><div class="cta"><h3>Vuoi capire meglio la tua situazione?</h3><p>FixTude analizza dati, entrate, spese e debiti e costruisce possibili scenari da approfondire.</p><a href="/privato/situazione">Inizia con FixTude →</a></div></article><footer>FixTude · <a href="mailto:info@fixtude.it">info@fixtude.it</a> · P. IVA 15990471003</footer></div></body></html>')


@app.route("/educazione-finanziaria/rate-difficili")
def guide_rates():
    return render_template_string('<!doctype html><html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Non riesci più a pagare le rate? Da dove cominciare · FixTude</title><style>\n:root{--blue:#4f46e5;--ink:#18212f;--muted:#697586;--bg:#f6f8fb;--line:#e5e7eb}*{box-sizing:border-box}body{margin:0;font-family:Arial,sans-serif;background:var(--bg);color:var(--ink)}.guide-wrap{max-width:900px;margin:auto;padding:22px}.guide-nav{display:flex;justify-content:space-between;align-items:center;margin-bottom:35px}.guide-nav .logo{font-size:28px;font-weight:800}.guide-nav .logo span{color:var(--blue)}.guide-nav a{color:var(--blue);text-decoration:none;font-weight:700}.hero{background:var(--ink);color:white;padding:42px;border-radius:22px;margin-bottom:22px}.hero small{color:#aeb7c6;font-weight:800;letter-spacing:.5px}.hero h1{font-size:42px;line-height:1.1;margin:12px 0}.hero p{font-size:18px;line-height:1.6;color:#d4dae3;margin:0}.article{background:white;border:1px solid var(--line);border-radius:20px;padding:38px;box-shadow:0 12px 35px rgba(0,0,0,.05)}.article h2{font-size:25px;margin:30px 0 10px}.article h2:first-child{margin-top:0}.article p,.article li{font-size:16px;line-height:1.7}.article ul{padding-left:23px}.tip{background:#f1f3ff;border-left:4px solid var(--blue);padding:17px 18px;border-radius:10px;margin:22px 0}.warning{background:#fff7ed;border-left:4px solid #f59e0b;padding:17px 18px;border-radius:10px;margin:22px 0}.links{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;margin:18px 0}.links a{padding:14px;background:#f6f8fb;border:1px solid var(--line);border-radius:10px;color:var(--blue);text-decoration:none;font-weight:700}.cta{margin-top:28px;background:var(--ink);color:white;padding:25px;border-radius:16px}.cta h3{margin:0 0 8px}.cta p{color:#d4dae3}.cta a{display:inline-block;background:var(--blue);color:white;text-decoration:none;padding:12px 18px;border-radius:9px;font-weight:700}.back{display:inline-block;margin-bottom:18px;color:var(--blue);text-decoration:none;font-weight:700}footer{text-align:center;color:var(--muted);font-size:13px;padding:25px 0}@media(max-width:700px){.guide-wrap{padding:14px}.guide-nav{margin-bottom:20px}.guide-nav .logo{font-size:24px}.hero{padding:27px 20px;border-radius:16px}.hero h1{font-size:31px}.hero p{font-size:16px}.article{padding:22px 18px;border-radius:16px}.article h2{font-size:22px}.article p,.article li{font-size:15px}.links{grid-template-columns:1fr}.cta{padding:20px}.cta a{display:block;text-align:center}}\n</style></head><body><div class="guide-wrap"><div class="guide-nav"><div class="logo">Fix<span>Tude</span></div><a href="/privato">Area Personale</a></div><a class="back" href="/">← Torna a FixTude</a><section class="hero"><small>GUIDA GRATUITA · 03</small><h1>Non riesci più a pagare le rate? Da dove cominciare</h1><p>Prima di cercare una soluzione, misura il peso reale delle rate sul tuo bilancio mensile.</p></section><article class="article"><h2>Quando una rata diventa un problema</h2><p>La difficoltà non nasce necessariamente quando una rata è molto alta. Può comparire quando, sommate tutte le rate e le spese necessarie, il reddito non lascia più un margine sufficiente.</p><h2>1. Somma tutte le rate</h2><p>Inserisci nello stesso calcolo finanziamenti, prestiti personali, carte revolving e altri impegni ricorrenti. Non guardare una rata alla volta.</p><h2>2. Calcola il reddito disponibile</h2><p>Parti dalle entrate mensili e sottrai le spese necessarie. Il risultato indica quanto rimane prima di considerare il peso complessivo delle rate.</p><h2>3. Guarda il rapporto tra rate e reddito</h2><p>Il rapporto tra rate mensili e reddito è un indicatore utile per descrivere il peso del debito, ma non è una soglia universale valida per tutti. Una stessa percentuale può avere effetti diversi in base a casa, famiglia, spese e stabilità del reddito.</p><div class="tip"><strong>Esempio:</strong> due persone possono avere la stessa rata totale, ma una può avere molte più spese essenziali dell\'altra. Per questo la rata da sola non racconta tutta la situazione.</div><h2>4. Individua i segnali di pressione</h2><ul><li>usi continuamente il credito per coprire spese ordinarie;</li><li>paghi una rata ricorrendo a un altro finanziamento;</li><li>rimandi spese essenziali;</li><li>hai già rate scadute o pagamenti in ritardo;</li><li>il reddito disponibile cambia molto da un mese all\'altro.</li></ul><h2>5. Non aspettare che il problema diventi urgente</h2><p>Se il bilancio mensile è già sotto pressione, conviene ricostruire subito la situazione completa e valutare quali informazioni mancano. Parlare tempestivamente con i soggetti interessati può essere più utile che attendere l\'accumulo di ulteriori arretrati.</p><div class="warning"><strong>Attenzione:</strong> non esiste una percentuale magica che stabilisca da sola se una situazione è sostenibile. Servono reddito, spese, composizione del debito e regolarità dei pagamenti.</div><h2>Il primo passo è misurare</h2><p>Con numeri aggiornati puoi capire se il problema riguarda una singola rata, l\'insieme degli impegni oppure l\'equilibrio generale tra entrate e uscite.</p><div class="cta"><h3>Vuoi capire meglio la tua situazione?</h3><p>FixTude analizza dati, entrate, spese e debiti e costruisce possibili scenari da approfondire.</p><a href="/privato/situazione">Inizia con FixTude →</a></div></article><footer>FixTude · <a href="mailto:info@fixtude.it">info@fixtude.it</a> · P. IVA 15990471003</footer></div></body></html>')




REGISTRATION_HTML = """
<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Registrazione FixTude</title>
<style>
*{box-sizing:border-box}
body{
    margin:0;
    font-family:Arial,Helvetica,sans-serif;
    background:#f5f7fb;
    color:#18212f;
    min-height:100vh;
}
.page{
    padding:28px 16px 50px;
}
.box{
    width:100%;
    max-width:460px;
    margin:35px auto 0;
    background:#fff;
    padding:34px;
    border:1px solid #e5e9f0;
    border-radius:20px;
    box-shadow:0 10px 30px rgba(24,33,47,.07);
}
.back{
    color:#4f46e5;
    text-decoration:none;
    font-weight:600;
    font-size:14px;
}
h1{
    margin:22px 0 8px;
    font-size:30px;
    line-height:1.15;
}
.subtitle{
    margin:0 0 26px;
    color:#667085;
    line-height:1.5;
    font-size:15px;
}
label{
    display:block;
    margin:0 0 7px;
    font-size:14px;
    font-weight:700;
    color:#344054;
}
.field{
    margin-bottom:19px;
}
input{
    width:100%;
    height:48px;
    padding:0 14px;
    border:1px solid #d8dee8;
    border-radius:10px;
    background:#fff;
    color:#18212f;
    font-size:16px;
    outline:none;
    transition:border-color .15s,box-shadow .15s;
}
input:focus{
    border-color:#4f46e5;
    box-shadow:0 0 0 3px rgba(79,70,229,.10);
}
.password-wrap{
    position:relative;
}
.password-wrap input{
    padding-right:48px;
}
.toggle-password{
    position:absolute;
    right:10px;
    top:50%;
    transform:translateY(-50%);
    width:32px;
    height:32px;
    padding:0;
    margin:0;
    background:transparent;
    color:#667085;
    border:0;
    border-radius:7px;
    cursor:pointer;
    display:flex;
    align-items:center;
    justify-content:center;
}
.toggle-password:hover{
    background:#f2f4f7;
    color:#344054;
}
.eye{
    width:18px;
    height:12px;
    border:1.7px solid currentColor;
    border-radius:70% 70% 70% 70% / 90% 90% 90% 90%;
    position:relative;
    display:block;
}
.eye:after{
    content:"";
    position:absolute;
    width:5px;
    height:5px;
    border:1.5px solid currentColor;
    border-radius:50%;
    left:50%;
    top:50%;
    transform:translate(-50%,-50%);
}
button.submit{
    width:100%;
    height:50px;
    margin-top:3px;
    padding:0 16px;
    background:#4f46e5;
    color:#fff;
    border:0;
    border-radius:10px;
    font-size:16px;
    font-weight:700;
    cursor:pointer;
}
button.submit:hover{
    background:#4338ca;
}
.email-note{
    margin:-5px 0 21px;
    padding:11px 13px;
    background:#f5f7ff;
    border:1px solid #e2e5ff;
    border-radius:9px;
    color:#555f73;
    font-size:13px;
    line-height:1.45;
}
.error{
    background:#fff1f2;
    color:#b42318;
    border:1px solid #fecdd3;
    padding:12px 13px;
    margin-bottom:20px;
    border-radius:9px;
    font-size:14px;
}
@media(max-width:600px){
    .page{
        padding:15px 12px 35px;
    }
    .box{
        margin:15px auto 0;
        padding:24px 19px 22px;
        border-radius:16px;
    }
    h1{
        font-size:26px;
    }
    .subtitle{
        font-size:14px;
        margin-bottom:22px;
    }
    input{
        height:48px;
        font-size:16px;
    }
}
</style>
</head>
<body>
<div class="page">
<div class="box">

<a class="back" href="/">← FixTude</a>

<h1>Crea il tuo account</h1>
<p class="subtitle">
Inserisci i tuoi dati per accedere alla tua Area Personale.
</p>

{% if error %}
<div class="error">{{ error }}</div>
{% endif %}

<form method="post">

<div class="field">
<label for="email">Email</label>
<input id="email" type="email" name="email" autocomplete="email" required>
</div>

<div class="email-note">
La tua email sarà utilizzata per accedere a FixTude e per ricevere i documenti PDF acquistati.
</div>

<div class="field">
<label for="reg-password">Password</label>
<div class="password-wrap">
<input id="reg-password" type="password" name="password"
       autocomplete="new-password" required minlength="8">
<button type="button" class="toggle-password"
        aria-label="Mostra password"
        onclick="togglePassword('reg-password', this)">
<span class="eye"></span>
</button>
</div>
</div>

<div class="field">
<label for="reg-confirm">Conferma password</label>
<div class="password-wrap">
<input id="reg-confirm" type="password" name="confirm_password"
       autocomplete="new-password" required minlength="8">
<button type="button" class="toggle-password"
        aria-label="Mostra conferma password"
        onclick="togglePassword('reg-confirm', this)">
<span class="eye"></span>
</button>
</div>
</div>

<button class="submit" type="submit">Crea account</button>

</form>
</div>
</div>

<script>
function togglePassword(id, button){
    const input = document.getElementById(id);
    const visible = input.type === "text";
    input.type = visible ? "password" : "text";
    button.setAttribute(
        "aria-label",
        visible ? "Mostra password" : "Nascondi password"
    );
}
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
# DATI FISCALI CLIENTE
# ============================================================

FISCAL_DATA_HTML = """
<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dati per la fattura · FixTude</title>
<style>
body{margin:0;font-family:Arial,sans-serif;background:#f6f8fb;color:#18212f}
.box{max-width:650px;margin:45px auto;background:white;padding:30px;border-radius:18px;box-shadow:0 12px 35px rgba(0,0,0,.06)}
h1{margin:0 0 10px;font-size:28px}.intro{color:#667085;line-height:1.5}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.full{grid-column:1/-1}
label{display:block;font-weight:700;font-size:13px;margin:7px 0 5px}input,select{width:100%;box-sizing:border-box;padding:12px;border:1px solid #d9dee8;border-radius:9px;font-size:14px;background:white}
button{width:100%;padding:13px;background:#4f46e5;color:white;border:0;border-radius:9px;font-weight:700;cursor:pointer;margin-top:16px}.error{background:#fee2e2;color:#991b1b;padding:12px;border-radius:9px;margin:15px 0}.note{background:#f1f3ff;padding:13px;border-radius:10px;font-size:13px;color:#4b5563;margin:18px 0}.back{display:inline-block;margin-bottom:18px;color:#4f46e5;text-decoration:none;font-weight:700}
@media(max-width:650px){.box{margin:15px;padding:21px;border-radius:15px}.grid{grid-template-columns:1fr}.full{grid-column:auto}h1{font-size:24px}}
</style>
</head>
<body>
<div class="box">
<a class="back" href="{{ url_for('payments') }}">← Torna ai pagamenti</a>
<h1>Dati per la fattura</h1>
<p class="intro">Inserisci i dati fiscali una sola volta. FixTude li conserverà nel tuo account per predisporre correttamente la documentazione fiscale relativa ai tuoi acquisti.</p>
{% if error %}<div class="error">{{ error }}</div>{% endif %}
<div class="note"><strong>Per un privato:</strong> sono necessari nome, cognome, codice fiscale e indirizzo. Se hai una PEC puoi indicarla; altrimenti la fattura elettronica potrà essere recapitata con codice destinatario 0000000.</div>
<form method="post">
<div class="grid">
<div><label>Nome *</label><input name="first_name" value="{{ data.first_name or '' }}" required></div>
<div><label>Cognome *</label><input name="last_name" value="{{ data.last_name or '' }}" required></div>
<div><label>Codice fiscale *</label><input name="fiscal_code" maxlength="16" value="{{ data.fiscal_code or '' }}" required></div>
<div><label>P. IVA (se presente)</label><input name="vat_number" maxlength="11" value="{{ data.vat_number or '' }}"></div>
<div class="full"><label>Indirizzo *</label><input name="billing_address" value="{{ data.billing_address or '' }}" required></div>
<div><label>CAP *</label><input name="billing_cap" maxlength="5" value="{{ data.billing_cap or '' }}" required></div>
<div><label>Comune *</label><input name="billing_city" value="{{ data.billing_city or '' }}" required></div>
<div><label>Provincia *</label><input name="billing_province" maxlength="2" value="{{ data.billing_province or '' }}" required></div>
<div><label>Codice destinatario</label><input name="recipient_code" maxlength="7" value="{{ data.recipient_code or '' }}" placeholder="0000000"></div>
<div><label>PEC</label><input type="email" name="pec" value="{{ data.pec or '' }}" placeholder="nome@pec.it"></div>
</div>
<button>Salva e continua al pagamento</button>
</form>
</div>
</body>
</html>
"""

@app.route("/dati-fiscali", methods=["GET", "POST"])
def fiscal_data():
    user = require_login("debtor")
    if not user:
        return redirect(url_for("debtor_login"))

    data = get_fiscal_data(user["email"])
    error = None

    if request.method == "POST":
        fields = {
            "first_name": request.form.get("first_name", "").strip(),
            "last_name": request.form.get("last_name", "").strip(),
            "fiscal_code": request.form.get("fiscal_code", "").strip().upper(),
            "billing_address": request.form.get("billing_address", "").strip(),
            "billing_cap": request.form.get("billing_cap", "").strip(),
            "billing_city": request.form.get("billing_city", "").strip(),
            "billing_province": request.form.get("billing_province", "").strip().upper(),
            "vat_number": request.form.get("vat_number", "").strip(),
            "recipient_code": request.form.get("recipient_code", "").strip().upper(),
            "pec": request.form.get("pec", "").strip().lower()
        }

        if any(not fields[k] for k in ("first_name","last_name","fiscal_code","billing_address","billing_cap","billing_city","billing_province")):
            error = "Compila tutti i campi obbligatori contrassegnati con *."
        elif not re.fullmatch(r"[A-Z0-9]{16}", fields["fiscal_code"]):
            error = "Il codice fiscale deve contenere 16 caratteri alfanumerici."
        elif not re.fullmatch(r"\d{5}", fields["billing_cap"]):
            error = "Il CAP deve contenere 5 cifre."
        elif not re.fullmatch(r"[A-Z]{2}", fields["billing_province"]):
            error = "La provincia deve essere indicata con due lettere."
        elif fields["vat_number"] and not re.fullmatch(r"\d{11}", fields["vat_number"]):
            error = "La Partita IVA deve contenere 11 cifre."
        elif fields["recipient_code"] and fields["recipient_code"] != "0000000" and not re.fullmatch(r"[A-Z0-9]{7}", fields["recipient_code"]):
            error = "Il codice destinatario deve contenere 7 caratteri."
        elif fields["pec"] and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", fields["pec"]):
            error = "L'indirizzo PEC non è valido."
        else:
            conn = db_connect()
            conn.execute("""
                UPDATE users SET first_name=?, last_name=?, fiscal_code=?,
                billing_address=?, billing_cap=?, billing_city=?, billing_province=?,
                vat_number=?, recipient_code=?, pec=? WHERE email=?
            """, (fields["first_name"],fields["last_name"],fields["fiscal_code"],fields["billing_address"],fields["billing_cap"],fields["billing_city"],fields["billing_province"],fields["vat_number"],fields["recipient_code"],fields["pec"],user["email"]))
            conn.commit(); conn.close()
            return redirect(url_for("payments"))

        data = {**data, **fields}

    return render_template_string(FISCAL_DATA_HTML, data=data, error=error)


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
<h1>{% if role == "debtor" %}Accesso area privata{% else %}Area Riservata FixTude{% endif %}</h1>
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

        # Se le credenziali demo del Risolutore vengono inserite nella login
        # generale, accompagniamo automaticamente l'utente alla login corretta.
        # Questo evita l'errore "Email o password non corretti" quando si parte
        # dal pulsante Accedi della home page. Il blocco di login del Risolutore
        # resta invariato.
        if email == "pro@fixtude.it" and password == "1234":
            return redirect(url_for("resolver_login"))

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

        # Accesso demo Risolutore: le credenziali pubblicate sono sempre valide.
        # Se il database di Render è vecchio, ricrea/aggiorna comunque l'utente.
        demo_resolver = (
            email == "pro@fixtude.it"
            and password == "1234"
        )

        if demo_resolver:
            try:
                conn = db_connect()
                existing = conn.execute(
                    "SELECT id FROM users WHERE email = ?",
                    ("pro@fixtude.it",)
                ).fetchone()
                password_hash = generate_password_hash("1234")
                if existing:
                    conn.execute(
                        "UPDATE users SET password_hash = ?, role = 'resolver' WHERE email = ?",
                        (password_hash, "pro@fixtude.it")
                    )
                else:
                    conn.execute(
                        "INSERT INTO users (email, password_hash, role, created_at) VALUES (?, ?, 'resolver', ?)",
                        ("pro@fixtude.it", password_hash, now_iso())
                    )
                conn.commit()
                conn.close()
                user = find_user(email)
            except Exception:
                app.logger.exception("Impossibile sincronizzare l'account demo Risolutore.")

        valid = bool(
            demo_resolver
            or (
                user
                and user["role"] == "resolver"
                and check_password_hash(
                    user["password_hash"],
                    password
                )
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

    user = require_login("debtor")
    if not user:
        return redirect(url_for("debtor_login"))

    case = get_case_for_email(user["email"])
    case_data = get_case_data(case["id"]) if case else {}

    analysis_payment = None
    pdf_payment = None
    solutions = []
    latest_solution = None
    notifications = []
    unread_count = 0

    if case:
        analysis_payment = get_paid_payment(user["email"], case["id"], "analysis")
        pdf_payment = get_paid_payment(user["email"], case["id"], "pdf")
        solutions = get_solutions(case["id"])
        latest_solution = solutions[0] if solutions else None

        conn = db_connect()
        notifications = conn.execute(
            """SELECT * FROM notifications WHERE email = ? ORDER BY id DESC LIMIT 5""",
            (user["email"],)
        ).fetchall()
        unread_count = conn.execute(
            """SELECT COUNT(*) FROM notifications WHERE email = ? AND read = 0""",
            (user["email"],)
        ).fetchone()[0]
        conn.close()

    pdf_ready = bool(
        pdf_payment and latest_solution and latest_solution["status"] == "sent"
        and latest_solution["pdf_path"] and Path(latest_solution["pdf_path"]).exists()
    )

    pdf_in_review = bool(pdf_payment and not pdf_ready)

    return render_template_string(
        """
        <!doctype html>
        <html lang="it">
        <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Area Personale - FixTude</title>
        <style>
        *{box-sizing:border-box}
        body{font-family:Arial,sans-serif;background:#f5f7fb;color:#172033;margin:0}
        .wrap{max-width:1050px;margin:auto;padding:28px 20px 50px}
        .top{display:flex;justify-content:space-between;align-items:center;gap:20px;margin-bottom:24px}
        .top h1{margin:0 0 6px;font-size:30px}.muted{color:#667085}
        .actions{display:flex;gap:14px;align-items:center}.actions a{text-decoration:none}
        .back{color:#4f46e5}.logout{color:#b42318}
        .hero{background:white;border-radius:18px;padding:25px;margin-bottom:18px;box-shadow:0 2px 10px rgba(0,0,0,.04)}
        .grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}
        .card{background:white;border-radius:16px;padding:22px;box-shadow:0 2px 10px rgba(0,0,0,.04)}
        .card h2{margin-top:0;font-size:20px}.card p{line-height:1.5}
        .link{display:inline-block;margin-top:10px;color:#4f46e5;text-decoration:none;font-weight:600}
        .status{display:inline-block;padding:7px 11px;border-radius:999px;font-size:13px;font-weight:700;margin:8px 0}
        .green{background:#e8f7ed;color:#176b3a}.orange{background:#fff4d6;color:#8a5a00}.gray{background:#eef1f5;color:#596273}
        .row{display:flex;justify-content:space-between;gap:15px;border-top:1px solid #edf0f4;padding:12px 0}.row:first-of-type{border-top:0}
        .notice{background:#fff8e6;border:1px solid #f2df9a;padding:14px;border-radius:12px;margin-top:12px}
        .mini{font-size:14px;color:#667085}
        @media(max-width:700px){.grid{grid-template-columns:1fr}.top{align-items:flex-start;flex-direction:column}.actions{width:100%;justify-content:space-between}}
        </style>
        </head>
        <body>
        <div class="wrap">

        <div class="top">
          <div>
            <h1>Area Personale FixTude</h1>
            <div class="muted">{{ case_data.get('name','') }} {{ case_data.get('surname','') }} · {{ user.email }}</div>
          </div>
          <div class="actions">
            <a class="back" href="javascript:history.back()">← Indietro</a>
            <a class="logout" href="{{ url_for('logout') }}">Esci</a>
          </div>
        </div>

        {% if not case %}
        <div class="hero">
          <h2>Inizia da qui</h2>
          <p>Inserisci i tuoi dati per creare la tua pratica FixTude.</p>
          <a class="link" href="{{ url_for('debtor_situation') }}">Inserisci i tuoi dati →</a>
        </div>
        {% else %}

        <div class="hero">
          <h2>La mia situazione</h2>
          <p>La tua pratica è attiva. Da questa area puoi gestire dati, analisi, documenti e pagamenti.</p>
          <a class="link" href="{{ url_for('case_summary') }}">Visualizza riepilogo →</a>
          &nbsp;&nbsp;
          <a class="link" href="{{ url_for('debtor_situation') }}">Modifica dati →</a>
        </div>

        <div class="grid">

          <div class="card">
            <h2>La mia analisi</h2>
            {% if analysis_payment %}
              <span class="status green">PAGAMENTO EFFETTUATO</span>
              <p class="mini">Analisi FixTude · € 1,99</p>
              <a class="link" href="{{ url_for('case_analysis') }}">Apri l'analisi →</a>
            {% else %}
              <span class="status gray">NON ACQUISTATA</span>
              <p class="mini">Analisi FixTude · € 1,99</p>
              <a class="link" href="{{ url_for('payments') }}">Acquista analisi →</a>
            {% endif %}
          </div>

          <div class="card">
            <h2>Il mio documento</h2>
            {% if pdf_ready %}
              <span class="status green">DOCUMENTO DISPONIBILE</span>
              <p class="mini">Documento FixTude · € 9,99</p>
              <a class="link" href="{{ url_for('download_solution', solution_id=latest_solution.id) }}">Scarica il PDF →</a>
            {% elif pdf_in_review %}
              <span class="status orange">IN REVISIONE</span>
              <p class="mini">Pagamento ricevuto · € 9,99</p>
              <div class="notice">Il documento è stato pagato in anticipo ed è ora in revisione completa da parte dell'Esperto. Riceverai una email quando il PDF sarà approvato.</div>
            {% else %}
              <span class="status gray">NON ACQUISTATO</span>
              <p class="mini">Documento FixTude · € 9,99</p>
              <p>Puoi acquistare subito il documento. Il pagamento viene effettuato ora; il PDF definitivo sarà disponibile dopo la revisione e validazione dell'Esperto.</p>
              <form method="post" action="{{ url_for('create_checkout') }}" style="margin-top:14px">
                <input type="hidden" name="service" value="pdf">
                <button type="submit" style="padding:13px 22px;background:#4f46e5;color:white;border:0;border-radius:9px;font-weight:700;cursor:pointer">Paga € 9,99 con Stripe</button>
              </form>
              <p class="mini" style="margin-top:10px">Pagamento sicuro tramite Stripe. Dopo il pagamento la pratica passerà in revisione.</p>
            {% endif %}
          </div>

          <div class="card">
            <h2>Pagamenti e documenti fiscali</h2>
            <p>Consulta pagamenti, ricevute e documenti riepilogativi.</p>
            <a class="link" href="{{ url_for('payments') }}">Apri pagamenti →</a>
          </div>

          <div class="card">
            <h2>Documenti caricati</h2>
            <p>Gestisci i documenti che hai fornito a FixTude per l'analisi della tua situazione.</p>
            <a class="link" href="{{ url_for('debtor_situation') }}">Gestisci la situazione →</a>
          </div>

          <div class="card">
            <h2>Notifiche</h2>
            {% if unread_count %}
              <span class="status orange">{{ unread_count }} NUOVE</span>
            {% else %}
              <span class="status gray">NESSUNA NUOVA</span>
            {% endif %}
            {% for n in notifications[:3] %}
              <div class="row"><span>{{ n.title }}</span><span class="mini">{{ n.created_at }}</span></div>
            {% endfor %}
            <a class="link" href="{{ url_for('debtor_notifications') }}">Vedi notifiche →</a>
          </div>

          <div class="card">
            <h2>Profilo e sicurezza</h2>
            <p>Email: <strong>{{ user.email }}</strong></p>
            <a class="link" href="{{ url_for('password_forgot') }}">Recupera / modifica password →</a>
          </div>

        </div>
        {% endif %}
        </div>
        </body>
        </html>
        """,
        user=user,
        case=case,
        case_data=case_data,
        analysis_payment=analysis_payment,
        pdf_payment=pdf_payment,
        latest_solution=latest_solution,
        pdf_ready=pdf_ready,
        pdf_in_review=pdf_in_review,
        notifications=notifications,
        unread_count=unread_count
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

    analysis_payment = get_paid_payment(user["email"], case["id"], "analysis")
    pdf_payment = get_paid_payment(user["email"], case["id"], "pdf")

    solutions = get_solutions(
        case["id"]
    )

    has_document = any(
        s["status"] == "sent"
        for s in solutions
    )
    latest_solution = solutions[0] if solutions else None

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
        button{padding:13px 20px;background:#4f46e5;color:white;border:0;border-radius:8px;font-weight:700;cursor:pointer}
        .paid{background:#e8f7ed;padding:12px;border-radius:8px}
        .review{background:#fff4d6;padding:12px;border-radius:8px}
        .small{color:#667085;font-size:14px}
        </style>
        </head>
        <body>

        <div class="wrap">

        <a href="{{ url_for('debtor_dashboard') }}" style="text-decoration:none;color:#4f46e5">
        ← Indietro
        </a>

        <h1>Pagamenti</h1>

        <div class="card">
        <h2>Dati per la fattura</h2>
        {% if fiscal_complete %}
        <div class="paid">✓ Dati fiscali presenti</div>
        <p class="small">Puoi modificarli prima di un nuovo acquisto.</p>
        {% else %}
        <div class="review">⚠ Completa i dati fiscali prima del pagamento.</div>
        {% endif %}
        <a href="{{ url_for('fiscal_data') }}">{{ 'Modifica dati fiscali' if fiscal_complete else 'Inserisci dati fiscali →' }}</a>
        </div>

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
        <p><a href="{{ url_for('download_payment_document', payment_id=analysis_payment.id, document_type='receipt') }}">Scarica ricevuta</a> · <a href="{{ url_for('download_payment_document', payment_id=analysis_payment.id, document_type='fiscal') }}">Scarica documento riepilogativo</a></p>
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
        la validazione dell'Esperto.
        </p>

        <div class="price">€ 9,99</div>

        {% if pdf_paid %}

        <div class="paid">
        ✓ Pagamento ricevuto: € 9,99
        </div>
        <p><a href="{{ url_for('download_payment_document', payment_id=pdf_payment.id, document_type='receipt') }}">Scarica ricevuta</a> · <a href="{{ url_for('download_payment_document', payment_id=pdf_payment.id, document_type='fiscal') }}">Scarica documento riepilogativo</a></p>

        {% if has_document %}
        <p><strong>✓ Documento definitivo disponibile.</strong></p>
        <p><a href="{{ url_for('download_solution', solution_id=latest_solution.id) }}">Scarica PDF definitivo →</a></p>
        {% else %}
        <p><strong>🕐 Documento in revisione.</strong></p>
        <p>Il pagamento è stato ricevuto. L'Esperto sta completando la revisione. Riceverai una email quando il PDF definitivo sarà disponibile.</p>
        {% endif %}

        {% else %}

        <p class="small">Paghi ora € 9,99. L'Esperto completerà la revisione dopo il pagamento e il PDF definitivo verrà reso disponibile nell'Area Personale e inviato via email.</p>

        <form method="post"
        action="{{ url_for('create_checkout') }}">

        <input type="hidden"
        name="service"
        value="pdf">

        <button type="submit">
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
        fiscal_complete=fiscal_data_complete(user["email"]),
        has_document=has_document,
        latest_solution=latest_solution,
        analysis_payment=analysis_payment,
        pdf_payment=pdf_payment
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

    # Prima del pagamento raccogliamo i dati necessari alla successiva predisposizione
    # della fattura elettronica. La trasmissione allo SdI resta separata e manuale
    # tramite Agenzia delle Entrate in questa fase di lancio.
    if not fiscal_data_complete(user["email"]):
        return redirect(url_for("fiscal_data", next="pagamenti"))

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
                "user_email": user["email"],
                "fiscal_code": get_fiscal_data(user["email"]).get("fiscal_code", ""),
                "vat_number": get_fiscal_data(user["email"]).get("vat_number", "")
            },
            success_url=(
                url_for("payment_success", _external=True)
                + "?session_id={CHECKOUT_SESSION_ID}"
            ),
            cancel_url=url_for(
                "payments",
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
# PAYMENT DOCUMENT DOWNLOAD
# ============================================================

@app.route("/pagamenti/documento/<int:payment_id>/<document_type>")
def download_payment_document(payment_id, document_type):
    user = require_login("debtor")
    if not user or document_type not in ("receipt", "fiscal"):
        abort(403)
    payment = get_payment(payment_id)
    if not payment or payment["user_email"] != user["email"] or payment["status"] != "paid":
        abort(403)
    path = generate_payment_pdf(payment_id, document_type)
    return send_file(path, as_attachment=True, download_name=Path(path).name)


# ============================================================
# PAYMENT SUCCESS
# ============================================================

@app.route(
    "/pagamenti/success"
)
def payment_success():

    user = require_login("debtor")
    if not user:
        return redirect(url_for("debtor_login"))

    session_id = request.args.get("session_id", "").strip()
    payment = None
    if session_id:
        conn = db_connect()
        payment = conn.execute(
            "SELECT * FROM payments WHERE stripe_session_id = ? AND user_email = ?",
            (session_id, user["email"])
        ).fetchone()
        conn.close()

    status = payment["status"] if payment else "pending"
    if payment and status == "paid":
        # I documenti vengono preparati dal webhook. Qui non si richiama Stripe
        # e non si avvia una seconda elaborazione del pagamento.
        pass

    return render_template_string(
        """
        <!doctype html><html lang="it"><head>
        <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Pagamento FixTude</title>
        <style>body{font-family:Arial;background:#f6f8fb}.box{max-width:650px;margin:80px auto;background:white;padding:35px;border-radius:18px;text-align:center}.ok{background:#e8f7ed;padding:15px;border-radius:10px}.wait{background:#fff7df;padding:15px;border-radius:10px}a{display:inline-block;margin-top:20px}</style>
        </head><body><div class="box">
        {% if status == "paid" %}
        <h1>Pagamento completato</h1><div class="ok">Il pagamento è stato registrato. Ricevuta e documento riepilogativo sono disponibili nell'Area Privata.</div>
        {% else %}
        <h1>Pagamento in verifica</h1><div class="wait">Il pagamento è stato ricevuto da Stripe e verrà confermato automaticamente. Non effettuare un secondo pagamento.</div>
        {% endif %}
        <a href="{{ url_for('payments') }}">Torna ai pagamenti</a>
        </div></body></html>
        """, status=status
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

# ============================================================
# STAFF / ESPERTO DASHBOARD
# ============================================================

def get_case_payment_state(case_id, email):
    return {"analysis": get_paid_payment(email, case_id, "analysis"), "kit": get_paid_payment(email, case_id, "pdf")}

def get_library_files(active_only=True):
    conn=db_connect(); sql="SELECT * FROM kit_library" + (" WHERE active=1" if active_only else "") + " ORDER BY category,title"; rows=conn.execute(sql).fetchall(); conn.close(); return rows

def get_solution_attachments(solution_id):
    conn=db_connect(); rows=conn.execute("SELECT kl.* FROM solution_attachments sa JOIN kit_library kl ON kl.id=sa.library_id WHERE sa.solution_id=? ORDER BY kl.category,kl.title",(solution_id,)).fetchall(); conn.close(); return rows

def save_solution_attachments(solution_id, library_ids):
    conn=db_connect(); conn.execute("DELETE FROM solution_attachments WHERE solution_id=?",(solution_id,))
    for raw in library_ids:
        try: lid=int(raw)
        except (TypeError,ValueError): continue
        conn.execute("INSERT OR IGNORE INTO solution_attachments(solution_id,library_id,created_at) VALUES(?,?,?)",(solution_id,lid,now_iso()))
    conn.commit(); conn.close()

def get_paid_service_label(case_id,email):
    state=get_case_payment_state(case_id,email)
    if state["kit"]: return "KIT FIXTUDE — €9,99","kit"
    if state["analysis"]: return "ANALISI FIXTUDE — €1,99","analysis"
    return "NESSUN SERVIZIO PAGATO","none"

def build_paid_response_content(case_id):
    calc=calculate_case(case_id); data=get_case_data(case_id); analysis=latest_analysis(case_id)
    name=((data.get("name","")+" "+data.get("surname","")).strip() or "Cliente FixTude")
    scenarios=(analysis or {}).get("scenarios",[]) if analysis else []; warnings=calc.get("warnings",[])
    lines=[
      "RISPOSTA FIXTUDE — ANALISI DELLA SITUAZIONE","",f"Cliente: {name}",f"Data elaborazione: {datetime.now().strftime('%d/%m/%Y')}","",
      "1. OGGETTO E FINALITÀ","La presente risposta riepiloga i dati forniti dal cliente e restituisce una lettura organizzata della situazione economica e debitoria. L'obiettivo è aiutare il cliente a comprendere i principali elementi di pressione finanziaria e a individuare possibili percorsi da approfondire. Le valutazioni sono indicative e non costituiscono parere legale, finanziario o garanzia di accettazione da parte dei creditori.","",
      "2. QUADRO ECONOMICO RILEVATO",f"Entrate mensili dichiarate: € {calc['total_income']:.2f}",f"Spese mensili dichiarate: € {calc['total_expenses']:.2f}",f"Disponibilità teorica residua: € {calc['monthly_capacity']:.2f}",f"Debito complessivo dichiarato: € {calc['total_debt']:.2f}",f"Rate mensili dichiarate: € {calc['total_payments']:.2f}",f"Incidenza delle rate sulle entrate: {(calc['total_payments']/calc['total_income']*100 if calc['total_income'] else 0):.1f}%",f"Valutazione sintetica di sostenibilità: {calc['sustainability']}","",
      "3. LETTURA DELLA SITUAZIONE","I dati inseriti devono essere letti nel loro insieme. La differenza tra entrate e spese rappresenta una disponibilità teorica e non equivale automaticamente alla somma che un creditore accetterà come rata. La sostenibilità di un eventuale piano dipende inoltre dalla regolarità dei redditi, dalle spese non ricorrenti, dal numero e dalla natura delle posizioni e dalle condizioni richieste dalle singole controparti.","",
      "4. ELEMENTI DI ATTENZIONE"]
    lines += [f"• {w}" for w in warnings] if warnings else ["Non sono emersi avvisi automatici particolari sulla base dei dati inseriti."]
    lines += ["","5. POSSIBILI PERCORSI DA APPROFONDIRE"]
    if scenarios:
        for sc in scenarios:
            lines.append(f"• {sc.get('title','Scenario')} — {sc.get('description','')}")
            if sc.get("estimated_monthly"): lines.append(f"  Rata simulata: € {sc['estimated_monthly']:.2f}; durata indicativa: {sc.get('months','n.d.')} mesi.")
            if sc.get("estimated_amount"): lines.append(f"  Importo transattivo simulato: € {sc['estimated_amount']:.2f}.")
    else: lines.append("È opportuno completare l'analisi prima di formulare una simulazione economica.")
    lines += ["","6. INDICAZIONI OPERATIVE","Prima di assumere impegni è consigliabile verificare ogni posizione con la documentazione disponibile: contratto, estratto della posizione, comunicazioni ricevute, importi richiesti e scadenze. Eventuali richieste di rateizzazione, rinegoziazione o definizione transattiva devono essere rivolte al creditore competente e diventano efficaci solo se accettate dalla controparte secondo le modalità previste.","","7. CONCLUSIONE","Sulla base dei dati forniti, FixTude ha organizzato la situazione e individuato le principali aree sulle quali concentrare l'attenzione. Le simulazioni contenute nella presente risposta servono esclusivamente come supporto informativo. Se la situazione presenta elementi complessi, contestazioni, procedure giudiziarie o importi rilevanti, è opportuno sottoporre la documentazione a un professionista qualificato prima di assumere decisioni.","","NOTA FIXTUDE","Questo documento è stato preparato sulla base delle informazioni inserite nel servizio. FixTude non garantisce l'esito di richieste, trattative o procedure e non sostituisce un professionista abilitato."]
    return "\n\n".join(lines)

def generate_staff_response_pdf(solution_id):
    conn=db_connect(); solution=conn.execute("SELECT * FROM solution_documents WHERE id=?",(solution_id,)).fetchone(); conn.close()
    if not solution: raise FileNotFoundError("Documento non trovato.")
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, PageBreak
    from reportlab.lib.units import mm
    path=PDF_DIR/f"fixtude_risposta_{solution_id}.pdf"; styles=getSampleStyleSheet()
    body=ParagraphStyle("FixBody",parent=styles["BodyText"],fontName="Helvetica",fontSize=10.5,leading=15,spaceAfter=7)
    h=ParagraphStyle("FixH",parent=styles["Heading2"],fontName="Helvetica-Bold",fontSize=15,leading=19,spaceBefore=5,spaceAfter=10)
    title=ParagraphStyle("FixTitle",parent=styles["Title"],fontName="Helvetica-Bold",fontSize=22,leading=26,alignment=1,spaceAfter=15)
    story=[Paragraph("FixTude",title),Paragraph(_safe_pdf_text(solution["title"]),h),Spacer(1,5*mm)]
    paragraphs=[x.strip() for x in solution["content"].split("\n\n") if x.strip()]
    # Tre pagine minime: separazione strutturale, senza ripetere artificialmente il contenuto.
    cut1=max(1,len(paragraphs)//3); cut2=max(cut1+1,(len(paragraphs)*2)//3)
    for idx,block in enumerate(paragraphs):
        if idx==cut1 or idx==cut2: story.append(PageBreak())
        safe=_safe_pdf_text(block).replace("\n","<br/>")
        story.append(Paragraph(safe,h if re.match(r"^[0-9]+\.",block) or block in ("RISPOSTA FIXTUDE — ANALISI DELLA SITUAZIONE","NOTA FIXTUDE") else body))
    story.append(Spacer(1,8*mm)); story.append(Paragraph("Documento informativo FixTude. Non costituisce parere legale o finanziario.",body))
    SimpleDocTemplate(str(path),pagesize=A4,rightMargin=19*mm,leftMargin=19*mm,topMargin=18*mm,bottomMargin=18*mm).build(story)
    return str(path)

@app.route("/risolutore")
def resolver_dashboard():
    user=require_login("resolver")
    if not user: return redirect(url_for("resolver_login"))
    conn=db_connect(); cases=conn.execute("SELECT * FROM cases ORDER BY id DESC").fetchall(); conn.close()
    rows=[]
    for case in cases:
        label,service=get_paid_service_label(case["id"],case["email"]); rows.append({"case":case,"data":get_case_data(case["id"]),"service_label":label,"service":service,"solutions":get_solutions(case["id"])})
    return render_template_string('''<!doctype html><html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Area Riservata FixTude</title><style>*{box-sizing:border-box}body{margin:0;background:#f4f6fa;color:#18212f;font-family:Arial}.wrap{max-width:1180px;margin:auto;padding:22px}.top{display:flex;justify-content:space-between;align-items:center}.brand{font-size:27px;font-weight:800}.muted{color:#687385}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:16px 0}.stat,.card{background:#fff;border:1px solid #e2e7ef;border-radius:15px;padding:18px;box-shadow:0 4px 16px rgba(20,30,50,.04)}.stat b{font-size:25px;display:block;margin-top:5px}.tabs{display:flex;gap:8px;margin:15px 0}.tab{background:#eef1f7;border-radius:9px;padding:8px 12px;font-weight:700}.case{display:grid;grid-template-columns:1fr auto;gap:15px;align-items:center}.badge{display:inline-block;padding:7px 10px;border-radius:999px;font-size:12px;font-weight:800;background:#eef0f6}.paid{background:#e8f8f0;color:#166b46}.pending{background:#fff4d9;color:#745500}.btn{display:inline-block;border:0;border-radius:9px;padding:10px 14px;text-decoration:none;font-weight:700;background:#4f46e5;color:white}.library-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}@media(max-width:800px){.grid{grid-template-columns:1fr 1fr}.library-grid{grid-template-columns:1fr}.case{grid-template-columns:1fr}}@media(max-width:500px){.grid{grid-template-columns:1fr}}</style></head><body><div class="wrap"><div class="top"><div><div class="brand">Area Riservata FixTude</div><div class="muted">Pratiche, risposte, PDF e KIT da validare</div></div><a href="{{url_for('logout')}}">Esci</a></div><div class="grid"><div class="stat">Pratiche<b>{{rows|length}}</b></div><div class="stat">Analisi €1,99<b>{{rows|selectattr('service','equalto','analysis')|list|length}}</b></div><div class="stat">Kit €9,99<b>{{rows|selectattr('service','equalto','kit')|list|length}}</b></div><div class="stat">Libreria PDF<b>{{library|length}}</b></div></div><div class="tabs"><span class="tab">📂 Tutte le pratiche</span><a class="tab" href="{{url_for('kit_library')}}">📚 Libreria PDF</a></div>{% for row in rows %}<div class="card" style="margin-bottom:12px"><div class="case"><div><strong>Pratica #{{row.case.id}}</strong> · {{row.data.get('name','Cliente')}} {{row.data.get('surname','')}}</div><div><div class="muted">{{row.case.email}}</div><span class="badge {{'paid' if row.service!='none' else 'pending'}}">{{row.service_label}}</span> <span class="badge">{{row.solutions|length}} documenti</span></div><a class="btn" href="{{url_for('resolver_case',case_id=row.case.id)}}">Apri pratica →</a></div></div>{% else %}<div class="card">Nessuna pratica.</div>{% endfor %}<div id="libreria" class="card"><h2>Libreria PDF FixTude</h2><p class="muted">Carica i PDF che lo Staff potrà selezionare, validare e inviare ai clienti del KIT.</p><form method="post" action="{{url_for('kit_library_upload')}}" enctype="multipart/form-data"><input name="title" required placeholder="Titolo" style="padding:10px;width:28%"><input name="category" placeholder="Categoria" style="padding:10px;width:18%"><input name="file" required type="file" accept="application/pdf"><button class="btn">Carica PDF</button></form><div class="library-grid" style="margin-top:14px">{% for f in library %}<div class="stat"><strong>{{f.title}}</strong><div class="muted">{{f.category}} · {{'VALIDATO' if f.validated else 'DA VALIDARE'}}</div><div>{{f.original_filename}}</div>{% if not f.validated %}<form method="post" action="{{url_for('kit_library_validate',library_id=f.id)}}"><button class="btn" style="margin-top:8px">Valida PDF</button></form>{% endif %}<a href="{{url_for('kit_library_download',library_id=f.id)}}">Scarica / verifica</a></div>{% endfor %}</div></div></div></body></html>''',rows=rows,library=get_library_files())

@app.route("/risolutore/libreria")
def kit_library():
    user=require_login("resolver")
    if not user: return redirect(url_for("resolver_login"))
    library=get_library_files()
    return render_template_string('''<!doctype html><html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Libreria PDF — FixTude</title><style>*{box-sizing:border-box}body{margin:0;background:#f4f6fa;color:#18212f;font-family:Arial}.wrap{max-width:1180px;margin:auto;padding:22px}.top{display:flex;justify-content:space-between;align-items:center}.card{background:#fff;border:1px solid #e2e7ef;border-radius:15px;padding:20px;margin:14px 0;box-shadow:0 4px 16px rgba(20,30,50,.04)}.btn{display:inline-block;border:0;border-radius:9px;padding:10px 14px;text-decoration:none;font-weight:700;background:#4f46e5;color:white;cursor:pointer}.muted{color:#687385}.library-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.item{border:1px solid #e2e7ef;border-radius:12px;padding:15px;background:#fafbfd}.ok{color:#166b46;font-weight:800}.wait{color:#745500;font-weight:800}input{padding:10px;border:1px solid #d9e0ea;border-radius:8px}@media(max-width:800px){.library-grid{grid-template-columns:1fr}.upload{display:grid;gap:10px}}</style></head><body><div class="wrap"><div class="top"><div><h1 style="margin-bottom:4px">📚 Libreria PDF FixTude</h1><div class="muted">Gestisci, verifica e valida i documenti che potranno essere inseriti nei KIT.</div></div><a href="{{url_for('resolver_dashboard')}}">← Dashboard Staff</a></div><div class="card"><h2>Carica nuovo PDF</h2><p class="muted">Il documento viene caricato come <b>DA VALIDARE</b>. Dopo la verifica potrà essere selezionato nei KIT.</p><form class="upload" method="post" action="{{url_for('kit_library_upload')}}" enctype="multipart/form-data"><input name="title" required placeholder="Titolo del documento"><input name="category" placeholder="Categoria"><input name="file" required type="file" accept="application/pdf"><button class="btn">Carica PDF</button></form></div><div class="card"><h2>Documenti in libreria ({{library|length}})</h2><div class="library-grid">{% for f in library %}<div class="item"><h3>{{f.title}}</h3><div class="muted">{{f.category}}</div><p>{{f.original_filename}}</p><div class="{{'ok' if f.validated else 'wait'}}">{{'✓ VALIDATO' if f.validated else '⚠ DA VALIDARE'}}</div>{% if not f.validated %}<form method="post" action="{{url_for('kit_library_validate',library_id=f.id)}}"><button class="btn" style="margin-top:10px">Valida PDF</button></form>{% endif %}<a style="display:block;margin-top:10px" href="{{url_for('kit_library_download',library_id=f.id)}}">Scarica / verifica PDF</a></div>{% else %}<p class="muted">Nessun PDF presente nella libreria.</p>{% endfor %}</div></div></div></body></html>''',library=library)

@app.route("/risolutore/libreria/upload",methods=["POST"])
def kit_library_upload():
    user=require_login("resolver")
    if not user: return redirect(url_for("resolver_login"))
    uploaded=request.files.get("file"); title=request.form.get("title","").strip(); category=request.form.get("category","Generale").strip() or "Generale"
    if not uploaded or not uploaded.filename.lower().endswith(".pdf") or not title: return redirect(url_for("resolver_dashboard"))
    filename=secure_filename(uploaded.filename); path=KIT_LIBRARY_DIR/f"{uuid.uuid4().hex}_{filename}"; uploaded.save(path)
    conn=db_connect(); conn.execute("INSERT INTO kit_library(title,category,description,file_path,original_filename,validated,active,created_at,updated_at) VALUES(?,?,?,?,?,0,1,?,?)",(title,category,"",str(path),filename,now_iso(),now_iso())); conn.commit(); conn.close()
    return redirect(url_for("resolver_dashboard")+"#libreria")

@app.route("/risolutore/libreria/<int:library_id>/valida",methods=["POST"])
def kit_library_validate(library_id):
    user=require_login("resolver")
    if not user: return redirect(url_for("resolver_login"))
    conn=db_connect(); conn.execute("UPDATE kit_library SET validated=1,updated_at=? WHERE id=?",(now_iso(),library_id)); conn.commit(); conn.close(); return redirect(url_for("resolver_dashboard")+"#libreria")

@app.route("/risolutore/libreria/<int:library_id>/download")
def kit_library_download(library_id):
    user=require_login("resolver")
    if not user: return redirect(url_for("resolver_login"))
    conn=db_connect(); row=conn.execute("SELECT * FROM kit_library WHERE id=?",(library_id,)).fetchone(); conn.close()
    if not row or not Path(row["file_path"]).exists(): abort(404)
    return send_file(row["file_path"],as_attachment=True,download_name=row["original_filename"])

@app.route("/risolutore/pratica/<int:case_id>")
def resolver_case(case_id):
    user=require_login("resolver")
    if not user: return redirect(url_for("resolver_login"))
    case=get_case(case_id)
    if not case: abort(404)
    calc=calculate_case(case_id); analysis=latest_analysis(case_id); solutions=get_solutions(case_id); service_label,service=get_paid_service_label(case_id,case["email"]); library=get_library_files()
    response_solution=next((x for x in solutions if x["solution_type"] in ("paid_response","kit_report")),None); response_attachments=get_solution_attachments(response_solution["id"]) if response_solution else []
    payment_ratio=round(calc["total_payments"]/calc["total_income"]*100,1) if calc["total_income"] else 0; situation_label="DA APPROFONDIRE" if calc["monthly_capacity"]<=0 else ("SOTTO PRESSIONE" if calc["total_payments"]>calc["monthly_capacity"] else "DA VALUTARE")
    return render_template_string('''<!doctype html><html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Pratica #{{case.id}} — Staff FixTude</title><style>*{box-sizing:border-box}body{margin:0;background:#f5f7fb;color:#18212f;font-family:Arial}.wrap{max-width:1180px;margin:auto;padding:20px}.top{display:flex;justify-content:space-between}.hero{background:#18212f;color:#fff;border-radius:18px;padding:22px;margin:15px 0}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.card{background:#fff;border:1px solid #e3e8ef;border-radius:15px;padding:20px;margin:12px 0}.metric{font-size:23px;font-weight:800}.muted{color:#6b7482}.badge{display:inline-block;padding:7px 10px;border-radius:999px;background:#e8f8f0;color:#176945;font-size:12px;font-weight:800}.warning{background:#fff6df;border:1px solid #eed69a;padding:12px;border-radius:10px;margin:8px 0}.lib{display:grid;grid-template-columns:1fr 1fr;gap:8px}.lib label{display:block;padding:10px;border:1px solid #dfe5ed;border-radius:9px;background:#fafbfd}.btn{display:inline-block;border:0;border-radius:9px;padding:10px 14px;font-weight:700;text-decoration:none;cursor:pointer;background:#4f46e5;color:#fff}.sec{background:#edf0f6;color:#253047}textarea{width:100%;min-height:330px;padding:13px;border:1px solid #d9e0ea;border-radius:10px;font-family:Arial;line-height:1.5}@media(max-width:800px){.grid{grid-template-columns:1fr 1fr}.lib{grid-template-columns:1fr}}@media(max-width:500px){.grid{grid-template-columns:1fr}}</style></head><body><div class="wrap"><div class="top"><a href="{{url_for('resolver_dashboard')}}">← Dashboard Staff</a><a href="{{url_for('logout')}}">Esci</a></div><div class="hero"><div style="color:#b9c2d0">PRATICA #{{case.id}} · {{service_label}}</div><h1>{{calc.data.get('name','Cliente')}} {{calc.data.get('surname','')}}</h1><div>{{case.email}}</div><p><span class="badge">{{situation_label}}</span></p></div><div class="grid"><div class="card"><div class="muted">Entrate</div><div class="metric">€ {{'%.2f'|format(calc.total_income)}}</div></div><div class="card"><div class="muted">Spese</div><div class="metric">€ {{'%.2f'|format(calc.total_expenses)}}</div></div><div class="card"><div class="muted">Debito</div><div class="metric">€ {{'%.2f'|format(calc.total_debt)}}</div></div><div class="card"><div class="muted">Rate / reddito</div><div class="metric">{{payment_ratio}}%</div></div></div><div class="card"><h2>Dati cliente e posizioni</h2><p><b>Email:</b> {{case.email}}</p><p><b>Telefono:</b> {{calc.data.get('phone','—')}}</p><p><b>Indirizzo:</b> {{calc.data.get('address','—')}}</p>{% for d in calc.debts %}<div style="padding:10px;border-top:1px solid #edf0f4"><b>{{d.creditor}}</b> · {{d.debt_type or 'Posizione'}} · € {{'%.2f'|format(d.current_amount)}} · rata € {{'%.2f'|format(d.monthly_payment)}}</div>{% endfor %}</div><div class="card"><h2>Analisi e generatore di risposte</h2><form method="post" action="{{url_for('resolver_run_analysis',case_id=case.id)}}"><button class="btn">Genera / rigenera analisi</button></form>{% if analysis %}<div style="margin-top:15px;white-space:pre-line;line-height:1.55">{{analysis.summary}}</div>{% for w in calc.warnings %}<div class="warning">⚠ {{w}}</div>{% endfor %}{% endif %}</div>{% if service=='analysis' %}<div class="card"><h2>Risposta cliente — €1,99</h2><p class="muted">La risposta deve essere articolata e il PDF deve avere almeno 3 pagine. Il testo è modificabile prima dell'invio.</p>{% if response_solution %}<form method="post" action="{{url_for('save_paid_response',solution_id=response_solution.id)}}"><textarea name="content">{{response_solution.content}}</textarea><p><button class="btn sec">Salva e rigenera PDF</button> <a class="btn" href="{{url_for('download_solution',solution_id=response_solution.id)}}">Anteprima PDF</a></p></form><form method="post" action="{{url_for('send_paid_response',solution_id=response_solution.id)}}"><button class="btn">✓ Valida e invia al cliente da info@fixtude.it</button></form>{% else %}<form method="post" action="{{url_for('generate_paid_response',case_id=case.id)}}"><button class="btn">Genera risposta articolata + PDF</button></form>{% endif %}</div>{% endif %}{% if service=='kit' %}<div class="card"><h2>Kit FixTude — €9,99</h2><p class="muted">Seleziona i PDF della libreria, verifica il contenuto e inviali insieme al rapporto personalizzato.</p><form method="post" action="{{url_for('prepare_kit',case_id=case.id)}}"><div class="lib">{% for f in library %}<label><input type="checkbox" name="library_ids" value="{{f.id}}" {% if f.id in response_attachments|map(attribute='id')|list %}checked{% endif %}> <b>{{f.title}}</b><br><span class="muted">{{f.category}} · {{'VALIDATO' if f.validated else 'DA VALIDARE'}}</span></label>{% endfor %}</div><p><button class="btn">Salva selezione KIT</button></p></form>{% if response_solution %}<a class="btn" href="{{url_for('download_solution',solution_id=response_solution.id)}}">Scarica rapporto personalizzato</a>{% endif %}<form method="post" action="{{url_for('send_kit',case_id=case.id)}}" style="margin-top:10px"><button class="btn">✓ Valida e invia KIT al cliente da info@fixtude.it</button></form></div>{% endif %}<div class="card"><h2>Documenti</h2>{% for s in solutions %}<div style="border-top:1px solid #edf0f4;padding:12px 0"><b>{{s.title}}</b> · {{s.status}} {% if s.final_email_sent_at %}· Email inviata{% endif %}{% if s.pdf_path %} · <a href="{{url_for('download_solution',solution_id=s.id)}}">PDF</a>{% endif %}</div>{% else %}<p class="muted">Nessun documento.</p>{% endfor %}</div></div></body></html>''',case=case,calc=calc,analysis=analysis,solutions=solutions,service=service,service_label=service_label,library=library,response_solution=response_solution,response_attachments=response_attachments,payment_ratio=payment_ratio,situation_label=situation_label)

@app.route("/risolutore/pratica/<int:case_id>/genera-risposta",methods=["POST"])
def generate_paid_response(case_id):
    user=require_login("resolver")
    if not user: return redirect(url_for("resolver_login"))
    case=get_case(case_id)
    if not case: abort(404)
    if not get_paid_payment(case["email"],case_id,"analysis") and not get_paid_payment(case["email"],case_id,"pdf"): abort(403)
    content=build_paid_response_content(case_id); conn=db_connect(); existing=conn.execute("SELECT * FROM solution_documents WHERE case_id=? AND solution_type='paid_response' ORDER BY id DESC LIMIT 1",(case_id,)).fetchone(); ts=now_iso()
    if existing: conn.execute("UPDATE solution_documents SET title=?,content=?,status='pending_review',updated_at=?,approved_at=NULL,sent_at=NULL WHERE id=?",("Risposta FixTude",content,ts,existing["id"])); sid=existing["id"]
    else: sid=conn.execute("INSERT INTO solution_documents(case_id,title,solution_type,content,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",(case_id,"Risposta FixTude","paid_response",content,"pending_review",ts,ts)).lastrowid
    conn.commit(); conn.close(); path=generate_staff_response_pdf(sid); conn=db_connect(); conn.execute("UPDATE solution_documents SET pdf_path=?,updated_at=? WHERE id=?",(path,now_iso(),sid)); conn.commit(); conn.close(); return redirect(url_for("resolver_case",case_id=case_id))

@app.route("/risolutore/soluzione/<int:solution_id>/salva-risposta",methods=["POST"])
def save_paid_response(solution_id):
    user=require_login("resolver")
    if not user: return redirect(url_for("resolver_login"))
    content=request.form.get("content","").strip(); conn=db_connect(); sol=conn.execute("SELECT * FROM solution_documents WHERE id=?",(solution_id,)).fetchone()
    if not sol: conn.close(); abort(404)
    conn.execute("UPDATE solution_documents SET content=?,status='pending_review',approved_at=NULL,sent_at=NULL,updated_at=? WHERE id=?",(content,now_iso(),solution_id)); case_id=sol["case_id"]; conn.commit(); conn.close(); path=generate_staff_response_pdf(solution_id); conn=db_connect(); conn.execute("UPDATE solution_documents SET pdf_path=?,updated_at=? WHERE id=?",(path,now_iso(),solution_id)); conn.commit(); conn.close(); return redirect(url_for("resolver_case",case_id=case_id))

@app.route("/risolutore/pratica/<int:case_id>/kit",methods=["POST"])
def prepare_kit(case_id):
    user=require_login("resolver")
    if not user: return redirect(url_for("resolver_login"))
    case=get_case(case_id)
    if not case: abort(404)
    ids=request.form.getlist("library_ids"); solutions=get_solutions(case_id); solution=next((x for x in solutions if x["solution_type"] in ("paid_response","kit_report")),None)
    if not solution:
        content=build_paid_response_content(case_id); conn=db_connect(); ts=now_iso(); sid=conn.execute("INSERT INTO solution_documents(case_id,title,solution_type,content,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",(case_id,"Rapporto Kit FixTude","kit_report",content,"pending_review",ts,ts)).lastrowid; conn.commit(); conn.close(); path=generate_staff_response_pdf(sid); conn=db_connect(); conn.execute("UPDATE solution_documents SET pdf_path=? WHERE id=?",(path,sid)); conn.commit(); conn.close(); solution=get_solutions(case_id)[-1]
    save_solution_attachments(solution["id"],ids); return redirect(url_for("resolver_case",case_id=case_id))

def _smtp_sender():
    host=os.environ.get("SMTP_HOST","").strip(); port=int(os.environ.get("SMTP_PORT","587") or 587); username=os.environ.get("SMTP_USERNAME","").strip(); password=os.environ.get("SMTP_PASSWORD","").strip(); sender="info@fixtude.it"; sender_name=os.environ.get("SMTP_FROM_NAME","FixTude").strip(); return host,port,username,password,sender,sender_name

def _send_staff_email(to_email,subject,body,attachments):
    host,port,username,password,sender,sender_name=_smtp_sender()
    if not host: return False
    msg=EmailMessage(); msg["Subject"]=subject; msg["From"]=f"{sender_name} <info@fixtude.it>"; msg["To"]=to_email; msg.set_content(body)
    for path,filename in attachments:
        if path and Path(path).exists():
            with open(path,"rb") as f: msg.add_attachment(f.read(),maintype="application",subtype="pdf",filename=filename)
    with smtplib.SMTP(host,port,timeout=20) as smtp:
        smtp.starttls()
        if username and password: smtp.login(username,password)
        smtp.send_message(msg)
    return True

@app.route("/risolutore/soluzione/<int:solution_id>/invia",methods=["POST"])
def send_paid_response(solution_id):
    user=require_login("resolver")
    if not user: return redirect(url_for("resolver_login"))
    conn=db_connect(); sol=conn.execute("SELECT * FROM solution_documents WHERE id=?",(solution_id,)).fetchone(); conn.close()
    if not sol: abort(404)
    case=get_case(sol["case_id"])
    if not case: abort(404)
    path=generate_staff_response_pdf(solution_id); conn=db_connect(); conn.execute("UPDATE solution_documents SET pdf_path=?,status='sent',approved_at=?,sent_at=?,updated_at=? WHERE id=?",(path,now_iso(),now_iso(),now_iso(),solution_id)); conn.commit(); conn.close()
    data=get_case_data(sol["case_id"]); name=((data.get("name","")+" "+data.get("surname","")).strip() or "Cliente")
    ok=_send_staff_email(case["email"],"FixTude - la tua risposta è disponibile",f"Gentile {name},\n\nabbiamo completato la revisione della tua richiesta. In allegato trovi la risposta FixTude.\n\nCordiali saluti,\nFixTude",[(path,"Risposta_FixTude.pdf")])
    conn=db_connect(); conn.execute("UPDATE solution_documents SET final_email_sent_at=? WHERE id=?",(now_iso() if ok else None,solution_id)); conn.commit(); conn.close(); return redirect(url_for("resolver_case",case_id=sol["case_id"]))

@app.route("/risolutore/pratica/<int:case_id>/invia-kit",methods=["POST"])
def send_kit(case_id):
    user=require_login("resolver")
    if not user: return redirect(url_for("resolver_login"))
    case=get_case(case_id)
    if not case: abort(404)
    solutions=get_solutions(case_id); report=next((x for x in solutions if x["solution_type"] in ("paid_response","kit_report")),None); attachments=[]
    if report and report["pdf_path"] and Path(report["pdf_path"]).exists(): attachments.append((report["pdf_path"],"Rapporto_FixTude.pdf"))
    for f in get_solution_attachments(report["id"]) if report else []:
        if f["validated"] and Path(f["file_path"]).exists(): attachments.append((f["file_path"],f["original_filename"]))
    if not attachments: return redirect(url_for("resolver_case",case_id=case_id))
    data=get_case_data(case_id); name=((data.get("name","")+" "+data.get("surname","")).strip() or "Cliente")
    ok=_send_staff_email(case["email"],"FixTude - il tuo KIT è disponibile",f"Gentile {name},\n\nabbiamo completato la preparazione del tuo KIT FixTude. In allegato trovi il rapporto e i documenti selezionati e verificati.\n\nCordiali saluti,\nFixTude",attachments)
    if report:
        conn=db_connect(); conn.execute("UPDATE solution_documents SET status='sent',approved_at=?,sent_at=?,final_email_sent_at=?,updated_at=? WHERE id=?",(now_iso(),now_iso(),now_iso() if ok else None,now_iso(),report["id"])); conn.commit(); conn.close()
    return redirect(url_for("resolver_case",case_id=case_id))


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
# FINAL SOLUTION EMAIL
# ============================================================

def send_final_solution_email(solution_id):
    """Invia al cliente il PDF definitivo dopo l'approvazione dell'Esperto."""
    conn = db_connect()
    solution = conn.execute(
        "SELECT * FROM solution_documents WHERE id = ?",
        (solution_id,)
    ).fetchone()
    conn.close()

    if not solution or solution["status"] != "sent":
        return False

    if solution["final_email_sent_at"]:
        return True

    if not solution["pdf_path"] or not Path(solution["pdf_path"]).exists():
        return False

    case = get_case(solution["case_id"])
    if not case:
        return False

    host = os.environ.get("SMTP_HOST", "").strip()
    port = int(os.environ.get("SMTP_PORT", "587") or 587)
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "").strip()
    sender = os.environ.get("SMTP_FROM_EMAIL", "").strip() or username
    sender_name = os.environ.get("SMTP_FROM_NAME", "FixTude").strip()
    if not host or not sender:
        return False

    data = get_case_data(solution["case_id"])
    customer_name = (data.get("name", "") + " " + data.get("surname", "")).strip() or "Cliente FixTude"

    msg = EmailMessage()
    msg["Subject"] = "FixTude - il tuo documento è disponibile"
    msg["From"] = f"{sender_name} <{sender}>"
    msg["To"] = case["email"]
    msg.set_content(
        f"Gentile {customer_name},\n\n"
        "la revisione completa del tuo documento FixTude è stata conclusa dall'Esperto.\n\n"
        "Il documento PDF definitivo è ora disponibile nella tua Area Personale FixTude. "
        "Lo trovi anche in allegato a questa email.\n\n"
        "Cordiali saluti,\nFixTude"
    )
    with open(solution["pdf_path"], "rb") as f:
        msg.add_attachment(
            f.read(),
            maintype="application",
            subtype="pdf",
            filename=Path(solution["pdf_path"]).name
        )

    with smtplib.SMTP(host, port, timeout=15) as smtp:
        smtp.starttls()
        if username and password:
            smtp.login(username, password)
        smtp.send_message(msg)

    conn = db_connect()
    conn.execute(
        "UPDATE solution_documents SET final_email_sent_at = ? WHERE id = ?",
        (now_iso(), solution_id)
    )
    conn.commit()
    conn.close()
    return True


def _safe_send_final_solution_email(solution_id):
    try:
        send_final_solution_email(solution_id)
    except Exception:
        app.logger.exception("Errore invio PDF finale per solution_id=%s", solution_id)


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
                    "L'Esperto ha validato "
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

    # Il PDF viene inviato solo dopo l'approvazione definitiva.
    threading.Thread(
        target=lambda: _safe_send_final_solution_email(solution_id),
        daemon=True
    ).start()

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
