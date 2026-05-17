FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libc6-dev \
    iproute2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application source
COPY src/ ./src/

# Set Python path to find the src module
ENV PYTHONPATH=/app

# Expose the web application port
EXPOSE 8000

# Run the application
CMD ["python", "src/app.py"]
