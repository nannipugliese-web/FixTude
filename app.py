from flask import Flask, request, redirect, url_for, session, render_template, render_template_string, send_file, abort
import sqlite3
import os
import json
from datetime import datetime, timezone
from pathlib import Path

try:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False

from werkzeug.utils import secure_filename


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
# UTILITÀ
# ============================================================

def now_iso():
    return datetime.now(
        timezone.utc
    ).isoformat(timespec="seconds")


def db_connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def table_columns(conn, table):
    rows = conn.execute(
        f"PRAGMA table_info({table})"
    ).fetchall()

    return {
        row[1]
        for row in rows
    }


def ensure_column(
    conn,
    table,
    column,
    definition
):
    if column not in table_columns(conn, table):
        conn.execute(
            f"""
            ALTER TABLE {table}
            ADD COLUMN {column} {definition}
            """
        )


def money(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


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


# ============================================================
# DATABASE
# ============================================================

def init_db():

    conn = db_connect()

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cases (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            email TEXT NOT NULL,

            data TEXT NOT NULL DEFAULT '{}',

            created_at TEXT NOT NULL,

            updated_at TEXT NOT NULL

        )
        """
    )

    ensure_column(
        conn,
        "cases",
        "email",
        "TEXT"
    )

    ensure_column(
        conn,
        "cases",
        "data",
        "TEXT NOT NULL DEFAULT '{}'"
    )

    ensure_column(
        conn,
        "cases",
        "created_at",
        "TEXT"
    )

    ensure_column(
        conn,
        "cases",
        "updated_at",
        "TEXT"
    )


    conn.execute(
        """
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
        """
    )

    ensure_column(
        conn,
        "debts",
        "debt_type",
        "TEXT"
    )

    ensure_column(
        conn,
        "debts",
        "current_amount",
        "REAL DEFAULT 0"
    )

    ensure_column(
        conn,
        "debts",
        "monthly_payment",
        "REAL DEFAULT 0"
    )

    ensure_column(
        conn,
        "debts",
        "notes",
        "TEXT"
    )

    ensure_column(
        conn,
        "debts",
        "created_at",
        "TEXT"
    )


    conn.execute(
        """
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
        """
    )


    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_analyses (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            case_id INTEGER NOT NULL,

            analysis_json TEXT NOT NULL,

            created_at TEXT NOT NULL,

            FOREIGN KEY(case_id)
                REFERENCES cases(id)
                ON DELETE CASCADE

        )
        """
    )


    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS solution_documents (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            case_id INTEGER NOT NULL,

            analysis_id INTEGER,

            title TEXT NOT NULL,

            solution_type TEXT NOT NULL,

            content TEXT NOT NULL,

            pdf_path TEXT,

            status TEXT NOT NULL
                DEFAULT 'pending_review',

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
        """
    )


    conn.execute(
        """
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
        """
    )


    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notifications (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            case_id INTEGER NOT NULL,

            email TEXT NOT NULL,

            title TEXT NOT NULL,

            message TEXT NOT NULL,

            notification_type TEXT NOT NULL
                DEFAULT 'in_app',

            read INTEGER NOT NULL DEFAULT 0,

            created_at TEXT NOT NULL,

            FOREIGN KEY(case_id)
                REFERENCES cases(id)
                ON DELETE CASCADE

        )
        """
    )


    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS push_subscriptions (

            id INTEGER PRIMARY KEY AUTOINCREMENT,

            email TEXT NOT NULL,

            endpoint TEXT NOT NULL,

            subscription_json TEXT NOT NULL,

            created_at TEXT NOT NULL

        )
        """
    )


    conn.commit()
    conn.close()


init_db()


# ============================================================
# SESSIONE
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
        or ""
    ).strip()

    surname = str(
        data.get("surname", "")
        or ""
    ).strip()

    full = (
        f"{name} {surname}"
        .strip()
    )

    return full or "Cliente FixTude"


# ============================================================
# ANALISI ECONOMICA
# ============================================================

def calculate_case(case_id):

    data = get_case_data(case_id)

    debts = get_debts(case_id)

    incomes = (
        data.get("incomes", [])
        or []
    )

    expenses = (
        data.get("expenses", [])
        or []
    )


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
        total_income
        - total_expenses
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
        total_payments > monthly_capacity
        and monthly_capacity > 0
    ):

        warnings.append(
            "Le rate indicate superano la disponibilità teorica mensile."
        )


    if not data.get("employment"):

        warnings.append(
            "La situazione lavorativa non è stata indicata."
        )


    return {

        "total_income":
            round(total_income, 2),

        "total_expenses":
            round(total_expenses, 2),

        "monthly_capacity":
            round(monthly_capacity, 2),

        "total_debt":
            round(total_debt, 2),

        "total_payments":
            round(total_payments, 2),

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

    capacity = calc["monthly_capacity"]

    debt = calc["total_debt"]

    payments = calc["total_payments"]

    scenarios = []


    if debt <= 0:

        return [

            {

                "type":
                    "raccolta_dati",

                "title":
                    "Completamento del quadro",

                "description":
                    (
                        "Prima di formulare una proposta "
                        "economica è necessario valorizzare "
                        "almeno una posizione debitoria."
                    ),

                "estimated_monthly":
                    0,

                "priority":
                    "alta"

            }

        ]


    if capacity > 0:

        sustainable = min(
            capacity * 0.30,
            payments
            if payments > 0
            else capacity * 0.30
        )


        sustainable = max(
            50,
            round(sustainable, 2)
        )


        months = max(
            1,
            round(debt / sustainable)
        )


        scenarios.append(

            {

                "type":
                    "piano_rientro",

                "title":
                    "Piano di rientro sostenibile",

                "description":
                    (
                        "Ipotesi di rata costruita "
                        "partendo dalla disponibilità "
                        "teorica mensile indicata. "
                        "È una simulazione e non una "
                        "proposta vincolante per il creditore."
                    ),

                "estimated_monthly":
                    sustainable,

                "months":
                    months,

                "priority":
                    (
                        "alta"
                        if payments > capacity
                        else "media"
                    )

            }

        )


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
                    target
                    / settlement_monthly
                )
            )


            scenarios.append(

                {

                    "type":
                        "saldo_stralcio",

                    "title":
                        "Ipotesi di definizione transattiva",

                    "description":
                        (
                            "Possibile scenario da approfondire "
                            "con il creditore, subordinato alla "
                            "disponibilità di una somma e "
                            "all'accettazione della controparte."
                        ),

                    "estimated_amount":
                        target,

                    "estimated_monthly":
                        settlement_monthly,

                    "months":
                        months2,

                    "priority":
                        "media"

                }

            )


    scenarios.append(

        {

            "type":
                "rinegoziazione",

            "title":
                "Richiesta di rinegoziazione",

            "description":
                (
                    "Richiesta di riduzione della rata "
                    "o di diversa articolazione dei pagamenti, "
                    "supportata dal quadro economico raccolto."
                ),

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

        }

    )


    if calc["sustainability"] == "critica":

        scenarios.append(

            {

                "type":
                    "approfondimento_professionale",

                "title":
                    "Approfondimento con professionista qualificato",

                "description":
                    (
                        "La sostenibilità corrente risulta critica. "
                        "Il caso merita una valutazione professionale "
                        "prima di assumere impegni economici."
                    ),

                "estimated_monthly":
                    0,

                "priority":
                    "alta"

            }

        )


    return scenarios[:4]


# ============================================================
# TESTO ANALISI
# ============================================================

def build_analysis_text(
    calc,
    scenarios
):

    capacity = calc["monthly_capacity"]

    debt = calc["total_debt"]

    payments = calc["total_payments"]

    sustainability = calc["sustainability"]


    if sustainability == "critica":

        opening = (
            "Il quadro economico presenta una forte tensione: "
            "la disponibilità mensile indicata non appare "
            "sufficiente a sostenere gli impegni rilevati."
        )

    elif sustainability in (
        "debole",
        "sotto pressione"
    ):

        opening = (
            "Il quadro evidenzia una disponibilità mensile "
            "limitata rispetto agli impegni debitori indicati."
        )

    else:

        opening = (
            "Il quadro evidenzia una disponibilità mensile "
            "che consente di ipotizzare alcune modalità "
            "di gestione degli impegni, da verificare "
            "con i creditori."
        )


    lines = [

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
            f"€ {capacity:.2f}."
        ),

        (
            f"Debito complessivo indicato: "
            f"€ {debt:.2f}."
        ),

        (
            f"Rate mensili indicate: "
            f"€ {payments:.2f}."
        ),

        (
            "Le elaborazioni di FixTude hanno valore "
            "informativo e simulativo: non costituiscono "
            "certificazione di insolvenza, parere legale "
            "né garanzia di accettazione da parte dei creditori."
        )

    ]


    if calc["warnings"]:

        lines.append(
            "Elementi da verificare: "
            + " ".join(
                calc["warnings"]
            )
        )


    return "\n\n".join(lines)


# ============================================================
# PDF
# ============================================================

def safe_pdf_text(text):

    text = str(
        text or ""
    )

    replacements = {

        "–": "-",

        "—": "-",

        "’": "'",

        "‘": "'",

        "“": '"',

        "”": '"',

        "→": "->",

        "…": "..."

    }


    for old, new in replacements.items():

        text = text.replace(
            old,
            new
        )


    return text


def generate_solution_pdf(solution_id):

    if not REPORTLAB_AVAILABLE:

        raise RuntimeError(
            "reportlab non installato. "
            "Aggiungere reportlab a requirements.txt."
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

        raise FileNotFoundError(
            "Soluzione non trovata"
        )


    case = conn.execute(
        """
        SELECT *
        FROM cases
        WHERE id = ?
        """,
        (solution["case_id"],)
    ).fetchone()


    conn.close()


    data = get_case_data(
        solution["case_id"]
    )


    path = (
        PDF_DIR
        / f"fixtude_soluzione_{solution_id}.pdf"
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


    heading = ParagraphStyle(
        "FixHeading",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=16,
        spaceBefore=10,
        spaceAfter=8
    )


    doc = SimpleDocTemplate(

        str(path),

        pagesize=A4,

        rightMargin=45,

        leftMargin=45,

        topMargin=45,

        bottomMargin=45

    )


    story = []


    story.append(

        Paragraph(

            safe_pdf_text(
                solution["title"]
            ),

            title_style

        )

    )


    story.append(

        Paragraph(

            (
                "Cliente: "
                + safe_pdf_text(
                    client_name(data)
                )
            ),

            normal

        )

    )


    story.append(

        Paragraph(

            (
                "Data: "
                + datetime.now().strftime(
                    "%d/%m/%Y"
                )
            ),

            normal

        )

    )


    story.append(
        Spacer(1, 8)
    )


    story.append(

        Paragraph(

            "Documento generato da FixTude "
            "per revisione del Risolutore AI.",

            normal

        )

    )


    story.append(
        Spacer(1, 8)
    )


    story.append(

        Paragraph(
            "Proposta / scenario",
            heading
        )

    )


    for paragraph in solution["content"].split("\n"):

        paragraph = paragraph.strip()

        if paragraph:

            story.append(

                Paragraph(

                    safe_pdf_text(
                        paragraph
                    ),

                    normal

                )

            )


    story.append(
        Spacer(1, 10)
    )


    story.append(

        Paragraph(
            "Nota importante",
            heading
        )

    )


    story.append(

        Paragraph(

            (
                "Questo documento contiene una simulazione "
                "elaborata sulla base delle informazioni disponibili. "
                "Non rappresenta una certificazione di insolvenza, "
                "un parere legale o una proposta accettata dal creditore. "
                "Prima di assumere impegni è necessario verificare "
                "dati, documenti e condizioni con i soggetti competenti."
            ),

            normal

        )

    )


    doc.build(story)


    return str(path)


# ============================================================
# AGENTE LOCALE
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


    old_pending = conn.execute(

        """
        SELECT id, pdf_path
        FROM solution_documents
        WHERE case_id = ?
        AND status = 'pending_review'
        """,

        (case_id,)

    ).fetchall()


    for old in old_pending:

        if old["pdf_path"]:

            try:

                Path(
                    old["pdf_path"]
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


    cur = conn.execute(

        """
        INSERT INTO ai_analyses
        (
            case_id,
            analysis_json,
            created_at
        )
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


    solution_ids = []


    for scenario in scenarios:

        title = scenario["title"]


        lines = [

            f"Scenario: {title}.",

            scenario["description"],

            (
                "Priorità di revisione: "
                f"{scenario.get('priority', 'media')}."
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
                    "Durata teorica della simulazione: "
                    f"circa {scenario['months']} mesi."
                )

            )


        lines.append(

            "Il Risolutore AI deve verificare "
            "il contenuto e, se necessario, "
            "correggerlo prima dell'invio al cliente."

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

            VALUES
            (
                ?,
                ?,
                ?,
                ?,
                ?,
                'pending_review',
                ?,
                ?
            )

            """,

            (

                case_id,

                analysis_id,

                title,

                scenario["type"],

                "\n".join(lines),

                created,

                created

            )

        )


        solution_ids.append(
            cur.lastrowid
        )


    conn.commit()
    conn.close()


    for solution_id in solution_ids:

        try:

            pdf_path = generate_solution_pdf(
                solution_id
            )


            conn = db_connect()


            conn.execute(

                """
                UPDATE solution_documents

                SET
                    pdf_path = ?,
                    updated_at = ?

                WHERE id = ?
                """,

                (
                    pdf_path,
                    now_iso(),
                    solution_id
                )

            )


            conn.commit()
            conn.close()


        except Exception as exc:

            conn = db_connect()


            conn.execute(

                """
                UPDATE solution_documents

                SET
                    supervisor_note = ?,
                    updated_at = ?

                WHERE id = ?
                """,

                (
                    f"PDF non generato: {exc}",
                    now_iso(),
                    solution_id
                )

            )


            conn.commit()
            conn.close()


    return analysis_payload


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


# ============================================================
# NOTIFICHE
# ============================================================

def create_notification(
    case_id,
    email,
    title,
    message
):

    conn = db_connect()


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

        VALUES
        (
            ?,
            ?,
            ?,
            ?,
            'in_app',
            0,
            ?
        )
        """,

        (
            case_id,
            email,
            title,
            message,
            now_iso()
        )

    )


    conn.commit()
    conn.close()


def get_notifications_for_email(
    email,
    case_id=None,
    unread_only=False
):

    conn = db_connect()


    sql = """
        SELECT *
        FROM notifications
        WHERE email = ?
    """


    params = [
        email
    ]


    if case_id is not None:

        sql += """
            AND case_id = ?
        """

        params.append(
            case_id
        )


    if unread_only:

        sql += """
            AND read = 0
        """


    sql += """
        ORDER BY id DESC
    """


    rows = conn.execute(
        sql,
        params
    ).fetchall()


    conn.close()


    return rows


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    return render_template(
        "home.html"
    )


# ============================================================
# LOGIN PRIVATO
# ============================================================

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


        user = DEMO_USERS.get(
            email
        )


        if (
            user
            and user["password"] == password
            and user["role"] == "debtor"
        ):

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


    return render_template(

        "login.html",

        role="debtor",

        error=error

    )


# ============================================================
# LOGIN RISOLUTORE
# ============================================================

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


        user = DEMO_USERS.get(
            email
        )


        if (
            user
            and user["password"] == password
            and user["role"] == "resolver"
        ):

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


    return render_template(

        "login.html",

        role="resolver",

        error=error

    )


# ============================================================
# DASHBOARD PRIVATO
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


    completed = bool(case)


    unread = 0


    if case:

        conn = db_connect()


        unread = conn.execute(

            """
            SELECT COUNT(*)
            FROM notifications
            WHERE case_id = ?
            AND read = 0
            """,

            (case["id"],)

        ).fetchone()[0]


        conn.close()


    return render_template(

        "dashboard.html",

        role="debtor",

        completed=completed,

        unread=unread

    )


# ============================================================
# DASHBOARD RISOLUTORE
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


        solutions = get_solutions(
            case["id"]
        )


        pending = sum(

            1

            for s in solutions

            if s["status"]
            == "pending_review"

        )


        sent = sum(

            1

            for s in solutions

            if s["status"]
            == "sent"

        )


        case_cards.append(

            {

                "id":
                    case["id"],

                "name":
                    client_name(data),

                "email":
                    case["email"],

                "updated_at":
                    case["updated_at"],

                "pending":
                    pending,

                "sent":
                    sent

            }

        )


    return render_template_string(

        RESOLVER_DASHBOARD_HTML,

        cases=case_cards

    )


# ============================================================
# NUOVA PRATICA RISOLUTORE
# ============================================================

@app.route("/risolutore/pratica")
def resolver_practice():

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


    case = conn.execute(

        """
        SELECT *
        FROM cases
        ORDER BY id DESC
        LIMIT 1
        """

    ).fetchone()


    conn.close()


    if case:

        return redirect(

            url_for(

                "resolver_case",

                case_id=case["id"]

            )

        )


    return redirect(

        url_for(
            "resolver_dashboard"
        )

    )


# ============================================================
# DETTAGLIO PRATICA RISOLUTORE
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


    data = get_case_data(
        case_id
    )


    calc = calculate_case(
        case_id
    )


    analysis = latest_analysis(
        case_id
    )


    solutions = get_solutions(
        case_id
    )


    documents = get_documents(
        case_id
    )


    return render_template_string(

        RESOLVER_CASE_HTML,

        case=case,

        data=data,

        calc=calc,

        analysis=analysis,

        solutions=solutions,

        documents=documents,

        client_name=client_name(
            data
        )

    )


# ============================================================
# AVVIO ANALISI RISOLUTORE
# ============================================================

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
            url_for(
                "resolver_login"
            )
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
# SITUAZIONE PRIVATO
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
                request.form.get(
                    "name",
                    ""
                ).strip(),

            "surname":
                request.form.get(
                    "surname",
                    ""
                ).strip(),

            "employment":
                request.form.get(
                    "employment",
                    ""
                ).strip(),

            "phone":
                request.form.get(
                    "phone",
                    ""
                ).strip(),

            "address":
                request.form.get(
                    "address",
                    ""
                ).strip(),

            "incomes":
                [],

            "expenses":
                []

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

                data["incomes"].append(

                    {

                        "label":
                            label.strip()
                            or "Entrata",

                        "amount":
                            parse_float(
                                amount
                            )

                    }

                )


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

                data["expenses"].append(

                    {

                        "label":
                            label.strip()
                            or "Spesa",

                        "amount":
                            parse_float(
                                amount
                            )

                    }

                )


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

                SET
                    data = ?,
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


    return render_template(

        "new_situation.html",

        case=case,

        data=existing,

        debts=existing_debts

    )


# ============================================================
# RIEPILOGO
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


    return render_template(

        "summary.html",

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
# ANALISI PRIVATO
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


    analysis = latest_analysis(
        case["id"]
    )


    solutions = get_solutions(
        case["id"]
    )


    if (
        not analysis
        or not solutions
    ):

        analysis = run_local_agent(
            case["id"]
        )


        solutions = get_solutions(
            case["id"]
        )


    calc = calculate_case(
        case["id"]
    )


    notifications = get_notifications_for_email(

        user["email"],

        case["id"],

        unread_only=False

    )


    return render_template_string(

        DEBTOR_ANALYSIS_HTML,

        calc=calc,

        analysis=analysis,

        solutions=solutions,

        notifications=notifications

    )


# ============================================================
# NOTIFICHE PRIVATO
# ============================================================

@app.route("/privato/notifiche")
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


    notifications = get_notifications_for_email(

        user["email"],

        None,

        unread_only=False

    )


    return render_template_string(

        NOTIFICATIONS_HTML,

        notifications=notifications,

        debtor=True

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
# CORREZIONE RISOLUTORE
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
            url_for(
                "resolver_login"
            )
        )


    corrected = request.form.get(
        "content",
        ""
    ).strip()


    note = request.form.get(
        "note",
        ""
    ).strip()


    if not corrected:

        return redirect(
            request.referrer
            or url_for(
                "resolver_dashboard"
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


    original = solution["content"]


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

            original,

            corrected,

            note,

            now_iso()

        )

    )


    conn.execute(

        """
        UPDATE solution_documents

        SET

            content = ?,

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

        generate_solution_pdf(
            solution_id
        )


        conn = db_connect()


        conn.execute(

            """
            UPDATE solution_documents

            SET
                pdf_path = ?,
                updated_at = ?

            WHERE id = ?
            """,

            (

                str(
                    PDF_DIR
                    / f"fixtude_soluzione_{solution_id}.pdf"
                ),

                now_iso(),

                solution_id

            )

        )


        conn.commit()
        conn.close()


    except Exception:

        pass


    return redirect(

        url_for(

            "resolver_case",

            case_id=case_id

        )

    )


# ============================================================
# APPROVAZIONE E INVIO
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


    pdf_path = solution["pdf_path"]


    if (
        not pdf_path
        or not Path(pdf_path).exists()
    ):

        try:

            pdf_path = generate_solution_pdf(
                solution_id
            )

        except Exception:

            pdf_path = None


    timestamp = now_iso()


    conn = db_connect()


    conn.execute(

        """
        UPDATE solution_documents

        SET

            status = 'sent',

            approved_at = ?,

            sent_at = ?,

            updated_at = ?,

            pdf_path = ?

        WHERE id = ?

        """,

        (

            timestamp,

            timestamp,

            timestamp,

            pdf_path,

            solution_id

        )

    )


    conn.commit()
    conn.close()


    create_notification(

        case["id"],

        case["email"],

        "Nuovo documento disponibile",

        (
            "Il Risolutore AI ha validato "
            "e reso disponibile il documento: "
            f"{solution['title']}."
        )

    )


    return redirect(

        url_for(

            "resolver_case",

            case_id=case["id"]

        )

    )


# ============================================================
# DOWNLOAD PDF
# ============================================================

@app.route(
    "/soluzioni/<int:solution_id>/download"
)
def download_solution(solution_id):

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


    case = (

        conn.execute(

            """
            SELECT *
            FROM cases
            WHERE id = ?
            """,

            (solution["case_id"],)

        ).fetchone()

        if solution

        else None

    )


    conn.close()


    if not solution or not case:

        abort(404)


    if (

        user["role"] == "debtor"

        and case["email"]
        != user["email"]

    ):

        abort(403)


    if user["role"] not in (
        "debtor",
        "resolver"
    ):

        abort(403)


    if (

        solution["status"] != "sent"

        and user["role"] == "debtor"

    ):

        abort(403)


    path = solution["pdf_path"]


    if (
        not path
        or not Path(path).exists()
    ):

        try:

            path = generate_solution_pdf(
                solution_id
            )

        except Exception as exc:

            return (
                f"PDF non disponibile: {exc}",
                500
            )


    return send_file(

        path,

        as_attachment=True,

        download_name=Path(
            path
        ).name

    )


# ============================================================
# DOWNLOAD DOCUMENTI CARICATI
# ============================================================

@app.route(
    "/documenti/<int:document_id>/download"
)
def download_uploaded_document(
    document_id
):

    user = current_user()


    if not user:

        return redirect(
            url_for(
                "home"
            )
        )


    conn = db_connect()


    document = conn.execute(

        """
        SELECT *
        FROM documents
        WHERE id = ?
        """,

        (document_id,)

    ).fetchone()


    case = (

        conn.execute(

            """
            SELECT *
            FROM cases
            WHERE id = ?
            """,

            (document["case_id"],)

        ).fetchone()

        if document

        else None

    )


    conn.close()


    if not document or not case:

        abort(404)


    if (

        user["role"] == "debtor"

        and case["email"]
        != user["email"]

    ):

        abort(403)


    path = Path(
        document["stored_path"]
    )


    if not path.exists():

        abort(404)


    return send_file(

        path,

        as_attachment=True,

        download_name=document["filename"]

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
# RISOLUTORE DASHBOARD HTML
# ============================================================

RESOLVER_DASHBOARD_HTML = """

<!DOCTYPE html>

<html lang="it">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1.0"
>

<title>
    FixTude - Risolutore AI
</title>

<style>

body {

    font-family:
        Arial,
        sans-serif;

    background:
        #f7f8fa;

    color:
        #1f2933;

    margin:
        0;

}

.wrap {

    max-width:
        900px;

    margin:
        0 auto;

    padding:
        35px 20px;

}

.top {

    display:
        flex;

    justify-content:
        space-between;

    align-items:
        center;

    margin-bottom:
        35px;

}

.eyebrow {

    font-size:
        12px;

    letter-spacing:
        2px;

    color:
        #697586;

}

.card {

    background:
        white;

    border:
        1px solid #e4e7eb;

    border-radius:
        14px;

    padding:
        24px;

    margin:
        15px 0;

}

.button {

    display:
        inline-block;

    padding:
        12px 16px;

    background:
        #1f2933;

    color:
        white;

    text-decoration:
        none;

    border-radius:
        8px;

}

.muted {

    color:
        #697586;

}

.badge {

    display:
        inline-block;

    padding:
        5px 8px;

    border-radius:
        20px;

    background:
        #eef2f5;

    margin-right:
        5px;

    font-size:
        12px;

}

.logout {

    color:
        #697586;

    text-decoration:
        none;

}

.new {

    margin-bottom:
        25px;

}

</style>

</head>

<body>

<div class="wrap">

<div class="top">

<div>

<div class="eyebrow">
FIXTUDE · RISOLUTORE AI
</div>

<h1>
Pratiche
</h1>

<p class="muted">

Qui supervisioni le analisi e le
proposte generate dall'agente.

</p>

</div>

<a
    class="logout"
    href="{{ url_for('logout') }}"
>
Esci
</a>

</div>


<div class="new">

<a
    class="button"
    href="{{ url_for('resolver_practice') }}"
>
Apri ultima pratica →
</a>

</div>


{% if cases %}

{% for c in cases %}

<div class="card">

<h2>
{{ c.name }}
</h2>

<p class="muted">
{{ c.email }}
</p>

<p>

<span class="badge">
In revisione: {{ c.pending }}
</span>

<span class="badge">
Inviate: {{ c.sent }}
</span>

</p>

<a
    class="button"
    href="{{ url_for('resolver_case', case_id=c.id) }}"
>
Apri pratica →
</a>

</div>

{% endfor %}

{% else %}

<div class="card">

<h2>
Nessuna pratica ancora
</h2>

<p class="muted">

Accedi come privato e inserisci
una prima situazione di prova.

</p>

</div>

{% endif %}

</div>

</body>

</html>

"""


# ============================================================
# RISOLUTORE PRATICA HTML
# ============================================================

RESOLVER_CASE_HTML = """

<!DOCTYPE html>

<html lang="it">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1.0"
>

<title>
FixTude - Pratica
</title>

<style>

body {

    font-family:
        Arial,
        sans-serif;

    background:
        #f7f8fa;

    color:
        #1f2933;

    margin:
        0;

}

.wrap {

    max-width:
        950px;

    margin:
        0 auto;

    padding:
        30px 20px;

}

.top {

    display:
        flex;

    justify-content:
        space-between;

    align-items:
        center;

}

.card {

    background:
        white;

    border:
        1px solid #e4e7eb;

    border-radius:
        14px;

    padding:
        22px;

    margin:
        16px 0;

}

.metric {

    display:
        inline-block;

    vertical-align:
        top;

    width:
        21%;

    min-width:
        150px;

    margin:
        1%;

    background:
        #f7f8fa;

    padding:
        15px;

    border-radius:
        10px;

}

.metric strong {

    display:
        block;

    font-size:
        20px;

    margin-top:
        7px;

}

.button,
button {

    display:
        inline-block;

    padding:
        11px 15px;

    background:
        #1f2933;

    color:
        white;

    text-decoration:
        none;

    border:
        0;

    border-radius:
        8px;

    cursor:
        pointer;

}

.secondary {

    background:
        #eef2f5;

    color:
        #1f2933;

}

.solution {

    border-top:
        1px solid #e4e7eb;

    padding-top:
        20px;

    margin-top:
        20px;

}

.status {

    font-size:
        12px;

    padding:
        5px 8px;

    border-radius:
        20px;

    background:
        #eef2f5;

}

.content {

    width:
        100%;

    min-height:
        170px;

    box-sizing:
        border-box;

    padding:
        12px;

    border:
        1px solid #ccd3da;

    border-radius:
        8px;

    font-family:
        Arial,
        sans-serif;

}

.note {

    width:
        100%;

    box-sizing:
        border-box;

    padding:
        10px;

    margin:
        8px 0;

    border:
        1px solid #ccd3da;

    border-radius:
        8px;

}

.warning {

    background:
        #fff7e6;

    padding:
        12px;

    border-radius:
        8px;

    margin:
        8px 0;

}

.ok {

    background:
        #edf8f0;

    padding:
        12px;

    border-radius:
        8px;

    margin:
        8px 0;

}

.back {

    color:
        #697586;

    text-decoration:
        none;

}

.actions {

    display:
        flex;

    gap:
        8px;

    flex-wrap:
        wrap;

    margin-top:
        10px;

}

</style>

</head>

<body>

<div class="wrap">


<div class="top">

<div>

<div class="eyebrow">
FIXTUDE · RISOLUTORE AI
</div>

<h1>
{{ client_name }}
</h1>

<p>
{{ case.email }}
</p>

</div>

<a
    class="back"
    href="{{ url_for('resolver_dashboard') }}"
>
← Pratiche
</a>

</div>


<div class="card">

<h2>
Quadro economico
</h2>


<div class="metric">

<span>
Entrate
</span>

<strong>
€ {{ '%.2f'|format(calc.total_income) }}
</strong>

</div>


<div class="metric">

<span>
Spese
</span>

<strong>
€ {{ '%.2f'|format(calc.total_expenses) }}
</strong>

</div>


<div class="metric">

<span>
Disponibilità
</span>

<strong>
€ {{ '%.2f'|format(calc.monthly_capacity) }}
</strong>

</div>


<div class="metric">

<span>
Debiti
</span>

<strong>
€ {{ '%.2f'|format(calc.total_debt) }}
</strong>

</div>


<p>

<b>
Sostenibilità:
</b>

{{ calc.sustainability }}

</p>


{% for w in calc.warnings %}

<div class="warning">

{{ w }}

</div>

{% endfor %}

</div>


<div class="card">

<h2>
Agente
</h2>

<p>

L'agente locale genera una prima
analisi simulativa senza consumare API.

</p>

<form
    method="post"
    action="{{ url_for('resolver_run_analysis', case_id=case.id) }}"
>

<button type="submit">

Genera / rigenera analisi e PDF →

</button>

</form>

</div>


{% if analysis %}

<div class="card">

<h2>
Analisi generata
</h2>

<p>
{{ analysis.summary }}
</p>


{% for w in analysis.warnings %}

<div class="warning">

{{ w }}

</div>

{% endfor %}

</div>

{% endif %}


<div class="card">

<h2>
Soluzioni da supervisionare
</h2>


{% if solutions %}


{% for s in solutions %}

<div class="solution">

<h3>

{{ s.title }}

<span class="status">
{{ s.status }}
</span>

</h3>


<p>

<b>
Tipo:
</b>

{{ s.solution_type }}

</p>


<form
    method="post"
    action="{{ url_for('correct_solution', solution_id=s.id) }}"
>

<textarea
    class="content"
    name="content"
>{{ s.content }}</textarea>


<input
    class="note"
    type="text"
    name="note"
    placeholder="Nota del supervisore (facoltativa)"
    value="{{ s.supervisor_note or '' }}"
>


<div class="actions">

<button
    type="submit"
    class="secondary"
>
Salva correzione
</button>


{% if s.pdf_path %}

<a
    class="button secondary"
    href="{{ url_for('download_solution', solution_id=s.id) }}"
>
PDF
</a>

{% endif %}

</div>

</form>


{% if s.status != 'sent' %}

<form
    method="post"
    action="{{ url_for('approve_solution', solution_id=s.id) }}"
    style="margin-top:10px"
>

<button type="submit">

Valida e invia al cliente →

</button>

</form>


{% else %}

<div class="ok">

Documento già validato
e inviato al cliente.

</div>

{% endif %}


</div>

{% endfor %}


{% else %}

<p>

Nessuna soluzione generata.
Avvia l'analisi.

</p>

{% endif %}


</div>


</div>

</body>

</html>

"""


# ============================================================
# ANALISI PRIVATO HTML
# ============================================================

DEBTOR_ANALYSIS_HTML = """

<!DOCTYPE html>

<html lang="it">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1.0"
>

<title>
FixTude - Analisi
</title>

<style>

body {

    font-family:
        Arial,
        sans-serif;

    background:
        #f7f8fa;

    color:
        #1f2933;

    margin:
        0;

}

.wrap {

    max-width:
        900px;

    margin:
        0 auto;

    padding:
        35px 20px;

}

.eyebrow {

    font-size:
        12px;

    letter-spacing:
        2px;

    color:
        #697586;

}

.card {

    background:
        white;

    border:
        1px solid #e4e7eb;

    border-radius:
        14px;

    padding:
        24px;

    margin:
        16px 0;

}

.metrics {

    display:
        grid;

    grid-template-columns:
        repeat(
            auto-fit,
            minmax(
                180px,
                1fr
            )
        );

    gap:
        12px;

}

.metric {

    background:
        #f7f8fa;

    padding:
        16px;

    border-radius:
        10px;

}

.metric strong {

    display:
        block;

    font-size:
        22px;

    margin-top:
        8px;

}

.solution {

    border-top:
        1px solid #e4e7eb;

    padding-top:
        15px;

    margin-top:
        15px;

}

.button {

    display:
        inline-block;

    padding:
        12px 16px;

    background:
        #1f2933;

    color:
        white;

    text-decoration:
        none;

    border-radius:
        8px;

}

.warning {

    background:
        #fff7e6;

    padding:
        12px;

    border-radius:
        8px;

    margin:
        8px 0;

}

.notification {

    background:
        #edf8f0;

    padding:
        12px;

    border-radius:
        8px;

    margin:
        8px 0;

}

.back {

    color:
        #697586;

    text-decoration:
        none;

}

.small {

    color:
        #697586;

    font-size:
        13px;

}

</style>

</head>

<body>

<div class="wrap">


<div
    style="
        display:flex;
        justify-content:space-between
    "
>

<div>

<div class="eyebrow">
IL TUO QUADRO
</div>

<h1>
Abbiamo messo ordine.
</h1>

<p>

Questa è una prima elaborazione
automatica dei dati inseriti.

</p>

</div>


<a
    class="back"
    href="{{ url_for('debtor_dashboard') }}"
>
← Area privata
</a>

</div>


<div class="card">

<div class="metrics">


<div class="metric">

Entrate

<strong>

€ {{ '%.2f'|format(
    calc.total_income
) }}

</strong>

</div>


<div class="metric">

Spese

<strong>

€ {{ '%.2f'|format(
    calc.total_expenses
) }}

</strong>

</div>


<div class="metric">

Disponibilità

<strong>

€ {{ '%.2f'|format(
    calc.monthly_capacity
) }}

</strong>

</div>


<div class="metric">

Debiti

<strong>

€ {{ '%.2f'|format(
    calc.total_debt
) }}

</strong>

</div>


</div>


<p>

<b>
Valutazione preliminare:
</b>

{{ calc.sustainability }}

</p>

</div>


<div class="card">

<h2>
Prima analisi
</h2>


<p>
{{ analysis.summary }}
</p>


{% for w in analysis.warnings %}

<div class="warning">

{{ w }}

</div>

{% endfor %}

</div>


<div class="card">

<h2>
Possibili strade da approfondire
</h2>


{% for s in solutions %}

<div class="solution">

<h3>
{{ s.title }}
</h3>


<p>
{{ s.content }}
</p>


<p class="small">

Stato:
{{ s.status }}

</p>


{% if s.status == 'sent' %}

<a
    class="button"
    href="{{ url_for('download_solution', solution_id=s.id) }}"
>
Apri il documento →
</a>


{% else %}

<p class="small">

Il documento è in revisione
prima dell'eventuale invio.

</p>

{% endif %}


</div>

{% endfor %}

</div>


{% if notifications %}

<div class="card">

<h2>
Notifiche
</h2>


{% for n in notifications %}

<div class="notification">

<b>
{{ n.title }}
</b>

<br>

{{ n.message }}

</div>

{% endfor %}


<a
    class="button"
    href="{{ url_for('debtor_notifications') }}"
>
Apri tutte le notifiche →
</a>

</div>

{% endif %}


<div class="card">

<p class="small">

FixTude fornisce elaborazioni informative
e simulazioni. Non certifica l'insolvenza,
non sostituisce un professionista e non può
garantire l'accettazione delle proposte
da parte dei creditori.

</p>

</div>


</div>

</body>

</html>

"""


# ============================================================
# NOTIFICHE HTML
# ============================================================

NOTIFICATIONS_HTML = """

<!DOCTYPE html>

<html lang="it">

<head>

<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1.0"
>

<title>
FixTude - Notifiche
</title>

<style>

body {

    font-family:
        Arial,
        sans-serif;

    background:
        #f7f8fa;

    color:
        #1f2933;

}

.wrap {

    max-width:
        800px;

    margin:
        0 auto;

    padding:
        35px 20px;

}

.card {

    background:
        #fff;

    border:
        1px solid #e4e7eb;

    border-radius:
        14px;

    padding:
        20px;

    margin:
        12px 0;

}

.button {

    display:
        inline-block;

    background:
        #1f2933;

    color:
        #fff;

    padding:
        11px 15px;

    text-decoration:
        none;

    border-radius:
        8px;

}

</style>

</head>

<body>

<div class="wrap">


<a
    href="{{ url_for('debtor_dashboard') }}"
>
← Area privata
</a>


<h1>
Notifiche
</h1>


<form
    method="post"
    action="{{ url_for('mark_notifications_read') }}"
>

<button
    class="button"
    type="submit"
>
Segna tutte come lette
</button>

</form>


{% for n in notifications %}

<div class="card">

<b>
{{ n.title }}
</b>

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
# AVVIO
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
