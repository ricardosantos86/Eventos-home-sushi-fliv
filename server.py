import os
import json
import threading
from datetime import datetime
from collections import defaultdict
from flask import Flask, request, jsonify
from flask_cors import CORS
import win32print
from PIL import Image

app = Flask(__name__)
CORS(app)

counter_lock = threading.Lock()

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE  = os.path.join(BASE_DIR, 'config.json')
HISTORY_FILE = os.path.join(BASE_DIR, 'historico.json')
LOGO_PATH    = os.path.join(BASE_DIR, 'logo.png')

# ESC/POS constants
ESC = b'\x1b'
GS  = b'\x1d'
W   = 42  # chars per line (80mm, Font A)

# CP858 suporta € (0xD5) e caracteres portugueses
CODEPAGE_CP858 = ESC + b't\x13'


# ── Histórico & contador ──────────────────────────────────────────────────────

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, encoding='utf-8') as f:
            return json.load(f)
    return []

def save_history(history):
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
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


# ── Config ────────────────────────────────────────────────────────────────────

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


# ── Rotas API ─────────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    cfg = load_config()
    return jsonify({'status': 'ok', 'printer': cfg.get('printer', ''), 'counter': order_counter})


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
    global order_counter
    order = request.get_json()
    if not order:
        return jsonify({'ok': False, 'error': 'Dados inválidos'}), 400

    edit_num = order.get('editNum')
    is_edit  = bool(edit_num)

    with counter_lock:
        num = edit_num if is_edit else f'{order_counter:04d}'

    history = load_history()
    entry = {
        'num':            num,
        'date':           order.get('date', ''),
        'time':           order.get('time', ''),
        'obs':            order.get('obs', ''),
        'lines':          order.get('lines', []),
        'total':          order.get('total', 0),
        'pagamento':      order.get('pagamento', ''),
        'valor_entregue': order.get('valor_entregue', 0),
        'troco':          order.get('troco', 0),
    }
    if is_edit:
        idx = next((i for i, o in enumerate(history) if o.get('num') == num), None)
        if idx is not None:
            history[idx] = entry
        else:
            history.append(entry)
    else:
        history.append(entry)
        with counter_lock:
            order_counter += 1

    save_history(history)
    return jsonify({'ok': True, 'num': num})


@app.route('/api/print', methods=['POST'])
def print_order():
    global order_counter
    order = request.get_json()
    if not order:
        return jsonify({'ok': False, 'error': 'Dados inválidos'}), 400

    cfg = load_config()
    printer_name = cfg.get('printer', '')
    if not printer_name:
        return jsonify({'ok': False, 'error': 'NO_PRINTER'}), 400

    edit_num = order.get('editNum')
    is_edit  = bool(edit_num)

    with counter_lock:
        num = edit_num if is_edit else f'{order_counter:04d}'
    order['num'] = num

    try:
        copy1 = build_escpos_estabelecimento(order)
        copy2 = build_escpos_cliente(order)
        raw_print(printer_name, copy1 + copy2)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    # Guarda no histórico
    history = load_history()
    entry = {
        'num':            num,
        'date':           order.get('date', ''),
        'time':           order.get('time', ''),
        'obs':            order.get('obs', ''),
        'lines':          order.get('lines', []),
        'total':          order.get('total', 0),
        'pagamento':      order.get('pagamento', ''),
        'valor_entregue': order.get('valor_entregue', 0),
        'troco':          order.get('troco', 0),
    }
    if is_edit:
        idx = next((i for i, o in enumerate(history) if o.get('num') == num), None)
        if idx is not None:
            history[idx] = entry
        else:
            history.append(entry)
    else:
        history.append(entry)
        with counter_lock:
            order_counter += 1

    save_history(history)
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


if __name__ == '__main__':
    print('=' * 46)
    print('  HOME SUSHI POS — Servidor de impressão')
    print('  http://localhost:5000')
    print('=' * 46)
    app.run(host='localhost', port=5000, debug=False)
