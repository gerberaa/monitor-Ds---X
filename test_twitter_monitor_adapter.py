#!/usr/bin/env python3
"""
Тестовий скрипт для перевірки роботи Twitter Monitor Adapter
"""

import asyncio
import logging
from twitter_monitor_adapter import TwitterMonitorAdapter

# Налаштування логування
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

async def test_twitter_monitor_adapter():
    """Тестування Twitter Monitor Adapter"""
    print("🚀 Тестування Twitter Monitor Adapter")
    print("=" * 50)
    
    try:
        # Ініціалізуємо адаптер
        print("1. Ініціалізація адаптера...")
        adapter = TwitterMonitorAdapter()
        print("✅ Адаптер ініціалізовано")
        
        # Тестуємо додавання акаунта
        print("\n2. Тестування додавання акаунта...")
        test_username = "elonmusk"  # Використовуємо відомий публічний акаунт
        success = adapter.add_account(test_username)
        if success:
            print(f"✅ Акаунт @{test_username} додано успішно")
        else:
            print(f"❌ Помилка додавання акаунта @{test_username}")
            return
        
        # Тестуємо отримання твітів
        print(f"\n3. Тестування отримання твітів для @{test_username}...")
        tweets = await adapter.get_user_tweets(test_username, limit=3)
        
        if tweets:
            print(f"✅ Отримано {len(tweets)} твітів:")
            for i, tweet in enumerate(tweets, 1):
                text_preview = tweet['text'][:100] + "..." if len(tweet['text']) > 100 else tweet['text']
                print(f"   {i}. {text_preview}")
                print(f"      🔗 {tweet['url']}")
                if tweet.get('images'):
                    print(f"      📷 Зображень: {len(tweet['images'])}")
                print()
        else:
            print("❌ Не вдалося отримати твіти")
            return
        
        # Тестуємо перевірку нових твітів
        print("4. Тестування перевірки нових твітів...")
        new_tweets = await adapter.check_new_tweets()
        print(f"✅ Знайдено {len(new_tweets)} нових твітів")
        
        # Тестуємо форматування сповіщення
        if tweets:
            print("\n5. Тестування форматування сповіщення...")
            notification = adapter.format_tweet_notification(tweets[0])
            print("✅ Сповіщення відформатовано:")
            print(notification)
        
        print("\n🎉 Всі тести пройшли успішно!")
        
    except Exception as e:
        print(f"❌ Помилка під час тестування: {e}")
        logger.error(f"Помилка тестування: {e}", exc_info=True)
    
    finally:
        # Закриваємо адаптер
        try:
            await adapter.__aexit__(None, None, None)
            print("\n✅ Адаптер закрито")
        except:
            pass

async def main():
    """Головна функція"""
    await test_twitter_monitor_adapter()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Тестування перервано користувачем")
    except Exception as e:
        print(f"\n❌ Неочікувана помилка: {e}")
