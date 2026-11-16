# 🎓 Paper Finder Telegram Bot (ربات تلگرامی مقالهیاب)

ربات تلگرامی سریع، ماژولار و کاملاً رایگان به زبان Python (`aiogram 3.x`) برای جستجوی مقالات علمی، نمایش چکیده (Abstract) و دانلود مستقیم فایل PDF از پایگاههای **OpenAlex**، **arXiv**، **Semantic Scholar** و **Unpaywall** به همراه سیستم Fallback هوشمند.

## ✨ ویژگیها
- **بدون نیاز به کپچا یا سرویس پولی:** متصل به APIهای رسمی دانشگاهی (OpenAlex Polite Pool, arXiv, Semantic Scholar, Unpaywall).
- **پشتیبانی از جستجوی مستقیم با شناسه:** دریافت مستقیم `DOI`، شناسه `arXiv` و `PMID`.
- **سازگار با پروکسی لوکال ویندوز:** پشتیبانی از پروکسیهای SOCKS5 و HTTP (مانند v2rayN / Nekoray).
- **آماده دیپلوی روی VPS و Hugging Face Spaces:** دارای وبسرور سلامت داخلی روی پورت `7860` و داکرفایل استاندارد.

## 🚀 راهاندازی سریع در ویندوز (Local)
1. کپی کردن فایل تنظیمات:
   ```powershell
   copy .env.example .env
   ```
2. مقدار `BOT_TOKEN`، `CONTACT_EMAIL` و در صورت نیاز `TELEGRAM_PROXY=socks5://127.0.0.1:10808` را در فایل `.env` قرار دهید.
3. نصب پیشنیازها و اجرا:
   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   python main.py
   ```

## 🐳 اجرا روی VPS با Docker Compose
```bash
cp .env.example .env
nano .env
docker compose up -d --build
```
