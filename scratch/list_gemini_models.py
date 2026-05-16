import google.generativeai as genai
import os
from dotenv import load_dotenv

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

print("Listing models...")
try:
    for m in genai.list_models():
        print(f"- {m.name} (methods: {m.supported_generation_methods})")
except Exception as e:
    print(f"Error: {e}")
