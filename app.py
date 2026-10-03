from flask import Flask, render_template, request, redirect, url_for, session
from werkzeug.utils import secure_filename
import os
import uuid

app = Flask(__name__)

app.secret_key = "fixtude-demo-secret-key"

UPLOAD_FOLDER = "uploads"

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

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024


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


def allowed_file(filename):

    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower()
        in ALLOWED_EXTENSIONS
    )


# -------------------------------------------------
# HOME
# -------------------------------------------------

@app.route("/")
def home():

    return render_template("home.html")


# -------------------------------------------------
# LOGIN PRIVATO
# -------------------------------------------------

@app.route("/privato/login", methods=["GET", "POST"])
def debtor_login():

    error = None

    if request.method == "POST":

        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

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

            return redirect(url_for("debtor_dashboard"))

        error = "I dati inseriti non risultano corretti. Controllali e riprova."

    return render_template(
        "login.html",
        role="debtor",
        error=error
    )


# -------------------------------------------------
# LOGIN RISOLUTORE
# -------------------------------------------------

@app.route("/risolutore/login", methods=["GET", "POST"])
def resolver_login():

    error = None

    if request.method == "POST":

        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

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

            return redirect(url_for("resolver_dashboard"))

        error = "I dati inseriti non risultano corretti. Controllali e riprova."

    return render_template(
        "login.html",
        role="resolver",
        error=error
    )


# -------------------------------------------------
# AREA PRIVATA
# -------------------------------------------------

@app.route("/privato")
def debtor_dashboard():

    if session.get("role") != "debtor":

        return redirect(url_for("debtor_login"))

    return render_template(
        "dashboard.html",
        role="debtor",
        name=session.get("name")
    )


# -------------------------------------------------
# AREA PROFESSIONALE
# -------------------------------------------------

@app.route("/risolutore")
def resolver_dashboard():

    if session.get("role") != "resolver":

        return redirect(url_for("resolver_login"))

    return render_template(
        "dashboard.html",
        role="resolver",
        name=session.get("name")
    )


# -------------------------------------------------
# NUOVA SITUAZIONE PRIVATO
# -------------------------------------------------

@app.route("/privato/situazione", methods=["GET", "POST"])
def debtor_situation():

    if session.get("role") != "debtor":

        return redirect(url_for("debtor_login"))

    message = None
    uploaded_files = []

    if request.method == "POST":

        privacy = request.form.get("privacy")

        if not privacy:

            message = (
                "Per continuare è necessario prendere visione "
                "dell'informativa privacy."
            )

        else:

            documents = request.files.getlist("documents")

            for file in documents:

                if file and file.filename and allowed_file(file.filename):

                    extension = file.filename.rsplit(".", 1)[1].lower()

                    unique_name = (
                        f"{uuid.uuid4().hex}.{extension}"
                    )

                    filename = secure_filename(unique_name)

                    filepath = os.path.join(
                        app.config["UPLOAD_FOLDER"],
                        filename
                    )

                    file.save(filepath)

                    uploaded_files.append(file.filename)

            message = (
                "Il primo quadro della tua situazione "
                "è stato ricevuto."
            )

    return render_template(
        "new_situation.html",
        message=message,
        uploaded_files=uploaded_files
    )


# -------------------------------------------------
# NUOVA PRATICA RISOLUTORE
# -------------------------------------------------

@app.route("/risolutore/pratica", methods=["GET", "POST"])
def resolver_practice():

    if session.get("role") != "resolver":

        return redirect(url_for("resolver_login"))

    return render_template(
        "new_practice.html"
    )


# -------------------------------------------------
# LOGOUT
# -------------------------------------------------

@app.route("/logout")
def logout():

    session.clear()

    return redirect(url_for("home"))


# -------------------------------------------------
# AVVIO
# -------------------------------------------------

if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=5000
    )
