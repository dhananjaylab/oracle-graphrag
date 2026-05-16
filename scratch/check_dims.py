import google.generativeai as genai
import os
from dotenv import load_dotenv

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

text = "test"
result = genai.embed_content(
    model="models/gemini-embedding-001",
    content=text,
    task_type="retrieval_document",
)
print(f"Dimension of models/gemini-embedding-001: {len(result['embedding'])}")

try:
    result2 = genai.embed_content(
        model="models/text-embedding-004",
        content=text,
        task_type="retrieval_document",
    )
    print(f"Dimension of models/text-embedding-004: {len(result2['embedding'])}")
except Exception as e:
    print(f"models/text-embedding-004 failed: {e}")
