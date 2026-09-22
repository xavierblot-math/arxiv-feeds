name: Update arXiv feeds

on:
  schedule:
    - cron: "30 4 * * *"   # every day at 04:30 UTC
  workflow_dispatch:        # allows a manual "Run workflow" button

permissions:
  contents: write

jobs:
  update:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
      - name: Build feeds
        run: python3 fetch_feeds.py
      - name: Commit feeds
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add feeds
          git diff --cached --quiet || git commit -m "Update feeds"
          git push
