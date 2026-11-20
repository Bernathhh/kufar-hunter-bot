FROM python:3.11-slim

WORKDIR /app

# Copy application files
COPY . /app/

# Environment variables
ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
