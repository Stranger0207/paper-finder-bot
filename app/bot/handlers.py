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
from aiogram.types import (
    Message, CallbackQuery, BufferedInputFile,
    InlineKeyboardMarkup, InlineKeyboardButton
)

from app.config import settings
from app.database import db
from app.services.aggregator import (
    aggregator, clean_doi, extract_multiple_dois,
    filter_and_sort_papers, generate_citations, generate_bibtex,
    generate_ris, generate_csv_bytes
)
from app.services.models import PaperItem
from app.bot.keyboards import (
    build_paper_keyboard, build_results_list_keyboard,
    build_cite_keyboard, build_saved_library_keyboard,
    build_subscription_keyboard, build_active_subscription_keyboard,
    build_admin_receipt_keyboard
)

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

    # Publisher info
    publisher_line = ""
    if paper.publisher:
        safe_publisher = safe_html(paper.publisher, 100)
        publisher_line = f"\n🏢 <b>ناشر:</b> {safe_publisher}"

    # TL;DR line
    tldr_line = ""
    if paper.tldr:
        safe_tldr = safe_html(paper.tldr, 250)
        tldr_line = f"\n\n💡 <b>خلاصه یک‌خطی (TL;DR):</b> <i>{safe_tldr}</i>"

    card = (
        f"📄 <b>{index}. {safe_title}</b>\n\n"
        f"✍️ <b>نویسندگان:</b> {safe_authors}\n"
        f"🏛 <b>انتشارات:</b> {safe_venue} ({safe_year}){publisher_line}\n"
        f"📊 <b>تعداد ارجاعات:</b> {paper.citation_count} | <b>منبع:</b> {safe_source}\n"
        f"🆔 <b>DOI:</b> <code>{doi_text}</code>\n"
        f"📌 <b>وضعیت فایل:</b> {oa_badge}"
        f"{tldr_line}"
    )
    return card[:4000]


def format_papers_list_message(
    papers: list[PaperItem],
    query: str,
    page: int = 1,
    start_idx: int = 1,
    sort_label: str = ""
) -> str:
    """Format multiple paper search results into a single clean text message under 3800 characters."""
    safe_q = safe_html(query, 50)
    header_extra = f"\n{sort_label}" if sort_label else ""
    lines = [
        f"🔍 <b>نتایج جستجو برای:</b> «<code>{safe_q}</code>» (صفحه {page})\n"
        f"<i>💡 برای مشاهده مشخصات کامل، چکیده و دریافت PDF، روی شماره مقاله در کیبورد زیر بزنید:</i>{header_extra}\n"
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


# ── Subscription Access Verification ──────────────────────────────────

PREMIUM_PROMO_TEXT = (
    "💎 <b>این قابلیت مخصوص کاربران دارای اشتراک ویژه است!</b>\n\n"
    "✨ <b>با خرید اشتراک ویژه یکماهه (۵۰,۰۰۰ تومان)، به امکانات زیر دسترسی کامل خواهید داشت:</b>\n"
    "• ♾ جستجوی نامحدود مقالات بدون سقف روزانه (سقف عادی: ۱۰ جستجو در ۲۴ ساعت)\n"
    "• 🔍 مشاهده و جستجوی نتایج بیشتر از ۲۰ مقاله (صفحات بعدی)\n"
    "• ⭐️ ذخیره مقالات در کتابخانه شخصی (/saved)\n"
    "• 🕸 دسترسی به شبکه مقالات مشابه\n"
    "• 📎 دریافت رفرنس استاندارد (APA, IEEE, Vancouver, BibTeX, RIS)\n"
    "• 🇮🇷 ترجمه نامحدود چکیده مقالات به فارسی روان\n\n"
    "💳 <b>شماره کارت واریز:</b> <code>6219861843148577</code> (به نام رضایی - بلوبانک)\n"
    "💵 <b>مبلغ:</b> ۵۰,۰۰۰ تومان (۳۰ روزه)\n\n"
    "📌 پس از واریز، روی دکمه زیر بزنید و رسید خود را ارسال کنید:"
)


def build_premium_promo_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 ارسال رسید پرداخت", callback_data="send_receipt_guide")],
        [InlineKeyboardButton(text="💳 خرید اشتراک ویژه", callback_data="buy_sub")]
    ])


async def check_premium_access(event: Union[CallbackQuery, Message]) -> bool:
    """Check if user has active premium subscription; if not, show promo and return False."""
    user = event.from_user
    if not user:
        return False
    is_prem = await db.is_user_premium(user.id)
    if not is_prem:
        kb = build_premium_promo_keyboard()
        if isinstance(event, CallbackQuery):
            await event.answer("❌ نیاز به اشتراک ویژه", show_alert=True)
            if event.message:
                await event.message.answer(PREMIUM_PROMO_TEXT, reply_markup=kb)
        else:
            await event.answer(PREMIUM_PROMO_TEXT, reply_markup=kb)
        return False
    return True


# ── Command Handlers ─────────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    if user:
        try:
            await db.register_or_update_user(user.id, user.username, user.full_name)
        except Exception as e:
            logger.warning(f"Error registering user {user.id}: {e}")

    welcome_text = (
        "🎓 <b>به ربات هوشمند مقاله‌یاب (Paper Finder Bot) خوش آمدید!</b>\n\n"
        "من به پایگاه‌های علمی <b>OpenAlex</b>، <b>arXiv</b>، <b>Semantic Scholar</b> و <b>Crossref</b> متصل هستم و مقالات علمی را بدون معطلی برای شما جستجو و دانلود می‌کنم.\n\n"
        "🔍 <b>چطور جستجو کنم؟</b>\n"
        "• <b>موضوع یا عنوان مقاله</b> را ارسال کنید (مثلاً: <code>Transformer attention mechanism</code>).\n"
        "• یا مستقیماً <b>شناسه DOI</b> را بفرستید (مثلاً: <code>10.1038/nature14539</code>).\n"
        "• یا <b>شناسه arXiv / PMID</b> را ارسال کنید (مثلاً: <code>1706.03762</code>).\n\n"
        "💎 <b>اشتراک ویژه یک‌ماهه (۵۰,۰۰۰ تومان):</b>\n"
        "دسترسی به ترجمه نامحدود فارسی چکیده مقالات، اولویت جستجو و دانلود.\n"
        "برای خرید یا بررسی وضعیت: /subscribe را ارسال کنید.\n\n"
        "📌 دستورات کاربردی:\n"
        "/subscribe — خرید و مدیریت اشتراک ویژه یک‌ماهه\n"
        "/stats — مشاهده آمار فعالیت و وضعیت اشتراک شما\n"
        "/saved — مشاهده کتابخانه شخصی و خروجی اکسل/EndNote\n"
        "/help — راهنمای کامل استفاده"
    )
    await safe_send_message(message, welcome_text[:4000])


@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "📚 <b>راهنمای استفاده از ربات مقاله‌یاب:</b>\n\n"
        "1️⃣ <b>جستجوی کلیدواژه:</b> عبارت تخصصی یا عنوان مقاله را ارسال کنید تا لیست مقالات به همراه کیبورد انتخاب برای دریافت چکیده و PDF نمایش داده شود.\n"
        "2️⃣ <b>دانلود مستقیم یا با DOI:</b> برای هر مقاله، روی شماره آن در کیبورد بزنید و سپس دکمه <b>«📥 دریافت PDF»</b> را انتخاب کنید تا فایل مستقیماً در تلگرام ارسال شود.\n"
        "3️⃣ <b>مشاهده چکیده:</b> با زدن دکمه <b>«📖 چکیده»</b>، خلاصه کامل مقاله در پیامی مجزا نمایش داده می‌شود.\n"
        "4️⃣ <b>ترجمه فارسی:</b> با دکمه <b>«🇮🇷 ترجمه فارسی»</b> چکیده و عنوان مقاله به فارسی ترجمه می‌شود (ویژه کاربران اشتراک ویژه).\n"
        "5️⃣ <b>رفرنس‌دهی:</b> دکمه <b>«📎 رفرنس»</b> سه فرمت APA, IEEE, Vancouver به همراه خروجی BibTeX و RIS ارائه می‌دهد.\n"
        "6️⃣ <b>مقالات مشابه:</b> با دکمه <b>«🕸 مقالات مشابه»</b> مقالات مرتبط پیشنهاد می‌شوند.\n"
        "7️⃣ <b>کتابخانه شخصی:</b> مقالات مورد علاقه را با <b>«⭐️ ذخیره مقاله»</b> ذخیره و با /saved مشاهده و خروجی بگیرید.\n"
        "8️⃣ <b>اشتراک ویژه:</b> اشتراک ۳۰ روزه با هزینه ۵۰,۰۰۰ تومان برای ترجمه نامحدود چکیده و امکانات پیشرفته با /subscribe.\n\n"
        "📌 دستورات فعال:\n"
        "/start — شروع به کار و معرفی ربات\n"
        "/subscribe — خرید و مدیریت اشتراک ویژه یک‌ماهه\n"
        "/stats — آمار فعالیت و وضعیت اشتراک\n"
        "/saved — مشاهده کتابخانه شخصی و خروجی اکسل/EndNote\n"
        "/help — راهنمای جامع استفاده"
    )
    await safe_send_message(message, help_text[:4000])


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not message.from_user:
        return
    stats = await db.get_user_stats(message.from_user.id)
    is_prem = stats.get("is_premium")
    prem_status = "💎 فعال (ویژه)" if is_prem else "عادی (رایگان)"
    expires_line = f"\n⏳ انقضای اشتراک: <code>{stats.get('premium_expires_at')}</code>" if stats.get("premium_expires_at") else ""

    if is_prem:
        quota_line = "♾ <b>نامحدود</b>"
    else:
        recent_24h = stats.get("recent_searches_24h", 0)
        daily_limit = stats.get("daily_limit", settings.DAILY_SEARCH_LIMIT)
        quota_line = f"<b>{recent_24h} از {daily_limit}</b> (در ۲۴ ساعت اخیر)"

    text = (
        f"📊 <b>آمار فعالیت شما:</b>\n\n"
        f"👑 وضعیت اشتراک: <b>{prem_status}</b>{expires_line}\n"
        f"🔎 سقف جستجوی روزانه: {quota_line}\n"
        f"📈 مجموع کل جستجوهای شما: <b>{stats['user_searches']}</b>\n"
        f"🗂 مقالات کش‌شده در پایگاه: <b>{stats['total_cached_papers']}</b>"
    )
    await safe_send_message(message, text[:4000])


@router.message(Command("saved"))
async def cmd_saved(message: Message):
    if not message.from_user:
        return
    if not await check_premium_access(message):
        return
    papers = await db.get_user_bookmarks(message.from_user.id)
    if not papers:
        await safe_send_message(
            message,
            "📚 <b>کتابخانه شخصی شما خالی است.</b>\n\n"
            "💡 برای ذخیره مقاله، پس از جستجو و مشاهده کارت مقاله، دکمه <b>«⭐️ ذخیره مقاله»</b> را بزنید.\n"
            "مقالات ذخیره‌شده اینجا نمایش داده می‌شوند و می‌توانید خروجی CSV، BibTeX و RIS بگیرید."
        )
        return

    lines = [f"📚 <b>کتابخانه شخصی شما ({len(papers)} مقاله):</b>\n"]
    for idx, p in enumerate(papers, 1):
        icon = "🟢" if p.pdf_url else "🟡"
        safe_title = safe_html(p.title, 85)
        safe_year = safe_html(p.year, 10) or "نامشخص"
        lines.append(f"<b>{idx}.</b> {icon} <b>{safe_title}</b> ({safe_year})")

    text = "\n".join(lines)[:3800]
    kb = build_saved_library_keyboard(papers)
    await safe_send_message(message, text, reply_markup=kb, disable_web_page_preview=True)


# ── Subscription & Payment Verification Handlers ──────────────────────

SUBSCRIPTION_TEXT = PREMIUM_PROMO_TEXT


def format_active_subscription_message(sub_info: dict) -> str:
    """Format rich subscription overview for active premium subscribers without bank/card details."""
    if sub_info.get("is_lifetime"):
        time_left_str = "♾ <b>دائمی / نامحدود</b>"
        exp_date_str = "نامحدود"
    else:
        days = sub_info.get("remaining_days", 0)
        hours = sub_info.get("remaining_hours", 0)
        if days > 0:
            time_left_str = f"<b>{days} روز</b> و <b>{hours} ساعت</b>"
        else:
            time_left_str = f"<b>{hours} ساعت</b> (کمتر از ۱ روز باقیمانده)"
        exp_date_str = sub_info.get("expires_at") or "نامشخص"

    text = (
        "💎 <b>اشتراک ویژه شما فعال است</b>\n\n"
        f"⏳ <b>اعتبار باقیمانده:</b> {time_left_str}\n"
        f"📅 <b>تاریخ انقضا:</b> <code>{exp_date_str}</code>\n"
        "🔎 <b>سقف مجاز جستجو:</b> ♾ <b>کاملاً نامحدود</b>\n\n"
        "✨ <b>امکانات فعال شما در این دوره:</b>\n"
        "• 🔍 جستجوی کاملاً نامحدود مقالات بدون سقف ۲۴ ساعته\n"
        "• 📄 مشاهده صفحات بعدی و دریافت بیش از ۲۰ نتیجه\n"
        "• 🇮🇷 ترجمه روان و نامحدود چکیده و تایتل مقالات به زبان فارسی\n"
        "• ⭐️ ذخیره در کتابخانه شخصی (/saved) با خروجی‌های متنوع\n"
        "• 📎 دریافت رفرنس‌های دانشگاهی استاندارد (APA, IEEE, Vancouver)\n"
        "• 🕸 کاوش در شبکه مقالات مشابه و مرتبط\n\n"
        "💡 برای جستجو کافیست عنوان مقاله یا کلیدواژه مورد نظر را ارسال کنید."
    )
    return text[:4000]


@router.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    """Handle /subscribe command: shows active status and remaining days for premium users,
    or bank card details and payment guidance for regular/unsubscribed users."""
    if not message.from_user:
        return
    sub_info = await db.get_user_subscription_details(message.from_user.id)
    if sub_info.get("is_active"):
        await safe_send_message(
            message,
            format_active_subscription_message(sub_info),
            reply_markup=build_active_subscription_keyboard()
        )
    else:
        await safe_send_message(
            message,
            SUBSCRIPTION_TEXT,
            reply_markup=build_subscription_keyboard()
        )



@router.callback_query(F.data == "buy_sub")
async def handle_buy_sub_callback(callback: CallbackQuery):
    """Handle buy subscription button callback."""
    await callback.answer()
    if callback.message:
        await safe_send_message(
            callback.message,
            SUBSCRIPTION_TEXT,
            reply_markup=build_subscription_keyboard()
        )


@router.callback_query(F.data == "check_sub_status")
async def handle_check_sub_status(callback: CallbackQuery):
    """Check and display user subscription status."""
    await callback.answer()
    if not callback.from_user or not callback.message:
        return
    sub_info = await db.get_user_subscription_details(callback.from_user.id)
    if sub_info.get("is_active"):
        await safe_send_message(
            callback.message,
            format_active_subscription_message(sub_info),
            reply_markup=build_active_subscription_keyboard()
        )
    else:
        status_text = (
            "📊 <b>وضعیت اشتراک: عادی (رایگان)</b>\n\n"
            "❌ شما در حال حاضر اشتراک ویژه فعال ندارید.\n\n"
            "برای فعال‌سازی و دسترسی به امکانات ویژه (جستجوی نامحدود، ترجمه فارسی، ذخیره مقالات، رفرنس‌دهی و مقالات مشابه) روی دکمه <b>«💳 خرید اشتراک ویژه»</b> کلیک کنید."
        )
        await safe_send_message(callback.message, status_text, reply_markup=build_subscription_keyboard())


@router.callback_query(F.data == "renew_sub")
async def handle_renew_sub_callback(callback: CallbackQuery):
    """Handle early subscription renewal request from active premium users."""
    await callback.answer()
    if callback.message:
        renew_header = (
            "🔄 <b>تمدید اشتراک ویژه:</b>\n"
            "<i>(با واریز و تایید رسید، ۳۰ روز به انتهای اعتبار فعلی شما افزوده می‌شود.)</i>\n\n"
        )
        await safe_send_message(
            callback.message,
            renew_header + SUBSCRIPTION_TEXT,
            reply_markup=build_subscription_keyboard()
        )


@router.callback_query(F.data == "sub_lib")
async def handle_sub_lib_callback(callback: CallbackQuery):
    """Shortcut to view saved library from subscription keyboard."""
    await callback.answer()
    if not callback.from_user or not callback.message:
        return
    papers = await db.get_user_bookmarks(callback.from_user.id)
    if not papers:
        await safe_send_message(
            callback.message,
            "📚 <b>کتابخانه شخصی شما خالی است.</b>\n\n"
            "💡 برای ذخیره مقاله، پس از جستجو و مشاهده کارت مقاله، دکمه <b>«⭐️ ذخیره مقاله»</b> را بزنید."
        )
        return
    lines = [f"📚 <b>کتابخانه شخصی شما ({len(papers)} مقاله):</b>\n"]
    for idx, p in enumerate(papers, 1):
        icon = "🟢" if p.pdf_url else "🟡"
        safe_title = safe_html(p.title, 85)
        safe_year = safe_html(p.year, 10) or "نامشخص"
        lines.append(f"<b>{idx}.</b> {icon} <b>{safe_title}</b> ({safe_year})")
    text = "\n".join(lines)[:3800]
    kb = build_saved_library_keyboard(papers)
    await safe_send_message(callback.message, text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data == "sub_stats")
async def handle_sub_stats_callback(callback: CallbackQuery):
    """Shortcut to view user stats from subscription keyboard."""
    await callback.answer()
    if not callback.from_user or not callback.message:
        return
    stats = await db.get_user_stats(callback.from_user.id)
    is_prem = stats.get("is_premium")
    prem_status = "💎 فعال (ویژه)" if is_prem else "عادی (رایگان)"
    expires_line = f"\n⏳ انقضای اشتراک: <code>{stats.get('premium_expires_at')}</code>" if stats.get("premium_expires_at") else ""
    quota_line = "♾ <b>نامحدود</b>" if is_prem else f"<b>{stats.get('recent_searches_24h', 0)} از {stats.get('daily_limit', settings.DAILY_SEARCH_LIMIT)}</b> (در ۲۴ ساعت اخیر)"

    text = (
        f"📊 <b>آمار فعالیت شما:</b>\n\n"
        f"👑 وضعیت اشتراک: <b>{prem_status}</b>{expires_line}\n"
        f"🔎 سقف جستجوی روزانه: {quota_line}\n"
        f"📈 مجموع کل جستجوهای شما: <b>{stats['user_searches']}</b>\n"
        f"🗂 مقالات کش‌شده در پایگاه: <b>{stats['total_cached_papers']}</b>"
    )
    await safe_send_message(callback.message, text[:4000])


@router.callback_query(F.data == "send_receipt_guide")
async def handle_send_receipt_guide(callback: CallbackQuery):
    """Handle click on send receipt button, adding user to pending receipts."""
    await callback.answer()
    if not callback.from_user:
        return
    await db.add_pending_receipt(callback.from_user.id)
    guide_text = "📸 لطفاً تصویر یا اسکرین‌شات رسید پرداخت خود را همین‌جا ارسال کنید تا برای بررسی به تیم پشتیبانی ارسال شود."
    if callback.message:
        await safe_send_message(callback.message, guide_text)


@router.message(F.photo | F.document)
async def handle_receipt_upload(message: Message):
    """Receive receipt photo or document from user and forward to admin group."""
    if not message.from_user:
        return
    if not await db.has_pending_receipt(message.from_user.id):
        return

    admin_group_id = getattr(settings, "ADMIN_GROUP_ID", -1003989191024)
    user = message.from_user
    safe_name = safe_html(user.full_name, 100) or "کاربر"
    user_id = user.id
    username_str = f"@{user.username}" if user.username else "ندارد"
    safe_username = safe_html(username_str, 50)

    caption = (
        "📩 <b>رسید پرداخت جدید دریافت شد!</b>\n\n"
        f"👤 <b>کاربر:</b> {safe_name}\n"
        f"🆔 <b>شناسه عددی:</b> <code>{user_id}</code>\n"
        f"🔗 <b>نام کاربری:</b> {safe_username}\n"
        "💰 <b>مبلغ:</b> ۵۰,۰۰۰ تومان (اشتراک ویژه یک‌ماهه)\n\n"
        "لطفاً پس از بررسی حساب، پرداخت را تایید یا رد کنید:"
    )
    admin_kb = build_admin_receipt_keyboard(user_id)

    try:
        if message.photo:
            await message.bot.send_photo(
                chat_id=admin_group_id,
                photo=message.photo[-1].file_id,
                caption=caption,
                reply_markup=admin_kb
            )
        elif message.document:
            await message.bot.send_document(
                chat_id=admin_group_id,
                document=message.document.file_id,
                caption=caption,
                reply_markup=admin_kb
            )

        await db.remove_pending_receipt(user_id)
        await message.answer(
            "✅ رسید شما با موفقیت برای تیم پشتیبانی ارسال شد. پس از بررسی و تایید، اشتراک ویژه یک‌ماهه شما فعال خواهد شد."
        )
    except Exception as e:
        logger.error(f"Error forwarding receipt to admin group {admin_group_id}: {e}")
        await message.answer(
            "⚠️ در ارسال رسید به پشتیبانی خطایی رخ داد. لطفاً چند لحظه بعد مجدداً امتحان کنید یا با پشتیبانی در تماس باشید."
        )


@router.callback_query(F.data.startswith("adm_accept:"))
async def handle_admin_accept(callback: CallbackQuery):
    """Handle admin accepting payment receipt in admin group."""
    parts = callback.data.split(":", 1)
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await callback.answer("خطا در شناسه کاربر.", show_alert=True)
        return

    user_id = int(parts[1])
    await db.set_user_premium(user_id, status=1, days=30)

    # Notify the user
    try:
        await callback.bot.send_message(
            chat_id=user_id,
            text=(
                "🎉 <b>پرداخت شما تایید شد!</b>\n\n"
                "اشتراک ویژه یک‌ماهه (۳۰ روزه) شما با موفقیت فعال گردید. هم‌اکنون به تمامی امکانات ویژه (مشاهده بیش از ۲۰ نتیجه، ذخیره مقالات، مقالات مشابه، دریافت رفرنس و ترجمه روان چکیده به فارسی) دسترسی کامل دارید."
            )
        )
    except Exception as e:
        logger.warning(f"Could not notify user {user_id} of premium approval: {e}")

    # Edit admin message
    admin_name = safe_html(callback.from_user.full_name if callback.from_user else "ادمین", 60)
    status_suffix = f"\n\n✅ <b>تایید شده توسط {admin_name}</b>"
    try:
        if callback.message:
            if callback.message.caption:
                new_caption = (callback.message.caption or "") + status_suffix
                await callback.message.edit_caption(caption=new_caption[:1024], reply_markup=None)
            elif callback.message.text:
                new_text = (callback.message.text or "") + status_suffix
                await callback.message.edit_text(text=new_text[:4096], reply_markup=None)
    except Exception as e:
        logger.warning(f"Failed to edit admin message on accept: {e}")

    await callback.answer("✅ پرداخت تایید و اشتراک کاربر ۳۰ روزه فعال شد.")


@router.callback_query(F.data.startswith("adm_reject:"))
async def handle_admin_reject(callback: CallbackQuery):
    """Handle admin rejecting payment receipt in admin group."""
    parts = callback.data.split(":", 1)
    if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
        await callback.answer("خطا در شناسه کاربر.", show_alert=True)
        return

    user_id = int(parts[1])

    # Notify the user
    try:
        await callback.bot.send_message(
            chat_id=user_id,
            text=(
                "❌ متأسفانه رسید پرداخت شما توسط پشتیبانی رد شد. در صورت وجود مشکل با پشتیبانی در تماس باشید."
            )
        )
    except Exception as e:
        logger.warning(f"Could not notify user {user_id} of premium rejection: {e}")

    # Edit admin message
    admin_name = safe_html(callback.from_user.full_name if callback.from_user else "ادمین", 60)
    status_suffix = f"\n\n❌ <b>رد شده توسط {admin_name}</b>"
    try:
        if callback.message:
            if callback.message.caption:
                new_caption = (callback.message.caption or "") + status_suffix
                await callback.message.edit_caption(caption=new_caption[:1024], reply_markup=None)
            elif callback.message.text:
                new_text = (callback.message.text or "") + status_suffix
                await callback.message.edit_text(text=new_text[:4096], reply_markup=None)
    except Exception as e:
        logger.warning(f"Failed to edit admin message on reject: {e}")

    await callback.answer("❌ رسید رد شد.")


# ── Search Handler ───────────────────────────────────────────────────

@router.message(F.text & ~F.text.startswith("/"))
async def handle_search_query(message: Message):
    query = (message.text or "").strip()
    if len(query) < 3:
        await safe_send_message(message, "⚠️ لطفاً عبارت دقیق‌تری (حداقل ۳ کاراکتر) برای جستجو وارد کنید.")
        return

    if message.from_user:
        user_id = message.from_user.id
        # 1. Anti-spam check: max 25 searches per minute
        if await db.check_rate_limit(user_id, max_per_minute=25):
            await safe_send_message(
                message,
                "⚠️ <b>تعداد درخواست‌های شما بیش از حد مجاز است.</b>\nلطفاً لحظاتی بعد مجدداً امتحان کنید."
            )
            return

        # 2. Daily search quota check for regular (non-premium) users (10 searches / 24 hours)
        is_premium = await db.is_user_premium(user_id)
        if not is_premium:
            recent_searches = await db.get_user_recent_search_count(user_id, hours=24)
            if recent_searches >= settings.DAILY_SEARCH_LIMIT:
                limit_text = (
                    "⚠️ <b>سقف مجاز جستجوی روزانه شما تکمیل شده است!</b>\n\n"
                    f"کاربران عادی مجاز به <b>{settings.DAILY_SEARCH_LIMIT} جستجو در هر ۲۴ ساعت</b> هستند.\n"
                    f"تعداد جستجوهای انجام‌شده توسط شما در ۲۴ ساعت اخیر: <b>{recent_searches} از {settings.DAILY_SEARCH_LIMIT}</b> ❌\n\n"
                    "💎 <b>برای جستجوی نامحدود چه کار کنم؟</b>\n"
                    "با تهیه <b>اشتراک ویژه</b>، سقف روزانه برداشته شده و جستجوی شما <b>کاملاً نامحدود</b> می‌شود. همچنین به امکانات زیر دسترسی خواهید داشت:\n"
                    "• 🔍 جستجوی کاملاً نامحدود و بدون سقف ۲۴ ساعته\n"
                    "• 🇮🇷 ترجمه روان و نامحدود فارسی چکیده و تایتل مقالات\n"
                    "• 📄 مشاهده صفحات بعدی و دریافت بیش از ۲۰ نتیجه\n"
                    "• ⭐️ ذخیره در کتابخانه شخصی (/saved) با خروجی‌های متنوع\n"
                    "• 📎 دریافت رفرنس‌های علمی استاندارد و مقالات مرتبط\n\n"
                    "💳 برای ارتقا به اشتراک ویژه، دستور /subscribe را ارسال کنید یا دکمه‌های زیر را لمس نمایید:"
                )
                kb = build_subscription_keyboard()
                await safe_send_message(message, limit_text, reply_markup=kb)
                return

        await db.log_search(user_id, query)

    # Batch DOI detection: if message contains 2+ DOIs, handle them together
    dois = extract_multiple_dois(query)
    if len(dois) >= 2:
        status_msg = await safe_send_message(
            message,
            f"🔍 تعداد {len(dois[:10])} شناسه DOI شناسایی شد. در حال استعلام همزمان مقالات..."
        )
        papers = await aggregator.lookup_multiple_dois(dois[:10])

        if not papers:
            not_found_msg = "❌ هیچ‌یک از شناسه‌های DOI ارسال‌شده یافت نشد.\n💡 لطفاً شناسه‌ها را بررسی و مجدداً ارسال کنید."
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

        await db.save_papers_batch(papers)

        list_text = format_papers_list_message(papers, f"Batch DOI ({len(papers)} مقاله)", page=1, start_idx=1)
        list_kb = build_results_list_keyboard(papers, start_idx=1)
        await safe_send_message(message, list_text, reply_markup=list_kb, disable_web_page_preview=True)
        return

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
        is_saved = False
        if message.from_user:
            is_saved = await db.is_bookmarked(message.from_user.id, single_paper.short_id)
        card_html = format_paper_card(single_paper, 1)
        kb = build_paper_keyboard(single_paper, is_saved=is_saved)
        await safe_send_message(message, card_html, reply_markup=kb, disable_web_page_preview=True)
        return

    # Normal case: Multiple results returned -> batch save and single unified list message
    await db.save_papers_batch(papers)

    session_id = None
    next_page = None
    if len(papers) >= settings.MAX_RESULTS:
        session_id = hashlib.sha256(query.strip().lower().encode("utf-8")).hexdigest()[:10]
        session_cache.set(session_id, {"query": query, "page": 1, "papers": papers})
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
        "/stats — آمار فعالیت و مقالات کش‌شده\n"
        "/saved — مشاهده کتابخانه شخصی و خروجی اکسل/EndNote\n\n"
        "💡 برای جستجوی مقاله، کافیست <b>عنوان، کلیدواژه، شناسه DOI یا arXiv ID</b> را بدون اسلش (/) ارسال کنید."
    )
    await safe_send_message(message, unknown_text[:4000])


# ── Callback Handlers ────────────────────────────────────────────────

@router.callback_query(F.data.startswith("more:"))
async def handle_more_results_callback(callback: CallbackQuery):
    if not callback.from_user:
        return
    if not await check_premium_access(callback):
        return
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
        session_cache.set(session_id, {"query": query, "page": target_page, "papers": papers})
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


@router.callback_query(F.data.startswith("flt:"))
async def handle_filter_callback(callback: CallbackQuery):
    """Handle filter/sort buttons: flt:{session_id}:{mode}"""
    await callback.answer()

    parts = callback.data.split(":")
    if len(parts) < 3:
        return

    session_id = parts[1]
    mode = parts[2]

    # Retrieve papers from session cache or re-search
    session_data = session_cache.get(session_id)
    papers = None
    query = ""

    if session_data:
        papers = session_data.get("papers")
        query = session_data.get("query", "")

    if not papers:
        # Fallback: re-search from db session
        db_session = await db.get_search_session(session_id)
        if db_session:
            query = db_session["query"]
            papers = await aggregator.search_papers(query, page=1, page_size=settings.MAX_RESULTS)
            if papers:
                await db.save_papers_batch(papers)

    if not papers:
        if callback.message:
            await safe_send_message(callback.message, "⚠️ مهلت این جستجو منقضی شده. لطفاً مجدداً جستجو کنید.")
        return

    sorted_papers = filter_and_sort_papers(papers, mode)

    sort_labels = {
        "cit": "🔥 مرتب‌شده بر اساس بیشترین ارجاع",
        "new": "🆕 مرتب‌شده بر اساس جدیدترین",
        "oa": "🟢 اولویت مقالات دارای PDF رایگان",
    }
    sort_label = sort_labels.get(mode, "")

    list_text = format_papers_list_message(sorted_papers, query, page=1, start_idx=1, sort_label=sort_label)
    list_kb = build_results_list_keyboard(sorted_papers, start_idx=1, session_id=session_id)

    if callback.message:
        await safe_edit_text(
            callback.message,
            list_text,
            reply_markup=list_kb,
            disable_web_page_preview=True
        )


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

    is_saved = False
    if callback.from_user:
        is_saved = await db.is_bookmarked(callback.from_user.id, short_id)

    card_html = format_paper_card(paper, idx)
    kb = build_paper_keyboard(paper, is_saved=is_saved)
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


@router.callback_query(F.data.startswith("tr:"))
async def handle_translate_callback(callback: CallbackQuery):
    """Handle Persian translation of abstract and title for premium users."""
    if not callback.from_user:
        return

    # Check premium subscription
    if not await check_premium_access(callback):
        return

    await callback.answer("🇮🇷 در حال ترجمه چکیده به فارسی...")

    parts = callback.data.split(":", 1)
    if len(parts) < 2:
        return

    short_id = parts[1]
    paper = await db.get_paper(short_id)

    if not paper:
        if callback.message:
            await safe_send_message(callback.message, "⚠️ اطلاعات این مقاله در حافظه منقضی شده است. لطفاً مجدداً جستجو کنید.")
        return

    empty_abstracts = {
        "چکیده‌ای برای این مقاله ثبت نشده است.",
        "چکیده‌ای برای این مقاله در پایگاه ثبت نشده است.",
        "چکیده‌ای برای این مقاله در دسترس نیست.",
        "بدون چکیده", "چکیده‌ای ثبت نشده است."
    }

    if not paper.abstract or paper.abstract.strip() in empty_abstracts:
        if callback.message:
            await safe_send_message(
                callback.message,
                f"⚠️ چکیده‌ای برای مقاله <b>{safe_html(paper.title, 200)}</b> در پایگاه ثبت نشده است تا ترجمه شود."
            )
        return

    # Status update for user
    status_msg = None
    if callback.message:
        status_msg = await safe_send_message(
            callback.message,
            "⏳ در حال ترجمه هوشمند عنوان و چکیده مقاله به فارسی روان..."
        )

    # Translate title and abstract concurrently with resilient multi-engine fallbacks
    translated_title, translated_abstract = await asyncio.gather(
        aggregator.translate_to_persian(paper.title),
        aggregator.translate_to_persian(paper.abstract[:2500]),
        return_exceptions=True
    )

    if status_msg:
        try:
            await status_msg.delete()
        except Exception:
            pass

    t_title = translated_title if isinstance(translated_title, str) else None
    t_abstract = translated_abstract if isinstance(translated_abstract, str) else None

    safe_title_en = safe_html(paper.title, 300)
    safe_title_fa = safe_html(t_title, 300) if t_title else "ترجمه عنوان ممکن نشد."
    safe_abstract_fa = safe_html(t_abstract, 3200) if t_abstract else "ترجمه چکیده ممکن نشد یا در دسترس نیست."

    text = (
        f"🇮🇷 <b>ترجمه فارسی مقاله:</b>\n\n"
        f"📌 <b>عنوان انگلیسی:</b>\n{safe_title_en}\n\n"
        f"📌 <b>عنوان فارسی:</b>\n{safe_title_fa}\n\n"
        f"📖 <b>ترجمه روان چکیده:</b>\n{safe_abstract_fa}"
    )
    if callback.message:
        await safe_send_message(callback.message, text[:4000])


@router.callback_query(F.data.startswith("cite:"))
async def handle_cite_callback(callback: CallbackQuery):
    """Handle citation/reference generation."""
    if not callback.from_user:
        return

    if not await check_premium_access(callback):
        return

    await callback.answer()

    parts = callback.data.split(":", 1)
    if len(parts) < 2:
        return

    short_id = parts[1]
    paper = await db.get_paper(short_id)

    if not paper:
        if callback.message:
            await safe_send_message(callback.message, "⚠️ اطلاعات این مقاله در حافظه منقضی شده است.")
        return

    citations = generate_citations(paper)
    safe_apa = safe_html(citations["apa"], 600)
    safe_ieee = safe_html(citations["ieee"], 600)
    safe_vancouver = safe_html(citations["vancouver"], 600)

    text = (
        f"📎 <b>رفرنس‌های مقاله:</b>\n\n"
        f"🔹 <b>APA:</b>\n<code>{safe_apa}</code>\n\n"
        f"🔹 <b>IEEE:</b>\n<code>{safe_ieee}</code>\n\n"
        f"🔹 <b>Vancouver:</b>\n<code>{safe_vancouver}</code>"
    )
    kb = build_cite_keyboard(short_id)
    if callback.message:
        await safe_send_message(callback.message, text[:4000], reply_markup=kb)


@router.callback_query(F.data.startswith("dlbib:"))
async def handle_download_bibtex_callback(callback: CallbackQuery):
    """Handle BibTeX file download."""
    if not callback.from_user:
        return

    if not await check_premium_access(callback):
        return

    await callback.answer("📥 در حال ساخت فایل BibTeX...")

    parts = callback.data.split(":", 1)
    if len(parts) < 2:
        return

    short_id = parts[1]
    paper = await db.get_paper(short_id)

    if not paper:
        if callback.message:
            await safe_send_message(callback.message, "⚠️ اطلاعات مقاله منقضی شده است.")
        return

    content = generate_bibtex([paper])
    doc_file = BufferedInputFile(content.encode("utf-8"), filename=f"paper_{short_id}.bib")
    if callback.message:
        try:
            await callback.message.answer_document(document=doc_file, caption="📚 فایل BibTeX مقاله")
        except Exception as e:
            logger.warning(f"Error sending BibTeX file: {e}")


@router.callback_query(F.data.startswith("dlris:"))
async def handle_download_ris_callback(callback: CallbackQuery):
    """Handle RIS file download."""
    if not callback.from_user:
        return

    if not await check_premium_access(callback):
        return

    await callback.answer("📥 در حال ساخت فایل RIS...")

    parts = callback.data.split(":", 1)
    if len(parts) < 2:
        return

    short_id = parts[1]
    paper = await db.get_paper(short_id)

    if not paper:
        if callback.message:
            await safe_send_message(callback.message, "⚠️ اطلاعات مقاله منقضی شده است.")
        return

    content = generate_ris([paper])
    doc_file = BufferedInputFile(content.encode("utf-8"), filename=f"paper_{short_id}.ris")
    if callback.message:
        try:
            await callback.message.answer_document(document=doc_file, caption="🗂 فایل RIS مقاله (EndNote/Mendeley)")
        except Exception as e:
            logger.warning(f"Error sending RIS file: {e}")


@router.callback_query(F.data.startswith("relp:"))
async def handle_related_papers_callback(callback: CallbackQuery):
    """Handle related papers discovery."""
    if not callback.from_user:
        return

    if not await check_premium_access(callback):
        return

    await callback.answer("🕸 در حال جستجوی مقالات مشابه...")

    parts = callback.data.split(":", 1)
    if len(parts) < 2:
        return

    short_id = parts[1]
    paper = await db.get_paper(short_id)

    if not paper:
        if callback.message:
            await safe_send_message(callback.message, "⚠️ اطلاعات این مقاله در حافظه منقضی شده است.")
        return

    safe_title_preview = safe_html(paper.title, 100)
    wait_msg = None
    if callback.message:
        wait_msg = await safe_send_message(
            callback.message,
            f"🕸 در حال یافتن مقالات مشابه با:\n<b>{safe_title_preview}</b>..."
        )

    related_papers = await aggregator.get_related_papers(paper, limit=10)

    if wait_msg:
        try:
            await wait_msg.delete()
        except Exception:
            pass

    if not related_papers:
        if callback.message:
            await safe_send_message(callback.message, "❌ مقاله مشابهی یافت نشد.")
        return

    await db.save_papers_batch(related_papers)

    safe_original = safe_html(paper.title, 60)
    lines = [f"🕸 <b>مقالات مشابه با:</b> «{safe_original}»\n"]
    for idx, rp in enumerate(related_papers, 1):
        icon = "🟢" if rp.pdf_url else "🟡"
        safe_t = safe_html(rp.title, 85)
        safe_y = safe_html(rp.year, 10) or "نامشخص"
        lines.append(f"<b>{idx}.</b> {icon} <b>{safe_t}</b> ({safe_y})")

    text = "\n".join(lines)[:3800]
    kb = build_results_list_keyboard(related_papers, start_idx=1)
    if callback.message:
        await safe_send_message(callback.message, text, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("save:"))
async def handle_save_callback(callback: CallbackQuery):
    """Handle bookmark toggle."""
    if not callback.from_user:
        return

    if not await check_premium_access(callback):
        return

    parts = callback.data.split(":", 1)
    if len(parts) < 2:
        await callback.answer()
        return

    short_id = parts[1]
    paper = await db.get_paper(short_id)

    if not paper:
        await callback.answer("⚠️ اطلاعات مقاله منقضی شده است.")
        return

    is_now_saved = await db.toggle_bookmark(callback.from_user.id, short_id)

    if is_now_saved:
        await callback.answer("✅ به کتابخانه شخصی (/saved) اضافه شد.")
    else:
        await callback.answer("🗑 از کتابخانه شخصی حذف شد.")

    # Update keyboard to reflect new bookmark state
    try:
        if callback.message:
            await callback.message.edit_reply_markup(
                reply_markup=build_paper_keyboard(paper, is_saved=is_now_saved)
            )
    except TelegramBadRequest:
        pass
    except Exception as e:
        logger.warning(f"Error updating bookmark keyboard: {e}")


@router.callback_query(F.data.startswith("exp:"))
async def handle_export_callback(callback: CallbackQuery):
    """Handle library export (CSV, BibTeX, RIS) and clear."""
    if not callback.from_user:
        return

    if not await check_premium_access(callback):
        return

    await callback.answer()

    parts = callback.data.split(":", 1)
    if len(parts) < 2:
        return

    export_type = parts[1]

    if export_type == "clear":
        await db.clear_user_bookmarks(callback.from_user.id)
        if callback.message:
            await safe_edit_text(
                callback.message,
                "🗑 <b>کتابخانه شخصی شما با موفقیت پاک شد.</b>\n\n"
                "💡 برای ذخیره مقاله جدید، پس از جستجو دکمه «⭐️ ذخیره مقاله» را بزنید."
            )
        return

    papers = await db.get_user_bookmarks(callback.from_user.id)
    if not papers:
        if callback.message:
            await safe_send_message(callback.message, "📚 کتابخانه شخصی شما خالی است.")
        return

    if export_type == "csv":
        content = generate_csv_bytes(papers)
        doc_file = BufferedInputFile(content, filename="saved_papers.csv")
        caption = f"📊 خروجی اکسل — {len(papers)} مقاله ذخیره‌شده"
    elif export_type == "bib":
        content = generate_bibtex(papers).encode("utf-8")
        doc_file = BufferedInputFile(content, filename="saved_papers.bib")
        caption = f"📚 خروجی BibTeX — {len(papers)} مقاله ذخیره‌شده"
    elif export_type == "ris":
        content = generate_ris(papers).encode("utf-8")
        doc_file = BufferedInputFile(content, filename="saved_papers.ris")
        caption = f"🗂 خروجی RIS (EndNote) — {len(papers)} مقاله ذخیره‌شده"
    else:
        return

    if callback.message:
        try:
            await callback.message.answer_document(document=doc_file, caption=caption[:1000])
        except Exception as e:
            logger.warning(f"Error sending export file: {e}")


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
