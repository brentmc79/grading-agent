# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Service listens on port 8080 by default (Cloud Run will override PORT env var)
ENV PORT=8080

# Run uvicorn when the container launches. 
# We use shell form to allow environment variable expansion for PORT.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT}
