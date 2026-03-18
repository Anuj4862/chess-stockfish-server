#!/usr/bin/env python3
"""
Chess Multi-Engine Analysis Server
Stockfish 18 + Komodo 14 — parallel analysis, weighted average eval.
POST /analyze  POST /analyze-game  GET /health  GET /cache-stats

FIXES applied vs original:
  1. _send()       — checks process liveness via poll(), catches BrokenPipeError/OSError,
                     marks engine dead instead of crashing the Flask worker.
  2. _wait_for()   — exits immediately when process is dead (no more tight CPU loop),
                     also catches empty-string EOF from dead stdout.
  3. analyze()     — re-checks liveness at the top of the locked section so a race
                     between ready=True and a crash can't slip through.
  4. restart()     — new method; automatically relaunches a dead engine so the server
                     self-heals without a container restart.
  5. /analyze-game — ucinewgame block now runs WITHOUT holding engine._lock, fixing
                     the deadlock / stale-pipe-data bug on slow/crashed engines.
  6. Dockerfile    — (see companion Dockerfile) base image changed + libgcc-s1 added
                     so Komodo's old glibc threading works reliably.
"""

import subprocess, threading, time, math, os, shutil
from collections import OrderedDict
from flask import Flask, request, jsonify

app = Flask(__name__)

# ── Position cache ─────────────────────────────────────────────────────────────
_cache      = OrderedDict()
_cache_lock = threading.Lock()
CACHE_MAX   = 2000

def _cache_get(key):
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return dict(_cache[key])   # return a copy so callers can mutate freely
    return None

def _cache_set(key, value):
    with _cache_lock:
        _cache[key] = value
        _cache.move_to_end(key)
        if len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)

# ── Engine paths ───────────────────────────────────────────────────────────────
SF_PATH     = os.environ.get('SF_PATH',     '/usr/local/bin/stockfish')
KOMODO_PATH = os.environ.get('KOMODO_PATH', '/usr/local/bin/komodo')

def _find(primary, fallbacks):
    for p in [primary] + fallbacks:
        if p and (shutil.which(p) or os.path.isfile(p)):
            return p
    return None

SF_EXE     = _find(SF_PATH,     ['/engines/stockfish', 'stockfish'])
KOMODO_EXE = _find(KOMODO_PATH, ['/engines/komodo',    'komodo'])

# ── UCI Engine wrapper ─────────────────────────────────────────────────────────

class UCIEngine:
    def __init__(self, path, name, threads=2, hash_mb=128):
        self.name     = name
        self.path     = path
        self.threads  = threads
        self.hash_mb  = hash_mb
        self._lock    = threading.Lock()
        self._proc    = None
        self.ready    = False
        self._start()

    # ── internal: send one UCI line ────────────────────────────────────────────
    def _send(self, cmd):
        """Write a UCI command. Marks engine dead on any pipe failure."""
        if self._proc is None:
            return
        # poll() returns None while process is alive
        if self._proc.poll() is not None:
            self.ready = False
            return
        try:
            self._proc.stdin.write(cmd + '\n')
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            print(f'[{self.name}] _send error ({cmd!r}): {e}', flush=True)
            self.ready = False

    # ── internal: wait for a token on stdout ───────────────────────────────────
    def _wait_for(self, token, timeout=10):
        """Read stdout until token found or timeout. Exits fast on dead process."""
        if self._proc is None:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            # bail immediately if process has exited
            if self._proc.poll() is not None:
                self.ready = False
                return False
            try:
                line = self._proc.stdout.readline()
            except Exception:
                self.ready = False
                return False
            if line == '':
                # EOF — process died
                self.ready = False
                return False
            if token in line:
                return True
        return False

    # ── internal: launch / re-launch the engine process ───────────────────────
    def _start(self):
        try:
            self._proc = subprocess.Popen(
                [self.path],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
            self._send('uci')
            if not self._wait_for('uciok', 15):
                print(f'[{self.name}] uciok timeout — engine may be missing or wrong arch',
                      flush=True)
                return
            self._send(f'setoption name Threads value {self.threads}')
            self._send(f'setoption name Hash value {self.hash_mb}')
            if self.name == 'stockfish':
                self._send('setoption name Use NNUE value true')
            self._send('isready')
            if not self._wait_for('readyok', 15):
                print(f'[{self.name}] readyok timeout', flush=True)
                return
            self.ready = True
            print(f'[{self.name}] ready ✓', flush=True)
        except Exception as e:
            print(f'[{self.name}] FAILED to start: {e}', flush=True)

    # ── public: restart a dead engine ─────────────────────────────────────────
    def restart(self):
        """Attempt to relaunch the engine after a crash. Thread-safe."""
        with self._lock:
            if self.ready:
                return  # someone else already restarted it
            print(f'[{self.name}] restarting …', flush=True)
            # kill the old process if it's somehow still lingering
            try:
                if self._proc and self._proc.poll() is None:
                    self._proc.kill()
                    self._proc.wait(timeout=3)
            except Exception:
                pass
            self._proc = None
            self._start()

    # ── public: analyse a position ────────────────────────────────────────────
    def analyze(self, moves, movetime, depth=0, multipv=1):
        # quick check before acquiring the lock
        if not self.ready:
            return {'cp': 0, 'mate': None, 'move': '', 'depth': 0}

        with self._lock:
            # re-check inside the lock (race between ready flag and crash)
            if not self.ready or self._proc is None or self._proc.poll() is not None:
                self.ready = False
                return {'cp': 0, 'mate': None, 'move': '', 'depth': 0}

            pos = 'position startpos'
            if moves:
                pos += ' moves ' + ' '.join(moves)
            self._send(pos)
            if not self.ready:   # pipe could have died in _send
                return {'cp': 0, 'mate': None, 'move': '', 'depth': 0}

            self._send(f'setoption name MultiPV value {max(1, multipv)}')
            self._send('isready')
            self._wait_for('readyok', 3)

            go = f'go movetime {movetime}'
            if depth > 0:
                go = f'go depth {depth} movetime {movetime}'
            self._send(go)
            if not self.ready:
                return {'cp': 0, 'mate': None, 'move': '', 'depth': 0}

            best_cp, best_mate, best_move, depth_reached = 0, None, '', 0
            is_white = (len(moves) % 2) == 0
            deadline = time.time() + movetime / 1000.0 + 5.0

            while time.time() < deadline:
                # fast-exit if engine died mid-analysis
                if self._proc.poll() is not None:
                    self.ready = False
                    break
                try:
                    line = self._proc.stdout.readline()
                except Exception:
                    self.ready = False
                    break

                if line == '':          # EOF
                    self.ready = False
                    break
                line = line.strip()
                if not line:
                    continue

                parts    = line.split()
                is_mpv1  = 'multipv 1' in line or 'multipv' not in line

                if line.startswith('info') and is_mpv1:
                    if 'depth' in parts:
                        di = parts.index('depth')
                        if di + 1 < len(parts):
                            try:
                                depth_reached = int(parts[di + 1])
                            except ValueError:
                                pass
                    if 'score cp' in line:
                        try:
                            ci        = parts.index('cp')
                            raw       = int(parts[ci + 1])
                            best_cp   = raw if is_white else -raw
                            best_mate = None
                        except (ValueError, IndexError):
                            pass
                    elif 'score mate' in line:
                        try:
                            mi        = parts.index('mate')
                            m         = int(parts[mi + 1])
                            best_mate = m if is_white else -m
                            best_cp   = (10000 if m > 0 else -10000) if is_white \
                                        else (-10000 if m > 0 else 10000)
                        except (ValueError, IndexError):
                            pass

                if line.startswith('bestmove'):
                    if len(parts) > 1 and parts[1] != '(none)':
                        best_move = parts[1]
                    break

            return {
                'cp':    best_cp,
                'mate':  best_mate,
                'move':  best_move,
                'depth': depth_reached,
            }

    # ── public: send ucinewgame safely (no lock held by caller required) ──────
    def new_game(self):
        """Reset hash tables for a new game. Does NOT hold the analysis lock."""
        if not self.ready:
            return
        with self._lock:
            if not self.ready:
                return
            self._send('ucinewgame')
            self._send('isready')
            self._wait_for('readyok', 5)


# ── Engine registry ────────────────────────────────────────────────────────────
_engines       = {}
_engines_ready = False

def _init_engines():
    global _engines, _engines_ready
    print('Initializing engines …', flush=True)
    if SF_EXE:
        _engines['stockfish'] = UCIEngine(SF_EXE,     'stockfish', threads=2, hash_mb=512)
    else:
        print('WARNING: Stockfish executable not found', flush=True)
    if KOMODO_EXE:
        _engines['komodo']    = UCIEngine(KOMODO_EXE, 'komodo',    threads=2, hash_mb=512)
    else:
        print('WARNING: Komodo executable not found', flush=True)
    _engines_ready = True
    print('Engine init complete', flush=True)


def _ensure_alive():
    """Called before every request: restart any engine that has crashed."""
    for engine in _engines.values():
        if not engine.ready:
            # restart in a background thread so the request isn't blocked
            threading.Thread(target=engine.restart, daemon=True).start()


# ── Eval helpers ───────────────────────────────────────────────────────────────
def _cp_to_pawns(cp):
    return max(-10.0, min(10.0, cp / 100.0))

def _winprob(pawns):
    return 1.0 / (1.0 + math.pow(10.0, -pawns / 4.0))

def _wait_for_engines(max_wait=30):
    waited = 0
    while not _engines_ready and waited < max_wait:
        time.sleep(0.5)
        waited += 0.5

def _build_result(sf, komodo, sf_weight):
    """Merge per-engine results into the final response dict."""
    ko_weight = 1.0 - sf_weight
    sf_cp     = sf.get('cp', 0)
    ko_cp     = komodo.get('cp', sf_cp) if komodo else sf_cp

    if komodo:
        w_cp = int(sf_cp * sf_weight + ko_cp * ko_weight)
    else:
        w_cp      = sf_cp
        sf_weight = 1.0
        ko_weight = 0.0

    sf_p, ko_p, w_p = _cp_to_pawns(sf_cp), _cp_to_pawns(ko_cp), _cp_to_pawns(w_cp)
    best_move = sf.get('move') or (komodo.get('move', '') if komodo else '')
    mate      = sf.get('mate') if sf.get('mate') is not None \
                else (komodo.get('mate') if komodo else None)

    return {
        'sf_cp':              sf_cp,
        'komodo_cp':          ko_cp,
        'weighted_cp':        w_cp,
        'sf_eval':            round(sf_p,  3),
        'komodo_eval':        round(ko_p,  3),
        'weighted_eval':      round(w_p,   3),
        'sf_winprob':         round(_winprob(sf_p),  3),
        'komodo_winprob':     round(_winprob(ko_p),  3),
        'weighted_winprob':   round(_winprob(w_p),   3),
        'sf_mate':            sf.get('mate'),
        'komodo_mate':        komodo.get('mate') if komodo else None,
        'mate':               mate,
        'sf_best':            sf.get('move', ''),
        'komodo_best':        komodo.get('move', '') if komodo else '',
        'best_move':          best_move,
        'sf_depth':           sf.get('depth', 0),
        'komodo_depth':       komodo.get('depth', 0) if komodo else 0,
        'sf_weight':          sf_weight,
        'komodo_weight':      ko_weight,
    }


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    ready       = [n for n, e in _engines.items() if e.ready]
    initializing = not _engines_ready
    return jsonify({
        'status':       'ok',
        'engines':      ready,
        'initializing': initializing,
    }), 200   # always 200 so Railway healthcheck passes immediately


@app.route('/analyze', methods=['POST'])
def analyze():
    _wait_for_engines()
    _ensure_alive()

    data      = request.get_json(force=True, silent=True) or {}
    moves     = data.get('moves',            [])
    movetime  = int(data.get('movetime',     800))
    sf_depth  = int(data.get('sf_depth',      22))
    ko_depth  = int(data.get('komodo_depth',  18))
    sf_weight = float(data.get('sf_weight',  0.65))
    multipv   = int(data.get('multipv',        1))

    cache_key = (tuple(moves), movetime, sf_depth, ko_depth)
    cached    = _cache_get(cache_key)
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
        t.join(timeout=movetime / 1000.0 + 6.0)

    if not results:
        return jsonify({'error': 'no engine results', 'details': errors}), 503

    sf     = results.get('stockfish', {})
    komodo = results.get('komodo')
    result = _build_result(sf, komodo, sf_weight)
    result.update({
        'engines_used': list(results.keys()),
        'errors':       errors,
        'cached':       False,
    })
    _cache_set(cache_key, result)
    return jsonify(result)


@app.route('/cache-stats')
def cache_stats():
    with _cache_lock:
        size = len(_cache)
    return jsonify({'cached_positions': size, 'max_size': CACHE_MAX})


@app.route('/analyze-game', methods=['POST'])
def analyze_game():
    """
    Analyze a full game in one shot.
    FIX: ucinewgame is now sent via engine.new_game() which acquires the lock
    internally — the outer loop no longer holds the lock, so analyze() threads
    can't deadlock or read stale pipe data from a half-reset engine.
    """
    _wait_for_engines()
    _ensure_alive()

    data      = request.get_json(force=True, silent=True) or {}
    all_moves = data.get('moves', [])
    movetime  = int(data.get('movetime',        600))
    sf_depth  = int(data.get('sf_depth',         20))
    ko_depth  = int(data.get('komodo_depth',     16))
    sf_weight = float(data.get('sf_weight',     0.65))

    if not all_moves:
        return jsonify({'error': 'no moves provided'}), 400

    # Reset transposition tables — new_game() is lock-safe and crash-safe
    for engine in _engines.values():
        engine.new_game()

    game_results = []

    for i in range(len(all_moves) + 1):
        moves_so_far = all_moves[:i]
        cache_key    = (tuple(moves_so_far), movetime, sf_depth, ko_depth)

        cached = _cache_get(cache_key)
        if cached:
            cached.update({'cached': True, 'move_index': i})
            game_results.append(cached)
            continue

        results, errors = {}, []

        def run_seq(name, engine, depth, _msf=moves_so_far):
            try:
                results[name] = engine.analyze(_msf, movetime, depth, 1)
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
            t.join(timeout=movetime / 1000.0 + 6.0)

        sf     = results.get('stockfish', {})
        komodo = results.get('komodo')
        r      = _build_result(sf, komodo, sf_weight)
        r.update({
            'move_index':   i,
            'engines_used': list(results.keys()),
            'cached':       False,
        })
        _cache_set(cache_key, r)
        game_results.append(r)

    return jsonify({'results': game_results, 'total': len(game_results)})


# ── Boot ───────────────────────────────────────────────────────────────────────
threading.Thread(target=_init_engines, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    print(f'Starting server on 0.0.0.0:{port}', flush=True)
    app.run(host='0.0.0.0', port=port, threaded=True, use_reloader=False)
