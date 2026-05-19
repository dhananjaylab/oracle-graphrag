#!/bin/bash

# Start FastAPI backend in the background
# It runs on port 8000 so the frontend can reach it internally at http://localhost:8000
uvicorn backend.main:app --host 127.0.0.1 --port 8000 &

# Wait a few seconds to let the backend start up
sleep 3

# Start Streamlit frontend on port 7860 (the default port Hugging Face exposes)
streamlit run frontend/app.py --server.port 7860 --server.address 0.0.0.0