#!/usr/bin/env python3
"""
Simple launcher for Twitter Monitor

Usage:
    python start_monitor.py
"""

import asyncio
import subprocess
import sys
import os


def check_dependencies():
    """Check if required dependencies are installed."""
    try:
        import httpx
        import fake_useragent
        import aiosqlite
        return True
    except ImportError as e:
        print(f"❌ Missing dependency: {e}")
        print("💡 Install dependencies: pip install -r requirements.txt")
        return False


def check_accounts():
    """Check if accounts are configured."""
    if not os.path.exists("accounts.db"):
        print("❌ No accounts found!")
        print("💡 Add account first: python quick_add_account.py")
        return False
    
    # Check if accounts.db has content
    if os.path.getsize("accounts.db") == 0:
        print("❌ Empty accounts database!")
        print("💡 Add account first: python quick_add_account.py")
        return False
    
    return True


def main():
    """Main launcher."""
    print("🚀 Twitter Monitor Launcher")
    print("=" * 30)
    
    # Check dependencies
    if not check_dependencies():
        return
    
    # Check accounts
    if not check_accounts():
        choice = input("\nWould you like to add an account now? (y/n): ").lower()
        if choice == 'y':
            os.system("python quick_add_account.py")
        else:
            return
    
    # Get username to monitor
    username = input("\n👤 Enter username to monitor (without @): ").strip()
    if not username:
        print("❌ Username is required")
        return
    
    # Get interval
    try:
        interval = input("⏱️  Check interval in seconds (default 60): ").strip()
        interval = int(interval) if interval else 60
        
        if interval < 10:
            print("⚠️  Setting minimum interval to 10 seconds")
            interval = 10
    except ValueError:
        interval = 60
    
    print(f"\n🎯 Starting monitor for @{username} with {interval}s interval")
    print("Press Ctrl+C to stop\n")
    
    # Start monitoring
    try:
        # Create a simple monitor script call
        cmd = [sys.executable, "monitor_profile.py", username]
        
        # Set environment variable for interval
        env = os.environ.copy()
        env["MONITOR_INTERVAL"] = str(interval)
        
        # Run the monitor
        subprocess.run(cmd, env=env)
        
    except KeyboardInterrupt:
        print("\n👋 Monitor stopped")
    except Exception as e:
        print(f"\n❌ Error: {e}")


if __name__ == "__main__":
    main()
