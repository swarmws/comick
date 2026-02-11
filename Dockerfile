# Use official Python 3.11 slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements (we can create a small requirements.txt)
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the app code
COPY comick.py .

# Expose the port your app will run on
EXPOSE 4775

# Run with Gunicorn + Uvicorn workers
CMD ["gunicorn", "comick:app", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:4775"]
