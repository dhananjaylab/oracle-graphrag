"""
backend/prompts/enrichment_prompt.py  (v2)

Changes from v1:
  - Accepts db_name and domain_hint to give Gemini richer context
    (e.g. "Risk Management DB, NPA domain" primes better descriptions
    than sending raw column names from RISK.NPA_MASTER without context)
  - Expanded abbreviation guide covering risk + market risk domains
  - PII detection prompt reinforced with banking-specific PII patterns
"""


def build_enrichment_prompt(
    table_name: str,
    table_comment: str,
    columns: list[dict],
    db_name: str = "",
    domain_hint: str = "",
) -> str:
    """
    Build a Gemini prompt to enrich cryptic Oracle column names.

    Sends ONLY schema metadata (names + data types + existing comments).
    No actual data values are ever included.

    Args:
        table_name:    Oracle table name (e.g. LOAN_MASTER)
        table_comment: Existing Oracle table comment (may be empty/cryptic)
        columns:       list of column dicts with keys: column_name, data_type,
                       col_comment (may be empty)
        db_name:       Human-readable database name for context (e.g. "Core Banking")
        domain_hint:   Business domain hint (e.g. "Lending — loan origination and EMI")
    """

    col_lines: list[str] = []
    for c in columns:
        line = f"  - {c['column_name']} ({c['data_type']}"
        if c.get("data_precision") and c.get("data_scale") is not None:
            line += f"({c['data_precision']},{c['data_scale']})"
        elif c.get("data_length") and c["data_type"] in ("VARCHAR2", "CHAR", "NVARCHAR2"):
            line += f"({c['data_length']})"
        if c.get("col_comment"):
            line += f', existing comment: "{c["col_comment"]}"'
        if c.get("nullable") == "N":
            line += ", NOT NULL"
        line += ")"
        col_lines.append(line)

    context_lines: list[str] = []
    if db_name:
        context_lines.append(f"Database: {db_name}")
    if domain_hint:
        context_lines.append(f"Business domain: {domain_hint}")
    if table_comment:
        context_lines.append(f"Existing table description: {table_comment}")
    context_block = "\n".join(context_lines)

    return f"""You are a banking data dictionary expert.
Given cryptic Oracle database column names from a core banking / risk management system,
generate clear business labels and descriptions for each column.

{context_block}
Table name: {table_name}

Columns to enrich:
{chr(10).join(col_lines)}

═══════════════════════════════════════════════
BANKING ABBREVIATION GUIDE
═══════════════════════════════════════════════

General:
  TXN=Transaction, AMT=Amount, DT=Date, CD=Code, NO=Number, NM=Name, ID=Identifier
  HDR=Header, DTL=Detail, MST/MSTR=Master reference, REF=Reference
  SEQ=Sequence, FLG=Flag (Y/N), IND=Indicator, TYP=Type, CAT/CATG=Category
  EFF=Effective, EXPIRY=Expiry date, LIMIT=Credit limit, STATUS=Status

Currency & amounts:
  FCY=Foreign Currency, LCY=Local Currency (INR), CCY=Currency Code (ISO)
  CR=Credit, DR=Debit, BAL=Balance, OS=Outstanding

Dates & periods:
  MTD=Month-to-Date, YTD=Year-to-Date, QTD=Quarter-to-Date
  PREV=Previous, CUR=Current, NEXT=Next

Core banking:
  ACCT=Account, CUST=Customer, BRCH=Branch, GL=General Ledger, COA=Chart of Accounts
  CASA=Current and Savings Account, SB=Savings Bank, CA=Current Account
  PROD=Product, SGMT=Segment, RM=Relationship Manager
  KYC=Know Your Customer, PAN=Permanent Account Number, UID=Aadhaar UID

Lending:
  LOAN=Loan, DISB=Disbursement, EMI=Equated Monthly Installment
  LTV=Loan-to-Value, ROI=Rate of Interest, MORATORIUM=Moratorium period
  SANCTION=Sanctioned amount, OUTST=Outstanding, PRIN=Principal, INT=Interest

Risk & NPA:
  NPA=Non-Performing Asset, SUB=Substandard, DBT=Doubtful, LOSS=Loss
  PROV=Provision, ECL=Expected Credit Loss, IFRS=IFRS 9 classification
  PD=Probability of Default, LGD=Loss Given Default, EAD=Exposure at Default
  SMA=Special Mention Account (SMA-0/1/2), OVR=Overdue
  RATING=Credit rating grade, EXPOSURE=Total credit exposure

Market risk:
  FX=Foreign Exchange, FWD=Forward contract, SPOT=Spot rate
  IR=Interest Rate, VaR=Value at Risk, DV01=Dollar Value of 01 bps
  MTM=Mark-to-Market, PNL=Profit and Loss

═══════════════════════════════════════════════
PII IDENTIFICATION RULES
═══════════════════════════════════════════════
Mark is_pii = true for columns that store:
  - Customer name, father's name, mother's name, nominee name
  - Date of birth, age
  - PAN number, Aadhaar / UID number, passport number, driving licence
  - Mobile number, home phone, work phone
  - Personal email address
  - Residential address, city, pin code (customer-level, not branch)
  - Bank account number (full), credit/debit card number
  - Income, salary, tax details

Mark is_pii = false for:
  - Branch/office address, branch codes
  - Product codes, GL codes, transaction types
  - Account balance aggregates
  - Internal system IDs and sequence numbers

═══════════════════════════════════════════════
RULES
═══════════════════════════════════════════════
1. Infer meaning from TABLE context + column name + data type TOGETHER
2. Labels: 2–5 words, Title Case, no abbreviations
3. Descriptions: one clear sentence — what does this column store / represent?
4. For amount columns: note the currency context (LCY = Indian Rupees, FCY = foreign)
5. For date columns: note whether it's a transaction date, value date, effective date, etc.
6. Return ONLY valid JSON — no preamble, no explanation, no markdown fences

JSON structure (one object per column, in input order):
[
  {{
    "column":      "EXACT_COLUMN_NAME_AS_GIVEN",
    "label":       "Human Readable Label",
    "description": "One clear sentence describing what this column stores.",
    "is_pii":      false
  }}
]"""
