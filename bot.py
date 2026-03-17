import os
import json
import asyncio
import logging
from anthropic import AsyncAnthropic

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── ENV ──────────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.environ["BOT_TOKEN"]
ADMIN_ID        = int(os.environ["ADMIN_TELEGRAM_ID"])
ANTHROPIC_KEY   = os.environ["ANTHROPIC_API_KEY"]
AUTO_THRESHOLD  = int(os.environ.get("AUTO_THRESHOLD", "10"))  # после скольки одобрений → авто

anthropic = AsyncAnthropic(api_key=ANTHROPIC_KEY)

# ── MEMORY (в памяти процесса + JSON файл для персистентности) ────────────────
MEMORY_FILE = "memory.json"

def load_memory() -> dict:
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "style_rules": [
            "Обращаться к ученику по имени в начале (Ром, Макс и тд)",
            "Коротко и по делу — 1-3 предложения, без воды",
            "Заканчивать предложения скобкой вместо точки — 'всё верно)'",
            "К группе обращаться 'Ребята,' или 'Ребят,' — не официально",
            "Сложный вопрос — предложить разобрать на созвоне)",
            "Живой разговорный язык, сокращения: тк, лс, вс, сб, мск",
            "Похвала коротко и энергично: 'Красавец!', 'Прям пушка!!!'",
            "Без официоза: не 'данный', 'следует', 'необходимо'",
            "Если ученик прав — просто подтвердить: 'Да, всё верно)'",
        ],
        "course_context": (
            "Онлайн-курс по YouTube-монетизации. "
            "Ученики создают каналы без лица: слайдшоу, анимации, нейросети. "
            "Ключевые понятия: TDF, ниша, подниша, превью, RPM, AB-тест, "
            "продвинутые функции YouTube, ВПН. "
            "Инструменты: Blackquery, ВПН (Liberator), ChatGPT. "
            "Созвоны в Zoom, дз сдают в лс."
        ),
        "approved_count": 0,
        "edited_count": 0,
        "auto_mode": False,
    }

def save_memory(mem: dict):
    with open(MEMORY_FILE, "w") as f:
        json.dump(mem, f, ensure_ascii=False, indent=2)

memory = load_memory()

# ── PENDING DRAFTS: chat_id → {question, draft, original_msg_id} ─────────────
pending: dict[str, dict] = {}

# ── AI FUNCTIONS ──────────────────────────────────────────────────────────────
def build_system_prompt() -> str:
    rules = "\n".join(f"{i+1}. {r}" for i, r in enumerate(memory["style_rules"]))
    return f"""Ты куратор онлайн-курса. Отвечаешь от имени живого куратора в Telegram-группе учеников.

Контекст курса:
{memory['course_context']}

Правила стиля (строго соблюдай):
{rules}

Отвечай коротко, по-человечески, 1-3 предложения. Только сам ответ, без пояснений."""

async def generate_draft(question: str, sender_name: str) -> str:
    prompt = f"Ученик {sender_name} пишет: {question}"
    msg = await anthropic.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=400,
        system=build_system_prompt(),
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()

async def extract_style_rule(original: str, edited: str) -> str | None:
    try:
        msg = await anthropic.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content":
                f"Куратор исправил ответ AI. Сформулируй одно краткое правило стиля (до 10 слов).\n"
                f"Оригинал: \"{original}\"\nПравка: \"{edited}\"\nТолько правило, без пояснений."
            }],
        )
        rule = msg.content[0].text.strip()
        return rule if 5 < len(rule) < 120 else None
    except Exception:
        return None

# ── HANDLERS ──────────────────────────────────────────────────────────────────
async def on_group_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return

    # Игнорируем сообщения от самого бота и от администратора
    user = msg.from_user
    if user.is_bot or user.id == ADMIN_ID:
        return

    question   = msg.text.strip()
    sender     = user.first_name or user.username or "Ученик"
    chat_id    = msg.chat_id
    message_id = msg.message_id

    # Генерируем черновик
    try:
        draft = await generate_draft(question, sender)
    except Exception as e:
        logger.error(f"Draft generation failed: {e}")
        return

    draft_key = f"{chat_id}:{message_id}"
    pending[draft_key] = {
        "question": question,
        "draft":    draft,
        "chat_id":  chat_id,
        "reply_to": message_id,
        "sender":   sender,
    }

    total     = memory["approved_count"] + memory["edited_count"]
    acc_pct   = round(memory["approved_count"] / total * 100) if total else 0
    mode_tag  = "🟢 АВТО" if memory["auto_mode"] else "🟡 ПРОВЕРКА"

    # В авто-режиме — отправляем сразу, тебе просто уведомление
    if memory["auto_mode"]:
        await ctx.bot.send_message(chat_id=chat_id, text=draft,
                                   reply_to_message_id=message_id)
        await ctx.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"🤖 {mode_tag} | {sender}:\n«{question}»\n\n✅ Отправлено автоматически:\n{draft}"
        )
        return

    # Ручной режим — шлём тебе на проверку
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Одобрить", callback_data=f"approve:{draft_key}"),
            InlineKeyboardButton("✏️ Исправить", callback_data=f"edit:{draft_key}"),
        ],
        [InlineKeyboardButton("🚫 Пропустить", callback_data=f"skip:{draft_key}")],
    ])

    stats_line = f"Одобрено: {memory['approved_count']} | Исправлено: {memory['edited_count']} | Точность: {acc_pct}%"

    await ctx.bot.send_message(
        chat_id=ADMIN_ID,
        text=(
            f"📩 Новый вопрос от {sender}:\n"
            f"«{question}»\n\n"
            f"🤖 Черновик AI:\n{draft}\n\n"
            f"📊 {stats_line}"
        ),
        reply_markup=keyboard,
    )

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()

    action, draft_key = query.data.split(":", 1)
    item = pending.get(draft_key)
    if not item:
        await query.edit_message_text("⚠️ Черновик устарел или уже обработан.")
        return

    if action == "approve":
        await ctx.bot.send_message(
            chat_id=item["chat_id"],
            text=item["draft"],
            reply_to_message_id=item["reply_to"],
        )
        memory["approved_count"] += 1
        _check_auto_mode()
        save_memory(memory)
        del pending[draft_key]
        total = memory["approved_count"] + memory["edited_count"]
        acc   = round(memory["approved_count"] / total * 100) if total else 0
        await query.edit_message_text(
            f"✅ Отправлен оригинал\n\n«{item['draft']}»\n\n"
            f"📊 Одобрено: {memory['approved_count']} | Точность: {acc}%"
            + ("\n\n🟢 Авто-режим включён!" if memory["auto_mode"] else "")
        )

    elif action == "edit":
        # Просим написать правку следующим сообщением
        ctx.user_data["awaiting_edit"] = draft_key
        await query.edit_message_text(
            f"✏️ Напиши исправленный ответ следующим сообщением.\n\n"
            f"Вопрос: «{item['question']}»\n"
            f"Черновик AI: {item['draft']}"
        )

    elif action == "skip":
        del pending[draft_key]
        await query.edit_message_text(f"🚫 Пропущено: «{item['question']}»")

async def on_admin_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Ловит исправленный ответ от администратора"""
    if update.message.from_user.id != ADMIN_ID:
        return

    draft_key = ctx.user_data.get("awaiting_edit")
    if not draft_key:
        return

    item = pending.get(draft_key)
    if not item:
        await update.message.reply_text("⚠️ Черновик уже обработан.")
        ctx.user_data.pop("awaiting_edit", None)
        return

    edited_text = update.message.text.strip()

    # Отправляем исправленный ответ в группу
    await ctx.bot.send_message(
        chat_id=item["chat_id"],
        text=edited_text,
        reply_to_message_id=item["reply_to"],
    )

    # Обучение: извлекаем правило из разницы
    rule = await extract_style_rule(item["draft"], edited_text)
    rule_msg = ""
    if rule:
        memory["style_rules"].append(rule)
        rule_msg = f"\n\n🧠 Новое правило добавлено:\n«{rule}»"

    memory["edited_count"] += 1
    _check_auto_mode()
    save_memory(memory)
    del pending[draft_key]
    ctx.user_data.pop("awaiting_edit", None)

    total = memory["approved_count"] + memory["edited_count"]
    acc   = round(memory["approved_count"] / total * 100) if total else 0
    await update.message.reply_text(
        f"✅ Исправленный ответ отправлен в группу.{rule_msg}\n\n"
        f"📊 Одобрено: {memory['approved_count']} | Исправлено: {memory['edited_count']} | Точность: {acc}%"
    )

def _check_auto_mode():
    total = memory["approved_count"] + memory["edited_count"]
    if total >= AUTO_THRESHOLD and not memory["auto_mode"]:
        acc = memory["approved_count"] / total
        if acc >= 0.80:
            memory["auto_mode"] = True

# ── COMMANDS ──────────────────────────────────────────────────────────────────
async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    total = memory["approved_count"] + memory["edited_count"]
    acc   = round(memory["approved_count"] / total * 100) if total else 0
    rules = "\n".join(f"{i+1}. {r}" for i, r in enumerate(memory["style_rules"]))
    mode  = "🟢 АВТО" if memory["auto_mode"] else "🟡 РУЧНОЙ"
    await update.message.reply_text(
        f"📊 Статистика\n\n"
        f"Режим: {mode}\n"
        f"Одобрено: {memory['approved_count']}\n"
        f"Исправлено: {memory['edited_count']}\n"
        f"Точность: {acc}%\n\n"
        f"🧠 Правил в памяти: {len(memory['style_rules'])}\n\n{rules}"
    )

async def cmd_manual(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    memory["auto_mode"] = False
    save_memory(memory)
    await update.message.reply_text("🟡 Переключён в ручной режим — все ответы проходят проверку.")

async def cmd_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message.from_user.id != ADMIN_ID:
        return
    memory["auto_mode"] = True
    save_memory(memory)
    await update.message.reply_text("🟢 Переключён в авто-режим — бот отвечает сам.")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Групповые сообщения
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.GROUPS & ~filters.COMMAND,
        on_group_message
    ))

    # Кнопки одобрения/правки
    app.add_handler(CallbackQueryHandler(on_callback))

    # Личные сообщения от администратора (правки)
    app.add_handler(MessageHandler(
        filters.TEXT & filters.ChatType.PRIVATE & ~filters.COMMAND,
        on_admin_message
    ))

    # Команды для администратора
    from telegram.ext import CommandHandler
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("manual",  cmd_manual))
    app.add_handler(CommandHandler("auto",    cmd_auto))

    logger.info("Бот запущен...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
