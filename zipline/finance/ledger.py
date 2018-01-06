#
# Copyright 2017 Quantopian, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import division

from collections import namedtuple, OrderedDict
from math import isnan

import logbook
import numpy as np
import pandas as pd
from six import iteritems, itervalues, PY2

from zipline.assets import Future
from zipline.finance.transaction import Transaction
import zipline.protocol as zp
from zipline.utils.compat import values_as_list
from zipline.utils.sentinel import sentinel
from .position import Position, positiondict

log = logbook.Logger('Performance')


PositionStats = namedtuple(
    'PositionStats',
    [
        'net_exposure',
        'gross_value',
        'gross_exposure',
        'short_value',
        'short_exposure',
        'shorts_count',
        'long_value',
        'long_exposure',
        'longs_count',
        'net_value',
    ],
)


class PositionTracker(object):
    """The current state of the positions held.

    Parameters
    ----------
    data_frequency : {'daily', 'minute'}
        The data frequency of the simulation.
    """
    def __init__(self, data_frequency):
        # asset => position object
        self.positions = positiondict()
        self._unpaid_dividends = {}
        self._unpaid_stock_dividends = {}
        self._positions_store = zp.Positions()

        self.data_frequency = data_frequency

        # cache the stats until something alters our positions
        self._dirty_stats = True
        self._stats = None

    def update_position(self,
                        asset,
                        amount=None,
                        last_sale_price=None,
                        last_sale_date=None,
                        cost_basis=None):
        self._dirty_stats = True

        if asset not in self.positions:
            position = Position(asset)
            self.positions[asset] = position
        else:
            position = self.positions[asset]

        if amount is not None:
            position.amount = amount
        if last_sale_price is not None:
            position.last_sale_price = last_sale_price
        if last_sale_date is not None:
            position.last_sale_date = last_sale_date
        if cost_basis is not None:
            position.cost_basis = cost_basis

    def execute_transaction(self, txn):
        self._dirty_stats = True

        asset = txn.asset

        if asset not in self.positions:
            position = Position(asset)
            self.positions[asset] = position
        else:
            position = self.positions[asset]

        position.update(txn)

        if position.amount == 0:
            del self.positions[asset]

            try:
                # if this position exists in our user-facing dictionary,
                # remove it as well.
                del self._positions_store[asset]
            except KeyError:
                pass

    def handle_commission(self, asset, cost):
        # Adjust the cost basis of the stock if we own it
        if asset in self.positions:
            self._dirty_stats = True
            self.positions[asset].adjust_commission_cost_basis(asset, cost)

    def handle_splits(self, splits):
        """
        Processes a list of splits by modifying any positions as needed.

        Parameters
        ----------
        splits: list
            A list of splits.  Each split is a tuple of (asset, ratio).

        Returns
        -------
        int: The leftover cash from fractional sahres after modifying each
            position.
        """
        total_leftover_cash = 0

        for asset, ratio in splits:
            if asset in self.positions:
                self._dirty_stats = True

                # Make the position object handle the split. It returns the
                # leftover cash from a fractional share, if there is any.
                position = self.positions[asset]
                leftover_cash = position.handle_split(asset, ratio)
                total_leftover_cash += leftover_cash

        return total_leftover_cash

    def earn_dividends(self, dividends, stock_dividends):
        """
        Given a list of dividends whose ex_dates are all the next trading day,
        calculate and store the cash and/or stock payments to be paid on each
        dividend's pay date.

        Parameters
        ----------
        dividends: iterable of (asset, amount, pay_date) namedtuples

        stock_dividends: iterable of (asset, payment_asset, ratio, pay_date)
            namedtuples.
        """
        for dividend in dividends:
            # Store the earned dividends so that they can be paid on the
            # dividends' pay_dates.
            div_owed = self.positions[dividend.asset].earn_dividend(dividend)
            try:
                self._unpaid_dividends[dividend.pay_date].append(div_owed)
            except KeyError:
                self._unpaid_dividends[dividend.pay_date] = [div_owed]

        for stock_dividend in stock_dividends:
            div_owed = \
                self.positions[stock_dividend.asset].earn_stock_dividend(
                    stock_dividend)
            try:
                self._unpaid_stock_dividends[stock_dividend.pay_date].\
                    append(div_owed)
            except KeyError:
                self._unpaid_stock_dividends[stock_dividend.pay_date] = \
                    [div_owed]

    def pay_dividends(self, next_trading_day):
        """
        Returns a cash payment based on the dividends that should be paid out
        according to the accumulated bookkeeping of earned, unpaid, and stock
        dividends.
        """
        net_cash_payment = 0.0

        try:
            payments = self._unpaid_dividends[next_trading_day]
            # Mark these dividends as paid by dropping them from our unpaid
            del self._unpaid_dividends[next_trading_day]
        except KeyError:
            payments = []

        # representing the fact that we're required to reimburse the owner of
        # the stock for any dividends paid while borrowing.
        for payment in payments:
            net_cash_payment += payment['amount']

        # Add stock for any stock dividends paid.  Again, the values here may
        # be negative in the case of short positions.
        try:
            stock_payments = self._unpaid_stock_dividends[next_trading_day]
        except KeyError:
            stock_payments = []

        for stock_payment in stock_payments:
            payment_asset = stock_payment['payment_asset']
            share_count = stock_payment['share_count']
            # note we create a Position for stock dividend if we don't
            # already own the asset
            if payment_asset in self.positions:
                position = self.positions[payment_asset]
            else:
                position = self.positions[payment_asset] = \
                    Position(payment_asset)

            position.amount += share_count

        return net_cash_payment

    def maybe_create_close_position_transaction(self, asset, dt, data_portal):
        if not self.positions.get(asset):
            return None

        amount = self.positions.get(asset).amount
        price = data_portal.get_spot_value(
            asset, 'price', dt, self.data_frequency)

        # Get the last traded price if price is no longer available
        if isnan(price):
            price = self.positions.get(asset).last_sale_price

        txn = Transaction(
            asset=asset,
            amount=(-1 * amount),
            dt=dt,
            price=price,
            commission=0,
            order_id=None,
        )
        return txn

    def get_positions(self):

        positions = self._positions_store

        for asset, pos in iteritems(self.positions):

            if pos.amount == 0:
                # Clear out the position if it has become empty since the last
                # time get_positions was called.  Catching the KeyError is
                # faster than checking `if asset in positions`, and this can be
                # potentially called in a tight inner loop.
                try:
                    del positions[asset]
                except KeyError:
                    pass
                continue

            position = zp.Position(asset)
            position.amount = pos.amount
            position.cost_basis = pos.cost_basis
            position.last_sale_price = pos.last_sale_price
            position.last_sale_date = pos.last_sale_date

            # Adds the new position if we didn't have one before, or overwrite
            # one we have currently
            positions[asset] = position

        return positions

    def get_positions_list(self):
        positions = []
        for asset, pos in iteritems(self.positions):
            if pos.amount != 0:
                positions.append(pos.to_dict())
        return positions

    def sync_last_sale_prices(self,
                              dt,
                              data_portal,
                              handle_non_market_minutes=False):
        self._dirty_stats = True

        if not handle_non_market_minutes:
            for asset, position in iteritems(self.positions):
                last_sale_price = data_portal.get_spot_value(
                    asset,
                    'price',
                    dt,
                    self.data_frequency,
                )

                if not np.isnan(last_sale_price):
                    position.last_sale_price = last_sale_price
        else:
            previous_minute = data_portal.trading_calendar.previous_minute(dt)
            for asset, position in iteritems(self.positions):
                last_sale_price = data_portal.get_adjusted_value(
                    asset,
                    'price',
                    previous_minute,
                    dt,
                    self.data_frequency,
                )

                if not np.isnan(last_sale_price):
                    position.last_sale_price = last_sale_price

    @property
    def stats(self):
        if not self._dirty_stats:
            return self._stats

        net_value = long_value = short_value = 0
        long_exposure = short_exposure = 0
        longs_count = shorts_count = 0
        for position in itervalues(self.positions):
            # NOTE: this loop does a lot of stuff!
            # we call this function every single minute of the simulations
            # so let's not iterate through every single position multiple
            # times.
            exposure = position.amount * position.last_sale_price

            if isinstance(position.asset, Future):
                # Futures don't have an inherent position value.
                value = 0
                exposure *= position.asset.multiplier
            else:
                value = exposure

            if exposure > 0:
                longs_count += 1
                long_value += value
                long_exposure = exposure
            elif exposure < 0:
                shorts_count += 1
                short_value += value
                short_exposure += exposure

            net_value += value

        gross_value = long_value - short_value
        gross_exposure = long_exposure - short_exposure
        net_exposure = long_exposure + short_exposure

        # TODO: investigate cnamedtuple here because instance creation speed
        # is much faster
        self._stats = stats = PositionStats(
            long_value=long_value,
            gross_value=gross_value,
            short_value=short_value,
            long_exposure=long_exposure,
            short_exposure=short_exposure,
            gross_exposure=gross_exposure,
            net_exposure=net_exposure,
            longs_count=longs_count,
            shorts_count=shorts_count,
            net_value=net_value
        )
        return stats


if PY2:
    def move_to_end(ordered_dict, key, last=False):
        if last:
            ordered_dict[key] = ordered_dict.pop(key)
        else:
            # please don't do this in python 2 ;_;
            new_first_element = ordered_dict.pop(key)

            # the items (without the given key) in the order they were inserted
            items = ordered_dict.items()

            # reset the ordered_dict to re-insert in the new order
            ordered_dict.clear()

            ordered_dict[key] = new_first_element

            # add the items back in their original order
            ordered_dict.update(items)
else:
    move_to_end = OrderedDict.move_to_end


PeriodStats = namedtuple(
    'PeriodStats',
    'net_liquidation gross_leverage net_leverage',
)


not_overridden = sentinel(
    'not_overridden',
    'Mark that an account field has not been overridden',
)


class Ledger(object):
    """The ledger tracks all orders and transactions as well as the current
    state of the portfolio and positions.

    Attributes
    ----------
    portfolio : zipline.protocol.Portfolio
        The portfolio being managed.
    position_tracker : PositionTracker
        The current set of positions.
    daily_returns : pd.Series
        The daily returns series. Days that have not yet finished will hold
        a value of ``np.nan``.
    """
    def __init__(self, trading_sessions, capital_base, data_frequency):
        # Have some fields of the portfolio changed? This should be accessed
        # through ``self._dirty_portfolio``
        self.__dirty_portfolio = False

        self._portfolio = zp.Portfolio(trading_sessions[0], capital_base)

        self.daily_returns = pd.Series(
            np.nan,
            index=trading_sessions,
        )
        self._previous_total_returns = 0

        # this is a component of the cache key for the account
        self._position_stats = None

        # Have some fields of the account changed?
        self._dirty_account = True
        self._account = zp.Account(self._portfolio)

        # The broker blotter can override some fields on the account. This is
        # way to tangled up at the moment but we aren't fixing it today.
        self._account_overrides = {}

        self.position_tracker = PositionTracker(data_frequency)

        self._processed_transactions = {}

        self._orders_by_modified = {}
        self._orders_by_id = OrderedDict()

        # Keyed by asset, the previous last sale price of positions with
        # payouts on price differences, e.g. Futures.
        #
        # This dt is not the previous minute to the minute for which the
        # calculation is done, but the last sale price either before the period
        # start, or when the price at execution.
        self._payout_last_sale_prices = {}

    @property
    def _dirty_portfolio(self):
        return self.__dirty_portfolio

    @_dirty_portfolio.setter
    def _dirty_portfolio(self, value):
        if value:
            # marking the portfolio as dirty also marks the account as dirty
            self.__dirty_portfolio = self._dirty_account = value
        else:
            self.__dirty_portfolio = value

    def end_of_session(self, session_label):
        """Reset the state being tracked on a per-session basis.
        """
        self._processed_transactions = {}
        self._orders_by_modified = {}
        self._orders_by_id = OrderedDict()

        # compute the day's return from the cumulative returns today and
        # yesterday
        current_total_returns = self.portfolio.returns
        self.daily_returns[session_label] = (
            (current_total_returns + 1) /
            (self._previous_total_returns + 1) -
            1
        )
        self._previous_total_returns = current_total_returns

    def sync_last_sale_prices(self,
                              dt,
                              data_portal,
                              handle_non_market_minutes=False):
        self.position_tracker.sync_last_sale_prices(
            dt,
            data_portal,
            handle_non_market_minutes=handle_non_market_minutes,
        )
        self._portfolio_dirty = True

    @staticmethod
    def _calculate_execution_cash_flow(transaction):
        """Calculates the cash flow from executing the given transaction
        """
        if isinstance(transaction.asset, Future):
            return 0.0

        return -1 * transaction.price * transaction.amount

    @staticmethod
    def _calculate_payout(multiplier, amount, old_price, price):

        return (price - old_price) * multiplier * amount

    def process_transaction(self, transaction):
        """Add a transaction to ledger, updating the current state as needed.

        Parameters
        ----------
        transaction : zp.Transaction
            The transaction to execute.
        """
        self.position_tracker.execute_transaction(transaction)

        self._dirty_portfolio = True
        vars(self._portfolio)['cash_flow'] += (
            self._calculate_execution_cash_flow(transaction)
        )

        asset = transaction.asset
        if isinstance(asset, Future):
            try:
                old_price = self._payout_last_sale_prices[asset]
            except KeyError:
                self._payout_last_sale_prices[asset] = transaction.price
            else:
                position = self.position_tracker.positions[asset]
                amount = position.amount
                price = transaction.price

                self._dirty_portfolio = True
                vars(self._portfolio)['cash'] += self._calculate_payout(
                    asset.multiplier,
                    amount,
                    old_price,
                    price,
                )

                if amount + transaction.amount == 0:
                    del self._payout_last_sale_prices[asset]
                else:
                    self._payout_last_sale_prices[asset] = price

        # we only ever want the dict form from now on
        transaction_dict = transaction.to_dict()
        try:
            self._processed_transactions[transaction.dt].append(
                transaction_dict,
            )
        except KeyError:
            self._processed_transactions[transaction.dt] = [transaction_dict]

    def process_splits(self, splits):
        """Processes a list of splits by modifying any positions as needed.

        Parameters
        ----------
        splits: list[(Asset, float)]
            A list of splits. Each split is a tuple of (asset, ratio).
        """
        leftover_cash = self.position_tracker.handle_splits(splits)
        if leftover_cash > 0:
            self._dirty_portfolio = True
            self._portfolio.cash_flow += leftover_cash

    def process_order(self, order):
        """Keep track of an order that was placed.

        Parameters
        ----------
        order : zp.Order
            The order to record.
        """
        order_dict = order.to_dict()
        try:
            dt_orders = self._orders_by_modified[order.dt]
        except KeyError:
            self._orders_by_modified[order.dt] = OrderedDict([
                (order.id, order_dict),
            ])
            self._orders_by_id[order.id] = order_dict
        else:
            self._orders_by_id[order.id] = dt_orders[order.id] = order_dict
            # to preserve the order of the orders by modified date
            move_to_end(dt_orders, order.id, last=True)
            move_to_end(self._orders_by_id, order.id, last=True)

    def process_commission(self, commission):
        """Process the commission.

        Parameters
        ----------
        commission : zp.Event
            The commission being paid.
        """
        asset = commission['asset']
        cost = commission['cost']

        self.position_tracker.handle_commission(asset, cost)
        self._dirty_portfolio = True
        vars(self._portfolio)['cash_flow'] -= cost

    def close_position(self, asset, dt, data_portal):
        txn = self.position_tracker.maybe_create_close_position_transaction(
            asset,
            dt,
            data_portal,
        )
        if txn is not None:
            self.process_transaction(txn)

    def process_dividends(self, next_session, asset_finder, adjustment_reader):
        """Process dividends for the next session.

        This will earn us any dividends whose ex-date is the next session as
        well as paying out any dividends whose pay-date is the next session
        """
        position_tracker = self.position_tracker

        # Earn dividends whose ex_date is the next trading day. We need to
        # check if we own any of these stocks so we know to pay them out when
        # the pay date comes.
        held_sids = set(position_tracker.positions)
        if held_sids:
            cash_dividends = adjustment_reader.get_dividends_with_ex_date(
                held_sids,
                next_session,
                asset_finder
            )
            stock_dividends = (
                adjustment_reader.get_stock_dividends_with_ex_date(
                    held_sids,
                    next_session,
                    asset_finder
                )
            )

            # Earning a dividend just marks that we need to get paid out on
            # the dividend's pay-date. This does not affect our cash yet.
            position_tracker.earn_dividends(
                cash_dividends,
                stock_dividends,
            )

        # Pay out the dividends whose pay-date is the next session. This does
        # affect out cash.
        self._dirty_portfolio = True
        vars(self._portfolio)['cash_flow'] += position_tracker.pay_dividends(
            next_session,
        )

    def capital_change(self, change_amount):
        portfolio = vars(self._portfolio)

        # we update the cash and total value so this is not dirty
        portfolio['portfolio_value'] += change_amount
        portfolio['cash'] += change_amount

    def transactions(self, dt=None):
        """Retrieve the dict-form of all of the transactions in a given bar or
        for the whole simulation.

        Parameters
        ----------
        dt : pd.Timestamp or None, optional
            The particular datetime to look up transactions for. If not passed,
            or None is explicitly passed, all of the transactions will be
            returned.

        Returns
        -------
        transactions : list[dict]
            The transaction information.
        """
        if dt is None:
            # flatten the by-day transactions
            return [
                txn
                for by_day in itervalues(self._processed_transactions)
                for txn in by_day
            ]

        return self._processed_transactions[dt]

    def orders(self, dt=None):
        """Retrieve the dict-form of all of the orders in a given bar or for
        the whole simulation.

        Parameters
        ----------
        dt : pd.Timestamp or None, optional
            The particular datetime to look up order for. If not passed, or
            None is explicitly passed, all of the orders will be returned.

        Returns
        -------
        orders : list[dict]
            The order information.
        """
        if dt is None:
            # orders by id is already flattened
            return values_as_list(self._orders_by_id)

        return self._orders_by_modified[dt]

    def _get_payout_total(self, positions):
        calculate_payout = self._calculate_payout

        total = 0
        for asset, old_price in iteritems(self._payout_last_sale_prices):
            position = positions[asset]
            amount = positions.amount
            total += calculate_payout(
                asset.multiplier,
                amount,
                old_price,
                position.last_sale_price,
            )

        return total

    @property
    def portfolio(self):
        """Compute the current portfolio.

        Notes
        -----
        This is cached, repeated access will not recompute the portfolio until
        the portfolio has changed.
        """
        portfolio = self._portfolio
        if not self._dirty_portfolio:
            # There have been no changes to the portfolio since the last
            # request.
            return portfolio

        portfolio_dict = vars(portfolio)
        pt = self.position_tracker
        position_stats = pt.stats

        portfolio_dict['positions_value'] = position_value = (
            position_stats.net_value
        )
        portfolio_dict['positions_exposure'] = position_stats.net_exposure

        payout = self._get_payout_total(pt.positions)

        portfolio_dict['cash'] = cash = (
            portfolio.starting_cash +
            portfolio.cash_flow +
            payout
        )

        start_value = portfolio.portfolio_value

        # update the new starting value
        portfolio_dict['portfolio_value'] = end_value = cash + position_value

        pnl = end_value - start_value
        if start_value != 0:
            returns = pnl / start_value
        else:
            returns = 0.0

        portfolio_dict['pnl'] += pnl
        portfolio_dict['returns'] = (
            (1 + portfolio.returns) *
            (1 + returns) -
            1
        )

        # the portfolio has been fully synced
        self._dirty_portfolio = False
        return portfolio

    @staticmethod
    def _calculate_net_liquidation(ending_cash, long_value, short_value):
        return ending_cash + long_value + short_value

    @staticmethod
    def _calculate_leverage(exposure, net_liquidation):
        if net_liquidation != 0:
            return exposure / net_liquidation

        return np.inf

    def calculate_period_stats(self):
        position_stats = self.position_tracker.stats
        net_liquidation = self._calculate_net_liquidation(
            self._portfolio.cash,
            position_stats.long_value,
            position_stats.short_value,
        )
        gross_leverage = self._calculate_leverage(
            position_stats.gross_exposure,
            net_liquidation,
        )
        net_leverage = self._calculate_leverage(
            position_stats.net_exposure,
            net_liquidation,
        )

        return net_liquidation, gross_leverage, net_leverage

    def override_account_fields(self,
                                settled_cash=not_overridden,
                                accrued_interest=not_overridden,
                                buying_power=not_overridden,
                                equity_with_loan=not_overridden,
                                total_positions_value=not_overridden,
                                total_positions_exposure=not_overridden,
                                regt_equity=not_overridden,
                                regt_margin=not_overridden,
                                initial_margin_requirement=not_overridden,
                                maintenance_margin_requirement=not_overridden,
                                available_funds=not_overridden,
                                excess_liquidity=not_overridden,
                                cushion=not_overridden,
                                day_trades_remaining=not_overridden,
                                leverage=not_overridden,
                                net_leverage=not_overridden,
                                net_liquidation=not_overridden):
        """Override fields on ``self.account``.
        """
        # mark that the portfolio is dirty to override the fields again
        self._dirty_account = True
        self._account_overrides = kwargs = {
            k: v for k, v in locals().items() if v is not not_overridden
        }
        del kwargs['self']

    @property
    def account(self):
        account = self._account

        if self._dirty_account:
            portfolio = self.portfolio
            account_dict = vars(account)

            # If no attribute is found in the ``_account_overrides`` resort to
            # the following default values. If an attribute is found use the
            # existing value. For instance, a broker may provide updates to
            # these attributes. In this case we do not want to over write the
            # broker values with the default values.
            account_dict['settled_cash'] = portfolio.cash
            account_dict['accrued_interest'] = 0.0
            account_dict['buying_power'] = np.inf
            account_dict['equity_with_loan'] = portfolio.portfolio_value
            account_dict['total_positions_value'] = (
                portfolio.portfolio_value - portfolio.cash
            )
            account_dict['total_positions_exposure'] = (
                portfolio.positions_exposure
            )
            account_dict['regt_equity'] = portfolio.cash
            account_dict['regt_margin'] = np.inf
            account_dict['initial_margin_requirement'] = 0.0
            account_dict['maintenance_margin_requirement'] = 0.0
            account_dict['available_funds'] = portfolio.cash
            account_dict['excess_liquidity'] = portfolio.cash
            account_dict['cushion'] = (
                portfolio.cash / portfolio.portfolio_value
            )
            account_dict['day_trades_remaining'] = np.inf
            (account_dict['net_liquidation'],
             account_dict['gross_leverage'],
             account_dict['net_leverage']) = self.calculate_period_stats()

            # apply the overrides
            account_dict.update(self._account_overrides)

            # the account has been fully synced
            self._dirty_account = False

        return account
