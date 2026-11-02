import asyncio
import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Dict, Any, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
import yt_dlp

# --- Конфигурация путей ---
BASE_DIR = Path(__file__).resolve().parent
TEMP_DIR = BASE_DIR / "temp"
TEMP_DIR.mkdir(parents=True, exist_ok=True)
COOKIES_FILE = BASE_DIR / "cookies.txt"
LOG_FILE = BASE_DIR / "bot.log"

BOT_TOKEN = "8608779506:AAHFk39BYTlxqvq-Mql_iGhyf8ZSHK3ItVM"

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(LOG_FILE), encoding="utf-8"),
    ],
)
logger = logging.getLogger("UniversalMediaBot")

URL_REGEX = re.compile(
    r"https?://(?:www.)?[-a-zA-Z0-9@:%._+~#=]{1,256}.[a-zA-Z0-9()]{1,6}[-a-zA-Z0-9()@:%_+.~#?&//=]*"
)

# Кэш задач: task_id -> {url, title, uploader, duration}
TASKS_CACHE: Dict[str, Dict[str, Any]] = {}


def extract_url(text: str) -> Optional[str]:
    if not text:
        return None
    match = URL_REGEX.search(text)
    return match.group(0) if match else None


def clean_filename(name: str) -> str:
    cleaned = re.sub(r'[\/*?:"<>|]', "", name).strip()
    return cleaned[:80] if cleaned else "audio"


# --- Скачивание через yt-dlp ---
def _ytdlp_download_video(url: str, work_dir: Path) -> tuple[bool, Optional[Dict[str, Any]], str]:
    file_id = uuid.uuid4().hex[:8]
    outtmpl = str(work_dir / f"{file_id}.%(ext)s")

    ydl_opts = {
        "format": "bestvideo[filesize<45M][ext=mp4]+bestaudio[ext=m4a]/bestvideo[filesize<48M]+bestaudio/best[filesize<48M]/best",
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "max_filesize": 49 * 1024 * 1024,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "mweb", "web"]
            }
        },
    }

    if COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 0:
        ydl_opts["cookiefile"] = str(COOKIES_FILE)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        if not info:
            return False, None, "Не удалось получить метаданные видео."

        # Ищем готовый видеофайл
        target_file = None
        for f in work_dir.glob(f"{file_id}*.*"):
            if f.suffix.lower() in [".mp4", ".mkv", ".webm", ".mov"]:
                target_file = f
                break

        if not target_file or not target_file.exists():
            return False, None, "Файл видео не найден после скачивания."

        return True, {
            "title": info.get("title") or "Видео",
            "uploader": info.get("uploader") or info.get("channel") or "",
            "duration": info.get("duration"),
            "file_path": target_file,
            "url": url,
        }, ""

    except Exception as e:
        err = str(e)
        logger.warning(f"yt-dlp error for {url}: {err}")
        if "max-filesize" in err.lower() or "larger than" in err.lower():
            return False, None, "Видео весит более 50 МБ (лимит отправки видео в Telegram)."
        if "confirm you’re not a bot" in err or "sign in" in err.lower():
            return False, None, "Это видео с возрастным ограничением (18+) или защищено YouTube."
        return False, None, f"Ошибка скачивания: {err.splitlines()[-1][:120]}"


def _ytdlp_download_mp3(url: str, work_dir: Path) -> tuple[bool, Optional[Path], str]:
    file_id = uuid.uuid4().hex[:8]
    outtmpl = str(work_dir / f"{file_id}.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": outtmpl,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ],
        "quiet": True,
        "no_warnings": True,
        "max_filesize": 50 * 1024 * 1024,
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "mweb", "web"]
            }
        },
    }

    if COOKIES_FILE.exists() and COOKIES_FILE.stat().st_size > 0:
        ydl_opts["cookiefile"] = str(COOKIES_FILE)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        target_file = None
        for f in work_dir.glob(f"{file_id}*.mp3"):
            target_file = f
            break

        if not target_file or not target_file.exists():
            return False, None, "MP3 файл не найден после конвертации."

        return True, target_file, ""

    except Exception as e:
        err = str(e)
        logger.warning(f"yt-dlp audio error: {err}")
        return False, None, f"Ошибка загрузки аудио: {err.splitlines()[-1][:120]}"


def convert_video_file_to_mp3(input_file: Path, output_file: Path) -> bool:
    try:
        cmd = [
            "ffmpeg",
            "-y",
            "-i", str(input_file),
            "-vn",
            "-acodec", "libmp3lame",
            "-b:a", "192k",
            str(output_file),
        ]
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60)
        return res.returncode == 0 and output_file.exists() and output_file.stat().st_size > 0
    except Exception as e:
        logger.warning(f"ffmpeg conversion error: {e}")
        return False


# --- Telegram Handlers ---
dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(msg: Message):
    text = (
        "🎬 <b>Универсальный загрузчик видео и MP3</b>\n\n"
        "⚡ <b>Как пользоваться:</b>\n"
        "• Отправь <b>любую ссылку</b> на видео (TikTok, Shorts, Reels, YouTube, VK и др.)\n"
        "• Бот скачает ролик и отправит прямо в чат с кнопкой <b>🎵 Скачать MP3</b>\n"
        "• Отправь видео/кружочек из Telegram — бот мгновенно извлечет из него чистый звук"
    )
    await msg.answer(text, parse_mode=ParseMode.HTML)


@dp.message(Command("help"))
async def cmd_help(msg: Message):
    await cmd_start(msg)


# Прием cookies.txt
@dp.message(F.document & (F.document.file_name == "cookies.txt"))
async def handle_cookies(msg: Message, bot: Bot):
    try:
        await bot.download(msg.document, destination=COOKIES_FILE)
        await msg.reply(
            "✅ <b>Файл cookies.txt успешно обновлен!</b>\n"
            "Теперь бот авторизован и может скачивать защищенные ролики.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await msg.reply(f"❌ Ошибка сохранения cookies: {e}")


# Обработка текстовых ссылок
@dp.message(F.text)
async def handle_link(msg: Message):
    url = extract_url(msg.text)
    if not url:
        await msg.reply("💡 Отправь мне ссылку на видео (YouTube, TikTok, VK и т.д.) или видеофайл/кружочек.")
        return
    status = await msg.reply("⏳ <b>Скачиваю видео...</b>", parse_mode=ParseMode.HTML)

    task_dir = TEMP_DIR / uuid.uuid4().hex[:12]
    task_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Скачиваем видео в отдельном потоке
        ok, res, err = await asyncio.to_thread(_ytdlp_download_video, url, task_dir)

        if not ok or not res:
            task_id = uuid.uuid4().hex[:8]
            TASKS_CACHE[task_id] = {"url": url, "title": "Аудиодорожка", "uploader": "Audio"}
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="🎵 Скачать MP3", callback_data=f"mp3:{task_id}")]
                ]
            )
            if "50 МБ" in err:
                await status.edit_text(
                    "⚠️ <b>Видео слишком большое для отправки в Telegram (> 50 МБ).</b>\n\n"
                    "Вы можете скачать аудиодорожку ролика в формате MP3:",
                    reply_markup=kb,
                    parse_mode=ParseMode.HTML,
                )
            else:
                await status.edit_text(
                    f"❌ <b>Не удалось скачать видео:</b>\n<code>{html.escape(err)}</code>\n\n"
                    f"Попробовать скачать аудиодорожку в MP3?",
                    reply_markup=kb,
                    parse_mode=ParseMode.HTML,
                )
            return

        video_path = res["file_path"]
        title = res["title"]
        uploader = res["uploader"]
        duration = res["duration"]

        task_id = uuid.uuid4().hex[:8]
        TASKS_CACHE[task_id] = {
            "url": url,
            "title": title,
            "uploader": uploader,
            "duration": duration,
        }

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🎵 Скачать MP3", callback_data=f"mp3:{task_id}")]
            ]
        )

        caption = f"<b>{html.escape(title)}</b>"
        if uploader:
            caption += f"\n👤 {html.escape(uploader)}"
        if len(caption) > 1000:
            caption = caption[:990] + "..."

        await status.edit_text("📤 <b>Отправляю видео...</b>", parse_mode=ParseMode.HTML)

        video_input = FSInputFile(video_path, filename=f"{clean_filename(title)}.mp4")
        await msg.reply_video(
            video=video_input,
            caption=caption,
            parse_mode=ParseMode.HTML,
            duration=duration,
            supports_streaming=True,
            reply_markup=kb,
        )

        try:
            await status.delete()
        except Exception:
            pass

    except Exception as e:
        logger.exception("Ошибка при обработке ссылки:")
        await status.edit_text(f"⚠️ Ошибка обработки: {html.escape(str(e))}")
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)


# Обработка нажатия кнопки "Скачать MP3"
@dp.callback_query(F.data.startswith("mp3:"))
async def handle_mp3_callback(call: CallbackQuery):
    await call.answer("Извлекаю аудио...")
    task_id = call.data.split(":", 1)[1]
    task = TASKS_CACHE.get(task_id)

    if not task:
        await call.message.reply("⚠️ Срок действия кнопки истек. Пожалуйста, отправьте ссылку заново.")
        return

    status = await call.message.reply("⏳ <b>Конвертирую в MP3...</b>", parse_mode=ParseMode.HTML)

    task_dir = TEMP_DIR / uuid.uuid4().hex[:12]
    task_dir.mkdir(parents=True, exist_ok=True)

    try:
        ok, mp3_path, err = await asyncio.to_thread(_ytdlp_download_mp3, task["url"], task_dir)
        if not ok or not mp3_path:
            await status.edit_text(f"❌ <b>Не удалось получить MP3:</b>\n{html.escape(err)}", parse_mode=ParseMode.HTML)
            return

        title = task.get("title") or "Audio"
        uploader = task.get("uploader") or "Video to MP3"
        duration = task.get("duration")

        await status.edit_text("📤 <b>Отправляю аудио...</b>", parse_mode=ParseMode.HTML)

        audio_file = FSInputFile(mp3_path, filename=f"{clean_filename(title)}.mp3")
        await call.message.reply_audio(
            audio=audio_file,
            title=title[:60],
