from fastapi import APIRouter
from backend.services.neo4j_service import get_schema_summary
from backend.models import SchemaResponse, TableSummary

router = APIRouter()

# Realistic banking example questions for the UI sidebar
EXAMPLE_QUESTIONS = [
    "Show total loan disbursements by branch for the current quarter",
    "List all transactions above ₹10 lakh in the last 30 days",
    "What is the NPA ratio by product segment as of this month end?",
    "Show month-over-month GL account balance movement this year",
    "Which customers have overdue EMI payments older than 90 days?",
    "Compare CASA balance across top 5 branches for the current year",
    "Show top 10 foreign currency transactions this week by amount",
]


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/schema", response_model=SchemaResponse)
async def schema():
    data = await get_schema_summary()
    tables = [
        TableSummary(
            name=t["name"],
            description=t.get("description") or "No description available",
            column_count=t.get("column_count", 0),
        )
        for t in data.get("tables", [])
    ]
    return SchemaResponse(tables=tables, total_tables=len(tables))


@router.get("/examples")
async def examples():
    return {"examples": EXAMPLE_QUESTIONS}
