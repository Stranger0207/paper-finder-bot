import asyncio
import logging
import re
import xml.etree.ElementTree as ET
from typing import Optional
from urllib.parse import quote, urljoin

import httpx
from curl_cffi.requests import AsyncSession

from app.config import settings
from app.services.models import PaperItem

logger = logging.getLogger(__name__)

DOI_REGEX = re.compile(r"(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)", re.IGNORECASE)
ARXIV_REGEX = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:)?(\d{4}\.\d{4,5}(?:v\d+)?)", re.IGNORECASE)
PMID_REGEX = re.compile(r"^(?:pmid:?\s*)?(\d{7,9})$", re.IGNORECASE)


def reconstruct_abstract(inverted_index: Optional[dict]) -> str:
    """Reconstruct plain-text abstract from OpenAlex abstract_inverted_index."""
    if not inverted_index or not isinstance(inverted_index, dict):
        return "چکیدهای برای این مقاله در پایگاه ثبت نشده است."
    try:
        word_positions = []
        for word, positions in inverted_index.items():
            for pos in positions:
                word_positions.append((pos, word))
        word_positions.sort(key=lambda x: x[0])
        text = " ".join(w for _, w in word_positions)
        return text[:1800] + ("..." if len(text) > 1800 else "")
    except Exception:
        return "چکیدهای برای این مقاله در دسترس نیست."


def clean_doi(raw_doi: Optional[str]) -> Optional[str]:
    if not raw_doi:
        return None
    raw_doi = raw_doi.replace("https://doi.org/", "").replace("http://doi.org/", "").strip()
    match = DOI_REGEX.search(raw_doi)
    return match.group(1) if match else None


class PaperAggregator:
    def __init__(self):
        self.headers = {
            "User-Agent": f"PaperFinderBot/2.0 (mailto:{settings.CONTACT_EMAIL})",
            "Accept": "application/json",
        }
        self.timeout = httpx.Timeout(20.0, connect=10.0)

    def _get_httpx_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=self.headers,
            timeout=self.timeout,
            follow_redirects=True,
            proxy=settings.clean_proxy
        )

    def _parse_openalex_work(self, item: dict) -> Optional[PaperItem]:
        title = item.get("display_name") or item.get("title")
        if not title:
            return None

        authorships = item.get("authorships", [])
        authors_list = [
            a.get("author", {}).get("display_name", "")
            for a in authorships[:4]
            if a.get("author", {}).get("display_name")
        ]
        authors = ", ".join(authors_list) if authors_list else "نامشخص"
        if len(authorships) > 4:
            authors += " et al."

        year = str(item.get("publication_year") or "نامشخص")
        primary_loc = item.get("primary_location") or {}
        source_info = primary_loc.get("source") or {}
        venue = source_info.get("display_name") or "OpenAlex"

        doi = clean_doi(item.get("doi"))
        open_access = item.get("open_access") or {}
        pdf_url = open_access.get("oa_url") or primary_loc.get("pdf_url")
        landing_url = primary_loc.get("landing_page_url") or item.get("id")
        abstract = reconstruct_abstract(item.get("abstract_inverted_index"))
        citations = item.get("cited_by_count") or 0

        return PaperItem(
            title=title.strip(),
            authors=authors,
            year=year,
            venue=venue,
            abstract=abstract,
            doi=doi,
            pdf_url=pdf_url,
            landing_url=landing_url,
            source="OpenAlex",
            citation_count=citations
        )

    async def search_openalex(self, query: str, limit: int = 14, page: int = 1) -> list[PaperItem]:
        url = "https://api.openalex.org/works"
        params = {
            "search": query,
            "per-page": limit,
            "page": page,
            "mailto": settings.CONTACT_EMAIL
        }
        results = []
        try:
            async with self._get_httpx_client() as client:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    for item in data.get("results", []):
                        paper = self._parse_openalex_work(item)
                        if paper:
                            results.append(paper)
        except Exception as e:
            logger.warning(f"OpenAlex search error: {e}")
        return results

    async def search_arxiv(self, query: str, limit: int = 6, page: int = 1) -> list[PaperItem]:
        start = max(0, (page - 1) * limit)
        url = f"http://export.arxiv.org/api/query?search_query=all:{quote(query)}&start={start}&max_results={limit}"
        results = []
        try:
            async with self._get_httpx_client() as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    root = ET.fromstring(resp.text)
                    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
                    for entry in root.findall("atom:entry", ns):
                        title_el = entry.find("atom:title", ns)
                        if title_el is None or not title_el.text:
                            continue
                        title = " ".join(title_el.text.strip().split())

                        summary_el = entry.find("atom:summary", ns)
                        abstract = (
                            " ".join(summary_el.text.strip().split())
                            if (summary_el is not None and summary_el.text)
                            else "بدون چکیده"
                        )

                        authors_els = entry.findall("atom:author/atom:name", ns)
                        authors_list = [a.text.strip() for a in authors_els[:4] if a.text]
                        authors = ", ".join(authors_list) + (" et al." if len(authors_els) > 4 else "")

                        published_el = entry.find("atom:published", ns)
                        year = published_el.text[:4] if (published_el is not None and published_el.text) else "نامشخص"

                        id_el = entry.find("atom:id", ns)
                        landing_url = id_el.text.strip() if (id_el is not None and id_el.text) else ""
                        pdf_url = landing_url.replace("/abs/", "/pdf/") + ".pdf" if "/abs/" in landing_url else None

                        doi_el = entry.find("arxiv:doi", ns)
                        doi = clean_doi(doi_el.text) if (doi_el is not None and doi_el.text) else None

                        results.append(PaperItem(
                            title=title,
                            authors=authors or "نامشخص",
                            year=year,
                            venue="arXiv Preprint",
                            abstract=abstract[:1800],
                            doi=doi,
                            pdf_url=pdf_url,
                            landing_url=landing_url,
                            source="arXiv"
                        ))
        except Exception as e:
            logger.warning(f"arXiv search error: {e}")
        return results

    async def search_semantic_scholar(self, query: str, limit: int = 6, page: int = 1) -> list[PaperItem]:
        url = "https://api.semanticscholar.org/graph/v1/paper/search"
        offset = max(0, (page - 1) * limit)
        params = {
            "query": query,
            "limit": limit,
            "offset": offset,
            "fields": "title,authors,year,venue,abstract,externalIds,openAccessPdf,citationCount,url"
        }
        results = []
        try:
            async with self._get_httpx_client() as client:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    for item in data.get("data", []):
                        title = item.get("title")
                        if not title:
                            continue
                        auth_list = [a.get("name", "") for a in item.get("authors", [])[:4] if a.get("name")]
                        authors = ", ".join(auth_list) or "نامشخص"
                        ext_ids = item.get("externalIds") or {}
                        doi = clean_doi(ext_ids.get("DOI"))
                        oa_pdf = (item.get("openAccessPdf") or {}).get("url")
                        results.append(PaperItem(
                            title=title,
                            authors=authors,
                            year=str(item.get("year") or "نامشخص"),
                            venue=item.get("venue") or "Semantic Scholar",
                            abstract=(item.get("abstract") or "چکیدهای ثبت نشده است.")[:1800],
                            doi=doi,
                            pdf_url=oa_pdf,
                            landing_url=item.get("url"),
                            source="SemanticScholar",
                            citation_count=item.get("citationCount") or 0
                        ))
        except Exception as e:
            logger.warning(f"Semantic Scholar search error: {e}")
        return results

    async def lookup_by_identifier(self, identifier: str) -> Optional[PaperItem]:
        """Direct paper lookup via DOI, arXiv ID/URL, or PMID."""
        identifier = identifier.strip()

        arxiv_match = ARXIV_REGEX.search(identifier)
        if arxiv_match and "10." not in identifier:
            arxiv_id = arxiv_match.group(1)
            papers = await self.search_arxiv(f"id:{arxiv_id}", limit=1)
            if papers:
                return papers[0]

        doi_match = DOI_REGEX.search(identifier)
        pmid_match = PMID_REGEX.match(identifier)

        if doi_match:
            lookup_path = f"https://doi.org/{doi_match.group(1)}"
        elif pmid_match:
            lookup_path = f"pmid:{pmid_match.group(1)}"
        else:
            return None

        url = f"https://api.openalex.org/works/{lookup_path}"
        try:
            async with self._get_httpx_client() as client:
                resp = await client.get(url, params={"mailto": settings.CONTACT_EMAIL})
                if resp.status_code == 200:
                    paper = self._parse_openalex_work(resp.json())
                    if paper:
                        return paper
        except Exception as e:
            logger.warning(f"Direct lookup failed for {identifier}: {e}")
        return None

    async def search_papers(self, query: str, page: int = 1, page_size: int = settings.MAX_RESULTS) -> list[PaperItem]:
        """Aggregate search across OpenAlex, arXiv, and Semantic Scholar or direct ID lookup."""
        if page == 1:
            direct_paper = await self.lookup_by_identifier(query)
            if direct_paper:
                return [direct_paper]

        oa_task = self.search_openalex(query, limit=14, page=page)
        arxiv_task = self.search_arxiv(query, limit=6, page=page)
        s2_task = self.search_semantic_scholar(query, limit=6, page=page)

        oa_res, arxiv_res, s2_res = await asyncio.gather(oa_task, arxiv_task, s2_task)

        combined: list[PaperItem] = []
        seen_keys: set[str] = set()

        for paper in (oa_res + arxiv_res + s2_res):
            dedup_key = (paper.doi or paper.title[:45]).lower().strip()
            if dedup_key not in seen_keys:
                seen_keys.add(dedup_key)
                combined.append(paper)
            if len(combined) >= page_size:
                break

        return combined

    async def resolve_unpaywall_pdf(self, doi: str) -> Optional[str]:
        """Resolve free Open Access PDF via official Unpaywall API."""
        url = f"https://api.unpaywall.org/v2/{quote(doi)}"
        try:
            async with self._get_httpx_client() as client:
                resp = await client.get(url, params={"email": settings.CONTACT_EMAIL})
                if resp.status_code == 200:
                    data = resp.json()
                    best_oa = data.get("best_oa_location") or {}
                    return best_oa.get("url_for_pdf") or best_oa.get("url")
        except Exception as e:
            logger.warning(f"Unpaywall lookup error: {e}")
        return None

    async def resolve_fallback_mirror_pdf(self, doi: str) -> Optional[str]:
        """Use curl_cffi with browser TLS impersonation to extract direct PDF from fallback mirrors."""
        for mirror in settings.mirrors_list:
            target_url = f"{mirror}/{doi}"
            try:
                async with AsyncSession(impersonate="chrome120", proxy=settings.clean_proxy, timeout=15) as session:
                    resp = await session.get(target_url)
                    if resp.status_code != 200:
                        continue
                    html_text = resp.text

                    # 1. Meta tag with PDF URL (Sci-Hub modern format)
                    meta_match = re.search(
                        r'<meta\s+[^>]*content=["\']([^"\']+\.pdf[^"\']*)["\']',
                        html_text,
                        re.IGNORECASE
                    )
                    if not meta_match:
                        meta_match = re.search(
                            r'<meta\s+[^>]*(?:citation_pdf_url|pdf_url)[^>]*content=["\']([^"\']+)["\']',
                            html_text,
                            re.IGNORECASE
                        )
                    if meta_match:
                        raw_link = meta_match.group(1).split("#")[0]
                        if raw_link.startswith("//"):
                            return "https:" + raw_link
                        return urljoin(mirror, raw_link)

                    # 2. iframe / embed src
                    match = re.search(
                        r'(?:iframe|embed)[^>]+src=["\']([^"\']+\.pdf[^"\']*)["\']',
                        html_text,
                        re.IGNORECASE
                    )
                    if not match:
                        match = re.search(
                            r'location\.href\s*=\s*["\']([^"\']+\.pdf[^"\']*)["\']',
                            html_text,
                            re.IGNORECASE
                        )
                    if not match:
                        match = re.search(
                            r'href=["\']([^"\']+\.pdf[^"\']*)["\']',
                            html_text,
                            re.IGNORECASE
                        )
                    if match:
                        raw_link = match.group(1).split("#")[0]
                        if raw_link.startswith("//"):
                            return "https:" + raw_link
                        if raw_link.startswith("/"):
                            return urljoin(mirror, raw_link)
                        return raw_link
            except Exception as e:
                logger.debug(f"Mirror {mirror} failed for {doi}: {e}")
        return None

    async def get_best_pdf_url(self, paper: PaperItem) -> Optional[str]:
        if paper.pdf_url and paper.pdf_url.startswith("http"):
            return paper.pdf_url

        if paper.doi:
            unpaywall_url = await self.resolve_unpaywall_pdf(paper.doi)
            if unpaywall_url:
                return unpaywall_url

            mirror_url = await self.resolve_fallback_mirror_pdf(paper.doi)
            if mirror_url:
                return mirror_url

        return None

    async def download_pdf_bytes(self, pdf_url: str) -> Optional[bytes]:
        """Download PDF bytes while verifying %PDF magic header and Telegram file size limits."""
        max_bytes = settings.MAX_PDF_SIZE_MB * 1024 * 1024
        try:
            async with AsyncSession(impersonate="chrome120", proxy=settings.clean_proxy, timeout=35) as session:
                resp = await session.get(pdf_url, allow_redirects=True)
                if resp.status_code == 200:
                    content = resp.content
                    if len(content) > max_bytes:
                        return None
                    if content.startswith(b"%PDF"):
                        return content
        except Exception as e:
            logger.warning(f"Failed downloading PDF bytes from {pdf_url}: {e}")
        return None


aggregator = PaperAggregator()
