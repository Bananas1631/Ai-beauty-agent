"""Вымышленные сайты салонов из evals/sites/ как HTTP-сервер в памяти (httpx.MockTransport).

Тесты и evals RAG обходят «сайт» так же, как настоящий: robots.txt, sitemap.xml, HTML-страницы,
но без сети и всегда с одинаковым содержимым.
"""

from pathlib import Path

import httpx

SITES = Path(__file__).parent / "sites"


def site_host(slug: str) -> str:
    return f"{slug}.example"


def site_transport(slug: str, *, with_sitemap: bool = True) -> httpx.MockTransport:
    folder = SITES / slug
    host = site_host(slug)
    pages = sorted(path.name for path in folder.glob("*.html"))

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host not in (host, f"www.{host}"):
            return httpx.Response(404)
        path = request.url.path
        if path == "/robots.txt":
            body = f"User-agent: *\nDisallow: /admin/\nSitemap: https://{host}/sitemap.xml\n"
            return httpx.Response(200, text=body, headers={"content-type": "text/plain"})
        if path == "/sitemap.xml":
            if not with_sitemap:
                return httpx.Response(404)
            locs = "".join(f"<url><loc>https://{host}/{'' if p == 'index.html' else p}</loc></url>" for p in pages)
            xml = f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>'
            return httpx.Response(200, text=xml, headers={"content-type": "application/xml"})
        name = "index.html" if path in ("", "/") else path.lstrip("/")
        file = folder / name
        if name in pages and file.is_file():
            return httpx.Response(
                200, text=file.read_text(encoding="utf-8"), headers={"content-type": "text/html; charset=utf-8"}
            )
        return httpx.Response(404)

    return httpx.MockTransport(handle)


def site_client(slug: str, *, with_sitemap: bool = True) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=site_transport(slug, with_sitemap=with_sitemap), follow_redirects=False)
