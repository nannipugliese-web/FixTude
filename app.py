
from flask import Flask, render_template_string, make_response
import os

app = Flask(__name__)

# ============================================================
# FIXTUDE V1 NEW
# Misuratore gratuito della sostenibilita del debito
#
# Nessun database
# Nessun login
# Nessun pagamento
# Nessun caricamento di documenti
# Nessuna memorizzazione dei dati economici
# Calcolo eseguito esclusivamente nel browser
# Richiesta consulenza tramite client email dell'utente
# ============================================================

HTML = r"""<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="theme-color" content="#123c70">
    <meta name="robots" content="index,follow">
    <meta name="description" content="Misura gratuitamente la sostenibilita della tua situazione debitoria. Nessuna registrazione e nessun pagamento.">
    <title>FixTude | Misura la sostenibilita del tuo debito</title>

    <style>
        :root {
            --blue: #174f91;
            --blue-dark: #12345b;
            --blue-light: #eaf3ff;
            --text: #1b2b40;
            --muted: #637286;
            --border: #dce5ef;
            --bg: #f5f8fc;
            --white: #ffffff;
            --green: #187747;
            --green-bg: #e9f8ef;
            --orange: #985600;
            --orange-bg: #fff4df;
            --red: #a52626;
            --red-bg: #ffeded;
        }

        * {
            box-sizing: border-box;
        }

        html {
            scroll-behavior: smooth;
        }

        body {
            margin: 0;
            font-family: Arial, Helvetica, sans-serif;
            color: var(--text);
            background: var(--bg);
            line-height: 1.6;
        }

        a {
            color: var(--blue);
        }

        button, input {
            font: inherit;
        }

        button, a {
            -webkit-tap-highlight-color: transparent;
        }

        .topbar {
            background: var(--white);
            border-bottom: 1px solid var(--border);
        }

        .nav {
            max-width: 1100px;
            margin: auto;
            padding: 18px 22px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
        }

        .brand {
            color: var(--blue-dark);
            text-decoration: none;
            font-size: 28px;
            font-weight: 800;
            letter-spacing: -1px;
        }

        .brand span {
            color: #3986d8;
        }

        .nav-note {
            font-size: 13px;
            color: var(--muted);
            text-align: right;
        }

        .hero {
            background: linear-gradient(130deg, #12345b, #2167b0);
            color: white;
            padding: 62px 22px 68px;
        }

        .hero-inner {
            max-width: 940px;
            margin: auto;
            text-align: center;
        }

        .eyebrow {
            display: inline-block;
            border: 1px solid rgba(255,255,255,.4);
            border-radius: 30px;
            padding: 6px 13px;
            font-size: 12px;
            font-weight: 700;
            letter-spacing: .7px;
            text-transform: uppercase;
        }

        h1 {
            font-size: clamp(32px, 5vw, 49px);
            line-height: 1.12;
            letter-spacing: -1.3px;
            margin: 22px 0 18px;
        }

        .hero p {
            max-width: 720px;
            margin: 0 auto;
            font-size: 18px;
            color: #edf5ff;
        }

        .trust-row {
            display: flex;
            justify-content: center;
            flex-wrap: wrap;
            gap: 12px 24px;
            margin-top: 28px;
            font-size: 14px;
            color: #f1f7ff;
        }

        .container {
            width: min(100% - 32px, 960px);
            margin: -30px auto 60px;
            position: relative;
        }

        .card {
            background: var(--white);
            border: 1px solid var(--border);
            border-radius: 18px;
            padding: 30px;
            box-shadow: 0 12px 38px rgba(21, 55, 91, .07);
            margin-bottom: 24px;
        }

        .section-label {
            color: var(--blue);
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: .8px;
            font-size: 12px;
        }

        h2 {
            font-size: clamp(23px, 3vw, 31px);
            line-height: 1.2;
            letter-spacing: -.5px;
            margin: 9px 0 12px;
        }

        h3 {
            margin: 0 0 10px;
            font-size: 19px;
        }

        .muted {
            color: var(--muted);
        }

        .intro {
            margin: 0 0 25px;
        }

        .fields {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 20px;
        }

        .field {
            min-width: 0;
        }

        label {
            display: block;
            font-weight: 700;
            margin-bottom: 8px;
            font-size: 14px;
        }

        .input-wrap {
            position: relative;
        }

        input[type="number"] {
            width: 100%;
            min-height: 51px;
            border: 1px solid #cbd7e5;
            border-radius: 10px;
            background: white;
            padding: 12px 43px 12px 13px;
            color: var(--text);
            outline: none;
        }

        input[type="number"]:focus {
            border-color: #3986d8;
            box-shadow: 0 0 0 3px rgba(57,134,216,.14);
        }

        .euro {
            position: absolute;
            right: 14px;
            top: 50%;
            transform: translateY(-50%);
            color: var(--muted);
            pointer-events: none;
        }

        .field-help {
            display: block;
            font-size: 12px;
            color: var(--muted);
            margin-top: 6px;
        }

        .primary-button,
        .secondary-button {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            text-align: center;
            text-decoration: none;
            border: 0;
            border-radius: 10px;
            min-height: 52px;
            padding: 14px 22px;
            font-weight: 800;
            cursor: pointer;
            transition: transform .15s, background .15s;
        }

        .primary-button {
            background: var(--blue);
            color: white;
        }

        .primary-button:hover {
            background: var(--blue-dark);
            transform: translateY(-1px);
        }

        .secondary-button {
            color: var(--blue);
            background: var(--blue-light);
            border: 1px solid #c9def7;
        }

        .button-full {
            width: 100%;
            margin-top: 24px;
        }

        .privacy-hint {
            text-align: center;
            font-size: 12px;
            color: var(--muted);
            margin: 14px 0 0;
        }

        .result {
            display: none;
            margin-top: 28px;
            border: 1px solid var(--border);
            border-radius: 15px;
            overflow: hidden;
            scroll-margin-top: 20px;
        }

        .result.visible {
            display: block;
        }

        .result-head {
            padding: 24px;
            background: var(--blue-light);
        }

        .result-head h3 {
            font-size: 23px;
            margin: 6px 0;
        }

        .result-body {
            padding: 24px;
        }

        .numbers {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 12px;
            margin-bottom: 24px;
        }

        .number-box {
            background: #f6f8fb;
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 15px;
            min-width: 0;
        }

        .number-label {
            display: block;
            font-size: 12px;
            color: var(--muted);
            margin-bottom: 7px;
        }

        .number-value {
            font-size: clamp(16px, 2.5vw, 23px);
            font-weight: 800;
            overflow-wrap: anywhere;
        }

        .status-green .result-head {
            background: var(--green-bg);
            color: var(--green);
        }

        .status-orange .result-head {
            background: var(--orange-bg);
            color: var(--orange);
        }

        .status-red .result-head {
            background: var(--red-bg);
            color: var(--red);
        }

        .result-copy {
            margin: 0 0 17px;
        }

        .disclaimer {
            background: #f6f8fb;
            border-left: 4px solid #8ca8c7;
            padding: 13px 15px;
            font-size: 12px;
            color: var(--muted);
            border-radius: 0 8px 8px 0;
        }

        .consultation {
            background: linear-gradient(145deg, #fff, #f0f6ff);
        }

        .consult-grid {
            display: grid;
            grid-template-columns: 1.2fr .8fr;
            gap: 24px;
            align-items: center;
        }

        .consult-points {
            list-style: none;
            padding: 0;
            margin: 18px 0 0;
        }

        .consult-points li {
            margin: 9px 0;
            padding-left: 27px;
            position: relative;
        }

        .consult-points li::before {
            content: "✓";
            position: absolute;
            left: 0;
            color: var(--green);
            font-weight: 800;
        }

        .consult-action {
            background: white;
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 22px;
            text-align: center;
        }

        .consult-action .primary-button {
            width: 100%;
            margin-top: 12px;
        }

        .small {
            font-size: 12px;
        }

        .how-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 18px;
            margin-top: 22px;
        }

        .how-item {
            border: 1px solid var(--border);
            background: white;
            padding: 20px;
            border-radius: 12px;
        }

        .step {
            width: 34px;
            height: 34px;
            border-radius: 10px;
            background: var(--blue-light);
            color: var(--blue);
            display: grid;
            place-items: center;
            font-weight: 800;
            margin-bottom: 13px;
        }

        .footer {
            background: #102c4d;
            color: #dce8f6;
            padding: 34px 22px;
        }

        .footer-inner {
            max-width: 960px;
            margin: auto;
        }

        .footer a {
            color: #dce8f6;
        }

        .footer p {
            font-size: 12px;
            margin: 8px 0;
        }

        .footer-brand {
            font-weight: 800;
            font-size: 22px;
            color: white;
        }

        .hidden {
            display: none !important;
        }

        @media (max-width: 650px) {
            .nav {
                padding: 14px 17px;
            }

            .brand {
                font-size: 25px;
            }

            .nav-note {
                max-width: 145px;
                font-size: 11px;
            }

            .hero {
                padding: 43px 18px 54px;
            }

            .hero p {
                font-size: 16px;
            }

            .trust-row {
                gap: 8px 14px;
                font-size: 12px;
            }

            .container {
                width: calc(100% - 22px);
                margin-top: -22px;
            }

            .card {
                padding: 21px 17px;
                border-radius: 14px;
            }

            .fields {
                grid-template-columns: 1fr;
                gap: 16px;
            }

            .numbers {
                grid-template-columns: 1fr;
            }

            .number-box {
                padding: 13px;
            }

            .number-value {
                font-size: 22px;
            }

            .consult-grid,
            .how-grid {
                grid-template-columns: 1fr;
            }

            .primary-button,
            .secondary-button {
                width: 100%;
            }
        }

        @media (prefers-reduced-motion: reduce) {
            html {
                scroll-behavior: auto;
            }

            * {
                transition: none !important;
            }
        }
    </style>
</head>

<body>
<header class="topbar">
    <nav class="nav" aria-label="Navigazione principale">
        <a class="brand" href="/" aria-label="FixTude home">
            Fix<span>Tude</span>
        </a>
        <div class="nav-note">
            Uno strumento gratuito<br>
            per fare chiarezza sui debiti
        </div>
    </nav>
</header>

<main>
    <section class="hero">
        <div class="hero-inner">
            <span class="eyebrow">Gratuito · Senza registrazione</span>
            <h1>La tua situazione debitoria,<br>finalmente più chiara.</h1>
            <p>
                Inserisci le tue entrate, le spese essenziali e le rate
                dei debiti. Scopri il margine mensile che ti rimane
                e ottieni un'indicazione preliminare sulla sostenibilita
                della tua situazione.
            </p>
            <div class="trust-row">
                <span>✓ Nessun pagamento</span>
                <span>✓ Nessun account</span>
                <span>✓ Dati economici elaborati sul dispositivo</span>
            </div>
        </div>
    </section>

    <div class="container">
        <section class="card" id="misuratore">
            <div class="section-label">Il misuratore FixTude</div>
            <h2>Quanto e sostenibile il tuo debito?</h2>
            <p class="muted intro">
                Inserisci importi mensili realistici. Il calcolo e
                indicativo e richiede soltanto pochi dati.
                Non inserire nomi, codici fiscali o dati bancari.
            </p>

            <form id="calculator" novalidate>
                <div class="fields">
                    <div class="field">
                        <label for="income">Entrate nette mensili</label>
                        <div class="input-wrap">
                            <input id="income" type="number" min="0"
                                max="100000000" step="0.01"
                                inputmode="decimal" placeholder="2000"
                                required>
                            <span class="euro">€</span>
                        </div>
                        <span class="field-help">
                            Stipendi, pensioni e altre entrate regolari.
                        </span>
                    </div>

                    <div class="field">
                        <label for="expenses">Spese essenziali mensili</label>
                        <div class="input-wrap">
                            <input id="expenses" type="number" min="0"
                                max="100000000" step="0.01"
                                inputmode="decimal" placeholder="1200"
                                required>
                            <span class="euro">€</span>
                        </div>
                        <span class="field-help">
                            Casa, alimentari, utenze, trasporti e necessita.
                        </span>
                    </div>

                    <div class="field">
                        <label for="payments">Rate mensili dei debiti</label>
                        <div class="input-wrap">
                            <input id="payments" type="number" min="0"
                                max="100000000" step="0.01"
                                inputmode="decimal" placeholder="600"
                                required>
                            <span class="euro">€</span>
                        </div>
                        <span class="field-help">
                            Somma delle rate e degli altri pagamenti
                            mensili dei debiti.
                        </span>
                    </div>

                    <div class="field">
                        <label for="totalDebt">Debito complessivo residuo</label>
                        <div class="input-wrap">
                            <input id="totalDebt" type="number" min="0"
                                max="100000000000" step="0.01"
                                inputmode="decimal" placeholder="25000"
                                required>
                            <span class="euro">€</span>
                        </div>
                        <span class="field-help">
                            Importo complessivo indicativo ancora da pagare.
                        </span>
                    </div>
                </div>

                <button type="submit" class="primary-button button-full">
                    CALCOLA GRATUITAMENTE
                </button>
                <p class="privacy-hint">
                    Il calcolo avviene nel browser: i valori economici
                    non vengono inviati al server.
                </p>
            </form>

            <div id="error" class="disclaimer hidden" role="alert"
                 style="margin-top:18px"></div>

            <section id="result" class="result" aria-live="polite"
                     aria-atomic="true" tabindex="-1">
                <div class="result-head">
                    <div class="section-label">Il tuo esito indicativo</div>
                    <h3 id="resultTitle"></h3>
                    <p id="resultSubtitle"></p>
                </div>

                <div class="result-body">
                    <div class="numbers">
                        <div class="number-box">
                            <span class="number-label">
                                Margine mensile dopo le spese e le rate
                            </span>
                            <div class="number-value" id="marginValue"></div>
                        </div>
                        <div class="number-box">
                            <span class="number-label">
                                Rate rispetto alle entrate
                            </span>
                            <div class="number-value" id="ratioValue"></div>
                        </div>
                        <div class="number-box">
                            <span class="number-label">
                                Debito residuo dichiarato
                            </span>
                            <div class="number-value" id="debtValue"></div>
                        </div>
                    </div>

                    <p class="result-copy" id="resultText"></p>
                    <div class="disclaimer">
                        <strong>Importante:</strong>
                        questa simulazione non e una diagnosi finanziaria,
                        una verifica dei debiti o una garanzia di solvibilita.
                        Non considera, tra l'altro, interessi futuri,
                        arretrati, spese irregolari, patrimonio, scadenze
                        giudiziarie o variazioni delle entrate.
                        Il risultato non sostituisce una valutazione
                        individuale.
                    </div>

                    <button type="button" id="resetButton"
                            class="secondary-button button-full">
                        AZZERA I DATI E RICOMINCIA
                    </button>
                </div>
            </section>
        </section>

        <section class="card consultation" id="consulenza">
            <div class="consult-grid">
                <div>
                    <div class="section-label">Un primo confronto gratuito</div>
                    <h2>Non sai quale passo compiere?</h2>
                    <p class="muted">
                        Se la tua situazione ti preoccupa, puoi chiedere
                        una prima consulenza gratuita senza impegno.
                        Potrai spiegare la tua esigenza e valutare
                        se approfondire il caso.
                    </p>
                    <ul class="consult-points">
                        <li>Nessun obbligo di acquistare un servizio.</li>
                        <li>Nessun documento richiesto dal misuratore.</li>
                        <li>Il contatto e separato dal calcolo economico.</li>
                    </ul>
                </div>

                <div class="consult-action">
                    <h3>Richiedi un ricontatto</h3>
                    <p class="muted small">
                        Il modulo prepara una e-mail nel tuo programma
                        di posta. Potrai controllarla e inviarla tu.
                    </p>

                    <form id="contactForm" novalidate>
                        <div class="field" style="text-align:left">
                            <label for="contactName">Nome</label>
                            <input id="contactName" type="text"
                                maxlength="100" autocomplete="name"
                                placeholder="Il tuo nome"
                                style="width:100%;min-height:48px;border:1px solid #cbd7e5;border-radius:9px;padding:11px">
                        </div>

                        <div class="field" style="text-align:left;margin-top:14px">
                            <label for="contactEmail">E-mail o telefono</label>
                            <input id="contactEmail" type="text"
                                maxlength="150" autocomplete="email"
                                placeholder="Come possiamo contattarti?"
                                style="width:100%;min-height:48px;border:1px solid #cbd7e5;border-radius:9px;padding:11px"
                                required>
                        </div>

                        <div class="field" style="text-align:left;margin-top:14px">
                            <label for="contactTime">Quando preferisci?</label>
                            <input id="contactTime" type="text"
                                maxlength="100"
                                placeholder="Es. pomeriggio (facoltativo)"
                                style="width:100%;min-height:48px;border:1px solid #cbd7e5;border-radius:9px;padding:11px">
                        </div>

                        <p class="small muted" style="text-align:left">
                            Non inserire qui dettagli bancari, codici fiscali
                            o documenti. Prima di inviare la richiesta,
                            consulta l'informativa privacy del servizio.
                        </p>

                        <p class="small" style="text-align:left">
                            <a href="/privacy">Leggi l'informativa privacy</a>
                        </p>

                        <button type="submit" class="primary-button">
                            PREPARA LA RICHIESTA
                        </button>
                        <p id="contactMessage" class="small muted"
                           role="status"></p>
                    </form>
                </div>
            </div>
        </section>

        <section class="card">
            <div class="section-label">Come funziona</div>
            <h2>Tre passaggi, nessuna registrazione</h2>

            <div class="how-grid">
                <div class="how-item">
                    <div class="step">1</div>
                    <h3>Inserisci gli importi</h3>
                    <p class="muted small">
                        Indica entrate, spese essenziali, rate e debito
                        residuo complessivo.
                    </p>
                </div>
                <div class="how-item">
                    <div class="step">2</div>
                    <h3>Leggi il risultato</h3>
                    <p class="muted small">
                        Visualizza il margine mensile e un'indicazione
                        preliminare della sostenibilita.
                    </p>
                </div>
                <div class="how-item">
                    <div class="step">3</div>
                    <h3>Se vuoi, chiedi aiuto</h3>
                    <p class="muted small">
                        Puoi preparare una richiesta di ricontatto
                        gratuita, senza impegno.
                    </p>
                </div>
            </div>
        </section>
    </div>
</main>

<footer class="footer">
    <div class="footer-inner">
        <div class="footer-brand">FixTude</div>
        <p>Uno strumento gratuito per orientarsi nella propria
           situazione debitoria.</p>
        <p><a href="/privacy">Informativa privacy</a> ·
           <a href="mailto:info@fixtude.it">info@fixtude.it</a></p>
        <p>
            Partita IVA 15990471003
        </p>
        <p>
            Il misuratore offre informazioni orientative e non costituisce
            consulenza legale, finanziaria o professionale.
            L'esito non garantisce che un debito sia sostenibile
            o che un creditore accetti una proposta.
        </p>
    </div>
</footer>

<script>
(function () {
    "use strict";

    const calculator = document.getElementById("calculator");
    const result = document.getElementById("result");
    const errorBox = document.getElementById("error");

    const euroFormatter = new Intl.NumberFormat("it-IT", {
        style: "currency",
        currency: "EUR",
        maximumFractionDigits: 2
    });

    function euro(value) {
        return euroFormatter.format(value);
    }

    function getNumber(id) {
        const input = document.getElementById(id);
        const raw = input.value.trim();

        if (raw === "") {
            return null;
        }

        const value = Number(raw);

        if (!Number.isFinite(value) || value < 0) {
            return null;
        }

        return value;
    }

    function showError(message) {
        errorBox.textContent = message;
        errorBox.classList.remove("hidden");
        result.classList.remove("visible");
        errorBox.scrollIntoView({ behavior: "smooth", block: "center" });
    }

    function hideError() {
        errorBox.textContent = "";
        errorBox.classList.add("hidden");
    }

    calculator.addEventListener("submit", function (event) {
        event.preventDefault();
        hideError();

        const income = getNumber("income");
        const expenses = getNumber("expenses");
        const payments = getNumber("payments");
        const totalDebt = getNumber("totalDebt");

        if ([income, expenses, payments, totalDebt].some(
            value => value === null
        )) {
            showError(
                "Controlla tutti i campi: inserisci importi numerici " +
                "validi e non negativi. Se un importo e zero, inserisci 0."
            );
            return;
        }

        if (income <= 0) {
            showError(
                "Per effettuare il calcolo e necessario indicare " +
                "entrate mensili maggiori di zero."
            );
            return;
        }

        if ([income, expenses, payments, totalDebt].some(
            value => value > 100000000000
        )) {
            showError("Uno degli importi inseriti e troppo elevato.");
            return;
        }

        const margin = income - expenses - payments;
        const paymentRatio = (payments / income) * 100;
        const marginRatio = (margin / income) * 100;

        let statusClass;
        let title;
        let subtitle;
        let text;

        /*
         * Soglie indicative di orientamento, non standard ufficiali.
         * Si considerano insieme margine residuo e peso delle rate.
         */
        if (margin < 0 || paymentRatio >= 40) {
            statusClass = "status-red";
            title = "Situazione potenzialmente critica";
            subtitle = "Il bilancio merita particolare attenzione.";
            text =
                margin < 0
                ? "In base agli importi inseriti, le spese essenziali " +
                  "e le rate superano le entrate mensili. Il bilancio " +
                  "presenta quindi un disavanzo. Prima di assumere " +
                  "nuovi impegni, potrebbe essere utile esaminare " +
                  "attentamente le uscite e chiedere un confronto."
                : "Le rate dei debiti assorbono una quota elevata " +
                  "delle entrate dichiarate. Anche se rimane un margine " +
                  "dopo le spese indicate, imprevisti o variazioni " +
                  "del reddito potrebbero mettere sotto pressione " +
                  "il bilancio.";
        } else if (marginRatio < 10 || paymentRatio >= 25) {
            statusClass = "status-orange";
            title = "Sostenibilita sotto pressione";
            subtitle = "Il margine disponibile potrebbe essere limitato.";
            text =
                "Il calcolo mostra un margine relativamente contenuto " +
                "oppure un peso significativo delle rate rispetto " +
                "alle entrate. Per capire meglio la situazione, " +
                "sarebbe opportuno considerare anche spese non mensili, " +
                "imprevisti, eventuali arretrati e stabilita del reddito.";
        } else {
            statusClass = "status-green";
            title = "Quadro indicativamente sostenibile";
            subtitle = "Gli importi inseriti lasciano un margine mensile.";
            text =
                "Sulla base dei soli dati dichiarati, le entrate " +
                "coprono le spese essenziali e le rate indicate, " +
                "con un margine residuo. Questo non esclude difficolta " +
                "future: verifica anche le spese occasionali, le " +
                "scadenze e l'eventuale presenza di pagamenti arretrati.";
        }

        result.className = "result visible " + statusClass;

        document.getElementById("resultTitle").textContent = title;
        document.getElementById("resultSubtitle").textContent = subtitle;
        document.getElementById("marginValue").textContent = euro(margin);
        document.getElementById("ratioValue").textContent =
            paymentRatio.toLocaleString("it-IT", {
                maximumFractionDigits: 1
            }) + "%";
        document.getElementById("debtValue").textContent = euro(totalDebt);
        document.getElementById("resultText").textContent = text;

        result.scrollIntoView({ behavior: "smooth", block: "start" });
    });

    document.getElementById("resetButton").addEventListener("click", function () {
        calculator.reset();
        result.classList.remove("visible");
        hideError();

        ["income", "expenses", "payments", "totalDebt"].forEach(function (id) {
            document.getElementById(id).value = "";
        });

        document.getElementById("income").focus();
        document.getElementById("misuratore").scrollIntoView({
            behavior: "smooth",
            block: "start"
        });
    });

    /*
     * Il modulo non invia dati a FixTude e non effettua richieste HTTP.
     * Prepara una e-mail sul dispositivo dell'utente.
     * L'utente verifica e invia la richiesta dal proprio programma di posta.
     */
    document.getElementById("contactForm").addEventListener(
        "submit", function (event) {
            event.preventDefault();

            const name = document.getElementById("contactName").value.trim();
            const contact = document.getElementById("contactEmail").value.trim();
            const preferredTime = document.getElementById("contactTime").value.trim();
            const message = document.getElementById("contactMessage");

            if (!contact) {
                message.textContent =
                    "Inserisci un indirizzo e-mail o un numero di telefono.";
                return;
            }

            if (contact.length > 150 || name.length > 100 ||
                preferredTime.length > 100) {
                message.textContent =
                    "Controlla la lunghezza dei dati inseriti.";
                return;
            }

            const subject = "Richiesta prima consulenza gratuita FixTude";
            const body = [
                "Richiedo di essere ricontattato per una prima consulenza gratuita, senza impegno.",
                "",
                "Nome: " + (name || "Non indicato"),
                "Contatto: " + contact,
                "Fascia oraria preferita: " + (preferredTime || "Non indicata"),
                "",
                "Confermo di aver preso visione dell'informativa privacy disponibile sul sito FixTude."
            ].join("\n");

            const mailto =
                "mailto:info@fixtude.it?subject=" +
                encodeURIComponent(subject) +
                "&body=" + encodeURIComponent(body);

            message.textContent =
                "Si aprira il tuo programma di posta. Controlla la richiesta " +
                "e inviala per completare il contatto.";

            window.location.href = mailto;
        }
    );
})();
</script>
</body>
</html>
"""


PRIVACY_HTML = r"""<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="robots" content="index,follow">
    <title>Informativa privacy | FixTude</title>
    <style>
        body {
            margin: 0;
            background: #f5f8fc;
            color: #1b2b40;
            font: 16px/1.7 Arial, Helvetica, sans-serif;
        }
        main {
            max-width: 800px;
            margin: 35px auto;
            padding: 28px;
            background: white;
            border: 1px solid #dce5ef;
            border-radius: 15px;
        }
        h1, h2 { line-height: 1.25; }
        a { color: #174f91; }
        .notice {
            padding: 15px;
            background: #fff4df;
            border-left: 4px solid #985600;
        }
        @media(max-width:600px) {
            main { margin: 12px; padding: 20px; }
        }
    </style>
</head>
<body>
<main>
    <p><a href="/">← Torna a FixTude</a></p>
    <h1>Informativa privacy</h1>

    <div class="notice">
        <strong>Bozza da completare prima della pubblicazione.</strong>
        Questa pagina contiene una struttura informativa iniziale.
        Il titolare deve verificare e completare i dati identificativi,
        le informazioni tecniche sull'hosting, i tempi di conservazione
        e gli eventuali trattamenti effettuati dai fornitori.
    </div>

    <h2>1. Titolare del trattamento</h2>
    <p>
        Il titolare del trattamento dei dati raccolti attraverso il servizio
        FixTude deve essere identificato qui con denominazione o nome,
        indirizzo e recapiti completi.
        Contatto e-mail attualmente previsto:
        <a href="mailto:info@fixtude.it">info@fixtude.it</a>.
    </p>

    <h2>2. Dati inseriti nel misuratore</h2>
    <p>
        Il misuratore elabora nel browser dell'utente gli importi economici
        inseriti per calcolare un risultato indicativo.
        Il codice applicativo non invia tali importi al server e non li
        salva in un database. I dati restano temporaneamente presenti
        nella pagina finche l'utente non li cancella, ricarica o chiude.
    </p>

    <h2>3. Richiesta di ricontatto</h2>
    <p>
        Se l'utente sceglie di richiedere una consulenza, il modulo prepara
        una e-mail tramite il programma di posta del dispositivo.
        L'utente deve verificare e inviare personalmente il messaggio.
        I dati contenuti nella e-mail vengono quindi trasmessi al destinatario
        e potranno essere trattati per gestire la richiesta di ricontatto.
    </p>
    <p>
        Prima di pubblicare il servizio, il titolare deve indicare in modo
        completo finalita, base giuridica, destinatari, tempi di conservazione,
        diritti dell'interessato e modalita per esercitarli.
        L'eventuale invio di comunicazioni promozionali richiede una
        valutazione e una gestione separata.
    </p>

    <h2>4. Dati tecnici e hosting</h2>
    <p>
        Il fornitore di hosting potrebbe trattare dati tecnici necessari
        a erogare, proteggere e diagnosticare il servizio, come indirizzi IP,
        log di accesso e informazioni sul dispositivo. Prima della pubblicazione
        occorre verificare le impostazioni, i log e la documentazione
        del fornitore effettivamente utilizzato.
    </p>

    <h2>5. Sicurezza e conservazione</h2>
    <p>
        Le richieste di contatto ricevute via e-mail devono essere accessibili
        solo a persone autorizzate e conservate per un periodo proporzionato
        alla finalita, definito dal titolare.
    </p>

    <h2>6. Diritti dell'interessato</h2>
    <p>
        Per informazioni o per esercitare i diritti previsti dalla normativa
        applicabile, l'interessato puo contattare il titolare ai recapiti
        che dovranno essere completati e verificati prima della pubblicazione.
    </p>

    <p>
        La presente pagina non sostituisce la verifica dell'informativa
        definitiva rispetto ai trattamenti effettivamente svolti dal servizio.
    </p>
</main>
</body>
</html>
"""


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=()"
    )
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.route("/", methods=["GET"])
def home():
    response = make_response(render_template_string(HTML))
    return response


@app.route("/privacy", methods=["GET"])
def privacy():
    return make_response(render_template_string(PRIVACY_HTML))


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok", "application": "FixTude V1 New"}, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
