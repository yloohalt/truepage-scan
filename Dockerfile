FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home scanner
COPY truepage ./truepage
USER scanner
EXPOSE 8000
CMD ["sh", "-c", "uvicorn truepage.server:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --no-access-log"]
