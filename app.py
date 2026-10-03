from flask import Flask, render_template, request, redirect, url_for, session

app = Flask(__name__)
app.secret_key = "fixtude-dev-secret-change-me"

# Demo accounts - only for the first MVP.
USERS = {
    "debitore": {
        "email": "demo@fixtude.it",
        "password": "1234",
        "role": "debitore",
        "name": "Demo Debitore"
    },
    "risolutore": {
        "email": "pro@fixtude.it",
        "password": "1234",
        "role": "risolutore",
        "name": "Demo Risolutore"
    }
}

@app.route("/")
def home():
    return render_template("home.html")

@app.route("/login/<role>", methods=["GET", "POST"])
def login(role):
    if role not in USERS:
        return redirect(url_for("home"))

    error = None

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        user = USERS[role]

        if email == user["email"] and password == user["password"]:
            session["user"] = user
            return redirect(url_for("dashboard", role=role))

        error = "Email o password non corretti."

    return render_template("login.html", role=role, error=error)

@app.route("/dashboard/<role>")
def dashboard(role):
    user = session.get("user")

    if not user or user.get("role") != role:
        return redirect(url_for("login", role=role))

    return render_template("dashboard.html", user=user)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
