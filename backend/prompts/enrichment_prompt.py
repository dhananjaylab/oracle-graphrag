def build_enrichment_prompt(table_name: str, table_comment: str, columns: list[dict]) -> str:
    """
    Build a prompt that sends ONLY schema metadata (no data) to Gemini
    and asks it to generate business-readable labels and descriptions
    for cryptic Oracle column names.
    """
    col_lines = []
    for c in columns:
        line = f"  - {c['column_name']} ({c['data_type']}"
        if c.get("col_comment"):
            line += f", existing comment: {c['col_comment']}"
        line += ")"
        col_lines.append(line)

    table_context = ""
    if table_comment:
        table_context = f"Existing table description: {table_comment}\n"

    return f"""You are a banking data dictionary expert.
Given cryptic Oracle database column names from a core banking system, generate clear business labels and descriptions.

Table name: {table_name}
{table_context}
Columns to enrich:
{chr(10).join(col_lines)}

BANKING ABBREVIATION GUIDE:
TXN=Transaction, AMT=Amount, DT=Date, CD=Code, NO/NM=Number/Name, ID=Identifier
FCY=Foreign Currency, LCY=Local Currency, CCY=Currency Code
ACCT=Account, CUST=Customer, BRCH=Branch, GL=General Ledger
BAL=Balance, CR=Credit, DR=Debit, INT=Interest Rate
PROD=Product, SGMT=Segment, CATG=Category, TYP=Type
MTD=Month-to-Date, YTD=Year-to-Date, QTD=Quarter-to-Date
NPA=Non-Performing Asset, CASA=Current and Savings Account
EMI=Equated Monthly Installment, LTV=Loan-to-Value Ratio
HDR=Header, DTL=Detail, MST/MASTER=Master reference table
REF=Reference, SEQ=Sequence, FLG=Flag, IND=Indicator
DISB=Disbursement, OUT=Outstanding, OVR=Overdue/Override
LIMIT=Credit Limit, EXPIRY=Expiry, EFF=Effective

RULES:
1. Infer meaning from the table context + column name + data type together
2. Labels should be 2-4 words, Title Case
3. Descriptions should be one clear sentence (what does this column store?)
4. If a column is clearly a PII field (name, phone, email, PAN, Aadhaar), note it in the description
5. Return ONLY valid JSON — no other text, no markdown, no code fences

Return this exact JSON structure:
[
  {{
    "column": "EXACT_COLUMN_NAME",
    "label": "Human Readable Label",
    "description": "Clear one-sentence description of what this column stores",
    "is_pii": false
  }}
]"""
