"""Column layout of every tab in the "GTA6 Clipper" Google Sheet.

Agents write rows in exactly this column order. To add a column, append it at the END of
the list (never insert or rename), then run the "Setup sheet" workflow again: it adds the
new header without touching existing data.
"""

TABS = {
    # Scout output: trending videos worth a look.
    "Candidates": [
        "video_id", "url", "title", "channel_id", "channel_title", "published_at",
        "kind", "duration_s", "views", "subscribers", "age_h", "views_per_hour",
        "outlier_score", "is_watchlist", "campaign_lead", "campaign_link",
        "found_at", "status", "score", "found_by", "updated_at", "signals_at", "sat_checked_at", "sat_shorts",
        "channel_handle",
    ],
    # Tracker summary: one row per tracked video, latest numbers.
    "Tracker": [
        "video_id", "title", "channel_title", "kind", "age_h", "views", "likes",
        "comments", "velocity", "acceleration", "engagement", "reach_score",
        "label", "snapshots", "last_snapshot_at", "tracking", "url",
    ],
    # Signals + Watcher output: candidate moments inside videos.
    "Moments": [
        "moment_id", "video_id", "start_s", "end_s", "source", "signal", "strength",
        "reason", "content_type", "style_tags", "suggested_brand", "has_promo",
        "gate_status", "created_at", "quality", "standalone", "hook_idea", "title",
        "channel_title", "url", "watched_at", "saturation", "gate_note",
    ],
    # Human-readable view of the active Playbook rules.
    "Playbook": [
        "rule_id", "section", "rule", "value", "source", "updated_at",
    ],
    # Decider output (Claude in chat), read by the Colab Fetcher.
    "Batch": [
        "batch_id", "moment_id", "video_id", "start_s", "end_s", "brand",
        "content_type", "hook_text", "title_label", "title", "description",
        "hashtags", "creator_credit", "approved", "fetch_status", "render_status",
        "output_path", "rendered_at", "template_version", "ig_caption",
    ],
    # Owner logs every post here; Analyst fills in results.
    "Posted": [
        "post_id", "batch_id", "moment_id", "brand", "platform", "url", "posted_at",
        "views_24h", "views_7d", "likes_7d", "comments_7d", "final_label",
        "platform_id", "views_now", "likes_now", "comments_now", "stats_at",
        "views_6h", "views_12h", "views_48h",
    ],
    # Trending words/phrases in fast-growing GTA 6 titles (Scout, every run).
    "Keywords": [
        "phrase", "score", "videos", "prev_score", "trend", "example_title",
        "first_seen", "updated_at",
    ],
    # Winning Shorts to copy a style from, per editing trick (Researcher, lib/examples.py).
    "Examples": [
        "example_id", "category", "rank", "have_skill", "url", "title", "channel_title", "views",
        "breakout", "age_days", "style", "updated_at", "trick_rank", "trick_use",
    ],
    # Live chat counted while a stream is live (Apps Script pollLive): reactions per 10 s bin.
    "LiveChat": ["key", "video_id", "bin_s", "reactions", "messages", "updated_at"],
    # Researcher notes on top/average GTA Shorts.
    "Research": [
        "research_id", "video_id", "url", "label", "views", "age_days", "hook_type",
        "length_s", "caption_style", "title", "hashtags", "notes", "analyzed_at",
        "channel_title", "subscribers", "published_at", "collected_at", "score", "features",
        "model", "credits",
        "fresh_label", "views_per_hour", "fresh_at",
        "channel_median_views", "breakout", "baseline_source",
    ],
}
