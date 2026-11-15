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
        "found_at", "status", "score", "found_by", "updated_at",
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
        "gate_status", "created_at",
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
    ],
    # Owner logs every post here; Analyst fills in results.
    "Posted": [
        "post_id", "batch_id", "moment_id", "brand", "platform", "url", "posted_at",
        "views_24h", "views_7d", "likes_7d", "comments_7d", "final_label",
    ],
    # Researcher notes on top/average GTA Shorts.
    "Research": [
        "research_id", "video_id", "url", "label", "views", "age_days", "hook_type",
        "length_s", "caption_style", "title", "hashtags", "notes", "analyzed_at",
    ],
}
