import html
import re
from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, BufferedInputFile

from app.config import settings
from app.database import db
from app.services.aggregator import aggregator
from app.services.models import PaperItem
from app.bot.keyboards import build_paper_keyboard

router = Router()


def format_paper_card(paper: PaperItem, index: int) -> str:
    title = html.escape(paper.title)
    authors = html.escape(paper.authors)
    venue = html.escape(paper.venue)
    year = html.escape(paper.year)
    source = html.escape(paper.source)
    doi_text = html.escape(paper.doi) if paper.doi else "ندارد"
    oa_badge = "🟢 دسترسی باز (Open Access)" if paper.pdf_url else "🟡 نیازمند استعلام / DOI"

    return (
        f"📄 <b>{index}. {title}</b>\n\n"
        f"✍️ <b>نویسندگان:</b> {authors}\n"
        f"🏛 <b>انتشارات:</b> {venue} ({year})\n"
        f"📊 <b>تعداد ارجاعات:</b> {paper.citation_count} | <b>منبع:</b> {source}\n"
        f"🆔 <b>DOI:</b> <code>{doi_text}</code>\n"
        f"📌 <b>وضعیت فایل:</b> {oa_badge}"
    )


@router.message(CommandStart())
async def cmd_start(message: Message):
    user = message.from_user
    if user:
        await db.register_or_update_user(user.id, user.username, user.full_name)

    welcome_text = (
        "🎓 <b>به ربات هوشمند مقالهیاب (Paper Finder Bot) خوش آمدید!</b>\n\n"
        "من به پایگاههای علمی <b>OpenAlex</b>، <b>arXiv</b>، <b>Semantic Scholar</b> و <b>Unpaywall</b> متصل هستم و بدون نیاز به کپچا مقالات علمی را برای شما پیدا میکنم.\n\n"
        "🔍 <b>چطور جستجو کنم؟</b>\n"
        "• <b>موضوع یا عنوان مقاله</b> را به انگلیسی ارسال کنید (مثلاً: <code>Transformer attention mechanism</code>).\n"
        "• یا مستقیماً <b>شناسه DOI</b> را بفرستید (مثلاً: <code>10.1038/nature14539</code>).\n"
        "• یا <b>شناسه arXiv / PMID</b> را ارسال کنید (مثلاً: <code>1706.03762</code>).\n\n"
        "📌 دستورات کاربردی:\n"
        "/stats — مشاهده آمار جستجوهای شما\n"
        "/help — راهنمای کامل استفاده"
    )
    await message.answer(welcome_text)


@router.message(Command("help"))
async def cmd_help(message: Message):
    help_text = (
        "📚 <b>راهنمای استفاده از ربات مقالهیاب:</b>\n\n"
        "1️⃣ <b>جستجوی کلیدواژه:</b> عبارت تخصصی یا عنوان مقاله را تایپ و ارسال کنید تا ۵ مقاله برتر به همراه دکمه دانلود PDF و چکیده نمایش داده شوند.\n"
        "2️⃣ <b>دانلود با DOI:</b> اگر مقاله در ژورنالهای پولی باشد، کافیست روی دکمه <b>«📥 دریافت فایل PDF»</b> بزنید تا ربات از طریق Unpaywall و آینههای پشتیبان، فایل PDF را استخراج و مستقیماً در تلگرام آپلود کند.\n"
        "3️⃣ <b>مشاهده چکیده:</b> با زدن دکمه <b>«📖 نمایش چکیده»</b>، خلاصه کامل مقاله در یک پیام مجزا ارسال میشود."
    )
    await message.answer(help_text)


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if not message.from_user:
        return
    stats = await db.get_user_stats(message.from_user.id)
    await message.answer(
        f"📊 <b>آمار فعالیت شما:</b>\n\n"
        f"🔎 تعداد جستجوهای شما: <b>{stats['user_searches']}</b>\n"
        f"🗂 مقالات کششده در دیتابیس: <b>{stats['total_cached_papers']}</b>"
    )


@router.message(F.text)
async def handle_search_query(message: Message):
    query = (message.text or "").strip()
    if len(query) < 3:
        await message.answer("⚠️ لطفاً عبارت دقیقتری (حداقل ۳ کاراکتر) برای جستجو وارد کنید.")
        return

    if message.from_user:
        await db.log_search(message.from_user.id, query)

    status_msg = await message.answer("🔍 در حال جستجو در پایگاههای علمی (OpenAlex, arXiv, Semantic Scholar)...")

    papers = await aggregator.search_papers(query)
    if not papers:
        await status_msg.edit_text(
            "❌ مقالهای با این مشخصات یافت نشد.\n"
            "💡 پیشنهاد: کلیدواژههای انگلیسی، عنوان دقیقتر یا شناسه DOI را امتحان کنید."
        )
        return

    await status_msg.delete()

    for idx, paper in enumerate(papers, start=1):
        await db.save_paper(paper)
        card_html = format_paper_card(paper, idx)
        kb = build_paper_keyboard(paper)
        await message.answer(card_html, reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("abs:"))
async def handle_abstract_callback(callback: CallbackQuery):
    short_id = callback.data.split(":", 1)[1]
    paper = await db.get_paper(short_id)

    if not paper:
        await callback.answer("⚠️ اطلاعات این مقاله در حافظه منقضی شده است. لطفاً مجدداً جستجو کنید.", show_alert=True)
        return

    await callback.answer()
    title = html.escape(paper.title)
    abstract = html.escape(paper.abstract)

    text = (
        f"📖 <b>چکیده مقاله (Abstract):</b>\n"
        f"<b>{title}</b>\n\n"
        f"{abstract}"
    )
    await callback.message.answer(text)


@router.callback_query(F.data.startswith("pdf:"))
async def handle_pdf_callback(callback: CallbackQuery):
    short_id = callback.data.split(":", 1)[1]
    paper = await db.get_paper(short_id)

    if not paper:
        await callback.answer("⚠️ اطلاعات این مقاله در حافظه منقضی شده است. لطفاً دوباره جستجو کنید.", show_alert=True)
        return

    await callback.answer("⏳ در حال بررسی و استخراج فایل PDF...")
    wait_msg = await callback.message.answer(
        f"⏳ در حال استعلام و آمادهسازی فایل PDF برای:\n<b>{html.escape(paper.title[:100])}</b>..."
    )

    pdf_url = await aggregator.get_best_pdf_url(paper)
    if not pdf_url:
        fallback_links = []
        if paper.doi:
            for idx, mirror in enumerate(settings.mirrors_list, start=1):
                fallback_links.append(f"🔗 <a href='{mirror}/{html.escape(paper.doi)}'>لینک کمکی آینه {idx}</a>")
        extra_msg = "\n".join(fallback_links) if fallback_links else ""
        await wait_msg.edit_text(
            f"❌ متأسفانه نسخه PDF مستقیم برای این مقاله به صورت باز یافت نشد.\n\n{extra_msg}",
            disable_web_page_preview=True
        )
        return

    pdf_bytes = await aggregator.download_pdf_bytes(pdf_url)
    if pdf_bytes:
        safe_filename = re.sub(r"[^a-zA-Z0-9_-]", "_", paper.title[:50]).strip("_") or "paper"
        doc_file = BufferedInputFile(pdf_bytes, filename=f"{safe_filename}.pdf")
        caption = (
            f"📄 <b>{html.escape(paper.title[:200])}</b>\n"
            f"📅 سال انتشار: {html.escape(paper.year)}\n"
            f"🆔 DOI: <code>{html.escape(paper.doi or 'N/A')}</code>"
        )
        await callback.message.answer_document(document=doc_file, caption=caption)
        await wait_msg.delete()
    else:
        await wait_msg.edit_text(
            f"✅ <b>لینک دانلود فایل PDF آماده شد:</b>\n\n"
            f"📥 <a href='{html.escape(pdf_url)}'>برای دانلود مستقیم فایل PDF اینجا کلیک کنید</a>\n\n"
            f"<i>(به دلیل حجم بالای فایل یا محدودیت فایروال ناشر، لینک مستقیم ارسال شد.)</i>",
            disable_web_page_preview=True
        )
