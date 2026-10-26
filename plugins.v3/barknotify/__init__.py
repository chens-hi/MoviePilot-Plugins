"""MoviePilot Bark 消息推送插件。

监听宿主 ``EventType.NoticeMessage`` 广播，把 MoviePilot 通知按用户配置
转发到 Bark（iOS 推送 App，https://api.day.app）。
配置页通过「用户数量」下拉控制表单式用户槽位的展示数（上限 10，
V3 表单不支持动态增删行），每个用户可独立配置 Bark key、通知类型、
生效时间段与强提醒。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app.sdk.events import Event, eventmanager
from app.sdk.logging import logger
from app.sdk.network import RequestUtils
from app.sdk.plugin import _PluginBase
from app.schemas.types import EventType, NotificationType

# Bark 默认公共服务器，支持自建替换
BARK_SEND_URL = "https://api.day.app"
# 成功判定：HTTP 200 且响应 JSON code == 200
BARK_SUCCESS_CODE = 200
# 详情页保留的推送记录条数
HISTORY_LIMIT = 20
# 用户槽位上限（V3 表单不支持动态增删行，用「用户数量」下拉控制实际展示数）
MAX_SLOTS = 10
# 未配置时的默认用户数量
DEFAULT_USER_COUNT = 5
# 「用户数量」下拉选项
COUNT_OPTIONS = [{"title": str(i), "value": i} for i in range(1, MAX_SLOTS + 1)]
# 通知类型选项（枚举 name 作为值，中文名展示）
TYPE_OPTIONS = [{"title": item.value, "value": item.name} for item in NotificationType]
# 合法类型 name 集合，用于防御性过滤
TYPE_NAMES = {item.name for item in NotificationType}
# 时间下拉选项：30 分钟粒度，"全天" 表示不限时段
TIME_OPTIONS = [{"title": "全天", "value": ""}] + [
    {"title": f"{hour:02d}:{minute:02d}", "value": f"{hour:02d}:{minute:02d}"}
    for hour in range(24)
    for minute in (0, 30)
]


def _parse_hhmm(text: str) -> int:
    """解析 HH:mm 为当日分钟数，非法输入抛 ValueError。"""
    parts = text.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"时间格式非法：{text}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"时间超出范围：{text}")
    return hour * 60 + minute


def parse_time_window(start: str, end: str) -> Optional[Tuple[int, int]]:
    """
    把起止时间 ``HH:mm`` 解析为 (起始分钟, 结束分钟)。

    返回 None 表示全天（任一为空、或起止相同）；起止跨天（起始大于结束）
    合法，表示从当日起始时间到次日结束时间。非法输入抛 ValueError。
    """
    if not start.strip() or not end.strip():
        return None
    begin, finish = _parse_hhmm(start), _parse_hhmm(end)
    if begin == finish:
        return None
    return begin, finish


def in_time_window(window: Optional[Tuple[int, int]], now_minutes: Optional[int] = None) -> bool:
    """
    判断当前时刻是否在生效时间段内。window 为 None 表示全天。
    """
    if window is None:
        return True
    if now_minutes is None:
        now = datetime.now()
        now_minutes = now.hour * 60 + now.minute
    start, end = window
    if start < end:
        return start <= now_minutes <= end
    # 跨天区间，如 22:00-07:00
    return now_minutes >= start or now_minutes <= end


class BarkNotify(_PluginBase):
    """Bark 消息推送插件。

    监听 ``EventType.NoticeMessage``，只处理未指定专用渠道的广播消息，
    按每个用户的通知类型、生效时间段与强提醒开关推送。
    """

    # 插件名称
    plugin_name = "Bark消息推送"
    # 插件描述
    plugin_desc = "多用户 Bark 消息推送，支持类型过滤、生效时段与强提醒。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/chens-hi/MoviePilot-Plugins/main/icons/barknotify.png"
    # 插件版本：必须与 package.v3.json 中该插件的 version 保持一致
    plugin_version = "1.3.6"
    # 插件作者
    plugin_author = "chens-hi"
    # 作者主页
    author_url = "https://github.com/chens-hi"
    # 插件标签
    plugin_label = "消息通知"
    # 插件配置项ID前缀
    plugin_config_prefix = "barknotify_"
    # 加载顺序
    plugin_order = 31
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _server = BARK_SEND_URL
    _sound: Optional[str] = None
    _users: List[Dict[str, Any]] = []

    def init_plugin(self, config: Optional[Dict[str, Any]] = None) -> None:
        """根据插件配置初始化运行状态，可重复初始化。"""
        config = config or {}
        # 记录启用状态变化：仅在"从未启用变为启用"时发测试消息，保存配置不重复发送
        was_enabled = self._enabled
        self._enabled = bool(config.get("enabled"))
        self._server = (config.get("server") or "").strip().rstrip("/") or BARK_SEND_URL
        self._sound = (config.get("sound") or "").strip() or None
        self._users = self._build_users(config)
        if self._enabled and not was_enabled and bool(config.get("test_on_enable", True)):
            self._send_test_messages()

    def _send_test_messages(self) -> None:
        """插件启用时向所有已配置用户发送测试推送，验证配置连通性。"""
        for user in self._users:
            payload: Dict[str, Any] = {
                "device_key": user["bark_key"],
                "title": "【Bark消息推送】测试消息",
                "body": f"{user['name']} 的推送配置正常（{datetime.now().strftime('%H:%M:%S')}）",
                "group": "Bark消息推送",
                "level": "active",
            }
            if self._sound:
                payload["sound"] = self._sound
            try:
                ok = self._send_bark(user, payload)
            except Exception as err:
                logger.error(f"Bark 测试消息发送异常（{user['name']}）：{err}")
                ok = False
            self._record_history(user["name"], "测试消息", "active", ok)

    def get_state(self) -> bool:
        """获取插件启用状态（需启用且至少配置了一个用户）。"""
        return self._enabled and bool(self._users)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """当前插件不注册远程命令。"""
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        """当前插件不注册后端 API。"""
        return []

    @staticmethod
    def _build_users(config: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        从表单槽位配置（u{n}_* 字段）解析生效用户列表。

        未启用的槽位跳过；启用但缺少名称或 Key 的槽位记日志跳过；
        通知类型为空表示全部类型；起止时间任一为空表示全天。
        """
        users: List[Dict[str, Any]] = []
        # 用户数量由配置的下拉决定，超出数量的槽位忽略（其配置仍保留，调回数量即恢复）
        try:
            user_count = int(config.get("user_count") or DEFAULT_USER_COUNT)
        except (TypeError, ValueError):
            user_count = DEFAULT_USER_COUNT
        user_count = max(1, min(user_count, MAX_SLOTS))
        for idx in range(1, user_count + 1):
            prefix = f"u{idx}_"
            if not config.get(f"{prefix}enabled"):
                continue
            name = (config.get(f"{prefix}name") or "").strip()
            bark_key = (config.get(f"{prefix}key") or "").strip()
            if not name or not bark_key:
                logger.warn(f"Bark 用户{idx} 已启用但名称或 Key 为空，已跳过")
                continue
            # 通知类型：下拉多选直接给出枚举 name；留空 = 全部
            types = [t for t in (config.get(f"{prefix}types") or [])
                     if t in TYPE_NAMES] or None
            start = (config.get(f"{prefix}start") or "").strip()
            end = (config.get(f"{prefix}end") or "").strip()
            window: Optional[Tuple[int, int]] = None
            if start or end:
                try:
                    window = parse_time_window(start, end)
                except ValueError as err:
                    logger.warn(f"Bark 用户{idx} 的时间段非法（{err}），按全天处理")
            users.append({
                "name": name,
                "bark_key": bark_key,
                "types": types,
                "window": window,
                "critical": bool(config.get(f"{prefix}critical")),
            })
        return users

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """拼装配置页面：全局设置 + 表单式用户槽位。"""
        defaults: Dict[str, Any] = {
            "enabled": False,
            "server": BARK_SEND_URL,
            "sound": "",
            "test_on_enable": True,
            "user_count": DEFAULT_USER_COUNT,
        }
        slots: List[dict] = []
        for idx in range(1, MAX_SLOTS + 1):
            prefix = f"u{idx}_"
            # 槽位开关跟随「用户数量」显隐，槽位字段同时受开关与数量控制
            switch_show = f"{{{{user_count >= {idx}}}}}"
            show = f"{{{{{prefix}enabled && user_count >= {idx}}}}}"
            defaults.update({
                f"{prefix}enabled": False,
                f"{prefix}name": "",
                f"{prefix}key": "",
                f"{prefix}types": [],
                f"{prefix}start": "",
                f"{prefix}end": "",
                f"{prefix}critical": False,
            })
            slots.extend([
                {
                    "component": "VRow",
                    "props": {"show": switch_show},
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 2},
                            "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": f"{prefix}enabled",
                                        "label": f"用户{idx}",
                                        "show": switch_show,
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 4},
                            "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": f"{prefix}name",
                                        "label": "名称",
                                        "placeholder": "如：我的手机",
                                        "show": show,
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 6},
                            "content": [
                                {
                                    "component": "VTextField",
                                    "props": {
                                        "model": f"{prefix}key",
                                        "label": "Bark Key",
                                        "placeholder": "Bark App 内复制",
                                        "show": show,
                                    },
                                }
                            ],
                        },
                    ],
                },
                {
                    "component": "VRow",
                    "props": {"show": show},
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 6},
                            "content": [
                                {
                                    "component": "VSelect",
                                    "props": {
                                        "model": f"{prefix}types",
                                        "label": "通知类型（可多选，留空接收全部）",
                                        "items": TYPE_OPTIONS,
                                        "multiple": True,
                                        "chips": True,
                                        "show": show,
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 6, "md": 2},
                            "content": [
                                {
                                    "component": "VSelect",
                                    "props": {
                                        "model": f"{prefix}start",
                                        "label": "生效开始",
                                        "items": TIME_OPTIONS,
                                        "show": show,
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 6, "md": 2},
                            "content": [
                                {
                                    "component": "VSelect",
                                    "props": {
                                        "model": f"{prefix}end",
                                        "label": "生效结束",
                                        "items": TIME_OPTIONS,
                                        "show": show,
                                    },
                                }
                            ],
                        },
                        {
                            "component": "VCol",
                            "props": {"cols": 12, "md": 2},
                            "content": [
                                {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": f"{prefix}critical",
                                        "label": "强提醒",
                                        "show": show,
                                    },
                                }
                            ],
                        },
                    ],
                },
                {"component": "VDivider", "props": {"class": "my-2", "show": switch_show}},
            ])

        def tip(label: str, desc: str) -> dict:
            # 标签定宽，保证多条说明左侧对齐
            return {
                "component": "div",
                "props": {"class": "d-flex"},
                "content": [
                    {
                        "component": "span",
                        "props": {
                            "class": "font-weight-bold flex-shrink-0",
                            "style": "width: 5em",
                        },
                        "text": label,
                    },
                    {"component": "span", "text": desc},
                ],
            }

        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "test_on_enable",
                                        "label": "发送测试消息",
                                    },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                    "component": "VTextField",
                                    "props": {
                                        "model": "server",
                                        "label": "Bark 服务器地址",
                                        "placeholder": BARK_SEND_URL,
                                    },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "sound",
                                            "label": "铃声（可选）",
                                            "placeholder": "如 minuet，留空用 App 默认",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "user_count",
                                            "label": "用户数量",
                                            "items": COUNT_OPTIONS,
                                            "hint": "控制下方展示的用户槽位数",
                                            "persistent-hint": True,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    *slots,
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "density": "comfortable",
                                            "title": "配置说明",
                                            "class": "text-body-2",
                                        },
                                        "content": [
                                            {
                                                "component": "div",
                                                "props": {"class": "d-flex flex-column ga-1"},
                                                "content": [
                                                    tip("通知类型", "留空则接收全部通知类型"),
                                                    tip("生效时段",
                                                        "开始/结束选「全天」即不限时段，"
                                                        "可跨天（如 22:00 → 07:00）；"
                                                        "时段外消息按静默（passive）推送"),
                                                    tip("强提醒",
                                                        "使用 critical 级别（静音也响铃），"
                                                        "需在 Bark App 内授权「重要警告」"),
                                                ],
                                            }
                                        ],
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ], defaults

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页：用户列表与最近推送记录。"""
        user_rows = []
        for user in self._users:
            types_text = "全部类型" if not user["types"] else "、".join(
                item.value for item in NotificationType if item.name in user["types"])
            window_text = "全天" if not user["window"] else (
                "跨天时段" if user["window"][0] > user["window"][1] else "时段内")
            user_rows.append({
                "component": "VCardText",
                "props": {"class": "py-2"},
                "content": [
                    {
                        "component": "span",
                        "props": {"class": "mr-2", "style": "font-weight: bold"},
                        "text": user["name"],
                    },
                    {
                        "component": "span",
                        "props": {"class": "mr-2 text--secondary"},
                        "text": f"类型：{types_text}",
                    },
                    {
                        "component": "span",
                        "props": {"class": "mr-2 text--secondary"},
                        "text": f"生效：{window_text}" if user["window"] else "生效：全天",
                    },
                    {
                        "component": "VChip",
                        "props": {
                            "size": "x-small",
                            "color": "orange" if user["critical"] else "default",
                            "class": "ml-1",
                        },
                        "text": "强提醒" if user["critical"] else "普通",
                    },
                ],
            })

        history_rows = []
        for record in reversed(self.get_data("history") or []):
            history_rows.append({
                "component": "VCardText",
                "props": {"class": "py-1"},
                "content": [
                    {
                        "component": "span",
                        "props": {"class": "mr-2 text--secondary"},
                        "text": record.get("time", ""),
                    },
                    {
                        "component": "span",
                        "props": {"class": "mr-2"},
                        "text": f"[{record.get('user', '')}]",
                    },
                    {
                        "component": "span",
                        "props": {"class": "mr-2"},
                        "text": record.get("title", "") or "(无标题)",
                    },
                    {
                        "component": "span",
                        "props": {
                            "class": "ml-1",
                            "style": "font-weight: bold; color: "
                                     + ("#4CAF50" if record.get("ok") else "#E53935"),
                        },
                        "text": "✓" if record.get("ok") else "✘",
                    },
                ],
            })

        def _card(title: str, rows: List[dict], empty_text: str) -> dict:
            """构造详情页卡片：有内容显示行，无内容显示占位说明。"""
            return {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "mb-4"},
                "content": [
                    {"component": "VCardTitle", "props": {"class": "pb-2"}, "text": title},
                    *(rows or [{
                        "component": "VCardText",
                        "content": [{
                            "component": "span",
                            "props": {"class": "text--secondary"},
                            "text": empty_text,
                        }],
                    }]),
                ],
            }

        return [
            _card("用户列表", user_rows,
                  "尚未配置用户，请在插件设置中开启并填写。"),
            _card("最近推送", history_rows, "暂无推送记录。"),
        ]

    def _post_bark(self, url: str, payload: Dict[str, Any]):
        """发送 Bark 请求，独立方法便于测试替换。"""
        # 凭证 POST 禁用重定向，避免 device_key 随跳转泄露
        return RequestUtils(content_type="application/json").post_res(
            url, json=payload, allow_redirects=False)

    def _send_bark(self, user: Dict[str, Any], payload: Dict[str, Any]) -> bool:
        """调用 Bark API 推送，仅以 HTTP 200 且 JSON code==200 判定成功。"""
        res = self._post_bark(f"{self._server}/push", payload)
        if res is None:
            logger.warn(f"Bark 消息发送失败（{user['name']}）：未获取到返回信息")
            return False
        if res.status_code != 200:
            logger.warn(
                f"Bark 消息发送失败（{user['name']}），HTTP 状态码：{res.status_code}")
            return False
        try:
            code = res.json().get("code")
        except Exception as err:
            logger.warn(f"Bark 消息发送失败（{user['name']}），响应非 JSON：{err}")
            return False
        if code != BARK_SUCCESS_CODE:
            logger.warn(f"Bark 消息发送失败（{user['name']}），接口 code={code}")
            return False
        return True

    def _record_history(self, user_name: str, title: str, level: str, ok: bool) -> None:
        """记录最近推送结果，仅保留最近 HISTORY_LIMIT 条。"""
        history = self.get_data("history") or []
        history.append({
            "time": datetime.now().strftime("%m-%d %H:%M:%S"),
            "user": user_name,
            "title": (title or "")[:50],
            "level": level,
            "ok": ok,
        })
        self.save_data("history", history[-HISTORY_LIMIT:])

    def _resolve_level(self, user: Dict[str, Any]) -> str:
        """按生效时间段与强提醒开关决定 Bark 推送级别。"""
        if not in_time_window(user["window"]):
            return "passive"
        return "critical" if user["critical"] else "active"

    def _match_type(self, msg_type: Any, user: Dict[str, Any]) -> bool:
        """判断消息类型是否属于用户的接收范围（types 为空 = 全部）。"""
        if not user["types"]:
            return True
        if msg_type is None:
            return False
        return getattr(msg_type, "name", msg_type) in user["types"]

    @eventmanager.register(EventType.NoticeMessage)
    def send(self, event: Event) -> None:
        """消息发送事件：按用户配置逐个推送。"""
        if not self.get_state():
            return
        if not event.event_data:
            return

        msg_body = event.event_data
        # 已指定专用通知渠道的定向消息不处理，避免与宿主渠道重复
        channel = getattr(msg_body.get("channel"), "value", msg_body.get("channel"))
        if channel:
            return

        msg_type = msg_body.get("type")
        title = msg_body.get("title")
        text = msg_body.get("text")
        if not title and not text:
            logger.warn("Bark 消息推送：标题和内容不能同时为空")
            return

        for user in self._users:
            if not self._match_type(msg_type, user):
                continue
            level = self._resolve_level(user)
            payload: Dict[str, Any] = {
                "device_key": user["bark_key"],
                "body": text or title,
                "group": getattr(msg_type, "value", msg_type) or "其它",
                "level": level,
            }
            if title:
                payload["title"] = title
            if self._sound:
                payload["sound"] = self._sound
            if msg_body.get("image"):
                payload["icon"] = msg_body.get("image")
            if msg_body.get("link"):
                payload["url"] = msg_body.get("link")

            try:
                ok = self._send_bark(user, payload)
            except Exception as err:
                logger.error(f"Bark 消息发送异常（{user['name']}）：{err}")
                ok = False
            self._record_history(user["name"], title or text or "", level, ok)

    def stop_service(self) -> None:
        """停止插件：释放后台资源。本插件未持有线程或调度器，重置状态即可。"""
        self._enabled = False
