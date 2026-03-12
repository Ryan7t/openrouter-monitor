from __future__ import annotations

from datetime import datetime, time

from .models import AccountCredits, KeyMetrics, UserThresholds
from .utils import format_currency, format_hhmm, format_local_datetime


def build_help_message() -> str:
    return "\n".join(
        [
            "OpenRouter Monitor 使用指南",
            "",
            "查看报告",
            "  /详细 — 查看所有 Key 的余额和用量",
            "",
            "管理 Key",
            "  /绑定 <Key> <备注名> — 添加一个 Key（备注名可选）",
            "  /删除 <备注名或完整Key> — 删除已绑定的 Key",
            "",
            "个人设置",
            "  /配置 — 查看当前设置",
            "  /配置 推送时间 09:00 — 修改每日推送时间",
            "  余额提醒（分三级，余额降到设定金额时通知你）：",
            "  /配置 警告 10 — 余额低于 $10 时，轻度提醒",
            "  /配置 危险 5 — 余额低于 $5 时，较强提醒",
            "  /配置 严重 1 — 余额低于 $1 时，紧急提醒",
            "",
            "/帮助 — 查看本指南",
            "",
            "群聊中请先 @机器人 再发送指令，私聊直接发送即可。",
            "也支持英文指令：/detail /bind /delete /config /help",
        ]
    )


def build_no_keys_message() -> str:
    return "你还没有绑定任何 Key，请发送 /绑定 <Key> 来添加。"


def _format_key_line(alias: str | None, masked_key: str) -> str:
    if alias:
        return f"Key: {alias}（{masked_key}）"
    return f"Key: {masked_key}"


def build_bind_success_message(
    alias: str | None,
    masked_key: str,
    push_time: time,
    thresholds: UserThresholds,
    existed: bool,
) -> str:
    title = "绑定成功" if not existed else "更新成功"
    lines = [title, _format_key_line(alias, masked_key)]
    lines.extend(
        [
            "",
            f"每日推送时间: {format_hhmm(push_time)}",
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


def build_config_message(push_time: time, thresholds: UserThresholds, push_enabled: bool, key_count: int) -> str:
    return "\n".join(
        [
            "当前设置",
            f"已绑定 Key: {key_count} 个",
            f"每日推送: {'已开启' if push_enabled else '未开启'}",
            f"推送时间: {format_hhmm(push_time)}",
            "",
            "余额提醒:",
            f"警告 — 余额低于 {format_currency(thresholds.warning)} 时提醒",
            f"危险 — 余额低于 {format_currency(thresholds.danger)} 时提醒",
            f"严重 — 余额低于 {format_currency(thresholds.critical)} 时提醒",
        ]
    )


def build_config_updated_message(label: str, value: str) -> str:
    return f"设置已更新: {label} — {value}"


def build_detail_report(
    checked_at: datetime,
    key_sections: list[str],
    push_time: time,
) -> str:
    lines = [
        "OpenRouter 余额报告",
        f"查询时间: {format_local_datetime(checked_at)}",
        f"推送时间: {format_hhmm(push_time)} | 已绑定: {len(key_sections)} 个",
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
