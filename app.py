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
from pathlib import Path
from datetime import datetime, timezone

from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

# ============================================================
# FIXTUDE
# ============================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "FIXTUDE_SECRET",
    "fixtude-secret-change-this"
)

BASE_DIR = Path(__file__).resolve().parent

DB_PATH = BASE_DIR / "fixtude.db"
UPLOAD_DIR = BASE_DIR / "uploads"
PDF_DIR = BASE_DIR / "generated_pdfs"

UPLOAD_DIR.mkdir(exist_ok=True)
PDF_DIR.mkdir(exist_ok=True)


# ============================================================
# PDF
# ============================================================

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (
        SimpleDocTemplate,
        Paragraph,
        Spacer
    )
    from reportlab.lib.enums import TA_LEFT

    REPORTLAB_AVAILABLE = True

except Exception:
    REPORTLAB_AVAILABLE = False


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
# DATABASE
# ============================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )


def db_connect():
    conn = sqlite3.connect(DB_PATH)

    conn.row_factory = sqlite3.Row

    conn.execute(
        "PRAGMA foreign_keys = ON"
    )

    return conn


def init_db():

    conn = db_connect()

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
            created_at TEXT NOT NULL,
            FOREIGN KEY(case_id)
                REFERENCES cases(id)
                ON DELETE CASCADE
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            stored_path TEXT NOT NULL,
            created_at TEXT NOT NULL,
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
                ON DELETE CASCADE
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

    # Crea gli account demo se non esistono.
    for email, user in DEMO_USERS.items():

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
                    generate_password_hash(
                        user["password"]
                    ),
                    user["role"],
                    now_iso()
                )
            )

    conn.commit()
    conn.close()


init_db()


# ============================================================
# AUTH
# ============================================================

def current_user():
    return session.get("user")


def require_login(role=None):

    user = current_user()

    if not user:
        return None

    if role and user.get("role") != role:
        return None

    return user


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
# NUMERI
# ============================================================

def parse_float(value):

    if value is None:
        return 0.0

    text = str(value).strip()

    text = text.replace(
        "€",
        ""
    ).replace(
        " ",
        ""
    )

    if not text:
        return 0.0

    if "," in text and "." in text:

        if text.rfind(",") > text.rfind("."):

            text = (
                text
                .replace(".", "")
                .replace(",", ".")
            )

        else:

            text = text.replace(",", "")

    elif "," in text:

        text = (
            text
            .replace(".", "")
            .replace(",", ".")
        )

    try:
        return float(text)

    except ValueError:
        return 0.0


def money(value):

    try:
        return float(value or 0)

    except (
        TypeError,
        ValueError
    ):
        return 0.0


# ============================================================
# CASE
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

    case = get_case(case_id)

    if not case:
        return {}

    try:

        return json.loads(
            case["data"] or "{}"
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


def get_documents(case_id):

    conn = db_connect()

    rows = conn.execute(
        """
        SELECT *
        FROM documents
        WHERE case_id = ?
        ORDER BY id DESC
        """,
        (case_id,)
    ).fetchall()

    conn.close()

    return rows


def client_name(data):

    name = str(
        data.get("name", "")
    ).strip()

    surname = str(
        data.get("surname", "")
    ).strip()

    result = (
        f"{name} {surname}"
    ).strip()

    return result or "Cliente FixTude"


# ============================================================
# ANALISI
# ============================================================

def calculate_case(case_id):

    data = get_case_data(
        case_id
    )

    debts = get_debts(
        case_id
    )

    incomes = data.get(
        "incomes",
        []
    ) or []

    expenses = data.get(
        "expenses",
        []
    ) or []

    total_income = sum(
        parse_float(
            item.get("amount")
        )
        for item in incomes
        if isinstance(item, dict)
    )

    total_expenses = sum(
        parse_float(
            item.get("amount")
        )
        for item in expenses
        if isinstance(item, dict)
    )

    monthly_capacity = (
        total_income -
        total_expenses
    )

    total_debt = sum(
        money(
            row["current_amount"]
        )
        for row in debts
    )

    total_payments = sum(
        money(
            row["monthly_payment"]
        )
        for row in debts
    )

    if monthly_capacity <= 0:

        sustainability = "critica"

    elif monthly_capacity < total_payments:

        sustainability = "debole"

    elif total_payments <= 0:

        sustainability = "da valutare"

    elif total_payments <= (
        monthly_capacity * 0.30
    ):

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

    if (
        total_payments >
        monthly_capacity >
        0
    ):

        warnings.append(
            "Le rate indicate superano la disponibilità teorica mensile."
        )

    if not data.get(
        "employment"
    ):

        warnings.append(
            "La situazione lavorativa non è stata indicata."
        )

    return {
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

        "total_payments": round(
            total_payments,
            2
        ),

        "sustainability":
            sustainability,

        "warnings":
            warnings,

        "debts":
            debts,

        "data":
            data
    }


# ============================================================
# SCENARI
# ============================================================

def build_scenarios(calc):

    capacity = calc[
        "monthly_capacity"
    ]

    debt = calc[
        "total_debt"
    ]

    payments = calc[
        "total_payments"
    ]

    scenarios = []

    if debt <= 0:

        return [{
            "type":
                "raccolta_dati",

            "title":
                "Completamento del quadro",

            "description":
                "Prima di formulare una proposta economica è necessario valorizzare almeno una posizione debitoria.",

            "estimated_monthly":
                0,

            "priority":
                "alta"
        }]

    if capacity > 0:

        sustainable = min(
            capacity * 0.30,
            payments
            if payments > 0
            else capacity * 0.30
        )

        sustainable = max(
            50,
            round(
                sustainable,
                2
            )
        )

        months = max(
            1,
            round(
                debt /
                sustainable
            )
        )

        scenarios.append({

            "type":
                "piano_rientro",

            "title":
                "Piano di rientro sostenibile",

            "description":
                "Ipotesi di rata costruita partendo dalla disponibilità teorica mensile indicata. È una simulazione e non una proposta vincolante per il creditore.",

            "estimated_monthly":
                sustainable,

            "months":
                months,

            "priority":
                "alta"
                if payments > capacity
                else "media"
        })

        if debt > 5000:

            target = round(
                debt * 0.70,
                2
            )

            settlement_monthly = max(
                50,
                round(
                    capacity * 0.25,
                    2
                )
            )

            months2 = max(
                1,
                round(
                    target /
                    settlement_monthly
                )
            )

            scenarios.append({

                "type":
                    "saldo_stralcio",

                "title":
                    "Ipotesi di definizione transattiva",

                "description":
                    "Possibile scenario da approfondire con il creditore, subordinato alla disponibilità di una somma e all'accettazione della controparte.",

                "estimated_amount":
                    target,

                "estimated_monthly":
                    settlement_monthly,

                "months":
                    months2,

                "priority":
                    "media"
            })

    scenarios.append({

        "type":
            "rinegoziazione",

        "title":
            "Richiesta di rinegoziazione",

        "description":
            "Richiesta di riduzione della rata o di diversa articolazione dei pagamenti, supportata dal quadro economico raccolto.",

        "estimated_monthly":
            max(
                0,
                round(
                    min(
                        payments,
                        max(
                            capacity * 0.30,
                            0
                        )
                    ),
                    2
                )
            ),

        "priority":
            "media"
    })

    if calc[
        "sustainability"
    ] == "critica":

        scenarios.append({

            "type":
                "approfondimento_professionale",

            "title":
                "Approfondimento con professionista qualificato",

            "description":
                "La sostenibilità corrente risulta critica. Il caso merita una valutazione professionale prima di assumere impegni economici.",

            "estimated_monthly":
                0,

            "priority":
                "alta"
        })

    return scenarios[:4]


# ============================================================
# ANALYSIS TEXT
# ============================================================

def build_analysis_text(
    calc,
    scenarios
):

    if calc[
        "sustainability"
    ] == "critica":

        opening = (
            "Il quadro economico presenta "
            "una forte tensione: la disponibilità "
            "mensile indicata non appare sufficiente "
            "a sostenere gli impegni rilevati."
        )

    elif calc[
        "sustainability"
    ] in (
        "debole",
        "sotto pressione"
    ):

        opening = (
            "Il quadro evidenzia una disponibilità "
            "mensile limitata rispetto agli impegni "
            "debitori indicati."
        )

    else:

        opening = (
            "Il quadro evidenzia una disponibilità "
            "mensile che consente di ipotizzare "
            "alcune modalità di gestione degli impegni, "
            "da verificare con i creditori."
        )

    text = [
        opening,

        (
            f"Entrate mensili indicate: "
            f"€ {calc['total_income']:.2f}."
        ),

        (
            f"Spese mensili indicate: "
            f"€ {calc['total_expenses']:.2f}."
        ),

        (
            f"Disponibilità teorica: "
            f"€ {calc['monthly_capacity']:.2f}."
        ),

        (
            f"Debito complessivo indicato: "
            f"€ {calc['total_debt']:.2f}."
        ),

        (
            f"Rate mensili indicate: "
            f"€ {calc['total_payments']:.2f}."
        ),

        (
            "Le elaborazioni di FixTude hanno valore "
            "informativo e simulativo e non costituiscono "
            "certificazione di insolvenza, parere legale "
            "né garanzia di accettazione da parte dei creditori."
        )
    ]

    if calc["warnings"]:

        text.append(
            "Elementi da verificare: "
            +
            " ".join(
                calc["warnings"]
            )
        )

    return "\n\n".join(
        text
    )


# ============================================================
# PDF
# ============================================================

def generate_pdf(solution_id):

    if not REPORTLAB_AVAILABLE:

        return None

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
        return None

    filename = (
        f"fixtude_soluzione_"
        f"{solution_id}.pdf"
    )

    filepath = (
        PDF_DIR /
        filename
    )

    styles = getSampleStyleSheet()

    normal = ParagraphStyle(
        "FixNormal",
        parent=styles["Normal"],
        fontName="Helvetica",
        fontSize=10.5,
        leading=15,
        spaceAfter=8
    )

    title_style = ParagraphStyle(
        "FixTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=20,
        leading=24,
        spaceAfter=16
    )

    document = SimpleDocTemplate(
        str(filepath),
        pagesize=A4,
        rightMargin=45,
        leftMargin=45,
        topMargin=45,
        bottomMargin=45
    )

    story = []

    story.append(
        Paragraph(
            solution["title"],
            title_style
        )
    )

    story.append(
        Paragraph(
            "Documento generato da FixTude",
            normal
        )
    )

    story.append(
        Spacer(
            1,
            10
        )
    )

    for line in (
        solution["content"]
        .split("\n")
    ):

        line = line.strip()

        if line:

            story.append(
                Paragraph(
                    line.replace(
                        "&",
                        "&amp;"
                    ),
                    normal
                )
            )

    story.append(
        Spacer(
            1,
            10
        )
    )

    story.append(
        Paragraph(
            "<b>Nota importante</b>",
            normal
        )
    )

    story.append(
        Paragraph(
            "Questo documento contiene una simulazione "
            "elaborata sulla base delle informazioni "
            "disponibili. Non rappresenta una certificazione "
            "di insolvenza, un parere legale o una proposta "
            "accettata dal creditore.",
            normal
        )
    )

    document.build(
        story
    )

    conn = db_connect()

    conn.execute(
        """
        UPDATE solution_documents
        SET pdf_path = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            str(filepath),
            now_iso(),
            solution_id
        )
    )

    conn.commit()
    conn.close()

    return str(filepath)


# ============================================================
# AGENT
# ============================================================

def run_local_agent(case_id):

    calc = calculate_case(
        case_id
    )

    scenarios = build_scenarios(
        calc
    )

    analysis_text = build_analysis_text(
        calc,
        scenarios
    )

    created = now_iso()

    analysis_payload = {

        "created_at":
            created,

        "summary":
            analysis_text,

        "metrics": {

            "total_income":
                calc["total_income"],

            "total_expenses":
                calc["total_expenses"],

            "monthly_capacity":
                calc["monthly_capacity"],

            "total_debt":
                calc["total_debt"],

            "total_payments":
                calc["total_payments"],

            "sustainability":
                calc["sustainability"]
        },

        "warnings":
            calc["warnings"],

        "scenarios":
            scenarios
    }

    conn = db_connect()

    old = conn.execute(
        """
        SELECT id, pdf_path
        FROM solution_documents
        WHERE case_id = ?
        AND status = 'pending_review'
        """,
        (case_id,)
    ).fetchall()

    for row in old:

        if row["pdf_path"]:

            try:
                Path(
                    row["pdf_path"]
                ).unlink(
                    missing_ok=True
                )

            except Exception:
                pass

    conn.execute(
        """
        DELETE FROM solution_documents
        WHERE case_id = ?
        AND status = 'pending_review'
        """,
        (case_id,)
    )

    cursor = conn.execute(
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

    analysis_id = cursor.lastrowid

    solution_ids = []

    for scenario in scenarios:

        lines = [

            f"Scenario: {scenario['title']}.",

            scenario["description"],

            (
                "Priorità di revisione: "
                +
                scenario.get(
                    "priority",
                    "media"
                )
                +
                "."
            )
        ]

        if scenario.get(
            "estimated_monthly"
        ):

            lines.append(
                (
                    "Rata mensile simulata: "
                    f"€ {scenario['estimated_monthly']:.2f}."
                )
            )

        if scenario.get(
            "estimated_amount"
        ):

            lines.append(
                (
                    "Importo transattivo simulato: "
                    f"€ {scenario['estimated_amount']:.2f}."
                )
            )

        if scenario.get(
            "months"
        ):

            lines.append(
                (
                    "Durata teorica: "
                    f"circa {scenario['months']} mesi."
                )
            )

        lines.append(
            "Il Risolutore deve verificare "
            "il contenuto prima dell'eventuale "
            "invio al cliente."
        )

        content = "\n".join(
            lines
        )

        cursor = conn.execute(
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
            VALUES (?, ?, ?, ?, ?, 'pending_review', ?, ?)
            """,
            (
                case_id,
                analysis_id,
                scenario["title"],
                scenario["type"],
                content,
                created,
                created
            )
        )

        solution_ids.append(
            cursor.lastrowid
        )

    conn.commit()
    conn.close()

    for solution_id in solution_ids:

        generate_pdf(
            solution_id
        )

    return analysis_payload


# ============================================================
# HOME
# ============================================================

HOME_HTML = """
<!DOCTYPE html>

<html lang="it">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>
FixTude | Metti ordine nella tua situazione debitoria
</title>

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    font-family:
        Arial,
        Helvetica,
        sans-serif;
    background: #f5f7fa;
    color: #18212b;
}

a {
    text-decoration: none;
}

.wrap {
    width: min(
        1180px,
        92%
    );
    margin: auto;
}

header {
    background: white;
    border-bottom:
        1px solid #e4e8ec;
}

.nav {
    min-height: 76px;
    display: flex;
    align-items: center;
    justify-content: space-between;
}

.logo {
    font-size: 30px;
    font-weight: 800;
    color: #172536;
}

.logo span {
    color: #f47b20;
}

.nav-login {
    color: #172536;
    font-weight: 700;
}

.hero {
    padding: 55px 0;
}

.hero-grid {
    display: grid;
    grid-template-columns:
        1fr 1fr;
    gap: 28px;
}

.card {
    background: white;
    border:
        1px solid #e2e7eb;
    border-radius: 22px;
    padding: 40px;
    box-shadow:
        0 12px 35px
        rgba(20,35,55,.07);
}

.eyebrow {
    display: inline-block;
    background: #fff0e6;
    color: #e96813;
    padding: 8px 13px;
    border-radius: 30px;
    font-size: 13px;
    font-weight: 800;
    margin-bottom: 18px;
}

h1 {
    font-size:
        clamp(
            36px,
            5vw,
            52px
        );
    line-height: 1.05;
    margin: 0 0 20px;
    letter-spacing: -1.8px;
}

.lead {
    color: #5d6a76;
    font-size: 18px;
    line-height: 1.6;
}

.points {
    margin:
        25px 0;
}

.point {
    margin: 13px 0;
    color: #45525e;
    line-height: 1.45;
}

.point::before {
    content: "✓";
    color: #f47b20;
    font-weight: 800;
    margin-right: 10px;
}

.actions {
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
}

.button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 48px;
    padding:
        0 22px;
    border-radius: 10px;
    font-weight: 800;
}

.primary {
    background: #f47b20;
    color: white;
}

.secondary {
    background: #edf1f4;
    color: #172536;
}

.note {
    margin-top: 18px;
    font-size: 12px;
    line-height: 1.5;
    color: #7b8792;
}

.free {
    background: #172536;
    color: white;
}

.free-badge {
    display: inline-block;
    background: #f47b20;
    color: white;
    padding:
        7px 13px;
    border-radius: 30px;
    font-size: 12px;
    font-weight: 800;
    text-transform: uppercase;
    margin-bottom: 17px;
}

.free h2 {
    font-size: 30px;
    line-height: 1.15;
    margin: 0 0 13px;
}

.free > p {
    color: #c8d1d9;
    line-height: 1.55;
}

.sic-grid {
    display: grid;
    grid-template-columns:
        1fr 1fr;
    gap: 13px;
    margin-top: 25px;
}

.sic {
    min-height: 135px;
    background: #243447;
    border:
        1px solid #35495c;
    border-radius: 13px;
    padding: 17px;
    color: white;
    transition: .2s;
}

.sic:hover {
    transform: translateY(-2px);
    background: #2b3d50;
}

.thumb {
    width: 43px;
    height: 43px;
    border-radius: 9px;
    background: white;
    color: #172536;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 800;
    margin-bottom: 10px;
}

.sic strong {
    display: block;
    margin-bottom: 5px;
}

.sic small {
    color: #b9c4cd;
    line-height: 1.4;
}

.sic-link {
    display: block;
    margin-top: 12px;
    font-size: 12px;
    font-weight: 700;
    color: #f5a268;
}

.free-note {
    margin-top: 20px;
    padding-top: 17px;
    border-top:
        1px solid #35495c;
    color: #b9c4cd;
    font-size: 12px;
    line-height: 1.5;
}

.how {
    padding-bottom: 60px;
}

.how-title {
    text-align: center;
    margin-bottom: 25px;
}

.steps {
    display: grid;
    grid-template-columns:
        repeat(3,1fr);
    gap: 20px;
}

.step {
    background: white;
    border:
        1px solid #e2e7eb;
    border-radius: 16px;
    padding: 25px;
}

.step-number {
    width: 38px;
    height: 38px;
    border-radius: 50%;
    background: #fff0e6;
    color: #f47b20;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 800;
}

footer {
    background: #111d2b;
    color: #c7d0d8;
    padding: 28px 0;
}

.footer {
    display: flex;
    justify-content: space-between;
    gap: 20px;
}

.footer-brand {
    color: white;
    font-weight: 800;
    font-size: 20px;
}

.footer-data {
    text-align: right;
    font-size: 12px;
    line-height: 1.7;
}

@media(max-width:850px) {

    .hero-grid {
        grid-template-columns: 1fr;
    }

    .steps {
        grid-template-columns: 1fr;
    }

}

@media(max-width:600px) {

    .card {
        padding: 27px 22px;
    }

    .sic-grid {
        grid-template-columns: 1fr;
    }

    .actions {
        flex-direction: column;
    }

    .button {
        width: 100%;
    }

    .footer {
        flex-direction: column;
    }

    .footer-data {
        text-align: left;
    }

}

</style>

</head>

<body>

<header>

<div class="wrap nav">

<a
    href="/"
    class="logo"
>
Fix<span>Tude</span>
</a>

<a
    href="/privato/login"
    class="nav-login"
>
Accedi
</a>

</div>

</header>


<main>

<section class="hero">

<div class="wrap hero-grid">


<!-- SINISTRA -->

<div class="card">

<div class="eyebrow">
Servizio FixTude
</div>

<h1>
Metti ordine nella tua situazione debitoria.
</h1>

<p class="lead">

Inserisci i tuoi dati e la documentazione
disponibile. FixTude analizza il quadro
complessivo e ti aiuta a individuare
possibili strade da approfondire.

</p>

<div class="points">

<div class="point">
Un unico spazio per raccogliere debiti,
entrate, spese e documenti.
</div>

<div class="point">
Un'analisi organizzata della tua
situazione economica.
</div>

<div class="point">
Possibili scenari da valutare,
senza promettere risultati garantiti.
</div>

</div>

<div class="actions">

<a
    class="button primary"
    href="/registrazione"
>
Registrati gratuitamente
</a>

<a
    class="button secondary"
    href="/privato/login"
>
Accedi
</a>

</div>

<div class="note">

La registrazione non comporta alcun acquisto.
Eventuali servizi a pagamento saranno
indicati chiaramente prima dell'acquisto.

</div>

</div>


<!-- DESTRA -->

<div class="card free">

<div class="free-badge">
Servizio gratuito
</div>

<h2>
Controlla da solo le tue banche dati
</h2>

<p>

Puoi richiedere direttamente agli enti ufficiali
i dati che ti riguardano nelle principali banche
dati creditizie e nella CAI.

</p>

<div class="sic-grid">


<a
    class="sic"
    href="https://www.modulorichiesta.crif.com/"
    target="_blank"
    rel="noopener"
>

<div class="thumb">
CR
</div>

<strong>
CRIF
</strong>

<small>
Modulo ufficiale per l'accesso
ai dati del SIC.
</small>

<span class="sic-link">
Modulo ufficiale →
</span>

</a>


<a
    class="sic"
    href="https://www.experian.it/content/dam/noindex/emea/italy/Nuovo-modulo-SIC.pdf"
    target="_blank"
    rel="noopener"
>

<div class="thumb">
EX
</div>

<strong>
Experian
</strong>

<small>
Modulo ufficiale da compilare,
firmare e inviare.
</small>

<span class="sic-link">
Scarica il PDF →
</span>

</a>


<a
    class="sic"
    href="https://consumatore.ctconline.it/sic/apri-istanza"
    target="_blank"
    rel="noopener"
>

<div class="thumb">
CTC
</div>

<strong>
CTC
</strong>

<small>
Procedura ufficiale di accesso
ai dati del SIC.
</small>

<span class="sic-link">
Modulo / istruzioni →
</span>

</a>


<a
    class="sic"
    href="https://www.bancaditalia.it/servizi-cittadino/servizi/accesso-cai/Modulo-di-richiesta-dei-dati-nominativi-CAI.pdf?force_download=1"
    target="_blank"
    rel="noopener"
>

<div class="thumb">
CAI
</div>

<strong>
CAI
</strong>

<small>
Richiesta ufficiale dei dati
nominativi della CAI.
</small>

<span class="sic-link">
Scarica il PDF →
</span>

</a>

</div>

<div class="free-note">

I collegamenti portano ai siti o ai documenti
ufficiali dei rispettivi enti. La richiesta
è personale e gratuita secondo le modalità
previste dagli enti.

</div>

</div>

</div>

</section>


<section class="how">

<div class="wrap">

<div class="how-title">

<h2>
Come funziona FixTude
</h2>

<p>
Un percorso semplice per capire meglio
la tua situazione.
</p>

</div>

<div class="steps">

<div class="step">

<div class="step-number">
1
</div>

<h3>
Registrati
</h3>

<p>
Crea il tuo account e accedi
alla tua area personale.
</p>

</div>

<div class="step">

<div class="step-number">
2
</div>

<h3>
Inserisci i dati
</h3>

<p>
Raccogli debiti, entrate,
spese e documenti.
</p>

</div>

<div class="step">

<div class="step-number">
3
</div>

<h3>
Analizza
</h3>

<p>
FixTude organizza le informazioni
e individua possibili scenari
da approfondire.
</p>

</div>

</div>

</div>

</section>

</main>


<footer>

<div class="wrap footer">

<div class="footer-brand">
FixTude
</div>

<div class="footer-data">

© 2026 FixTude<br>

<a
    href="mailto:info@fixtude.it"
    style="color:#f5a268"
>
info@fixtude.it
</a>

<br>

P. IVA: DA INSERIRE

</div>

</div>

</footer>

</body>

</html>
"""


# ============================================================
# REGISTRAZIONE
# ============================================================

REGISTRATION_HTML = """
<!DOCTYPE html>

<html lang="it">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>
FixTude - Registrazione
</title>

<style>

body {
    margin: 0;
    background: #f5f7fa;
    font-family: Arial, sans-serif;
    color: #18212b;
}

.box {
    width: min(
        460px,
        92%
    );
    margin:
        70px auto;
    background: white;
    padding: 35px;
    border-radius: 20px;
    border:
        1px solid #e1e6eb;
    box-shadow:
        0 15px 40px
        rgba(20,35,55,.08);
}

.logo {
    font-size: 30px;
    font-weight: 800;
    margin-bottom: 25px;
}

.logo span {
    color: #f47b20;
}

h1 {
    margin-bottom: 8px;
}

.muted {
    color: #687581;
    line-height: 1.5;
}

label {
    display: block;
    margin:
        17px 0 7px;
    font-size: 13px;
    font-weight: 700;
}

input {
    width: 100%;
    padding: 13px;
    box-sizing: border-box;
    border:
        1px solid #d5dce2;
    border-radius: 9px;
    font-size: 15px;
}

button {
    width: 100%;
    margin-top: 22px;
    padding: 14px;
    border: 0;
    border-radius: 9px;
    background: #f47b20;
    color: white;
    font-size: 15px;
    font-weight: 800;
}

.error {
    background: #fff0f0;
    color: #a52b2b;
    padding: 12px;
    border-radius: 8px;
    margin-top: 15px;
    font-size: 13px;
}

a {
    color: #e96813;
    font-weight: 700;
}

</style>

</head>

<body>

<div class="box">

<div class="logo">
Fix<span>Tude</span>
</div>

<h1>
Crea il tuo account
</h1>

<p class="muted">
Registrati gratuitamente per accedere
alla tua area personale.
</p>

{% if error %}

<div class="error">
{{ error }}
</div>

{% endif %}

<form
    method="POST"
>

<label>
Email
</label>

<input
    type="email"
    name="email"
    required
>

<label>
Password
</label>

<input
    type="password"
    name="password"
    minlength="8"
    required
>

<label>
Conferma password
</label>

<input
    type="password"
    name="confirm_password"
    minlength="8"
    required
>

<button
    type="submit"
>
Crea account
</button>

</form>

<p>
<a href="/">
← Torna alla Home
</a>
</p>

</div>

</body>

</html>
"""


# ============================================================
# LOGIN
# ============================================================

LOGIN_HTML = """
<!DOCTYPE html>

<html lang="it">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>
FixTude - Accesso
</title>

<style>

body {
    margin: 0;
    background: #f5f7fa;
    font-family: Arial, sans-serif;
}

.box {
    width: min(
        420px,
        92%
    );
    margin:
        80px auto;
    background: white;
    padding: 35px;
    border-radius: 20px;
    border:
        1px solid #e1e6eb;
}

.logo {
    font-size: 30px;
    font-weight: 800;
}

.logo span {
    color: #f47b20;
}

h1 {
    margin-top: 25px;
}

label {
    display: block;
    margin:
        18px 0 7px;
    font-weight: 700;
    font-size: 13px;
}

input {
    width: 100%;
    padding: 13px;
    box-sizing: border-box;
    border:
        1px solid #d4dbe2;
    border-radius: 8px;
}

button {
    width: 100%;
    margin-top: 22px;
    padding: 14px;
    border: 0;
    border-radius: 9px;
    background: #f47b20;
    color: white;
    font-weight: 800;
}

.error {
    background: #fff0f0;
    color: #a52b2b;
    padding: 11px;
    border-radius: 8px;
}

a {
    color: #e96813;
}

</style>

</head>

<body>

<div class="box">

<div class="logo">
Fix<span>Tude</span>
</div>

<h1>

{% if role == "debtor" %}
Accesso area personale
{% else %}
Accesso Risolutore
{% endif %}

</h1>

{% if error %}

<p class="error">
{{ error }}
</p>

{% endif %}

<form method="POST">

<label>
Email
</label>

<input
    type="email"
    name="email"
    required
>

<label>
Password
</label>

<input
    type="password"
    name="password"
    required
>

<button type="submit">
Accedi
</button>

</form>

{% if role == "debtor" %}

<p>
Non hai ancora un account?
<a href="/registrazione">
Registrati gratuitamente
</a>
</p>

{% endif %}

<p>
<a href="/">
← Torna alla Home
</a>
</p>

</div>

</body>

</html>
"""


# ============================================================
# DASHBOARD DEBITORE
# ============================================================

DASHBOARD_HTML = """
<!DOCTYPE html>

<html lang="it">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>
FixTude - Area personale
</title>

<style>

body {
    margin: 0;
    font-family: Arial, sans-serif;
    background: #f5f7fa;
    color: #18212b;
}

.wrap {
    width: min(
        1000px,
        92%
    );
    margin: auto;
}

header {
    background: white;
    border-bottom:
        1px solid #e2e7eb;
}

.nav {
    min-height: 72px;
    display: flex;
    justify-content: space-between;
    align-items: center;
}

.logo {
    font-size: 27px;
    font-weight: 800;
}

.logo span {
    color: #f47b20;
}

.logout {
    color: #697581;
}

h1 {
    margin-top: 45px;
}

.card {
    background: white;
    border:
        1px solid #e1e6eb;
    border-radius: 16px;
    padding: 25px;
    margin: 18px 0;
}

.buttons {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
}

.button {
    display: inline-block;
    background: #172536;
    color: white;
    padding:
        13px 18px;
    border-radius: 9px;
    text-decoration: none;
    font-weight: 700;
}

.primary {
    background: #f47b20;
}

.small {
    color: #697581;
    font-size: 13px;
}

</style>

</head>

<body>

<header>

<div class="wrap nav">

<div class="logo">
Fix<span>Tude</span>
</div>

<a
    href="/logout"
    class="logout"
>
Esci
</a>

</div>

</header>

<main class="wrap">

<h1>
La tua area personale
</h1>

<div class="card">

<h2>
Benvenuto
</h2>

<p>
Qui puoi inserire e organizzare
la tua situazione economica.
</p>

<div class="buttons">

<a
    class="button primary"
    href="/privato/situazione"
>
Inserisci / modifica situazione
</a>

{% if completed %}

<a
    class="button"
    href="/privato/riepilogo"
>
Riepilogo
</a>

<a
    class="button"
    href="/privato/analisi"
>
Analizza situazione
</a>

<a
    class="button"
    href="/privato/notifiche"
>
Notifiche
</a>

{% endif %}

</div>

</div>

<div class="card">

<h2>
Come funziona
</h2>

<p class="small">

Inserisci entrate, spese e posizioni debitorie.
FixTude organizza i dati e produce una prima
analisi simulativa. Le eventuali proposte
devono essere verificate prima di essere utilizzate.

</p>

</div>

</main>

</body>

</html>
"""


# ============================================================
# SITUAZIONE
# ============================================================

SITUATION_HTML = """
<!DOCTYPE html>

<html lang="it">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>
FixTude - La tua situazione
</title>

<style>

body {
    margin: 0;
    background: #f5f7fa;
    font-family: Arial, sans-serif;
    color: #18212b;
}

.wrap {
    width: min(
        900px,
        92%
    );
    margin: auto;
    padding:
        35px 0;
}

.card {
    background: white;
    border:
        1px solid #e1e6eb;
    border-radius: 15px;
    padding: 25px;
    margin:
        18px 0;
}

.row {
    display: grid;
    grid-template-columns:
        1fr 1fr;
    gap: 15px;
}

label {
    display: block;
    margin:
        14px 0 6px;
    font-weight: 700;
    font-size: 13px;
}

input {
    width: 100%;
    padding: 12px;
    box-sizing: border-box;
    border:
        1px solid #d4dbe2;
    border-radius: 8px;
}

.button {
    background: #f47b20;
    color: white;
    border: 0;
    border-radius: 9px;
    padding:
        14px 20px;
    font-weight: 800;
}

a {
    color: #e96813;
}

@media(max-width:650px) {

    .row {
        grid-template-columns: 1fr;
    }

}

</style>

</head>

<body>

<div class="wrap">

<a href="/privato">
← Area personale
</a>

<h1>
La tua situazione
</h1>

<p>
Inserisci i dati economici disponibili.
</p>

<form
    method="POST"
>

<div class="card">

<h2>
Dati personali
</h2>

<div class="row">

<div>

<label>
Nome
</label>

<input
    name="name"
    value="{{ data.get('name','') }}"
    required
>

</div>

<div>

<label>
Cognome
</label>

<input
    name="surname"
    value="{{ data.get('surname','') }}"
    required
>

</div>

</div>

<label>
Situazione lavorativa
</label>

<input
    name="employment"
    value="{{ data.get('employment','') }}"
    placeholder="Dipendente, pensionato, autonomo..."
>

<label>
Telefono
</label>

<input
    name="phone"
    value="{{ data.get('phone','') }}"
>

<label>
Comune / indirizzo
</label>

<input
    name="address"
    value="{{ data.get('address','') }}"
>

</div>


<div class="card">

<h2>
Entrate mensili
</h2>

{% for i in range(4) %}

<div class="row">

<div>

<label>
Entrata {{ i + 1 }}
</label>

<input
    name="income_label"
    placeholder="Es. stipendio"
>

</div>

<div>

<label>
Importo
</label>

<input
    name="income_amount"
    type="number"
    step="0.01"
    min="0"
>

</div>

</div>

{% endfor %}

</div>


<div class="card">

<h2>
Spese mensili
</h2>

{% for i in range(8) %}

<div class="row">

<div>

<label>
Spesa {{ i + 1 }}
</label>

<input
    name="expense_label"
    placeholder="Es. affitto"
>

</div>

<div>

<label>
Importo
</label>

<input
    name="expense_amount"
    type="number"
    step="0.01"
    min="0"
>

</div>

</div>

{% endfor %}

</div>


<div class="card">

<h2>
Posizioni debitorie
</h2>

{% for i in range(6) %}

<div class="row">

<div>

<label>
Creditore {{ i + 1 }}
</label>

<input
    name="creditor"
    placeholder="Banca / finanziaria / altro"
>

</div>

<div>

<label>
Tipo
</label>

<input
    name="debt_type"
    placeholder="Prestito / carta / altro"
>

</div>

</div>

<div class="row">

<div>

<label>
Debito residuo
</label>

<input
    name="debt_amount"
    type="number"
    step="0.01"
    min="0"
>

</div>

<div>

<label>
Rata mensile
</label>

<input
    name="debt_payment"
    type="number"
    step="0.01"
    min="0"
>

</div>

</div>

{% endfor %}

</div>


<div class="card">

<button
    class="button"
    type="submit"
>
Salva e continua →
</button>

</div>

</form>

</div>

</body>

</html>
"""


# ============================================================
# SUMMARY
# ============================================================

SUMMARY_HTML = """
<!DOCTYPE html>

<html lang="it">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>
FixTude - Riepilogo
</title>

<style>

body {
    margin: 0;
    background: #f5f7fa;
    font-family: Arial, sans-serif;
}

.wrap {
    width: min(
        900px,
        92%
    );
    margin: auto;
    padding: 35px 0;
}

.grid {
    display: grid;
    grid-template-columns:
        repeat(
            auto-fit,
            minmax(
                180px,
                1fr
            )
        );
    gap: 12px;
}

.metric,
.card {
    background: white;
    border:
        1px solid #e1e6eb;
    border-radius: 14px;
    padding: 20px;
}

.card {
    margin-top: 18px;
}

.metric span {
    color: #697581;
    font-size: 13px;
}

.metric strong {
    display: block;
    font-size: 23px;
    margin-top: 8px;
}

.button {
    display: inline-block;
    background: #f47b20;
    color: white;
    padding:
        13px 18px;
    border-radius: 9px;
    text-decoration: none;
    font-weight: 800;
}

.debt {
    border-top:
        1px solid #e4e8ec;
    padding:
        14px 0;
}

</style>

</head>

<body>

<div class="wrap">

<a href="/privato">
← Area personale
</a>

<h1>
Riepilogo della situazione
</h1>

<div class="grid">

<div class="metric">
<span>Entrate mensili</span>
<strong>
€ {{ "%.2f"|format(total_income) }}
</strong>
</div>

<div class="metric">
<span>Spese mensili</span>
<strong>
€ {{ "%.2f"|format(total_expenses) }}
</strong>
</div>

<div class="metric">
<span>Disponibilità teorica</span>
<strong>
€ {{ "%.2f"|format(monthly_capacity) }}
</strong>
</div>

<div class="metric">
<span>Debito complessivo</span>
<strong>
€ {{ "%.2f"|format(total_debt) }}
</strong>
</div>

<div class="metric">
<span>Rate mensili</span>
<strong>
€ {{ "%.2f"|format(total_payments) }}
</strong>
</div>

</div>


<div class="card">

<h2>
Posizioni debitorie
</h2>

{% for debt in debts %}

<div class="debt">

<strong>
{{ debt["creditor"] }}
</strong>

<br>

{{ debt["debt_type"] or "Tipo non indicato" }}

<br>

Debito:
€ {{ "%.2f"|format(debt["current_amount"] or 0) }}

<br>

Rata:
€ {{ "%.2f"|format(debt["monthly_payment"] or 0) }}

</div>

{% else %}

<p>
Nessuna posizione debitoria registrata.
</p>

{% endfor %}

</div>


<div class="card">

<h2>
Prossimo passo
</h2>

<p>
FixTude può ora elaborare una prima analisi
e individuare possibili scenari.
</p>

<a
    class="button"
    href="/privato/analisi"
>
Analizza la situazione →
</a>

</div>

</div>

</body>

</html>
"""


# ============================================================
# ANALYSIS
# ============================================================

ANALYSIS_HTML = """
<!DOCTYPE html>

<html lang="it">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>
FixTude - Analisi
</title>

<style>

body {
    margin: 0;
    background: #f5f7fa;
    font-family: Arial, sans-serif;
    color: #18212b;
}

.wrap {
    width: min(
        950px,
        92%
    );
    margin: auto;
    padding: 35px 0;
}

.card {
    background: white;
    border:
        1px solid #e1e6eb;
    border-radius: 15px;
    padding: 25px;
    margin:
        18px 0;
}

.metric {
    display: inline-block;
    width: 180px;
    margin: 5px;
    background: #f5f7fa;
    padding: 17px;
    border-radius: 10px;
}

.metric strong {
    display: block;
    font-size: 20px;
    margin-top: 7px;
}

.warning {
    background: #fff7e8;
    border-left:
        4px solid #f47b20;
    padding: 12px;
    margin: 8px 0;
}

.solution {
    border:
        1px solid #e1e6eb;
    border-radius: 12px;
    padding: 20px;
    margin:
        12px 0;
}

.small {
    color: #697581;
    font-size: 13px;
}

.button {
    display: inline-block;
    background: #f47b20;
    color: white;
    padding:
        12px 17px;
    border-radius: 8px;
    text-decoration: none;
    font-weight: 700;
}

</style>

</head>

<body>

<div class="wrap">

<a href="/privato">
← Area personale
</a>

<h1>
Analisi FixTude
</h1>

<div class="card">

<h2>
Quadro economico
</h2>

<div class="metric">
Entrate
<strong>
€ {{ "%.2f"|format(calc.total_income) }}
</strong>
</div>

<div class="metric">
Spese
<strong>
€ {{ "%.2f"|format(calc.total_expenses) }}
</strong>
</div>

<div class="metric">
Disponibilità
<strong>
€ {{ "%.2f"|format(calc.monthly_capacity) }}
</strong>
</div>

<div class="metric">
Debito
<strong>
€ {{ "%.2f"|format(calc.total_debt) }}
</strong>
</div>

</div>


<div class="card">

<h2>
Prima valutazione
</h2>

<p>
{{ analysis.summary }}
</p>

{% for warning in analysis.warnings %}

<div class="warning">
{{ warning }}
</div>

{% endfor %}

</div>


<div class="card">

<h2>
Possibili scenari
</h2>

{% for solution in solutions %}

<div class="solution">

<h3>
{{ solution.title }}
</h3>

<p>
{{ solution.content }}
</p>

<p class="small">
Stato:
{{ solution.status }}
</p>

{% if solution.status == "approved" %}

<a
    class="button"
    href="/solution/{{ solution.id }}/download"
>
Scarica documento
</a>

{% else %}

<p class="small">
Il documento deve essere verificato
dal Risolutore prima dell'eventuale invio.
</p>

{% endif %}

</div>

{% endfor %}

</div>


<div class="card">

<p class="small">

FixTude fornisce elaborazioni informative
e simulazioni. Non certifica l'insolvenza,
non sostituisce un professionista e non
garantisce l'accettazione delle proposte
da parte dei creditori.

</p>

</div>

</div>

</body>

</html>
"""


# ============================================================
# RESOLVER DASHBOARD
# ============================================================

RESOLVER_HTML = """
<!DOCTYPE html>

<html lang="it">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>
FixTude - Risolutore
</title>

<style>

body {
    margin: 0;
    background: #f5f7fa;
    font-family: Arial, sans-serif;
}

.wrap {
    width: min(
        1050px,
        92%
    );
    margin: auto;
    padding: 35px 0;
}

.card {
    background: white;
    border:
        1px solid #e1e6eb;
    border-radius: 15px;
    padding: 22px;
    margin:
        12px 0;
}

.button {
    display: inline-block;
    background: #172536;
    color: white;
    padding:
        11px 16px;
    border-radius: 8px;
    text-decoration: none;
}

.logout {
    float: right;
}

</style>

</head>

<body>

<div class="wrap">

<a href="/logout">
Esci
</a>

<h1>
Risolutore FixTude
</h1>

<p>
Pratiche presenti nel sistema.
</p>

{% for case in cases %}

<div class="card">

<h3>
Pratica #{{ case.id }}
</h3>

<p>
Email:
{{ case.email }}
</p>

<p>
Cliente:
{{ case.name }}
</p>

<p>
Aggiornata:
{{ case.updated_at }}
</p>

<a
    class="button"
    href="/risolutore/pratica/{{ case.id }}"
>
Apri pratica
</a>

</div>

{% else %}

<div class="card">
Nessuna pratica presente.
</div>

{% endfor %}

</div>

</body>

</html>
"""


# ============================================================
# RESOLVER CASE
# ============================================================

RESOLVER_CASE_HTML = """
<!DOCTYPE html>

<html lang="it">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>
FixTude - Pratica
</title>

<style>

body {
    margin: 0;
    background: #f5f7fa;
    font-family: Arial, sans-serif;
}

.wrap {
    width: min(
        1050px,
        92%
    );
    margin: auto;
    padding: 35px 0;
}

.card {
    background: white;
    border:
        1px solid #e1e6eb;
    border-radius: 15px;
    padding: 24px;
    margin: 15px 0;
}

.solution {
    border:
        1px solid #e1e6eb;
    border-radius: 12px;
    padding: 20px;
    margin: 12px 0;
}

textarea {
    width: 100%;
    min-height: 220px;
    box-sizing: border-box;
    padding: 12px;
}

button {
    background: #f47b20;
    color: white;
    border: 0;
    border-radius: 8px;
    padding:
        12px 18px;
    font-weight: 700;
}

.approve {
    background: #177245;
}

</style>

</head>

<body>

<div class="wrap">

<a href="/risolutore">
← Dashboard Risolutore
</a>

<h1>
Pratica #{{ case.id }}
</h1>

<div class="card">

<h2>
{{ client_name }}
</h2>

<p>
Email:
{{ case.email }}
</p>

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
Debito:
€ {{ "%.2f"|format(calc.total_debt) }}
</p>

</div>


<div class="card">

<h2>
Analisi
</h2>

{% if analysis %}

<p>
{{ analysis.summary }}
</p>

{% else %}

<p>
L'analisi non è ancora stata eseguita.
</p>

{% endif %}

<form
    method="POST"
    action="/risolutore/pratica/{{ case.id }}/analizza"
>

<button>
Esegui / aggiorna analisi
</button>

</form>

</div>


<div class="card">

<h2>
Scenari generati
</h2>

{% for solution in solutions %}

<div class="solution">

<h3>
{{ solution.title }}
</h3>

<p>
{{ solution.content }}
</p>

<p>
Stato:
<strong>
{{ solution.status }}
</strong>
</p>


{% if solution.status == "pending_review" %}

<form
    method="POST"
    action="/risolutore/soluzione/{{ solution.id }}/correggi"
>

<label>
Correggi il testo se necessario
</label>

<textarea
    name="content"
>{{ solution.content }}</textarea>

<br><br>

<label>
Nota del Risolutore
</label>

<textarea
    name="note"
    style="min-height:100px"
></textarea>

<br><br>

<button>
Salva correzione
</button>

</form>

<br>

<form
    method="POST"
    action="/risolutore/soluzione/{{ solution.id }}/approva"
>

<button
    class="approve"
>
Approva e invia al cliente
</button>

</form>

{% endif %}

</div>

{% else %}

<p>
Nessuno scenario generato.
</p>

{% endfor %}

</div>

</div>

</body>

</html>
"""


# ============================================================
# NOTIFICATIONS
# ============================================================

NOTIFICATIONS_HTML = """
<!DOCTYPE html>

<html lang="it">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>
FixTude - Notifiche
</title>

<style>

body {
    margin: 0;
    background: #f5f7fa;
    font-family: Arial, sans-serif;
}

.wrap {
    width: min(
        800px,
        92%
    );
    margin: auto;
    padding: 35px 0;
}

.card {
    background: white;
    border:
        1px solid #e1e6eb;
    border-radius: 14px;
    padding: 20px;
    margin: 12px 0;
}

.button {
    background: #172536;
    color: white;
    padding:
        11px 16px;
    border-radius: 8px;
    border: 0;
}

</style>

</head>

<body>

<div class="wrap">

<a href="/privato">
← Area personale
</a>

<h1>
Notifiche
</h1>

<form
    method="POST"
    action="/privato/notifiche/lette"
>

<button class="button">
Segna tutte come lette
</button>

</form>

{% for n in notifications %}

<div class="card">

<strong>
{{ n.title }}
</strong>

<p>
{{ n.message }}
</p>

<small>
{{ n.created_at }}
</small>

</div>

{% else %}

<div class="card">
Nessuna notifica.
</div>

{% endfor %}

</div>

</body>

</html>
"""


# ============================================================
# HOME ROUTE
# ============================================================

@app.route("/")
def home():

    return render_template_string(
        HOME_HTML
    )


# ============================================================
# REGISTRATION
# ============================================================

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

        if (
            not email
            or "@"
            not in email
        ):

            error = (
                "Inserisci un indirizzo email valido."
            )

        elif len(password) < 8:

            error = (
                "La password deve contenere almeno 8 caratteri."
            )

        elif password != confirm:

            error = (
                "Le password non coincidono."
            )

        elif find_user(email):

            error = (
                "Questa email è già registrata."
            )

        else:

            conn = db_connect()

            conn.execute(
                """
                INSERT INTO users
                (
                    email,
                    password_hash,
                    role,
                    created_at
                )
                VALUES (?, ?, 'debtor', ?)
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
                "email":
                    email,

                "role":
                    "debtor"
            }

            return redirect(
                url_for(
                    "debtor_dashboard"
                )
            )

    return render_template_string(
        REGISTRATION_HTML,
        error=error
    )


# ============================================================
# DEBTOR LOGIN
# ============================================================

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

        user = find_user(
            email
        )

        valid = bool(
            user
            and user["role"] == "debtor"
            and check_password_hash(
                user["password_hash"],
                password
            )
        )

        if not valid:

            legacy = DEMO_USERS.get(
                email
            )

            valid = bool(
                legacy
                and legacy["password"]
                == password
                and legacy["role"]
                == "debtor"
            )

        if valid:

            session.clear()

            session["user"] = {
                "email":
                    email,

                "role":
                    "debtor"
            }

            return redirect(
                url_for(
                    "debtor_dashboard"
                )
            )

        error = (
            "Email o password non corretti."
        )

    return render_template_string(
        LOGIN_HTML,
        role="debtor",
        error=error
    )


# ============================================================
# RESOLVER LOGIN
# ============================================================

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

        user = find_user(
            email
        )

        valid = bool(
            user
            and user["role"] == "resolver"
            and check_password_hash(
                user["password_hash"],
                password
            )
        )

        if not valid:

            legacy = DEMO_USERS.get(
                email
            )

            valid = bool(
                legacy
                and legacy["password"]
                == password
                and legacy["role"]
                == "resolver"
            )

        if valid:

            session.clear()

            session["user"] = {
                "email":
                    email,

                "role":
                    "resolver"
            }

            return redirect(
                url_for(
                    "resolver_dashboard"
                )
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
            url_for(
                "debtor_login"
            )
        )

    case = get_case_for_email(
        user["email"]
    )

    completed = bool(
        case
    )

    return render_template_string(
        DASHBOARD_HTML,
        completed=completed
    )


# ============================================================
# DEBTOR SITUATION
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
            url_for(
                "debtor_login"
            )
        )

    case = get_case_for_email(
        user["email"]
    )

    existing = (
        get_case_data(
            case["id"]
        )
        if case
        else {}
    )

    existing_debts = (
        get_debts(
            case["id"]
        )
        if case
        else []
    )

    if request.method == "POST":

        data = {

            "name":
                request.form
                .get(
                    "name",
                    ""
                )
                .strip(),

            "surname":
                request.form
                .get(
                    "surname",
                    ""
                )
                .strip(),

            "employment":
                request.form
                .get(
                    "employment",
                    ""
                )
                .strip(),

            "phone":
                request.form
                .get(
                    "phone",
                    ""
                )
                .strip(),

            "address":
                request.form
                .get(
                    "address",
                    ""
                )
                .strip(),

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

            if (
                label.strip()
                or amount.strip()
            ):

                data["incomes"].append({

                    "label":
                        label.strip()
                        or "Entrata",

                    "amount":
                        parse_float(
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

            if (
                label.strip()
                or amount.strip()
            ):

                data["expenses"].append({

                    "label":
                        label.strip()
                        or "Spesa",

                    "amount":
                        parse_float(
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

            cursor = conn.execute(
                """
                INSERT INTO cases
                (
                    email,
                    data,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?)
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

            case_id = (
                cursor.lastrowid
            )

        for creditor, debt_type, amount, payment in zip(
            creditors,
            debt_types,
            debt_amounts,
            debt_payments
        ):

            if (
                creditor.strip()
                or amount.strip()
            ):

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
                    VALUES (?, ?, ?, ?, ?, ?, ?)
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
            url_for(
                "case_summary"
            )
        )

    return render_template_string(
        SITUATION_HTML,
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
            url_for(
                "debtor_login"
            )
        )

    case = get_case_for_email(
        user["email"]
    )

    if not case:

        return redirect(
            url_for(
                "debtor_situation"
            )
        )

    calc = calculate_case(
        case["id"]
    )

    return render_template_string(
        SUMMARY_HTML,

        data=calc["data"],

        debts=calc["debts"],

        total_income=
            calc["total_income"],

        total_expenses=
            calc["total_expenses"],

        monthly_capacity=
            calc["monthly_capacity"],

        total_debt=
            calc["total_debt"],

        total_payments=
            calc["total_payments"]
    )


# ============================================================
# ANALYSIS
# ============================================================

@app.route("/privato/analisi")
def case_analysis():

    user = require_login(
        "debtor"
    )

    if not user:

        return redirect(
            url_for(
                "debtor_login"
            )
        )

    case = get_case_for_email(
        user["email"]
    )

    if not case:

        return redirect(
            url_for(
                "debtor_situation"
            )
        )

    analysis = run_local_agent(
        case["id"]
    )

    conn = db_connect()

    solutions = conn.execute(
        """
        SELECT *
        FROM solution_documents
        WHERE case_id = ?
        ORDER BY id
        """,
        (case["id"],)
    ).fetchall()

    conn.close()

    calc = calculate_case(
        case["id"]
    )

    return render_template_string(
        ANALYSIS_HTML,

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
            url_for(
                "resolver_login"
            )
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

    case_cards = []

    for case in cases:

        data = get_case_data(
            case["id"]
        )

        case_cards.append({

            "id":
                case["id"],

            "email":
                case["email"],

            "name":
                client_name(data),

            "updated_at":
                case["updated_at"]
        })

    return render_template_string(
        RESOLVER_HTML,
        cases=case_cards
    )


# ============================================================
# RESOLVER CASE
# ============================================================

@app.route(
    "/risolutore/pratica/<int:case_id>"
)
def resolver_case(case_id):

    user = require_login(
        "resolver"
    )

    if not user:

        return redirect(
            url_for(
                "resolver_login"
            )
        )

    case = get_case(
        case_id
    )

    if not case:

        abort(404)

    calc = calculate_case(
        case_id
    )

    analysis = None

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

    solutions = conn.execute(
        """
        SELECT *
        FROM solution_documents
        WHERE case_id = ?
        ORDER BY id
        """,
        (case_id,)
    ).fetchall()

    conn.close()

    if row:

        try:

            analysis = json.loads(
                row["analysis_json"]
            )

        except Exception:

            analysis = None

    return render_template_string(
        RESOLVER_CASE_HTML,

        case=case,

        calc=calc,

        analysis=analysis,

        solutions=solutions,

        client_name=
            client_name(
                calc["data"]
            )
    )


# ============================================================
# RESOLVER ANALYSIS
# ============================================================

@app.route(
    "/risolutore/pratica/<int:case_id>/analizza",
    methods=["POST"]
)
def resolver_run_analysis(
    case_id
):

    user = require_login(
        "resolver"
    )

    if not user:

        return redirect(
            url_for(
                "resolver_login"
            )
        )

    if not get_case(
        case_id
    ):

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
def correct_solution(
    solution_id
):

    user = require_login(
        "resolver"
    )

    if not user:

        return redirect(
            url_for(
                "resolver_login"
            )
        )

    corrected = (
        request.form
        .get(
            "content",
            ""
        )
        .strip()
    )

    note = (
        request.form
        .get(
            "note",
            ""
        )
        .strip()
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

    if not corrected:

        corrected = solution[
            "content"
        ]

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
        VALUES (?, ?, ?, ?, ?)
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
            updated_at = ?
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
    conn.close()

    generate_pdf(
        solution_id
    )

    return redirect(
        url_for(
            "resolver_case",
            case_id=solution["case_id"]
        )
    )


# ============================================================
# APPROVE
# ============================================================

@app.route(
    "/risolutore/soluzione/<int:solution_id>/approva",
    methods=["POST"]
)
def approve_solution(
    solution_id
):

    user = require_login(
        "resolver"
    )

    if not user:

        return redirect(
            url_for(
                "resolver_login"
            )
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

    timestamp = now_iso()

    conn.execute(
        """
        UPDATE solution_documents
        SET status = 'approved',
            approved_at = ?,
            sent_at = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
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
            VALUES (?, ?, ?, ?, 'in_app', 0, ?)
            """,
            (
                case["id"],
                case["email"],
                "Nuova comunicazione disponibile",
                (
                    "Il Risolutore FixTude ha "
                    "approvato una nuova proposta: "
                    +
                    solution["title"]
                ),
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
    "/solution/<int:solution_id>/download"
)
def download_solution(
    solution_id
):

    user = current_user()

    if not user:

        return redirect(
            url_for(
                "home"
            )
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

    if (
        user["role"] == "debtor"
        and solution["status"]
        != "approved"
    ):

        abort(403)

    path = solution[
        "pdf_path"
    ]

    if not path or not os.path.exists(
        path
    ):

        path = generate_pdf(
            solution_id
        )

    if not path:

        abort(404)

    return send_file(
        path,
        as_attachment=True,
        download_name=os.path.basename(
            path
        )
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
            url_for(
                "debtor_login"
            )
        )

    conn = db_connect()

    notifications = conn.execute(
        """
        SELECT *
        FROM notifications
        WHERE email = ?
        ORDER BY id DESC
        """,
        (user["email"],)
    ).fetchall()

    conn.close()

    return render_template_string(
        NOTIFICATIONS_HTML,
        notifications=notifications
    )


@app.route(
    "/privato/notifiche/lette",
    methods=["POST"]
)
def mark_notifications_read():

    user = require_login(
        "debtor"
    )

    if not user:

        return redirect(
            url_for(
                "debtor_login"
            )
        )

    conn = db_connect()

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

    return redirect(
        url_for(
            "debtor_notifications"
        )
    )


# ============================================================
# LOGOUT
# ============================================================

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for(
            "home"
        )
    )


# ============================================================
# ERROR
# ============================================================

@app.errorhandler(413)
def too_large(error):

    return (
        "Il file supera il limite consentito.",
        413
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
