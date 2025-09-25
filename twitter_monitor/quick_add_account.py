#!/usr/bin/env python3
"""
Quick Account Setup - Add account with just auth_token

This is a simplified script to quickly add a Twitter/X account using just the auth_token.
Perfect for when you have cookies and don't want to fill all the fields.

Usage:
    python quick_add_account.py
"""

import asyncio
import getpass
from twscrape import API


async def quick_add_account():
    """Quick account setup with minimal requirements."""
    print("🚀 Quick Twitter/X Account Setup")
    print("=" * 40)
    print()
    print("This script will quickly add your account using cookies/auth_token.")
    print("You only need: username, auth_token (and optionally ct0)")
    print()
    
    # Get username
    username = input("📝 Username (without @): ").strip()
    if not username:
        print("❌ Username is required")
        return False
    
    # Get auth_token
    print("\n🔑 Getting auth_token:")
    print("1. Go to https://x.com in your browser (logged in)")
    print("2. Open Developer Tools (F12)")
    print("3. Go to Application/Storage > Cookies > https://x.com")
    print("4. Find 'auth_token' and copy its value")
    print()
    
    auth_token = getpass.getpass("🔐 Enter auth_token: ").strip()
    if not auth_token:
        print("❌ auth_token is required")
        return False
    
    # Get ct0 (optional but recommended)
    ct0 = getpass.getpass("🔐 Enter ct0 (optional, press Enter to skip): ").strip()
    
    # Build cookies string
    if ct0:
        cookies = f"auth_token={auth_token}; ct0={ct0}"
    else:
        cookies = f"auth_token={auth_token}"
    
    print(f"\n🍪 Cookies prepared: auth_token=***...{auth_token[-4:]} {'+ ct0=***...' + ct0[-4:] if ct0 else ''}")
    
    # Add account
    try:
        api = API()
        
        # Use dummy values for required but unused fields when using cookies
        dummy_password = "dummy_pass"
        dummy_email = f"{username}@dummy.com"
        dummy_email_pass = "dummy_email_pass"
        
        await api.pool.add_account(
            username=username,
            password=dummy_password,
            email=dummy_email,
            email_password=dummy_email_pass,
            cookies=cookies
        )
        
        print(f"✅ Account @{username} added successfully!")
        print("💡 You can now use the data extraction script!")
        
        return True
        
    except Exception as e:
        print(f"❌ Error adding account: {e}")
        return False


async def main():
    """Main function."""
    print("Welcome to the quick account setup!")
    print()
    
    success = await quick_add_account()
    
    if success:
        print("\n🎉 Setup complete!")
        print("\n📋 Next steps:")
        print("1. Test your setup: python setup_accounts.py (option 4 - Show account status)")
        print("2. Extract tweet data: python extract_post_data.py <tweet_url>")
    else:
        print("\n❌ Setup failed. Please try again.")
    
    print("\n👋 Goodbye!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n👋 Setup cancelled by user.")
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
