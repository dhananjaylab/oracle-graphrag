"""scratch/check_dims.py — confirm embedding model dimensions before ingestion."""
import os, sys
from dotenv import load_dotenv
load_dotenv()

import google.generativeai as genai
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

for model_name in ["models/gemini-embedding-001", "models/text-embedding-004"]:
    try:
        result = genai.embed_content(
            model=model_name, content="test banking schema",
            task_type="retrieval_document",
        )
        print(f"✅ {model_name}: {len(result['embedding'])} dims")
    except Exception as e:
        print(f"❌ {model_name}: {e}")
