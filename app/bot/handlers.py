import asyncio
import hashlib
import html
import logging
import re
import time
from typing import Optional
from urllib.parse import quote

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton

from app.config import settings
from app.database import db
from app.services.aggregator import aggregator, clean_doi
from app.services.models import PaperItem
from app.bot.keyboards import build_paper_keyboard

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
    # Replace illegal filesystem characters with underscore
    clean = re.sub(r'[\\/*?:"<>|]', "_", str(name))
    # Consolidate multiple whitespace and underscores
    clean = re.sub(r"[\s_]+", "_", clean).strip("._- ")
    clean = clean[:max_len].strip("._- ")
    if not clean:
        return "paper.pdf"
    return f"{clean}.pdf"


async def safe_edit_text(message: Message, text: str, **kwargs) -> Optional[Message]:
    """Safely edit message text suppressing 'message is not modified' error."""
    try:
        return await message.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return message
        logger.warning(f"TelegramBadRequest in safe_edit_text: {e}")
        return None
    except Exception as e:
        logger.warning(f"Error editing message: {e}")
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
    await message.answer(welcome_text[:4000])


@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "📚 <b>راهنمای استفاده از ربات مقاله‌یاب:</b>\n\n"
        "1️⃣ <b>جستجوی کلیدواژه:</b> عبارت تخصصی یا عنوان مقاله را ارسال کنید تا نتایج به همراه دکمه دانلود PDF و چکیده نمایش داده شوند.\n"
        "2️⃣ <b>دانلود با DOI:</b> اگر مقاله در ژورنال‌های پولی باشد، روی دکمه <b>«📥 دریافت فایل PDF»</b> بزنید تا ربات از طریق Unpaywall و آینه‌های کمکی، فایل PDF را مستقیماً در تلگرام ارسال کند.\n"
        "3️⃣ <b>مشاهده چکیده:</b> با زدن دکمه <b>«📖 نمایش چکیده»</b>، خلاصه کامل مقاله در پیامی مجزا نمایش داده می‌شود."
    )
    await message.answer(help_text[:4000])


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
    await message.answer(text[:4000])


@router.message(F.text & ~F.text.startswith("/"))
async def handle_search_query(message: Message):
    query = (message.text or "").strip()
    if len(query) < 3:
        await message.answer("⚠️ لطفاً عبارت دقیق‌تری (حداقل ۳ کاراکتر) برای جستجو وارد کنید.")
        return

    if message.from_user:
        # Rate limit check: max 25 searches per minute
        if await db.check_rate_limit(message.from_user.id, max_per_minute=25):
            await message.answer("⚠️ <b>تعداد درخواست‌های شما بیش از حد مجاز است.</b>\nلطفاً لحظاتی بعد مجدداً امتحان کنید.")
            return
        await db.log_search(message.from_user.id, query)

    status_msg = await message.answer("🔍 در حال جستجو در پایگاه‌های علمی (OpenAlex, arXiv, Semantic Scholar, Crossref)...")

    papers = await aggregator.search_papers(query, page=1, page_size=settings.MAX_RESULTS)
    if not papers:
        await safe_edit_text(
            status_msg,
            "❌ مقاله‌ای با این مشخصات یافت نشد.\n"
            "💡 پیشنهاد: کلیدواژه‌های انگلیسی، عنوان دقیق‌تر یا شناسه DOI را امتحان کنید."
        )
        return

    try:
        await status_msg.delete()
    except Exception:
        pass

    for idx, paper in enumerate(papers, start=1):
        await db.save_paper(paper)
        card_html = format_paper_card(paper, idx)
        kb = build_paper_keyboard(paper)
        await message.answer(card_html, reply_markup=kb, disable_web_page_preview=True)
        await asyncio.sleep(0.04)

    if len(papers) >= settings.MAX_RESULTS:
        session_id = hashlib.sha256(query.strip().lower().encode("utf-8")).hexdigest()[:10]
        session_cache.set(session_id, {"query": query, "page": 1})
        await db.save_search_session(session_id, query, page=1)

        prompt_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ بله، نمایش ۲۰ نتیجه بیشتر", callback_data=f"more:{session_id}:2")]
        ])
        safe_q = safe_html(query, 40)
        more_text = (
            f"📋 <b>نمایش ۲۰ نتیجه برتر اول به پایان رسید.</b>\n\n"
            f"🔍 آیا مایلید <b>۲۰ مقاله بعدی</b> درباره «<code>{safe_q}</code>» نمایش داده شوند؟"
        )
        await message.answer(more_text[:4000], reply_markup=prompt_kb)


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
    await message.answer(unknown_text[:4000])


@router.callback_query(F.data.startswith("more:"))
async def handle_more_results_callback(callback: CallbackQuery):
    # Ensure callback.answer() is called exactly once
    await callback.answer()

    parts = callback.data.split(":")
    if len(parts) < 3:
        if callback.message:
            await callback.message.answer("⚠️ داده‌های درخواست نامعتبر است.")
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
            await callback.message.answer("⚠️ مهلت این جستجو منقضی شده است. لطفاً موضوع را مجدداً جستجو کنید.")
        return

    query = session_data["query"]
    safe_q = safe_html(query, 40)

    if callback.message:
        await safe_edit_text(
            callback.message,
            f"⏳ در حال جستجو و دریافت ۲۰ مقاله بعدی (صفحه {target_page}) برای «<code>{safe_q}</code>»..."
        )

    papers = await aggregator.search_papers(query, page=target_page, page_size=settings.MAX_RESULTS)
    if not papers:
        if callback.message:
            await safe_edit_text(
                callback.message,
                f"🏁 نتیجه بیشتری در پایگاه‌های علمی برای «<code>{safe_q}</code>» یافت نشد."
            )
        return

    if callback.message:
        try:
            await callback.message.delete()
        except Exception:
            pass

    start_idx = (target_page - 1) * settings.MAX_RESULTS + 1
    for idx, paper in enumerate(papers, start=start_idx):
        await db.save_paper(paper)
        card_html = format_paper_card(paper, idx)
        kb = build_paper_keyboard(paper)
        if callback.message:
            await callback.message.answer(card_html, reply_markup=kb, disable_web_page_preview=True)
        await asyncio.sleep(0.04)

    if callback.message:
        if len(papers) >= settings.MAX_RESULTS:
            session_cache.set(session_id, {"query": query, "page": target_page})
            await db.save_search_session(session_id, query, page=target_page)
            next_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ بله، نمایش ۲۰ نتیجه بیشتر", callback_data=f"more:{session_id}:{target_page + 1}")]
            ])
            text = (
                f"📋 <b>نمایش نتایج صفحه {target_page} به پایان رسید.</b>\n\n"
                f"🔍 آیا مایلید <b>۲۰ مقاله بعدی</b> (صفحه {target_page + 1}) نمایش داده شوند؟"
            )
            await callback.message.answer(text[:4000], reply_markup=next_kb)
        else:
            await callback.message.answer("🏁 به انتهای نتایج این جستجو رسیدید.")


@router.callback_query(F.data.startswith("abs:"))
async def handle_abstract_callback(callback: CallbackQuery):
    # Ensure callback.answer() is called exactly once
    await callback.answer()

    parts = callback.data.split(":", 1)
    if len(parts) < 2:
        return

    short_id = parts[1]
    paper = await db.get_paper(short_id)

    if not paper:
        if callback.message:
            await callback.message.answer("⚠️ اطلاعات این مقاله در حافظه منقضی شده است. لطفاً مجدداً جستجو کنید.")
        return

    safe_title = safe_html(paper.title, 300)
    # Ensure entire abstract message respects 4000 char Telegram limit
    safe_abstract = safe_html(paper.abstract, 3200)

    text = (
        f"📖 <b>چکیده مقاله (Abstract):</b>\n"
        f"<b>{safe_title}</b>\n\n"
        f"{safe_abstract}"
    )
    if callback.message:
        await callback.message.answer(text[:4000])


@router.callback_query(F.data.startswith("pdf:"))
async def handle_pdf_callback(callback: CallbackQuery):
    # Ensure callback.answer() is called exactly once with user feedback
    await callback.answer("⏳ در حال بررسی و استخراج فایل PDF...")

    parts = callback.data.split(":", 1)
    if len(parts) < 2:
        return

    short_id = parts[1]
    paper = await db.get_paper(short_id)

    if not paper:
        if callback.message:
            await callback.message.answer("⚠️ اطلاعات این مقاله در حافظه منقضی شده است. لطفاً دوباره جستجو کنید.")
        return

    safe_title_preview = safe_html(paper.title, 100)
    wait_msg = None
    if callback.message:
        wait_msg = await callback.message.answer(
            f"⏳ در حال استعلام و آماده‌سازی فایل PDF برای:\n<b>{safe_title_preview}</b>..."
        )

    pdf_url = await aggregator.get_best_pdf_url(paper)
    if not pdf_url:
        fallback_links = []
        if paper.doi:
            clean_d = clean_doi(paper.doi)
            if clean_d:
                enc_d = quote(clean_d, safe="")
                safe_enc_d = safe_html(enc_d)
                fallback_links.append(f"🔗 <a href='https://sci-hub.ru/{safe_enc_d}'>دانلود از آینه Sci-Hub ۱</a>")
                fallback_links.append(f"🔗 <a href='https://sci-hub.st/{safe_enc_d}'>دانلود از آینه Sci-Hub ۲</a>")
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
            await callback.message.answer(not_found_text[:4000], disable_web_page_preview=True)
        return

    # Download PDF bytes with strict 50MB and timeout safeguards
    pdf_bytes = await aggregator.download_pdf_bytes(pdf_url)
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
            await callback.message.answer_document(document=doc_file, caption=caption)
        if wait_msg:
            try:
                await wait_msg.delete()
            except Exception:
                pass
    else:
        # If direct file download failed or exceeded 50MB, provide direct URL to the user
        safe_direct_link = safe_html(pdf_url)
        direct_link_text = (
            f"✅ <b>لینک دانلود مستقیم فایل PDF آماده است:</b>\n\n"
            f"📥 <a href='{safe_direct_link}'>برای دریافت و دانلود مستقیم فایل PDF اینجا کلیک کنید</a>\n\n"
            f"<i>(به دلیل حجم بالای فایل یا محدودیت فایروال ناشر، لینک مستقیم ارسال شد.)</i>"
        )
        if wait_msg:
            await safe_edit_text(wait_msg, direct_link_text[:4000], disable_web_page_preview=True)
        elif callback.message:
            await callback.message.answer(direct_link_text[:4000], disable_web_page_preview=True)
