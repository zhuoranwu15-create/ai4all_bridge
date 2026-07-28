"""日报指标共用的产品/渠道 SQL 过滤辅助。"""

from typing import List, Tuple

from nearline.reporting.scope import ReportScope


def account_clause(scope: ReportScope, alias: str = "a") -> Tuple[str, List[str]]:
    """返回账号归属作用域 SQL 及参数；渠道表示账号初始/归属渠道。"""
    return f"{alias}.app_id = ? AND {alias}.channel = ?", [scope.app_id, scope.channel]


def event_clause(
    scope: ReportScope,
    event_alias: str,
    account_alias: str = "a",
) -> Tuple[str, List[str]]:
    """返回事件级渠道 SQL；旧事实缺渠道时回落账号归属渠道。"""
    return (
        f"{account_alias}.app_id = ? AND "
        f"COALESCE({event_alias}.channel, {account_alias}.channel) = ?",
        [scope.app_id, scope.channel],
    )
