from flask import (
    Flask, render_template, request, redirect, url_for,
    session, send_from_directory, abort
)
from werkzeug.utils import secure_filename
import sqlite3
import os
import uuid
import json
from datetime import datetime
from html import escape

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.units import mm


# =========================================================
# FIXTUDE
# =========================================================
#
# Flusso:
#
# DEBITORE
#     ↓
# situazione
#     ↓
# riepilogo
#     ↓
# analisi FixTude
#     ↓
# possibili soluzioni
#     ↓
# PDF
#     ↓
# RISOLUTORE / SUPERVISORE
#     ↓
# correzione
#     ↓
# approvazione
#     ↓
# notifica al debitore
#
# Questa versione utilizza un motore locale/simulato.
# Non utilizza API OpenAI.
#
# =========================================================


app = Flask(__name__)

app.secret_key = "fixtude-demo-secret-key"


DATABASE = "fixtude.db"

UPLOAD_FOLDER = "uploads"
PDF_FOLDER = "generated_pdfs"


os.makedirs(
    UPLOAD_FOLDER,
    exist_ok=True
)

os.makedirs(
    PDF_FOLDER,
    exist_ok=True
)


app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["PDF_FOLDER"] = PDF_FOLDER

app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024


ALLOWED_EXTENSIONS = {
    "pdf",
    "jpg",
    "jpeg",
    "png",
    "doc",
    "docx",
    "xls",
    "xlsx"
}


# =========================================================
# UTENTI DEMO
# =========================================================

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

    connection = sqlite3.connect(
        DATABASE
    )

    connection.row_factory = sqlite3.Row

    return connection


def init_database():

    db = get_db()


    # -----------------------------------------------------
    # CASES
    # -----------------------------------------------------

    db.execute("""
        CREATE TABLE IF NOT EXISTS cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            data TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)


    # -----------------------------------------------------
    # DEBTS
    # -----------------------------------------------------

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


    # -----------------------------------------------------
    # DOCUMENTS
    # -----------------------------------------------------

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


    # -----------------------------------------------------
    # ANALISI AI
    # -----------------------------------------------------

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


    # -----------------------------------------------------
    # DOCUMENTI / SOLUZIONI
    # -----------------------------------------------------

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


    # -----------------------------------------------------
    # SUPERVISIONE / CORREZIONI
    # -----------------------------------------------------

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


    # -----------------------------------------------------
    # NOTIFICHE
    # -----------------------------------------------------

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


    db.commit()

    db.close()


init_database()


# =========================================================
# UTILITY
# =========================================================

def now_iso():

    return datetime.utcnow().isoformat()


def allowed_file(filename):

    return (
        "."
        in filename
        and filename.rsplit(
            ".",
            1
        )[1].lower()
        in ALLOWED_EXTENSIONS
    )


def number(value):

    try:

        if value is None:
            return 0.0

        value = str(
            value
        ).replace(
            ",",
            "."
        )

        return float(
            value
        )

    except (
        ValueError,
        TypeError
    ):

        return 0.0


def euro(value):

    return f"€ {number(value):,.2f}"


# =========================================================
# CASE
# =========================================================

def get_case(email):

    db = get_db()

    row = db.execute(
        """
        SELECT *
        FROM cases
        WHERE email = ?
        """,
        (email,)
    ).fetchone()

    db.close()

    return row


def get_case_by_id(case_id):

    db = get_db()

    row = db.execute(
        """
        SELECT *
        FROM cases
        WHERE id = ?
        """,
        (case_id,)
    ).fetchone()

    db.close()

    return row


def get_case_data(email):

    row = get_case(
        email
    )

    if not row:

        return {}


    try:

        return json.loads(
            row["data"]
        )

    except (
        TypeError,
        json.JSONDecodeError
    ):

        return {}


def save_case(email, data):

    db = get_db()

    timestamp = now_iso()

    serialized = json.dumps(
        data,
        ensure_ascii=False
    )


    existing = db.execute(
        """
        SELECT id
        FROM cases
        WHERE email = ?
        """,
        (email,)
    ).fetchone()


    if existing:

        db.execute(
            """
            UPDATE cases
            SET data = ?,
                updated_at = ?
            WHERE email = ?
            """,
            (
                serialized,
                timestamp,
                email
            )
        )

    else:

        db.execute(
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
                email,
                serialized,
                timestamp,
                timestamp
            )
        )


    db.commit()


    case = db.execute(
        """
        SELECT id
        FROM cases
        WHERE email = ?
        """,
        (email,)
    ).fetchone()


    db.close()


    return case["id"]


# =========================================================
# DEBITI
# =========================================================

def get_debts(case_id):

    db = get_db()

    rows = db.execute(
        """
        SELECT *
        FROM debts
        WHERE case_id = ?
        ORDER BY id
        """,
        (case_id,)
    ).fetchall()

    db.close()

    return rows


def save_debts(case_id, debts):

    db = get_db()


    db.execute(
        """
        DELETE FROM debts
        WHERE case_id = ?
        """,
        (case_id,)
    )


    for debt in debts:

        db.execute(
            """
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
            """,
            (
                case_id,
                debt.get(
                    "creditor",
                    ""
                ),
                debt.get(
                    "debt_type",
                    ""
                ),
                number(
                    debt.get(
                        "original_amount"
                    )
                ),
                number(
                    debt.get(
                        "current_amount"
                    )
                ),
                number(
                    debt.get(
                        "monthly_payment"
                    )
                ),
                debt.get(
                    "status",
                    ""
                ),
                debt.get(
                    "notes",
                    ""
                )
            )
        )


    db.commit()

    db.close()


# =========================================================
# DOCUMENTI
# =========================================================

def get_documents(case_id):

    db = get_db()

    rows = db.execute(
        """
        SELECT *
        FROM documents
        WHERE case_id = ?
        ORDER BY id
        """,
        (case_id,)
    ).fetchall()

    db.close()

    return rows


# =========================================================
# ANALISI
# =========================================================

def get_latest_analysis(case_id):

    db = get_db()

    row = db.execute(
        """
        SELECT *
        FROM ai_analyses
        WHERE case_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (case_id,)
    ).fetchone()

    db.close()

    return row


# =========================================================
# SOLUZIONI
# =========================================================

def get_solution_documents(case_id):

    db = get_db()

    rows = db.execute(
        """
        SELECT *
        FROM solution_documents
        WHERE case_id = ?
        ORDER BY id
        """,
        (case_id,)
    ).fetchall()

    db.close()

    return rows


def get_solution_by_id(solution_id):

    db = get_db()

    row = db.execute(
        """
        SELECT *
        FROM solution_documents
        WHERE id = ?
        """,
        (solution_id,)
    ).fetchone()

    db.close()

    return row


# =========================================================
# NOTIFICHE
# =========================================================

def get_notifications(email):

    db = get_db()

    rows = db.execute(
        """
        SELECT *
        FROM notifications
        WHERE email = ?
        ORDER BY id DESC
        """,
        (email,)
    ).fetchall()

    db.close()

    return rows


# =========================================================
# MOTORE DI ANALISI FIXTUDE
# =========================================================

def calculate_case(case_id):

    case = get_case_by_id(
        case_id
    )

    if not case:

        return None


    try:

        data = json.loads(
            case["data"]
        )

    except (
        TypeError,
        json.JSONDecodeError
    ):

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


    debts = get_debts(
        case_id
    )


    # -----------------------------------------------------
    # CALCOLI
    # -----------------------------------------------------

    total_income = sum(
        number(value)
        for value in income.values()
    )


    total_expenses = sum(
        number(value)
        for value in expenses.values()
    )


    monthly_capacity = (
        total_income
        -
        total_expenses
    )


    total_debt = sum(
        number(
            debt["current_amount"]
        )
        for debt in debts
    )


    total_payments = sum(
        number(
            debt["monthly_payment"]
        )
        for debt in debts
    )


    payment_ratio = (

        total_payments
        /
        total_income
        *
        100

        if total_income > 0
        else 0

    )


    total_assets = sum(
        number(value)
        for value in assets.values()
    )


    # -----------------------------------------------------
    # AVVERTIMENTI
    # -----------------------------------------------------

    warnings = []


    if total_income <= 0:

        warnings.append(
            "Non risultano entrate mensili valorizzate."
        )


    if monthly_capacity < 0:

        warnings.append(
            "Le spese indicate superano "
            "le entrate dichiarate."
        )


    if total_debt <= 0:

        warnings.append(
            "Non risultano importi debitori "
            "sufficientemente dettagliati."
        )


    if (
        total_payments > total_income
        and total_income > 0
    ):

        warnings.append(
            "Le rate indicate superano "
            "le entrate mensili dichiarate."
        )


    if (
        total_income > 0
        and payment_ratio >= 40
    ):

        warnings.append(
            "Il peso delle rate dichiarate "
            "è elevato rispetto alle entrate."
        )


    if not data.get(
        "authorization"
    ):

        warnings.append(
            "Il campo relativo all'autorizzazione "
            "non risulta valorizzato."
        )


    # -----------------------------------------------------
    # SOSTENIBILITÀ
    # -----------------------------------------------------

    if monthly_capacity > 0:

        sustainability = (
            "I dati inseriti mostrano una disponibilità "
            "mensile positiva, da verificare rispetto "
            "alla sostenibilità delle rate e alle "
            "esigenze del nucleo familiare."
        )

    elif monthly_capacity == 0:

        sustainability = (
            "Le entrate risultano sostanzialmente "
            "assorbite dalle spese indicate."
        )

    else:

        sustainability = (
            "Le spese indicate risultano superiori "
            "alle entrate dichiarate. "
            "La situazione richiede un approfondimento."
        )


    # -----------------------------------------------------
    # OGGETTO ANALISI
    # -----------------------------------------------------

    analysis = {

        "case_id": case_id,

        "generated_at": now_iso(),

        "engine": (
            "FixTude Local Analysis Engine v1"
        ),

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
        ),

        "sustainability": sustainability

    }


    return analysis


# =========================================================
# SCENARI POSSIBILI
# =========================================================

def build_scenarios(
    analysis,
    debts
):

    scenarios = []


    capacity = number(
        analysis[
            "monthly_capacity"
        ]
    )

    debt = number(
        analysis[
            "total_debt"
        ]
    )

    payments = number(
        analysis[
            "total_monthly_payments"
        ]
    )

    income = number(
        analysis[
            "total_income"
        ]
    )

    ratio = number(
        analysis[
            "payment_ratio"
        ]
    )


    client = analysis.get(
        "client",
        {}
    )


    client_name = (

        f'{client.get("first_name", "")} '
        f'{client.get("last_name", "")}'

    ).strip()


    if not client_name:

        client_name = "Cliente"


    # -----------------------------------------------------
    # PIANO DI RIENTRO
    # -----------------------------------------------------

    if (
        debt > 0
        and capacity > 0
        and payments <= capacity
    ):

        scenarios.append({

            "type": "piano_rientro",

            "title":
                "Ipotesi di piano di rientro sostenibile",

            "reason":
                (
                    "I dati inseriti evidenziano una "
                    "disponibilità mensile positiva che "
                    "può essere confrontata con le rate "
                    "e con l'esposizione complessiva."
                )

        })


    # -----------------------------------------------------
    # RINEGOZIAZIONE
    # -----------------------------------------------------

    if (
        debt > 0
        and payments > 0
        and (
            capacity < payments
            or ratio >= 40
        )
    ):

        scenarios.append({

            "type": "rinegoziazione",

            "title":
                "Ipotesi di rinegoziazione degli impegni",

            "reason":
                (
                    "Il peso delle rate indicate appare "
                    "significativo rispetto alla capacità "
                    "mensile dichiarata. Può essere utile "
                    "valutare una riduzione della rata o "
                    "una diversa distribuzione dei pagamenti."
                )

        })


    # -----------------------------------------------------
    # TRANSAZIONE
    # -----------------------------------------------------

    if debt > 0:

        scenarios.append({

            "type": "transazione",

            "title":
                "Ipotesi di proposta transattiva",

            "reason":
                (
                    "L'esposizione complessiva può essere "
                    "oggetto di un approfondimento finalizzato "
                    "a valutare una possibile proposta ai creditori."
                )

        })


    # -----------------------------------------------------
    # APPROFONDIMENTO
    # -----------------------------------------------------

    if (
        capacity <= 0
        or ratio >= 50
        or not debts
    ):

        scenarios.append({

            "type": "approfondimento",

            "title":
                "Necessità di approfondimento professionale",

            "reason":
                (
                    "I dati disponibili evidenziano elementi "
                    "che meritano una valutazione più approfondita "
                    "prima di individuare la soluzione concretamente "
                    "perseguibile."
                )

        })


    if not scenarios:

        scenarios.append({

            "type": "approfondimento",

            "title":
                "Raccolta di ulteriori informazioni",

            "reason":
                (
                    "I dati disponibili non sono ancora sufficienti "
                    "per formulare ipotesi operative."
                )

        })


    # Massimo 4 proposte nel MVP.

    scenarios = scenarios[:4]


    # -----------------------------------------------------
    # TESTO DOCUMENTI
    # -----------------------------------------------------

    for scenario in scenarios:

        scenario[
            "draft_text"
        ] = build_solution_text(
            client_name,
            analysis,
            debts,
            scenario
        )


    return scenarios


# =========================================================
# TESTO DELLA SOLUZIONE
# =========================================================

def build_solution_text(
    client_name,
    analysis,
    debts,
    scenario
):

    capacity = euro(
        analysis[
            "monthly_capacity"
        ]
    )

    income = euro(
        analysis[
            "total_income"
        ]
    )

    expenses = euro(
        analysis[
            "total_expenses"
        ]
    )

    debt = euro(
        analysis[
            "total_debt"
        ]
    )

    payments = euro(
        analysis[
            "total_monthly_payments"
        ]
    )

    ratio = (
        f'{analysis["payment_ratio"]:.1f}%'
    )


    debt_lines = []


    for item in debts:

        creditor = (
            item["creditor"]
            or
            "Creditore non indicato"
        )

        amount = euro(
            item["current_amount"]
        )

        payment = euro(
            item["monthly_payment"]
        )


        debt_lines.append(
            f"- {creditor}: "
            f"esposizione indicata {amount}; "
            f"rata indicata {payment}."
        )


    debt_section = (
        "\n".join(
            debt_lines
        )
        or
        "- Nessuna posizione debitoria dettagliata."
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


5. PROSSIMO PASSO

La proposta deve essere verificata e, se necessario,
modificata dal supervisore prima di qualsiasi utilizzo
o comunicazione al cliente.


Il documento rappresenta una possibile linea di
approfondimento costruita esclusivamente sui dati
forniti dall'utente.


6. AVVERTENZA

Il documento non costituisce consulenza legale,
finanziaria o certificazione della situazione di
insolvenza e non determina automaticamente
l'accesso a procedure previste dalla legge.


7. VERIFICA

La versione definitiva deve essere approvata dal
supervisore FixTude prima dell'invio al cliente.
"""


# =========================================================
# GENERAZIONE PDF
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

        parent=styles[
            "BodyText"
        ],

        fontName="Helvetica",

        fontSize=10.5,

        leading=15,

        spaceAfter=7,

        alignment=TA_LEFT

    )


    title_style = ParagraphStyle(

        "FixTudeTitle",

        parent=styles[
            "Title"
        ],

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


        safe = escape(
            line
        )


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
            or
            line.startswith(
                (
                    "1.",
                    "2.",
                    "3.",
                    "4.",
                    "5.",
                    "6.",
                    "7."
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


    document.build(
        story
    )


    return filename


# =========================================================
# RIGENERA PDF
# =========================================================

def regenerate_solution_pdf(
    solution_id
):

    db = get_db()


    solution = db.execute(
        """
        SELECT *
        FROM solution_documents
        WHERE id = ?
        """,
        (solution_id,)
    ).fetchone()


    if not solution:

        db.close()

        return None


    # Elimina vecchio PDF
    if solution["pdf_filename"]:

        old_path = os.path.join(

            app.config[
                "PDF_FOLDER"
            ],

            solution[
                "pdf_filename"
            ]

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


    filename = create_pdf(

        solution_id,

        solution["title"],

        solution["draft_text"]

    )


    db.execute(
        """
        UPDATE solution_documents
        SET pdf_filename = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            filename,
            now_iso(),
            solution_id
        )
    )


    db.commit()

    db.close()


    return filename


# =========================================================
# AGENT ORCHESTRATOR
# =========================================================

def run_local_agent(
    case_id
):

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


    analysis[
        "scenario_count"
    ] = len(
        scenarios
    )


    analysis[
        "scenarios"
    ] = [

        {
            "type":
                item["type"],

            "title":
                item["title"],

            "reason":
                item["reason"]
        }

        for item in scenarios

    ]


    db = get_db()

    timestamp = now_iso()


    # -----------------------------------------------------
    # SALVA ANALISI
    # -----------------------------------------------------

    db.execute(
        """
        INSERT INTO ai_analyses
        (
            case_id,
            status,
            analysis_json,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            case_id,
            "review",
            json.dumps(
                analysis,
                ensure_ascii=False
            ),
            timestamp,
            timestamp
        )
    )


    analysis_id = db.execute(
        """
        SELECT last_insert_rowid()
        """
    ).fetchone()[0]


    # -----------------------------------------------------
    # RIMUOVE SOLO LE BOZZE PRECEDENTI
    # -----------------------------------------------------

    old_drafts = db.execute(
        """
        SELECT id, pdf_filename
        FROM solution_documents
        WHERE case_id = ?
        AND status IN
        (
            'pending_review',
            'draft'
        )
        """,
        (case_id,)
    ).fetchall()


    for old in old_drafts:

        if old["pdf_filename"]:

            old_path = os.path.join(

                app.config[
                    "PDF_FOLDER"
                ],

                old[
                    "pdf_filename"
                ]

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


    db.execute(
        """
        DELETE FROM solution_documents
        WHERE case_id = ?
        AND status IN
        (
            'pending_review',
            'draft'
        )
        """,
        (case_id,)
    )


    db.commit()


    # -----------------------------------------------------
    # CREA SOLUZIONI
    # -----------------------------------------------------

    created_ids = []


    for scenario in scenarios:

        cursor = db.execute(
            """
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
            VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                case_id,
                analysis_id,
                scenario["type"],
                scenario["title"],
                scenario["draft_text"],
                scenario["draft_text"],
                "pending_review",
                timestamp,
                timestamp
            )
        )


        created_ids.append(
            cursor.lastrowid
        )


    db.commit()

    db.close()


    # -----------------------------------------------------
    # GENERA PDF
    # -----------------------------------------------------

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
# LOGIN DEBITORE
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
            and
            user["password"] == password
            and
            user["role"] == "debtor"

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


# =========================================================
# LOGIN RISOLUTORE
# =========================================================

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
            and
            user["password"] == password
            and
            user["role"] == "resolver"

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
# DASHBOARD DEBITORE
# =========================================================

@app.route(
    "/privato"
)
def debtor_dashboard():

    if session.get(
        "role"
    ) != "debtor":

        return redirect(
            url_for(
                "debtor_login"
            )
        )


    email = session.get(
        "email"
    )


    data = get_case_data(
        email
    )


    completed = bool(
        data
    )


    notification_count = 0


    case = get_case(
        email
    )


    if case:

        db = get_db()


        row = db.execute(
            """
            SELECT COUNT(*) AS total
            FROM notifications
            WHERE email = ?
            AND read = 0
            """,
            (email,)
        ).fetchone()


        notification_count = row[
            "total"
        ]


        db.close()


    return render_template(

        "dashboard.html",

        role="debtor",

        name=session.get(
            "name"
        ),

        completed=completed,

        notification_count=
            notification_count

    )


# =========================================================
# DASHBOARD RISOLUTORE
# =========================================================

@app.route(
    "/risolutore"
)
def resolver_dashboard():

    if session.get(
        "role"
    ) != "resolver":

        return redirect(
            url_for(
                "resolver_login"
            )
        )


    db = get_db()


    cases = db.execute(
        """
        SELECT

            c.id,

            c.email,

            c.data,

            c.updated_at,

            a.status
                AS analysis_status,

            COUNT(s.id)
                AS solution_count

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
        """
    ).fetchall()


    db.close()


    return render_template(

        "resolver_dashboard.html",

        cases=cases,

        name=session.get(
            "name"
        )

    )


# =========================================================
# ROUTE COMPATIBILE CON dashboard.html ESISTENTE
# =========================================================

@app.route(
    "/risolutore/pratica"
)
def resolver_practice():

    if session.get(
        "role"
    ) != "resolver":

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
# NUOVA SITUAZIONE DEBITORE
# =========================================================

@app.route(
    "/privato/situazione",
    methods=["GET", "POST"]
)
def debtor_situation():

    if session.get(
        "role"
    ) != "debtor":

        return redirect(
            url_for(
                "debtor_login"
            )
        )


    email = session.get(
        "email"
    )


    # -----------------------------------------------------
    # GET
    # -----------------------------------------------------

    if request.method == "GET":

        data = get_case_data(
            email
        )


        return render_template(

            "new_situation.html",

            data=data

        )


    # -----------------------------------------------------
    # DATI
    # -----------------------------------------------------

    data = {

        "personal": {

            "first_name":
                request.form.get(
                    "first_name",
                    ""
                ).strip(),

            "last_name":
                request.form.get(
                    "last_name",
                    ""
                ).strip(),

            "tax_code":
                request.form.get(
                    "tax_code",
                    ""
                ).strip().upper(),

            "birth_date":
                request.form.get(
                    "birth_date",
                    ""
                ),

            "address":
                request.form.get(
                    "address",
                    ""
                ).strip(),

            "city":
                request.form.get(
                    "city",
                    ""
                ).strip(),

            "phone":
                request.form.get(
                    "phone",
                    ""
                ).strip(),

            "email":
                email

        },


        "family": {

            "members":
                request.form.get(
                    "family_members",
                    "1"
                ),

            "dependents":
                request.form.get(
                    "dependents",
                    "0"
                ),

            "notes":
                request.form.get(
                    "family_notes",
                    ""
                ).strip()

        },


        "income": {

            "net_monthly":
                number(
                    request.form.get(
                        "net_monthly"
                    )
                ),

            "other_income":
                number(
                    request.form.get(
                        "other_income"
                    )
                ),

            "variable_income":
                number(
                    request.form.get(
                        "variable_income"
                    )
                )

        },


        "expenses": {

            "housing":
                number(
                    request.form.get(
                        "housing"
                    )
                ),

            "utilities":
                number(
                    request.form.get(
                        "utilities"
                    )
                ),

            "food":
                number(
                    request.form.get(
                        "food"
                    )
                ),

            "transport":
                number(
                    request.form.get(
                        "transport"
                    )
                ),

            "family":
                number(
                    request.form.get(
                        "family_expenses"
                    )
                ),

            "other":
                number(
                    request.form.get(
                        "other_expenses"
                    )
                )

        },


        "assets": {

            "bank":
                number(
                    request.form.get(
                        "bank_balance"
                    )
                ),

            "property":
                number(
                    request.form.get(
                        "property_value"
                    )
                ),

            "vehicles":
                number(
                    request.form.get(
                        "vehicles_value"
                    )
                ),

            "other":
                number(
                    request.form.get(
                        "other_assets"
                    )
                )

        },


        "procedures": {

            "status":
                request.form.get(
                    "procedures",
                    ""
                ),

            "details":
                request.form.get(
                    "procedure_details",
                    ""
                ).strip()

        },


        "authorization":
            request.form.get(
                "authorization",
                ""
            ).strip(),


        "privacy":
            bool(
                request.form.get(
                    "privacy"
                )
            )

    }


    # -----------------------------------------------------
    # DEBITI
    # -----------------------------------------------------

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
        len(
            creditors
        )
    ):

        creditor = creditors[
            i
        ].strip()


        if not creditor:

            continue


        debts.append({

            "creditor":
                creditor,

            "debt_type":
                (
                    debt_types[i]
                    if i < len(
                        debt_types
                    )
                    else ""
                ),

            "original_amount":
                (
                    original_amounts[i]
                    if i < len(
                        original_amounts
                    )
                    else 0
                ),

            "current_amount":
                (
                    current_amounts[i]
                    if i < len(
                        current_amounts
                    )
                    else 0
                ),

            "monthly_payment":
                (
                    monthly_payments[i]
                    if i < len(
                        monthly_payments
                    )
                    else 0
                ),

            "status":
                (
                    statuses[i]
                    if i < len(
                        statuses
                    )
                    else ""
                ),

            "notes":
                (
                    notes[i]
                    if i < len(
                        notes
                    )
                    else ""
                )

        })


    # -----------------------------------------------------
    # SALVATAGGIO
    # -----------------------------------------------------

    case_id = save_case(
        email,
        data
    )


    save_debts(
        case_id,
        debts
    )


    # -----------------------------------------------------
    # DOCUMENTI
    # -----------------------------------------------------

    files = request.files.getlist(
        "documents"
    )


    db = get_db()


    for file in files:

        if (
            not file
            or
            not file.filename
        ):

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

            f"{uuid.uuid4().hex}"
            f".{extension}"

        )


        filepath = os.path.join(

            app.config[
                "UPLOAD_FOLDER"
            ],

            stored_name

        )


        file.save(
            filepath
        )


        db.execute(
            """
            INSERT INTO documents
            (
                case_id,
                original_name,
                stored_name,
                uploaded_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                case_id,
                file.filename,
                stored_name,
                now_iso()
            )
        )


    db.commit()

    db.close()


    return redirect(
        url_for(
            "case_summary"
        )
    )


# =========================================================
# RIEPILOGO
# =========================================================

@app.route(
    "/privato/riepilogo"
)
def case_summary():

    if session.get(
        "role"
    ) != "debtor":

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
        total_income
        -
        total_expenses
    )


    total_debt = sum(

        number(
            debt["current_amount"]
        )

        for debt in debts

    )


    total_monthly_payments = sum(

        number(
            debt["monthly_payment"]
        )

        for debt in debts

    )


    return render_template(

        "summary.html",

        data=data,

        debts=debts,

        total_income=
            total_income,

        total_expenses=
            total_expenses,

        monthly_capacity=
            monthly_capacity,

        total_debt=
            total_debt,

        total_monthly_payments=
            total_monthly_payments

    )


# =========================================================
# ANALISI DEBITORE
# =========================================================

@app.route(
    "/privato/analisi"
)
def case_analysis():

    if session.get(
        "role"
    ) != "debtor":

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


    # -----------------------------------------------------
    # QUI PARTE L'AGENTE
    # -----------------------------------------------------

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
            analysis_row[
                "analysis_json"
            ]
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

        total_income=
            analysis[
                "total_income"
            ],

        total_expenses=
            analysis[
                "total_expenses"
            ],

        monthly_capacity=
            analysis[
                "monthly_capacity"
            ],

        total_debt=
            analysis[
                "total_debt"
            ],

        total_payments=
            analysis[
                "total_monthly_payments"
            ],

        sustainability=
            analysis[
                "sustainability"
            ],

        warnings=
            analysis[
                "warnings"
            ],

        scenarios=
            analysis[
                "scenarios"
            ],

        analysis=
            analysis

    )


# =========================================================
# DETTAGLIO PRATICA RISOLUTORE
# =========================================================

@app.route(
    "/risolutore/pratica/<int:case_id>"
)
def resolver_case(
    case_id
):

    if session.get(
        "role"
    ) != "resolver":

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
# RISOLUTORE - RIESGUI ANALISI
# =========================================================

@app.route(
    "/risolutore/pratica/<int:case_id>/analizza",
    methods=["POST"]
)
def resolver_run_analysis(
    case_id
):

    if session.get(
        "role"
    ) != "resolver":

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
# CORREZIONE DELLA SOLUZIONE
# =========================================================

@app.route(
    "/risolutore/soluzione/<int:solution_id>/correggi",
    methods=["POST"]
)
def correct_solution(
    solution_id
):

    if session.get(
        "role"
    ) != "resolver":

        return redirect(
            url_for(
                "resolver_login"
            )
        )


    db = get_db()


    solution = db.execute(
        """
        SELECT *
        FROM solution_documents
        WHERE id = ?
        """,
        (solution_id,)
    ).fetchone()


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


    # -----------------------------------------------------
    # MEMORIZZA LA CORREZIONE
    # -----------------------------------------------------

    db.execute(
        """
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
        """,
        (
            solution_id,

            solution[
                "draft_text"
            ],

            corrected_text,

            correction_note,

            session.get(
                "email"
            ),

            now_iso()

        )
    )


    # -----------------------------------------------------
    # AGGIORNA DOCUMENTO
    # -----------------------------------------------------

    db.execute(
        """
        UPDATE solution_documents

        SET draft_text = ?,
            status = 'pending_review',
            updated_at = ?,
            approved_at = NULL,
            sent_at = NULL

        WHERE id = ?
        """,
        (
            corrected_text,

            now_iso(),

            solution_id

        )
    )


    db.commit()

    db.close()


    # -----------------------------------------------------
    # RIGENERA REALMENTE IL PDF
    # -----------------------------------------------------

    regenerate_solution_pdf(
        solution_id
    )


    solution = get_solution_by_id(
        solution_id
    )


    return redirect(
        url_for(
            "resolver_case",
            case_id=solution[
                "case_id"
            ]
        )
    )


# =========================================================
# APPROVA E INVIA
# =========================================================

@app.route(
    "/risolutore/soluzione/<int:solution_id>/approva",
    methods=["POST"]
)
def approve_solution(
    solution_id
):

    if session.get(
