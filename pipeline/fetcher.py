"""
B 站视频信息获取模块

通过 B 站 API 获取指定 UP 主的最新视频列表。

B 站 API 认证说明：
- 旧接口 space/arc/search 现在要求登录（-799）
- 新接口 space/wbi/arc/search 需要 WBI 签名（w_rid / wts）
- 本模块实现了完整的 WBI 签名流程，配合 cookies.txt 使用效果最佳

Cookie 获取方式（推荐）：
  安装浏览器插件「Get cookies.txt LOCALLY」
  → 在已登录的 bilibili.com 页面点击导出
  → 保存为 cookies.txt，填写路径到 config.yaml 的 download.cookies_file
"""
from __future__ import annotations

import hashlib
import re
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, unquote
import functools
import logging
import time
import httpx

from .config import config

logger = logging.getLogger(__name__)

_SPACE_WBI_API = "https://api.bilibili.com/x/space/wbi/arc/search"
_NAV_API       = "https://api.bilibili.com/x/web-interface/nav"

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://space.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Origin": "https://space.bilibili.com",
}

# WBI 混淆表（固定值，来自 B 站源码）
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]


@dataclass
class VideoInfo:
    bvid: str
    aid: int
    title: str
    description: str
    duration: int
    pub_date: datetime
    up_uid: str
    up_name: str
    url: str = field(init=False)

    def __post_init__(self):
        self.url = f"https://www.bilibili.com/video/{self.bvid}"

    def __repr__(self) -> str:
        return (
            f"VideoInfo(bvid={self.bvid!r}, title={self.title!r}, "
            f"pub_date={self.pub_date.strftime('%Y-%m-%d %H:%M')}, "
            f"up={self.up_name!r})"
        )


class BilibiliFetcher:
    """从 B 站 API 拉取 UP 主最新视频列表（含 WBI 签名支持）"""

    def __init__(self):
        cookie_dict = self._load_cookies()

        # 绑定到 .bilibili.com 域
        bili_cookies = httpx.Cookies()
        for name, value in cookie_dict.items():
            bili_cookies.set(name, value, domain=".bilibili.com")

        self._client = httpx.Client(
            headers=_DEFAULT_HEADERS,
            cookies=bili_cookies,
            timeout=15,
            follow_redirects=True,
        )
        self._mixin_key: str | None = None   # WBI 签名密钥，懒加载

        if cookie_dict:
            logger.info("已加载 B 站 Cookie（共 %d 个字段：%s）",
                        len(cookie_dict), ", ".join(cookie_dict.keys()))
        else:
            logger.warning(
                "未检测到 B 站 Cookie。\n"
                "推荐使用 cookies.txt 方式：安装插件「Get cookies.txt LOCALLY」\n"
                "→ 在已登录的 bilibili.com 导出 → 填到 config.yaml 的 download.cookies_file"
            )

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def fetch_latest_videos(
        self,
        uid: str,
        up_name: str = "",
        max_videos: int = 5,
        days_filter: int = 1,
    ) -> list[VideoInfo]:
        logger.info("正在获取 UP 主 %s（%s）的最新视频…", up_name or uid, uid)
        raw_list = self._get_video_list(uid, ps=max_videos)

        tz_cst = timezone(timedelta(hours=8))
        cutoff = (
            datetime.now(tz=tz_cst) - timedelta(days=days_filter)
            if days_filter > 0
            else None
        )

        results: list[VideoInfo] = []
        for item in raw_list:
            pub_ts = item.get("created", 0)
            pub_date = datetime.fromtimestamp(pub_ts, tz=tz_cst)

            if cutoff and pub_date < cutoff:
                logger.debug(
                    "跳过旧视频 %s（发布于 %s）",
                    item.get("bvid"),
                    pub_date.strftime("%Y-%m-%d"),
                )
                continue

            info = VideoInfo(
                bvid=item.get("bvid", ""),
                aid=item.get("aid", 0),
                title=item.get("title", ""),
                description=item.get("description", ""),
                duration=item.get("length", 0)
                if isinstance(item.get("length"), int)
                else self._parse_duration(item.get("length", "0:0")),
                pub_date=pub_date,
                up_uid=uid,
                up_name=up_name or str(uid),
            )
            results.append(info)

            if len(results) >= max_videos:
                break

        logger.info("UP 主 %s 共找到 %d 条符合条件的视频", up_name or uid, len(results))
        return results

    def fetch_all_hosts(self) -> list[VideoInfo]:
        all_videos: list[VideoInfo] = []
        for host in config.up_hosts:
            uid = str(host.get("uid", ""))
            if not uid:
                logger.warning("配置中存在没有 UID 的 UP 主，已跳过")
                continue

            videos = self.fetch_latest_videos(
                uid=uid,
                up_name=host.get("name", uid),
                max_videos=host.get("max_videos", 3),
                days_filter=config.days_filter,
            )
            all_videos.extend(videos)
            time.sleep(config.request_interval)

        logger.info("共获取 %d 条待处理视频", len(all_videos))
        return all_videos

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _get_video_list(self, uid: str, ps: int = 10) -> list[dict]:
        """使用 WBI 签名请求视频列表"""
        base_params: dict = {
            "mid": uid,
            "ps": ps,
            "pn": 1,
            "order": "pubdate",
            "tid": 0,
            "keyword": "",
        }
        try:
            signed_params = self._wbi_sign(base_params)
            resp = self._client.get(_SPACE_WBI_API, params=signed_params)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            logger.error("HTTP %s 请求 WBI 接口失败", e.response.status_code)
            return []
        except Exception as e:
            logger.error("请求 B 站 API 异常: %s", e)
            return []

        code = data.get("code", -1)
        if code == 0:
            return data.get("data", {}).get("list", {}).get("vlist", [])

        msg = data.get("message", "")
        if code in (-799, -101):
            logger.error(
                "B 站 API 要求登录 (code=%s: %s)。\n"
                "推荐使用 cookies.txt：安装「Get cookies.txt LOCALLY」插件\n"
                "→ 在已登录的 bilibili.com 导出 → 路径填到 download.cookies_file",
                code, msg,
            )
        elif code == -403:
            logger.error(
                "B 站 API 权限不足 (code=-403)，WBI 签名可能已过期，下次运行会自动刷新。"
            )
            self._mixin_key = None  # 清除缓存，下次重新获取
        else:
            logger.error("B 站 API 返回错误 code=%s: %s", code, msg)
        return []

    def _wbi_sign(self, params: dict) -> dict:
        """为请求参数添加 WBI 签名（w_rid / wts）"""
        mixin_key = self._get_mixin_key()
        wts = int(_time.time())
        signed = dict(params)
        signed["wts"] = wts

        # 过滤特殊字符，按 key 排序后拼接
        query = urlencode(
            sorted(
                {k: re.sub(r"[!'()*]", "", str(v)) for k, v in signed.items()}.items()
            )
        )
        w_rid = hashlib.md5((query + mixin_key).encode()).hexdigest()
        signed["w_rid"] = w_rid
        return signed

    def _get_mixin_key(self) -> str:
        """获取 WBI 混淆密钥（从 /nav 接口动态获取，结果缓存到本次运行）"""
        if self._mixin_key:
            return self._mixin_key

        try:
            resp = self._client.get(_NAV_API)
            resp.raise_for_status()
            nav = resp.json()
            wbi_img = nav.get("data", {}).get("wbi_img", {})
            img_key = wbi_img.get("img_url", "").split("/")[-1].split(".")[0]
            sub_key = wbi_img.get("sub_url", "").split("/")[-1].split(".")[0]
        except Exception as e:
            logger.warning("获取 WBI 密钥失败: %s，使用空密钥（可能影响签名）", e)
            img_key, sub_key = "", ""

        raw_key = img_key + sub_key
        self._mixin_key = "".join(
            raw_key[i] for i in _MIXIN_KEY_ENC_TAB if i < len(raw_key)
        )[:32]
        logger.debug("WBI mixin_key 已更新: %s…", self._mixin_key[:8])
        return self._mixin_key

    @staticmethod
    def _load_cookies() -> dict[str, str]:
        """
        按优先级加载 B 站登录 Cookie：
        1. config.yaml 的 bilibili.sessdata（最简单，只需填 SESSDATA 值）
        2. config.yaml 的 download.cookies_file（Netscape 格式 cookies 文件）
        """
        cookies: dict[str, str] = {}

        # 方式 1：直接配置 SESSDATA
        sessdata = config.get("bilibili", "sessdata", default="")
        if sessdata:
            # SESSDATA 从浏览器复制时是 URL 编码形式（含 %2C 等），
            # httpx 设置 cookie 时会再次编码，必须先解码为原始字符串
            cookies["SESSDATA"] = unquote(sessdata)
            bili_jct = config.get("bilibili", "bili_jct", default="")
            if bili_jct:
                cookies["bili_jct"] = bili_jct
            dedeuserid = config.get("bilibili", "dedeuserid", default="")
            if dedeuserid:
                cookies["DedeUserID"] = dedeuserid
            return cookies

        # 方式 2：从 cookies.txt 文件读取
        cookies_file = config.cookies_file
        if cookies_file and Path(cookies_file).exists():
            cookies = _parse_netscape_cookies(cookies_file, domain_filter="bilibili.com")
            return cookies

        return cookies

    @staticmethod
    def _parse_duration(length_str: str) -> int:
        try:
            parts = length_str.split(":")
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        except (ValueError, AttributeError):
            pass
        return 0


def _parse_netscape_cookies(
    filepath: str, domain_filter: str = ""
) -> dict[str, str]:
    """解析 Netscape 格式 cookies.txt，返回 name->value 字典"""
    cookies: dict[str, str] = {}
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                domain, _, _, _, _, name, value = parts[:7]
                if domain_filter and domain_filter not in domain:
                    continue
                cookies[name] = value
    except Exception as e:
        logger.warning("读取 cookies 文件失败: %s", e)
    return cookies
