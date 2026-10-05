from flask import (
    Flask, render_template, render_template_string, request, redirect,
    url_for, session, send_from_directory, abort, flash
)
from werkzeug.utils import secure_filename
import sqlite3
import os
import uuid
import json
from datetime import datetime
from html import escape

import stripe

# PDF
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.units import mm


# =========================================================
# FIXTUDE
# MVP AGENT + SUPERVISION WORKFLOW + STRIPE PAYMENTS
#
# Flow:
# Debtor -> case -> payment -> analysis -> scenarios -> PDFs
# -> Resolver review -> correction -> approval
# -> debtor -> payment -> PDF download
#
# Stripe:
# €1.99 = FixTude Analysis
# €9.99 = Approved PDF document
#
# Stripe keys are read ONLY from environment variables.
# =========================================================


app = Flask(__name__)

# In production put a strong SECRET_KEY in Render.
app.secret_key = os.environ.get(
    "SECRET_KEY",
    "fixtude-demo-secret-key"
)


DATABASE = "fixtude.db"
UPLOAD_FOLDER = "uploads"
PDF_FOLDER = "generated_pdfs"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(PDF_FOLDER, exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["PDF_FOLDER"] = PDF_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024


# =========================================================
# STRIPE CONFIGURATION
# =========================================================

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


# =========================================================
# PAYMENT SERVICES
# Amounts are in euro cents.
# =========================================================

PAYMENT_SERVICES = {
    "analysis": {
        "name": "Analisi FixTude",
        "description": (
            "Analisi della situazione economica e debitoria "
            "con individuazione di possibili scenari."
        ),
        "amount": 199,
        "currency": "eur"
    },
    "pdf": {
        "name": "Documento PDF FixTude",
        "description": (
            "Documento PDF definitivo relativo alla soluzione "
            "approvata dal Risolutore."
        ),
        "amount": 999,
        "currency": "eur"
    }
}


# =========================================================
# GENERAL CONFIGURATION
# =========================================================

ALLOWED_EXTENSIONS = {
    "pdf", "jpg", "jpeg", "png",
    "doc", "docx", "xls", "xlsx"
}


USERS = {
    "demo@fixtude.it": {
        "password": "1234",
        "role": "debtor",
        "name": "Demo Debitore"
    },
    "pro@fixtude.it": {
        "password": "1234",
        "role": "resolver",
        "name": "Demo Risolutore"
    }
}


# =========================================================
# DATABASE
# =========================================================

def get_db():
    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def init_database():
    db = get_db()

    db.execute("""
        CREATE TABLE IF NOT EXISTS cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            data TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS debts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER NOT NULL,
            creditor TEXT,
            debt_type TEXT,
            original_amount REAL DEFAULT 0,
            current_amount REAL DEFAULT 0,
            monthly_payment REAL DEFAULT 0,
            status TEXT,
            notes TEXT,
            FOREIGN KEY(case_id) REFERENCES cases(id)
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER NOT NULL,
            original_name TEXT,
            stored_name TEXT,
            uploaded_at TEXT,
            FOREIGN KEY(case_id) REFERENCES cases(id)
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS ai_analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft',
            analysis_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(case_id) REFERENCES cases(id)
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS solution_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER NOT NULL,
            analysis_id INTEGER,
            solution_type TEXT NOT NULL,
            title TEXT NOT NULL,
            draft_text TEXT NOT NULL,
            original_text TEXT NOT NULL,
            pdf_filename TEXT,
            status TEXT NOT NULL DEFAULT 'pending_review',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            approved_at TEXT,
            sent_at TEXT,
            FOREIGN KEY(case_id) REFERENCES cases(id),
            FOREIGN KEY(analysis_id) REFERENCES ai_analyses(id)
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS supervision (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            solution_id INTEGER NOT NULL,
            original_text TEXT,
            corrected_text TEXT,
            correction_note TEXT,
            supervisor_email TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(solution_id) REFERENCES solution_documents(id)
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            notification_type TEXT DEFAULT 'in_app',
            read INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY(case_id) REFERENCES cases(id)
        )
    """)

    # =====================================================
    # STRIPE PAYMENTS
    # =====================================================

    db.execute("""
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
            FOREIGN KEY(case_id) REFERENCES cases(id)
        )
    """)

    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_payments_user_case_service
        ON payments(user_email, case_id, service)
    """)

    db.execute("""
        CREATE INDEX IF NOT EXISTS idx_payments_stripe_session
        ON payments(stripe_session_id)
    """)

    db.commit()
    db.close()


init_database()


# =========================================================
# UTILITIES
# =========================================================

def now_iso():
    return datetime.utcnow().isoformat()


def allowed_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower()
        in ALLOWED_EXTENSIONS
    )


def number(value):
    try:
        if value is None:
            return 0.0

        value = str(value).replace(",", ".")

        return float(value)

    except (ValueError, TypeError):
        return 0.0


def euro(value):
    return f"€ {number(value):,.2f}"


def get_case(email):
    db = get_db()

    row = db.execute(
        "SELECT * FROM cases WHERE email = ?",
        (email,)
    ).fetchone()

    db.close()

    return row


def get_case_by_id(case_id):
    db = get_db()

    row = db.execute(
        "SELECT * FROM cases WHERE id = ?",
        (case_id,)
    ).fetchone()

    db.close()

    return row


def get_case_data(email):
    row = get_case(email)

    if not row:
        return {}

    try:
        return json.loads(row["data"])

    except (TypeError, json.JSONDecodeError):
        return {}


def save_case(email, data):
    db = get_db()

    timestamp = now_iso()

    serialized = json.dumps(
        data,
        ensure_ascii=False
    )

    existing = db.execute(
        "SELECT id FROM cases WHERE email = ?",
        (email,)
    ).fetchone()

    if existing:

        db.execute("""
            UPDATE cases
            SET data = ?, updated_at = ?
            WHERE email = ?
        """, (
            serialized,
            timestamp,
            email
        ))

    else:

        db.execute("""
            INSERT INTO cases
            (
                email,
                data,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?)
        """, (
            email,
            serialized,
            timestamp,
            timestamp
        ))

    db.commit()

    case = db.execute(
        "SELECT id FROM cases WHERE email = ?",
        (email,)
    ).fetchone()

    db.close()

    return case["id"]


def get_debts(case_id):
    db = get_db()

    rows = db.execute("""
        SELECT *
        FROM debts
        WHERE case_id = ?
        ORDER BY id
    """, (
        case_id,
    )).fetchall()

    db.close()

    return rows


def save_debts(case_id, debts):
    db = get_db()

    db.execute(
        "DELETE FROM debts WHERE case_id = ?",
        (case_id,)
    )

    for debt in debts:

        db.execute("""
            INSERT INTO debts
            (
                case_id,
                creditor,
                debt_type,
                original_amount,
                current_amount,
                monthly_payment,
                status,
                notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            case_id,
            debt.get("creditor", ""),
            debt.get("debt_type", ""),
            number(debt.get("original_amount")),
            number(debt.get("current_amount")),
            number(debt.get("monthly_payment")),
            debt.get("status", ""),
            debt.get("notes", "")
        ))

    db.commit()
    db.close()


def get_documents(case_id):
    db = get_db()

    rows = db.execute("""
        SELECT *
        FROM documents
        WHERE case_id = ?
        ORDER BY id
    """, (
        case_id,
    )).fetchall()

    db.close()

    return rows


def get_latest_analysis(case_id):
    db = get_db()

    row = db.execute("""
        SELECT *
        FROM ai_analyses
        WHERE case_id = ?
        ORDER BY id DESC
        LIMIT 1
    """, (
        case_id,
    )).fetchone()

    db.close()

    return row


def get_solution_documents(case_id):
    db = get_db()

    rows = db.execute("""
        SELECT *
        FROM solution_documents
        WHERE case_id = ?
        ORDER BY id
    """, (
        case_id,
    )).fetchall()

    db.close()

    return rows


def get_notifications(email):
    db = get_db()

    rows = db.execute("""
        SELECT *
        FROM notifications
        WHERE email = ?
        ORDER BY id DESC
    """, (
        email,
    )).fetchall()

    db.close()

    return rows


# =========================================================
# STRIPE PAYMENT UTILITIES
# =========================================================

def get_paid_payment(email, case_id, service):
    db = get_db()

    row = db.execute("""
        SELECT *
        FROM payments
        WHERE user_email = ?
          AND case_id = ?
          AND service = ?
          AND status = 'paid'
        ORDER BY id DESC
        LIMIT 1
    """, (
        email,
        case_id,
        service
    )).fetchone()

    db.close()

    return row


def get_payment_by_id(payment_id):
    db = get_db()

    row = db.execute("""
        SELECT *
        FROM payments
        WHERE id = ?
    """, (
        payment_id,
    )).fetchone()

    db.close()

    return row


def get_payment_by_session(session_id):
    db = get_db()

    row = db.execute("""
        SELECT *
        FROM payments
        WHERE stripe_session_id = ?
    """, (
        session_id,
    )).fetchone()

    db.close()

    return row


def create_pending_payment(
    email,
    case_id,
    service
):
    service_data = PAYMENT_SERVICES.get(service)

    if not service_data:
        return None

    db = get_db()

    existing = db.execute("""
        SELECT *
        FROM payments
        WHERE user_email = ?
          AND case_id = ?
          AND service = ?
          AND status = 'pending'
        ORDER BY id DESC
        LIMIT 1
    """, (
        email,
        case_id,
        service
    )).fetchone()

    if existing:
        db.close()
        return existing["id"]

    cursor = db.execute("""
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
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        email,
        case_id,
        service,
        service_data["amount"],
        service_data["currency"],
        "pending",
        now_iso()
    ))

    payment_id = cursor.lastrowid

    db.commit()
    db.close()

    return payment_id


def mark_payment_paid(
    payment_id,
    stripe_session_id=None,
    stripe_payment_intent_id=None
):
    db = get_db()

    payment = db.execute("""
        SELECT *
        FROM payments
        WHERE id = ?
    """, (
        payment_id,
    )).fetchone()

    if not payment:
        db.close()
        return False

    if payment["status"] == "paid":
        db.close()
        return True

    db.execute("""
        UPDATE payments
        SET
            status = 'paid',
            stripe_session_id = COALESCE(?, stripe_session_id),
            stripe_payment_intent_id = COALESCE(
                ?,
                stripe_payment_intent_id
            ),
            paid_at = ?
        WHERE id = ?
    """, (
        stripe_session_id,
        stripe_payment_intent_id,
        now_iso(),
        payment_id
    ))

    db.commit()
    db.close()

    return True


def mark_payment_cancelled(payment_id):
    db = get_db()

    db.execute("""
        UPDATE payments
        SET status = 'cancelled'
        WHERE id = ?
          AND status = 'pending'
    """, (
        payment_id,
    ))

    db.commit()
    db.close()


def format_payment_amount(cents):
    return f"€ {cents / 100:.2f}"


# =========================================================
# ANALYSIS ENGINE
# =========================================================

def calculate_case(case_id):
    case = get_case_by_id(case_id)

    if not case:
        return None

    try:
        data = json.loads(
            case["data"]
        )

    except (TypeError, json.JSONDecodeError):
        data = {}

    income = data.get(
        "income",
        {}
    )

    expenses = data.get(
        "expenses",
        {}
    )

    assets = data.get(
        "assets",
        {}
    )

    procedures = data.get(
        "procedures",
        {}
    )

    family = data.get(
        "family",
        {}
    )

    debts = get_debts(case_id)

    total_income = sum(
        number(v)
        for v in income.values()
    )

    total_expenses = sum(
        number(v)
        for v in expenses.values()
    )

    monthly_capacity = (
        total_income - total_expenses
    )

    total_debt = sum(
        number(debt["current_amount"])
        for debt in debts
    )

    total_payments = sum(
        number(debt["monthly_payment"])
        for debt in debts
    )

    payment_ratio = (
        total_payments /
        total_income *
        100
        if total_income > 0
        else 0
    )

    total_assets = sum(
        number(v)
        for v in assets.values()
    )

    warnings = []

    if total_income <= 0:
        warnings.append(
            "Non risultano entrate mensili valorizzate."
        )

    if monthly_capacity < 0:
        warnings.append(
            "Le spese indicate superano le entrate dichiarate."
        )

    if total_debt <= 0:
        warnings.append(
            "Non risultano importi debitori sufficientemente dettagliati."
        )

    if (
        total_payments > total_income
        and total_income > 0
    ):
        warnings.append(
            "Le rate indicate superano le entrate mensili dichiarate."
        )

    if (
        total_income > 0
        and payment_ratio >= 40
    ):
        warnings.append(
            "Il peso delle rate dichiarate è elevato rispetto alle entrate."
        )

    if not data.get("authorization"):
        warnings.append(
            "Il campo relativo all'autorizzazione non risulta valorizzato."
        )

    analysis = {
        "case_id": case_id,
        "generated_at": now_iso(),
        "engine": "FixTude Local Analysis Engine v1",
        "client": data.get(
            "personal",
            {}
        ),
        "family": family,
        "total_income": round(
            total_income,
            2
        ),
        "total_expenses": round(
            total_expenses,
            2
        ),
        "monthly_capacity": round(
            monthly_capacity,
            2
        ),
        "total_debt": round(
            total_debt,
            2
        ),
        "total_monthly_payments": round(
            total_payments,
            2
        ),
        "payment_ratio": round(
            payment_ratio,
            2
        ),
        "total_assets": round(
            total_assets,
            2
        ),
        "debt_count": len(
            debts
        ),
        "warnings": warnings,
        "procedures": procedures,
        "documents_count": len(
            get_documents(case_id)
        )
    }

    if monthly_capacity > 0:

        analysis["sustainability"] = (
            "I dati inseriti mostrano una disponibilità mensile "
            "positiva, da verificare rispetto alla sostenibilità "
            "delle rate e alle esigenze del nucleo familiare."
        )

    elif monthly_capacity == 0:

        analysis["sustainability"] = (
            "Le entrate risultano sostanzialmente assorbite "
            "dalle spese indicate."
        )

    else:

        analysis["sustainability"] = (
            "Le spese indicate risultano superiori alle entrate "
            "dichiarate. La situazione richiede un approfondimento."
        )

    return analysis


def build_scenarios(
    analysis,
    debts
):
    """
    Produces candidate scenarios.

    This is deliberately a deterministic MVP engine.
    It does NOT make legal determinations.
    """

    scenarios = []

    capacity = number(
        analysis["monthly_capacity"]
    )

    debt = number(
        analysis["total_debt"]
    )

    payments = number(
        analysis["total_monthly_payments"]
    )

    income = number(
        analysis["total_income"]
    )

    ratio = number(
        analysis["payment_ratio"]
    )

    client = analysis.get(
        "client",
        {}
    )

    client_name = (
        f'{client.get("first_name", "")} '
        f'{client.get("last_name", "")}'
    ).strip() or "Cliente"

    if debt > 0:

        if (
            capacity > 0
            and payments <= capacity
        ):

            scenarios.append({
                "type": "piano_rientro",
                "title": (
                    "Ipotesi di piano di rientro sostenibile"
                ),
                "reason": (
                    "I dati inseriti evidenziano una disponibilità "
                    "mensile positiva che può essere confrontata "
                    "con le rate e con l'esposizione complessiva."
                )
            })

        if (
            payments > 0
            and (
                capacity < payments
                or ratio >= 40
            )
        ):

            scenarios.append({
                "type": "rinegoziazione",
                "title": (
                    "Ipotesi di rinegoziazione degli impegni"
                ),
                "reason": (
                    "Il peso delle rate indicate appare significativo "
                    "rispetto alla capacità mensile dichiarata. "
                    "Può essere utile valutare una riduzione della rata "
                    "o una diversa distribuzione dei pagamenti."
                )
            })

        scenarios.append({
            "type": "transazione",
            "title": (
                "Ipotesi di proposta transattiva"
            ),
            "reason": (
                "L'esposizione complessiva può essere oggetto di "
                "un approfondimento finalizzato a valutare una "
                "possibile proposta ai creditori."
            )
        })

    if (
        capacity <= 0
        or ratio >= 50
        or not debts
    ):

        scenarios.append({
            "type": "approfondimento",
            "title": (
                "Necessità di approfondimento professionale"
            ),
            "reason": (
                "I dati disponibili evidenziano elementi che meritano "
                "una valutazione più approfondita prima di individuare "
                "la soluzione concretamente perseguibile."
            )
        })

    if not scenarios:

        scenarios.append({
            "type": "approfondimento",
            "title": (
                "Raccolta di ulteriori informazioni"
            ),
            "reason": (
                "I dati disponibili non sono ancora sufficienti "
                "per formulare ipotesi operative."
            )
        })

    scenarios = scenarios[:4]

    for scenario in scenarios:

        scenario["draft_text"] = build_solution_text(
            client_name,
            analysis,
            debts,
            scenario
        )

    return scenarios


def build_solution_text(
    client_name,
    analysis,
    debts,
    scenario
):
    capacity = euro(
        analysis["monthly_capacity"]
    )

    income = euro(
        analysis["total_income"]
    )

    expenses = euro(
        analysis["total_expenses"]
    )

    debt = euro(
        analysis["total_debt"]
    )

    payments = euro(
        analysis["total_monthly_payments"]
    )

    ratio = (
        f'{analysis["payment_ratio"]:.1f}%'
    )

    debt_lines = []

    for item in debts:

        creditor = (
            item["creditor"]
            or "Creditore non indicato"
        )

        amount = euro(
            item["current_amount"]
        )

        payment = euro(
            item["monthly_payment"]
        )

        debt_lines.append(
            f"- {creditor}: esposizione indicata "
            f"{amount}; rata indicata {payment}."
        )

    debt_section = (
        "\n".join(debt_lines)
        or "- Nessuna posizione debitoria dettagliata."
    )

    return f"""FIXTUDE
Documento di lavoro – {scenario["title"]}

Cliente: {client_name}

1. QUADRO ECONOMICO

Entrate mensili dichiarate: {income}
Spese mensili dichiarate: {expenses}
Disponibilità teorica mensile: {capacity}
Esposizione debitoria indicata: {debt}
Totale rate indicate: {payments}
Rapporto rate/entrate: {ratio}

2. POSIZIONI INDICATE

{debt_section}

3. LETTURA DELLA SITUAZIONE

{analysis["sustainability"]}

4. IPOTESI INDIVIDUATA

{scenario["reason"]}

Questa proposta rappresenta una possibile linea di approfondimento
costruita esclusivamente sui dati forniti dall'utente.

5. PROSSIMO PASSO

La proposta deve essere verificata e, se necessario, modificata
dal supervisore prima di qualsiasi utilizzo o comunicazione al cliente.

Il documento non costituisce consulenza legale, finanziaria o
certificazione della situazione di insolvenza e non determina
automaticamente l'accesso a procedure previste dalla legge.

6. VERIFICA

La versione definitiva deve essere approvata dal supervisore
FixTude prima dell'invio al cliente.
"""


# =========================================================
# PDF GENERATION
# =========================================================

def create_pdf(
    solution_id,
    title,
    text
):
    filename = (
        f"fixtude_solution_"
        f"{solution_id}_"
        f"{uuid.uuid4().hex[:10]}.pdf"
    )

    filepath = os.path.join(
        app.config["PDF_FOLDER"],
        filename
    )

    styles = getSampleStyleSheet()

    body_style = ParagraphStyle(
        "FixTudeBody",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=10.5,
        leading=15,
        spaceAfter=7,
        alignment=TA_LEFT
    )

    title_style = ParagraphStyle(
        "FixTudeTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=18,
        leading=22,
        spaceAfter=15
    )

    story = []

    lines = text.splitlines()

    first_non_empty = True

    for raw_line in lines:

        line = raw_line.strip()

        if not line:

            story.append(
                Spacer(
                    1,
                    4 * mm
                )
            )

            continue

        safe = escape(line)

        if first_non_empty:

            story.append(
                Paragraph(
                    safe,
                    title_style
                )
            )

            first_non_empty = False

        elif (
            line.isupper()
            or line.startswith(
                (
                    "1.",
                    "2.",
                    "3.",
                    "4.",
                    "5.",
                    "6."
                )
            )
        ):

            story.append(
                Paragraph(
                    f"<b>{safe}</b>",
                    body_style
                )
            )

        else:

            story.append(
                Paragraph(
                    safe.replace(
                        "  ",
                        "&nbsp;&nbsp;"
                    ),
                    body_style
                )
            )

    document = SimpleDocTemplate(
        filepath,
        pagesize=A4,
        rightMargin=20 * mm,
        leftMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        title=title
    )

    document.build(story)

    return filename


def regenerate_solution_pdf(
    solution_id
):
    db = get_db()

    solution = db.execute("""
        SELECT *
        FROM solution_documents
        WHERE id = ?
    """, (
        solution_id,
    )).fetchone()

    if not solution:

        db.close()

        return None

    filename = create_pdf(
        solution_id,
        solution["title"],
        solution["draft_text"]
    )

    db.execute("""
        UPDATE solution_documents
        SET
            pdf_filename = ?,
            updated_at = ?
        WHERE id = ?
    """, (
        filename,
        now_iso(),
        solution_id
    ))

    db.commit()
    db.close()

    return filename


# =========================================================
# AGENT ORCHESTRATOR
# =========================================================

def run_local_agent(
    case_id
):
    """
    Creates/replaces the current analysis and candidate solutions.

    Existing approved/sent documents are NOT deleted.
    Pending drafts from a previous run are replaced.
    """

    analysis = calculate_case(
        case_id
    )

    if not analysis:
        return None

    debts = get_debts(
        case_id
    )

    scenarios = build_scenarios(
        analysis,
        debts
    )

    analysis["scenario_count"] = len(
        scenarios
    )

    analysis["scenarios"] = [
        {
            "type": item["type"],
            "title": item["title"],
            "reason": item["reason"]
        }
        for item in scenarios
    ]

    db = get_db()

    timestamp = now_iso()

    db.execute("""
        INSERT INTO ai_analyses
        (
            case_id,
            status,
            analysis_json,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        case_id,
        "review",
        json.dumps(
            analysis,
            ensure_ascii=False
        ),
        timestamp,
        timestamp
    ))

    analysis_id = db.execute(
        "SELECT last_insert_rowid()"
    ).fetchone()[0]

    old_drafts = db.execute("""
        SELECT id, pdf_filename
        FROM solution_documents
        WHERE case_id = ?
        AND status IN (
            'pending_review',
            'draft'
        )
    """, (
        case_id,
    )).fetchall()

    for old in old_drafts:

        if old["pdf_filename"]:

            old_path = os.path.join(
                app.config["PDF_FOLDER"],
                old["pdf_filename"]
            )

            if os.path.exists(
                old_path
            ):

                try:
                    os.remove(
                        old_path
                    )

                except OSError:
                    pass

    db.execute("""
        DELETE FROM solution_documents
        WHERE case_id = ?
        AND status IN (
            'pending_review',
            'draft'
        )
    """, (
        case_id,
    ))

    db.commit()

    created_ids = []

    for scenario in scenarios:

        cursor = db.execute("""
            INSERT INTO solution_documents
            (
                case_id,
                analysis_id,
                solution_type,
                title,
                draft_text,
                original_text,
                status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            case_id,
            analysis_id,
            scenario["type"],
            scenario["title"],
            scenario["draft_text"],
            scenario["draft_text"],
            "pending_review",
            timestamp,
            timestamp
        ))

        created_ids.append(
            cursor.lastrowid
        )

    db.commit()
    db.close()

    for solution_id in created_ids:

        regenerate_solution_pdf(
            solution_id
        )

    return analysis_id


# =========================================================
# HOME
# =========================================================

@app.route("/")
def home():
    return render_template(
        "home.html"
    )


# =========================================================
# LOGIN
# =========================================================

@app.route(
    "/privato/login",
    methods=["GET", "POST"]
)
def debtor_login():

    error = None

    if request.method == "POST":

        email = request.form.get(
            "email",
            ""
        ).strip().lower()

        password = request.form.get(
            "password",
            ""
        )

        user = USERS.get(
            email
        )

        if (
            user
            and user["password"] == password
            and user["role"] == "debtor"
        ):

            session.clear()

            session["email"] = email
            session["role"] = "debtor"
            session["name"] = user["name"]

            return redirect(
                url_for(
                    "debtor_dashboard"
                )
            )

        error = (
            "I dati inseriti non risultano corretti. "
            "Controllali e riprova."
        )

    return render_template(
        "login.html",
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

        email = request.form.get(
            "email",
            ""
        ).strip().lower()

        password = request.form.get(
            "password",
            ""
        )

        user = USERS.get(
            email
        )

        if (
            user
            and user["password"] == password
            and user["role"] == "resolver"
        ):

            session.clear()

            session["email"] = email
            session["role"] = "resolver"
            session["name"] = user["name"]

            return redirect(
                url_for(
                    "resolver_dashboard"
                )
            )

        error = (
            "I dati inseriti non risultano corretti. "
            "Controllali e riprova."
        )

    return render_template(
        "login.html",
        role="resolver",
        error=error
    )


# =========================================================
# DEBTOR DASHBOARD
# =========================================================

@app.route("/privato")
def debtor_dashboard():

    if session.get("role") != "debtor":

        return redirect(
            url_for(
                "debtor_login"
            )
        )

    data = get_case_data(
        session.get("email")
    )

    completed = bool(
        data
    )

    case = get_case(
        session.get("email")
    )

    notification_count = 0

    if case:

        db = get_db()

        row = db.execute("""
            SELECT COUNT(*) AS total
            FROM notifications
            WHERE email = ?
            AND read = 0
        """, (
            session.get("email"),
        )).fetchone()

        notification_count = row["total"]

        db.close()

    return render_template(
        "dashboard.html",
        role="debtor",
        name=session.get("name"),
        completed=completed,
        notification_count=notification_count
    )


# =========================================================
# RESOLVER DASHBOARD
# =========================================================

@app.route("/risolutore")
def resolver_dashboard():

    if session.get("role") != "resolver":

        return redirect(
            url_for(
                "resolver_login"
            )
        )

    db = get_db()

    cases = db.execute("""
        SELECT
            c.id,
            c.email,
            c.data,
            c.updated_at,
            a.status AS analysis_status,
            COUNT(s.id) AS solution_count
        FROM cases c

        LEFT JOIN ai_analyses a
            ON a.id = (
                SELECT MAX(a2.id)
                FROM ai_analyses a2
                WHERE a2.case_id = c.id
            )

        LEFT JOIN solution_documents s
            ON s.case_id = c.id

        GROUP BY c.id

        ORDER BY c.updated_at DESC
    """).fetchall()

    db.close()

    return render_template(
        "resolver_dashboard.html",
        cases=cases,
        name=session.get("name")
    )


# =========================================================
# RESOLVER PRACTICE ENTRY
# =========================================================

@app.route("/risolutore/pratica")
def resolver_practice():

    if session.get("role") != "resolver":

        return redirect(
            url_for(
                "resolver_login"
            )
        )

    return redirect(
        url_for(
            "resolver_dashboard"
        )
    )


# =========================================================
# DEBTOR SITUATION
# =========================================================

@app.route(
    "/privato/situazione",
    methods=["GET", "POST"]
)
def debtor_situation():

    if session.get("role") != "debtor":

        return redirect(
            url_for(
                "debtor_login"
            )
        )

    email = session.get(
        "email"
    )

    if request.method == "GET":

        data = get_case_data(
            email
        )

        return render_template(
            "new_situation.html",
            data=data
        )

    data = {
        "personal": {
            "first_name": request.form.get(
                "first_name",
                ""
            ).strip(),

            "last_name": request.form.get(
                "last_name",
                ""
            ).strip(),

            "tax_code": request.form.get(
                "tax_code",
                ""
            ).strip().upper(),

            "birth_date": request.form.get(
                "birth_date",
                ""
            ),

            "address": request.form.get(
                "address",
                ""
            ).strip(),

            "city": request.form.get(
                "city",
                ""
            ).strip(),

            "phone": request.form.get(
                "phone",
                ""
            ).strip(),

            "email": email
        },

        "family": {
            "members": request.form.get(
                "family_members",
                "1"
            ),

            "dependents": request.form.get(
                "dependents",
                "0"
            ),

            "notes": request.form.get(
                "family_notes",
                ""
            ).strip()
        },

        "income": {
            "net_monthly": number(
                request.form.get(
                    "net_monthly"
                )
            ),

            "other_income": number(
                request.form.get(
                    "other_income"
                )
            ),

            "variable_income": number(
                request.form.get(
                    "variable_income"
                )
            )
        },

        "expenses": {
            "housing": number(
                request.form.get(
                    "housing"
                )
            ),

            "utilities": number(
                request.form.get(
                    "utilities"
                )
            ),

            "food": number(
                request.form.get(
                    "food"
                )
            ),

            "transport": number(
                request.form.get(
                    "transport"
                )
            ),

            "family": number(
                request.form.get(
                    "family_expenses"
                )
            ),

            "other": number(
                request.form.get(
                    "other_expenses"
                )
            )
        },

        "assets": {
            "bank": number(
                request.form.get(
                    "bank_balance"
                )
            ),

            "property": number(
                request.form.get(
                    "property_value"
                )
            ),

            "vehicles": number(
                request.form.get(
                    "vehicles_value"
                )
            ),

            "other": number(
                request.form.get(
                    "other_assets"
                )
            )
        },

        "procedures": {
            "status": request.form.get(
                "procedures",
                ""
            ),

            "details": request.form.get(
                "procedure_details",
                ""
            ).strip()
        },

        "authorization": request.form.get(
            "authorization",
            ""
        ).strip(),

        "privacy": bool(
            request.form.get(
                "privacy"
            )
        )
    }

    creditors = request.form.getlist(
        "creditor[]"
    )

    debt_types = request.form.getlist(
        "debt_type[]"
    )

    original_amounts = request.form.getlist(
        "original_amount[]"
    )

    current_amounts = request.form.getlist(
        "current_amount[]"
    )

    monthly_payments = request.form.getlist(
        "monthly_payment[]"
    )

    statuses = request.form.getlist(
        "debt_status[]"
    )

    notes = request.form.getlist(
        "debt_notes[]"
    )

    debts = []

    for i in range(
        len(creditors)
    ):

        creditor = creditors[i].strip()

        if not creditor:
            continue

        debts.append({
            "creditor": creditor,

            "debt_type": (
                debt_types[i]
                if i < len(debt_types)
                else ""
            ),

            "original_amount": (
                original_amounts[i]
                if i < len(original_amounts)
                else 0
            ),

            "current_amount": (
                current_amounts[i]
                if i < len(current_amounts)
                else 0
            ),

            "monthly_payment": (
                monthly_payments[i]
                if i < len(monthly_payments)
                else 0
            ),

            "status": (
                statuses[i]
                if i < len(statuses)
                else ""
            ),

            "notes": (
                notes[i]
                if i < len(notes)
                else ""
            )
        })

    case_id = save_case(
        email,
        data
    )

    save_debts(
        case_id,
        debts
    )

    files = request.files.getlist(
        "documents"
    )

    db = get_db()

    for file in files:

        if not file or not file.filename:
            continue

        if not allowed_file(
            file.filename
        ):
            continue

        extension = file.filename.rsplit(
            ".",
            1
        )[1].lower()

        stored_name = secure_filename(
            f"{uuid.uuid4().hex}.{extension}"
        )

        filepath = os.path.join(
            app.config["UPLOAD_FOLDER"],
            stored_name
        )

        file.save(
            filepath
        )

        db.execute("""
            INSERT INTO documents
            (
                case_id,
                original_name,
                stored_name,
                uploaded_at
            )
            VALUES (?, ?, ?, ?)
        """, (
            case_id,
            file.filename,
            stored_name,
            now_iso()
        ))

    db.commit()
    db.close()

    return redirect(
        url_for(
            "case_summary"
        )
    )


# =========================================================
# SUMMARY
# =========================================================

@app.route("/privato/riepilogo")
def case_summary():

    if session.get("role") != "debtor":

        return redirect(
            url_for(
                "debtor_login"
            )
        )

    email = session.get(
        "email"
    )

    case = get_case(
        email
    )

    if not case:

        return redirect(
            url_for(
                "debtor_situation"
            )
        )

    data = get_case_data(
        email
    )

    debts = get_debts(
        case["id"]
    )

    income = data.get(
        "income",
        {}
    )

    expenses = data.get(
        "expenses",
        {}
    )

    total_income = sum(
        number(v)
        for v in income.values()
    )

    total_expenses = sum(
        number(v)
        for v in expenses.values()
    )

    monthly_capacity = (
        total_income -
        total_expenses
    )

    total_debt = sum(
        number(debt["current_amount"])
        for debt in debts
    )

    total_monthly_payments = sum(
        number(debt["monthly_payment"])
        for debt in debts
    )

    return render_template(
        "summary.html",
        data=data,
        debts=debts,
        total_income=total_income,
        total_expenses=total_expenses,
        monthly_capacity=monthly_capacity,
        total_debt=total_debt,
        total_monthly_payments=total_monthly_payments
    )


# =========================================================
# PAYMENT AREA
# =========================================================

PAYMENTS_HTML = """
<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pagamenti - FixTude</title>

<style>
* {
    box-sizing: border-box;
}

body {
    margin: 0;
    font-family:
        Inter,
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        sans-serif;
    background: #f5f7fb;
    color: #182230;
}

.container {
    width: min(1050px, calc(100% - 32px));
    margin: 0 auto;
    padding: 40px 0 60px;
}

.header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 20px;
    margin-bottom: 35px;
}

.brand {
    font-size: 25px;
    font-weight: 800;
}

.back {
    color: #526071;
    text-decoration: none;
}

.hero {
    background: #111827;
    color: white;
    border-radius: 24px;
    padding: 35px;
    margin-bottom: 25px;
}

.hero small {
    text-transform: uppercase;
    letter-spacing: .12em;
    opacity: .7;
}

.hero h1 {
    margin: 8px 0 12px;
    font-size: clamp(30px, 5vw, 46px);
}

.hero p {
    margin: 0;
    max-width: 720px;
    color: #d6dbe4;
    line-height: 1.65;
}

.grid {
    display: grid;
    grid-template-columns:
        repeat(2, minmax(0, 1fr));
    gap: 22px;
}

.card {
    background: white;
    border: 1px solid #e5e9f0;
    border-radius: 22px;
    padding: 28px;
    box-shadow:
        0 10px 30px rgba(15, 23, 42, .06);
}

.card h2 {
    margin: 0 0 10px;
    font-size: 24px;
}

.card p {
    color: #647184;
    line-height: 1.6;
}

.price {
    font-size: 35px;
    font-weight: 800;
    margin: 20px 0;
}

.button {
    display: inline-block;
    width: 100%;
    border: 0;
    border-radius: 12px;
    padding: 14px 18px;
    background: #111827;
    color: white;
    font-size: 16px;
    font-weight: 700;
    cursor: pointer;
}

.button:hover {
    opacity: .92;
}

.badge {
    display: inline-block;
    border-radius: 99px;
    padding: 6px 10px;
    font-size: 12px;
    font-weight: 700;
    background: #eef2ff;
    color: #3730a3;
}

.notice {
    background: #fff8e7;
    border: 1px solid #f0d58a;
    color: #715b16;
    border-radius: 14px;
    padding: 16px;
    margin-bottom: 22px;
    line-height: 1.5;
}

.success {
    background: #ecfdf3;
    border: 1px solid #a7e3bd;
    color: #17683a;
    border-radius: 14px;
    padding: 16px;
    margin-bottom: 22px;
}

.error {
    background: #fff0f0;
    border: 1px solid #efb3b3;
    color: #8a2222;
    border-radius: 14px;
    padding: 16px;
    margin-bottom: 22px;
}

.small {
    color: #788597;
    font-size: 13px;
    line-height: 1.55;
    margin-top: 14px;
}

.locked {
    opacity: .65;
}

@media (max-width: 760px) {
    .grid {
        grid-template-columns: 1fr;
    }

    .hero {
        padding: 26px;
    }

    .container {
        width: min(100% - 22px, 1050px);
    }
}
</style>
</head>

<body>

<div class="container">

    <div class="header">
        <div class="brand">FixTude</div>

        <a
            class="back"
            href="{{ url_for('debtor_dashboard') }}"
        >
            ← Torna alla tua area
        </a>
    </div>

    <section class="hero">

        <small>Area pagamenti</small>

        <h1>
            Completa il servizio FixTude
        </h1>

        <p>
            I pagamenti vengono gestiti in modo sicuro
            attraverso Stripe. FixTude non memorizza
            i dati della carta.
        </p>

    </section>

    {% if success %}

        <div class="success">
            {{ success }}
        </div>

    {% endif %}

    {% if error %}

        <div class="error">
            {{ error }}
        </div>

    {% endif %}

    {% if not case %}

        <div class="notice">
            Per utilizzare i servizi a pagamento devi
            prima creare la tua situazione debitoria.
        </div>

    {% else %}

        <div class="grid">

            <div class="card">

                <span class="badge">
                    Servizio FixTude
                </span>

                <h2>
                    Analisi della situazione
                </h2>

                <p>
                    Analisi dei dati economici e delle
                    posizioni debitorie inserite, con
                    elaborazione di possibili scenari
                    da valutare.
                </p>

                <div class="price">
                    € 1,99
                </div>

                {% if analysis_paid %}

                    <div class="success">
                        Analisi già acquistata.
                    </div>

                    <a
                        class="button"
                        href="{{ url_for('case_analysis') }}"
                        style="text-decoration:none;text-align:center;"
                    >
                        Vai all'analisi
                    </a>

                {% else %}

                    <form
                        method="POST"
                        action="{{ url_for('payment_checkout') }}"
                    >

                        <input
                            type="hidden"
                            name="service"
                            value="analysis"
                        >

                        <button
                            class="button"
                            type="submit"
                        >
                            Paga € 1,99 con Stripe
                        </button>

                    </form>

                {% endif %}

                <div class="small">
                    Pagamento una tantum.
                    Nessun abbonamento automatico.
                </div>

            </div>


            <div class="card">

                <span class="badge">
                    Documento finale
                </span>

                <h2>
                    Documento PDF approvato
                </h2>

                <p>
                    Accesso al documento PDF relativo
                    alla soluzione elaborata e approvata
                    dal Risolutore FixTude.
                </p>

                <div class="price">
                    € 9,99
                </div>

                {% if pdf_paid %}

                    <div class="success">
                        Documento già acquistato.
                    </div>

                    <a
                        class="button"
                        href="{{ url_for('debtor_documents') }}"
                        style="text-decoration:none;text-align:center;"
                    >
                        Vai ai documenti
                    </a>

                {% elif approved_count > 0 %}

                    <form
                        method="POST"
                        action="{{ url_for('payment_checkout') }}"
                    >

                        <input
                            type="hidden"
                            name="service"
                            value="pdf"
                        >

                        <button
                            class="button"
                            type="submit"
                        >
                            Paga € 9,99 con Stripe
                        </button>

                    </form>

                {% else %}

                    <div class="notice">
                        Il documento PDF sarà acquistabile
                        quando una proposta sarà stata
                        verificata e approvata dal Risolutore.
                    </div>

                {% endif %}

                <div class="small">
                    Pagamento una tantum.
                    Nessun abbonamento automatico.
                </div>

            </div>

        </div>

    {% endif %}

    <div
        class="small"
        style="margin-top:28px;text-align:center;"
    >
        I pagamenti sono elaborati da Stripe.
        FixTude non riceve né memorizza i dati completi
        della carta utilizzata per il pagamento.
    </div>

</div>

</body>
</html>
"""


@app.route("/pagamenti")
def payments():

    if session.get("role") != "debtor":

        return redirect(
            url_for(
                "debtor_login"
            )
        )

    email = session.get(
        "email"
    )

    case = get_case(
        email
    )

    analysis_paid = False
    pdf_paid = False
    approved_count = 0

    if case:

        analysis_paid = bool(
            get_paid_payment(
                email,
                case["id"],
                "analysis"
            )
        )

        pdf_paid = bool(
            get_paid_payment(
                email,
                case["id"],
                "pdf"
            )
        )

        db = get_db()

        row = db.execute("""
            SELECT COUNT(*) AS total
            FROM solution_documents
            WHERE case_id = ?
              AND status = 'approved'
        """, (
            case["id"],
        )).fetchone()

        approved_count = row["total"]

        db.close()

    return render_template_string(
        PAYMENTS_HTML,
        case=case,
        analysis_paid=analysis_paid,
        pdf_paid=pdf_paid,
        approved_count=approved_count,
        success=request.args.get(
            "success"
        ),
        error=request.args.get(
            "error"
        )
    )


# =========================================================
# CREATE STRIPE CHECKOUT SESSION
# =========================================================

@app.route(
    "/pagamenti/checkout",
    methods=["POST"]
)
def payment_checkout():

    if session.get("role") != "debtor":

        return redirect(
            url_for(
                "debtor_login"
            )
        )

    if not STRIPE_SECRET_KEY:

        return redirect(
            url_for(
                "payments",
                error=(
                    "Stripe non è ancora configurato "
                    "sul server. Inserisci STRIPE_SECRET_KEY "
                    "nelle Environment Variables di Render."
                )
            )
        )

    service = request.form.get(
        "service",
        ""
    ).strip()

    service_data = PAYMENT_SERVICES.get(
        service
    )

    if not service_data:

        return redirect(
            url_for(
                "payments",
                error="Servizio di pagamento non valido."
            )
        )

    email = session.get(
        "email"
    )

    case = get_case(
        email
    )

    if not case:

        return redirect(
            url_for(
                "payments",
                error=(
                    "Prima di procedere devi creare "
                    "la tua situazione."
                )
            )
        )

    case_id = case["id"]

    # =====================================================
    # OWNERSHIP CHECK
    # =====================================================

    if case["email"] != email:

        abort(403)

    # =====================================================
    # PDF CAN ONLY BE PURCHASED AFTER APPROVAL
    # =====================================================

    if service == "pdf":

        db = get_db()

        approved = db.execute("""
            SELECT COUNT(*) AS total
            FROM solution_documents
            WHERE case_id = ?
              AND status = 'approved'
        """, (
            case_id,
        )).fetchone()

        db.close()

        if not approved or approved["total"] <= 0:

            return redirect(
                url_for(
                    "payments",
                    error=(
                        "Il documento PDF non è ancora "
                        "disponibile per l'acquisto."
                    )
                )
            )

    # =====================================================
    # DON'T CHARGE TWICE
    # =====================================================

    already_paid = get_paid_payment(
        email,
        case_id,
        service
    )

    if already_paid:

        return redirect(
            url_for(
                "payments",
                success=(
                    "Questo servizio risulta già acquistato."
                )
            )
        )

    payment_id = create_pending_payment(
        email,
        case_id,
        service
    )

    if not payment_id:

        return redirect(
            url_for(
                "payments",
                error=(
                    "Non è stato possibile creare "
                    "il pagamento."
                )
            )
        )

    try:

        checkout_session = (
            stripe.checkout.Session.create(
                mode="payment",

                line_items=[
                    {
                        "price_data": {
                            "currency": (
                                service_data["currency"]
                            ),

                            "product_data": {
                                "name": (
                                    service_data["name"]
                                ),

                                "description": (
                                    service_data[
                                        "description"
                                    ]
                                )
                            },

                            "unit_amount": (
                                service_data["amount"]
                            )
                        },

                        "quantity": 1
                    }
                ],

                customer_email=email,

                success_url=(
                    url_for(
                        "payment_success",
                        _external=True
                    )
                    + "?session_id="
                    + "{CHECKOUT_SESSION_ID}"
                ),

                cancel_url=(
                    url_for(
                        "payment_cancel",
                        _external=True
                    )
                    + "?payment_id="
                    + str(payment_id)
                ),

                metadata={
                    "payment_id": str(
                        payment_id
                    ),

                    "case_id": str(
                        case_id
                    ),

                    "service": service,

                    "user_email": email
                }
            )
        )

    except Exception as exc:

        mark_payment_cancelled(
            payment_id
        )

        app.logger.exception(
            "Stripe Checkout error"
        )

        return redirect(
            url_for(
                "payments",
                error=(
                    "Non è stato possibile "
                    "avviare il pagamento Stripe."
                )
            )
        )

    db = get_db()

    db.execute("""
        UPDATE payments
        SET stripe_session_id = ?
        WHERE id = ?
    """, (
        checkout_session.id,
        payment_id
    ))

    db.commit()
    db.close()

    return redirect(
        checkout_session.url,
        code=303
    )


# =========================================================
# STRIPE SUCCESS
#
# IMPORTANT:
# This route does NOT mark the payment as paid.
# Payment confirmation comes from the Stripe webhook.
# =========================================================

@app.route("/pagamenti/success")
def payment_success():

    if session.get("role") != "debtor":

        return redirect(
            url_for(
                "debtor_login"
            )
        )

    session_id = request.args.get(
        "session_id",
        ""
    ).strip()

    if not session_id:

        return redirect(
            url_for(
                "payments",
                error=(
                    "Sessione di pagamento non trovata."
                )
            )
        )

    payment = get_payment_by_session(
        session_id
    )

    if not payment:

        return redirect(
            url_for(
                "payments",
                error=(
                    "Pagamento non ancora registrato. "
                    "Riprova tra qualche secondo."
                )
            )
        )

    if payment["user_email"] != session.get(
        "email"
    ):

        abort(403)

    status_message = (
        "Pagamento ricevuto. "
        "Stripe sta confermando la transazione."
    )

    if payment["status"] == "paid":

        status_message = (
            "Pagamento completato con successo."
        )

    elif payment["status"] == "cancelled":

        status_message = (
            "Il pagamento risulta annullato."
        )

    return redirect(
        url_for(
            "payments",
            success=status_message
        )
    )


# =========================================================
# STRIPE CANCEL
# =========================================================

@app.route("/pagamenti/cancel")
def payment_cancel():

    if session.get("role") != "debtor":

        return redirect(
            url_for(
                "debtor_login"
            )
        )

    payment_id = request.args.get(
        "payment_id",
        ""
    ).strip()

    if payment_id.isdigit():

        payment = get_payment_by_id(
            int(payment_id)
        )

        if payment:

            if payment["user_email"] == session.get(
                "email"
            ):

                mark_payment_cancelled(
                    int(payment_id)
                )

    return redirect(
        url_for(
            "payments",
            error=(
                "Pagamento annullato. "
                "Nessun addebito è stato effettuato."
            )
        )
    )


# =========================================================
# STRIPE WEBHOOK
# =========================================================

@app.route(
    "/stripe/webhook",
    methods=["POST"]
)
def stripe_webhook():

    if not STRIPE_WEBHOOK_SECRET:

        app.logger.error(
            "STRIPE_WEBHOOK_SECRET non configurato."
        )

        return (
            "Webhook non configurato",
            500
        )

    payload = request.get_data(
        cache=False,
        as_text=False
    )

    signature = request.headers.get(
        "Stripe-Signature"
    )

    if not signature:

        return (
            "Missing Stripe signature",
            400
        )

    try:

        event = stripe.Webhook.construct_event(
            payload,
            signature,
            STRIPE_WEBHOOK_SECRET
        )

    except ValueError:

        return (
            "Invalid payload",
            400
        )

    except stripe.error.SignatureVerificationError:

        return (
            "Invalid signature",
            400
        )

    except Exception:

        app.logger.exception(
            "Stripe webhook error"
        )

        return (
            "Webhook error",
            400
        )

    event_type = event.get(
        "type"
    )

    # =====================================================
    # CHECKOUT COMPLETED
    # =====================================================

    if event_type == (
        "checkout.session.completed"
    ):

        checkout_session = (
            event["data"]["object"]
        )

        metadata = (
            checkout_session.get(
                "metadata",
                {}
            )
        )

        payment_id_value = (
            metadata.get(
                "payment_id"
            )
        )

        payment_status = (
            checkout_session.get(
                "payment_status"
            )
        )

        if (
            payment_id_value
            and str(payment_id_value).isdigit()
            and payment_status == "paid"
        ):

            mark_payment_paid(
                int(payment_id_value),
                stripe_session_id=(
                    checkout_session.get(
                        "id"
                    )
                ),
                stripe_payment_intent_id=(
                    checkout_session.get(
                        "payment_intent"
                    )
                )
            )

    # =====================================================
    # ASYNC PAYMENT SUCCESS
    # =====================================================

    elif event_type == (
        "checkout.session.async_payment_succeeded"
    ):

        checkout_session = (
            event["data"]["object"]
        )

        metadata = (
            checkout_session.get(
                "metadata",
                {}
            )
        )

        payment_id_value = (
            metadata.get(
                "payment_id"
            )
        )

        if (
            payment_id_value
            and str(payment_id_value).isdigit()
        ):

            mark_payment_paid(
                int(payment_id_value),
                stripe_session_id=(
                    checkout_session.get(
                        "id"
                    )
                ),
                stripe_payment_intent_id=(
                    checkout_session.get(
                        "payment_intent"
                    )
                )
            )

    # =====================================================
    # CHECKOUT EXPIRED
    # =====================================================

    elif event_type == (
        "checkout.session.expired"
    ):

        checkout_session = (
            event["data"]["object"]
        )

        metadata = (
            checkout_session.get(
                "metadata",
                {}
            )
        )

        payment_id_value = (
            metadata.get(
                "payment_id"
            )
        )

        if (
            payment_id_value
            and str(payment_id_value).isdigit()
        ):

            mark_payment_cancelled(
                int(payment_id_value)
            )

    return "", 200


# =========================================================
# RUN AGENT
#
# NEW:
# Analysis requires the €1.99 payment.
# =========================================================

@app.route("/privato/analisi")
def case_analysis():

    if session.get("role") != "debtor":

        return redirect(
            url_for(
                "debtor_login"
            )
        )

    email = session.get(
        "email"
    )

    case = get_case(
        email
    )

    if not case:

        return redirect(
            url_for(
                "debtor_situation"
            )
        )

    # =====================================================
    # PAYMENT GATE
    # =====================================================

    paid = get_paid_payment(
        email,
        case["id"],
        "analysis"
    )

    if not paid:

        return redirect(
            url_for(
                "payments",
                error=(
                    "Per elaborare la tua situazione "
                    "è necessario acquistare l'Analisi FixTude "
                    "al costo di € 1,99."
                )
            )
        )

    # =====================================================
    # RUN LOCAL AGENT
    # =====================================================

    analysis_id = run_local_agent(
        case["id"]
    )

    if not analysis_id:

        return (
            "Impossibile elaborare la pratica.",
            500
        )

    analysis_row = get_latest_analysis(
        case["id"]
    )

    try:

        analysis = json.loads(
            analysis_row["analysis_json"]
        )

    except (
        TypeError,
        json.JSONDecodeError
    ):

        analysis = calculate_case(
            case["id"]
        )

    debts = get_debts(
        case["id"]
    )

    return render_template(
        "analysis.html",
        data=get_case_data(
            email
        ),
        debts=debts,
        total_income=analysis[
            "total_income"
        ],
        total_expenses=analysis[
            "total_expenses"
        ],
        monthly_capacity=analysis[
            "monthly_capacity"
        ],
        total_debt=analysis[
            "total_debt"
        ],
        total_payments=analysis[
            "total_monthly_payments"
        ],
        sustainability=analysis[
            "sustainability"
        ],
        warnings=analysis[
            "warnings"
        ],
        scenarios=analysis[
            "scenarios"
        ],
        analysis=analysis
    )


# =========================================================
# RESOLVER CASE DETAIL
# =========================================================

@app.route(
    "/risolutore/pratica/<int:case_id>"
)
def resolver_case(
    case_id
):

    if session.get("role") != "resolver":

        return redirect(
            url_for(
                "resolver_login"
            )
        )

    case = get_case_by_id(
        case_id
    )

    if not case:
        abort(404)

    data = json.loads(
        case["data"]
    )

    debts = get_debts(
        case_id
    )

    documents = get_documents(
        case_id
    )

    analysis_row = get_latest_analysis(
        case_id
    )

    solutions = get_solution_documents(
        case_id
    )

    analysis = None

    if analysis_row:

        try:

            analysis = json.loads(
                analysis_row[
                    "analysis_json"
                ]
            )

        except (
            TypeError,
            json.JSONDecodeError
        ):

            analysis = None

    return render_template(
        "resolver_case.html",
        case=case,
        data=data,
        debts=debts,
        documents=documents,
        analysis=analysis,
        analysis_row=analysis_row,
        solutions=solutions
    )


# =========================================================
# REGENERATE ANALYSIS / SOLUTIONS
# =========================================================

@app.route(
    "/risolutore/pratica/<int:case_id>/analizza",
    methods=["POST"]
)
def resolver_run_analysis(
    case_id
):

    if session.get("role") != "resolver":

        return redirect(
            url_for(
                "resolver_login"
            )
        )

    case = get_case_by_id(
        case_id
    )

    if not case:
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


# =========================================================
# EDIT / CORRECT SOLUTION
# =========================================================

@app.route(
    "/risolutore/soluzione/<int:solution_id>/correggi",
    methods=["POST"]
)
def correct_solution(
    solution_id
):

    if session.get("role") != "resolver":

        return redirect(
            url_for(
                "resolver_login"
            )
        )

    db = get_db()

    solution = db.execute("""
        SELECT *
        FROM solution_documents
        WHERE id = ?
    """, (
        solution_id,
    )).fetchone()

    if not solution:

        db.close()

        abort(404)

    corrected_text = request.form.get(
        "draft_text",
        ""
    ).strip()

    correction_note = request.form.get(
        "correction_note",
        ""
    ).strip()

    if not corrected_text:

        corrected_text = solution[
            "draft_text"
        ]

    db.execute("""
        INSERT INTO supervision
        (
            solution_id,
            original_text,
            corrected_text,
            correction_note,
            supervisor_email,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        solution_id,
        solution["draft_text"],
        corrected_text,
        correction_note,
        session.get("email"),
        now_iso()
    ))

    db.execute("""
        UPDATE solution_documents
        SET
            draft_text = ?,
            status = 'pending_review',
            updated_at = ?,
            approved_at = NULL,
            sent_at = NULL
        WHERE id = ?
    """, (
        corrected_text,
        now_iso(),
        solution_id
    ))

    db.commit()
    db.close()

    regenerate_solution_pdf(
        solution_id
    )

    solution = get_solution_by_id(
        solution_id
    )

    return redirect(
        url_for(
            "resolver_case",
            case_id=solution["case_id"]
        )
    )


def get_solution_by_id(
    solution_id
):

    db = get_db()

    row = db.execute("""
        SELECT *
        FROM solution_documents
        WHERE id = ?
    """, (
        solution_id,
    )).fetchone()

    db.close()

    return row


# =========================================================
# APPROVE + SEND
# =========================================================

@app.route(
    "/risolutore/soluzione/<int:solution_id>/approva",
    methods=["POST"]
)
def approve_solution(
    solution_id
):

    if session.get("role") != "resolver":

        return redirect(
            url_for(
                "resolver_login"
            )
        )

    db = get_db()

    solution = db.execute("""
        SELECT *
        FROM solution_documents
        WHERE id = ?
    """, (
        solution_id,
    )).fetchone()

    if not solution:

        db.close()

        abort(404)

    case = db.execute("""
        SELECT *
        FROM cases
        WHERE id = ?
    """, (
        solution["case_id"],
    )).fetchone()

    if not case:

        db.close()

        abort(404)

    timestamp = now_iso()

    db.execute("""
        UPDATE solution_documents
        SET
            status = 'approved',
            approved_at = ?,
            sent_at = ?,
            updated_at = ?
        WHERE id = ?
    """, (
        timestamp,
        timestamp,
        timestamp,
        solution_id
    ))

    db.execute("""
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
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        case["id"],
        case["email"],
        "Una nuova comunicazione è disponibile",
        (
            f'FixTude ha completato la verifica della proposta '
            f'"{solution["title"]}". '
            f'La versione approvata è ora disponibile nella tua area.'
        ),
        "in_app",
        0,
        timestamp
    ))

    db.commit()
    db.close()

    return redirect(
        url_for(
            "resolver_case",
            case_id=solution["case_id"]
        )
    )


# =========================================================
# PDF DOWNLOAD
#
# NEW:
# Debtor must have paid €9.99 for PDF access.
# Resolver can download freely.
# =========================================================

@app.route(
    "/pdf/<filename>"
)
def download_pdf(
    filename
):

    if session.get("role") not in (
        "debtor",
        "resolver"
    ):

        return redirect(
            url_for(
                "home"
            )
        )

    safe_filename = secure_filename(
        filename
    )

    if safe_filename != filename:

        abort(404)

    solution = None

    db = get_db()

    # =====================================================
    # DEBTOR
    # =====================================================

    if session.get("role") == "debtor":

        solution = db.execute("""
            SELECT s.*
            FROM solution_documents s
            JOIN cases c
                ON c.id = s.case_id
            WHERE s.pdf_filename = ?
              AND c.email = ?
              AND s.status = 'approved'
        """, (
            filename,
            session.get("email")
        )).fetchone()

    # =====================================================
    # RESOLVER
    # =====================================================

    else:

        solution = db.execute("""
            SELECT *
            FROM solution_documents
            WHERE pdf_filename = ?
        """, (
            filename,
        )).fetchone()

    db.close()

    if not solution:

        abort(404)

    # =====================================================
    # PAYMENT GATE FOR DEBTOR
    # =====================================================

    if session.get("role") == "debtor":

        paid = get_paid_payment(
            session.get("email"),
            solution["case_id"],
            "pdf"
        )

        if not paid:

            return redirect(
                url_for(
                    "payments",
                    error=(
                        "Per scaricare il documento PDF "
                        "è necessario completare il pagamento "
                        "di € 9,99."
                    )
                )
            )

    return send_from_directory(
        app.config["PDF_FOLDER"],
        safe_filename,
        as_attachment=True
    )


# =========================================================
# DEBTOR NOTIFICATIONS
# =========================================================

@app.route(
    "/privato/notifiche"
)
def debtor_notifications():

    if session.get("role") != "debtor":

        return redirect(
            url_for(
                "debtor_login"
            )
        )

    email = session.get(
        "email"
    )

    notifications = get_notifications(
        email
    )

    db = get_db()

    db.execute("""
        UPDATE notifications
        SET read = 1
        WHERE email = ?
    """, (
        email,
    ))

    db.commit()
    db.close()

    return render_template(
        "notifications.html",
        notifications=notifications
    )


# =========================================================
# DEBTOR APPROVED DOCUMENTS
# =========================================================

@app.route(
    "/privato/documenti"
)
def debtor_documents():

    if session.get("role") != "debtor":

        return redirect(
            url_for(
                "debtor_login"
            )
        )

    case = get_case(
        session.get("email")
    )

    if not case:

        return redirect(
            url_for(
                "debtor_situation"
            )
        )

    db = get_db()

    solutions = db.execute("""
        SELECT *
        FROM solution_documents
        WHERE case_id = ?
          AND status = 'approved'
        ORDER BY approved_at DESC
    """, (
        case["id"],
    )).fetchall()

    db.close()

    return render_template(
        "debtor_documents.html",
        solutions=solutions
    )


# =========================================================
# LOGOUT
# =========================================================

@app.route(
    "/logout"
)
def logout():

    session.clear()

    return redirect(
        url_for(
            "home"
        )
    )


# =========================================================
# ERROR HANDLERS
# =========================================================

@app.errorhandler(413)
def file_too_large(
    error
):

    return (
        "Il file supera il limite massimo consentito di 50 MB.",
        413
    )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=5000
    )
