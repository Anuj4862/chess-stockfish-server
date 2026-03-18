FROM python:3.12-slim

RUN apt-get update && apt-get install -y wget && rm -rf /var/lib/apt/lists/*

# Download Stockfish 18 (released January 31, 2026) — latest and strongest
RUN wget -q https://github.com/official-stockfish/Stockfish/releases/download/sf_18/stockfish-ubuntu-x86-64-avx2.tar \
    && tar -xf stockfish-ubuntu-x86-64-avx2.tar \
    && mv stockfish/stockfish-ubuntu-x86-64-avx2 /usr/local/bin/stockfish \
    && chmod +x /usr/local/bin/stockfish \
    && rm -rf stockfish stockfish-ubuntu-x86-64-avx2.tar

WORKDIR /app
COPY stockfish_server.py .

ENV STOCKFISH=/usr/local/bin/stockfish
ENV THREADS=2
ENV HASH_MB=128

EXPOSE 3333
CMD ["python", "stockfish_server.py"]
