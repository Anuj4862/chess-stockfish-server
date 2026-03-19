#!/usr/bin/env python3
"""
Chess Multi-Engine Analysis Server
Stockfish 18 + Komodo 14 — parallel analysis, weighted average eval.
POST /analyze  GET /health
"""

import subprocess, threading, time, json, math, os, shutil
from collections import OrderedDict
from flask import Flask, request, jsonify

app = Flask(__name__)

# ── Position cache ────────────────────────────────────────────────────────────
# Key: (moves_tuple, movetime, sf_depth, ko_depth)
# Keeps last 2000 unique positions in memory (LRU-style via OrderedDict)
_cache = OrderedDict()
_cache_lock = threading.Lock()
CACHE_MAX = 2000

def _cache_get(key):
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)  # mark as recently used
            return _cache[key]
    return None

def _cache_set(key, value):
    with _cache_lock:
        _cache[key] = value
        _cache.move_to_end(key)
        if len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)  # evict oldest

SF_PATH     = os.environ.get('SF_PATH',     '/usr/local/bin/stockfish')
KOMODO_PATH = os.environ.get('KOMODO_PATH', '/usr/local/bin/komodo')

def _find(primary, fallbacks):
    for p in [primary] + fallbacks:
        if p and (shutil.which(p) or os.path.isfile(p)):
            return p
    return None

SF_EXE     = _find(SF_PATH,     ['/engines/stockfish', 'stockfish'])
KOMODO_EXE = _find(KOMODO_PATH, ['/engines/komodo', 'komodo'])


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

    def _wait_for(self, token, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = self._proc.stdout.readline()
                if token in line:
                    return True
            except Exception:
                return False
        return False

    def _start(self, threads, hash_mb):
        try:
            self._proc = subprocess.Popen(
                [self.path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1)
            self._send('uci')
            if not self._wait_for('uciok', 15):
                print(f'[{self.name}] uciok timeout')
                return
            self._send(f'setoption name Threads value {threads}')
            self._send(f'setoption name Hash value {hash_mb}')
            if self.name == 'stockfish':
                self._send('setoption name Use NNUE value true')
            self._send('isready')
            if not self._wait_for('readyok', 15):
                print(f'[{self.name}] readyok timeout')
                return
            self.ready = True
            print(f'[{self.name}] ready OK', flush=True)
        except Exception as e:
            print(f'[{self.name}] FAILED: {e}', flush=True)

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
                try:
                    line = self._proc.stdout.readline().strip()
                except Exception:
                    break
                if not line:
                    continue
                parts = line.split()
                is_mpv1 = 'multipv 1' in line or 'multipv' not in line
                if line.startswith('info') and is_mpv1:
                    if 'depth' in parts:
                        di = parts.index('depth')
                        if di + 1 < len(parts):
                            try: depth_reached = int(parts[di+1])
                            except: pass
                    if 'score cp' in line:
                        try:
                            ci = parts.index('cp')
                            raw = int(parts[ci+1])
                            best_cp   = raw if is_white else -raw
                            best_mate = None
                        except: pass
                    elif 'score mate' in line:
                        try:
                            mi = parts.index('mate')
                            m  = int(parts[mi+1])
                            best_mate = m if is_white else -m
                            best_cp   = (10000 if m > 0 else -10000) if is_white else (-10000 if m > 0 else 10000)
                        except: pass
                if line.startswith('bestmove'):
                    if len(parts) > 1 and parts[1] != '(none)':
                        best_move = parts[1]
                    break

            return {'cp': best_cp, 'mate': best_mate, 'move': best_move, 'depth': depth_reached}


_engines = {}
_engines_ready = False

def _init_engines():
    global _engines, _engines_ready
    print('Initializing engines...', flush=True)
    if SF_EXE:
        _engines['stockfish'] = UCIEngine(SF_EXE, 'stockfish', threads=1, hash_mb=64)
    else:
        print('WARNING: Stockfish not found', flush=True)
    if KOMODO_EXE:
        _engines['komodo'] = UCIEngine(KOMODO_EXE, 'komodo', threads=1, hash_mb=64)
    else:
        print('WARNING: Komodo not found', flush=True)
    _engines_ready = True
    print('Engine init complete', flush=True)


def _cp_to_pawns(cp):
    return max(-10.0, min(10.0, cp / 100.0))

def _winprob(pawns):
    return 1.0 / (1.0 + math.pow(10.0, -pawns / 4.0))


@app.route('/health')
def health():
    # Always respond immediately — even before engines are ready
    ready = [n for n, e in _engines.items() if e.ready]
    initializing = not _engines_ready
    return jsonify({
        'status': 'ok',
        'engines': ready,
        'initializing': initializing,
    }), 200  # always 200 so healthcheck passes immediately


@app.route('/analyze', methods=['POST'])
def analyze():
    # Wait up to 30s for engines if still initializing
    waited = 0
    while not _engines_ready and waited < 30:
        time.sleep(0.5)
        waited += 0.5

    data         = request.get_json(force=True, silent=True) or {}
    moves        = data.get('moves',         [])
    movetime     = int(data.get('movetime',  800))
    sf_depth     = int(data.get('sf_depth',  22))
    ko_depth     = int(data.get('komodo_depth', 18))
    sf_weight    = float(data.get('sf_weight', 0.65))
    ko_weight    = 1.0 - sf_weight
    multipv      = int(data.get('multipv',   1))

    # ── Cache lookup ─────────────────────────────────────────────────────────
    cache_key = (tuple(moves), movetime, sf_depth, ko_depth)
    cached = _cache_get(cache_key)
    if cached:
        cached['cached'] = True
        return jsonify(cached)

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

    result = {
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
        'cached': False,
    }
    _cache_set(cache_key, result)
    return jsonify(result)


@app.route('/cache-stats')
def cache_stats():
    with _cache_lock:
        size = len(_cache)
    return jsonify({'cached_positions': size, 'max_size': CACHE_MAX})



@app.route('/analyze-game', methods=['POST'])
def analyze_game():
    # Analyze full game in one shot.
    # Sends ucinewgame once so engines reuse transposition tables across moves.
    # 3-10x faster than calling /analyze separately for every move.
    waited = 0
    while not _engines_ready and waited < 30:
        time.sleep(0.5)
        waited += 0.5

    data      = request.get_json(force=True, silent=True) or {}
    all_moves = data.get('moves', [])
    movetime  = int(data.get('movetime', 600))
    sf_depth  = int(data.get('sf_depth', 20))
    ko_depth  = int(data.get('komodo_depth', 16))
    sf_weight = float(data.get('sf_weight', 0.65))
    ko_weight = 1.0 - sf_weight

    if not all_moves:
        return jsonify({'error': 'no moves provided'}), 400

    # ucinewgame once per game session
    for engine in _engines.values():
        if engine.ready and engine._proc:
            with engine._lock:
                engine._send('ucinewgame')
                engine._send('isready')
                engine._wait_for('readyok', 3)

    game_results = []

    for i in range(len(all_moves) + 1):
        moves_so_far = all_moves[:i]
        cache_key = (tuple(moves_so_far), movetime, sf_depth, ko_depth)
        cached = _cache_get(cache_key)
        if cached:
            game_results.append({**cached, 'cached': True, 'move_index': i})
            continue

        results, errors = {}, []

        def run_seq(name, engine, depth):
            try:
                results[name] = engine.analyze(moves_so_far, movetime, depth, 1)
            except Exception as e:
                errors.append(str(e))
                results[name] = {'cp': 0, 'mate': None, 'move': '', 'depth': 0}

        threads = []
        for name, engine in _engines.items():
            if not engine.ready:
                continue
            d = sf_depth if name == 'stockfish' else ko_depth
            t = threading.Thread(target=run_seq, args=(name, engine, d), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join(timeout=movetime/1000.0 + 6.0)

        sf    = results.get('stockfish', {})
        ko    = results.get('komodo', {})
        sf_cp = sf.get('cp', 0)
        ko_cp = ko.get('cp', sf_cp)
        w_cp  = int(sf_cp * sf_weight + ko_cp * ko_weight) if 'komodo' in results else sf_cp
        sf_p, ko_p, w_p = _cp_to_pawns(sf_cp), _cp_to_pawns(ko_cp), _cp_to_pawns(w_cp)
        best  = sf.get('move') or ko.get('move', '')
        mate  = sf.get('mate') if sf.get('mate') is not None else ko.get('mate')

        r = {
            'move_index': i,
            'sf_cp': sf_cp, 'komodo_cp': ko_cp, 'weighted_cp': w_cp,
            'sf_eval': round(sf_p,3), 'komodo_eval': round(ko_p,3), 'weighted_eval': round(w_p,3),
            'sf_winprob': round(_winprob(sf_p),3),
            'komodo_winprob': round(_winprob(ko_p),3),
            'weighted_winprob': round(_winprob(w_p),3),
            'sf_mate': sf.get('mate'), 'komodo_mate': ko.get('mate'), 'mate': mate,
            'sf_best': sf.get('move',''), 'komodo_best': ko.get('move',''),
            'best_move': best,
            'sf_depth': sf.get('depth',0), 'komodo_depth': ko.get('depth',0),
            'engines_used': list(results.keys()),
            'cached': False,
        }
        _cache_set(cache_key, r)
        game_results.append(r)

    return jsonify({'results': game_results, 'total': len(game_results)})

# Start engines in background — Flask responds to /health immediately
threading.Thread(target=_init_engines, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print(f'Starting server on 0.0.0.0:{port}', flush=True)
    # Use werkzeug directly — no gunicorn needed, works instantly
    app.run(host='0.0.0.0', port=port, threaded=True, use_reloader=False)
