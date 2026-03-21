"""Tests for manual trade merge and futures position grouping."""

from datetime import datetime
from core.positions import build_positions


def _make_trade(symbol, opt_type, expiration, strike, activity_type, date,
                quantity, price, amount, commission=0):
    return {
        'date': date,
        'activity_type': activity_type,
        'symbol': symbol,
        'opt_type': opt_type,
        'expiration': expiration,
        'strike': strike,
        'quantity': quantity,
        'price': price,
        'amount': amount,
        'commission': commission,
    }


def _identity_key(t):
    return (t['symbol'], t['opt_type'], t['expiration'], t['strike'])


class TestFuturesPositionGrouping:
    def test_futures_position_id_uses_fut_sentinel(self):
        trades = [
            _make_trade('/ESH26', None, None, None, 'Bought To Open',
                        datetime(2026, 3, 15), 2, 5200.0, -10400.0),
        ]
        positions = build_positions(trades, {}, _identity_key)
        assert len(positions) == 1
        pid = positions[0]['position_id']
        assert 'FUT' in pid
        assert '/ESH26' in pid
        assert 'None' not in pid

    def test_futures_trades_group_by_symbol(self):
        trades = [
            _make_trade('/ESH26', None, None, None, 'Bought To Open',
                        datetime(2026, 3, 15), 2, 5200.0, -10400.0),
            _make_trade('/ESH26', None, None, None, 'Sold To Close',
                        datetime(2026, 3, 18), 2, 5250.0, 10500.0),
        ]
        positions = build_positions(trades, {}, _identity_key)
        assert len(positions) == 1
        assert positions[0]['status'] == 'Closed'
        assert positions[0]['total_pnl'] == 100.0

    def test_option_position_id_unchanged(self):
        trades = [
            _make_trade('AAPL', 'PUT', '04/17/26', 200.0, 'Sold Short',
                        datetime(2026, 3, 15), 5, 3.50, 1750.0),
        ]
        positions = build_positions(trades, {}, _identity_key)
        pid = positions[0]['position_id']
        assert 'AAPL' in pid
        assert 'PUT' in pid
        assert '200.0' in pid

    def test_futures_with_different_strikes_separate_positions(self):
        """Futures at different entry prices create separate positions."""
        trades = [
            _make_trade('/NQM26', None, None, 21000.0, 'Sold Short',
                        datetime(2026, 2, 26), 3, 21000.0, 63000.0),
            _make_trade('/NQM26', None, None, 21500.0, 'Sold Short',
                        datetime(2026, 3, 5), 2, 21500.0, 43000.0),
        ]
        positions = build_positions(trades, {}, _identity_key)
        assert len(positions) == 2
        strikes = {p['strike'] for p in positions}
        assert strikes == {21000.0, 21500.0}

    def test_futures_position_id_includes_strike(self):
        """Futures position_id includes strike when present."""
        trades = [
            _make_trade('/NQM26', None, None, 21000.0, 'Sold Short',
                        datetime(2026, 2, 26), 3, 21000.0, 63000.0),
        ]
        positions = build_positions(trades, {}, _identity_key)
        pid = positions[0]['position_id']
        assert '21000' in pid
        assert 'FUT' in pid

    def test_mixed_options_and_futures(self):
        trades = [
            _make_trade('AAPL', 'PUT', '04/17/26', 200.0, 'Sold Short',
                        datetime(2026, 3, 15), 5, 3.50, 1750.0),
            _make_trade('/ESH26', None, None, None, 'Bought To Open',
                        datetime(2026, 3, 15), 2, 5200.0, -10400.0),
        ]
        positions = build_positions(trades, {}, _identity_key)
        assert len(positions) == 2
        symbols = {p['symbol'] for p in positions}
        assert symbols == {'AAPL', '/ESH26'}


class TestMergeManualTrades:
    def test_manual_trades_merge_with_csv(self, tmp_path, monkeypatch):
        from core.db import add_trade, get_all_trades

        db_path = str(tmp_path / 'test_trades.db')
        monkeypatch.setattr('core.db.get_db_path', lambda: db_path)

        add_trade({
            'date': datetime(2026, 3, 10),
            'activity_type': 'Sold Short',
            'symbol': 'TSLA',
            'opt_type': 'PUT',
            'expiration': '04/17/26',
            'strike': 150.0,
            'quantity': 3,
            'price': 2.00,
            'amount': 600.0,
            'commission': 1.50,
            'instrument_type': 'option',
        })

        manual_trades = get_all_trades()
        csv_trades = [
            _make_trade('AAPL', 'PUT', '04/17/26', 200.0, 'Sold Short',
                        datetime(2026, 3, 15), 5, 3.50, 1750.0),
        ]

        combined = csv_trades + manual_trades
        positions = build_positions(combined, {}, _identity_key)
        assert len(positions) == 2
        symbols = {p['symbol'] for p in positions}
        assert symbols == {'AAPL', 'TSLA'}

    def test_manual_future_merges_with_options(self, tmp_path, monkeypatch):
        from core.db import add_trade, get_all_trades

        db_path = str(tmp_path / 'test_trades.db')
        monkeypatch.setattr('core.db.get_db_path', lambda: db_path)

        add_trade({
            'date': datetime(2026, 3, 15),
            'activity_type': 'Bought To Open',
            'symbol': '/ESH26',
            'opt_type': None,
            'expiration': None,
            'strike': None,
            'quantity': 2,
            'price': 5200.0,
            'amount': -10400.0,
            'commission': 4.50,
            'instrument_type': 'future',
        })

        manual_trades = get_all_trades()
        option_trades = [
            _make_trade('AAPL', 'PUT', '04/17/26', 200.0, 'Sold Short',
                        datetime(2026, 3, 15), 5, 3.50, 1750.0),
        ]

        combined = option_trades + manual_trades
        positions = build_positions(combined, {}, _identity_key)
        assert len(positions) == 2
        types = {p['symbol']: p['opt_type'] for p in positions}
        assert types['AAPL'] == 'PUT'
        assert types['/ESH26'] is None

    def test_manual_trades_filtered_by_date_range(self, tmp_path, monkeypatch):
        from core.db import add_trade, get_all_trades

        db_path = str(tmp_path / 'test_trades.db')
        monkeypatch.setattr('core.db.get_db_path', lambda: db_path)

        add_trade({
            'date': datetime(2026, 1, 5),
            'activity_type': 'Sold Short',
            'symbol': 'OLD',
            'opt_type': 'PUT',
            'expiration': '02/01/26',
            'strike': 100.0,
            'quantity': 1,
            'price': 1.00,
            'amount': 100.0,
            'instrument_type': 'option',
        })
        add_trade({
            'date': datetime(2026, 3, 15),
            'activity_type': 'Sold Short',
            'symbol': 'NEW',
            'opt_type': 'PUT',
            'expiration': '04/17/26',
            'strike': 200.0,
            'quantity': 1,
            'price': 2.00,
            'amount': 200.0,
            'instrument_type': 'option',
        })

        all_manual = get_all_trades()
        start = datetime(2026, 3, 1)
        end = datetime(2026, 3, 31)
        filtered = [t for t in all_manual if start <= t['date'] <= end]
        assert len(filtered) == 1
        assert filtered[0]['symbol'] == 'NEW'


class TestActivityTypeDisambiguation:
    """Test that ambiguous E-Trade activity types are correctly mapped."""

    def test_csv_sold_opening_becomes_sold_short(self):
        from core.parser import parse_csv_content
        csv = (
            'Activity/Trade Date,Settlement Date,Account,Activity Type,Description,'
            'Symbol/CUSIP#,Transaction Type,Quantity,Price,Amount,Commission\n'
            '03/09/26,03/11/26,ACCT,Sold,PUT TQQQ 04/17/26 50.000 OPENING TRANSACTION,'
            'TQQQ260417P00050000,Option,40.0,5.46,21840.0,0.52\n'
        )
        trades, _, _, _ = parse_csv_content(csv)
        assert len(trades) == 1
        assert trades[0]['activity_type'] == 'Sold Short'

    def test_csv_sold_closing_becomes_sold_to_close(self):
        from core.parser import parse_csv_content
        csv = (
            'Activity/Trade Date,Settlement Date,Account,Activity Type,Description,'
            'Symbol/CUSIP#,Transaction Type,Quantity,Price,Amount,Commission\n'
            '03/09/26,03/11/26,ACCT,Sold,PUT TQQQ 04/17/26 50.000 CLOSING TRANSACTION,'
            'TQQQ260417P00050000,Option,40.0,5.46,21840.0,0.52\n'
        )
        trades, _, _, _ = parse_csv_content(csv)
        assert len(trades) == 1
        assert trades[0]['activity_type'] == 'Sold To Close'

    def test_csv_bought_closing_becomes_bought_to_cover(self):
        from core.parser import parse_csv_content
        csv = (
            'Activity/Trade Date,Settlement Date,Account,Activity Type,Description,'
            'Symbol/CUSIP#,Transaction Type,Quantity,Price,Amount,Commission\n'
            '03/09/26,03/11/26,ACCT,Bought,PUT TQQQ 04/17/26 50.000 CLOSING TRANSACTION,'
            'TQQQ260417P00050000,Option,40.0,1.20,4800.0,0.52\n'
        )
        trades, _, _, _ = parse_csv_content(csv)
        assert len(trades) == 1
        assert trades[0]['activity_type'] == 'Bought To Cover'

    def test_csv_bought_opening_becomes_bought_to_open(self):
        from core.parser import parse_csv_content
        csv = (
            'Activity/Trade Date,Settlement Date,Account,Activity Type,Description,'
            'Symbol/CUSIP#,Transaction Type,Quantity,Price,Amount,Commission\n'
            '03/09/26,03/11/26,ACCT,Bought,CALL AAPL 04/17/26 200.000 OPENING TRANSACTION,'
            'AAPL260417C00200000,Option,10.0,3.50,3500.0,0.65\n'
        )
        trades, _, _, _ = parse_csv_content(csv)
        assert len(trades) == 1
        assert trades[0]['activity_type'] == 'Bought To Open'

    def test_api_sold_opening_becomes_sold_short(self):
        from etrade.models import _map_transaction_type
        result = _map_transaction_type('Sold', 'PUT TQQQ 04/17/26 50.000 OPENING TRANSACTION')
        assert result == 'Sold Short'

    def test_api_sold_closing_becomes_sold_to_close(self):
        from etrade.models import _map_transaction_type
        result = _map_transaction_type('Sold', 'PUT TQQQ 04/17/26 50.000 CLOSING TRANSACTION')
        assert result == 'Sold To Close'

    def test_api_bought_closing_becomes_bought_to_cover(self):
        from etrade.models import _map_transaction_type
        result = _map_transaction_type('Bought', 'CALL AAPL 04/17/26 200.000 CLOSING TRANSACTION')
        assert result == 'Bought To Cover'

    def test_api_bought_opening_stays_bought_to_open(self):
        from etrade.models import _map_transaction_type
        result = _map_transaction_type('Bought', 'CALL AAPL 04/17/26 200.000 OPENING TRANSACTION')
        assert result == 'Bought To Open'
