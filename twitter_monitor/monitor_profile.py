#!/usr/bin/env python3
"""
Twitter/X Profile Monitor

This script continuously monitors a specific Twitter/X profile for new posts
and automatically extracts data from them.

Usage:
    python monitor_profile.py @username
    python monitor_profile.py elonmusk
    
Example:
    python monitor_profile.py @elonmusk
"""

import asyncio
import json
import sys
import os
from datetime import datetime, timezone
from typing import Set, Optional
import time

from twscrape import API
from twscrape.models import Tweet, User


class ProfileMonitor:
    def __init__(self, username: str, check_interval: int = 60):
        """
        Initialize profile monitor.
        
        Args:
            username: Twitter username to monitor (with or without @)
            check_interval: How often to check for new posts (seconds)
        """
        self.username = username.lstrip('@')
        self.check_interval = check_interval
        self.api = API()
        self.seen_tweet_ids: Set[int] = set()
        self.user_info: Optional[User] = None
        self.monitoring = False
        
        # Create output directory
        self.output_dir = f"monitor_{self.username}"
        os.makedirs(self.output_dir, exist_ok=True)
        
        # Load previously seen tweets
        self.load_seen_tweets()
    
    def load_seen_tweets(self):
        """Load previously seen tweet IDs from file."""
        seen_file = os.path.join(self.output_dir, "seen_tweets.json")
        if os.path.exists(seen_file):
            try:
                with open(seen_file, 'r') as f:
                    data = json.load(f)
                    self.seen_tweet_ids = set(data.get('seen_ids', []))
                print(f"📚 Loaded {len(self.seen_tweet_ids)} previously seen tweets")
            except Exception as e:
                print(f"⚠️  Could not load seen tweets: {e}")
    
    def save_seen_tweets(self):
        """Save seen tweet IDs to file."""
        seen_file = os.path.join(self.output_dir, "seen_tweets.json")
        try:
            data = {
                'seen_ids': list(self.seen_tweet_ids),
                'last_update': datetime.now().isoformat(),
                'username': self.username
            }
            with open(seen_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"⚠️  Could not save seen tweets: {e}")
    
    async def get_user_info(self) -> bool:
        """Get user information."""
        try:
            self.user_info = await self.api.user_by_login(self.username)
            if self.user_info:
                print(f"👤 Monitoring: @{self.user_info.username} ({self.user_info.displayname})")
                print(f"📊 Followers: {self.user_info.followersCount:,}")
                print(f"📝 Total tweets: {self.user_info.statusesCount:,}")
                return True
            else:
                print(f"❌ User @{self.username} not found or not accessible")
                return False
        except Exception as e:
            print(f"❌ Error fetching user info: {e}")
            return False
    
    async def save_tweet_data(self, tweet: Tweet):
        """Save comprehensive tweet data to file."""
        try:
            # Create filename with timestamp and tweet ID
            timestamp = tweet.date.strftime("%Y%m%d_%H%M%S")
            filename = f"tweet_{timestamp}_{tweet.id}.json"
            filepath = os.path.join(self.output_dir, filename)
            
            # Prepare comprehensive data
            data = {
                "tweet": {
                    "id": tweet.id,
                    "id_str": tweet.id_str,
                    "url": tweet.url,
                    "date": tweet.date.isoformat(),
                    "content": tweet.rawContent,
                    "language": tweet.lang,
                    "engagement": {
                        "replies": tweet.replyCount,
                        "retweets": tweet.retweetCount,
                        "likes": tweet.likeCount,
                        "quotes": tweet.quoteCount,
                        "bookmarks": tweet.bookmarkedCount,
                        "views": tweet.viewCount
                    },
                    "hashtags": tweet.hashtags,
                    "cashtags": tweet.cashtags,
                    "mentioned_users": [user.dict() for user in tweet.mentionedUsers],
                    "links": [link.dict() for link in tweet.links],
                    "source": tweet.source,
                    "source_label": tweet.sourceLabel
                },
                "user": tweet.user.dict(),
                "media": None,
                "location": None,
                "reply_info": None,
                "retweet_info": None,
                "quote_info": None,
                "card": None,
                "extracted_at": datetime.now().isoformat()
            }
            
            # Add media if present
            if tweet.media:
                data["media"] = {
                    "photos": [photo.dict() for photo in tweet.media.photos],
                    "videos": [video.dict() for video in tweet.media.videos],
                    "animated": [anim.dict() for anim in tweet.media.animated]
                }
            
            # Add location if present
            if tweet.place:
                data["location"] = {
                    "place": tweet.place.dict(),
                    "coordinates": tweet.coordinates.dict() if tweet.coordinates else None
                }
            
            # Add reply info
            if tweet.inReplyToTweetId:
                data["reply_info"] = {
                    "in_reply_to_tweet_id": tweet.inReplyToTweetId,
                    "in_reply_to_user": tweet.inReplyToUser.dict() if tweet.inReplyToUser else None
                }
            
            # Add retweet info
            if tweet.retweetedTweet:
                data["retweet_info"] = {
                    "original_tweet": tweet.retweetedTweet.dict(),
                    "original_user": tweet.retweetedTweet.user.dict()
                }
            
            # Add quote info
            if tweet.quotedTweet:
                data["quote_info"] = {
                    "quoted_tweet": tweet.quotedTweet.dict(),
                    "quoted_user": tweet.quotedTweet.user.dict()
                }
            
            # Add card info
            if tweet.card:
                data["card"] = tweet.card.dict()
            
            # Save to file
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            
            print(f"💾 Saved: {filename}")
            return filepath
            
        except Exception as e:
            print(f"❌ Error saving tweet data: {e}")
            return None
    
    def format_tweet_notification(self, tweet: Tweet) -> str:
        """Format tweet for console notification."""
        content = tweet.rawContent
        if len(content) > 100:
            content = content[:97] + "..."
        
        engagement = f"❤️{tweet.likeCount} 🔄{tweet.retweetCount} 💬{tweet.replyCount}"
        
        media_info = ""
        if tweet.media:
            photo_count = len(tweet.media.photos)
            video_count = len(tweet.media.videos)
            gif_count = len(tweet.media.animated)
            
            if photo_count > 0:
                media_info += f" 📸{photo_count}"
            if video_count > 0:
                media_info += f" 🎥{video_count}"
            if gif_count > 0:
                media_info += f" 🎬{gif_count}"
        
        return f"""
🆕 NEW TWEET from @{tweet.user.username}
📅 {tweet.date.strftime('%Y-%m-%d %H:%M:%S')}
📝 {content}
📊 {engagement}{media_info}
🔗 {tweet.url}
"""
    
    async def check_for_new_tweets(self) -> int:
        """Check for new tweets and process them."""
        try:
            new_tweets = 0
            tweets_checked = 0
            
            # Get recent tweets (limit to 20 to avoid rate limits)
            async for tweet in self.api.user_tweets(self.user_info.id, limit=20):
                tweets_checked += 1
                
                # Skip if we've already seen this tweet
                if tweet.id in self.seen_tweet_ids:
                    continue
                
                # New tweet found!
                print(self.format_tweet_notification(tweet))
                
                # Save tweet data
                await self.save_tweet_data(tweet)
                
                # Add to seen tweets
                self.seen_tweet_ids.add(tweet.id)
                new_tweets += 1
            
            # Save updated seen tweets
            if new_tweets > 0:
                self.save_seen_tweets()
            
            return new_tweets
            
        except Exception as e:
            print(f"❌ Error checking for new tweets: {e}")
            return 0
    
    async def start_monitoring(self):
        """Start continuous monitoring."""
        print(f"🚀 Starting profile monitor for @{self.username}")
        print(f"⏱️  Check interval: {self.check_interval} seconds")
        print(f"📁 Output directory: {self.output_dir}")
        print("=" * 60)
        
        # Get user info
        if not await self.get_user_info():
            return
        
        # Initial load of recent tweets to avoid spam
        print("🔄 Loading recent tweets to establish baseline...")
        try:
            async for tweet in self.api.user_tweets(self.user_info.id, limit=10):
                self.seen_tweet_ids.add(tweet.id)
            self.save_seen_tweets()
            print(f"✅ Baseline established with {len(self.seen_tweet_ids)} tweets")
        except Exception as e:
            print(f"⚠️  Could not establish baseline: {e}")
        
        self.monitoring = True
        print(f"\n👀 Now monitoring @{self.username} for new tweets...")
        print("Press Ctrl+C to stop monitoring\n")
        
        try:
            while self.monitoring:
                start_time = time.time()
                
                # Check for new tweets
                new_count = await self.check_for_new_tweets()
                
                if new_count == 0:
                    current_time = datetime.now().strftime('%H:%M:%S')
                    print(f"⏰ {current_time} - No new tweets from @{self.username}")
                
                # Wait for next check
                elapsed = time.time() - start_time
                sleep_time = max(0, self.check_interval - elapsed)
                
                if sleep_time > 0:
                    await asyncio.sleep(sleep_time)
                    
        except KeyboardInterrupt:
            print("\n🛑 Monitoring stopped by user")
        except Exception as e:
            print(f"\n❌ Monitoring error: {e}")
        finally:
            self.monitoring = False
            print(f"📊 Final stats: {len(self.seen_tweet_ids)} total tweets tracked")


async def main():
    """Main function."""
    if len(sys.argv) != 2:
        print("Usage: python monitor_profile.py <username>")
        print("\nExamples:")
        print("python monitor_profile.py @elonmusk")
        print("python monitor_profile.py elonmusk")
        sys.exit(1)
    
    username = sys.argv[1]
    
    print("🔧 Twitter/X Profile Monitor")
    print("=" * 40)
    print(f"Target: {username}")
    print()
    
    # Ask for check interval
    try:
        interval_input = input("⏱️  Check interval in seconds (default 60): ").strip()
        interval = int(interval_input) if interval_input else 60
        
        if interval < 10:
            print("⚠️  Minimum interval is 10 seconds (to avoid rate limits)")
            interval = 10
            
    except ValueError:
        interval = 60
        print("Using default interval: 60 seconds")
    
    # Create and start monitor
    monitor = ProfileMonitor(username, interval)
    await monitor.start_monitoring()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Goodbye!")
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        sys.exit(1)
