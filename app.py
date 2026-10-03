from flask import Flask, render_template, request, redirect, url_for, session
from werkzeug.utils import secure_filename
import sqlite3
import os
import uuid
import json
from datetime import datetime


app = Flask(__name__)

app.secret_key = "fixtude-demo-secret-key"

DATABASE = "fixtude.db"
UPLOAD_FOLDER = "uploads"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
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


# ---------------------------------------------------------
# DATABASE
# ---------------------------------------------------------

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

    db.commit()

    db.close()


init_database()


# ---------------------------------------------------------
# UTILITY
# ---------------------------------------------------------

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


def get_case(email):

    db = get_db()

    row = db.execute(
        "SELECT * FROM cases WHERE email = ?",
        (email,)
    ).fetchone()

    db.close()

    return row


def get_case_data(email):

    row = get_case(email)

    if not row:
        return {}

    return json.loads(row["data"])


def save_case(email, data):

    db = get_db()

    now = datetime.utcnow().isoformat()

    existing = db.execute(
        "SELECT id FROM cases WHERE email = ?",
        (email,)
    ).fetchone()

    serialized = json.dumps(data, ensure_ascii=False)

    if existing:

        db.execute("""
            UPDATE cases
            SET data = ?, updated_at = ?
            WHERE email = ?
        """, (
            serialized,
            now,
            email
        ))

    else:

        db.execute("""
            INSERT INTO cases
            (email, data, created_at, updated_at)
            VALUES (?, ?, ?, ?)
        """, (
            email,
            serialized,
            now,
            now
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
    """, (case_id,)).fetchall()

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


# ---------------------------------------------------------
# HOME
# ---------------------------------------------------------

@app.route("/")
def home():

    return render_template("home.html")


# ---------------------------------------------------------
# LOGIN PRIVATO
# ---------------------------------------------------------

@app.route("/privato/login", methods=["GET", "POST"])
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

        user = USERS.get(email)

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
                url_for("debtor_dashboard")
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


# ---------------------------------------------------------
# LOGIN RISOLUTORE
# ---------------------------------------------------------

@app.route("/risolutore/login", methods=["GET", "POST"])
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

        user = USERS.get(email)

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
                url_for("resolver_dashboard")
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


# ---------------------------------------------------------
# DASHBOARD PRIVATO
# ---------------------------------------------------------

@app.route("/privato")
def debtor_dashboard():

    if session.get("role") != "debtor":

        return redirect(
            url_for("debtor_login")
        )

    data = get_case_data(
        session.get("email")
    )

    completed = bool(data)

    return render_template(
        "dashboard.html",
        role="debtor",
        name=session.get("name"),
        completed=completed
    )


# ---------------------------------------------------------
# DASHBOARD RISOLUTORE
# ---------------------------------------------------------

@app.route("/risolutore")
def resolver_dashboard():

    if session.get("role") != "resolver":

        return redirect(
            url_for("resolver_login")
        )

    return render_template(
        "dashboard.html",
        role="resolver",
        name=session.get("name"),
        completed=False
    )


# ---------------------------------------------------------
# NUOVA SITUAZIONE PRIVATO
# ---------------------------------------------------------

@app.route(
    "/privato/situazione",
    methods=["GET", "POST"]
)
def debtor_situation():

    if session.get("role") != "debtor":

        return redirect(
            url_for("debtor_login")
        )

    email = session.get("email")

    if request.method == "GET":

        data = get_case_data(email)

        return render_template(
            "new_situation.html",
            data=data
        )

    # -----------------------------------------------------
    # DATI PERSONALI
    # -----------------------------------------------------

    data = {

        "personal": {
            "first_name": request.form.get(
                "first_name", ""
            ).strip(),

            "last_name": request.form.get(
                "last_name", ""
            ).strip(),

            "tax_code": request.form.get(
                "tax_code", ""
            ).strip().upper(),

            "birth_date": request.form.get(
                "birth_date", ""
            ),

            "address": request.form.get(
                "address", ""
            ).strip(),

            "city": request.form.get(
                "city", ""
            ).strip(),

            "phone": request.form.get(
                "phone", ""
            ).strip(),

            "email": email
        },

        # -------------------------------------------------
        # FAMIGLIA
        # -------------------------------------------------

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

        # -------------------------------------------------
        # ENTRATE
        # -------------------------------------------------

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

        # -------------------------------------------------
        # SPESE
        # -------------------------------------------------

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

        # -------------------------------------------------
        # PATRIMONIO
        # -------------------------------------------------

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

        # -------------------------------------------------
        # PROCEDURE
        # -------------------------------------------------

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

        # -------------------------------------------------
        # CONSENSO
        # -------------------------------------------------

        "authorization": request.form.get(
            "authorization",
            ""
        ).strip(),

        "privacy": bool(
            request.form.get("privacy")
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

    for i in range(len(creditors)):

        if not creditors[i].strip():
            continue

        debts.append({

            "creditor": creditors[i].strip(),

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

    # -----------------------------------------------------
    # DOCUMENTI
    # -----------------------------------------------------

    files = request.files.getlist(
        "documents"
    )

    db = get_db()

    for file in files:

        if not file or not file.filename:
            continue

        if not allowed_file(file.filename):
            continue

        extension = file.filename.rsplit(
            ".",
            1
        )[1].lower()

        stored_name = (
            f"{uuid.uuid4().hex}.{extension}"
        )

        stored_name = secure_filename(
            stored_name
        )

        filepath = os.path.join(
            app.config["UPLOAD_FOLDER"],
            stored_name
        )

        file.save(filepath)

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
            datetime.utcnow().isoformat()
        ))

    db.commit()

    db.close()

    return redirect(
        url_for("case_summary")
    )


# ---------------------------------------------------------
# RIEPILOGO
# ---------------------------------------------------------

@app.route("/privato/riepilogo")
def case_summary():

    if session.get("role") != "debtor":

        return redirect(
            url_for("debtor_login")
        )

    email = session.get("email")

    case = get_case(email)

    if not case:

        return redirect(
            url_for("debtor_situation")
        )

    data = get_case_data(email)

    debts = get_debts(case["id"])

    income = data.get(
        "income",
        {}
    )

    expenses = data.get(
        "expenses",
        {}
    )

    total_income = (
        number(income.get("net_monthly"))
        + number(income.get("other_income"))
        + number(income.get("variable_income"))
    )

    total_expenses = sum(
        number(value)
        for value in expenses.values()
    )

    monthly_capacity = (
        total_income - total_expenses
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


# ---------------------------------------------------------
# ANALISI
# ---------------------------------------------------------

@app.route("/privato/analisi")
def case_analysis():

    if session.get("role") != "debtor":

        return redirect(
            url_for("debtor_login")
        )

    email = session.get("email")

    case = get_case(email)

    if not case:

        return redirect(
            url_for("debtor_situation")
        )

    data = get_case_data(email)

    debts = get_debts(case["id"])

    income = data.get(
        "income",
        {}
    )

    expenses = data.get(
        "expenses",
        {}
    )

    total_income = sum(
        number(value)
        for value in income.values()
    )

    total_expenses = sum(
        number(value)
        for value in expenses.values()
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

    if monthly_capacity > 0:

        sustainability = (
            "Esiste una disponibilità mensile "
            "positiva sulla base dei dati inseriti."
        )

    elif monthly_capacity == 0:

        sustainability = (
            "Le entrate risultano sostanzialmente "
            "assorbite dalle spese indicate."
        )

    else:

        sustainability = (
            "Le spese indicate risultano superiori "
            "alle entrate dichiarate."
        )

    if total_debt == 0:

        debt_message = (
            "Non sono ancora presenti importi "
            "debitori sufficientemente dettagliati."
        )

    else:

        debt_message = (
            f"Sono state indicate {len(debts)} "
            f"posizioni per un'esposizione complessiva "
            f"di circa € {total_debt:,.2f}."
        )

    return render_template(
        "analysis.html",
        data=data,
        debts=debts,
        total_income=total_income,
        total_expenses=total_expenses,
        monthly_capacity=monthly_capacity,
        total_debt=total_debt,
        total_payments=total_payments,
        sustainability=sustainability,
        debt_message=debt_message
    )


# ---------------------------------------------------------
# LOGOUT
# ---------------------------------------------------------

@app.route("/logout")
def logout():

    session.clear()

    return redirect(
        url_for("home")
    )


# ---------------------------------------------------------
# AVVIO
# ---------------------------------------------------------

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=5000
    )
