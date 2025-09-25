#!/usr/bin/env python3
"""
Quick Account Check

Fast check to see if your Twitter/X account is working.

Usage:
    python quick_check.py
"""

import asyncio
import sys
from twscrape import API


async def quick_test():
    """Quick test of account functionality."""
    print("⚡ Quick Account Check")
    print("=" * 30)
    
    api = API()
    
    # Check if we have accounts
    try:
        accounts = await api.pool.accounts_info()
        
        if not accounts:
            print("❌ No accounts found!")
            print("💡 Add account first: python quick_add_account.py")
            return False
        
        active_accounts = [acc for acc in accounts if acc.get('active', False)]
        
        if not active_accounts:
            print("❌ No active accounts found!")
            print("💡 Check account status: python check_account.py")
            return False
        
        print(f"✅ Found {len(active_accounts)} active account(s)")
        
        # Quick test - try to get a public user
        print("🔍 Testing API access...")
        
        user = await api.user_by_login("elonmusk")  # Use Elon Musk as test (more reliable)
        
        if user:
            print(f"✅ API working! Successfully fetched @{user.username}")
            print(f"   👥 Followers: {user.followersCount:,}")
            print(f"   📝 Tweets: {user.statusesCount:,}")
            return True
        else:
            print("❌ API test failed - couldn't fetch user data")
            return False
            
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        
        # Common error messages and solutions
        error_str = str(e).lower()
        if "unauthorized" in error_str or "forbidden" in error_str:
            print("💡 Solution: Your cookies may be expired. Try re-adding your account.")
        elif "rate limit" in error_str:
            print("💡 Solution: Rate limited. Wait 15 minutes and try again.")
        elif "no account" in error_str:
            print("💡 Solution: Add an account first: python quick_add_account.py")
        else:
            print("💡 Solution: Run full check: python check_account.py")
        
        return False


async def main():
    """Main function."""
    success = await quick_test()
    
    if success:
        print("\n🎉 Account is working!")
        print("🚀 Ready to extract tweet data!")
        print("\n📝 Usage: python extract_post_data.py <tweet_url>")
    else:
        print("\n❌ Account check failed!")
        print("🔧 Run detailed check: python check_account.py")
    
    return success


if __name__ == "__main__":
    try:
        result = asyncio.run(main())
        sys.exit(0 if result else 1)
    except KeyboardInterrupt:
        print("\n\n👋 Check cancelled.")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        sys.exit(1)
