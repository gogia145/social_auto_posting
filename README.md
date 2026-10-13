# Free Trending Topic → Facebook Page Automation

This starter project uses public RSS feeds (no X API required), runs on GitHub Actions, and can publish short headline-based captions to a Facebook Page through Meta's Graph API.

## Important limitations
- This is not an X scraper and does not collect every viral social post. It reads public RSS feeds from news publishers.
- The caption builder is a simple template, not an AI model. It avoids copying article bodies, but headlines and summaries can still have rights or licensing considerations. Link to the source and only publish content you are allowed to use.
- RSS feeds can change, block automated access, or be unavailable. Remove feeds that do not work or whose terms do not allow your intended use.
- Facebook publishing requires a Facebook Page, a Meta developer app, the correct permissions, and a valid Page access token. API access is subject to Meta's terms.
- Instagram publishing is not included in this starter because Instagram's publishing API has extra account, permission, and media requirements.
- Start with `DRY_RUN=true`; inspect the log before enabling live posting. Automated news publishing can spread inaccurate or outdated information, so consider manual review.

## Setup
1. Create a GitHub repository and upload all files in this folder, preserving `.github/workflows/autopost.yml`.
2. In GitHub, open **Settings → Secrets and variables → Actions**.
3. Add repository secrets:
   - `FACEBOOK_PAGE_ID`
   - `FACEBOOK_PAGE_ACCESS_TOKEN`
4. Add repository variables:
   - `DRY_RUN` = `true`
   - `MAX_POSTS_PER_RUN` = `3`
   - `META_GRAPH_API_VERSION` = `v26.0` (change if Meta's current supported version differs)
5. Go to **Actions → Free Trending Topic Publisher → Run workflow**. Review the logs. While `DRY_RUN=true`, nothing is published.
6. When satisfied, change `DRY_RUN` to `false`. The workflow is scheduled hourly at minute 17 UTC. GitHub-hosted runner availability and free quotas apply.

## Permissions and token safety
Never paste access tokens into source code or chat. Store them only as GitHub Actions secrets. Use the minimum permissions needed, and rotate a token if it is exposed.

## Feed list
Edit `RSS_FEEDS` in `main.py` to add or remove RSS feeds. Check each publisher's terms and feed usage rules before using it for automated commercial or high-volume publishing.

## How duplicates are handled
Successfully published item IDs are saved in `posted_items.json` and committed back to the repository. The GitHub workflow's write permission is used for this. Dry runs do not mark items as posted.

## Safer operating mode
For a page that represents a business or public brand, we recommend keeping `DRY_RUN=true` until you have reviewed the output and then using manual approval or a moderation step for sensitive news.
