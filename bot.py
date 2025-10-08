import logging
import asyncio
import threading
import requests
import tempfile
import os
import json
from datetime import datetime
from typing import List, Dict, Optional, Any, Set
import time
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, CallbackQuery
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes, JobQueue
from security_manager import SecurityManager
from project_manager import ProjectManager
from discord_monitor import DiscordMonitor
from twitter_monitor import TwitterMonitor
from twitter_monitor_adapter import TwitterMonitorAdapter
from access_manager import access_manager
from config import BOT_TOKEN, ADMIN_PASSWORD, SECURITY_TIMEOUT, MESSAGES, DISCORD_AUTHORIZATION, MONITORING_INTERVAL, TWITTER_AUTH_TOKEN, TWITTER_CSRF_TOKEN, TWITTER_MONITORING_INTERVAL

# Налаштування логування - тільки критичні помилки для швидкості
import logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.ERROR
)

# Відключаємо детальне логування для Twitter моніторингу
logging.getLogger('twitter_monitor').setLevel(logging.WARNING)
logging.getLogger('twitter_monitor_adapter').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Ініціалізація менеджерів
security_manager = SecurityManager(SECURITY_TIMEOUT)
project_manager = ProjectManager()
discord_monitor = DiscordMonitor(DISCORD_AUTHORIZATION) if DISCORD_AUTHORIZATION else None
twitter_monitor = TwitterMonitor(TWITTER_AUTH_TOKEN, TWITTER_CSRF_TOKEN) if TWITTER_AUTH_TOKEN and TWITTER_CSRF_TOKEN else None
twitter_monitor_adapter = None  # Twitter Monitor Adapter (заміна Selenium)

# Словник для зберігання стану користувачів (очікують пароль)
waiting_for_password = {}

# Словник для зберігання стану додавання проектів
user_states: Dict[int, Dict[str, Any]] = {}  # user_id -> {'state': 'adding_project', 'data': {...}}

# Глобальна змінна для зберігання активного бота
bot_instance = None

# Глобальна система відстеження відправлених твітів
global_sent_tweets: Dict[str, Set[str]] = {}  # account -> set of sent tweet_ids

# Глобальні змінні для UI
user_states = {}  # Зберігаємо стани користувачів для форм
waiting_for_password = {}  # Користувачі, які очікують введення паролю
main_menu_messages = {}  # Зберігаємо ID головних меню для редагування

# Декоратор авторизації має бути оголошений до використання
def require_auth(func):
    """Декоратор для перевірки авторизації користувача"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user or not update.message:
            return
        
        user_id = update.effective_user.id
        
        # Перевіряємо чи користувач авторизований
        if not access_manager.is_authorized(user_id):
            await update.message.reply_text(
                "🔐 **Доступ обмежено!**\n\n"
                "Для використання цієї команди необхідна авторизація.\n"
                "Використовуйте команду /login для входу в систему.",
            )
            return
        
        # Викликаємо оригінальну функцію
        return await func(update, context)
    
    return wrapper

# ===================== Синхронізація моніторів з проектами =====================
def clean_forbidden_accounts():
    """Очистити заборонені акаунти з моніторів"""
    forbidden_accounts = ['twitter', 'x']
    
    # Очищаємо Twitter монітор
    if twitter_monitor:
        for account in forbidden_accounts:
            if account in twitter_monitor.monitoring_accounts:
                twitter_monitor.monitoring_accounts.discard(account)
                if account in twitter_monitor.sent_tweets:
                    del twitter_monitor.sent_tweets[account]
                if account in twitter_monitor.seen_tweets:
                    del twitter_monitor.seen_tweets[account]
                logger.info(f"🧹 Видалено заборонений Twitter акаунт: {account}")
        twitter_monitor.save_seen_tweets()
    
    # Очищаємо Twitter Monitor Adapter
    if twitter_monitor_adapter:
        for account in forbidden_accounts:
            if account in twitter_monitor_adapter.monitoring_accounts:
                twitter_monitor_adapter.monitoring_accounts.discard(account)
                if account in twitter_monitor_adapter.sent_tweets:
                    del twitter_monitor_adapter.sent_tweets[account]
                if account in twitter_monitor_adapter.seen_tweets:
                    del twitter_monitor_adapter.seen_tweets[account]
                logger.info(f"🧹 Видалено заборонений Twitter Monitor Adapter акаунт: {account}")
        twitter_monitor_adapter.save_seen_tweets()

def sync_monitors_with_projects() -> None:
    """Звести активні монітори до фактичних проектів і збережених Twitter Monitor Adapter акаунтів"""
    try:
        # Спочатку очищаємо заборонені акаунти
        clean_forbidden_accounts()
        
        # Збираємо цільові Twitter usernames із проектів
        project_usernames = set()
        discord_channels = {}  # channel_id -> original_url
        
        logger.info("🔍 Аналізуємо всі проекти...")
        for user_id, projects in project_manager.data.get('projects', {}).items():
            logger.info(f"👤 Користувач {user_id}: {len(projects)} проектів")
            for p in projects:
                if p.get('platform') == 'twitter':
                    url = p.get('url', '')
                    sync_username = extract_twitter_username(url)
                    logger.info(f"   🐦 Twitter проект: URL='{url}' -> username='{sync_username}'")
                    if sync_username and sync_username.lower() not in ['twitter', 'x', 'elonmusk']:
                        project_usernames.add(sync_username)
                    elif sync_username and sync_username.lower() in ['twitter', 'x', 'elonmusk']:
                        logger.warning(f"   ⚠️ Пропущено заборонений Twitter акаунт: {sync_username}")
                elif p.get('platform') == 'discord':
                    url = p.get('url', '')
                    channel_id = extract_discord_channel_id(url)
                    logger.info(f"   💬 Discord проект: URL='{url}' -> channel_id='{channel_id}'")
                    if channel_id:
                        discord_channels[channel_id] = url  # Зберігаємо оригінальний URL
        
        logger.info(f"📊 Результат аналізу:")
        logger.info(f"   🐦 Знайдено Twitter usernames: {list(project_usernames)}")
        logger.info(f"   💬 Знайдено Discord channels: {list(discord_channels.keys())}")

        # Додаємо явно збережені Twitter Monitor Adapter акаунти (якщо ще є)
        twitter_adapter_saved = set(project_manager.get_selenium_accounts() or [])  # Використовуємо ту ж функцію
        # Фільтруємо заборонені акаунти
        twitter_adapter_saved = {acc for acc in twitter_adapter_saved if acc.lower() not in ['twitter', 'x']}
        target_usernames = project_usernames.union(twitter_adapter_saved)

        # Синхронізація Twitter API монітора
        global twitter_monitor
        if twitter_monitor is not None:
            current = set(getattr(twitter_monitor, 'monitoring_accounts', set()))
            # Видаляємо зайві
            for username in list(current - target_usernames):
                try:
                    if username:
                        twitter_monitor.remove_account(username)
                        logger.info(f"🗑️ Видалено Twitter акаунт з моніторингу: {username}")
                except Exception:
                    pass
            # Додаємо відсутні (із проектів/selenium_saved)
            for username in list(target_usernames - current):
                try:
                    twitter_monitor.add_account(username)
                    logger.info(f"➕ Додано Twitter акаунт до моніторингу: {username}")
                except Exception:
                    pass

        # Синхронізація Twitter Monitor Adapter (основний підхід)
        global twitter_monitor_adapter
        if twitter_monitor_adapter is not None:
            current = set(getattr(twitter_monitor_adapter, 'monitoring_accounts', set()))
            # Видаляємо зайві
            for username in list(current - target_usernames):
                twitter_monitor_adapter.monitoring_accounts.discard(username)
                logger.info(f"🗑️ Видалено Twitter Monitor Adapter акаунт з моніторингу: {username}")
            # Додаємо відсутні
            for username in list(target_usernames - current):
                twitter_monitor_adapter.add_account(username)
                logger.info(f"➕ Додано Twitter Monitor Adapter акаунт до моніторингу: {username}")

        # Синхронізація Discord монітора
        global discord_monitor
        if discord_monitor is not None:
            current_channels = set(str(ch) for ch in getattr(discord_monitor, 'monitoring_channels', []))
            logger.info(f"🔄 Discord монітор: поточні канали = {list(current_channels)}")
            logger.info(f"🔄 Discord монітор: цільові канали = {list(discord_channels.keys())}")
            
            # Додаємо нові канали
            for channel_id, original_url in discord_channels.items():
                if channel_id not in current_channels:
                    try:
                        discord_monitor.add_channel(original_url)  # Передаємо оригінальний URL
                        logger.info(f"➕ Додано Discord канал до моніторингу: {channel_id} ({original_url})")
                    except Exception as e:
                        logger.error(f"❌ Помилка додавання Discord каналу {channel_id}: {e}")
            # Видаляємо зайві канали
            for channel_id in current_channels - set(discord_channels.keys()):
                try:
                    discord_monitor.remove_channel(channel_id)
                    logger.info(f"🗑️ Видалено Discord канал з моніторингу: {channel_id}")
                except Exception as e:
                    logger.error(f"❌ Помилка видалення Discord каналу {channel_id}: {e}")
        else:
            logger.warning("⚠️ Discord монітор не ініціалізовано (DISCORD_AUTHORIZATION відсутній?)")

        if target_usernames:
            logger.info(f"🔄 Синхронізовано Twitter моніторинг: {len(target_usernames)} акаунтів")
        if discord_channels:
            logger.info(f"🔄 Синхронізовано Discord моніторинг: {len(discord_channels)} каналів")
            
        # Завжди намагаємося запустити моніторинг
        logger.info("🚀 Автоматично запускаємо моніторинг...")
        auto_start_monitoring()
        
        if target_usernames or discord_channels:
            logger.info(f"✅ Знайдено проекти для моніторингу: {len(target_usernames)} Twitter + {len(discord_channels)} Discord")
        else:
            logger.info("ℹ️ Поки що немає проектів для моніторингу, але монітори готові до роботи")

    except Exception as e:
        logger.error(f"Помилка синхронізації моніторів: {e}")

def auto_start_monitoring() -> None:
    """Автоматично запустити всі доступні монітори"""
    try:
        global twitter_monitor, discord_monitor, twitter_monitor_adapter
        import threading
        
        # Запускаємо Twitter API моніторинг
        if twitter_monitor and hasattr(twitter_monitor, 'monitoring_accounts'):
            accounts = getattr(twitter_monitor, 'monitoring_accounts', set())
            if accounts and TWITTER_AUTH_TOKEN:
                logger.info(f"🐦 Автоматично запускаємо Twitter API моніторинг для {len(accounts)} акаунтів")
                try:
                    # Запускаємо в окремому потоці якщо ще не запущено
                    if not hasattr(auto_start_monitoring, '_twitter_started'):
                        twitter_thread = threading.Thread(target=lambda: asyncio.run(start_twitter_monitoring()))
                        twitter_thread.daemon = True
                        twitter_thread.start()
                        auto_start_monitoring._twitter_started = True
                        logger.info("✅ Twitter API моніторинг автоматично запущено")
                except Exception as e:
                    logger.error(f"Помилка запуску Twitter моніторингу: {e}")
        
        # Запускаємо Twitter Monitor Adapter моніторинг (основний підхід)
        if twitter_monitor_adapter and hasattr(twitter_monitor_adapter, 'monitoring_accounts'):
            accounts = getattr(twitter_monitor_adapter, 'monitoring_accounts', set())
            if accounts:
                logger.info(f"🚀 Автоматично запускаємо Twitter Monitor Adapter моніторинг для {len(accounts)} акаунтів")
                try:
                    # Запускаємо в окремому потоці якщо ще не запущено
                    if not hasattr(auto_start_monitoring, '_twitter_adapter_started'):
                        twitter_adapter_thread = threading.Thread(target=lambda: asyncio.run(start_twitter_monitor_adapter()))
                        twitter_adapter_thread.daemon = True
                        twitter_adapter_thread.start()
                        auto_start_monitoring._twitter_adapter_started = True
                        logger.info("✅ Twitter Monitor Adapter моніторинг автоматично запущено")
                except Exception as e:
                    logger.error(f"Помилка запуску Twitter Monitor Adapter моніторингу: {e}")
        
        # Запускаємо Discord моніторинг
        logger.info(f"💬 Discord монітор: {'✅ Ініціалізовано' if discord_monitor else '❌ Не ініціалізовано'}")
        if discord_monitor:
            logger.info(f"💬 Discord монітор має атрибут 'monitoring_channels': {'✅ Так' if hasattr(discord_monitor, 'monitoring_channels') else '❌ Ні'}")
            if hasattr(discord_monitor, 'monitoring_channels'):
                channels = getattr(discord_monitor, 'monitoring_channels', [])
                logger.info(f"💬 Discord монітор: знайдено {len(channels)} каналів: {channels}")
                logger.info(f"💬 Discord AUTHORIZATION: {'✅ Є' if DISCORD_AUTHORIZATION else '❌ Відсутній'}")
                
                if channels and DISCORD_AUTHORIZATION:
                    logger.info(f"💬 Автоматично запускаємо Discord моніторинг для {len(channels)} каналів")
                    try:
                        # Запускаємо в окремому потоці якщо ще не запущено
                        if not hasattr(auto_start_monitoring, '_discord_started'):
                            discord_thread = threading.Thread(target=lambda: asyncio.run(start_discord_monitoring()))
                            discord_thread.daemon = True
                            discord_thread.start()
                            auto_start_monitoring._discord_started = True
                            logger.info("✅ Discord моніторинг автоматично запущено")
                    except Exception as e:
                        logger.error(f"Помилка запуску Discord моніторингу: {e}")
                elif not channels:
                    logger.info("ℹ️ Discord монітор: немає каналів для моніторингу")
                elif not DISCORD_AUTHORIZATION:
                    logger.warning("⚠️ Discord моніторинг пропущено: відсутній DISCORD_AUTHORIZATION")
            else:
                logger.warning("⚠️ Discord монітор не має атрибута 'monitoring_channels'")
        else:
            logger.warning("⚠️ Discord монітор не ініціалізовано (DISCORD_AUTHORIZATION відсутній?)")
        
        logger.info("✅ Автоматичний запуск всіх моніторів завершено")
        
    except Exception as e:
        logger.error(f"Помилка автоматичного запуску моніторингу: {e}")

# ===================== Утиліти для Telegram chat_id =====================
def normalize_chat_id(chat_id_value: str) -> str:
    """Нормалізувати chat_id: додає -100 для каналів/супергруп, якщо відсутній.
    Приймає рядок з цифрами або вже валідний від'ємний chat_id."""
    try:
        val = str(chat_id_value).strip()
        original_val = val
        
        if val.startswith('@'):
            logger.debug(f"🔍 Chat ID {original_val} залишається як username")
            return val  # username, нехай Telegram обробить
        # Якщо вже від'ємний - залишаємо
        if val.startswith('-'):
            logger.debug(f"🔍 Chat ID {original_val} вже нормалізований")
            return val
        # Якщо це лише цифри (ймовірно, канал/супергрупа, що потребує -100)
        if val.isdigit():
            result = '-100' + val
            logger.debug(f"🔍 Chat ID {original_val} нормалізовано до {result}")
            return result
        
        logger.debug(f"🔍 Chat ID {original_val} залишається без змін")
        return val
    except Exception as e:
        logger.error(f"❌ Помилка нормалізації chat_id {chat_id_value}: {e}")
        return str(chat_id_value)

def create_project_thread_sync(bot_token: str, chat_id: str, project_name: str, project_tag: str, user_id: str = None) -> Optional[int]:
    """Синхронно створити thread для проекту в групі"""
    try:
        # Перевіряємо, чи вже є thread для цього проекту
        if user_id:
            existing_thread_id = get_project_thread_id(user_id, project_name, chat_id)
            if existing_thread_id:
                logger.info(f"🔍 Знайдено існуючий thread {existing_thread_id} для проекту '{project_name}'")
                return existing_thread_id
        
        # Створюємо тему в групі для цього проекту
        url = f"https://api.telegram.org/bot{bot_token}/createForumTopic"
        data = {
            'chat_id': normalize_chat_id(chat_id),
            'name': f"{project_tag} {project_name}",
            'icon_color': 0x6FB9F0,  # Синій колір
        }
        
        response = requests.post(url, data=data, timeout=10)
        
        # Додаємо затримку після запиту для уникнення rate limit
        import time
        time.sleep(1)  # Зменшено для швидшої роботи
        
        logger.info(f"🔧 API запит створення thread: {url}")
        logger.info(f"🔧 API дані: {data}")
        logger.info(f"🔧 API відповідь status: {response.status_code}")
        
        if response.status_code == 200:
            result = response.json()
            logger.info(f"🔧 API відповідь: {result}")
            if result.get('ok'):
                thread_id = result['result']['message_thread_id']
                logger.info(f"✅ Створено thread {thread_id} для проекту '{project_name}' з тегом {project_tag}")
                
                # Зберігаємо mapping thread_id для проекту
                if user_id:
                    save_project_thread_id(user_id, project_name, chat_id, thread_id)
                
                return thread_id
            else:
                logger.error(f"❌ Telegram API помилка при створенні thread: {result}")
                logger.error(f"❌ Можливо канал {chat_id} не є форум групою. Forum топіки можна створювати тільки в форум групах.")
                return None
        else:
            try:
                result = response.json()
                logger.error(f"❌ HTTP {response.status_code} при створенні thread: {result}")
            except:
                logger.error(f"❌ HTTP {response.status_code} при створенні thread (не JSON відповідь)")
            return None
            
        if response.status_code == 429:
            logger.warning(f"⚠️ Rate limit при створенні thread, чекаємо 10 секунд...")
            time.sleep(10)
            # Повторна спроба після rate limit
            response2 = requests.post(url, data=data, timeout=10)
            if response2.status_code == 200:
                result2 = response2.json()
                if result2.get('ok'):
                    thread_id = result2['result']['message_thread_id']
                    logger.info(f"✅ Створено thread {thread_id} для проекту '{project_name}' після повторної спроби")
                    
                    # Зберігаємо mapping thread_id для проекту
                    if user_id:
                        save_project_thread_id(user_id, project_name, chat_id, thread_id)
                    
                    return thread_id
            logger.error(f"❌ Не вдалося створити thread після повторної спроби: {response2.status_code}")
        else:
            logger.error(f"❌ HTTP помилка при створенні thread: {response.status_code}")
        
        return None
        
    except Exception as e:
        logger.error(f"❌ Помилка створення thread для проекту '{project_name}': {e}")
        return None

async def create_project_thread(bot_token: str, chat_id: str, project_name: str, project_tag: str, user_id: str = None) -> Optional[int]:
    """Асинхронно створити thread для проекту в групі"""
    return create_project_thread_sync(bot_token, chat_id, project_name, project_tag, user_id)

def send_message_to_thread_sync(bot_token: str, chat_id: str, thread_id: int, text: str, project_tag: str = "") -> bool:
    """Синхронно відправити повідомлення в thread з тегом"""
    try:
        # Додаємо тег до початку повідомлення
        if project_tag and not text.startswith(project_tag):
            tagged_text = f"{project_tag}\n\n{text}"
        else:
            tagged_text = text
            
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        data = {
            'chat_id': normalize_chat_id(chat_id),
            'message_thread_id': thread_id,
            'text': tagged_text,
            'parse_mode': 'HTML'
        }
        
        logger.info(f"🔍 Відправляємо в thread: chat_id={data['chat_id']}, thread_id={thread_id}, текст довжиною {len(tagged_text)} символів")
        logger.debug(f"🔍 Текст повідомлення: {repr(tagged_text)}")
        
        response = requests.post(url, data=data, timeout=10)
        
        if response.status_code == 200:
            result = response.json()
            if result.get('ok'):
                logger.info(f"✅ Повідомлення відправлено в thread {thread_id} з тегом {project_tag}")
                # Додаємо затримку після успішної відправки для уникнення rate limit
                import time
                time.sleep(0.7)  # Зменшено для швидшої роботи
                return True
            else:
                logger.error(f"❌ Telegram API помилка при відправці в thread: {result}")
        elif response.status_code == 429:
            # Обробка rate limit
            try:
                error_response = response.json()
                retry_after = error_response.get('parameters', {}).get('retry_after', 15)
                logger.warning(f"⚠️ Rate limit при відправці в thread, чекаємо {retry_after} секунд...")
                import time
                time.sleep(retry_after + 1)
                # Повторна спроба
                response2 = requests.post(url, data=data, timeout=10)
                if response2.status_code == 200:
                    result2 = response2.json()
                    if result2.get('ok'):
                        logger.info(f"✅ Повідомлення відправлено в thread {thread_id} після повторної спроби")
                        time.sleep(1)
                        return True
                logger.error(f"❌ Не вдалося відправити повідомлення після повторної спроби")
            except Exception as e:
                logger.error(f"❌ Помилка обробки rate limit: {e}")
        else:
            logger.error(f"❌ HTTP помилка при відправці в thread: {response.status_code}")
            try:
                error_response = response.json()
                logger.error(f"❌ Деталі помилки: {error_response}")
            except:
                logger.error(f"❌ Текст відповіді: {response.text}")
        
        return False
        
    except Exception as e:
        logger.error(f"❌ Помилка відправки повідомлення в thread {thread_id}: {e}")
        return False

async def send_message_to_thread(bot_token: str, chat_id: str, thread_id: int, text: str, project_tag: str = "") -> bool:
    """Асинхронно відправити повідомлення в thread з тегом"""
    return send_message_to_thread_sync(bot_token, chat_id, thread_id, text, project_tag)

def send_photo_to_thread_sync(bot_token: str, chat_id: str, thread_id: int, photo_url: str, caption: str = "", project_tag: str = "") -> bool:
    """Синхронно відправити фото в thread з тегом"""
    try:
        # Додаємо тег до початку підпису
        if project_tag and caption and not caption.startswith(project_tag):
            tagged_caption = f"{project_tag} {caption}"
        elif project_tag and not caption:
            tagged_caption = project_tag
        else:
            tagged_caption = caption
            
        # Спочатку завантажуємо зображення
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        }
        
        response = requests.get(photo_url, headers=headers, timeout=15)
        response.raise_for_status()
        
        # Відправляємо через Telegram API
        url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
        
        files = {'photo': ('image.jpg', response.content, 'image/jpeg')}
        data = {
            'chat_id': normalize_chat_id(chat_id),
            'message_thread_id': thread_id,
            'caption': tagged_caption[:1024] if tagged_caption else '',  # Обмеження Telegram
            'parse_mode': 'HTML'
        }
        
        response = requests.post(url, files=files, data=data, timeout=30)
        
        if response.status_code == 200:
            result = response.json()
            if result.get('ok'):
                logger.info(f"✅ Фото відправлено в thread {thread_id} з тегом {project_tag}")
                # Додаємо затримку після успішної відправки для уникнення rate limit
                import time
                time.sleep(1)  # Зменшено для швидшої роботи
                return True
            else:
                logger.error(f"❌ Telegram API помилка при відправці фото в thread: {result}")
        elif response.status_code == 429:
            # Обробка rate limit
            try:
                error_response = response.json()
                retry_after = error_response.get('parameters', {}).get('retry_after', 15)
                logger.warning(f"⚠️ Rate limit при відправці фото в thread, чекаємо {retry_after} секунд...")
                import time
                time.sleep(retry_after + 2)
                # Повторна спроба
                response2 = requests.post(url, files=files, data=data, timeout=30)
                if response2.status_code == 200:
                    result2 = response2.json()
                    if result2.get('ok'):
                        logger.info(f"✅ Фото відправлено в thread {thread_id} після повторної спроби")
                        time.sleep(1.5)
                        return True
                logger.error(f"❌ Не вдалося відправити фото після повторної спроби")
            except Exception as e:
                logger.error(f"❌ Помилка обробки rate limit для фото: {e}")
        else:
            logger.error(f"❌ HTTP помилка при відправці фото в thread: {response.status_code}")
        
        return False
        
    except Exception as e:
        logger.error(f"❌ Помилка відправки фото в thread {thread_id}: {e}")
        return False

async def send_photo_to_thread(bot_token: str, chat_id: str, thread_id: int, photo_url: str, caption: str = "", project_tag: str = "") -> bool:
    """Асинхронно відправити фото в thread з тегом"""
    return send_photo_to_thread_sync(bot_token, chat_id, thread_id, photo_url, caption, project_tag)

def send_message_with_photos_to_thread_sync(bot_token: str, chat_id: str, thread_id: int, text: str, photo_urls: List[str], project_tag: str = "") -> bool:
    """Синхронно відправити повідомлення з фотографіями в thread (фото в одному повідомленні)"""
    try:
        # Додаємо тег до початку повідомлення
        if project_tag and not text.startswith(project_tag):
            tagged_text = f"{project_tag}\n\n{text}"
        else:
            tagged_text = text
        
        if not photo_urls:
            # Якщо немає фото, відправляємо звичайне повідомлення
            return send_message_to_thread_sync(bot_token, chat_id, thread_id, tagged_text, project_tag)
        
        # Якщо є тільки одне фото, використовуємо sendPhoto з caption
        if len(photo_urls) == 1:
            try:
                # Завантажуємо зображення
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                }
                
                response = requests.get(photo_urls[0], headers=headers, timeout=15)
                response.raise_for_status()
                
                # Відправляємо через sendPhoto з текстом як caption
                url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
                
                files = {'photo': ('image.jpg', response.content, 'image/jpeg')}
                data = {
                    'chat_id': normalize_chat_id(chat_id),
                    'message_thread_id': thread_id,
                    'caption': tagged_text[:1024] if tagged_text else '',  # Обмеження Telegram для caption
                    'parse_mode': 'HTML'
                }
                
                response = requests.post(url, files=files, data=data, timeout=30)
                
                if response.status_code == 200:
                    result = response.json()
                    if result.get('ok'):
                        logger.info(f"✅ Повідомлення з фото відправлено в thread {thread_id}")
                        import time
                        time.sleep(1.5)
                        return True
                elif response.status_code == 429:
                    # Обробка rate limit
                    error_response = response.json()
                    retry_after = error_response.get('parameters', {}).get('retry_after', 15)
                    logger.warning(f"⚠️ Rate limit, чекаємо {retry_after} секунд...")
                    import time
                    time.sleep(retry_after + 2)
                    # Повторна спроба
                    response2 = requests.post(url, files=files, data=data, timeout=30)
                    if response2.status_code == 200:
                        result2 = response2.json()
                        if result2.get('ok'):
                            logger.info(f"✅ Повідомлення з фото відправлено після повторної спроби")
                            time.sleep(1.5)
                            return True
                
                logger.error(f"❌ Не вдалося відправити повідомлення з фото: {response.status_code}")
                return False
                
            except Exception as e:
                logger.error(f"❌ Помилка відправки повідомлення з одним фото: {e}")
                return False
        
        else:
            # Якщо кілька фото, спочатку відправляємо текст, потім медіа-групу
            # Спочатку відправляємо текстове повідомлення
            success = send_message_to_thread_sync(bot_token, chat_id, thread_id, tagged_text, "")
            if not success:
                return False
                
            # Потім відправляємо медіа-групу з фото
            try:
                media = []
                for i, photo_url in enumerate(photo_urls[:10]):  # Telegram дозволяє максимум 10 медіа в групі
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    }
                    
                    response = requests.get(photo_url, headers=headers, timeout=15)
                    response.raise_for_status()
                    
                    media.append({
                        'type': 'photo',
                        'media': f'attach://photo{i}',
                        'caption': f'📷 {i+1}/{len(photo_urls)}' if i == 0 else ''  # Caption тільки на першому фото
                    })
                
                url = f"https://api.telegram.org/bot{bot_token}/sendMediaGroup"
                
                # Підготовка файлів для відправки
                files = {}
                for i, photo_url in enumerate(photo_urls[:10]):
                    response = requests.get(photo_url, headers=headers, timeout=15)
                    files[f'photo{i}'] = ('image.jpg', response.content, 'image/jpeg')
                
                data = {
                    'chat_id': normalize_chat_id(chat_id),
                    'message_thread_id': thread_id,
                    'media': json.dumps(media)
                }
                
                response = requests.post(url, files=files, data=data, timeout=30)
                
                if response.status_code == 200:
                    result = response.json()
                    if result.get('ok'):
                        logger.info(f"✅ Медіа-група з {len(photo_urls)} фото відправлена в thread {thread_id}")
                        import time
                        time.sleep(2)
                        return True
                elif response.status_code == 429:
                    # Обробка rate limit
                    error_response = response.json()
                    retry_after = error_response.get('parameters', {}).get('retry_after', 15)
                    logger.warning(f"⚠️ Rate limit для медіа-групи, чекаємо {retry_after} секунд...")
                    import time
                    time.sleep(retry_after + 2)
                
                logger.error(f"❌ Не вдалося відправити медіа-групу: {response.status_code}")
                return False
                
            except Exception as e:
                logger.error(f"❌ Помилка відправки медіа-групи: {e}")
                return False
        
    except Exception as e:
        logger.error(f"❌ Помилка відправки повідомлення з фотографіями в thread {thread_id}: {e}")
        return False

# ===================== Визначення отримувачів за проектами =====================
def get_users_tracking_discord_channel(channel_id: str) -> List[Dict]:
    """Повертає список даних користувачів і проектів, що мають проект з цим Discord channel_id."""
    try:
        tracked_data: List[Dict] = []
        target = (channel_id or '').strip()
        for user_id_str, projects in project_manager.data.get('projects', {}).items():
            for p in projects:
                if p.get('platform') == 'discord':
                    cid = extract_discord_channel_id(p.get('url', ''))
                    if cid == target:
                        try:
                            tracked_data.append({
                                'user_id': int(user_id_str),
                                'project': p
                            })
                        except:
                            pass
        return tracked_data
    except Exception:
        return []

def get_discord_server_name(channel_id: str, guild_id: str) -> str:
    """Отримати назву Discord сервера з проекту користувача"""
    try:
        # Шукаємо проект з цим channel_id
        for user_id_str, projects in project_manager.data.get('projects', {}).items():
            for project in projects:
                if project.get('platform') == 'discord':
                    project_channel_id = extract_discord_channel_id(project.get('url', ''))
                    if project_channel_id == channel_id:
                        # Повертаємо назву проекту як назву сервера
                        project_name = project.get('name', 'Discord')
                        # Якщо назва проекту вже містить "Discord", не дублюємо
                        if 'Discord' in project_name:
                            return project_name
                        else:
                            return f"Discord Server ({project_name})"
        
        # Якщо не знайшли, повертаємо з guild_id
        return f"Discord Server ({guild_id})"
    except Exception as e:
        logger.error(f"Помилка отримання назви Discord сервера: {e}")
        return f"Discord Server ({guild_id})"

# ===================== Система збереження mapping'у гілок =====================
def load_threads_mapping() -> Dict:
    """Завантажити mapping проектів до thread_id"""
    try:
        with open('threads_mapping.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_threads_mapping(mapping: Dict) -> None:
    """Зберегти mapping проектів до thread_id"""
    try:
        with open('threads_mapping.json', 'w', encoding='utf-8') as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2)
        logger.info(f"💾 Збережено mapping гілок: {len(mapping)} записів")
    except Exception as e:
        logger.error(f"❌ Помилка збереження mapping'у гілок: {e}")

def get_project_thread_id(user_id: str, project_name: str, chat_id: str) -> Optional[int]:
    """Отримати thread_id для проекту"""
    mapping = load_threads_mapping()
    key = f"{user_id}_{project_name}_{chat_id}"
    thread_id = mapping.get(key)
    if thread_id:
        logger.debug(f"🔍 Знайдено thread_id для {project_name}: {thread_id}")
    else:
        logger.debug(f"🔍 Не знайдено thread_id для {project_name}")
    return thread_id

def save_project_thread_id(user_id: str, project_name: str, chat_id: str, thread_id: int) -> None:
    """Зберегти thread_id для проекту"""
    mapping = load_threads_mapping()
    key = f"{user_id}_{project_name}_{chat_id}"
    mapping[key] = thread_id
    save_threads_mapping(mapping)
    logger.info(f"💾 Збережено thread_id {thread_id} для проекту {project_name}")

# ===================== Визначення отримувачів за проектами =====================
def get_users_tracking_twitter(username: str) -> List[Dict]:
    """Повертає список даних користувачів і проектів, що мають проект з цим Twitter username."""
    try:
        tracked_data: List[Dict] = []
        target = (username or '').replace('@', '').strip().lower()
        
        # Додаткове логування для діагностики
        logger.info(f"🔍 Шукаємо користувачів для Twitter акаунта: '{target}'")
        
        for user_id_str, projects in project_manager.data.get('projects', {}).items():
            for p in projects:
                if p.get('platform') == 'twitter':
                    u = extract_twitter_username(p.get('url', '') or '')
                    if u:
                        project_username = u.replace('@', '').strip().lower()
                        logger.debug(f"   Порівнюємо '{project_username}' з '{target}' для користувача {user_id_str}")
                        if project_username == target:
                            tracked_data.append({
                                'user_id': int(user_id_str),
                                'project': p
                            })
                            logger.info(f"✅ Знайдено користувача {user_id_str} для Twitter акаунта {target}")
                            break
        
        if not tracked_data:
            logger.warning(f"⚠️ Не знайдено користувачів для Twitter акаунта '{target}' - твіт буде пропущено")
        
        return tracked_data
    except Exception as e:
        logger.error(f"Помилка в get_users_tracking_twitter для {username}: {e}")
        return []


@require_auth
async def handle_forwarded_channel_setup(update: Update, context: ContextTypes.DEFAULT_TYPE, fwd_chat) -> None:
    """Автоматичне налаштування каналу за пересланим повідомленням з каналу/групи."""
    if not update.effective_user or not update.message:
        return
        
    user_id = update.effective_user.id
    try:
        chat_type = getattr(fwd_chat, 'type', '')
        chat_id = getattr(fwd_chat, 'id', None)
        title = getattr(fwd_chat, 'title', '') or getattr(fwd_chat, 'username', '') or 'Unknown'
        if not chat_id:
            await update.message.reply_text("❌ Не вдалося визначити ID каналу із пересланого повідомлення.")
            return
        # Зберігаємо чат для користувача
        channel_id_str = str(chat_id)
        project_manager.set_forward_channel(user_id, channel_id_str)
        # Тестове повідомлення у канал
        try:
            await context.bot.send_message(
                chat_id=normalize_chat_id(channel_id_str),
                text=f"✅ Канал підключено! Користувач @{update.effective_user.username or user_id} отримуватиме сповіщення сюди.")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Не вдалося надіслати повідомлення у канал: {e}")
        await update.message.reply_text(
            f"✅ Автоналаштування завершено!\n\nКанал: {title}\nID: `{normalize_chat_id(channel_id_str)}`",
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка автоналаштування: {e}")

# ===================== Персональні налаштування пересилання =====================
@require_auth
async def forward_enable_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
        
    user_id = update.effective_user.id
    if project_manager.enable_forward(user_id):
        status = project_manager.get_forward_status(user_id)
        channel_id = status.get('channel_id') or '—'
        await update.message.reply_text(
            f"🟢 Пересилання увімкнено. Поточний канал: `{channel_id}`",
        )
    else:
        await update.message.reply_text("❌ Не вдалося увімкнути пересилання.")

@require_auth
async def forward_disable_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
        
    user_id = update.effective_user.id
    if project_manager.disable_forward(user_id):
        await update.message.reply_text("🔴 Пересилання вимкнено.")
    else:
        await update.message.reply_text("❌ Не вдалося вимкнути пересилання.")

@require_auth
async def forward_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
        
    user_id = update.effective_user.id
    status = project_manager.get_forward_status(user_id)
    enabled = status.get('enabled', False)
    channel_id = status.get('channel_id') or '—'
    await update.message.reply_text(
        f"📊 Статус пересилання\n\n"
        f"• Статус: {'🟢 Увімкнено' if enabled else '🔴 Вимкнено'}\n"
        f"• Канал: `{channel_id}`\n\n"
        f"Як налаштувати канал: додайте бота як адміністратора в канал/групу та напишіть там: @" + context.bot.username + " ping",
    )

@require_auth
async def forward_set_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
        
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text(
            "Вкажіть ID каналу. Приклад: /forward_set_channel -1001234567890\n\nПідказка: простіше — зайдіть у потрібний канал та напишіть там повідомлення: @"
            + context.bot.username + " ping (бот збере ID автоматично)")
        return
    channel_id = context.args[0]
    if project_manager.set_forward_channel(user_id, str(channel_id)):
        await update.message.reply_text(
            f"✅ Канал пересилання збережено: `{channel_id}`",
        )
    else:
        await update.message.reply_text("❌ Не вдалося зберегти канал.")

@require_auth
async def forward_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
        
    user_id = update.effective_user.id
    channel_id = project_manager.get_forward_channel(user_id)
    if not channel_id:
        await update.message.reply_text("❌ Канал не налаштовано. Спробуйте /forward_set_channel або напишіть у каналі: @" + context.bot.username + " ping")
        return
    
    # Перевіряємо режим thread'ів
    forward_status = project_manager.get_forward_status(user_id)
    use_threads = forward_status.get('use_threads', True)
    
    try:
        if use_threads:
            # Тестування в режимі thread'ів
            user_projects = project_manager.get_user_projects(user_id)
            if not user_projects:
                await update.message.reply_text("❌ У вас немає проектів для тестування thread'ів")
                return
            
            # Беремо перший проект для тесту
            test_project = user_projects[0]
            project_id = test_project.get('id')
            project_name = test_project.get('name', 'Test Project')
            project_tag = test_project.get('tag', f"#test_{project_id}")
            
            # Отримуємо або створюємо thread
            thread_id = project_manager.get_project_thread(user_id, project_id)
            
            if not thread_id:
                thread_id = create_project_thread_sync(BOT_TOKEN, channel_id, project_name, project_tag, str(user_id))
                
                if thread_id:
                    project_manager.set_project_thread(user_id, project_id, thread_id)
            
            if thread_id:
                test_text = (
                    f"🧪 **Тестове повідомлення thread'а**\n\n"
                    f"• Проект: {project_name}\n"
                    f"• Тег: {project_tag}\n"
                    f"• Thread ID: {thread_id}\n"
                    f"• Час: {datetime.now().strftime('%H:%M:%S')}\n\n"
                    f"✅ Якщо ви бачите це повідомлення в окремій гілці, то все працює правильно!"
                )
                
                success = send_message_to_thread_sync(BOT_TOKEN, channel_id, thread_id, test_text, project_tag)
                
                if success:
                    await update.message.reply_text(f"✅ Тестове повідомлення відправлено в гілку '{project_name}' (Thread {thread_id})")
                else:
                    await update.message.reply_text(f"❌ Помилка відправки в thread {thread_id}")
            else:
                await update.message.reply_text("❌ Не вдалося створити або знайти thread")
        else:
            # Стандартний тест без thread'ів
            text = (
                "#test_message\n\n"
                "✅ Тестове повідомлення пересилання\n\n"
                "Це перевірка ваших персональних налаштувань в режимі тегів."
            )
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            data = {'chat_id': normalize_chat_id(channel_id), 'text': text}
            r = requests.post(url, data=data, timeout=5)
            if r.status_code == 200:
                await update.message.reply_text("✅ Тест відправлено у ваш канал пересилання з тегом.")
            else:
                await update.message.reply_text(f"❌ Помилка відправки у канал: {r.status_code}")
    except Exception as e:
        await update.message.reply_text(f"❌ Виняток: {e}")

@require_auth  
async def thread_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда для тестування конкретного thread'а проекту"""
    if not update.effective_user or not update.message:
        return
        
    user_id = update.effective_user.id
    
    if not context.args:
        await update.message.reply_text(
            "🧪 **Тестування гілок проектів**\n\n"
            "Використання: /thread_test [project_id]\n\n"
            "Приклади:\n"
            "• `/thread_test 1` - тест першого проекту\n"
            "• `/thread_test` - тест всіх проектів\n\n"
            "Команда створить або знайде гілку для проекту і відправить тестове повідомлення."
        )
        return
    
    try:
        project_id = int(context.args[0])
        project = project_manager.get_project_by_id(user_id, project_id)
        
        if not project:
            await update.message.reply_text(f"❌ Проект з ID {project_id} не знайдено")
            return
        
        forward_channel = project_manager.get_forward_channel(user_id)
        if not forward_channel:
            await update.message.reply_text("❌ Канал пересилання не налаштовано")
            return
        
        project_name = project.get('name', 'Test Project')
        project_tag = project.get('tag', f"#test_{project_id}")
        
        # Отримуємо або створюємо thread
        thread_id = project_manager.get_project_thread(user_id, project_id)
        
        if not thread_id:
            thread_id = create_project_thread_sync(BOT_TOKEN, forward_channel, project_name, project_tag, str(user_id))
            
            if thread_id:
                project_manager.set_project_thread(user_id, project_id, thread_id)
        
        if thread_id:
            test_text = (
                f"🧪 **Тестування гілки проекту**\n\n"
                f"• Проект: {project_name}\n"
                f"• Тег: {project_tag}\n"
                f"• Платформа: {project.get('platform', 'unknown')}\n"
                f"• URL: {project.get('url', 'немає')}\n"
                f"• Thread ID: {thread_id}\n"
                f"• Час тесту: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n\n"
                f"✅ Тест пройшов успішно! Гілка працює правильно."
            )
            
            success = send_message_to_thread_sync(BOT_TOKEN, forward_channel, thread_id, test_text, project_tag)
            
            if success:
                await update.message.reply_text(
                    f"✅ **Тест успішний!**\n\n"
                    f"Проект: {project_name}\n"
                    f"Гілка: {thread_id}\n"
                    f"Тег: {project_tag}\n\n"
                    f"Перевірте канал для тестового повідомлення."
                )
            else:
                await update.message.reply_text(f"❌ Помилка відправки в гілку {thread_id}")
        else:
            await update.message.reply_text("❌ Не вдалося створити або знайти гілку")
            
    except ValueError:
        await update.message.reply_text("❌ Невірний ID проекту. Використовуйте число.")
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка: {e}")

@require_auth  
async def setup_quick_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Швидке налаштування каналу пересилання"""
    if not update.effective_user or not update.message:
        return
        
    user_id = update.effective_user.id
    
    # Перевіряємо чи вже налаштовано
    current_channel = project_manager.get_forward_channel(user_id)
    if current_channel:
        await update.message.reply_text(
            f"ℹ️ **Канал вже налаштовано**\n\n"
            f"Поточний канал: `{current_channel}`\n\n"
            f"🔄 Для зміни каналу:\n"
            f"1. Створіть нову групу з увімкненими Topics (гілки)\n"
            f"2. Додайте бота як адміністратора\n"
            f"3. Напишіть в групі: @{context.bot.username} ping\n\n"
            f"⚡ Або використайте `/forward_set_channel <ID_каналу>`"
        )
        return
    
    await update.message.reply_text(
        f"🚀 **Швидке налаштування каналу пересилання**\n\n"
        f"📋 **Кроки:**\n"
        f"1. Створіть нову групу в Telegram\n"
        f"2. Увімкніть Topics (гілки) в налаштуваннях групи\n"
        f"3. Додайте цього бота як адміністратора з правами керування повідомленнями\n"
        f"4. Напишіть в групі: `@{context.bot.username} ping`\n\n"
        f"✅ Бот автоматично налаштує групу для пересилання!\n\n"
        f"💡 **Альтернатива:** `/forward_set_channel <ID_каналу>`"
    )

def cleanup_old_tweets():
    """Очистити старі твіти з глобального відстеження (залишити тільки останні 200)"""
    global global_sent_tweets
    
    for account in global_sent_tweets:
        if len(global_sent_tweets[account]) > 200:
            # Конвертуємо в список та залишаємо останні 200 записів
            tweets_list = list(global_sent_tweets[account])
            
            # Розділяємо на ID твітів та хеші контенту
            tweet_ids = [t for t in tweets_list if not t.startswith('content_')]
            content_hashes = [t for t in tweets_list if t.startswith('content_')]
            
            # Залишаємо останні 100 ID твітів та 100 хешів контенту
            tweet_ids = tweet_ids[-100:] if len(tweet_ids) > 100 else tweet_ids
            content_hashes = content_hashes[-100:] if len(content_hashes) > 100 else content_hashes
            
            # Об'єднуємо та оновлюємо
            global_sent_tweets[account] = set(tweet_ids + content_hashes)
            logger.info(f"Очищено старі твіти для {account}, залишено {len(global_sent_tweets[account])} записів")

def reset_seen_tweets():
    """Очистити всі збережені seen_tweets файли (використовувати обережно!)"""
    global twitter_monitor, twitter_monitor_adapter
    
    try:
        import os
        
        # Очищаємо файли seen_tweets
        files_to_clear = [
            "twitter_api_seen_tweets.json",
            "twitter_monitor_seen_tweets.json"
        ]
        
        for file_path in files_to_clear:
            if os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    logger.info(f"Видалено файл {file_path}")
                except Exception as e:
                    logger.error(f"Помилка видалення файлу {file_path}: {e}")
        
        # Очищаємо пам'ять в моніторах
        if twitter_monitor:
            twitter_monitor.seen_tweets = {}
            twitter_monitor.sent_tweets = {}
            logger.info("Очищено seen_tweets в Twitter Monitor")
            
        if twitter_monitor_adapter:
            twitter_monitor_adapter.seen_tweets = {}
            twitter_monitor_adapter.sent_tweets = {}
            logger.info("Очищено seen_tweets в Twitter Monitor Adapter")
            
        # Очищаємо глобальний список
        global_sent_tweets.clear()
        logger.info("Очищено global_sent_tweets")
        
        logger.info("✅ Всі seen_tweets успішно очищено!")
        return True
        
    except Exception as e:
        logger.error(f"Помилка очищення seen_tweets: {e}")
        return False

def format_success_message(title: str, message: str, additional_info: str = None) -> str:
    """Форматувати повідомлення про успіх"""
    formatted = f"✅ {title}\n\n{message}"
    if additional_info:
        formatted += f"\n\n💡 {additional_info}"
    return formatted

def format_error_message(title: str, error: str, suggestion: str = None) -> str:
    """Форматувати повідомлення про помилку"""
    formatted = f"❌ {title}\n\n{error}"
    if suggestion:
        formatted += f"\n\n💡 Рекомендація: {suggestion}"
    return formatted

def format_info_message(title: str, message: str, details: str = None) -> str:
    """Форматувати інформаційне повідомлення"""
    formatted = f"ℹ️ {title}\n\n{message}"
    if details:
        formatted += f"\n\n📋 Деталі:\n{details}"
    return formatted

def format_warning_message(title: str, message: str, action: str = None) -> str:
    """Форматувати попереджувальне повідомлення"""
    formatted = f"⚠️ {title}\n\n{message}"
    if action:
        formatted += f"\n\n🔧 Дія: {action}"
    return formatted

async def delete_message_after_delay(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay: int = 3):
    """Видалити повідомлення через певний час"""
    try:
        await asyncio.sleep(delay)
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception as e:
        logger.warning(f"Не вдалося видалити повідомлення {message_id}: {e}")

async def safe_delete_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    """Безпечно видалити повідомлення"""
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception as e:
        logger.warning(f"Не вдалося видалити повідомлення {message_id}: {e}")
        return False

def download_and_send_image(image_url: str, chat_id: str, caption: str = "") -> bool:
    """Завантажити та відправити зображення в Telegram"""
    try:
        # Додаємо параметри для Twitter зображень якщо потрібно
        if 'pbs.twimg.com/media/' in image_url and '?' not in image_url:
            image_url += '?format=jpg&name=medium'
        
        # Завантажуємо зображення
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Referer': 'https://x.com/'
        }
        
        logger.info(f"📥 Завантажуємо зображення: {image_url}")
        response = requests.get(image_url, headers=headers, timeout=15)
        response.raise_for_status()
        logger.info(f"✅ Зображення завантажено успішно, розмір: {len(response.content)} байт")
        
        # Перевіряємо розмір файлу (максимум 20MB для Telegram)
        if len(response.content) > 20 * 1024 * 1024:
            logger.warning(f"Зображення занадто велике: {len(response.content)} байт")
            return False
        
        # Визначаємо розширення файлу
        content_type = response.headers.get('content-type', '')
        if 'jpeg' in content_type or 'jpg' in content_type:
            suffix = '.jpg'
        elif 'png' in content_type:
            suffix = '.png'
        elif 'webp' in content_type:
            suffix = '.webp'
        else:
            suffix = '.jpg'  # За замовчуванням
        
        # Створюємо тимчасовий файл
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_file:
            temp_file.write(response.content)
            temp_file_path = temp_file.name
        
        try:
            # Відправляємо фото через Telegram API
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
            
            with open(temp_file_path, 'rb') as photo_file:
                files = {'photo': photo_file}
                data = {
                    'chat_id': normalize_chat_id(chat_id),
                    'caption': caption[:1024] if caption else '',  # Telegram обмежує caption до 1024 символів
                }
                
                response = requests.post(url, files=files, data=data, timeout=30)
                
                if response.status_code == 200:
                    logger.info(f"✅ Зображення відправлено в канал {chat_id}")
                    return True
                else:
                    logger.error(f"❌ Помилка відправки зображення: {response.status_code}")
                    logger.error(f"Відповідь сервера: {response.text}")
                    return False
                    
        finally:
            # Видаляємо тимчасовий файл
            try:
                os.unlink(temp_file_path)
            except:
                pass
                
    except Exception as e:
        logger.error(f"Помилка завантаження/відправки зображення: {e}")
        return False

def get_main_menu_keyboard(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    """Створити головне меню з урахуванням ролі користувача"""
    keyboard = [
        # Основні функції
        [InlineKeyboardButton("📋 Мої проекти", callback_data="my_projects"),
         InlineKeyboardButton("➕ Створити проект", callback_data="add_project")],
        
        # Моніторинг
        [InlineKeyboardButton("🐦 Twitter", callback_data="twitter_adapter"),
         InlineKeyboardButton("💬 Discord", callback_data="discord_history")],
        
        # Швидкі дії
        [InlineKeyboardButton("⚡ Швидкі дії", callback_data="quick_actions"),
         InlineKeyboardButton("📊 Статистика", callback_data="user_stats")],
        
        # Налаштування
        [InlineKeyboardButton("📢 Пересилання", callback_data="forward_settings"),
         InlineKeyboardButton("⚙️ Налаштування", callback_data="settings")],
        
        # Допомога та інформація
        [InlineKeyboardButton("❓ Допомога", callback_data="help"),
         InlineKeyboardButton("ℹ️ Про бота", callback_data="about")]
    ]
    
    # Додаємо адміністративні кнопки для адміністраторів
    if user_id and access_manager.is_admin(user_id):
        keyboard.append([InlineKeyboardButton("👑 Адміністративна панель", callback_data="admin_panel")])
    
    return InlineKeyboardMarkup(keyboard)

def get_platform_keyboard() -> InlineKeyboardMarkup:
    """Створити клавіатуру вибору платформи"""
    keyboard = [
        [InlineKeyboardButton("🐦 Twitter/X", callback_data="platform_twitter")],
        [InlineKeyboardButton("💬 Discord", callback_data="platform_discord")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_projects_menu_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Створити меню управління проектами"""
    projects = project_manager.get_user_projects(user_id)
    selenium_accounts = project_manager.get_selenium_accounts()
    
    keyboard = []
    
    # Twitter проекти
    twitter_projects = [p for p in projects if p['platform'] == 'twitter']
    if twitter_projects:
        keyboard.append([InlineKeyboardButton("🐦 Twitter проекти", callback_data="twitter_projects")])
    
    # Discord проекти
    discord_projects = [p for p in projects if p['platform'] == 'discord']
    if discord_projects:
        keyboard.append([InlineKeyboardButton("💬 Discord проекти", callback_data="discord_projects")])
    
    # Twitter Monitor Adapter акаунти
    if selenium_accounts:
        keyboard.append([InlineKeyboardButton("🚀 Twitter Monitor Adapter", callback_data="twitter_adapter_accounts")])
    
    # Кнопки додавання
    keyboard.append([InlineKeyboardButton("➕ Додати Twitter", callback_data="add_twitter")])
    keyboard.append([InlineKeyboardButton("➕ Додати Discord", callback_data="add_discord")])
    keyboard.append([InlineKeyboardButton("🚀 Додати Twitter Adapter", callback_data="add_twitter_adapter")])
    
    # Назад
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")])
    
    return InlineKeyboardMarkup(keyboard)

def get_twitter_projects_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Створити клавіатуру Twitter проектів"""
    projects = project_manager.get_user_projects(user_id)
    twitter_projects = [p for p in projects if p['platform'] == 'twitter']
    
    keyboard = []
    
    for project in twitter_projects:
        # Обмежуємо довжину назви
        name = project['name'][:20] + "..." if len(project['name']) > 20 else project['name']
        keyboard.append([
            InlineKeyboardButton(f"🐦 {name}", callback_data=f"view_twitter_{project['id']}"),
            InlineKeyboardButton("❌", callback_data=f"delete_twitter_{project['id']}")
        ])
    
    keyboard.append([InlineKeyboardButton("➕ Додати Twitter", callback_data="add_twitter")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="projects_menu")])
    
    return InlineKeyboardMarkup(keyboard)

def get_discord_projects_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Створити клавіатуру Discord проектів"""
    projects = project_manager.get_user_projects(user_id)
    discord_projects = [p for p in projects if p['platform'] == 'discord']
    
    keyboard = []
    
    for project in discord_projects:
        # Обмежуємо довжину назви
        name = project['name'][:20] + "..." if len(project['name']) > 20 else project['name']
        keyboard.append([
            InlineKeyboardButton(f"💬 {name}", callback_data=f"view_discord_{project['id']}"),
            InlineKeyboardButton("❌", callback_data=f"delete_discord_{project['id']}")
        ])
    
    keyboard.append([InlineKeyboardButton("➕ Додати Discord", callback_data="add_discord")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="projects_menu")])
    
    return InlineKeyboardMarkup(keyboard)

def get_twitter_adapter_accounts_keyboard() -> InlineKeyboardMarkup:
    """Створити клавіатуру Twitter Monitor Adapter акаунтів"""
    twitter_adapter_accounts = project_manager.get_selenium_accounts()
    
    keyboard = []
    
    for username in twitter_adapter_accounts:
        keyboard.append([
            InlineKeyboardButton(f"🚀 @{username}", callback_data=f"view_twitter_adapter_{username}"),
            InlineKeyboardButton("❌", callback_data=f"delete_twitter_adapter_{username}")
        ])
    
    keyboard.append([InlineKeyboardButton("➕ Додати Twitter Adapter", callback_data="add_twitter_adapter")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="projects_menu")])
    
    return InlineKeyboardMarkup(keyboard)

def get_history_count_keyboard() -> InlineKeyboardMarkup:
    """Створити клавіатуру вибору кількості повідомлень"""
    keyboard = [
        [InlineKeyboardButton("📄 Останні 5", callback_data="history_5")],
        [InlineKeyboardButton("📄 Останні 10", callback_data="history_10")],
        [InlineKeyboardButton("📄 Останні 20", callback_data="history_20")],
        [InlineKeyboardButton("📄 Останні 50", callback_data="history_50")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_discord_channels_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Створити клавіатуру з Discord каналами користувача"""
    projects = project_manager.get_user_projects(user_id)
    discord_projects = [p for p in projects if p['platform'] == 'discord']
    
    keyboard = []
    for project in discord_projects:
        keyboard.append([InlineKeyboardButton(
            f"💬 {project['name']}", 
            callback_data=f"channel_{project['id']}"
        )])
    
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)

def get_forward_settings_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Створити клавіатуру налаштувань пересилання"""
    forward_status = project_manager.get_forward_status(user_id)
    use_threads = forward_status.get('use_threads', True)
    
    keyboard = []
    
    if forward_status['enabled']:
        keyboard.append([InlineKeyboardButton("🔴 Вимкнути пересилання", callback_data="disable_forward")])
        keyboard.append([InlineKeyboardButton("✏️ Змінити канал", callback_data="change_channel")])
    else:
        keyboard.append([InlineKeyboardButton("🟢 Увімкнути пересилання", callback_data="enable_forward")])
        keyboard.append([InlineKeyboardButton("📝 Встановити канал", callback_data="set_channel")])
    
    # Додаємо управління thread'ами
    threads_text = "🧵 Використовувати теги" if use_threads else "🧵 Використовувати гілки"
    threads_action = "disable_threads" if use_threads else "enable_threads"
    keyboard.append([InlineKeyboardButton(threads_text, callback_data=threads_action)])
    
    if use_threads and forward_status['enabled']:
        keyboard.append([InlineKeyboardButton("🧪 Тест гілок", callback_data="test_threads")])
        keyboard.append([InlineKeyboardButton("🔧 Управління гілками", callback_data="manage_threads")])
    
    keyboard.append([InlineKeyboardButton("🤖 Автоналаштування", callback_data="auto_setup")])
    keyboard.append([InlineKeyboardButton("📊 Статус", callback_data="forward_status")])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")])
    
    return InlineKeyboardMarkup(keyboard)

def get_quick_actions_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Створити клавіатуру швидких дій"""
    keyboard = [
        [InlineKeyboardButton("🚀 Запустити всі монітори", callback_data="start_all_monitors")],
        [InlineKeyboardButton("⏹️ Зупинити всі монітори", callback_data="stop_all_monitors")],
        [InlineKeyboardButton("📊 Швидка статистика", callback_data="quick_stats")],
        [InlineKeyboardButton("🔍 Діагностика", callback_data="diagnostics")],
        [InlineKeyboardButton("📝 Останні повідомлення", callback_data="recent_messages")],
        [InlineKeyboardButton("🔄 Оновити дані", callback_data="refresh_data")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_help_keyboard() -> InlineKeyboardMarkup:
    """Створити клавіатуру допомоги"""
    keyboard = [
        [InlineKeyboardButton("🚀 Початок роботи", callback_data="help_getting_started")],
        [InlineKeyboardButton("🐦 Twitter налаштування", callback_data="help_twitter")],
        [InlineKeyboardButton("💬 Discord налаштування", callback_data="help_discord")],
        [InlineKeyboardButton("📢 Пересилання", callback_data="help_forwarding")],
        [InlineKeyboardButton("⚙️ Налаштування", callback_data="help_settings")],
        [InlineKeyboardButton("❓ Часті питання", callback_data="help_faq")],
        [InlineKeyboardButton("📞 Підтримка", callback_data="help_support")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_settings_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Створити клавіатуру налаштувань"""
    keyboard = [
        [InlineKeyboardButton("🔔 Сповіщення", callback_data="settings_notifications")],
        [InlineKeyboardButton("⏰ Інтервали моніторингу", callback_data="settings_intervals")],
        [InlineKeyboardButton("🎨 Тема інтерфейсу", callback_data="settings_theme")],
        [InlineKeyboardButton("🌐 Мова", callback_data="settings_language")],
        [InlineKeyboardButton("🔒 Безпека", callback_data="settings_security")],
        [InlineKeyboardButton("📊 Експорт даних", callback_data="settings_export")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_diagnostics_keyboard() -> InlineKeyboardMarkup:
    """Створити клавіатуру діагностики"""
    keyboard = [
        [InlineKeyboardButton("🔍 Перевірити бота", callback_data="check_bot_status")],
        [InlineKeyboardButton("📺 Тест каналів", callback_data="test_channels")],
        [InlineKeyboardButton("🔗 Discord API", callback_data="test_discord_api")],
        [InlineKeyboardButton("📊 Статистика", callback_data="show_stats")],
        [InlineKeyboardButton("🔄 Перезавантажити", callback_data="reload_data")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_panel_keyboard() -> InlineKeyboardMarkup:
    """Створити клавіатуру адміністративної панелі"""
    keyboard = [
        [InlineKeyboardButton("👥 Управління користувачами", callback_data="admin_users")],
        [InlineKeyboardButton("📊 Статистика та аналітика", callback_data="admin_stats")],
        [InlineKeyboardButton("🔧 Системне управління", callback_data="admin_system")],
        [InlineKeyboardButton("📋 Всі проекти", callback_data="admin_all_projects")],
        [InlineKeyboardButton("➕ Створити проект", callback_data="admin_create_for_user")],
        [InlineKeyboardButton("🔍 Пошук та фільтри", callback_data="admin_search")],
        [InlineKeyboardButton("📈 Моніторинг", callback_data="admin_monitoring")],
        [InlineKeyboardButton("⚙️ Налаштування", callback_data="admin_settings")],
        [InlineKeyboardButton("⬅️ Головне меню", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_users_keyboard() -> InlineKeyboardMarkup:
    """Створити клавіатуру управління користувачами"""
    keyboard = [
        [InlineKeyboardButton("👥 Список користувачів", callback_data="admin_list_users")],
        [InlineKeyboardButton("📊 Статистика користувачів", callback_data="admin_user_stats")],
        [InlineKeyboardButton("➕ Додати користувача", callback_data="admin_add_user")],
        [InlineKeyboardButton("👑 Додати адміністратора", callback_data="admin_add_admin")],
        [InlineKeyboardButton("🔍 Пошук користувача", callback_data="admin_search_user")],
        [InlineKeyboardButton("🔄 Змінити роль", callback_data="admin_change_role")],
        [InlineKeyboardButton("🔐 Скинути пароль", callback_data="admin_reset_password")],
        [InlineKeyboardButton("🔁 Налаштування пересилання", callback_data="admin_forward")],
        [InlineKeyboardButton("🗑️ Видалити користувача", callback_data="admin_delete_user")],
        [InlineKeyboardButton("📈 Активність користувачів", callback_data="admin_user_activity")],
        [InlineKeyboardButton("⬅️ Назад до адмін панелі", callback_data="admin_panel")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_forward_keyboard(target_user_id: int) -> InlineKeyboardMarkup:
    """Клавіатура керування пересиланням для конкретного користувача"""
    status = project_manager.get_forward_status(target_user_id)
    enabled = status.get('enabled', False)
    keyboard = []
    if enabled:
        keyboard.append([InlineKeyboardButton("🔴 Вимкнути", callback_data=f"admin_forward_disable_{target_user_id}")])
        keyboard.append([InlineKeyboardButton("✏️ Змінити канал", callback_data=f"admin_forward_set_{target_user_id}")])
    else:
        keyboard.append([InlineKeyboardButton("🟢 Увімкнути", callback_data=f"admin_forward_enable_{target_user_id}")])
        keyboard.append([InlineKeyboardButton("📝 Встановити канал", callback_data=f"admin_forward_set_{target_user_id}")])
    keyboard.append([
        InlineKeyboardButton("📊 Статус", callback_data=f"admin_forward_status_{target_user_id}"),
        InlineKeyboardButton("🧪 Тест", callback_data=f"admin_forward_test_{target_user_id}")
    ])
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin_users")])
    return InlineKeyboardMarkup(keyboard)

def get_admin_system_keyboard() -> InlineKeyboardMarkup:
    """Створити клавіатуру системного управління"""
    keyboard = [
        [InlineKeyboardButton("📊 Статистика системи", callback_data="admin_system_stats")],
        [InlineKeyboardButton("📋 Логи системи", callback_data="admin_system_logs")],
        [InlineKeyboardButton("💾 Бекап та відновлення", callback_data="admin_backup_restore")],
        [InlineKeyboardButton("🔄 Очистити сесії", callback_data="admin_cleanup_sessions")],
        [InlineKeyboardButton("🧹 Очистити кеш", callback_data="admin_clear_cache")],
        [InlineKeyboardButton("�️ Очистити seen_tweets", callback_data="admin_clear_seen_tweets")],
        [InlineKeyboardButton("�🔧 Налаштування системи", callback_data="admin_system_config")],
        [InlineKeyboardButton("⚠️ Скинути систему", callback_data="admin_reset_system")],
        [InlineKeyboardButton("⬅️ Назад до адмін панелі", callback_data="admin_panel")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_search_keyboard() -> InlineKeyboardMarkup:
    """Створити клавіатуру пошуку та фільтрів"""
    keyboard = [
        [InlineKeyboardButton("🔍 Пошук користувачів", callback_data="admin_search_users")],
        [InlineKeyboardButton("📋 Пошук проектів", callback_data="admin_search_projects")],
        [InlineKeyboardButton("📊 Фільтри статистики", callback_data="admin_stats_filters")],
        [InlineKeyboardButton("📅 Фільтр за датою", callback_data="admin_date_filter")],
        [InlineKeyboardButton("🏷️ Фільтр за тегами", callback_data="admin_tag_filter")],
        [InlineKeyboardButton("📈 Розширена аналітика", callback_data="admin_advanced_analytics")],
        [InlineKeyboardButton("⬅️ Назад до адмін панелі", callback_data="admin_panel")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_monitoring_keyboard() -> InlineKeyboardMarkup:
    """Створити клавіатуру моніторингу"""
    keyboard = [
        [InlineKeyboardButton("📈 Статус моніторингу", callback_data="admin_monitoring_status")],
        [InlineKeyboardButton("🔔 Налаштування сповіщень", callback_data="admin_notifications")],
        [InlineKeyboardButton("⏰ Розклад моніторингу", callback_data="admin_monitoring_schedule")],
        [InlineKeyboardButton("📊 Логи моніторингу", callback_data="admin_monitoring_logs")],
        [InlineKeyboardButton("🔄 Перезапустити моніторинг", callback_data="admin_restart_monitoring")],
        [InlineKeyboardButton("⚡ Швидкість відповіді", callback_data="admin_response_time")],
        [InlineKeyboardButton("⬅️ Назад до адмін панелі", callback_data="admin_panel")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_settings_keyboard() -> InlineKeyboardMarkup:
    """Створити клавіатуру налаштувань"""
    keyboard = [
        [InlineKeyboardButton("🔐 Налаштування безпеки", callback_data="admin_security_settings")],
        [InlineKeyboardButton("🎨 Налаштування інтерфейсу", callback_data="admin_ui_settings")],
        [InlineKeyboardButton("📱 Налаштування бота", callback_data="admin_bot_settings")],
        [InlineKeyboardButton("🌐 Налаштування мережі", callback_data="admin_network_settings")],
        [InlineKeyboardButton("💾 Налаштування збереження", callback_data="admin_storage_settings")],
        [InlineKeyboardButton("🔧 Розширені налаштування", callback_data="admin_advanced_settings")],
        [InlineKeyboardButton("⬅️ Назад до адмін панелі", callback_data="admin_panel")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_stats_keyboard() -> InlineKeyboardMarkup:
    """Створити клавіатуру статистики"""
    keyboard = [
        [InlineKeyboardButton("📊 Загальна статистика", callback_data="admin_general_stats")],
        [InlineKeyboardButton("👥 Статистика користувачів", callback_data="admin_user_stats")],
        [InlineKeyboardButton("📋 Статистика проектів", callback_data="admin_project_stats")],
        [InlineKeyboardButton("📈 Графіки та діаграми", callback_data="admin_charts")],
        [InlineKeyboardButton("📅 Статистика за період", callback_data="admin_period_stats")],
        [InlineKeyboardButton("🔍 Детальна аналітика", callback_data="admin_detailed_analytics")],
        [InlineKeyboardButton("📤 Експорт даних", callback_data="admin_export_data")],
        [InlineKeyboardButton("⬅️ Назад до адмін панелі", callback_data="admin_panel")]
    ]
    return InlineKeyboardMarkup(keyboard)

def escape_html(text: str) -> str:
    """Екранувати спеціальні символи для HTML"""
    if not text:
        return ""
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def extract_twitter_username(url: str) -> Optional[str]:
    """Витягти username з Twitter URL або просто username"""
    try:
        if not url:
            return None
            
        url = url.strip()
        
        # Якщо це повний URL з twitter.com або x.com
        if 'twitter.com' in url or 'x.com' in url:
            # Видаляємо протокол
            url = url.replace('https://', '').replace('http://', '')
            
            # Видаляємо www
            if url.startswith('www.'):
                url = url[4:]
                
            # Витягуємо username
            if url.startswith('twitter.com/'):
                username = url.split('/')[1]
            elif url.startswith('x.com/'):
                username = url.split('/')[1]
            else:
                return None
                
            # Очищаємо від зайвих символів
            username = username.split('?')[0].split('#')[0]
            
            return username if username else None
            
        # Якщо це просто username (без URL)
        elif url and not url.startswith('http') and not '/' in url:
            # Видаляємо @ якщо є
            username = url.replace('@', '').strip()
            # Перевіряємо що це валідний username (тільки букви, цифри, підкреслення)
            if username and username.replace('_', '').replace('-', '').isalnum():
                return username
            
        return None
    except Exception as e:
        logger.error(f"Помилка витягування Twitter username з '{url}': {e}")
        return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник команди /start"""
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    username = update.effective_user.username or "Unknown"
    
    # Перевіряємо чи користувач вже авторизований через нову систему
    if access_manager.is_authorized(user_id):
        # Оновлюємо активність сесії
        access_manager.update_session_activity(user_id)
        # Перевіряємо статус Twitter Monitor Adapter моніторингу
        twitter_adapter_status = "🚀 Активний" if twitter_monitor_adapter and twitter_monitor_adapter.monitoring_active else "⏸️ Неактивний"
        twitter_adapter_count = len(twitter_monitor_adapter.monitoring_accounts) if twitter_monitor_adapter else 0
        
        # Отримуємо роль користувача
        user_role = access_manager.get_user_role(user_id)
        role_emoji = "👑" if user_role == "admin" else "👤"
        role_text = "Адміністратор" if user_role == "admin" else "Користувач"
        
        welcome_text = format_success_message(
            f"Привіт, {username}!",
            f"{role_emoji} **Роль:** {role_text}\n"
            "✅ Ви авторизовані в системі.\n\n"
            f"🚀 **Twitter Monitor Adapter моніторинг:** {twitter_adapter_status}\n"
            f"📊 **Акаунтів для моніторингу:** {twitter_adapter_count}",
            "Використовуйте меню нижче для навігації по всіх функціях бота."
        )
        # Видаляємо команду /start для чистоти
        if update.message:
            asyncio.create_task(safe_delete_message(context, update.effective_chat.id, update.message.message_id))
        
        # Відправляємо головне меню та зберігаємо його ID
        menu_message = await update.message.reply_text(
            welcome_text,
            reply_markup=get_main_menu_keyboard(user_id),
        )
        main_menu_messages[user_id] = menu_message.message_id
    else:
        auth_text = format_info_message(
            f"Привіт, {username}!",
            "🔐 Для використання бота необхідна авторизація",
            "• Використовуйте команду /login для входу в систему\n"
            "• Якщо ви новий користувач, зверніться до адміністратора\n"
            "• Після авторизації ви отримаєте доступ до всіх функцій"
        )
        await update.message.reply_text(auth_text, )

async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда для авторизації користувача"""
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    username = update.effective_user.username or "Unknown"
    
    # Перевіряємо чи користувач вже авторизований
    if access_manager.is_authorized(user_id):
        # Оновлюємо активність сесії
        access_manager.update_session_activity(user_id)
        await update.message.reply_text(
            "✅ Ви вже авторизовані в системі!",
        )
        return
    
    # Перевіряємо чи користувач існує в системі
    user_data = access_manager.get_user_by_telegram_id(user_id)
    if not user_data:
        await update.message.reply_text(
            "❌ **Користувач не знайдений!**\n\n"
            "Ваш Telegram ID не зареєстрований в системі.\n"
            "Зверніться до адміністратора для реєстрації.",
        )
        return
    
    # Перевіряємо чи користувач активний
    if not user_data.get("is_active", True):
        await update.message.reply_text(
            "❌ **Доступ заблоковано!**\n\n"
            "Ваш акаунт деактивований.\n"
            "Зверніться до адміністратора.",
        )
        return
    
    # Запитуємо пароль
    await update.message.reply_text(
        "🔐 Введіть пароль для авторизації:\n\n"
        "Надішліть пароль повідомленням.",
    )
    
    # Видаляємо команду /login для чистоти
    if update.message:
        asyncio.create_task(safe_delete_message(context, update.effective_chat.id, update.message.message_id))
    
    # Встановлюємо стан очікування паролю
    waiting_for_password[user_id] = True

async def logout_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда для виходу з системи"""
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    
    if access_manager.is_authorized(user_id):
        access_manager.logout_user(user_id)
        await update.message.reply_text(
            "👋 **Ви успішно вийшли з системи!**\n\n"
            "Для повторного входу використовуйте команду /login",
        )
    else:
        await update.message.reply_text(
            "ℹ️ Ви не авторизовані в системі.",
        )

async def register_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда для реєстрації нового користувача (тільки для адміністратора)"""
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    
    # Перевіряємо чи користувач має права адміністратора
    if not access_manager.check_permission(user_id, "can_manage_users"):
        await update.message.reply_text(
            "❌ **Доступ заборонено!**\n\n"
            "Тільки адміністратор може реєструвати нових користувачів.",
        )
        return
    
    if not context.args:
        await update.message.reply_text(
            "📝 **Реєстрація нового користувача**\n\n"
            "Використання: /register <telegram_id> <username> [password]\n\n"
            "Приклад: /register 123456789 JohnDoe mypassword",
        )
        return
    
    try:
        target_telegram_id = int(context.args[0])
        username = context.args[1] if len(context.args) > 1 else ""
        password = context.args[2] if len(context.args) > 2 else None
        
        # Додаємо користувача
        new_user_id = access_manager.add_user(target_telegram_id, username or "Unknown", password or "")
        
        if new_user_id:
            await update.message.reply_text(
                f"✅ **Користувач успішно зареєстрований!**\n\n"
                f"• Telegram ID: {target_telegram_id}\n"
                f"• Username: {username}\n"
                f"• User ID: {new_user_id}\n"
                f"• Пароль: {password or 'за замовчуванням'}",
            )
        else:
            await update.message.reply_text(
                "❌ Помилка реєстрації користувача.",
            )
            
    except ValueError:
        await update.message.reply_text(
            "❌ **Неправильний формат!**\n\n"
            "Telegram ID повинен бути числом.\n"
            "Приклад: /register 123456789 JohnDoe",
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Помилка реєстрації: {str(e)}",
        )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else None
    message_text = update.message.text if update.message else None
    # Додаємо user_id у список пінгованих для Discord, якщо очікується введення
    if 'awaiting_ping_user_discord' in context.user_data:
        state = context.user_data['awaiting_ping_user_discord']
        project_id = state['project_id']
        if message_text.isdigit():
            new_uid = message_text.strip()
            project_manager.add_project_ping_user(user_id, project_id, new_uid)
            del context.user_data['awaiting_ping_user_discord']
            project = project_manager.get_project_by_id(user_id, project_id)
            ping_users = project_manager.get_project_ping_users(user_id, project_id)
            text = f"👤 <b>Кого пінгувати для Discord-проекту:</b> <b>{project['name']}</b>\n\n"
            if ping_users:
                text += "Поточний список user_id для пінгу:\n"
                for uid2 in ping_users:
                    text += f"• <code>{uid2}</code>\n"
            else:
                text += "Наразі список порожній.\n"
            text += "\nВи можете додати або видалити user_id для пінгу.\nВведіть user_id для додавання або натисніть кнопку для видалення."
            keyboard = []
            for uid2 in ping_users:
                keyboard.append([InlineKeyboardButton(f"❌ {uid2}", callback_data=f"remove_ping_discord_{project_id}_{uid2}")])
            keyboard.append([InlineKeyboardButton("➕ Додати user_id", callback_data=f"add_ping_discord_{project_id}")])
            keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"view_discord_{project_id}")])
            await update.message.reply_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
        elif message_text == "/cancel":
            del context.user_data['awaiting_ping_user_discord']
            await update.message.reply_text("Додавання user_id скасовано.")
        else:
            await update.message.reply_text("Введіть коректний user_id (число) або /cancel для скасування.")
        return
    # Додаємо user_id у список пінгованих, якщо очікується введення
    if 'awaiting_ping_user' in context.user_data:
        state = context.user_data['awaiting_ping_user']
        project_id = state['project_id']
        # Перевіряємо, що повідомлення містить лише число
        if message_text.isdigit():
            new_uid = message_text.strip()
            project_manager.add_project_ping_user(user_id, project_id, new_uid)
            del context.user_data['awaiting_ping_user']
            # Повертаємося до меню пінгів
            project = project_manager.get_project_by_id(user_id, project_id)
            ping_users = project_manager.get_project_ping_users(user_id, project_id)
            text = f"👤 <b>Кого пінгувати для проекту:</b> <b>{project['name']}</b>\n\n"
            if ping_users:
                text += "Поточний список user_id для пінгу:\n"
                for uid2 in ping_users:
                    text += f"• <code>{uid2}</code>\n"
            else:
                text += "Наразі список порожній.\n"
            text += "\nВи можете додати або видалити user_id для пінгу.\nВведіть user_id для додавання або натисніть кнопку для видалення."
            keyboard = []
            for uid2 in ping_users:
                keyboard.append([InlineKeyboardButton(f"❌ {uid2}", callback_data=f"remove_ping_{project_id}_{uid2}")])
            keyboard.append([InlineKeyboardButton("➕ Додати user_id", callback_data=f"add_ping_{project_id}")])
            keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"view_twitter_{project_id}")])
            await update.message.reply_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
        elif message_text == "/cancel":
            del context.user_data['awaiting_ping_user']
            await update.message.reply_text("Додавання user_id скасовано.")
        else:
            await update.message.reply_text("Введіть коректний user_id (число) або /cancel для скасування.")
        return
    """Обробник повідомлень"""
    # Перевіряємо чи це повідомлення від користувача (не від каналу)
    if not update.effective_user or not update.message:
        return
    
    # Перевіряємо чи це не канал
    if update.message.chat.type in ['channel', 'supergroup']:
        # Якщо це канал, перевіряємо чи бота пінгнули
        if update.message.text and '@' in update.message.text:
            # Шукаємо username бота в тексті
            bot_username = context.bot.username
            if bot_username and f'@{bot_username}' in update.message.text:
                # Бота пінгнули в каналі - автоматично встановлюємо канал для пересилання
                await handle_channel_ping(update, context)
        return
        
    user_id = update.effective_user.id
    message_text = update.message.text
    
    # Автонастройка каналу через переслане повідомлення з каналу/групи
    # Якщо адмін у стані налаштування каналу для іншого користувача — обробимо в спец. хендлері нижче
    try:
        fwd_chat = getattr(update.message, 'forward_from_chat', None)
        if fwd_chat and update.message.chat.type == 'private':
            if not (user_id in user_states and user_states[user_id]['state'] == 'admin_forward_set_channel'):
                await handle_forwarded_channel_setup(update, context, fwd_chat)
                return
    except Exception:
        pass
    
    # Якщо користувач очікує введення пароля для нової системи
    if user_id in waiting_for_password:
        # ВАЖЛИВО: Негайно видаляємо повідомлення з паролем для безпеки
        if update.message:
            asyncio.create_task(safe_delete_message(context, update.effective_chat.id, update.message.message_id))
        
        # Спробуємо авторизувати через нову систему
        if message_text and access_manager.authenticate_user(user_id, message_text):
            del waiting_for_password[user_id]
            # Оновлюємо активність сесії
            access_manager.update_session_activity(user_id)
            success_text = format_success_message(
                "Авторизація успішна!",
                "Ви успішно увійшли в систему та отримали доступ до всіх функцій бота.",
                "Оберіть дію з меню нижче для початку роботи."
            )
            # Відправляємо головне меню та зберігаємо його ID
            menu_message = await update.message.reply_text(
                success_text,
                reply_markup=get_main_menu_keyboard(user_id),
            )
            main_menu_messages[user_id] = menu_message.message_id
        else:
            error_text = format_error_message(
                "Неправильний пароль!",
                "Введений пароль не підходить для вашого акаунту.",
                "Спробуйте ще раз або зверніться до адміністратора для отримання правильного паролю."
            )
            await update.message.reply_text(error_text, )
        return
    
    # Перевіряємо авторизацію для інших повідомлень через нову систему
    if not access_manager.is_authorized(user_id):
        await update.message.reply_text(
            "🔐 **Доступ обмежено!**\n\n"
            "Для використання бота необхідна авторизація.\n"
            "Використовуйте команду /login для входу в систему.",
        )
        return
    
    # Оновлюємо активність користувача
    security_manager.update_user_activity(user_id)
    
    # Обробляємо стан додавання проекту
    if user_id in user_states:
        if user_states[user_id]['state'] == 'adding_project':
            await handle_project_creation(update, context)
        elif user_states[user_id]['state'] == 'setting_forward_channel':
            await handle_forward_channel_setting(update, context)
        elif user_states[user_id]['state'] == 'admin_forward_select_user':
            await handle_admin_forward_select_user(update, context)
        elif user_states[user_id]['state'] == 'admin_forward_set_channel':
            await handle_admin_forward_set_channel(update, context)
        elif user_states[user_id]['state'] == 'admin_creating_project_for_user':
            await handle_admin_create_project_for_user(update, context)
        elif user_states[user_id]['state'] == 'adding_twitter':
            await handle_twitter_addition(update, context)
        elif user_states[user_id]['state'] == 'adding_discord':
            await handle_discord_addition(update, context)
        elif user_states[user_id]['state'] == 'adding_twitter_adapter':
            await handle_twitter_adapter_addition(update, context)
        elif user_states[user_id]['state'] == 'admin_creating_user':
            await handle_admin_user_creation(update, context)
        elif user_states[user_id]['state'] == 'admin_creating_admin':
            await handle_admin_admin_creation(update, context)
        elif user_states[user_id]['state'] == 'admin_searching_user':
            await handle_admin_user_search(update, context)
        elif user_states[user_id]['state'] == 'admin_deleting_user':
            await handle_admin_user_deletion(update, context)
        elif user_states[user_id]['state'] == 'admin_changing_role':
            await handle_admin_role_change(update, context)
        elif user_states[user_id]['state'] == 'admin_resetting_password':
            await handle_admin_password_reset(update, context)
        elif user_states[user_id]['state'] == 'admin_resetting_system':
            await handle_admin_system_reset(update, context)
        return
    
    # Автоматично видаляємо всі текстові повідомлення користувача для чистоти чату
    if update.message and not message_text.startswith('/'):
        asyncio.create_task(safe_delete_message(context, update.effective_chat.id, update.message.message_id))
    
    # Обробляємо команди
    if message_text and message_text.startswith('/'):
        # Видаляємо команди також для чистоти
        if update.message:
            asyncio.create_task(safe_delete_message(context, update.effective_chat.id, update.message.message_id))
        await handle_command(update, context, message_text)
    else:
        # Для звичайних повідомлень показуємо підказку через головне меню
        if user_id in main_menu_messages:
            try:
                # Оновлюємо існуюче головне меню з підказкою
                hint_text = format_info_message(
                    "Використовуйте меню",
                    f"Ваше повідомлення: \"{message_text[:50]}{'...' if len(message_text) > 50 else ''}\"",
                    f"• Час до закінчення сесії: {security_manager.get_session_time_left(user_id)} секунд\n"
                    "• Для навігації використовуйте кнопки меню нижче\n"
                    "• Всі команди доступні через інтерфейс"
                )
                await context.bot.edit_message_text(
                    text=hint_text,
                    chat_id=update.effective_chat.id,
                    message_id=main_menu_messages[user_id],
                    reply_markup=get_main_menu_keyboard(user_id),
                )
            except Exception:
                # Якщо не вдалося редагувати, створюємо нове меню
                menu_message = await update.message.reply_text(
                    format_info_message(
                        "Використовуйте меню",
                        "Для навігації використовуйте кнопки меню нижче",
                        f"Час до закінчення сесії: {security_manager.get_session_time_left(user_id)} секунд"
                    ),
                    reply_markup=get_main_menu_keyboard(user_id),
                )
                main_menu_messages[user_id] = menu_message.message_id
        else:
            # Створюємо нове головне меню
            menu_message = await update.message.reply_text(
                format_info_message(
                    "Використовуйте меню",
                    "Для навігації використовуйте кнопки меню нижче",
                    f"Час до закінчення сесії: {security_manager.get_session_time_left(user_id)} секунд"
                ),
                reply_markup=get_main_menu_keyboard(user_id),
            )
            main_menu_messages[user_id] = menu_message.message_id

async def handle_command(update: Update, context: ContextTypes.DEFAULT_TYPE, command: str) -> None:
    """Обробник команд"""
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    
    if command == '/status':
        time_left = security_manager.get_session_time_left(user_id)
        await update.message.reply_text(
            f"Статус сесії:\n"
            f"Авторизований: {'Так' if security_manager.is_user_authorized(user_id) else 'Ні'}\n"
            f"Час до закінчення: {time_left} секунд"
        )
    elif command == '/logout':
        security_manager.deauthorize_user(user_id)
        await update.message.reply_text("Ви вийшли з системи.")
    elif command == '/help':
        await update.message.reply_text(
            "Доступні команди:\n"
            "/start - Почати роботу з ботом\n"
            "/status - Перевірити статус сесії\n"
            "/logout - Вийти з системи\n"
            "/help - Показати цю довідку"
        )
    else:
        await update.message.reply_text("Невідома команда. Використайте /help для довідки.")

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник callback запитів"""
    if not update.callback_query or not update.effective_user:
        return
    
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    
    # Перевіряємо авторизацію
    if not access_manager.is_authorized(user_id):
        await query.edit_message_text(
            "🔐 **Доступ обмежено!**\n\n"
            "Ваша сесія закінчилася. Для використання бота необхідна повторна авторизація.\n"
            "Використовуйте команду /login для входу в систему.",
        )
        return
    
    # Оновлюємо активність користувача
    access_manager.update_session_activity(user_id)
    
    # Додаємо/оновлюємо користувача в базі даних
    if not project_manager.get_user_data(user_id):
        project_manager.add_user(user_id, {
            'first_name': update.effective_user.first_name,
            'username': update.effective_user.username
        })
    else:
        project_manager.update_user_last_seen(user_id)
    
    callback_data = query.data
    
    # Перевіряємо, чи callback_data не є None
    if callback_data is None:
        await query.edit_message_text(
            "❌ Помилка: некоректні дані callback",
            reply_markup=get_main_menu_keyboard(user_id)
        )
        return
    
    if callback_data == "main_menu":
        # Оновлюємо ID головного меню
        main_menu_messages[user_id] = query.message.message_id
        
        welcome_text = format_success_message(
            "Головне меню",
            "Оберіть дію з меню нижче:",
            "Всі функції бота доступні через це меню."
        )
        await query.edit_message_text(
            welcome_text,
            reply_markup=get_main_menu_keyboard(user_id),
        )
    elif callback_data == "add_project":
        await query.edit_message_text(
            "➕ Додавання нового проекту\n\nОберіть платформу для моніторингу:",
            reply_markup=get_platform_keyboard()
        )
    elif callback_data == "my_projects":
        projects_text = format_info_message(
            "Мої проекти",
            "Управління вашими проектами моніторингу",
            "Тут ви можете переглядати, додавати та видаляти свої проекти для моніторингу Twitter та Discord."
        )
        await query.edit_message_text(
            projects_text,
            reply_markup=get_projects_menu_keyboard(user_id),
        )
    elif callback_data == "projects_menu":
        await query.edit_message_text(
            "📋 Управління проектами\n\nОберіть категорію:",
            reply_markup=get_projects_menu_keyboard(user_id)
        )
    elif callback_data == "twitter_projects":
        await query.edit_message_text(
            "🐦 Twitter проекти\n\nОберіть проект для управління:",
            reply_markup=get_twitter_projects_keyboard(user_id)
        )
    elif callback_data == "discord_projects":
        await query.edit_message_text(
            "💬 Discord проекти\n\nОберіть проект для управління:",
            reply_markup=get_discord_projects_keyboard(user_id)
        )
    elif callback_data == "twitter_adapter_accounts":
        await query.edit_message_text(
            "🚀 Twitter Monitor Adapter акаунти\n\nОберіть акаунт для управління:",
            reply_markup=get_twitter_adapter_accounts_keyboard()
        )
    elif callback_data == "add_twitter":
        user_states[user_id] = {
            'state': 'adding_twitter',
            'data': {}
        }
        await query.edit_message_text(
            "🐦 Додавання Twitter акаунта\n\nВведіть username акаунта (без @):"
        )
    elif callback_data == "add_discord":
        user_states[user_id] = {
            'state': 'adding_discord',
            'data': {}
        }
        await query.edit_message_text(
            "💬 Додавання Discord каналу\n\nВведіть ID каналу:"
        )
    elif callback_data == "add_twitter_adapter":
        user_states[user_id] = {
            'state': 'adding_twitter_adapter',
            'data': {}
        }
        await query.edit_message_text(
            "🚀 Додавання Twitter Monitor Adapter акаунта\n\nВведіть username акаунта (без @):"
        )
    elif callback_data == "platform_twitter":
        user_states[user_id] = {
            'state': 'adding_project',
            'data': {'platform': 'twitter'}
        }
        await query.edit_message_text(
            "🐦 Додавання проекту Twitter/X\n\nВведіть назву проекту:"
        )
    elif callback_data == "platform_discord":
        user_states[user_id] = {
            'state': 'adding_project',
            'data': {'platform': 'discord'}
        }
        await query.edit_message_text(
            "💬 Додавання проекту Discord\n\nВведіть назву проекту:"
        )
    elif callback_data == "help":
        await query.edit_message_text(
            "❓ **Система допомоги**\n\n"
            "Оберіть розділ для отримання детальної інформації:",
            reply_markup=get_help_keyboard()
        )
    elif callback_data == "twitter_adapter":
        # Перевіряємо статус Twitter Monitor Adapter моніторингу
        twitter_adapter_status = "🚀 Активний" if twitter_monitor_adapter and twitter_monitor_adapter.monitoring_active else "⏸️ Неактивний"
        twitter_adapter_count = len(twitter_monitor_adapter.monitoring_accounts) if twitter_monitor_adapter else 0
        
        twitter_adapter_text = (
            "🐦 **Twitter Monitor Adapter Моніторинг**\n\n"
            f"📊 **Статус:** {twitter_adapter_status}\n"
            f"👥 **Акаунтів:** {twitter_adapter_count}\n"
            f"🔄 **Автозапуск:** ✅ Увімкнено\n\n"
            "🔧 **Доступні команди:**\n"
            "• `/twitter_add username` - Додати акаунт\n"
            "• `/twitter_test username` - Тестувати моніторинг\n"
            "• `/twitter_start` - Запустити моніторинг\n"
            "• `/twitter_stop` - Зупинити моніторинг\n"
            "• `/twitter_remove username` - Видалити акаунт\n\n"
            "📝 **Приклад використання:**\n"
            "1. `/twitter_add pilk_xz` - додайте акаунт\n"
            "2. `/twitter_test pilk_xz` - протестуйте\n"
            "3. `/twitter_start` - запустіть моніторинг\n"
            "4. Моніторинг запуститься автоматично!\n\n"
            "💡 **Переваги Twitter Monitor Adapter:**\n"
            "• Швидкий API доступ\n"
            "• Надійний парсинг\n"
            "• Автоматичне оновлення\n"
            "• Обхід обмежень API\n"
            "• Автоматичний запуск з ботом"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")]]
        await query.edit_message_text(
            twitter_adapter_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    elif callback_data.startswith("delete_twitter_adapter_"):
        username = callback_data.replace("delete_twitter_adapter_", "")
        try:
            project_manager.remove_selenium_account(username)
            if twitter_monitor_adapter:
                twitter_monitor_adapter.remove_account(username)
            # Синхронізація після змін
            sync_monitors_with_projects()
            await query.edit_message_text(
                f"✅ Twitter Monitor Adapter акаунт @{username} успішно видалено!",
                reply_markup=get_twitter_adapter_accounts_keyboard()
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ Помилка видалення акаунта: {e}",
                reply_markup=get_twitter_adapter_accounts_keyboard()
            )
    elif callback_data.startswith("view_discord_"):
        project_id = int(callback_data.replace("view_discord_", ""))
        project = project_manager.get_project_by_id(user_id, project_id)
        if project:
            text = f"💬 <b>Discord проект: {project['name']}</b>\n\n"
            text += f"📝 <b>Опис:</b> {project.get('description', 'Немає опису')}\n"
            text += f"🔗 <b>URL:</b> {project.get('url', 'Немає URL')}\n"
            text += f"📅 <b>Створено:</b> {project.get('created_at', 'Невідомо')}\n"
            text += f"🔄 <b>Статус:</b> {'Активний' if project.get('is_active', True) else 'Неактивний'}"
            keyboard = [
                [InlineKeyboardButton("👤 Кого пінгувати", callback_data=f"ping_menu_discord_{project_id}")],
                [InlineKeyboardButton("❌ Видалити", callback_data=f"delete_discord_{project_id}")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="discord_projects")]
            ]
            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
    elif callback_data.startswith("ping_menu_discord_"):
        project_id = int(callback_data.replace("ping_menu_discord_", ""))
        project = project_manager.get_project_by_id(user_id, project_id)
        if project:
            ping_users = project_manager.get_project_ping_users(user_id, project_id)
            text = f"👤 <b>Кого пінгувати для Discord-проекту:</b> <b>{project['name']}</b>\n\n"
            if ping_users:
                text += "Поточний список user_id для пінгу:\n"
                for uid in ping_users:
                    text += f"• <code>{uid}</code>\n"
            else:
                text += "Наразі список порожній.\n"
            text += "\nВи можете додати або видалити user_id для пінгу.\nВведіть user_id для додавання або натисніть кнопку для видалення."
            keyboard = []
            for uid in ping_users:
                keyboard.append([InlineKeyboardButton(f"❌ {uid}", callback_data=f"remove_ping_discord_{project_id}_{uid}")])
            keyboard.append([InlineKeyboardButton("➕ Додати user_id", callback_data=f"add_ping_discord_{project_id}")])
            keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"view_discord_{project_id}")])
            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
    elif callback_data.startswith("remove_ping_discord_"):
        # remove_ping_discord_{project_id}_{uid}
        parts = callback_data.split("_")
        project_id = int(parts[3])
        uid = parts[4]
        project_manager.remove_project_ping_user(user_id, project_id, uid)
        project = project_manager.get_project_by_id(user_id, project_id)
        ping_users = project_manager.get_project_ping_users(user_id, project_id)
        text = f"👤 <b>Кого пінгувати для Discord-проекту:</b> <b>{project['name']}</b>\n\n"
        if ping_users:
            text += "Поточний список user_id для пінгу:\n"
            for uid2 in ping_users:
                text += f"• <code>{uid2}</code>\n"
        else:
            text += "Наразі список порожній.\n"
        text += "\nВи можете додати або видалити user_id для пінгу.\nВведіть user_id для додавання або натисніть кнопку для видалення."
        keyboard = []
        for uid2 in ping_users:
            keyboard.append([InlineKeyboardButton(f"❌ {uid2}", callback_data=f"remove_ping_discord_{project_id}_{uid2}")])
        keyboard.append([InlineKeyboardButton("➕ Додати user_id", callback_data=f"add_ping_discord_{project_id}")])
        keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"view_discord_{project_id}")])
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
    elif callback_data.startswith("add_ping_discord_"):
        project_id = int(callback_data.replace("add_ping_discord_", ""))
        await query.edit_message_text(
            f"Введіть user_id, якого потрібно додати до списку пінгованих для цього Discord-проекту.\n\nПісля введення, просто надішліть його у чат.\n\n<code>/cancel</code> — скасувати.",
            parse_mode="HTML"
        )
        context.user_data['awaiting_ping_user_discord'] = {'project_id': project_id}
    elif callback_data.startswith("add_ping_"):
        # Для Twitter-проектів (аналогічно Discord)
        project_id = int(callback_data.replace("add_ping_", ""))
        await query.edit_message_text(
            f"Введіть user_id, якого потрібно додати до списку пінгованих для цього Twitter-проекту.\n\nПісля введення, просто надішліть його у чат.\n\n<code>/cancel</code> — скасувати.",
            parse_mode="HTML"
        )
        context.user_data['awaiting_ping_user'] = {'project_id': project_id}
    elif callback_data.startswith("view_twitter_"):
        project_id = int(callback_data.replace("view_twitter_", ""))
        project = project_manager.get_project_by_id(user_id, project_id)
        if project:
            text = f"🐦 <b>Twitter проект: {project['name']}</b>\n\n"
            text += f"📝 <b>Опис:</b> {project.get('description', 'Немає опису')}\n"
            text += f"🔗 <b>URL:</b> {project.get('url', 'Немає URL')}\n"
            text += f"📅 <b>Створено:</b> {project.get('created_at', 'Невідомо')}\n"
            text += f"🔄 <b>Статус:</b> {'Активний' if project.get('is_active', True) else 'Неактивний'}"
            keyboard = [
                [InlineKeyboardButton("👤 Кого пінгувати", callback_data=f"ping_menu_{project_id}")],
                [InlineKeyboardButton("❌ Видалити", callback_data=f"delete_twitter_{project_id}")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="twitter_projects")]
            ]
            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
    elif callback_data.startswith("ping_menu_"):
        project_id = int(callback_data.replace("ping_menu_", ""))
        project = project_manager.get_project_by_id(user_id, project_id)
        if project:
            ping_users = project_manager.get_project_ping_users(user_id, project_id)
            text = f"👤 <b>Кого пінгувати для проекту:</b> <b>{project['name']}</b>\n\n"
            if ping_users:
                text += "Поточний список user_id для пінгу:\n"
                for uid in ping_users:
                    text += f"• <code>{uid}</code>\n"
            else:
                text += "Наразі список порожній.\n"
            text += "\nВи можете додати або видалити user_id для пінгу.\nВведіть user_id для додавання або натисніть кнопку для видалення."
            keyboard = []
            for uid in ping_users:
                keyboard.append([InlineKeyboardButton(f"❌ {uid}", callback_data=f"remove_ping_{project_id}_{uid}")])
            keyboard.append([InlineKeyboardButton("➕ Додати user_id", callback_data=f"add_ping_{project_id}")])
            keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"view_twitter_{project_id}")])
            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
    elif callback_data.startswith("view_discord_"):
        project_id = int(callback_data.replace("view_discord_", ""))
        project = project_manager.get_project_by_id(user_id, project_id)
        if project:
            text = f"💬 **Discord проект: {project['name']}**\n\n"
            text += f"📝 **Опис:** {project.get('description', 'Немає опису')}\n"
            text += f"🔗 **URL:** {project.get('url', 'Немає URL')}\n"
            text += f"📅 **Створено:** {project.get('created_at', 'Невідомо')}\n"
            text += f"🔄 **Статус:** {'Активний' if project.get('is_active', True) else 'Неактивний'}"
            
            keyboard = [
                [InlineKeyboardButton("❌ Видалити", callback_data=f"delete_discord_{project_id}")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="discord_projects")]
            ]
            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
    elif callback_data.startswith("view_twitter_adapter_"):
        username = callback_data.replace("view_twitter_adapter_", "")
        twitter_adapter_accounts = project_manager.get_selenium_accounts()
        if username in twitter_adapter_accounts:
            account_data = project_manager.data['selenium_accounts'][username]
            text = f"🚀 **Twitter Monitor Adapter: @{username}**\n\n"
            text += f"📅 **Додано:** {account_data.get('added_at', 'Невідомо')}\n"
            text += f"👤 **Додав:** {account_data.get('added_by', 'Невідомо')}\n"
            text += f"🔄 **Статус:** {'Активний' if account_data.get('is_active', True) else 'Неактивний'}\n"
            text += f"⏰ **Остання перевірка:** {account_data.get('last_checked', 'Ніколи')}"
            
            keyboard = [
                [InlineKeyboardButton("❌ Видалити", callback_data=f"delete_twitter_adapter_{username}")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="twitter_adapter_accounts")]
            ]
            await query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
    elif callback_data == "account_manager":
        # Показуємо менеджер акаунтів
        projects = project_manager.get_user_projects(user_id)
        
        if not projects:
            await query.edit_message_text(
                "🔧 **Менеджер акаунтів**\n\n❌ У вас немає проектів для моніторингу.\n\nДодайте проекти через меню бота.",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        # Групуємо по платформах
        twitter_projects = [p for p in projects if p['platform'] == 'twitter']
        discord_projects = [p for p in projects if p['platform'] == 'discord']
        
        # Форматуємо список
        text = "🔧 **Менеджер акаунтів**\n\n"
        
        if twitter_projects:
            text += "🐦 **Twitter/X акаунти:**\n"
            for i, project in enumerate(twitter_projects, 1):
                project_username: Optional[str] = extract_twitter_username(project['url'])
                if project_username:
                    text += f"{i}. @{project_username} ({project['name']})\n"
            text += "\n"
        
        if discord_projects:
            text += "💬 **Discord канали:**\n"
            for i, project in enumerate(discord_projects, 1):
                channel_id = extract_discord_channel_id(project['url'])
                text += f"{i}. Канал {channel_id} ({project['name']})\n"
            text += "\n"
        
        # Додаємо команди для видалення
        text += "🔧 **Команди для управління:**\n"
        text += "• /remove_twitter username - видалити Twitter акаунт\n"
        text += "• /remove_discord channel_id - видалити Discord канал\n"
        text += "• /accounts - показати список акаунтів"
        
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")]]
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    elif callback_data == "discord_history":
        # Перевіряємо чи є Discord проекти
        projects = project_manager.get_user_projects(user_id)
        discord_projects = [p for p in projects if p['platform'] == 'discord']
        
        if not discord_projects:
            await query.edit_message_text(
                "📜 Історія Discord\n\n❌ У вас немає Discord проектів для перегляду історії.\n\nДодайте Discord проект спочатку.",
                reply_markup=get_main_menu_keyboard(user_id)
            )
        else:
            await query.edit_message_text(
                "📜 Історія Discord\n\nОберіть канал для перегляду історії повідомлень:",
                reply_markup=get_discord_channels_keyboard(user_id)
            )
    elif callback_data.startswith("channel_"):
        # Зберігаємо вибраний канал для історії
        project_id = int(callback_data.split("_")[1])
        project = project_manager.get_project_by_id(user_id, project_id)
        
        if project:
            user_states[user_id] = {
                'state': 'viewing_history',
                'data': {'project': project}
            }
            await query.edit_message_text(
                f"📜 Історія каналу: {project['name']}\n\nОберіть кількість повідомлень для перегляду:",
                reply_markup=get_history_count_keyboard()
            )
    elif callback_data.startswith("history_"):
        # Отримуємо історію повідомлень
        count = int(callback_data.split("_")[1])
        await handle_discord_history(update, context, count)
    elif callback_data == "settings":
        await query.edit_message_text(
            "⚙️ **Налаштування**\n\n"
            "Налаштуйте бота під свої потреби:",
            reply_markup=get_settings_keyboard(user_id)
        )
    elif callback_data == "forward_settings":
        forward_status = project_manager.get_forward_status(user_id)
        
        if forward_status['enabled']:
            status_text = f"📢 Налаштування пересилання\n\n✅ Пересилання увімкнено\n📺 Канал: {forward_status['channel_id']}\n🕒 Налаштовано: {forward_status['created_at'][:19] if forward_status['created_at'] else 'Невідомо'}\n\n💡 Сповіщення відправляються тільки в налаштований канал, не в особисті повідомлення."
        else:
            status_text = "📢 Налаштування пересилання\n\n❌ Пересилання вимкнено\n\nНалаштуйте канал для автоматичного пересилання сповіщень з ваших проектів.\n\n💡 Сповіщення будуть відправлятися тільки в налаштований канал."
        
        await query.edit_message_text(
            status_text,
            reply_markup=get_forward_settings_keyboard(user_id)
        )
    elif callback_data == "enable_forward":
        if project_manager.enable_forward(user_id):
            await query.edit_message_text(
                "✅ Пересилання увімкнено!\n\nТепер потрібно встановити канал для пересилання.",
                reply_markup=get_forward_settings_keyboard(user_id)
            )
        else:
            await query.edit_message_text(
                "❌ Помилка увімкнення пересилання",
                reply_markup=get_forward_settings_keyboard(user_id)
            )
    elif callback_data == "disable_forward":
        if project_manager.disable_forward(user_id):
            await query.edit_message_text(
                "🔴 Пересилання вимкнено",
                reply_markup=get_forward_settings_keyboard(user_id)
            )
        else:
            await query.edit_message_text(
                "❌ Помилка вимкнення пересилання",
                reply_markup=get_forward_settings_keyboard(user_id)
            )
    elif callback_data in ["set_channel", "change_channel"]:
        user_states[user_id] = {
            'state': 'setting_forward_channel',
            'data': {}
        }
        await query.edit_message_text(
            "📝 Встановлення каналу для пересилання\n\n"
            "**Спосіб 1 - Автоматичне налаштування:**\n"
            "1. Додайте бота в канал як адміністратора\n"
            "2. Пінгніть бота в каналі: @parseryamatobot\n"
            "3. Бот автоматично налаштує канал\n\n"
            "**Спосіб 2 - Ручне налаштування:**\n"
            "Введіть ID каналу або username каналу:\n\n"
            "Приклади:\n"
            "• @channel_username\n"
            "• -1001234567890 (ID каналу)\n"
            "• channel_username (без @)\n\n"
            "💡 Рекомендуємо використовувати автоматичне налаштування!"
        )
    elif callback_data == "auto_setup":
        bot_username = context.bot.username
        await query.edit_message_text(
            f"🤖 **Автоматичне налаштування каналу**\n\n"
            f"Для автоматичного налаштування каналу:\n\n"
            f"1️⃣ **Додайте бота в канал**\n"
            f"   • Додайте @{bot_username} в канал як адміністратора\n"
            f"   • Надайте права на відправку повідомлень\n\n"
            f"2️⃣ **Пінгніть бота в каналі**\n"
            f"   • Напишіть в каналі: @{bot_username}\n"
            f"   • Бот автоматично налаштує канал\n\n"
            f"3️⃣ **Готово!**\n"
            f"   • Канал буде налаштовано для пересилання\n"
            f"   • Ви отримаєте підтвердження\n\n"
            f"💡 **Переваги:**\n"
            f"• Не потрібно знати ID каналу\n"
            f"• Автоматичне налаштування\n"
            f"• Миттєве підтвердження",
            reply_markup=get_forward_settings_keyboard(user_id)
        )
    elif callback_data == "forward_status":
        forward_status = project_manager.get_forward_status(user_id)
        user_projects = project_manager.get_user_projects(user_id)
        discord_projects = [p for p in user_projects if p['platform'] == 'discord']
        twitter_projects = [p for p in user_projects if p['platform'] == 'twitter']
        use_threads = forward_status.get('use_threads', True)
        
        status_text = (
            f"📊 Статус пересилання\n\n"
            f"🔄 Статус: {'✅ Увімкнено' if forward_status['enabled'] else '❌ Вимкнено'}\n"
            f"📺 Канал: {forward_status['channel_id'] or 'Не встановлено'}\n"
            f"🧵 Режим: {'Гілки (threads)' if use_threads else 'Теги в каналі'}\n"
            f"📋 Проектів: Discord {len(discord_projects)}, Twitter {len(twitter_projects)}\n"
            f"🕒 Налаштовано: {forward_status['created_at'][:19] if forward_status['created_at'] else 'Невідомо'}\n\n"
        )
        
        if use_threads:
            status_text += "🧵 **Режим гілок**: Кожен проект має свою окрему гілку в групі\n"
            project_threads = forward_status.get('project_threads', {})
            if project_threads:
                status_text += f"🔧 Активних гілок: {len(project_threads)}\n"
        else:
            status_text += "🏷️ **Режим тегів**: Всі повідомлення в одному каналі з тегами проектів\n"
        
        status_text += "\n💡 Сповіщення відправляються тільки в налаштований канал.\n\n"
        
        if forward_status['enabled'] and (discord_projects or twitter_projects):
            status_text += "📢 Сповіщення будуть пересилатися з проектів:\n"
            for project in discord_projects + twitter_projects:
                platform_emoji = "💬" if project['platform'] == 'discord' else "🐦"
                project_tag = project.get('tag', f"#{project['platform']}_project_{project['id']}")
                status_text += f"• {platform_emoji} {project['name']} ({project_tag})\n"
        elif not discord_projects and not twitter_projects:
            status_text += "⚠️ У вас немає проектів для моніторингу."
        
        await query.edit_message_text(
            status_text,
            reply_markup=get_forward_settings_keyboard(user_id)
        )
    elif callback_data == "enable_threads":
        # Увімкнути використання thread'ів
        forward_status = project_manager.get_forward_status(user_id)
        if forward_status['enabled']:
            project_manager.data['settings']['forward_settings'][str(user_id)]['use_threads'] = True
            project_manager.save_data()
            await query.edit_message_text(
                "🧵 **Режим гілок увімкнено!**\n\n"
                "Тепер кожен проект буде мати свою окрему гілку в групі.\n"
                "Це дозволяє краще організувати повідомлення та легше їх знаходити.\n\n"
                "💡 **Переваги гілок:**\n"
                "• Кожен проект в окремій гілці\n"
                "• Зручна навігація\n"
                "• Автоматичне створення гілок\n"
                "• Теги в назві гілки",
                reply_markup=get_forward_settings_keyboard(user_id)
            )
        else:
            await query.edit_message_text(
                "❌ Спочатку увімкніть пересилання та встановіть канал.",
                reply_markup=get_forward_settings_keyboard(user_id)
            )
    elif callback_data == "disable_threads":
        # Вимкнути використання thread'ів (використовувати теги)
        forward_status = project_manager.get_forward_status(user_id)
        if forward_status['enabled']:
            project_manager.data['settings']['forward_settings'][str(user_id)]['use_threads'] = False
            project_manager.save_data()
            await query.edit_message_text(
                "🏷️ **Режим тегів увімкнено!**\n\n"
                "Тепер всі повідомлення будуть відправлятися в основний канал з тегами проектів.\n"
                "Кожне повідомлення буде містити тег проекту на початку.\n\n"
                "💡 **Переваги тегів:**\n"
                "• Всі повідомлення в одному каналі\n"
                "• Легкий пошук за тегами\n"
                "• Простий інтерфейс\n"
                "• Сумісність зі старими каналами",
                reply_markup=get_forward_settings_keyboard(user_id)
            )
        else:
            await query.edit_message_text(
                "❌ Спочатку увімкніть пересилання та встановіть канал.",
                reply_markup=get_forward_settings_keyboard(user_id)
            )
    elif callback_data == "test_threads":
        # Тестування thread'ів
        forward_status = project_manager.get_forward_status(user_id)
        if not forward_status['enabled']:
            await query.edit_message_text(
                "❌ Пересилання не налаштовано",
                reply_markup=get_forward_settings_keyboard(user_id)
            )
            return
        
        user_projects = project_manager.get_user_projects(user_id)
        if not user_projects:
            await query.edit_message_text(
                "❌ У вас немає проектів для тестування",
                reply_markup=get_forward_settings_keyboard(user_id)
            )
            return
        
        forward_channel = forward_status['channel_id']
        test_results = []
        
        for project in user_projects[:3]:  # Тестуємо перші 3 проекти
            project_id = project.get('id')
            project_name = project.get('name', 'Test Project')
            project_tag = project.get('tag', f"#test_{project_id}")
            
            try:
                # Спробуємо створити або знайти thread
                thread_id = project_manager.get_project_thread(user_id, project_id)
                
                if not thread_id:
                    thread_id = create_project_thread_sync(BOT_TOKEN, forward_channel, project_name, project_tag, str(user_id))
                    
                    if thread_id:
                        project_manager.set_project_thread(user_id, project_id, thread_id)
                
                if thread_id:
                    # Відправляємо тестове повідомлення в thread
                    test_text = (
                        f"🧪 **Тестове повідомлення**\n\n"
                        f"• Проект: {project_name}\n"
                        f"• Тег: {project_tag}\n"
                        f"• Thread ID: {thread_id}\n"
                        f"• Час: {datetime.now().strftime('%H:%M:%S')}\n\n"
                        f"✅ Якщо ви бачите це повідомлення, гілка працює правильно!"
                    )
                    
                    success = send_message_to_thread_sync(BOT_TOKEN, forward_channel, thread_id, test_text, project_tag)
                    
                    if success:
                        test_results.append(f"✅ {project_name} (thread {thread_id})")
                    else:
                        test_results.append(f"❌ {project_name} - помилка відправки")
                else:
                    test_results.append(f"❌ {project_name} - не вдалося створити thread")
                    
            except Exception as e:
                test_results.append(f"❌ {project_name} - помилка: {str(e)[:50]}")
        
        result_text = (
            f"🧪 **Результати тестування гілок**\n\n"
            f"📊 Протестовано проектів: {len(test_results)}\n\n"
            + "\n".join(test_results) +
            f"\n\n💡 Перевірте канал {forward_channel} для тестових повідомлень."
        )
        
        await query.edit_message_text(
            result_text,
            reply_markup=get_forward_settings_keyboard(user_id)
        )
    elif callback_data == "manage_threads":
        # Управління thread'ами
        forward_status = project_manager.get_forward_status(user_id)
        user_projects = project_manager.get_user_projects(user_id)
        project_threads = forward_status.get('project_threads', {})
        
        threads_info = []
        for project in user_projects:
            project_id = str(project.get('id'))
            project_name = project.get('name', 'Unknown')
            project_tag = project.get('tag', f"#project_{project_id}")
            thread_id = project_threads.get(project_id)
            
            if thread_id:
                threads_info.append(f"🧵 {project_name} ({project_tag}) - Thread {thread_id}")
            else:
                threads_info.append(f"❌ {project_name} ({project_tag}) - Немає thread'а")
        
        if threads_info:
            threads_text = "\n".join(threads_info[:10])  # Показуємо перші 10
            if len(threads_info) > 10:
                threads_text += f"\n... та ще {len(threads_info) - 10} проектів"
        else:
            threads_text = "❌ Немає активних гілок"
        
        manage_text = (
            f"🔧 **Управління гілками**\n\n"
            f"📊 Всього проектів: {len(user_projects)}\n"
            f"🧵 Активних гілок: {len(project_threads)}\n\n"
            f"**Список гілок:**\n{threads_text}\n\n"
            f"💡 Гілки створюються автоматично при появі нових повідомлень."
        )
        
        keyboard = [
            [InlineKeyboardButton("🔄 Пересоздать все гілки", callback_data="recreate_all_threads")],
            [InlineKeyboardButton("🧹 Очистити неактивні", callback_data="cleanup_threads")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="forward_settings")]
        ]
        
        await query.edit_message_text(
            manage_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif callback_data == "recreate_all_threads":
        # Пересоздать все thread'и
        forward_status = project_manager.get_forward_status(user_id)
        if not forward_status['enabled']:
            await query.edit_message_text(
                "❌ Пересилання не налаштовано",
                reply_markup=get_forward_settings_keyboard(user_id)
            )
            return
        
        user_projects = project_manager.get_user_projects(user_id)
        forward_channel = forward_status['channel_id']
        
        # Очищаємо старі thread'и
        project_manager.data['settings']['forward_settings'][str(user_id)]['project_threads'] = {}
        
        created_threads = []
        errors = []
        
        for project in user_projects:
            project_id = project.get('id')
            project_name = project.get('name', 'Project')
            project_tag = project.get('tag', f"#project_{project_id}")
            
            try:
                thread_id = create_project_thread_sync(BOT_TOKEN, forward_channel, project_name, project_tag, str(user_id))
                
                if thread_id:
                    project_manager.set_project_thread(user_id, project_id, thread_id)
                    created_threads.append(f"✅ {project_name} - Thread {thread_id}")
                else:
                    errors.append(f"❌ {project_name} - не вдалося створити")
                    
            except Exception as e:
                errors.append(f"❌ {project_name} - помилка: {str(e)[:30]}")
        
        result_text = (
            f"🔄 **Результати пересоздання гілок**\n\n"
            f"✅ Створено: {len(created_threads)}\n"
            f"❌ Помилок: {len(errors)}\n\n"
        )
        
        if created_threads:
            result_text += "**Створені гілки:**\n" + "\n".join(created_threads[:5])
            if len(created_threads) > 5:
                result_text += f"\n... та ще {len(created_threads) - 5}"
        
        if errors:
            result_text += "\n\n**Помилки:**\n" + "\n".join(errors[:3])
            
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="manage_threads")]]
        await query.edit_message_text(
            result_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif callback_data == "cleanup_threads":
        # Очистити неактивні thread'и
        forward_status = project_manager.get_forward_status(user_id)
        user_projects = project_manager.get_user_projects(user_id)
        project_threads = forward_status.get('project_threads', {}).copy()
        
        # Визначаємо які thread'и треба залишити
        active_project_ids = {str(p.get('id')) for p in user_projects}
        threads_to_remove = []
        
        for project_id, thread_id in project_threads.items():
            if project_id not in active_project_ids:
                threads_to_remove.append(project_id)
                project_manager.remove_project_thread(user_id, int(project_id))
        
        cleanup_text = (
            f"🧹 **Очищення гілок завершено**\n\n"
            f"📊 Всього було thread'ів: {len(project_threads)}\n"
            f"🗑️ Видалено неактивних: {len(threads_to_remove)}\n"
            f"✅ Залишилося активних: {len(project_threads) - len(threads_to_remove)}\n\n"
            f"💡 Видалені thread'и належали проектам, які більше не існують."
        )
        
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="manage_threads")]]
        await query.edit_message_text(
            cleanup_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    elif callback_data == "diagnostics":
        diagnostics_text = (
            "🔧 **Діагностика системи**\n\n"
            "Оберіть тип перевірки:\n\n"
            "🔍 **Перевірити бота** - статус бота та підключення\n"
            "📺 **Тест каналів** - перевірка доступу до каналів\n"
            "🔗 **Discord API** - тест підключення до Discord\n"
            "📊 **Статистика** - детальна статистика системи\n"
            "🔄 **Перезавантажити** - оновити дані"
        )
        await query.edit_message_text(
            diagnostics_text,
            reply_markup=get_diagnostics_keyboard()
        )
    elif callback_data == "check_bot_status":
        try:
            # Перевіряємо статус бота
            bot_info = await context.bot.get_me()
            bot_status = "✅ Активний"
            
            # Перевіряємо кількість авторизованих користувачів
            auth_users = len(security_manager.authorized_users)
            
            # Перевіряємо Discord моніторинг
            discord_status = "✅ Активний" if discord_monitor else "❌ Вимкнено"
            
            status_text = (
                f"🔍 **Статус бота**\n\n"
                f"🤖 Бот: {bot_status}\n"
                f"📛 Ім'я: {bot_info.first_name}\n"
                f"🆔 ID: {bot_info.id}\n"
                f"👤 Username: @{bot_info.username}\n\n"
                f"👥 Авторизованих користувачів: {auth_users}\n"
                f"🔗 Discord моніторинг: {discord_status}\n"
                f"📊 Проектів: {len(project_manager.get_user_projects(user_id))}\n"
                f"🕒 Час: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            
            await query.edit_message_text(
                status_text,
                reply_markup=get_diagnostics_keyboard()
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ **Помилка перевірки бота**\n\n{str(e)}",
                reply_markup=get_diagnostics_keyboard()
            )
    elif callback_data == "test_channels":
        try:
            forward_channel = project_manager.get_forward_channel(user_id)
            
            if forward_channel:
                # Спробуємо відправити тестове повідомлення
                test_message = (
                    f"🧪 **Тестове повідомлення**\n\n"
                    f"📺 Канал: {forward_channel}\n"
                    f"👤 Від: {update.effective_user.first_name}\n"
                    f"🕒 Час: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    f"✅ Якщо ви бачите це повідомлення, канал працює!"
                )
                
                await context.bot.send_message(
                    chat_id=forward_channel,
                    text=test_message,
                )
                
                result_text = f"✅ **Тест каналу пройшов успішно!**\n\n📺 Канал: `{forward_channel}`\n📤 Тестове повідомлення відправлено"
            else:
                result_text = "❌ **Канал не налаштовано**\n\nСпочатку встановіть канал для пересилання в налаштуваннях."
            
            await query.edit_message_text(
                result_text,
                reply_markup=get_diagnostics_keyboard()
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ **Помилка тесту каналу**\n\n{str(e)}\n\n💡 Перевірте:\n• Чи додано бота в канал\n• Чи є у бота права адміністратора\n• Чи правильний ID каналу",
                reply_markup=get_diagnostics_keyboard()
            )
    elif callback_data == "test_discord_api":
        try:
            if not DISCORD_AUTHORIZATION:
                await query.edit_message_text(
                    "❌ **Discord API не налаштовано**\n\nВстановіть AUTHORIZATION токен в .env файлі",
                    reply_markup=get_diagnostics_keyboard()
                )
                return
            
            # Тестуємо Discord API
            import aiohttp
            headers = {
                'Authorization': DISCORD_AUTHORIZATION,
                'User-Agent': 'DiscordBot (https://github.com/discord/discord-api-docs, 1.0)'
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get('https://discord.com/api/v10/users/@me', headers=headers) as response:
                    if response.status == 200:
                        user_data = await response.json()
                        result_text = (
                            f"✅ **Discord API працює**\n\n"
                            f"👤 Користувач: {user_data.get('username', 'Невідомо')}\n"
                            f"🆔 ID: {user_data.get('id', 'Невідомо')}\n"
                            f"📧 Email: {user_data.get('email', 'Приховано')}\n"
                            f"🔐 Верифікований: {'✅' if user_data.get('verified', False) else '❌'}\n"
                            f"📊 Статус: {response.status}"
                        )
                    else:
                        result_text = f"❌ **Discord API помилка**\n\nСтатус: {response.status}\nВідповідь: {await response.text()}"
            
            await query.edit_message_text(
                result_text,
                reply_markup=get_diagnostics_keyboard()
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ **Помилка Discord API**\n\n{str(e)}",
                reply_markup=get_diagnostics_keyboard()
            )
    elif callback_data == "show_stats":
        try:
            stats = project_manager.get_statistics()
            user_projects = project_manager.get_user_projects(user_id)
            discord_projects = [p for p in user_projects if p['platform'] == 'discord']
            forward_status = project_manager.get_forward_status(user_id)
            
            # Підраховуємо відстежені повідомлення
            sent_messages = project_manager.data['settings'].get('sent_messages', {})
            total_tracked = sum(
                len(channel_messages) 
                for user_messages in sent_messages.values() 
                for channel_messages in user_messages.values()
            )
            
            stats_text = (
                f"📊 **Статистика системи**\n\n"
                f"👥 Всього користувачів: {stats['total_users']}\n"
                f"📋 Всього проектів: {stats['total_projects']}\n"
                f"🔗 Discord проектів: {len(discord_projects)}\n"
                f"🐦 Twitter проектів: {len([p for p in user_projects if p['platform'] == 'twitter'])}\n\n"
                f"📢 **Пересилання:**\n"
                f"🔄 Статус: {'✅ Увімкнено' if forward_status['enabled'] else '❌ Вимкнено'}\n"
                f"📺 Канал: {forward_status['channel_id'] or 'Не встановлено'}\n\n"
                f"💾 **Дані:**\n"
                f"📁 Розмір файлу: {stats.get('data_size', 'Невідомо')}\n"
                f"📨 Відстежених повідомлень: {total_tracked}\n"
                f"🕒 Останнє оновлення: {stats.get('last_update', 'Невідомо')}"
            )
            
            await query.edit_message_text(
                stats_text,
                reply_markup=get_diagnostics_keyboard()
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ **Помилка отримання статистики**\n\n{str(e)}",
                reply_markup=get_diagnostics_keyboard()
            )
    elif callback_data == "reload_data":
        try:
            # Перезавантажуємо дані
            project_manager.load_data()
            # Проводимо синхронізацію моніторів
            sync_monitors_with_projects()
            
            # Перезапускаємо Discord моніторинг
            if discord_monitor:
                discord_monitor.monitoring_channels.clear()
                for user_id_str, projects in project_manager.data['projects'].items():
                    for project in projects:
                        if project['platform'] == 'discord':
                            channel_id = project['link'].split('/')[-1]
                            discord_monitor.add_channel(channel_id)
            
            await query.edit_message_text(
                "🔄 **Дані перезавантажено**\n\n✅ Проекти оновлено\n✅ Discord канали оновлено\n✅ Налаштування оновлено",
                reply_markup=get_diagnostics_keyboard()
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ **Помилка перезавантаження**\n\n{str(e)}",
                reply_markup=get_diagnostics_keyboard()
            )
    # Видалення проектів з меню: Twitter
    elif callback_data.startswith("delete_twitter_"):
        project_id = int(callback_data.split('_')[-1])
        project = project_manager.get_project_by_id(user_id, project_id)
        if not project:
            await query.edit_message_text("❌ Проект не знайдено.", reply_markup=get_twitter_projects_keyboard(user_id))
            return
        removed_username: Optional[str] = extract_twitter_username(project.get('url', ''))
        if project_manager.remove_project(user_id, project_id):
            # Синхронізуємо монітори після видалення
            sync_monitors_with_projects()
            
            # Зупиняємо моніторинг цього акаунта відразу
            try:
                if twitter_monitor and removed_username:
                    twitter_monitor.remove_account(removed_username)
            except Exception:
                pass
            if twitter_monitor_adapter and removed_username and removed_username in getattr(twitter_monitor_adapter, 'monitoring_accounts', set()):
                twitter_monitor_adapter.monitoring_accounts.discard(removed_username)
                if removed_username in twitter_monitor_adapter.seen_tweets:
                    del twitter_monitor_adapter.seen_tweets[removed_username]
            # Також приберемо із збережених Selenium акаунтів, якщо це був він
            try:
                if removed_username:
                    project_manager.remove_selenium_account(removed_username)
            except Exception:
                pass
            # Синхронізація після змін
            sync_monitors_with_projects()
            await query.edit_message_text(
                f"✅ Twitter акаунт @{removed_username or 'Unknown'} видалено та зупинено моніторинг.",
                reply_markup=get_twitter_projects_keyboard(user_id)
            )
        else:
            await query.edit_message_text(
                "❌ Помилка видалення Twitter проекту.",
                reply_markup=get_twitter_projects_keyboard(user_id)
            )
    # Видалення проектів з меню: Discord
    elif callback_data.startswith("delete_discord_"):
        project_id = int(callback_data.split('_')[-1])
        project = project_manager.get_project_by_id(user_id, project_id)
        if not project:
            await query.edit_message_text("❌ Проект не знайдено.", reply_markup=get_discord_projects_keyboard(user_id))
            return
        channel_id = extract_discord_channel_id(project.get('url', ''))
        if project_manager.remove_project(user_id, project_id):
            # Синхронізуємо монітори після видалення
            sync_monitors_with_projects()
            
            # Зупиняємо моніторинг цього каналу відразу
            if discord_monitor and channel_id in getattr(discord_monitor, 'monitoring_channels', set()):
                discord_monitor.monitoring_channels.discard(channel_id)
                if channel_id in discord_monitor.last_message_ids:
                    del discord_monitor.last_message_ids[channel_id]
            # Синхронізація після змін
            sync_monitors_with_projects()
            await query.edit_message_text(
                f"✅ Discord канал {channel_id} видалено та зупинено моніторинг.",
                reply_markup=get_discord_projects_keyboard(user_id)
            )
        else:
            await query.edit_message_text(
                "❌ Помилка видалення Discord проекту.",
                reply_markup=get_discord_projects_keyboard(user_id)
            )
    
    # Обробники адміністративної панелі
    elif callback_data == "admin_panel":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ **Доступ заборонено!**\n\nТільки адміністратор має доступ до цієї панелі.",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        await query.edit_message_text(
            "👑 **Адміністративна панель**\n\n"
            "🎯 **Добро пожаловать в центр управления!**\n\n"
            "📊 **Доступные разделы:**\n"
            "• 👥 Управление пользователями\n"
            "• 📊 Статистика и аналитика\n"
            "• 🔧 Системное управление\n"
            "• 📋 Просмотр всех проектов\n"
            "• 🔍 Поиск и фильтры\n"
            "• 📈 Мониторинг системы\n"
            "• ⚙️ Настройки\n\n"
            "Выберите нужный раздел для работы:",
            reply_markup=get_admin_panel_keyboard(),
        )
    elif callback_data == "admin_users":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
            
        await query.edit_message_text(
            "👥 **Управління користувачами**\n\n"
            "🎯 **Центр управления пользователями**\n\n"
            "📋 **Доступные действия:**\n"
            "• 👥 Просмотр списка пользователей\n"
            "• 📊 Статистика пользователей\n"
            "• ➕ Добавление новых пользователей\n"
            "• 👑 Создание администраторов\n"
            "• 🔍 Поиск пользователей\n"
            "• 🔄 Изменение ролей\n"
            "• 🔐 Сброс паролей\n"
            "• 🔁 Настройка пересылки\n"
            "• 🗑️ Удаление пользователей\n"
            "• 📈 Активность пользователей\n\n"
            "Выберите нужное действие:",
            reply_markup=get_admin_users_keyboard(),
        )
    elif callback_data == "admin_create_for_user":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text("❌ Доступ заборонено!", reply_markup=get_main_menu_keyboard(user_id))
            return
        # Перший крок: ввести Telegram ID цільового користувача
        user_states[user_id] = {
            'state': 'admin_creating_project_for_user',
            'data': {'step': 'telegram_id'}
        }
        await query.edit_message_text(
            "➕ **Створення проекту для користувача**\n\nВведіть Telegram ID користувача:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="admin_panel")]])
        )
    elif callback_data == "admin_forward":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        # Запитуємо target user id
        user_states[user_id] = {
            'state': 'admin_forward_select_user',
            'data': {}
        }
        await query.edit_message_text(
            "🔁 **Керування пересиланням (користувач)**\n\nВведіть Telegram ID користувача:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="admin_users")]])
        )
    elif callback_data == "admin_stats":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
            
        try:
            stats = project_manager.get_project_statistics(user_id)
            users_list = project_manager.get_all_users_with_projects(user_id)
            
            stats_text = (
                f"📊 **Статистика системи**\n\n"
                f"👥 Всього користувачів: {stats['total_users']}\n"
                f"✅ Активних користувачів: {stats['active_users']}\n"
                f"📋 Всього проектів: {stats['total_projects']}\n"
                f"🐦 Twitter проектів: {stats['twitter_projects']}\n"
                f"💬 Discord проектів: {stats['discord_projects']}\n"
                f"🚀 Twitter Monitor Adapter акаунтів: {stats['selenium_accounts']}\n\n"
                f"👑 **Адміністраторів:** {len(access_manager.get_all_admins())}\n"
                f"👤 **Звичайних користувачів:** {len(access_manager.get_all_users_by_role('user'))}"
            )
            
            await query.edit_message_text(
                stats_text,
                reply_markup=get_admin_panel_keyboard()
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ **Помилка отримання статистики**\n\n{str(e)}",
                reply_markup=get_admin_panel_keyboard()
            )
    elif callback_data == "admin_list_users":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
            
        try:
            all_users = access_manager.get_all_users()
            
            if not all_users:
                await query.edit_message_text(
                    "👥 **Список користувачів**\n\n"
                    "Користувачів не знайдено.",
                    reply_markup=get_admin_users_keyboard()
                )
                return
            
            users_text = "👥 **Список користувачів**\n\n"
            
            for i, user in enumerate(all_users[:10], 1):  # Показуємо перших 10
                role_emoji = "👑" if user.get('role', 'user') == 'admin' else "👤"
                status_emoji = "✅" if user.get('is_active', True) else "❌"
                
                users_text += (
                    f"{i}. {role_emoji} {user.get('username', 'Без імені')}\n"
                    f"   ID: {user.get('telegram_id')}\n"
                    f"   Статус: {status_emoji}\n"
                    f"   Створено: {user.get('created_at', '')[:10]}\n\n"
                )
            
            if len(all_users) > 10:
                users_text += f"... та ще {len(all_users) - 10} користувачів"
            
            await query.edit_message_text(
                users_text,
                reply_markup=get_admin_users_keyboard()
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ **Помилка отримання списку користувачів**\n\n{str(e)}",
                reply_markup=get_admin_users_keyboard()
            )
    elif callback_data == "admin_all_projects":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
            
        try:
            all_projects = project_manager.get_all_projects(user_id)
            total_projects = sum(len(projects) for projects in all_projects.values())
            
            if total_projects == 0:
                await query.edit_message_text(
                    "📋 **Всі проекти**\n\n"
                    "Проектів не знайдено.",
                    reply_markup=get_admin_panel_keyboard()
                )
                return
            
            projects_text = f"📋 **Всі проекти** (Всього: {total_projects})\n\n"
            
            shown_projects = 0
            for user_id_str, projects in all_projects.items():
                if shown_projects >= 5:  # Показуємо тільки перші 5 користувачів
                    break
                    
                user_data = access_manager.get_user_by_telegram_id(int(user_id_str))
                username = user_data.get('username', 'Без імені') if user_data else 'Невідомий'
                
                projects_text += f"👤 **{username}** ({len(projects)} проектів):\n"
                
                for project in projects[:3]:  # По 3 проекти на користувача
                    platform_emoji = "🐦" if project.get('platform') == 'twitter' else "💬"
                    projects_text += f"   {platform_emoji} {project.get('name', 'Без назви')}\n"
                
                if len(projects) > 3:
                    projects_text += f"   ... та ще {len(projects) - 3} проектів\n"
                
                projects_text += "\n"
                shown_projects += 1
            
            if len(all_projects) > 5:
                projects_text += f"... та ще {len(all_projects) - 5} користувачів з проектами"
            
            await query.edit_message_text(
                projects_text,
                reply_markup=get_admin_panel_keyboard()
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ **Помилка отримання проектів**\n\n{str(e)}",
                reply_markup=get_admin_panel_keyboard()
            )
    elif callback_data == "admin_add_user":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        # Встановлюємо стан для створення користувача
        user_states[user_id] = {
            'state': 'admin_creating_user',
            'data': {'step': 'telegram_id'}
        }
        
        await query.edit_message_text(
            "👤 **Створення нового користувача**\n\n"
            "Введіть Telegram ID користувача:\n\n"
            "💡 **Приклад:** 123456789",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Скасувати", callback_data="admin_users")
            ]])
        )
    elif callback_data == "admin_add_admin":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        # Встановлюємо стан для створення адміністратора
        user_states[user_id] = {
            'state': 'admin_creating_admin',
            'data': {'step': 'telegram_id'}
        }
        
        await query.edit_message_text(
            "👑 **Створення нового адміністратора**\n\n"
            "Введіть Telegram ID адміністратора:\n\n"
            "💡 **Приклад:** 123456789",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Скасувати", callback_data="admin_users")
            ]])
        )
    elif callback_data == "admin_search_user":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        # Встановлюємо стан для пошуку
        user_states[user_id] = {
            'state': 'admin_searching_user',
            'data': {}
        }
        
        await query.edit_message_text(
            "🔍 **Пошук користувача**\n\n"
            "Введіть username або Telegram ID для пошуку:\n\n"
            "💡 **Приклади:**\n"
            "• JohnDoe (пошук за username)\n"
            "• 123456789 (пошук за Telegram ID)",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Скасувати", callback_data="admin_users")
            ]])
        )
    elif callback_data == "admin_delete_user":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        # Встановлюємо стан для видалення
        user_states[user_id] = {
            'state': 'admin_deleting_user',
            'data': {}
        }
        
        await query.edit_message_text(
            "🗑️ **Видалення користувача**\n\n"
            "⚠️ **УВАГА!** Ця дія видалить користувача повністю!\n\n"
            "Введіть Telegram ID користувача для видалення:\n\n"
            "💡 **Приклад:** 123456789",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Скасувати", callback_data="admin_users")
            ]])
        )
    elif callback_data == "admin_change_role":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        # Встановлюємо стан для зміни ролі
        user_states[user_id] = {
            'state': 'admin_changing_role',
            'data': {'step': 'telegram_id'}
        }
        
        await query.edit_message_text(
            "🔄 **Зміна ролі користувача**\n\n"
            "Введіть Telegram ID користувача:\n\n"
            "💡 **Приклад:** 123456789",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Скасувати", callback_data="admin_users")
            ]])
        )
    elif callback_data == "admin_reset_password":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        # Встановлюємо стан для скидання паролю
        user_states[user_id] = {
            'state': 'admin_resetting_password',
            'data': {'step': 'telegram_id'}
        }
        
        await query.edit_message_text(
            "🔐 **Скидання паролю користувача**\n\n"
            "Введіть Telegram ID користувача:\n\n"
            "💡 **Приклад:** 123456789",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Скасувати", callback_data="admin_users")
            ]])
        )
    elif callback_data == "admin_user_stats":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        try:
            stats = access_manager.get_user_statistics()
            
            stats_text = (
                f"📊 **Статистика користувачів**\n\n"
                f"👥 **Загальна статистика:**\n"
                f"• Всього користувачів: {stats['total_users']}\n"
                f"• Активних: {stats['active_users']}\n"
                f"• Неактивних: {stats['inactive_users']}\n\n"
                f"👑 **За ролями:**\n"
                f"• Адміністраторів: {stats['admin_users']}\n"
                f"• Звичайних користувачів: {stats['regular_users']}\n\n"
                f"🟢 **Активність:**\n"
                f"• Онлайн зараз: {stats['online_users']}\n"
                f"• Входили за останні 24 год: {stats['recent_logins']}"
            )
            
            await query.edit_message_text(
                stats_text,
                reply_markup=get_admin_users_keyboard()
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ **Помилка отримання статистики**\n\n{str(e)}",
                reply_markup=get_admin_users_keyboard()
            )
    
    # Нові обробники для статистики
    elif callback_data == "admin_general_stats":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        try:
            user_stats = access_manager.get_user_statistics()
            system_stats = access_manager.get_system_statistics()
            
            stats_text = (
                f"📊 **Загальна статистика системи**\n\n"
                f"👥 **Користувачі:**\n"
                f"• Всього користувачів: {user_stats['total_users']}\n"
                f"• Активних: {user_stats['active_users']}\n"
                f"• Адміністраторів: {user_stats['admin_users']}\n"
                f"• Звичайних: {user_stats['regular_users']}\n\n"
                f"📋 **Проекти:**\n"
                f"• Всього проектів: {system_stats.get('total_projects', 0)}\n"
                f"• Активних: {system_stats.get('active_projects', 0)}\n\n"
                f"🟢 **Активність:**\n"
                f"• Онлайн зараз: {user_stats['online_users']}\n"
                f"• Входили за 24 год: {user_stats['recent_logins']}\n"
                f"• Активних сесій: {system_stats.get('active_sessions', 0)}\n\n"
                f"💾 **Система:**\n"
                f"• Время работы: {system_stats.get('uptime', 'N/A')}\n"
                f"• Версия: 2.0 Enhanced"
            )
            
            await query.edit_message_text(
                stats_text,
                reply_markup=get_admin_stats_keyboard(),
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ **Помилка отримання статистики**\n\n{str(e)}",
                reply_markup=get_admin_stats_keyboard()
            )
    
    elif callback_data == "admin_project_stats":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        try:
            all_projects = project_manager.get_all_projects(user_id)
            twitter_projects = [p for projects in all_projects.values() for p in projects if p.get('platform') == 'twitter']
            discord_projects = [p for projects in all_projects.values() for p in projects if p.get('platform') == 'discord']
            
            stats_text = (
                f"📋 **Статистика проектів**\n\n"
                f"📊 **Загальна статистика:**\n"
                f"• Всього проектів: {sum(len(projects) for projects in all_projects.values())}\n"
                f"• Twitter проектів: {len(twitter_projects)}\n"
                f"• Discord проектів: {len(discord_projects)}\n\n"
                f"🐦 **Twitter проекти:**\n"
                f"• Активних: {len([p for p in twitter_projects if p.get('active', False)])}\n"
                f"• Неактивних: {len([p for p in twitter_projects if not p.get('active', False)])}\n\n"
                f"💬 **Discord проекти:**\n"
                f"• Активних: {len([p for p in discord_projects if p.get('active', False)])}\n"
                f"• Неактивних: {len([p for p in discord_projects if not p.get('active', False)])}\n\n"
                f"📈 **Популярні платформи:**\n"
                f"• Twitter: {len(twitter_projects)} проектів\n"
                f"• Discord: {len(discord_projects)} проектів"
            )
            
            await query.edit_message_text(
                stats_text,
                reply_markup=get_admin_stats_keyboard(),
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ **Помилка отримання статистики проектів**\n\n{str(e)}",
                reply_markup=get_admin_stats_keyboard()
            )
    
    elif callback_data == "admin_charts":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        await query.edit_message_text(
            "📈 **Графіки та діаграми**\n\n"
            "🎯 **Візуальна аналітика**\n\n"
            "📊 **Доступні графіки:**\n"
            "• 📈 Графік активності користувачів\n"
            "• 📊 Діаграма розподілу проектів\n"
            "• 📅 Графік створення проектів\n"
            "• 🔄 Графік активності моніторингу\n"
            "• 📱 Статистика платформ\n\n"
            "⚠️ **Примітка:** Графіки будуть додані в наступних версіях\n"
            "Поки що доступна текстова статистика.",
            reply_markup=get_admin_stats_keyboard(),
        )
    
    elif callback_data == "admin_export_data":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        await query.edit_message_text(
            "📤 **Експорт даних**\n\n"
            "🎯 **Експорт системних даних**\n\n"
            "📋 **Доступні формати:**\n"
            "• 📊 Excel файл (.xlsx)\n"
            "• 📄 CSV файл (.csv)\n"
            "• 📋 JSON файл (.json)\n"
            "• 📝 Текстовий файл (.txt)\n\n"
            "⚠️ **Примітка:** Функція експорту буде додана в наступних версіях\n"
            "Поки що дані доступні через адмін панель.",
            reply_markup=get_admin_stats_keyboard(),
        )
    
    elif callback_data == "admin_system_stats":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        try:
            stats = access_manager.get_system_statistics()
            
            stats_text = (
                f"📊 **Системна статистика**\n\n"
                f"👥 **Користувачі:**\n"
                f"• Всього користувачів: {stats['total_users']}\n"
                f"• Активних сесій: {stats['active_sessions']}\n\n"
                f"📋 **Проекти:**\n"
                f"• Всього проектів: {stats['total_projects']}\n"
                f"• Активних моніторів: {stats['active_monitors']}\n\n"
                f"⚙️ **Система:**\n"
                f"• Статус: {stats['system_uptime']}\n"
                f"• Останній бекап: {stats['last_backup']}\n"
                f"• Використання сховища: {stats['storage_usage']} символів"
            )
            
            await query.edit_message_text(
                stats_text,
                reply_markup=get_admin_system_keyboard()
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ **Помилка отримання статистики**\n\n{str(e)}",
                reply_markup=get_admin_system_keyboard()
            )
    elif callback_data == "admin_system_logs":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        try:
            logs = access_manager.get_logs(20)  # Останні 20 записів
            
            if not logs:
                logs_text = "📋 **Логи системи**\n\n❌ Логи відсутні"
            else:
                logs_text = "📋 **Логи системи** (останні 20 записів)\n\n"
                for log in logs:
                    logs_text += f"• {log}\n"
            
            await query.edit_message_text(
                logs_text,
                reply_markup=get_admin_system_keyboard()
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ **Помилка отримання логів**\n\n{str(e)}",
                reply_markup=get_admin_system_keyboard()
            )
    elif callback_data == "admin_cleanup_sessions":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        try:
            cleaned_count = access_manager.cleanup_inactive_sessions()
            
            if cleaned_count > 0:
                await query.edit_message_text(
                    f"🔄 **Очищення сесій завершено!**\n\n"
                    f"✅ Очищено {cleaned_count} неактивних сесій\n\n"
                    f"Неактивні користувачі були розлогінені.",
                    reply_markup=get_admin_system_keyboard()
                )
            else:
                await query.edit_message_text(
                    f"🔄 **Очищення сесій завершено!**\n\n"
                    f"ℹ️ Неактивних сесій не знайдено\n\n"
                    f"Всі сесії активні.",
                    reply_markup=get_admin_system_keyboard()
                )
        except Exception as e:
            await query.edit_message_text(
                f"❌ **Помилка очищення сесій**\n\n{str(e)}",
                reply_markup=get_admin_system_keyboard()
            )
    elif callback_data == "admin_create_backup":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        try:
            if access_manager.backup_data():
                await query.edit_message_text(
                    f"💾 **Резервна копія створена!**\n\n"
                    f"✅ Дані успішно збережено\n\n"
                    f"Резервна копія збережена в папці 'backups'.",
                    reply_markup=get_admin_system_keyboard()
                )
            else:
                await query.edit_message_text(
                    f"❌ **Помилка створення резервної копії!**\n\n"
                    f"Спробуйте ще раз.",
                    reply_markup=get_admin_system_keyboard()
                )
        except Exception as e:
            await query.edit_message_text(
                f"❌ **Помилка створення резервної копії**\n\n{str(e)}",
                reply_markup=get_admin_system_keyboard()
            )
    elif callback_data == "admin_reset_system":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        # Встановлюємо стан для підтвердження скидання
        user_states[user_id] = {
            'state': 'admin_resetting_system',
            'data': {}
        }
        
        await query.edit_message_text(
            "⚠️ **СКИДАННЯ СИСТЕМИ**\n\n"
            "🚨 **УВАГА!** Ця дія видалить ВСІХ користувачів крім адміністраторів!\n\n"
            "📋 **Що буде видалено:**\n"
            "• Всіх звичайних користувачів\n"
            "• Всі їхні проекти\n"
            "• Всі активні сесії\n\n"
            "✅ **Що буде збережено:**\n"
            "• Всіх адміністраторів\n"
            "• Резервна копія буде створена автоматично\n\n"
            "🔐 **Для підтвердження введіть:** CONFIRM_RESET",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Скасувати", callback_data="admin_system")
            ]])
        )
    
    # Обробники для моніторингу
    elif callback_data == "admin_monitoring_status":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        try:
            # Отримуємо статус моніторингу
            discord_status = "🟢 Активний" if discord_monitor else "🔴 Неактивний"
            twitter_status = "🟢 Активний" if twitter_monitor else "🔴 Неактивний"
            twitter_adapter_status = "🟢 Активний" if twitter_monitor_adapter else "🔴 Неактивний"
            
            status_text = (
                f"📈 **Статус моніторингу**\n\n"
                f"🎯 **Поточний стан системи:**\n\n"
                f"💬 **Discord моніторинг:**\n"
                f"• Статус: {discord_status}\n"
                f"• Авторизація: {'✅ Налаштована' if DISCORD_AUTHORIZATION else '❌ Не налаштована'}\n\n"
                f"🐦 **Twitter моніторинг:**\n"
                f"• Статус: {twitter_status}\n"
                f"• Авторизація: {'✅ Налаштована' if TWITTER_AUTH_TOKEN else '❌ Не налаштована'}\n\n"
                f"🔧 **Twitter Monitor Adapter:**\n"
                f"• Статус: {twitter_adapter_status}\n"
                f"• База даних: {'✅ Налаштована' if os.path.exists('./twitter_monitor/accounts.db') else '❌ Не налаштована'}\n\n"
                f"⏰ **Остання перевірка:** {datetime.now().strftime('%H:%M:%S')}\n"
                f"🔄 **Інтервал перевірки:** {MONITORING_INTERVAL} секунд"
            )
            
            await query.edit_message_text(
                status_text,
                reply_markup=get_admin_monitoring_keyboard(),
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ **Помилка отримання статусу**\n\n{str(e)}",
                reply_markup=get_admin_monitoring_keyboard()
            )
    
    elif callback_data == "admin_notifications":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        await query.edit_message_text(
            "🔔 **Налаштування сповіщень**\n\n"
            "🎯 **Центр уведомлений**\n\n"
            "📋 **Доступні налаштування:**\n"
            "• 📧 Email сповіщення\n"
            "• 📱 Telegram сповіщення\n"
            "• 🔔 Discord сповіщення\n"
            "• ⚠️ Сповіщення про помилки\n"
            "• 📊 Звіти про активність\n"
            "• 🚨 Критичні сповіщення\n\n"
            "⚠️ **Примітка:** Налаштування сповіщень будуть додані в наступних версіях",
            reply_markup=get_admin_monitoring_keyboard(),
        )
    
    elif callback_data == "admin_restart_monitoring":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        await query.edit_message_text(
            "🔄 **Перезапуск моніторингу**\n\n"
            "🎯 **Перезапуск системи моніторингу**\n\n"
            "📋 **Що буде перезапущено:**\n"
            "• Discord моніторинг\n"
            "• Twitter моніторинг\n"
            "• Twitter Monitor Adapter\n"
            "• Всі активні сесії\n\n"
            "⚠️ **Примітка:** Функція перезапуску буде додана в наступних версіях\n"
            "Поки що перезапустіть бот вручну.",
            reply_markup=get_admin_monitoring_keyboard(),
        )
    
    # Додаткові адміністративні обробники
    elif callback_data == "admin_backup_restore":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                format_error_message("Доступ заборонено!", "Тільки адміністратор має доступ до цієї функції."),
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        await query.edit_message_text(
            format_info_message(
                "Бекап та відновлення",
                "Управління резервними копіями системи",
                "⚠️ Функція бекапу буде додана в наступних версіях.\n"
                "Поки що дані зберігаються в файлах data.json та projects.json"
            ),
            reply_markup=get_admin_system_keyboard()
        )
    
    elif callback_data == "admin_clear_cache":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                format_error_message("Доступ заборонено!", "Тільки адміністратор має доступ до цієї функції."),
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        try:
            # Очищуємо кеш (глобальні змінні)
            global global_sent_tweets
            global_sent_tweets.clear()
            
            await query.edit_message_text(
                format_success_message(
                    "Кеш очищено",
                    "Всі тимчасові дані успішно видалено",
                    "Кеш відправлених твітів очищено. Система працюватиме швидше."
                ),
                reply_markup=get_admin_system_keyboard()
            )
        except Exception as e:
            await query.edit_message_text(
                format_error_message("Помилка очищення кешу", str(e)),
                reply_markup=get_admin_system_keyboard()
            )
    
    elif callback_data == "admin_clear_seen_tweets":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                format_error_message("Доступ заборонено!", "Тільки адміністратор має доступ до цієї функції."),
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        try:
            # Викликаємо функцію очищення seen_tweets
            success = reset_seen_tweets()
            
            if success:
                await query.edit_message_text(
                    format_success_message(
                        "Seen_tweets очищено",
                        "Всі файли з відправленими твітами успішно видалено",
                        "⚠️ УВАГА: Бот може повторно відправити старі твіти! Використовуйте обережно."
                    ),
                    reply_markup=get_admin_system_keyboard()
                )
            else:
                await query.edit_message_text(
                    format_error_message(
                        "Помилка очищення", 
                        "Не всі файли вдалося очистити",
                        "Перевірте логи для деталей"
                    ),
                    reply_markup=get_admin_system_keyboard()
                )
        except Exception as e:
            await query.edit_message_text(
                format_error_message("Помилка очищення", str(e)),
                reply_markup=get_admin_system_keyboard()
            )
    
    elif callback_data == "admin_system_config":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                format_error_message("Доступ заборонено!", "Тільки адміністратор має доступ до цієї функції."),
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        config_text = format_info_message(
            "Налаштування системи",
            "Поточні налаштування бота",
            f"🔧 Конфігурація:\n"
            f"• Тайм-аут сесії: {SECURITY_TIMEOUT} секунд\n"
            f"• Інтервал Discord: {MONITORING_INTERVAL} секунд\n"
            f"• Інтервал Twitter: {TWITTER_MONITORING_INTERVAL} секунд\n"
            f"• Активних сесій: {len(access_manager.user_sessions)}\n\n"
            f"⚠️ Зміна налаштувань буде додана в наступних версіях"
        )
        
        await query.edit_message_text(
            config_text,
            reply_markup=get_admin_system_keyboard()
        )
    
    # Обробники пошуку
    elif callback_data == "admin_search_users":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                format_error_message("Доступ заборонено!", "Тільки адміністратор має доступ до цієї функції."),
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        await query.edit_message_text(
            format_info_message(
                "Пошук користувачів",
                "Функція пошуку користувачів",
                "⚠️ Детальний пошук буде додано в наступних версіях.\n"
                "Поки що використовуйте 'Список користувачів' для перегляду всіх користувачів."
            ),
            reply_markup=get_admin_search_keyboard()
        )
    
    elif callback_data == "admin_search_projects":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                format_error_message("Доступ заборонено!", "Тільки адміністратор має доступ до цієї функції."),
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        await query.edit_message_text(
            format_info_message(
                "Пошук проектів",
                "Функція пошуку проектів",
                "⚠️ Детальний пошук буде додано в наступних версіях.\n"
                "Поки що використовуйте 'Всі проекти' для перегляду всіх проектів."
            ),
            reply_markup=get_admin_search_keyboard()
        )
    
    # Обробники для налаштувань
    elif callback_data == "admin_security_settings":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        await query.edit_message_text(
            "🔐 **Налаштування безпеки**\n\n"
            "🎯 **Центр безпеки**\n\n"
            "📋 **Доступні налаштування:**\n"
            "• 🔑 Налаштування паролів\n"
            "• ⏰ Таймаути сесій\n"
            "• 🚫 Блокування користувачів\n"
            "• 📝 Логи безпеки\n"
            "• 🔒 Шифрування даних\n"
            "• 🛡️ Захист від атак\n\n"
            "⚠️ **Примітка:** Розширені налаштування безпеки будуть додані в наступних версіях",
            reply_markup=get_admin_settings_keyboard(),
        )
    
    elif callback_data == "admin_ui_settings":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        await query.edit_message_text(
            "🎨 **Налаштування інтерфейсу**\n\n"
            "🎯 **Центр налаштувань UI**\n\n"
            "📋 **Доступні налаштування:**\n"
            "• 🎨 Теми інтерфейсу\n"
            "• 📱 Розмір кнопок\n"
            "• 🌍 Мова інтерфейсу\n"
            "• 📊 Стиль статистики\n"
            "• 🔔 Стиль сповіщень\n"
            "• 📋 Макет меню\n\n"
            "⚠️ **Примітка:** Налаштування інтерфейсу будуть додані в наступних версіях",
            reply_markup=get_admin_settings_keyboard(),
        )
    
    # Обробники нових меню
    elif callback_data == "quick_actions":
        await query.edit_message_text(
            "⚡ **Швидкі дії**\n\n"
            "Оберіть дію для швидкого виконання:",
            reply_markup=get_quick_actions_keyboard(user_id)
        )
    elif callback_data == "about":
        about_text = (
            "ℹ️ **Про бота**\n\n"
            "🤖 **Telegram Monitor Bot**\n"
            "Версія: 2.0\n\n"
            "📋 **Функції:**\n"
            "• Моніторинг Twitter/X акаунтів\n"
            "• Моніторинг Discord каналів\n"
            "• Автоматичне пересилання повідомлень\n"
            "• Система безпеки з авторизацією\n"
            "• Адміністративна панель\n"
            "• Twitter Monitor Adapter для обходу обмежень\n\n"
            "👨‍💻 **Розробник:** megymin\n"
            "📅 **Останнє оновлення:** 2025"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")]]
        await query.edit_message_text(
            about_text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    # Обробники швидких дій
    elif callback_data == "start_all_monitors":
        try:
            # Запускаємо всі монітори через автоматичну функцію
            auto_start_monitoring()
            
            await query.edit_message_text(
                "🚀 **Всі монітори запущено!**\n\n"
                "✅ Twitter API моніторинг активний\n"
                "✅ Twitter Monitor Adapter моніторинг активний\n"
                "✅ Discord моніторинг активний\n"
                "✅ Автоматичні сповіщення увімкнено",
                reply_markup=get_quick_actions_keyboard(user_id)
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ **Помилка запуску моніторів**\n\n{str(e)}",
                reply_markup=get_quick_actions_keyboard(user_id)
            )
    elif callback_data == "stop_all_monitors":
        try:
            # Зупиняємо всі монітори
            if twitter_monitor_adapter:
                twitter_monitor_adapter.monitoring_active = False
            
            # Зупиняємо Twitter API моніторинг
            if twitter_monitor:
                twitter_monitor.monitoring_active = False
            
            # Зупиняємо Discord моніторинг
            if discord_monitor:
                discord_monitor.monitoring_active = False
            
            await query.edit_message_text(
                "⏹️ **Всі монітори зупинено!**\n\n"
                "🔴 Twitter API моніторинг зупинено\n"
                "🔴 Twitter Monitor Adapter моніторинг зупинено\n"
                "🔴 Discord моніторинг зупинено\n"
                "🔴 Автоматичні сповіщення вимкнено",
                reply_markup=get_quick_actions_keyboard(user_id)
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ **Помилка зупинки моніторів**\n\n{str(e)}",
                reply_markup=get_quick_actions_keyboard(user_id)
            )
    elif callback_data == "quick_stats":
        try:
            stats = project_manager.get_statistics()
            user_projects = project_manager.get_user_projects(user_id)
            twitter_count = len([p for p in user_projects if p['platform'] == 'twitter'])
            discord_count = len([p for p in user_projects if p['platform'] == 'discord'])
            twitter_adapter_count = len(project_manager.get_selenium_accounts())
            
            quick_stats_text = (
                "📊 **Швидка статистика**\n\n"
                f"👤 **Ваші проекти:**\n"
                f"• Twitter: {twitter_count}\n"
                f"• Discord: {discord_count}\n"
                f"• Twitter Monitor Adapter: {twitter_adapter_count}\n\n"
                f"🌐 **Загальна статистика:**\n"
                f"• Всього користувачів: {stats.get('total_users', 0)}\n"
                f"• Всього проектів: {stats.get('total_projects', 0)}\n"
                f"• Активних сесій: {len(access_manager.user_sessions)}"
            )
            
            await query.edit_message_text(
                quick_stats_text,
                reply_markup=get_quick_actions_keyboard(user_id)
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ **Помилка отримання статистики**\n\n{str(e)}",
                reply_markup=get_quick_actions_keyboard(user_id)
            )
    elif callback_data == "recent_messages":
        await query.edit_message_text(
            "📝 **Останні повідомлення**\n\nОберіть кількість повідомлень для перегляду:",
            reply_markup=get_history_count_keyboard()
        )
    elif callback_data == "refresh_data":
        try:
            project_manager.load_data()
            sync_monitors_with_projects()
            
            await query.edit_message_text(
                "🔄 **Дані оновлено!**\n\n"
                "✅ Проекти перезавантажено\n"
                "✅ Монітори синхронізовано\n"
                "✅ Налаштування оновлено",
                reply_markup=get_quick_actions_keyboard(user_id)
            )
        except Exception as e:
            await query.edit_message_text(
                f"❌ **Помилка оновлення даних**\n\n{str(e)}",
                reply_markup=get_quick_actions_keyboard(user_id)
            )
    
    # Обробники допомоги
    elif callback_data == "help_getting_started":
        help_text = (
            "🚀 **Початок роботи**\n\n"
            "**Крок 1:** Авторизуйтеся за допомогою /login\n"
            "**Крок 2:** Створіть новий проект через меню\n"
            "**Крок 3:** Додайте посилання на Twitter або Discord\n"
            "**Крок 4:** Налаштуйте пересилання повідомлень\n"
            "**Крок 5:** Запустіть моніторинг\n\n"
            "💡 **Поради:**\n"
            "• Використовуйте швидкі дії для зручності\n"
            "• Перевіряйте діагностику при проблемах\n"
            "• Налаштуйте автоматичне пересилання"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="help")]]
        await query.edit_message_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard))
    elif callback_data == "help_twitter":
        help_text = (
            "🐦 **Twitter налаштування**\n\n"
            "**Формат посилань:**\n"
            "• https://twitter.com/username\n"
            "• https://x.com/username\n\n"
            "**Twitter Monitor Adapter моніторинг:**\n"
            "• Обходить обмеження API\n"
            "• Автоматичний запуск\n"
            "• Підтримка зображень\n\n"
            "**Команди:**\n"
            "• /twitter_start - запустити\n"
            "• /twitter_stop - зупинити\n"
            "• /twitter_add username - додати акаунт"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="help")]]
        await query.edit_message_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard))
    elif callback_data == "help_discord":
        help_text = (
            "💬 **Discord налаштування**\n\n"
            "**Формат посилань:**\n"
            "• https://discord.com/channels/server_id/channel_id\n\n"
            "**Налаштування:**\n"
            "• Потрібен AUTHORIZATION токен\n"
            "• Встановіть в .env файлі\n"
            "• Перевірте через діагностику\n\n"
            "**Функції:**\n"
            "• Моніторинг нових повідомлень\n"
            "• Автоматичне пересилання\n"
            "• Підтримка зображень та файлів"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="help")]]
        await query.edit_message_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard))
    elif callback_data == "help_forwarding":
        help_text = (
            "📢 **Налаштування пересилання**\n\n"
            "**Автоналаштування:**\n"
            "• Додайте бота в канал як адміністратора\n"
            "• Напишіть в каналі: @botusername ping\n"
            "• Бот автоматично налаштує пересилання\n\n"
            "**Ручне налаштування:**\n"
            "• /forward_set_channel CHANNEL_ID\n"
            "• /forward_on - увімкнути\n"
            "• /forward_off - вимкнути\n\n"
            "**Тестування:**\n"
            "• /forward_test - відправити тестове повідомлення"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="help")]]
        await query.edit_message_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard))
    elif callback_data == "help_faq":
        help_text = (
            "❓ **Часті питання**\n\n"
            "**Q: Чому не працює Twitter моніторинг?**\n"
            "A: Спробуйте Twitter Monitor Adapter моніторинг - він обходить обмеження API\n\n"
            "**Q: Як налаштувати Discord?**\n"
            "A: Потрібен AUTHORIZATION токен в .env файлі\n\n"
            "**Q: Сесія постійно закінчується**\n"
            "A: Сесія діє 5 хвилин. Використовуйте бота активно\n\n"
            "**Q: Не отримую сповіщення**\n"
            "A: Перевірте налаштування пересилання та права бота"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="help")]]
        await query.edit_message_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard))
    elif callback_data == "help_support":
        help_text = format_info_message(
            "Підтримка",
            "Технічна підтримка та допомога",
            "• Використовуйте діагностику системи для перевірки стану\n"
            "• Перевіряйте логи через адміністративну панель\n"
            "• Перезапускайте монітори при необхідності\n"
            "• Зверніться до адміністратора при серйозних проблемах"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="help")]]
        await query.edit_message_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    # Обробники налаштувань
    elif callback_data == "settings_notifications":
        settings_text = (
            "🔔 **Налаштування сповіщень**\n\n"
            "**Поточні налаштування:**\n"
            "• Сповіщення про нові повідомлення: ✅ Увімкнено\n"
            "• Сповіщення про помилки: ✅ Увімкнено\n"
            "• Звукові сповіщення: ❌ Вимкнено\n\n"
            "⚠️ **Примітка:** Детальні налаштування сповіщень будуть додані в наступних версіях"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="settings")]]
        await query.edit_message_text(settings_text, reply_markup=InlineKeyboardMarkup(keyboard))
    elif callback_data == "settings_intervals":
        settings_text = (
            "⏰ **Інтервали моніторингу**\n\n"
            f"**Поточні налаштування:**\n"
            f"• Discord: {MONITORING_INTERVAL} секунд\n"
            f"• Twitter: {TWITTER_MONITORING_INTERVAL} секунд\n"
            f"• Twitter Monitor Adapter: 30 секунд\n\n"
            "⚠️ **Примітка:** Зміна інтервалів буде додана в наступних версіях.\n"
            "Поки що інтервали налаштовуються в config.py"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="settings")]]
        await query.edit_message_text(settings_text, reply_markup=InlineKeyboardMarkup(keyboard))
    elif callback_data == "settings_theme":
        settings_text = (
            "🎨 **Тема інтерфейсу**\n\n"
            "**Поточна тема:** Стандартна\n\n"
            "**Доступні теми:**\n"
            "• 🌟 Стандартна (поточна)\n"
            "• 🌙 Темна (планується)\n"
            "• 🌈 Кольорова (планується)\n\n"
            "⚠️ **Примітка:** Вибір теми буде додано в наступних версіях"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="settings")]]
        await query.edit_message_text(settings_text, reply_markup=InlineKeyboardMarkup(keyboard))
    elif callback_data == "settings_language":
        settings_text = (
            "🌐 **Налаштування мови**\n\n"
            "**Поточна мова:** 🇺🇦 Українська\n\n"
            "**Доступні мови:**\n"
            "• 🇺🇦 Українська (поточна)\n"
            "• 🇬🇧 English (планується)\n"
            "• 🇷🇺 Русский (планується)\n\n"
            "⚠️ **Примітка:** Багатомовність буде додана в наступних версіях"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="settings")]]
        await query.edit_message_text(settings_text, reply_markup=InlineKeyboardMarkup(keyboard))
    elif callback_data == "settings_security":
        session_time_left = security_manager.get_session_time_left(user_id) if security_manager else 0
        settings_text = (
            "🔒 **Налаштування безпеки**\n\n"
            "**Поточні налаштування:**\n"
            f"• Тайм-аут сесії: {SECURITY_TIMEOUT} секунд\n"
            f"• Час до закінчення сесії: {session_time_left} секунд\n"
            f"• Активних сесій: {len(access_manager.user_sessions)}\n\n"
            "**Функції безпеки:**\n"
            "• Автоматичне завершення сесії\n"
            "• Авторизація за паролем\n"
            "• Контроль доступу адміністраторів\n\n"
            "⚠️ **Примітка:** Додаткові налаштування безпеки будуть додані пізніше"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="settings")]]
        await query.edit_message_text(settings_text, reply_markup=InlineKeyboardMarkup(keyboard))
    elif callback_data == "settings_export":
        try:
            stats = project_manager.get_statistics()
            user_projects = project_manager.get_user_projects(user_id)
            
            export_text = (
                "📊 **Експорт даних**\n\n"
                "**Ваші дані:**\n"
                f"• Проекти: {len(user_projects)}\n"
                f"• Twitter проекти: {len([p for p in user_projects if p['platform'] == 'twitter'])}\n"
                f"• Discord проекти: {len([p for p in user_projects if p['platform'] == 'discord'])}\n\n"
                "**Загальна статистика:**\n"
                f"• Всього користувачів: {stats.get('total_users', 0)}\n"
                f"• Всього проектів: {stats.get('total_projects', 0)}\n"
                f"• Розмір файлу даних: {stats.get('data_file_size', 0)} байт\n\n"
                "⚠️ **Примітка:** Функція експорту в файл буде додана пізніше"
            )
        except Exception as e:
            export_text = f"❌ **Помилка отримання даних для експорту**\n\n{str(e)}"
        
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="settings")]]
        await query.edit_message_text(export_text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    # Додаткові обробники для моніторингу
    elif callback_data == "admin_monitoring_schedule":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                format_error_message("Доступ заборонено!", "Тільки адміністратор має доступ до цієї функції."),
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        await query.edit_message_text(
            format_info_message(
                "Розклад моніторингу",
                "Налаштування розкладу моніторингу",
                "⚠️ Функція розкладу буде додана в наступних версіях.\n"
                "Поки що моніторинг працює постійно з фіксованими інтервалами."
            ),
            reply_markup=get_admin_monitoring_keyboard()
        )
    
    elif callback_data == "admin_monitoring_logs":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                format_error_message("Доступ заборонено!", "Тільки адміністратор має доступ до цієї функції."),
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        await query.edit_message_text(
            format_info_message(
                "Логи моніторингу",
                "Журнал подій моніторингу",
                "⚠️ Детальні логи моніторингу будуть додані в наступних версіях.\n"
                "Поки що перевіряйте консоль для логів системи."
            ),
            reply_markup=get_admin_monitoring_keyboard()
        )
    
    # Обробники фільтрів та аналітики
    elif callback_data == "admin_stats_filters":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                format_error_message("Доступ заборонено!", "Тільки адміністратор має доступ до цієї функції."),
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        await query.edit_message_text(
            format_info_message(
                "Фільтри статистики",
                "Розширені фільтри для статистики",
                "⚠️ Функція фільтрів буде додана в наступних версіях.\n"
                "Поки що доступна базова статистика."
            ),
            reply_markup=get_admin_search_keyboard()
        )
    
    elif callback_data == "admin_date_filter":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                format_error_message("Доступ заборонено!", "Тільки адміністратор має доступ до цієї функції."),
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        await query.edit_message_text(
            format_info_message(
                "Фільтр за датою",
                "Фільтрація даних за часовим періодом",
                "⚠️ Функція фільтрації за датою буде додана в наступних версіях.\n"
                "Поки що доступні всі дані без фільтрації."
            ),
            reply_markup=get_admin_search_keyboard()
        )
    
    elif callback_data == "admin_tag_filter":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                format_error_message("Доступ заборонено!", "Тільки адміністратор має доступ до цієї функції."),
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        await query.edit_message_text(
            format_info_message(
                "Фільтр за тегами",
                "Фільтрація даних за тегами",
                "⚠️ Система тегів буде додана в наступних версіях.\n"
                "Поки що використовуйте пошук за назвою проектів."
            ),
            reply_markup=get_admin_search_keyboard()
        )
    
    elif callback_data == "admin_advanced_analytics":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                format_error_message("Доступ заборонено!", "Тільки адміністратор має доступ до цієї функції."),
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        await query.edit_message_text(
            format_info_message(
                "Розширена аналітика",
                "Детальний аналіз даних системи",
                "⚠️ Розширена аналітика буде додана в наступних версіях.\n"
                "Поки що доступна базова статистика в розділі 'Статистика та аналітика'."
            ),
            reply_markup=get_admin_search_keyboard()
        )
    
    # Обробники відсутніх callback'ів
    elif callback_data == "user_stats":
        try:
            stats = project_manager.get_statistics()
            user_projects = project_manager.get_user_projects(user_id)
            twitter_count = len([p for p in user_projects if p['platform'] == 'twitter'])
            discord_count = len([p for p in user_projects if p['platform'] == 'discord'])
            twitter_adapter_count = len(project_manager.get_selenium_accounts())
            
            stats_text = format_info_message(
                "Ваша статистика",
                f"📊 Ваші проекти:\n"
                f"• Twitter: {twitter_count}\n"
                f"• Discord: {discord_count}\n"
                f"• Twitter Monitor Adapter: {twitter_adapter_count}\n\n"
                f"🌐 Загальна статистика:\n"
                f"• Всього користувачів: {stats.get('total_users', 0)}\n"
                f"• Всього проектів: {stats.get('total_projects', 0)}\n"
                f"• Активних сесій: {len(access_manager.user_sessions)}",
                f"Час до закінчення сесії: {security_manager.get_session_time_left(user_id)} секунд"
            )
            
            await query.edit_message_text(
                stats_text,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")]])
            )
        except Exception as e:
            await query.edit_message_text(
                format_error_message("Помилка статистики", str(e)),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")]])
            )
    
    elif callback_data == "change_channel":
        user_states[user_id] = {'state': 'setting_forward_channel'}
        await query.edit_message_text(
            "✏️ Зміна каналу пересилання\n\n"
            "Надішліть ID каналу або перешліть повідомлення з каналу:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="forward_settings")]])
        )
    
    elif callback_data == "set_channel":
        user_states[user_id] = {'state': 'setting_forward_channel'}
        await query.edit_message_text(
            "📝 Встановлення каналу пересилання\n\n"
            "Надішліть ID каналу або перешліть повідомлення з каналу:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="forward_settings")]])
        )
    
    elif callback_data.startswith("history_"):
        # Отримуємо історію повідомлень
        count = int(callback_data.split("_")[1])
        await handle_discord_history(update, context, count)
    
    elif callback_data == "help_settings":
        help_text = format_info_message(
            "Налаштування допомоги",
            "Як налаштувати бота під свої потреби",
            "• Використовуйте розділ 'Налаштування' в головному меню\n"
            "• Налаштуйте інтервали моніторингу\n"
            "• Оберіть тему інтерфейсу\n"
            "• Налаштуйте сповіщення\n"
            "• Експортуйте свої дані при потребі"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Назад", callback_data="help")]]
        await query.edit_message_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard))
    
    # Обробники адміністративних функцій
    elif callback_data == "admin_system":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                format_error_message("Доступ заборонено!", "Тільки адміністратор має доступ до цієї панелі."),
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        await query.edit_message_text(
            format_info_message(
                "Системне управління",
                "Управління системними функціями бота",
                "Тут ви можете керувати системними налаштуваннями, переглядати логи, створювати бекапи та виконувати інші адміністративні завдання."
            ),
            reply_markup=get_admin_system_keyboard()
        )
    
    elif callback_data == "admin_user_activity":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                format_error_message("Доступ заборонено!", "Тільки адміністратор має доступ до цієї функції."),
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        try:
            # Отримуємо активність користувачів
            active_sessions = len(access_manager.user_sessions)
            total_users = len(project_manager.data.get('users', {}))
            
            activity_text = format_info_message(
                "Активність користувачів",
                f"📊 Загальна інформація:\n"
                f"• Всього користувачів: {total_users}\n"
                f"• Активних сесій: {active_sessions}\n"
                f"• Користувачів онлайн: {active_sessions}",
                "Детальна статистика активності користувачів"
            )
            
            await query.edit_message_text(
                activity_text,
                reply_markup=get_admin_users_keyboard()
            )
        except Exception as e:
            await query.edit_message_text(
                format_error_message("Помилка отримання активності", str(e)),
                reply_markup=get_admin_users_keyboard()
        )
    
    # Нові обробники для покращеної адмін панелі
    elif callback_data == "admin_search":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        await query.edit_message_text(
            "🔍 **Пошук та фільтри**\n\n"
            "🎯 **Центр поиска и фильтрации**\n\n"
            "📋 **Доступные функции:**\n"
            "• 🔍 Поиск пользователей\n"
            "• 📋 Поиск проектов\n"
            "• 📊 Фильтры статистики\n"
            "• 📅 Фильтр по дате\n"
            "• 🏷️ Фильтр по тегам\n"
            "• 📈 Расширенная аналитика\n\n"
            "Выберите нужную функцию:",
            reply_markup=get_admin_search_keyboard(),
        )
    
    elif callback_data == "admin_monitoring":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        await query.edit_message_text(
            "📈 **Моніторинг системи**\n\n"
            "🎯 **Центр мониторинга**\n\n"
            "📋 **Доступные функции:**\n"
            "• 📈 Статус мониторинга\n"
            "• 🔔 Настройки уведомлений\n"
            "• ⏰ Расписание мониторинга\n"
            "• 📊 Логи мониторинга\n"
            "• 🔄 Перезапуск мониторинга\n"
            "• ⚡ Скорость отклика\n\n"
            "Выберите нужную функцию:",
            reply_markup=get_admin_monitoring_keyboard(),
        )
    
    elif callback_data == "admin_settings":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        await query.edit_message_text(
            "⚙️ **Налаштування системи**\n\n"
            "🎯 **Центр настроек**\n\n"
            "📋 **Доступные настройки:**\n"
            "• 🔐 Настройки безопасности\n"
            "• 🎨 Настройки интерфейса\n"
            "• 📱 Настройки бота\n"
            "• 🌐 Настройки сети\n"
            "• 💾 Настройки хранения\n"
            "• 🔧 Расширенные настройки\n\n"
            "Выберите нужный раздел:",
            reply_markup=get_admin_settings_keyboard(),
        )
    
    elif callback_data == "admin_stats":
        if not access_manager.is_admin(user_id):
            await query.edit_message_text(
                "❌ Доступ заборонено!",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return
        
        await query.edit_message_text(
            "📊 **Статистика та аналітика**\n\n"
            "🎯 **Центр статистики**\n\n"
            "📋 **Доступные отчеты:**\n"
            "• 📊 Общая статистика\n"
            "• 👥 Статистика пользователей\n"
            "• 📋 Статистика проектов\n"
            "• 📈 Графики и диаграммы\n"
            "• 📅 Статистика за период\n"
            "• 🔍 Детальная аналитика\n"
            "• 📤 Экспорт данных\n\n"
            "Выберите нужный отчет:",
            reply_markup=get_admin_stats_keyboard(),
        )
    
    # Адмін керування пересиланням: дії з кнопок
    elif callback_data.startswith("admin_forward_enable_"):
        if not access_manager.is_admin(user_id):
            return
        target_id = int(callback_data.split('_')[-1])
        project_manager.enable_forward(target_id)
        await query.edit_message_text(
            f"🟢 Пересилання увімкнено для `{target_id}`",
            reply_markup=get_admin_forward_keyboard(target_id),
        )
    elif callback_data.startswith("admin_forward_disable_"):
        if not access_manager.is_admin(user_id):
            return
        target_id = int(callback_data.split('_')[-1])
        project_manager.disable_forward(target_id)
        await query.edit_message_text(
            f"🔴 Пересилання вимкнено для `{target_id}`",
            reply_markup=get_admin_forward_keyboard(target_id),
        )
    elif callback_data.startswith("admin_forward_status_"):
        if not access_manager.is_admin(user_id):
            return
        target_id = int(callback_data.split('_')[-1])
        status = project_manager.get_forward_status(target_id)
        enabled = status.get('enabled', False)
        channel = status.get('channel_id') or '—'
        await query.edit_message_text(
            f"📊 Статус пересилання для `{target_id}`\n\nСтатус: {'🟢 увімкнено' if enabled else '🔴 вимкнено'}\nКанал: `{channel}`",
            reply_markup=get_admin_forward_keyboard(target_id),
        )
    elif callback_data.startswith("admin_forward_test_"):
        if not access_manager.is_admin(user_id):
            return
        target_id = int(callback_data.split('_')[-1])
        forward_channel = project_manager.get_forward_channel(target_id)
        if not forward_channel:
            await query.edit_message_text(
                f"❌ У користувача `{target_id}` не налаштований канал.",
                reply_markup=get_admin_forward_keyboard(target_id),
            )
        else:
            try:
                test_text = (
                    f"🧪 Тест пересилання\n\n"
                    f"Це тестове повідомлення від адміністратора для користувача `{target_id}`."
                )
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
                data = {
                    'chat_id': normalize_chat_id(forward_channel),
                    'text': test_text,
                }
                r = requests.post(url, data=data, timeout=5)
                if r.status_code == 200:
                    await query.edit_message_text(
                        f"✅ Тестове повідомлення надіслано у `{normalize_chat_id(forward_channel)}`",
                        reply_markup=get_admin_forward_keyboard(target_id),
                    )
                else:
                    await query.edit_message_text(
                        f"❌ Помилка надсилання ({r.status_code}). Перевірте права бота у каналі.",
                        reply_markup=get_admin_forward_keyboard(target_id)
                    )
            except Exception as e:
                await query.edit_message_text(
                    f"❌ Виняток при надсиланні: {e}",
                    reply_markup=get_admin_forward_keyboard(target_id)
                )
    elif callback_data.startswith("admin_forward_set_"):
        if not access_manager.is_admin(user_id):
            return
        target_id = int(callback_data.split('_')[-1])
        # Переводимо у стан очікування ID каналу
        user_states[user_id] = {'state': 'admin_forward_set_channel', 'data': {'target_id': target_id}}
        await query.edit_message_text(
            f"📝 Перешліть повідомлення з потрібного каналу АБО введіть його ID для користувача `{target_id}`:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="admin_users")]]),
        )

async def handle_project_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник створення проекту"""
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    message_text = update.message.text
    state_data: Dict[str, Any] = user_states[user_id]['data']
    
    if 'name' not in state_data:
        # Зберігаємо назву проекту
        state_data['name'] = message_text
        platform = state_data['platform']
        
        if platform == 'twitter':
            await update.message.reply_text(
                f"✅ Назва проекту: {message_text}\n\n"
                f"🐦 Тепер введіть посилання на Twitter/X сторінку:\n"
                f"Приклад: https://twitter.com/username"
            )
        else:  # discord
            await update.message.reply_text(
                f"✅ Назва проекту: {message_text}\n\n"
                f"💬 Тепер введіть посилання на Discord канал:\n"
                f"Приклад: https://discord.com/channels/1408570777275469866/1413243132467871839"
            )
    else:
        # Зберігаємо посилання та завершуємо створення
        state_data['url'] = message_text
        
        # Додаємо проект
        if project_manager.add_project(user_id, state_data):
            # Синхронізуємо монітори з новими проектами
            sync_monitors_with_projects()
            
            # Додаємо до відповідного моніторингу
            if state_data['platform'] == 'discord' and discord_monitor:
                try:
                    discord_monitor.add_channel(state_data['url'])
                    logger.info(f"Додано Discord канал до моніторингу: {state_data['url']}")
                except Exception as e:
                    logger.error(f"Помилка додавання Discord каналу до моніторингу: {e}")
            elif state_data['platform'] == 'twitter' and twitter_monitor:
                try:
                    # Витягуємо username з URL
                    username = extract_twitter_username(state_data['url'])
                    if username:
                        twitter_monitor.add_account(username)
                        logger.info(f"Додано Twitter акаунт до моніторингу: {username}")
                except Exception as e:
                    logger.error(f"Помилка додавання Twitter акаунта до моніторингу: {e}")
                    
            success_text = (
                f"🎉 Проект успішно додано!\n\n"
                f"📝 Назва: {state_data['name']}\n"
                f"🌐 Платформа: {state_data['platform'].title()}\n"
                f"🔗 Посилання: {state_data['url']}\n\n"
                f"Проект додано до списку моніторингу."
            )
            await update.message.reply_text(
                success_text,
                reply_markup=get_main_menu_keyboard(user_id)
            )
        else:
            await update.message.reply_text(
                "❌ Помилка при додаванні проекту. Спробуйте ще раз.",
                reply_markup=get_main_menu_keyboard(user_id)
            )
        
        # Очищуємо стан користувача
        del user_states[user_id]

async def handle_forward_channel_setting(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник встановлення каналу для пересилання"""
    if not update.effective_user or not update.message or not update.message.text:
        return
    
    user_id = update.effective_user.id
    message_text = update.message.text.strip()
    
    # Очищаємо @ якщо є
    if message_text.startswith('@'):
        message_text = message_text[1:]
    
    # Валідація каналу
    if not message_text:
        await update.message.reply_text("❌ Введіть правильний ID або username каналу.")
        return
    
    # Спробуємо встановити канал
    if project_manager.set_forward_channel(user_id, message_text):
        success_text = (
            f"✅ Канал для пересилання встановлено!\n\n"
            f"📺 Канал: {message_text}\n"
            f"🔄 Статус: Увімкнено\n\n"
            f"Тепер всі нові повідомлення з ваших Discord проектів будуть автоматично пересилатися в цей канал."
        )
        await update.message.reply_text(
            success_text,
            reply_markup=get_main_menu_keyboard(user_id)
        )
    else:
        await update.message.reply_text(
            "❌ Помилка встановлення каналу. Спробуйте ще раз.",
            reply_markup=get_main_menu_keyboard(user_id)
        )
    
    # Очищуємо стан користувача
    if user_id in user_states:
        del user_states[user_id]

@require_auth
async def handle_admin_create_project_for_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Майстер створення проекту для іншого користувача (адмін)"""
    if not update.effective_user or not update.message or not update.message.text:
        return
    
    admin_id = update.effective_user.id
    state = user_states.get(admin_id, {}).get('data', {})
    step = state.get('step', 'telegram_id')
    text = update.message.text.strip()
    
    # Крок 1: вибір користувача
    if step == 'telegram_id':
        if not text.isdigit():
            await update.message.reply_text("❌ Введіть числовий Telegram ID користувача:")
            return
        target_id = int(text)
        target = access_manager.get_user_by_telegram_id(target_id)
        if not target:
            await update.message.reply_text("❌ Користувача не знайдено. Введіть інший Telegram ID:")
            return
        state['target_id'] = target_id
        state['step'] = 'platform'
        await update.message.reply_text(
            "🌐 Вкажіть платформу проекту: 'twitter' або 'discord'",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Скасувати", callback_data="admin_panel")]])
        )
        return
    
    # Крок 2: платформа
    if step == 'platform':
        platform = text.lower()
        if platform not in ['twitter', 'discord']:
            await update.message.reply_text("❌ Невірна платформа. Введіть 'twitter' або 'discord':")
            return
        state['platform'] = platform
        state['step'] = 'name'
        await update.message.reply_text("📝 Введіть назву проекту:")
        return
    
    # Крок 3: назва
    if step == 'name':
        state['name'] = text
        state['step'] = 'url'
        if state['platform'] == 'twitter':
            await update.message.reply_text("🔗 Введіть посилання на Twitter/X Без @ (напр. username):")
        else:
            await update.message.reply_text("🔗 Введіть посилання на Discord канал (напр. https://discord.com/channels/<server>/<channel>):")
        return
    
    # Крок 4: URL та створення
    if step == 'url':
        state['url'] = text
        project_data = {
            'name': state['name'],
            'platform': state['platform'],
            'url': state['url'],
            'description': f"Адміном створено для {state['target_id']}"
        }
        # Створюємо проект від імені target_id
        ok = project_manager.add_project(admin_id, project_data, target_user_id=state['target_id'])
        if ok:
            # Синхронізуємо монітори з новими проектами
            sync_monitors_with_projects()
            
            # Додаємо у відповідний монітор одразу
            if state['platform'] == 'twitter':
                username = extract_twitter_username(state['url'])
                if twitter_monitor and username:
                    twitter_monitor.add_account(username)
            else:
                if discord_monitor:
                    discord_monitor.add_channel(state['url'])
            sync_monitors_with_projects()
            await update.message.reply_text("✅ Проект створено і додано до моніторингу.")
        else:
            await update.message.reply_text("❌ Не вдалося створити проект.")
        # Завершуємо майстер
        if admin_id in user_states:
            del user_states[admin_id]

async def handle_twitter_addition(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник додавання Twitter акаунта"""
    if not update.effective_user or not update.message or not update.message.text:
        return
    
    user_id = update.effective_user.id
    username = update.message.text.strip().replace('@', '')
    
    try:
        # Додаємо до Twitter моніторингу
        if twitter_monitor:
            twitter_monitor.add_account(username)
        
        # Створюємо проект
        project_data = {
            'name': f"Twitter: @{username}",
            'platform': 'twitter',
            'url': f"https://twitter.com/{username}",
            'description': f"Моніторинг Twitter акаунта @{username}"
        }
        
        if project_manager.add_project(user_id, project_data):
            # Синхронізуємо монітори з новими проектами
            sync_monitors_with_projects()
            
            await update.message.reply_text(
                f"✅ **Twitter акаунт успішно додано!**\n\n"
                f"🐦 **Username:** @{username}\n"
                f"🔗 **URL:** https://twitter.com/{username}\n\n"
                f"Акаунт додано до моніторингу.",
                reply_markup=get_twitter_projects_keyboard(user_id),
            )
        else:
            await update.message.reply_text(
                "❌ Помилка при додаванні проекту.",
                reply_markup=get_twitter_projects_keyboard(user_id)
            )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Помилка: {str(e)}",
            reply_markup=get_twitter_projects_keyboard(user_id)
        )
    
    # Очищаємо стан
    del user_states[user_id]

async def handle_discord_addition(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник додавання Discord каналу"""
    if not update.effective_user or not update.message or not update.message.text:
        return
    
    user_id = update.effective_user.id
    channel_id = update.message.text.strip()
    
    try:
        # Додаємо до Discord моніторингу
        if discord_monitor:
            discord_monitor.add_channel(channel_id)
        
        # Створюємо проект
        project_data = {
            'name': f"Discord: {channel_id}",
            'platform': 'discord',
            'url': f"https://discord.com/channels/{channel_id}",
            'description': f"Моніторинг Discord каналу {channel_id}"
        }
        
        if project_manager.add_project(user_id, project_data):
            # Синхронізуємо монітори з новими проектами
            sync_monitors_with_projects()
            
            await update.message.reply_text(
                f"✅ **Discord канал успішно додано!**\n\n"
                f"💬 **Channel ID:** {channel_id}\n"
                f"🔗 **URL:** https://discord.com/channels/{channel_id}\n\n"
                f"Канал додано до моніторингу.",
                reply_markup=get_discord_projects_keyboard(user_id),
            )
        else:
            await update.message.reply_text(
                "❌ Помилка при додаванні проекту.",
                reply_markup=get_discord_projects_keyboard(user_id)
            )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Помилка: {str(e)}",
            reply_markup=get_discord_projects_keyboard(user_id)
        )
    
    # Очищаємо стан
    del user_states[user_id]

async def handle_admin_user_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник створення користувача адміністратором"""
    if not update.effective_user or not update.message or not update.message.text:
        return
    
    user_id = update.effective_user.id
    message_text = update.message.text.strip()
    state_data = user_states[user_id]['data']
    
    try:
        if state_data['step'] == 'telegram_id':
            # Перевіряємо чи це число
            if not message_text.isdigit():
                await update.message.reply_text(
                    "❌ **Неправильний формат!**\n\n"
                    "Telegram ID повинен бути числом.\n"
                    "Введіть Telegram ID ще раз:"
                )
                return
            
            telegram_id = int(message_text)
            
            # Перевіряємо чи користувач вже існує
            existing_user = access_manager.get_user_by_telegram_id(telegram_id)
            if existing_user:
                await update.message.reply_text(
                    f"❌ **Користувач вже існує!**\n\n"
                    f"Користувач з Telegram ID {telegram_id} вже зареєстрований в системі.\n"
                    f"Роль: {'Адміністратор' if existing_user.get('role') == 'admin' else 'Користувач'}\n\n"
                    f"Введіть інший Telegram ID:"
                )
                return
            
            # Зберігаємо Telegram ID та переходимо до наступного кроку
            state_data['telegram_id'] = telegram_id
            state_data['step'] = 'username'
            
            await update.message.reply_text(
                f"✅ **Telegram ID:** {telegram_id}\n\n"
                f"👤 **Введіть username користувача:**\n\n"
                f"💡 **Приклад:** JohnDoe\n"
                f"💡 **Примітка:** Username може бути порожнім"
            )
            
        elif state_data['step'] == 'username':
            # Зберігаємо username та переходимо до паролю
            username = message_text.strip()
            state_data['username'] = username
            state_data['step'] = 'password'
            
            await update.message.reply_text(
                f"✅ **Telegram ID:** {state_data['telegram_id']}\n"
                f"✅ **Username:** {username or 'Не вказано'}\n\n"
                f"🔐 **Введіть пароль користувача:**\n\n"
                f"💡 **Приклад:** mypassword123\n"
                f"💡 **Примітка:** Якщо залишити порожнім, буде використано пароль за замовчуванням"
            )
            
        elif state_data['step'] == 'password':
            # Зберігаємо пароль та створюємо користувача
            password = message_text.strip()
            
            # Створюємо користувача
            created_user_id = access_manager.add_user(
                state_data['telegram_id'],
                state_data['username'] or "Unknown",
                password or ""
            )
            
            if created_user_id:
                await update.message.reply_text(
                    f"🎉 **Користувач успішно створений!**\n\n"
                    f"👤 **Username:** {state_data['username'] or 'Не вказано'}\n"
                    f"🆔 **Telegram ID:** {state_data['telegram_id']}\n"
                    f"🔐 **Пароль:** {password or 'за замовчуванням'}\n"
                    f"👑 **Роль:** Користувач\n\n"
                    f"Користувач може увійти в систему командою /login",
                    reply_markup=get_admin_users_keyboard(),
                )
            else:
                await update.message.reply_text(
                    "❌ **Помилка створення користувача!**\n\n"
                    "Спробуйте ще раз.",
                    reply_markup=get_admin_users_keyboard()
                )
            
            # Очищаємо стан
            del user_states[user_id]
            
    except Exception as e:
        await update.message.reply_text(
            f"❌ **Помилка:** {str(e)}\n\n"
            f"Спробуйте ще раз.",
            reply_markup=get_admin_users_keyboard()
        )
        # Очищаємо стан при помилці
        if user_id in user_states:
            del user_states[user_id]

async def handle_admin_admin_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник створення адміністратора адміністратором"""
    if not update.effective_user or not update.message or not update.message.text:
        return
    
    user_id = update.effective_user.id
    message_text = update.message.text.strip()
    state_data = user_states[user_id]['data']
    
    try:
        if state_data['step'] == 'telegram_id':
            # Перевіряємо чи це число
            if not message_text.isdigit():
                await update.message.reply_text(
                    "❌ **Неправильний формат!**\n\n"
                    "Telegram ID повинен бути числом.\n"
                    "Введіть Telegram ID ще раз:"
                )
                return
            
            telegram_id = int(message_text)
            
            # Перевіряємо чи користувач вже існує
            existing_user = access_manager.get_user_by_telegram_id(telegram_id)
            if existing_user:
                await update.message.reply_text(
                    f"❌ **Користувач вже існує!**\n\n"
                    f"Користувач з Telegram ID {telegram_id} вже зареєстрований в системі.\n"
                    f"Роль: {'Адміністратор' if existing_user.get('role') == 'admin' else 'Користувач'}\n\n"
                    f"Введіть інший Telegram ID:"
                )
                return
            
            # Зберігаємо Telegram ID та переходимо до наступного кроку
            state_data['telegram_id'] = telegram_id
            state_data['step'] = 'username'
            
            await update.message.reply_text(
                f"✅ **Telegram ID:** {telegram_id}\n\n"
                f"👤 **Введіть username адміністратора:**\n\n"
                f"💡 **Приклад:** AdminJohn\n"
                f"💡 **Примітка:** Username може бути порожнім"
            )
            
        elif state_data['step'] == 'username':
            # Зберігаємо username та переходимо до паролю
            username = message_text.strip()
            state_data['username'] = username
            state_data['step'] = 'password'
            
            await update.message.reply_text(
                f"✅ **Telegram ID:** {state_data['telegram_id']}\n"
                f"✅ **Username:** {username or 'Не вказано'}\n\n"
                f"🔐 **Введіть пароль адміністратора:**\n\n"
                f"💡 **Приклад:** adminpass123\n"
                f"💡 **Примітка:** Якщо залишити порожнім, буде використано пароль за замовчуванням"
            )
            
        elif state_data['step'] == 'password':
            # Зберігаємо пароль та створюємо адміністратора
            password = message_text.strip()
            
            # Створюємо адміністратора
            created_user_id = access_manager.create_admin_user(
                state_data['telegram_id'],
                state_data['username'] or "Unknown",
                password or ""
            )
            
            if created_user_id:
                await update.message.reply_text(
                    f"🎉 **Адміністратор успішно створений!**\n\n"
                    f"👤 **Username:** {state_data['username'] or 'Не вказано'}\n"
                    f"🆔 **Telegram ID:** {state_data['telegram_id']}\n"
                    f"🔐 **Пароль:** {password or 'за замовчуванням'}\n"
                    f"👑 **Роль:** Адміністратор\n\n"
                    f"Адміністратор може увійти в систему командою /login",
                    reply_markup=get_admin_users_keyboard(),
                )
            else:
                await update.message.reply_text(
                    "❌ **Помилка створення адміністратора!**\n\n"
                    "Спробуйте ще раз.",
                    reply_markup=get_admin_users_keyboard()
                )
            
            # Очищаємо стан
            del user_states[user_id]
            
    except Exception as e:
        await update.message.reply_text(
            f"❌ **Помилка:** {str(e)}\n\n"
            f"Спробуйте ще раз.",
            reply_markup=get_admin_users_keyboard()
        )
        # Очищаємо стан при помилці
        if user_id in user_states:
            del user_states[user_id]

async def handle_admin_user_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник пошуку користувачів адміністратором"""
    if not update.effective_user or not update.message or not update.message.text:
        return
    
    user_id = update.effective_user.id
    message_text = update.message.text.strip()
    
    try:
        # Шукаємо користувачів
        results = access_manager.search_users(message_text)
        
        if not results:
            await update.message.reply_text(
                f"🔍 **Результати пошуку**\n\n"
                f"❌ Користувачів не знайдено за запитом: '{message_text}'\n\n"
                f"Спробуйте інший запит:",
                reply_markup=get_admin_users_keyboard()
            )
            return
        
        # Форматуємо результати
        results_text = f"🔍 **Результати пошуку** (знайдено: {len(results)})\n\n"
        
        for i, result in enumerate(results[:10], 1):  # Показуємо перших 10
            role_emoji = "👑" if result.get('role') == 'admin' else "👤"
            status_emoji = "✅" if result.get('is_active', True) else "❌"
            match_type = "username" if result.get('match_type') == 'username' else "Telegram ID"
            
            results_text += (
                f"{i}. {role_emoji} **{result.get('username', 'Без імені')}**\n"
                f"   🆔 ID: `{result.get('telegram_id')}`\n"
                f"   📊 Статус: {status_emoji}\n"
                f"   🔍 Знайдено за: {match_type}\n"
                f"   📅 Створено: {result.get('created_at', '')[:10]}\n\n"
            )
        
        if len(results) > 10:
            results_text += f"... та ще {len(results) - 10} результатів"
        
        await update.message.reply_text(
            results_text,
            reply_markup=get_admin_users_keyboard(),
        )
        
    except Exception as e:
        await update.message.reply_text(
            f"❌ **Помилка пошуку:** {str(e)}",
            reply_markup=get_admin_users_keyboard()
        )
    finally:
        # Очищаємо стан
        if user_id in user_states:
            del user_states[user_id]

async def handle_admin_user_deletion(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник видалення користувачів адміністратором"""
    if not update.effective_user or not update.message or not update.message.text:
        return
    
    user_id = update.effective_user.id
    message_text = update.message.text.strip()
    
    try:
        # Перевіряємо чи це число
        if not message_text.isdigit():
            await update.message.reply_text(
                "❌ **Неправильний формат!**\n\n"
                "Telegram ID повинен бути числом.\n"
                "Введіть Telegram ID ще раз:"
            )
            return
        
        target_telegram_id = int(message_text)
        
        # Перевіряємо чи користувач існує
        target_user = access_manager.get_user_by_telegram_id(target_telegram_id)
        if not target_user:
            await update.message.reply_text(
                f"❌ **Користувач не знайдений!**\n\n"
                f"Користувач з Telegram ID {target_telegram_id} не існує в системі.\n\n"
                f"Введіть інший Telegram ID:"
            )
            return
        
        # Перевіряємо чи не намагаємося видалити себе
        if target_telegram_id == user_id:
            await update.message.reply_text(
                "❌ **Неможливо видалити себе!**\n\n"
                "Ви не можете видалити власний акаунт.\n\n"
                "Введіть інший Telegram ID:"
            )
            return
        
        # Видаляємо користувача
        if access_manager.delete_user(target_telegram_id):
            username = target_user.get('username', 'Без імені')
            role = target_user.get('role', 'user')
            
            await update.message.reply_text(
                f"🗑️ **Користувач успішно видалений!**\n\n"
                f"👤 **Username:** {username}\n"
                f"🆔 **Telegram ID:** {target_telegram_id}\n"
                f"👑 **Роль:** {'Адміністратор' if role == 'admin' else 'Користувач'}\n\n"
                f"Користувач повністю видалений з системи.",
                reply_markup=get_admin_users_keyboard(),
            )
        else:
            await update.message.reply_text(
                "❌ **Помилка видалення користувача!**\n\n"
                "Спробуйте ще раз.",
                reply_markup=get_admin_users_keyboard()
            )
        
    except Exception as e:
        await update.message.reply_text(
            f"❌ **Помилка:** {str(e)}",
            reply_markup=get_admin_users_keyboard()
        )
    finally:
        # Очищаємо стан
        if user_id in user_states:
            del user_states[user_id]

async def handle_admin_role_change(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник зміни ролі користувача адміністратором"""
    if not update.effective_user or not update.message or not update.message.text:
        return
    
    user_id = update.effective_user.id
    message_text = update.message.text.strip()
    state_data = user_states[user_id]['data']
    
    try:
        if state_data['step'] == 'telegram_id':
            # Перевіряємо чи це число
            if not message_text.isdigit():
                await update.message.reply_text(
                    "❌ **Неправильний формат!**\n\n"
                    "Telegram ID повинен бути числом.\n"
                    "Введіть Telegram ID ще раз:"
                )
                return
            
            target_telegram_id = int(message_text)
            
            # Перевіряємо чи користувач існує
            target_user = access_manager.get_user_by_telegram_id(target_telegram_id)
            if not target_user:
                await update.message.reply_text(
                    f"❌ **Користувач не знайдений!**\n\n"
                    f"Користувач з Telegram ID {target_telegram_id} не існує в системі.\n\n"
                    f"Введіть інший Telegram ID:"
                )
                return
            
            # Зберігаємо дані та переходимо до вибору ролі
            state_data['target_telegram_id'] = target_telegram_id
            state_data['target_user'] = target_user
            state_data['step'] = 'new_role'
            
            current_role = target_user.get('role', 'user')
            current_role_text = "Адміністратор" if current_role == "admin" else "Користувач"
            
            await update.message.reply_text(
                f"✅ **Користувач знайдений:**\n\n"
                f"👤 **Username:** {target_user.get('username', 'Без імені')}\n"
                f"🆔 **Telegram ID:** {target_telegram_id}\n"
                f"👑 **Поточна роль:** {current_role_text}\n\n"
                f"🔄 **Виберіть нову роль:**\n\n"
                f"Введіть: 'admin' або 'user'",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Скасувати", callback_data="admin_users")
                ]])
            )
            
        elif state_data['step'] == 'new_role':
            new_role = message_text.lower().strip()
            
            if new_role not in ['admin', 'user']:
                await update.message.reply_text(
                    "❌ **Невірна роль!**\n\n"
                    "Доступні ролі: 'admin' або 'user'\n"
                    "Введіть роль ще раз:"
                )
                return
            
            target_telegram_id = state_data['target_telegram_id']
            target_user = state_data['target_user']
            
            # Змінюємо роль
            if access_manager.change_user_role(target_telegram_id, new_role):
                old_role_text = "Адміністратор" if target_user and target_user.get('role') == 'admin' else "Користувач"
                new_role_text = "Адміністратор" if new_role == 'admin' else "Користувач"
                
                await update.message.reply_text(
                    f"🔄 **Роль успішно змінена!**\n\n"
                    f"👤 **Username:** {target_user.get('username', 'Без імені') if target_user else 'Без імені'}\n"
                    f"🆔 **Telegram ID:** {target_telegram_id}\n"
                    f"👑 **Стара роль:** {old_role_text}\n"
                    f"👑 **Нова роль:** {new_role_text}\n\n"
                    f"Дозволи користувача оновлено автоматично.",
                    reply_markup=get_admin_users_keyboard(),
                )
            else:
                await update.message.reply_text(
                    "❌ **Помилка зміни ролі!**\n\n"
                    "Спробуйте ще раз.",
                    reply_markup=get_admin_users_keyboard()
                )
            
            # Очищаємо стан
            del user_states[user_id]
            
    except Exception as e:
        await update.message.reply_text(
            f"❌ **Помилка:** {str(e)}",
            reply_markup=get_admin_users_keyboard()
        )
        # Очищаємо стан при помилці
        if user_id in user_states:
            del user_states[user_id]

async def handle_admin_password_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник скидання паролю користувача адміністратором"""
    if not update.effective_user or not update.message or not update.message.text:
        return
    
    user_id = update.effective_user.id
    message_text = update.message.text.strip()
    state_data = user_states[user_id]['data']
    
    try:
        if state_data['step'] == 'telegram_id':
            # Перевіряємо чи це число
            if not message_text.isdigit():
                await update.message.reply_text(
                    "❌ **Неправильний формат!**\n\n"
                    "Telegram ID повинен бути числом.\n"
                    "Введіть Telegram ID ще раз:"
                )
                return
            
            target_telegram_id = int(message_text)
            
            # Перевіряємо чи користувач існує
            target_user = access_manager.get_user_by_telegram_id(target_telegram_id)
            if not target_user:
                await update.message.reply_text(
                    f"❌ **Користувач не знайдений!**\n\n"
                    f"Користувач з Telegram ID {target_telegram_id} не існує в системі.\n\n"
                    f"Введіть інший Telegram ID:"
                )
                return
            
            # Зберігаємо дані та переходимо до введення паролю
            state_data['target_telegram_id'] = target_telegram_id
            state_data['target_user'] = target_user
            state_data['step'] = 'new_password'
            
            await update.message.reply_text(
                f"✅ **Користувач знайдений:**\n\n"
                f"👤 **Username:** {target_user.get('username', 'Без імені')}\n"
                f"🆔 **Telegram ID:** {target_telegram_id}\n\n"
                f"🔐 **Введіть новий пароль:**\n\n"
                f"💡 **Примітка:** Якщо залишити порожнім, буде використано пароль за замовчуванням",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Скасувати", callback_data="admin_users")
                ]])
            )
            
        elif state_data['step'] == 'new_password':
            new_password = message_text.strip()
            target_telegram_id = state_data['target_telegram_id']
            target_user = state_data['target_user']
            
            # Скидаємо пароль
            if access_manager.reset_user_password(target_telegram_id, new_password or ""):
                password_text = new_password if new_password else "за замовчуванням"
                
                await update.message.reply_text(
                    f"🔐 **Пароль успішно скинуто!**\n\n"
                    f"👤 **Username:** {target_user.get('username', 'Без імені') if target_user else 'Без імені'}\n"
                    f"🆔 **Telegram ID:** {target_telegram_id}\n"
                    f"🔐 **Новий пароль:** {password_text}\n\n"
                    f"Користувач буде розлогінений з усіх пристроїв.",
                    reply_markup=get_admin_users_keyboard(),
                )
            else:
                await update.message.reply_text(
                    "❌ **Помилка скидання паролю!**\n\n"
                    "Спробуйте ще раз.",
                    reply_markup=get_admin_users_keyboard()
                )
            
            # Очищаємо стан
            del user_states[user_id]
            
    except Exception as e:
        await update.message.reply_text(
            f"❌ **Помилка:** {str(e)}",
            reply_markup=get_admin_users_keyboard()
        )
        # Очищаємо стан при помилці
        if user_id in user_states:
            del user_states[user_id]

async def handle_admin_system_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник скидання системи адміністратором"""
    if not update.effective_user or not update.message or not update.message.text:
        return
    
    user_id = update.effective_user.id
    message_text = update.message.text.strip()
    
    try:
        if message_text == "CONFIRM_RESET":
            # Підтверджуємо скидання системи
            if access_manager.reset_system():
                await update.message.reply_text(
                    f"⚠️ **СИСТЕМА СКИНУТА!**\n\n"
                    f"✅ **Виконано:**\n"
                    f"• Всіх користувачів видалено\n"
                    f"• Всі проекти видалено\n"
                    f"• Всі сесії очищено\n"
                    f"• Резервна копія створена\n\n"
                    f"👑 **Збережено:**\n"
                    f"• Всіх адміністраторів\n"
                    f"• Системні налаштування\n\n"
                    f"Система готова до нового використання.",
                    reply_markup=get_admin_system_keyboard(),
                )
            else:
                await update.message.reply_text(
                    "❌ **Помилка скидання системи!**\n\n"
                    "Спробуйте ще раз.",
                    reply_markup=get_admin_system_keyboard()
                )
        else:
            await update.message.reply_text(
                "❌ **Неправильне підтвердження!**\n\n"
                "Для підтвердження скидання системи введіть точно: **CONFIRM_RESET**\n\n"
                "⚠️ **УВАГА!** Ця дія незворотна!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Скасувати", callback_data="admin_system")
                ]])
            )
            return
        
    except Exception as e:
        await update.message.reply_text(
            f"❌ **Помилка:** {str(e)}",
            reply_markup=get_admin_system_keyboard()
        )
    finally:
        # Очищаємо стан
        if user_id in user_states:
            del user_states[user_id]

async def handle_admin_forward_select_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник вибору користувача для керування пересиланням"""
    if not update.effective_user or not update.message or not update.message.text:
        return
    
    admin_id = update.effective_user.id
    message_text = update.message.text.strip()
    try:
        if not message_text.isdigit():
            await update.message.reply_text("❌ Введіть числовий Telegram ID користувача:")
            return
        target_id = int(message_text)
        target_user = access_manager.get_user_by_telegram_id(target_id)
        if not target_user:
            await update.message.reply_text("❌ Користувача не знайдено. Введіть інший Telegram ID:")
            return
        # Зберігаємо і показуємо меню керування пересиланням
        user_states[admin_id] = {'state': 'admin_forward_set_user_menu', 'data': {'target_id': target_id}}
        status = project_manager.get_forward_status(target_id)
        enabled = status.get('enabled', False)
        channel = status.get('channel_id') or '—'
        await update.message.reply_text(
            f"🔁 Пересилання для користувача `{target_id}`\n\nСтатус: {'🟢 увімкнено' if enabled else '🔴 вимкнено'}\nКанал: `{channel}`",
            reply_markup=get_admin_forward_keyboard(target_id),
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка: {e}")

async def handle_admin_forward_set_channel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Адмін встановлює канал для вибраного користувача"""
    if not update.effective_user or not update.message or not update.message.text:
        return
    
    admin_id = update.effective_user.id
    message_text = update.message.text.strip()
    state = user_states.get(admin_id, {}).get('data', {})
    target_id = state.get('target_id')
    if not target_id:
        await update.message.reply_text("❌ Сесія втрачена. Поверніться в адмін-меню.")
        return
    # Підтримуємо 2 способи: текстовий ID або переслане повідомлення з каналу
    fwd_chat = getattr(update.message, 'forward_from_chat', None)
    if fwd_chat:
        channel_id_str = str(getattr(fwd_chat, 'id', ''))
    else:
        if not message_text:
            await update.message.reply_text("❌ Введіть ID каналу або перешліть повідомлення з нього.")
            return
        channel_id_str = message_text
    if project_manager.set_forward_channel(target_id, channel_id_str):
        await update.message.reply_text(
            f"✅ Канал збережено для {target_id}: {normalize_chat_id(channel_id_str)}",
            reply_markup=get_admin_forward_keyboard(target_id)
        )
    else:
        await update.message.reply_text("❌ Не вдалося зберегти канал.")

async def handle_twitter_adapter_addition(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник додавання Twitter Monitor Adapter акаунта"""
    if not update.effective_user or not update.message or not update.message.text:
        return
    
    user_id = update.effective_user.id
    username = update.message.text.strip().replace('@', '')
    
    try:
        # Додаємо до Twitter Monitor Adapter моніторингу
        if twitter_monitor_adapter:
            twitter_monitor_adapter.add_account(username)
        
        # Додаємо до проектного менеджера
        project_manager.add_selenium_account(username, user_id)
        
        # Синхронізуємо монітори після додавання
        sync_monitors_with_projects()
        
        await update.message.reply_text(
            f"✅ **Twitter Monitor Adapter акаунт успішно додано!**\n\n"
            f"🚀 **Username:** @{username}\n"
            f"🔗 **URL:** https://x.com/{username}\n\n"
            f"Акаунт додано до Twitter Monitor Adapter моніторингу.",
            reply_markup=get_twitter_adapter_accounts_keyboard(),
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Помилка: {str(e)}",
            reply_markup=get_twitter_adapter_accounts_keyboard()
        )
    
    # Очищаємо стан
    del user_states[user_id]

async def handle_channel_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник пінгу бота в каналі"""
    if not update.message or not update.message.chat:
        return
        
    try:
        # Отримуємо інформацію про канал
        channel_id = update.message.chat.id
        channel_title = update.message.chat.title or "Unknown Channel"
        
        # Отримуємо інформацію про користувача, який пінгнув
        if update.message.from_user:
            user_id = update.message.from_user.id
            username = update.message.from_user.username or update.message.from_user.first_name
            
            # Перевіряємо чи користувач авторизований (узгоджено з іншими перевірками)
            if not access_manager.is_authorized(user_id):
                # Відправляємо повідомлення в особисті повідомлення
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"🔒 Ви не авторизовані для налаштування пересилання.\n\nСпочатку авторизуйтесь в боті: @{context.bot.username}"
                    )
                except:
                    pass  # Якщо не можемо відправити в особисті повідомлення
                return
            
            # Встановлюємо канал для пересилання
            if project_manager.set_forward_channel(user_id, str(channel_id)):
                # Відправляємо підтвердження в канал
                safe_channel_title = escape_html(channel_title)
                safe_username = escape_html(username)
                
                confirmation_text = (
                    f"✅ **Канал налаштовано для пересилання!**\n\n"
                    f"📺 Канал: {safe_channel_title}\n"
                    f"👤 Налаштовано: @{safe_username}\n"
                    f"🔄 Статус: Увімкнено\n\n"
                    f"Тепер всі нові повідомлення з Discord проектів будуть автоматично пересилатися в цей канал."
                )
                
                await context.bot.send_message(
                    chat_id=normalize_chat_id(str(channel_id)),
                    text=confirmation_text,
                )
                
                # Відправляємо повідомлення в особисті повідомлення
                try:
                    await context.bot.send_message(
                        chat_id=normalize_chat_id(str(user_id)),
                        text=f"✅ Канал '{channel_title}' успішно налаштовано для пересилання сповіщень!"
                    )
                except:
                    pass
                    
                logger.info(f"Канал {channel_id} ({channel_title}) налаштовано для користувача {user_id}")
            else:
                # Відправляємо повідомлення про помилку в особисті повідомлення
                try:
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"❌ Помилка налаштування каналу '{channel_title}'. Спробуйте ще раз."
                    )
                except:
                    pass
        else:
            # Якщо не можемо визначити користувача
            await context.bot.send_message(
                chat_id=channel_id,
                text="❌ Не вдалося визначити користувача для налаштування пересилання."
            )
            
    except Exception as e:
        logger.error(f"Помилка обробки пінгу в каналі: {e}")
        try:
            await context.bot.send_message(
                chat_id=update.message.chat.id,
                text="❌ Помилка налаштування пересилання. Спробуйте ще раз."
            )
        except:
            pass

async def handle_discord_history(update: Update, context: ContextTypes.DEFAULT_TYPE, count: int) -> None:
    """Обробник перегляду історії Discord"""
    if not update.callback_query or not update.effective_user:
        return
        
    query = update.callback_query
    user_id = update.effective_user.id
    
    if user_id not in user_states or user_states[user_id]['state'] != 'viewing_history':
        await query.edit_message_text("❌ Помилка: стан сесії втрачено.", reply_markup=get_main_menu_keyboard(user_id))
        return
    
    project = user_states[user_id]['data']['project']
    
    # Показуємо завантаження
    await query.edit_message_text(f"📥 Завантаження останніх {count} повідомлень з каналу {project['name']}...")
    
    try:
        # Отримуємо повідомлення з Discord
        messages = await get_discord_messages_history(project['url'], count)
        
        if not messages:
            await query.edit_message_text(
                f"📜 Історія каналу: {project['name']}\n\n❌ Не вдалося отримати повідомлення.\nМожливо, немає доступу до каналу або канал порожній.",
                reply_markup=get_main_menu_keyboard(user_id)
            )
        else:
            # Форматуємо повідомлення
            history_text = format_discord_history(messages, project['name'], count)
            
            # Розбиваємо на частини якщо текст занадто довгий
            if len(history_text) > 4000:
                # Telegram має ліміт на довжину повідомлення
                parts = [history_text[i:i+4000] for i in range(0, len(history_text), 4000)]
                for i, part in enumerate(parts):
                    if i == 0:
                        await query.edit_message_text(part)
                    else:
                        await context.bot.send_message(chat_id=user_id, text=part)
            else:
                await query.edit_message_text(history_text, reply_markup=get_main_menu_keyboard(user_id))
                
    except Exception as e:
        logger.error(f"Помилка отримання історії Discord: {e}")
        await query.edit_message_text(
            f"❌ Помилка при отриманні історії каналу {project['name']}:\n{str(e)}",
            reply_markup=get_main_menu_keyboard(user_id)
        )
    finally:
        # Очищуємо стан користувача
        if user_id in user_states:
            del user_states[user_id]

async def get_discord_messages_history(channel_url: str, limit: int) -> List[Dict]:
    """Отримати історію повідомлень з Discord каналу"""
    if not DISCORD_AUTHORIZATION:
        return []
    
    try:
        # Парсимо URL для отримання channel_id
        import re
        match = re.search(r'discord\.com/channels/(\d+)/(\d+)', channel_url)
        if not match:
            return []
        
        channel_id = match.group(2)
        
        # Створюємо новий session для цього запиту
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://discord.com/api/v9/channels/{channel_id}/messages?limit={limit}",
                headers={
                    'Authorization': DISCORD_AUTHORIZATION,
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }
            ) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    logger.error(f"Помилка отримання повідомлень: {response.status}")
                    return []
                
    except Exception as e:
        logger.error(f"Помилка в get_discord_messages_history: {e}")
        return []

def format_discord_history(messages: List[Dict], channel_name: str, count: int) -> str:
    """Форматувати історію повідомлень Discord"""
    from datetime import datetime
    
    header = f"📜 **Історія каналу: {channel_name}**\n"
    header += f"📊 Останні {count} повідомлень:\n\n"
    
    if not messages:
        return header + "❌ Повідомлення не знайдено."
    
    formatted_messages = []
    for i, message in enumerate(messages, 1):
        author = message.get('author', {}).get('username', 'Unknown')
        content = message.get('content', '')
        timestamp = message.get('timestamp', '')
        
        # Форматуємо час
        try:
            if timestamp:
                dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                time_str = dt.strftime('%d.%m.%Y %H:%M')
            else:
                time_str = 'Unknown time'
        except:
            time_str = 'Unknown time'
        
        # Обмежуємо довжину повідомлення
        if len(content) > 200:
            content = content[:200] + "..."
        
        formatted_msg = f"**{i}.** 👤 {author} | 🕒 {time_str}\n"
        if content:
            formatted_msg += f"💬 {content}\n"
        formatted_msg += "─" * 30 + "\n"
        
        formatted_messages.append(formatted_msg)
    
    return header + "\n".join(formatted_messages)

def handle_discord_notifications_sync(new_messages: List[Dict]) -> None:
    """Обробник нових повідомлень Discord з підтримкою thread'ів та тегів"""
    global bot_instance
    
    if not bot_instance:
        return
        
    try:
        logger.info(f"📨 handle_discord_notifications_sync: отримано {len(new_messages)} Discord повідомлень для обробки")
        
        # Кеші для оптимізації
        channel_to_tracked_data: Dict[str, List[Dict]] = {}
        user_to_forward_channel: Dict[int, str] = {}
        
        # Обробляємо кожне повідомлення МИТТЄВО
        for message in new_messages:
            try:
                message_id = message.get('message_id', '')
                channel_id = message.get('channel_id', '')
            
                # Красиве форматування
                author = escape_html(message['author'])
                content = escape_html(message['content'])
                
                # Обрізаємо текст якщо він занадто довгий
                if len(content) > 200:
                    content = content[:200] + "..."
                
                # Форматуємо дату
                timestamp = message.get('timestamp', '')
                formatted_date = "Не відомо"
                time_ago = ""
                
                if timestamp:
                    try:
                        from datetime import datetime
                        dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                        formatted_date = dt.strftime("%d %B, %H:%M UTC")
                        time_ago = _get_time_ago(dt)
                    except:
                        formatted_date = timestamp[:19] if len(timestamp) > 19 else timestamp
                
                # Отримуємо інформацію про сервер з URL
                server_name = "Discord"
                guild_id = ""
                try:
                    # Спробуємо витягти guild_id з URL
                    url_parts = message['url'].split('/')
                    if len(url_parts) >= 5:
                        guild_id = url_parts[4]
                        # Отримуємо назву сервера з проекту користувача
                        server_name = get_discord_server_name(channel_id, guild_id)
                        logger.info(f"🏷️ Discord сервер для каналу {channel_id}: {server_name}")
                except Exception as e:
                    logger.error(f"Помилка отримання назви сервера: {e}")
                    pass
                
                # Отримуємо зображення з повідомлення
                images = message.get('images', [])
                
                # Отримуємо всіх користувачів та проекти, які відстежують цей Discord канал
                if channel_id in channel_to_tracked_data:
                    tracked_data = channel_to_tracked_data[channel_id]
                else:
                    tracked_data = get_users_tracking_discord_channel(channel_id)
                    channel_to_tracked_data[channel_id] = tracked_data

                # Додаємо детальне логування для діагностики
                logger.info(f"🔍 Discord канал {channel_id}: знайдено {len(tracked_data)} проектів")
                for item in tracked_data:
                    logger.info(f"   📋 Проект: {item['project']['name']} (користувач: {item['user_id']})")

                if not tracked_data:
                    logger.warning(f"🚫 Discord канал {channel_id}: немає проектів, що відстежують цей канал")
                    continue
                
                logger.info(f"✅ Обробляємо Discord повідомлення {message_id} для {len(tracked_data)} проектів")

                # Не дублювати відправку в одну гілку
                sent_targets: Set[str] = set()

                for tracked_item in tracked_data:
                    try:
                        user_id = tracked_item['user_id']
                        project = tracked_item['project']
                        project_id = project.get('id')
                        project_name = project.get('name', 'Discord Project')
                        project_tag = project.get('tag', f"#ds_project_{project_id}")
                        # Швидка перевірка каналу пересилання
                        if user_id in user_to_forward_channel:
                            forward_channel = user_to_forward_channel[user_id]
                        else:
                            forward_channel = project_manager.get_forward_channel(user_id)
                            user_to_forward_channel[user_id] = forward_channel
                        if not forward_channel:
                            logger.warning(f"🚫 Користувач {user_id} не має налаштованого каналу для пересилання")
                            logger.warning(f"💡 Підказка: налаштуйте канал пересилання командою /forward_set_channel")
                            continue
                        logger.info(f"✅ Користувач {user_id} має канал пересилання: {forward_channel}")
                        # Очищаємо канал від зайвих символів
                        clean_channel = forward_channel.split('/')[0] if '/' in forward_channel else forward_channel
                        # Перевіряємо чи використовуються thread'и
                        forward_status = project_manager.get_forward_status(user_id)
                        use_threads = forward_status.get('use_threads', True)
                        # Формуємо унікальний ключ для цього повідомлення і проекту
                        forward_key = f"discord_{channel_id}_{message_id}_{project_id}"
                        if use_threads:
                            # Робота з thread'ами
                            thread_id = project_manager.get_project_thread(user_id, project_id)
                            logger.info(f"🔍 Перевіряємо Discord thread для проекту {project_name}: thread_id = {thread_id}")
                            if not thread_id:
                                # Створюємо новий thread
                                logger.info(f"🔧 Створюємо новий Discord thread для проекту {project_name} в каналі {clean_channel}")
                                thread_id = create_project_thread_sync(BOT_TOKEN, clean_channel, project_name, project_tag, str(user_id))
                                if thread_id:
                                    project_manager.set_project_thread(user_id, project_id, thread_id)
                                    logger.info(f"✅ Створено Discord thread {thread_id} для проекту {project_name}")
                                else:
                                    logger.warning(f"⚠️ Не вдалося створити Discord thread для проекту {project_name}")
                                    logger.info(f"🔄 Перемикаємося на режим відправки з тегами замість threads")
                                    # Перемикаємося на режим з тегами
                                    use_threads = False
                            else:
                                logger.info(f"✅ Використовується існуючий Discord thread {thread_id} для проекту {project_name}")
                            # Унікальний ключ для thread'а
                            thread_key = f"{clean_channel}_{thread_id}"
                            if thread_key in sent_targets:
                                continue
                            if project_manager.is_message_sent(forward_key, clean_channel, user_id):
                                continue
                            # Формуємо пінги для всіх user_id із ping_users
                            ping_users = project_manager.get_project_ping_users(user_id, project_id)
                            ping_mentions = ""
                            if ping_users:
                                ping_mentions = " ".join([f'<a href="tg://user?id={uid}">@{uid}</a>' for uid in ping_users])
                            # Формуємо гіперпосилання на власника проекту
                            user_mention = f'<a href="tg://user?id={user_id}">Користувач</a>'
                            # Формуємо правильний Discord url
                            discord_url = message.get('url')
                            # Якщо url не містить server_id, будуємо вручну
                            if discord_url and '/channels/' in discord_url:
                                url_parts = discord_url.split('/')
                                if len(url_parts) >= 7:
                                    server_id = url_parts[4]
                                    channel_id = url_parts[5]
                                    message_id = url_parts[6]
                                else:
                                    server_id = guild_id or ''
                                    channel_id = channel_id
                                    message_id = message.get('message_id', '')
                                discord_url = f"https://discord.com/channels/{server_id}/{channel_id}/{message_id}"
                            else:
                                # fallback: будуємо з guild_id, channel_id, message_id
                                discord_url = f"https://discord.com/channels/{guild_id}/{channel_id}/{message.get('message_id','')}"
                            # Формуємо повідомлення у стилі Twitter + пінги
                            forward_text = (
                                f"💬 <b>Нове повідомлення з Discord</b>\n"
                                f"• Проект: {project_name}\n"
                                f"• Сервер: {server_name}\n"
                                f"• Автор: {author} | {user_mention}"
                            )
                            if ping_mentions:
                                forward_text += f"\n• Пінг: {ping_mentions}"
                            forward_text += (
                                f"\n• Дата: {formatted_date} ({time_ago})\n"
                                f"• Текст: {content}\n"
                                f'🔗 {discord_url}'
                            )
                            if images:
                                forward_text += f"\n📷 Зображень: {len(images)}"
                            logger.info(f"📤 Відправляємо Discord повідомлення в thread {thread_id} для проекту {project_name} в канал {clean_channel}")
                            # Відправляємо повідомлення в thread
                            success = send_message_to_thread_sync(BOT_TOKEN, clean_channel, thread_id, forward_text, project_tag)
                            logger.info(f"📊 Результат відправки Discord повідомлення в thread {thread_id}: success = {success}")
                            if success:
                                # Відправляємо зображення в thread якщо є
                                if images:
                                    for i, image_url in enumerate(images[:5]):  # Максимум 5 зображень
                                        try:
                                            image_caption = f"📷 Discord зображення {i+1}/{len(images)}" if len(images) > 1 else "📷 Discord зображення"
                                            send_photo_to_thread_sync(BOT_TOKEN, clean_channel, thread_id, image_url, image_caption, project_tag)
                                            import time
                                            time.sleep(1)
                                        except Exception as e:
                                            logger.error(f"Помилка відправки Discord зображення в thread: {e}")
                                project_manager.add_sent_message(forward_key, clean_channel, user_id)
                                sent_targets.add(thread_key)
                                logger.info(f"✅ Переслано в thread {thread_id} проекту {project_name}")
                            else:
                                logger.error(f"❌ Помилка відправки в thread {thread_id}")
                        else:
                            # Стара логіка - відправка в основний канал з тегом
                            target_key = f"{clean_channel}_{project_tag}"
                            if target_key in sent_targets:
                                continue
                            if project_manager.is_message_sent(forward_key, clean_channel, user_id):
                                continue
                            # Формуємо повідомлення з тегом
                            user_mention = f'<a href="tg://user?id={user_id}">Користувач</a>'
                            forward_text = (
                                f"{project_tag}\n\n"
                                f"💬 <b>Нове повідомлення з Discord</b>\n"
                                f"• Проект: {project_name}\n"
                                f"• Сервер: {server_name}\n"
                                f"• Автор: {author} | {user_mention}\n"
                                f"• Дата: {formatted_date} ({time_ago})\n"
                                f"• Текст: {content}\n"
                                f'🔗 {message["url"]}'
                            )
                            if images:
                                forward_text += f"\n📷 Зображень: {len(images)}"
                            logger.info(f"📤 Відправляємо Discord повідомлення з тегом {project_tag} в канал {clean_channel}")
                            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
                            data = {
                                'chat_id': normalize_chat_id(clean_channel),
                                'text': forward_text,
                                'parse_mode': 'HTML',
                            }
                            response = requests.post(url, data=data, timeout=3)
                            if response.status_code == 200:
                                # Відправляємо зображення з тегом якщо є
                                if images:
                                    for i, image_url in enumerate(images[:5]):
                                        try:
                                            image_caption = f"{project_tag} 📷 Discord зображення {i+1}/{len(images)}" if len(images) > 1 else f"{project_tag} 📷 Discord зображення"
                                            download_and_send_image(image_url, clean_channel, image_caption)
                                            import time
                                            time.sleep(1)
                                        except Exception as e:
                                            logger.error(f"Помилка відправки Discord зображення: {e}")
                                project_manager.add_sent_message(forward_key, clean_channel, user_id)
                                sent_targets.add(target_key)
                                logger.info(f"✅ Переслано в канал {clean_channel} з тегом {project_tag}")
                            else:
                                logger.error(f"❌ Помилка відправки в канал {clean_channel}: {response.status_code}")
                    except Exception as e:
                        logger.error(f"Помилка обробки Discord проекту користувача {user_id}: {e}")
                    
            except Exception as e:
                logger.error(f"Помилка обробки Discord повідомлення {message_id}: {e}")
                    
    except Exception as e:
        logger.error(f"Помилка обробки Discord сповіщень: {e}")

def handle_twitter_notifications_sync(new_tweets: List[Dict]) -> None:
    """Обробник нових твітів Twitter (оптимізована версія)"""
    global bot_instance, global_sent_tweets
    
    if not bot_instance:
        return
        
    try:
        # Швидка обробка твітів
        logger.info(f"📨 handle_twitter_notifications_sync: отримано {len(new_tweets)} твітів для обробки")
        for tweet in new_tweets:
            tweet_id = tweet.get('tweet_id', '')
            account = tweet.get('account', '')
            logger.info(f"🔍 Обробляємо твіт {tweet_id} від {account}")
            
            # Отримуємо всіх користувачів та проекти, які відстежують цей Twitter акаунт
            tracked_data = get_users_tracking_twitter(account)
            
            # ВАЖЛИВО: Якщо немає проектів які відстежують цей акаунт - пропускаємо твіт
            if not tracked_data:
                logger.warning(f"🚫 Твіт від {account} пропущено - акаунт не додано до жодного проекту")
                continue
            
            logger.info(f"✅ Знайдено {len(tracked_data)} проектів для акаунта {account}")
            
            # Фільтруємо тільки користувачів з налаштованим пересиланням
            users_with_forwarding: List[Dict] = []
            for tracked_item in tracked_data:
                user_id = tracked_item['user_id']
                forward_channel = project_manager.get_forward_channel(user_id)
                logger.info(f"🔍 Перевіряємо користувача {user_id}: forward_channel = {forward_channel}")
                if forward_channel:
                    users_with_forwarding.append(tracked_item)
                    logger.info(f"✅ Користувач {user_id} має налаштоване пересилання в канал {forward_channel}")
                else:
                    logger.warning(f"⚠️ Користувач {user_id} не має налаштованого каналу пересилання")
            
            if not users_with_forwarding:
                logger.warning(f"🚫 Твіт від {account} пропущено - немає користувачів з налаштованим пересиланням")
                logger.warning(f"💡 Підказка: налаштуйте канал пересилання командою /forward_set_channel або через меню бота")
                continue
            
            logger.info(f"✅ Знайдено {len(users_with_forwarding)} користувачів з налаштованим пересиланням для акаунта {account}")

            # Глобальна перевірка дублікатів
            if account not in global_sent_tweets:
                global_sent_tweets[account] = set()
            
            # Перевіряємо чи цей твіт вже був відправлений глобально
            if tweet_id in global_sent_tweets[account]:
                logger.info(f"Твіт {tweet_id} для {account} вже був відправлений, пропускаємо")
                continue
            
            # Додаткова перевірка за контентом (для випадків коли ID може змінюватися)
            tweet_text = tweet.get('text', '').strip()
            # Спочатку перевіряємо чи є готовий content_key з Twitter Monitor Adapter
            content_key = tweet.get('content_key')
            if not content_key and tweet_text:
                # Створюємо хеш контенту для додаткової перевірки
                import hashlib
                content_hash = hashlib.md5(f"{account}_{tweet_text}".encode('utf-8')).hexdigest()[:12]
                content_key = f"content_{content_hash}"
                
            if content_key and content_key in global_sent_tweets[account]:
                logger.info(f"Контент твіта для {account} вже був відправлений, пропускаємо")
                continue
            
            # ВАЖЛИВО: НЕ додаємо твіт до відправлених ТУТ - тільки після успішної відправки!
            
            # Додаємо затримку між обробкою твітів для уникнення rate limit
            import time
            time.sleep(10)  # Збільшено для уникнення rate limit
            
            # Красиве форматування
            author = escape_html(tweet.get('author', 'Unknown'))
            text = escape_html(tweet.get('text', ''))
            
            # Обрізаємо текст якщо він занадто довгий
            if len(text) > 200:
                text = text[:200] + "..."
            
            # Форматуємо дату
            timestamp = tweet.get('timestamp', '')
            formatted_date = "Не відомо"
            time_ago = ""
            
            if timestamp:
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    formatted_date = dt.strftime("%d %B, %H:%M UTC")
                    time_ago = _get_time_ago(dt)
                except:
                    formatted_date = timestamp[:19] if len(timestamp) > 19 else timestamp
            
            # Отримуємо зображення з твіта
            images = tweet.get('images', [])
            
            # --- Додаємо пінги ---
            ping_users = project_manager.get_project_ping_users(user_id, project_id) if 'project_id' in locals() else []
            ping_mentions = " ".join([f'<a href="tg://user?id={uid}">@{uid}</a>' for uid in ping_users]) if ping_users else ""
            forward_text = (
                f"🐦 <b>Новий твіт з Twitter</b>\n"
                f"• Профіль: @{account}\n"
                f"• Автор: {author}\n"
            )
            if ping_mentions:
                forward_text += f"• Пінг: {ping_mentions}\n"
            forward_text += (
                f"• Дата: {formatted_date} ({time_ago})\n"
                f"• Текст: {text}\n"
                f'🔗 {tweet.get("url", "")}'
            )
            # Додаємо інформацію про зображення якщо є
            if images:
                forward_text += f"\n📷 Зображень: {len(images)}"
            
            # Не дублювати відправку в одну гілку
            sent_targets: Set[str] = set()
            
            # Флаг для відстеження чи був твіт успішно відправлений хоча б одному користувачу
            tweet_successfully_sent = False

            # Паралельна обробка користувачів групами по 3
            batch_size = 3
            for i in range(0, len(users_with_forwarding), batch_size):
                batch = users_with_forwarding[i:i + batch_size]
                
                # Обробляємо групу користувачів
                for tracked_item in batch:
                    try:
                        user_id = tracked_item['user_id']
                        project = tracked_item['project']
                        project_id = project.get('id')
                        project_name = project.get('name', 'Twitter Project')
                        project_tag = project.get('tag', f"#tw_project_{project_id}")
                        
                        # Швидка перевірка каналу пересилання
                        forward_channel = project_manager.get_forward_channel(user_id)
                        if not forward_channel:
                            continue
                        
                        # Очищаємо канал від зайвих символів
                        clean_channel = forward_channel.split('/')[0] if '/' in forward_channel else forward_channel
                        
                        # Перевіряємо чи використовуються thread'и
                        forward_status = project_manager.get_forward_status(user_id)
                        use_threads = forward_status.get('use_threads', True)
                        
                        # Формуємо унікальний ключ для цього твіта і проекту
                        forward_key = f"twitter_{account}_{tweet_id}_{project_id}"
                        
                        if use_threads:
                            # Робота з thread'ами
                            thread_id = project_manager.get_project_thread(user_id, project_id)
                            logger.info(f"🔍 Перевіряємо thread для проекту {project_name}: thread_id = {thread_id}")
                            
                            if not thread_id:
                                # Створюємо новий thread
                                logger.info(f"🔧 Створюємо новий thread для проекту {project_name} в каналі {clean_channel}")
                                thread_id = create_project_thread_sync(BOT_TOKEN, clean_channel, project_name, project_tag, str(user_id))
                                
                                if thread_id:
                                    project_manager.set_project_thread(user_id, project_id, thread_id)
                                    logger.info(f"✅ Створено thread {thread_id} для проекту {project_name}")
                                else:
                                    logger.warning(f"⚠️ Не вдалося створити thread для проекту {project_name} в каналі {clean_channel}")
                                    logger.info(f"🔄 Перемикаємося на режим відправки з тегами замість threads")
                                    # Перемикаємося на режим з тегами
                                    use_threads = False
                            else:
                                logger.info(f"✅ Використовується існуючий thread {thread_id} для проекту {project_name}")
                            
                            # Унікальний ключ для thread'а
                            thread_key = f"{clean_channel}_{thread_id}"
                            if thread_key in sent_targets:
                                continue
                            
                            if project_manager.is_message_sent(forward_key, clean_channel, user_id):
                                continue
                            
                            # Формуємо повідомлення для thread'а з пінгуванням user_id
                            ping_users = project_manager.get_project_ping_users(user_id, project_id)
                            ping_mentions = " ".join([f'<a href="tg://user?id={uid}">@{uid}</a>' for uid in ping_users]) if ping_users else ""
                            user_mention = f'<a href="tg://user?id={user_id}">Користувач</a>'
                            thread_forward_text = (
                                f"🐦 <b>Новий твіт з Twitter</b> 👤 {user_mention}\n"
                                f"• Проект: {project_name}\n"
                                f"• Профіль: @{account}\n"
                                f"• Автор: {author}\n"
                            )
                            if ping_mentions:
                                thread_forward_text += f"• Пінг: {ping_mentions}\n"
                            thread_forward_text += (
                                f"• Дата: {formatted_date} ({time_ago})\n"
                                f"• Текст: {text}\n"
                                f'🔗 {tweet.get("url", "")}'
                            )
                            if images:
                                thread_forward_text += f"\n📷 Зображень: {len(images)}"
                            
                            logger.info(f"📤 Відправляємо Twitter твіт в thread {thread_id} для проекту {project_name} в канал {clean_channel}")
                            
                            # Відправляємо повідомлення з фотографіями в одному повідомленні
                            if images:
                                logger.info(f"📷 Знайдено {len(images)} зображень, відправляємо в одному повідомленні")
                                success = send_message_with_photos_to_thread_sync(BOT_TOKEN, clean_channel, thread_id, thread_forward_text, images, project_tag)
                            else:
                                # Якщо немає зображень, відправляємо звичайне повідомлення
                                logger.info(f"📝 Відправляємо текстове повідомлення в thread {thread_id}")
                                success = send_message_to_thread_sync(BOT_TOKEN, clean_channel, thread_id, thread_forward_text, project_tag)
                            
                            logger.info(f"📊 Результат відправки в thread {thread_id}: success = {success}")
                            
                            if success:
                                project_manager.add_sent_message(forward_key, clean_channel, user_id)
                                sent_targets.add(thread_key)
                                tweet_successfully_sent = True
                                logger.info(f"✅ Переслано Twitter твіт in thread {thread_id} проекту {project_name}")
                            else:
                                logger.error(f"❌ Помилка відправки Twitter твіта в thread {thread_id}")
                        else:
                            # Стара логіка - відправка в основний канал з тегом
                            target_key = f"{clean_channel}_{project_tag}"
                            if target_key in sent_targets:
                                continue
                            
                            if project_manager.is_message_sent(forward_key, clean_channel, user_id):
                                continue
                            
                            # Формуємо повідомлення з тегом і пінгами
                            ping_users = project_manager.get_project_ping_users(user_id, project_id)
                            ping_mentions = " ".join([f'<a href="tg://user?id={uid}">@{uid}</a>' for uid in ping_users]) if ping_users else ""
                            tagged_forward_text = (
                                f"{project_tag}\n\n"
                                f"🐦 **Новий твіт з Twitter**\n"
                                f"• Проект: {project_name}\n"
                                f"• Профіль: @{account}\n"
                                f"• Автор: {author}\n"
                            )
                            if ping_mentions:
                                tagged_forward_text += f"• Пінг: {ping_mentions}\n"
                            tagged_forward_text += (
                                f"• Дата: {formatted_date} ({time_ago})\n"
                                f"• Текст: {text}\n"
                                f"🔗 {tweet.get('url', '')}"
                            )
                            if images:
                                tagged_forward_text += f"\n📷 Зображень: {len(images)}"
                            
                            logger.info(f"📤 Відправляємо Twitter твіт з тегом {project_tag} в канал {clean_channel}")
                            
                            url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
                            data = {
                                'chat_id': normalize_chat_id(clean_channel),
                                'text': tagged_forward_text,
                            }
                            response = requests.post(url, data=data, timeout=3)
                            
                            if response.status_code == 200:
                                # Відправляємо зображення з тегом якщо є
                                if images:
                                    logger.info(f"📷 Знайдено {len(images)} зображень для відправки в канал {clean_channel}")
                                    for i, image_url in enumerate(images[:5]):
                                        try:
                                            image_caption = f"{project_tag} 📷 Twitter зображення {i+1}/{len(images)}" if len(images) > 1 else f"{project_tag} 📷 Twitter зображення"
                                            success = download_and_send_image(image_url, clean_channel, image_caption)
                                            if success:
                                                logger.info(f"✅ Зображення {i+1} успішно відправлено з тегом {project_tag}")
                                            else:
                                                logger.warning(f"⚠️ Не вдалося відправити зображення {i+1}")
                                            import time
                                            time.sleep(0.5)  # Зменшено затримку
                                        except Exception as e:
                                            logger.error(f"Помилка відправки Twitter зображення: {e}")
                                
                                project_manager.add_sent_message(forward_key, clean_channel, user_id)
                                sent_targets.add(target_key)
                                tweet_successfully_sent = True
                                logger.info(f"✅ Переслано Twitter твіт в канал {clean_channel} з тегом {project_tag}")
                            else:
                                logger.error(f"❌ Помилка відправки Twitter твіта в канал {clean_channel}: {response.status_code}")
                    
                    except Exception as e:
                        logger.error(f"Помилка обробки Twitter проекту користувача {user_id}: {e}")
                
                # Невелика затримка між групами користувачів
                if i + batch_size < len(users_with_forwarding):
                    import time
                    time.sleep(0.2)  # 0.2 секунди між групами
            
            # ТІЛЬКИ ПІСЛЯ УСПІШНОЇ ВІДПРАВКИ хоча б одному користувачу додаємо твіт до глобального списку
            if tweet_successfully_sent:
                global_sent_tweets[account].add(tweet_id)
                if content_key:
                    global_sent_tweets[account].add(content_key)
                logger.info(f"📝 Твіт {tweet_id} додано до списку відправлених для акаунта {account}")
                
                # Також додаємо до Twitter Monitor Adapter якщо він використовується
                global twitter_monitor_adapter, twitter_monitor
                if twitter_monitor_adapter:
                    try:
                        twitter_monitor_adapter.mark_tweet_as_sent(account, tweet_id, content_key)
                        logger.debug(f"Твіт {tweet_id} відмічено як відправлений в Twitter Monitor Adapter")
                    except Exception as e:
                        logger.error(f"Помилка відмітки твіта в Twitter Monitor Adapter: {e}")
                
                # Також додаємо до звичайного Twitter Monitor якщо він використовується  
                if twitter_monitor:
                    try:
                        twitter_monitor.mark_tweet_as_sent(account, tweet_id, content_key)
                        twitter_monitor.save_seen_tweets()  # Зберігаємо зміни
                        logger.debug(f"Твіт {tweet_id} відмічено як відправлений в Twitter Monitor")
                    except Exception as e:
                        logger.error(f"Помилка відмітки твіта в Twitter Monitor: {e}")
                
                # Зберігаємо зміни в Twitter Monitor Adapter
                if twitter_monitor_adapter:
                    try:
                        twitter_monitor_adapter.save_seen_tweets()
                        logger.debug(f"Збережено зміни в Twitter Monitor Adapter")
                    except Exception as e:
                        logger.error(f"Помилка збереження в Twitter Monitor Adapter: {e}")
                
                # Періодично очищуємо старі твіти
                if len(global_sent_tweets[account]) % 50 == 0:  # Кожні 50 твітів
                    cleanup_old_tweets()
            else:
                logger.warning(f"⚠️ Твіт {tweet_id} НЕ додано до списку відправлених - жодна відправка не була успішною")
                    
    except Exception as e:
        logger.error(f"Помилка обробки Twitter сповіщень: {e}")

async def start_discord_monitoring():
    """Запустити моніторинг Discord"""
    global discord_monitor
    
    if not DISCORD_AUTHORIZATION:
        logger.warning("Discord authorization токен не налаштовано - пропускаємо Discord моніторинг")
        return
        
    if not discord_monitor:
        logger.warning("Discord монітор не ініціалізовано - пропускаємо Discord моніторинг")
        return
        
    try:
        async with discord_monitor:
            # Додаємо всі Discord канали з проектів користувачів
            for user_id, projects in project_manager.data['projects'].items():
                for project in projects:
                    if project['platform'] == 'discord':
                        discord_monitor.add_channel(project['url'])
                        
            channels_list = list(getattr(discord_monitor, 'channels', []))
            logger.info(f"💬 Запуск Discord моніторингу для каналів: {channels_list}")
            logger.info("🔄 Discord моніторинг активний та працює в фоновому режимі...")
            await discord_monitor.start_monitoring(handle_discord_notifications_sync, 10)
            
    except Exception as e:
        logger.error(f"Помилка моніторингу Discord: {e}")

async def start_twitter_monitoring():
    """Запустити моніторинг Twitter з покращеним HTML парсингом"""
    global twitter_monitor
    
    if not twitter_monitor or not TWITTER_AUTH_TOKEN:
        logger.warning("Twitter auth_token не налаштовано")
        return
        
    try:
        async with twitter_monitor:
            # Додаємо всі Twitter акаунти з проектів користувачів
            for user_id, projects in project_manager.data['projects'].items():
                for project in projects:
                    if project['platform'] == 'twitter':
                        username = extract_twitter_username(project['url'])
                        if username:
                            twitter_monitor.add_account(username)
                            
            accounts_list = list(twitter_monitor.monitoring_accounts)
            logger.info(f"🐦 Запуск Twitter API моніторингу для акаунтів: {accounts_list}")
            logger.info("🔄 Twitter моніторинг активний та працює в фоновому режимі...")
            
            # Запускаємо власний цикл моніторингу з HTML парсингом
            while True:
                try:
                    # Отримуємо нові твіти через покращений HTML парсинг
                    new_tweets = await twitter_monitor.check_new_tweets()
                    
                    if new_tweets:
                        # Обробляємо кожен твіт ОДРАЗУ після знаходження
                        for tweet in new_tweets:
                            try:
                                # Конвертуємо формат для сумісності з існуючим кодом
                                formatted_tweet = {
                                    'tweet_id': tweet.get('id', ''),
                                    'account': tweet.get('user', {}).get('screen_name', ''),
                                    'author': tweet.get('user', {}).get('name', ''),
                                    'text': tweet.get('text', ''),
                                    'url': tweet.get('url', ''),
                                    'timestamp': tweet.get('created_at', '')
                                }
                                
                                # МИТТЄВО відправляємо кожен твіт (масив з 1 елементом)
                                handle_twitter_notifications_sync([formatted_tweet])
                                
                                # Невелика затримка між твітами для уникнення rate limit
                                await asyncio.sleep(0.5)
                                
                            except Exception as e:
                                logger.error(f"Помилка обробки твіта {tweet.get('id', 'unknown')}: {e}")
                        
                        logger.info(f"Twitter API: миттєво оброблено {len(new_tweets)} нових твітів")
                    
                    # Чекаємо перед наступною перевіркою (швидший моніторинг)
                    await asyncio.sleep(15)
                    
                except Exception as e:
                    logger.error(f"Помилка в циклі моніторингу Twitter: {e}")
                    await asyncio.sleep(30)  # Коротша затримка при помилці
            
    except Exception as e:
        logger.error(f"Помилка моніторингу Twitter: {e}")

async def start_twitter_monitor_adapter():
    """Запустити Twitter Monitor Adapter моніторинг"""
    global twitter_monitor_adapter
    
    if not twitter_monitor_adapter:
        logger.warning("Twitter Monitor Adapter не ініціалізовано")
        return
    
    try:
        twitter_monitor_adapter.monitoring_active = True
        
        if twitter_monitor_adapter.monitoring_accounts:
            accounts_list = list(twitter_monitor_adapter.monitoring_accounts)
            logger.info(f"🚀 Запуск Twitter Monitor Adapter моніторингу для акаунтів: {accounts_list}")
            logger.info("🔄 Twitter Monitor Adapter моніторинг активний та працює в фоновому режимі...")
        else:
            logger.info("🚀 Twitter Monitor Adapter моніторинг запущено (очікує додавання акаунтів)")
        
        # Основний цикл моніторингу
        while twitter_monitor_adapter.monitoring_active:
            try:
                # Отримуємо нові твіти через Twitter Monitor Adapter
                new_tweets = await twitter_monitor_adapter.check_new_tweets()
                
                if new_tweets:
                    # Обробляємо кожен твіт ОДРАЗУ після знаходження
                    for tweet in new_tweets:
                        try:
                            # Конвертуємо формат для сумісності з існуючим кодом
                            formatted_tweet = {
                                'tweet_id': tweet.get('id', ''),
                                'account': tweet.get('user', {}).get('screen_name', ''),
                                'author': tweet.get('user', {}).get('name', ''),
                                'text': tweet.get('text', ''),
                                'url': tweet.get('url', ''),
                                'timestamp': tweet.get('created_at', ''),
                                'images': tweet.get('images', []),  # Додаємо зображення!
                                'content_key': tweet.get('content_key')  # Додаємо content_key якщо є
                            }
                            
                            # МИТТЄВО відправляємо кожен твіт (масив з 1 елементом)
                            handle_twitter_notifications_sync([formatted_tweet])
                            
                            # Невелика затримка між твітами для уникнення rate limit
                            await asyncio.sleep(0.5)
                            
                        except Exception as e:
                            logger.error(f"Помилка обробки твіта {tweet.get('id', 'unknown')}: {e}")
                    
                    logger.info(f"Twitter Monitor Adapter: миттєво оброблено {len(new_tweets)} нових твітів")
                
                # Чекаємо перед наступною перевіркою (швидше для активного моніторингу)
                await asyncio.sleep(10)
                
            except Exception as e:
                logger.error(f"Помилка в циклі Twitter Monitor Adapter моніторингу: {e}")
                await asyncio.sleep(30)  # Затримка перед спробою відновлення
                
    except Exception as e:
        logger.error(f"Помилка моніторингу Twitter Monitor Adapter: {e}")
    finally:
        twitter_monitor_adapter.monitoring_active = False


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обробник помилок"""
    logger.error(f"Update {update} caused error {context.error}")

async def check_sessions(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Перевірити закінчені сесії"""
    try:
        security_manager.check_expired_sessions(context.bot)
    except Exception as e:
        logger.error(f"Помилка перевірки сесій: {e}")

async def cleanup_old_messages(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Очистити старі повідомлення"""
    try:
        project_manager.cleanup_old_messages(hours=24)
    except Exception as e:
        logger.error(f"Помилка очищення старих повідомлень: {e}")

async def cleanup_access_sessions(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Очистити закінчені сесії доступу"""
    try:
        access_manager.cleanup_expired_sessions()
    except Exception as e:
        logger.error(f"Помилка очищення сесій доступу: {e}")

def _get_time_ago(dt: datetime) -> str:
    """Отримати час тому"""
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        
        # Переконуємося що dt має timezone
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        
        diff = now - dt
        
        total_seconds = int(diff.total_seconds())
        
        if total_seconds < 0:
            return "щойно"
        elif total_seconds < 60:
            return f"{total_seconds} секунд тому"
        elif total_seconds < 3600:
            minutes = total_seconds // 60
            return f"{minutes} хвилин тому"
        elif total_seconds < 86400:
            hours = total_seconds // 3600
            return f"{hours} годин тому"
        else:
            days = total_seconds // 86400
            return f"{days} днів тому"
    except Exception as e:
        logger.error(f"Помилка обчислення часу: {e}")
        return ""



# Менеджер акаунтів
@require_auth
async def accounts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показати всі акаунти для моніторингу"""
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    
    # Отримуємо проекти користувача
    projects = project_manager.get_user_projects(user_id)
    
    # Отримуємо Twitter Monitor Adapter акаунти (використовуємо ту ж функцію)
    twitter_adapter_accounts = project_manager.get_selenium_accounts()
    
    # Групуємо по платформах
    twitter_projects = [p for p in projects if p['platform'] == 'twitter']
    discord_projects = [p for p in projects if p['platform'] == 'discord']
    
    # Форматуємо список
    text = "📋 **Ваші акаунти для моніторингу:**\n\n"
    
    # Twitter Monitor Adapter акаунти
    if twitter_adapter_accounts:
        text += "🚀 **Twitter Monitor Adapter акаунти:**\n"
        for i, username in enumerate(twitter_adapter_accounts, 1):
            account_info = project_manager.get_selenium_account_info(username)
            status = "✅ Активний" if account_info and account_info.get('is_active', True) else "❌ Неактивний"
            text += f"{i}. @{username} - {status}\n"
        text += "\n"
    
    # Звичайні Twitter проекти
    if twitter_projects:
        text += "🐦 **Звичайні Twitter/X акаунти:**\n"
        for i, project in enumerate(twitter_projects, 1):
            twitter_username: Optional[str] = extract_twitter_username(project['url'])
            if twitter_username:
                text += f"{i}. @{twitter_username} ({project['name']})\n"
        text += "\n"
    
    # Discord канали
    if discord_projects:
        text += "💬 **Discord канали:**\n"
        for i, project in enumerate(discord_projects, 1):
            channel_id = extract_discord_channel_id(project['url'])
            text += f"{i}. Канал {channel_id} ({project['name']})\n"
        text += "\n"
    
    # Якщо немає акаунтів
    if not twitter_adapter_accounts and not twitter_projects and not discord_projects:
        text += "❌ У вас немає акаунтів для моніторингу.\n\n"
        text += "Додайте акаунти через меню бота або команди:\n"
        text += "• /twitter_add username - додати Twitter Monitor Adapter акаунт\n"
        text += "• Меню 'Додати проект' - додати звичайний проект"
    
    # Додаємо команди для управління
    text += "\n🔧 **Команди для управління:**\n"
    text += "• /twitter_add username - додати Twitter Monitor Adapter акаунт\n"
    text += "• /twitter_remove username - видалити Twitter Monitor Adapter акаунт\n"
    text += "• /remove_twitter username - видалити звичайний Twitter акаунт\n"
    text += "• /remove_discord channel_id - видалити Discord канал\n"
    text += "• /accounts - показати цей список"
    
    await update.message.reply_text(text, )

@require_auth
async def remove_twitter_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Видалити Twitter акаунт з моніторингу"""
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    
    if not context.args:
        await update.message.reply_text("❌ Вкажіть username Twitter акаунта!\n\n**Приклад:** /remove_twitter pilk_xz")
        return
    
    username = context.args[0].replace('@', '').strip()
    
    # Знаходимо проект для видалення
    projects = project_manager.get_user_projects(user_id)
    twitter_projects = [p for p in projects if p['platform'] == 'twitter']
    
    project_to_remove = None
    for project in twitter_projects:
        if extract_twitter_username(project['url']) == username:
            project_to_remove = project
            break
    
    if not project_to_remove:
        await update.message.reply_text(f"❌ Twitter акаунт @{username} не знайдено в ваших проектах.")
        return
    
    # Видаляємо проект
    if project_manager.remove_project(user_id, project_to_remove['id']):
        # Синхронізуємо монітори після видалення
        sync_monitors_with_projects()
        
        await update.message.reply_text(f"✅ Twitter акаунт @{username} видалено з моніторингу.")
        
        # Також видаляємо з активних моніторів
        global twitter_monitor_adapter
        if twitter_monitor_adapter and username in twitter_monitor_adapter.monitoring_accounts:
            twitter_monitor_adapter.monitoring_accounts.discard(username)
            if username in twitter_monitor_adapter.seen_tweets:
                del twitter_monitor_adapter.seen_tweets[username]
            await update.message.reply_text(f"✅ Акаунт @{username} також видалено з Twitter Monitor Adapter моніторингу.")
        global twitter_monitor
        try:
            if twitter_monitor:
                twitter_monitor.remove_account(username)
        except Exception:
            pass
        # Після змін — синхронізуємо стан усіх моніторів
        sync_monitors_with_projects()
    else:
        await update.message.reply_text(f"❌ Помилка видалення Twitter акаунта @{username}.")


# Twitter Monitor Adapter команди (основний підхід)

@require_auth
async def twitter_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Додати акаунт для Twitter Monitor Adapter моніторингу"""
    if not update.effective_user or not update.message:
        return
    
    if not context.args:
        await update.message.reply_text("❌ Вкажіть username Twitter акаунта!\n\n**Приклад:** /twitter_add pilk_xz")
        return
    
    username = context.args[0].replace('@', '').strip()
    
    # Перевіряємо чи акаунт не заборонений
    if username.lower() in ['twitter', 'x', 'elonmusk']:
        await update.message.reply_text("❌ Заборонено моніторинг офіційного Twitter акаунта!")
        return
    
    global twitter_monitor_adapter
    
    if not twitter_monitor_adapter:
        twitter_monitor_adapter = TwitterMonitorAdapter()
    
    # Додаємо в базу даних (використовуємо ту ж функцію що і для Selenium)
    project_manager.add_selenium_account(username)
    
    # Синхронізуємо монітори після додавання
    sync_monitors_with_projects()
    
    # Додаємо акаунт в поточний монітор
    if twitter_monitor_adapter.add_account(username):
        await update.message.reply_text(
            f"✅ **Додано Twitter акаунт для Twitter Monitor Adapter моніторингу:**\n\n"
            f"• Username: @{username}\n"
            f"• Збережено в базу даних\n"
            f"• Додано до моніторингу\n\n"
            f"🚀 Використовується новий підхід через Twitter Monitor API!"
        )
    else:
        await update.message.reply_text(f"❌ Помилка додавання акаунта @{username}")

@require_auth
async def twitter_test_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Тестувати Twitter Monitor Adapter моніторинг"""
    if not update.effective_user or not update.message:
        return
    
    if not context.args:
        await update.message.reply_text("❌ Вкажіть username Twitter акаунта!\n\n**Приклад:** /twitter_test pilk_xz")
        return
    
    username = context.args[0].replace('@', '').strip()
    
    global twitter_monitor_adapter
    
    if not twitter_monitor_adapter:
        twitter_monitor_adapter = TwitterMonitorAdapter()
    
    await update.message.reply_text(f"🔍 Тестування Twitter Monitor Adapter моніторингу для @{username}...")
    
    try:
        tweets = await twitter_monitor_adapter.get_user_tweets(username, limit=3)
        
        if tweets:
            result_text = f"✅ **Twitter Monitor Adapter тест успішний!**\n\nЗнайдено {len(tweets)} твітів:\n\n"
            
            for i, tweet in enumerate(tweets, 1):
                text_preview = tweet['text'][:100] + "..." if len(tweet['text']) > 100 else tweet['text']
                result_text += f"{i}. {text_preview}\n"
                result_text += f"   🔗 [Перейти]({tweet['url']})\n"
                if tweet.get('images'):
                    result_text += f"   📷 Зображень: {len(tweet['images'])}\n"
                result_text += "\n"
                
            await update.message.reply_text(result_text)
        else:
            await update.message.reply_text(f"❌ Не вдалося отримати твіти для @{username}")
            
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка тестування: {str(e)}")

@require_auth
async def twitter_start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Запустити Twitter Monitor Adapter моніторинг"""
    if not update.effective_user or not update.message:
        return
    
    global twitter_monitor_adapter
    
    if not twitter_monitor_adapter:
        twitter_monitor_adapter = TwitterMonitorAdapter()
    
    if not twitter_monitor_adapter.monitoring_accounts:
        await update.message.reply_text("❌ Немає акаунтів для моніторингу! Додайте Twitter акаунти спочатку.")
        return
    
    twitter_monitor_adapter.monitoring_active = True
    
    # Запускаємо моніторинг в окремому потоці
    import threading
    monitor_thread = threading.Thread(target=lambda: asyncio.run(start_twitter_monitor_adapter()))
    monitor_thread.daemon = True
    monitor_thread.start()
    
    accounts_list = list(twitter_monitor_adapter.monitoring_accounts)
    await update.message.reply_text(
        f"🚀 **Twitter Monitor Adapter моніторинг запущено!**\n\n"
        f"• Акаунтів: {len(accounts_list)}\n"
        f"• Список: @{', @'.join(accounts_list)}\n\n"
        f"🔄 Моніторинг активний та працює в фоновому режимі..."
    )

@require_auth
async def twitter_stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Зупинити Twitter Monitor Adapter моніторинг"""
    if not update.effective_user or not update.message:
        return
    
    global twitter_monitor_adapter
    
    if twitter_monitor_adapter:
        twitter_monitor_adapter.monitoring_active = False
        await twitter_monitor_adapter.__aexit__(None, None, None)
        twitter_monitor_adapter = None
    
    await update.message.reply_text("⏹️ **Twitter Monitor Adapter моніторинг зупинено!**")

@require_auth
async def twitter_remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Видалити Twitter Monitor Adapter акаунт з моніторингу"""
    if not update.effective_user or not update.message:
        return
    
    global twitter_monitor_adapter
    
    if not context.args:
        await update.message.reply_text("❌ Вкажіть username Twitter акаунта!\n\n**Приклад:** /twitter_remove pilk_xz")
        return
    
    username = context.args[0].replace('@', '').strip()
    
    # Видаляємо з бази даних
    if project_manager.remove_selenium_account(username):
        # Синхронізуємо монітори після видалення
        sync_monitors_with_projects()
        
        # Видаляємо з поточного монітора
        if twitter_monitor_adapter and username in twitter_monitor_adapter.monitoring_accounts:
            twitter_monitor_adapter.monitoring_accounts.discard(username)
            if username in twitter_monitor_adapter.seen_tweets:
                del twitter_monitor_adapter.seen_tweets[username]
        
        await update.message.reply_text(
            f"✅ **Видалено Twitter Monitor Adapter акаунт:**\n\n"
            f"• Username: @{username}\n"
            f"• Видалено з бази даних\n"
            f"• Видалено з поточного монітора",
        )
    else:
        await update.message.reply_text(f"❌ Акаунт @{username} не знайдено в Twitter Monitor Adapter моніторингу")

@require_auth
async def test_tweet_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Тестова команда для перевірки відправки твітів"""
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    
    # Створюємо тестовий твіт
    test_tweet = {
        'tweet_id': 'test_' + str(int(time.time())),
        'account': 'irys_xyz',
        'author': 'Irys',
        'text': 'Це тестовий твіт для перевірки системи пересилання',
        'url': 'https://x.com/irys_xyz/status/test',
        'timestamp': datetime.now().isoformat(),
        'images': []
    }
    
    try:
        # Відправляємо тестовий твіт
        handle_twitter_notifications_sync([test_tweet])
        await update.message.reply_text("✅ Тестовий твіт відправлено!")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка відправки тестового твіта: {e}")

@require_auth
async def test_discord_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Тестова команда для перевірки відправки Discord повідомлень"""
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    
    # Створюємо тестове Discord повідомлення
    test_message = {
        'message_id': 'test_' + str(int(time.time())),
        'channel_id': '1413243132467871839',  # Канал з проекту
        'author': 'Test User',
        'content': 'Це тестове повідомлення для перевірки системи Discord пересилання',
        'url': 'https://discord.com/channels/1408570777275469866/1413243132467871839/test',
        'timestamp': datetime.now().isoformat(),
        'images': []
    }
    
    try:
        # Відправляємо тестове Discord повідомлення
        handle_discord_notifications_sync([test_message])
        await update.message.reply_text("✅ Тестове Discord повідомлення відправлено!")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка відправки тестового Discord повідомлення: {e}")

@require_auth  
async def reset_discord_history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда для очищення історії Discord повідомлень (тільки для адміністратора)"""
    if not update.effective_user or not update.message:
        return
        
    user_id = update.effective_user.id
    
    # Перевіряємо чи користувач є адміністратором
    if not access_manager.is_admin(user_id):
        await update.message.reply_text(
            "❌ **Доступ заборонено!**\n\n"
            "Тільки адміністратор може очищати історію Discord.",
        )
        return
    
    try:
        # Очищаємо історію Discord повідомлень
        global discord_monitor
        if discord_monitor:
            discord_monitor.last_message_ids = {}
            logger.info("Очищено історію Discord повідомлень")
        
        await update.message.reply_text(
            "✅ **Discord історія очищена**\n\n"
            "Історія останніх повідомлень Discord очищена. "
            "Бот може повторно відправити старі повідомлення з Discord каналів!",
        )
        
    except Exception as e:
        await update.message.reply_text(
            f"❌ **Помилка очищення Discord історії**\n\n{str(e)}",
        )

@require_auth
async def remove_discord_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Видалити Discord канал з моніторингу"""
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    
    if not context.args:
        await update.message.reply_text("❌ Вкажіть ID Discord каналу!\n\n**Приклад:** /remove_discord 1358806016648544326")
        return
    
    channel_id = context.args[0].strip()
    
    # Знаходимо проект для видалення
    projects = project_manager.get_user_projects(user_id)
    discord_projects = [p for p in projects if p['platform'] == 'discord']
    
    project_to_remove = None
    for project in discord_projects:
        if extract_discord_channel_id(project['url']) == channel_id:
            project_to_remove = project
            break
    
    if not project_to_remove:
        await update.message.reply_text(f"❌ Discord канал {channel_id} не знайдено в ваших проектах.")
        return
    
    # Видаляємо проект
    if project_manager.remove_project(user_id, project_to_remove['id']):
        # Синхронізуємо монітори після видалення
        sync_monitors_with_projects()
        
        await update.message.reply_text(f"✅ Discord канал {channel_id} видалено з моніторингу.")
        
        # Також видаляємо з Discord монітора якщо він активний
        global discord_monitor
        if discord_monitor and channel_id in discord_monitor.monitoring_channels:
            discord_monitor.monitoring_channels.discard(channel_id)
            if channel_id in discord_monitor.last_message_ids:
                del discord_monitor.last_message_ids[channel_id]
            await update.message.reply_text(f"✅ Канал {channel_id} також видалено з Discord моніторингу.")
        # Після змін — синхронізуємо стан
        sync_monitors_with_projects()
    else:
        await update.message.reply_text(f"❌ Помилка видалення Discord каналу {channel_id}.")


def extract_discord_channel_id(url: str) -> str:
    """Витягти channel_id з Discord URL"""
    try:
        if not url:
            return ""
        
        import re
        # Спробуємо знайти channel_id в URL
        match = re.search(r'discord\.com/channels/\d+/(\d+)', url)
        if match:
            return match.group(1)
        
        # Якщо це просто ID (тільки цифри)
        if url.isdigit():
            return url
            
        logger.warning(f"Не вдалося витягти Discord channel_id з: {url}")
        return ""
    except Exception as e:
        logger.error(f"Помилка витягування Discord channel_id з '{url}': {e}")
        return ""

async def admin_create_user_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда для створення нового користувача (тільки для адміністратора)"""
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    
    # Перевіряємо чи користувач є адміністратором
    if not access_manager.is_admin(user_id):
        await update.message.reply_text(
            "❌ **Доступ заборонено!**\n\n"
            "Тільки адміністратор може створювати нових користувачів.",
        )
        return
    
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "📝 **Створення нового користувача**\n\n"
            "Використання: /admin_create_user <telegram_id> <username> [password]\n\n"
            "Приклад: /admin_create_user 123456789 JohnDoe mypassword",
        )
        return
    
    try:
        telegram_id = int(context.args[0])
        username = context.args[1]
        password = context.args[2] if len(context.args) > 2 else None
        
        # Створюємо користувача
        user_id_created = access_manager.add_user(telegram_id, username or "Unknown", password or "")
        
        if user_id_created:
            await update.message.reply_text(
                f"✅ **Користувач успішно створений!**\n\n"
                f"👤 **Username:** {username}\n"
                f"🆔 **Telegram ID:** {telegram_id}\n"
                f"🔐 **Пароль:** {password or 'за замовчуванням'}\n"
                f"👑 **Роль:** Користувач\n\n"
                f"Користувач може увійти в систему командою /login",
            )
        else:
            await update.message.reply_text(
                "❌ Помилка створення користувача (можливо, користувач вже існує).",
            )
            
    except ValueError:
        await update.message.reply_text(
            "❌ **Неправильний формат!**\n\n"
            "Telegram ID повинен бути числом.\n"
            "Приклад: /admin_create_user 123456789 JohnDoe",
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Помилка створення користувача: {str(e)}",
        )

async def admin_create_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда для створення нового адміністратора (тільки для адміністратора)"""
    if not update.effective_user or not update.message:
        return
    
    user_id = update.effective_user.id
    
    # Перевіряємо чи користувач є адміністратором
    if not access_manager.is_admin(user_id):
        await update.message.reply_text(
            "❌ **Доступ заборонено!**\n\n"
            "Тільки адміністратор може створювати інших адміністраторів.",
        )
        return
    
    if not context.args or len(context.args) < 2:
        await update.message.reply_text(
            "📝 **Створення нового адміністратора**\n\n"
            "Використання: /admin_create_admin <telegram_id> <username> [password]\n\n"
            "Приклад: /admin_create_admin 123456789 AdminJohn adminpass123",
        )
        return
    
    try:
        telegram_id = int(context.args[0])
        username = context.args[1]
        password = context.args[2] if len(context.args) > 2 else None
        
        # Створюємо адміністратора
        user_id_created = access_manager.create_admin_user(telegram_id, username or "Unknown", password or "")
        
        if user_id_created:
            await update.message.reply_text(
                f"✅ **Адміністратор успішно створений!**\n\n"
                f"👤 **Username:** {username}\n"
                f"🆔 **Telegram ID:** {telegram_id}\n"
                f"🔐 **Пароль:** {password or 'за замовчуванням'}\n"
                f"👑 **Роль:** Адміністратор\n\n"
                f"Адміністратор може увійти в систему командою /login",
            )
        else:
            await update.message.reply_text(
                "❌ Помилка створення адміністратора (можливо, користувач вже існує).",
            )
            
    except ValueError:
        await update.message.reply_text(
            "❌ **Неправильний формат!**\n\n"
            "Telegram ID повинен бути числом.\n"
            "Приклад: /admin_create_admin 123456789 AdminJohn",
        )
    except Exception as e:
        await update.message.reply_text(
            f"❌ Помилка створення адміністратора: {str(e)}",
        )

async def admin_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда для перегляду всіх користувачів (тільки для адміністратора)"""
    if not update.effective_user or not update.message:
        return
        
    user_id = update.effective_user.id
    
    # Перевіряємо чи користувач є адміністратором
    if not access_manager.is_admin(user_id):
        await update.message.reply_text(
            "❌ **Доступ заборонено!**\n\n"
            "Тільки адміністратор може переглядати список користувачів.",
        )
        return
    
    try:
        all_users = access_manager.get_all_users()
        
        if not all_users:
            await update.message.reply_text(
                "👥 **Список користувачів**\n\n"
                "Користувачів не знайдено.",
            )
            return
        
        users_text = "👥 **Список користувачів**\n\n"
        
        for i, user in enumerate(all_users[:20], 1):  # Показуємо перших 20
            role_emoji = "👑" if user.get('role', 'user') == 'admin' else "👤"
            status_emoji = "✅" if user.get('is_active', True) else "❌"
            
            users_text += (
                f"{i}. {role_emoji} **{user.get('username', 'Без імені')}**\n"
                f"   🆔 ID: `{user.get('telegram_id')}`\n"
                f"   📊 Статус: {status_emoji}\n"
                f"   📅 Створено: {user.get('created_at', '')[:10]}\n\n"
            )
        
        if len(all_users) > 20:
            users_text += f"... та ще {len(all_users) - 20} користувачів\n\n"
        
        users_text += f"**Всього користувачів:** {len(all_users)}"
        
        await update.message.reply_text(users_text, )
        
    except Exception as e:
        if update.message:
            await update.message.reply_text(
                f"❌ **Помилка отримання списку користувачів**\n\n{str(e)}",
            )

async def reset_seen_tweets_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Команда для очищення всіх seen_tweets (тільки для адміністратора)"""
    if not update.effective_user or not update.message:
        return
        
    user_id = update.effective_user.id
    
    # Перевіряємо чи користувач є адміністратором
    if not access_manager.is_admin(user_id):
        await update.message.reply_text(
            "❌ **Доступ заборонено!**\n\n"
            "Тільки адміністратор може очищати seen_tweets.",
        )
        return
    
    try:
        # Викликаємо функцію очищення seen_tweets
        success = reset_seen_tweets()
        
        if success:
            await update.message.reply_text(
                "✅ **Seen_tweets очищено**\n\n"
                "Всі файли з відправленими твітами успішно видалено.\n\n"
                "⚠️ **УВАГА:** Бот може повторно відправити старі твіти! "
                "Використовуйте цю команду обережно.",
            )
        else:
            await update.message.reply_text(
                "❌ **Помилка очищення**\n\n"
                "Не всі файли вдалося очистити. Перевірте логи для деталей.",
            )
        
    except Exception as e:
        await update.message.reply_text(
            f"❌ **Помилка очищення seen_tweets**\n\n{str(e)}",
        )

def main() -> None:
    """Головна функція"""
    global bot_instance
    
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не встановлено! Створіть файл .env з BOT_TOKEN")
        return
    
    if not DISCORD_AUTHORIZATION:
        logger.warning("AUTHORIZATION токен не встановлено! Discord моніторинг буде відключено")
    
    # Створюємо додаток
    application = Application.builder().token(BOT_TOKEN).build()
    bot_instance = application.bot
    
    # Додаємо обробники
    application.add_handler(CommandHandler("start", start))
    
    # Команди авторизації
    application.add_handler(CommandHandler("login", login_command))
    application.add_handler(CommandHandler("logout", logout_command))
    application.add_handler(CommandHandler("register", register_command))
    
    application.add_handler(CallbackQueryHandler(handle_callback_query))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Twitter Monitor Adapter команди (основний підхід) - реєструються пізніше
    
    # Менеджер акаунтів
    application.add_handler(CommandHandler("accounts", accounts_command))
    application.add_handler(CommandHandler("remove_twitter", remove_twitter_command))
    application.add_handler(CommandHandler("remove_discord", remove_discord_command))
    
    # Пересилання (персональні налаштування)
    application.add_handler(CommandHandler("forward_on", forward_enable_command))
    application.add_handler(CommandHandler("forward_off", forward_disable_command))
    application.add_handler(CommandHandler("forward_status", forward_status_command))
    application.add_handler(CommandHandler("forward_set_channel", forward_set_channel_command))
    application.add_handler(CommandHandler("forward_test", forward_test_command))
    application.add_handler(CommandHandler("thread_test", thread_test_command))
    application.add_handler(CommandHandler("setup", setup_quick_command))
    
    # Адміністративні команди
    application.add_handler(CommandHandler("admin_create_user", admin_create_user_command))
    application.add_handler(CommandHandler("admin_create_admin", admin_create_admin_command))
    application.add_handler(CommandHandler("admin_users", admin_users_command))
    application.add_handler(CommandHandler("reset_seen_tweets", reset_seen_tweets_command))
    
    # Twitter Monitor Adapter команди (основний підхід)
    application.add_handler(CommandHandler("twitter_add", twitter_add_command))
    application.add_handler(CommandHandler("twitter_test", twitter_test_command))
    application.add_handler(CommandHandler("twitter_start", twitter_start_command))
    application.add_handler(CommandHandler("twitter_stop", twitter_stop_command))
    application.add_handler(CommandHandler("twitter_remove", twitter_remove_command))
    application.add_handler(CommandHandler("test_tweet", test_tweet_command))
    application.add_handler(CommandHandler("test_discord", test_discord_command))
    application.add_handler(CommandHandler("reset_discord_history", reset_discord_history_command))
    
    application.add_error_handler(error_handler)
    
    # Додаємо періодичну перевірку сесій (кожну хвилину)
    job_queue = application.job_queue
    if job_queue:
        job_queue.run_repeating(check_sessions, interval=300, first=300)  # Кожні 5 хвилин
        
        # Додаємо періодичне очищення старих повідомлень (кожні 2 години)
        job_queue.run_repeating(cleanup_old_messages, interval=7200, first=7200)
        
        # Додаємо періодичне очищення сесій доступу (кожні 30 хвилин)
        job_queue.run_repeating(cleanup_access_sessions, interval=1800, first=1800)  # Кожні 30 хвилин
    
        # Додаємо періодичну синхронізацію моніторів (кожні 5 хвилин)
        job_queue.run_repeating(lambda context: sync_monitors_with_projects(), interval=300, first=300)  # Кожні 5 хвилин
    
    logger.info("🚀 Бот запускається...")
    
    # Перевіряємо конфігурацію
    logger.info("🔧 Перевірка конфігурації:")
    logger.info(f"   🤖 BOT_TOKEN: {'✅ Є' if BOT_TOKEN else '❌ Відсутній'}")
    logger.info(f"   🐦 TWITTER_AUTH_TOKEN: {'✅ Є' if TWITTER_AUTH_TOKEN else '❌ Відсутній'}")
    logger.info(f"   💬 DISCORD_AUTHORIZATION: {'✅ Є' if DISCORD_AUTHORIZATION else '❌ Відсутній'}")
    
    # Показуємо статистику існуючих проектів
    try:
        total_users = len(project_manager.data.get('users', {}))
        total_projects = 0
        twitter_projects = 0
        discord_projects = 0
        
        for user_id, projects in project_manager.data.get('projects', {}).items():
            total_projects += len(projects)
            for project in projects:
                if project.get('platform') == 'twitter':
                    twitter_projects += 1
                elif project.get('platform') == 'discord':
                    discord_projects += 1
        
        logger.info(f"📊 Статистика системи:")
        logger.info(f"   👥 Користувачів: {total_users}")
        logger.info(f"   📋 Всього проектів: {total_projects}")
        logger.info(f"   🐦 Twitter проектів: {twitter_projects}")
        logger.info(f"   💬 Discord проектів: {discord_projects}")
        
        if total_projects > 0:
            logger.info("✅ Знайдено існуючі проекти - будуть автоматично запущені для моніторингу")
        else:
            logger.info("ℹ️ Проекти не знайдено - монітори будуть готові до додавання проектів")
    except Exception as e:
        logger.error(f"Помилка отримання статистики проектів: {e}")
    
    # Ініціалізуємо Twitter Monitor Adapter (основний підхід)
    global twitter_monitor_adapter
    try:
        twitter_monitor_adapter = TwitterMonitorAdapter()
        logger.info("✅ Twitter Monitor Adapter ініціалізовано")
        
        # Завантажуємо збережені акаунти в адаптер
        saved_accounts = project_manager.get_selenium_accounts()
        if saved_accounts:
            logger.info(f"Завантажено {len(saved_accounts)} збережених акаунтів в Twitter Monitor Adapter: {saved_accounts}")
            for username in saved_accounts:
                twitter_monitor_adapter.add_account(username)
    except Exception as e:
        logger.error(f"Помилка ініціалізації Twitter Monitor Adapter: {e}")
        twitter_monitor_adapter = None
    
    
    # На старті проводимо синхронізацію моніторів з проектами/базою
    # Це автоматично запустить всі монітори для існуючих проектів
    logger.info("🔄 Синхронізуємо монітори з існуючими проектами...")
    sync_monitors_with_projects()

    # Показуємо поточний стан моніторингу
    try:
        twitter_accounts = len(getattr(twitter_monitor, 'monitoring_accounts', set())) if twitter_monitor else 0
        twitter_adapter_accounts = len(getattr(twitter_monitor_adapter, 'monitoring_accounts', set())) if twitter_monitor_adapter else 0
        discord_channels = len(getattr(discord_monitor, 'channels', [])) if discord_monitor else 0
        
        logger.info("📈 Поточний стан моніторингу:")
        logger.info(f"   🐦 Twitter API: {twitter_accounts} акаунтів")
        logger.info(f"   🚀 Twitter Monitor Adapter: {twitter_adapter_accounts} акаунтів") 
        logger.info(f"   💬 Discord: {discord_channels} каналів")
        
        total_monitoring = twitter_accounts + twitter_adapter_accounts + discord_channels
        if total_monitoring > 0:
            logger.info(f"✅ Всього активних моніторів: {total_monitoring}")
            logger.info("🎯 Бот готовий до роботи та автоматично моніторить всі налаштовані проекти!")
        else:
            logger.info("ℹ️ Монітори готові, очікуємо додавання проектів")
    except Exception as e:
        logger.error(f"Помилка отримання стану моніторингу: {e}")
    
    logger.info("✅ Синхронізація завершена, всі монітори запущені автоматично")
    
    # Запускаємо бота
    try:
        application.run_polling()
    except KeyboardInterrupt:
        # Примусово зберігаємо дані при завершенні
        project_manager.save_data(force=True)
        logger.info("Бот зупинено, дані збережено")

if __name__ == '__main__':
    main()