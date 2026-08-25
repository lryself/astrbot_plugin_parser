"""CPP 无差别同人站（allcpp.cn）展品解析器"""

from html import unescape
from re import DOTALL, Match, findall, search, sub
from typing import Any, ClassVar

from aiohttp import ClientError

from ..config import PluginConfig
from ..cookie import CookieJar
from ..data import MediaContent, Platform, SendGroup, TextContent
from ..download import Downloader
from .base import BaseParser, handle

# 图片 CDN，与页面 `getLookWebPicUrl()` 对应
PIC_CDN = "https://imagecdn3.allcpp.cn"


class AllcppParser(BaseParser):
    """allcpp.cn（无差别同人站）展品解析器

    展品详情页（/d/<id>.do）会服务端渲染标题、封面、标签、作者等信息；
    图集（试阅）内容走 ``allcpp/doujinshi/contribute/getList.do`` 接口，
    该接口需要登录，未配置 Cookie 时退化为仅返回封面。
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
    def _clean_text(text: str) -> str:
        text = unescape(text or "")
        text = sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _clean_detail(text: str) -> str:
        """清理展品详情文字：去 <img>、<br> 换行、去空白行"""
        text = unescape(text or "")
        text = sub(r"<img[^>]*>", "", text)
        text = text.replace("<br>", "\n").replace("<br/>", "\n")
        lines = [line.strip() for line in text.split("\n")]
        return "\n".join(line for line in lines if line)
