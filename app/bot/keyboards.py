from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from app.services.models import PaperItem


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
    if paper.landing_url and paper.landing_url.startswith("http"):
        link_row.append(InlineKeyboardButton(text="🌐 صفحه مقاله", url=paper.landing_url))
    if paper.doi:
        link_row.append(InlineKeyboardButton(text="🔗 لینک DOI", url=f"https://doi.org/{paper.doi}"))

    if link_row:
        buttons.append(link_row)

    return InlineKeyboardMarkup(inline_keyboard=buttons)
