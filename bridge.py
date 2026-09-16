"""
Ponte para monitor externo (sistema paralelo no evento).

Fluxo previsto:
  1. Configurar bridge.url com o link do monitor deles (mesma rede)
  2. Ajustar parse_remote_orders() ao HTML/JSON real deles
  3. Ativar bridge.enabled = true no config.json

Enquanto o parser nao estiver adaptado, a ponte corre mas nao importa nada
(ou usa /api/bridge/import para colar HTML/JSON a mao).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Callable

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SEEN_FILE = os.path.join(BASE_DIR, 'bridge_seen.json')

_bridge_lock = threading.Lock()
_bridge_thread = None
_bridge_stop = threading.Event()
_bridge_state = {
    'running': False,
    'enabled': False,
    'url': '',
    'last_poll': None,
    'last_ok': None,
    'last_error': None,
    'imported': 0,
    'skipped': 0,
}


def _load_seen():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                return set(str(x) for x in data)
        except Exception:
            pass
    return set()


def _save_seen(seen: set):
    # Mantém só os mais recentes para o ficheiro nao crescer sem limite
    items = list(seen)[-500:]
    with open(SEEN_FILE, 'w', encoding='utf-8') as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def order_fingerprint(order: dict) -> str:
    """ID externo ou hash do conteudo — evita importar o mesmo pedido 2x."""
    ext = str(order.get('external_id') or '').strip()
    if ext:
        return f'ext:{ext}'
    raw = json.dumps({
        'lines': order.get('lines', []),
        'total': order.get('total', 0),
        'time': order.get('time', ''),
        'date': order.get('date', ''),
    }, ensure_ascii=False, sort_keys=True)
    return 'hash:' + hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]


def parse_remote_orders(payload: str, content_type: str = '') -> list[dict]:
    """
    Converte a resposta do monitor deles numa lista de pedidos nossos.

    Cada pedido:
      {
        'external_id': '123',          # ideal (nº deles)
        'obs': '',
        'lines': [{'name': '...', 'qty': 1, 'price': 0}],
        'total': 0,
        'date': '26/07/2026',          # opcional
        'time': '12:30',               # opcional
      }

    TODO: quando tiveres o HTML/JSON real, substitui esta funcao.
    Tentativa generica: se for JSON com lista, mapeia campos comuns.
    """
    text = (payload or '').strip()
    if not text:
        return []

    # 1) JSON direto (alguns monitores caseiros sao so um /api/...)
    ct = (content_type or '').lower()
    if 'json' in ct or text.startswith('{') or text.startswith('['):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if data is not None:
            return _parse_json_orders(data)

    # 2) HTML — placeholder: extrai blocos data-order / data-id se existirem
    found = []
    for m in re.finditer(r'data-order-id=["\']([^"\']+)["\']', text, re.I):
        found.append({
            'external_id': m.group(1),
            'obs': 'Importado do monitor externo (parser basico)',
            'lines': [{'name': 'Pedido externo', 'qty': 1, 'price': 0}],
            'total': 0,
        })
    if found:
        return found

    # Sem parser adaptado ainda
    return []


def _parse_json_orders(data) -> list[dict]:
    if isinstance(data, dict):
        for key in ('orders', 'queue', 'pedidos', 'items', 'data'):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]

    if not isinstance(data, list):
        return []

    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        ext = item.get('external_id') or item.get('id') or item.get('num') or item.get('numero') or ''
        lines_in = item.get('lines') or item.get('itens') or item.get('items') or []
        lines = []
        for ln in lines_in:
            if not isinstance(ln, dict):
                continue
            lines.append({
                'name': str(ln.get('name') or ln.get('nome') or ln.get('product') or 'Item'),
                'qty': int(ln.get('qty') or ln.get('qtd') or ln.get('quantity') or 1),
                'price': float(ln.get('price') or ln.get('preco') or 0),
            })
        if not lines and (item.get('name') or item.get('nome')):
            lines = [{'name': str(item.get('name') or item.get('nome')), 'qty': 1, 'price': float(item.get('price') or 0)}]
        out.append({
            'external_id': str(ext),
            'obs': str(item.get('obs') or item.get('notes') or '').strip(),
            'lines': lines,
            'total': float(item.get('total') or 0),
            'date': item.get('date') or item.get('data') or '',
            'time': item.get('time') or item.get('hora') or '',
        })
    return out


def fetch_remote(url: str, timeout: float = 5.0) -> tuple[str, str]:
    req = urllib.request.Request(
        url,
        headers={'User-Agent': 'HomeSushi-Bridge/1.0', 'Accept': 'text/html,application/json,*/*'},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or 'utf-8'
        body = raw.decode(charset, errors='replace')
        return body, resp.headers.get('Content-Type', '')


def get_state():
    with _bridge_lock:
        return dict(_bridge_state)


def _set_state(**kwargs):
    with _bridge_lock:
        _bridge_state.update(kwargs)


def import_orders(orders: list[dict], ingest_fn: Callable[[dict], dict], source: str = 'bridge') -> dict:
    """Importa pedidos novos via callback ingest_fn(order_dict) -> {ok, num, ...}."""
    seen = _load_seen()
    imported = 0
    skipped = 0
    results = []

    now = datetime.now()
    for raw in orders:
        order = dict(raw or {})
        fp = order_fingerprint(order)
        if fp in seen:
            skipped += 1
            continue

        if not order.get('date'):
            order['date'] = now.strftime('%d/%m/%Y')
        if not order.get('time'):
            order['time'] = now.strftime('%H:%M')
        if not order.get('lines'):
            skipped += 1
            continue

        order.setdefault('pagamento', 'ROCK&DÃO')
        order.setdefault('valor_entregue', 0)
        order.setdefault('troco', 0)
        order['status'] = 'pendente'
        order['toMonitor'] = True
        order['source'] = source
        order['bridge_fp'] = fp
        # Nao imprimir papel cliente automaticamente em imports da ponte
        order['skip_print'] = True

        try:
            res = ingest_fn(order)
        except Exception as e:
            results.append({'ok': False, 'error': str(e), 'fp': fp})
            continue

        if res.get('ok'):
            seen.add(fp)
            imported += 1
            results.append({'ok': True, 'num': res.get('num'), 'fp': fp})
        else:
            results.append({'ok': False, 'error': res.get('error'), 'fp': fp})

    _save_seen(seen)
    with _bridge_lock:
        _bridge_state['imported'] += imported
        _bridge_state['skipped'] += skipped
    return {'imported': imported, 'skipped': skipped, 'results': results}


def _loop(get_config: Callable[[], dict], ingest_fn: Callable[[dict], dict]):
    _set_state(running=True)
    print('  [bridge] thread iniciada')
    while not _bridge_stop.is_set():
        cfg = (get_config() or {}).get('bridge') or {}
        enabled = bool(cfg.get('enabled'))
        url = str(cfg.get('url') or '').strip()
        interval = float(cfg.get('interval_sec') or 1.5)
        interval = max(1.0, min(interval, 60.0))

        _set_state(enabled=enabled, url=url)

        if enabled and url:
            try:
                body, ctype = fetch_remote(url)
                orders = parse_remote_orders(body, ctype)
                if orders:
                    summary = import_orders(orders, ingest_fn, source='bridge')
                    print(f"  [bridge] poll OK — novos={summary['imported']} ignorados={summary['skipped']}")
                else:
                    # Parser ainda nao adaptado ou fila vazia
                    pass
                _set_state(last_poll=datetime.now().isoformat(timespec='seconds'),
                           last_ok=datetime.now().isoformat(timespec='seconds'),
                           last_error=None)
            except Exception as e:
                _set_state(last_poll=datetime.now().isoformat(timespec='seconds'),
                           last_error=str(e))
                print(f'  [bridge] erro: {e}')
        else:
            _set_state(last_error=None)

        _bridge_stop.wait(interval)

    _set_state(running=False)
    print('  [bridge] thread parada')


def start_bridge(get_config: Callable[[], dict], ingest_fn: Callable[[dict], dict]):
    global _bridge_thread
    if _bridge_thread and _bridge_thread.is_alive():
        return
    _bridge_stop.clear()
    _bridge_thread = threading.Thread(
        target=_loop,
        args=(get_config, ingest_fn),
        name='monitor-bridge',
        daemon=True,
    )
    _bridge_thread.start()


def stop_bridge():
    _bridge_stop.set()
