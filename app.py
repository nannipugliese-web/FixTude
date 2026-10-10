
from flask import Flask, render_template_string, make_response
import os

app = Flask(__name__)

# ============================================================
# FIXTUDE V1 NEW
# Home + Misuratore + Consulenza
# Calcolo economico esclusivamente nel browser.
# Nessun database, login, pagamento o upload.
# ============================================================

STYLE = r"""
:root {
    --blue: #18569a;
    --blue-dark: #12365e;
    --light-blue: #eaf3ff;
    --text: #1d2c40;
    --muted: #68788b;
    --border: #dce5ef;
    --background: #f4f7fb;
    --white: #ffffff;
    --green: #187747;
    --green-bg: #e8f7ed;
    --orange: #8d5900;
    --orange-bg: #fff3db;
    --red: #a12626;
    --red-bg: #ffebeb;
}

* { box-sizing: border-box; }

html { scroll-behavior: smooth; }

body {
    margin: 0;
    background: var(--background);
    color: var(--text);
    font-family: Arial, Helvetica, sans-serif;
    font-size: 16px;
    line-height: 1.6;
}

a { color: var(--blue); }

button, input, select, textarea { font: inherit; }

.topbar {
    background: white;
    border-bottom: 1px solid var(--border);
}

.nav {
    max-width: 1080px;
    margin: auto;
    padding: 15px 22px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 18px;
}

.logo {
    color: var(--blue-dark);
    text-decoration: none;
    font-size: 29px;
    font-weight: 800;
    letter-spacing: -1px;
}

.logo span { color: #3986d8; }

.nav-links {
    display: flex;
    gap: 20px;
    align-items: center;
    flex-wrap: wrap;
}

.nav-links a {
    text-decoration: none;
    font-size: 14px;
    font-weight: 700;
}

.hero {
    background: linear-gradient(130deg, #12365e, #2168b2);
    color: white;
    padding: 66px 22px 72px;
}

.hero-inner {
    max-width: 880px;
    margin: auto;
    text-align: center;
}

.eyebrow {
    display: inline-block;
    padding: 5px 13px;
    border: 1px solid rgba(255,255,255,.45);
    border-radius: 30px;
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .7px;
}

h1 {
    margin: 22px 0 18px;
    font-size: clamp(32px, 5vw, 49px);
    line-height: 1.13;
    letter-spacing: -1px;
}

.hero p {
    max-width: 690px;
    margin: auto;
    color: #eef5ff;
    font-size: 18px;
}

.container {
    width: min(100% - 32px, 960px);
    margin: 36px auto 60px;
}

.card {
    background: white;
    border: 1px solid var(--border);
    border-radius: 17px;
    padding: 30px;
    margin-bottom: 24px;
    box-shadow: 0 8px 28px rgba(22, 55, 90, .045);
}

.section-label {
    color: var(--blue);
    font-size: 12px;
    font-weight: 800;
    text-transform: uppercase;
    letter-spacing: .8px;
}

h2 {
    font-size: clamp(24px, 3.5vw, 32px);
    line-height: 1.22;
    letter-spacing: -.5px;
    margin: 8px 0 12px;
}

h3 {
    margin: 0 0 8px;
    font-size: 19px;
    line-height: 1.35;
}

p { margin-top: 0; }

.muted { color: var(--muted); }

.button {
    display: inline-flex;
    justify-content: center;
    align-items: center;
    min-height: 49px;
    padding: 12px 20px;
    border-radius: 10px;
    border: 0;
    text-decoration: none;
    text-align: center;
    font-weight: 800;
    cursor: pointer;
    transition: background .15s, transform .15s;
}

.button-primary {
    background: var(--blue);
    color: white;
}

.button-primary:hover {
    background: var(--blue-dark);
    transform: translateY(-1px);
}

.button-light {
    background: var(--light-blue);
    color: var(--blue);
    border: 1px solid #cbdff6;
}

.button-outline {
    color: var(--blue);
    background: white;
    border: 1px solid var(--border);
}

.button-full { width: 100%; }

.home-grid {
    display: grid;
    grid-template-columns: 1.2fr .8fr;
    gap: 22px;
    align-items: stretch;
}

.home-main {
    background: linear-gradient(145deg, white, #f0f6ff);
}

.benefits {
    padding: 0;
    list-style: none;
    margin: 20px 0 24px;
}

.benefits li {
    margin: 9px 0;
    padding-left: 26px;
    position: relative;
}

.benefits li:before {
    content: "✓";
    color: var(--green);
    position: absolute;
    left: 0;
    font-weight: 800;
}

.home-side {
    background: var(--blue-dark);
    color: white;
}

.home-side .section-label,
.home-side .muted {
    color: #d4e6fb;
}

.home-side .button {
    margin-top: 10px;
}

.steps {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 16px;
    margin-top: 20px;
}

.step-card {
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 20px;
    background: white;
}

.step-number {
    display: grid;
    place-items: center;
    width: 34px;
    height: 34px;
    border-radius: 10px;
    background: var(--light-blue);
    color: var(--blue);
    font-weight: 800;
    margin-bottom: 12px;
}

.form-section {
    margin-top: 28px;
    padding-top: 23px;
    border-top: 1px solid var(--border);
}

.form-section:first-of-type {
    margin-top: 20px;
}

.section-heading {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 14px;
    margin-bottom: 15px;
}

.section-heading p {
    margin: 4px 0 0;
    font-size: 13px;
    color: var(--muted);
}

.entry-list {
    display: grid;
    gap: 12px;
}

.entry-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(120px, .55fr) 40px;
    gap: 10px;
    align-items: end;
    padding: 13px;
    background: #f8fafd;
    border: 1px solid var(--border);
    border-radius: 11px;
}

.entry-row.debt-row {
    grid-template-columns:
        minmax(0, 1.1fr)
        minmax(100px, .65fr)
        minmax(100px, .65fr)
        40px;
}

.field { min-width: 0; }

.field label {
    display: block;
    font-size: 13px;
    font-weight: 700;
    margin-bottom: 6px;
}

.field input,
.field select,
.field textarea {
    width: 100%;
    min-width: 0;
    min-height: 45px;
    border: 1px solid #cbd7e5;
    border-radius: 8px;
    padding: 10px;
    background: white;
    color: var(--text);
    outline: none;
}

.field input:focus,
.field select:focus,
.field textarea:focus {
    border-color: #3986d8;
    box-shadow: 0 0 0 3px rgba(57,134,216,.12);
}

.remove-button {
    width: 40px;
    height: 43px;
    border: 1px solid #e8caca;
    background: #fff3f3;
    color: #a12626;
    border-radius: 8px;
    cursor: pointer;
    font-size: 20px;
}

.add-button {
    margin-top: 12px;
    border: 1px solid #cbdff6;
    background: var(--light-blue);
    color: var(--blue);
    padding: 9px 13px;
    border-radius: 8px;
    font-size: 13px;
    font-weight: 800;
    cursor: pointer;
}

.running-total {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-top: 13px;
    padding: 12px 15px;
    background: #f1f5fa;
    border-radius: 9px;
    font-size: 14px;
}

.running-total strong { font-size: 18px; }

.result {
    display: none;
    margin-top: 28px;
    border: 1px solid var(--border);
    border-radius: 14px;
    overflow: hidden;
}

.result.visible { display: block; }

.result-head {
    padding: 23px;
    background: var(--light-blue);
}

.result-head h3 {
    font-size: 25px;
    margin: 7px 0;
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

.result-body { padding: 23px; }

.result-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
    margin-bottom: 20px;
}

.result-box {
    padding: 15px;
    background: #f6f8fb;
    border: 1px solid var(--border);
    border-radius: 11px;
    min-width: 0;
}

.result-box span {
    display: block;
    color: var(--muted);
    font-size: 12px;
    margin-bottom: 5px;
}

.result-box strong {
    font-size: clamp(18px, 3vw, 25px);
    overflow-wrap: anywhere;
}

.disclaimer {
    background: #f6f8fb;
    border-left: 4px solid #8ca8c7;
    border-radius: 0 8px 8px 0;
    padding: 13px 15px;
    color: var(--muted);
    font-size: 12px;
}

.result-actions {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    margin-top: 20px;
}

.contact-intro {
    background: linear-gradient(145deg, #fff, #eff6ff);
}

.contact-grid {
    display: grid;
    grid-template-columns: .85fr 1.15fr;
    gap: 24px;
    align-items: start;
}

.contact-panel {
    border: 1px solid var(--border);
    background: white;
    border-radius: 13px;
    padding: 22px;
}

.contact-panel .field { margin-bottom: 16px; }

.contact-panel .button { margin-top: 5px; }

.check-line {
    display: flex;
    align-items: flex-start;
    gap: 10px;
    font-size: 13px;
    color: var(--muted);
    margin: 16px 0;
}

.check-line input {
    width: 17px;
    height: 17px;
    flex: 0 0 auto;
    margin-top: 3px;
}

.footer {
    padding: 32px 22px;
    background: #102c4d;
    color: #dce8f6;
}

.footer-inner {
    max-width: 960px;
    margin: auto;
}

.footer-brand {
    color: white;
    font-size: 22px;
    font-weight: 800;
}

.footer p {
    font-size: 12px;
    margin: 8px 0;
}

.footer a { color: #dce8f6; }

.notice {
    padding: 14px;
    background: #fff4df;
    border-left: 4px solid #985600;
    border-radius: 0 8px 8px 0;
    font-size: 13px;
}

.hidden { display: none !important; }

@media (max-width: 700px) {
    .nav {
        padding: 13px 16px;
        align-items: flex-start;
    }

    .logo { font-size: 26px; }

    .nav-links {
        gap: 8px 12px;
        justify-content: flex-end;
        font-size: 12px;
    }

    .nav-links a { font-size: 12px; }

    .hero { padding: 43px 18px 50px; }

    .hero p { font-size: 16px; }

    .container {
        width: calc(100% - 22px);
        margin-top: 22px;
        margin-bottom: 35px;
    }

    .card {
        padding: 21px 16px;
        border-radius: 13px;
    }

    .home-grid,
    .contact-grid,
    .steps {
        grid-template-columns: 1fr;
    }

    .entry-row,
    .entry-row.debt-row {
        grid-template-columns: minmax(0, 1fr) minmax(0, .8fr) 36px;
        gap: 8px;
        padding: 10px;
    }

    .entry-row .field:first-child {
        grid-column: 1 / 3;
    }

    .entry-row .field:nth-child(2) {
        grid-column: 1 / 3;
    }

    .entry-row.debt-row .field:first-child {
        grid-column: 1 / 4;
    }

    .entry-row.debt-row .field:nth-child(2) {
        grid-column: 1 / 2;
    }

    .entry-row.debt-row .field:nth-child(3) {
        grid-column: 2 / 3;
    }

    .entry-row .remove-button {
        grid-column: 3;
        grid-row: 1;
        align-self: end;
        width: 36px;
    }

    .entry-row.debt-row .remove-button {
        grid-column: 3;
        grid-row: 2;
    }

    .section-heading {
        align-items: flex-start;
    }

    .section-heading .add-button {
        flex: 0 0 auto;
        margin-top: 0;
    }

    .result-grid { grid-template-columns: 1fr; }

    .result-actions { grid-template-columns: 1fr; }

    .button { width: 100%; }

    .nav-links .nav-contact { display: none; }
}

@media (max-width: 380px) {
    .nav {
        flex-direction: column;
        gap: 5px;
    }

    .nav-links {
        width: 100%;
        justify-content: flex-start;
    }

    .entry-row,
    .entry-row.debt-row {
        grid-template-columns: minmax(0, 1fr) 36px;
    }

    .entry-row .field,
    .entry-row .field:first-child,
    .entry-row .field:nth-child(2),
    .entry-row.debt-row .field,
    .entry-row.debt-row .field:first-child,
    .entry-row.debt-row .field:nth-child(2),
    .entry-row.debt-row .field:nth-child(3) {
        grid-column: 1 / 2;
    }

    .entry-row .remove-button,
    .entry-row.debt-row .remove-button {
        grid-column: 2;
        grid-row: 1;
    }
}

@media (prefers-reduced-motion: reduce) {
    html { scroll-behavior: auto; }
    * { transition: none !important; }
}
"""

NAV = r"""
<header class="topbar">
    <nav class="nav">
        <a class="logo" href="/">Fix<span>Tude</span></a>
        <div class="nav-links">
            <a href="/">Home</a>
            <a href="/misuratore">Misura il tuo debito</a>
            <a class="nav-contact" href="/consulenza">Consulenza gratuita</a>
        </div>
    </nav>
</header>
"""

FOOTER = r"""
<footer class="footer">
    <div class="footer-inner">
        <div class="footer-brand">FixTude</div>
        <p>Uno strumento gratuito per fare chiarezza sulla propria
        situazione economica e debitoria.</p>
        <p><a href="/privacy">Informativa privacy</a> ·
        <a href="mailto:info@fixtude.it">info@fixtude.it</a></p>
        <p>Partita IVA 15990471003</p>
        <p>Il misuratore fornisce una stima orientativa e non costituisce
        consulenza finanziaria, legale o professionale, né garantisce
        la solvibilità o l'accettazione di proposte da parte dei creditori.</p>
    </div>
</footer>
"""

HOME_HTML = r"""
<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="FixTude: misura gratuitamente quanto è sostenibile la tua situazione debitoria. Senza registrazione e senza pagamenti.">
<meta name="theme-color" content="#12365e">
<title>FixTude | Fai chiarezza sui tuoi debiti</title>
<style>__STYLE__</style>
</head>
<body>
__NAV__

<section class="hero">
    <div class="hero-inner">
        <span class="eyebrow">Gratuito · Senza registrazione</span>
        <h1>Metti a fuoco la tua<br>situazione debitoria.</h1>
        <p>
            Capire quanto rimane ogni mese è il primo passo per
            valutare il peso dei debiti. FixTude ti aiuta a mettere
            in ordine entrate, spese e rate con un calcolo semplice.
        </p>
    </div>
</section>

<main class="container">
    <div class="home-grid">
        <section class="card home-main">
            <div class="section-label">Il servizio gratuito</div>
            <h2>Quanto ti rimane davvero ogni mese?</h2>
            <p class="muted">
                Inserisci le tue entrate, le spese e gli impegni debitori.
                Otterrai il saldo mensile e un'indicazione preliminare
                sulla sostenibilità del bilancio.
            </p>
            <ul class="benefits">
                <li>Più voci di entrata e di spesa</li>
                <li>Rate mensili e debito residuo</li>
                <li>Calcolo immediato del margine disponibile</li>
                <li>Nessun account, pagamento o caricamento di documenti</li>
            </ul>
            <a class="button button-primary" href="/misuratore">
                INIZIA IL CALCOLO GRATUITO
            </a>
        </section>

        <aside class="card home-side">
            <div class="section-label">Un primo confronto</div>
            <h2>Non sai come procedere?</h2>
            <p class="muted">
                Dopo aver fatto il calcolo, se desideri approfondire
                la tua situazione puoi chiedere una prima consulenza
                gratuita senza impegno.
            </p>
            <a class="button button-light" href="/consulenza">
                RICHIEDI UN RICONTATTO
            </a>
        </aside>
    </div>

    <section class="card">
        <div class="section-label">Come funziona</div>
        <h2>Tre passaggi semplici</h2>
        <div class="steps">
            <div class="step-card">
                <div class="step-number">1</div>
                <h3>Inserisci i tuoi numeri</h3>
                <p class="muted">
                    Redditi, spese essenziali, rate e debito residuo.
                </p>
            </div>
            <div class="step-card">
                <div class="step-number">2</div>
                <h3>Ottieni il risultato</h3>
                <p class="muted">
                    Visualizza il saldo mensile e il peso delle rate.
                </p>
            </div>
            <div class="step-card">
                <div class="step-number">3</div>
                <h3>Decidi se approfondire</h3>
                <p class="muted">
                    Se lo desideri, richiedi un primo ricontatto gratuito.
                </p>
            </div>
        </div>
    </section>

    <p class="muted" style="font-size:12px;text-align:center">
        Il risultato è orientativo e si basa esclusivamente sui dati inseriti.
        Non sostituisce una valutazione individuale.
    </p>
</main>

__FOOTER__
</body>
</html>
"""

CALCULATOR_HTML = r"""
<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Calcolatore gratuito di entrate, uscite, rate e debiti FixTude.">
<meta name="theme-color" content="#12365e">
<title>Misuratore gratuito | FixTude</title>
<style>__STYLE__</style>
</head>
<body>
__NAV__

<section class="hero" style="padding-top:42px;padding-bottom:48px">
    <div class="hero-inner">
        <span class="eyebrow">Passaggio 1 di 2</span>
        <h1>Ricostruisci il tuo bilancio mensile.</h1>
        <p>
            Inserisci le voci che descrivono la tua situazione.
            Puoi aggiungere tutte le righe necessarie.
            I dati economici vengono elaborati nel browser.
        </p>
    </div>
</section>

<main class="container">
<section class="card">
    <div class="section-label">Misuratore FixTude</div>
    <h2>Entrate, uscite e debiti</h2>
    <p class="muted">
        Inserisci importi mensili per entrate, spese e rate.
        Per il debito residuo indica invece il totale ancora da pagare.
        Se una voce non è presente, puoi lasciarla a zero.
    </p>

    <form id="budgetForm" novalidate>
        <section class="form-section">
            <div class="section-heading">
                <div>
                    <h3>1. Entrate mensili</h3>
                    <p>Importi netti che ricevi normalmente ogni mese.</p>
                </div>
                <button type="button" class="add-button" id="addIncome">
                    + Aggiungi
                </button>
            </div>
            <div id="incomeList" class="entry-list"></div>
            <div class="running-total">
                <span>Totale entrate</span>
                <strong id="incomeTotal">€ 0,00</strong>
            </div>
        </section>

        <section class="form-section">
            <div class="section-heading">
                <div>
                    <h3>2. Uscite mensili</h3>
                    <p>Spese di casa, alimentari, utenze, trasporti e altre spese correnti.</p>
                </div>
                <button type="button" class="add-button" id="addExpense">
                    + Aggiungi
                </button>
            </div>
            <div id="expenseList" class="entry-list"></div>
            <div class="running-total">
                <span>Totale uscite, escluse le rate qui sotto</span>
                <strong id="expenseTotal">€ 0,00</strong>
            </div>
        </section>

        <section class="form-section">
            <div class="section-heading">
                <div>
                    <h3>3. Debiti e finanziamenti</h3>
                    <p>Per ogni debito indica la rata mensile e il capitale residuo indicativo.</p>
                </div>
                <button type="button" class="add-button" id="addDebt">
                    + Aggiungi
                </button>
            </div>
            <div id="debtList" class="entry-list"></div>
            <div class="running-total">
                <span>Totale rate mensili</span>
                <strong id="paymentTotal">€ 0,00</strong>
            </div>
            <div class="running-total">
                <span>Debito residuo complessivo</span>
                <strong id="debtTotal">€ 0,00</strong>
            </div>
        </section>

        <button type="submit" class="button button-primary button-full"
                style="margin-top:27px">
            CALCOLA IL MIO BILANCIO
        </button>

        <p class="muted" style="font-size:12px;text-align:center;margin-top:12px">
            Non inserire nomi, codici fiscali, numeri di conto o dati identificativi
            dei creditori. Non occorre caricare documenti.
        </p>
    </form>

    <div id="errorBox" class="notice hidden" role="alert"
         style="margin-top:18px"></div>

    <section id="result" class="result" aria-live="polite"
             aria-atomic="true" tabindex="-1">
        <div class="result-head">
            <div class="section-label">Il tuo esito indicativo</div>
            <h3 id="resultTitle"></h3>
            <p id="resultSubtitle"></p>
        </div>
        <div class="result-body">
            <div class="result-grid">
                <div class="result-box">
                    <span>Entrate mensili</span>
                    <strong id="rIncome"></strong>
                </div>
                <div class="result-box">
                    <span>Uscite mensili, escluse le rate</span>
                    <strong id="rExpenses"></strong>
                </div>
                <div class="result-box">
                    <span>Rate mensili complessive</span>
                    <strong id="rPayments"></strong>
                </div>
                <div class="result-box">
                    <span>Residuo mensile dopo spese e rate</span>
                    <strong id="rRemaining"></strong>
                </div>
                <div class="result-box">
                    <span>Rate in rapporto alle entrate</span>
                    <strong id="rRatio"></strong>
                </div>
                <div class="result-box">
                    <span>Debito residuo complessivo</span>
                    <strong id="rDebt"></strong>
                </div>
            </div>

            <p id="resultText"></p>

            <div class="disclaimer">
                <strong>Attenzione:</strong> questa è una simulazione preliminare,
                non una diagnosi finanziaria né una garanzia di solvibilità.
                Non considera automaticamente arretrati, interessi futuri,
                scadenze legali, patrimonio, spese occasionali o variazioni
                del reddito. Le soglie sono indicatori orientativi, non
                standard ufficiali validi per ogni famiglia.
            </div>

            <div class="result-actions">
                <button type="button" class="button button-outline"
                        id="resetButton">
                    AZZERA E RICOMINCIA
                </button>
                <a class="button button-primary" href="/consulenza">
                    CHIEDI UNA CONSULENZA GRATUITA
                </a>
            </div>
        </div>
    </section>
</section>

<p class="muted" style="text-align:center;font-size:13px">
    <a href="/">← Torna alla home</a>
</p>
</main>

__FOOTER__

<script>
(function () {
    "use strict";

    const $ = id => document.getElementById(id);

    const money = new Intl.NumberFormat("it-IT", {
        style: "currency",
        currency: "EUR",
        maximumFractionDigits: 2
    });

    const categories = {
        income: [
            "Stipendio",
            "Pensione",
            "Lavoro autonomo",
            "Altre entrate",
            "Altro"
        ],
        expense: [
            "Affitto o mutuo",
            "Alimentari",
            "Utenze",
            "Trasporti",
            "Spese familiari",
            "Salute",
            "Altre spese",
            "Altro"
        ],
        debt: [
            "Prestito personale",
            "Carta di credito",
            "Finanziamento",
            "Mutuo",
            "Debito fiscale",
            "Utenze arretrate",
            "Altro debito"
        ]
    };

    function createSelect(items, label) {
        const select = document.createElement("select");
        select.setAttribute("aria-label", label);

        items.forEach(function (item) {
            const option = document.createElement("option");
            option.value = item;
            option.textContent = item;
            select.appendChild(option);
        });

        return select;
    }

    function createAmount(label, placeholder) {
        const input = document.createElement("input");
        input.type = "number";
        input.min = "0";
        input.max = "100000000000";
        input.step = "0.01";
        input.inputMode = "decimal";
        input.placeholder = placeholder || "0,00";
        input.setAttribute("aria-label", label);
        input.className = "amount";
        return input;
    }

    function createField(labelText, control) {
        const wrapper = document.createElement("div");
        wrapper.className = "field";

        const label = document.createElement("label");
        label.textContent = labelText;

        wrapper.appendChild(label);
        wrapper.appendChild(control);
        return wrapper;
    }

    function addRow(type, preset) {
        const list = $(type + "List");
        const row = document.createElement("div");
        row.className = "entry-row" + (type === "debt" ? " debt-row" : "");
        row.dataset.type = type;

        const select = createSelect(
            categories[type],
            type === "income" ? "Tipo di entrata" :
            type === "expense" ? "Tipo di uscita" : "Tipo di debito"
        );

        if (preset) select.value = preset;

        row.appendChild(createField(
            type === "income" ? "Fonte dell'entrata" :
            type === "expense" ? "Categoria della spesa" : "Tipo di debito",
            select
        ));

        if (type === "debt") {
            const payment = createAmount("Rata mensile", "Rata mensile");
            const balance = createAmount("Debito residuo", "Debito residuo");

            row.appendChild(createField("Rata mensile (€)", payment));
            row.appendChild(createField("Debito residuo (€)", balance));
        } else {
            const amount = createAmount(
                type === "income" ? "Entrata mensile" : "Spesa mensile",
                "Importo mensile"
            );
            row.appendChild(createField("Importo mensile (€)", amount));
        }

        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "remove-button";
        remove.textContent = "×";
        remove.title = "Rimuovi questa voce";
        remove.setAttribute("aria-label", "Rimuovi questa voce");

        remove.addEventListener("click", function () {
            row.remove();
            updateTotals();
        });

        row.appendChild(remove);

        row.querySelectorAll("input").forEach(function (input) {
            input.addEventListener("input", updateTotals);
        });

        list.appendChild(row);
        updateTotals();
    }

    function parseAmount(input) {
        if (input.value.trim() === "") return 0;

        const value = Number(input.value);

        if (!Number.isFinite(value) || value < 0 ||
            value > 100000000000) {
            throw new Error("Controlla gli importi: devono essere numerici e non negativi.");
        }

        return value;
    }

    function sumRows(type, index) {
        let total = 0;

        document.querySelectorAll(
            '.entry-row[data-type="' + type + '"]'
        ).forEach(function (row) {
            const inputs = row.querySelectorAll("input");

            if (type === "debt") {
                total += parseAmount(inputs[index]);
            } else {
                total += parseAmount(inputs[0]);
            }
        });

        return total;
    }

    function getTotals() {
        return {
            income: sumRows("income", 0),
            expenses: sumRows("expense", 0),
            payments: sumRows("debt", 0),
            debt: sumRows("debt", 1)
        };
    }

    function updateTotals() {
        try {
            const t = getTotals();
            $("incomeTotal").textContent = money.format(t.income);
            $("expenseTotal").textContent = money.format(t.expenses);
            $("paymentTotal").textContent = money.format(t.payments);
            $("debtTotal").textContent = money.format(t.debt);
        } catch (e) {
            $("incomeTotal").textContent = "Controlla importi";
            $("expenseTotal").textContent = "Controlla importi";
            $("paymentTotal").textContent = "Controlla importi";
            $("debtTotal").textContent = "Controlla importi";
        }
    }

    function showError(message) {
        $("errorBox").textContent = message;
        $("errorBox").classList.remove("hidden");
        $("result").classList.remove("visible");
        $("errorBox").scrollIntoView({
            behavior: "smooth",
            block: "center"
        });
    }

    function setResult(id, value) {
        $(id).textContent = value;
    }

    $("addIncome").addEventListener("click", () => addRow("income"));
    $("addExpense").addEventListener("click", () => addRow("expense"));
    $("addDebt").addEventListener("click", () => addRow("debt"));

    $("budgetForm").addEventListener("submit", function (event) {
        event.preventDefault();

        $("errorBox").classList.add("hidden");

        let t;

        try {
            t = getTotals();
        } catch (error) {
            showError(error.message);
            return;
        }

        if (t.income <= 0) {
            showError(
                "Inserisci almeno un'entrata mensile maggiore di zero."
            );
            return;
        }

        const remaining = t.income - t.expenses - t.payments;
        const paymentRatio = t.payments / t.income * 100;
        const remainingRatio = remaining / t.income * 100;

        let status, title, subtitle, description;

        if (remaining < 0 || paymentRatio >= 40) {
            status = "status-red";
            title = "Situazione potenzialmente critica";
            subtitle = "Il bilancio merita particolare attenzione.";

            description = remaining < 0
                ? "Le uscite e le rate indicate superano le entrate. " +
                  "Il bilancio presenta un disavanzo mensile. Può essere " +
                  "utile ricostruire le spese e valutare tempestivamente " +
                  "un confronto sulla situazione complessiva."
                : "Le rate assorbono una quota elevata delle entrate " +
                  "dichiarate. Il margine può essere vulnerabile a " +
                  "imprevisti o variazioni del reddito.";
        } else if (remainingRatio < 10 || paymentRatio >= 25) {
            status = "status-orange";
            title = "Sostenibilità sotto pressione";
            subtitle = "Il margine disponibile potrebbe essere limitato.";

            description =
                "Il bilancio lascia un margine relativamente contenuto " +
                "oppure le rate rappresentano una quota significativa " +
                "delle entrate. Considera anche spese non mensili, " +
                "arretrati e stabilità del reddito.";
        } else {
            status = "status-green";
            title = "Quadro indicativamente sostenibile";
            subtitle = "Gli importi inseriti lasciano un margine mensile.";

            description =
                "In base ai soli importi dichiarati, le entrate coprono " +
                "le spese e le rate inserite. Il risultato non esclude " +
                "difficoltà future e non considera automaticamente " +
                "imprevisti, arretrati o variazioni del reddito.";
        }

        $("result").className = "result visible " + status;

        setResult("resultTitle", title);
        setResult("resultSubtitle", subtitle);
        setResult("rIncome", money.format(t.income));
        setResult("rExpenses", money.format(t.expenses));
        setResult("rPayments", money.format(t.payments));
        setResult("rRemaining", money.format(remaining));
        setResult("rRatio", paymentRatio.toLocaleString("it-IT", {
            maximumFractionDigits: 1
        }) + "%");
        setResult("rDebt", money.format(t.debt));
        setResult("resultText", description);

        $("result").scrollIntoView({
            behavior: "smooth",
            block: "start"
        });
    });

    $("resetButton").addEventListener("click", function () {
        ["incomeList", "expenseList", "debtList"].forEach(function (id) {
            $(id).replaceChildren();
        });

        $("result").classList.remove("visible");
        $("errorBox").classList.add("hidden");

        addRow("income", "Stipendio");
        addRow("expense", "Affitto o mutuo");
        addRow("debt", "Prestito personale");

        $("misuratore");
        window.scrollTo({ top: 0, behavior: "smooth" });
    });

    // Righe iniziali vuote, personalizzabili dall'utente.
    addRow("income", "Stipendio");
    addRow("expense", "Affitto o mutuo");
    addRow("debt", "Prestito personale");
})();
</script>
</body>
</html>
"""

CONTACT_HTML = r"""
<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Richiedi una prima consulenza gratuita FixTude, senza impegno.">
<meta name="theme-color" content="#12365e">
<title>Consulenza gratuita | FixTude</title>
<style>__STYLE__</style>
</head>
<body>
__NAV__

<section class="hero" style="padding-top:42px;padding-bottom:48px">
    <div class="hero-inner">
        <span class="eyebrow">Passaggio 2 di 2 · Facoltativo</span>
        <h1>Facciamo chiarezza insieme.</h1>
        <p>
            Se desideri approfondire la tua situazione, puoi preparare
            una richiesta di ricontatto per una prima consulenza gratuita,
            senza impegno.
        </p>
    </div>
</section>

<main class="container">
    <div class="contact-grid">
        <section class="card contact-intro">
            <div class="section-label">Un primo confronto</div>
            <h2>Parliamo della tua esigenza.</h2>
            <p class="muted">
                Non devi acquistare nulla e non sei obbligato a proseguire.
                In questa fase bastano pochi dati di contatto.
            </p>
            <ul class="benefits">
                <li>Prima richiesta gratuita</li>
                <li>Nessun obbligo di acquistare servizi</li>
                <li>Non inviare documenti o dati bancari</li>
                <li>Il calcolo economico non viene allegato</li>
            </ul>
            <p class="muted" style="font-size:13px">
                Per motivi di riservatezza, non riportare nel messaggio
                dettagli di conti, codici fiscali o informazioni sensibili
                sui tuoi creditori.
            </p>
        </section>

        <section class="contact-panel">
            <h2>Richiedi un ricontatto</h2>
            <p class="muted" style="font-size:13px">
                Compila i campi. Si aprirà il tuo programma di posta
                con una bozza diretta a info@fixtude.it.
                La richiesta sarà inviata solo se deciderai di premere
                Invia nel tuo programma di posta.
            </p>

            <form id="contactForm" novalidate>
                <div class="field">
                    <label for="name">Nome</label>
                    <input id="name" maxlength="100" autocomplete="name"
                           placeholder="Il tuo nome">
                </div>

                <div class="field">
                    <label for="contact">E-mail o telefono *</label>
                    <input id="contact" maxlength="150"
                           autocomplete="email"
                           placeholder="Come possiamo contattarti?"
                           required>
                </div>

                <div class="field">
                    <label for="time">Quando preferisci essere contattato?</label>
                    <select id="time">
                        <option value="">Nessuna preferenza</option>
                        <option value="Mattina">Mattina</option>
                        <option value="Pomeriggio">Pomeriggio</option>
                        <option value="Sera">Sera</option>
                    </select>
                </div>

                <div class="field">
                    <label for="message">Messaggio facoltativo</label>
                    <textarea id="message" maxlength="500" rows="4"
                              placeholder="Descrivi brevemente la tua esigenza, senza inserire dati sensibili."></textarea>
                </div>

                <label class="check-line">
                    <input type="checkbox" id="privacyCheck" required>
                    <span>
                        Dichiaro di aver letto l'
                        <a href="/privacy" target="_blank">informativa privacy</a>
                        e chiedo di essere ricontattato per gestire questa richiesta.
                    </span>
                </label>

                <button type="submit" class="button button-primary button-full">
                    PREPARA LA RICHIESTA
                </button>

                <p id="contactError" class="notice hidden"
                   role="alert" style="margin-top:14px"></p>

                <p class="muted" style="font-size:12px;margin-top:14px">
                    La richiesta non viene salvata dal sito. Se invii
                    l'e-mail, i dati saranno trattati dal destinatario
                    per gestire il ricontatto secondo l'informativa.
                </p>
            </form>
        </section>
    </div>

    <p class="muted" style="text-align:center;font-size:13px">
        <a href="/misuratore">← Torna al misuratore</a>
    </p>
</main>

__FOOTER__

<script>
(function () {
    "use strict";

    const form = document.getElementById("contactForm");
    const error = document.getElementById("contactError");

    form.addEventListener("submit", function (event) {
        event.preventDefault();
        error.classList.add("hidden");

        const name = document.getElementById("name").value.trim();
        const contact = document.getElementById("contact").value.trim();
        const time = document.getElementById("time").value;
        const message = document.getElementById("message").value.trim();
        const privacy = document.getElementById("privacyCheck").checked;

        if (!contact) {
            error.textContent = "Inserisci un'e-mail o un numero di telefono.";
            error.classList.remove("hidden");
            return;
        }

        if (!privacy) {
            error.textContent =
                "Per proseguire, dichiara di aver letto l'informativa privacy.";
            error.classList.remove("hidden");
            return;
        }

        const subject = "Richiesta di prima consulenza gratuita FixTude";
        const body = [
            "Richiedo una prima consulenza gratuita, senza impegno.",
            "",
            "Nome: " + (name || "Non indicato"),
            "Contatto: " + contact,
            "Fascia oraria preferita: " + (time || "Nessuna preferenza"),
            "",
            "Messaggio: " + (message || "Nessun messaggio aggiuntivo"),
            "",
            "Ho dichiarato di aver letto l'informativa privacy."
        ].join("\n");

        const url = "mailto:info@fixtude.it?subject=" +
            encodeURIComponent(subject) +
            "&body=" + encodeURIComponent(body);

        window.location.href = url;
    });
})();
</script>
</body>
</html>
"""

PRIVACY_HTML = r"""
<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Informativa privacy | FixTude</title>
<style>__STYLE__</style>
</head>
<body>
__NAV__
<main class="container">
<section class="card">
    <div class="section-label">Privacy</div>
    <h1 style="font-size:32px">Informativa sul trattamento dei dati</h1>

    <div class="notice">
        Questa è una bozza da completare e verificare prima della
        pubblicazione. Occorre identificare il titolare, precisare le
        finalità e basi giuridiche, i tempi di conservazione, i diritti
        dell'interessato e i trattamenti effettivi dei fornitori.
    </div>

    <h2>1. Titolare del trattamento</h2>
    <p>
        Il titolare deve inserire qui la denominazione o il nome completo,
        l'indirizzo e i recapiti. Contatto attualmente previsto:
        <a href="mailto:info@fixtude.it">info@fixtude.it</a>.
    </p>

    <h2>2. Dati del misuratore</h2>
    <p>
        Il calcolo di entrate, uscite, rate e debito residuo viene eseguito
        nel browser dell'utente. Il codice del misuratore non invia questi
        importi al server e non li registra in un database. I valori
        rimangono temporaneamente nella pagina aperta e vengono persi
        quando la pagina viene ricaricata o chiusa.
    </p>

    <h2>3. Richiesta di ricontatto</h2>
    <p>
        Se l'utente compila il modulo, il sito prepara una bozza di e-mail
        sul dispositivo. Il messaggio viene trasmesso soltanto se l'utente
        lo invia dal proprio programma di posta. Il titolare deve indicare
        finalità, base giuridica, destinatari, tempi di conservazione e
        modalità per esercitare i diritti applicabili.
    </p>

    <h2>4. Dati tecnici e hosting</h2>
    <p>
        Il fornitore di hosting può trattare dati tecnici necessari
        all'erogazione e alla sicurezza del sito, come indirizzi IP e log.
        Prima del lancio occorre verificare la configurazione e la
        documentazione del fornitore effettivamente utilizzato.
    </p>

    <h2>5. Diritti e contatti</h2>
    <p>
        L'informativa definitiva deve indicare come contattare il titolare
        ed esercitare i diritti previsti dalla normativa applicabile.
    </p>
</section>
</main>
__FOOTER__
</body>
</html>
"""


def render_page(source):
    html = source.replace("__STYLE__", STYLE)
    html = html.replace("__NAV__", NAV)
    html = html.replace("__FOOTER__", FOOTER)
    response = make_response(render_template_string(html))
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=()"
    )
    return response


@app.route("/", methods=["GET"])
def home():
    return render_page(HOME_HTML)


@app.route("/misuratore", methods=["GET"])
def misuratore():
    return render_page(CALCULATOR_HTML)


@app.route("/consulenza", methods=["GET"])
def consulenza():
    return render_page(CONTACT_HTML)


@app.route("/privacy", methods=["GET"])
def privacy():
    return render_page(PRIVACY_HTML)


@app.route("/health", methods=["GET"])
def health():
    return {
        "status": "ok",
        "application": "FixTude V1 New",
        "pages": ["/", "/misuratore", "/consulenza", "/privacy"]
    }, 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
