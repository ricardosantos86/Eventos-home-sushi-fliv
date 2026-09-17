import os
import json
import socket
import threading
import time
import subprocess
import sys
from datetime import datetime
from collections import defaultdict
from flask import Flask, request, jsonify, send_from_directory, make_response
from flask_cors import CORS
import win32print
from PIL import Image

app = Flask(__name__)
CORS(app)

counter_lock = threading.Lock()

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE  = os.path.join(BASE_DIR, 'config.json')
LOGO_PATH    = os.path.join(BASE_DIR, 'logo.png')
LEGACY_HISTORY_FILE = os.path.join(BASE_DIR, 'historico.json')

DEFAULT_EVENTO = {
    'nome': 'ROCK&DÃO',
    'detalhe': '18 e 19 setembro 2026 · Viseu',
    'ficheiro': 'historico_rock_dao.json',
}

try:
    import bridge as monitor_bridge
except ImportError:
    monitor_bridge = None

# ESC/POS constants
ESC = b'\x1b'
GS  = b'\x1d'
W   = 42  # chars per line (80mm, Font A)

# CP858 suporta € (0xD5) e caracteres portugueses
CODEPAGE_CP858 = ESC + b't\x13'


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_config(data):
    try:
        cfg = load_config()
        cfg.update(data)
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except IOError as e:
        print(f'  Erro ao guardar config: {e}')


def get_evento(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    ev = dict(DEFAULT_EVENTO)
    ev.update(cfg.get('evento') or {})
    nome = str(ev.get('nome') or DEFAULT_EVENTO['nome']).strip() or DEFAULT_EVENTO['nome']
    detalhe = str(ev.get('detalhe') or DEFAULT_EVENTO['detalhe']).strip()
    ficheiro = str(ev.get('ficheiro') or '').strip()
    if not ficheiro:
        slug = ''.join(c if c.isalnum() else '_' for c in nome.lower()).strip('_')
        ficheiro = f'historico_{slug or "evento"}.json'
    if not ficheiro.lower().endswith('.json'):
        ficheiro += '.json'
    return {'nome': nome, 'detalhe': detalhe, 'ficheiro': ficheiro}


def history_path(cfg=None):
    return os.path.join(BASE_DIR, get_evento(cfg)['ficheiro'])


def _ensure_history_file():
    """Usa ficheiro do evento; migra historico.json antigo se existir."""
    path = history_path()
    if os.path.exists(path):
        return path
    if os.path.exists(LEGACY_HISTORY_FILE):
        try:
            os.replace(LEGACY_HISTORY_FILE, path)
            print(f'  [historico] migrado historico.json → {os.path.basename(path)}')
        except OSError:
            pass
    return path


# ── Histórico & contador ──────────────────────────────────────────────────────

def load_history():
    path = _ensure_history_file()
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return []

def save_history(history):
    path = _ensure_history_file()
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

def next_counter():
    history = load_history()
    if not history:
        return 1
    nums = []
    for o in history:
        try:
            nums.append(int(o['num']))
        except (KeyError, ValueError):
            pass
    return max(nums) + 1 if nums else 1

# inicializa o contador ao arrancar
order_counter = next_counter()
print(f'  Último pedido detectado: #{order_counter - 1:04d} — a continuar a partir de #{order_counter:04d}')


# ── Impressora ────────────────────────────────────────────────────────────────

def list_printers():
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    return [p['pPrinterName'] for p in win32print.EnumPrinters(flags, None, 4)]

def raw_print(printer_name, data: bytes):
    h = win32print.OpenPrinter(printer_name)
    try:
        win32print.StartDocPrinter(h, 1, ('Talão HOME SUSHI', None, 'RAW'))
        win32print.StartPagePrinter(h)
        win32print.WritePrinter(h, data)
        win32print.EndPagePrinter(h)
        win32print.EndDocPrinter(h)
    finally:
        win32print.ClosePrinter(h)


# ── ESC/POS receipt builder ───────────────────────────────────────────────────

PRINTABLE_DOTS = 576   # 72mm a 203 DPI
LOGO_MAX_W     = 256   # largura máxima do logo (centrado no papel)

def image_to_escpos(path):
    """Converte imagem PNG para bytes ESC/POS (GS v 0), centrada no papel."""
    try:
        img = Image.open(path)

        # Fundo branco para transparência
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGBA')
            bg = Image.new('RGB', img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg

        img = img.convert('L')  # escala de cinzas

        # Remove espaço branco em redor do logo
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)

        # Redimensiona mantendo proporção
        if img.width > LOGO_MAX_W:
            h = int(img.height * LOGO_MAX_W / img.width)
            img = img.resize((LOGO_MAX_W, h), Image.LANCZOS)

        # Centra num canvas com a largura total imprimível
        canvas = Image.new('L', (PRINTABLE_DOTS, img.height), 255)
        canvas.paste(img, ((PRINTABLE_DOTS - img.width) // 2, 0))
        canvas = canvas.convert('1')

        width_bytes = PRINTABLE_DOTS // 8
        height      = canvas.height
        pixels      = canvas.load()

        data = bytearray()
        data += b'\x1d\x76\x30\x00'                          # GS v 0
        data += bytes([width_bytes & 0xFF, width_bytes >> 8]) # largura em bytes
        data += bytes([height & 0xFF, height >> 8])           # altura em linhas

        for y in range(height):
            for bx in range(width_bytes):
                byte = 0
                for bit in range(8):
                    x = bx * 8 + bit
                    if pixels[x, y] == 0:        # pixel preto → imprime
                        byte |= (0x80 >> bit)
                data += bytes([byte])

        return bytes(data)
    except Exception as e:
        print(f'  Aviso: logo nao impresso: {e}')
        return b''

def esc_line(text=''):
    return text.encode('cp858', errors='replace') + b'\n'

def divider():
    return esc_line('-' * W)

def center_pad(text, width=W):
    return text.center(width)

def _obs_lines(buf, obs):
    """Imprime bloco de observações."""
    words, line = obs.split(), ''
    for word in words:
        if len(line) + len(word) + 1 > W:
            buf += esc_line(line)
            line = word
        else:
            line = (line + ' ' + word).strip()
    if line:
        buf += esc_line(line)
    return buf

def build_escpos_estabelecimento(order):
    """Cópia interna — só número, itens e observações."""
    buf = bytearray()
    buf += ESC + b'@'
    buf += CODEPAGE_CP858

    # Data + hora (pequeno, esquerda)
    buf += ESC + b'a\x00'
    buf += esc_line(f"{order['date']}  {order['time']}")

    # Número grande
    buf += ESC + b'a\x01'
    buf += ESC + b'!\x38'
    buf += esc_line(f"PEDIDO #{order['num']}")
    buf += ESC + b'!\x00'
    buf += ESC + b'a\x00'
    buf += divider()

    # Itens — só quantidade e nome, sem preços
    COL_QTY  = 4
    COL_NAME = W - COL_QTY - 2
    for item in order['lines']:
        qty_s = f"{item['qty']}x"
        name  = item['name'][:COL_NAME]
        buf += esc_line(f"{qty_s:<{COL_QTY}}  {name}")

    buf += divider()

    # Observações
    obs = order.get('obs', '').strip()
    if obs:
        buf += ESC + b'E\x01'
        buf += esc_line('OBS:')
        buf += ESC + b'E\x00'
        buf = _obs_lines(buf, obs)

    # ── Pagamento (cópia interna) ──
    pagamento = order.get('pagamento', '')
    if pagamento:
        buf += ESC + b'E\x01'
        buf += esc_line(pagamento)
        buf += ESC + b'E\x00'

    buf += b'\n\n\n'
    buf += GS + b'V\x41\x03'   # corte
    return bytes(buf)

def build_escpos_cliente(order):
    """Cópia completa para o cliente."""
    buf = bytearray()
    buf += ESC + b'@'
    buf += CODEPAGE_CP858

    # ── Cabeçalho ──
    logo = image_to_escpos(LOGO_PATH)
    if logo:
        buf += logo
        buf += b'\n\n'
    else:
        buf += ESC + b'a\x01'
        buf += ESC + b'!\x38'
        buf += esc_line('HOME SUSHI')
        buf += ESC + b'!\x00'
        buf += b'\n'

    buf += ESC + b'J\x08'          # avanço (~1mm) antes de centrar
    buf += ESC + b'a\x01'          # centrar
    buf += esc_line('Urb. Quinta del Rei, Lote 264')
    buf += esc_line('3500-613 Viseu')
    buf += esc_line('Tel: 232 396 351 | 933 330 867')
    buf += esc_line('viseu@homesushi.pt')
    buf += esc_line('NIF: 513 977 970')
    buf += b'\n'

    # ── Número ──
    buf += ESC + b'a\x00'          # esquerda
    buf += divider()
    date_part  = f"{order['date']} {order['time']}"
    copy_label = 'Cópia - Cliente'
    pad = W - len(date_part) - len(copy_label)
    buf += esc_line(copy_label + ' ' * max(pad, 1) + date_part)
    buf += ESC + b'a\x01'          # centrar
    buf += ESC + b'!\x38'
    buf += esc_line(f"PEDIDO #{order['num']}")
    buf += ESC + b'!\x00'
    buf += ESC + b'a\x00'          # esquerda
    buf += divider()

    # ── Itens ──
    COL_QTY  = 4
    COL_SUB  = 9
    COL_NAME = W - COL_QTY - COL_SUB - 2
    buf += esc_line(f"{'Qtd':<{COL_QTY}}  {'Item':<{COL_NAME}}{'Sub':>{COL_SUB}}")
    buf += divider()
    for item in order['lines']:
        qty_s = f"{item['qty']}x"
        name  = item['name'][:COL_NAME]
        sub   = f"{item['price'] * item['qty']:.2f}".replace('.', ',') + '€'
        buf += esc_line(f"{qty_s:<{COL_QTY}}  {name:<{COL_NAME}}{sub:>{COL_SUB}}")
    buf += divider()

    # ── Observações ──
    obs = order.get('obs', '').strip()
    if obs:
        buf += ESC + b'E\x01'
        buf += esc_line('OBS:')
        buf += ESC + b'E\x00'
        buf = _obs_lines(buf, obs)
        buf += divider()

    # ── Total ──
    buf += ESC + b'a\x00'          # esquerda
    buf += ESC + b'!\x10'
    buf += ESC + b'E\x01'
    total_s = f"{order['total']:.2f}".replace('.', ',') + '€'
    buf += esc_line(f"TOTAL{total_s:>{W - 5}}")
    buf += ESC + b'!\x00'
    buf += ESC + b'E\x00'
    buf += divider()

    # ── Pagamento ──
    pagamento = order.get('pagamento', '')
    if pagamento:
        buf += esc_line(f"{'PAGAMENTO:':<12}{pagamento}")
        if pagamento == 'DINHEIRO':
            valor_entregue = float(order.get('valor_entregue', 0) or 0)
            troco          = float(order.get('troco', 0) or 0)
            if valor_entregue > 0:
                val_s   = f"{valor_entregue:.2f}".replace('.', ',') + '€'
                troco_s = f"{troco:.2f}".replace('.', ',') + '€'
                buf += esc_line(f"{'ENTREGUE:':<12}{val_s}")
                buf += esc_line(f"{'TROCO:':<12}{troco_s}")
        buf += divider()

    # ── Rodapé ──
    buf += b'\n'
    buf += ESC + b'a\x01'          # centrar
    buf += ESC + b'E\x01'
    buf += esc_line('Apresente este talão')
    buf += esc_line('para levantar o seu pedido.')
    buf += ESC + b'E\x00'
    buf += b'\n'
    buf += esc_line('Obrigado pela sua visita!')
    buf += esc_line('www.homesushi.pt')
    buf += b'\n\n\n'
    buf += GS + b'V\x41\x03'
    return bytes(buf)


def build_escpos_daily_report(date_str, orders):
    """Talão de fecho do dia (Z-report): resumo, pagamentos e itens."""
    buf = bytearray()
    buf += ESC + b'@'
    buf += CODEPAGE_CP858

    # ── Cabeçalho ──
    logo = image_to_escpos(LOGO_PATH)
    if logo:
        buf += logo
        buf += b'\n'
    buf += ESC + b'a\x01'
    buf += ESC + b'!\x38'
    buf += esc_line('FECHO DO DIA')
    buf += ESC + b'!\x00'
    buf += b'\n'
    buf += esc_line(f"Data: {date_str}")
    buf += esc_line(f"Emitido: {datetime.now().strftime('%d/%m/%Y %H:%M')}")
    buf += ESC + b'a\x00'
    buf += divider()

    # ── Resumo ──
    total_geral = sum(float(o.get('total', 0) or 0) for o in orders)
    n_pedidos   = len(orders)

    buf += ESC + b'E\x01'
    buf += esc_line('RESUMO')
    buf += ESC + b'E\x00'
    n_str     = str(n_pedidos)
    total_str = f"{total_geral:.2f}".replace('.', ',') + '€'
    buf += esc_line(f"{'Pedidos:':<20}{n_str:>{W - 20}}")
    buf += esc_line(f"{'Total:':<20}{total_str:>{W - 20}}")
    buf += divider()

    # ── Por forma de pagamento ──
    by_pay = defaultdict(lambda: {'count': 0, 'total': 0.0})
    for o in orders:
        pay = (o.get('pagamento') or 'SEM REGISTO').upper()
        by_pay[pay]['count'] += 1
        by_pay[pay]['total'] += float(o.get('total', 0) or 0)

    if by_pay:
        buf += ESC + b'E\x01'
        buf += esc_line('PAGAMENTOS')
        buf += ESC + b'E\x00'
        for pay, data in sorted(by_pay.items()):
            label    = f"{pay} ({data['count']})"
            total_s  = f"{data['total']:.2f}".replace('.', ',') + '€'
            buf += esc_line(f"{label:<20}{total_s:>{W - 20}}")
        buf += divider()

    # ── Itens vendidos ──
    by_item = defaultdict(lambda: {'qty': 0, 'total': 0.0})
    for o in orders:
        for line in o.get('lines', []):
            name  = line.get('name', '')
            qty   = int(line.get('qty', 0) or 0)
            price = float(line.get('price', 0) or 0)
            by_item[name]['qty']   += qty
            by_item[name]['total'] += qty * price

    if by_item:
        buf += ESC + b'E\x01'
        buf += esc_line('ITENS VENDIDOS')
        buf += ESC + b'E\x00'
        COL_QTY  = 4
        COL_SUB  = 9
        COL_NAME = W - COL_QTY - COL_SUB - 2
        total_qty = 0
        for name, data in sorted(by_item.items(), key=lambda kv: -kv[1]['qty']):
            qty_s = f"{data['qty']}x"
            sub   = f"{data['total']:.2f}".replace('.', ',') + '€'
            n     = name[:COL_NAME]
            buf += esc_line(f"{qty_s:<{COL_QTY}}  {n:<{COL_NAME}}{sub:>{COL_SUB}}")
            total_qty += data['qty']
        buf += divider()
        buf += esc_line(f"{'Total artigos:':<20}{total_qty:>{W - 20}}")
        buf += divider()

    # ── Total geral ──
    buf += ESC + b'a\x00'
    buf += ESC + b'!\x10'
    buf += ESC + b'E\x01'
    buf += esc_line(f"TOTAL{total_str:>{W - 5}}")
    buf += ESC + b'!\x00'
    buf += ESC + b'E\x00'
    buf += divider()

    buf += b'\n'
    buf += ESC + b'a\x01'
    buf += esc_line('--- Fim de relatório ---')
    buf += b'\n\n\n'
    buf += GS + b'V\x41\x03'
    return bytes(buf)


def build_escpos_controlo(order, titulo='CONTROLO', rodape='CONCLUIDO', simples=False):
    """Papel de controlo. simples=True: só número e itens (Concluído no monitor)."""
    buf = bytearray()
    buf += ESC + b'@'
    buf += CODEPAGE_CP858

    buf += ESC + b'a\x01'
    if not simples:
        buf += ESC + b'E\x01'
        buf += esc_line(titulo)
        buf += ESC + b'E\x00'
        buf += esc_line(f"{order.get('date', '')}  {order.get('time', '')}")
        buf += divider()

    buf += ESC + b'!\x38'
    buf += esc_line(f"PEDIDO #{order['num']}")
    buf += ESC + b'!\x00'
    buf += ESC + b'a\x00'
    buf += divider()

    COL_QTY  = 4
    COL_NAME = W - COL_QTY - 2
    for item in order.get('lines', []):
        qty_s = f"{int(item.get('qty', 0) or 0)}x"
        name  = str(item.get('name', ''))[:COL_NAME]
        buf += esc_line(f"{qty_s:<{COL_QTY}}  {name}")

    buf += divider()

    obs = str(order.get('obs', '') or '').strip()
    if obs:
        buf += ESC + b'E\x01'
        buf += esc_line('OBS:')
        buf += ESC + b'E\x00'
        buf = _obs_lines(buf, obs)
        buf += divider()

    if not simples:
        pagamento = str(order.get('pagamento', '') or '').strip()
        total = float(order.get('total', 0) or 0)
        total_s = f"{total:.2f}".replace('.', ',') + '€'
        meta = f"{pagamento} · {total_s}" if pagamento else total_s
        buf += ESC + b'a\x01'
        buf += ESC + b'!\x10'
        buf += esc_line(meta)
        buf += ESC + b'!\x00'
        buf += b'\n'
        if rodape:
            buf += ESC + b'E\x01'
            buf += esc_line(rodape)
            buf += ESC + b'E\x00'

    buf += b'\n\n\n'
    buf += GS + b'V\x41\x03'
    return bytes(buf)


# ── Rede / páginas ────────────────────────────────────────────────────────────

def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def find_order_index(history, num):
    num = str(num)
    return next((i for i, o in enumerate(history) if str(o.get('num', '')) == num), None)


def print_order_copies(order):
    cfg = load_config()
    printer_name = cfg.get('printer', '')
    if not printer_name:
        raise ValueError('NO_PRINTER')
    # Garante tipos numéricos (histórico pode ter total em string)
    order = dict(order)
    order['total'] = float(order.get('total', 0) or 0)
    for line in order.get('lines', []):
        line['qty'] = int(line.get('qty', 0) or 0)
        line['price'] = float(line.get('price', 0) or 0)
    copy1 = build_escpos_estabelecimento(order)
    copy2 = build_escpos_cliente(order)
    raw_print(printer_name, copy1 + copy2)


def print_controlo(order, titulo='CONTROLO', rodape='CONCLUIDO', simples=False):
    """Imprime um papel de controlo."""
    cfg = load_config()
    printer_name = cfg.get('printer', '')
    if not printer_name:
        raise ValueError('NO_PRINTER')
    order = dict(order)
    order['total'] = float(order.get('total', 0) or 0)
    for line in order.get('lines', []):
        line['qty'] = int(line.get('qty', 0) or 0)
        line['price'] = float(line.get('price', 0) or 0)
    raw_print(printer_name, build_escpos_controlo(order, titulo=titulo, rodape=rodape, simples=simples))


# ── Rotas API ─────────────────────────────────────────────────────────────────

def _html_page(filename):
    resp = make_response(send_from_directory(BASE_DIR, filename))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@app.route('/')
def index():
    return _html_page('home_sushi_pos.html')


@app.route('/monitor')
def monitor_page():
    return _html_page('monitor.html')


@app.route('/logo.png')
def serve_logo():
    return send_from_directory(BASE_DIR, 'logo.png')


@app.route('/health')
def health():
    cfg = load_config()
    ev = get_evento(cfg)
    lan_ip = get_lan_ip()
    base = f'http://{lan_ip}:5000' if lan_ip else 'http://127.0.0.1:5000'
    return jsonify({
        'status': 'ok',
        'printer': cfg.get('printer', ''),
        'counter': order_counter,
        'evento': ev,
        'historico_ficheiro': ev['ficheiro'],
        'lan_ip': lan_ip or '',
        'pos_url': base + '/',
        'monitor_url': base + '/monitor',
    })


@app.route('/api/counter')
def get_counter():
    return jsonify({'counter': order_counter})


@app.route('/api/printers')
def get_printers():
    try:
        printers = list_printers()
        cfg = load_config()
        return jsonify({'printers': printers, 'selected': cfg.get('printer', '')})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/config', methods=['GET', 'POST'])
def config():
    if request.method == 'POST':
        save_config(request.get_json())
        return jsonify({'ok': True})
    return jsonify(load_config())


@app.route('/api/history')
def get_history():
    return jsonify({'history': load_history()})


@app.route('/api/finalize', methods=['POST'])
def finalize_order():
    order = request.get_json()
    if not order:
        return jsonify({'ok': False, 'error': 'Dados inválidos'}), 400
    return jsonify(create_order_entry(order))


def create_order_entry(order):
    """Cria/atualiza pedido no historico. Usado pelo POS e pela ponte externa."""
    global order_counter
    if not order:
        return {'ok': False, 'error': 'Dados inválidos'}

    edit_num = order.get('editNum')
    is_edit  = bool(edit_num)

    with counter_lock:
        num = edit_num if is_edit else f'{order_counter:04d}'

    history = load_history()
    entry = {
        'num':            num,
        'date':           order.get('date', ''),
        'time':           order.get('time', ''),
        'cliente':        str(order.get('cliente', '') or '').strip(),
        'obs':            order.get('obs', ''),
        'lines':          order.get('lines', []),
        'total':          order.get('total', 0),
        'pagamento':      order.get('pagamento', ''),
        'valor_entregue': order.get('valor_entregue', 0),
        'troco':          order.get('troco', 0),
        'status':         'pendente' if order.get('toMonitor', True) else 'registado',
        'source':         order.get('source', 'pos'),
    }
    if order.get('external_id'):
        entry['external_id'] = str(order.get('external_id'))
    if order.get('bridge_fp'):
        entry['bridge_fp'] = str(order.get('bridge_fp'))

    status_in = str(order.get('status', '') or '').strip().lower()
    if status_in in ('pendente', 'registado'):
        entry['status'] = status_in
    if is_edit:
        idx = find_order_index(history, num)
        if idx is not None:
            history[idx] = entry
        else:
            history.append(entry)
    else:
        history.append(entry)
        with counter_lock:
            order_counter += 1

    save_history(history)

    printed = False
    print_error = None
    skip_print = bool(order.get('skip_print'))
    # Ao enviar para o monitor (POS), imprime papel de controlo para o cliente
    if entry['status'] == 'pendente' and not skip_print:
        try:
            print_controlo(
                entry,
                titulo='CONTROLO CLIENTE',
                rodape='Apresente este talao',
            )
            printed = True
        except ValueError as e:
            print_error = str(e)
        except Exception as e:
            print_error = str(e)

    return {
        'ok': True,
        'num': num,
        'status': entry['status'],
        'printed': printed,
        'print_error': print_error,
    }


@app.route('/api/evento', methods=['GET', 'POST'])
def api_evento():
    """Lê / atualiza nome do evento (dashboard)."""
    if request.method == 'GET':
        return jsonify({'ok': True, 'evento': get_evento()})

    payload = request.get_json() or {}
    cfg = load_config()
    atual = get_evento(cfg)
    antigo_path = history_path(cfg)

    nome = str(payload.get('nome', atual['nome']) or '').strip() or atual['nome']
    detalhe = str(payload.get('detalhe', atual['detalhe']) or '').strip()
    ficheiro = str(payload.get('ficheiro', atual['ficheiro']) or '').strip() or atual['ficheiro']
    if not ficheiro.lower().endswith('.json'):
        ficheiro += '.json'
    # Evita path traversal
    ficheiro = os.path.basename(ficheiro)

    novo = {'nome': nome, 'detalhe': detalhe, 'ficheiro': ficheiro}
    cfg['evento'] = novo
    save_config(cfg)

    novo_path = history_path(cfg)
    renamed = False
    if antigo_path != novo_path and os.path.exists(antigo_path) and not os.path.exists(novo_path):
        try:
            os.replace(antigo_path, novo_path)
            renamed = True
            print(f'  [historico] renomeado {os.path.basename(antigo_path)} → {os.path.basename(novo_path)}')
        except OSError as e:
            return jsonify({
                'ok': False,
                'error': f'Não foi possível renomear o ficheiro: {e}',
                'evento': get_evento(),
            }), 500

    return jsonify({'ok': True, 'evento': get_evento(), 'renamed': renamed})


@app.route('/api/bridge', methods=['GET', 'POST'])
def bridge_config():
    """Estado e configuracao da ponte para monitor externo."""
    if request.method == 'POST':
        payload = request.get_json() or {}
        cfg = load_config()
        bridge_cfg = dict(cfg.get('bridge') or {})
        if 'enabled' in payload:
            bridge_cfg['enabled'] = bool(payload.get('enabled'))
        if 'url' in payload:
            bridge_cfg['url'] = str(payload.get('url') or '').strip()
        if 'interval_sec' in payload:
            try:
                interval = float(payload.get('interval_sec') or 1.5)
                bridge_cfg['interval_sec'] = max(1.0, min(interval, 60.0))
            except (TypeError, ValueError):
                return jsonify({'ok': False, 'error': 'interval_sec invalido'}), 400
        cfg['bridge'] = bridge_cfg
        save_config(cfg)
        return jsonify({'ok': True, 'bridge': bridge_cfg})

    cfg = load_config().get('bridge') or {}
    state = monitor_bridge.get_state() if monitor_bridge else {}
    return jsonify({'ok': True, 'bridge': cfg, 'state': state})


@app.route('/api/bridge/import', methods=['POST'])
def bridge_import():
    """
    Importacao manual / teste:
      { "html": "<...>" }  ou  { "json": {...}|[...] }  ou  { "orders": [...] }
    """
    if not monitor_bridge:
        return jsonify({'ok': False, 'error': 'bridge indisponivel'}), 500
    payload = request.get_json() or {}
    orders = payload.get('orders')
    if orders is None and payload.get('json') is not None:
        raw = payload.get('json')
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        orders = monitor_bridge.parse_remote_orders(text, 'application/json')
    if orders is None and payload.get('html'):
        orders = monitor_bridge.parse_remote_orders(str(payload.get('html')), 'text/html')
    if not isinstance(orders, list):
        return jsonify({'ok': False, 'error': 'Sem pedidos para importar'}), 400

    summary = monitor_bridge.import_orders(orders, create_order_entry, source='bridge-manual')
    return jsonify({'ok': True, **summary})


@app.route('/api/print', methods=['POST'])
def print_order():
    global order_counter
    order = request.get_json()
    if not order:
        return jsonify({'ok': False, 'error': 'Dados inválidos'}), 400

    edit_num = order.get('editNum')
    is_edit  = bool(edit_num)

    with counter_lock:
        num = edit_num if is_edit else f'{order_counter:04d}'
    order['num'] = num

    try:
        print_order_copies(order)
    except ValueError as e:
        if str(e) == 'NO_PRINTER':
            return jsonify({'ok': False, 'error': 'NO_PRINTER'}), 400
        return jsonify({'ok': False, 'error': str(e)}), 500
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    # Guarda no histórico (impressão directa — não passa pelo monitor)
    history = load_history()
    entry = {
        'num':            num,
        'date':           order.get('date', ''),
        'time':           order.get('time', ''),
        'cliente':        str(order.get('cliente', '') or '').strip(),
        'obs':            order.get('obs', ''),
        'lines':          order.get('lines', []),
        'total':          order.get('total', 0),
        'pagamento':      order.get('pagamento', ''),
        'valor_entregue': order.get('valor_entregue', 0),
        'troco':          order.get('troco', 0),
        'status':         'impresso',
    }
    if is_edit:
        idx = find_order_index(history, num)
        if idx is not None:
            history[idx] = entry
        else:
            history.append(entry)
    else:
        history.append(entry)
        with counter_lock:
            order_counter += 1

    save_history(history)
    return jsonify({'ok': True, 'num': num, 'status': 'impresso'})


@app.route('/api/queue')
def get_queue():
    history = load_history()
    pending = [o for o in history if o.get('status') == 'pendente']
    pending.sort(key=lambda o: int(o.get('num', 0) or 0))
    done = [o for o in history if o.get('status') in ('impresso', 'removido', 'rejeitado')]
    done.sort(key=lambda o: int(o.get('num', 0) or 0), reverse=True)

    def _public(order):
        view = dict(order)
        view.pop('cliente', None)
        return view

    return jsonify({
        'queue': [_public(o) for o in pending],
        'count': len(pending),
        'cancelled': [_public(o) for o in done[:80]],
        'cancelled_count': len(done),
        'evento': get_evento(),
    })


@app.route('/api/queue/approve', methods=['POST'])
def approve_queue_order():
    payload = request.get_json() or {}
    num = str(payload.get('num', '')).strip()
    if not num:
        return jsonify({'ok': False, 'error': 'Numero em falta'}), 400

    with counter_lock:
        history = load_history()
        idx = find_order_index(history, num)
        if idx is None:
            return jsonify({'ok': False, 'error': 'Pedido nao encontrado'}), 404
        order = history[idx]
        if order.get('status') != 'pendente':
            return jsonify({'ok': False, 'error': 'Pedido ja processado'}), 409

        try:
            print_controlo(order, simples=True)
        except ValueError as e:
            if str(e) == 'NO_PRINTER':
                return jsonify({'ok': False, 'error': 'NO_PRINTER'}), 400
            return jsonify({'ok': False, 'error': str(e)}), 500
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)}), 500

        history[idx]['status'] = 'impresso'
        save_history(history)

    return jsonify({'ok': True, 'num': num, 'status': 'impresso'})


@app.route('/api/queue/reject', methods=['POST'])
def reject_queue_order():
    payload = request.get_json() or {}
    num = str(payload.get('num', '')).strip()
    if not num:
        return jsonify({'ok': False, 'error': 'Numero em falta'}), 400

    with counter_lock:
        history = load_history()
        idx = find_order_index(history, num)
        if idx is None:
            return jsonify({'ok': False, 'error': 'Pedido nao encontrado'}), 404
        if history[idx].get('status') != 'pendente':
            return jsonify({'ok': False, 'error': 'Pedido ja processado'}), 409

        history[idx]['status'] = 'removido'
        save_history(history)

    return jsonify({'ok': True, 'num': num, 'status': 'removido'})


@app.route('/api/queue/restore', methods=['POST'])
def restore_queue_order():
    payload = request.get_json() or {}
    num = str(payload.get('num', '')).strip()
    if not num:
        return jsonify({'ok': False, 'error': 'Numero em falta'}), 400

    with counter_lock:
        history = load_history()
        idx = find_order_index(history, num)
        if idx is None:
            return jsonify({'ok': False, 'error': 'Pedido nao encontrado'}), 404
        if history[idx].get('status') not in ('rejeitado', 'removido', 'impresso'):
            return jsonify({'ok': False, 'error': 'Este pedido nao pode ser recuperado para a fila'}), 409

        history[idx]['status'] = 'pendente'
        save_history(history)

    return jsonify({'ok': True, 'num': num, 'status': 'pendente'})


@app.route('/api/delete', methods=['POST'])
def delete_order():
    payload = request.get_json() or {}
    num = str(payload.get('num', '')).strip()
    if not num:
        return jsonify({'ok': False, 'error': 'Numero em falta'}), 400

    history = load_history()
    new_history = [o for o in history if str(o.get('num', '')) != num]
    if len(new_history) == len(history):
        return jsonify({'ok': False, 'error': 'Pedido nao encontrado'}), 404

    save_history(new_history)
    return jsonify({'ok': True, 'num': num})


@app.route('/api/daily_report', methods=['POST'])
def print_daily_report():
    payload = request.get_json() or {}
    date_str = payload.get('date', '').strip()
    if not date_str:
        return jsonify({'ok': False, 'error': 'Data em falta'}), 400

    cfg = load_config()
    printer_name = cfg.get('printer', '')
    if not printer_name:
        return jsonify({'ok': False, 'error': 'NO_PRINTER'}), 400

    history = load_history()
    orders  = [o for o in history if o.get('date') == date_str]
    if not orders:
        return jsonify({'ok': False, 'error': 'Sem pedidos para este dia'}), 404

    try:
        data = build_escpos_daily_report(date_str, orders)
        raw_print(printer_name, data)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    return jsonify({
        'ok': True,
        'pedidos': len(orders),
        'total': sum(float(o.get('total', 0) or 0) for o in orders),
    })


@app.route('/api/shutdown', methods=['POST'])
def shutdown():
    """Encerra servidor, CMDs e janelas do browser do POS/Monitor."""
    def kill_and_exit():
        time.sleep(0.35)
        flags = 0x08000000 if sys.platform == 'win32' else 0  # CREATE_NO_WINDOW
        base = BASE_DIR.replace('\\', '\\\\')

        # 1) PIDs gravados no arranque
        for name in ('.pos_launcher.pid', '.pos_server.pid'):
            path = os.path.join(BASE_DIR, name)
            try:
                with open(path, encoding='utf-8') as f:
                    pid = int(f.read().strip())
                if pid and pid != os.getpid():
                    subprocess.run(
                        ['taskkill', '/F', '/T', '/PID', str(pid)],
                        capture_output=True, creationflags=flags
                    )
            except Exception:
                pass
            try:
                os.remove(path)
            except Exception:
                pass

        # 2) PowerShell: fechar browsers do POS + CMDs + python server.py
        ps = f'''
$ErrorActionPreference = 'SilentlyContinue'
Get-Process chrome,msedge,brave,firefox -ErrorAction SilentlyContinue | ForEach-Object {{
  $t = $_.MainWindowTitle
  if ($t -and ($t -match 'Home Sushi|Monitor de Pedidos|HOME SUSHI')) {{
    try {{ [void]$_.CloseMainWindow() }} catch {{}}
  }}
}}
Start-Sleep -Milliseconds 500
Get-Process cmd,powershell,WindowsTerminal -ErrorAction SilentlyContinue | ForEach-Object {{
  $t = $_.MainWindowTitle
  if ($t -and ($t -match 'HOME SUSHI POS|HOME SUSHI Servidor')) {{
    try {{ Stop-Process -Id $_.Id -Force }} catch {{}}
  }}
}}
Get-CimInstance Win32_Process | Where-Object {{
  $_.Name -match '^python' -and $_.CommandLine -and ($_.CommandLine -like '*server.py*') -and ($_.CommandLine -like '*{base}*')
}} | ForEach-Object {{
  try {{ Stop-Process -Id $_.ProcessId -Force }} catch {{}}
}}
taskkill /F /FI "WINDOWTITLE eq HOME SUSHI POS*" 2>$null | Out-Null
taskkill /F /FI "WINDOWTITLE eq HOME SUSHI Servidor*" 2>$null | Out-Null
'''
        try:
            subprocess.run(
                ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps],
                capture_output=True, creationflags=flags, timeout=8
            )
        except Exception:
            pass

        os._exit(0)

    threading.Thread(target=kill_and_exit, daemon=True).start()
    return jsonify({'ok': True})


if __name__ == '__main__':
    # Grava PID para o SAIR conseguir encerrar tudo
    try:
        with open(os.path.join(BASE_DIR, '.pos_server.pid'), 'w', encoding='utf-8') as f:
            f.write(str(os.getpid()))
    except Exception:
        pass

    # Ponte para monitor externo (desligada ate configurares URL + parser)
    if monitor_bridge:
        monitor_bridge.start_bridge(load_config, create_order_entry)

    lan_ip = get_lan_ip()
    ev = get_evento()
    print('=' * 46)
    print(f'  HOME SUSHI POS — {ev["nome"]}')
    print(f'  {ev["detalhe"]}')
    print(f'  Histórico: {ev["ficheiro"]}')
    print('  POS:     http://localhost:5000')
    print('  Monitor: http://localhost:5000/monitor')
    if lan_ip:
        print(f'  Rede POS: http://{lan_ip}:5000/')
        print(f'  Rede Mon: http://{lan_ip}:5000/monitor')
    bridge_on = bool((load_config().get('bridge') or {}).get('enabled'))
    print(f'  Bridge:  {"ligada" if bridge_on else "desligada"}')
    print('=' * 46)
    app.run(host='0.0.0.0', port=5000, debug=False)
