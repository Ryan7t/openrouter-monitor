from __future__ import annotations

from datetime import datetime, time

from .models import AccountCredits, BalanceSnapshot, KeyMetrics, KeyTrendInfo, QuietHoursConfig, UserThresholds
from .utils import format_currency, format_hhmm, format_local_datetime, format_short_datetime

LABEL_QUIET_HOURS = "免打扰时段"


def build_help_message() -> str:
    lines = [
        "OpenRouter Monitor 使用指南",
        "",
        "查看报告",
        "  /详细 — 查看所有 Key 的余额和用量",
        "  /趋势 — 查看所有 Key 的余额变化趋势（最近7天）",
        "",
        "管理 Key",
        "  /绑定 <Key> <备注名> — 添加一个 Key（备注名可选）",
        "  /删除 <备注名或完整Key> — 删除已绑定的 Key",
        "",
        "推送设置",
        "  /配置 推送时间 09:00 — 设置每日报告的推送时间",
        "  /配置 间隔 30 — 开启间隔推送，每 30 分钟自动推送一次余额报告",
        "  /配置 间隔 关闭 — 关闭间隔推送",
        "  /配置 间隔静默 23:00 08:00 — 设置免打扰时段，该时段内暂停间隔推送",
        "  /配置 间隔静默 关闭 — 关闭免打扰时段",
        "  说明: 每日推送在固定时间发送一次汇总；间隔推送在此基础上额外按设定频率推送。",
        "  免打扰时段仅影响间隔推送，不影响每日推送。",
        "",
        "余额提醒（余额降到设定值时自动通知）",
        "  /配置 警告 10 — 余额低于 $10 时提醒",
        "  /配置 危险 5 — 余额低于 $5 时提醒",
        "  /配置 严重 1 — 余额低于 $1 时提醒",
        "",
        "其他",
        "  /配置 — 查看当前所有设置",
        "  /帮助 — 查看本指南",
        "",
        "群聊中请先 @机器人 再发送指令，私聊直接发送即可。",
        "也支持英文指令：/detail /trend /bind /delete /config /help",
    ]
    return "\n".join(lines)


def build_no_keys_message() -> str:
    return "你还没有绑定任何 Key，请发送 /绑定 <Key> 来添加。"


def _format_key_line(alias: str | None, masked_key: str) -> str:
    if alias:
        return f"Key: {alias}（{masked_key}）"
    return f"Key: {masked_key}"


def format_push_interval_status(push_interval_minutes: int | None) -> str:
    if push_interval_minutes is None:
        return "未开启"
    return f"每 {push_interval_minutes} 分钟一次"


def format_interval_quiet_hours_status(interval_quiet_hours: QuietHoursConfig | None) -> str:
    if interval_quiet_hours is None:
        return "未开启"
    return f"{format_hhmm(interval_quiet_hours.start)} - {format_hhmm(interval_quiet_hours.end)}"


def build_bind_success_message(
    alias: str | None,
    masked_key: str,
    push_time: time,
    push_interval_minutes: int | None,
    interval_quiet_hours: QuietHoursConfig | None,
    thresholds: UserThresholds,
    existed: bool,
) -> str:
    title = "绑定成功" if not existed else "更新成功"
    lines = [title, _format_key_line(alias, masked_key)]
    lines.extend(
        [
            "",
            f"每日推送时间: {format_hhmm(push_time)}",
            f"间隔推送: {format_push_interval_status(push_interval_minutes)}",
            f"免打扰时段: {format_interval_quiet_hours_status(interval_quiet_hours)}",
            f"余额提醒: 警告 {format_currency(thresholds.warning)}"
            f" / 危险 {format_currency(thresholds.danger)}"
            f" / 严重 {format_currency(thresholds.critical)}",
            "",
            "余额监控已开启，会在余额不足时自动提醒你。",
            "修改设置请发送 /配置，查看帮助请发送 /帮助。",
        ]
    )
    return "\n".join(lines)


def build_delete_success_message(alias: str | None, masked_key: str, push_enabled: bool) -> str:
    lines = ["删除成功", _format_key_line(alias, masked_key)]
    if push_enabled:
        lines.append("其余 Key 的监控和推送不受影响。")
    else:
        lines.append("当前已无绑定的 Key，每日推送已自动关闭。")
    return "\n".join(lines)


def build_config_message(
    push_time: time,
    push_interval_minutes: int | None,
    interval_quiet_hours: QuietHoursConfig | None,
    thresholds: UserThresholds,
    push_enabled: bool,
    key_count: int,
) -> str:
    lines = [
        "当前设置",
        f"已绑定 Key: {key_count} 个",
        f"每日推送: {'已开启' if push_enabled else '未开启'}",
        f"每日推送时间: {format_hhmm(push_time)}",
        f"间隔推送: {format_push_interval_status(push_interval_minutes)}",
    ]
    if push_interval_minutes is not None:
        lines.append(f"{LABEL_QUIET_HOURS}: {format_interval_quiet_hours_status(interval_quiet_hours)}")
    lines.extend(
        [
            "",
            "余额提醒:",
            f"警告 — 余额低于 {format_currency(thresholds.warning)} 时提醒",
            f"危险 — 余额低于 {format_currency(thresholds.danger)} 时提醒",
            f"严重 — 余额低于 {format_currency(thresholds.critical)} 时提醒",
        ]
    )
    return "\n".join(lines)


def build_config_updated_message(label: str, value: str) -> str:
    return f"设置已更新: {label} — {value}"


def build_detail_report(
    checked_at: datetime,
    key_sections: list[str],
    push_time: time,
    push_interval_minutes: int | None,
    interval_quiet_hours: QuietHoursConfig | None,
) -> str:
    lines = [
        "OpenRouter 余额报告",
        f"查询时间: {format_local_datetime(checked_at)}",
        f"每日推送时间: {format_hhmm(push_time)} | 间隔推送: {format_push_interval_status(push_interval_minutes)} | 免打扰时段: {format_interval_quiet_hours_status(interval_quiet_hours)} | 已绑定: {len(key_sections)} 个",
        "",
    ]
    lines.append("\n\n".join(key_sections))
    return "\n".join(lines)


def build_detail_key_section(
    alias: str | None,
    masked_key: str,
    metrics: KeyMetrics | None,
    key_error: str | None,
    credits: AccountCredits | None,
    credits_error: str | None,
) -> str:
    divider = "——————————"
    if alias:
        header = f"【{alias}】{masked_key}"
    else:
        header = masked_key
    lines = [divider, header]

    if credits is None:
        if credits_error:
            lines.extend(["", f"账户余额查询失败: {credits_error}"])
    else:
        remaining = credits.total_credits - credits.total_usage
        lines.extend(
            [
                "",
                f"账户充值: {format_currency(credits.total_credits)}",
                f"账户已用: {format_currency(credits.total_usage)}",
                f"账户余额: {format_currency(remaining)}",
            ]
        )

    if metrics is None:
        lines.extend(["", f"Key 查询失败: {key_error or '未知错误'}"])
    else:
        lines.extend(_format_key_details(metrics))

    return "\n".join(lines)


def _format_key_details(metrics: KeyMetrics) -> list[str]:
    lines: list[str] = []

    if metrics.label:
        lines.append(f"标签: {metrics.label}")
    if metrics.is_free_tier:
        lines.append("类型: 免费 Key")
    if metrics.expires_at:
        lines.append(f"到期时间: {_format_expires_at(metrics.expires_at)}")

    if metrics.limit is not None:
        lines.append("")
        lines.append(f"消费上限: {format_currency(metrics.limit)}")
        lines.append(f"已消费: {format_currency(metrics.usage)}")
        if metrics.limit_remaining is not None:
            lines.append(f"剩余额度: {format_currency(metrics.limit_remaining)}")
        if metrics.limit_reset:
            lines.append(f"额度重置: {metrics.limit_reset}")
    else:
        lines.append("")
        lines.append("消费上限: 未设置")
        lines.append(f"已消费: {format_currency(metrics.usage)}")

    lines.append("")
    lines.append(
        f"用量: 今日 {format_currency(metrics.usage_daily)}"
        f" | 本周 {format_currency(metrics.usage_weekly)}"
        f" | 本月 {format_currency(metrics.usage_monthly)}"
    )

    if metrics.byok_usage > 0 or metrics.include_byok_in_limit:
        byok_line = f"BYOK: {format_currency(metrics.byok_usage)}"
        if metrics.include_byok_in_limit:
            byok_line += "（已计入消费上限）"
        lines.append(byok_line)

    return lines


def _format_expires_at(raw: str) -> str:
    try:
        dt = datetime.fromisoformat(raw)
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return raw


def build_threshold_alert_message(
    alias: str | None,
    masked_key: str,
    level: str,
    threshold_amount: float,
    metrics: KeyMetrics,
    checked_at: datetime,
) -> str:
    level_label = {"warning": "警告", "danger": "危险", "critical": "严重"}.get(level, level)
    lines = [
        f"【{level_label}】余额不足提醒",
        _format_key_line(alias, masked_key),
        f"剩余额度: {format_currency(metrics.limit_remaining)}",
        f"提醒线: {format_currency(threshold_amount)}",
        f"检测时间: {format_local_datetime(checked_at)}",
    ]
    if metrics.limit is not None:
        lines.append(f"消费限额上限: {format_currency(metrics.limit)}")
    lines.append(f"累计消费: {format_currency(metrics.usage)}")
    return "\n".join(lines)


def build_failure_alert_message(
    alias: str | None,
    masked_key: str,
    error_message: str,
    consecutive_failures: int,
    checked_at: datetime,
    critical: bool,
) -> str:
    severity = "紧急" if critical else "异常"
    return "\n".join(
        [
            f"【{severity}】Key 检测失败",
            _format_key_line(alias, masked_key),
            f"失败原因: {error_message}",
            f"连续失败: {consecutive_failures} 次",
            f"检测时间: {format_local_datetime(checked_at)}",
        ]
    )


def build_trend_report(
    checked_at: datetime,
    key_trends: list[KeyTrendInfo],
) -> str:
    lines = [
        "OpenRouter 余额趋势报告",
        f"查询时间: {format_local_datetime(checked_at)}",
        f"已绑定: {len(key_trends)} 个 Key",
        "",
    ]

    if not key_trends:
        lines.append("暂无 Key 数据。")
        return "\n".join(lines)

    trend_sections: list[str] = []
    for trend in key_trends:
        trend_sections.append(_build_trend_key_section(trend))

    lines.append("\n\n".join(trend_sections))
    return "\n".join(lines)


def _build_trend_key_section(trend: KeyTrendInfo) -> str:
    divider = "——————————"
    if trend.alias:
        header = f"【{trend.alias}】{trend.masked_key}"
    else:
        header = trend.masked_key

    lines = [divider, header, ""]
    lines.append(f"当前余额: {format_currency(trend.current_balance)}")

    if trend.daily_consumption is not None:
        if trend.daily_consumption > 0:
            lines.append(f"日均消耗: {format_currency(trend.daily_consumption)}")
        elif trend.daily_consumption < 0:
            lines.append(f"日均增长: {format_currency(-trend.daily_consumption)}")
        else:
            lines.append("日均消耗: $0.00（无变化）")
    else:
        lines.append("日均消耗: 数据不足")

    if trend.estimated_days is not None:
        if trend.estimated_days <= 0:
            lines.append("预计可用: 余额已耗尽")
        else:
            lines.append(f"预计可用: 约 {trend.estimated_days:.1f} 天")
    elif trend.daily_consumption is not None and trend.daily_consumption <= 0:
        lines.append("预计可用: 余额在增长或持平，无法估算")
    else:
        lines.append("预计可用: 数据不足")

    lines.append("")
    lines.append("最近7天快照:")

    if trend.snapshots:
        for snapshot in trend.snapshots:
            lines.append(f"  {format_short_datetime(snapshot.timestamp)} — {format_currency(snapshot.balance)}")
    else:
        lines.append("  暂无历史记录")

    return "\n".join(lines)
