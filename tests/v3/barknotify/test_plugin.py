"""barknotify 插件单元测试。

覆盖：用户槽位解析、时间段判断（含跨天与边界）、通知类型过滤、
推送级别映射（静默/普通/强提醒）、Bark 请求成功判定与事件处理流程。
"""

from unittest.mock import patch

import pytest

from app.plugins.barknotify import (
    DEFAULT_USER_COUNT, MAX_SLOTS, BarkNotify, in_time_window, parse_time_window,
)
from app.sdk.events import Event
from app.schemas.types import EventType, NotificationType


# ---------- 时间段解析 ----------

def test_parse_time_window():
    assert parse_time_window("", "") is None
    assert parse_time_window("", "08:00") is None
    assert parse_time_window("08:00", "23:00") == (480, 1380)
    # 起止相同视为全天
    assert parse_time_window("08:00", "08:00") is None
    # 跨天区间
    assert parse_time_window("22:00", "07:00") == (1320, 420)


def test_parse_time_window_invalid():
    for start, end in (("abc", "09:00"), ("8", "09:00"),
                       ("25:00", "26:00"), ("0800", "0900")):
        with pytest.raises(ValueError):
            parse_time_window(start, end)


def test_in_time_window_normal():
    window = (480, 1380)  # 08:00-23:00
    assert in_time_window(window, now_minutes=480) is True
    assert in_time_window(window, now_minutes=1380) is True
    assert in_time_window(window, now_minutes=600) is True
    assert in_time_window(window, now_minutes=479) is False
    assert in_time_window(window, now_minutes=1381) is False


def test_in_time_window_overnight():
    window = (1320, 420)  # 22:00-07:00 跨天
    assert in_time_window(window, now_minutes=23 * 60) is True
    assert in_time_window(window, now_minutes=6 * 60) is True
    assert in_time_window(window, now_minutes=420) is True
    assert in_time_window(window, now_minutes=12 * 60) is False
    assert in_time_window(window, now_minutes=421) is False  # 07:01


def test_in_time_window_all_day():
    assert in_time_window(None, now_minutes=0) is True


# ---------- 用户槽位解析 ----------

def _slot(idx=1, name="手机", key="key-a", types=None, start="", end="", critical=False):
    prefix = f"u{idx}_"
    return {
        f"{prefix}enabled": True, f"{prefix}name": name, f"{prefix}key": key,
        f"{prefix}types": types or [], f"{prefix}start": start,
        f"{prefix}end": end, f"{prefix}critical": critical,
    }


def test_build_users_full_slot():
    users = BarkNotify._build_users(_slot(
        types=["Download", "Organize"], start="08:00", end="23:00", critical=True))
    assert users == [{
        "name": "手机", "bark_key": "key-a",
        "types": ["Download", "Organize"],
        "window": (480, 1380), "critical": True,
    }]


def test_build_users_defaults_are_all_day_all_types():
    users = BarkNotify._build_users(_slot())
    assert users[0]["types"] is None
    assert users[0]["window"] is None
    assert users[0]["critical"] is False


def test_build_users_skips_disabled_and_incomplete_slots():
    config = {f"u{i}_enabled": False for i in range(1, MAX_SLOTS + 1)}
    config.update(_slot(idx=2, name="正常", key="key-b"))
    # 缺少 Key 的槽位跳过
    config.update(_slot(idx=3, name="没key", key=""))
    assert BarkNotify._build_users(config) == [{
        "name": "正常", "bark_key": "key-b",
        "types": None, "window": None, "critical": False,
    }]


def test_build_users_filters_unknown_types():
    users = BarkNotify._build_users(_slot(types=["Download", "不存在"]))
    assert users[0]["types"] == ["Download"]
    # 全部无法识别时按全部类型处理
    users = BarkNotify._build_users(_slot(types=["不存在"]))
    assert users[0]["types"] is None


def test_build_users_bad_time_keeps_user_as_all_day():
    users = BarkNotify._build_users(_slot(start="25:00", end="26:00"))
    assert users[0]["window"] is None


def test_build_users_default_count_limits_slots():
    config = {}
    for idx in range(1, MAX_SLOTS + 2):
        config.update(_slot(idx=idx, name=f"u{idx}", key=f"k{idx}"))
    assert len(BarkNotify._build_users(config)) == DEFAULT_USER_COUNT


def test_build_users_respects_user_count():
    config = {"user_count": 2}
    for idx in range(1, 4):
        config.update(_slot(idx=idx, name=f"u{idx}", key=f"k{idx}"))
    users = BarkNotify._build_users(config)
    assert [u["name"] for u in users] == ["u1", "u2"]
    # 超出数量的槽位配置保留：调回数量即恢复
    config["user_count"] = 3
    assert len(BarkNotify._build_users(config)) == 3


def test_build_users_empty():
    assert BarkNotify._build_users({}) == []


# ---------- 推送级别 ----------

def _user(**kwargs) -> dict:
    return BarkNotify._build_users(_slot(**kwargs))[0]


def test_resolve_level_active():
    assert BarkNotify()._resolve_level(_user()) == "active"


def test_resolve_level_critical():
    assert BarkNotify()._resolve_level(_user(critical=True)) == "critical"


def test_resolve_level_passive_overrides_critical():
    user = _user(critical=True)
    with patch("app.plugins.barknotify.in_time_window", return_value=False):
        assert BarkNotify()._resolve_level(user) == "passive"


# ---------- 插件状态与事件处理 ----------

@pytest.fixture
def plugin():
    item = BarkNotify()
    item.del_data("history")
    config = {
        "enabled": True,
        "server": "https://api.day.app/",
        "sound": "minuet",
        "test_on_enable": False,
    }
    config.update(_slot(idx=1, name="手机", key="key-a",
                        types=["Download"], start="08:00", end="23:00", critical=True))
    config.update(_slot(idx=2, name="平板", key="key-b"))
    item.init_plugin(config)
    yield item
    item.stop_service()


def _event(mtype=NotificationType.Download, channel=None, title="标题",
           text="内容", image=None, link=None):
    return Event(event_type=EventType.NoticeMessage, event_data={
        "channel": channel, "type": mtype, "title": title, "text": text,
        "image": image, "link": link,
    })


def test_state_requires_users():
    item = BarkNotify()
    item.init_plugin({"enabled": True})
    assert item.get_state() is False
    item.init_plugin({"enabled": True, **_slot()})
    assert item.get_state() is True


def test_init_plugin_strips_trailing_slash():
    item = BarkNotify()
    item.init_plugin({"enabled": True, "test_on_enable": False,
                      **_slot(), "server": "https://my.host/"})
    assert item._server == "https://my.host"


# ---------- 启用时的测试消息 ----------

def test_enable_sends_test_messages():
    item = BarkNotify()
    item.del_data("history")
    calls = []
    item._post_bark = lambda url, payload: calls.append(dict(payload)) or _ok_response()
    item.init_plugin({"enabled": True, **_slot(),
                      **_slot(idx=2, name="平板", key="key-b")})
    assert len(calls) == 2
    assert {p["device_key"] for p in calls} == {"key-a", "key-b"}
    assert all(p["title"] == "【Bark消息推送】测试消息" for p in calls)
    assert all(p["level"] == "active" for p in calls)
    history = item.get_data("history")
    assert len(history) == 2
    assert all(record["title"] == "测试消息" and record["ok"] for record in history)


def test_enable_without_test_switch_does_not_send():
    item = BarkNotify()
    calls = []
    item._post_bark = lambda url, payload: calls.append(dict(payload)) or _ok_response()
    item.init_plugin({"enabled": True, "test_on_enable": False, **_slot()})
    assert calls == []


def test_disabled_does_not_send_test_message():
    item = BarkNotify()
    calls = []
    item._post_bark = lambda url, payload: calls.append(dict(payload)) or _ok_response()
    item.init_plugin({"enabled": False, **_slot()})
    assert calls == []


def test_reinit_while_enabled_does_not_resend():
    item = BarkNotify()
    item.del_data("history")
    calls = []
    item._post_bark = lambda url, payload: calls.append(dict(payload)) or _ok_response()
    config = {"enabled": True, **_slot()}
    item.init_plugin(config)
    assert len(calls) == 1
    # 已处于启用状态时再次保存配置，不重复发送测试消息
    item.init_plugin(config)
    assert len(calls) == 1


def test_send_builds_payload_per_user(plugin):
    calls = []
    plugin._post_bark = lambda url, payload: calls.append((url, dict(payload))) or _ok_response()
    plugin.send(_event())
    # 两个用户都收到（当前时间落在 08:00-23:00 之外的用户是 passive，仍会发送）
    assert len(calls) == 2
    url, payload = calls[0]
    assert url == "https://api.day.app/push"
    assert payload["device_key"] == "key-a"
    assert payload["title"] == "标题"
    assert payload["body"] == "内容"
    assert payload["group"] == "资源下载"
    assert payload["sound"] == "minuet"
    assert payload["level"] in ("active", "critical", "passive")


def test_send_skips_directed_channel(plugin):
    calls = []
    plugin._post_bark = lambda url, payload: calls.append(url) or _ok_response()
    plugin.send(_event(channel="wechat"))
    assert calls == []


def test_send_skips_when_disabled():
    item = BarkNotify()
    item.init_plugin({"enabled": False, **_slot()})
    calls = []
    item._post_bark = lambda url, payload: calls.append(url) or _ok_response()
    item.send(_event())
    assert calls == []


def test_send_filters_type_per_user(plugin):
    calls = []
    plugin._post_bark = lambda url, payload: calls.append(dict(payload)) or _ok_response()
    # 手机用户只收资源下载，订阅消息只应发给平板
    plugin.send(_event(mtype=NotificationType.Subscribe))
    assert len(calls) == 1
    assert calls[0]["device_key"] == "key-b"


def test_send_maps_image_and_link(plugin):
    payloads = []
    plugin._post_bark = lambda url, payload: payloads.append(dict(payload)) or _ok_response()
    plugin.send(_event(image="https://img/poster.jpg", link="https://mp.example"))
    assert any(p.get("icon") == "https://img/poster.jpg" for p in payloads)
    assert all(p.get("url") == "https://mp.example" for p in payloads)


def test_send_records_history(plugin):
    plugin._post_bark = lambda url, payload: _ok_response()
    plugin.send(_event())
    history = plugin.get_data("history")
    assert len(history) == 2
    assert {record["user"] for record in history} == {"手机", "平板"}
    assert all(record["ok"] for record in history)
    assert all(record["level"] in ("active", "critical", "passive") for record in history)


def test_history_limited_to_20(plugin):
    plugin._post_bark = lambda url, payload: _ok_response()
    for _ in range(15):
        plugin.send(_event())
    assert len(plugin.get_data("history")) == 20


# ---------- Bark 响应判定 ----------

class _Response:
    def __init__(self, status_code=200, code=200):
        self.status_code = status_code
        self._code = code

    def json(self):
        return {"code": self._code, "message": "success"}


def _ok_response():
    return _Response()


def test_send_bark_success(plugin):
    plugin._post_bark = lambda url, payload: _Response(status_code=200, code=200)
    assert plugin._send_bark({"name": "x"}, {}) is True


def test_send_bark_code_not_200(plugin):
    plugin._post_bark = lambda url, payload: _Response(status_code=200, code=400)
    assert plugin._send_bark({"name": "x"}, {}) is False


def test_send_bark_http_error(plugin):
    plugin._post_bark = lambda url, payload: _Response(status_code=500)
    assert plugin._send_bark({"name": "x"}, {}) is False


def test_send_bark_no_response(plugin):
    plugin._post_bark = lambda url, payload: None
    assert plugin._send_bark({"name": "x"}, {}) is False


def test_send_bark_non_json_response(plugin):
    class BadResponse:
        status_code = 200

        def json(self):
            raise ValueError("not json")

    plugin._post_bark = lambda url, payload: BadResponse()
    assert plugin._send_bark({"name": "x"}, {}) is False


# ---------- 表单与详情页 ----------

def _collect_models(node, found):
    if isinstance(node, dict):
        props = node.get("props") or {}
        if "model" in props:
            found.append(props["model"])
        for child in node.get("content") or []:
            _collect_models(child, found)
    elif isinstance(node, list):
        for item in node:
            _collect_models(item, found)


def test_form_covers_all_models_and_defaults():
    form, defaults = BarkNotify().get_form()
    assert form[0]["component"] == "VForm"
    models = []
    _collect_models(form, models)
    assert set(models) == set(defaults)
    # 全局项 5 个（含用户数量）+ 每个槽位 7 项
    assert len(defaults) == 5 + MAX_SLOTS * 7
    assert defaults["enabled"] is False
    assert defaults["server"] == "https://api.day.app"
    assert defaults["test_on_enable"] is True
    assert defaults["u1_types"] == []
    assert defaults["u1_start"] == ""
    assert defaults["user_count"] == DEFAULT_USER_COUNT


def test_form_key_is_plain_text():
    form, _ = BarkNotify().get_form()
    assert "password" not in str(form)


def test_form_show_conditional_on_slot_enabled_and_count():
    form, _ = BarkNotify().get_form()
    content = str(form)
    # 槽位字段受开关与用户数量双重控制，槽位开关仅受数量控制
    assert "{{u1_enabled && user_count >= 1}}" in content
    assert "{{user_count >= 1}}" in content
    # 第 10 槽存在但默认隐藏
    assert "{{user_count >= 10}}" in content


def test_form_hidden_slots_hide_wrapper_rows():
    form, _ = BarkNotify().get_form()
    slot_rows = [
        node for node in form[0]["content"]
        if node.get("component") == "VRow" and "show" in (node.get("props") or {})
    ]
    # 每个槽位两行都必须整体受控，否则隐藏后 VCol 的 padding 仍会留下空白
    assert len(slot_rows) == MAX_SLOTS * 2
    shows = [node["props"]["show"] for node in slot_rows]
    assert "{{user_count >= 10}}" in shows
    assert "{{u10_enabled && user_count >= 10}}" in shows


def test_page_renders_with_and_without_users(plugin):
    assert plugin.get_page()
    empty = BarkNotify()
    assert empty.get_page()
