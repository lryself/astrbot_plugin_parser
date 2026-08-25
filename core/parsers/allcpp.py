"""CPP 无差别同人站（allcpp.cn）展品解析器"""

import json
from html import unescape
from re import DOTALL, Match, findall, search, sub
from typing import Any, ClassVar

from aiohttp import ClientError

from ..config import PluginConfig
from ..cookie import CookieJar
from ..data import MediaContent, Platform, SendGroup, TextContent
from ..download import Downloader
from .base import BaseParser, ParseException, handle

# 图片 CDN，与页面 `getLookWebPicUrl()` 对应
PIC_CDN = "https://imagecdn3.allcpp.cn"


class AllcppParser(BaseParser):
    """allcpp.cn（无差别同人站）解析器

    展品详情页（/d/<id>.do）会服务端渲染标题、封面、标签、作者等信息；
    图集（试阅）内容走 ``allcpp/doujinshi/contribute/getList.do`` 接口，
    该接口需要登录，未配置 Cookie 时退化为仅返回封面。

    图文（后花园文章）详情页（/w/<id>.do）内容由前端通过
    ``works.allcpp.cn/rest/works/<id>`` 接口异步加载，这里直接请求该接口。
    """

    platform: ClassVar[Platform] = Platform(
        name="allcpp", display_name="CPP无差别同人站"
    )

    def __init__(self, config: PluginConfig, downloader: Downloader):
        super().__init__(config, downloader)
        self.mycfg = config.parser.allcpp
        self.headers.update(
            {
                "accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "referer": "https://www.allcpp.cn/",
            }
        )
        self.cookiejar = CookieJar(config, self.mycfg, domain="allcpp.cn")
        if self.cookiejar.cookies_str:
            self.headers["cookie"] = self.cookiejar.cookies_str

    # https://icp.red/WmBvrs8XK （短链，重定向到 allcpp 展品页）
    @handle("icp.red", r"icp\.red/(?P<short_code>[A-Za-z0-9]+)")
    async def _parse_short_link(self, searched: Match[str]):
        url = f"https://icp.red/{searched.group('short_code')}"
        return await self.parse_with_redirect(url)

    @handle("allcpp.cn", r"allcpp\.cn/d/(?P<did>\d+)")
    async def _parse(self, searched: Match[str]):
        did = searched.group("did")
        page_url = f"https://www.allcpp.cn/d/{did}.do"

        html_text = await self._fetch_page(page_url)
        info = self._parse_page(html_text)

        title = info["title"] or f"展品 {did}"
        author = self.create_author(info["author_name"]) if info["author_name"] else None

        # 图集（试阅）需要登录，未登录时退化为仅封面；样图同样并入
        gallery = self._extract_gallery_images(await self._fetch_gallery(did))
        image_urls = self._collect_images(info["cover"], gallery + info["sample_images"])
        image_contents: list[MediaContent] = (
            self.create_image_contents(image_urls) if image_urls else []
        )

        text = self._build_text(info, title)
        contents: list[MediaContent] = []
        if text:
            contents.append(TextContent(text))
        contents.extend(image_contents)

        send_groups: list[SendGroup] = []
        if len(contents) >= self.cfg.forward_threshold:
            # 达到阈值：整体一个 group，不设 force_merge，交由 sender 合并转发
            send_groups.append(SendGroup(contents=contents))
        else:
            if text:
                send_groups.append(
                    SendGroup(contents=[TextContent(text)], force_merge=False)
                )
            for img in image_contents:
                send_groups.append(SendGroup(contents=[img], force_merge=False))

        return self.result(
            title=title,
            text=text,
            author=author,
            contents=contents,
            send_groups=send_groups,
            url=page_url,
        )

    # https://www.allcpp.cn/w/<id>.do 图文（后花园文章）详情页
    @handle("allcpp.cn/w/", r"allcpp\.cn/w/(?P<wid>\d+)")
    async def _parse_work(self, searched: Match[str]):
        wid = searched.group("wid")
        page_url = f"https://www.allcpp.cn/w/{wid}.do"

        info = self._parse_work_data(await self._fetch_work(wid))

        title = info["title"] or f"图文 {wid}"
        author = (
            self.create_author(info["author_name"], avatar_url=info["author_avatar"])
            if info["author_name"]
            else None
        )

        image_urls = self._collect_images(None, info["pics"])
        image_contents: list[MediaContent] = (
            self.create_image_contents(image_urls) if image_urls else []
        )

        show_work_content = bool(getattr(self.mycfg, "show_work_content", False))
        max_length = int(getattr(self.mycfg, "text_max_length", 100) or 100)

        text = self._build_work_text(
            info,
            title,
            show_work_content=show_work_content,
            max_length=max_length,
        )
        contents: list[MediaContent] = []
        if text:
            contents.append(TextContent(text))
        contents.extend(image_contents)

        send_groups: list[SendGroup] = []
        if len(contents) >= self.cfg.forward_threshold:
            send_groups.append(SendGroup(contents=contents))
        else:
            if text:
                send_groups.append(
                    SendGroup(contents=[TextContent(text)], force_merge=False)
                )
            for img in image_contents:
                send_groups.append(SendGroup(contents=[img], force_merge=False))

        return self.result(
            title=title,
            text=text,
            author=author,
            contents=contents,
            send_groups=send_groups,
            url=page_url,
        )

    async def _fetch_page(self, url: str) -> str:
        """拉取展品详情页 HTML"""
        async with self.session.get(url, headers=self.headers) as resp:
            if resp.status >= 400:
                raise ClientError(f"HTTP {resp.status} {resp.reason}")
            return await resp.text()

    async def _fetch_gallery(self, did: str) -> list[dict[str, Any]]:
        """拉取展品试阅（图集）内容，失败或未登录时返回空列表"""
        url = "https://www.allcpp.cn/allcpp/doujinshi/contribute/getList.do"
        params = {"pageindex": 1, "pagesize": 20, "id": did, "_plat": "web"}
        try:
            async with self.session.get(
                url, params=params, headers=self.headers
            ) as resp:
                if resp.status >= 400:
                    return []
                data = await resp.json()
        except (ClientError, ValueError):
            return []
        return data if isinstance(data, list) else []

    async def _fetch_work(self, wid: str) -> dict[str, Any]:
        """拉取图文（后花园文章）详情，接口在 works.allcpp.cn 子域"""
        url = f"https://works.allcpp.cn/rest/works/{wid}"
        try:
            async with self.session.get(url, headers=self.headers) as resp:
                if resp.status >= 400:
                    raise ClientError(f"HTTP {resp.status} {resp.reason}")
                text = await resp.text()
                data = json.loads(text) if text.strip() else None
        except (ClientError, ValueError):
            raise ParseException(f"图文 {wid} 不存在或已删除")
        if not isinstance(data, dict) or not data.get("id"):
            raise ParseException(f"图文 {wid} 不存在或已删除")
        return data

    def _parse_page(self, html_text: str) -> dict[str, Any]:
        """从详情页 HTML 提取公开信息"""
        info: dict[str, Any] = {
            "title": None,
            "cover": None,
            "tags": [],
            "heat": None,
            "status": None,
            "author_name": None,
            "detail_text": None,
            "sample_images": [],
            "is_logged_in": False,
        }

        # 标题
        if m := search(r'<h1 class="djs-info-title">(.*?)</h1>', html_text, DOTALL):
            info["title"] = self._clean_text(m.group(1))

        # 登录态（IS_USER_TRUENAME=true 表示已登录且实名）
        if m := search(r"var IS_USER_TRUENAME=(\w+);", html_text):
            info["is_logged_in"] = m.group(1).lower() == "true"

        # 封面（取原图 href，不含 OSS 压缩样式）
        if m := search(r'<a class="djs-info-cover"[^>]*href="([^"]+)"', html_text):
            info["cover"] = m.group(1).strip()

        # 标签
        if m := search(r'<ul class="djs-info-tag">(.*?)</ul>', html_text, DOTALL):
            tags = [
                self._clean_text(t)
                for t in findall(r"<li[^>]*>(.*?)</li>", m.group(1), DOTALL)
            ]
            info["tags"] = [t for t in tags if t]

        # 总热度
        if m := search(r'class="djs-info-hot-txt">(.*?)</label>', html_text, DOTALL):
            if span := search(r"<span>([^<]+)</span>", m.group(1)):
                info["heat"] = span.group(1).strip()

        # 状态（策划中 / 已发售 等）
        if m := search(r'class="sell-url[^"]*"[^>]*>(.*?)</a>', html_text, DOTALL):
            info["status"] = self._clean_text(m.group(1))

        # 上传人信息（作者）
        if m := search(r'<div id="djs-create-user">(.*?)</div>', html_text, DOTALL):
            uploader_block = m.group(1)
            if title_m := search(r'title="([^"]+)"', uploader_block):
                info["author_name"] = title_m.group(1).strip()
            elif label_m := search(r"<label[^>]*>(.*?)</label>", uploader_block, DOTALL):
                info["author_name"] = self._clean_text(label_m.group(1))

        # 展品详情（登录后才有）：文字描述 + 图集样图
        if m := search(
            r'<div class="djs-tab-box info[^"]*"[^>]*>(.*?)</div>',
            html_text,
            DOTALL,
        ):
            detail_block = m.group(1)
            if p := search(r"<p[^>]*>(.*?)</p>", detail_block, DOTALL):
                info["detail_text"] = self._clean_detail(p.group(1))
            info["sample_images"] = [
                u.split("?")[0]
                for u in findall(r'<img[^>]*src="([^"]+)"', detail_block)
            ]

        return info

    @staticmethod
    def _parse_work_data(work: dict[str, Any]) -> dict[str, Any]:
        """从图文接口数据中提取展示信息"""
        user = work.get("user") or {}
        face = user.get("face") or {}

        info: dict[str, Any] = {
            "title": work.get("name"),
            "author_name": user.get("nickname"),
            "author_avatar": AllcppParser._build_face_url(face.get("picUrl")),
            "theme": work.get("theme"),
            "type_label": AllcppParser._work_type_label(work.get("type")),
            "hot": work.get("hotCount"),
            "tags": [],
            "foreword": None,
            "content": None,
            "pics": [],
        }

        tags = work.get("tags")
        if isinstance(tags, list):
            info["tags"] = [
                t.get("tag")
                for t in tags
                if isinstance(t, dict) and t.get("tag")
            ]

        foreword = work.get("foreword")
        if isinstance(foreword, str) and foreword.strip():
            info["foreword"] = AllcppParser._clean_detail(foreword)

        content = work.get("content")
        if isinstance(content, str) and content.strip():
            info["pics"].extend(AllcppParser._extract_content_images(content))
            info["content"] = AllcppParser._clean_detail(content)

        pics = work.get("pics")
        if isinstance(pics, list):
            for p in pics:
                if not isinstance(p, dict):
                    continue
                pic = p.get("pic")
                pic_url = pic.get("picUrl") if isinstance(pic, dict) else None
                url = AllcppParser._build_pic_url(str(pic_url) if pic_url else "")
                if url:
                    info["pics"].append(url)

        return info

    @staticmethod
    def _work_type_label(work_type: Any) -> str | None:
        """图文类型标签：0=图片，1=文字"""
        if work_type == 0:
            return "图片"
        if work_type == 1:
            return "文字"
        return None

    @staticmethod
    def _extract_content_images(content: str) -> list[str]:
        """提取图文正文内嵌的图片（/uupload/image/... 相对路径）"""
        images: list[str] = []
        for src in findall(r'<img[^>]*src="([^"]+)"', content):
            url = AllcppParser._build_content_pic_url(src)
            if url:
                images.append(url)
        return images

    def _extract_gallery_images(self, data: list[dict[str, Any]]) -> list[str]:
        """从试阅接口数据中提取图集图片 URL"""
        images: list[str] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            works = item.get("works")
            if not isinstance(works, dict):
                continue
            pics = works.get("pics")
            if not isinstance(pics, list):
                continue
            for p in pics:
                if not isinstance(p, dict):
                    continue
                pic = p.get("pic")
                pic_url = pic.get("picUrl") if isinstance(pic, dict) else None
                if not pic_url:
                    pic_url = p.get("picUrl")
                url = self._build_pic_url(str(pic_url) if pic_url else "")
                if url:
                    images.append(url)
        return images

    @staticmethod
    def _build_text(info: dict[str, Any], title: str) -> str | None:
        parts: list[str] = []
        if title:
            parts.append(title)
        if info.get("author_name"):
            parts.append(f"作者：{info['author_name']}")
        if info.get("tags"):
            parts.append("标签：" + " · ".join(info["tags"]))
        if info.get("heat"):
            parts.append(f"总热度：{info['heat']}")
        if info.get("status"):
            parts.append(f"状态：{info['status']}")
        if info.get("detail_text"):
            parts.append(info["detail_text"])
        if not info.get("is_logged_in"):
            parts.append("（未登录，无法获取展品详情与图集，配置 Cookie 后可查看）")
        return "\n".join(parts) if parts else None

    @staticmethod
    def _build_work_text(
        info: dict[str, Any],
        title: str,
        *,
        show_work_content: bool = False,
        max_length: int = 100,
    ) -> str | None:
        parts: list[str] = []
        if title:
            parts.append(title)
        if info.get("author_name"):
            parts.append(f"作者：{info['author_name']}")
        if info.get("theme"):
            parts.append(f"主题：{info['theme']}")
        if info.get("type_label"):
            parts.append(f"类型：{info['type_label']}")
        if info.get("tags"):
            parts.append("标签：" + " · ".join(info["tags"]))
        if info.get("hot"):
            parts.append(f"热度：{info['hot']}")
        if info.get("foreword"):
            parts.append(info["foreword"])
        # 正文：图片类始终展示；文字类受「显示正文」配置控制
        if info.get("content") and (
            info.get("type_label") != "文字" or show_work_content
        ):
            parts.append(info["content"])
        text = "\n".join(parts) if parts else None
        # 仅文字类图文限制字数，图片类不截断
        if (
            text
            and info.get("type_label") == "文字"
            and max_length > 0
            and len(text) > max_length
        ):
            text = text[: max_length - 1] + "…"
        return text

    @staticmethod
    def _collect_images(cover: str | None, gallery: list[str]) -> list[str]:
        """合并封面与图集，去重"""
        images: list[str] = []
        seen: set[str] = set()
        for url in ([cover] if cover else []) + gallery:
            key = url.split("?", 1)[0]
            if key in seen:
                continue
            seen.add(key)
            images.append(url)
        return images

    @staticmethod
    def _build_pic_url(raw: str) -> str | None:
        raw = (raw or "").strip()
        if not raw:
            return None
        if raw.startswith("http"):
            return raw
        return f"{PIC_CDN}/upload/{raw.lstrip('/')}"

    @staticmethod
    def _build_face_url(raw: str) -> str | None:
        """用户头像 URL（/face/ 目录）"""
        raw = (raw or "").strip()
        if not raw:
            return None
        if raw.startswith("http"):
            return raw
        return f"{PIC_CDN}/face/{raw.lstrip('/')}"

    @staticmethod
    def _build_content_pic_url(raw: str) -> str | None:
        """正文内嵌图片 URL（/uupload/image/... 已含目录前缀）"""
        raw = (raw or "").strip()
        if not raw:
            return None
        if raw.startswith("http"):
            return raw
        return f"{PIC_CDN}/{raw.lstrip('/')}"

    @staticmethod
    def _clean_text(text: str) -> str:
        text = unescape(text or "")
        text = sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _clean_detail(text: str) -> str:
        """清理 HTML 文字：去标签、<br>/</p> 换行、去空白行"""
        text = unescape(text or "")
        text = sub(r"<br\s*/?>", "\n", text)
        text = sub(r"</p>", "\n", text)
        text = sub(r"<[^>]+>", "", text)
        lines = [line.strip() for line in text.split("\n")]
        return "\n".join(line for line in lines if line)
