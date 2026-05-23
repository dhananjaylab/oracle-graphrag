"""scratch/list_gemini_models.py — list accessible Gemini models."""
import os
from dotenv import load_dotenv
load_dotenv()

import google.generativeai as genai
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

print("Available Gemini models:\n")
for m in genai.list_models():
    methods = getattr(m, "supported_generation_methods", [])
    print(f"  {m.name}")
    print(f"    Methods: {methods}\n")
