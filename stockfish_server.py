"""
Chess Review App — Stockfish TCP Server
Deploy on Railway.app (free $5/month credit)

Protocol (same as our app's network engine):
  Client sends: UCI commands (position, go, stop, etc.)
  Server sends:  Stockfish stdout lines back

Usage:
  python stockfish_server.py
  
Environment variables:
  PORT       — port to listen on (Railway sets this automatically)
  STOCKFISH  — path to stockfish binary (default: stockfish)
  THREADS    — number of threads (default: 2)
  HASH_MB    — hash table size in MB (default: 128)
"""

import asyncio
import os
import logging
import subprocess
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("chess-server")

PORT       = int(os.environ.get("PORT", 3333))
SF_PATH    = os.environ.get("STOCKFISH", "stockfish")
THREADS    = int(os.environ.get("THREADS", 2))
HASH_MB    = int(os.environ.get("HASH_MB", 128))

# One Stockfish process per client connection
# Each client gets dedicated engine — no shared state

class StockfishSession:
    def __init__(self, client_addr):
        self.addr = client_addr
        self.proc = None

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            SF_PATH,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        log.info(f"[{self.addr}] Stockfish started (pid {self.proc.pid})")
        # Send init options
        self._send("uci")
        self._send(f"setoption name Threads value {THREADS}")
        self._send(f"setoption name Hash value {HASH_MB}")
        self._send("setoption name Use NNUE value true")
        self._send("isready")

    def _send(self, cmd: str):
        if self.proc and self.proc.stdin:
            self.proc.stdin.write((cmd + "\n").encode())

    async def flush(self):
        if self.proc and self.proc.stdin:
            await self.proc.stdin.drain()

    async def stop(self):
        if self.proc:
            try:
                self._send("quit")
                await self.flush()
                await asyncio.wait_for(self.proc.wait(), timeout=2.0)
            except Exception:
                self.proc.kill()
            log.info(f"[{self.addr}] Stockfish stopped")


async def handle_client(reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter):
    addr = writer.get_extra_info("peername")
    log.info(f"[{addr}] Connected")

    session = StockfishSession(addr)
    try:
        await session.start()
    except FileNotFoundError:
        log.error(f"Stockfish not found at: {SF_PATH}")
        writer.close()
        return

    # Forward Stockfish stdout → client
    async def sf_to_client():
        try:
            while True:
                line = await session.proc.stdout.readline()
                if not line:
                    break
                writer.write(line)
                await writer.drain()
        except Exception:
            pass

    # Forward client → Stockfish stdin
    async def client_to_sf():
        try:
            while True:
                data = await reader.readline()
                if not data:
                    break
                cmd = data.decode(errors="ignore").strip()
                if cmd:
                    session._send(cmd)
                    await session.flush()
        except Exception:
            pass

    # Run both directions concurrently
    sf_task     = asyncio.create_task(sf_to_client())
    client_task = asyncio.create_task(client_to_sf())

    done, pending = await asyncio.wait(
        [sf_task, client_task],
        return_when=asyncio.FIRST_COMPLETED
    )
    for t in pending:
        t.cancel()

    await session.stop()
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass
    log.info(f"[{addr}] Disconnected")


async def main():
    server = await asyncio.start_server(
        handle_client, "0.0.0.0", PORT
    )
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    log.info(f"Chess Review Stockfish Server listening on {addrs}")
    log.info(f"Stockfish: {SF_PATH} | Threads: {THREADS} | Hash: {HASH_MB}MB")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Server stopped")
