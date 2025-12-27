#!/usr/bin/env python3
"""
Reddit Giveaway Monitor with Discord Notifications
Monitors r/plsdonategame for giveaway posts and sends Discord alerts.
Supports both system tray (GUI) and console mode for headless systems.
"""

import threading
import time
import os
import sys
import logging
import queue
import re
import signal
from collections import deque
from datetime import datetime, timezone, timedelta
from logging.handlers import RotatingFileHandler
from typing import Optional, Tuple, Dict, List, Any, Iterator
from dataclasses import dataclass, field
from PIL import Image, ImageDraw

# ------------------ DEPENDENCY CHECK ------------------
missing_deps = []
optional_deps = []

try:
    import praw 
    from praw.exceptions import RedditAPIException, PRAWException
    from praw.models import Submission
except ImportError:
    missing_deps.append("praw")

try:
    import requests
except ImportError:
    missing_deps.append("requests")

try:
    from dotenv import load_dotenv
except ImportError:
    missing_deps.append("python-dotenv")

# pystray is optional for tray functionality
try:
    import pystray
    from pystray import MenuItem as item
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False
    optional_deps.append("pystray")

if missing_deps:
    print(f"ERROR: Missing required dependencies: {', '.join(missing_deps)}")
    print(f"Install with: pip install {' '.join(missing_deps)}")
    sys.exit(1)

if optional_deps:
    print(f"INFO: Optional dependencies not available: {', '.join(optional_deps)}")
    print("System tray functionality will be disabled. Console mode will be used.")
    print(f"To enable tray: pip install {' '.join(optional_deps)}")
    print()

# ------------------ CONFIGURATION ------------------
load_dotenv()
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@dataclass
class Config:
    """Centralized configuration for the giveaway monitor."""
    # Subreddit
    SUBREDDIT: str = "plsdonategame"
    
    # File paths
    SEEN_FILE: str = os.path.join(BASE_DIR, "seen_posts.txt")
    FAILED_NOTIFICATIONS_FILE: str = os.path.join(BASE_DIR, "failed_notifications.txt")
    LOG_FILE: str = os.path.join(BASE_DIR, "giveaway_monitor.log")
    SHUTDOWN_TIME_FILE: str = os.path.join(BASE_DIR, "last_shutdown.txt")
    
    # Timing (seconds)
    FAST_INTERVAL: int = 5
    DEEP_INTERVAL: int = 60
    SAVE_INTERVAL: int = 300
    SHUTDOWN_TIMEOUT: int = 30
    THREAD_JOIN_TIMEOUT: float = 10.0
    WORKER_HEALTH_CHECK_INTERVAL: int = 30
    
    # Discord rate limiting (5 messages per 2 seconds)
    DISCORD_RATE_LIMIT_MESSAGES: int = 5
    DISCORD_RATE_LIMIT_WINDOW: float = 2.0
    
    # Retry configuration
    MAX_RETRY_ATTEMPTS: int = 3
    MAX_BACKOFF_TIME: int = 60
    BASE_BACKOFF: int = 2
    REDDIT_RECONNECT_ATTEMPTS: int = 5
    REDDIT_RECONNECT_DELAY: int = 30
    
    # Limits
    FAST_LIMIT: int = 10
    DEEP_LIMIT: int = 60
    MAX_SEEN_POSTS: int = 2000
    MAX_TITLE_LENGTH: int = 250
    MAX_DESC_LENGTH: int = 1000
    
    # Post filtering
    POST_MAX_AGE_HOURS: int = 24
    TARGET_FLAIRS: List[str] = field(default_factory=lambda: ["Free Giveaway", "Requirement Giveaway"])
    
    # Discord
    WEBHOOK_URL: Optional[str] = os.getenv("DISCORD_WEBHOOK_URL")
    ROLE_ID: Optional[str] = os.getenv("DISCORD_ROLE_ID")
    
    # Reddit credentials
    REDDIT_CLIENT_ID: Optional[str] = os.getenv("REDDIT_CLIENT_ID")
    REDDIT_CLIENT_SECRET: Optional[str] = os.getenv("REDDIT_CLIENT_SECRET")
    REDDIT_USERNAME: Optional[str] = os.getenv("REDDIT_USERNAME")
    REDDIT_PASSWORD: Optional[str] = os.getenv("REDDIT_PASSWORD")
    REDDIT_USER_AGENT: str = ""
    
    # Required environment variables
    REQUIRED_ENV_VARS: List[str] = field(default_factory=lambda: [
        "REDDIT_CLIENT_ID",
        "REDDIT_CLIENT_SECRET",
        "REDDIT_USERNAME",
        "REDDIT_PASSWORD",
        "DISCORD_WEBHOOK_URL"
    ])
    
    def __post_init__(self):
        """Validate configuration after initialization."""
        self._validate_reddit_agent()
        self._validate_config()
    
    def _validate_reddit_agent(self) -> None:
        """Ensure user agent is properly formed."""
        if not self.REDDIT_USERNAME:
            logging.warning("REDDIT_USERNAME not set - using generic user agent")
            self.REDDIT_USER_AGENT = "giveaway_monitor/1.0"
        else:
            self.REDDIT_USER_AGENT = f"giveaway_monitor/1.0 by u/{self.REDDIT_USERNAME}"
    
    def _validate_config(self) -> None:
        """Validate configuration values."""
        if self.MAX_SEEN_POSTS < 100:
            raise ValueError("MAX_SEEN_POSTS must be at least 100")
        
        if self.WEBHOOK_URL and not self._is_valid_webhook_url(self.WEBHOOK_URL):
            raise ValueError("Invalid Discord webhook URL format")
        
        if self.ROLE_ID and not self._is_valid_snowflake(self.ROLE_ID):
            logging.warning(f"ROLE_ID '{self.ROLE_ID}' may not be a valid Discord snowflake")
    
    @staticmethod
    def _is_valid_webhook_url(url: str) -> bool:
        """Validate Discord webhook URL format."""
        pattern = r'^https://discord\.com/api/webhooks/\d+/[\w-]+$'
        return bool(re.match(pattern, url))
    
    @staticmethod
    def _is_valid_snowflake(snowflake: str) -> bool:
        """Validate Discord snowflake ID format."""
        return snowflake.isdigit() and len(snowflake) >= 17


config = Config()


# ------------------ LOGGING ------------------
def setup_logging() -> None:
    """Configure logging with rotating file handler and console output."""
    log_handler = RotatingFileHandler(
        config.LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding='utf-8'
    )
    
    console_handler = logging.StreamHandler()
    
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(levelname)s: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=[console_handler, log_handler]
    )


setup_logging()


# ------------------ RATE LIMITER ------------------
class RateLimiter:
    """
    Token bucket rate limiter for Discord API.
    Thread-safe implementation that releases lock during sleep.
    """
    
    def __init__(self, max_messages: int, time_window: float):
        self.max_messages = max_messages
        self.time_window = time_window
        self.tokens = max_messages
        self.last_update = time.time()
        self.lock = threading.Lock()
    
    def acquire(self) -> None:
        """Wait until a token is available. Releases lock during sleep to prevent blocking."""
        while True:
            with self.lock:
                now = time.time()
                elapsed = now - self.last_update
                
                # Refill tokens based on elapsed time
                self.tokens = min(
                    self.max_messages,
                    self.tokens + (elapsed / self.time_window) * self.max_messages
                )
                self.last_update = now
                
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                
                # Calculate wait time
                wait_time = (1 - self.tokens) * (self.time_window / self.max_messages)
            
            # Sleep OUTSIDE the lock to prevent blocking other threads
            time.sleep(min(wait_time, 0.1))  # Cap at 100ms for responsiveness


# ------------------ MAIN MONITOR CLASS ------------------
class GiveawayMonitor:
    """
    Monitors a subreddit for giveaway posts and sends notifications to Discord.
    
    This class uses multiple threads:
    - Main thread: Reddit monitoring loop
    - Discord worker thread: Sends notifications from queue
    - Health monitor thread: Ensures worker thread stays alive
    - System tray thread (optional): GUI interaction
    """
    
    def __init__(self):
        """Initialize the monitor with all required components."""
        self._validate_environment()
        
        # Threading primitives
        self._stop_event = threading.Event()
        self._pause_lock = threading.RLock()
        self._is_paused = False
        self.alert_queue: queue.Queue = queue.Queue()
        
        # Worker health tracking
        self.discord_worker_alive = threading.Event()
        self.discord_worker_alive.set()
        self.discord_thread: Optional[threading.Thread] = None
        
        # Rate limiter for Discord
        self.rate_limiter = RateLimiter(
            config.DISCORD_RATE_LIMIT_MESSAGES,
            config.DISCORD_RATE_LIMIT_WINDOW
        )
        
        # Statistics tracking
        self.stats = {
            "total_checked": 0,
            "giveaways_found": 0,
            "failed_notifications": 0,
            "start_time": time.time(),
            "status": "Initializing",
            "last_check": None,
            "reddit_reconnects": 0,
            "worker_restarts": 0
        }
        self.stats_lock = threading.Lock()
        
        # Seen posts tracking
        self.seen_posts_list: deque = deque(maxlen=config.MAX_SEEN_POSTS)
        self.seen_posts_set: set = set()
        self.seen_lock = threading.Lock()
        
        # Reddit connection
        self.reddit: Optional[praw.Reddit] = None
        self.reddit_lock = threading.Lock()
        self._connect_reddit()
        
        # Load historical data
        self._load_seen_posts()
        self._last_shutdown_time = self._load_shutdown_time()

    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensures cleanup."""
        self.stop()
        return False
    
    def __del__(self):
        """Destructor - final cleanup."""
        try:
            if hasattr(self, '_stop_event') and not self._stop_event.is_set():
                self.stop()
        except:
            pass  # Ignore errors during cleanup

    def _validate_environment(self) -> None:
        """
        Validate all required environment variables exist.
        
        Raises:
            SystemExit: If any required variables are missing.
        """
        missing = [var for var in config.REQUIRED_ENV_VARS if not os.getenv(var)]
        
        if missing:
            logging.error("=" * 70)
            logging.error("CONFIGURATION ERROR: Missing environment variables")
            logging.error("=" * 70)
            for var in missing:
                logging.error(f"  [MISSING] {var}")
            logging.error("")
            logging.error("Please create a .env file in the same directory with:")
            logging.error("")
            for var in config.REQUIRED_ENV_VARS:
                logging.error(f"  {var}=your_value_here")
            logging.error("")
            logging.error("=" * 70)
            sys.exit(1)
        
        logging.info("[OK] Environment variables validated")

    def _connect_reddit(self, attempt: int = 1) -> bool:
        """
        Establish connection to Reddit API with error handling and retry logic.
        
        Args:
            attempt: Current connection attempt number.
            
        Returns:
            True if connection successful, False otherwise.
        """
        try:
            with self.reddit_lock:
                self.reddit = praw.Reddit(
                    client_id=config.REDDIT_CLIENT_ID,
                    client_secret=config.REDDIT_CLIENT_SECRET,
                    username=config.REDDIT_USERNAME,
                    password=config.REDDIT_PASSWORD,
                    user_agent=config.REDDIT_USER_AGENT
                )
                # Test connection
                self.reddit.user.me()
                logging.info("[OK] Connected to Reddit API")
                return True
                
        except PRAWException as e:
            if attempt >= config.REDDIT_RECONNECT_ATTEMPTS:
                logging.error(f"[FAIL] Reddit Connection Failed after {attempt} attempts: {e}")
                logging.error("Check your Reddit credentials in .env file")
                return False
            
            logging.warning(f"[WARN] Reddit connection attempt {attempt} failed: {e}")
            logging.info(f"Retrying in {config.REDDIT_RECONNECT_DELAY}s...")
            time.sleep(config.REDDIT_RECONNECT_DELAY)
            
            self._update_stat("reddit_reconnects", lambda x: x + 1)
            
            return self._connect_reddit(attempt + 1)
            
        except Exception as e:
            logging.exception(f"[FAIL] Unexpected Reddit error: {e}")
            return False

    def _close_reddit(self) -> None:
        """Explicitly close Reddit connection."""
        with self.reddit_lock:
            if self.reddit:
                self.reddit = None
                logging.info("[OK] Reddit connection closed")

    def _get_stat(self, key: str) -> Any:
        """Thread-safe statistics getter."""
        with self.stats_lock:
            return self.stats.get(key)

    def _set_stat(self, key: str, value: Any) -> None:
        """Thread-safe statistics setter."""
        with self.stats_lock:
            self.stats[key] = value

    def _update_stat(self, key: str, func) -> None:
        """Thread-safe statistics updater with function."""
        with self.stats_lock:
            self.stats[key] = func(self.stats.get(key, 0))

    def _load_shutdown_time(self) -> Optional[float]:
        """
        Load the last shutdown timestamp.
        
        Returns:
            Unix timestamp of last shutdown, or None if not available.
        """
        if os.path.exists(config.SHUTDOWN_TIME_FILE):
            try:
                with open(config.SHUTDOWN_TIME_FILE, "r", encoding='utf-8') as f:
                    timestamp = float(f.read().strip())
                    dt = datetime.fromtimestamp(timestamp)
                    logging.info(f"[OK] Last shutdown: {dt.strftime('%Y-%m-%d %H:%M:%S')}")
                    return timestamp
            except Exception as e:
                logging.warning(f"[WARN] Could not load shutdown time: {e}")
        return None

    def _save_shutdown_time(self) -> None:
        """Save the current time as shutdown timestamp."""
        try:
            with open(config.SHUTDOWN_TIME_FILE, "w", encoding='utf-8') as f:
                f.write(str(time.time()))
        except Exception as e:
            logging.exception(f"[FAIL] Failed to save shutdown time: {e}")

    def _load_seen_posts(self) -> None:
        """Load previously seen posts from file."""
        if os.path.exists(config.SEEN_FILE):
            try:
                with open(config.SEEN_FILE, "r", encoding='utf-8') as f:
                    ids = [line.strip() for line in f if line.strip()]
                    self.seen_posts_list.extend(ids)
                    self.seen_posts_set.update(ids)
                logging.info(f"[OK] Loaded {len(self.seen_posts_set)} seen posts")
            except Exception as e:
                logging.exception(f"[WARN] Failed to load history: {e}")

    def save_state(self) -> None:
        """
        Atomically save seen posts to file using temp file pattern.
        
        This ensures the file is never corrupted even if the process crashes.
        """
        try:
            with self.seen_lock:
                data = list(self.seen_posts_list)
            
            temp_file = config.SEEN_FILE + ".tmp"
            with open(temp_file, "w", encoding='utf-8') as f:
                f.write("\n".join(data))
            
            # Atomic rename
            os.replace(temp_file, config.SEEN_FILE)
        except Exception as e:
            logging.exception(f"[FAIL] Save failed: {e}")

    def _log_failed_notification(self, post_data: Dict[str, str]) -> None:
        """
        Log failed Discord notifications for manual review.
        
        Args:
            post_data: Dictionary containing post information.
        """
        try:
            with open(config.FAILED_NOTIFICATIONS_FILE, "a", encoding='utf-8') as f:
                timestamp = datetime.now().isoformat()
                f.write(f"\n{'='*60}\n")
                f.write(f"Failed at: {timestamp}\n")
                f.write(f"Title: {post_data['title']}\n")
                f.write(f"URL: {post_data['url']}\n")
                f.write(f"Flair: {post_data['flair']}\n")
                f.write(f"{'='*60}\n")
            logging.warning(f"Logged failed notification to {config.FAILED_NOTIFICATIONS_FILE}")
        except Exception as e:
            logging.exception(f"Failed to log failed notification: {e}")

    def _is_giveaway(self, post: Submission) -> Tuple[bool, str]:
        """
        Determine if a post is a valid giveaway based on flair and age.
        
        Checks are ordered by performance: flair first (fastest), then age.
        
        Args:
            post: PRAW submission object.
            
        Returns:
            Tuple of (is_valid, reason_code).
        """
        # Check flair first (fastest check)
        flair = getattr(post, 'link_flair_text', None)
        if not flair or flair not in config.TARGET_FLAIRS:
            return False, "no_flair"

        # Check age - skip posts created before last shutdown to prevent duplicates
        if self._last_shutdown_time:
            if post.created_utc < self._last_shutdown_time:
                return False, "before_shutdown"
        
        # Also check maximum age
        age_hours = (time.time() - post.created_utc) / 3600
        if age_hours > config.POST_MAX_AGE_HOURS:
            return False, "too_old"
            
        return True, "valid"

    def _start_discord_worker(self) -> None:
        """Start or restart the Discord worker thread."""
        self.discord_thread = threading.Thread(
            target=self._discord_worker,
            daemon=True,
            name="DiscordWorker"
        )
        self.discord_thread.start()
        logging.info("[OK] Discord worker started")

    def _discord_worker(self) -> None:
        """
        Worker thread that sends queued Discord notifications.
        
        Runs continuously until stop event is set, processing the alert queue
        with proper rate limiting.
        """
        self.discord_worker_alive.set()
        
        try:
            while not self._stop_event.is_set():
                try:
                    post_data = self.alert_queue.get(timeout=2)
                except queue.Empty:
                    continue

                # Rate limit before sending
                self.rate_limiter.acquire()
                
                success = self._send_discord_payload(post_data)
                if not success:
                    self._log_failed_notification(post_data)
                    self._update_stat("failed_notifications", lambda x: x + 1)
                
                self.alert_queue.task_done()
        except Exception as e:
            logging.exception(f"[FAIL] Discord worker crashed: {e}")
        finally:
            self.discord_worker_alive.clear()
            logging.info("Discord worker shutting down")

    def _check_worker_health(self) -> None:
        """Check if Discord worker is alive and restart if needed."""
        if not self.discord_worker_alive.is_set():
            logging.warning("[WARN] Discord worker died! Attempting restart...")
            self._update_stat("worker_restarts", lambda x: x + 1)
            self._start_discord_worker()

    def _send_discord_payload(self, post_data: Dict[str, str]) -> bool:
        """
        Send a notification to Discord with exponential backoff retry.
        
        Args:
            post_data: Dictionary containing post information.
            
        Returns:
            True if notification was sent successfully, False otherwise.
        """
        content = f"<@&{config.ROLE_ID}>" if config.ROLE_ID else ""
        
        payload = {
            "content": content,
            "embeds": [{
                "title": f"🎁 {post_data['title']}",
                "description": post_data['desc'],
                "url": post_data['url'],
                "color": 0xFF4500,
                "fields": [
                    {"name": "Subreddit", "value": f"r/{post_data['subreddit']}", "inline": True},
                    {"name": "Author", "value": f"u/{post_data['author']}", "inline": True},
                    {"name": "Flair", "value": post_data['flair'], "inline": True}
                ],
                "timestamp": post_data['timestamp']
            }]
        }

        for attempt in range(config.MAX_RETRY_ATTEMPTS):
            try:
                r = requests.post(
                    config.WEBHOOK_URL,
                    json=payload,
                    timeout=10
                )
                
                if r.status_code in [200, 204]:
                    logging.info(f"[SENT] {post_data['title'][:40]}...")
                    return True
                    
                elif r.status_code == 429:
                    # Handle rate limiting
                    retry_after = r.json().get('retry_after', 5)
                    logging.warning(f"[WARN] Discord rate limited. Waiting {retry_after}s...")
                    time.sleep(retry_after)
                    
                else:
                    logging.warning(f"[WARN] Discord returned status {r.status_code}")
                    backoff = min(config.BASE_BACKOFF ** attempt, config.MAX_BACKOFF_TIME)
                    time.sleep(backoff)
                    
            except requests.exceptions.Timeout:
                logging.warning(f"[WARN] Discord request timeout (attempt {attempt + 1}/{config.MAX_RETRY_ATTEMPTS})")
                if attempt < config.MAX_RETRY_ATTEMPTS - 1:
                    backoff = min(config.BASE_BACKOFF ** attempt, config.MAX_BACKOFF_TIME)
                    time.sleep(backoff)
                    
            except Exception as e:
                logging.exception(f"[FAIL] Discord send failed (attempt {attempt + 1}/{config.MAX_RETRY_ATTEMPTS}): {e}")
                if attempt < config.MAX_RETRY_ATTEMPTS - 1:
                    backoff = min(config.BASE_BACKOFF ** attempt, config.MAX_BACKOFF_TIME)
                    time.sleep(backoff)
        
        logging.error(f"[FAIL] Failed to send notification after {config.MAX_RETRY_ATTEMPTS} attempts")
        return False

    def _sanitize_text(self, text: str, max_length: int) -> str:
        """
        Sanitize and truncate text for Discord embeds.
        
        Args:
            text: Text to sanitize.
            max_length: Maximum allowed length.
            
        Returns:
            Sanitized and truncated text.
        """
        if not text:
            return ""
        
        text = text.strip()
        if len(text) > max_length:
            return text[:max_length-3] + "..."
        return text

    def _process_posts(self, posts: Iterator[Submission]) -> int:
        """
        Process a list of posts and queue valid giveaways for notification.
        
        Args:
            posts: Iterator of PRAW submission objects.
            
        Returns:
            Number of new giveaways found.
        """
        new_finds = 0
        
        for post in posts:
            if self._stop_event.is_set():
                break

            # Atomic check-and-add operation (fixes race condition)
            with self.seen_lock:
                if post.id in self.seen_posts_set:
                    continue
                
                # Add to set FIRST to prevent race condition
                self.seen_posts_set.add(post.id)
                self.seen_posts_list.append(post.id)
                
                # Trim set if it grows too large (memory leak prevention)
                if len(self.seen_posts_set) > config.MAX_SEEN_POSTS * 1.2:
                    self.seen_posts_set = set(self.seen_posts_list)

            self._update_stat("total_checked", lambda x: x + 1)

            # Check if post is a giveaway
            is_valid, reason = self._is_giveaway(post)
            
            if is_valid:
                try:
                    # Safely extract and sanitize post data
                    safe_desc = self._sanitize_text(post.selftext, config.MAX_DESC_LENGTH)
                    safe_title = self._sanitize_text(post.title, config.MAX_TITLE_LENGTH)
                    author_name = post.author.name if post.author else "[Deleted]"
                    
                    post_data = {
                        "title": safe_title or "Untitled Post",
                        "desc": safe_desc or "No content.",
                        "url": f"https://reddit.com{post.permalink}",
                        "subreddit": post.subreddit.display_name,
                        "author": author_name,
                        "flair": post.link_flair_text,
                        "timestamp": datetime.fromtimestamp(
                            post.created_utc,
                            timezone.utc
                        ).isoformat()
                    }
                    
                    self.alert_queue.put(post_data)
                    new_finds += 1
                    
                    self._update_stat("giveaways_found", lambda x: x + 1)
                        
                except Exception as e:
                    logging.exception(f"[FAIL] Error processing post {post.id}: {e}")
        
        return new_finds

    def _fetch_cycle(self, limit: int) -> int:
        """
        Fetch posts from Reddit and process them.
        
        Args:
            limit: Maximum number of posts to fetch.
            
        Returns:
            Number of new giveaways found.
        """
        try:
            with self.reddit_lock:
                if not self.reddit:
                    if not self._connect_reddit():
                        return 0
                
                subreddit = self.reddit.subreddit(config.SUBREDDIT)
                posts = subreddit.new(limit=limit)
            
            new_finds = self._process_posts(posts)
            
            self._set_stat("last_check", datetime.now())
            
            return new_finds
            
        except RedditAPIException as e:
            if 'RATELIMIT' in str(e):
                logging.warning("[WARN] Reddit rate limit hit. Waiting 60s...")
                time.sleep(60)
            else:
                logging.warning(f"[WARN] Reddit API Error: {e}")
            return 0
            
        except PRAWException as e:
            logging.error(f"[WARN] Reddit connection lost: {e}")
            self._close_reddit()
            # Attempt reconnection
            if self._connect_reddit():
                logging.info("[OK] Reconnected to Reddit")
            return 0
            
        except Exception as e:
            logging.exception(f"[FAIL] Unexpected error during fetch: {e}")
            return 0

    def run(self) -> None:
        """
        Main monitoring loop.
        
        Continuously fetches posts from Reddit, alternating between fast checks
        (recent posts) and deep checks (more historical posts). Saves state
        periodically and monitors worker health.
        """
        logging.info("=" * 50)
        logging.info("Monitor Started")
        logging.info("=" * 50)
        
        # Start Discord worker thread
        self._start_discord_worker()
        
        last_deep_check = 0
        last_save = time.time()
        last_health_check = time.time()
        
        self._set_stat("status", "Running")

        while not self._stop_event.is_set():
            # Check worker health periodically
            if time.time() - last_health_check > config.WORKER_HEALTH_CHECK_INTERVAL:
                self._check_worker_health()
                last_health_check = time.time()
            
            # Check pause state
            with self._pause_lock:
                is_paused = self._is_paused
            
            if not is_paused:
                # Fast check (recent posts)
                self._fetch_cycle(config.FAST_LIMIT)

                # Periodic deep check
                if time.time() - last_deep_check > config.DEEP_INTERVAL:
                    self._fetch_cycle(config.DEEP_LIMIT)
                    last_deep_check = time.time()

                # Periodic save
                if time.time() - last_save > config.SAVE_INTERVAL:
                    self.save_state()
                    last_save = time.time()
            
            # Wait for next cycle or stop event
            self._stop_event.wait(timeout=config.FAST_INTERVAL)

        # Graceful shutdown
        logging.info("Waiting for Discord queue to empty...")
        
        # Wait for queue with timeout to prevent hanging
        start_wait = time.time()
        while not self.alert_queue.empty() and (time.time() - start_wait) < config.SHUTDOWN_TIMEOUT:
            time.sleep(0.5)
        
        self.save_state()
        self._save_shutdown_time()
        self._close_reddit()
        
        logging.info("=" * 50)
        logging.info("Monitor Stopped")
        logging.info("=" * 50)

    def stop(self) -> None:
        """Signal the monitor to stop gracefully."""
        logging.info("Stop signal received")
        self._stop_event.set()

    def toggle_pause(self) -> bool:
        """
        Toggle the pause state of the monitor.
        
        Returns:
            True if now paused, False if now running.
        """
        with self._pause_lock:
            self._is_paused = not self._is_paused
            is_paused = self._is_paused
            state = "Paused" if is_paused else "Running"
        
        logging.info(f"State changed to: {state}")
        
        self._set_stat("status", state)
        
        return is_paused

    def get_info(self) -> str:
        """
        Get current status information as a formatted string.
        
        Returns:
            Multi-line string with current statistics.
        """
        with self.stats_lock:
            uptime = int(time.time() - self.stats["start_time"])
            q_size = self.alert_queue.qsize()
            
            last_check = self.stats.get("last_check")
            if last_check:
                if isinstance(last_check, datetime):
                    last_check_str = last_check.strftime("%H:%M:%S")
                else:
                    last_check_str = str(last_check)
            else:
                last_check_str = "Never"
            
            return (
                f"Status: {self.stats['status']}\n"
                f"Uptime: {uptime // 3600}h {(uptime % 3600) // 60}m\n"
                f"Last Check: {last_check_str}\n"
                f"Checked: {self.stats['total_checked']}\n"
                f"Found: {self.stats['giveaways_found']}\n"
                f"Failed: {self.stats['failed_notifications']}\n"
                f"Reconnects: {self.stats['reddit_reconnects']}\n"
                f"Worker Restarts: {self.stats['worker_restarts']}\n"
                f"Queue: {q_size}\n"
                f"Seen Posts: {len(self.seen_posts_set)}"
            )


# ------------------ SYSTEM TRAY GUI ------------------
def create_tray_icon() -> Image.Image:
    """
    Create a system tray icon image.
    
    Returns:
        PIL Image object for the tray icon.
    """
    img = Image.new("RGB", (64, 64), "black")
    d = ImageDraw.Draw(img)
    d.rectangle((16, 16, 48, 48), fill="#FF4500")  # Reddit orange
    d.ellipse((22, 22, 42, 42), fill="white")
    return img


def _run_with_tray(bot: GiveawayMonitor, monitor_thread: threading.Thread) -> None:
    """
    Run with system tray icon (GUI mode).
    
    Args:
        bot: GiveawayMonitor instance
        monitor_thread: Thread running the monitor
    """
    if not HAS_TRAY:
        logging.error("[FAIL] pystray not available but tray mode was requested")
        return
    
    def on_toggle(icon, item):
        """Handle pause/resume toggle from tray menu."""
        is_paused = bot.toggle_pause()
        icon.title = f"Reddit Monitor - {'Paused' if is_paused else 'Running'}"

    def on_quit(icon, item):
        """Handle quit action from tray menu."""
        try:
            icon.notify("Shutting down...", "Giveaway Monitor")
        except:
            pass  # Notifications may not be supported
        bot.stop()
        monitor_thread.join(timeout=config.THREAD_JOIN_TIMEOUT)
        icon.stop()

    def on_status(icon, item):
        """Handle status request from tray menu."""
        status_info = bot.get_info()
        logging.info(f"\n{'='*40}\nSTATUS:\n{status_info}\n{'='*40}")
        try:
            icon.notify(status_info, "Giveaway Monitor Status")
        except:
            pass  # Notifications may not be supported

    menu = pystray.Menu(
        pystray.MenuItem("Toggle Pause/Resume", on_toggle),
        pystray.MenuItem("Show Status", on_status),
        pystray.MenuItem("Quit", on_quit)
    )

    icon = pystray.Icon(
        "GiveawayMonitor",
        create_tray_icon(),
        "Reddit Monitor - Running",
        menu
    )
    
    logging.info("[OK] System Tray Icon Started")
    logging.info("Right-click the tray icon for options")
    icon.run()


def _run_console_mode(bot: GiveawayMonitor, monitor_thread: threading.Thread) -> None:
    """
    Run in console mode with signal handlers (headless mode).
    
    Args:
        bot: GiveawayMonitor instance
        monitor_thread: Thread running the monitor
    """
    def signal_handler(signum, frame):
        """Handle termination signals."""
        logging.info(f"Received signal {signum}")
        bot.stop()
    
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logging.info("=" * 60)
    logging.info("Running in CONSOLE MODE")
    logging.info("=" * 60)
    logging.info("Press Ctrl+C to stop")
    logging.info("")
    logging.info("To view status, send SIGUSR1 signal:")
    logging.info(f"  kill -USR1 {os.getpid()}")
    logging.info("=" * 60)
    
    # Optional: Handle SIGUSR1 for status display
    def show_status(signum, frame):
        """Display status when SIGUSR1 is received."""
        status_info = bot.get_info()
        logging.info(f"\n{'='*40}\nSTATUS:\n{status_info}\n{'='*40}\n")
    
    try:
        signal.signal(signal.SIGUSR1, show_status)
    except AttributeError:
        # SIGUSR1 not available on Windows
        pass
    
    try:
        # Keep main thread alive
        while monitor_thread.is_alive():
            monitor_thread.join(timeout=1.0)
    except KeyboardInterrupt:
        logging.info("Keyboard interrupt received")
        bot.stop()
    finally:
        monitor_thread.join(timeout=config.THREAD_JOIN_TIMEOUT)


def check_display_available() -> bool:
    """
    Check if a display is available for GUI mode.
    
    Returns:
        True if display is available, False otherwise.
    """
    if sys.platform.startswith('linux'):
        display = os.environ.get('DISPLAY')
        wayland_display = os.environ.get('WAYLAND_DISPLAY')
        return bool(display or wayland_display)
    return True  # Assume available on non-Linux


def main() -> None:
    """
    Main entry point - initializes monitor and chooses appropriate UI mode.
    
    Automatically detects:
    - If pystray is available
    - If running in a GUI environment (DISPLAY set)
    - If running on a headless server
    
    Then starts in the appropriate mode (tray or console).
    """
    print("=" * 60)
    print("Reddit Giveaway Monitor")
    print("=" * 60)
    
    # Determine which mode to use
    use_tray = False
    
    if HAS_TRAY:
        if check_display_available():
            use_tray = True
            mode = "System Tray (GUI)"
        else:
            mode = "Console (No Display Detected)"
    else:
        mode = "Console (pystray not installed)"
    
    print(f"Mode: {mode}")
    print(f"Monitoring: r/{config.SUBREDDIT}")
    print("=" * 60)
    print()
    
    # Initialize monitor
    bot = GiveawayMonitor()
    
    # Start monitor thread
    monitor_thread = threading.Thread(
        target=bot.run,
        daemon=True,
        name="MonitorThread"
    )
    monitor_thread.start()
    
    # Run in appropriate mode
    if use_tray:
        _run_with_tray(bot, monitor_thread)
    else:
        _run_console_mode(bot, monitor_thread)


if __name__ == "__main__":
    main()
