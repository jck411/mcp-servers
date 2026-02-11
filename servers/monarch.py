"""Standalone Monarch Money MCP server.

Exposes financial account, transaction, budget, and analytics tools via MCP.
Zero imports from Backend_FastAPI — fully standalone.

Run:
    python -m servers.monarch --transport streamable-http --host 0.0.0.0 --port 9008
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Optional

from fastmcp import FastMCP
from monarchmoney import MonarchMoney, RequireMFAException

from shared.monarch_auth import MonarchCredentials, get_monarch_credentials, get_session_file_path

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class MonarchAuthError(Exception):
    """Raised when authentication with Monarch Money fails."""


class MonarchAPIError(Exception):
    """Raised when Monarch Money API calls fail."""


# ---------------------------------------------------------------------------
# Server setup
# ---------------------------------------------------------------------------

DEFAULT_HTTP_PORT = 9008

mcp = FastMCP("monarch")

# ---------------------------------------------------------------------------
# Client management
# ---------------------------------------------------------------------------

_monarch_client: Optional[MonarchMoney] = None
_client_lock = asyncio.Lock()


async def _get_client(force_refresh: bool = False) -> MonarchMoney:
    """Get an authenticated MonarchMoney client."""
    global _monarch_client
    if _monarch_client and not force_refresh:
        return _monarch_client

    async with _client_lock:
        if _monarch_client and not force_refresh:
            return _monarch_client

        creds = get_monarch_credentials()
        if not creds:
            raise MonarchAuthError("Monarch Money credentials not configured.")

        session_file = get_session_file_path()
        mm = MonarchMoney(session_file=str(session_file))

        # Try to load existing session only if not forcing refresh
        if not force_refresh and session_file.exists():
            try:
                mm.load_session(str(session_file))
            except Exception:
                pass

        # Check if logged in
        is_logged_in = False
        if not force_refresh:
            try:
                await mm.get_subscription_details()
                is_logged_in = True
            except Exception:
                is_logged_in = False

        if not is_logged_in:
            try:
                mfa_secret = (
                    creds.mfa_secret.strip().replace(" ", "") if creds.mfa_secret else None
                )
                await mm.login(
                    email=creds.email,
                    password=creds.password,
                    mfa_secret_key=mfa_secret,
                    use_saved_session=False,
                )
            except RequireMFAException:
                raise MonarchAuthError(
                    "MFA required but no secret provided. Please update credentials."
                )

            mm.save_session(str(session_file))

        _monarch_client = mm
        return _monarch_client


def _is_auth_error(e: Exception) -> bool:
    """Check if an exception looks like an auth/token error."""
    msg = str(e)
    return "401" in msg or "Unauthorized" in msg or "Invalid token" in msg


async def _retry_on_auth(
    fn,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Call *fn* with a fresh client on auth failures."""
    try:
        mm = await _get_client()
        return await fn(mm, *args, **kwargs)
    except Exception as e:
        if _is_auth_error(e):
            try:
                mm = await _get_client(force_refresh=True)
                return await fn(mm, *args, **kwargs)
            except Exception as retry_e:
                return {"error": f"Retry failed: {retry_e}"}
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Account tools
# ---------------------------------------------------------------------------


@mcp.tool("get_monarch_accounts")
async def get_monarch_accounts() -> dict[str, Any]:
    """Retrieve all Monarch Money accounts with their balances."""

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        data = await mm.get_accounts()
        accounts = data.get("accounts", [])
        simplified = [
            {
                "id": acc.get("id"),
                "name": acc.get("displayName"),
                "type": acc.get("type"),
                "subtype": acc.get("subtype"),
                "balance": acc.get("currentBalance"),
                "currency": acc.get("currency"),
                "updated_at": acc.get("updatedAt"),
            }
            for acc in accounts
        ]
        return {"accounts": simplified, "total_count": len(simplified)}

    return await _retry_on_auth(_call)


@mcp.tool("get_monarch_account_types")
async def get_monarch_account_types() -> dict[str, Any]:
    """List available account types and subtypes for manual accounts."""

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        data = await mm.get_account_type_options()
        options = data.get("accountTypeOptions", [])
        simplified: list[dict[str, Any]] = []
        seen_types: set[str] = set()
        for opt in options:
            type_info = opt.get("type", {})
            type_name = type_info.get("name")
            if type_name in seen_types:
                continue
            seen_types.add(type_name)
            subtypes = [
                {"name": s.get("name"), "display": s.get("display")}
                for s in type_info.get("possibleSubtypes", [])
            ]
            simplified.append(
                {
                    "type": type_name,
                    "display": type_info.get("display"),
                    "group": type_info.get("group"),
                    "subtypes": subtypes,
                }
            )
        return {"account_types": simplified}

    return await _retry_on_auth(_call)


@mcp.tool("create_monarch_manual_account")
async def create_monarch_manual_account(
    name: str,
    type: str,
    subtype: str,
    balance: float = 0.0,
    include_in_net_worth: bool = True,
) -> dict[str, Any]:
    """Create a new manual account.

    Use get_monarch_account_types to find valid type/subtype values.
    """

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        return await mm.create_manual_account(
            account_name=name,
            account_type=type,
            account_sub_type=subtype,
            account_balance=balance,
            is_in_net_worth=include_in_net_worth,
        )

    return await _retry_on_auth(_call)


@mcp.tool("update_monarch_account")
async def update_monarch_account(
    account_id: str,
    name: Optional[str] = None,
    balance: Optional[float] = None,
    include_in_net_worth: Optional[bool] = None,
    hide_from_list: Optional[bool] = None,
    hide_transactions: Optional[bool] = None,
) -> dict[str, Any]:
    """Update an existing account. Only provide fields that need to be updated."""

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        return await mm.update_account(
            account_id=account_id,
            account_name=name,
            account_balance=balance,
            include_in_net_worth=include_in_net_worth,
            hide_from_summary_list=hide_from_list,
            hide_transactions_from_reports=hide_transactions,
        )

    return await _retry_on_auth(_call)


@mcp.tool("delete_monarch_account")
async def delete_monarch_account(account_id: str) -> dict[str, Any]:
    """Delete an account."""

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        return await mm.delete_account(account_id)

    return await _retry_on_auth(_call)


# ---------------------------------------------------------------------------
# Transaction tools
# ---------------------------------------------------------------------------


@mcp.tool("get_monarch_transactions")
async def get_monarch_transactions(
    limit: int = 10,
    search: Optional[str] = None,
    category: Optional[str] = None,
) -> dict[str, Any]:
    """Retrieve recent transactions.

    Args:
        limit: Number of transactions to return (default 10)
        search: Optional search query string
        category: Optional category name to filter by
    """

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        data = await mm.get_transactions(limit=limit, search=search or "")
        all_txs = data.get("allTransactions", {}).get("results", [])
        if category:
            all_txs = [
                t
                for t in all_txs
                if t.get("category", {}).get("name", "").lower() == category.lower()
            ]
        simplified = [
            {
                "id": tx.get("id"),
                "date": tx.get("date"),
                "merchant": tx.get("merchant", {}).get("name"),
                "amount": tx.get("amount"),
                "category": tx.get("category", {}).get("name"),
                "notes": tx.get("notes"),
                "pending": tx.get("pending"),
                "goal_id": tx.get("goal", {}).get("id") if tx.get("goal") else None,
            }
            for tx in all_txs
        ]
        return {"transactions": simplified, "count": len(simplified)}

    return await _retry_on_auth(_call)


@mcp.tool("get_monarch_account_transactions")
async def get_monarch_account_transactions(
    account_id: str,
    limit: int = 100,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """Retrieve transactions for a specific account.

    Args:
        account_id: ID of the account
        limit: Max transactions to fetch before filtering (default 100)
        start_date: Optional start date in YYYY-MM-DD format
        end_date: Optional end date in YYYY-MM-DD format
    """

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        if start_date:
            datetime.strptime(start_date, "%Y-%m-%d")
        if end_date:
            datetime.strptime(end_date, "%Y-%m-%d")

        data = await mm.get_transactions(limit=limit * 2)
        all_txs = data.get("allTransactions", {}).get("results", [])

        filtered = [
            tx for tx in all_txs if tx.get("account", {}).get("id") == account_id
        ]
        if start_date or end_date:
            filtered = [
                tx
                for tx in filtered
                if (not start_date or tx.get("date", "") >= start_date)
                and (not end_date or tx.get("date", "") <= end_date)
            ]
        filtered = filtered[:limit]

        simplified = [
            {
                "id": tx.get("id"),
                "date": tx.get("date"),
                "merchant": tx.get("merchant", {}).get("name"),
                "amount": tx.get("amount"),
                "category": tx.get("category", {}).get("name"),
                "notes": tx.get("notes"),
                "pending": tx.get("pending"),
                "account": tx.get("account", {}).get("displayName"),
                "goal_id": tx.get("goal", {}).get("id") if tx.get("goal") else None,
            }
            for tx in filtered
        ]
        return {
            "transactions": simplified,
            "count": len(simplified),
            "account_id": account_id,
        }

    return await _retry_on_auth(_call)


@mcp.tool("get_monarch_transaction_details")
async def get_monarch_transaction_details(transaction_id: str) -> dict[str, Any]:
    """Get full details for a specific transaction."""

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        return await mm.get_transaction_details(transaction_id)

    return await _retry_on_auth(_call)


@mcp.tool("create_monarch_transaction")
async def create_monarch_transaction(
    date: str,
    account_id: str,
    amount: float,
    merchant_name: str,
    category_id: str,
    notes: str = "",
) -> dict[str, Any]:
    """Create a new manual transaction.

    Args:
        date: Transaction date in YYYY-MM-DD format
        account_id: ID of the account
        amount: Transaction amount
        merchant_name: Name of the merchant
        category_id: ID of the category
        notes: Optional notes
    """

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        datetime.strptime(date, "%Y-%m-%d")
        return await mm.create_transaction(
            date=date,
            account_id=account_id,
            amount=amount,
            merchant_name=merchant_name,
            category_id=category_id,
            notes=notes,
        )

    return await _retry_on_auth(_call)


@mcp.tool("update_monarch_transaction")
async def update_monarch_transaction(
    transaction_id: str,
    notes: Optional[str] = None,
    category_id: Optional[str] = None,
    merchant_name: Optional[str] = None,
    amount: Optional[float] = None,
    date: Optional[str] = None,
    goal_id: Optional[str] = None,
) -> dict[str, Any]:
    """Update an existing transaction. Only provide fields that need to be updated.

    Args:
        transaction_id: ID of the transaction to update
        notes: Optional notes
        category_id: Category ID
        merchant_name: Merchant name
        amount: Transaction amount
        date: Transaction date in YYYY-MM-DD format
        goal_id: Goal ID to associate transaction with (use empty string to clear)
    """

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        if date:
            datetime.strptime(date, "%Y-%m-%d")
        return await mm.update_transaction(
            transaction_id=transaction_id,
            notes=notes,
            category_id=category_id,
            merchant_name=merchant_name,
            amount=amount,
            date=date,
            goal_id=goal_id,
        )

    return await _retry_on_auth(_call)


@mcp.tool("delete_monarch_transaction")
async def delete_monarch_transaction(transaction_id: str) -> dict[str, Any]:
    """Delete a transaction."""

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        success = await mm.delete_transaction(transaction_id)
        return {"success": success}

    return await _retry_on_auth(_call)


@mcp.tool("get_monarch_transaction_splits")
async def get_monarch_transaction_splits(transaction_id: str) -> dict[str, Any]:
    """Get split details for a transaction."""

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        return await mm.get_transaction_splits(transaction_id)

    return await _retry_on_auth(_call)


@mcp.tool("update_monarch_transaction_splits")
async def update_monarch_transaction_splits(
    transaction_id: str,
    splits: list[dict[str, Any]],
) -> dict[str, Any]:
    """Update split details for a transaction.

    Args:
        transaction_id: ID of the transaction to split
        splits: List of split dicts, each with amount (float), category_id (str),
                and optional notes (str).
    """

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        return await mm.update_transaction_splits(transaction_id, splits)

    return await _retry_on_auth(_call)


# ---------------------------------------------------------------------------
# Budget & goal tools
# ---------------------------------------------------------------------------


@mcp.tool("get_monarch_budgets")
async def get_monarch_budgets(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """Retrieve budget status and remaining amounts.

    Args:
        start_date: Start date in YYYY-MM-DD format (default: last month)
        end_date: End date in YYYY-MM-DD format (default: next month)
    """

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        if start_date:
            datetime.strptime(start_date, "%Y-%m-%d")
        if end_date:
            datetime.strptime(end_date, "%Y-%m-%d")
        return await mm.get_budgets(start_date=start_date, end_date=end_date)

    return await _retry_on_auth(_call)


@mcp.tool("set_monarch_budget_amount")
async def set_monarch_budget_amount(
    amount: float,
    category_id: str,
    start_date: str,
    apply_to_future: bool = False,
) -> dict[str, Any]:
    """Set or delete budget amount for a category.

    Args:
        amount: Budget amount (set to 0.0 to delete/clear the budget)
        category_id: ID of the category
        start_date: Start date in YYYY-MM-DD format (usually first of month)
        apply_to_future: Whether to apply to future months
    """

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        datetime.strptime(start_date, "%Y-%m-%d")
        return await mm.set_budget_amount(
            amount=amount,
            category_id=category_id,
            start_date=start_date,
            apply_to_future=apply_to_future,
        )

    return await _retry_on_auth(_call)


@mcp.tool("get_monarch_goals")
async def get_monarch_goals(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """Retrieve financial goals (v2) with planned and actual contributions.

    Args:
        start_date: Start date in YYYY-MM-DD format (default: last month)
        end_date: End date in YYYY-MM-DD format (default: next month)
    """

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        if start_date:
            datetime.strptime(start_date, "%Y-%m-%d")
        if end_date:
            datetime.strptime(end_date, "%Y-%m-%d")
        data = await mm.get_budgets(
            start_date=start_date,
            end_date=end_date,
            use_v2_goals=True,
            use_legacy_goals=False,
        )
        goals = data.get("goalsV2", [])
        simplified = [
            {
                "id": g.get("id"),
                "name": g.get("name"),
                "priority": g.get("priority"),
                "completed_at": g.get("completedAt"),
                "archived_at": g.get("archivedAt"),
                "planned_contributions": g.get("plannedContributions", []),
                "monthly_summaries": g.get("monthlyContributionSummaries", []),
            }
            for g in goals
        ]
        return {"goals": simplified, "count": len(simplified)}

    return await _retry_on_auth(_call)


# ---------------------------------------------------------------------------
# Category & tag tools
# ---------------------------------------------------------------------------


@mcp.tool("get_monarch_categories")
async def get_monarch_categories() -> dict[str, Any]:
    """List all transaction categories."""

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        data = await mm.get_transaction_categories()
        categories = data.get("categories", [])
        simplified = [
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "group": c.get("group", {}).get("name"),
                "type": c.get("group", {}).get("type"),
                "is_system": c.get("isSystemCategory"),
            }
            for c in categories
        ]
        return {"categories": simplified, "count": len(simplified)}

    return await _retry_on_auth(_call)


@mcp.tool("get_monarch_category_groups")
async def get_monarch_category_groups() -> dict[str, Any]:
    """List all transaction category groups."""

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        data = await mm.get_transaction_category_groups()
        groups = data.get("categoryGroups", [])
        simplified = [
            {"id": g.get("id"), "name": g.get("name"), "type": g.get("type")}
            for g in groups
        ]
        return {"category_groups": simplified, "count": len(simplified)}

    return await _retry_on_auth(_call)


@mcp.tool("create_monarch_category")
async def create_monarch_category(
    name: str,
    group_id: str,
    icon: str = "❓",
) -> dict[str, Any]:
    """Create a new transaction category.

    Args:
        name: Category name
        group_id: ID of the category group (use get_monarch_category_groups)
        icon: Emoji icon for the category
    """

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        return await mm.create_transaction_category(
            group_id=group_id, transaction_category_name=name, icon=icon
        )

    return await _retry_on_auth(_call)


@mcp.tool("delete_monarch_transaction_category")
async def delete_monarch_transaction_category(category_id: str) -> dict[str, Any]:
    """Delete a transaction category."""

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        success = await mm.delete_transaction_category(category_id)
        return {"success": success}

    return await _retry_on_auth(_call)


@mcp.tool("delete_monarch_transaction_categories")
async def delete_monarch_transaction_categories(
    category_ids: list[str],
) -> dict[str, Any]:
    """Delete multiple transaction categories."""

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        results = await mm.delete_transaction_categories(category_ids)
        return {"results": results}

    return await _retry_on_auth(_call)


@mcp.tool("get_monarch_tags")
async def get_monarch_tags() -> dict[str, Any]:
    """List all transaction tags."""

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        data = await mm.get_transaction_tags()
        tags = data.get("householdTransactionTags", [])
        return {"tags": tags, "count": len(tags)}

    return await _retry_on_auth(_call)


@mcp.tool("create_monarch_tag")
async def create_monarch_tag(name: str, color: str) -> dict[str, Any]:
    """Create a new transaction tag. Color should be a hex code (e.g. #FF0000)."""

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        return await mm.create_transaction_tag(name, color)

    return await _retry_on_auth(_call)


@mcp.tool("set_monarch_transaction_tags")
async def set_monarch_transaction_tags(
    transaction_id: str,
    tag_ids: list[str],
) -> dict[str, Any]:
    """Set tags for a transaction (overwrites existing tags)."""

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        return await mm.set_transaction_tags(transaction_id, tag_ids)

    return await _retry_on_auth(_call)


# ---------------------------------------------------------------------------
# Analytics tools
# ---------------------------------------------------------------------------


@mcp.tool("get_monarch_cashflow")
async def get_monarch_cashflow(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """Analyze cashflow (income vs expenses). Dates in YYYY-MM-DD format."""

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        data = await mm.get_cashflow(start_date=start_date, end_date=end_date)
        summary_list = data.get("summary", [])
        summary_data: dict[str, Any] = {}
        if summary_list and isinstance(summary_list, list) and len(summary_list) > 0:
            summary_data = summary_list[0].get("summary", {})
        return {
            "income": summary_data.get("sumIncome"),
            "expenses": summary_data.get("sumExpense"),
            "savings": summary_data.get("savings"),
            "savings_rate": summary_data.get("savingsRate"),
        }

    return await _retry_on_auth(_call)


@mcp.tool("get_monarch_spending_summary")
async def get_monarch_spending_summary() -> dict[str, Any]:
    """Retrieve summary of transaction aggregates (income, expense, savings)."""

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        data = await mm.get_transactions_summary()
        aggregates = data.get("aggregates", [])
        if aggregates and isinstance(aggregates, list) and len(aggregates) > 0:
            return aggregates[0].get("summary", {})
        return {}

    return await _retry_on_auth(_call)


@mcp.tool("get_monarch_spending_by_category")
async def get_monarch_spending_by_category(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """Analyze spending patterns by category. Dates in YYYY-MM-DD format."""

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        if start_date:
            datetime.strptime(start_date, "%Y-%m-%d")
        if end_date:
            datetime.strptime(end_date, "%Y-%m-%d")
        data = await mm.get_cashflow(start_date=start_date, end_date=end_date)

        spending: list[dict[str, Any]] = []
        for group_data in data.get("byCategoryGroup", []):
            group_by = group_data.get("groupBy", {})
            category_group = group_by.get("categoryGroup", {})
            group_name = category_group.get("name")
            group_type = category_group.get("type")
            summary = group_data.get("summary", {})
            amount = summary.get("sum", 0)
            if group_type == "expense" and amount < 0:
                spending.append(
                    {"category_group": group_name, "type": group_type, "amount": abs(amount)}
                )
        spending.sort(key=lambda x: x.get("amount", 0), reverse=True)
        return {
            "spending_by_category": spending,
            "start_date": start_date,
            "end_date": end_date,
            "total_categories": len(spending),
        }

    return await _retry_on_auth(_call)


# ---------------------------------------------------------------------------
# Holdings & net-worth tools
# ---------------------------------------------------------------------------


@mcp.tool("get_monarch_holdings")
async def get_monarch_holdings(account_id: str) -> dict[str, Any]:
    """Retrieve investment holdings for a specific account."""

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        data = await mm.get_account_holdings(account_id)  # type: ignore[arg-type]
        holdings: list[dict[str, Any]] = []
        portfolio = data.get("portfolio", {})
        agg = portfolio.get("aggregateHoldings", {})
        for edge in agg.get("edges", []):
            node = edge.get("node", {})
            security = node.get("security", {})
            holdings.append(
                {
                    "name": security.get("name") or "Unknown",
                    "ticker": security.get("ticker"),
                    "quantity": node.get("quantity"),
                    "price": security.get("currentPrice"),
                    "value": node.get("totalValue"),
                    "basis": node.get("basis"),
                    "return_dollars": node.get("securityPriceChangeDollars"),
                    "return_percent": node.get("securityPriceChangePercent"),
                    "type": security.get("type"),
                }
            )
        return {"holdings": holdings, "count": len(holdings)}

    return await _retry_on_auth(_call)


@mcp.tool("get_monarch_net_worth_history")
async def get_monarch_net_worth_history(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    account_type: Optional[str] = None,
) -> dict[str, Any]:
    """Retrieve net worth history (aggregate snapshots). Dates in YYYY-MM-DD format."""

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        if start_date:
            datetime.strptime(start_date, "%Y-%m-%d")
        if end_date:
            datetime.strptime(end_date, "%Y-%m-%d")
        return await mm.get_aggregate_snapshots(
            start_date=start_date,  # type: ignore[arg-type]
            end_date=end_date,  # type: ignore[arg-type]
            account_type=account_type,
        )

    return await _retry_on_auth(_call)


@mcp.tool("get_monarch_snapshots_by_account_type")
async def get_monarch_snapshots_by_account_type(
    start_date: str,
    timeframe: str = "month",
) -> dict[str, Any]:
    """Retrieve snapshots of net values grouped by account type.

    Args:
        start_date: Start date in YYYY-MM-DD format
        timeframe: Aggregation period — "month" or "year" (default: "month")
    """

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        datetime.strptime(start_date, "%Y-%m-%d")
        if timeframe not in ("month", "year"):
            return {"error": "timeframe must be either 'month' or 'year'"}
        return await mm.get_account_snapshots_by_type(start_date, timeframe)

    return await _retry_on_auth(_call)


@mcp.tool("get_monarch_account_history")
async def get_monarch_account_history(account_id: str) -> dict[str, Any]:
    """Retrieve historical balances for a specific account."""

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        data = await mm.get_account_history(account_id)  # type: ignore[arg-type]
        return {"history": data, "count": len(data) if isinstance(data, list) else 0}

    return await _retry_on_auth(_call)


# ---------------------------------------------------------------------------
# Recurring & institutions
# ---------------------------------------------------------------------------


@mcp.tool("get_monarch_recurring_transactions")
async def get_monarch_recurring_transactions(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict[str, Any]:
    """Retrieve upcoming recurring transactions (bills, subscriptions). Dates in YYYY-MM-DD."""

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        return await mm.get_recurring_transactions(start_date=start_date, end_date=end_date)

    return await _retry_on_auth(_call)


@mcp.tool("get_monarch_institutions")
async def get_monarch_institutions() -> dict[str, Any]:
    """List all connected financial institutions."""

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        data = await mm.get_institutions()
        credentials = data.get("credentials", [])
        simplified = [
            {
                "id": (cred.get("institution") or {}).get("id"),
                "name": (cred.get("institution") or {}).get("name"),
                "status": (cred.get("institution") or {}).get("status"),
                "updated_at": cred.get("displayLastUpdatedAt"),
                "data_provider": cred.get("dataProvider"),
            }
            for cred in credentials
        ]
        return {"institutions": simplified, "count": len(simplified)}

    return await _retry_on_auth(_call)


# ---------------------------------------------------------------------------
# Refresh tools
# ---------------------------------------------------------------------------


@mcp.tool("refresh_monarch_data")
async def refresh_monarch_data() -> dict[str, Any]:
    """Trigger a refresh of data from connected institutions (non-blocking).

    Use check_monarch_refresh_status to check if refresh is complete.
    """

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        accounts_data = await mm.get_accounts()
        accounts = accounts_data.get("accounts", [])
        account_ids = [acc["id"] for acc in accounts]
        if not account_ids:
            return {"status": "No accounts found to refresh"}
        await mm.request_accounts_refresh(account_ids)
        return {
            "status": "Refresh initiated",
            "account_count": len(account_ids),
            "message": "Use check_monarch_refresh_status to check completion",
        }

    return await _retry_on_auth(_call)


@mcp.tool("check_monarch_refresh_status")
async def check_monarch_refresh_status() -> dict[str, Any]:
    """Check the status of account data refresh."""

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        is_complete = await mm.is_accounts_refresh_complete()
        accounts_data = await mm.get_accounts()
        accounts = accounts_data.get("accounts", [])
        statuses = []
        for acc in accounts:
            credential = acc.get("credential") or {}
            institution = credential.get("institution") or {}
            statuses.append(
                {
                    "id": acc.get("id"),
                    "name": acc.get("displayName"),
                    "type": acc.get("type"),
                    "sync_disabled": acc.get("syncDisabled"),
                    "updated_at": acc.get("updatedAt"),
                    "data_provider": credential.get("dataProvider"),
                    "institution": institution.get("name"),
                }
            )
        return {
            "refresh_complete": is_complete,
            "status": "Complete" if is_complete else "In progress",
            "accounts": statuses,
            "total_accounts": len(statuses),
        }

    return await _retry_on_auth(_call)


# ---------------------------------------------------------------------------
# Balance history upload
# ---------------------------------------------------------------------------


@mcp.tool("upload_monarch_account_balance_history")
async def upload_monarch_account_balance_history(
    account_id: str,
    csv_content: str,
) -> dict[str, Any]:
    """Upload historical balance data for a manual account.

    CSV format: two columns ``date,balance`` (YYYY-MM-DD, numeric).

    Args:
        account_id: ID of the account to upload history for
        csv_content: CSV string with date,balance columns
    """

    async def _call(mm: MonarchMoney) -> dict[str, Any]:
        if not csv_content.strip():
            return {"error": "CSV content cannot be empty"}
        lines = csv_content.strip().split("\n")
        if len(lines) < 2:
            return {"error": "CSV must have at least a header row and one data row"}
        header = lines[0].strip().lower()
        if "date" not in header or "balance" not in header:
            return {"error": "CSV must have 'date' and 'balance' columns"}
        await mm.upload_account_balance_history(account_id, csv_content)
        data_rows = len(lines) - 1
        return {
            "success": True,
            "account_id": account_id,
            "rows_uploaded": data_rows,
            "message": f"Successfully uploaded {data_rows} balance records",
        }

    return await _retry_on_auth(_call)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def run(
    transport: str = "stdio",
    host: str = "0.0.0.0",
    port: int = DEFAULT_HTTP_PORT,
) -> None:  # pragma: no cover - integration entrypoint
    """Run the Monarch MCP server with the specified transport."""
    if transport == "streamable-http":
        mcp.run(
            transport="streamable-http",
            host=host,
            port=port,
            json_response=True,
            stateless_http=True,
            uvicorn_config={"access_log": False},
        )
    else:
        mcp.run(transport="stdio")


def main() -> None:  # pragma: no cover - CLI helper
    import argparse

    parser = argparse.ArgumentParser(description="Monarch Money MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport protocol to use",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host to bind HTTP server to",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help="Port for HTTP server",
    )
    args = parser.parse_args()
    run(args.transport, args.host, args.port)


if __name__ == "__main__":  # pragma: no cover
    main()
