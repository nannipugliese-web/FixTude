from flask import (
    Flask,
    request,
    redirect,
    url_for,
    session,
    send_file,
    render_template_string
)

import sqlite3
import os
import json
from datetime import datetime

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle
)
from reportlab.lib import colors


# ============================================================
# CONFIGURAZIONE
# ============================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY",
    "fixtude-secret-key-mvp-2026"
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DB_PATH = os.path.join(
    BASE_DIR,
    "fixtude.db"
)

UPLOAD_DIR = os.path.join(
    BASE_DIR,
    "uploads"
)

PDF_DIR = os.path.join(
    BASE_DIR,
    "generated_pdfs"
)

os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(PDF_DIR, exist_ok=True)


# ============================================================
# DATABASE
# ============================================================

def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def now():
    return datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def init_db():

    db = get_db()

    db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE,
            password TEXT,
            role TEXT
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT,
            surname TEXT,
            email TEXT,
            phone TEXT,
            address TEXT,
            employment TEXT,
            procedure TEXT,
            created_at TEXT,
            updated_at TEXT
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS incomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER,
            description TEXT,
            amount REAL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER,
            description TEXT,
            amount REAL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS debts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER,
            creditor TEXT,
            debt_type TEXT,
            current_amount REAL,
            monthly_payment REAL
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER,
            filename TEXT,
            original_name TEXT,
            created_at TEXT
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS ai_analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER,
            analysis_text TEXT,
            warnings TEXT,
            created_at TEXT
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS solution_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER,
            title TEXT,
            solution_type TEXT,
            content TEXT,
            pdf_path TEXT,
            status TEXT DEFAULT 'pending_review',
            created_at TEXT,
            approved_at TEXT,
            sent_at TEXT
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS supervision (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            solution_id INTEGER,
            original_text TEXT,
            corrected_text TEXT,
            supervisor_note TEXT,
            created_at TEXT
        )
    """)

    db.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER,
            email TEXT,
            title TEXT,
            message TEXT,
            notification_type TEXT,
            read INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)

    # Utenti demo
    users = [
        (
            "demo@fixtude.it",
            "1234",
            "debtor"
        ),
        (
            "pro@fixtude.it",
            "1234",
            "resolver"
        )
    ]

    for email, password, role in users:

        existing = db.execute(
            "SELECT id FROM users WHERE email = ?",
            (email,)
        ).fetchone()

        if not existing:

            db.execute(
                """
                INSERT INTO users
                (email, password, role)
                VALUES (?, ?, ?)
                """,
                (
                    email,
                    password,
                    role
                )
            )

    db.commit()
    db.close()


init_db()


# ============================================================
# UTILITY
# ============================================================

def money(value):

    try:
        return float(value or 0)
    except Exception:
        return 0.0


def current_user():

    user_id = session.get("user_id")

    if not user_id:
        return None

    db = get_db()

    user = db.execute(
        "SELECT * FROM users WHERE id = ?",
        (user_id,)
    ).fetchone()

    db.close()

    return user


def require_login():

    if not session.get("user_id"):
        return redirect(url_for("home"))

    return None


def get_user_case():

    user = current_user()

    if not user:
        return None

    db = get_db()

    case = db.execute(
        """
        SELECT *
        FROM cases
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (user["id"],)
    ).fetchone()

    db.close()

    return case


def calculate_case(case_id):

    db = get_db()

    incomes = db.execute(
        """
        SELECT *
        FROM incomes
        WHERE case_id = ?
        ORDER BY id
        """,
        (case_id,)
    ).fetchall()

    expenses = db.execute(
        """
        SELECT *
        FROM expenses
        WHERE case_id = ?
        ORDER BY id
        """,
        (case_id,)
    ).fetchall()

    debts = db.execute(
        """
        SELECT *
        FROM debts
        WHERE case_id = ?
        ORDER BY id
        """,
        (case_id,)
    ).fetchall()

    db.close()

    total_income = sum(
        money(x["amount"])
        for x in incomes
    )

    total_expenses = sum(
        money(x["amount"])
        for x in expenses
    )

    total_debt = sum(
        money(x["current_amount"])
        for x in debts
    )

    total_payments = sum(
        money(x["monthly_payment"])
        for x in debts
    )

    monthly_capacity = (
        total_income -
        total_expenses -
        total_payments
    )

    available_before_debt = (
        total_income -
        total_expenses
    )

    return {
        "incomes": incomes,
        "expenses": expenses,
        "debts": debts,
        "total_income": total_income,
        "total_expenses": total_expenses,
        "total_debt": total_debt,
        "total_payments": total_payments,
        "monthly_capacity": monthly_capacity,
        "available_before_debt": available_before_debt
    }


# ============================================================
# PAGINA BASE
# ============================================================

BASE_STYLE = """

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    background: #f7f8fa;
    color: #15171a;
    font-family: Arial, Helvetica, sans-serif;
}

.container {
    width: min(1050px, 94%);
    margin: 0 auto;
    padding: 45px 0 80px;
}

h1 {
    font-size: 42px;
    line-height: 1.05;
    margin: 10px 0 20px;
}

h2 {
    font-size: 25px;
    margin: 8px 0 15px;
}

p {
    line-height: 1.6;
}

.eyebrow {
    font-size: 12px;
    letter-spacing: 2px;
    font-weight: bold;
    color: #666;
}

.intro {
    color: #60656b;
    max-width: 700px;
    font-size: 17px;
}

.card {
    background: white;
    border: 1px solid #e1e4e8;
    border-radius: 18px;
    padding: 28px;
    margin: 20px 0;
}

.grid {
    display: grid;
    grid-template-columns: repeat(auto-fit,minmax(220px,1fr));
    gap: 16px;
}

.metric {
    background: white;
    border: 1px solid #e1e4e8;
    border-radius: 16px;
    padding: 22px;
}

.metric span {
    display: block;
    color: #777;
    font-size: 14px;
    margin-bottom: 10px;
}

.metric strong {
    font-size: 26px;
}

label {
    display: block;
    font-weight: bold;
    margin: 17px 0 7px;
}

input,
select,
textarea {
    width: 100%;
    padding: 13px 14px;
    border: 1px solid #ccd1d6;
    border-radius: 9px;
    background: white;
    font-size: 16px;
}

textarea {
    min-height: 160px;
    resize: vertical;
}

button,
.button {
    display: inline-block;
    border: 0;
    border-radius: 10px;
    padding: 14px 20px;
    background: #15171a;
    color: white;
    font-size: 15px;
    text-decoration: none;
    cursor: pointer;
    margin-top: 18px;
}

.button.light {
    background: #eef0f2;
    color: #15171a;
}

.button.green {
    background: #176b42;
}

.button.orange {
    background: #a45c00;
}

.small {
    color: #70757a;
    font-size: 14px;
}

table {
    width: 100%;
    border-collapse: collapse;
    background: white;
    border-radius: 12px;
    overflow: hidden;
}

th,
td {
    padding: 13px;
    border-bottom: 1px solid #e5e7e9;
    text-align: left;
}

th {
    background: #f0f2f4;
}

.warning {
    background: #fff4df;
    border: 1px solid #efd39b;
    padding: 16px;
    border-radius: 12px;
    margin: 10px 0;
}

.success {
    background: #e8f6ee;
    border: 1px solid #b8ddc8;
    padding: 16px;
    border-radius: 12px;
    margin: 10px 0;
}

.nav {
    display: flex;
    justify-content: space-between;
    gap: 15px;
    margin-bottom: 35px;
}

.solution {
    border: 1px solid #ddd;
    border-radius: 16px;
    background: white;
    padding: 24px;
    margin: 18px 0;
}

.status {
    display: inline-block;
    background: #eee;
    padding: 5px 9px;
    border-radius: 20px;
    font-size: 12px;
}

@media(max-width:650px) {
    h1 {
        font-size: 32px;
    }

    .container {
        padding-top: 25px;
    }
}

"""


def page(title, body):

    return f"""
<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} - FixTude</title>
<style>
{BASE_STYLE}
</style>
</head>

<body>

<main class="container">

{body}

</main>

</body>
</html>
"""


# ============================================================
# HOME
# ============================================================

@app.route("/")
def home():

    return page(
        "FixTude",
        """
<div class="eyebrow">FIXTUDE</div>

<h1>
IL TUO<br>
PRIMO PASSO
</h1>

<p class="intro">
Analisi della situazione debitoria ·
Organizzazione dei dati ·
Possibili soluzioni
</p>

<div class="grid">

<div class="card">

<div class="eyebrow">01</div>

<h2>Sono un privato</h2>

<p>
Voglio capire meglio la mia situazione
e le possibilità che posso valutare.
</p>

<a class="button"
href="/privato/login">
Entra nella tua area →
</a>

</div>


<div class="card">

<div class="eyebrow">02</div>

<h2>Sono un risolutore</h2>

<p>
Voglio controllare le pratiche
e verificare le analisi prodotte da FixTude.
</p>

<a class="button"
href="/risolutore/login">
Accedi all'area →
</a>

</div>

</div>
"""
    )


# ============================================================
# LOGIN
# ============================================================

@app.route(
    "/privato/login",
    methods=["GET", "POST"]
)
def debtor_login():

    error = ""

    if request.method == "POST":

        email = request.form.get(
            "email",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )

        db = get_db()

        user = db.execute(
            """
            SELECT *
            FROM users
            WHERE email = ?
            AND password = ?
            AND role = 'debtor'
            """,
            (
                email,
                password
            )
        ).fetchone()

        db.close()

        if user:

            session.clear()

            session["user_id"] = user["id"]
            session["role"] = "debtor"

            return redirect(
                url_for("debtor_dashboard")
            )

        error = "Email o password non corretti."

    return page(
        "Area privata",
        f"""
<div class="eyebrow">AREA PRIVATA</div>

<h1>Partiamo da qui.</h1>

<p class="intro">
Accedi alla tua area FixTude.
</p>

<div class="card">

{f'<div class="warning">{error}</div>' if error else ''}

<form method="POST">

<label>Email</label>

<input
type="email"
name="email"
required
placeholder="La tua email"
>

<label>Password</label>

<input
type="password"
name="password"
required
placeholder="Password"
>

<button type="submit">
Entra nella tua area →
</button>

</form>

</div>

<a href="/" class="button light">
← Torna indietro
</a>
"""
    )


@app.route(
    "/risolutore/login",
    methods=["GET", "POST"]
)
def resolver_login():

    error = ""

    if request.method == "POST":

        email = request.form.get(
            "email",
            ""
        ).strip()

        password = request.form.get(
            "password",
            ""
        )

        db = get_db()

        user = db.execute(
            """
            SELECT *
            FROM users
            WHERE email = ?
            AND password = ?
            AND role = 'resolver'
            """,
            (
                email,
                password
            )
        ).fetchone()

        db.close()

        if user:

            session.clear()

            session["user_id"] = user["id"]
            session["role"] = "resolver"

            return redirect(
                url_for("resolver_dashboard")
            )

        error = "Email o password non corretti."

    return page(
        "Area risolutore",
        f"""
<div class="eyebrow">RISOLUTORE AI · SUPERVISIONE</div>

<h1>
Area di controllo.
</h1>

<p class="intro">
Qui puoi controllare le pratiche,
le analisi e le proposte generate da FixTude.
</p>

<div class="card">

{f'<div class="warning">{error}</div>' if error else ''}

<form method="POST">

<label>Email</label>

<input
type="email"
name="email"
required
placeholder="pro@fixtude.it"
>

<label>Password</label>

<input
type="password"
name="password"
required
placeholder="Password"
>

<button type="submit">
Accedi →
</button>

</form>

</div>
"""
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
# DASHBOARD DEBITORE
# ============================================================

@app.route("/privato")
def debtor_dashboard():

    guard = require_login()

    if guard:
        return guard

    if session.get("role") != "debtor":
        return redirect(
            url_for("home")
        )

    case = get_user_case()

    if case:

        calc = calculate_case(
            case["id"]
        )

        body = f"""

<div class="nav">

<div class="eyebrow">
AREA PRIVATA
</div>

<a class="button light"
href="/logout">
Esci
</a>

</div>

<h1>
La tua situazione è stata raccolta.
</h1>

<p class="intro">
Abbiamo organizzato le informazioni che hai inserito.
Ora puoi vedere il quadro e l'analisi.
</p>

<div class="grid">

<div class="metric">
<span>Entrate mensili</span>
<strong>
€ {calc["total_income"]:,.2f}
</strong>
</div>

<div class="metric">
<span>Spese mensili</span>
<strong>
€ {calc["total_expenses"]:,.2f}
</strong>
</div>

<div class="metric">
<span>Debiti</span>
<strong>
€ {calc["total_debt"]:,.2f}
</strong>
</div>

<div class="metric">
<span>Disponibilità prima delle rate</span>
<strong>
€ {calc["available_before_debt"]:,.2f}
</strong>
</div>

</div>

<div class="card">

<h2>
Cosa vuoi fare?
</h2>

<a class="button"
href="/privato/riepilogo">
Guarda il riepilogo →
</a>

<a class="button light"
href="/privato/analisi">
Vai all'analisi →
</a>

<a class="button light"
href="/privato/notifiche">
Notifiche
</a>

</div>

"""

    else:

        body = """

<div class="eyebrow">
AREA PRIVATA
</div>

<h1>
Ciao.
</h1>

<p class="intro">
Iniziamo costruendo un primo quadro
della tua situazione.
</p>

<div class="card">

<h2>
Facciamo ordine.
</h2>

<p>
Non devi avere già tutti i documenti.
Partiamo dalle informazioni economiche essenziali.
</p>

<a class="button"
href="/privato/situazione">
Inizia →
</a>

</div>

"""

    return page(
        "Area privata",
        body
    )


# ============================================================
# NUOVA SITUAZIONE
# ============================================================

@app.route(
    "/privato/situazione",
    methods=["GET", "POST"]
)
def debtor_situation():

    guard = require_login()

    if guard:
        return guard

    if session.get("role") != "debtor":
        return redirect(
            url_for("home")
        )

    if request.method == "POST":

        # ----------------------------------------------------
        # ANAGRAFICA
        # ----------------------------------------------------

        name = request.form.get(
            "name",
            ""
        ).strip()

        surname = request.form.get(
            "surname",
            ""
        ).strip()

        employment = request.form.get(
            "employment",
            ""
        ).strip()

        phone = request.form.get(
            "phone",
            ""
        ).strip()

        address = request.form.get(
            "address",
            ""
        ).strip()

        procedure = request.form.get(
            "procedure",
            "no"
        )

        user = current_user()

        db = get_db()

        # ----------------------------------------------------
        # CERCA EVENTUALE PRATICA ESISTENTE
        # ----------------------------------------------------

        existing = db.execute(
            """
            SELECT *
            FROM cases
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (user["id"],)
        ).fetchone()

        if existing:

            case_id = existing["id"]

            db.execute(
                """
                UPDATE cases
                SET
                    name = ?,
                    surname = ?,
                    phone = ?,
                    address = ?,
                    employment = ?,
                    procedure = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    surname,
                    phone,
                    address,
                    employment,
                    procedure,
                    now(),
                    case_id
                )
            )

            # Ripartiamo dai dati economici appena inseriti
            db.execute(
                "DELETE FROM incomes WHERE case_id = ?",
                (case_id,)
            )

            db.execute(
                "DELETE FROM expenses WHERE case_id = ?",
                (case_id,)
            )

            db.execute(
                "DELETE FROM debts WHERE case_id = ?",
                (case_id,)
            )

        else:

            cursor = db.execute(
                """
                INSERT INTO cases
                (
                    user_id,
                    name,
                    surname,
                    email,
                    phone,
                    address,
                    employment,
                    procedure,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user["id"],
                    name,
                    surname,
                    user["email"],
                    phone,
                    address,
                    employment,
                    procedure,
                    now(),
                    now()
                )
            )

            case_id = cursor.lastrowid

        # ----------------------------------------------------
        # ENTRATE
        #
        # request.form.getlist è fondamentale:
        # permette di leggere tutte le entrate
        # con lo stesso name.
        # ----------------------------------------------------

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

            label = (label or "").strip()

            value = money(amount)

            if label and value > 0:

                db.execute(
                    """
                    INSERT INTO incomes
                    (
                        case_id,
                        description,
                        amount
                    )
                    VALUES (?, ?, ?)
                    """,
                    (
                        case_id,
                        label,
                        value
                    )
                )

        # ----------------------------------------------------
        # SPESE
        # ----------------------------------------------------

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

            label = (label or "").strip()

            value = money(amount)

            if label and value > 0:

                db.execute(
                    """
                    INSERT INTO expenses
                    (
                        case_id,
                        description,
                        amount
                    )
                    VALUES (?, ?, ?)
                    """,
                    (
                        case_id,
                        label,
                        value
                    )
                )

        # ----------------------------------------------------
        # DEBITI
        # ----------------------------------------------------

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

        max_debts = max(
            len(creditors),
            len(debt_types),
            len(debt_amounts),
            len(debt_payments)
        )

        for i in range(max_debts):

            creditor = (
                creditors[i].strip()
                if i < len(creditors)
                else ""
            )

            debt_type = (
                debt_types[i].strip()
                if i < len(debt_types)
                else ""
            )

            amount = (
                money(debt_amounts[i])
                if i < len(debt_amounts)
                else 0
            )

            payment = (
                money(debt_payments[i])
                if i < len(debt_payments)
                else 0
            )

            if creditor or amount > 0:

                db.execute(
                    """
                    INSERT INTO debts
                    (
                        case_id,
                        creditor,
                        debt_type,
                        current_amount,
                        monthly_payment
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        case_id,
                        creditor or "Creditore non indicato",
                        debt_type,
                        amount,
                        payment
                    )
                )

        db.commit()
        db.close()

        return redirect(
            url_for("case_summary")
        )

    # ========================================================
    # FORM
    # ========================================================

    body = """

<div class="eyebrow">
LA TUA SITUAZIONE
</div>

<h1>
Partiamo dai numeri.
</h1>

<p class="intro">
Inserisci le informazioni che conosci.
Potrai completare il quadro successivamente.
</p>

<form method="POST">

<div class="card">

<div class="eyebrow">01</div>

<h2>Anagrafica</h2>

<label>Nome</label>
<input
name="name"
placeholder="Nome"
>

<label>Cognome</label>
<input
name="surname"
placeholder="Cognome"
>

<label>Situazione lavorativa</label>
<input
name="employment"
placeholder="Es. dipendente, pensionato, autonomo"
>

<label>Telefono</label>
<input
name="phone"
placeholder="Numero di telefono"
>

<label>Comune / indirizzo</label>
<input
name="address"
placeholder="Comune o indirizzo"
>

</div>


<div class="card">

<div class="eyebrow">02</div>

<h2>Entrate mensili</h2>

<p class="small">
Puoi inserire più entrate.
</p>

<label>Entrata 1</label>
<input
name="income_label"
placeholder="Es. stipendio"
>

<label>Importo mensile</label>
<input
type="number"
step="0.01"
min="0"
name="income_amount"
placeholder="€"
>


<label>Entrata 2</label>
<input
name="income_label"
placeholder="Es. pensione, affitto, altro"
>

<label>Importo mensile</label>
<input
type="number"
step="0.01"
min="0"
name="income_amount"
placeholder="€"
>


<label>Entrata 3</label>
<input
name="income_label"
placeholder="Altra entrata"
>

<label>Importo mensile</label>
<input
type="number"
step="0.01"
min="0"
name="income_amount"
placeholder="€"
>

</div>


<div class="card">

<div class="eyebrow">03</div>

<h2>Spese mensili</h2>

<p class="small">
Inserisci le principali uscite.
</p>

<label>Spesa 1</label>
<input
name="expense_label"
placeholder="Es. affitto / mutuo"
>

<label>Importo mensile</label>
<input
type="number"
step="0.01"
min="0"
name="expense_amount"
placeholder="€"
>


<label>Spesa 2</label>
<input
name="expense_label"
placeholder="Es. alimentari"
>

<label>Importo mensile</label>
<input
type="number"
step="0.01"
min="0"
name="expense_amount"
placeholder="€"
>


<label>Spesa 3</label>
<input
name="expense_label"
placeholder="Es. utenze"
>

<label>Importo mensile</label>
<input
type="number"
step="0.01"
min="0"
name="expense_amount"
placeholder="€"
>


<label>Spesa 4</label>
<input
name="expense_label"
placeholder="Altre spese"
>

<label>Importo mensile</label>
<input
type="number"
step="0.01"
min="0"
name="expense_amount"
placeholder="€"
>

</div>


<div class="card">

<div class="eyebrow">04</div>

<h2>Debiti</h2>

<p class="small">
Inserisci le posizioni che conosci.
</p>


<label>Creditore 1</label>
<input
name="creditor"
placeholder="Es. banca, finanziaria, Agenzia Entrate..."
>

<label>Tipo di debito</label>
<input
name="debt_type"
placeholder="Es. prestito, carta, bolletta..."
>

<label>Debito residuo</label>
<input
type="number"
step="0.01"
min="0"
name="debt_amount"
placeholder="€"
>

<label>Rata mensile</label>
<input
type="number"
step="0.01"
min="0"
name="debt_payment"
placeholder="€"
>


<label>Creditore 2</label>
<input
name="creditor"
placeholder="Creditore"
>

<label>Tipo di debito</label>
<input
name="debt_type"
placeholder="Tipo di debito"
>

<label>Debito residuo</label>
<input
type="number"
step="0.01"
min="0"
name="debt_amount"
placeholder="€"
>

<label>Rata mensile</label>
<input
type="number"
step="0.01"
min="0"
name="debt_payment"
placeholder="€"
>


<label>Creditore 3</label>
<input
name="creditor"
placeholder="Creditore"
>

<label>Tipo di debito</label>
<input
name="debt_type"
placeholder="Tipo di debito"
>

<label>Debito residuo</label>
<input
type="number"
step="0.01"
min="0"
name="debt_amount"
placeholder="€"
>

<label>Rata mensile</label>
<input
type="number"
step="0.01"
min="0"
name="debt_payment"
placeholder="€"
>

</div>


<div class="card">

<div class="eyebrow">05</div>

<h2>Procedure in corso</h2>

<label>
<input
type="radio"
name="procedure"
value="no"
checked
>
No, che io sappia
</label>

<label>
<input
type="radio"
name="procedure"
value="yes"
>
Sì
</label>

</div>


<button type="submit">
Continua →
</button>

</form>

"""

    return page(
        "La tua situazione",
        body
    )


# ============================================================
# RIEPILOGO
# ============================================================

@app.route("/privato/riepilogo")
def case_summary():

    guard = require_login()

    if guard:
        return guard

    case = get_user_case()

    if not case:

        return redirect(
            url_for("debtor_situation")
        )

    calc = calculate_case(
        case["id"]
    )

    income_rows = ""

    for x in calc["incomes"]:

        income_rows += f"""
<tr>
<td>{x["description"]}</td>
<td>€ {money(x["amount"]):,.2f}</td>
</tr>
"""

    expense_rows = ""

    for x in calc["expenses"]:

        expense_rows += f"""
<tr>
<td>{x["description"]}</td>
<td>€ {money(x["amount"]):,.2f}</td>
</tr>
"""

    debt_rows = ""

    for x in calc["debts"]:

        debt_rows += f"""
<tr>
<td>{x["creditor"]}</td>
<td>{x["debt_type"] or "-"}</td>
<td>€ {money(x["current_amount"]):,.2f}</td>
<td>€ {money(x["monthly_payment"]):,.2f}</td>
</tr>
"""

    body = f"""

<div class="nav">

<div class="eyebrow">
IL TUO RIEPILOGO
</div>

<a class="button light"
href="/privato">
← Area privata
</a>

</div>

<h1>
Ecco il tuo quadro.
</h1>

<p class="intro">
Questi sono i dati economici che FixTude
ha effettivamente ricevuto e registrato.
</p>


<div class="grid">

<div class="metric">

<span>
Entrate mensili
</span>

<strong>
€ {calc["total_income"]:,.2f}
</strong>

</div>


<div class="metric">

<span>
Spese mensili
</span>

<strong>
€ {calc["total_expenses"]:,.2f}
</strong>

</div>


<div class="metric">

<span>
Disponibilità prima delle rate
</span>

<strong>
€ {calc["available_before_debt"]:,.2f}
</strong>

</div>


<div class="metric">

<span>
Debito complessivo
</span>

<strong>
€ {calc["total_debt"]:,.2f}
</strong>

</div>

</div>


<div class="card">

<h2>
Entrate
</h2>

<table>

<tr>
<th>Descrizione</th>
<th>Importo</th>
</tr>

{income_rows or '''
<tr>
<td colspan="2">
Nessuna entrata inserita.
</td>
</tr>
'''}

</table>

</div>


<div class="card">

<h2>
Spese
</h2>

<table>

<tr>
<th>Descrizione</th>
<th>Importo</th>
</tr>

{expense_rows or '''
<tr>
<td colspan="2">
Nessuna spesa inserita.
</td>
</tr>
'''}

</table>

</div>


<div class="card">

<h2>
Debiti
</h2>

<table>

<tr>
<th>Creditore</th>
<th>Tipo</th>
<th>Debito</th>
<th>Rata</th>
</tr>

{debt_rows or '''
<tr>
<td colspan="4">
Nessuna posizione inserita.
</td>
</tr>
'''}

</table>

</div>


<div class="card">

<h2>
Totale rate mensili
</h2>

<p>
<strong>
€ {calc["total_payments"]:,.2f}
</strong>
</p>

<a
class="button"
href="/privato/analisi"
>
Analizza la situazione →
</a>

</div>

"""

    return page(
        "Riepilogo",
        body
    )


# ============================================================
# ANALISI LOCALE
# ============================================================

def generate_analysis(case_id):

    calc = calculate_case(
        case_id
    )

    income = calc["total_income"]
    expenses = calc["total_expenses"]
    debt = calc["total_debt"]
    payments = calc["total_payments"]
    capacity = calc["monthly_capacity"]

    warnings = []

    if income <= 0:

        warnings.append(
            "Non risultano entrate mensili valorizzate."
        )

    if debt <= 0:

        warnings.append(
            "Non risultano debiti valorizzati."
        )

    if payments > calc["available_before_debt"]:

        warnings.append(
            "Le rate indicate superano la disponibilità "
            "teorica prima delle rate."
        )

    if capacity < 0:

        sustainability = (
            "La situazione indicata mostra "
            "una disponibilità mensile negativa."
        )

    elif capacity < 200:

        sustainability = (
            "La disponibilità residua appare molto limitata."
        )

    elif debt > income * 24:

        sustainability = (
            "Il debito indicato appare elevato "
            "rispetto alle entrate mensili."
        )

    else:

        sustainability = (
            "I dati indicati mostrano una disponibilità "
            "mensile positiva da approfondire."
        )

    analysis_text = f"""
FixTude ha elaborato una prima lettura
dei dati economici inseriti.

Entrate mensili: € {income:,.2f}

Spese mensili: € {expenses:,.2f}

Disponibilità prima delle rate:
€ {calc["available_before_debt"]:,.2f}

Rate mensili indicate:
€ {payments:,.2f}

Disponibilità dopo le rate:
€ {capacity:,.2f}

Debito complessivo:
€ {debt:,.2f}

Prima valutazione:
{sustainability}

Questa analisi è una valutazione organizzativa
e non costituisce una certificazione di insolvenza
né una consulenza legale.
"""

    return (
        calc,
        analysis_text,
        warnings
    )


# ============================================================
# GENERAZIONE SCENARI
# ============================================================

def generate_scenarios(case_id):

    calc = calculate_case(
        case_id
    )

    income = calc["total_income"]
    expenses = calc["total_expenses"]
    debt = calc["total_debt"]
    capacity = calc["monthly_capacity"]
    payments = calc["total_payments"]

    scenarios = []

    # --------------------------------------------------------
    # SCENARIO 1
    # --------------------------------------------------------

    if debt > 0:

        target_payment = max(
            0,
            round(
                min(
                    capacity * 0.70,
                    payments
                ),
                2
            )
        )

        scenarios.append({
            "title": "Ipotesi di piano sostenibile",
            "type": "piano_rientro",
            "content": f"""
Oggetto: proposta di verifica di un piano di rientro.

Sulla base delle informazioni economiche
attualmente disponibili, la disponibilità
teorica mensile risulta pari a € {capacity:,.2f}.

Una prima ipotesi prudenziale potrebbe
prevedere una rata complessiva nell'ordine
di € {target_payment:,.2f} mensili,
da verificare con i singoli creditori.

La proposta definitiva dovrà essere valutata
sulla base della documentazione completa,
della posizione aggiornata e delle condizioni
del creditore.
"""
        })

    # --------------------------------------------------------
    # SCENARIO 2
    # --------------------------------------------------------

    if debt > 0:

        scenarios.append({
            "title": "Ipotesi di negoziazione",
            "type": "negoziazione",
            "content": f"""
Oggetto: richiesta di valutazione della posizione.

Il debitore dichiara una situazione economica
caratterizzata da entrate mensili pari a
€ {income:,.2f}, spese pari a
€ {expenses:,.2f} e debiti complessivi indicati
per € {debt:,.2f}.

Si propone di richiedere al creditore
l'aggiornamento della posizione e la valutazione
di una possibile soluzione transattiva o
di una rimodulazione sostenibile.

La richiesta non costituisce riconoscimento
automatico dell'importo né proposta vincolante.
"""
        })

    # --------------------------------------------------------
    # SCENARIO 3
    # --------------------------------------------------------

    if capacity <= 0:

        scenarios.append({
            "title": "Richiesta di approfondimento della difficoltà economica",
            "type": "difficolta_economica",
            "content": f"""
Oggetto: comunicazione di difficoltà economica.

Dai dati attualmente inseriti emerge una
disponibilità mensile teorica pari a
€ {capacity:,.2f} dopo le rate indicate.

Prima di formulare una proposta economica
è opportuno acquisire documentazione completa,
verificare le singole posizioni e approfondire
la sostenibilità effettiva.

Potrà essere richiesta al creditore la
documentazione aggiornata della posizione.
"""
        })

    return scenarios


# ============================================================
# GENERAZIONE PDF
# ============================================================

def create_pdf(
    solution_id,
    title,
    content,
    case
):

    filename = (
        f"fixtude_solution_{solution_id}.pdf"
    )

    path = os.path.join(
        PDF_DIR,
        filename
    )

    doc = SimpleDocTemplate(
        path,
        pagesize=A4,
        rightMargin=45,
        leftMargin=45,
        topMargin=50,
        bottomMargin=50
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "FixTudeTitle",
        parent=styles["Title"],
        alignment=TA_CENTER,
        fontSize=20,
        leading=25,
        spaceAfter=25
    )

    normal_style = ParagraphStyle(
        "FixTudeNormal",
        parent=styles["BodyText"],
        fontSize=10.5,
        leading=16,
        spaceAfter=10
    )

    story = []

    story.append(
        Paragraph(
            "FIXTUDE",
            title_style
        )
    )

    story.append(
        Paragraph(
            title,
            styles["Heading2"]
        )
    )

    story.append(
        Spacer(1, 15)
    )

    client_name = (
        f'{case["name"] or ""} '
        f'{case["surname"] or ""}'
    ).strip()

    if not client_name:
        client_name = "Cliente"

    story.append(
        Paragraph(
            f"Cliente: {client_name}",
            normal_style
        )
    )

    story.append(
        Paragraph(
            f"Data: {datetime.now().strftime('%d/%m/%Y')}",
            normal_style
        )
    )

    story.append(
        Spacer(1, 15)
    )

    for paragraph in content.split("\n"):

        text = paragraph.strip()

        if not text:
            story.append(
                Spacer(1, 6)
            )
            continue

        safe = (
            text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

        story.append(
            Paragraph(
                safe,
                normal_style
            )
        )

    story.append(
        Spacer(1, 25)
    )

    story.append(
        Paragraph(
            "Documento generato da FixTude. "
            "Il contenuto rappresenta una proposta "
            "di analisi e non costituisce consulenza "
            "legale o certificazione di insolvenza.",
            normal_style
        )
    )

    doc.build(story)

    return path


# ============================================================
# ESECUZIONE AGENTE
# ============================================================

def run_agent(case_id):

    calc, analysis_text, warnings = generate_analysis(
        case_id
    )

    scenarios = generate_scenarios(
        case_id
    )

    db = get_db()

    db.execute(
        """
        INSERT INTO ai_analyses
        (
            case_id,
            analysis_text,
            warnings,
            created_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            case_id,
            analysis_text,
            json.dumps(warnings),
            now()
        )
    )

    # Cancella solamente le proposte ancora
    # in lavorazione.
    old = db.execute(
        """
        SELECT *
        FROM solution_documents
        WHERE case_id = ?
        AND status = 'pending_review'
        """,
        (case_id,)
    ).fetchall()

    for row in old:

        if row["pdf_path"]:

            try:

                if os.path.exists(
                    row["pdf_path"]
                ):
                    os.remove(
                        row["pdf_path"]
                    )

            except Exception:
                pass

        db.execute(
            """
            DELETE FROM solution_documents
            WHERE id = ?
            """,
            (row["id"],)
        )

    db.commit()

    for scenario in scenarios:

        cursor = db.execute(
            """
            INSERT INTO solution_documents
            (
                case_id,
                title,
                solution_type,
                content,
                status,
                created_at
            )
            VALUES (?, ?, ?, ?, 'pending_review', ?)
            """,
            (
                case_id,
                scenario["title"],
                scenario["type"],
                scenario["content"],
                now()
            )
        )

        solution_id = cursor.lastrowid

        case = db.execute(
            """
            SELECT *
            FROM cases
            WHERE id = ?
            """,
            (case_id,)
        ).fetchone()

        pdf_path = create_pdf(
            solution_id,
            scenario["title"],
            scenario["content"],
            case
        )

        db.execute(
            """
            UPDATE solution_documents
            SET pdf_path = ?
            WHERE id = ?
            """,
            (
                pdf_path,
                solution_id
            )
        )

    db.commit()
    db.close()

    return {
        "calc": calc,
        "analysis": analysis_text,
        "warnings": warnings,
        "scenarios": scenarios
    }


# ============================================================
# ANALISI DEBITORE
# ============================================================

@app.route("/privato/analisi")
def case_analysis():

    guard = require_login()

    if guard:
        return guard

    case = get_user_case()

    if not case:

        return redirect(
            url_for("debtor_situation")
        )

    db = get_db()

    latest_analysis = db.execute(
        """
        SELECT *
        FROM ai_analyses
        WHERE case_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (case["id"],)
    ).fetchone()

    solutions = db.execute(
        """
        SELECT *
        FROM solution_documents
        WHERE case_id = ?
        ORDER BY id DESC
        """,
        (case["id"],)
    ).fetchall()

    db.close()

    if not latest_analysis or not solutions:

        result = run_agent(
            case["id"]
        )

        calc = result["calc"]
        analysis_text = result["analysis"]
        warnings = result["warnings"]

    else:

        calc = calculate_case(
            case["id"]
        )

        analysis_text = latest_analysis[
            "analysis_text"
        ]

        try:

            warnings = json.loads(
                latest_analysis["warnings"]
            )

        except Exception:

            warnings = []

    db = get_db()

    solutions = db.execute(
        """
        SELECT *
        FROM solution_documents
        WHERE case_id = ?
        ORDER BY id DESC
        """,
        (case["id"],)
    ).fetchall()

    db.close()

    warning_html = ""

    for warning in warnings:

        warning_html += f"""
<div class="warning">
{warning}
</div>
"""

    solution_html = ""

    for solution in solutions:

        status = solution["status"]

        solution_html += f"""

<div class="solution">

<div class="status">
{status}
</div>

<h2>
{solution["title"]}
</h2>

<p>
{solution["content"].replace(chr(10), "<br>")}
</p>

</div>

"""

    body = f"""

<div class="nav">

<div class="eyebrow">
IL TUO QUADRO
</div>

<a class="button light"
href="/privato">
Area privata
</a>

</div>

<h1>
Abbiamo messo ordine.
</h1>

<p class="intro">
Questa è la prima analisi automatica
dei dati che hai inserito.
</p>


<div class="grid">

<div class="metric">

<span>
Entrate
</span>

<strong>
€ {calc["total_income"]:,.2f}
</strong>

</div>


<div class="metric">

<span>
Spese
</span>

<strong>
€ {calc["total_expenses"]:,.2f}
</strong>

</div>


<div class="metric">

<span>
Rate
</span>

<strong>
€ {calc["total_payments"]:,.2f}
</strong>

</div>


<div class="metric">

<span>
Debito complessivo
</span>

<strong>
€ {calc["total_debt"]:,.2f}
</strong>

</div>

</div>


<div class="card">

<h2>
Prima lettura
</h2>

<p>
{analysis_text.replace(chr(10), "<br>")}
</p>

</div>


{warning_html}


<div class="card">

<h2>
Possibili strade individuate
</h2>

<p class="small">
Le proposte vengono ora sottoposte
alla supervisione del Risolutore AI.
</p>

{solution_html}

</div>


<div class="card">

<p>
<strong>
Cosa succede adesso?
</strong>
</p>

<p>
Le possibili soluzioni vengono inviate
all'area di supervisione. Prima dell'invio
definitivo, il contenuto può essere controllato,
corretto e approvato.
</p>

</div>

"""

    return page(
        "Analisi",
        body
    )


# ============================================================
# DASHBOARD RISOLUTORE
# ============================================================

@app.route("/risolutore")
def resolver_dashboard():

    guard = require_login()

    if guard:
        return guard

    if session.get("role") != "resolver":
        return redirect(
            url_for("home")
        )

    db = get_db()

    cases = db.execute(
        """
        SELECT
            c.*,
            (
                SELECT COUNT(*)
                FROM solution_documents s
                WHERE s.case_id = c.id
                AND s.status = 'pending_review'
            ) AS pending_solutions
        FROM cases c
        ORDER BY c.updated_at DESC
        """
    ).fetchall()

    db.close()

    rows = ""

    for case in cases:

        client = (
            f'{case["name"] or ""} '
            f'{case["surname"] or ""}'
        ).strip()

        if not client:
            client = "Cliente"

        rows += f"""

<tr>

<td>
<strong>
{client}
</strong>
<br>
<span class="small">
{case["email"]}
</span>
</td>

<td>
{case["pending_solutions"]}
</td>

<td>
<a
class="button light"
href="/risolutore/pratica/{case["id"]}"
>
Apri pratica →
</a>
</td>

</tr>

"""

    if not rows:

        rows = """
<tr>
<td colspan="3">
Nessuna pratica disponibile.
</td>
</tr>
"""

    body = f"""

<div class="nav">

<div class="eyebrow">
RISOLUTORE AI · SUPERVISIONE
</div>

<a
class="button light"
href="/logout"
>
Esci
</a>

</div>

<h1>
Pratiche.
</h1>

<p class="intro">
Qui vengono raccolte le pratiche e le proposte
generate automaticamente da FixTude.
</p>

<div class="card">

<table>

<tr>
<th>Cliente</th>
<th>Da verificare</th>
<th></th>
</tr>

{rows}

</table>

</div>

"""

    return page(
        "Risolutore",
        body
    )


# ============================================================
# COMPATIBILITÀ CON IL VECCHIO LINK
# ============================================================

@app.route("/risolutore/pratica")
def resolver_practice():

    return redirect(
        url_for("resolver_dashboard")
    )


# ============================================================
# DETTAGLIO PRATICA
# ============================================================

@app.route(
    "/risolutore/pratica/<int:case_id>"
)
def resolver_case(case_id):

    guard = require_login()

    if guard:
        return guard

    if session.get("role") != "resolver":
        return redirect(
            url_for("home")
        )

    db = get_db()

    case = db.execute(
        """
        SELECT *
        FROM cases
        WHERE id = ?
        """,
        (case_id,)
    ).fetchone()

    db.close()

    if not case:

        return "Pratica non trovata", 404

    calc = calculate_case(
        case_id
    )

    db = get_db()

    analyses = db.execute(
        """
        SELECT *
        FROM ai_analyses
        WHERE case_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (case_id,)
    ).fetchone()

    solutions = db.execute(
        """
        SELECT *
        FROM solution_documents
        WHERE case_id = ?
        ORDER BY id DESC
        """,
        (case_id,)
    ).fetchall()

    db.close()

    client = (
        f'{case["name"] or ""} '
        f'{case["surname"] or ""}'
    ).strip()

    if not client:
        client = "Cliente"

    solution_html = ""

    for solution in solutions:

        actions = ""

        if solution["status"] == "pending_review":

            actions = f"""

<form
method="POST"
action="/risolutore/soluzione/{solution["id"]}/correggi"
>

<label>
Correggi il testo
</label>

<textarea
name="corrected_text"
required
>{solution["content"]}</textarea>

<label>
Nota del supervisore
</label>

<input
name="supervisor_note"
placeholder="Motivo della modifica"
>

<button
type="submit"
class="button orange"
>
Salva correzione
</button>

</form>


<form
method="POST"
action="/risolutore/soluzione/{solution["id"]}/approva"
>

<button
type="submit"
class="button green"
>
Approva e invia al cliente →
</button>

</form>

"""

        else:

            actions = f"""

<div class="success">

Questa proposta è stata validata e
inviata al cliente.

</div>

"""

        solution_html += f"""

<div class="solution">

<div class="status">
{solution["status"]}
</div>

<h2>
{solution["title"]}
</h2>

<p>
{solution["content"].replace(chr(10), "<br>")}
</p>

<a
class="button light"
href="/risolutore/soluzione/{solution["id"]}/pdf"
>
Scarica PDF
</a>

{actions}

</div>

"""

    analysis_html = ""

    if analyses:

        analysis_html = f"""

<div class="card">

<h2>
Analisi automatica
</h2>

<p>
{analyses["analysis_text"].replace(chr(10), "<br>")}
</p>

</div>

"""

    body = f"""

<div class="nav">

<div>

<div class="eyebrow">
PRATICA #{case_id}
</div>

<h1>
{client}
</h1>

</div>

<a
class="button light"
href="/risolutore"
>
← Pratiche
</a>

</div>


<div class="grid">

<div class="metric">

<span>Entrate</span>

<strong>
€ {calc["total_income"]:,.2f}
</strong>

</div>

<div class="metric">

<span>Spese</span>

<strong>
€ {calc["total_expenses"]:,.2f}
</strong>

</div>

<div class="metric">

<span>Rate</span>

<strong>
€ {calc["total_payments"]:,.2f}
</strong>

</div>

<div class="metric">

<span>Debiti</span>

<strong>
€ {calc["total_debt"]:,.2f}
</strong>

</div>

</div>


<div class="card">

<h2>
Riesegui analisi
</h2>

<p>
Il Risolutore AI può elaborare nuovamente
la situazione e produrre nuove proposte.
</p>

<form
method="POST"
action="/risolutore/pratica/{case_id}/analizza"
>

<button type="submit">
Esegui analisi →
</button>

</form>

</div>


{analysis_html}


<div class="card">

<h2>
Proposte generate
</h2>

{solution_html or
'<p>Nessuna proposta ancora generata.</p>'
}

</div>

"""

    return page(
        f"Pratica {case_id}",
        body
    )


# ============================================================
# RIESAME AGENTE DAL RISOLUTORE
# ============================================================

@app.route(
    "/risolutore/pratica/<int:case_id>/analizza",
    methods=["POST"]
)
def resolver_run_analysis(case_id):

    guard = require_login()

    if guard:
        return guard

    if session.get("role") != "resolver":
        return redirect(
            url_for("home")
        )

    run_agent(
        case_id
    )

    return redirect(
        url_for(
            "resolver_case",
            case_id=case_id
        )
    )


# ============================================================
# CORREZIONE PROPOSTA
# ============================================================

@app.route(
    "/risolutore/soluzione/<int:solution_id>/correggi",
    methods=["POST"]
)
def correct_solution(solution_id):

    guard = require_login()

    if guard:
        return guard

    if session.get("role") != "resolver":
        return redirect(
            url_for("home")
        )

    corrected_text = request.form.get(
        "corrected_text",
        ""
    ).strip()

    supervisor_note = request.form.get(
        "supervisor_note",
        ""
    ).strip()

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

        return "Soluzione non trovata", 404

    # Memorizziamo la correzione.
    db.execute(
        """
        INSERT INTO supervision
        (
            solution_id,
            original_text,
            corrected_text,
            supervisor_note,
            created_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            solution_id,
            solution["content"],
            corrected_text,
            supervisor_note,
            now()
        )
    )

    db.execute(
        """
        UPDATE solution_documents
        SET content = ?,
            status = 'pending_review'
        WHERE id = ?
        """,
        (
            corrected_text,
            solution_id
        )
    )

    db.commit()

    case = db.execute(
        """
        SELECT *
        FROM cases
        WHERE id = ?
        """,
        (solution["case_id"],)
    ).fetchone()

    db.close()

    # Rigeneriamo il PDF.
    pdf_path = create_pdf(
        solution_id,
        solution["title"],
        corrected_text,
        case
    )

    db = get_db()

    db.execute(
        """
        UPDATE solution_documents
        SET pdf_path = ?
        WHERE id = ?
        """,
        (
            pdf_path,
            solution_id
        )
    )

    db.commit()
    db.close()

    return redirect(
        url_for(
            "resolver_case",
            case_id=solution["case_id"]
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

    guard = require_login()

    if guard:
        return guard

    if session.get("role") != "resolver":
        return redirect(
            url_for("home")
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

        return "Soluzione non trovata", 404

    case = db.execute(
        """
        SELECT *
        FROM cases
        WHERE id = ?
        """,
        (solution["case_id"],)
    ).fetchone()

    if not case:

        db.close()

        return "Pratica non trovata", 404

    approved_time = now()

    db.execute(
        """
        UPDATE solution_documents
        SET
            status = 'sent',
            approved_at = ?,
            sent_at = ?
        WHERE id = ?
        """,
        (
            approved_time,
            approved_time,
            solution_id
        )
    )

    # --------------------------------------------------------
    # NOTIFICA AL DEBITORE
    # --------------------------------------------------------

    email = case["email"]

    db.execute(
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
        VALUES (?, ?, ?, ?, ?, 0, ?)
        """,
        (
            case["id"],
            email,
            "Nuovo documento disponibile",
            (
                "FixTude ha completato una proposta "
                f"relativa alla tua situazione: "
                f"{solution['title']}."
            ),
            "in_app",
            approved_time
        )
    )

    db.commit()
    db.close()

    return redirect(
        url_for(
            "resolver_case",
            case_id=solution["case_id"]
        )
    )


# ============================================================
# DOWNLOAD PDF
# ============================================================

@app.route(
    "/risolutore/soluzione/<int:solution_id>/pdf"
)
def download_solution(solution_id):

    guard = require_login()

    if guard:
        return guard

    db = get_db()

    solution = db.execute(
        """
        SELECT *
        FROM solution_documents
        WHERE id = ?
        """,
        (solution_id,)
    ).fetchone()

    db.close()

    if not solution:

        return "Documento non trovato", 404

    path = solution["pdf_path"]

    if not path or not os.path.exists(path):

        return "PDF non disponibile", 404

    return send_file(
        path,
        as_attachment=True,
        download_name=(
            f"FixTude_{solution_id}.pdf"
        )
    )


# ============================================================
# NOTIFICHE DEBITORE
# ============================================================

@app.route("/privato/notifiche")
def notifications():

    guard = require_login()

    if guard:
        return guard

    if session.get("role") != "debtor":
        return redirect(
            url_for("home")
        )

    user = current_user()

    db = get_db()

    rows = db.execute(
        """
        SELECT n.*
        FROM notifications n
        JOIN cases c
        ON c.id = n.case_id
        WHERE c.user_id = ?
        ORDER BY n.id DESC
        """,
        (user["id"],)
    ).fetchall()

    db.close()

    html = ""

    for row in rows:

        html += f"""

<div class="card">

<div class="status">
{"LETTA" if row["read"] else "NUOVA"}
</div>

<h2>
{row["title"]}
</h2>

<p>
{row["message"]}
</p>

<span class="small">
{row["created_at"]}
</span>

</div>

"""

    if not html:

        html = """
<div class="card">
<p>
Non ci sono ancora notifiche.
</p>
</div>
"""

    body = f"""

<div class="nav">

<div class="eyebrow">
NOTIFICHE
</div>

<a
class="button light"
href="/privato"
>
← Area privata
</a>

</div>

<h1>
Le tue notifiche.
</h1>

{html}

"""

    # Segna come lette
    db = get_db()

    db.execute(
        """
        UPDATE notifications
        SET read = 1
        WHERE email = ?
        """,
        (user["email"],)
    )

    db.commit()
    db.close()

    return page(
        "Notifiche",
        body
    )


# ============================================================
# HEALTH CHECK RENDER
# ============================================================

@app.route("/health")
def health():

    return {
        "status": "ok",
        "app": "FixTude"
    }


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
