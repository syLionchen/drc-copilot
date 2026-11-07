# Generates data/sample_contract.pdf: a 2-page English "Daily Revenue Contribution
# Agreement" between DRC Fund I (investor) and Kaffee und Tee GmbH (merchant).
# It is the input for ai/contract_parser.py. SAMPLE document, not legal advice.
#
# Run:  python -m data.make_contract
#
# Uses fpdf2 core fonts (Latin-1 only), so the text sticks to plain ASCII.
# Sections are numbered automatically; cross-references use {sec_<key>} placeholders.
# The font size is chosen automatically so the contract fits exactly 2 pages.
from pathlib import Path

from fpdf import FPDF

PDF_PATH = Path(__file__).resolve().parent / "sample_contract.pdf"
FONT_SIZES = (12, 11.5, 11, 10.5, 10, 9.5)

TITLE = "DAILY REVENUE CONTRIBUTION AGREEMENT"
SUBTITLE = "Effective Date: 2026-11-06"

PREAMBLE = (
    'This Daily Revenue Contribution Agreement (the "Agreement") is entered into on 2026-11-06 '
    'between DRC Fund I (the "Investor") and Kaffee und Tee GmbH, a limited liability company '
    'with its place of business in Hannover, Germany (the "Merchant").'
)
BACKGROUND = (
    "The Merchant operates a cafe and tea business and receives customer payments through PayPal "
    "and in its shop. The Merchant wishes to obtain growth financing from the Investor in exchange "
    "for a share of its daily revenue, and the Investor is willing to provide it, on the terms "
    "set out below."
)

# (key, heading, body) - page 1 holds the money terms, page 2 the covenants.
SECTIONS_P1 = [
    ("definitions", "Definitions",
     '"Trading Day" means a day on which the Merchant is open for business. Days on which the '
     "Merchant is closed, such as public holidays and rest days notified to the Investor in "
     "advance, are not Trading Days. A day with no completed transactions that was not notified "
     "in advance as a closure day counts as a Trading Day with zero receipts. Trading Days and "
     "daily cut-off times are determined in the Europe/Berlin time zone. "
     '"Total Receipts" means all amounts received by the Merchant from customers for completed '
     "sales through any payment channel, including PayPal, card, cash and bank transfer. "
     '"Verified Net Revenue" means, for each Trading Day, the Merchant\'s Total Receipts, net of '
     "PayPal receiving fees, excluding any transaction that the Servicer's verification process "
     "identifies as not genuine, such as self-dealing, voided or refunded transactions. "
     '"Baseline Threshold" means 90% of the average daily Verified Net Revenue per Trading Day '
     "over the 90 days before the Funding Date, fixed at signing at EUR 703.26. "
     '"Servicer" means the servicing agent appointed by the Investor. "Escrow Account" means '
     'the PayPal business account "DRC Fund I Escrow" held for the benefit of the Investor.'),
    ("investment", "Investment",
     'The Investor shall pay the Merchant EUR 20,000 (the "Investment Amount"). The Investment '
     'Amount is funded on 2026-11-06 (the "Funding Date").'),
    ("share", "Revenue Share",
     "On each Trading Day starting on the Funding Date, the Merchant shall contribute to the "
     "Investor 5% of Verified Net Revenue per Trading Day up to the Baseline Threshold of "
     "EUR 703.26, and 3% on the portion of Verified Net Revenue above the Baseline Threshold "
     '(together, the "Daily Contribution").'),
    ("cap", "Repayment Cap and Automatic Termination",
     "The Merchant's total Daily Contributions are capped at 1.15 times the Investment Amount, "
     'being EUR 23,000 (the "Repayment Cap"). This Agreement terminates automatically when '
     "cumulative contributions reach the Repayment Cap. On the day the cap is reached, the Daily "
     "Contribution is reduced so that cumulative contributions equal the Repayment Cap exactly. "
     "Payout fees do not count toward the Repayment Cap."),
    ("term", "Maximum Term",
     "The maximum term of this Agreement is 24 months from the Funding Date. This Agreement ends "
     "on the earlier of the date on which the Repayment Cap is reached and the end of the maximum "
     "term, after which no further Daily Contribution is due."),
    ("settlement", "Settlement",
     "The Daily Contribution is settled daily via PayPal Payouts to the Escrow Account. The full "
     "Daily Contribution is transferred to the Escrow Account. PayPal payout fees are borne by "
     "the Merchant in addition to the Daily Contribution; they are charged to the Merchant "
     "separately and are not deducted from the amount transferred. Payout fees do not count "
     "toward the Repayment Cap."),
    ("collection", "Collection",
     "Contributions are collected daily from the Merchant's PayPal balance; if the balance is "
     "insufficient, the shortfall is collected by SEPA direct debit from the Merchant's "
     "nominated bank account. For this purpose the Merchant shall, on or before the Funding "
     "Date, grant a SEPA direct debit mandate in favour of the Investor (or the Servicer acting "
     "on its behalf) and shall keep the mandate in force while this Agreement is in force."),
]
SECTIONS_P2 = [
    ("servicing", "Servicing Fee",
     'The Servicer is entitled to a servicing fee of 5% of each Daily Contribution (the '
     '"Servicing Fee"). The Servicing Fee is deducted inside the escrow pool before any '
     "distribution to the Investor. The remainder of each Daily Contribution is distributed to "
     "the Investor."),
    ("health", "Health Covenant",
     "If the average Verified Net Revenue over the last 7 Trading Days falls more than 20% "
     "below the average over the last 30 Trading Days, the revenue share rate of 5% may be "
     "reduced to 3% for up to 30 days, subject to the approval of the Investor. The 3% rate on "
     "the portion above the Baseline Threshold is not affected. No reduction applies without the "
     "Investor's approval."),
    ("data", "Data Covenant",
     "The Merchant authorises the Investor and the Servicer to have read-only access to the "
     "Merchant's PayPal transaction data for the purpose of verifying revenue, and shall not "
     "restrict or revoke this access while this Agreement is in force. Unexplained card-revenue "
     "gaps of 3 or more Trading Days constitute a reporting breach by the Merchant."),
    ("channel", "Payment Channel Covenant",
     "The Merchant shall continue to accept customer payments through PayPal, including card "
     "payments processed by PayPal, on the same basis as before the Funding Date. The Merchant "
     "shall not direct, incentivise or encourage customers to use other means of payment, or "
     "otherwise divert sales away from PayPal, in order to reduce Verified Net Revenue. "
     "If the PayPal share of Total Receipts falls more than 15 percentage points below its "
     "90-day pre-funding baseline, the Servicer may request a written explanation and a review; "
     "this alone is not an event of default. Cash and other receipts outside PayPal must be "
     "reported under Section {sec_reporting}."),
    ("reporting", "Financial and Tax Reporting",
     "The Merchant shall deliver to the Investor in electronic form: (a) within 10 days after "
     "each month-end, a statement of Total Receipts for that month by payment channel, including "
     "cash and other receipts outside PayPal; (b) within 14 days after filing, a copy of each "
     "periodic VAT return (Umsatzsteuer-Voranmeldung); and (c) within 6 months after each "
     "financial year-end, its annual financial statements and the related income tax filing or "
     "assessment. On reasonable notice, the Investor or the Servicer may inspect the books and "
     "records supporting these reports, including cash receipts. Failure to deliver a report on "
     "time is a reporting breach."),
    ("representations", "Representations",
     "The Merchant represents that all transactions it reports are genuine sales to independent "
     "customers. The Merchant shall not create artificial, circular or self-dealing transactions, "
     "and shall not void or re-price sales in order to reduce Verified Net Revenue."),
    ("breach", "Consequences of Breach",
     "Upon a reporting breach or a breach of Section {sec_channel}, the Merchant shall provide a "
     "written explanation to the Investor without undue delay, and the Investor may withhold its "
     "approval of any rate reduction under Section {sec_health} until the breach is remedied."),
    ("law", "Governing Law and General Provisions",
     "This Agreement is governed by the laws of the Federal Republic of Germany. The place of "
     "jurisdiction is Hannover. This Agreement is the entire agreement between the parties on "
     "its subject matter, and amendments require written form and the signature of both parties."),
]

# section numbers, e.g. REFS["sec_health"] == "9"
REFS = {f"sec_{key}": str(i)
        for i, (key, _, _) in enumerate(SECTIONS_P1 + SECTIONS_P2, start=1)}


class ContractPDF(FPDF):
    def footer(self):
        self.set_y(-14)
        self.set_font("Times", "I", 8)
        self.cell(0, 8, f"SAMPLE DOCUMENT FOR DEMONSTRATION ONLY - Page {self.page_no()} of {{nb}}",
                  align="C")


def write_section(pdf, size, key, heading, body):
    pdf.set_font("Times", "", size)
    text = f"**{REFS['sec_' + key]}. {heading}.** {body.format(**REFS)}"
    pdf.multi_cell(0, size * 0.5, text, align="J", markdown=True,
                   new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2.5)


def build_pdf(size):
    pdf = ContractPDF(format="A4", unit="mm")
    pdf.alias_nb_pages()
    pdf.set_margins(22, 20, 22)
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.set_title("Daily Revenue Contribution Agreement (sample)")
    pdf.set_author("DRC Copilot - sample document")

    # ---- page 1
    pdf.add_page()
    pdf.set_font("Times", "B", 16)
    pdf.cell(0, 10, TITLE, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Times", "I", size)
    pdf.cell(0, 7, SUBTITLE, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)
    pdf.set_font("Times", "", size)
    for text in (PREAMBLE, BACKGROUND):
        pdf.multi_cell(0, size * 0.5, text, align="J", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2.5)
    for key, heading, body in SECTIONS_P1:
        write_section(pdf, size, key, heading, body)

    # ---- page 2
    pdf.add_page()
    for key, heading, body in SECTIONS_P2:
        write_section(pdf, size, key, heading, body)

    pdf.ln(2)
    pdf.set_font("Times", "", size)
    pdf.multi_cell(0, size * 0.5, "Signed in two originals, one for each party.", align="L",
                   new_x="LMARGIN", new_y="NEXT")
    pdf.ln(10)
    y = pdf.get_y()
    left_x, right_x, width = 22, 112, 76
    pdf.set_xy(left_x, y)
    pdf.cell(width, 5, "For DRC Fund I (Investor)")
    pdf.set_xy(right_x, y)
    pdf.cell(width, 5, "For Kaffee und Tee GmbH (Merchant)")
    pdf.line(left_x, y + 16, left_x + width, y + 16)
    pdf.line(right_x, y + 16, right_x + width, y + 16)
    pdf.set_font("Times", "I", 9)
    pdf.set_xy(left_x, y + 17)
    pdf.cell(width, 5, "Name, title, date")
    pdf.set_xy(right_x, y + 17)
    pdf.cell(width, 5, "Name, title, date")
    return pdf


def main():
    for size in FONT_SIZES:
        pdf = build_pdf(size)
        if pdf.page_no() == 2:
            pdf.output(str(PDF_PATH))
            print(f"Wrote {PDF_PATH} (2 pages, Times {size} pt)")
            return
    raise SystemExit("Contract text does not fit on 2 pages even at the smallest font size.")


if __name__ == "__main__":
    main()
