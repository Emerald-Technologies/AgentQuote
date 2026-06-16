FROM mcr.microsoft.com/devcontainers/python:3.12

WORKDIR /app

# Install dependencies first (cacheable layer — code changes won't invalidate this)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Then copy source code
COPY . .

EXPOSE 8088

CMD ["python", "main.py"]
