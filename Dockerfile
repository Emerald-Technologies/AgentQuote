FROM python:3.12-slim

WORKDIR /app

# Install dependencies first (cacheable layer — code changes won't invalidate this)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Then copy source code
COPY . .

EXPOSE 8088

# Use a slim Python base to stay within the hosted agent CPU tier image size limit.
ENTRYPOINT ["python", "main.py"]
