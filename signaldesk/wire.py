from contextlib import closing
"""Strict shared signal protocol; no broker commands or volume from the network."""
import json
import sqlite3
import time
import uuid
from pathlib import Path
from .core import SignalError, dec, parse_signal


def number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise SignalError('Цена должна быть числом.')
    try:
        result = dec(value)
    except Exception:
        raise SignalError('Неверная цена.') from None
    if not result.is_finite() or result <= 0:
        raise SignalError('Цена должна быть конечным положительным числом.')
    return format(result, 'f')


CONTROL_TYPES = {'terminate_signal', 'terminate_tp', 'modify_tp', 'breakeven_signal', 'breakeven_tp', 'cancel_orders', 'modify_sl', 'cancel_all_orders', 'terminate_all_signals', 'modify_all_sl'}


def validate_control(event):
    kind = event['event_type']
    body = event.get('event_body')
    fields = {'id', 'signal_id'}
    if kind in ('terminate_tp', 'modify_tp', 'breakeven_tp'): fields.add('tp_index')
    if kind == 'modify_tp': fields.add('new_tp')
    if kind in ('modify_sl','modify_all_sl'): fields.add('new_sl')
    if not isinstance(body, dict) or set(body) != fields:
        raise SignalError('Неверные поля команды управления сигналом.')
    body = body.copy()
    for key in ('id', 'signal_id'):
        try: body[key] = str(uuid.UUID(body[key]))
        except (ValueError, TypeError, AttributeError): raise SignalError('ID должен быть UUID.') from None
    if 'tp_index' in body and (type(body['tp_index']) is not int or not 1 <= body['tp_index'] <= 20):
        raise SignalError('Номер тейка должен быть от 1 до 20.')
    if kind == 'modify_tp': body['new_tp'] = number(body['new_tp'])
    if kind in ('modify_sl','modify_all_sl'): body['new_sl'] = number(body['new_sl'])
    normalized = {'event_type': kind, 'event_body': body}
    return normalized, normalized, kind


def validate_event(event):
    if isinstance(event, dict) and set(event) == {'event_type', 'event_body'} and event.get('event_type') in CONTROL_TYPES:
        return validate_control(event)
    if not isinstance(event, dict) or set(event) != {'event_type', 'event_body'} or event['event_type'] != 'open_order':
        raise SignalError('Нужны event_type: open_order и event_body.')
    body = event['event_body']
    fields = {'id', 'side', 'symbol', 'entry', 'take_profits', 'stop_loss'}
    if not isinstance(body, dict) or set(body) != fields:
        raise SignalError('event_body: id, side, symbol, entry, take_profits, stop_loss.')
    try:
        ident = str(uuid.UUID(body['id']))
    except (ValueError, TypeError, AttributeError):
        raise SignalError('id должен быть UUID.') from None
    if not isinstance(body['side'], str) or not isinstance(body['symbol'], str):
        raise SignalError('Неверное направление или инструмент.')
    side = body['side'].upper()
    symbol = body['symbol'].upper()
    if side not in ('BUY', 'SELL') or symbol not in ('XAUUSD', 'XAGUSD'):
        raise SignalError('Поддерживаются BUY/SELL и XAUUSD/XAGUSD.')
    entry = body['entry']
    if isinstance(entry, list):
        if len(entry) != 2:
            raise SignalError('Диапазон entry должен содержать две цены.')
        entry = [number(v) for v in entry]
        entry_text = ' - '.join(entry)
    else:
        entry = number(entry)
        entry_text = entry
    targets = body['take_profits']
    if not isinstance(targets, list) or not 1 <= len(targets) <= 20:
        raise SignalError('take_profits — массив из 1–20 цен.')
    targets = [number(v) for v in targets]
    stop = number(body['stop_loss'])
    text = f'{side} {symbol}\n{entry_text}\n' + '\n'.join(f'TP {i}: {v}' for i, v in enumerate(targets, 1)) + f'\nSL: {stop}'
    signal = parse_signal(text)
    normalized = {'event_type': 'open_order', 'event_body': dict(id=ident, side=side, symbol=symbol, entry=entry, take_profits=targets, stop_loss=stop)}
    return normalized, signal, text


def decode_delivery(raw):
    if len(raw.encode()) > 16384:
        raise SignalError('Слишком большой сигнал.')
    packet = json.loads(raw)
    if not isinstance(packet, dict) or set(packet) != {'event_type', 'event_body'} or not isinstance(packet['event_body'], dict):
        raise SignalError('Неверная доставка сигнала.')
    packet['event_body'] = packet['event_body'].copy()
    expiry = packet['event_body'].pop('expires_at', None)
    if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not time.time() < expiry <= time.time() + 125:
        raise SignalError('Сигнал устарел или имеет неверный срок действия.')
    event, signal, text = validate_event({k: packet[k] for k in ('event_type', 'event_body')})
    return event['event_body']['id'], signal, text, expiry


class Inbox:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS inbox(id TEXT PRIMARY KEY, state TEXT, updated REAL)')

    def claim(self, ident):
        with closing(sqlite3.connect(self.path)) as db, db:
            try:
                db.execute('INSERT INTO inbox VALUES(?,?,?)', (ident, 'received', time.time()))
                return True
            except sqlite3.IntegrityError:
                return False

    def finish(self, ident, state):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE inbox SET state=?,updated=? WHERE id=?', (state, time.time(), ident))
