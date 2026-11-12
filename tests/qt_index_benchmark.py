"""Opt-in QT index comparison. No production imports this module.

Run from the repository root with Python's UTF-8 mode; see README.md.
Only --profile reads a real dictionary, read-only, emitting aggregate counts.
All measured children use the same generated SQLite fixture and interpreter.
"""
from array import array
from collections import Counter
from contextlib import closing, contextmanager
import ctypes
import gc
import hashlib
import importlib.metadata
import json
import math
import os
import platform
from pathlib import Path
import random
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import qt as original

FIXTURE_ID = 0x51544231


def profile_database(path):
    """No keys, meanings, scopes or paths escape the read-only connection."""
    result = {'schema': 1, 'tiers': [], 'word_lengths': [], 'adjacent_prefix_lengths': {}, 'meaning_shapes': {}}
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('BEGIN')
        for kind in original.KINDS[:-1]:
            count, mean, maximum = db.execute(
                'SELECT count(*),avg(length(word)),max(length(word)) '
                "FROM entries WHERE scope='*' AND kind=?", (kind,)).fetchone()
            if not count: continue
            result['tiers'].append({'kind': kind, 'entries': count, 'mean_word_length': mean,
                                    'max_word_length': maximum})
            result['word_lengths'].extend([kind, n, c] for n, c in db.execute(
                "SELECT length(word),count(*) FROM entries WHERE scope='*' AND kind=? GROUP BY length(word)", (kind,)))
            previous, prefixes, meaning_bytes, shapes = '', Counter(), 0, Counter()
            for word, meaning in db.execute("SELECT word,meaning FROM entries WHERE scope='*' AND kind=? ORDER BY word", (kind,)):
                n = 0
                for a, b in zip(previous, word):
                    if a != b: break
                    n += 1
                prefixes[n] += 1
                previous = word
                first = meaning.split('¦', 1)[0].strip()
                size = len(first.encode('utf-8'))
                maximum = max(map(ord, first), default=0)
                width = 1 if maximum <= 255 else 2 if maximum <= 65535 else 4
                shapes[(len(first), size, width)] += 1
                meaning_bytes += size
            result['tiers'][-1]['mean_first_meaning_bytes'] = meaning_bytes / count
            result['adjacent_prefix_lengths'][kind] = dict(sorted(prefixes.items()))
            result['meaning_shapes'][kind] = [[*shape, count] for shape, count in sorted(shapes.items())]
    return result


def generate(profile, path):
    """Preserve length and sorted-adjacency LCP histograms using public keys.

    At depth d, distinct prefixes = words of length >= d minus adjacent LCPs
    >= d. Construct exactly those node and terminal counts, not random words
    biased toward less sharing and an artificially larger baseline trie.
    """
    path = Path(path)
    if path.exists(): raise FileExistsError('Refusing to replace an existing dictionary fixture')
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(314159)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(f'PRAGMA application_id={FIXTURE_ID}')
        db.execute('CREATE TABLE entries(kind TEXT NOT NULL,word TEXT NOT NULL,meaning TEXT NOT NULL,scope TEXT NOT NULL,PRIMARY KEY(kind,scope,word))')
        for tier in profile['tiers']:
            kind = tier['kind']
            lengths = {n: count for k, n, count in profile['word_lengths'] if k == kind}
            if sum(lengths.values()) != tier['entries']: raise ValueError('Profile count/length mismatch')
            histogram = {int(n): count for n, count in profile['adjacent_prefix_lengths'][kind].items()}
            parents, batch, i = [''], [], 0
            mandatory = 1
            meaning_bytes = max(0, round(tier['mean_first_meaning_bytes']))
            shapes = [tuple(shape[:3]) for shape in profile.get('meaning_shapes', {}).get(kind, []) for _ in range(shape[3])]
            if shapes and len(shapes) != tier['entries']: raise ValueError('Meaning shape count mismatch')
            rng.shuffle(shapes)
            for depth in range(1, max(lengths) + 1):
                count = sum(c for n, c in lengths.items() if n >= depth) - sum(c for n, c in histogram.items() if n >= depth)
                if count < mandatory or lengths.get(depth, 0) > count: raise ValueError('Inconsistent prefix profile')
                # Nonterminal parents must continue; terminal parents may continue.
                continuing = min(len(parents), count)
                child_counts = [1] * continuing
                for _ in range(count - continuing): child_counts[rng.randrange(continuing)] += 1
                nodes = []
                for parent, children in zip(parents, child_counts):
                    offset = rng.randrange(17062)
                    for child in range(children):
                        label = (offset + child * 101) % 17062
                        if child >= 17062: raise ValueError('Synthetic alphabet exhausted')
                        codepoint = 0x20000 + label if label % 997 == 996 else 0x4e00 + label
                        nodes.append(parent + chr(codepoint))
                rng.shuffle(nodes)
                terminal_count = lengths.get(depth, 0)
                for word in nodes[:terminal_count]:
                    if shapes: meaning = synthetic_meaning(*shapes[i], i)
                    else: meaning = synthetic_meaning(meaning_bytes, meaning_bytes, 1, i)
                    batch.append((kind, word, meaning + '¦khác', '*'))
                    i += 1
                    if len(batch) == 2000:
                        db.executemany('INSERT INTO entries VALUES(?,?,?,?)', batch)
                        batch.clear()
                parents = nodes[terminal_count:] + nodes[:terminal_count]
                mandatory = len(nodes) - terminal_count
            db.executemany('INSERT INTO entries VALUES(?,?,?,?)', batch)
    actual = profile_database(path)
    if sorted(actual['word_lengths']) != sorted(profile['word_lengths']): raise AssertionError('Length histogram changed')
    if json.loads(json.dumps(actual['adjacent_prefix_lengths'])) != json.loads(json.dumps(profile['adjacent_prefix_lengths'])):
        raise AssertionError('Prefix histogram changed')
    if 'meaning_shapes' in profile:
        if {k: sorted(v) for k, v in actual['meaning_shapes'].items()} != {k: sorted(v) for k, v in profile['meaning_shapes'].items()}:
            raise AssertionError('Meaning histogram changed')
    return actual


def synthetic_meaning(characters, utf8_bytes, width, number):
    """Match anonymous CPython width/character/UTF-8-size bins, no real text."""
    four = max(1, utf8_bytes - 3 * characters) if width == 4 else 0
    extra = utf8_bytes - characters - 3 * four
    remaining = characters - four
    three = min(remaining, extra // 2) if width > 1 else 0
    two = extra - 2 * three
    one = remaining - three - two
    if min(four, three, two, one) < 0: raise ValueError('Invalid meaning shape')
    return '𠀀' * four + 'ữ' * three + ('ư' if width > 1 else 'á') * two + str(number).ljust(one, 'x')[:one]


class MeaningTable:
    def __init__(self, count):
        self.blob = bytearray()
        self.offsets = array('Q', [0]) * count
        self.lengths = array('I', [0]) * count

    def add(self, key_id, meaning):
        value = meaning.split('¦', 1)[0].strip().encode('utf-8')
        self.offsets[key_id] = len(self.blob)
        self.lengths[key_id] = len(value)
        self.blob.extend(value)

    def get(self, key_id):
        start = self.offsets[key_id]
        return self.blob[start:start + self.lengths[key_id]].decode('utf-8')


class CompactTier:
    """Benchmark-only immutable base + copy-on-write overlay, None = tombstone."""
    def __init__(self, db, kind, scope, backend):
        query = 'SELECT word,meaning FROM entries WHERE kind=? AND scope=?'
        self.backend = backend
        self.overlay = {}
        self.overlay_first = frozenset()
        if backend == 'marisa':
            import marisa_trie
            if importlib.metadata.version('marisa-trie') != '1.4.1': raise RuntimeError('Benchmark requires marisa-trie==1.4.1')
            self.keys = marisa_trie.Trie(word for word, in db.execute(
                'SELECT word FROM entries WHERE kind=? AND scope=?', (kind, scope)))
        else: self.keys = {}
        count = len(self.keys) if backend == 'marisa' else db.execute(
            'SELECT count(*) FROM entries WHERE kind=? AND scope=?', (kind, scope)).fetchone()[0]
        self.meanings = MeaningTable(count)
        first, lengths = set(), set()
        self.buckets = {} if backend == 'bucketed' else None
        for index, (word, meaning) in enumerate(db.execute(query, (kind, scope))):
            if backend == 'marisa': index = self.keys[word]
            else: self.keys[word] = index
            self.meanings.add(index, meaning)
            first.add(word[0])
            lengths.add(len(word))
            if self.buckets is not None and len(word) >= 2:
                head = word[:2]
                self.buckets[head] = self.buckets.get(head, 0) | (1 << len(word))
        self.first = frozenset(first)
        self.lengths = tuple(sorted(lengths, reverse=True))
        self.maximum = max(lengths, default=0)
        self.count = count

    def __bool__(self): return self.count > 0

    def updated(self, word, meaning):
        from copy import copy
        result = copy(self)
        result.overlay = dict(self.overlay)
        previous = self.exact(word)
        result.overlay[word] = None if meaning is None else meaning.split('¦', 1)[0].strip()
        result.overlay_first = self.overlay_first | {word[0]}
        result.maximum = max(self.maximum, len(word))
        result.count += int(meaning is not None) - int(previous is not None)
        return result

    def exact(self, word):
        if word in self.overlay: return self.overlay[word]
        try: return self.meanings.get(self.keys[word])
        except KeyError: return None

    def longest(self, text, start):
        char = text[start]
        if char not in self.first and char not in self.overlay_first: return None
        query = text[start:start + self.maximum]
        best = None
        if char in self.overlay_first:
            # Deliberately measured across overlay sizes before choosing any bound.
            for n in range(1, len(query) + 1):
                value = self.overlay.get(query[:n])
                if value is not None: best = (start + n, value)
        if self.backend == 'marisa':
            candidates = self.keys.iter_prefixes_with_ids(query)
        elif self.backend == 'bucketed':
            candidates = self.bucket_candidates(query)
        else:
            candidates = ((query[:n], self.keys[query[:n]]) for n in self.lengths
                          if n <= len(query) and query[:n] in self.keys)
        chosen = None
        for word, key_id in candidates:
            if word in self.overlay or (best and start + len(word) <= best[0]): continue
            if chosen is None or len(word) > len(chosen[0]): chosen = (word, key_id)
        if chosen: return (start + len(chosen[0]), self.meanings.get(chosen[1]))
        return best

    def bucket_candidates(self, query):
        mask = self.buckets.get(query[:2], 0) & ((1 << (len(query) + 1)) - 1)
        while mask:
            length = mask.bit_length() - 1
            word = query[:length]
            index = self.keys.get(word)
            if index is not None: yield word, index
            mask ^= 1 << length
        index = self.keys.get(query[0])
        if index is not None: yield query[0], index


class PrototypeDictionary(original.QtDictionary):
    def _build_scope(self, scope, generation):
        with self._reader() as db:
            rules = self._build_rules(db, scope)
            # Tuple rows avoid retaining a second Python object index.
            db.row_factory = None
            tiers = {kind: CompactTier(db, kind, scope, self.backend) for kind in original.KINDS[:-1]}
        return original._ScopeSnapshot(tiers, rules, bool(rules or any(tiers.values())), generation)


@contextmanager
def candidate_engine(backend, directory):
    """Keep the real engine's loop, SQL writes and snapshot protocol unchanged.

    Dispatch patches exist only in an isolated benchmark process/test context;
    the baseline uses the production engine without patches.
    """
    if backend == 'nested':
        engine = original.QtDictionary(directory)
        try: yield engine
        finally: engine.close()
        return
    with patch.object(original, '_longest', CompactTier.longest), \
         patch.object(original, '_copy_path', CompactTier.updated):
        engine = PrototypeDictionary(directory)
        engine.backend = backend
        try: yield engine
        finally: engine.close()


def memory():
    if sys.platform != 'win32': raise RuntimeError('This measurement uses Windows working-set and commit counters')
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ('peak_rss', 'rss', 'peak_pool', 'pool', 'peak_nonpaged',
                                               'nonpaged', 'pagefile', 'peak_pagefile', 'private')]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    function = ctypes.windll.psapi.GetProcessMemoryInfo
    function.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    if not function(wintypes.HANDLE(-1), ctypes.byref(counters), counters.cb): raise ctypes.WinError()
    return {key: getattr(counters, key) for key in ('rss', 'peak_rss', 'private')}


def timed(function, repeats=7):
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        result = function()
        samples.append((time.perf_counter() - start) * 1000)
    ordered = sorted(samples)
    return {'median_ms': statistics.median(samples), 'p95_ms': ordered[math.ceil(len(ordered) * .95) - 1],
            'max_ms': max(samples), 'samples': len(samples)}, result


def workloads(path):
    with closing(sqlite3.connect(path)) as db:
        stride = max(1, db.execute("SELECT count(*) FROM entries WHERE kind='vietphrase'").fetchone()[0] // 512)
        words = [word for word, in db.execute("SELECT word FROM entries WHERE scope='*' AND kind='vietphrase' AND rowid % ?=0 LIMIT 512", (stride,))]
        fallthrough = [word for word, in db.execute("SELECT h.word FROM entries h WHERE h.kind='hanviet' AND h.scope='*' "
            "AND NOT EXISTS(SELECT 1 FROM entries e WHERE e.scope='*' AND e.kind IN ('name','vietphrase','pronouns') AND e.word=h.word) LIMIT 512")]
    if not words: raise ValueError('Fixture needs a VietPhrase tier')
    seeds = {'matches': '|'.join(words), 'misses': '丽丸乁𠄢' * 128,
             'shared_root_misses': '|'.join(word[0] + '丽' for word in words),
             'mixed': '|'.join(word + ' tiếng Việt, 123! 丽\n' for word in words),
             'fallthrough': '|'.join(fallthrough) or '𠀀'}
    return {f'{name}_{length}': (seed * (length // len(seed) + 1))[:length]
            for name, seed in seeds.items() for length in (10000, 100000)}


def measure(backend, directory, repeats, phase='all'):
    with closing(sqlite3.connect((Path(directory) / 'qt.sqlite3').resolve().as_uri() + '?mode=ro', uri=True)) as db:
        if db.execute('PRAGMA application_id').fetchone()[0] != FIXTURE_ID:
            raise ValueError('Only generated synthetic fixtures may be measured/mutated')
    gc.collect()
    result = {'backend': backend, 'phase': phase, 'python': sys.version, 'sqlite': sqlite3.sqlite_version,
              'platform': platform.platform(), 'cpu': platform.processor(), 'cpu_count': os.cpu_count(),
              'pid': os.getpid(), 'before': memory(), 'persistent_cache': False, 'warm_mmap_startup_ms': None}
    if backend == 'marisa': result['dependency'] = {'marisa-trie': importlib.metadata.version('marisa-trie')}
    with candidate_engine(backend, directory) as engine:
        start = time.perf_counter()
        snapshot = engine._compiled('*')
        result['build_ms'] = (time.perf_counter() - start) * 1000
        gc.collect()
        result['steady'] = memory()
        print(f'{backend}: compiled', flush=True)
        if phase == 'restore':
            del snapshot
            with engine._reader() as db:
                count = db.execute("SELECT count(*) FROM entries WHERE kind='name' AND scope='*'").fetchone()[0]
            replacement = '\n'.join('測' + chr(0x4e00 + i) + '=nhập thử' for i in range(2000))
            result['bulk_replace'], report = timed(lambda: engine.import_text('name', replacement, 'replace'), 1)
            result['large_restore'], restored = timed(lambda: engine.restore_backup(report['backup_id']), 1)
            if restored['restored'] != count: raise AssertionError('Large restore did not restore the original tier')
            result['restored_entries'] = count
            result['after_replacement'] = memory()
            result['concurrent'] = concurrent_rebuild(engine, workloads(engine.path)['mixed_10000'])
            result['after_concurrent'] = memory()
            return result
        result['acquisition'], acquired = timed(lambda: engine._compiled('*'), 100)
        del acquired
        inputs = workloads(engine.path)
        result['translation'] = {}
        # Fixed, already acquired snapshot isolates traversal from acquisition.
        with patch.object(engine, '_compiled', return_value=snapshot):
            for name, text in inputs.items():
                stats, output = timed(lambda: engine.translate(text), repeats)
                stats['output_sha256'] = hashlib.sha256(output.encode('utf-8')).hexdigest()
                result['translation'][name] = stats
        del snapshot
        gc.collect()
        print(f'{backend}: translated', flush=True)
        result['mutations'] = {}
        for scope in ('*', 'book'):
            engine._compiled(scope)
            for action in ('add', 'update', 'delete'):
                samples = []
                for i in range(9):
                    word = '𠀀測' + chr(0x4e00 + i)
                    if action != 'add': engine.save('name', word, 'before', scope)
                    start = time.perf_counter()
                    if action == 'delete': engine.delete('name', word, scope)
                    else: engine.save('name', word, 'after', scope)
                    samples.append((time.perf_counter() - start) * 1000)
                result['mutations'][scope + '_' + action] = {'median_ms': statistics.median(samples), 'max_ms': max(samples)}
        result['overlay'] = []
        if backend != 'nested':
            for target in (16, 128, 512, 2048):
                tier = engine._cache['*'].tries['vietphrase']
                overlay_start = time.perf_counter()
                while len(tier.overlay) < target:
                    word = '𠀀' + chr(0x4e00 + len(tier.overlay))
                    tier = tier.updated(word, 'thay đổi')
                copy_ms = (time.perf_counter() - overlay_start) * 1000
                latency, _ = timed(lambda: [tier.longest('𠀀一𠀀丁', 0) for _ in range(10000)], 3)
                overlay_bytes = sys.getsizeof(tier.overlay) + sum(sys.getsizeof(k) + sys.getsizeof(v) for k, v in tier.overlay.items())
                result['overlay'].append({'entries': target, 'estimated_bytes': overlay_bytes,
                                          'copy_updates_ms': copy_ms,
                                          'lookup_10000': latency})
            del tier
        # Serialization is a size experiment, not a production persistent cache.
        if backend == 'marisa':
            serialized = Path(directory) / 'serialized'
            serialized.mkdir()
            for kind, tier in engine._cache['*'].tries.items():
                tier.keys.save(str(serialized / (kind + '.marisa')))
                (serialized / (kind + '.utf8')).write_bytes(tier.meanings.blob)
                with (serialized / (kind + '.offsets')).open('wb') as stream: tier.meanings.offsets.tofile(stream)
                with (serialized / (kind + '.lengths')).open('wb') as stream: tier.meanings.lengths.tofile(stream)
            result['serialized_bytes'] = sum(path.stat().st_size for path in serialized.iterdir())
            del tier
        result['before_replacement'] = memory()
        print(f'{backend}: mutations measured', flush=True)
        # Real import/restore exercise snapshot replacement and SQLite-authoritative writes.
        bulk = '\n'.join('測' + chr(0x4e00 + i) + '=nhập thử' for i in range(2000))
        result['bulk_merge'], _ = timed(lambda: engine.import_text('name', bulk), 3)
        report = engine.import_text('pronouns', '他=hắn\n她=cô', 'replace')
        result['restore'], _ = timed(lambda: engine.restore_backup(report['backup_id']), 3)
        result['after_replacement'] = memory()
        print(f'{backend}: replacements measured', flush=True)
        result['concurrent'] = concurrent_rebuild(engine, inputs['mixed_10000'])
    return result


def concurrent_rebuild(engine, text):
    latencies, error, edit_wait = [], [], []
    started, rebuilt = threading.Event(), threading.Event()
    build = engine._build_scope
    def observed(*args):
        started.set()
        return build(*args)
    def writer():
        try: engine.import_text('name', '測試=concurrent')
        except BaseException as exc: error.append(repr(exc))
        finally: rebuilt.set()
    def edit():
        start = time.perf_counter()
        try: engine.save('name', '並行', 'edit')
        except BaseException as exc: error.append(repr(exc))
        edit_wait.append((time.perf_counter() - start) * 1000)
    with patch.object(engine, '_build_scope', side_effect=observed):
        thread = threading.Thread(target=writer)
        thread.start()
        if not started.wait(30): raise TimeoutError('Rebuild never started')
        editor = threading.Thread(target=edit)
        editor.start()
        # A chapter request every 100 ms; an unpaced CPU loop starves SQLite's
        # per-row GIL reacquisition and is not a representative reading cadence.
        while not rebuilt.is_set() and len(latencies) < 300:
            start = time.perf_counter()
            engine.translate(text)
            elapsed = time.perf_counter() - start
            latencies.append(elapsed * 1000)
            rebuilt.wait(max(0, .1 - elapsed))
        thread.join(120)
        editor.join(120)
        if thread.is_alive() or editor.is_alive(): raise TimeoutError('Rebuild did not finish')
    if error: raise RuntimeError(error)
    return {'includes_publication': True, 'reader_interval_ms': 100, 'reader_samples': len(latencies), 'reader_median_ms': statistics.median(latencies) if latencies else None,
            'reader_max_ms': max(latencies, default=None), 'edit_wait_ms': edit_wait[0]}


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', type=Path)
    parser.add_argument('--generate', type=Path, help='Anonymous aggregate profile JSON')
    parser.add_argument('--fixture', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--child', choices=('nested', 'flat', 'marisa', 'bucketed'))
    parser.add_argument('--runs', type=int, default=3)
    parser.add_argument('--repeats', type=int, default=7)
    parser.add_argument('--phase', choices=('all', 'restore'), default='all')
    args = parser.parse_args()
    if args.profile:
        output = profile_database(args.profile)
    elif args.generate:
        if not args.fixture: parser.error('--generate requires --fixture')
        output = generate(json.loads(args.generate.read_text(encoding='utf-8')), args.fixture)
    elif args.child:
        if not args.fixture: parser.error('--child requires --fixture')
        import faulthandler
        faulthandler.dump_traceback_later(180, repeat=True)
        output = measure(args.child, args.fixture.parent, args.repeats, args.phase)
        faulthandler.cancel_dump_traceback_later()
    else:
        if not args.fixture or args.runs < 1: parser.error('Requires --fixture and positive --runs')
        import shutil
        import tempfile
        with args.fixture.open('rb') as stream: digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        output = {'runs': [], 'fixture_sha256': digest}
        for run in range(args.runs):
            for backend in ('nested', 'flat', 'marisa', 'bucketed'):
                with tempfile.TemporaryDirectory(prefix='qt-index-') as directory:
                    fixture = Path(directory) / 'qt.sqlite3'
                    shutil.copyfile(args.fixture, fixture)
                    child_output = Path(directory) / 'result.json'
                    subprocess.run([sys.executable, '-X', 'utf8', str(Path(__file__).resolve()), '--child', backend,
                        '--fixture', str(fixture), '--output', str(child_output), '--repeats', str(args.repeats),
                        '--phase', args.phase], check=True, timeout=300)
                    result = json.loads(child_output.read_text(encoding='utf-8'))
                    result['run'] = run
                    if output['runs'] and 'translation' in result:
                        for name, value in result['translation'].items():
                            if value['output_sha256'] != output['runs'][0]['translation'][name]['output_sha256']:
                                raise AssertionError(f'Translation mismatch: {backend}/{name}')
                    output['runs'].append(result)
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(json.dumps(output, indent=2), encoding='utf-8')
                    print(f'{backend} run {run + 1}: build {result["build_ms"]:.0f} ms, '
                          f'RSS {result["steady"]["rss"] / 1048576:.1f} MiB', flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding='utf-8')


if __name__ == '__main__': main()
