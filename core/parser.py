"""CSV parsing and trade normalization for E-Trade and Fidelity transaction history."""

import csv
import io
import re
from datetime import datetime

# Matches Fidelity option symbols: -UNDERLYING{YYMMDD}{C|P}{STRIKE}
# e.g. -NBIS260327C105, -TNA260320C46.5, -GGLL260417C97.15
_FIDELITY_OPT_RE = re.compile(r'^-([A-Z]+)(\d{6})([CP])([\d.]+)$')


def _detect_broker(rows):
    """Return 'fidelity', 'etrade', or None based on the CSV header sentinel."""
    for row in rows[:5]:
        if row and row[0].strip() == 'Run Date':
            return 'fidelity'
        if row and row[0].strip() == 'Activity/Trade Date':
            return 'etrade'
    return None


def _parse_rows(rows):
    """Parse E-Trade CSV rows into normalized trade dicts."""
    header_idx = None
    for i, row in enumerate(rows):
        if row and row[0].strip() == 'Activity/Trade Date':
            header_idx = i
            break

    if header_idx is None:
        return []

    trades = []
    for i in range(header_idx + 1, len(rows)):
        row = rows[i]
        if not row or len(row) < 11 or not row[0].strip():
            continue

        date_str = row[0].strip()
        try:
            trade_date = datetime.strptime(date_str, '%m/%d/%y')
        except ValueError:
            continue

        activity_type = row[3].strip()
        description = row[4].strip()

        # E-Trade sometimes uses short activity names — disambiguate via description
        if activity_type == 'Sold':
            activity_type = 'Sold Short' if 'OPENING' in description.upper() else 'Sold To Close'
        elif activity_type == 'Bought':
            activity_type = 'Bought To Open' if 'OPENING' in description.upper() else 'Bought To Cover'

        desc_clean = description.split(' ADJUST')[0].strip()
        match = re.match(r'(CALL|PUT)\s+(\w+)\s+(\d{2}/\d{2}/\d{2})\s+([\d.]+)', desc_clean)
        if not match:
            continue

        opt_type = match.group(1)
        symbol = match.group(2)
        expiration = match.group(3)
        strike = float(match.group(4))

        quantity = int(float(row[7].strip())) if row[7].strip() and row[7].strip() != '--' else 0
        price = float(row[8].strip()) if row[8].strip() and row[8].strip() != '--' else 0.0
        amount = float(row[9].strip()) if row[9].strip() and row[9].strip() != '--' else 0.0
        commission = float(row[10].strip()) if row[10].strip() and row[10].strip() != '--' else 0.0

        trades.append({
            'date': trade_date,
            'date_str': date_str,
            'activity_type': activity_type,
            'symbol': symbol,
            'opt_type': opt_type,
            'expiration': expiration,
            'strike': strike,
            'quantity': quantity,
            'price': price,
            'amount': amount,
            'commission': commission,
            'account': '',
        })

    return trades


def _parse_fidelity_rows(rows):
    """Parse Fidelity CSV rows into normalized trade dicts.

    Fidelity columns (header: Run Date):
      0: Run Date, 1: Account, 2: Account Number, 3: Action, 4: Symbol,
      5: Description, 6: Type, 7: Price ($), 8: Quantity, 9: Commission ($),
      10: Fees ($), 11: Accrued Interest ($), 12: Amount ($), 13: Settlement Date

    Option symbols use the format: -UNDERLYING{YYMMDD}{C|P}{STRIKE}
    e.g. -NBIS260327C105, -TNA260320C46.5

    Commission and Fees are combined to match the single 'commission' field in
    the normalized trade dict schema.
    """
    header_idx = None
    for i, row in enumerate(rows):
        if row and row[0].strip() == 'Run Date':
            header_idx = i
            break

    if header_idx is None:
        return []

    def _parse_float(s):
        s = s.strip() if s else ''
        return float(s) if s else 0.0

    trades = []
    for i in range(header_idx + 1, len(rows)):
        row = rows[i]
        if not row or len(row) < 13 or not row[0].strip():
            continue

        date_str = row[0].strip()
        try:
            trade_date = datetime.strptime(date_str, '%m/%d/%Y')
        except ValueError:
            continue

        # Symbol column must match the option format — skip all non-option rows
        symbol_raw = row[4].strip()
        m = _FIDELITY_OPT_RE.match(symbol_raw)
        if not m:
            continue

        underlying = m.group(1)
        exp_yymmdd = m.group(2)
        cp = m.group(3)
        strike = float(m.group(4))

        # Convert YYMMDD → MM/DD/YY to match the existing expiration format
        exp_date = datetime.strptime(exp_yymmdd, '%y%m%d')
        expiration = exp_date.strftime('%m/%d/%y')

        opt_type = 'CALL' if cp == 'C' else 'PUT'

        # Map Fidelity action string to canonical activity_type
        action = row[3].strip().upper()
        if 'YOU SOLD' in action and 'OPENING' in action:
            activity_type = 'Sold Short'
        elif 'YOU SOLD' in action and 'CLOSING' in action:
            activity_type = 'Sold To Close'
        elif 'YOU BOUGHT' in action and 'OPENING' in action:
            activity_type = 'Bought To Open'
        elif 'YOU BOUGHT' in action and 'CLOSING' in action:
            activity_type = 'Bought To Cover'
        elif action.startswith('ASSIGNED'):
            activity_type = 'Option Assigned'
        elif 'EXPIRED' in action:
            activity_type = 'Option Expired'
        else:
            continue

        account_name = row[1].strip()
        price = _parse_float(row[7])
        quantity = abs(int(float(row[8]))) if row[8].strip() else 0
        # Fidelity reports commission and regulatory fees separately — combine
        commission = _parse_float(row[9]) + _parse_float(row[10])
        amount = _parse_float(row[12])

        trades.append({
            'date': trade_date,
            'date_str': date_str,
            'activity_type': activity_type,
            'symbol': underlying,
            'opt_type': opt_type,
            'expiration': expiration,
            'strike': strike,
            'quantity': quantity,
            'price': price,
            'amount': amount,
            'commission': commission,
            'account': account_name,
        })

    return trades


def _build_split_map(trades):
    """Build mapping from pre-split to post-split contract keys using MISC entries."""
    misc_old = {}
    misc_new = {}

    for t in trades:
        if t['activity_type'] == 'MISC':
            key = (t['symbol'], t['opt_type'], t['expiration'])
            if t['quantity'] > 0:
                misc_old[key] = (t['symbol'], t['opt_type'], t['expiration'], t['strike'])
            elif t['quantity'] < 0:
                misc_new[key] = (t['symbol'], t['opt_type'], t['expiration'], t['strike'])

    split_map = {}
    for key in misc_old:
        if key in misc_new:
            split_map[misc_old[key]] = misc_new[key]

    return split_map


def normalize_trades(trades):
    """Apply split mapping and filter out MISC entries.

    Returns (real_trades, split_map, get_contract_key_fn).
    """
    split_map = _build_split_map(trades)

    def get_contract_key(t):
        key = (t['symbol'], t['opt_type'], t['expiration'], t['strike'])
        return split_map.get(key, key)

    real_trades = [t for t in trades if t['activity_type'] != 'MISC']
    return real_trades, split_map, get_contract_key


def parse_csv(filepath):
    """Parse a CSV file (E-Trade or Fidelity) from disk.

    Returns (real_trades, split_map, get_contract_key, broker).
    broker is 'etrade', 'fidelity', or None.
    """
    with open(filepath, 'r') as f:
        reader = csv.reader(f)
        rows = list(reader)
    broker = _detect_broker(rows)
    trades = _parse_fidelity_rows(rows) if broker == 'fidelity' else _parse_rows(rows)
    return (*normalize_trades(trades), broker)


def parse_csv_content(content_string):
    """Parse a CSV (E-Trade or Fidelity) from an in-memory string.

    Returns (real_trades, split_map, get_contract_key, broker).
    broker is 'etrade', 'fidelity', or None.
    """
    reader = csv.reader(io.StringIO(content_string))
    rows = list(reader)
    broker = _detect_broker(rows)
    trades = _parse_fidelity_rows(rows) if broker == 'fidelity' else _parse_rows(rows)
    return (*normalize_trades(trades), broker)
