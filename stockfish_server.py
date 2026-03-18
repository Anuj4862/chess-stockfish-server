#!/usr/bin/env python3
"""
Chess Multi-Engine Analysis Server
Runs Stockfish 18 + Komodo Dragon in parallel.
Returns weighted-average evaluation via HTTP JSON API.

POST /analyze
  moves, movetime, sf_depth, komodo_depth, sf_weight, multipv
GET  /health
"""

import subprocess, threading, time, json, math, os, shutil
from typing import Optional
from flask import Flask, request, jsonify

app = Flask(__name__)

SF_PATH     = os.environ.get('SF_PATH',     '/usr/local/bin/stockfish')
KOMODO_PATH = os.environ.get('KOMODO_PATH', '/usr/local/bin/komodo')

def _find(primary, fallbacks):
    for p in [primary] + fallbacks:
        if p and (shutil.which(p) or os.path.isfile(p)):
            return p
    return None

SF_EXE     = _find(SF_PATH,     ['/engines/stockfish', 'stockfish'])
KOMODO_EXE = _find(KOMODO_PATH, ['/engines/komodo', '/engines/dragon', 'komodo', 'dragon'])


class UCIEngine:
    def __init__(self, path, name, threads=2, hash_mb=128):
        self.name  = name
        self.path  = path
        self._lock = threading.Lock()
        self._proc = None
        self.ready = False
        self._start(threads, hash_mb)

    def _send(self, cmd):
        if self._proc and self._proc.stdin:
            self._proc.stdin.write(cmd + '\n')
            self._proc.stdin.flush()

    def _wait_for(self, token, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self._proc.stdout.readline()
            if token in line:
                return True
        return False

    def _start(self, threads, hash_mb):
        try:
            self._proc = subprocess.Popen(
                [self.path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1)
            self._send('uci')
            self._wait_for('uciok', 10)
            self._send(f'setoption name Threads value {threads}')
            self._send(f'setoption name Hash value {hash_mb}')
            if self.name == 'stockfish':
                self._send('setoption name Use NNUE value true')
            self._send('isready')
            self._wait_for('readyok', 10)
            self.ready = True
            print(f'[{self.name}] ready OK')
        except Exception as e:
            print(f'[{self.name}] FAILED: {e}')

    def analyze(self, moves, movetime, depth=0, multipv=1):
        if not self.ready or not self._proc:
            return {'cp': 0, 'mate': None, 'move': '', 'depth': 0}
        with self._lock:
            pos = 'position startpos'
            if moves:
                pos += ' moves ' + ' '.join(moves)
            self._send(pos)
            self._send(f'setoption name MultiPV value {max(1, multipv)}')
            self._send('isready')
            self._wait_for('readyok', 3)
            go = f'go movetime {movetime}'
            if depth > 0:
                go = f'go depth {depth} movetime {movetime}'
            self._send(go)

            best_cp, best_mate, best_move, depth_reached = 0, None, '', 0
            is_white = (len(moves) % 2) == 0
            deadline = time.time() + movetime / 1000.0 + 5.0

            while time.time() < deadline:
                line = self._proc.stdout.readline().strip()
                if not line:
                    continue
                parts = line.split()
                is_mpv1 = 'multipv 1' in line or 'multipv' not in line
                if line.startswith('info') and is_mpv1:
                    if 'depth' in parts:
                        di = parts.index('depth')
                        if di + 1 < len(parts):
                            depth_reached = int(parts[di+1])
                    if 'score cp' in line:
                        ci = parts.index('cp')
                        raw = int(parts[ci+1])
                        best_cp   = raw if is_white else -raw
                        best_mate = None
                    elif 'score mate' in line:
                        mi = parts.index('mate')
                        m  = int(parts[mi+1])
                        best_mate = m if is_white else -m
                        best_cp   = (10000 if m > 0 else -10000) if is_white else (-10000 if m > 0 else 10000)
                if line.startswith('bestmove'):
                    if len(parts) > 1 and parts[1] != '(none)':
                        best_move = parts[1]
                    break

            return {'cp': best_cp, 'mate': best_mate, 'move': best_move, 'depth': depth_reached}


_engines = {}

def _init_engines():
    if SF_EXE:
        _engines['stockfish'] = UCIEngine(SF_EXE, 'stockfish', threads=2, hash_mb=128)
    else:
        print('WARNING: Stockfish not found')
    if KOMODO_EXE:
        _engines['komodo'] = UCIEngine(KOMODO_EXE, 'komodo', threads=2, hash_mb=128)
    else:
        print('WARNING: Komodo not found — SF only mode')

def _cp_to_pawns(cp):
    return max(-10.0, min(10.0, cp / 100.0))

def _winprob(pawns):
    return 1.0 / (1.0 + math.pow(10.0, -pawns / 4.0))


@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'engines': [n for n,e in _engines.items() if e.ready]})


@app.route('/analyze', methods=['POST'])
def analyze():
    data         = request.get_json(force=True, silent=True) or {}
    moves        = data.get('moves',         [])
    movetime     = int(data.get('movetime',  800))
    sf_depth     = int(data.get('sf_depth',  22))
    ko_depth     = int(data.get('komodo_depth', 18))
    sf_weight    = float(data.get('sf_weight', 0.65))
    ko_weight    = 1.0 - sf_weight
    multipv      = int(data.get('multipv',   1))

    results, errors = {}, []

    def run(name, engine, depth):
        try:
            results[name] = engine.analyze(moves, movetime, depth, multipv)
        except Exception as e:
            errors.append(f'{name}: {e}')
            results[name] = {'cp': 0, 'mate': None, 'move': '', 'depth': 0}

    threads = []
    for name, engine in _engines.items():
        if not engine.ready:
            continue
        d = sf_depth if name == 'stockfish' else ko_depth
        t = threading.Thread(target=run, args=(name, engine, d), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join(timeout=movetime/1000.0 + 6.0)

    if not results:
        return jsonify({'error': 'no engine results', 'details': errors}), 503

    sf     = results.get('stockfish', {})
    komodo = results.get('komodo',    {})
    sf_cp     = sf.get('cp', 0)
    ko_cp     = komodo.get('cp', sf_cp)

    if 'komodo' in results:
        w_cp = int(sf_cp * sf_weight + ko_cp * ko_weight)
    else:
        w_cp = sf_cp
        sf_weight, ko_weight = 1.0, 0.0

    sf_p, ko_p, w_p = _cp_to_pawns(sf_cp), _cp_to_pawns(ko_cp), _cp_to_pawns(w_cp)
    best_move = sf.get('move') or komodo.get('move', '')
    mate = sf.get('mate') if sf.get('mate') is not None else komodo.get('mate')

    return jsonify({
        'sf_cp': sf_cp,       'komodo_cp': ko_cp,       'weighted_cp': w_cp,
        'sf_eval': round(sf_p,3), 'komodo_eval': round(ko_p,3), 'weighted_eval': round(w_p,3),
        'sf_winprob': round(_winprob(sf_p),3),
        'komodo_winprob': round(_winprob(ko_p),3),
        'weighted_winprob': round(_winprob(w_p),3),
        'sf_mate': sf.get('mate'), 'komodo_mate': komodo.get('mate'), 'mate': mate,
        'sf_best': sf.get('move',''), 'komodo_best': komodo.get('move',''), 'best_move': best_move,
        'sf_depth': sf.get('depth',0), 'komodo_depth': komodo.get('depth',0),
        'sf_weight': sf_weight, 'komodo_weight': ko_weight,
        'engines_used': list(results.keys()), 'errors': errors,
    })


if __name__ == '__main__':
    _init_engines()
    port = int(os.environ.get('PORT', 8080))
    print(f'Listening on port {port}')
    app.run(host='0.0.0.0', port=port, threaded=True)
