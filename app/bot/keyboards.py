from typing import Optional
from urllib.parse import quote
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from app.config import settings
from app.services.models import PaperItem
from app.services.aggregator import clean_doi


def build_paper_keyboard(paper: PaperItem) -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton(
                text="📖 نمایش چکیده (Abstract)",
                callback_data=f"abs:{paper.short_id}"
            ),
            InlineKeyboardButton(
                text="📥 دریافت فایل PDF",
                callback_data=f"pdf:{paper.short_id}"
            )
        ]
    ]

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
    """Build keyboard with 5-per-row numbered selection buttons and optional next page button."""
    buttons: list[list[InlineKeyboardButton]] = []
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
