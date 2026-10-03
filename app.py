
from flask import Flask, render_template, request, redirect, url_for, session
from werkzeug.utils import secure_filename
import os
import uuid

app = Flask(__name__)
app.secret_key = "fixtude-demo-secret-key"

UPLOAD_FOLDER = "uploads"
ALLOWED_EXTENSIONS = {
    "pdf", "jpg", "jpeg", "png",
    "doc", "docx", "xls", "xlsx"
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
        "." in filename and
        filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


@app.route("/")
def home():
    return render_template("home.html")


@app.route("/login/<role>", methods=["GET", "POST"])
def login(role):

    if role not in ["debtor", "resolver"]:
        return redirect(url_for("home"))

    error = None

    if request.method == "POST":

        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = USERS.get(email)

        if user and user["password"] == password and user["role"] == role:

            session["email"] = email
            session["role"] = role
            session["name"] = user["name"]

            if role == "debtor":
                return redirect(url_for("debtor_dashboard"))

            return redirect(url_for("resolver_dashboard"))

        error = "I dati inseriti non risultano corretti. Controllali e riprova."

    return render_template(
        "login.html",
        role=role,
        error=error
    )


@app.route("/debtor")
def debtor_dashboard():

    if session.get("role") != "debtor":
        return redirect(url_for("login", role="debtor"))

    return render_template(
        "dashboard.html",
        role="debtor",
        name=session.get("name")
    )


@app.route("/resolver")
def resolver_dashboard():

    if session.get("role") != "resolver":
        return redirect(url_for("login", role="resolver"))

    return render_template(
        "dashboard.html",
        role="resolver",
        name=session.get("name")
    )


@app.route("/debtor/new", methods=["GET", "POST"])
def debtor_new():

    if session.get("role") != "debtor":
        return redirect(url_for("login", role="debtor"))

    message = None
    uploaded_files = []

    if request.method == "POST":

        privacy = request.form.get("privacy")

        if not privacy:
            message = "Per continuare è necessario prendere visione e accettare l'informativa privacy."
        else:

            for file in request.files.getlist("documents"):

                if file and file.filename and allowed_file(file.filename):

                    extension = file.filename.rsplit(".", 1)[1].lower()
                    unique_name = f"{uuid.uuid4().hex}.{extension}"

                    filename = secure_filename(unique_name)

                    file.save(
                        os.path.join(
                            app.config["UPLOAD_FOLDER"],
                            filename
                        )
                    )

                    uploaded_files.append(file.filename)

            message = (
                "Le informazioni sono state ricevute. "
                "La tua pratica potrà essere completata successivamente."
            )

    return render_template(
        "new_situation.html",
        message=message,
        uploaded_files=uploaded_files
    )


@app.route("/resolver/new")
def resolver_new():

    if session.get("role") != "resolver":
        return redirect(url_for("login", role="resolver"))

    return render_template("new_practice.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
