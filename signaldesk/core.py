from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, ROUND_FLOOR
import re


class SignalError(ValueError):
    pass


@dataclass(frozen=True)
class Signal:
    side: str
    symbol: str
    low: Decimal
    high: Decimal
    targets: tuple[Decimal, ...]
    stop: Decimal
    limit: bool = False


@dataclass(frozen=True)
class Leg:
    entry: Decimal
    target: Decimal
    stop: Decimal
    lot: Decimal


NUM = r"\d+(?:[.,]\d+)?"


def dec(value):
    return Decimal(str(value).replace(',', '.'))


def parse_signal(text: str) -> Signal:
    text = text.replace('−', '-').replace('–', '-').replace('—', '-')
    heads = list(re.finditer(r'\b(BUY|SELL)\s+(?:LIMIT\s+)?([A-Z][A-Z0-9_.#-]*)', text, re.I))
    if len(heads) != 1:
        raise SignalError('Вставьте ровно один сигнал с BUY или SELL и названием инструмента.')
    head = heads[0]
    body = text[head.end():]
    entry = re.match(rf'\s*(?:[:@]|ENTRY\s*:?)?\s*({NUM})(?:\s*-\s*({NUM}))?', body, re.I)
    if not entry:
        raise SignalError('После BUY/SELL и инструмента нужен вход: например 4185 - 4186.')
    lo, hi = sorted((dec(entry[1]), dec(entry[2] or entry[1])))
    tail = body[entry.end():]
    target_pattern = rf'(?:take\s*profit|\bTP|\bТП|тейк(?:\s*профит)?)\s*(\d+)?\s*[:=]\s*({NUM})'
    stop_pattern = rf'(?:\bSL\b|stop\s*loss|\bстоп(?:\s*лосс)?)\s*[:=]?\s*({NUM})'
    targets = list(re.finditer(target_pattern, tail, re.I))
    markers = re.findall(r'(?:take\s*profit|\bTP\b|\bТП\b|тейк(?:\s*профит)?)', tail, re.I)
    if len(markers) != len(targets):
        raise SignalError('Не удалось прочитать все тейки. Используйте формат «TP 1: 4179.58».')
    stops = re.findall(stop_pattern, tail, re.I)
    if not targets or len(stops) != 1:
        raise SignalError('Нужны тейки в формате «TP 1: 4179.58» и один «стоп 4193.74».')
    remainder = re.sub(target_pattern, '', tail, flags=re.I)
    remainder = re.sub(stop_pattern, '', remainder, flags=re.I)
    if remainder.strip():
        raise SignalError('В сигнале остался нераспознанный текст или неверное число. Оставьте только направление, инструмент, вход, тейки и стоп.')
    numbers = [int(t[1]) for t in targets if t[1]]
    if numbers and (len(numbers) != len(targets) or numbers != list(range(1, len(targets) + 1))):
        raise SignalError('Нумерация тейков должна идти подряд: 1, 2, 3…')
    values = tuple(dec(t[2]) for t in targets)
    side = head[1].upper()
    stop = dec(stops[0])
    if min(lo, stop, *values) <= 0:
        raise SignalError('Все цены должны быть положительными.')
    if len(values) > 20 or len(set(values)) != len(values):
        raise SignalError('Допустимо от 1 до 20 различных тейк-профитов.')
    if side == 'SELL':
        valid = stop > hi and all(t < lo for t in values) and list(values) == sorted(values, reverse=True)
    else:
        valid = stop < lo and all(t > hi for t in values) and list(values) == sorted(values)
    if not valid:
        raise SignalError('Проверьте стоп и тейки: тейки должны идти от ближайшего к дальнему по направлению сделки.')
    return Signal(side, head[2].upper(), lo, hi, values, stop, bool(re.search(r"\bLIMIT\b", head[0], re.I)))


def split_total_lot(total, count, step, minimum, maximum):
    total, step, minimum, maximum = map(dec, (total, step, minimum, maximum))
    if not total.is_finite() or total <= 0 or count < 1:
        raise SignalError('Суммарный общий лот должен быть положительным числом.')
    if step <= 0:
        raise SignalError('Брокер не предоставил допустимый шаг лота.')
    per_order = (total / count / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if per_order < minimum:
        raise SignalError(f'Общего лота {total:f} недостаточно для {count} ордеров: минимум {minimum:f} на каждый.')
    if per_order > maximum:
        raise SignalError(f'Получается {per_order:f} на ордер — больше максимума брокера {maximum:f}.')
    return per_order


def grid(signal: Signal, lot=Decimal('0.01'), tick=None, entry_offset_percent=0) -> tuple[Leg, ...]:
    lot = dec(lot)
    if not lot.is_finite() or lot <= 0:
        raise SignalError('Лот должен быть положительным числом.')
    n = len(signal.targets)
    offset = dec(entry_offset_percent)
    if not offset.is_finite() or not 0 <= offset <= 50:
        raise SignalError('Смещение входов должно быть от 0 до 50%.')
    inset = (signal.high - signal.low) * offset / 100
    low, high = signal.low + inset, signal.high - inset
    start, end = (low, high) if signal.side == 'SELL' else (high, low)
    entries = [(start + end) / 2] * n if not signal.limit or n == 1 or low == high else [start + (end - start) * i / (n - 1) for i in range(n)]
    if tick is not None:
        tick = dec(tick)
        entries = [(e / tick).quantize(Decimal('1'), rounding=ROUND_HALF_UP) * tick for e in entries]
        if any(e < signal.low or e > signal.high for e in entries):
            raise SignalError('После округления по шагу брокера вход выходит за диапазон.')
        if any(p % tick != 0 for p in (*signal.targets, signal.stop)):
            raise SignalError('Стоп или тейк не кратен шагу цены брокера. Исправьте сигнал.')
        if signal.limit and low != high and len(set(entries)) != n:
            raise SignalError('Диапазон слишком узкий для разных входов с шагом цены брокера.')
    return tuple(Leg(e, tp, signal.stop, lot) for e, tp in zip(entries, signal.targets))
