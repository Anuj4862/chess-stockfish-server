FROM python:3.12-slim

RUN apt-get update && apt-get install -y wget tar && rm -rf /var/lib/apt/lists/*

# ── Stockfish 18 — downloaded at build time (too large for GitHub) ────────────
RUN wget -q \
    "https://github.com/official-stockfish/Stockfish/releases/download/sf_18/stockfish-ubuntu-x86-64-avx2.tar" \
    -O /tmp/sf.tar && \
    tar -xf /tmp/sf.tar -C /tmp && \
    mv /tmp/stockfish/stockfish-ubuntu-x86-64-avx2 /usr/local/bin/stockfish && \
    chmod +x /usr/local/bin/stockfish && \
    rm -rf /tmp/sf.tar /tmp/stockfish && \
    echo "quit" | /usr/local/bin/stockfish | head -1 && \
    echo "✓ Stockfish 18 OK"

# ── Komodo 14 — committed directly in repo (small enough at 1.8MB) ────────────
COPY komodo /usr/local/bin/komodo
RUN chmod +x /usr/local/bin/komodo && \
    echo "quit" | /usr/local/bin/komodo | head -1 && \
    echo "✓ Komodo 14 OK"

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt --no-cache-dir
COPY stockfish_server.py .

ENV SF_PATH=/usr/local/bin/stockfish
ENV KOMODO_PATH=/usr/local/bin/komodo
ENV PORT=8080

EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--timeout", "120", "--workers", "2", "stockfish_server:app"]
