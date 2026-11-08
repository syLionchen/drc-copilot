# Generates three SIMULATED German merchant documents in data/merchant_docs/ (safe to commit):
#
#   z_bericht_A_2026-10-15.pdf   "Kassensystem A": Z-Bericht in a table layout
#   z_bericht_B_2026-10-22.pdf   "Kassensystem B": Tagesabschluss, no tables, one item per line,
#                                different field names, German number format (1.234,56 EUR).
#                                ONE PLANTED INCONSISTENCY: Kartenzahlungen are 18 % LOWER than the
#                                card_revenue_eur of that day in data/verified_daily.csv (a merchant hiding
#                                revenue to pay less revenue share). Gesamtumsatz is reduced to match, so
#                                Bar + Karte + PayPal = Gesamtumsatz holds inside the document: the gap
#                                is only visible against external (card terminal / verified) data.
#   ustva_2026-10.pdf            Copy of the Umsatzsteuer-Voranmeldung for October 2026
#   answer_key.json              the true numbers of every document and the planted inconsistency
#
# Numbers come from the project's simulated history:
#   data/verified_daily.csv   verified_gross (= Umsatz brutto) and card_revenue_eur per day
#   data/daily_summary.csv    only what verified_daily.csv does not contain: cash / PayPal split,
#                             number of transactions and the voided (Storno) amount
# Every page carries the footer "SAMPLE DOCUMENT FOR DEMONSTRATION ONLY".
#
# Run (from the project root):  python -m data.make_merchant_docs
import csv
import json
from datetime import date, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path

from fpdf import FPDF

DATA = Path(__file__).resolve().parent
OUT_DIR = DATA / "merchant_docs"
VERIFIED_DAILY = DATA / "verified_daily.csv"
DAILY_SUMMARY = DATA / "daily_summary.csv"

DOC_A_DAY, DOC_B_DAY, USTVA_MONTH = "2026-10-15", "2026-10-22", "2026-10"
CARD_UNDERSTATEMENT = Decimal("0.82")       # planted: reported card payments are 18 % too low
SHARE_19 = Decimal("0.60")                  # 60 % of gross revenue is taxed at 19 %, 40 % at 7 %
FOOTER = "SAMPLE DOCUMENT FOR DEMONSTRATION ONLY"
COMPANY = "Kaffee und Tee GmbH, Hannover"
WEEKDAYS = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
CENT = Decimal("0.01")


# ---------------------------------------------------------------- helpers
def q2(x):
    return Decimal(x).quantize(CENT, rounding=ROUND_HALF_UP)


def de(x):
    """German number format: 1234.5 -> '1.234,50'."""
    return f"{q2(x):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def eur(x):
    return f"{de(x)} €"


def de_date(iso):
    d = date.fromisoformat(iso)
    return f"{d.day:02d}.{d.month:02d}.{d.year}"


def weekday(iso):
    return WEEKDAYS[date.fromisoformat(iso).weekday()]


def js(x):
    """Decimal -> plain string with 2 places for the answer key."""
    return f"{q2(x):.2f}"


def vat_split(gross):
    """60 % of the gross amount at 19 %, the rest at 7 %. Returns {rate: (gross, net, vat)}."""
    g19 = q2(gross * SHARE_19)
    g7 = gross - g19
    n19, n7 = q2(g19 / Decimal("1.19")), q2(g7 / Decimal("1.07"))
    return {"19": (g19, n19, g19 - n19), "7": (g7, n7, g7 - n7)}


def load_csv(path, hint):
    if not path.exists():
        raise SystemExit(f"{path} not found. Run: {hint}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def day_facts(iso, verified, summary):
    """Everything the documents need about one day, cross-checked between the two CSV files."""
    v = next((r for r in verified if r["date"] == iso), None)
    s = next((r for r in summary if r["date"] == iso), None)
    if v is None or s is None:
        raise SystemExit(f"{iso} is missing in data/verified_daily.csv or data/daily_summary.csv")
    gross, card = Decimal(v["verified_gross"]), Decimal(v["card_revenue_eur"])
    paypal, cash = Decimal(s["paypal_eur"]), Decimal(s["pos_cash_reported_eur"])
    if Decimal(s["pos_card_eur"]) != card or paypal + card + cash != gross:
        raise SystemExit(f"{iso}: the two CSV files disagree (verified_gross, card, cash and PayPal must add up). "
                         f"Regenerate them with: python -m data.generate_data and python -m ai.anomaly")
    txn_count = int(s["txn_count"])
    storno_count = int((Decimal(s["void_rate"]) * txn_count).quantize(Decimal(1), rounding=ROUND_HALF_UP))
    index = [r["date"] for r in verified].index(iso) + 1
    return {"date": iso, "z_nr": f"{index:04d}", "gross": gross, "card": card, "paypal": paypal, "cash": cash,
            "transactions": txn_count, "storno_count": storno_count, "storno_amount": Decimal(s["voided_eur"])}


class DocPDF(FPDF):
    def __init__(self):
        super().__init__(format="A4", unit="mm")
        self.core_fonts_encoding = "cp1252"          # lets the core fonts print the euro sign
        self.set_margins(18, 16, 18)
        self.set_auto_page_break(auto=True, margin=18)

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(110, 110, 110)
        self.cell(0, 6, FOOTER, align="C")
        self.set_text_color(0, 0, 0)


def grid_row(pdf, cells, widths, aligns, height=8, fill=None, bold=False):
    pdf.set_font("Helvetica", "B" if bold else "", 10)
    if fill:
        pdf.set_fill_color(*fill)
    for i, (text, width, align) in enumerate(zip(cells, widths, aligns)):
        last = i == len(cells) - 1
        pdf.cell(width, height, text, border=1, align=align, fill=bool(fill),
                 new_x="LMARGIN" if last else "RIGHT", new_y="NEXT" if last else "TOP")


def wrap_text(pdf, text, width):
    """Split text into lines that fit `width` (mm) with the current font. Explicit \\n starts a new line."""
    lines = []
    for part in text.split("\n"):
        current = ""
        for word in part.split(" "):
            candidate = f"{current} {word}".strip()
            if current and pdf.get_string_width(candidate) > width:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
    return lines


def wrapped_row(pdf, cells, widths, aligns, line_height=6, pad=2, fill=None, bold=False):
    """One table row whose cells wrap their text; the row is as tall as its tallest cell."""
    pdf.set_font("Helvetica", "B" if bold else "", 10)
    wrapped = [wrap_text(pdf, text, width - 2 * pad) for text, width in zip(cells, widths)]
    height = max(len(lines) for lines in wrapped) * line_height + 2 * pad
    if pdf.get_y() + height > pdf.h - pdf.b_margin:
        pdf.add_page()
        pdf.set_font("Helvetica", "B" if bold else "", 10)
    x, y = pdf.l_margin, pdf.get_y()
    if fill:
        pdf.set_fill_color(*fill)
    for lines, width, align in zip(wrapped, widths, aligns):
        pdf.rect(x, y, width, height, style="DF" if fill else "D")
        for i, line in enumerate(lines):
            pdf.set_xy(x + pad, y + pad + i * line_height)
            pdf.cell(width - 2 * pad, line_height, line, align=align)
        x += width
    pdf.set_xy(pdf.l_margin, y + height)


# ------------------------------------------------- document A: table layout
def build_doc_a(facts):
    pdf = DocPDF()
    pdf.add_page()
    pdf.set_fill_color(35, 35, 35)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 15)
    pdf.cell(0, 12, "  KASSENSYSTEM A   |   Z-Bericht (Tagesabschluss)", fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 8, f"{COMPANY}   -   Kasse 1", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    gray = (225, 225, 225)
    grid_row(pdf, ["Datum", "Z-Nr.", "Kasse", "Vorgänge"], [45, 40, 45, 44], ["L", "L", "L", "R"], fill=gray, bold=True)
    grid_row(pdf, [de_date(facts["date"]), facts["z_nr"], "1", str(facts["transactions"])],
             [45, 40, 45, 44], ["L", "L", "L", "R"])
    pdf.ln(5)

    widths, aligns = [90, 40, 44], ["L", "R", "R"]
    grid_row(pdf, ["Umsatz", "Anzahl", "Betrag EUR"], widths, aligns, fill=gray, bold=True)
    grid_row(pdf, ["Umsatz brutto", str(facts["transactions"]), de(facts["gross"])], widths, aligns, bold=True)
    grid_row(pdf, ["   davon Bar", "", de(facts["cash"])], widths, aligns)
    grid_row(pdf, ["   davon Karte", "", de(facts["card"])], widths, aligns)
    grid_row(pdf, ["   davon PayPal", "", de(facts["paypal"])], widths, aligns)
    pdf.ln(5)

    vat = vat_split(facts["gross"])
    widths, aligns = [40, 45, 45, 44], ["L", "R", "R", "R"]
    grid_row(pdf, ["Steuersatz", "Brutto EUR", "Netto EUR", "MwSt EUR"], widths, aligns, fill=gray, bold=True)
    for rate in ("19", "7"):
        gross, net, tax = vat[rate]
        grid_row(pdf, [f"MwSt {rate} %", de(gross), de(net), de(tax)], widths, aligns)
    grid_row(pdf, ["Summe", de(facts["gross"]), de(vat["19"][1] + vat["7"][1]), de(vat["19"][2] + vat["7"][2])],
             widths, aligns, bold=True)
    pdf.ln(5)

    widths, aligns = [90, 40, 44], ["L", "R", "R"]
    grid_row(pdf, ["Storno (Stornierungen)", "Anzahl", "Betrag EUR"], widths, aligns, fill=gray, bold=True)
    grid_row(pdf, ["Stornierte Vorgänge", str(facts["storno_count"]), de(facts["storno_amount"])], widths, aligns)
    pdf.ln(8)
    pdf.set_font("Helvetica", "", 9)
    next_day = (date.fromisoformat(facts["date"]) + timedelta(days=1)).isoformat()
    pdf.cell(0, 6, f"Z-Bericht erstellt am {de_date(next_day)}, 06:02 Uhr. Kassensystem A, Version 4.2 (Muster).",
             new_x="LMARGIN", new_y="NEXT")
    return pdf


# --------------------------------- document B: plain text list, other labels
def build_doc_b(facts, reported):
    pdf = DocPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 24)
    pdf.cell(0, 14, "Tagesabschluss", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    pdf.cell(0, 7, f"Kassensystem B  -  {COMPANY}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 7, f"{weekday(facts['date'])}, {de_date(facts['date'])}   |   Abschluss-Nr. B-{facts['z_nr']}",
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_draw_color(150, 150, 150)
    pdf.line(18, pdf.get_y(), 192, pdf.get_y())
    pdf.ln(5)

    vat = vat_split(reported["total"])
    lines = [
        ("Gesamtumsatz inkl. USt", eur(reported["total"])),
        ("Barzahlungen", eur(facts["cash"])),
        ("Kartenzahlungen", eur(reported["card"])),
        ("Online (PayPal)", eur(facts["paypal"])),
        ("Enthaltene USt 19 %", eur(vat["19"][2])),
        ("Enthaltene USt 7 %", eur(vat["7"][2])),
        ("Stornierte Vorgänge", f"{facts['storno_count']} Stück"),
        ("Stornosumme", eur(facts["storno_amount"])),
        ("Anzahl Bons", str(facts["transactions"])),
    ]
    for label, value in lines:
        pdf.set_font("Helvetica", "B" if label.startswith("Gesamtumsatz") else "", 13)
        pdf.cell(0, 9, f"{label}: {value}", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 6, "Abschluss automatisch erstellt (Kassensystem B, Muster).", new_x="LMARGIN", new_y="NEXT")
    return pdf


# ------------------------------------------------ document C: UStVA copy
def build_ustva(month_facts):
    pdf = DocPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 17)
    pdf.cell(0, 11, "Umsatzsteuer-Voranmeldung 2026", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "I", 11)
    pdf.cell(0, 7, "Kopie für die Unterlagen (Muster)", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    pdf.set_font("Helvetica", "", 11)
    for line in ("Voranmeldungszeitraum: Oktober 2026 (Monat 10)", "Steuernummer: 12/345/67890 (Muster)",
                 "Finanzamt: Hannover (Muster)", f"Unternehmen: {COMPANY}"):
        pdf.cell(0, 7, line, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    widths, aligns = [16, 86, 38, 34], ["C", "L", "R", "R"]
    gray = (225, 225, 225)
    # every cell wraps inside its own column, so no text can run into the next one
    wrapped_row(pdf, ["Kz", "Lieferungen und sonstige Leistungen", "Bemessungs-\ngrundlage EUR", "Steuer EUR"],
                widths, aligns, fill=gray, bold=True)
    rows = (("81", "Steuerpflichtige Umsätze zum Steuersatz von 19 % (ohne Umsatzsteuer)", "kz81", "tax81"),
            ("86", "Steuerpflichtige Umsätze zum Steuersatz von 7 % (ohne Umsatzsteuer)", "kz86", "tax86"))
    for kz, text, base_key, tax_key in rows:
        wrapped_row(pdf, [kz, text, de(month_facts[base_key]), de(month_facts[tax_key])], widths, aligns)
    wrapped_row(pdf, ["", "Summe der Umsatzsteuer (Muster, ohne Vorsteuerabzug)", "", de(month_facts["tax_total"])],
                widths, aligns, bold=True)
    pdf.ln(6)
    pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(0, 5, "Die Bemessungsgrundlagen sind auf volle Euro abgerundet angegeben. "
                         "Diese Kopie dient nur zur Demonstration.", new_x="LMARGIN", new_y="NEXT")
    return pdf


# -------------------------------------------------------------------- main
def main():
    verified = load_csv(VERIFIED_DAILY, "python -m ai.anomaly")
    summary = load_csv(DAILY_SUMMARY, "python -m data.generate_data")
    a, b = day_facts(DOC_A_DAY, verified, summary), day_facts(DOC_B_DAY, verified, summary)

    # planted inconsistency in document B: card payments 18 % BELOW the verified figure; the total is
    # reduced with it, so the document adds up internally (Bar + Karte + PayPal = Gesamtumsatz)
    card_reported = q2(b["card"] * CARD_UNDERSTATEMENT)
    b_reported = {"card": card_reported, "total": b["cash"] + card_reported + b["paypal"]}
    assert b["cash"] + card_reported + b["paypal"] == b_reported["total"]

    # UStVA: 60 % of October's verified gross revenue at 19 %, 40 % at 7 %, converted to net
    october = [r for r in verified if r["date"].startswith(USTVA_MONTH)]
    october_gross = sum((Decimal(r["verified_gross"]) for r in october), Decimal("0.00"))
    g19 = q2(october_gross * SHARE_19)
    g7 = october_gross - g19
    net19, net7 = g19 / Decimal("1.19"), g7 / Decimal("1.07")
    kz81, kz86 = net19.to_integral_value(rounding=ROUND_DOWN), net7.to_integral_value(rounding=ROUND_DOWN)
    tax81, tax86 = q2(kz81 * Decimal("0.19")), q2(kz86 * Decimal("0.07"))
    month_facts = {"kz81": kz81, "kz86": kz86, "tax81": tax81, "tax86": tax86, "tax_total": tax81 + tax86}

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    files = {
        f"z_bericht_A_{DOC_A_DAY}.pdf": build_doc_a(a),
        f"z_bericht_B_{DOC_B_DAY}.pdf": build_doc_b(b, b_reported),
        f"ustva_{USTVA_MONTH}.pdf": build_ustva(month_facts),
    }
    for name, pdf in files.items():
        pdf.output(str(OUT_DIR / name))
        print(f"wrote {OUT_DIR / name}")

    vat_a, vat_b = vat_split(a["gross"]), vat_split(b_reported["total"])
    split_note = "60 % of gross revenue at 19 %, 40 % at 7 %"
    answer_key = {
        "note": "Simulated documents for evaluation. Amounts are plain decimal strings in EUR.",
        "sources": {
            "verified_daily.csv": "verified_gross (Umsatz brutto) and card_revenue_eur per day",
            "daily_summary.csv": "cash / PayPal split, number of transactions and the voided amount "
                                 "(not available in verified_daily.csv)",
        },
        "documents": {
            f"z_bericht_A_{DOC_A_DAY}.pdf": {
                "type": "Z-Bericht", "system": "Kassensystem A", "layout": "tables", "date": DOC_A_DAY, "z_nr": a["z_nr"],
                "numbers": {
                    "umsatz_brutto": js(a["gross"]), "bar": js(a["cash"]), "karte": js(a["card"]),
                    "paypal": js(a["paypal"]), "vorgaenge": a["transactions"],
                    "mwst_19": {"brutto": js(vat_a["19"][0]), "netto": js(vat_a["19"][1]), "steuer": js(vat_a["19"][2])},
                    "mwst_7": {"brutto": js(vat_a["7"][0]), "netto": js(vat_a["7"][1]), "steuer": js(vat_a["7"][2])},
                    "storno_anzahl": a["storno_count"], "storno_betrag": js(a["storno_amount"]),
                },
                "vat_split_rule": split_note,
                "verified_reference": {"verified_gross": js(a["gross"]), "card_revenue_eur": js(a["card"])},
                "planted_inconsistencies": [],
            },
            f"z_bericht_B_{DOC_B_DAY}.pdf": {
                "type": "Tagesabschluss", "system": "Kassensystem B", "layout": "plain text list, German number format",
                "date": DOC_B_DAY, "abschluss_nr": f"B-{b['z_nr']}",
                "numbers": {
                    "gesamtumsatz_inkl_ust": js(b_reported["total"]), "barzahlungen": js(b["cash"]),
                    "kartenzahlungen": js(card_reported), "online_paypal": js(b["paypal"]),
                    "ust_19": js(vat_b["19"][2]), "ust_7": js(vat_b["7"][2]),
                    "stornierte_vorgaenge": b["storno_count"], "stornosumme": js(b["storno_amount"]),
                    "anzahl_bons": b["transactions"],
                },
                "vat_split_rule": split_note + " (of the reported total)",
                "verified_reference": {"verified_gross": js(b["gross"]), "card_revenue_eur": js(b["card"])},
                "internally_consistent": "Barzahlungen + Kartenzahlungen + Online (PayPal) = Gesamtumsatz inkl. USt",
                "planted_inconsistencies": [{
                    "description": "under-reported card revenue (-18%) vs card terminal data",
                    "field": "Kartenzahlungen",
                    "reported": js(card_reported), "verified": js(b["card"]),
                    "difference": js(card_reported - b["card"]), "deviation_percent": "-18",
                    "effect": f"Gesamtumsatz inkl. USt is {js(b['gross'] - b_reported['total'])} lower than "
                              f"verified_gross ({js(b_reported['total'])} vs {js(b['gross'])}); the document adds up "
                              f"internally, so the gap only shows against external data",
                    "simulated_motive": "merchant hides revenue to pay a smaller revenue share",
                    "detectable_by": "comparison with verified_daily.csv card_revenue_eur (card terminal data)",
                }],
            },
            f"ustva_{USTVA_MONTH}.pdf": {
                "type": "Umsatzsteuer-Voranmeldung (copy)", "period": "2026-10", "steuernummer": "12/345/67890 (Muster)",
                "numbers": {
                    "kennzahl_81_bemessungsgrundlage": f"{kz81:.0f}", "kennzahl_81_steuer": js(tax81),
                    "kennzahl_86_bemessungsgrundlage": f"{kz86:.0f}", "kennzahl_86_steuer": js(tax86),
                    "summe_umsatzsteuer": js(tax81 + tax86),
                },
                "derivation": {
                    "october_verified_gross_total": js(october_gross), "days": len(october),
                    "gross_at_19_percent": js(g19), "gross_at_7_percent": js(g7),
                    "net_19_exact": js(net19), "net_7_exact": js(net7),
                    "rule": split_note + "; net = gross / 1.19 and gross / 1.07; the form shows the net amounts "
                            "rounded down to whole euros",
                },
                "verified_reference": {"october_verified_gross": js(october_gross)},
                "planted_inconsistencies": [],
            },
        },
    }
    (OUT_DIR / "answer_key.json").write_text(json.dumps(answer_key, indent=2, ensure_ascii=False) + "\n",
                                             encoding="utf-8")
    print(f"wrote {OUT_DIR / 'answer_key.json'}")
    print(f"planted: Kartenzahlungen {eur(card_reported)} reported vs {eur(b['card'])} verified on {DOC_B_DAY} (-18 %)")


if __name__ == "__main__":
    main()
