#!/usr/bin/env python3
"""
Twitter Monitor Adapter для інтеграції з основним ботом

Цей модуль адаптує twitter_monitor для використання в основному боті,
замінюючи Selenium підхід на більш надійний API підхід.
"""

import asyncio
import logging
import json
import os
import time
from datetime import datetime
from typing import Set, List, Dict, Optional
from pathlib import Path

# Імпортуємо twitter_monitor модулі
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), 'twitter_monitor'))

from twscrape import API
from twscrape.models import Tweet, User

# Налаштування логування
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class TwitterMonitorAdapter:
    """Адаптер для інтеграції twitter_monitor з основним ботом"""
    
    def __init__(self, accounts_db_path: str = None):
        """
        Ініціалізація адаптера
        
        Args:
            accounts_db_path: Шлях до бази даних акаунтів twitter_monitor
        """
        self.accounts_db_path = accounts_db_path or "./twitter_monitor/accounts.db"
        self.api = None
        self.monitoring_accounts = set()
        self.seen_tweets = {}  # account -> set of tweet_ids
        self.sent_tweets = {}  # account -> set of sent tweet_ids
        self.monitoring_active = False
        self.seen_tweets_file = "twitter_monitor_seen_tweets.json"
        
        # Створюємо папку twitter_monitor якщо не існує
        twitter_monitor_dir = Path("twitter_monitor")
        twitter_monitor_dir.mkdir(exist_ok=True)
        
        # Завантажуємо збережені seen_tweets
        self.load_seen_tweets()
        
        # Словник для зберігання відповідності акаунтів до проектів
        self.account_projects = {}  # username -> project_name
        
        # Ініціалізуємо API
        self._init_api()
        
    def _init_api(self) -> bool:
        """Ініціалізувати API twitter_monitor"""
        try:
            if os.path.exists(self.accounts_db_path):
                self.api = API(pool=self.accounts_db_path)
                logger.info(f"Twitter Monitor API ініціалізовано з базою: {self.accounts_db_path}")
                return True
            else:
                logger.warning(f"База даних акаунтів не знайдена: {self.accounts_db_path}")
                logger.info("Створюємо нову базу даних...")
                self.api = API()
                return True
        except Exception as e:
            logger.error(f"Помилка ініціалізації API: {e}")
            return False
    
    def is_twitter_link_valid(self, tweet_text: str, username: str) -> bool:
        """Перевірити чи посилання в твіті відповідає проекту"""
        if not tweet_text:
            return True
            
        # Шукаємо всі Twitter посилання в тексті
        import re
        twitter_links = re.findall(r'https?://(?:twitter\.com|x\.com)/\w+/status/\d+', tweet_text)
        
        if not twitter_links:
            return True  # Немає посилань - твіт валідний
            
        # Перевіряємо кожне посилання
        for link in twitter_links:
            # Витягуємо username з посилання
            if '/status/' in link:
                link_username = link.split('/')[3]  # twitter.com/username/status/...
            else:
                continue
                
            # Якщо посилання не від поточного акаунта - твіт не валідний
            if link_username.lower() != username.lower():
                logger.info(f"🚫 Twitter Monitor: Фільтрація твіта: посилання на {link_username} не відповідає проекту {username}")
                return False
                
        return True
    
    async def __aenter__(self):
        """Асинхронний контекстний менеджер"""
        if self._init_api():
            logger.info("Twitter Monitor адаптер ініціалізовано")
        else:
            logger.warning("Twitter Monitor адаптер не вдалося ініціалізувати")
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Закрити сесію"""
        # Зберігаємо seen_tweets перед закриттям
        self.save_seen_tweets()
        logger.info("Twitter Monitor адаптер закрито")
        
    def add_account(self, username: str) -> bool:
        """Додати акаунт для моніторингу"""
        try:
            clean_username = username.replace('@', '').strip()
            
            # Забороняємо моніторинг офіційного Twitter акаунта (але дозволяємо для тестування)
            if clean_username.lower() in ['twitter', 'x']:
                logger.warning(f"❌ Заборонено моніторинг офіційного Twitter акаунта: {clean_username}")
                # Видаляємо з моніторингу якщо вже додано
                if clean_username in self.monitoring_accounts:
                    self.monitoring_accounts.discard(clean_username)
                if clean_username in self.sent_tweets:
                    del self.sent_tweets[clean_username]
                if clean_username in self.seen_tweets:
                    del self.seen_tweets[clean_username]
                # Зберігаємо зміни
                self.save_seen_tweets()
                return False
                
            if clean_username:
                self.monitoring_accounts.add(clean_username)
                if clean_username not in self.seen_tweets:
                    self.seen_tweets[clean_username] = set()
                if clean_username not in self.sent_tweets:
                    self.sent_tweets[clean_username] = set()
                logger.info(f"Додано акаунт для моніторингу: {clean_username}")
                return True
        except Exception as e:
            logger.error(f"Помилка додавання акаунта {username}: {e}")
        return False
        
    def get_monitoring_accounts(self) -> List[str]:
        """Отримати список акаунтів для моніторингу"""
        return list(self.monitoring_accounts)
        
    async def get_user_tweets(self, username: str, limit: int = 5) -> List[Dict]:
        """Отримати твіти користувача через twitter_monitor API"""
        if not self.api:
            logger.error("Twitter Monitor API не ініціалізовано")
            return []
            
        try:
            clean_username = username.replace('@', '').strip()
            
            # Отримуємо інформацію про користувача
            user = await self.api.user_by_login(clean_username)
            if not user:
                logger.error(f"Користувач @{clean_username} не знайдено")
                return []
            
            logger.info(f"Отримуємо твіти для @{clean_username} (ID: {user.id})")
            
            # Отримуємо твіти користувача
            tweets = []
            async for tweet in self.api.user_tweets(user.id, limit=limit):
                tweet_data = self._convert_tweet_to_dict(tweet, clean_username)
                if tweet_data:
                    tweets.append(tweet_data)
            
            logger.info(f"Знайдено {len(tweets)} твітів для {clean_username}")
            return tweets
            
        except Exception as e:
            logger.error(f"Помилка отримання твітів для {username}: {e}")
            return []
    
    def _convert_tweet_to_dict(self, tweet: Tweet, username: str) -> Optional[Dict]:
        """Конвертувати Tweet об'єкт в словник для сумісності з ботом"""
        try:
            # Перевіряємо чи текст твіта валідний для цього користувача
            if tweet.rawContent and len(tweet.rawContent) > 10:
                # Фільтруємо твіти з невалідними посиланнями
                if not self.is_twitter_link_valid(tweet.rawContent, username):
                    logger.debug(f"Twitter Monitor: Відфільтровано твіт з невалідними посиланнями для {username}: {tweet.rawContent[:50]}...")
                    return None
            
            # Витягуємо зображення
            images = []
            if tweet.media:
                # Фото
                for photo in tweet.media.photos:
                    images.append(photo.url)
                # Відео (thumbnail)
                for video in tweet.media.videos:
                    images.append(video.thumbnailUrl)
                # GIF
                for gif in tweet.media.animated:
                    images.append(gif.thumbnailUrl)
            
            # Фільтруємо короткі або порожні тексти (але дозволяємо твіти тільки з фото)
            if len(tweet.rawContent) < 5 and not images:
                return None
            
            return {
                'id': str(tweet.id),
                'text': tweet.rawContent,
                'url': tweet.url,
                'images': images,
                'created_at': tweet.date.isoformat(),
                'user': {
                    'screen_name': username,
                    'name': tweet.user.displayname if tweet.user else username
                }
            }
            
        except Exception as e:
            logger.debug(f"Помилка конвертації твіта: {e}")
            return None
    
    async def check_new_tweets(self) -> List[Dict]:
        """Перевірити нові твіти для всіх акаунтів"""
        if not self.api:
            logger.warning("Twitter Monitor API не ініціалізовано")
            return []
            
        new_tweets = []
        
        for username in self.monitoring_accounts:
            try:
                # Ініціалізуємо множини якщо не існують
                if username not in self.seen_tweets:
                    self.seen_tweets[username] = set()
                if username not in self.sent_tweets:
                    self.sent_tweets[username] = set()
                
                tweets = await self.get_user_tweets(username, limit=5)
                
                for tweet in tweets:
                    tweet_id = tweet.get('id')
                    tweet_text = tweet.get('text', '').strip()
                    
                    if tweet_id:
                        # Перевіряємо чи цей твіт вже був відправлений за ID
                        if tweet_id in self.sent_tweets[username]:
                            continue
                        
                        # Фільтруємо твіти з невалідними посиланнями
                        if not self.is_twitter_link_valid(tweet_text, username):
                            logger.info(f"🚫 Twitter Monitor: Відфільтровано твіт з невалідними посиланнями для {username}")
                            continue
                        
                        # Додаткова перевірка за контентом
                        if tweet_text:
                            import hashlib
                            content_hash = hashlib.md5(f"{username}_{tweet_text}".encode('utf-8')).hexdigest()[:12]
                            content_key = f"content_{content_hash}"
                            if content_key in self.sent_tweets[username]:
                                logger.info(f"Twitter Monitor: контент твіта для {username} вже був відправлений, пропускаємо")
                                continue
                            
                        # Додаємо до нових твітів
                        if tweet_id not in self.seen_tweets[username]:
                            logger.info(f"🆕 Twitter Monitor: Знайдено новий твіт від {username}: {tweet_text[:50]}...")
                            new_tweets.append(tweet)
                            self.seen_tweets[username].add(tweet_id)
                            self.sent_tweets[username].add(tweet_id)
                            
                            # Додаємо хеш контенту до відправлених
                            if tweet_text:
                                import hashlib
                                content_hash = hashlib.md5(f"{username}_{tweet_text}".encode('utf-8')).hexdigest()[:12]
                                content_key = f"content_{content_hash}"
                                self.sent_tweets[username].add(content_key)
                        
            except Exception as e:
                logger.error(f"Помилка перевірки твітів для {username}: {e}")
        
        # Зберігаємо оброблені твіти після кожної перевірки
        if new_tweets:
            self.save_seen_tweets()
                
        return new_tweets
    
    def format_tweet_notification(self, tweet: Dict) -> str:
        """Форматувати сповіщення про твіт"""
        try:
            username = tweet.get('user', {}).get('screen_name', 'unknown')
            name = tweet.get('user', {}).get('name', username)
            tweet_id = tweet.get('id', '')
            url = tweet.get('url', f"https://twitter.com/{username}/status/{tweet_id}")
            created_at = tweet.get('created_at', '')
            text = tweet.get('text', '')
            images = tweet.get('images', [])
            
            # Форматуємо дату
            try:
                from datetime import datetime
                if created_at:
                    dt = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                    formatted_date = dt.strftime("%d %B, %H:%M UTC")
                    time_ago = self._get_time_ago(dt)
                else:
                    formatted_date = "Не відомо"
                    time_ago = ""
            except:
                formatted_date = created_at
                time_ago = ""
            
            # Обрізаємо текст якщо він занадто довгий
            if len(text) > 200:
                text = text[:200] + "..."
            
            notification = f"""🐦 **Новий твіт з Twitter**
• Профіль: @{username}
• Автор: {name}
• Дата: {formatted_date} ({time_ago})
• Текст: {text}
🔗 [Перейти до твіта]({url})"""
            
            # Додаємо інформацію про зображення якщо є
            if images:
                notification += f"\n📷 Зображень: {len(images)}"
            
            return notification
            
        except Exception as e:
            logger.error(f"Помилка форматування сповіщення: {e}")
            return f"🐦 Новий твіт з Twitter: {tweet.get('text', 'Помилка форматування')}"
    
    def _get_time_ago(self, dt: datetime) -> str:
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
    
    def save_seen_tweets(self):
        """Зберегти список оброблених твітів"""
        try:
            # Конвертуємо set у list для JSON серіалізації
            data_to_save = {}
            for account, tweet_ids in self.seen_tweets.items():
                if isinstance(tweet_ids, set):
                    data_to_save[account] = list(tweet_ids)
                else:
                    data_to_save[account] = tweet_ids
            
            with open(self.seen_tweets_file, 'w', encoding='utf-8') as f:
                json.dump(data_to_save, f, ensure_ascii=False, indent=2)
            logger.debug(f"Збережено seen_tweets для {len(data_to_save)} акаунтів")
            return True
        except Exception as e:
            logger.error(f"Помилка збереження seen_tweets: {e}")
            return False
    
    def load_seen_tweets(self):
        """Завантажити список оброблених твітів"""
        try:
            if os.path.exists(self.seen_tweets_file):
                with open(self.seen_tweets_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                # Конвертуємо list назад у set
                for account, tweet_ids in data.items():
                    if isinstance(tweet_ids, list):
                        self.seen_tweets[account] = set(tweet_ids)
                    else:
                        self.seen_tweets[account] = set()
                
                logger.info(f"Завантажено seen_tweets для {len(self.seen_tweets)} акаунтів")
            else:
                logger.info("Файл twitter_monitor_seen_tweets.json не знайдено, починаємо з порожнього списку")
        except Exception as e:
            logger.error(f"Помилка завантаження seen_tweets: {e}")
            self.seen_tweets = {}

# Приклад використання
async def main():
    """Приклад використання TwitterMonitorAdapter"""
    async with TwitterMonitorAdapter() as monitor:
        # Додаємо акаунт для моніторингу
        monitor.add_account("pilk_xz")
        
        # Отримуємо твіти
        tweets = await monitor.get_user_tweets("pilk_xz", limit=5)
        print(f"Знайдено {len(tweets)} твітів")
        
        # Перевіряємо нові твіти
        new_tweets = await monitor.check_new_tweets()
        print(f"Знайдено {len(new_tweets)} нових твітів")

if __name__ == "__main__":
    asyncio.run(main())
