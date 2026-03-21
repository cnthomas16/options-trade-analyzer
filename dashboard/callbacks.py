"""Dash callbacks for interactivity — data loading, filtering, tab updates."""

import base64
import json
from datetime import datetime

from etrade.chunked_fetch import chunk_date_range, fetch_all_chunks

import dash_bootstrap_components as dbc
from dash import html, callback, Input, Output, State, no_update, ctx, ALL

from core.parser import parse_csv_content, normalize_trades
from core.positions import build_positions, compute_summary
from core.rolls import detect_rolls
from core.monthly import build_monthly_data
from dashboard.components import kpi_card, format_currency, KPI_ICONS
from dashboard.charts import monthly_income_chart, pnl_by_symbol_chart, pl_heatmap_chart, greeks_chart
from core.db import get_all_trades as get_manual_trades


def _serialize_trades(real_trades):
    """Convert trades to JSON-safe format (datetimes -> strings)."""
    out = []
    for t in real_trades:
        d = dict(t)
        d['date'] = t['date'].isoformat()
        out.append(d)
    return out


def _deserialize_trades(json_trades):
    """Restore trades from JSON (strings -> datetimes)."""
    out = []
    for t in json_trades:
        d = dict(t)
        d['date'] = datetime.fromisoformat(t['date'])
        out.append(d)
    return out


def _merge_manual_trades(base_trades, start_date=None, end_date=None):
    """Merge manual trades from SQLite with base trades, filtered by date range."""
    from datetime import datetime as dt
    manual = get_manual_trades()
    if start_date and end_date:
        if isinstance(start_date, str):
            start_date = dt.fromisoformat(start_date)
        if isinstance(end_date, str):
            end_date = dt.fromisoformat(end_date)
        manual = [t for t in manual
                  if start_date.date() <= t['date'].date() <= end_date.date()]
    return base_trades + manual


def _build_and_serialize_positions(trades, split_map=None, get_key=None):
    """Build positions from trades and serialize for trades-store.

    Shared helper to avoid duplicating the position serialization block.
    Returns positions_data list (JSON-safe).
    """
    if split_map is None:
        split_map = {}
    if get_key is None:
        get_key = lambda t: (t['symbol'], t['opt_type'], t['expiration'], t['strike'])

    pos_list = build_positions(trades, split_map, get_key)
    chains, standalone, chain_label_map = detect_rolls(pos_list)

    positions_data = []
    for p in pos_list:
        pd = {k: v for k, v in p.items() if k not in ('close_trades', 'open_trades', 'contract_key')}
        pd['open_date'] = p['open_date'].isoformat() if p['open_date'] else None
        pd['close_date'] = p['close_date'].isoformat() if p['close_date'] else None
        pd['contract_key'] = list(p['contract_key'])
        chain_info = chain_label_map.get(p['position_id'])
        if chain_info:
            pd['roll_chain'] = chain_info['label']
            pd['chain_index'] = chain_info['chain_index']
            pd['chain_leg'] = chain_info['chain_leg']
        else:
            pd['roll_chain'] = ''
            pd['chain_index'] = -1
            pd['chain_leg'] = -1
        positions_data.append(pd)

    return positions_data


def register_callbacks(app):
    """Register all Dash callbacks on the app."""

    # ── Auto-detect saved token on page load ──
    @app.callback(
        Output('session-store', 'data', allow_duplicate=True),
        Output('auth-status', 'children', allow_duplicate=True),
        Output('auth-message', 'children', allow_duplicate=True),
        Output('api-auth-collapse', 'is_open', allow_duplicate=True),
        Input('session-store', 'data'),
        prevent_initial_call='initial_duplicate',
    )
    def check_saved_session(session_data):
        """On load, verify any saved keyring token is still valid."""
        try:
            from etrade.auth import get_session
            session, error = get_session()
            if session:
                return (
                    {'authenticated': True},
                    dbc.Badge('Connected', color='success', className='ms-2'),
                    html.Span('Using saved token — no re-authentication needed.',
                              style={'color': '#00ff88', 'fontSize': '0.8rem'}),
                    False,
                )
        except Exception:
            pass
        return no_update, no_update, no_update, no_update

    # ── Toggle data source UI ──
    @app.callback(
        Output('upload-collapse', 'is_open'),
        Output('api-auth-collapse', 'is_open'),
        Output('account-selector', 'disabled'),
        Input('data-source-toggle', 'value'),
    )
    def toggle_data_source(source):
        if source == 'csv':
            return True, False, True
        return False, True, False

    # ── CSV Upload -> parse trades ──
    @app.callback(
        Output('trades-store', 'data'),
        Output('fetch-status', 'children', allow_duplicate=True),
        Output('fetch-log-store', 'data', allow_duplicate=True),
        Output('csv-account-filter', 'options'),
        Output('csv-account-filter', 'value'),
        Output('account-filter-collapse', 'is_open'),
        Input('csv-upload', 'contents'),
        State('csv-upload', 'filename'),
        State('date-range-picker', 'start_date'),
        State('date-range-picker', 'end_date'),
        prevent_initial_call=True,
    )
    def on_csv_upload(contents, filename, start_date, end_date):
        if not contents:
            return no_update, no_update, no_update, no_update, no_update, no_update
        content_type, content_string = contents.split(',')
        decoded = base64.b64decode(content_string).decode('utf-8')
        real_trades, split_map, get_key, broker = parse_csv_content(decoded)
        combined = _merge_manual_trades(real_trades, start_date, end_date)
        positions_data = _build_and_serialize_positions(combined, split_map, get_key)

        # Build account filter options for Fidelity multi-account CSVs
        account_options = []
        show_filter = False
        if broker == 'fidelity':
            accounts = sorted(set(
                t['account'] for t in real_trades if t.get('account')
            ))
            if len(accounts) > 1:
                account_options = [{'label': a, 'value': a} for a in accounts]
                show_filter = True

        return (
            {
                'trades': _serialize_trades(combined),
                'positions': positions_data,
                'filename': filename,
                'broker': broker,
            },
            None,
            {'status': 'idle'},
            account_options,
            None,   # reset filter to "All accounts"
            show_filter,
        )

    # ── Clear all trade data ──
    @app.callback(
        Output('trades-store', 'data', allow_duplicate=True),
        Output('fetch-log-store', 'data', allow_duplicate=True),
        Output('analyzer-store', 'data', allow_duplicate=True),
        Output('csv-account-filter', 'value', allow_duplicate=True),
        Output('account-filter-collapse', 'is_open', allow_duplicate=True),
        Input('clear-btn', 'n_clicks'),
        prevent_initial_call=True,
    )
    def on_clear_click(n_clicks):
        if not n_clicks:
            return no_update, no_update, no_update, no_update, no_update
        return {}, {'status': 'idle'}, None, None, False

    # ── Account filter (Fidelity multi-account CSV) ──
    @app.callback(
        Output('trades-store', 'data', allow_duplicate=True),
        Input('csv-account-filter', 'value'),
        State('trades-store', 'data'),
        State('date-range-picker', 'start_date'),
        State('date-range-picker', 'end_date'),
        prevent_initial_call=True,
    )
    def on_account_filter_change(selected_accounts, current_data, start_date, end_date):
        """Rebuild positions when the Fidelity account filter changes.

        All raw trades are kept in trades-store; only position building is
        filtered. Fidelity CSVs have no split-map entries, so the identity
        key function is safe to use here.
        """
        if not current_data or not current_data.get('trades'):
            return no_update

        all_trades = _deserialize_trades(current_data['trades'])

        if selected_accounts:
            # Manual trades (no account field) are always included
            filtered = [
                t for t in all_trades
                if t.get('source') == 'manual' or t.get('account', '') in selected_accounts
            ]
        else:
            filtered = all_trades

        positions_data = _build_and_serialize_positions(filtered)

        return {
            **current_data,
            'positions': positions_data,
        }

    # ── Load manual trades on page load (if no CSV/API data) ──
    @app.callback(
        Output('trades-store', 'data', allow_duplicate=True),
        Input('trades-store', 'modified_timestamp'),
        State('trades-store', 'data'),
        State('date-range-picker', 'start_date'),
        State('date-range-picker', 'end_date'),
        prevent_initial_call='initial_duplicate',
    )
    def load_manual_on_start(ts, current_data, start_date, end_date):
        """On page load, if trades-store is empty, load manual trades from SQLite."""
        if current_data and current_data.get('trades'):
            return no_update

        manual = _merge_manual_trades([], start_date, end_date)
        if not manual:
            return no_update

        positions_data = _build_and_serialize_positions(manual)

        return {
            'trades': _serialize_trades(manual),
            'positions': positions_data,
            'filename': 'Manual Trades',
        }

    # ── Trade Modal: Open/Close ──
    @app.callback(
        Output('trade-modal', 'is_open'),
        Input('open-trade-modal-btn', 'n_clicks'),
        State('trade-modal', 'is_open'),
        prevent_initial_call=True,
    )
    def toggle_trade_modal(n_clicks, is_open):
        if n_clicks:
            return not is_open
        return is_open

    # ── Trade Modal: Tab Toggle ──
    @app.callback(
        Output('modal-add-view', 'style'),
        Output('modal-manage-view', 'style'),
        Input('trade-modal-tabs', 'active_tab'),
    )
    def toggle_modal_tab(active_tab):
        if active_tab == 'modal-tab-manage':
            return {'display': 'none'}, {'display': 'block'}
        return {'display': 'block'}, {'display': 'none'}

    # ── Trade Modal: Instrument Toggle ──
    @app.callback(
        Output('option-only-fields-wrapper', 'style'),
        Output('trade-amount', 'placeholder'),
        Output('trade-strike-label', 'children'),
        Input('trade-instrument-toggle', 'value'),
    )
    def toggle_instrument_fields(instrument):
        if instrument == 'future':
            return {'display': 'none'}, 'Enter amount', 'Entry Price'
        if instrument == 'futures_option':
            return {'display': 'block'}, 'Enter amount', 'Strike Price'
        return {'display': 'block'}, 'Auto', 'Strike Price'

    # ── Trade Modal: Amount Auto-calc for Options ──
    @app.callback(
        Output('trade-amount', 'value'),
        Input('trade-quantity', 'value'),
        Input('trade-price', 'value'),
        Input('trade-activity-type', 'value'),
        State('trade-instrument-toggle', 'value'),
        State('trade-amount', 'value'),
        prevent_initial_call=True,
    )
    def auto_calc_amount(qty, price, activity_type, instrument, current_amount):
        if instrument in ('future', 'futures_option'):
            return no_update
        if qty and price and activity_type:
            raw = abs(qty) * abs(price) * 100
            # Positive for sells, negative for buys
            if activity_type in ('Sold Short', 'Sold To Close'):
                return round(raw, 2)
            else:
                return round(-raw, 2)
        return no_update

    # ── Trade Modal: Save Trade ──
    @app.callback(
        Output('trade-form-feedback', 'children'),
        Output('manual-trades-refresh', 'data'),
        Output('trade-edit-id', 'data', allow_duplicate=True),
        Output('trade-instrument-toggle', 'value', allow_duplicate=True),
        Output('trade-symbol', 'value'),
        Output('trade-activity-type', 'value'),
        Output('trade-opt-type', 'value'),
        Output('trade-strike', 'value'),
        Output('trade-quantity', 'value'),
        Output('trade-price', 'value'),
        Output('trade-amount', 'value', allow_duplicate=True),
        Output('trade-commission', 'value'),
        Input('save-trade-btn', 'n_clicks'),
        State('trade-edit-id', 'data'),
        State('trade-instrument-toggle', 'value'),
        State('trade-date-picker', 'date'),
        State('trade-activity-type', 'value'),
        State('trade-symbol', 'value'),
        State('trade-opt-type', 'value'),
        State('trade-strike', 'value'),
        State('trade-expiration-picker', 'date'),
        State('trade-quantity', 'value'),
        State('trade-price', 'value'),
        State('trade-amount', 'value'),
        State('trade-commission', 'value'),
        prevent_initial_call=True,
    )
    def save_trade(n_clicks, edit_id, instrument, trade_date, activity_type,
                   symbol, opt_type, strike, expiration, quantity, price, amount,
                   commission):
        if not n_clicks:
            return no_update, no_update, no_update, *([no_update] * 9)

        # Validate required fields
        errors = []
        if not trade_date:
            errors.append('Trade date is required')
        if not activity_type:
            errors.append('Activity type is required')
        if not symbol:
            errors.append('Symbol is required')
        if not quantity or quantity <= 0:
            errors.append('Contracts must be positive')
        if price is None:
            errors.append('Price is required')
        if amount is None:
            errors.append('Amount is required')
        if instrument in ('option', 'futures_option'):
            if not opt_type:
                errors.append('Option type is required')
            if not strike:
                errors.append('Strike is required')
            if not expiration:
                errors.append('Expiration is required')

        if errors:
            feedback = html.Div('; '.join(errors), className='trade-toast-error')
            return feedback, no_update, no_update, *([no_update] * 9)

        from core.db import add_trade, update_trade

        # Format expiration as MM/DD/YY for options and futures options
        has_option_fields = instrument in ('option', 'futures_option')
        exp_str = None
        if has_option_fields and expiration:
            exp_dt = datetime.fromisoformat(expiration) if isinstance(expiration, str) else expiration
            exp_str = exp_dt.strftime('%m/%d/%y')

        trade_dict = {
            'date': datetime.fromisoformat(trade_date) if isinstance(trade_date, str) else trade_date,
            'activity_type': activity_type,
            'symbol': symbol.upper().strip(),
            'opt_type': opt_type if has_option_fields else None,
            'expiration': exp_str if has_option_fields else None,
            'strike': float(strike) if strike else None,
            'quantity': int(quantity),
            'price': float(price),
            'amount': float(amount),
            'commission': float(commission) if commission else 0,
            'instrument_type': instrument,
        }

        try:
            if edit_id:
                update_trade(edit_id, trade_dict)
                msg = 'Trade updated'
            else:
                add_trade(trade_dict)
                msg = 'Trade added'
        except Exception as e:
            feedback = html.Div(f'Error: {e}', className='trade-toast-error')
            return feedback, no_update, no_update, *([no_update] * 9)

        feedback = html.Div(msg, className='trade-toast-success')
        # Clear form + bump refresh counter (12 values total)
        return feedback, (edit_id or 0) + 1, None, 'option', '', None, None, None, None, None, None, 0

    # ── Rebuild trades-store after manual trade changes ──
    @app.callback(
        Output('trades-store', 'data', allow_duplicate=True),
        Input('manual-trades-refresh', 'data'),
        State('trades-store', 'data'),
        State('date-range-picker', 'start_date'),
        State('date-range-picker', 'end_date'),
        prevent_initial_call=True,
    )
    def rebuild_after_manual_change(refresh_counter, current_data, start_date, end_date):
        if not refresh_counter:
            return no_update

        base_trades = []
        filename = 'Manual Trades'
        if current_data and current_data.get('trades'):
            base_trades = _deserialize_trades(current_data['trades'])
            # Filter out old manual trades — they'll be re-read from SQLite
            base_trades = [t for t in base_trades if t.get('source') != 'manual']
            filename = current_data.get('filename', filename)

        combined = _merge_manual_trades(base_trades, start_date, end_date)
        if not combined:
            return no_update

        positions_data = _build_and_serialize_positions(combined)

        return {
            'trades': _serialize_trades(combined),
            'positions': positions_data,
            'filename': filename,
        }

    # ── Trade Modal: Populate Manage List ──
    @app.callback(
        Output('manage-trades-list', 'children'),
        Output('manage-trades-summary', 'children'),
        Output('manage-trades-tab-label', 'label'),
        Input('trade-modal-tabs', 'active_tab'),
        Input('manual-trades-refresh', 'data'),
        Input('manage-search', 'value'),
        Input('manage-type-filter', 'value'),
        Input('pending-delete-store', 'data'),
    )
    def populate_manage_list(active_tab, refresh, search, type_filter, pending_delete):
        trades = get_manual_trades()

        # Apply filters
        if search:
            search_upper = search.upper().strip()
            trades = [t for t in trades if search_upper in t['symbol'].upper()]
        if type_filter and type_filter != 'all':
            trades = [t for t in trades if t.get('instrument_type', 'option') == type_filter]

        # Sort by most recent first
        trades.sort(key=lambda t: t['date'], reverse=True)

        tab_label = f"Manage Trades ({len(trades)})"

        if not trades:
            return html.Div('No manual trades yet.', className='text-secondary p-3'), '', tab_label

        rows = []
        for t in trades:
            tid = t['trade_id']
            inst = t.get('instrument_type', 'option')
            badge_cls = 'instrument-badge instrument-badge--opt' if inst == 'option' else 'instrument-badge instrument-badge--fut'
            badge_text = 'OPT' if inst == 'option' else 'FUT'

            if inst == 'option':
                details = f"{t.get('opt_type', '')} {t.get('strike', '')} {t.get('expiration', '')}"
            else:
                details = t['activity_type']

            amount_color = '#00ff88' if t['amount'] >= 0 else '#ff6b6b'
            date_str = t['date'].strftime('%m/%d/%y') if hasattr(t['date'], 'strftime') else str(t['date'])

            # Inline delete confirmation
            if pending_delete == tid:
                actions = html.Div([
                    html.Span('Delete this trade?', style={'color': '#ff6b6b'}),
                    dbc.Button('Yes', id={'type': 'confirm-delete-btn', 'index': tid},
                               size='sm', color='danger', className='ms-2'),
                    dbc.Button('No', id={'type': 'cancel-delete-btn', 'index': tid},
                               size='sm', outline=True, color='secondary', className='ms-1'),
                ], className='delete-confirm')
            else:
                actions = html.Div([
                    dbc.Button('Edit', id={'type': 'edit-trade-btn', 'index': tid},
                               size='sm', outline=True, color='info'),
                    dbc.Button('Delete', id={'type': 'delete-trade-btn', 'index': tid},
                               size='sm', outline=True, color='danger'),
                ], className='trade-actions')

            row = html.Div([
                html.Div([
                    html.Span(badge_text, className=badge_cls),
                    html.Div([
                        html.Span(t['symbol'], style={'fontWeight': '600', 'color': '#e6edf3',
                                                       'fontFamily': 'IBM Plex Mono'}),
                        html.Span(f' \u00b7 {details} \u00b7 {date_str}',
                                  className='trade-details'),
                    ]),
                ], className='trade-info'),
                html.Span(f"${t['amount']:,.2f}",
                          className='trade-amount',
                          style={'color': amount_color}),
                actions,
            ], className='manual-trade-row')
            rows.append(row)

        # Summary footer
        opt_trades = [t for t in trades if t.get('instrument_type', 'option') == 'option']
        fut_trades = [t for t in trades if t.get('instrument_type', 'option') == 'future']
        opt_pnl = sum(t['amount'] for t in opt_trades)
        fut_pnl = sum(t['amount'] for t in fut_trades)
        summary = html.Div([
            html.Span(f"Total: {len(trades)} trades"),
            html.Span(f"Options P&L: ${opt_pnl:,.2f}",
                      style={'color': '#00ff88' if opt_pnl >= 0 else '#ff6b6b'}),
            html.Span(f"Futures P&L: ${fut_pnl:,.2f}",
                      style={'color': '#00ff88' if fut_pnl >= 0 else '#ff6b6b'}),
        ], className='manage-summary')

        return rows, summary, tab_label

    # ── Trade Modal: Edit button → populate form ──
    @app.callback(
        Output('trade-modal-tabs', 'active_tab', allow_duplicate=True),
        Output('trade-edit-id', 'data'),
        Output('trade-instrument-toggle', 'value', allow_duplicate=True),
        Output('trade-date-picker', 'date'),
        Output('trade-activity-type', 'value', allow_duplicate=True),
        Output('trade-symbol', 'value', allow_duplicate=True),
        Output('trade-opt-type', 'value', allow_duplicate=True),
        Output('trade-strike', 'value', allow_duplicate=True),
        Output('trade-expiration-picker', 'date'),
        Output('trade-quantity', 'value', allow_duplicate=True),
        Output('trade-price', 'value', allow_duplicate=True),
        Output('trade-amount', 'value', allow_duplicate=True),
        Output('trade-commission', 'value', allow_duplicate=True),
        Input({'type': 'edit-trade-btn', 'index': ALL}, 'n_clicks'),
        prevent_initial_call=True,
    )
    def on_edit_trade(n_clicks_list):
        if not any(n_clicks_list):
            return (no_update,) * 13

        triggered = ctx.triggered_id
        if not triggered:
            return (no_update,) * 13

        trade_id = triggered['index']
        from core.db import get_trade
        t = get_trade(trade_id)
        if not t:
            return (no_update,) * 13

        inst = t.get('instrument_type', 'option')
        exp_date = None
        if t.get('expiration'):
            try:
                exp_date = datetime.strptime(t['expiration'], '%m/%d/%y').date().isoformat()
            except ValueError:
                exp_date = t['expiration']

        trade_date = t['date'].date().isoformat() if hasattr(t['date'], 'date') else t['date']

        return (
            'modal-tab-add',       # switch to Add tab
            trade_id,              # edit mode
            inst,                  # instrument toggle
            trade_date,            # date
            t['activity_type'],    # activity type
            t['symbol'],           # symbol
            t.get('opt_type'),     # opt type
            t.get('strike'),       # strike
            exp_date,              # expiration
            t.get('quantity'),     # qty
            t.get('price'),        # price
            t.get('amount'),       # amount
            t.get('commission', 0),  # commission
        )

    # ── Trade Modal: Delete button → set pending ──
    @app.callback(
        Output('pending-delete-store', 'data'),
        Input({'type': 'delete-trade-btn', 'index': ALL}, 'n_clicks'),
        prevent_initial_call=True,
    )
    def on_delete_click(n_clicks_list):
        if not any(n_clicks_list):
            return no_update
        triggered = ctx.triggered_id
        if not triggered:
            return no_update
        return triggered['index']

    # ── Trade Modal: Confirm delete ──
    @app.callback(
        Output('manual-trades-refresh', 'data', allow_duplicate=True),
        Output('pending-delete-store', 'data', allow_duplicate=True),
        Input({'type': 'confirm-delete-btn', 'index': ALL}, 'n_clicks'),
        prevent_initial_call=True,
    )
    def on_confirm_delete(n_clicks_list):
        if not any(n_clicks_list):
            return no_update, no_update
        triggered = ctx.triggered_id
        if not triggered:
            return no_update, no_update
        trade_id = triggered['index']
        from core.db import delete_trade
        delete_trade(trade_id)
        return datetime.now().timestamp(), None

    # ── Trade Modal: Cancel delete ──
    @app.callback(
        Output('pending-delete-store', 'data', allow_duplicate=True),
        Input({'type': 'cancel-delete-btn', 'index': ALL}, 'n_clicks'),
        prevent_initial_call=True,
    )
    def on_cancel_delete(n_clicks_list):
        if not any(n_clicks_list):
            return no_update
        return None

    # ── E-Trade Auth: Start Flow ──
    @app.callback(
        Output('auth-state-store', 'data'),
        Output('auth-message', 'children'),
        Input('auth-start-btn', 'n_clicks'),
        prevent_initial_call=True,
    )
    def start_etrade_auth(n_clicks):
        try:
            from etrade.auth import start_auth_flow
            url, req_token, req_secret = start_auth_flow()
            return (
                {'request_token': req_token, 'request_secret': req_secret},
                html.Span('Browser opened. Authorize, then paste the verifier code.',
                          style={'color': '#00d4ff', 'fontSize': '0.8rem'}),
            )
        except Exception as e:
            return no_update, html.Span(f'Error: {str(e)}',
                                        style={'color': '#ff6b6b', 'fontSize': '0.8rem'})

    # ── E-Trade Auth: Complete Flow ──
    @app.callback(
        Output('session-store', 'data'),
        Output('auth-message', 'children', allow_duplicate=True),
        Output('auth-status', 'children'),
        Input('auth-complete-btn', 'n_clicks'),
        State('verifier-input', 'value'),
        State('auth-state-store', 'data'),
        prevent_initial_call=True,
    )
    def complete_etrade_auth(n_clicks, verifier, auth_state):
        if not verifier or not auth_state:
            return no_update, 'Enter the verifier code first.', no_update

        try:
            from etrade.auth import complete_auth_flow
            session, error = complete_auth_flow(
                verifier,
                auth_state['request_token'],
                auth_state['request_secret'],
            )
            if error:
                return no_update, html.Span(error, style={'color': '#ff6b6b'}), no_update
            return (
                {'authenticated': True},
                html.Span('Authenticated!', style={'color': '#00ff88'}),
                dbc.Badge('Connected', color='success', className='ms-2'),
            )
        except Exception as e:
            return no_update, html.Span(f'Error: {str(e)}',
                                        style={'color': '#ff6b6b'}), no_update

    # ── E-Trade API: Fetch accounts ──
    @app.callback(
        Output('account-selector', 'options'),
        Input('session-store', 'data'),
        prevent_initial_call=True,
    )
    def load_accounts(session_data):
        if not session_data or not session_data.get('authenticated'):
            return []
        try:
            from etrade.auth import get_session
            session, error = get_session()
            if error:
                return []
            from etrade.client import get_accounts
            accounts = get_accounts(session)
            return [{'label': a['accountName'], 'value': a['accountIdKey']} for a in accounts]
        except Exception:
            return []

    # ── E-Trade API: Fetch transactions (background, chunked) ──
    @app.callback(
        Output('trades-store', 'data', allow_duplicate=True),
        Output('fetch-status', 'children'),
        Input('refresh-btn', 'n_clicks'),
        State('data-source-toggle', 'value'),
        State('account-selector', 'value'),
        State('date-range-picker', 'start_date'),
        State('date-range-picker', 'end_date'),
        State('session-store', 'data'),
        background=True,
        progress=Output('fetch-log-store', 'data'),
        prevent_initial_call=True,
    )
    def fetch_api_data(set_progress, n_clicks, source, account_id, start_date, end_date, session_data):
        """Fetch E-Trade transactions in 90-day chunks with live progress updates.

        set_progress is injected by Dash as the first argument when background=True
        and progress= is set. It writes to fetch-log-store, which triggers
        render_fetch_panel reactively.

        IMPORTANT: This callback cannot return Dash component objects (html.Span etc.)
        because it runs in a worker process. fetch-status.children must be str or None.
        """
        if source != 'api':
            return no_update, no_update
        if not account_id:
            set_progress({'status': 'error', 'chunks_done': 0, 'chunks_total': 0,
                          'log': [], 'summary': {'error': 'Select an account first'}})
            return no_update, None
        if not session_data or not session_data.get('authenticated'):
            set_progress({'status': 'error', 'chunks_done': 0, 'chunks_total': 0,
                          'log': [], 'summary': {'error': 'Authenticate first'}})
            return no_update, None

        from datetime import date as date_cls
        from etrade.auth import get_session
        from etrade.models import normalize_transactions

        start = date_cls.fromisoformat(start_date)
        end = date_cls.fromisoformat(end_date)
        chunks = chunk_date_range(start, end)

        set_progress({'status': 'running', 'chunks_done': 0, 'chunks_total': len(chunks),
                      'log': [], 'summary': {}})

        log = []

        def on_chunk(chunk_idx, total, log_entry):
            log.append(log_entry)
            set_progress({
                'status': 'running',
                'chunks_done': chunk_idx,
                'chunks_total': total,
                'log': log,
                'summary': {},
            })

        try:
            session, error = get_session()
            if error:
                set_progress({'status': 'error', 'chunks_done': 0,
                              'chunks_total': len(chunks), 'log': log,
                              'summary': {'error': f'Session error: {error}'}})
                return no_update, None

            all_txns = fetch_all_chunks(session, account_id, chunks, on_chunk)

            if not all_txns:
                set_progress({'status': 'done', 'chunks_done': len(chunks),
                              'chunks_total': len(chunks), 'log': log,
                              'summary': {'error': None, 'total_raw_txns': 0,
                                          'total_option_trades': 0, 'total_positions': 0,
                                          'skipped_activity_types': [],
                                          'fetch_time': datetime.now().isoformat()}})
                return no_update, None

            trades = normalize_transactions(all_txns)

            if not trades:
                set_progress({'status': 'done', 'chunks_done': len(chunks),
                              'chunks_total': len(chunks), 'log': log,
                              'summary': {'error': None, 'total_raw_txns': len(all_txns),
                                          'total_option_trades': 0, 'total_positions': 0,
                                          'skipped_activity_types': [],
                                          'fetch_time': datetime.now().isoformat()}})
                return no_update, None

            real_trades, split_map, get_key = normalize_trades(trades)
            combined = _merge_manual_trades(real_trades, start_date, end_date)
            positions_data = _build_and_serialize_positions(combined, split_map, get_key)

            recognized = {'Sold Short', 'Bought To Open', 'Bought To Cover',
                          'Sold To Close', 'Option Expired', 'Option Assigned'}
            found_types = set(t['activity_type'] for t in real_trades)
            skipped = list(found_types - recognized)

            summary = {
                'total_raw_txns': len(all_txns),
                'total_option_trades': len(real_trades),
                'total_positions': len(positions_data),
                'skipped_activity_types': skipped,
                'fetch_time': datetime.now().isoformat(),
                'error': None,
            }
            set_progress({'status': 'done', 'chunks_done': len(chunks),
                          'chunks_total': len(chunks), 'log': log, 'summary': summary})

            return {
                'trades': _serialize_trades(combined),
                'positions': positions_data,
                'filename': 'E-Trade API',
            }, None

        except Exception as e:
            set_progress({'status': 'error', 'chunks_done': len(log),
                          'chunks_total': len(chunks), 'log': log,
                          'summary': {'error': str(e)}})
            return no_update, None

    # ── KPI Cards ──
    @app.callback(
        Output('kpi-row', 'children'),
        Input('trades-store', 'data'),
    )
    def update_kpis(data):
        if not data:
            return [
                dbc.Col(kpi_card('Total P&L', '$0.00', 'neutral',
                                 icon=KPI_ICONS['pnl']), lg=True),
                dbc.Col(kpi_card('Win Rate', '\u2014', 'neutral',
                                 icon=KPI_ICONS['winrate']), lg=True),
                dbc.Col(kpi_card('Open Positions', '0', 'neutral',
                                 icon=KPI_ICONS['open']), lg=True),
                dbc.Col(kpi_card('Latest Month', '$0.00', 'neutral',
                                 icon=KPI_ICONS['monthly']), lg=True),
                dbc.Col(kpi_card('Total Positions', '0', 'neutral',
                                 icon=KPI_ICONS['total']), lg=True),
            ]

        positions = data['positions']
        trades = _deserialize_trades(data['trades'])

        closed = [p for p in positions if p['status'] != 'Open']
        open_pos = [p for p in positions if p['status'] == 'Open']
        wins = [p for p in closed if p['total_pnl'] > 0]
        losses = [p for p in closed if p['total_pnl'] < 0]
        breakeven = len(closed) - len(wins) - len(losses)
        total_pnl = sum(p['total_pnl'] for p in positions)

        if closed:
            win_pct = len(wins) / len(closed) * 100
            win_rate = f"{win_pct:.1f}%"
            win_subtitle = f"{len(wins)}W / {len(losses)}L"
            if breakeven:
                win_subtitle += f" / {breakeven}BE"
        else:
            win_rate = '\u2014'
            win_subtitle = None

        monthly = build_monthly_data(trades)
        latest_month_net = monthly[-1]['net'] if monthly else 0

        pnl_variant = 'positive' if total_pnl >= 0 else 'negative'
        month_variant = 'positive' if latest_month_net >= 0 else 'negative'

        return [
            dbc.Col(kpi_card('Total P&L', format_currency(total_pnl), pnl_variant,
                             icon=KPI_ICONS['pnl']), lg=True),
            dbc.Col(kpi_card('Win Rate', win_rate, 'cyan',
                             icon=KPI_ICONS['winrate'], subtitle=win_subtitle), lg=True),
            dbc.Col(kpi_card('Open Positions', str(len(open_pos)), 'amber',
                             icon=KPI_ICONS['open']), lg=True),
            dbc.Col(kpi_card('Latest Month', format_currency(latest_month_net), month_variant,
                             icon=KPI_ICONS['monthly']), lg=True),
            dbc.Col(kpi_card('Total Positions', str(len(positions)), 'violet',
                             icon=KPI_ICONS['total']), lg=True),
        ]

    # ── Symbol filter options ──
    @app.callback(
        Output('filter-symbol', 'options'),
        Input('trades-store', 'data'),
    )
    def update_symbol_filter(data):
        if not data:
            return []
        symbols = sorted(set(p['symbol'] for p in data['positions']))
        return [{'label': s, 'value': s} for s in symbols]

    # ── Positions Table ──
    @app.callback(
        Output('positions-table', 'data'),
        Input('trades-store', 'data'),
        Input('filter-symbol', 'value'),
        Input('filter-status', 'value'),
        Input('filter-direction', 'value'),
    )
    def update_positions_table(data, symbols, statuses, directions):
        if not data:
            return []

        positions = data['positions']

        if symbols:
            positions = [p for p in positions if p['symbol'] in symbols]
        if statuses:
            positions = [p for p in positions if p['status'] in statuses]
        if directions:
            positions = [p for p in positions if p['direction'] in directions]

        rows = []
        for p in sorted(positions, key=lambda x: x['open_date'] or ''):
            rows.append({
                'open_date': p['open_date'][:10] if p['open_date'] else '\u2014',
                'close_date': p['close_date'][:10] if p['close_date'] else 'OPEN',
                'symbol': p['symbol'],
                'opt_type': p['opt_type'],
                'strike': p['original_strike'],
                'expiration': p['expiration'],
                'contracts': p['contracts'],
                'direction': p['direction'],
                'avg_open_price': round(p['avg_open_price'], 2),
                'avg_close_price': round(p['avg_close_price'], 2),
                'total_pnl': round(p['total_pnl'], 2),
                'days_held': p['days_held'],
                'status': p['status'],
                'roll_chain': p.get('roll_chain', ''),
                'analyze': 'Analyze' if p['status'] == 'Open' else '',
                'position_id': p['position_id'],
            })

        return rows

    # ── Roll Chains Tab ──
    @app.callback(
        Output('rolls-container', 'children'),
        Input('trades-store', 'data'),
        Input('main-tabs', 'active_tab'),
    )
    def update_rolls_tab(data, active_tab):
        if active_tab != 'tab-rolls' or not data:
            return html.P('Upload a CSV or connect to E-Trade to see roll chains.',
                          style={'color': '#484f58', 'fontSize': '0.88rem',
                                 'fontFamily': "'DM Sans', sans-serif"})

        positions = data['positions']

        chains_by_index = {}
        for p in positions:
            ci = p.get('chain_index', -1)
            if ci < 0:
                continue
            chains_by_index.setdefault(ci, []).append(p)

        if not chains_by_index:
            return html.P('No roll chains detected.',
                          style={'color': '#484f58', 'fontSize': '0.88rem'})

        cards = []
        for chain_idx in sorted(chains_by_index.keys()):
            chain = chains_by_index[chain_idx]
            chain.sort(key=lambda x: x.get('chain_leg', 0))
            chain_name = f"Chain {chain_idx + 1}"
            first = chain[0]
            chain_pnl = sum(p['total_pnl'] for p in chain)
            times_rolled = len(chain) - 1

            pnl_col = '#00ff88' if chain_pnl >= 0 else '#ff6b6b'

            leg_rows = []
            for i, p in enumerate(chain):
                role = 'Original' if i == 0 else f'Roll {i}' if i < len(chain) - 1 else f'Roll {i} (Final)'

                roll_net_str = ''
                if i > 0:
                    prev = chain[i - 1]
                    roll_net = prev.get('total_close_amount', 0) + p.get('total_open_amount', 0)
                    roll_net_str = format_currency(roll_net)

                leg_rows.append(
                    html.Tr([
                        html.Td(role, style={'fontFamily': "'DM Sans', sans-serif",
                                            'fontWeight': '500'}),
                        html.Td(p['open_date'][:10] if p['open_date'] else '\u2014'),
                        html.Td(p['close_date'][:10] if p['close_date'] else 'OPEN',
                                style={'color': '#ffb347'} if not p['close_date'] or p['close_date'] == 'OPEN' else {}),
                        html.Td(f"${p['original_strike']:.2f}"),
                        html.Td(p['expiration']),
                        html.Td(str(p['contracts'])),
                        html.Td(f"${p['avg_open_price']:.2f}"),
                        html.Td(f"${p['avg_close_price']:.2f}"),
                        html.Td(
                            format_currency(p['total_pnl']),
                            style={'color': '#00ff88' if p['total_pnl'] >= 0 else '#ff6b6b',
                                   'fontWeight': 'bold'},
                        ),
                        html.Td(
                            roll_net_str,
                            style={'color': '#00ff88' if roll_net_str and not roll_net_str.startswith('-') else '#ff6b6b',
                                   'fontWeight': 'bold'} if roll_net_str else {},
                        ),
                        html.Td(p['status']),
                    ])
                )

            card = dbc.Card([
                dbc.CardHeader([
                    html.Strong(f"{chain_name}: {first['symbol']} {first['opt_type']}"),
                    html.Span(f" \u00b7 Rolled {times_rolled}x",
                              style={'color': '#8b949e', 'marginLeft': '8px'}),
                    html.Span(
                        f"Chain P&L: {format_currency(chain_pnl)}",
                        style={'color': pnl_col, 'fontWeight': 'bold', 'float': 'right',
                               'fontFamily': "'IBM Plex Mono', monospace"},
                    ),
                ]),
                dbc.CardBody(
                    dbc.Table([
                        html.Thead(html.Tr([
                            html.Th('Leg'), html.Th('Open'), html.Th('Close'),
                            html.Th('Strike'), html.Th('Expiry'), html.Th('Qty'),
                            html.Th('Open $'), html.Th('Close $'),
                            html.Th('P&L'), html.Th('Roll Net'),
                            html.Th('Status'),
                        ])),
                        html.Tbody(leg_rows),
                    ], bordered=True, hover=True, size='sm', className='mb-0'),
                ),
            ], className='mb-3 roll-chain-card')

            cards.append(card)

        return cards

    # ── Monthly Income Tab ──
    @app.callback(
        Output('monthly-chart', 'figure'),
        Output('monthly-table', 'data'),
        Input('trades-store', 'data'),
        Input('main-tabs', 'active_tab'),
    )
    def update_monthly_tab(data, active_tab):
        import plotly.graph_objects as go
        empty_fig = go.Figure().update_layout(
            template='plotly_dark',
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
        )

        if active_tab != 'tab-monthly' or not data:
            return empty_fig, []

        trades = _deserialize_trades(data['trades'])
        monthly = build_monthly_data(trades)

        fig = monthly_income_chart(monthly)
        return fig, monthly

    # ── P&L Analyzer: Analyze button click → tab switch + store write ──
    @app.callback(
        Output('analyzer-store', 'data'),
        Output('main-tabs', 'active_tab'),
        Input('positions-table', 'active_cell'),
        State('positions-table', 'data'),
        State('trades-store', 'data'),
        prevent_initial_call=True,
    )
    def on_analyze_click(active_cell, table_data, trades_data):
        if not active_cell or not table_data or not trades_data:
            return no_update, no_update
        if active_cell.get('column_id') != 'analyze':
            return no_update, no_update

        row = table_data[active_cell['row']]
        if row.get('status') != 'Open':
            return no_update, no_update

        position_id = row.get('position_id', '')
        position = None
        for p in trades_data.get('positions', []):
            if p['position_id'] == position_id:
                position = p
                break

        if not position:
            return no_update, no_update

        return position, 'tab-analyzer'

    # ── P&L Analyzer: Populate legs + fetch quote ──
    @app.callback(
        Output('analyzer-legs-table', 'data'),
        Output('analyzer-spot', 'value'),
        Output('analyzer-quote-status', 'children'),
        Output('analyzer-dte-display', 'children'),
        Output('analyzer-summary-strip', 'children'),
        Input('analyzer-store', 'data'),
        State('session-store', 'data'),
        prevent_initial_call=True,
    )
    def populate_analyzer(position, session_data):
        if not position:
            return no_update, no_update, no_update, no_update, no_update

        from datetime import date as date_cls

        symbol = position['symbol']
        opt_type = position['opt_type'].lower()
        strike = position['original_strike']
        contracts = position['contracts']
        direction = position['direction']
        expiration = position['expiration']
        avg_open_price = position['avg_open_price']

        qty = -contracts if direction == 'Short' else contracts

        exp_date = None
        for fmt in ('%m/%d/%y', '%Y-%m-%d', '%m/%d/%Y'):
            try:
                exp_date = datetime.strptime(expiration, fmt).date()
                break
            except (ValueError, TypeError):
                continue
        dte = max((exp_date - date_cls.today()).days, 0) if exp_date else 30

        leg = {
            'leg_num': 1,
            'type': opt_type,
            'strike': strike,
            'qty': qty,
            'iv': 0.30,
            'entry_price': round(avg_open_price, 2),
            'remove': '\u2715',
        }

        spot_value = None
        quote_msg = ''
        if session_data and session_data.get('authenticated'):
            try:
                from etrade.auth import get_session
                from etrade.client import get_quote, format_option_symbol
                session, error = get_session()
                if session and not error:
                    equity_quotes = get_quote(session, [symbol])
                    if symbol in equity_quotes:
                        spot_value = equity_quotes[symbol]['last_trade']

                    opt_sym = format_option_symbol(symbol, expiration, opt_type, strike)
                    opt_quotes = get_quote(session, [opt_sym])
                    if opt_sym in opt_quotes and opt_quotes[opt_sym]['iv'] > 0:
                        leg['iv'] = round(opt_quotes[opt_sym]['iv'], 4)

                    quote_msg = 'Quote loaded from E-Trade'
                else:
                    quote_msg = 'Session error \u2014 enter values manually'
            except Exception as exc:
                quote_msg = f'Quote fetch failed \u2014 enter values manually'
        else:
            quote_msg = 'No API session \u2014 enter spot and IV manually'

        summary_strip = html.Div([
            html.Strong(f'{symbol} ',
                        style={'fontSize': '1.15rem', 'color': '#00d4ff',
                               'fontFamily': "'Sora', sans-serif"}),
            html.Span(f'{opt_type.upper()} ${strike:.2f} exp {expiration}',
                      style={'fontFamily': "'IBM Plex Mono', monospace",
                             'fontSize': '0.9rem'}),
            html.Span(f' \u00b7 {direction} {contracts} contract(s)',
                      style={'color': '#8b949e', 'marginLeft': '8px'}),
        ], className='analyzer-summary')

        return [leg], spot_value, quote_msg, str(dte), summary_strip

    # ── P&L Analyzer: Add leg ──
    @app.callback(
        Output('analyzer-legs-table', 'data', allow_duplicate=True),
        Input('add-leg-btn', 'n_clicks'),
        State('analyzer-legs-table', 'data'),
        prevent_initial_call=True,
    )
    def add_analyzer_leg(n_clicks, current_data):
        if not current_data:
            current_data = []
        next_num = len(current_data) + 1
        current_data.append({
            'leg_num': next_num,
            'type': 'put',
            'strike': 0,
            'qty': 1,
            'iv': 0.30,
            'entry_price': 0,
            'remove': '\u2715',
        })
        return current_data

    # ── P&L Analyzer: Remove leg ──
    @app.callback(
        Output('analyzer-legs-table', 'data', allow_duplicate=True),
        Input('analyzer-legs-table', 'active_cell'),
        State('analyzer-legs-table', 'data'),
        prevent_initial_call=True,
    )
    def remove_analyzer_leg(active_cell, current_data):
        if not active_cell or not current_data:
            return no_update
        if active_cell.get('column_id') != 'remove':
            return no_update
        row_idx = active_cell['row']
        if row_idx < 0 or row_idx >= len(current_data):
            return no_update
        current_data.pop(row_idx)
        for i, leg in enumerate(current_data):
            leg['leg_num'] = i + 1
        return current_data

    # ── P&L Analyzer: Calculate → render heatmap + Greeks ──
    @app.callback(
        Output('analyzer-heatmap', 'figure'),
        Output('analyzer-delta', 'figure'),
        Output('analyzer-gamma', 'figure'),
        Output('analyzer-theta', 'figure'),
        Output('analyzer-vega', 'figure'),
        Output('analyzer-result-summary', 'children'),
        Input('calculate-btn', 'n_clicks'),
        State('analyzer-legs-table', 'data'),
        State('analyzer-spot', 'value'),
        State('analyzer-rate', 'value'),
        State('analyzer-dte-display', 'children'),
        prevent_initial_call=True,
    )
    def calculate_analyzer(n_clicks, legs_data, spot, rate, dte_str):
        import plotly.graph_objects as go
        import numpy as np
        from core.pricing import calculate_position_pl, calculate_greeks_profile

        empty_fig = go.Figure().update_layout(
            template='plotly_dark',
            paper_bgcolor='rgba(0,0,0,0)',
            plot_bgcolor='rgba(0,0,0,0)',
        )

        if not legs_data or spot is None:
            msg = html.Span('Enter spot price and at least one leg, then click Calculate.',
                            style={'color': '#484f58'})
            return empty_fig, empty_fig, empty_fig, empty_fig, empty_fig, msg

        try:
            spot = float(spot)
            if spot <= 0:
                raise ValueError(f'Spot price must be positive, got {spot}')
            rate_pct = float(rate) if rate is not None else 4.5
            r = rate_pct / 100.0

            if dte_str is None or dte_str == '':
                dte = 30
            else:
                dte = int(str(dte_str).strip())

            legs = []
            for leg in legs_data:
                opt_type = (leg.get('type') or 'put').lower()
                if opt_type not in ('call', 'put'):
                    opt_type = 'put'
                legs.append({
                    'type': opt_type,
                    'strike': float(leg['strike']),
                    'qty': int(float(leg['qty'])),
                    'iv': float(leg['iv']),
                    'entry_price': float(leg['entry_price']),
                })

            if not legs:
                raise ValueError('No valid legs in the table.')

            spot_low = spot * 0.80
            spot_high = spot * 1.20
            spot_prices = np.linspace(spot_low, spot_high, 50)

            dte_values = np.linspace(0, max(dte, 1), 20).astype(int)
            dte_values = np.unique(dte_values)[::-1]

            pl_grid, total_entry = calculate_position_pl(legs, spot_prices, dte_values, r)
            heatmap_fig = pl_heatmap_chart(pl_grid, spot_prices, dte_values)

            deltas, gammas, thetas, vegas = [], [], [], []
            for s in spot_prices:
                g = calculate_greeks_profile(legs, s, dte, r)
                deltas.append(g['delta'])
                gammas.append(g['gamma'])
                thetas.append(g['theta'])
                vegas.append(g['vega'])

            delta_fig = greeks_chart(spot_prices, deltas, 'Delta', '#00d4ff')
            gamma_fig = greeks_chart(spot_prices, gammas, 'Gamma', '#ffb347')
            theta_fig = greeks_chart(spot_prices, thetas, 'Theta', '#ff6b6b')
            vega_fig = greeks_chart(spot_prices, vegas, 'Vega', '#00ff88')

            exp_row_idx = int(np.argmin(dte_values))
            exp_pl = pl_grid[exp_row_idx, :]
            breakevens = []
            for i in range(len(exp_pl) - 1):
                if exp_pl[i] * exp_pl[i + 1] < 0:
                    x0, x1 = spot_prices[i], spot_prices[i + 1]
                    y0, y1 = exp_pl[i], exp_pl[i + 1]
                    be = x0 - y0 * (x1 - x0) / (y1 - y0)
                    breakevens.append(round(be, 2))

            max_profit = round(float(np.max(exp_pl)), 2)
            max_loss = round(float(np.min(exp_pl)), 2)

            summary_parts = []
            if breakevens:
                be_str = ', '.join(f'${b:,.2f}' for b in breakevens)
                summary_parts.append(
                    html.Span([
                        html.Span('Breakeven ', style={'color': '#8b949e'}),
                        html.Span(be_str, style={'color': '#e6edf3', 'fontWeight': '600',
                                                  'fontFamily': "'IBM Plex Mono', monospace"}),
                    ], className='me-4'))
            summary_parts.append(
                html.Span([
                    html.Span('Max Profit ', style={'color': '#8b949e'}),
                    html.Span(format_currency(max_profit),
                              style={'color': '#00ff88', 'fontWeight': '600',
                                     'fontFamily': "'IBM Plex Mono', monospace"}),
                ], className='me-4'))
            summary_parts.append(
                html.Span([
                    html.Span('Max Loss ', style={'color': '#8b949e'}),
                    html.Span(format_currency(max_loss),
                              style={'color': '#ff6b6b', 'fontWeight': '600',
                                     'fontFamily': "'IBM Plex Mono', monospace"}),
                ], className='me-4'))
            summary_parts.append(
                html.Span([
                    html.Span('Entry ', style={'color': '#484f58'}),
                    html.Span(format_currency(total_entry),
                              style={'color': '#8b949e',
                                     'fontFamily': "'IBM Plex Mono', monospace"}),
                ]))

            summary_div = html.Div(
                summary_parts,
                className='analyzer-summary',
                style={'marginTop': '12px'},
            )

            return heatmap_fig, delta_fig, gamma_fig, theta_fig, vega_fig, summary_div

        except Exception as exc:
            err = html.Span(f'Calculation error: {exc}',
                            style={'color': '#ff6b6b', 'fontSize': '0.85rem'})
            return empty_fig, empty_fig, empty_fig, empty_fig, empty_fig, err

    _register_fetch_panel_callback(app)


def _register_fetch_panel_callback(app):
    """Register the render_fetch_panel callback."""
    @app.callback(
        Output('fetch-status-panel', 'children'),
        Input('fetch-log-store', 'data'),
    )
    def render_fetch_panel(log_data):
        """Render the always-visible fetch status strip below the header."""
        if not log_data or log_data.get('status') == 'idle':
            return html.Div()

        status = log_data.get('status', 'idle')
        chunks_done = log_data.get('chunks_done', 0)
        chunks_total = log_data.get('chunks_total', 1)
        log = log_data.get('log', [])
        summary = log_data.get('summary', {})

        pct = int(chunks_done / max(chunks_total, 1) * 100)

        if status == 'running':
            icon = '\u21BB'
            icon_color = '#00d4ff'
            label = f'Fetching E-Trade data... ({chunks_done}/{chunks_total} chunks)'
            bar_color = 'info'
            animated = True
            striped = True
        elif status == 'done':
            icon = '\u2713'
            icon_color = '#00ff88'
            fetch_time = summary.get('fetch_time', '')
            if fetch_time:
                dt = datetime.fromisoformat(fetch_time)
                time_str = dt.strftime('%b %d %Y, %I:%M%p')
            else:
                time_str = '\u2014'
            n_trades = summary.get('total_option_trades', 0)
            n_pos = summary.get('total_positions', 0)
            label = f'Last fetch: {time_str} \u2014 {n_trades} trades, {n_pos} positions'
            bar_color = 'success'
            animated = False
            striped = False
        else:  # error
            icon = '\u2717'
            icon_color = '#ff6b6b'
            label = f'Fetch error: {summary.get("error", "Unknown error")}'
            bar_color = 'danger'
            animated = False
            striped = False

        summary_line = html.Div([
            html.Span(icon, style={'color': icon_color, 'marginRight': '8px',
                                   'fontSize': '1rem', 'fontWeight': 'bold'}),
            html.Span(label, style={'fontSize': '0.82rem', 'color': '#e6edf3',
                                    'fontFamily': "'DM Sans', sans-serif"}),
        ], className='d-flex align-items-center')

        panel_children = []

        if status == 'running':
            panel_children.append(dbc.Progress(
                value=pct,
                label=f'{pct}%' if pct >= 10 else '',
                color=bar_color,
                animated=animated,
                striped=striped,
                style={'height': '16px', 'marginBottom': '6px'},
            ))

        panel_children.append(summary_line)

        log_rows = []
        for entry in log:
            chunk_start = entry.get('chunk_start', '')[:10]
            chunk_end = entry.get('chunk_end', '')[:10]
            raw_txns = entry.get('raw_txns', 0)
            entry_status = entry.get('status', 'done')
            option_txns = entry.get('option_txns', '?')
            row_icon = '\u2713' if entry_status == 'done' else '\u2717'
            row_color = '#00ff88' if entry_status == 'done' else '#ff6b6b'
            log_rows.append(html.Div([
                html.Span(f'{row_icon} ', style={'color': row_color}),
                html.Span(f'{chunk_start} \u2192 {chunk_end}: ',
                          style={'fontWeight': '600', 'color': '#8b949e',
                                 'fontFamily': "'IBM Plex Mono', monospace"}),
                html.Span(f'{raw_txns} raw, {option_txns} options',
                          style={'color': '#484f58',
                                 'fontFamily': "'IBM Plex Mono', monospace"}),
            ], style={'fontSize': '0.75rem', 'padding': '1px 0'}))

        if status == 'running' and chunks_done < chunks_total:
            log_rows.append(html.Div(
                '\u21BB fetching next chunk...',
                style={'fontSize': '0.75rem', 'color': '#00d4ff', 'fontStyle': 'italic',
                       'paddingLeft': '4px'},
            ))

        skipped = summary.get('skipped_activity_types', [])
        if skipped:
            log_rows.append(html.Div([
                html.Span('\u26A0 Skipped: ',
                          style={'color': '#ffb347', 'fontWeight': '600'}),
                html.Span(', '.join(skipped), style={'color': '#ffb347'}),
            ], style={'fontSize': '0.75rem', 'padding': '2px 0'}))

        if log_rows:
            panel_children.append(html.Div(
                log_rows,
                style={'marginTop': '6px', 'paddingLeft': '16px',
                       'maxHeight': '160px', 'overflowY': 'auto'},
            ))

        return html.Div(panel_children, className='fetch-panel')
