# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory
WORKDIR /app

# Install system dependencies (optional, but good for building certain Python packages)
RUN apt-get update && apt-get install -y \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Ensure Python knows where to find the "backend" and "frontend" modules
ENV PYTHONPATH=/app

# Expose the port Hugging Face expects
EXPOSE 7860

# Make the start script executable
RUN chmod +x start.sh

# Run the application
CMD ["./start.sh"]