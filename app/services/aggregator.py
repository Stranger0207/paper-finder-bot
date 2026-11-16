import asyncio
import html
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
ARXIV_REGEX = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/|arxiv:)?(\d{4}\.\d{4,5}(?:v\d+)?)(?:\.pdf)?", re.IGNORECASE)
PMID_REGEX = re.compile(r"^(?:pmid:?\s*)?(\d{7,9})$", re.IGNORECASE)

MAX_PDF_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 50 MB limit


def reconstruct_abstract(inverted_index: Optional[dict]) -> str:
    """Reconstruct plain-text abstract from OpenAlex abstract_inverted_index."""
    if not inverted_index or not isinstance(inverted_index, dict):
        return "چکیده‌ای برای این مقاله در پایگاه ثبت نشده است."
    try:
        word_positions = []
        for word, positions in inverted_index.items():
            if isinstance(positions, (list, tuple)):
                for pos in positions:
                    if isinstance(pos, int):
                        word_positions.append((pos, str(word)))
        if not word_positions:
            return "چکیده‌ای برای این مقاله در پایگاه ثبت نشده است."
        word_positions.sort(key=lambda x: x[0])
        text = " ".join(w for _, w in word_positions)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:1800] + ("..." if len(text) > 1800 else "")
    except Exception:
        return "چکیده‌ای برای این مقاله در دسترس نیست."


def clean_jats_abstract(raw_abstract: Optional[str]) -> str:
    """Remove XML/JATS tags (e.g. <jats:p>) and unescape HTML/XML entities from abstract."""
    if not raw_abstract or not isinstance(raw_abstract, str):
        return "چکیده‌ای برای این مقاله ثبت نشده است."
    # Remove XML / JATS tags
    cleaned = re.sub(r"<[^>]+>", " ", raw_abstract)
    # Unescape HTML/XML entities like &lt;, &gt;, &amp;
    cleaned = html.unescape(cleaned)
    # Normalize whitespaces
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return "چکیده‌ای برای این مقاله ثبت نشده است."
    return cleaned[:1800] + ("..." if len(cleaned) > 1800 else "")


def clean_doi(raw_doi: Optional[str]) -> Optional[str]:
    """Extract clean DOI and strip any trailing punctuation (., ,, ;, ))."""
    if not raw_doi:
        return None
    raw_doi = str(raw_doi).replace("https://doi.org/", "").replace("http://doi.org/", "").replace("doi:", "").strip()
    match = DOI_REGEX.search(raw_doi)
    if not match:
        return None
    doi = match.group(1).strip()
    # Strip trailing punctuation: ., ,, ;, )
    doi = re.sub(r"[.,;\)]+$", "", doi)
    return doi if doi else None


def clean_arxiv_id(raw_arxiv: Optional[str]) -> Optional[str]:
    """Extract clean arXiv ID and remove .pdf suffix if present."""
    if not raw_arxiv:
        return None
    raw = str(raw_arxiv).strip()
    raw = re.sub(r"\.pdf$", "", raw, flags=re.IGNORECASE).strip()
    match = ARXIV_REGEX.search(raw)
    if match:
        aid = match.group(1).strip()
        return re.sub(r"\.pdf$", "", aid, flags=re.IGNORECASE)
    return None


def normalize_title(title: str) -> str:
    """Normalize paper title for fuzzy deduplication."""
    if not title:
        return ""
    return re.sub(r"[^\w\d]", "", title.lower())


def merge_paper_items(existing: PaperItem, new_item: PaperItem) -> None:
    """Merge missing/empty fields of existing record with new record's data."""
    # 1. Merge pdf_url
    if (not existing.pdf_url or not str(existing.pdf_url).startswith("http")) and (new_item.pdf_url and str(new_item.pdf_url).startswith("http")):
        existing.pdf_url = new_item.pdf_url

    # 2. Merge abstract
    empty_abstracts = {
        "چکیدهای برای این مقاله ثبت نشده است.",
        "چکیده‌ای برای این مقاله ثبت نشده است.",
        "بدون چکیده",
        "چکیدهای برای این مقاله در پایگاه ثبت نشده است.",
        "چکیده‌ای برای این مقاله در پایگاه ثبت نشده است.",
        "چکیدهای برای این مقاله در دسترس نیست.",
        "چکیده‌ای برای این مقاله در دسترس نیست."
    }
    if (not existing.abstract or existing.abstract.strip() in empty_abstracts) and (new_item.abstract and new_item.abstract.strip() not in empty_abstracts):
        existing.abstract = new_item.abstract

    # 3. Merge citation_count
    if (not existing.citation_count or existing.citation_count == 0) and (new_item.citation_count and new_item.citation_count > 0):
        existing.citation_count = new_item.citation_count

    # 4. Merge year
    if (not existing.year or existing.year == "نامشخص") and (new_item.year and new_item.year != "نامشخص"):
        existing.year = new_item.year

    # 5. Merge venue
    generic_venues = {"نامشخص", "OpenAlex", "Semantic Scholar", "Crossref", "arXiv Preprint"}
    if (not existing.venue or existing.venue in generic_venues) and (new_item.venue and new_item.venue not in generic_venues):
        existing.venue = new_item.venue

    # 6. Merge DOI
    if not existing.doi and new_item.doi:
        existing.doi = new_item.doi

    # 7. Merge arxiv_id
    if not existing.arxiv_id and new_item.arxiv_id:
        existing.arxiv_id = new_item.arxiv_id

    # 8. Merge landing_url
    if not existing.landing_url and new_item.landing_url:
        existing.landing_url = new_item.landing_url


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

        # In OpenAlex, only extract direct pdf_url fields (never oa_url which is an HTML landing page)
        pdf_url = None
        if primary_loc.get("pdf_url") and str(primary_loc.get("pdf_url")).startswith("http"):
            pdf_url = primary_loc.get("pdf_url").strip()
        if not pdf_url:
            best_oa = item.get("best_oa_location") or {}
            if best_oa.get("pdf_url") and str(best_oa.get("pdf_url")).startswith("http"):
                pdf_url = best_oa.get("pdf_url").strip()
        if not pdf_url:
            for loc in item.get("locations", []):
                if isinstance(loc, dict) and loc.get("pdf_url") and str(loc.get("pdf_url")).startswith("http"):
                    pdf_url = loc.get("pdf_url").strip()
                    break

        open_access = item.get("open_access") or {}
        landing_url = primary_loc.get("landing_page_url") or open_access.get("oa_url") or item.get("id")

        # Abstract reconstruction from inverted index if present
        abstract_inv = item.get("abstract_inverted_index")
        if abstract_inv:
            abstract = reconstruct_abstract(abstract_inv)
        else:
            abstract = item.get("abstract") or "چکیده‌ای برای این مقاله در پایگاه ثبت نشده است."

        citations = item.get("cited_by_count") or 0

        # ArXiv ID detection
        arxiv_id = None
        ids = item.get("ids") or {}
        if ids.get("arxiv"):
            arxiv_id = clean_arxiv_id(ids.get("arxiv"))
        if not arxiv_id:
            arxiv_id = clean_arxiv_id(landing_url) or clean_arxiv_id(pdf_url)

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
            citation_count=citations,
            arxiv_id=arxiv_id
        )

    def _extract_crossref_year(self, item: dict) -> str:
        """Safely extract publication year from Crossref date-parts without IndexError."""
        for date_field in ("published-print", "published-online", "issued", "created", "published"):
            date_info = item.get(date_field)
            if isinstance(date_info, dict):
                date_parts = date_info.get("date-parts")
                if (
                    isinstance(date_parts, list)
                    and len(date_parts) > 0
                    and isinstance(date_parts[0], list)
                    and len(date_parts[0]) > 0
                    and date_parts[0][0] is not None
                ):
                    val = str(date_parts[0][0]).strip()
                    if val:
                        return val
        return "نامشخص"

    def _parse_crossref_work(self, item: dict) -> Optional[PaperItem]:
        """Safely parse Crossref work item handling empty lists and JATS XML tags in abstract."""
        titles = item.get("title")
        title = ""
        if isinstance(titles, list) and len(titles) > 0 and titles[0]:
            title = str(titles[0]).strip()
        elif isinstance(titles, str) and titles.strip():
            title = titles.strip()

        if not title:
            return None

        # Authors
        author_list = []
        for a in item.get("author", [])[:4]:
            if isinstance(a, dict):
                name_parts = [a.get("given", ""), a.get("family", "")]
                full = " ".join(p.strip() for p in name_parts if p and p.strip())
                if not full and a.get("name"):
                    full = a.get("name", "").strip()
                if full:
                    author_list.append(full)
        authors = ", ".join(author_list) if author_list else "نامشخص"
        if len(item.get("author", [])) > 4:
            authors += " et al."

        year = self._extract_crossref_year(item)

        # Venue / Container Title
        containers = item.get("container-title")
        venue = "Crossref"
        if isinstance(containers, list) and len(containers) > 0 and containers[0]:
            venue = str(containers[0]).strip()
        elif isinstance(containers, str) and containers.strip():
            venue = containers.strip()

        doi = clean_doi(item.get("DOI"))
        abstract = clean_jats_abstract(item.get("abstract"))
        citations = item.get("is-referenced-by-count") or 0

        # Safe PDF direct link extraction
        pdf_url = None
        for link in item.get("link", []):
            if isinstance(link, dict) and link.get("content-type") == "application/pdf":
                candidate = link.get("URL")
                if candidate and str(candidate).startswith("http"):
                    pdf_url = str(candidate).strip()
                    break

        landing_url = item.get("URL") or (f"https://doi.org/{quote(doi, safe='')}" if doi else None)

        return PaperItem(
            title=title,
            authors=authors,
            year=year,
            venue=venue,
            abstract=abstract,
            doi=doi,
            pdf_url=pdf_url,
            landing_url=landing_url,
            source="Crossref",
            citation_count=citations
        )

    async def search_openalex(self, query: str, limit: int = 12, page: int = 1) -> list[PaperItem]:
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

    async def search_crossref(self, query: str, limit: int = 6, page: int = 1) -> list[PaperItem]:
        """Search Crossref works API safely."""
        url = "https://api.crossref.org/works"
        offset = max(0, (page - 1) * limit)
        params = {
            "query": query,
            "rows": limit,
            "offset": offset,
            "mailto": settings.CONTACT_EMAIL
        }
        results = []
        try:
            async with self._get_httpx_client() as client:
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    items = (data.get("message") or {}).get("items", [])
                    for item in items:
                        paper = self._parse_crossref_work(item)
                        if paper:
                            results.append(paper)
        except Exception as e:
            logger.warning(f"Crossref search error: {e}")
        return results

    async def search_arxiv(self, query: str, limit: int = 6, page: int = 1) -> list[PaperItem]:
        """Search arXiv API with sanitized query handling special characters."""
        start = max(0, (page - 1) * limit)

        # Sanitize query to prevent breaking arXiv query syntax
        if query.startswith("id:"):
            clean_q = query.strip()
        else:
            clean_q = re.sub(r"[^\w\s-]", " ", query)
            clean_q = re.sub(r"\s+", " ", clean_q).strip()
            if not clean_q:
                clean_q = query.strip()

        url = f"https://export.arxiv.org/api/query?search_query=all:{quote(clean_q)}&start={start}&max_results={limit}"
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
                        arxiv_id = clean_arxiv_id(landing_url)
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
                            source="arXiv",
                            arxiv_id=arxiv_id
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
                        arxiv_id = clean_arxiv_id(ext_ids.get("ArXiv"))
                        oa_pdf = (item.get("openAccessPdf") or {}).get("url")
                        results.append(PaperItem(
                            title=title,
                            authors=authors,
                            year=str(item.get("year") or "نامشخص"),
                            venue=item.get("venue") or "Semantic Scholar",
                            abstract=(item.get("abstract") or "چکیده‌ای ثبت نشده است.")[:1800],
                            doi=doi,
                            pdf_url=oa_pdf,
                            landing_url=item.get("url"),
                            source="SemanticScholar",
                            citation_count=item.get("citationCount") or 0,
                            arxiv_id=arxiv_id
                        ))
        except Exception as e:
            logger.warning(f"Semantic Scholar search error: {e}")
        return results

    async def lookup_by_identifier(self, identifier: str) -> Optional[PaperItem]:
        """Direct paper lookup via DOI, arXiv ID/URL, or PMID."""
        identifier = identifier.strip()

        # 1. arXiv ID lookup (stripping .pdf if present)
        arxiv_id = clean_arxiv_id(identifier)
        if arxiv_id and "10." not in identifier:
            papers = await self.search_arxiv(f"id:{arxiv_id}", limit=1)
            if papers:
                return papers[0]

        # 2. DOI or PMID lookup
        clean_d = clean_doi(identifier)
        pmid_match = PMID_REGEX.match(identifier)

        if clean_d:
            # Quote DOI in API URL
            encoded_doi = quote(clean_d, safe="")
            lookup_path = f"https://doi.org/{encoded_doi}"
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
            logger.warning(f"Direct OpenAlex lookup failed for {identifier}: {e}")

        # Fallback to Crossref direct DOI lookup if DOI exists
        if clean_d:
            try:
                encoded_doi = quote(clean_d, safe="")
                crossref_url = f"https://api.crossref.org/works/{encoded_doi}"
                async with self._get_httpx_client() as client:
                    resp = await client.get(crossref_url, params={"mailto": settings.CONTACT_EMAIL})
                    if resp.status_code == 200:
                        data = resp.json()
                        item = data.get("message")
                        if item:
                            paper = self._parse_crossref_work(item)
                            if paper:
                                return paper
            except Exception as e:
                logger.warning(f"Direct Crossref lookup failed for DOI {clean_d}: {e}")

        return None

    async def search_papers(self, query: str, page: int = 1, page_size: int = settings.MAX_RESULTS) -> list[PaperItem]:
        """Aggregate search across OpenAlex, arXiv, Semantic Scholar, and Crossref with comprehensive Deduplication and Merging."""
        if page == 1:
            direct_paper = await self.lookup_by_identifier(query)
            if direct_paper:
                return [direct_paper]

        oa_task = self.search_openalex(query, limit=12, page=page)
        arxiv_task = self.search_arxiv(query, limit=6, page=page)
        s2_task = self.search_semantic_scholar(query, limit=6, page=page)
        crossref_task = self.search_crossref(query, limit=6, page=page)

        oa_res, arxiv_res, s2_res, crossref_res = await asyncio.gather(
            oa_task, arxiv_task, s2_task, crossref_task
        )

        all_papers = oa_res + arxiv_res + s2_res + crossref_res

        combined: list[PaperItem] = []
        doi_map: dict[str, PaperItem] = {}
        arxiv_map: dict[str, PaperItem] = {}
        title_map: dict[str, PaperItem] = {}

        for paper in all_papers:
            existing: Optional[PaperItem] = None

            # 1. Match by DOI
            if paper.doi:
                norm_doi = paper.doi.lower().strip()
                existing = doi_map.get(norm_doi)

            # 2. Match by arXiv ID
            if not existing and paper.arxiv_id:
                norm_arxiv = paper.arxiv_id.lower().strip()
                existing = arxiv_map.get(norm_arxiv)

            # 3. Match by Normalized Title
            norm_title = normalize_title(paper.title)
            if not existing and len(norm_title) >= 12:
                existing = title_map.get(norm_title)

            if existing:
                # Instead of discarding, merge missing fields into existing record
                merge_paper_items(existing, paper)
                if existing.doi:
                    doi_map[existing.doi.lower().strip()] = existing
                if existing.arxiv_id:
                    arxiv_map[existing.arxiv_id.lower().strip()] = existing
                if len(norm_title) >= 12:
                    title_map[norm_title] = existing
            else:
                combined.append(paper)
                if paper.doi:
                    doi_map[paper.doi.lower().strip()] = paper
                if paper.arxiv_id:
                    arxiv_map[paper.arxiv_id.lower().strip()] = paper
                if len(norm_title) >= 12:
                    title_map[norm_title] = paper

            if len(combined) >= page_size:
                break

        return combined

    async def resolve_unpaywall_pdf(self, doi: str) -> Optional[str]:
        """Resolve free Open Access direct PDF via Unpaywall API checking all oa_locations."""
        clean_d = clean_doi(doi)
        if not clean_d:
            return None
        # Encode DOI with quote(clean_d, safe="")
        encoded_doi = quote(clean_d, safe="")
        url = f"https://api.unpaywall.org/v2/{encoded_doi}"
        try:
            async with self._get_httpx_client() as client:
                resp = await client.get(url, params={"email": settings.UNPAYWALL_EMAIL or settings.CONTACT_EMAIL})
                if resp.status_code == 200:
                    data = resp.json()
                    # Check best_oa_location first for direct PDF link only
                    best_oa = data.get("best_oa_location") or {}
                    url_pdf = best_oa.get("url_for_pdf")
                    if url_pdf and str(url_pdf).startswith("http"):
                        return str(url_pdf).strip()

                    # Fallback to other locations in oa_locations
                    for loc in data.get("oa_locations", []):
                        if isinstance(loc, dict):
                            loc_pdf = loc.get("url_for_pdf")
                            if loc_pdf and str(loc_pdf).startswith("http"):
                                return str(loc_pdf).strip()
        except Exception as e:
            logger.warning(f"Unpaywall lookup error: {e}")
        return None

    async def resolve_fallback_mirror_pdf(self, doi: str) -> Optional[str]:
        """Use curl_cffi with browser TLS impersonation to extract direct PDF from fallback mirrors."""
        clean_d = clean_doi(doi)
        if not clean_d:
            return None
        encoded_doi = quote(clean_d, safe="")

        for mirror in settings.mirrors_list:
            target_url = f"{mirror}/{encoded_doi}"
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
        """Download PDF bytes with total=90s, sock_read=30s timeout and strict 50MB Content-Length guard."""
        max_bytes = MAX_PDF_DOWNLOAD_BYTES

        # 1. Primary download attempt using curl_cffi AsyncSession with browser impersonation
        try:
            # timeout=(connect/sock_read=30, total=90)
            async with AsyncSession(
                impersonate="chrome120",
                proxy=settings.clean_proxy,
                timeout=(30, 90)
            ) as session:
                resp = await session.get(pdf_url, allow_redirects=True, stream=True)
                if resp.status_code == 200:
                    # Check Content-Length header before reading entire payload
                    content_len_header = resp.headers.get("content-length")
                    if content_len_header and content_len_header.isdigit():
                        if int(content_len_header) > max_bytes:
                            logger.warning(f"Aborting download: Content-Length ({content_len_header}) exceeds 50MB limit.")
                            return None

                    chunks = []
                    downloaded = 0
                    async for chunk in resp.aiter_content(chunk_size=65536):
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            logger.warning("Aborting download: streaming exceeded 50MB limit.")
                            return None
                        chunks.append(chunk)

                    content = b"".join(chunks)
                    if content.startswith(b"%PDF"):
                        return content
                    # Sometimes an initial HTML wrapper redirects, check if %PDF appears in first 1024 bytes
                    pdf_start = content.find(b"%PDF")
                    if pdf_start != -1 and pdf_start < 1024:
                        return content[pdf_start:]
        except Exception as e:
            logger.warning(f"curl_cffi download failed for {pdf_url}: {e}")

        # 2. Fallback download attempt using httpx AsyncClient
        try:
            httpx_timeout = httpx.Timeout(90.0, connect=30.0, read=30.0)
            async with httpx.AsyncClient(
                headers={"User-Agent": self.headers["User-Agent"]},
                timeout=httpx_timeout,
                follow_redirects=True,
                proxy=settings.clean_proxy
            ) as client:
                async with client.stream("GET", pdf_url) as resp:
                    if resp.status_code == 200:
                        content_len_header = resp.headers.get("content-length")
                        if content_len_header and content_len_header.isdigit():
                            if int(content_len_header) > max_bytes:
                                logger.warning(f"Aborting httpx download: Content-Length ({content_len_header}) exceeds 50MB.")
                                return None

                        chunks = []
                        downloaded = 0
                        async for chunk in resp.aiter_bytes(chunk_size=65536):
                            downloaded += len(chunk)
                            if downloaded > max_bytes:
                                logger.warning("Aborting httpx download: streamed bytes exceeded 50MB.")
                                return None
                            chunks.append(chunk)

                        content = b"".join(chunks)
                        if content.startswith(b"%PDF"):
                            return content
                        pdf_start = content.find(b"%PDF")
                        if pdf_start != -1 and pdf_start < 1024:
                            return content[pdf_start:]
        except Exception as e:
            logger.warning(f"httpx download fallback failed for {pdf_url}: {e}")

        return None


aggregator = PaperAggregator()
