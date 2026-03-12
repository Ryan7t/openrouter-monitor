from __future__ import annotations

import argparse
import logging
import sys

from .bot import FeishuLongConnectionApp
from .config import ConfigError, load_config
from .service import MonitorService, UserCommandError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="OpenRouter 用户中心化飞书机器人")
    parser.add_argument("--config", required=True, help="YAML 配置文件路径。")
    parser.add_argument(
        "--once",
        action="store_true",
        help="立即执行一次阈值扫描并退出。",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="输出某个用户的 /详细 报告并退出。",
    )
    parser.add_argument(
        "--push-text",
        help="给指定用户发送一条主动消息并退出。",
    )
    parser.add_argument(
        "--push-detail",
        action="store_true",
        help="立即主动发送一次 /详细 报告并退出。",
    )
    parser.add_argument(
        "--all-users",
        action="store_true",
        help="与 --push-detail 配合使用，向全部已绑定用户发送 /详细。",
    )
    parser.add_argument(
        "--user-open-id",
        help="与 --inspect、--push-text 或 --push-detail 配合使用，指定目标用户 open_id。",
    )
    args = parser.parse_args(argv)

    selected_modes = [bool(args.once), bool(args.inspect), bool(args.push_text), bool(args.push_detail)]
    if sum(selected_modes) > 1:
        parser.error("--once, --inspect, --push-text 和 --push-detail 只能四选一。")
    if args.all_users and not args.push_detail:
        parser.error("--all-users 只能和 --push-detail 一起使用。")
    if args.all_users and args.user_open_id:
        parser.error("--all-users 和 --user-open-id 不能同时使用。")

    configure_logging()
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        logging.error("Configuration error: %s", exc)
        return 2

    service = MonitorService(config)
    try:
        if args.inspect:
            open_id = service.resolve_stored_user_open_id(args.user_open_id)
            print(service.inspect_user(open_id))
            return 0
        if args.once:
            service.threshold_scan()
            print("阈值扫描完成。")
            return 0
        if args.push_text:
            open_id = args.user_open_id or service.resolve_stored_user_open_id()
            success = service.push_private_text(open_id, args.push_text)
            if success:
                print("主动消息发送成功。")
                return 0
            print("主动消息发送失败。")
            return 1
        if args.push_detail:
            if args.all_users:
                success_count, total_count = service.push_detail_for_all_users()
                print(f"已向 {success_count}/{total_count} 个已绑定用户发送 /详细 主动推送。")
                return 0 if success_count == total_count else 1

            open_id = service.resolve_stored_user_open_id(args.user_open_id)
            success = service.push_detail_for_user(open_id)
            if success:
                print(f"已向用户 {open_id} 发送 /详细 主动推送。")
                return 0
            print(f"向用户 {open_id} 发送 /详细 主动推送失败。")
            return 1
    except UserCommandError as exc:
        logging.error(str(exc))
        return 2

    app = FeishuLongConnectionApp(service)
    app.run_forever()
    return 0


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
