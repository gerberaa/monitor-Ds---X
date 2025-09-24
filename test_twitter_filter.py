#!/usr/bin/env python3
"""
Тестовий скрипт для перевірки фільтрації Twitter посилань
"""

import re

def is_twitter_link_valid(tweet_text: str, username: str) -> bool:
    """Перевірити чи посилання в твіті відповідає проекту"""
    if not tweet_text:
        return True
        
    # Шукаємо всі Twitter посилання в тексті
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
            print(f"🚫 Фільтрація твіта: посилання на {link_username} не відповідає проекту {username}")
            return False
            
    return True

def test_twitter_filter():
    """Тестування фільтрації Twitter посилань"""
    print("🧪 Тестування фільтрації Twitter посилань...")
    
    # Тест 1: Твіт без посилань (має пройти)
    tweet1 = "Це звичайний твіт без посилань"
    result1 = is_twitter_link_valid(tweet1, "GoKiteAI")
    print(f"✅ Тест 1 (без посилань): {result1}")
    
    # Тест 2: Твіт з посиланням на той самий акаунт (має пройти)
    tweet2 = "Перевірте наш новий продукт https://x.com/GoKiteAI/status/1234567890"
    result2 = is_twitter_link_valid(tweet2, "GoKiteAI")
    print(f"✅ Тест 2 (посилання на той самий акаунт): {result2}")
    
    # Тест 3: Твіт з посиланням на інший акаунт (має бути заблокований)
    tweet3 = "Цікавий твіт від іншого акаунта https://x.com/OtherAccount/status/1234567890"
    result3 = is_twitter_link_valid(tweet3, "GoKiteAI")
    print(f"❌ Тест 3 (посилання на інший акаунт): {result3}")
    
    # Тест 4: Твіт з кількома посиланнями (одне валідне, одне ні)
    tweet4 = "Наш твіт https://x.com/GoKiteAI/status/1234567890 та інший https://x.com/OtherAccount/status/0987654321"
    result4 = is_twitter_link_valid(tweet4, "GoKiteAI")
    print(f"❌ Тест 4 (змішані посилання): {result4}")
    
    # Тест 5: Твіт з посиланням на twitter.com (має пройти)
    tweet5 = "Наш твіт https://twitter.com/GoKiteAI/status/1234567890"
    result5 = is_twitter_link_valid(tweet5, "GoKiteAI")
    print(f"✅ Тест 5 (twitter.com посилання): {result5}")
    
    # Тест 6: Твіт з посиланням на інший акаунт (має бути заблокований)
    tweet6 = "Ретвіт https://x.com/elonmusk/status/1234567890"
    result6 = is_twitter_link_valid(tweet6, "GoKiteAI")
    print(f"❌ Тест 6 (посилання на elonmusk): {result6}")
    
    print("\n🎯 Результати тестування:")
    print(f"   Валідні твіти (мають пройти): {sum([result1, result2, result5])}/3")
    print(f"   Невалідні твіти (мають бути заблоковані): {sum([not result3, not result4, not result6])}/3")

if __name__ == "__main__":
    test_twitter_filter()
