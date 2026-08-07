FROM python:3.12-slim

# Empêche Python de bufferiser stdout/stderr (logs visibles immédiatement sur Render)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dépendances système minimales (build de asyncpg/bcrypt notamment)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x ./start.sh

EXPOSE 8000

# Render injecte la variable $PORT dynamiquement — start.sh la respecte.
CMD ["./start.sh"]
