#!/usr/bin/env python3
"""
Швидкий тест Twitter Monitor Adapter
"""

import asyncio
import sys
import os

# Додаємо twitter_monitor до шляху
sys.path.append(os.path.join(os.path.dirname(__file__), 'twitter_monitor'))

async def quick_test():
    """Швидкий тест основної функціональності"""
    print("🚀 Швидкий тест Twitter Monitor Adapter")
    print("=" * 45)
    
    try:
        from twitter_monitor_adapter import TwitterMonitorAdapter
        
        # Ініціалізуємо адаптер
        print("1. Ініціалізація...")
        adapter = TwitterMonitorAdapter()
        print("✅ Адаптер готовий")
        
        # Тестуємо додавання акаунта
        print("\n2. Тестування додавання акаунта...")
        test_user = "elonmusk"  # Використовуємо відомий публічний акаунт
        if adapter.add_account(test_user):
            print(f"✅ Акаунт @{test_user} додано")
        else:
            print(f"❌ Помилка додавання @{test_user}")
            return False
        
        # Тестуємо отримання твітів
        print(f"\n3. Тестування отримання твітів...")
        tweets = await adapter.get_user_tweets(test_user, limit=2)
        
        if tweets:
            print(f"✅ Отримано {len(tweets)} твітів")
            for i, tweet in enumerate(tweets, 1):
                preview = tweet['text'][:50] + "..." if len(tweet['text']) > 50 else tweet['text']
                print(f"   {i}. {preview}")
        else:
            print("❌ Твіти не отримані")
            return False
        
        # Тестуємо перевірку нових твітів
        print("\n4. Тестування перевірки нових твітів...")
        new_tweets = await adapter.check_new_tweets()
        print(f"✅ Знайдено {len(new_tweets)} нових твітів")
        
        print("\n🎉 Всі тести пройшли успішно!")
        print("\n📋 Наступні кроки:")
        print("1. Додайте свій Twitter акаунт: cd twitter_monitor && python quick_add_account.py")
        print("2. Запустіть бота: python bot.py")
        print("3. Використовуйте команди /twitter_add, /twitter_test, /twitter_start")
        
        return True
        
    except ImportError as e:
        print(f"❌ Помилка імпорту: {e}")
        print("💡 Переконайтеся що twitter_monitor встановлено: pip install -r twitter_monitor/requirements.txt")
        return False
    except Exception as e:
        print(f"❌ Помилка тестування: {e}")
        return False
    finally:
        try:
            await adapter.__aexit__(None, None, None)
        except:
            pass

def main():
    """Головна функція"""
    try:
        result = asyncio.run(quick_test())
        sys.exit(0 if result else 1)
    except KeyboardInterrupt:
        print("\n👋 Тест перервано")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Неочікувана помилка: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
