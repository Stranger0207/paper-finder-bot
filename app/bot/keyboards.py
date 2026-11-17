from typing import Optional
from urllib.parse import quote
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from app.config import settings
from app.services.models import PaperItem
from app.services.aggregator import clean_doi


def build_paper_keyboard(paper: PaperItem, is_saved: bool = False, is_premium: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        # Row 1: Abstract + Persian Translation
        [
            InlineKeyboardButton(
                text="📖 چکیده",
                callback_data=f"abs:{paper.short_id}"
            ),
            InlineKeyboardButton(
                text="🇮🇷 ترجمه فارسی",
                callback_data=f"tr:{paper.short_id}"
            )
        ],
        # Row 2: PDF Download + Citation
        [
            InlineKeyboardButton(
                text="📥 دریافت PDF",
                callback_data=f"pdf:{paper.short_id}"
            ),
            InlineKeyboardButton(
                text="📎 رفرنس (Cite)",
                callback_data=f"cite:{paper.short_id}"
            )
        ],
        # Row 3: Related Papers + Save/Bookmark
        [
            InlineKeyboardButton(
                text="🕸 مقالات مشابه",
                callback_data=f"relp:{paper.short_id}"
            ),
            InlineKeyboardButton(
                text="✅ ذخیره‌شده (حذف)" if is_saved else "⭐️ ذخیره مقاله",
                callback_data=f"save:{paper.short_id}"
            )
        ]
    ]

    # Row 4: External links
    link_row = []
    if paper.landing_url and str(paper.landing_url).startswith("http"):
        link_row.append(InlineKeyboardButton(text="🌐 صفحه مقاله", url=str(paper.landing_url)))
    if paper.doi:
        clean_d = clean_doi(paper.doi)
        if clean_d:
            encoded_doi = quote(clean_d, safe="/")
            link_row.append(InlineKeyboardButton(text="🔗 لینک DOI", url=f"https://doi.org/{encoded_doi}"))

    if link_row:
        buttons.append(link_row)

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_results_list_keyboard(
    papers: list[PaperItem],
    start_idx: int = 1,
    session_id: Optional[str] = None,
    next_page: Optional[int] = None
) -> InlineKeyboardMarkup:
    """Build keyboard with optional filter row, 5-per-row numbered selection buttons and optional next page button."""
    buttons: list[list[InlineKeyboardButton]] = []

    # Filter/Sort row (only if session_id is available)
    if session_id:
        buttons.append([
            InlineKeyboardButton(text="🔥 پراستنادها", callback_data=f"flt:{session_id}:cit"),
            InlineKeyboardButton(text="🆕 جدیدترین", callback_data=f"flt:{session_id}:new"),
            InlineKeyboardButton(text="🟢 دارای PDF", callback_data=f"flt:{session_id}:oa"),
        ])

    current_row: list[InlineKeyboardButton] = []

    for offset, paper in enumerate(papers):
        idx = start_idx + offset
        current_row.append(
            InlineKeyboardButton(
                text=f"📄 {idx}",
                callback_data=f"sel:{paper.short_id}:{idx}"
            )
        )
        if len(current_row) == 5:
            buttons.append(current_row)
            current_row = []

    if current_row:
        buttons.append(current_row)

    if len(papers) >= settings.MAX_RESULTS and session_id and next_page:
        buttons.append([
            InlineKeyboardButton(
                text=f"➕ نمایش ۲۰ نتیجه بعدی (صفحه {next_page})",
                callback_data=f"more:{session_id}:{next_page}"
            )
        ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_cite_keyboard(short_id: str) -> InlineKeyboardMarkup:
    """Build keyboard with BibTeX and RIS download buttons."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📥 فایل BibTeX (.bib)", callback_data=f"dlbib:{short_id}"),
            InlineKeyboardButton(text="📥 فایل EndNote (.ris)", callback_data=f"dlris:{short_id}")
        ]
    ])


def build_saved_library_keyboard(papers: list[PaperItem]) -> InlineKeyboardMarkup:
    """Build keyboard for saved library with paper selection and export buttons."""
    buttons: list[list[InlineKeyboardButton]] = []
    current_row: list[InlineKeyboardButton] = []

    for idx, p in enumerate(papers, 1):
        current_row.append(
            InlineKeyboardButton(
                text=f"📄 {idx}",
                callback_data=f"sel:{p.short_id}:{idx}"
            )
        )
        if len(current_row) == 5:
            buttons.append(current_row)
            current_row = []

    if current_row:
        buttons.append(current_row)

    # Export row 1
    buttons.append([
        InlineKeyboardButton(text="📊 خروجی اکسل (CSV)", callback_data="exp:csv"),
        InlineKeyboardButton(text="📚 خروجی BibTeX", callback_data="exp:bib"),
    ])
    # Export row 2
    buttons.append([
        InlineKeyboardButton(text="🗂 خروجی EndNote (RIS)", callback_data="exp:ris"),
        InlineKeyboardButton(text="🗑 پاک کردن لیست", callback_data="exp:clear"),
    ])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_subscription_keyboard() -> InlineKeyboardMarkup:
    """Build keyboard for premium subscription options (for users without active subscription)."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💳 خرید اشتراک ویژه (۵۰,۰۰۰ تومان)", callback_data="buy_sub")
        ],
        [
            InlineKeyboardButton(text="📤 ارسال رسید پرداخت", callback_data="send_receipt_guide")
        ],
        [
            InlineKeyboardButton(text="📊 وضعیت اشتراک من", callback_data="check_sub_status")
        ]
    ])


def build_active_subscription_keyboard() -> InlineKeyboardMarkup:
    """Build keyboard for users who already possess an active premium subscription."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📚 کتابخانه شخصی من (/saved)", callback_data="sub_lib"),
            InlineKeyboardButton(text="📊 آمار فعالیت (/stats)", callback_data="sub_stats")
        ],
        [
            InlineKeyboardButton(text="🔄 تمدید اشتراک ویژه", callback_data="renew_sub")
        ]
    ])


def build_admin_receipt_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Build approval keyboard for admin group with accept and reject buttons."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ تایید پرداخت", callback_data=f"adm_accept:{user_id}"),
            InlineKeyboardButton(text="❌ رد پرداخت", callback_data=f"adm_reject:{user_id}")
        ]
    ])
