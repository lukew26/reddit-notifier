import praw
import requests

# Reddit API credentials (fill after approval)
reddit = praw.Reddit(
    client_id="CLIENT_ID",
    client_secret="CLIENT_SECRET",
    username="YOUR_REDDIT_USERNAME",
    password="YOUR_REDDIT_PASSWORD",
    user_agent="giveaway-notifier/1.0"
)

DISCORD_WEBHOOK = "YOUR_DISCORD_WEBHOOK_URL"
SUBREDDIT = "exampleSubreddit"
KEYWORDS = ["giveaway", "win", "free", "contest", "raffle"]

def send_discord(post):
    data = {
        "content": f"🎁 Giveaway: {post.title}\nhttps://reddit.com{post.permalink}"
    }
    requests.post(DISCORD_WEBHOOK, json=data)

def is_giveaway(post):
    text = (post.title + " " + (post.selftext or "")).lower()
    return any(k in text for k in KEYWORDS)

for post in reddit.subreddit(SUBREDDIT).new(limit=10):
    if is_giveaway(post):
        send_discord(post)
