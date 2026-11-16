from urllib.parse import quote
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
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
            encoded_doi = quote(clean_d, safe="")
            link_row.append(InlineKeyboardButton(text="🔗 لینک DOI", url=f"https://doi.org/{encoded_doi}"))

    if link_row:
        buttons.append(link_row)

    return InlineKeyboardMarkup(inline_keyboard=buttons)
