import asyncio
import hashlib
import html
import logging
import re
import time
from typing import Optional, Union
from urllib.parse import quote

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, BufferedInputFile

from app.config import settings
from app.database import db
from app.services.aggregator import aggregator, clean_doi
from app.services.models import PaperItem
from app.bot.keyboards import build_paper_keyboard, build_results_list_keyboard

logger = logging.getLogger(__name__)

router = Router()


class SearchSessionCache:
    """In-memory cache for search pagination sessions with automatic TTL expiration to prevent memory leaks."""
    def __init__(self, ttl_seconds: int = 3600):
        self._cache: dict[str, dict] = {}
        self._ttl = ttl_seconds

    def _cleanup(self):
        now = time.time()
        expired_keys = [k for k, v in self._cache.items() if now - v.get("timestamp", 0) > self._ttl]
        for k in expired_keys:
            self._cache.pop(k, None)

    def get(self, session_id: str) -> Optional[dict]:
        self._cleanup()
        item = self._cache.get(session_id)
        if item:
            return item["data"]
        return None

    def set(self, session_id: str, data: dict):
        self._cleanup()
        self._cache[session_id] = {
            "data": data,
            "timestamp": time.time()
        }


session_cache = SearchSessionCache(ttl_seconds=3600)


def safe_html(text: Optional[str], max_len: Optional[int] = None) -> str:
    """Truncate text first to max_len, then escape HTML entities with quote=True."""
    if not text:
        return ""
    raw = str(text).strip()
    if max_len is not None and len(raw) > max_len:
        raw = raw[:max_len]
    return html.escape(raw, quote=True)


def sanitize_filename(name: Optional[str], max_len: int = 50) -> str:
    """Sanitize filename from illegal filesystem characters and default to paper.pdf if empty."""
    if not name:
        return "paper.pdf"
    clean = re.sub(r'[\\/*?:"<>|]', "_", str(name))
    clean = re.sub(r"[\s_]+", "_", clean).strip("._- ")
    clean = clean[:max_len].strip("._- ")
    if not clean:
        return "paper.pdf"
    return f"{clean}.pdf"


async def safe_send_message(
    target: Union[Message, CallbackQuery],
    text: str,
    max_retries: int = 3,
    **kwargs
) -> Optional[Message]:
    """Send a message safely handling TelegramRetryAfter (FloodWait) with automatic sleep and retry."""
    for attempt in range(max_retries):
        try:
            if isinstance(target, CallbackQuery):
                if target.message:
                    return await target.message.answer(text, **kwargs)
                return None
            return await target.answer(text, **kwargs)
        except TelegramRetryAfter as e:
            logger.warning(f"TelegramRetryAfter in safe_send_message: sleeping for {e.retry_after}s (attempt {attempt + 1}/{max_retries})")
            await asyncio.sleep(e.retry_after)
        except Exception as e:
            logger.warning(f"Error in safe_send_message: {e}")
            return None
    return None


async def safe_edit_text(message: Message, text: str, max_retries: int = 3, **kwargs) -> Optional[Message]:
    """Safely edit message text suppressing 'message is not modified' error and handling TelegramRetryAfter."""
    for attempt in range(max_retries):
        try:
            return await message.edit_text(text, **kwargs)
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return message
            logger.warning(f"TelegramBadRequest in safe_edit_text: {e}")
            return None
        except TelegramRetryAfter as e:
            logger.warning(f"TelegramRetryAfter in safe_edit_text: sleeping for {e.retry_after}s")
            await asyncio.sleep(e.retry_after)
        except Exception as e:
            logger.warning(f"Error editing message: {e}")
            return None
    return None


def format_paper_card(paper: PaperItem, index: int) -> str:
    """Format single paper result card respecting safe length and HTML escaping."""
    safe_title = safe_html(paper.title, 250)
    safe_authors = safe_html(paper.authors, 200) or "نامشخص"
    safe_venue = safe_html(paper.venue, 120) or "نامشخص"
    safe_year = safe_html(paper.year, 20) or "نامشخص"
    safe_source = safe_html(paper.source, 40) or "نامشخص"
    clean_d = clean_doi(paper.doi) if paper.doi else None
    doi_text = safe_html(clean_d, 80) if clean_d else "ندارد"
    oa_badge = "🟢 دسترسی باز (Open Access)" if paper.pdf_url else "🟡 نیازمند استعلام / DOI"

    card = (
        f"📄 <b>{index}. {safe_title}</b>\n\n"
        f"✍️ <b>نویسندگان:</b> {safe_authors}\n"
        f"🏛 <b>انتشارات:</b> {safe_venue} ({safe_year})\n"
        f"📊 <b>تعداد ارجاعات:</b> {paper.citation_count} | <b>منبع:</b> {safe_source}\n"
        f"🆔 <b>DOI:</b> <code>{doi_text}</code>\n"
        f"📌 <b>وضعیت فایل:</b> {oa_badge}"
    )
    return card[:4000]


def format_papers_list_message(
    papers: list[PaperItem],
    query: str,
    page: int = 1,
    start_idx: int = 1
) -> str:
    """Format multiple paper search results into a single clean text message under 3800 characters."""
    safe_q = safe_html(query, 50)
    lines = [
        f"🔍 <b>نتایج جستجو برای:</b> «<code>{safe_q}</code>» (صفحه {page})\n"
        f"<i>💡 برای مشاهده مشخصات کامل، چکیده و دریافت PDF، روی شماره مقاله در کیبورد زیر بزنید:</i>\n"
    ]
    for offset, paper in enumerate(papers):
        idx = start_idx + offset
        icon = "🟢" if paper.pdf_url else "🟡"
        safe_title = safe_html(paper.title, 85)
        safe_year = safe_html(paper.year, 10) or "نامشخص"
        safe_venue = safe_html(paper.venue, 35) or "نامشخص"
        safe_source = safe_html(paper.source, 20) or "نامشخص"
        lines.append(
            f"<b>{idx}.</b> {icon} <b>{safe_title}</b>\n"
            f"   🏛 <i>{safe_venue}</i> ({safe_year}) | {safe_source}\n"
        )
    full_text = "\n".join(lines)
    return full_text[:3800]


@router.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    if user:
        await db.register_or_update_user(user.id, user.username, user.full_name)

    welcome_text = (
        "🎓 <b>به ربات هوشمند مقاله‌یاب (Paper Finder Bot) خوش آمدید!</b>\n\n"
        "من به پایگاه‌های علمی <b>OpenAlex</b>، <b>arXiv</b>، <b>Semantic Scholar</b> و <b>Crossref</b> متصل هستم و مقالات علمی را بدون معطلی برای شما جستجو و دانلود می‌کنم.\n\n"
        "🔍 <b>چطور جستجو کنم؟</b>\n"
        "• <b>موضوع یا عنوان مقاله</b> را ارسال کنید (مثلاً: <code>Transformer attention mechanism</code>).\n"
        "• یا مستقیماً <b>شناسه DOI</b> را بفرستید (مثلاً: <code>10.1038/nature14539</code>).\n"
        "• یا <b>شناسه arXiv / PMID</b> را ارسال کنید (مثلاً: <code>1706.03762</code>).\n\n"
        "📌 دستورات کاربردی:\n"
        "/stats — مشاهده آمار جستجوهای شما\n"
        "/help — راهنمای کامل استفاده"
    )
    await safe_send_message(message, welcome_text[:4000])


@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "📚 <b>راهنمای استفاده از ربات مقاله‌یاب:</b>\n\n"
        "1️⃣ <b>جستجوی کلیدواژه:</b> عبارت تخصصی یا عنوان مقاله را ارسال کنید تا لیست مقالات به همراه کیبورد انتخاب برای دریافت چکیده و PDF نمایش داده شود.\n"
        "2️⃣ <b>دانلود مستقیم یا با DOI:</b> برای هر مقاله، روی شماره آن در کیبورد بزنید و سپس دکمه <b>«📥 دریافت فایل PDF»</b> را انتخاب کنید تا فایل مستقیماً در تلگرام ارسال شود.\n"
        "3️⃣ <b>مشاهده چکیده:</b> با زدن دکمه <b>«📖 نمایش چکیده»</b>، خلاصه کامل مقاله در پیامی مجزا نمایش داده می‌شود."
    )
    await safe_send_message(message, help_text[:4000])


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not message.from_user:
        return
    stats = await db.get_user_stats(message.from_user.id)
    text = (
        f"📊 <b>آمار فعالیت شما:</b>\n\n"
        f"🔎 تعداد جستجوهای شما: <b>{stats['user_searches']}</b>\n"
        f"🗂 مقالات کش‌شده در پایگاه: <b>{stats['total_cached_papers']}</b>"
    )
    await safe_send_message(message, text[:4000])


@router.message(F.text & ~F.text.startswith("/"))
async def handle_search_query(message: Message):
    query = (message.text or "").strip()
    if len(query) < 3:
        await safe_send_message(message, "⚠️ لطفاً عبارت دقیق‌تری (حداقل ۳ کاراکتر) برای جستجو وارد کنید.")
        return

    if message.from_user:
        # Rate limit check: max 25 searches per minute
        if await db.check_rate_limit(message.from_user.id, max_per_minute=25):
            await safe_send_message(
                message,
                "⚠️ <b>تعداد درخواست‌های شما بیش از حد مجاز است.</b>\nلطفاً لحظاتی بعد مجدداً امتحان کنید."
            )
            return
        await db.log_search(message.from_user.id, query)

    status_msg = await safe_send_message(
        message,
        "🔍 در حال جستجو در پایگاه‌های علمی (OpenAlex, arXiv, Semantic Scholar, Crossref)..."
    )

    papers = await aggregator.search_papers(query, page=1, page_size=settings.MAX_RESULTS)
    if not papers:
        not_found_msg = (
            "❌ مقاله‌ای با این مشخصات یافت نشد.\n"
            "💡 پیشنهاد: کلیدواژه‌های انگلیسی، عنوان دقیق‌تر یا شناسه DOI را امتحان کنید."
        )
        if status_msg:
            await safe_edit_text(status_msg, not_found_msg)
        else:
            await safe_send_message(message, not_found_msg)
        return

    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass

    # Exception case: direct single paper lookup (e.g. DOI or arXiv ID query)
    if len(papers) == 1:
        single_paper = papers[0]
        await db.save_paper(single_paper)
        card_html = format_paper_card(single_paper, 1)
        kb = build_paper_keyboard(single_paper)
        await safe_send_message(message, card_html, reply_markup=kb, disable_web_page_preview=True)
        return

    # Normal case: Multiple results returned -> batch save and single unified list message
    await db.save_papers_batch(papers)

    session_id = None
    next_page = None
    if len(papers) >= settings.MAX_RESULTS:
        session_id = hashlib.sha256(query.strip().lower().encode("utf-8")).hexdigest()[:10]
        session_cache.set(session_id, {"query": query, "page": 1})
        await db.save_search_session(session_id, query, page=1)
        next_page = 2

    list_text = format_papers_list_message(papers, query, page=1, start_idx=1)
    list_kb = build_results_list_keyboard(
        papers,
        start_idx=1,
        session_id=session_id,
        next_page=next_page
    )
    await safe_send_message(message, list_text, reply_markup=list_kb, disable_web_page_preview=True)


@router.message(F.text.startswith("/"))
async def handle_unknown_command(message: Message):
    unknown_text = (
        "❓ <b>دستور وارد شده ناشناخته است.</b>\n\n"
        "📌 دستورات فعال ربات:\n"
        "/start — شروع به کار و معرفی ربات\n"
        "/help — راهنمای جامع استفاده\n"
        "/stats — آمار فعالیت و مقالات کش‌شده\n\n"
        "💡 برای جستجوی مقاله، کافیست <b>عنوان، کلیدواژه، شناسه DOI یا arXiv ID</b> را بدون اسلش (/) ارسال کنید."
    )
    await safe_send_message(message, unknown_text[:4000])


@router.callback_query(F.data.startswith("more:"))
async def handle_more_results_callback(callback: CallbackQuery):
    await callback.answer()

    parts = callback.data.split(":")
    if len(parts) < 3:
        if callback.message:
            await safe_send_message(callback.message, "⚠️ داده‌های درخواست نامعتبر است.")
        return

    session_id = parts[1]
    try:
        target_page = int(parts[2])
    except ValueError:
        target_page = 2

    # Check in-memory cache first, then database
    session_data = session_cache.get(session_id)
    if not session_data:
        session_data = await db.get_search_session(session_id)

    if not session_data:
        if callback.message:
            await safe_send_message(
                callback.message,
                "⚠️ مهلت این جستجو منقضی شده است. لطفاً موضوع را مجدداً جستجو کنید."
            )
        return

    query = session_data["query"]
    safe_q = safe_html(query, 40)

    status_msg = None
    if callback.message:
        status_msg = await safe_send_message(
            callback.message,
            f"⏳ در حال جستجو و دریافت ۲۰ مقاله بعدی (صفحه {target_page}) برای «<code>{safe_q}</code>»..."
        )

    papers = await aggregator.search_papers(query, page=target_page, page_size=settings.MAX_RESULTS)
    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass

    if not papers:
        if callback.message:
            await safe_send_message(
                callback.message,
                f"🏁 نتیجه بیشتری در پایگاه‌های علمی برای «<code>{safe_q}</code>» یافت نشد."
            )
        return

    await db.save_papers_batch(papers)

    start_idx = (target_page - 1) * settings.MAX_RESULTS + 1
    next_page = None
    if len(papers) >= settings.MAX_RESULTS:
        session_cache.set(session_id, {"query": query, "page": target_page})
        await db.save_search_session(session_id, query, page=target_page)
        next_page = target_page + 1

    list_text = format_papers_list_message(papers, query, page=target_page, start_idx=start_idx)
    list_kb = build_results_list_keyboard(
        papers,
        start_idx=start_idx,
        session_id=session_id,
        next_page=next_page
    )
    if callback.message:
        await safe_send_message(callback.message, list_text, reply_markup=list_kb, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("sel:"))
async def handle_select_paper_callback(callback: CallbackQuery):
    await callback.answer()

    parts = callback.data.split(":")
    if len(parts) < 3:
        return

    short_id = parts[1]
    try:
        idx = int(parts[2])
    except ValueError:
        idx = 1

    paper = await db.get_paper(short_id)
    if not paper:
        if callback.message:
            await safe_send_message(
                callback.message,
                "⚠️ اطلاعات این مقاله در حافظه منقضی شده است. لطفاً مجدداً جستجو کنید."
            )
        return

    card_html = format_paper_card(paper, idx)
    kb = build_paper_keyboard(paper)
    if callback.message:
        await safe_send_message(
            callback.message,
            card_html,
            reply_markup=kb,
            disable_web_page_preview=True
        )


@router.callback_query(F.data.startswith("abs:"))
async def handle_abstract_callback(callback: CallbackQuery):
    await callback.answer()

    parts = callback.data.split(":", 1)
    if len(parts) < 2:
        return

    short_id = parts[1]
    paper = await db.get_paper(short_id)

    if not paper:
        if callback.message:
            await safe_send_message(callback.message, "⚠️ اطلاعات این مقاله در حافظه منقضی شده است. لطفاً مجدداً جستجو کنید.")
        return

    safe_title = safe_html(paper.title, 300)
    safe_abstract = safe_html(paper.abstract, 3200)

    text = (
        f"📖 <b>چکیده مقاله (Abstract):</b>\n"
        f"<b>{safe_title}</b>\n\n"
        f"{safe_abstract}"
    )
    if callback.message:
        await safe_send_message(callback.message, text[:4000])


@router.callback_query(F.data.startswith("pdf:"))
async def handle_pdf_callback(callback: CallbackQuery):
    await callback.answer("⏳ در حال بررسی و استخراج فایل PDF...")

    parts = callback.data.split(":", 1)
    if len(parts) < 2:
        return

    short_id = parts[1]
    paper = await db.get_paper(short_id)

    if not paper:
        if callback.message:
            await safe_send_message(callback.message, "⚠️ اطلاعات این مقاله در حافظه منقضی شده است. لطفاً دوباره جستجو کنید.")
        return

    safe_title_preview = safe_html(paper.title, 100)
    wait_msg = None
    if callback.message:
        wait_msg = await safe_send_message(
            callback.message,
            f"⏳ در حال استعلام و آماده‌سازی فایل PDF برای:\n<b>{safe_title_preview}</b>..."
        )

    # Multi-candidate priority download: 1. paper.pdf_url, 2. Unpaywall, 3. Sci-Hub mirrors
    pdf_bytes, best_url = await aggregator.download_paper_pdf(paper)

    if pdf_bytes:
        filename = sanitize_filename(paper.title)
        doc_file = BufferedInputFile(pdf_bytes, filename=filename)

        safe_caption_title = safe_html(paper.title, 200)
        safe_caption_year = safe_html(paper.year, 20)
        clean_d = clean_doi(paper.doi) if paper.doi else None
        safe_caption_doi = safe_html(clean_d or 'N/A', 100)

        caption = (
            f"📄 <b>{safe_caption_title}</b>\n"
            f"📅 سال انتشار: {safe_caption_year}\n"
            f"🆔 DOI: <code>{safe_caption_doi}</code>"
        )
        caption = caption[:1000]

        if callback.message:
            for attempt in range(3):
                try:
                    await callback.message.answer_document(document=doc_file, caption=caption)
                    break
                except TelegramRetryAfter as e:
                    logger.warning(f"TelegramRetryAfter sending document: sleeping for {e.retry_after}s")
                    await asyncio.sleep(e.retry_after)
                except Exception as e:
                    logger.warning(f"Error sending PDF document: {e}")
                    break

        if wait_msg:
            try:
                await wait_msg.delete()
            except Exception:
                pass
    elif best_url:
        safe_direct_link = safe_html(best_url)
        direct_link_text = (
            f"✅ <b>لینک دانلود مستقیم فایل PDF آماده است:</b>\n\n"
            f"📥 <a href='{safe_direct_link}'>برای دریافت و دانلود مستقیم فایل PDF اینجا کلیک کنید</a>\n\n"
            f"<i>(به دلیل حجم بالای فایل یا محدودیت فایروال ناشر، لینک مستقیم ارسال شد.)</i>"
        )
        if wait_msg:
            await safe_edit_text(wait_msg, direct_link_text[:4000], disable_web_page_preview=True)
        elif callback.message:
            await safe_send_message(callback.message, direct_link_text[:4000], disable_web_page_preview=True)
    else:
        fallback_links = []
        if paper.doi:
            clean_d = clean_doi(paper.doi)
            if clean_d:
                enc_d = quote(clean_d, safe="/")
                safe_enc_d = safe_html(enc_d)
                for i, mirror in enumerate(settings.mirrors_list, start=1):
                    fallback_links.append(f"🔗 <a href='{mirror}/{safe_enc_d}'>دانلود از آینه Sci-Hub {i}</a>")
                fallback_links.append(f"🔗 <a href='https://annas-archive.org/scidb/{safe_enc_d}'>دانلود از آرشیو آنا (Anna's Archive)</a>")
                fallback_links.append(f"🌐 <a href='https://doi.org/{safe_enc_d}'>صفحه اصلی در سایت ناشر (DOI)</a>")

        extra_msg = "\n".join(fallback_links) if fallback_links else ""
        not_found_text = (
            f"❌ متأسفانه نسخه رایگان با دسترسی باز برای این مقاله یافت نشد.\n\n"
            f"💡 می‌توانید از لینک‌های کمکی زیر برای دریافت مستقیم مقاله استفاده کنید:\n\n{extra_msg}"
        )
        if wait_msg:
            await safe_edit_text(wait_msg, not_found_text[:4000], disable_web_page_preview=True)
        elif callback.message:
            await safe_send_message(callback.message, not_found_text[:4000], disable_web_page_preview=True)
